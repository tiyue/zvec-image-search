from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

from image_vector_service.recommendation_store import (
    _MAX_PROFILE_EVENTS,
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


def _shared_item(
    number: int,
    *,
    item_id: str,
    sha256: str,
) -> RecommendationBatchItem:
    return RecommendationBatchItem(
        item_id=item_id,
        position=0,
        candidate_id=f"library-a:doc-{number}",
        library_id="library-a",
        doc_id=f"doc-{number}",
        sha256=sha256,
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

    def test_history_is_isolated_by_viewer_and_limited_to_240(self) -> None:
        for number in range(245):
            batch = self.store.create_batch(
                "desktop", f"request-{number}", (_item(number),)
            )
            self.assertTrue(
                self.store.mark_shown("desktop", batch.batch_id, f"shown-{number}")
            )
        android = self.store.create_batch("android-a", "request-a", (_item(999),))
        self.assertTrue(
            self.store.mark_shown("android-a", android.batch_id, "shown-999")
        )

        desktop = self.store.recent_sha256("desktop", limit=240)

        self.assertEqual(len(desktop), 240)
        self.assertEqual(desktop[0], "sha-244")
        self.assertEqual(desktop[-1], "sha-5")
        self.assertEqual(self.store.recent_sha256("android-a", limit=240), ("sha-999",))
        with self.assertRaises(ValueError):
            self.store.recent_sha256("desktop", limit=241)

    def test_final_preference_uses_latest_cross_viewer_sequence(self) -> None:
        desktop = self.store.create_batch(
            "desktop",
            "desktop-request",
            (_shared_item(1, item_id="desktop-item", sha256="shared-sha"),),
        )
        android = self.store.create_batch(
            "android-a",
            "android-request",
            (_shared_item(2, item_id="android-item", sha256="shared-sha"),),
        )

        self.assertTrue(
            self.store.record_action(
                "desktop", desktop.batch_id, "desktop-like", "desktop-item", "like"
            )
        )
        self.assertTrue(
            self.store.record_action(
                "android-a",
                android.batch_id,
                "android-dislike",
                "android-item",
                "dislike",
            )
        )
        for sequence in range(100):
            self.store.record_action(
                "android-a",
                android.batch_id,
                f"android-history-{sequence}",
                "android-item",
                "like" if sequence % 2 == 0 else "dislike",
            )
        self.store.record_action(
            "desktop", desktop.batch_id, "desktop-export", "desktop-item", "export"
        )
        self.store.close()
        self.store = RecommendationStore(self.path)
        latest = self.store.final_preferences(["shared-sha", "missing-sha"])

        self.assertEqual(len(latest), 1)
        self.assertEqual(set(latest), {"shared-sha"})
        self.assertEqual(latest["shared-sha"].action, "dislike")
        self.assertEqual(latest["shared-sha"].library_id, "library-a")
        self.assertEqual(latest["shared-sha"].doc_id, "doc-2")
        self.assertEqual(
            self.store.record_action_with_preference(
                "android-a",
                android.batch_id,
                "android-dislike",
                "android-item",
                "dislike",
            ),
            (False, "dislike"),
        )
        self.assertEqual(
            self.store.final_preferences(["shared-sha"])["shared-sha"].action,
            "dislike",
        )

        self.assertTrue(
            self.store.record_action(
                "desktop", desktop.batch_id, "desktop-like-2", "desktop-item", "like"
            )
        )
        self.assertEqual(
            self.store.final_preferences(["shared-sha"])["shared-sha"].action,
            "like",
        )

    def test_recent_final_preferences_are_distinct_and_bounded(self) -> None:
        for number, action in enumerate(("like", "dislike", "like"), 1):
            batch = self.store.create_batch(
                f"viewer-{number}",
                f"request-{number}",
                (
                    _shared_item(
                        number,
                        item_id=f"preference-item-{number}",
                        sha256="same-sha" if number < 3 else "new-sha",
                    ),
                ),
            )
            self.store.record_action(
                f"viewer-{number}",
                batch.batch_id,
                f"preference-event-{number}",
                f"preference-item-{number}",
                action,
            )

        latest = self.store.recent_final_preferences(limit=1)
        complete = self.store.recent_final_preferences(limit=256)

        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0].sha256, "new-sha")
        self.assertEqual(latest[0].action, "like")
        self.assertEqual([item.sha256 for item in complete], ["new-sha", "same-sha"])
        self.assertEqual(complete[1].action, "dislike")
        with self.assertRaises(ValueError):
            self.store.recent_final_preferences(limit=257)

    def test_detailed_action_result_tracks_exact_cross_viewer_preference_changes(
        self,
    ) -> None:
        desktop = self.store.create_batch(
            "desktop",
            "desktop-details",
            (_shared_item(1, item_id="desktop-details-item", sha256="shared-sha"),),
        )
        android = self.store.create_batch(
            "android-a",
            "android-details",
            (_shared_item(2, item_id="android-details-item", sha256="shared-sha"),),
        )
        dislike_first = self.store.create_batch(
            "desktop",
            "dislike-first-details",
            (
                _shared_item(
                    3, item_id="dislike-first-item", sha256="dislike-first-sha"
                ),
            ),
        )

        opened = self.store.record_action_with_preference_details(
            "desktop",
            desktop.batch_id,
            "desktop-open",
            "desktop-details-item",
            "open",
        )
        first_disliked = self.store.record_action_with_preference_details(
            "desktop",
            dislike_first.batch_id,
            "dislike-first-event",
            "dislike-first-item",
            "dislike",
        )
        liked = self.store.record_action_with_preference_details(
            "desktop",
            desktop.batch_id,
            "desktop-like-details",
            "desktop-details-item",
            "like",
        )
        same_like = self.store.record_action_with_preference_details(
            "android-a",
            android.batch_id,
            "android-like-details",
            "android-details-item",
            "like",
        )
        exported = self.store.record_action_with_preference_details(
            "android-a",
            android.batch_id,
            "android-export",
            "android-details-item",
            "export",
        )
        disliked = self.store.record_action_with_preference_details(
            "android-a",
            android.batch_id,
            "android-dislike-details",
            "android-details-item",
            "dislike",
        )
        replayed_like = self.store.record_action_with_preference_details(
            "desktop",
            desktop.batch_id,
            "desktop-like-details",
            "desktop-details-item",
            "like",
        )
        reliked = self.store.record_action_with_preference_details(
            "desktop",
            desktop.batch_id,
            "desktop-relike-details",
            "desktop-details-item",
            "like",
        )

        self.assertEqual(
            (opened.recorded, opened.preference, opened.preference_sequence),
            (True, None, None),
        )
        self.assertFalse(opened.preference_changed)
        self.assertEqual(first_disliked.preference, "dislike")
        self.assertTrue(first_disliked.preference_changed)
        self.assertTrue(liked.recorded)
        self.assertEqual(liked.preference, "like")
        self.assertIsInstance(liked.preference_sequence, int)
        self.assertTrue(liked.preference_changed)
        self.assertTrue(same_like.recorded)
        self.assertEqual(same_like.preference, "like")
        self.assertFalse(same_like.preference_changed)
        self.assertGreater(
            same_like.preference_sequence or 0, liked.preference_sequence or 0
        )
        self.assertEqual(exported.preference, "like")
        self.assertEqual(exported.preference_sequence, same_like.preference_sequence)
        self.assertFalse(exported.preference_changed)
        self.assertEqual(disliked.preference, "dislike")
        self.assertGreater(
            disliked.preference_sequence or 0, same_like.preference_sequence or 0
        )
        self.assertTrue(disliked.preference_changed)
        self.assertFalse(replayed_like.recorded)
        self.assertEqual(replayed_like.preference, "dislike")
        self.assertEqual(
            replayed_like.preference_sequence, disliked.preference_sequence
        )
        self.assertFalse(replayed_like.preference_changed)
        self.assertEqual(reliked.preference, "like")
        self.assertGreater(
            reliked.preference_sequence or 0, disliked.preference_sequence or 0
        )
        self.assertTrue(reliked.preference_changed)
        with self.assertRaises(FrozenInstanceError):
            liked.recorded = False  # type: ignore[misc]

    def test_detailed_action_result_rejects_invalid_or_conflicting_events_atomically(
        self,
    ) -> None:
        desktop = self.store.create_batch(
            "desktop",
            "desktop-invalid-details",
            (_shared_item(1, item_id="desktop-invalid-item", sha256="shared-sha"),),
        )
        liked = self.store.record_action_with_preference_details(
            "desktop",
            desktop.batch_id,
            "shared-event",
            "desktop-invalid-item",
            "like",
        )

        with self.assertRaises(ValueError):
            self.store.record_action_with_preference_details(
                "desktop",
                desktop.batch_id,
                "shown-through-action",
                "desktop-invalid-item",
                "shown",
            )
        with self.assertRaises(ValueError):
            self.store.record_action_with_preference_details(
                "other-viewer",
                desktop.batch_id,
                "wrong-viewer",
                "desktop-invalid-item",
                "dislike",
            )
        with self.assertRaises(ValueError):
            self.store.record_action_with_preference_details(
                "desktop",
                desktop.batch_id,
                "shared-event",
                "desktop-invalid-item",
                "dislike",
            )

        current = self.store.final_preferences(["shared-sha"])["shared-sha"]
        self.assertEqual(current.action, "like")
        self.assertEqual(current.sequence, liked.preference_sequence)

    def test_recent_preference_profile_uses_a_bounded_indexed_event_horizon(
        self,
    ) -> None:
        old_batch = self.store.create_batch(
            "desktop",
            "old-profile-request",
            (_shared_item(1, item_id="old-profile-item", sha256="old-profile-sha"),),
        )
        hot_batch = self.store.create_batch(
            "android-a",
            "hot-profile-request",
            (_shared_item(2, item_id="hot-profile-item", sha256="hot-profile-sha"),),
        )
        self.store.record_action(
            "desktop",
            old_batch.batch_id,
            "old-profile-like",
            "old-profile-item",
            "like",
        )
        for number in range(_MAX_PROFILE_EVENTS + 1):
            self.store.record_action(
                "android-a",
                hot_batch.batch_id,
                f"hot-profile-event-{number}",
                "hot-profile-item",
                "like" if number % 2 == 0 else "dislike",
            )

        profile = self.store.recent_final_preferences(limit=256)

        self.assertEqual(
            [(preference.sha256, preference.action) for preference in profile],
            [("hot-profile-sha", "like")],
        )
        self.assertEqual(
            self.store.final_preferences(["old-profile-sha"])["old-profile-sha"].action,
            "like",
        )

    def test_recent_preference_profile_query_uses_partial_covering_index(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            details = [
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT items.sha256, events.action, items.library_id, "
                    "items.doc_id, events.sequence "
                    "FROM events INDEXED BY idx_events_preference_sequence "
                    "JOIN items ON items.item_id = events.item_id "
                    "WHERE events.action IN ('like', 'dislike') "
                    "ORDER BY events.sequence DESC LIMIT ?",
                    (_MAX_PROFILE_EVENTS,),
                )
            ]
        finally:
            connection.close()

        self.assertTrue(
            any("idx_events_preference_sequence" in detail for detail in details),
            details,
        )
        self.assertFalse(any("TEMP B-TREE" in detail for detail in details), details)

    def test_candidate_preference_query_is_not_limited_by_profile_window(self) -> None:
        for number in range(260):
            batch = self.store.create_batch(
                "desktop",
                f"profile-request-{number}",
                (
                    _shared_item(
                        number,
                        item_id=f"profile-item-{number}",
                        sha256=f"profile-sha-{number}",
                    ),
                ),
            )
            self.store.record_action(
                "desktop",
                batch.batch_id,
                f"profile-event-{number}",
                f"profile-item-{number}",
                "like",
            )

        profile = self.store.recent_final_preferences(limit=256)
        oldest = self.store.final_preferences(["profile-sha-0"])

        self.assertEqual(len(profile), 256)
        self.assertNotIn("profile-sha-0", {item.sha256 for item in profile})
        self.assertEqual(oldest["profile-sha-0"].action, "like")

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
