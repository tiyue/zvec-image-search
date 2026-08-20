from __future__ import annotations

import math
import unittest

from image_vector_service.models import SearchHit
from image_vector_service.search_features import (
    DEFAULT_TAG_SOURCE_WEIGHTS,
    FEATURE_SCHEMA_VERSION,
    NUMERIC_FEATURE_NAMES,
    SearchFeatureError,
    SearchFeatures,
    TagMatchEvidence,
    extract_search_features,
    normalize_query_type,
)


class SearchFeatureTests(unittest.TestCase):
    def test_stable_schema_round_trips_without_losing_order(self) -> None:
        features = _features()

        self.assertEqual(
            tuple(features.numeric_values()),
            NUMERIC_FEATURE_NAMES,
        )
        self.assertEqual(
            SearchFeatures.from_mapping(features.to_dict()),
            features,
        )
        self.assertEqual(features.feature_schema_version, FEATURE_SCHEMA_VERSION)

    def test_tag_evidence_uses_strongest_source_once_per_tag(self) -> None:
        evidence = TagMatchEvidence(
            manual=frozenset({"原神", "雷电将军"}),
            folder=frozenset({"原神", "稻妻"}),
            alias=frozenset({"影"}),
            model_high_confidence=frozenset({"雷电将军"}),
            model=frozenset({"稻妻", "夜景"}),
            vector=frozenset({"紫色"}),
            query_tag_count=6,
        )

        expected = (1.0 + 1.0 + 0.9 + 0.9 + 0.45 + 0.25) / 6
        self.assertAlmostEqual(evidence.weighted_score(), expected)
        self.assertEqual(evidence.source_count("manual"), 2)
        self.assertEqual(len(evidence.unique_matches), 6)
        self.assertEqual(DEFAULT_TAG_SOURCE_WEIGHTS["folder"], 0.9)

    def test_extractor_keeps_cosine_distance_as_raw_vector_feature(self) -> None:
        hit = SearchHit(
            doc_id="doc-1",
            distance=0.18,
            fused_score=0.03,
            raw_score=0.03,
            normalized_score=0.91,
            confidence=0.87,
            rank=3,
            rank_agreement=0.84,
            fields={"library_id": "lib-1"},
            matched_tags=("原神",),
        )
        evidence = TagMatchEvidence(
            manual=frozenset({"原神"}),
            folder=frozenset({"雷电将军"}),
            query_tag_count=2,
        )

        features = extract_search_features(
            hit,
            query_type="image_text",
            tag_evidence=evidence,
            collection_size=12_000,
            duplicate_group_size=2,
            identity_match=True,
            work_match=0.8,
        )

        self.assertEqual(features.query_type, "combined")
        self.assertEqual(features.vector_raw_score, 0.18)
        self.assertEqual(features.vector_confidence, 0.87)
        self.assertEqual(features.collection_rank, 3.0)
        self.assertEqual(features.collection_size, 12_000.0)
        self.assertEqual(features.duplicate_group_size, 2.0)
        self.assertEqual(features.identity_match, 1.0)
        self.assertEqual(features.work_match, 0.8)
        self.assertEqual(features.image_text_agreement, 0.84)
        self.assertAlmostEqual(features.tag_match_score, 0.95)

    def test_extractor_accepts_original_channel_values_after_fusion(self) -> None:
        hit = SearchHit(
            doc_id="doc-1",
            distance=0.02,
            fused_score=0.02,
            confidence=0.95,
            fields={},
        )

        features = extract_search_features(
            hit,
            query_type="combined",
            vector_raw_score=0.31,
            vector_confidence=0.72,
            image_text_agreement=0.88,
        )

        self.assertEqual(features.vector_raw_score, 0.31)
        self.assertEqual(features.vector_confidence, 0.72)
        self.assertEqual(features.image_text_agreement, 0.88)

    def test_extractor_without_source_metadata_uses_low_weight_vector_evidence(
        self,
    ) -> None:
        hit = SearchHit(
            doc_id="doc-1",
            distance=0.4,
            fields={},
            matched_tags=("原神", "角色"),
        )

        features = extract_search_features(hit, query_type="tag")

        self.assertEqual(features.vector_tag_matches, 2.0)
        self.assertEqual(features.tag_match_score, 0.25)
        self.assertEqual(features.manual_tag_matches, 0.0)

    def test_schema_rejects_unknown_fields_versions_and_non_finite_values(self) -> None:
        payload = _features().to_dict()
        payload["unknown"] = 1
        with self.assertRaisesRegex(SearchFeatureError, "unknown fields"):
            SearchFeatures.from_mapping(payload)

        payload = _features().to_dict()
        payload["feature_schema_version"] = FEATURE_SCHEMA_VERSION + 1
        with self.assertRaisesRegex(SearchFeatureError, "Unsupported feature schema"):
            SearchFeatures.from_mapping(payload)

        payload = _features().to_dict()
        payload["vector_confidence"] = math.nan
        with self.assertRaisesRegex(SearchFeatureError, "must be finite"):
            SearchFeatures.from_mapping(payload)

    def test_query_type_aliases_are_explicit_and_typos_fail_closed(self) -> None:
        self.assertEqual(normalize_query_type("image_text"), "combined")
        self.assertEqual(normalize_query_type("Cosplayer"), "identity")
        self.assertEqual(normalize_query_type("pose"), "action")
        with self.assertRaisesRegex(SearchFeatureError, "Unsupported query_type"):
            normalize_query_type("identitty")


def _features(**overrides: object) -> SearchFeatures:
    values: dict[str, object] = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "vector_raw_score": 0.18,
        "vector_confidence": 0.87,
        "tag_match_score": 0.92,
        "manual_tag_matches": 2.0,
        "folder_tag_matches": 1.0,
        "alias_tag_matches": 0.0,
        "model_high_confidence_tag_matches": 1.0,
        "model_tag_matches": 1.0,
        "vector_tag_matches": 0.0,
        "identity_match": 1.0,
        "work_match": 1.0,
        "action_match": 0.0,
        "expression_match": 0.0,
        "scene_match": 0.0,
        "image_text_agreement": 0.84,
        "collection_rank": 3.0,
        "collection_size": 12_000.0,
        "duplicate_group_size": 1.0,
        "query_type": "identity_combined",
    }
    values.update(overrides)
    return SearchFeatures(**values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
