from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from image_vector_service.recommendation_store import (
    RecommendationBatchItem,
    RecommendationStore,
)


def _item(number: int, *, position: int = 0) -> RecommendationBatchItem:
    return RecommendationBatchItem(
        item_id=f"item-{number}",
        position=position,
        candidate_id=f"library-a:doc-{number}",
        library_id="library-a",
        doc_id=f"doc-{number}",
        sha256=f"sha-{number}",
        slot="random",
    )


class RecommendationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "recommendations.sqlite3"
        self.store = RecommendationStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_creates_an_idempotent_batch_without_counting_exposure(self) -> None:
        items = (_item(1), _item(2, position=1))

        first = self.store.create_batch("desktop", "request-1", items)
        replay = self.store.create_batch("desktop", "request-1", items)

        self.assertEqual(first, replay)
        self.assertEqual(self.store.batch_for_request("desktop", "request-1"), first)
        self.assertIsNone(self.store.batch_for_request("desktop", "missing"))
        self.assertEqual(self.store.recent_sha256("desktop", limit=60), ())
        self.assertEqual(
            self.store.exposure_counts("desktop", ["sha-1", "sha-2"]),
            {"sha-1": 0, "sha-2": 0},
        )

    def test_rejects_a_reused_request_or_event_with_different_content(self) -> None:
        self.store.create_batch("desktop", "request-1", (_item(1),))

        with self.assertRaises(ValueError):
            self.store.create_batch("desktop", "request-1", (_item(2),))
        with self.assertRaises(ValueError):
            self.store.create_batch("android-a", "request-2", (_item(1),))

    def test_history_is_isolated_by_viewer_and_limited_to_sixty(self) -> None:
        for number in range(65):
            batch = self.store.create_batch(
                "desktop", f"request-{number}", (_item(number),)
            )
            self.assertTrue(
                self.store.mark_shown("desktop", batch.batch_id, f"shown-{number}")
            )
        android = self.store.create_batch("android-a", "request-a", (_item(99),))
        self.assertTrue(
            self.store.mark_shown("android-a", android.batch_id, "shown-99")
        )

        desktop = self.store.recent_sha256("desktop", limit=60)

        self.assertEqual(len(desktop), 60)
        self.assertEqual(desktop[0], "sha-64")
        self.assertEqual(desktop[-1], "sha-5")
        self.assertEqual(self.store.recent_sha256("android-a", limit=60), ("sha-99",))

    def test_shown_once_counts_every_batch_sha_and_actions_are_event_idempotent(
        self,
    ) -> None:
        batch = self.store.create_batch(
            "desktop", "request-1", (_item(1), _item(2, position=1))
        )

        self.assertTrue(self.store.mark_shown("desktop", batch.batch_id, "shown-1"))
        self.assertFalse(self.store.mark_shown("desktop", batch.batch_id, "shown-1"))
        self.assertFalse(self.store.mark_shown("desktop", batch.batch_id, "shown-2"))
        self.assertTrue(
            self.store.record_action(
                "desktop", batch.batch_id, "opened-1", "item-1", "opened"
            )
        )
        self.assertFalse(
            self.store.record_action(
                "desktop", batch.batch_id, "opened-1", "item-1", "opened"
            )
        )
        with self.assertRaises(ValueError):
            self.store.record_action(
                "desktop", batch.batch_id, "opened-1", "item-2", "opened"
            )
        self.assertEqual(
            self.store.exposure_counts("desktop", ["sha-1", "sha-2"]),
            {"sha-1": 1, "sha-2": 1},
        )
        self.assertEqual(
            set(self.store.recent_sha256("desktop", limit=60)),
            {"sha-1", "sha-2"},
        )

    def test_database_schema_does_not_persist_paths_vectors_or_tokens(self) -> None:
        tables = self.store.schema_columns()
        columns = {column for values in tables.values() for column in values}

        self.assertFalse({"path", "vector", "token"} & columns)
        self.assertEqual(self.store.journal_mode(), "wal")
        self.assertTrue(self.store.foreign_keys_enabled())

    def test_shared_store_serializes_cross_thread_batch_creation(self) -> None:
        barrier = threading.Barrier(2)

        def create_batch() -> object:
            barrier.wait()
            return self.store.create_batch("desktop", "request-1", (_item(1),))

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = tuple(executor.map(lambda _: create_batch(), range(2)))

        self.assertEqual(first, second)
        self.assertEqual(self.store.batch_for_request("desktop", "request-1"), first)


if __name__ == "__main__":
    unittest.main()
