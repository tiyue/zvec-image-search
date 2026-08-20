from __future__ import annotations

import unittest

from image_vector_service.learning_ranker import RankingModel
from image_vector_service.models import SearchHit
from image_vector_service.rank_fusion import sort_confidence_hits
from image_vector_service.search_learning_config import (
    CalibrationRegistry,
    PlattCalibrator,
    SearchLearningBundle,
)
from image_vector_service.search_learning_runtime import apply_search_learning


def _bundle(*, shadow: bool, model: RankingModel | None) -> SearchLearningBundle:
    return SearchLearningBundle(
        configured=True,
        enabled=True,
        shadow_mode=shadow,
        ranker_model=model,
        calibrations=CalibrationRegistry(
            calibration_version="calibration-test",
            query_types={"text": PlattCalibrator(1.0, 0.0, 0.25)},
            fallback_reason=None,
        ),
        ranking_fallback_reason=None if model else "ranker_not_configured",
        calibration_fallback_reason=None,
        source_directory="test",
    )


def _model(**weights: float) -> RankingModel:
    return RankingModel(
        schema_version=1,
        model_version="ranker-test",
        feature_schema_version=1,
        intercept=0.0,
        weights=weights,
    )


class SearchLearningRuntimeTests(unittest.TestCase):
    def _hits(self) -> list[SearchHit]:
        return [
            SearchHit(
                "doc-vector",
                0.1,
                {"library_id": "lib-a"},
                rank=1,
                confidence=0.9,
                matched_tags=("刻晴",),
            ),
            SearchHit(
                "doc-manual",
                0.4,
                {
                    "library_id": "lib-a",
                    "_tag_evidence": {
                        "manual": ["刻晴"],
                        "folder": [],
                        "alias": [],
                        "model_high_confidence": [],
                        "model": [],
                        "vector": [],
                    },
                },
                rank=2,
                confidence=0.6,
                matched_tags=("刻晴",),
            ),
        ]

    def test_formal_model_reranks_and_exports_versioned_features(self) -> None:
        application = apply_search_learning(
            self._hits(),
            bundle=_bundle(shadow=False, model=_model(manual_tag_matches=4.0)),
            query_type="text",
            collection_sizes={"lib-a": 20_000},
        )

        self.assertEqual(
            [item.doc_id for item in application.hits], ["doc-manual", "doc-vector"]
        )
        first = application.hits[0]
        self.assertEqual(first.ranking_model_version, "ranker-test")
        self.assertEqual(first.feature_schema_version, 1)
        self.assertFalse(first.ranking_fallback)
        self.assertIsNotNone(first.search_features)
        assert first.search_features is not None
        self.assertEqual(first.search_features["manual_tag_matches"], 1.0)
        self.assertEqual(first.search_features["collection_size"], 20_000.0)
        self.assertTrue(application.diagnostics["applied"])

    def test_shadow_mode_computes_differences_but_preserves_display_order(self) -> None:
        application = apply_search_learning(
            self._hits(),
            bundle=_bundle(shadow=True, model=_model(manual_tag_matches=4.0)),
            query_type="text",
            collection_sizes={"lib-a": 100},
        )

        self.assertEqual(
            [item.doc_id for item in application.hits], ["doc-vector", "doc-manual"]
        )
        self.assertFalse(application.diagnostics["applied"])
        self.assertTrue(application.diagnostics["shadow_mode"])
        self.assertGreater(application.diagnostics["rank_changed_count"], 0)

    def test_missing_model_falls_back_without_breaking_search(self) -> None:
        application = apply_search_learning(
            self._hits(),
            bundle=_bundle(shadow=False, model=None),
            query_type="text",
            collection_sizes={"lib-a": 100},
        )
        self.assertEqual(
            [item.doc_id for item in application.hits], ["doc-vector", "doc-manual"]
        )
        self.assertTrue(application.diagnostics["ranking_fallback"])
        self.assertEqual(
            application.diagnostics["ranking_fallback_reason"],
            "ranker_not_configured",
        )

    def test_abstention_uses_calibrated_confidence_not_low_ranker_score(self) -> None:
        application = apply_search_learning(
            [
                SearchHit(
                    "doc-a",
                    0.1,
                    {"library_id": "lib-a"},
                    rank=1,
                    confidence=0.9,
                )
            ],
            bundle=_bundle(shadow=False, model=_model(vector_confidence=-20.0)),
            query_type="text",
            collection_sizes={"lib-a": 1},
        )
        self.assertLess(application.hits[0].ranking_score or 1.0, 0.20)
        ranking = sort_confidence_hits(
            application.hits,
            query_type="text",
            top_k=1,
            min_confidence=0.0,
        )
        assert ranking is not None
        self.assertEqual([item.doc_id for item in ranking.hits], ["doc-a"])


if __name__ == "__main__":
    unittest.main()
