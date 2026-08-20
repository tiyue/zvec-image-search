from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.state import IndexState


class FileSystemChangeQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec-fs-change-")
        self.state = IndexState(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_claim_limit_and_acknowledge_leave_remaining_changes_pending(self) -> None:
        self.state.enqueue_change("root-1", "first.jpg", "created")
        self.state.enqueue_change("root-1", "second.jpg", "created")

        claimed = self.state.claim_pending_changes("root-1", limit=1)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            self.state.acknowledge_claimed_changes(
                [int(change["id"]) for change in claimed]
            ),
            1,
        )
        self.assertEqual(self.state.count_pending_changes("root-1"), 1)

    def test_acknowledge_does_not_consume_a_newer_event_for_claimed_path(self) -> None:
        self.state.enqueue_change("root-1", "image.jpg", "created")
        claimed = self.state.claim_pending_changes("root-1")
        self.state.enqueue_change("root-1", "image.jpg", "modified")

        acknowledged = self.state.acknowledge_claimed_changes(
            [int(change["id"]) for change in claimed]
        )
        retried = self.state.claim_pending_changes("root-1")

        self.assertEqual(acknowledged, 0)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["event_type"], "modified")

    def test_release_returns_claimed_changes_to_pending(self) -> None:
        self.state.enqueue_change("root-1", "image.jpg", "created")
        claimed = self.state.claim_pending_changes("root-1")

        released = self.state.release_claimed_changes(
            [int(change["id"]) for change in claimed]
        )

        self.assertEqual(released, 1)
        self.assertEqual(self.state.count_pending_changes("root-1"), 1)

    def test_cycle_cutoff_keeps_later_events_for_the_next_cycle(self) -> None:
        self.state.enqueue_change("root-1", "first.jpg", "created")
        snapshot = self.state.pending_change_snapshot("root-1")
        self.state.enqueue_change("root-1", "second.jpg", "modified")

        claimed = self.state.claim_pending_changes(
            "root-1",
            sequence_at_most=snapshot["cutoff_sequence"],
        )

        self.assertEqual([item["relative_path"] for item in claimed], ["first.jpg"])
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["created"], 1)
        self.assertEqual(
            self.state.count_pending_changes(
                "root-1", sequence_at_most=snapshot["cutoff_sequence"]
            ),
            0,
        )
        self.assertEqual(self.state.count_pending_changes("root-1"), 1)

    def test_legacy_queue_without_sequence_or_index_is_migrated(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE fs_change_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(root_id, relative_path)
                );
                INSERT INTO fs_change_queue(root_id, relative_path, event_type)
                VALUES('root-1', 'legacy.jpg', 'created');
                """
            )
        finally:
            connection.close()

        migrated = IndexState(legacy_path)
        try:
            snapshot = migrated.pending_change_snapshot("root-1")
            index_columns = tuple(
                str(row["name"])
                for row in migrated.connection.execute(
                    "PRAGMA index_info('idx_fcq_pending')"
                )
            )
            self.assertEqual(snapshot["cutoff_sequence"], 1)
            self.assertEqual(
                index_columns,
                ("processed", "event_sequence", "queued_at"),
            )
        finally:
            migrated.close()

    def test_startup_recovery_restores_claims_and_runs(self) -> None:
        self.state.enqueue_change("root-1", "image.jpg", "created")
        self.state.claim_pending_changes("root-1")
        run_id = self.state.begin_index_run("root-1", "D:\\images")

        recovered = self.state.recover_interrupted_changes("root-1")
        run = self.state.connection.execute(
            "SELECT status, finished_at, needs_attention FROM index_runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()

        self.assertEqual(recovered, {"changes": 1, "index_runs": 1})
        self.assertEqual(self.state.count_pending_changes("root-1"), 1)
        self.assertIsNotNone(run)
        self.assertEqual(str(run["status"]), "failed")
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(int(run["needs_attention"]), 1)


if __name__ == "__main__":
    unittest.main()
