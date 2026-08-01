from __future__ import annotations

import unittest

from image_vector_service.recommendations import (
    RecommendationCandidate,
    select_recommendations,
)


def _candidate(
    number: int,
    *,
    sha: str | None = None,
    width: int = 2_000,
    height: int = 1_000,
    mtime_ns: int | None = None,
    exposure_count: int = 0,
    album_id: str | None = None,
    character: str | None = None,
    vector: tuple[float, ...] | None = (1.0, 0.0),
) -> RecommendationCandidate:
    return RecommendationCandidate(
        candidate_id=f"library-a:doc-{number}",
        doc_id=f"doc-{number}",
        sha256=sha or f"sha-{number}",
        width=width,
        height=height,
        mtime_ns=number if mtime_ns is None else mtime_ns,
        exposure_count=exposure_count,
        album_id=album_id,
        character=character,
        vector=vector,
    )


class RecommendationSelectionTests(unittest.TestCase):
    def test_returns_the_fixed_five_four_four_two_quota(self) -> None:
        candidates = [_candidate(number) for number in range(30)]

        result = select_recommendations(candidates, rng_seed=7)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.items), 15)
        self.assertEqual(
            result.counts_by_slot,
            {
                "quality": 5,
                "recent": 4,
                "low_exposure": 4,
                "random": 2,
            },
        )
        self.assertEqual(len({item.candidate.doc_id for item in result.items}), 15)

    def test_quality_pool_requires_technical_usability_and_prefers_vectors(
        self,
    ) -> None:
        vectorless = _candidate(1, vector=None, mtime_ns=100)
        vector_present = _candidate(2, vector=(0.0, 1.0), mtime_ns=1)
        too_small = _candidate(3, width=1_000, height=1_000)
        extreme_ratio = _candidate(4, width=8_001, height=2_000)
        remaining = [_candidate(number, width=1, height=1) for number in range(5, 24)]

        result = select_recommendations(
            [vectorless, vector_present, too_small, extreme_ratio, *remaining],
            rng_seed=3,
        )

        quality_ids = [
            item.candidate.doc_id for item in result.items if item.slot == "quality"
        ]
        self.assertIn("doc-2", quality_ids)
        self.assertLess(quality_ids.index("doc-2"), quality_ids.index("doc-1"))
        self.assertNotIn("doc-3", quality_ids)
        self.assertNotIn("doc-4", quality_ids)

    def test_enforces_sha_album_and_character_limits_without_author_data(self) -> None:
        candidates = [
            _candidate(number, sha="same" if number < 2 else None)
            for number in range(22)
        ]
        candidates.extend(
            _candidate(30 + number, album_id="album", character="same-character")
            for number in range(8)
        )

        result = select_recommendations(candidates, rng_seed=9)

        sha_values = [item.candidate.sha256 for item in result.items]
        self.assertLessEqual(sha_values.count("same"), 1)
        self.assertLessEqual(
            sum(item.candidate.album_id == "album" for item in result.items), 3
        )
        self.assertLessEqual(
            sum(item.candidate.character == "same-character" for item in result.items),
            5,
        )

    def test_history_falls_back_from_sixty_to_zero_only_when_needed(self) -> None:
        candidates = [_candidate(number) for number in range(15)]

        result = select_recommendations(
            candidates,
            recent_sha256=[f"sha-{number}" for number in range(15)],
            rng_seed=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.history_window, 0)
        self.assertEqual(len(result.items), 15)

    def test_missing_vectors_never_block_a_partial_result(self) -> None:
        result = select_recommendations(
            [_candidate(number, vector=None) for number in range(4)],
            rng_seed=2,
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.items), 4)
        self.assertEqual(result.history_window, 0)

    def test_random_backfill_completes_a_short_pool(self) -> None:
        candidates = [_candidate(number, width=1, height=1) for number in range(20)]

        result = select_recommendations(candidates, rng_seed=5)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.items), 15)
        self.assertEqual(result.counts_by_slot["quality"], 0)
        self.assertGreater(result.counts_by_slot["random"], 2)

    def test_mmr_penalizes_near_duplicates_when_a_different_vector_exists(self) -> None:
        duplicate = _candidate(1, vector=(1.0, 0.0), mtime_ns=20)
        near_duplicate = _candidate(2, vector=(0.99, 0.01), mtime_ns=19)
        distinct = _candidate(3, vector=(0.0, 1.0), mtime_ns=1)
        candidates = [duplicate, near_duplicate, distinct] + [
            _candidate(number, width=1, height=1, vector=None)
            for number in range(4, 22)
        ]

        result = select_recommendations(candidates, rng_seed=4)

        quality_ids = [
            item.candidate.doc_id for item in result.items if item.slot == "quality"
        ]
        self.assertEqual(quality_ids[:2], ["doc-1", "doc-3"])


if __name__ == "__main__":
    unittest.main()
