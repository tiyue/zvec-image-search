from __future__ import annotations

import unittest

from image_vector_service.models import SearchHit
from image_vector_service.result_diversity import (
    diversify_search_hits,
    series_group_key,
)


def _hit(rank: int, series: str, *, direct: bool = False) -> SearchHit:
    relative_path = f"{rank}.jpg" if direct else f"{series}/{rank}.jpg"
    return SearchHit(
        doc_id=f"doc-{rank}",
        distance=rank / 100,
        fields={
            "library_id": "library",
            "root_id": "root",
            "relative_path": relative_path,
            "sha256": f"{rank:064x}",
        },
        rank=rank,
        rank_source="image",
    )


class ResultDiversityTests(unittest.TestCase):
    def test_leading_results_limit_each_series_to_two(self) -> None:
        hits = [
            *[_hit(rank, "set-a") for rank in range(1, 7)],
            *[_hit(rank, "set-b") for rank in range(7, 11)],
            *[_hit(rank, "set-c") for rank in range(11, 15)],
        ]

        ranking = diversify_search_hits(hits, top_k=6)

        groups = [series_group_key(hit) for hit in ranking.hits]
        self.assertEqual(len(ranking.hits), 6)
        self.assertLessEqual(max(groups.count(group) for group in set(groups)), 2)
        self.assertGreater(ranking.suppressed_count, 0)
        self.assertFalse(ranking.relaxed)

    def test_cap_relaxes_only_when_not_enough_series_exist(self) -> None:
        hits = [_hit(rank, "only-set") for rank in range(1, 8)]

        ranking = diversify_search_hits(hits, top_k=5)

        self.assertEqual(len(ranking.hits), 5)
        self.assertTrue(ranking.relaxed)
        self.assertEqual([hit.rank for hit in ranking.hits], [1, 2, 3, 4, 5])

    def test_disabled_mode_preserves_relevance_order(self) -> None:
        hits = [_hit(rank, "set-a") for rank in range(1, 6)]

        ranking = diversify_search_hits(hits, top_k=4, enabled=False)

        self.assertEqual(
            [hit.doc_id for hit in ranking.hits],
            [
                "doc-1",
                "doc-2",
                "doc-3",
                "doc-4",
            ],
        )
        self.assertFalse(ranking.enabled)

    def test_direct_root_files_are_not_one_artificial_series(self) -> None:
        first = _hit(1, "unused", direct=True)
        second = _hit(2, "unused", direct=True)

        self.assertNotEqual(series_group_key(first), series_group_key(second))


if __name__ == "__main__":
    unittest.main()
