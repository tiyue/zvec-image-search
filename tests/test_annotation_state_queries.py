from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.annotation_service import _failed_annotation_is_retryable
from image_vector_service.state import (
    ENTRY_COLUMNS,
    IndexState,
    _annotation_retry_eligible,
)


def _entry(
    doc_id: str,
    *,
    root_id: str,
    relative_path: str,
    sha256: str,
) -> dict[str, object]:
    file_name = Path(relative_path).name
    return {
        "doc_id": doc_id,
        "root_id": root_id,
        "relative_path": relative_path,
        "parent_directory": Path(relative_path).parent.as_posix(),
        "file_name": file_name,
        "extension": Path(file_name).suffix.lower(),
        "mime_type": "image/jpeg",
        "sha256": sha256,
        "size_bytes": 100,
        "mtime_ns": 1,
        "width": 32,
        "height": 32,
        "tags": [],
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


class AnnotationStateQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "state.sqlite3"
        self.state = IndexState(self.state_path)

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def _store_annotation(
        self,
        entry: dict[str, object],
        *,
        status: str,
        source_sha256: str | None = None,
        policy: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        self.state.set_document_annotation(
            doc_id=str(entry["doc_id"]),
            source_sha256=source_sha256 or str(entry["sha256"]),
            cache_key=None,
            status=status,
            policy=policy,
            error=error,
        )

    def test_persisted_retry_decision_matches_annotation_policy(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"retry_eligible": True, "failure_category": "content_policy"}, ""),
            ({"retry_eligible": False, "failure_category": "schema_response"}, ""),
            ({"retry_eligible": 0, "failure_category": "schema_response"}, ""),
            ({"failure_category": "content_policy"}, "transport timeout"),
            ({"failure_category": "provider_auth"}, ""),
            ({"failure_category": "systemic"}, ""),
            ({"failure_category": "schema_response"}, ""),
            ({}, "DashScope data_inspection_failed: inappropriate content"),
            ({}, "invalid api key"),
            ({}, "model annotation schema_version is invalid"),
            ({}, "temporarily unavailable connection timeout"),
            ({}, "unknown item failure"),
        )
        for policy, error in cases:
            with self.subTest(policy=policy, error=error):
                expected = _failed_annotation_is_retryable(
                    {"policy": policy, "error": error}
                )
                actual = bool(_annotation_retry_eligible(policy, error))
                self.assertEqual(actual, expected)

    def test_legacy_annotation_schema_backfills_retry_eligibility(self) -> None:
        entries = [
            _entry(
                "content",
                root_id="root-a",
                relative_path="content.jpg",
                sha256="1" * 64,
            ),
            _entry(
                "schema",
                root_id="root-a",
                relative_path="schema.jpg",
                sha256="2" * 64,
            ),
        ]
        self.state.set_many(entries)
        self._store_annotation(
            entries[0],
            status="failed",
            error="data_inspection_failed: inappropriate content",
        )
        self._store_annotation(
            entries[1],
            status="failed",
            error="model annotation schema_version is invalid",
        )
        self.state.close()

        connection = sqlite3.connect(self.state_path)
        try:
            connection.executescript(
                """
                DROP INDEX IF EXISTS idx_document_annotations_status;
                DROP INDEX IF EXISTS idx_document_annotations_status_page;
                ALTER TABLE document_annotations RENAME TO annotations_current;
                CREATE TABLE document_annotations (
                    doc_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    cache_key TEXT,
                    status TEXT NOT NULL,
                    proposed_tags_json TEXT NOT NULL DEFAULT '[]',
                    accepted_tags_json TEXT NOT NULL DEFAULT '[]',
                    rejected_tags_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    entities_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO document_annotations (
                    doc_id, source_sha256, cache_key, status,
                    proposed_tags_json, accepted_tags_json, rejected_tags_json,
                    description, entities_json, warnings_json, structured_json,
                    policy_json, error, updated_at
                )
                SELECT doc_id, source_sha256, cache_key, status,
                    proposed_tags_json, accepted_tags_json, rejected_tags_json,
                    description, entities_json, warnings_json, structured_json,
                    policy_json, error, updated_at
                FROM annotations_current;
                DROP TABLE annotations_current;
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.state = IndexState(self.state_path)
        rows = {
            str(row["doc_id"]): int(row["retry_eligible"])
            for row in self.state.connection.execute(
                "SELECT doc_id, retry_eligible FROM document_annotations"
            )
        }
        self.assertEqual(rows, {"content": 0, "schema": 1})

    def test_scope_matrix_respects_root_sha_and_retry_policy(self) -> None:
        root_a = "root-a"
        root_b = "root-b"
        entries = {
            name: _entry(
                name,
                root_id=root,
                relative_path=f"set/{order:02d}-{name}.jpg",
                sha256=f"{order + 1:064x}",
            )
            for order, (name, root) in enumerate(
                (
                    ("none", root_a),
                    ("stale-accepted", root_a),
                    ("current-accepted", root_a),
                    ("explicit-true", root_a),
                    ("explicit-false", root_a),
                    ("content-policy", root_a),
                    ("schema-response", root_a),
                    ("stale-failed", root_a),
                    ("other-root", root_b),
                )
            )
        }
        self.state.set_many(entries.values())
        self._store_annotation(
            entries["stale-accepted"],
            status="accepted",
            source_sha256="f" * 64,
        )
        self._store_annotation(entries["current-accepted"], status="accepted")
        self._store_annotation(
            entries["explicit-true"],
            status="failed",
            policy={"retry_eligible": True, "failure_category": "content_policy"},
        )
        self._store_annotation(
            entries["explicit-false"],
            status="failed",
            policy={"retry_eligible": False, "failure_category": "schema_response"},
        )
        self._store_annotation(
            entries["content-policy"],
            status="failed",
            error="DashScope data_inspection_failed: inappropriate content",
        )
        self._store_annotation(
            entries["schema-response"],
            status="failed",
            error="model annotation schema_version is invalid",
        )
        self._store_annotation(
            entries["stale-failed"],
            status="failed",
            source_sha256="e" * 64,
            policy={"retry_eligible": True},
        )
        self._store_annotation(
            entries["other-root"],
            status="failed",
            policy={"retry_eligible": True},
        )

        def selected(scope: str) -> set[str]:
            return {
                str(item["doc_id"])
                for item in self.state.list_auto_tag_candidates(
                    scope,
                    root_id=root_a,
                    limit=100,
                )
            }

        self.assertEqual(
            selected("untagged"), {"none", "stale-accepted", "stale-failed"}
        )
        self.assertEqual(selected("failed"), {"explicit-true", "schema-response"})
        self.assertEqual(
            selected("failed_all"),
            {"explicit-true", "explicit-false", "content-policy", "schema-response"},
        )
        self.assertEqual(selected("all"), set(entries) - {"other-root"})

    def test_candidate_pages_use_stable_keysets_and_filter_before_limit(self) -> None:
        entries = [
            _entry(
                f"doc-{index}",
                root_id="root-a",
                relative_path=("same.jpg" if index in {2, 3} else f"{index}.jpg"),
                sha256=f"{index + 1:064x}",
            )
            for index in range(6)
        ]
        self.state.set_many(entries)
        self._store_annotation(entries[0], status="accepted")
        self._store_annotation(entries[1], status="accepted")

        untagged = self.state.list_auto_tag_candidates(
            "untagged",
            root_id="root-a",
            limit=2,
        )
        self.assertEqual(len(untagged), 2)
        self.assertNotIn("doc-0", {str(item["doc_id"]) for item in untagged})
        self.assertNotIn("doc-1", {str(item["doc_id"]) for item in untagged})

        trace: list[str] = []
        self.state.connection.set_trace_callback(trace.append)
        combined: list[dict[str, object]] = []
        after: tuple[str, str, str] | None = None
        while True:
            page = self.state.list_auto_tag_candidates(
                "all",
                root_id="root-a",
                limit=2,
                after=after,
            )
            if not page:
                break
            combined.extend(page)
            last = page[-1]
            after = (
                str(last["root_id"]),
                str(last["relative_path"]),
                str(last["doc_id"]),
            )
        self.state.connection.set_trace_callback(None)

        keys = [
            (str(item["root_id"]), str(item["relative_path"]), str(item["doc_id"]))
            for item in combined
        ]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), len(entries))
        selects = [
            statement
            for statement in trace
            if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(selects)
        self.assertTrue(
            all(" OFFSET " not in statement.upper() for statement in selects)
        )

    def test_candidate_query_count_is_constant_with_one_hundred_thousand_noise_rows(
        self,
    ) -> None:
        root_entries = [
            _entry(
                f"target-{index}",
                root_id="root-a",
                relative_path=f"target/{index:04d}.jpg",
                sha256=f"{index + 1:064x}",
            )
            for index in range(25)
        ]
        self.state.set_many(root_entries)

        def query() -> tuple[list[dict[str, object]], int]:
            trace: list[str] = []
            self.state.connection.set_trace_callback(trace.append)
            try:
                result = self.state.list_auto_tag_candidates(
                    "all",
                    root_id="root-a",
                    limit=25,
                )
            finally:
                self.state.connection.set_trace_callback(None)
            selects = sum(
                statement.lstrip().upper().startswith("SELECT") for statement in trace
            )
            return result, selects

        before, before_selects = query()
        placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)

        def noise_rows():
            for index in range(100_000):
                item = _entry(
                    f"noise-{index}",
                    root_id="root-b",
                    relative_path=f"noise/{index:06d}.jpg",
                    sha256=f"{index + 1000:064x}",
                )
                yield self.state._entry_values(item)

        with self.state.connection:
            self.state.connection.executemany(
                f"INSERT INTO entries({', '.join(ENTRY_COLUMNS)}) "
                f"VALUES({placeholders})",
                noise_rows(),
            )

        after, after_selects = query()
        self.assertEqual(before_selects, 1)
        self.assertEqual(after_selects, before_selects)
        self.assertEqual(
            [item["doc_id"] for item in after], [item["doc_id"] for item in before]
        )
        self.assertEqual(len(after), 25)

    def test_folder_annotation_reader_is_ordered_root_scoped_and_bounded(self) -> None:
        entries = [
            _entry(
                f"root-a-{index:04d}",
                root_id="root-a",
                relative_path=f"shared/{index:04d}.jpg",
                sha256=f"{index + 1:064x}",
            )
            for index in range(1_201)
        ]
        entries.extend(
            _entry(
                f"root-b-{index:04d}",
                root_id="root-b",
                relative_path=f"shared/{index:04d}.jpg",
                sha256=f"{index + 10_000:064x}",
            )
            for index in range(5)
        )
        self.state.set_many(entries)
        self._store_annotation(entries[0], status="accepted")

        pages = list(
            self.state.iter_entries_for_folders_with_annotations({("root-a", "shared")})
        )

        self.assertEqual([len(page) for page in pages], [500, 500, 201])
        self.assertLessEqual(max(map(len, pages)), 500)
        flattened = [item for page in pages for item in page]
        identities = [
            (
                str(entry["root_id"]),
                str(entry["relative_path"]),
                str(entry["doc_id"]),
            )
            for entry, _annotation in flattened
        ]
        self.assertEqual(identities, sorted(identities))
        self.assertEqual({identity[0] for identity in identities}, {"root-a"})
        self.assertIsNotNone(flattened[0][1])
        self.assertTrue(all(annotation is None for _, annotation in flattened[1:]))


if __name__ == "__main__":
    unittest.main()
