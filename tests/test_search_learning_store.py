from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.search_learning_store import (
    FEEDBACK_WEIGHTS,
    SearchCandidateRecord,
    SearchLearningStore,
    SearchLearningValidationError,
    SearchSessionRecord,
)


def _session(index: int, *, candidates: int = 2) -> SearchSessionRecord:
    session_id = f"search-{index:04d}"
    return SearchSessionRecord(
        session_id=session_id,
        query_type="text",
        requested_count=15,
        returned_count=candidates,
        library_ids=("lib-main",),
        latency_ms=25 + index,
        query_text=f"private query {index}",
        candidates=tuple(
            SearchCandidateRecord(
                library_id="lib-main",
                doc_id=f"doc-{index:04d}-{candidate}",
                sha256=f"{index + candidate:064x}"[-64:],
                original_rank=candidate + 1,
                displayed_rank=candidate + 1,
                displayed=True,
                ranking_score=0.8 - candidate * 0.1,
                features={
                    "feature_schema_version": 1,
                    "vector_confidence": 0.8 - candidate * 0.1,
                    "identity_match": int(candidate == 0),
                    "query_type": "text",
                    "absolute_path": "C:/private/image.jpg",
                    "authorization": "Bearer should-not-persist",
                },
            )
            for candidate in range(candidates)
        ),
    )


