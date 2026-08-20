from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.collection_write_outbox import (
    CollectionWriteConflictError,
    CollectionWriteFailure,
    CollectionWriteItem,
    CollectionWriteValidationError,
)
from image_vector_service.config import ConfigurationError
from image_vector_service.state import IndexState


def _state_entry(doc_id: str, *, relative_path: str = "album/image.jpg") -> dict:
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": relative_path,
        "parent_directory": "album",
        "file_name": Path(relative_path).name,
        "extension": ".jpg",
        "mime_type": "image/jpeg",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "mtime_ns": 456,
        "width": 32,
        "height": 48,
        "tags": ["角色:雷电将军"],
        "folder_tags": ["作品:原神"],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


def _collection_fields(*, relative_path: str = "album/image.jpg") -> dict:
    return {
        "root_id": "root-a",
        "relative_path": relative_path,
        "file_name": Path(relative_path).name,
        "extension": ".jpg",
        "mime_type": "image/jpeg",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "mtime_ns": 456,
        "width": 32,
        "height": 48,
        "model": "qwen3-vl-embedding",
        "tags": ["角色:雷电将军", "作品:原神"],
        "metadata_text": "",
        "metadata_text_hash": "",
    }


def _upsert(doc_id: str = "doc-a") -> CollectionWriteItem:
    return CollectionWriteItem(
        doc_id=doc_id,
        action="upsert",
        collection_fields=_collection_fields(),
        vectors={
            "embedding": [0.25, 0.5, 0.75],
            "metadata_embedding": [0.0, 0.0, 0.0],
        },
        state_entry=_state_entry(doc_id),
    )


class CollectionWriteOutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "image_collection.state.sqlite3"
        self.state = IndexState(self.path)

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def reopen(self) -> None:
        self.state.close()
        self.state = IndexState(self.path)

    def test_interrupted_upsert_replays_without_a_model_call(self) -> None:
        outbox = self.state.write_outbox
        operation = outbox.enqueue("index:run-a", "index", [_upsert()])
        self.assertEqual(operation.status, "pending")

        first_claim = outbox.claim_recoverable()
        self.assertEqual(len(first_claim), 1)
        self.assertEqual(first_claim[0].attempt_count, 1)
        self.assertEqual(first_claim[0].vectors["embedding"], [0.25, 0.5, 0.75])

        # Simulate a hard stop after Zvec accepted the exact payload but before
        # SQLite state/outbox completion. Reopening only scans applying rows.
        zvec_documents: dict[str, tuple[dict, dict]] = {}
        zvec_documents[first_claim[0].doc_id] = (
            first_claim[0].collection_fields or {},
            first_claim[0].vectors,
        )
        self.reopen()
        self.assertEqual(self.state.write_outbox.recovered_interrupted_count, 1)

        second_claim = self.state.write_outbox.claim_recoverable()
        self.assertEqual(len(second_claim), 1)
        self.assertEqual(second_claim[0].attempt_count, 2)
        # Idempotent Zvec upsert: assigning the same id replaces, never duplicates.
        zvec_documents[second_claim[0].doc_id] = (
            second_claim[0].collection_fields or {},
            second_claim[0].vectors,
        )
        self.state.set_many([second_claim[0].state_entry or {}])
        self.state.write_outbox.mark_applied(
            second_claim[0].operation_id, [second_claim[0].item_id]
        )

        self.assertEqual(list(zvec_documents), ["doc-a"])
        state_entry = self.state.get("doc-a")
        self.assertIsNotNone(state_entry)
        assert state_entry is not None
        self.assertEqual(state_entry["relative_path"], "album/image.jpg")
        completed = self.state.write_outbox.get_operation("index:run-a")
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, "applied")
        self.assertEqual(completed.applied_count, 1)
        self.assertEqual(completed.pending_count, 0)
        raw = self.state.connection.execute(
            "SELECT vectors_blob, fields_json, state_payload_json "
            "FROM collection_write_items WHERE operation_id = 'index:run-a'"
        ).fetchone()
        self.assertIsNone(raw["vectors_blob"])
        self.assertIsNone(raw["fields_json"])
        self.assertIsNone(raw["state_payload_json"])

    def test_enqueue_is_idempotent_and_rejects_changed_payload(self) -> None:
        first = self.state.write_outbox.enqueue("sync:run-a", "sync", [_upsert()])
        repeated = self.state.write_outbox.enqueue("sync:run-a", "sync", [_upsert()])
        self.assertEqual(repeated, first)
        self.assertEqual(
            self.state.connection.execute(
                "SELECT COUNT(*) FROM collection_write_items"
            ).fetchone()[0],
            1,
        )

        changed_fields = _collection_fields(relative_path="album/other.jpg")
        with self.assertRaises(CollectionWriteConflictError):
            self.state.write_outbox.enqueue(
                "sync:run-a",
                "sync",
                [
                    CollectionWriteItem(
                        doc_id="doc-a",
                        action="upsert",
                        collection_fields=changed_fields,
                        vectors={
                            "embedding": [0.25, 0.5, 0.75],
                            "metadata_embedding": [0.0, 0.0, 0.0],
                        },
                        state_entry=_state_entry(
                            "doc-a", relative_path="album/other.jpg"
                        ),
                    )
                ],
            )

    def test_interrupted_delete_is_safe_to_repeat(self) -> None:
        self.state.set_many([_state_entry("doc-a")])
        zvec_documents = {"doc-a": object()}
        self.state.write_outbox.enqueue(
            "delete:run-a",
            "sync_delete",
            [CollectionWriteItem(doc_id="doc-a", action="delete")],
        )
        first_claim = self.state.write_outbox.claim_recoverable()
        self.assertEqual(first_claim[0].state_action, "delete")
        zvec_documents.pop("doc-a", None)

        # Stop before SQLite removal/mark_applied, then replay the already
        # completed delete. Missing documents are a successful idempotent state.
        self.reopen()
        second_claim = self.state.write_outbox.claim_recoverable()
        zvec_documents.pop(second_claim[0].doc_id, None)
        self.state.remove_many([second_claim[0].doc_id])
        self.state.write_outbox.mark_applied(
            second_claim[0].operation_id, [second_claim[0].item_id]
        )

        self.assertEqual(zvec_documents, {})
        self.assertIsNone(self.state.get("doc-a"))
        self.assertEqual(
            self.state.write_outbox.get_operation("delete:run-a").status,  # type: ignore[union-attr]
            "applied",
        )

    def test_batch_status_retry_and_payload_retention(self) -> None:
        self.state.write_outbox.enqueue(
            "index:batch-a", "index", [_upsert("doc-a"), _upsert("doc-b")]
        )
        claimed = self.state.write_outbox.claim_recoverable(limit=2)
        self.assertEqual([item.doc_id for item in claimed], ["doc-a", "doc-b"])
        self.state.write_outbox.mark_applied("index:batch-a", ["doc-a"])
        self.state.write_outbox.mark_failed(
            "index:batch-a",
            {
                "doc-b": CollectionWriteFailure(
                    code="zvec_busy",
                    message="temporary write failure",
                    retryable=True,
                    retry_after_seconds=3600,
                )
            },
        )
        operation = self.state.write_outbox.get_operation("index:batch-a")
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.applied_count, 1)
        self.assertEqual(operation.failed_count, 1)
        self.assertEqual(self.state.write_outbox.peek_recoverable(), [])

        row = self.state.connection.execute(
            "SELECT vectors_blob FROM collection_write_items WHERE item_id = 'doc-b'"
        ).fetchone()
        self.assertIsNotNone(row["vectors_blob"])
        self.assertEqual(self.state.write_outbox.requeue("index:batch-a", ["doc-b"]), 1)
        retry = self.state.write_outbox.claim_recoverable()
        self.assertEqual(retry[0].doc_id, "doc-b")
        self.assertEqual(retry[0].attempt_count, 2)

    def test_errors_are_redacted_and_payload_schema_rejects_secrets(self) -> None:
        self.state.write_outbox.add_redactions("literal-secret-123")
        self.state.write_outbox.enqueue("index:redact", "index", [_upsert()])
        self.state.write_outbox.claim_recoverable()
        self.state.write_outbox.mark_failed(
            "index:redact",
            {
                "doc-a": CollectionWriteFailure(
                    code="provider_error",
                    message=(
                        "Authorization: Bearer abcdefghijklmnop\n"
                        "api_key=sk-supersecret literal-secret-123 "
                        r"C:\Users\long\Pictures\private.jpg"
                    ),
                    retryable=False,
                )
            },
        )
        raw = self.state.connection.execute(
            "SELECT error_message FROM collection_write_items "
            "WHERE operation_id = 'index:redact'"
        ).fetchone()[0]
        self.assertNotIn("abcdefghijklmnop", raw)
        self.assertNotIn("sk-supersecret", raw)
        self.assertNotIn("literal-secret-123", raw)
        self.assertNotIn(r"C:\Users\long", raw)
        self.assertNotIn("\n", raw)
        self.assertIn("<redacted>", raw)
        self.assertIn("<local-path>", raw)

        fields = _collection_fields()
        fields["api_key"] = "must-never-be-stored"
        with self.assertRaises(CollectionWriteValidationError):
            self.state.write_outbox.enqueue(
                "index:secret-field",
                "index",
                [
                    CollectionWriteItem(
                        doc_id="doc-a",
                        action="upsert",
                        collection_fields=fields,
                        vectors={
                            "embedding": [1.0],
                            "metadata_embedding": [0.0],
                        },
                        state_entry=_state_entry("doc-a"),
                    )
                ],
            )
        fields = _collection_fields()
        fields["metadata_text"] = "api_key=sk-must-never-be-stored"
        with self.assertRaises(CollectionWriteValidationError):
            self.state.write_outbox.enqueue(
                "index:secret-value",
                "index",
                [
                    CollectionWriteItem(
                        doc_id="doc-a",
                        action="upsert",
                        collection_fields=fields,
                        vectors={
                            "embedding": [1.0],
                            "metadata_embedding": [0.0],
                        },
                        state_entry=_state_entry("doc-a"),
                    )
                ],
            )
        state_entry = _state_entry("doc-a")
        state_entry["absolute_path"] = r"C:\Users\long\Pictures\private.jpg"
        with self.assertRaises(CollectionWriteValidationError):
            self.state.write_outbox.enqueue(
                "index:absolute-path",
                "index",
                [
                    CollectionWriteItem(
                        doc_id="doc-a",
                        action="upsert",
                        collection_fields=_collection_fields(),
                        vectors={
                            "embedding": [1.0],
                            "metadata_embedding": [0.0],
                        },
                        state_entry=state_entry,
                    )
                ],
            )

    def test_cjk_relative_path_with_subdirectories_is_accepted(self) -> None:
        """CJK folder names followed by subfolders must not be rejected.

        The old ``_LOCAL_PATHS`` lookbehind pattern treated ``/子目录/file``
        after a CJK character as a Unix absolute path because the character
        before ``/`` was outside ``[A-Za-z0-9_.-]``.
        """
        cjk_path = (
            "半半子 - Nier：2B 【49P-380MB】_jpg/"
            "半半子 - Nier：2B 【49P-380MB】/"
            "映画-40P/1.jpg"
        )
        item = CollectionWriteItem(
            doc_id="doc-cjk",
            action="upsert",
            collection_fields=_collection_fields(relative_path=cjk_path),
            vectors={
                "embedding": [0.25, 0.5, 0.75],
                "metadata_embedding": [0.0, 0.0, 0.0],
            },
            state_entry=_state_entry("doc-cjk", relative_path=cjk_path),
        )
        operation = self.state.write_outbox.enqueue("index:cjk", "index", [item])
        self.assertEqual(operation.status, "pending")

    def test_unix_absolute_path_is_still_rejected(self) -> None:
        fields = _collection_fields(relative_path="/home/user/images/1.jpg")
        with self.assertRaises(CollectionWriteValidationError):
            self.state.write_outbox.enqueue(
                "index:abs-unix",
                "index",
                [
                    CollectionWriteItem(
                        doc_id="doc-abs",
                        action="upsert",
                        collection_fields=fields,
                        vectors={
                            "embedding": [1.0],
                            "metadata_embedding": [0.0],
                        },
                        state_entry=_state_entry("doc-abs"),
                    )
                ],
            )

    def test_collection_rebind_discards_old_collection_writes(self) -> None:
        self.state.ensure_collection_uuid("collection-a")
        self.state.write_outbox.enqueue("index:old", "index", [_upsert()])
        reset = self.state.ensure_collection_uuid("collection-b", reset_if_unbound=True)
        self.assertFalse(reset)
        self.assertIsNone(self.state.write_outbox.get_operation("index:old"))

    def test_prune_applied_keeps_recent_and_never_deletes_recovery_work(self) -> None:
        outbox = self.state.write_outbox
        for index in range(4):
            operation_id = f"index:applied-{index}"
            outbox.enqueue(operation_id, "index", [_upsert(f"doc-{index}")])
            claimed = outbox.claim_operation(operation_id)
            outbox.mark_applied(operation_id, [claimed[0].item_id])

        outbox.enqueue(
            "index:pending-prune",
            "index",
            [_upsert("doc-pending")],
        )
        outbox.enqueue(
            "index:failed-prune",
            "index",
            [_upsert("doc-failed")],
        )
        failed = outbox.claim_operation("index:failed-prune")
        outbox.mark_failed(
            "index:failed-prune",
            {
                failed[0].item_id: CollectionWriteFailure(
                    code="invalid_document",
                    message="one bad item",
                    retryable=False,
                )
            },
        )

        pruned = outbox.prune_applied(keep_latest=2, batch_size=10)
        self.assertEqual(pruned, {"operations": 2, "items": 2})
        self.assertIsNone(outbox.get_operation("index:applied-0"))
        self.assertIsNone(outbox.get_operation("index:applied-1"))
        self.assertIsNotNone(outbox.get_operation("index:applied-2"))
        self.assertIsNotNone(outbox.get_operation("index:applied-3"))
        self.assertIsNotNone(outbox.get_operation("index:pending-prune"))
        self.assertIsNotNone(outbox.get_operation("index:failed-prune"))

        orphan_count = self.state.connection.execute(
            "SELECT COUNT(*) FROM collection_write_items "
            "WHERE operation_id IN ('index:applied-0', 'index:applied-1')"
        ).fetchone()[0]
        self.assertEqual(int(orphan_count), 0)

    def test_prune_applied_is_bounded_and_validates_limits(self) -> None:
        outbox = self.state.write_outbox
        for index in range(5):
            operation_id = f"sync:applied-{index}"
            outbox.enqueue(
                operation_id,
                "sync_delete",
                [CollectionWriteItem(doc_id=f"doc-{index}", action="delete")],
            )
            claimed = outbox.claim_operation(operation_id)
            outbox.mark_applied(operation_id, [claimed[0].item_id])

        first = outbox.prune_applied(keep_latest=1, batch_size=2)
        second = outbox.prune_applied(keep_latest=1, batch_size=2)
        third = outbox.prune_applied(keep_latest=1, batch_size=2)
        self.assertEqual(first["operations"], 2)
        self.assertEqual(second["operations"], 2)
        self.assertEqual(third["operations"], 0)

        with self.assertRaises(CollectionWriteValidationError):
            outbox.prune_applied(keep_latest=-1)
        with self.assertRaises(CollectionWriteValidationError):
            outbox.prune_applied(batch_size=0)


