from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from image_vector_service.state import IndexState


def _entry(
    doc_id: str,
    *,
    sha256: str,
    mtime_ns: int,
    width: int = 2_000,
    height: int = 1_200,
    parent_directory: str = "album",
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": f"{parent_directory}/{doc_id}.jpg",
        "parent_directory": parent_directory,
        "file_name": f"{doc_id}.jpg",
        "extension": ".jpg",
        "mime_type": "image/jpeg",
        "sha256": sha256,
        "size_bytes": 1_024,
        "mtime_ns": mtime_ns,
        "width": width,
        "height": height,
        "tags": [],
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


class RecommendationCandidateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = IndexState(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_sample_is_bounded_and_includes_recent_and_quality_candidates(self) -> None:
        entries = [
            _entry(
                f"{index:064x}",
                sha256=f"{index + 100:064x}",
                mtime_ns=index,
                width=2_000 if index % 2 == 0 else 320,
                height=1_200 if index % 2 == 0 else 200,
            )
            for index in range(30)
        ]
        self.state.set_many(entries)

        sampled = self.state.sample_recommendation_entries(
            random_cursor="f" * 64,
            limit_per_pool=4,
        )

        self.assertLessEqual(len(sampled), 12)
        doc_ids = {str(entry["doc_id"]) for entry, _annotation in sampled}
        self.assertIn(f"{29:064x}", doc_ids)
        self.assertTrue(
            any(
                int(entry["width"]) * int(entry["height"]) >= 2_000_000
                for entry, _annotation in sampled
            )
        )

    def test_sample_returns_only_current_accepted_annotation(
        self,
    ) -> None:
        accepted = _entry("1" * 64, sha256="a" * 64, mtime_ns=3)
        stale = _entry("2" * 64, sha256="b" * 64, mtime_ns=2)
        pending = _entry("3" * 64, sha256="c" * 64, mtime_ns=1)
        self.state.set_many([accepted, stale, pending])
        self.state.set_document_annotation(
            doc_id=str(accepted["doc_id"]),
            source_sha256=str(accepted["sha256"]),
            cache_key="accepted",
            status="accepted",
            accepted_tags=["刻晴"],
            structured={
                "entities": {"character": [{"name": "刻晴", "state": "confirmed"}]}
            },
        )
        self.state.set_document_annotation(
            doc_id=str(stale["doc_id"]),
            source_sha256="d" * 64,
            cache_key="stale",
            status="accepted",
            accepted_tags=["甘雨"],
            structured={
                "entities": {"character": [{"name": "甘雨", "state": "confirmed"}]}
            },
        )
        self.state.set_document_annotation(
            doc_id=str(pending["doc_id"]),
            source_sha256=str(pending["sha256"]),
            cache_key="pending",
            status="pending_review",
            proposed_tags=["胡桃"],
            structured={
                "entities": {"character": [{"name": "胡桃", "state": "confirmed"}]}
            },
        )

        sampled = self.state.sample_recommendation_entries(
            random_cursor="0" * 64,
            limit_per_pool=10,
        )
        annotations = {
            str(entry["doc_id"]): annotation for entry, annotation in sampled
        }

        self.assertEqual(annotations[str(accepted["doc_id"])]["status"], "accepted")
        self.assertIsNone(annotations[str(stale["doc_id"])])
        self.assertIsNone(annotations[str(pending["doc_id"])])

    def test_invalid_sampling_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.state.sample_recommendation_entries(
                random_cursor="not-a-hash",
                limit_per_pool=10,
            )
        with self.assertRaises(ValueError):
            self.state.sample_recommendation_entries(
                random_cursor="0" * 64,
                limit_per_pool=0,
            )


if __name__ == "__main__":
    unittest.main()
