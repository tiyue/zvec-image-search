from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from image_vector_service.activity_store import (
    ACTIVITY_SCHEMA_VERSION,
    ActivityStore,
    ActivityStoreUnavailable,
    InvalidActivityCursor,
    JobHistoryRecord,
)


class ActivityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config_home = self.root / "config-home"
        self.stores: list[ActivityStore] = []

    def tearDown(self) -> None:
        for store in reversed(self.stores):
            store.close()
        self.temporary.cleanup()

    def create_store(self, **options: object) -> ActivityStore:
        store = ActivityStore(self.config_home, **options)  # type: ignore[arg-type]
        self.stores.append(store)
        return store

    def test_creates_global_wal_schema_and_expected_indexes(self) -> None:
        store = self.create_store(auto_start=False)

        self.assertTrue(store.available)
        self.assertEqual(store.path, self.config_home / "activity.sqlite3")
        connection = sqlite3.connect(store.path)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            connection.close()

        self.assertEqual(journal_mode, "wal")
        self.assertEqual(user_version, ACTIVITY_SCHEMA_VERSION)
        self.assertIn("job_history", tables)
        self.assertIn("operation_logs", tables)
        self.assertIn("idx_operation_logs_time", indexes)
        self.assertIn("idx_operation_logs_level_category_time", indexes)
        self.assertIn("idx_operation_logs_library_time", indexes)
        self.assertIn("idx_operation_logs_job_time", indexes)

    def test_job_history_upsert_survives_restart_and_recovers_active_jobs(self) -> None:
        first = self.create_store()
        self.assertTrue(
            first.record_job(
                JobHistoryRecord(
                    job_id="job-running",
                    task_type="index_and_auto_tag",
                    library_id="library-a",
                    library_name="图库 A",
                    status="queued",
                    submitted_at="2026-07-18T10:00:00+00:00",
                    total=20,
                )
            )
        )
        self.assertTrue(
            first.record_job(
                {
                    "id": "job-running",
                    "command": "index_and_auto_tag",
                    "params": {"library_id": "library-a", "library_name": "图库 A"},
                    "status": "running",
                    "submitted_at": "2026-07-18T10:00:00+00:00",
                    "started_at": "2026-07-18T10:00:05+00:00",
                    "progress": {"processed": 7, "total": 20, "message": "索引中"},
                    "failure_count": 1,
                }
            )
        )
        self.assertTrue(first.flush())
        first.close()

        second = self.create_store(
            clock=lambda: datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(second.recovered_jobs, 1)
        page = second.list_job_history()
        self.assertEqual(page["total_count"], 1)
        recovered = page["items"][0]
        self.assertEqual(recovered["job_id"], "job-running")
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["processed"], 7)
        self.assertEqual(recovered["total"], 20)
        self.assertEqual(recovered["failed"], 1)
        self.assertEqual(recovered["error_code"], "backend_restarted")
        self.assertEqual(recovered["finished_at"], "2026-07-19T08:00:00.000+00:00")
        recovery_logs = second.list_operation_logs(category="backend")
        self.assertEqual(
            recovery_logs["items"][0]["event"],
            "jobs_interrupted_after_restart",
        )
        self.assertEqual(recovery_logs["items"][0]["details"]["recovered_jobs"], 1)

    def test_job_history_cursor_is_stable_and_active_jobs_sort_first(self) -> None:
        store = self.create_store()
        records = (
            JobHistoryRecord(
                "terminal-new",
                "sync",
                "succeeded",
                library_id="library-a",
                submitted_at="2026-07-19T10:00:00+00:00",
                finished_at="2026-07-19T10:02:00+00:00",
            ),
            JobHistoryRecord(
                "active-old",
                "index",
                "running",
                library_id="library-a",
                submitted_at="2026-07-18T10:00:00+00:00",
                started_at="2026-07-18T10:00:01+00:00",
            ),
            JobHistoryRecord(
                "terminal-old",
                "index",
                "failed",
                library_id="library-b",
                submitted_at="2026-07-17T10:00:00+00:00",
                finished_at="2026-07-17T10:02:00+00:00",
                error_code="index_failed",
                error_message="failed to index",
            ),
            JobHistoryRecord(
                "active-new",
                "auto_tag",
                "queued",
                library_id="library-b",
                submitted_at="2026-07-19T09:00:00+00:00",
            ),
            JobHistoryRecord(
                "terminal-mid",
                "sync",
                "cancelled",
                library_id="library-a",
                submitted_at="2026-07-18T12:00:00+00:00",
                finished_at="2026-07-18T12:01:00+00:00",
            ),
        )
        for record in records:
            self.assertTrue(store.record_job(record))
        self.assertTrue(store.flush())

        observed: list[str] = []
        cursor: str | None = None
        while True:
            page = store.list_job_history(cursor=cursor, limit=2)
            observed.extend(item["job_id"] for item in page["items"])
            self.assertEqual(page["total_count"], len(records))
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertIsInstance(cursor, str)

        self.assertEqual(
            observed,
            [
                "active-new",
                "active-old",
                "terminal-new",
                "terminal-mid",
                "terminal-old",
            ],
        )
        self.assertEqual(len(observed), len(set(observed)))
        filtered = store.list_job_history(
            status=["succeeded", "cancelled"],
            task_type="sync",
            library_id="library-a",
            query="terminal",
        )
        self.assertEqual(
            [item["job_id"] for item in filtered["items"]],
            ["terminal-new", "terminal-mid"],
        )
        with self.assertRaises(InvalidActivityCursor):
            store.list_job_history(cursor="not-a-cursor")

    def test_read_only_cluster_queries_are_hidden_from_job_history(self) -> None:
        store = self.create_store()
        records = (
            JobHistoryRecord("cluster-list-old", "cluster_list", "succeeded"),
            JobHistoryRecord("cluster-detail-old", "cluster_detail", "succeeded"),
            JobHistoryRecord("index-visible", "index", "succeeded"),
        )
        for record in records:
            self.assertTrue(store.record_job(record))
        self.assertTrue(store.flush())

        connection = sqlite3.connect(store.path)
        try:
            stored_count = connection.execute(
                "SELECT COUNT(*) FROM job_history"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(stored_count, 3)

        page = store.list_job_history()
        self.assertEqual(page["total_count"], 1)
        self.assertEqual(
            [item["job_id"] for item in page["items"]],
            ["index-visible"],
        )
        filtered = store.list_job_history(task_type="cluster_list")
        self.assertEqual(filtered["total_count"], 0)
        self.assertEqual(filtered["items"], [])

    def test_backend_job_progress_and_result_aliases_map_to_table_counts(self) -> None:
        store = self.create_store(auto_start=False)
        self.assertTrue(
            store.record_job(
                {
                    "id": "job-progress",
                    "command": "index",
                    "params": {"library_id": "library-a"},
                    "status": "running",
                    "submitted_at": "2026-07-19T09:00:00+00:00",
                    "progress": {
                        "current": 3,
                        "total": 8,
                        "message": "Embedded 3/8 unique images.",
                    },
                }
            )
        )
        self.assertTrue(
            store.record_job(
                {
                    "id": "job-auto-tag",
                    "command": "auto_tag",
                    "params": {"library_id": "library-a"},
                    "status": "partial",
                    "submitted_at": "2026-07-19T08:00:00+00:00",
                    "finished_at": "2026-07-19T08:10:00+00:00",
                    "result": {
                        "processed_count": 7,
                        "candidate_count": 9,
                        "failed_count": 2,
                    },
                }
            )
        )
        self.assertTrue(
            store.record_job(
                {
                    "id": "job-manual-tags",
                    "command": "manual_tag_batch",
                    "status": "succeeded",
                    "submitted_at": "2026-07-19T07:00:00+00:00",
                    "result": {
                        "completed": 12,
                        "total_count": 12,
                        "failed_count": 0,
                    },
                    "result_summary": "manual batch completed",
                }
            )
        )
        store.start()
        self.assertTrue(store.flush())

        items = {
            item["job_id"]: item for item in store.list_job_history(limit=10)["items"]
        }
        progress = items["job-progress"]
        self.assertEqual(progress["processed"], 3)
        self.assertEqual(progress["total"], 8)
        self.assertEqual(progress["progress"], 3 / 8)
        auto_tag = items["job-auto-tag"]
        self.assertEqual(auto_tag["processed"], 7)
        self.assertEqual(auto_tag["total"], 9)
        self.assertEqual(auto_tag["failed"], 2)
        self.assertEqual(auto_tag["progress"], 7 / 9)
        self.assertEqual(auto_tag["result_summary"]["processed_count"], 7)
        self.assertEqual(auto_tag["result_summary"]["candidate_count"], 9)
        manual = items["job-manual-tags"]
        self.assertEqual(manual["processed"], 12)
        self.assertEqual(manual["total"], 12)
        self.assertEqual(manual["failed"], 0)
        self.assertEqual(
            manual["result_summary"], {"summary": "manual batch completed"}
        )

    def test_operation_log_cursor_and_filters_have_no_duplicates(self) -> None:
        store = self.create_store()
        for index in range(7):
            self.assertTrue(
                store.log(
                    level="error" if index == 5 else "info",
                    category="image_failure" if index >= 5 else "search",
                    event=f"event_{index}",
                    source="backend" if index >= 5 else "facade",
                    message=f"event message {index}",
                    timestamp="2026-07-19T10:00:00+00:00",
                    library_id="library-b" if index >= 4 else "library-a",
                    job_id="job-errors" if index >= 5 else None,
                    details={"relative_path": f"folder/{index}.jpg"},
                )
            )
        self.assertTrue(store.flush())

        sequences: list[int] = []
        cursor: str | None = None
        while True:
            page = store.list_operation_logs(cursor=cursor, limit=2)
            sequences.extend(item["sequence"] for item in page["items"])
            self.assertEqual(page["total_count"], 7)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]

        self.assertEqual(sequences, sorted(sequences, reverse=True))
        self.assertEqual(len(sequences), 7)
        self.assertEqual(len(sequences), len(set(sequences)))
        failures = store.list_operation_logs(
            level="error",
            category="image_failure",
            library_id="library-b",
            job_id="job-errors",
            query="folder/5.jpg",
        )
        self.assertEqual(failures["total_count"], 1)
        self.assertEqual(failures["items"][0]["event"], "event_5")
        with self.assertRaises(InvalidActivityCursor):
            store.list_operation_logs(cursor="e30")

    def test_sensitive_and_unbounded_content_is_sanitized(self) -> None:
        exact_secret = "opaque-value-that-must-never-be-written"
        store = self.create_store(redactions=(exact_secret,))
        encoded_image = "A" * 500
        self.assertTrue(
            store.log(
                level="error",
                category="image_failure",
                event="image_processing_failed",
                source="backend_worker",
                message=(
                    "failed\r\nBearer abcdefghijklmnop "
                    "api_key=sk-secretvalue123 "
                    f"{exact_secret} C:\\Users\\person\\private.jpg "
                    f"data:image/png;base64,{encoded_image}; "
                    "model_prompt=do not retain this prompt"
                ),
                library_id="library-a",
                job_id="job-a",
                details={
                    "relative_path": "photos/safe.jpg",
                    "absolute_path": "C:\\Users\\person\\private.jpg",
                    "alternate_path": "/archive/private.jpg",
                    "authorization": "Bearer do-not-store-this",
                    "cookie": "session=do-not-store-this",
                    "password": "do-not-store-this",
                    "model_prompt": "full prompt must not be retained",
                    "model_response": "full response must not be retained",
                    "image_data": encoded_image,
                    "raw_bytes": b"\x00\x01private",
                },
            )
        )
        self.assertTrue(store.flush())

        item = store.list_operation_logs()["items"][0]
        serialized = json.dumps(item, ensure_ascii=False)
        raw_database = store.path.read_bytes()
        self.assertNotIn(exact_secret, serialized)
        self.assertNotIn("sk-secretvalue123", serialized)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("C:\\Users\\person", serialized)
        self.assertNotIn("/archive/private.jpg", serialized)
        self.assertNotIn(encoded_image, serialized)
        self.assertNotIn("do not retain this prompt", serialized)
        self.assertNotIn(exact_secret.encode(), raw_database)
        self.assertNotIn(b"sk-secretvalue123", raw_database)
        self.assertNotIn(b"C:\\Users\\person", raw_database)
        self.assertNotIn("\n", item["message"])
        self.assertIn("\\n", item["message"])
        self.assertEqual(item["details"]["relative_path"], "photos/safe.jpg")
        self.assertEqual(item["details"]["authorization"], "<redacted>")
        self.assertEqual(item["details"]["model_prompt"], "<redacted>")
        self.assertEqual(item["details"]["raw_bytes"], "<binary omitted>")

    def test_queue_prioritizes_errors_and_persists_drop_count(self) -> None:
        store = self.create_store(
            queue_capacity=2,
            write_batch_size=2,
            auto_start=False,
        )
        self.assertTrue(
            store.log(
                level="info",
                category="search",
                event="first",
                source="facade",
                message="first info",
            )
        )
        self.assertTrue(
            store.log(
                level="info",
                category="search",
                event="second",
                source="facade",
                message="second info",
            )
        )
        self.assertTrue(
            store.log(
                level="error",
                category="backend",
                event="important_failure",
                source="backend",
                message="must be retained",
            )
        )
        before = store.stats()
        self.assertEqual(before["queue_depth"], 2)
        self.assertEqual(before["dropped_info"], 1)

        self.assertTrue(store.start())
        self.assertTrue(store.flush())

        page = store.list_operation_logs(limit=10)
        events = {item["event"] for item in page["items"]}
        self.assertIn("important_failure", events)
        self.assertIn("activity_queue_dropped", events)
        summary = next(
            item for item in page["items"] if item["event"] == "activity_queue_dropped"
        )
        self.assertEqual(summary["details"]["info"], 1)
        self.assertEqual(store.stats()["pending_drop_counts"]["info"], 0)

    def test_job_updates_coalesce_to_latest_snapshot_before_write(self) -> None:
        store = self.create_store(
            queue_capacity=1,
            write_batch_size=1,
            auto_start=False,
        )
        self.assertTrue(store.record_job(JobHistoryRecord("job-1", "index", "running")))
        self.assertTrue(
            store.record_job(
                JobHistoryRecord(
                    "job-1",
                    "index",
                    "succeeded",
                    processed=10,
                    total=10,
                    progress=1,
                    finished_at="2026-07-19T10:00:00+00:00",
                )
            )
        )
        self.assertEqual(store.stats()["coalesced_jobs"], 1)
        store.start()
        self.assertTrue(store.flush())

        item = store.list_job_history()["items"][0]
        self.assertEqual(item["status"], "succeeded")
        self.assertEqual(item["processed"], 10)
        self.assertEqual(item["progress"], 1)

    def test_retention_is_bounded_and_preserves_active_jobs(self) -> None:
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        store = self.create_store(
            clock=lambda: now,
            log_retention_days=30,
            log_max_rows=3,
            job_max_rows=3,
            cleanup_batch_size=2,
            cleanup_every_writes=10_000,
        )
        timestamps = [
            "2026-05-01T00:00:00+00:00",
            "2026-07-15T00:00:00+00:00",
            "2026-07-16T00:00:00+00:00",
            "2026-07-17T00:00:00+00:00",
            "2026-07-18T00:00:00+00:00",
            "2026-07-19T00:00:00+00:00",
        ]
        for index, timestamp in enumerate(timestamps):
            self.assertTrue(
                store.log(
                    level="info",
                    category="search",
                    event=f"event_{index}",
                    source="facade",
                    message=f"message {index}",
                    timestamp=timestamp,
                )
            )
        for index in range(5):
            self.assertTrue(
                store.record_job(
                    JobHistoryRecord(
                        f"terminal-{index}",
                        "index",
                        "succeeded",
                        submitted_at=f"2026-07-{10 + index:02d}T00:00:00+00:00",
                        finished_at=f"2026-07-{10 + index:02d}T00:01:00+00:00",
                    )
                )
            )
        self.assertTrue(
            store.record_job(
                JobHistoryRecord(
                    "active-preserved",
                    "auto_tag",
                    "running",
                    submitted_at="2026-07-01T00:00:00+00:00",
                )
            )
        )
        self.assertTrue(store.flush())
        self.assertTrue(store.request_retention_cleanup(wait=True))

        logs = store.list_operation_logs(limit=10)
        self.assertEqual(logs["total_count"], 3)
        self.assertEqual(
            [item["event"] for item in logs["items"]],
            ["event_5", "event_4", "event_3"],
        )
        jobs = store.list_job_history(limit=10)
        self.assertEqual(jobs["total_count"], 3)
        self.assertIn("active-preserved", [item["job_id"] for item in jobs["items"]])

    def test_age_retention_removes_old_logs_even_below_row_cap(self) -> None:
        store = self.create_store(
            clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
            log_retention_days=30,
            log_max_rows=100,
            cleanup_batch_size=1,
            cleanup_every_writes=10_000,
        )
        for event, timestamp in (
            ("expired", "2026-05-01T00:00:00+00:00"),
            ("recent", "2026-07-18T00:00:00+00:00"),
        ):
            self.assertTrue(
                store.log(
                    level="info",
                    category="search",
                    event=event,
                    source="facade",
                    message=event,
                    timestamp=timestamp,
                )
            )
        self.assertTrue(store.flush())
        self.assertTrue(store.request_retention_cleanup(wait=True))

        page = store.list_operation_logs(limit=10)
        self.assertEqual(page["total_count"], 1)
        self.assertEqual(page["items"][0]["event"], "recent")

    def test_details_json_has_a_hard_utf8_size_limit(self) -> None:
        store = self.create_store(details_max_bytes=1_024)
        details = {f"field_{index}": "图" * 500 for index in range(20)}
        self.assertTrue(
            store.log(
                level="warning",
                category="backend",
                event="bounded_details",
                source="unit_test",
                message="bounded",
                details=details,
            )
        )
        self.assertTrue(store.flush())

        connection = sqlite3.connect(store.path)
        try:
            details_json = connection.execute(
                "SELECT details_json FROM operation_logs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertLessEqual(len(details_json.encode("utf-8")), 1_024)
        self.assertTrue(json.loads(details_json)["_truncated"])

    def test_concurrent_callers_are_serialized_without_losing_events(self) -> None:
        store = self.create_store(queue_capacity=1_024, write_batch_size=32)

        def write_events(worker: int) -> None:
            for index in range(75):
                accepted = store.log(
                    level="info",
                    category="test",
                    event="concurrent_event",
                    source="unit_test",
                    message=f"worker {worker} event {index}",
                    operation_id=f"worker-{worker}-{index}",
                )
                self.assertTrue(accepted)

        threads = [
            threading.Thread(target=write_events, args=(worker,)) for worker in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(store.flush(timeout=10))

        page = store.list_operation_logs(category="test", limit=200)
        second = store.list_operation_logs(
            category="test", cursor=page["next_cursor"], limit=200
        )
        operation_ids = {
            item["operation_id"] for item in [*page["items"], *second["items"]]
        }
        self.assertEqual(page["total_count"], 300)
        self.assertEqual(len(operation_ids), 300)

    def test_debug_is_stored_only_in_diagnostic_mode(self) -> None:
        store = self.create_store()
        self.assertFalse(
            store.log(
                level="debug",
                category="frontend",
                event="watchdog_poll",
                source="frontend",
                message="diagnostic only",
            )
        )
        self.assertEqual(store.stats()["filtered_debug"], 1)

        diagnostic_home = self.root / "diagnostic-config"
        diagnostic = ActivityStore(diagnostic_home, diagnostic_mode=True)
        self.stores.append(diagnostic)
        self.assertTrue(
            diagnostic.log(
                level="debug",
                category="frontend",
                event="watchdog_poll",
                source="frontend",
                message="diagnostic only",
            )
        )
        self.assertTrue(diagnostic.flush())
        self.assertEqual(diagnostic.list_operation_logs()["total_count"], 1)

    def test_write_failure_and_unavailable_path_do_not_escape_to_workers(self) -> None:
        store = self.create_store()
        with patch.object(
            store,
            "_write_batch",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            self.assertTrue(
                store.log(
                    level="error",
                    category="backend",
                    event="disk_failure_test",
                    source="unit_test",
                    message="the main task must continue",
                )
            )
            self.assertTrue(store.flush())
        self.assertEqual(store.stats()["write_failures"], 1)
        self.assertIn("disk is full", store.stats()["last_error"])

        blocking_file = self.root / "not-a-directory"
        blocking_file.write_text("occupied", encoding="utf-8")
        unavailable = ActivityStore(blocking_file / "config-home")
        self.stores.append(unavailable)
        self.assertFalse(unavailable.available)
        self.assertFalse(
            unavailable.log(
                level="error",
                category="backend",
                event="unavailable",
                source="unit_test",
                message="safe no-op",
            )
        )
        with self.assertRaises(ActivityStoreUnavailable):
            unavailable.list_operation_logs()


if __name__ == "__main__":
    unittest.main()
