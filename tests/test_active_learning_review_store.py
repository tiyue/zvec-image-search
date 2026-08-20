from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from image_vector_service.active_learning_review_store import (
    ActiveLearningReviewItem,
    ActiveLearningReviewStateError,
    ActiveLearningReviewStore,
    ActiveLearningReviewValidationError,
)


def _items(count: int) -> list[ActiveLearningReviewItem]:
    return [
        ActiveLearningReviewItem(
            doc_id=f"doc-{index:03d}",
            decision="accept",
            labels=("角色: 雷电将军",),
        )
        for index in range(1, count + 1)
    ]


class ActiveLearningReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "library.sqlite3"
        self.store = ActiveLearningReviewStore(self.database)

    def test_schema_coexists_with_library_database_and_keeps_user_version(self) -> None:
        other_database = Path(self.temporary_directory.name) / "existing.sqlite3"
        connection = sqlite3.connect(other_database)
        try:
            connection.execute("CREATE TABLE existing_library_data (value TEXT)")
            connection.execute("PRAGMA user_version=37")
            connection.commit()
        finally:
            connection.close()

        ActiveLearningReviewStore(other_database)

        connection = sqlite3.connect(other_database)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).casefold()
        finally:
            connection.close()
        self.assertIn("existing_library_data", tables)
        self.assertIn("active_learning_review_batches", tables)
        self.assertIn("active_learning_review_entries", tables)
        self.assertEqual(user_version, 37)
        self.assertEqual(journal_mode, "wal")

    def test_batch_and_pending_entries_survive_reopen(self) -> None:
        started = self.store.start_batch(
            batch_id="review-one",
            queue_id="queue-one",
            library_id="library-main",
            operation_id="operation-one",
            metadata={"selection_version": "active-learning-v1"},
            items=[
                ActiveLearningReviewItem(
                    doc_id="doc-accept",
                    decision="accept",
                    labels=("作品: 原神", "作品: 原神"),
                ),
                {"doc_id": "doc-reject", "decision": "reject"},
            ],
        )
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["total_count"], 2)

        reopened = ActiveLearningReviewStore(self.database)
        batch = reopened.get_batch("review-one")
        entries = reopened.list_entries("review-one")["items"]

        self.assertEqual(batch["queue_id"], "queue-one")
        self.assertEqual(batch["library_id"], "library-main")
        self.assertEqual(batch["operation_id"], "operation-one")
        self.assertEqual(batch["metadata"], {"selection_version": "active-learning-v1"})
        self.assertEqual([entry["status"] for entry in entries], ["pending", "pending"])
        self.assertEqual(entries[0]["requested_labels"], ["作品: 原神"])

    def test_failure_is_recorded_and_does_not_stop_later_items(self) -> None:
        self.store.start_batch(
            batch_id="mixed-review",
            queue_id="queue-mixed",
            items=[
                {"doc_id": "doc-a", "decision": "accept"},
                {"doc_id": "doc-b", "decision": "edit", "labels": ["角色: 甘雨"]},
                {"doc_id": "doc-c", "decision": "skip"},
                {"doc_id": "doc-d", "decision": "reject"},
            ],
        )
        self.store.record_entry_outcome(
            "mixed-review",
            "doc-b",
            status="failed",
            error_code="tag_write_failed",
            error_message="one image failed",
        )
        # The failed item remains local to doc-b; subsequent writes still succeed.
        applied = self.store.record_entry_outcome(
            "mixed-review",
            "doc-a",
            status="applied",
            before_snapshot={"tags": ["旧标签"]},
            after_snapshot={"tags": ["旧标签", "作品: 原神"]},
            feedback_event_id="feedback-a",
        )
        self.store.record_entry_outcome("mixed-review", "doc-c", status="skipped")
        self.store.record_entry_outcome(
            "mixed-review",
            "doc-d",
            status="conflict",
            error_message="annotation changed after the queue was created",
        )
        finished = self.store.finish_batch("mixed-review")

        self.assertEqual(finished["status"], "partial")
        self.assertEqual(finished["processed_count"], 4)
        self.assertEqual(finished["applied_count"], 1)
        self.assertEqual(finished["failed_count"], 1)
        self.assertEqual(finished["skipped_count"], 1)
        self.assertEqual(finished["conflict_count"], 1)
        self.assertEqual(applied["before_snapshot"], {"tags": ["旧标签"]})
        self.assertEqual(applied["feedback_event_id"], "feedback-a")
        self.assertTrue(applied["requires_undo"])

    def test_terminal_entry_is_idempotent_but_journal_cannot_be_rewritten(self) -> None:
        self.store.start_batch(
            batch_id="immutable-review", queue_id="queue", items=_items(1)
        )
        first = self.store.record_entry_outcome(
            "immutable-review",
            "doc-001",
            status="applied",
            before_snapshot={"tags": []},
            after_snapshot={"tags": ["角色: 雷电将军"]},
        )
        repeated = self.store.record_entry_outcome(
            "immutable-review",
            "doc-001",
            status="applied",
            before_snapshot={"tags": []},
            after_snapshot={"tags": ["角色: 雷电将军"]},
        )
        self.assertEqual(first, repeated)

        with self.assertRaises(ActiveLearningReviewStateError):
            self.store.record_entry_outcome(
                "immutable-review",
                "doc-001",
                status="applied",
                before_snapshot={"tags": []},
                after_snapshot={"tags": ["作品: 原神"]},
            )

    def test_finish_rejects_pending_entries(self) -> None:
        self.store.start_batch(
            batch_id="pending-review", queue_id="queue", items=_items(2)
        )
        self.store.record_entry_outcome("pending-review", "doc-001", status="skipped")
        with self.assertRaisesRegex(ActiveLearningReviewStateError, "1 pending"):
            self.store.finish_batch("pending-review")
        self.assertEqual(self.store.get_batch("pending-review")["status"], "running")

    def test_undo_uses_reverse_order_and_can_retry_only_failed_rollback(self) -> None:
        self.store.start_batch(
            batch_id="undo-review", queue_id="queue", items=_items(3)
        )
        self.store.record_entry_outcome(
            "undo-review",
            "doc-001",
            status="applied",
            before_snapshot={"tags": []},
            after_snapshot={"tags": ["角色: 雷电将军"]},
        )
        self.store.record_entry_outcome(
            "undo-review",
            "doc-002",
            status="failed",
            feedback_event_id="feedback-partially-created",
            requires_undo=True,
            error_message="tag write failed after feedback was saved",
        )
        self.store.record_entry_outcome("undo-review", "doc-003", status="skipped")
        self.store.finish_batch("undo-review")

        undo = self.store.begin_undo("undo-review")
        self.assertEqual(
            [entry["doc_id"] for entry in undo["entries"]],
            ["doc-002", "doc-001"],
        )
        self.store.record_undo_outcome(
            "undo-review", "doc-002", status="failed", error_message="busy"
        )
        self.store.record_undo_outcome("undo-review", "doc-001", status="undone")
        partial = self.store.finish_undo("undo-review")
        self.assertEqual(partial["status"], "undo_partial")
        self.assertTrue(partial["undo_available"])

        retry = self.store.begin_undo("undo-review")
        self.assertEqual([entry["doc_id"] for entry in retry["entries"]], ["doc-002"])
        self.store.record_undo_outcome("undo-review", "doc-002", status="undone")
        undone = self.store.finish_undo("undo-review")
        self.assertEqual(undone["status"], "undone")
        self.assertFalse(undone["undo_available"])
        self.assertIsNotNone(undone["undone_at"])

    def test_explicit_restart_recovery_preserves_history_and_side_effects(self) -> None:
        self.store.start_batch(
            batch_id="interrupted-review", queue_id="queue", items=_items(2)
        )
        self.store.record_entry_outcome(
            "interrupted-review",
            "doc-001",
            status="applied",
            before_snapshot={"tags": []},
            after_snapshot={"tags": ["角色: 雷电将军"]},
        )

        reopened = ActiveLearningReviewStore(self.database)
        # Opening for inspection does not interrupt a live owner.
        self.assertEqual(reopened.get_batch("interrupted-review")["status"], "running")
        recovered = reopened.recover_incomplete()
        self.assertEqual(recovered["review_batches"], ["interrupted-review"])
        batch = reopened.get_batch("interrupted-review")
        self.assertEqual(batch["status"], "interrupted")
        self.assertEqual(batch["processed_count"], 2)
        self.assertEqual(batch["failed_count"], 1)
        self.assertTrue(batch["undo_available"])
        self.assertEqual(
            reopened.get_entry("interrupted-review", "doc-002")["error_code"],
            "review_interrupted",
        )

    def test_restart_recovery_marks_pending_undo_without_replaying_it(self) -> None:
        self.store.start_batch(
            batch_id="interrupted-undo", queue_id="queue", items=_items(1)
        )
        self.store.record_entry_outcome(
            "interrupted-undo",
            "doc-001",
            status="applied",
            before_snapshot={"tags": []},
            after_snapshot={"tags": ["角色: 雷电将军"]},
        )
        self.store.finish_batch("interrupted-undo")
        self.store.begin_undo("interrupted-undo")

        reopened = ActiveLearningReviewStore(self.database)
        recovered = reopened.recover_incomplete()
        self.assertEqual(recovered["undo_batches"], ["interrupted-undo"])
        self.assertEqual(
            reopened.get_batch("interrupted-undo")["status"], "undo_partial"
        )
        entry = reopened.get_entry("interrupted-undo", "doc-001")
        self.assertEqual(entry["undo_status"], "failed")
        self.assertEqual(entry["undo_error_code"], "undo_interrupted")

        retry = reopened.begin_undo("interrupted-undo")
        self.assertEqual(len(retry["entries"]), 1)
        reopened.record_undo_outcome("interrupted-undo", "doc-001", status="undone")
        self.assertEqual(reopened.finish_undo("interrupted-undo")["status"], "undone")

    def test_batches_and_entries_are_paginated_and_filterable(self) -> None:
        for index in range(1, 4):
            batch_id = f"page-{index}"
            self.store.start_batch(
                batch_id=batch_id,
                queue_id=f"queue-{index}",
                library_id="library-a" if index != 2 else "library-b",
                items=_items(3),
            )
            for item_index in range(1, 4):
                self.store.record_entry_outcome(
                    batch_id,
                    f"doc-{item_index:03d}",
                    status="skipped",
                )
            self.store.finish_batch(batch_id)

        first = self.store.list_batches(limit=2)
        second = self.store.list_batches(limit=2, before_sequence=first["next_cursor"])
        self.assertEqual(
            [item["batch_id"] for item in first["items"]], ["page-3", "page-2"]
        )
        self.assertEqual([item["batch_id"] for item in second["items"]], ["page-1"])
        filtered = self.store.list_batches(library_id="library-a")
        self.assertEqual(
            [item["batch_id"] for item in filtered["items"]],
            ["page-3", "page-1"],
        )

        entry_first = self.store.list_entries("page-1", limit=2)
        entry_second = self.store.list_entries(
            "page-1", limit=2, after_order=entry_first["next_cursor"]
        )
        self.assertEqual(len(entry_first["items"]), 2)
        self.assertEqual(
            [item["doc_id"] for item in entry_second["items"]], ["doc-003"]
        )

    def test_success_queries_hide_undone_reviews_and_preserve_snapshots(self) -> None:
        self.store.start_batch(
            batch_id="successful-review",
            queue_id="queue-success",
            library_id="library-success",
            items=_items(3),
        )
        for index in range(1, 4):
            self.store.record_entry_outcome(
                "successful-review",
                f"doc-{index:03d}",
                status="applied",
                before_snapshot={"tags": []},
                after_snapshot={"tags": [f"角色: {index}"]},
                requires_undo=index != 2,
            )
        self.store.finish_batch("successful-review")

        latest = self.store.latest_applied_entry(
            "doc-003", library_id="library-success"
        )
        assert latest is not None
        self.assertEqual(latest["batch_id"], "successful-review")
        self.assertEqual(latest["queue_id"], "queue-success")
        self.assertEqual(latest["after_snapshot"], {"tags": ["角色: 3"]})

        first_page = self.store.list_applied_doc_ids(
            library_id="library-success", limit=2
        )
        second_page = self.store.list_applied_doc_ids(
            library_id="library-success",
            limit=2,
            after_doc_id=first_page["next_cursor"],
        )
        self.assertEqual(first_page["items"], ["doc-001", "doc-002"])
        self.assertEqual(second_page["items"], ["doc-003"])

        undoable = self.store.list_applied_entries("successful-review", limit=1)
        remaining = self.store.list_applied_entries(
            "successful-review",
            limit=5,
            before_order=undoable["next_cursor"],
        )
        self.assertEqual([item["doc_id"] for item in undoable["items"]], ["doc-003"])
        self.assertEqual([item["doc_id"] for item in remaining["items"]], ["doc-001"])

        self.store.begin_undo("successful-review")
        self.store.record_undo_outcome("successful-review", "doc-003", status="undone")
        self.store.record_undo_outcome("successful-review", "doc-001", status="undone")
        self.store.finish_undo("successful-review")
        # doc-002 was a successful no-op and did not need rollback; the other
        # two were actually undone and must be eligible for future review.
        self.assertIsNone(self.store.latest_applied_entry("doc-003"))
        self.assertEqual(self.store.list_applied_doc_ids()["items"], [])

    def test_concurrent_item_outcomes_keep_atomic_counters(self) -> None:
        self.store.start_batch(
            batch_id="parallel-review", queue_id="queue", items=_items(24)
        )

        def apply(index: int) -> None:
            self.store.record_entry_outcome(
                "parallel-review",
                f"doc-{index:03d}",
                status="applied",
                before_snapshot={"tags": []},
                after_snapshot={"tags": [f"角色: {index}"]},
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(apply, range(1, 25)))

        finished = self.store.finish_batch("parallel-review")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["processed_count"], 24)
        self.assertEqual(finished["applied_count"], 24)

    def test_invalid_batches_and_snapshots_are_rejected_before_writing(self) -> None:
        with self.assertRaises(ActiveLearningReviewValidationError):
            self.store.start_batch(
                batch_id="duplicate",
                queue_id="queue",
                items=[
                    {"doc_id": "same", "decision": "accept"},
                    {"doc_id": "same", "decision": "reject"},
                ],
            )
        self.assertEqual(self.store.list_batches()["items"], [])

        self.store.start_batch(
            batch_id="bad-snapshot", queue_id="queue", items=_items(1)
        )
        with self.assertRaisesRegex(
            ActiveLearningReviewValidationError, "finite values"
        ):
            self.store.record_entry_outcome(
                "bad-snapshot",
                "doc-001",
                status="applied",
                before_snapshot={"score": float("nan")},
                after_snapshot={"score": 1.0},
            )
        self.assertEqual(
            self.store.get_entry("bad-snapshot", "doc-001")["status"], "pending"
        )


if __name__ == "__main__":
    unittest.main()
