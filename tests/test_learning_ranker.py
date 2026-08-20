from __future__ import annotations

import math
import unittest

from image_vector_service.learning_ranker import (
    RANKING_MODEL_SCHEMA_VERSION,
    LearningRanker,
    RankingInput,
    RankingModel,
    RankingModelError,
    ranking_model_from_mapping,
)
from image_vector_service.search_features import (
    FEATURE_SCHEMA_VERSION,
    SearchFeatures,
)


class LearningRankerTests(unittest.TestCase):
    def test_logistic_inference_matches_explicit_formula(self) -> None:
        model = _model(
            intercept=-0.82,
            weights={
                "vector_confidence": 1.72,
                "tag_match_score": 1.31,
                "identity_match": 1.15,
            },
        )
        features = _features(
            vector_confidence=0.87,
            tag_match_score=0.92,
            identity_match=1.0,
        )

        decision = -0.82 + 1.72 * 0.87 + 1.31 * 0.92 + 1.15
        expected = 1.0 / (1.0 + math.exp(-decision))
        self.assertAlmostEqual(model.decision_function(features), decision)
        self.assertAlmostEqual(model.predict_score(features), expected)

    def test_logistic_inference_is_stable_at_extreme_values(self) -> None:
        positive = _model(
            intercept=1000.0,
            weights={"vector_confidence": 1.0},
        )
        negative = _model(
            intercept=-1000.0,
            weights={"vector_confidence": 1.0},
        )

        self.assertEqual(positive.predict_score(_features()), 1.0)
        self.assertEqual(negative.predict_score(_features()), 0.0)

    def test_linear_inference_is_clipped_to_public_score_range(self) -> None:
        high = _model(
            kind="linear",
            intercept=2.0,
            weights={"vector_confidence": 1.0},
        )
        low = _model(
            kind="linear",
            intercept=-2.0,
            weights={"vector_confidence": 1.0},
        )
        self.assertEqual(high.predict_score(_features()), 1.0)
        self.assertEqual(low.predict_score(_features()), 0.0)

    def test_active_ranker_orders_by_learned_score_with_deterministic_ties(
        self,
    ) -> None:
        ranker = LearningRanker(_model(intercept=0.0, weights={"identity_match": 4.0}))
        inputs = (
            _input("old-first", fallback_rank=1, identity_match=0.0),
            _input("learned-first", fallback_rank=2, identity_match=1.0),
        )

        batch = ranker.rank(inputs)

        self.assertTrue(batch.applied)
        self.assertFalse(batch.fallback)
        self.assertEqual(
            [item.candidate_id for item in batch.predictions],
            ["learned-first", "old-first"],
        )
        self.assertEqual(batch.predictions[0].learned_rank, 1)
        self.assertEqual(batch.diagnostics()["rank_changed_count"], 2)

    def test_shadow_mode_reports_learned_rank_but_preserves_visible_order(self) -> None:
        ranker = LearningRanker(_model(intercept=0.0, weights={"identity_match": 4.0}))
        inputs = (
            _input("old-first", fallback_rank=1, identity_match=0.0),
            _input("learned-first", fallback_rank=2, identity_match=1.0),
        )

        batch = ranker.rank(inputs, shadow_mode=True)

        self.assertFalse(batch.applied)
        self.assertTrue(batch.shadow_mode)
        self.assertEqual(
            [item.candidate_id for item in batch.predictions],
            ["old-first", "learned-first"],
        )
        self.assertEqual(batch.predictions[0].learned_rank, 2)
        self.assertEqual(batch.predictions[1].learned_rank, 1)

    def test_missing_model_preserves_fallback_order_and_scores(self) -> None:
        ranker = LearningRanker(None, fallback_reason="ranker_invalid:JSONDecodeError")
        inputs = (
            _input("a", fallback_rank=1, fallback_score=0.8),
            _input("b", fallback_rank=2, fallback_score=0.6),
        )

        batch = ranker.rank(inputs)

        self.assertTrue(batch.fallback)
        self.assertFalse(batch.applied)
        self.assertEqual([item.candidate_id for item in batch.predictions], ["a", "b"])
        self.assertEqual([item.ranking_score for item in batch.predictions], [0.8, 0.6])
        self.assertTrue(all(item.ranking_fallback for item in batch.predictions))
        self.assertEqual(
            batch.fallback_reason,
            "ranker_invalid:JSONDecodeError",
        )

    def test_model_parser_rejects_unknown_features_and_incompatible_versions(
        self,
    ) -> None:
        payload = _model().to_dict()
        payload["weights"] = {"invented_feature": 1.0}
        with self.assertRaisesRegex(RankingModelError, "unknown features"):
            ranking_model_from_mapping(payload)

        payload = _model().to_dict()
        payload["feature_schema_version"] = FEATURE_SCHEMA_VERSION + 1
        with self.assertRaisesRegex(RankingModelError, "incompatible"):
            ranking_model_from_mapping(payload)

        payload = _model().to_dict()
        payload["future_field"] = True
        with self.assertRaisesRegex(RankingModelError, "unknown fields"):
            ranking_model_from_mapping(payload)

    def test_ranker_rejects_duplicate_documents_and_fallback_ranks(self) -> None:
        ranker = LearningRanker(_model())
        duplicate = _input("a", fallback_rank=1)
        with self.assertRaisesRegex(RankingModelError, "Duplicate ranking candidate"):
            ranker.rank((duplicate, duplicate))
        with self.assertRaisesRegex(RankingModelError, "Duplicate fallback_rank"):
            ranker.rank(
                (
                    duplicate,
                    _input("b", fallback_rank=1),
                )
            )


def _model(**overrides: object) -> RankingModel:
    values: dict[str, object] = {
        "schema_version": RANKING_MODEL_SCHEMA_VERSION,
        "model_version": "ranker-test-v1",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "intercept": -0.5,
        "weights": {"vector_confidence": 1.0},
        "kind": "logistic",
    }
    values.update(overrides)
    return RankingModel(**values)  # type: ignore[arg-type]


def _input(
    candidate_id: str,
    *,
    fallback_rank: int,
    fallback_score: float = 0.5,
    **feature_overrides: object,
) -> RankingInput:
    return RankingInput(
        candidate_id=candidate_id,
        library_id="lib-1",
        features=_features(**feature_overrides),
        fallback_score=fallback_score,
        fallback_rank=fallback_rank,
    )


def _features(**overrides: object) -> SearchFeatures:
    values: dict[str, object] = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "vector_raw_score": 0.2,
        "vector_confidence": 0.8,
        "tag_match_score": 0.0,
        "manual_tag_matches": 0.0,
        "folder_tag_matches": 0.0,
        "alias_tag_matches": 0.0,
        "model_high_confidence_tag_matches": 0.0,
        "model_tag_matches": 0.0,
        "vector_tag_matches": 0.0,
        "identity_match": 0.0,
        "work_match": 0.0,
        "action_match": 0.0,
        "expression_match": 0.0,
        "scene_match": 0.0,
        "image_text_agreement": 0.0,
        "collection_rank": 1.0,
        "collection_size": 100.0,
        "duplicate_group_size": 1.0,
        "query_type": "text",
    }
    values.update(overrides)
    return SearchFeatures(**values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
