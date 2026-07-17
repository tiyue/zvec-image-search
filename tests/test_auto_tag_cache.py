from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from image_vector_service.annotation_service import (
    AUTO_TAGGING_SCHEMA_VERSION,
    FLASH_MODEL,
    PLUS_MODEL,
    PROMPT_VERSION,
    _cache_key,
)
from image_vector_service.auto_tag_cache import (
    AutoTagCacheFlightError,
    AutoTagCacheMigrationError,
    SharedAutoTagCache,
    make_auto_tag_cache_key,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.service import ImageVectorService
from image_vector_service.state import IndexState


class _FakeRepository:
    collection_uuid = "cache-migration-test-collection"
    created = False
    doc_count = 0

    def set_tag_catalog(self, _tags: list[str], **_kwargs: object) -> None:
        return None


class AutoTagCacheSingleFlightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.shared_path = (
            Path(self.temporary_directory.name) / "results" / "auto_tag_cache.sqlite3"
        )
        self.digest = "9" * 64
        self.cache_key = make_auto_tag_cache_key(
            self.digest,
            FLASH_MODEL,
            PROMPT_VERSION,
            AUTO_TAGGING_SCHEMA_VERSION,
        )
        # Create the schema before racing independent connections; the behavior
        # under test is cache-key single-flight, not concurrent database bootstrap.
        cache = SharedAutoTagCache(self.shared_path)
        cache.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_concurrent_instances_issue_only_one_model_call_and_share_result(
        self,
    ) -> None:
        both_missed = threading.Barrier(2)
        leader_started = threading.Event()
        allow_leader_to_finish = threading.Event()
        calls_lock = threading.Lock()
        model_calls = 0

        def resolve() -> tuple[bool, dict[str, Any] | None]:
            nonlocal model_calls
            cache = SharedAutoTagCache(self.shared_path)
            try:
                self.assertIsNone(cache.get(self.cache_key))
                both_missed.wait(timeout=5)
                flight = cache.claim(self.cache_key)
                if not flight.is_leader:
                    return False, cache.wait_for_result(
                        flight,
                        poll_interval=0.01,
                    )

                with flight:
                    # Re-read after claiming so a commit between the first miss and
                    # the claim cannot trigger a duplicate remote request.
                    cached = cache.get(self.cache_key)
                    if cached is not None:
                        return True, cached
                    with calls_lock:
                        model_calls += 1
                    leader_started.set()
                    self.assertTrue(allow_leader_to_finish.wait(timeout=5))
                    cache.set(
                        cache_key=self.cache_key,
                        sha256=self.digest,
                        model=FLASH_MODEL,
                        prompt_version=PROMPT_VERSION,
                        schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                        status="succeeded",
                        annotation={"description": "single shared response"},
                    )
                    return True, cache.get(self.cache_key)
            finally:
                cache.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resolve) for _index in range(2)]
            self.assertTrue(leader_started.wait(timeout=5))
            allow_leader_to_finish.set()
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertEqual(model_calls, 1)
        self.assertEqual(sum(1 for leader, _item in outcomes if leader), 1)
        for _leader, item in outcomes:
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(
                item["annotation"],
                {"description": "single shared response"},
            )

    def test_follower_wait_is_cancellable_without_releasing_the_leader(self) -> None:
        leader_cache = SharedAutoTagCache(self.shared_path)
        leader = leader_cache.claim(self.cache_key)
        self.assertTrue(leader.is_leader)
        follower_started = threading.Event()
        cancel = threading.Event()

        class ExpectedCancellation(RuntimeError):
            pass

        def wait_as_follower() -> None:
            follower_cache = SharedAutoTagCache(self.shared_path)
            try:
                follower = follower_cache.claim(self.cache_key)
                self.assertFalse(follower.is_leader)
                follower_started.set()

                def cancel_check() -> None:
                    if cancel.is_set():
                        raise ExpectedCancellation("cancelled by test")

                follower_cache.wait_for_result(
                    follower,
                    cancel_check=cancel_check,
                    poll_interval=0.01,
                )
            finally:
                follower_cache.close()

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(wait_as_follower)
                self.assertTrue(follower_started.wait(timeout=5))
                cancel.set()
                with self.assertRaises(ExpectedCancellation):
                    future.result(timeout=5)

            still_follower = leader_cache.claim(self.cache_key)
            self.assertFalse(still_follower.is_leader)
        finally:
            leader.release()
            leader_cache.close()

        next_cache = SharedAutoTagCache(self.shared_path)
        try:
            next_flight = next_cache.claim(self.cache_key)
            self.assertTrue(next_flight.is_leader)
            next_flight.release()
        finally:
            next_cache.close()

    def test_context_manager_releases_after_leader_exception(self) -> None:
        first_cache = SharedAutoTagCache(self.shared_path)
        second_cache = SharedAutoTagCache(self.shared_path)
        try:
            first = first_cache.claim(self.cache_key)
            follower = second_cache.claim(self.cache_key)
            self.assertTrue(first.is_leader)
            self.assertFalse(follower.is_leader)

            with self.assertRaisesRegex(RuntimeError, "injected failure"), first:
                raise RuntimeError("injected failure")

            self.assertIsNone(
                second_cache.wait_for_result(follower, poll_interval=0.01)
            )
            replacement = second_cache.claim(self.cache_key)
            self.assertTrue(replacement.is_leader)
            replacement.release()
        finally:
            first_cache.close()
            second_cache.close()

    def test_wait_rejects_a_claim_from_another_database(self) -> None:
        first_cache = SharedAutoTagCache(self.shared_path)
        second_cache = SharedAutoTagCache(
            self.shared_path.parent / "other-auto-tag-cache.sqlite3"
        )
        flight = first_cache.claim(self.cache_key)
        try:
            with self.assertRaises(AutoTagCacheFlightError):
                second_cache.wait_for_result(flight)
        finally:
            flight.release()
            first_cache.close()
            second_cache.close()

    def test_late_failed_writer_cannot_downgrade_concurrent_success(self) -> None:
        writers_ready = threading.Barrier(2)
        success_committed = threading.Event()

        def write_success() -> None:
            cache = SharedAutoTagCache(self.shared_path)
            try:
                writers_ready.wait(timeout=5)
                cache.set(
                    cache_key=self.cache_key,
                    sha256=self.digest,
                    model=FLASH_MODEL,
                    prompt_version=PROMPT_VERSION,
                    schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                    status="succeeded",
                    annotation={"description": "durable success"},
                    request_id="successful-request",
                    usage={"total_tokens": 42},
                    cost_yuan=0.02,
                )
                success_committed.set()
            finally:
                cache.close()

        def write_late_failure() -> None:
            cache = SharedAutoTagCache(self.shared_path)
            try:
                writers_ready.wait(timeout=5)
                self.assertTrue(success_committed.wait(timeout=5))
                cache.set(
                    cache_key=self.cache_key,
                    sha256=self.digest,
                    model=FLASH_MODEL,
                    prompt_version=PROMPT_VERSION,
                    schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                    status="failed",
                    request_id="late-failed-request",
                    error="injected late failure",
                )
            finally:
                cache.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            success = executor.submit(write_success)
            failure = executor.submit(write_late_failure)
            success.result(timeout=5)
            failure.result(timeout=5)

        cache = SharedAutoTagCache(self.shared_path)
        try:
            current = _required_cache_item(cache, self.cache_key)
            self.assertEqual(current["status"], "succeeded")
            self.assertEqual(current["annotation"], {"description": "durable success"})
            self.assertEqual(current["request_id"], "successful-request")
            self.assertEqual(current["usage"], {"total_tokens": 42})
            self.assertEqual(current["cost_yuan"], 0.02)
            self.assertEqual(current["error"], "")
            self.assertEqual(current["attempts"], 2)
        finally:
            cache.close()


class AutoTagCacheMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        # Zvec owns a process-wide file logger on Windows; it can keep the first test
        # log open until process exit even after ImageVectorService closes.
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary_directory.name)
        self.legacy_path = self.root / "legacy-state.sqlite3"
        self.shared_path = self.root / "results" / "auto_tag_cache.sqlite3"
        _create_legacy_cache(self.legacy_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_key_format_matches_annotation_coordinator_and_versions_are_isolated(
        self,
    ) -> None:
        digest = "a" * 64
        current = make_auto_tag_cache_key(
            digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
        )
        self.assertEqual(current, _cache_key(digest, FLASH_MODEL))

        identities = {
            current,
            make_auto_tag_cache_key(
                digest, PLUS_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
            ),
            make_auto_tag_cache_key(
                digest, FLASH_MODEL, "older-prompt", AUTO_TAGGING_SCHEMA_VERSION
            ),
            make_auto_tag_cache_key(
                digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION - 1
            ),
        }
        self.assertEqual(len(identities), 4)

    def test_migration_rekeys_source_row_and_repeated_run_is_idempotent(self) -> None:
        digest = "b" * 64
        _insert_legacy(
            self.legacy_path,
            digest=digest,
            cache_key="legacy-key-format",
            annotation={"description": "旧缓存结果"},
            attempts=4,
        )
        cache = SharedAutoTagCache(self.shared_path)
        try:
            first = cache.migrate_legacy_database(self.legacy_path)
            expected_key = make_auto_tag_cache_key(
                digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
            )
            migrated = _required_cache_item(cache, expected_key)
            self.assertEqual(first.imported, 1)
            self.assertEqual(first.skipped, 0)
            self.assertEqual(migrated["annotation"]["description"], "旧缓存结果")
            self.assertEqual(migrated["attempts"], 4)

            second = cache.migrate_legacy_database(self.legacy_path)
            self.assertTrue(second.already_current)
            self.assertEqual(second.changed, 0)
            self.assertEqual(_required_cache_item(cache, expected_key)["attempts"], 4)
        finally:
            cache.close()

        # Migration is deliberately read-only: even a noncanonical legacy key remains
        # untouched so downgrade/recovery is possible.
        with closing(sqlite3.connect(self.legacy_path)) as connection:
            source_key = connection.execute(
                "SELECT cache_key FROM auto_tag_cache"
            ).fetchone()[0]
        self.assertEqual(source_key, "legacy-key-format")

    def test_model_prompt_and_schema_rows_do_not_cross_use(self) -> None:
        digest = "c" * 64
        identities = (
            (FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION, "flash"),
            (PLUS_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION, "plus"),
            (FLASH_MODEL, "older-prompt", AUTO_TAGGING_SCHEMA_VERSION, "prompt"),
            (FLASH_MODEL, PROMPT_VERSION, 1, "schema"),
        )
        for model, prompt, schema, description in identities:
            _insert_legacy(
                self.legacy_path,
                digest=digest,
                model=model,
                prompt_version=prompt,
                schema_version=schema,
                annotation={"description": description},
            )

        cache = SharedAutoTagCache(self.shared_path)
        try:
            report = cache.migrate_legacy_database(self.legacy_path)
            self.assertEqual(report.imported, 4)
            for model, prompt, schema, description in identities:
                key = make_auto_tag_cache_key(digest, model, prompt, schema)
                self.assertEqual(
                    _required_cache_item(cache, key)["annotation"]["description"],
                    description,
                )
        finally:
            cache.close()

    def test_existing_success_is_not_overwritten_or_counted_as_an_attempt(self) -> None:
        digest = "d" * 64
        key = make_auto_tag_cache_key(
            digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
        )
        _insert_legacy(
            self.legacy_path,
            digest=digest,
            annotation={"description": "较旧的图库结果"},
            attempts=8,
        )
        cache = SharedAutoTagCache(self.shared_path)
        try:
            cache.set(
                cache_key=key,
                sha256=digest,
                model=FLASH_MODEL,
                prompt_version=PROMPT_VERSION,
                schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                status="succeeded",
                annotation={"description": "共享缓存现有结果"},
            )
            report = cache.migrate_legacy_database(self.legacy_path)
            current = _required_cache_item(cache, key)
            self.assertEqual(report.unchanged, 1)
            self.assertEqual(current["annotation"]["description"], "共享缓存现有结果")
            self.assertEqual(current["attempts"], 1)

            again = cache.migrate_legacy_database(self.legacy_path)
            self.assertTrue(again.already_current)
            self.assertEqual(_required_cache_item(cache, key)["attempts"], 1)
        finally:
            cache.close()

    def test_late_failure_cannot_downgrade_a_successful_shared_entry(self) -> None:
        digest = "f" * 64
        key = make_auto_tag_cache_key(
            digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
        )
        cache = SharedAutoTagCache(self.shared_path)
        try:
            cache.set(
                cache_key=key,
                sha256=digest,
                model=FLASH_MODEL,
                prompt_version=PROMPT_VERSION,
                schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                status="succeeded",
                annotation={"description": "usable result"},
                request_id="success-request",
                usage={"total_tokens": 100},
                cost_yuan=0.01,
            )
            cache.set(
                cache_key=key,
                sha256=digest,
                model=FLASH_MODEL,
                prompt_version=PROMPT_VERSION,
                schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                status="failed",
                request_id="late-failure",
                error="late concurrent failure",
            )

            current = _required_cache_item(cache, key)
            self.assertEqual(current["status"], "succeeded")
            self.assertEqual(current["annotation"], {"description": "usable result"})
            self.assertEqual(current["request_id"], "success-request")
            self.assertEqual(current["usage"], {"total_tokens": 100})
            self.assertEqual(current["cost_yuan"], 0.01)
            self.assertEqual(current["error"], "")
            self.assertEqual(current["attempts"], 2)
        finally:
            cache.close()

    def test_legacy_success_upgrades_shared_failure_without_new_attempt(self) -> None:
        digest = "e" * 64
        key = make_auto_tag_cache_key(
            digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
        )
        _insert_legacy(
            self.legacy_path,
            digest=digest,
            annotation={"description": "可复用结果"},
            attempts=3,
        )
        cache = SharedAutoTagCache(self.shared_path)
        try:
            cache.set(
                cache_key=key,
                sha256=digest,
                model=FLASH_MODEL,
                prompt_version=PROMPT_VERSION,
                schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                status="failed",
                error="temporary failure",
            )
            report = cache.migrate_legacy_database(self.legacy_path)
            current = _required_cache_item(cache, key)
            self.assertEqual(report.upgraded, 1)
            self.assertEqual(current["status"], "succeeded")
            self.assertEqual(current["annotation"]["description"], "可复用结果")
            self.assertEqual(current["attempts"], 3)
        finally:
            cache.close()

    def test_destination_error_rolls_back_and_source_can_be_retried(self) -> None:
        first_digest = "1" * 64
        failing_digest = "2" * 64
        _insert_legacy(self.legacy_path, digest=first_digest)
        _insert_legacy(self.legacy_path, digest=failing_digest)
        cache = SharedAutoTagCache(self.shared_path)
        cache.connection.execute(
            "CREATE TRIGGER reject_test_row BEFORE INSERT ON auto_tag_cache "
            f"WHEN NEW.sha256 = '{failing_digest}' BEGIN "
            "SELECT RAISE(ABORT, 'injected migration failure'); END"
        )
        cache.connection.commit()
        try:
            with self.assertRaises(AutoTagCacheMigrationError):
                cache.migrate_legacy_database(self.legacy_path)

            first_key = make_auto_tag_cache_key(
                first_digest,
                FLASH_MODEL,
                PROMPT_VERSION,
                AUTO_TAGGING_SCHEMA_VERSION,
            )
            self.assertIsNone(cache.get(first_key))
            marker_count = cache.connection.execute(
                "SELECT COUNT(*) FROM legacy_auto_tag_cache_migrations"
            ).fetchone()[0]
            self.assertEqual(marker_count, 0)

            cache.connection.execute("DROP TRIGGER reject_test_row")
            cache.connection.commit()
            retried = cache.migrate_legacy_database(self.legacy_path)
            self.assertEqual(retried.imported, 2)
        finally:
            cache.close()

        with closing(sqlite3.connect(self.legacy_path)) as source:
            self.assertEqual(
                source.execute("SELECT COUNT(*) FROM auto_tag_cache").fetchone()[0],
                2,
            )

    def test_service_startup_migrates_collection_cache_once(self) -> None:
        workspace = self.root / "workspace"
        results = self.root / "shared-results"
        state = IndexState(workspace / "image_collection.state.sqlite3")
        digest = "f" * 64
        key = make_auto_tag_cache_key(
            digest, FLASH_MODEL, PROMPT_VERSION, AUTO_TAGGING_SCHEMA_VERSION
        )
        state.set_auto_tag_cache(
            cache_key=key,
            sha256=digest,
            model=FLASH_MODEL,
            prompt_version=PROMPT_VERSION,
            schema_version=AUTO_TAGGING_SCHEMA_VERSION,
            status="succeeded",
            annotation={"description": "启动时迁移"},
        )
        state.close()

        config = ServiceConfig(workspace=workspace, results_directory=results)
        first = ImageVectorService(config=config, repository=_FakeRepository())
        try:
            self.assertEqual(first.auto_tag_cache_migration.imported, 1)
            self.assertEqual(
                _required_cache_item(first.auto_tag_cache, key)["annotation"][
                    "description"
                ],
                "启动时迁移",
            )
        finally:
            first.close()

        second = ImageVectorService(config=config, repository=_FakeRepository())
        try:
            self.assertTrue(second.auto_tag_cache_migration.already_current)
            self.assertEqual(
                _required_cache_item(second.auto_tag_cache, key)["attempts"], 1
            )
        finally:
            second.close()


def _create_legacy_cache(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE auto_tag_cache (
                cache_key TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                annotation_json TEXT,
                request_id TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}',
                cost_yuan REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX idx_auto_tag_cache_identity
                ON auto_tag_cache(sha256, model, prompt_version, schema_version);
            """
        )


def _required_cache_item(cache: SharedAutoTagCache, cache_key: str) -> dict[str, Any]:
    item = cache.get(cache_key)
    if item is None:
        raise AssertionError(f"Expected auto-tag cache item: {cache_key}")
    return item


def _insert_legacy(
    path: Path,
    *,
    digest: str,
    model: str = FLASH_MODEL,
    prompt_version: str = PROMPT_VERSION,
    schema_version: int = AUTO_TAGGING_SCHEMA_VERSION,
    cache_key: str | None = None,
    status: str = "succeeded",
    annotation: dict[str, object] | None = None,
    attempts: int = 1,
) -> None:
    key = cache_key or make_auto_tag_cache_key(
        digest, model, prompt_version, schema_version
    )
    payload = annotation if annotation is not None else {"description": digest[:4]}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO auto_tag_cache("
            "cache_key, sha256, model, prompt_version, schema_version, status, "
            "annotation_json, usage_json, attempts) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                digest,
                model,
                prompt_version,
                schema_version,
                status,
                json.dumps(payload, ensure_ascii=False),
                json.dumps({"input_tokens": 10}),
                attempts,
            ),
        )


if __name__ == "__main__":
    unittest.main()
