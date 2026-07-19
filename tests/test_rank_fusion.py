from __future__ import annotations

import unittest

from image_vector_service.models import RankSource, SearchHit
from image_vector_service.rank_fusion import (
    confidence_rank,
    sort_confidence_hits,
    weighted_rrf,
)


def hit(
    doc_id: str,
    confidence: float | None,
    *,
    source: RankSource,
    sha256: str | None = None,
    distance: float = 0.2,
    rank: int = 0,
    raw_score: float | None = None,
) -> SearchHit:
    value = SearchHit(
        doc_id=doc_id,
        distance=distance,
        fields={
            "library_id": "library-a",
            "sha256": sha256 or doc_id,
        },
        rank=rank,
        raw_score=confidence if raw_score is None else raw_score,
        normalized_score=confidence,
        confidence=confidence,
        match_state="high" if confidence is not None else None,
        rank_source=source,
    )
    if confidence is None:
        object.__setattr__(value, "confidence", None)
    return value


class ConfidenceRankTest(unittest.TestCase):
    def test_dual_source_reward_can_promote_a_combined_match(self):
        ranking = confidence_rank(
            [
                hit("dual", 0.70, source="image"),
                hit("dual", 0.70, source="text"),
                hit("text-only", 0.74, source="text"),
            ],
            query_type="image_text",
            top_k=5,
            min_confidence=0.0,
            score_gap=1.0,
            source_reward=0.05,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["dual", "text-only"])
        self.assertEqual(ranking.hits[0].rank_source, "fused")
        self.assertAlmostEqual(ranking.hits[0].confidence or 0.0, 0.75)
        self.assertEqual(ranking.hits[0].image_confidence, 0.70)
        self.assertEqual(ranking.hits[0].text_confidence, 0.70)
        self.assertEqual(ranking.hits[0].image_rank, 1)
        self.assertEqual(ranking.hits[0].text_rank, 1)
        self.assertAlmostEqual(ranking.hits[0].raw_score or 0.0, 0.75)
        self.assertEqual(
            ranking.hits[0].raw_score,
            ranking.hits[0].confidence,
        )

    def test_threshold_gap_sha_dedup_and_top_k_are_dynamic(self):
        ranking = confidence_rank(
            [
                hit("best", 0.92, source="text", sha256="same"),
                hit("duplicate", 0.91, source="text", sha256="same"),
                hit("second", 0.82, source="text"),
                hit("after-gap", 0.40, source="text"),
                hit("tail", 0.39, source="text"),
            ],
            query_type="text",
            top_k=4,
            min_confidence=0.35,
            score_gap=0.25,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["best", "second"])
        self.assertEqual([item.rank for item in ranking.hits], [1, 2])
        self.assertEqual(ranking.candidate_count, 4)
        self.assertEqual(ranking.filtered_count, 2)
        self.assertEqual(ranking.status, "ok")

    def test_sha_deduplication_happens_before_candidate_cap(self):
        ranking = confidence_rank(
            [
                hit("duplicate-a", 0.99, source="text", sha256="same"),
                hit("duplicate-b", 0.98, source="text", sha256="same"),
                hit("unique", 0.97, source="text", sha256="unique"),
            ],
            query_type="text",
            top_k=5,
            min_confidence=0.0,
            score_gap=1.0,
            max_candidates=2,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual(
            [item.doc_id for item in ranking.hits],
            ["duplicate-a", "unique"],
        )
        self.assertEqual(ranking.candidate_count, 2)

    def test_top_confidence_band_prunes_a_gradual_low_quality_tail(self):
        ranking = confidence_rank(
            [
                hit("best", 0.90, source="text"),
                hit("near-1", 0.88, source="text"),
                hit("near-2", 0.86, source="text"),
                hit("tail-1", 0.84, source="text"),
                hit("tail-2", 0.82, source="text"),
            ],
            query_type="text",
            top_k=10,
            min_confidence=0.0,
            score_gap=0.20,
            max_confidence_drop=0.05,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual(
            [item.doc_id for item in ranking.hits],
            ["best", "near-1", "near-2"],
        )
        self.assertEqual(ranking.filtered_count, 2)

    def test_hard_confidence_floor_excludes_values_below_twenty_percent(self):
        ranking = confidence_rank(
            [
                hit("best", 1.0, source="text"),
                hit("tail", 0.01, source="text"),
            ],
            query_type="text",
            top_k=10,
            min_confidence=0.0,
            score_gap=1.0,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["best"])
        self.assertEqual(
            ranking.diagnostics["effective_minimum_confidence"],
            0.20,
        )

    def test_invalid_confidence_band_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_confidence_drop"):
            confidence_rank(
                [hit("best", 0.9, source="text")],
                query_type="text",
                top_k=10,
                max_confidence_drop=1.01,
            )

    def test_threshold_can_return_zero_results(self):
        ranking = confidence_rank(
            [hit("weak", 0.24, source="image")],
            query_type="image",
            top_k=10,
            min_confidence=0.25,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual(ranking.hits, [])
        self.assertEqual(ranking.candidate_count, 1)
        self.assertEqual(ranking.filtered_count, 1)
        self.assertEqual(ranking.status, "no_reliable_match")

    def test_minimum_score_uses_documented_direction_per_query_type(self):
        for query_type in ("text", "image"):
            with self.subTest(query_type=query_type):
                ranking = confidence_rank(
                    [
                        hit(
                            "near",
                            0.95,
                            source=query_type,
                            raw_score=0.10,
                        ),
                        hit(
                            "far",
                            0.90,
                            source=query_type,
                            raw_score=0.30,
                        ),
                    ],
                    query_type=query_type,
                    top_k=5,
                    min_confidence=0.0,
                    minimum_score=0.20,
                    score_gap=1.0,
                )
                self.assertIsNotNone(ranking)
                assert ranking is not None
                self.assertEqual([item.doc_id for item in ranking.hits], ["near"])

        combined = confidence_rank(
            [
                hit("high", 0.80, source="image"),
                hit("high", 0.80, source="text"),
                hit("low", 0.60, source="image"),
                hit("low", 0.60, source="text"),
            ],
            query_type="image_text",
            top_k=5,
            min_confidence=0.0,
            minimum_score=0.75,
            score_gap=1.0,
        )
        self.assertIsNotNone(combined)
        assert combined is not None
        self.assertEqual([item.doc_id for item in combined.hits], ["high"])

    def test_low_confidence_override_skips_filters_but_keeps_order_dedup_and_top_k(
        self,
    ):
        ranking = confidence_rank(
            [
                hit("best", 0.24, source="text", sha256="same"),
                hit("duplicate", 0.23, source="text", sha256="same"),
                hit("after-gap", 0.10, source="text"),
                hit("tail", 0.01, source="text"),
            ],
            query_type="text",
            top_k=2,
            min_confidence=0.95,
            minimum_score=0.05,
            score_gap=0.01,
            max_confidence_drop=0.01,
            show_low_confidence=True,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["best"])
        self.assertEqual([item.rank for item in ranking.hits], [1])
        self.assertEqual(ranking.candidate_count, 3)
        self.assertEqual(ranking.filtered_count, 2)
        self.assertEqual(ranking.status, "low_confidence_override")
        self.assertEqual(ranking.diagnostics["hard_floor_filtered_count"], 2)

    def test_global_candidate_pool_expands_for_requests_above_fifty(self):
        ranking = confidence_rank(
            [
                hit(
                    f"doc-{index:02d}",
                    1.0 - index / 1000,
                    source="text",
                )
                for index in range(120)
            ],
            query_type="text",
            top_k=75,
            min_confidence=0.0,
            score_gap=1.0,
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual(ranking.candidate_count, 112)
        self.assertEqual(len(ranking.hits), 75)
        self.assertEqual(ranking.diagnostics["candidate_limit"], 112)

    def test_raw_score_breaks_confidence_ties_in_query_specific_direction(self):
        text_ranking = sort_confidence_hits(
            [
                hit("far", 0.8, source="text", raw_score=0.30),
                hit("near", 0.8, source="text", raw_score=0.10),
            ],
            query_type="text",
            top_k=2,
            score_gap=1.0,
            max_confidence_drop=1.0,
        )
        combined_ranking = sort_confidence_hits(
            [
                hit("lower", 0.8, source="fused", raw_score=0.70),
                hit("higher", 0.8, source="fused", raw_score=0.90),
            ],
            query_type="image_text",
            top_k=2,
            score_gap=1.0,
            max_confidence_drop=1.0,
        )
        assert text_ranking is not None and combined_ranking is not None
        self.assertEqual([item.doc_id for item in text_ranking.hits], ["near", "far"])
        self.assertEqual(
            [item.doc_id for item in combined_ranking.hits],
            ["higher", "lower"],
        )
        self.assertFalse(text_ranking.diagnostics["normalized_score_used"])

    def test_missing_confidence_requests_legacy_fallback(self):
        ranking = confidence_rank(
            [hit("legacy", None, source="text")],
            query_type="text",
            top_k=10,
        )
        self.assertIsNone(ranking)

    def test_confidence_v2_rewards_rank_agreement_and_exports_channels(self):
        ranking = confidence_rank(
            [
                hit("agree", 0.72, source="image", rank=2),
                hit("agree", 0.72, source="text", rank=2),
                hit("disagree", 0.74, source="image", rank=1),
                hit("disagree", 0.74, source="text", rank=31),
            ],
            query_type="image_text",
            top_k=5,
            min_confidence=0.0,
            score_gap=1.0,
            fusion_mode="confidence_v2",
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["agree", "disagree"])
        agreed = ranking.hits[0]
        self.assertAlmostEqual(agreed.confidence or 0.0, 0.7776)
        self.assertEqual(agreed.image_confidence, 0.72)
        self.assertEqual(agreed.text_confidence, 0.72)
        self.assertEqual(agreed.image_rank, 2)
        self.assertEqual(agreed.text_rank, 2)
        self.assertEqual(agreed.rank_agreement, 1.0)
        self.assertEqual(agreed.raw_score, agreed.confidence)

    def test_confidence_v2_penalizes_single_channel_high_score(self):
        candidates = [hit("image-only", 0.92, source="image", rank=1)]
        legacy = confidence_rank(
            candidates,
            query_type="image_text",
            top_k=5,
            min_confidence=0.85,
            score_gap=1.0,
        )
        protected = confidence_rank(
            candidates,
            query_type="image_text",
            top_k=5,
            min_confidence=0.85,
            score_gap=1.0,
            fusion_mode="confidence_v2",
        )
        self.assertIsNotNone(legacy)
        self.assertIsNotNone(protected)
        assert legacy is not None and protected is not None
        self.assertEqual([item.doc_id for item in legacy.hits], ["image-only"])
        self.assertEqual(protected.hits, [])
        self.assertEqual(protected.status, "no_reliable_match")
        self.assertEqual(protected.filtered_count, 1)

    def test_confidence_v2_does_not_reward_two_weak_channels(self):
        ranking = confidence_rank(
            [
                hit("weak", 0.30, source="image", rank=1),
                hit("weak", 0.30, source="text", rank=1),
            ],
            query_type="image_text",
            top_k=5,
            min_confidence=0.0,
            score_gap=1.0,
            fusion_mode="confidence_v2",
        )
        self.assertIsNotNone(ranking)
        assert ranking is not None
        self.assertAlmostEqual(ranking.hits[0].confidence or 0.0, 0.30)

    def test_zero_weight_channel_cannot_add_or_penalize_candidates(self):
        candidates = [
            hit("image-only", 0.90, source="image", rank=1),
            hit("text-only", 0.99, source="text", rank=1),
            hit("both", 0.80, source="image", rank=2),
            hit("both", 0.99, source="text", rank=2),
        ]
        for fusion_mode in ("confidence_v1", "confidence_v2"):
            with self.subTest(fusion_mode=fusion_mode):
                ranking = confidence_rank(
                    candidates,
                    query_type="image_text",
                    top_k=5,
                    image_weight=1.0,
                    text_weight=0.0,
                    min_confidence=0.0,
                    score_gap=1.0,
                    fusion_mode=fusion_mode,
                )
                self.assertIsNotNone(ranking)
                assert ranking is not None
                self.assertEqual(
                    [item.doc_id for item in ranking.hits],
                    ["image-only", "both"],
                )
                self.assertAlmostEqual(ranking.hits[0].confidence or 0.0, 0.90)
                self.assertAlmostEqual(ranking.hits[1].confidence or 0.0, 0.80)

        legacy = weighted_rrf(
            [hit("image-only", 0.90, source="image")],
            [hit("text-only", 0.99, source="text")],
            image_weight=1.0,
            text_weight=0.0,
        )
        self.assertEqual([item.doc_id for item in legacy], ["image-only"])


if __name__ == "__main__":
    unittest.main()
