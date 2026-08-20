from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import zvec

from image_vector_service import federated_search as federated_search_module
from image_vector_service.config import ConfigurationError
from image_vector_service.federated_search import (
    LibraryCandidateSet,
    rank_federated_hits,
)
from image_vector_service.library_config import LibraryDefinition
from image_vector_service.models import (
    PreparedSearchCandidates,
    ResolvedSearchHit,
    SearchHit,
)
from image_vector_service.search_quality import (
    FUSION_V1,
    FUSION_V2,
    SearchQualityConfig,
    SearchQualityThresholds,
    filter_search_hits,
    load_search_quality,
)
from image_vector_service.service import ImageVectorService


class ZvecScoreSemanticsTest(unittest.TestCase):
    def test_cosine_document_score_is_distance_and_lower_is_better(self):
        root = Path(tempfile.mkdtemp(prefix="zvec_score_semantics_")) / "collection"
        collection = zvec.create_and_open(
            str(root),
            zvec.CollectionSchema(
                name="score_semantics",
                fields=[zvec.FieldSchema("label", zvec.DataType.STRING)],
                vectors=[
                    zvec.VectorSchema(
                        "embedding",
                        zvec.DataType.VECTOR_FP32,
                        dimension=2,
                        index_param=zvec.HnswIndexParam(
                            metric_type=zvec.MetricType.COSINE
                        ),
                    )
                ],
            ),
        )
        try:
            statuses = collection.upsert(
                [
                    zvec.Doc(
                        id="same",
                        fields={"label": "same"},
                        vectors={"embedding": [1.0, 0.0]},
                    ),
                    zvec.Doc(
                        id="orthogonal",
                        fields={"label": "orthogonal"},
                        vectors={"embedding": [0.0, 1.0]},
                    ),
                    zvec.Doc(
                        id="opposite",
                        fields={"label": "opposite"},
                        vectors={"embedding": [-1.0, 0.0]},
                    ),
                ]
            )
            self.assertTrue(all(status.ok() for status in statuses))
            collection.optimize()
            documents = collection.query(
                queries=zvec.Query(field_name="embedding", vector=[1.0, 0.0]),
                topk=3,
                output_fields=["label"],
            )
            self.assertEqual(
                [document.id for document in documents],
                [
                    "same",
                    "orthogonal",
                    "opposite",
                ],
            )
            self.assertAlmostEqual(float(documents[0].score), 0.0, places=6)
            self.assertAlmostEqual(float(documents[1].score), 1.0, places=6)
            self.assertAlmostEqual(float(documents[2].score), 2.0, places=6)
        finally:
            collection.destroy()
            shutil.rmtree(root.parent, ignore_errors=True)


class SearchQualityConfigTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="zvec_search_quality_"))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def write(self, payload: object) -> None:
        (self.workspace / "search-quality.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_missing_config_keeps_legacy_unfiltered_behavior(self):
        config = load_search_quality(self.workspace)
        self.assertFalse(config.configured)
        self.assertEqual(config.text.minimum, 0.0)

    def test_loads_calibrator_production_format_and_metadata(self):
        self.write(
            {
                "schema_version": 1,
                "kind": "zvec-search-quality",
                "generated_at": "2026-07-13T00:00:00Z",
                "dataset": {"name": "fixture"},
                "constraints": {},
                "coverage": {},
                "run": {"id": "fixture"},
                "holdout_split": {
                    "manifest_fingerprint": "fixture-manifest",
                    "partition": "calibration",
                },
                "diagnostics": {"note": "ignored by runtime"},
                "thresholds": {"text": {"value": 0.85}},
                "text": {
                    "minimum_score": 0.30,
                    "minimum_confidence": 0.85,
                },
                "image": {
                    "minimum_score": 0.10,
                    "minimum_confidence": 0.95,
                },
                "combined": {
                    "minimum_score": 0.009,
                    "minimum_confidence": 0.549,
                },
            }
        )
        config = load_search_quality(self.workspace)
        self.assertTrue(config.configured)
        self.assertEqual(config.text.minimum_score, 0.30)
        self.assertEqual(config.text.minimum, 0.85)
        self.assertEqual(config.text.score_gap, 0.20)
        self.assertEqual(config.text.max_confidence_drop, 1.0)
        self.assertEqual(config.image.minimum, 0.95)
        self.assertEqual(config.combined.minimum_score, 0.009)
        self.assertEqual(config.combined.minimum, 0.549)
        serialized = config.to_dict()
        self.assertEqual(serialized["text"]["minimum_confidence"], 0.85)
        self.assertEqual(serialized["schema_version"], 2)
        self.assertEqual(serialized["fusion"]["mode"], FUSION_V1)

    def test_schema_v2_loads_per_mode_score_gap_and_fusion_v2(self):
        self.write(
            {
                "schema_version": 2,
                "text": {
                    "minimum_confidence": 0.2,
                    "score_gap": 0.11,
                    "max_confidence_drop": 0.21,
                },
                "image": {
                    "minimum_confidence": 0.3,
                    "score_gap": 0.12,
                    "max_confidence_drop": 0.22,
                },
                "combined": {
                    "minimum_confidence": 0.4,
                    "score_gap": 0.13,
                    "max_confidence_drop": 0.23,
                },
                "fusion": {
                    "mode": "confidence_v2",
                    "agreement_reward": 0.1,
                    "rank_decay": 8.0,
                    "weak_channel_floor": 0.5,
                    "weak_channel_penalty": 0.09,
                },
            }
        )
        config = load_search_quality(self.workspace)
        self.assertEqual(config.text.score_gap, 0.11)
        self.assertEqual(config.image.score_gap, 0.12)
        self.assertEqual(config.combined.score_gap, 0.13)
        self.assertEqual(config.text.max_confidence_drop, 0.21)
        self.assertEqual(config.image.max_confidence_drop, 0.22)
        self.assertEqual(config.combined.max_confidence_drop, 0.23)
        self.assertEqual(config.fusion_mode, FUSION_V2)
        self.assertEqual(config.fusion_options["rank_decay"], 8.0)

    def test_schema_v2_loads_optional_collection_null_calibration(self):
        self.write(
            {
                "schema_version": 2,
                "text": {"minimum_confidence": 0.2},
                "image": {"minimum_confidence": 0.3},
                "combined": {"minimum_confidence": 0.4},
                "collection_calibration": {
                    "mode": "null_mean_offset_v1",
                    "fallback": "identity",
                    "strength": 1.55,
                    "top_k": 10,
                    "modes": {
                        "text": {
                            "reference_confidence": 0.5,
                            "libraries": {
                                "large": {
                                    "baseline_confidence": 0.6,
                                    "offset": -0.1,
                                    "sample_count": 20,
                                },
                                "small": {
                                    "baseline_confidence": 0.4,
                                    "offset": 0.1,
                                    "sample_count": 10,
                                },
                            },
                        }
                    },
                },
            }
        )

        config = load_search_quality(self.workspace)

        self.assertTrue(config.collection_calibration.configured)
        self.assertEqual(
            config.collection_calibration.offsets_for("text"),
            {"large": -0.1, "small": 0.1},
        )
        self.assertEqual(config.collection_calibration.offsets_for("image"), {})
        self.assertEqual(
            config.to_dict()["collection_calibration"]["fallback"],
            "identity",
        )

    def test_collection_null_calibration_rejects_invalid_offset(self):
        self.write(
            {
                "schema_version": 2,
                "text": {},
                "image": {},
                "combined": {},
                "collection_calibration": {
                    "mode": "null_mean_offset_v1",
                    "modes": {
                        "text": {
                            "reference_confidence": 0.5,
                            "libraries": {
                                "broken": {
                                    "baseline_confidence": 0.5,
                                    "offset": 2.0,
                                    "sample_count": 1,
                                }
                            },
                        }
                    },
                },
            }
        )

        with self.assertRaisesRegex(ConfigurationError, "offset must be between"):
            load_search_quality(self.workspace)

    def test_schema_v1_never_enables_fusion_v2(self):
        self.write(
            {
                "schema_version": 1,
                "text": {"minimum_confidence": 0.2},
                "image": {"minimum_confidence": 0.2},
                "combined": {"minimum_confidence": 0.2},
                "fusion": {"mode": "confidence_v2"},
            }
        )
        config = load_search_quality(self.workspace)
        self.assertEqual(config.fusion_mode, FUSION_V1)
        self.assertEqual(config.fusion_options, {})

    def test_legacy_threshold_shape_remains_supported(self):
        self.write(
            {
                "schema_version": 1,
                "text": {"high": 0.8, "possible": 0.6, "minimum": 0.4},
                "image": {"high": 0.85, "possible": 0.65, "minimum": 0.5},
                "combined": {"high": 0.7, "possible": 0.4, "minimum": 0.3},
            }
        )
        config = load_search_quality(self.workspace)
        self.assertEqual(config.text.high, 0.8)
        self.assertEqual(config.text.possible, 0.6)
        self.assertEqual(config.text.minimum, 0.4)

    def test_invalid_config_is_rejected(self):
        invalid_sections = {
            "text": {"minimum_confidence": 0.2},
            "image": {"minimum_confidence": 0.2},
            "combined": {"minimum_confidence": 0.2},
        }
        cases = (
            {"schema_version": 3, **invalid_sections},
            {"schema_version": 1, **invalid_sections, "typo": True},
            {
                "schema_version": 2,
                **invalid_sections,
                "show_low_confidence": True,
            },
            {
                "schema_version": 1,
                **invalid_sections,
                "text": {"minimum_confidence": True},
            },
            {
                "schema_version": 1,
                **invalid_sections,
                "image": {"minimum_score": 2.1},
            },
            {
                "schema_version": 1,
                **invalid_sections,
                "combined": {"possible": 0.8, "high": 0.7},
            },
            {
                "schema_version": 2,
                **invalid_sections,
                "text": {"minimum_confidence": 0.2, "score_gap": 0.0},
            },
            {
                "schema_version": 2,
                **invalid_sections,
                "text": {
                    "minimum_confidence": 0.2,
                    "max_confidence_drop": 1.01,
                },
            },
            {
                "schema_version": 2,
                **invalid_sections,
                "fusion": {"mode": "confidence_v2", "rank_decay": 0.0},
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(ConfigurationError):
                    load_search_quality(self.workspace)

    def test_filter_uses_cosine_maximum_and_fused_minimum_raw_scores(self):
        cosine_hits = [
            SearchHit("near", 0.2, {}, rank_source="text"),
            SearchHit("far", 0.8, {}, rank_source="text"),
        ]
        cosine_thresholds = SearchQualityThresholds(
            minimum_score=0.3, minimum_confidence=0.0
        )
        self.assertEqual(
            [
                hit.doc_id
                for hit in filter_search_hits(cosine_hits, cosine_thresholds, "text")
            ],
            ["near"],
        )

        fused_hits = [
            SearchHit("enough", 0.2, {}, fused_score=0.01),
            SearchHit("low", 0.1, {}, fused_score=0.008),
        ]
        fused_thresholds = SearchQualityThresholds(
            minimum_score=0.009, minimum_confidence=0.0
        )
        self.assertEqual(
            [
                hit.doc_id
                for hit in filter_search_hits(fused_hits, fused_thresholds, "combined")
            ],
            ["enough"],
        )


class _CandidateRepository:
    def __init__(self, count: int = 80):
        self.doc_count = count
        self.calls: list[tuple[int, str]] = []

    def query(
        self,
        _vector,
        top_k,
        _tags,
        _tag_mode,
        rank_source,
    ):
        self.calls.append((top_k, rank_source))
        return [
            SearchHit(
                doc_id=f"doc-{index}",
                distance=(index + 2) / 100,
                fields={"sha256": f"sha-{index}"},
                rank=index + 1,
                rank_source=rank_source,
            )
            for index in range(min(top_k, self.doc_count))
        ]


class ConfiguredSingleCollectionRankingTest(unittest.TestCase):
    def service(
        self,
        minimum: float,
        *,
        minimum_score: float | None = None,
        score_gap: float = 0.20,
        max_confidence_drop: float = 1.0,
    ) -> tuple[ImageVectorService, _CandidateRepository]:
        service = object.__new__(ImageVectorService)
        repository = _CandidateRepository()
        service.repository = cast(Any, repository)
        thresholds = SearchQualityThresholds(
            minimum_score=minimum_score,
            minimum_confidence=minimum,
            score_gap=score_gap,
            max_confidence_drop=max_confidence_drop,
        )
        service.search_quality = SearchQualityConfig(
            text=thresholds,
            image=thresholds,
            combined=thresholds,
            configured=True,
        )
        return service, repository

    def test_configured_search_does_not_refill_weak_hits(self):
        service, repository = self.service(minimum=0.95)
        ranking = service._configured_single_ranking([1.0], query_type="text", top_k=20)
        self.assertEqual(repository.calls, [(50, "text")])
        self.assertEqual(ranking.candidate_count, 50)
        self.assertLess(len(ranking.hits), 20)
        self.assertEqual(ranking.filtered_count, 50 - len(ranking.hits))
        self.assertTrue(
            all(float(hit.confidence or 0.0) >= 0.95 for hit in ranking.hits)
        )

    def test_configured_search_reports_no_reliable_match(self):
        service, _repository = self.service(minimum=1.0)
        ranking = service._configured_single_ranking(
            [1.0], query_type="image", top_k=10
        )
        self.assertEqual(ranking.status, "no_reliable_match")
        self.assertEqual(ranking.hits, [])
        self.assertEqual(ranking.candidate_count, 50)
        self.assertEqual(ranking.filtered_count, 50)

    def test_configured_search_refills_budget_after_sha_exclusion(self):
        service, repository = self.service(minimum=0.0, score_gap=1.0)
        ranking = service._configured_single_ranking(
            [1.0],
            query_type="image",
            top_k=50,
            exclude_sha256="sha-0",
        )

        self.assertEqual(repository.calls, [(75, "image"), (76, "image")])
        self.assertEqual(ranking.candidate_count, 75)
        self.assertNotIn("doc-0", [hit.doc_id for hit in ranking.hits])
        self.assertIn("doc-50", [hit.doc_id for hit in ranking.hits])

    def test_configured_search_can_explicitly_return_low_confidence_candidates(self):
        service, repository = self.service(
            minimum=1.0,
            score_gap=0.001,
            max_confidence_drop=0.001,
        )
        ranking = service._configured_single_ranking(
            [1.0],
            query_type="text",
            top_k=3,
            show_low_confidence=True,
        )
        self.assertEqual(repository.calls, [(50, "text")])
        self.assertEqual(
            [hit.doc_id for hit in ranking.hits], ["doc-0", "doc-1", "doc-2"]
        )
        self.assertEqual(ranking.status, "low_confidence_override")
        self.assertEqual(ranking.candidate_count, 50)
        self.assertEqual(ranking.filtered_count, 47)

    def test_configured_search_uses_mode_specific_score_gap(self):
        service, _repository = self.service(minimum=0.0, score_gap=0.004)
        ranking = service._configured_single_ranking([1.0], query_type="text", top_k=10)
        self.assertEqual(len(ranking.hits), 1)

    def test_configured_search_applies_raw_minimum_score(self):
        service, _repository = self.service(
            minimum=0.0,
            minimum_score=0.025,
            score_gap=1.0,
        )
        ranking = service._configured_single_ranking([1.0], query_type="text", top_k=10)
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["doc-0"])

    def test_configured_search_uses_mode_specific_top_confidence_band(self):
        service, _repository = self.service(
            minimum=0.0,
            score_gap=1.0,
            max_confidence_drop=0.012,
        )
        ranking = service._configured_single_ranking([1.0], query_type="text", top_k=10)
        self.assertEqual(len(ranking.hits), 3)

    def test_configured_combined_search_uses_two_channels_without_rrf(self):
        service, repository = self.service(minimum=0.0)
        ranking = service._configured_combined_ranking(
            [1.0],
            [1.0],
            top_k=5,
            image_weight=0.5,
            text_weight=0.5,
            exclude_sha256=None,
            tags=(),
            tag_mode="all",
        )
        self.assertCountEqual(repository.calls, [(50, "image"), (50, "text")])
        self.assertEqual(len(ranking.hits), 5)
        self.assertTrue(all(hit.rank_source == "fused" for hit in ranking.hits))
        self.assertTrue(all(hit.fused_score is None for hit in ranking.hits))


class LowConfidenceFallbackCompatibilityTest(unittest.TestCase):
    def test_request_does_not_change_legacy_fallback_status(self):
        hit = SearchHit(
            doc_id="legacy",
            distance=0.2,
            fields={"sha256": "legacy"},
            rank_source="text",
        )
        collection = LibraryCandidateSet(
            library=LibraryDefinition(
                "library-a",
                "Library A",
                Path("images-a"),
                Path("workspace-a"),
            ),
            candidates=PreparedSearchCandidates(
                query_type="text",
                hits=[ResolvedSearchHit(hit=hit, source_path="legacy.jpg")],
                quality_configured=False,
                minimum_score=0.0,
            ),
        )
        ranking = rank_federated_hits([collection], top_k=10, show_low_confidence=True)
        self.assertEqual(ranking.status, "legacy_fallback")
        self.assertEqual([value.doc_id for value in ranking.hits], ["legacy"])
        diagnostics = federated_search_module._quality_diagnostics(
            [collection], ranking, show_low_confidence=True
        )
        self.assertTrue(diagnostics["low_confidence_requested"])
        self.assertFalse(diagnostics["low_confidence_override"])
        self.assertFalse(diagnostics["filtering_overridden"])


if __name__ == "__main__":
    unittest.main()