class SearchLearningStoreTests(unittest.TestCase):
    def test_database_is_separate_wal_and_query_text_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SearchLearningStore(root, redactions=("private-marker",))
            record = _session(1)
            first = record.candidates[0]
            record = SearchSessionRecord(
                session_id=record.session_id,
                query_type=record.query_type,
                requested_count=record.requested_count,
                returned_count=record.returned_count,
                library_ids=record.library_ids,
                latency_ms=record.latency_ms,
                query_text=record.query_text,
                candidates=(
                    SearchCandidateRecord(
                        library_id=first.library_id,
                        doc_id=first.doc_id,
                        original_rank=first.original_rank,
                        features={**first.features, "note": "private-marker"},
                    ),
                    *record.candidates[1:],
                ),
            )
            self.assertTrue(store.try_record_search(record))
            self.assertEqual(store.path.name, "search-learning.sqlite3")
            self.assertNotEqual(store.path.name, "activity.sqlite3")

            connection = sqlite3.connect(store.path)
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                query_text = connection.execute(
                    "SELECT query_text FROM search_sessions"
                ).fetchone()[0]
                features = connection.execute(
                    "SELECT features_json FROM search_candidates LIMIT 1"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(mode).casefold(), "wal")
            self.assertIsNone(query_text)
            self.assertNotIn("absolute_path", features)
            self.assertNotIn("authorization", features)
            self.assertNotIn("private/image", features)
            self.assertNotIn("should-not-persist", features)
            self.assertNotIn("private-marker", features)

    def test_explicit_feedback_overrides_deduplicates_and_revokes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            store.record_search(_session(2))
            relevant = store.record_feedback(
                session_id="search-0002",
                library_id="lib-main",
                doc_id="doc-0002-0",
                action="relevant",
                source="context_menu",
            )
            duplicate = store.record_feedback(
                session_id="search-0002",
                library_id="lib-main",
                doc_id="doc-0002-0",
                action="relevant",
                source="detail",
            )
            self.assertEqual(relevant["event_id"], duplicate["event_id"])
            self.assertFalse(duplicate["created"])
            self.assertEqual(relevant["feedback_weight"], FEEDBACK_WEIGHTS["relevant"])

            negative = store.record_feedback(
                session_id="search-0002",
                library_id="lib-main",
                doc_id="doc-0002-0",
                action="not_relevant",
                source="context_menu",
            )
            page = store.list_feedback(session_id="search-0002", active_only=False)
            by_id = {item["event_id"]: item for item in page["items"]}
            self.assertFalse(by_id[relevant["event_id"]]["active"])
            self.assertTrue(by_id[negative["event_id"]]["active"])
            self.assertEqual(negative["feedback_weight"], -1.0)

            undone = store.revoke_feedback(str(negative["event_id"]))
            self.assertFalse(undone["active"])
            self.assertEqual(store.training_counts()["explicit_samples"], 0)

    def test_implicit_feedback_requires_opt_in_and_uses_fixed_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            store.record_search(_session(3))
            ignored = store.record_feedback(
                session_id="search-0003",
                library_id="lib-main",
                doc_id="doc-0003-0",
                action="open",
                source="gallery",
            )
            self.assertFalse(ignored["accepted"])
            store.update_settings(implicit_feedback_enabled=True)
            for action in ("detail", "open", "copy", "export"):
                feedback = store.record_feedback(
                    session_id="search-0003",
                    library_id="lib-main",
                    doc_id="doc-0003-0",
                    action=action,
                    source="gallery",
                )
                self.assertEqual(feedback["feedback_weight"], FEEDBACK_WEIGHTS[action])

            examples = store.training_examples()
            self.assertEqual(len(examples), 1)
            self.assertFalse(examples[0]["explicit"])
            self.assertEqual(examples[0]["feedback_weight"], 0.8)

            store.record_feedback(
                session_id="search-0003",
                library_id="lib-main",
                doc_id="doc-0003-0",
                action="not_relevant",
                source="context_menu",
            )
            examples = store.training_examples()
            self.assertEqual(len(examples), 1)
            self.assertTrue(examples[0]["explicit"])
            self.assertEqual(examples[0]["label"], 0)

    def test_feedback_requires_a_recorded_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            store.record_search(_session(4))
            with self.assertRaises(SearchLearningValidationError):
                store.record_feedback(
                    session_id="search-0004",
                    library_id="lib-main",
                    doc_id="doc-missing",
                    action="relevant",
                    source="context_menu",
                )

    def test_session_and_feedback_pagination_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            for index in range(10, 15):
                store.record_search(_session(index, candidates=1))
                store.record_feedback(
                    session_id=f"search-{index:04d}",
                    library_id="lib-main",
                    doc_id=f"doc-{index:04d}-0",
                    action="relevant" if index % 2 else "not_relevant",
                    source="context_menu",
                )
            first = store.list_sessions(limit=2)
            second = store.list_sessions(cursor=first["next_cursor"], limit=2)
            self.assertEqual(len(first["items"]), 2)
            self.assertEqual(len(second["items"]), 2)
            self.assertTrue(
                {item["session_id"] for item in first["items"]}.isdisjoint(
                    {item["session_id"] for item in second["items"]}
                )
            )
            feedback = store.list_feedback(limit=2)
            feedback_next = store.list_feedback(cursor=feedback["next_cursor"], limit=2)
            self.assertTrue(
                {item["event_id"] for item in feedback["items"]}.isdisjoint(
                    {item["event_id"] for item in feedback_next["items"]}
                )
            )

    def test_unavailable_store_is_a_safe_noop_for_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocked = Path(temporary) / "not-a-directory"
            blocked.write_text("occupied", encoding="utf-8")
            store = SearchLearningStore(blocked)
            self.assertFalse(store.available)
            self.assertFalse(store.try_record_search(_session(20)))

    def test_clear_and_anonymous_export_do_not_touch_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "library-data.txt"
            sentinel.write_text("keep", encoding="utf-8")
            store = SearchLearningStore(root)
            store.record_search(_session(30, candidates=1))
            store.record_feedback(
                session_id="search-0030",
                library_id="lib-main",
                doc_id="doc-0030-0",
                action="relevant",
                source="context_menu",
            )
            exported = store.anonymous_export()
            encoded = json.dumps(exported)
            self.assertNotIn("lib-main", encoded)
            self.assertNotIn("doc-0030", encoded)
            self.assertNotIn("search-0030", encoded)
            cleared = store.clear_learning_data()
            self.assertTrue(cleared["cleared"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(store.list_sessions()["items"], [])


if __name__ == "__main__":
    unittest.main()