class CollectionWriteOutboxMigrationAndScaleTest(unittest.TestCase):
    def test_old_state_database_gets_additive_outbox_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-state.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value)
                VALUES('state_schema_version', '2'), ('legacy_sentinel', 'kept');
                """
            )
            connection.close()

            state = IndexState(path)
            try:
                self.assertEqual(state.get_metadata("legacy_sentinel"), "kept")
                self.assertEqual(
                    state.get_metadata("collection_write_outbox_schema_version"), "1"
                )
                names = {
                    str(row[0])
                    for row in state.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("collection_write_operations", names)
                self.assertIn("collection_write_items", names)
            finally:
                state.close()

            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE metadata SET value = '99' "
                "WHERE key = 'collection_write_outbox_schema_version'"
            )
            connection.commit()
            connection.close()
            with self.assertRaises(ConfigurationError):
                IndexState(path)

    def test_hundred_thousand_applied_rows_do_not_make_recovery_scan_full_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = IndexState(Path(temporary) / "state.sqlite3")
            try:
                now = "2026-01-01T00:00:00.000+00:00"
                state.connection.execute(
                    """
                    INSERT INTO collection_write_operations(
                        operation_id, operation_kind, status, item_count,
                        pending_count, applied_count, failed_count, payload_sha256,
                        created_at, updated_at, completed_at
                    ) VALUES('history:bulk', 'index', 'applied', 100000, 0,
                        100000, 0, ?, ?, ?, ?)
                    """,
                    ("0" * 64, now, now, now),
                )
                rows = (
                    (
                        "history:bulk",
                        f"history-{index}",
                        f"history-{index}",
                        "delete",
                        "none",
                        "applied",
                        "0" * 64,
                        0,
                        0,
                        now,
                        now,
                        now,
                    )
                    for index in range(100_000)
                )
                state.connection.executemany(
                    """
                    INSERT INTO collection_write_items(
                        operation_id, item_id, doc_id, action, state_action,
                        status, payload_sha256, retryable, attempt_count,
                        created_at, updated_at, applied_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                state.connection.commit()
                state.write_outbox.enqueue(
                    "delete:pending",
                    "sync_delete",
                    [CollectionWriteItem(doc_id="pending-doc", action="delete")],
                )

                query_plan = " ".join(
                    str(column)
                    for row in state.connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT * FROM collection_write_items
                        WHERE sequence > 0 AND (
                            status = 'pending' OR (
                                status = 'failed' AND retryable = 1 AND
                                (next_retry_at IS NULL OR next_retry_at <= ?)
                            )
                        ) ORDER BY sequence LIMIT 1
                        """,
                        (now,),
                    )
                    for column in row
                ).lower()
                self.assertIn("idx_collection_write_items_recovery", query_plan)

                virtual_machine_steps = 0

                def progress() -> int:
                    nonlocal virtual_machine_steps
                    virtual_machine_steps += 100
                    return 0

                state.connection.set_progress_handler(progress, 100)
                try:
                    page = state.write_outbox.peek_recoverable(limit=1)
                finally:
                    state.connection.set_progress_handler(None, 0)
                self.assertEqual([item.doc_id for item in page], ["pending-doc"])
                # An accidental table scan executes millions of VM steps here.
                self.assertLess(virtual_machine_steps, 10_000)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
