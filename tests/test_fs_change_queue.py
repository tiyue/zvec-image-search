from __future__ import annotations

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
