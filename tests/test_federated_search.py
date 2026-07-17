from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from image_vector_service import federated_search as federated_search_module
from image_vector_service.config import ServiceConfig
from image_vector_service.federated_search import (
    LibraryCandidateSet,
    aggregate_federated_hits,
    export_federated_search,
    rank_federated_hits,
)
from image_vector_service.library_config import LibraryDefinition
from image_vector_service.models import (
    PreparedSearch,
    PreparedSearchCandidates,
    RankSource,
    ResolvedSearchHit,
    SearchHit,
)


def candidate(
    doc_id: str,
    distance: float,
    sha256: str,
    source: Path,
    *,
    confidence: float | None = None,
    rank_source: RankSource | None = None,
    rank: int = 0,
    raw_score: float | None = None,
    matched_tags: tuple[str, ...] = (),
) -> ResolvedSearchHit:
    return ResolvedSearchHit(
        hit=SearchHit(
            doc_id=doc_id,
            distance=distance,
            fields={
                "root_id": "root",
                "relative_path": source.name,
                "sha256": sha256,
                "tags": [],
            },
            rank=rank,
            raw_score=confidence if raw_score is None else raw_score,
            normalized_score=confidence,
            confidence=confidence,
            match_state="high" if confidence is not None else None,
            rank_source=rank_source,
            matched_tags=matched_tags,
        ),
        source_path=str(source),
    )


class FederatedSearchTest(unittest.TestCase):
    def test_metadata_candidates_join_global_text_fusion(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "visual",
                            0.05,
                            "v" * 64,
                            self.source_a,
                            rank=1,
                            rank_source="text",
                        ),
                        candidate(
                            "shared",
                            0.10,
                            "s" * 64,
                            self.source_duplicate,
                            rank=2,
                            rank_source="text",
                        ),
                    ],
                    metadata_hits=[
                        candidate(
                            "shared",
                            0.02,
                            "s" * 64,
                            self.source_duplicate,
                            rank=1,
                            rank_source="metadata",
                        )
                    ],
                    candidate_k=50,
                    collection_size=3,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    metadata_hits=[
                        candidate(
                            "description",
                            0.03,
                            "d" * 64,
                            self.source_b,
                            rank=1,
                            rank_source="metadata",
                        )
                    ],
                    candidate_k=50,
                    collection_size=1,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=3)

        self.assertEqual(ranking.hits[0].doc_id, "shared")
        self.assertEqual(ranking.hits[0].rank_source, "fused")
        self.assertEqual(ranking.ranking_mode, "visual_metadata")
        self.assertEqual(ranking.candidate_count, 3)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="zvec_federated_test_"))
        self.results = self.root / "results"
        self.library_a = LibraryDefinition(
            "library-a",
            "Library A",
            self.root / "images-a",
            self.root / "workspace-a",
        )
        self.library_b = LibraryDefinition(
            "library-b",
            "Library B",
            self.root / "images-b",
            self.root / "workspace-b",
        )
        self.source_a = self.root / "a.jpg"
        self.source_b = self.root / "b.jpg"
        self.source_duplicate = self.root / "duplicate.jpg"
        self.source_a.write_bytes(b"a")
        self.source_b.write_bytes(b"b")
        self.source_duplicate.write_bytes(b"duplicate")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_distance_results_are_globally_sorted_and_sha_deduplicated(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate("a1", 0.20, "same", self.source_a),
                        candidate("a2", 0.30, "unique-a", self.source_duplicate),
                    ],
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate("b1", 0.10, "same", self.source_b),
                    ],
                ),
            ),
        ]
        hits = aggregate_federated_hits(collections)
        self.assertEqual([hit.doc_id for hit in hits], ["b1", "a2"])
        self.assertEqual(hits[0].fields["library_id"], "library-b")
        self.assertEqual(hits[0].fields["library_name"], "Library B")
        self.assertEqual([hit.rank for hit in hits], [1, 2])

    def test_combined_query_uses_one_global_rrf_ranking(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[candidate("a", 0.10, "a", self.source_a)],
                    text_hits=[candidate("a", 0.40, "a", self.source_a)],
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[candidate("b", 0.20, "b", self.source_b)],
                    text_hits=[candidate("b", 0.10, "b", self.source_b)],
                ),
            ),
        ]
        hits = aggregate_federated_hits(collections, image_weight=0.2, text_weight=0.8)
        self.assertEqual([hit.doc_id for hit in hits], ["b", "a"])
        self.assertTrue(all(hit.fused_score is not None for hit in hits))
        self.assertGreater(hits[0].fused_score or 0, hits[1].fused_score or 0)

    def test_text_query_fuses_accepted_tags_across_collections(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "vector-only",
                            0.05,
                            "vector-only",
                            self.source_a,
                            confidence=0.95,
                            rank_source="text",
                            rank=1,
                        )
                    ],
                    quality_configured=False,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "tag-and-vector",
                            0.20,
                            "tag-and-vector",
                            self.source_b,
                            confidence=0.80,
                            rank_source="text",
                            rank=1,
                        )
                    ],
                    tag_hits=[
                        candidate(
                            "tag-and-vector",
                            0.0,
                            "tag-and-vector",
                            self.source_b,
                            confidence=1.0,
                            rank_source="tag",
                            rank=1,
                            matched_tags=("坐姿",),
                        )
                    ],
                    quality_configured=False,
                    ranking_mode="hybrid_tag_vector",
                    hybrid_search={"enabled": True},
                ),
            ),
        ]

        ranking = rank_federated_hits(
            collections,
            top_k=2,
            diversify_results=False,
        )

        self.assertEqual(
            [hit.doc_id for hit in ranking.hits],
            ["tag-and-vector", "vector-only"],
        )
        self.assertEqual(ranking.ranking_mode, "hybrid_tag_vector")
        self.assertEqual(ranking.hits[0].matched_tags, ("坐姿",))
        self.assertEqual(ranking.hits[0].rank_source, "fused")

    def test_confidence_ranking_is_global_and_reports_diagnostics(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "a",
                            0.05,
                            "same",
                            self.source_a,
                            confidence=0.95,
                            rank_source="text",
                        ),
                        candidate(
                            "a-weak",
                            0.10,
                            "a-weak",
                            self.source_duplicate,
                            confidence=0.20,
                            rank_source="text",
                        ),
                    ],
                    quality_configured=True,
                    minimum_confidence=0.25,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "b-duplicate",
                            0.01,
                            "same",
                            self.source_b,
                            confidence=0.90,
                            rank_source="text",
                        ),
                    ],
                    quality_configured=True,
                    minimum_confidence=0.25,
                ),
            ),
        ]
        ranking = rank_federated_hits(
            collections,
            top_k=10,
            min_confidence=0.25,
        )
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["a"])
        self.assertEqual(ranking.status, "ok")
        self.assertEqual(ranking.candidate_count, 2)
        self.assertEqual(ranking.filtered_count, 1)
        self.assertEqual(ranking.ranking_mode, "confidence")

    def test_large_library_cannot_overwhelm_higher_confidence_small_library(self):
        large_hits = [
            candidate(
                f"large-{index:02d}",
                0.70 + index / 1000,
                f"large-{index:02d}",
                self.source_a,
                confidence=0.60 - index / 1000,
                rank_source="text",
                rank=index + 1,
            )
            for index in range(25)
        ]
        small_hits = [
            candidate(
                "small-best",
                0.02,
                "small-best",
                self.source_b,
                confidence=0.99,
                rank_source="text",
                rank=1,
            ),
            candidate(
                "small-second",
                0.04,
                "small-second",
                self.source_duplicate,
                confidence=0.97,
                rank_source="text",
                rank=2,
            ),
        ]
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=large_hits,
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=small_hits,
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=5)

        self.assertEqual(
            [hit.doc_id for hit in ranking.hits[:2]],
            ["small-best", "small-second"],
        )
        self.assertEqual(
            [hit.fields["library_id"] for hit in ranking.hits[:2]],
            ["library-b", "library-b"],
        )
        self.assertEqual(ranking.candidate_count, 27)

    def test_small_library_gets_no_scale_or_local_rank_bonus(self):
        large_hits = [
            candidate(
                f"large-low-{index:02d}",
                0.80 + index / 1000,
                f"large-low-{index:02d}",
                self.source_a,
                confidence=0.50 - index / 1000,
                rank_source="text",
                rank=index + 1,
            )
            for index in range(24)
        ]
        large_hits.append(
            candidate(
                "large-best",
                0.03,
                "large-best",
                self.source_a,
                confidence=0.97,
                rank_source="text",
                rank=25,
            )
        )
        small_hits = [
            candidate(
                "small-local-first",
                0.04,
                "small-local-first",
                self.source_b,
                confidence=0.96,
                rank_source="text",
                rank=1,
            )
        ]
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=large_hits,
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=small_hits,
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=2)

        self.assertEqual(
            [hit.doc_id for hit in ranking.hits],
            ["large-best", "small-local-first"],
        )
        self.assertEqual(
            [hit.fields["library_id"] for hit in ranking.hits],
            ["library-a", "library-b"],
        )

    def test_missing_confidence_preserves_weighted_rrf_fallback(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[candidate("a", 0.10, "a", self.source_a)],
                    text_hits=[candidate("a", 0.40, "a", self.source_a)],
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[candidate("b", 0.20, "b", self.source_b)],
                    text_hits=[candidate("b", 0.10, "b", self.source_b)],
                ),
            ),
        ]
        ranking = rank_federated_hits(
            collections,
            top_k=1,
            image_weight=0.2,
            text_weight=0.8,
        )
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["b"])
        self.assertEqual(ranking.status, "legacy_fallback")
        self.assertEqual(ranking.ranking_mode, "weighted_rrf")

    def test_federated_v2_requires_identical_explicit_configuration(self):
        options = {
            "agreement_reward": 0.08,
            "rank_decay": 10.0,
            "weak_channel_floor": 0.45,
            "weak_channel_penalty": 0.08,
        }
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "a",
                            0.2,
                            "a",
                            self.source_a,
                            confidence=0.8,
                            rank_source="image",
                            rank=1,
                        )
                    ],
                    text_hits=[
                        candidate(
                            "a",
                            0.2,
                            "a",
                            self.source_a,
                            confidence=0.7,
                            rank_source="text",
                            rank=2,
                        )
                    ],
                    quality_configured=True,
                    fusion_mode="confidence_v2",
                    fusion_options=options,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "b",
                            0.2,
                            "b",
                            self.source_b,
                            confidence=0.75,
                            rank_source="image",
                            rank=2,
                        )
                    ],
                    text_hits=[
                        candidate(
                            "b",
                            0.2,
                            "b",
                            self.source_b,
                            confidence=0.75,
                            rank_source="text",
                            rank=2,
                        )
                    ],
                    quality_configured=True,
                    fusion_mode="confidence_v2",
                    fusion_options=options,
                ),
            ),
        ]
        ranking = rank_federated_hits(collections, top_k=5, score_gap=1.0)
        self.assertEqual(ranking.ranking_mode, "confidence_v2")
        self.assertTrue(all(hit.rank_source == "fused" for hit in ranking.hits))
        self.assertTrue(all(hit.image_rank is not None for hit in ranking.hits))
        self.assertTrue(all(hit.text_rank is not None for hit in ranking.hits))

        collections[1] = LibraryCandidateSet(
            self.library_b,
            PreparedSearchCandidates(
                **{
                    **collections[1].candidates.__dict__,
                    "fusion_options": {**options, "agreement_reward": 0.1},
                }
            ),
        )
        fallback = rank_federated_hits(collections, top_k=5, score_gap=1.0)
        self.assertEqual(fallback.ranking_mode, "confidence")

    def test_configured_combined_v1_exports_fused_confidence_as_raw_score(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "a",
                            0.30,
                            "a",
                            self.source_a,
                            confidence=0.60,
                            rank_source="image",
                            rank=1,
                        )
                    ],
                    text_hits=[
                        candidate(
                            "a",
                            0.20,
                            "a",
                            self.source_a,
                            confidence=0.70,
                            rank_source="text",
                            rank=1,
                        )
                    ],
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "b",
                            0.18,
                            "b",
                            self.source_b,
                            confidence=0.82,
                            rank_source="image",
                            rank=1,
                        )
                    ],
                    text_hits=[
                        candidate(
                            "b",
                            0.22,
                            "b",
                            self.source_b,
                            confidence=0.78,
                            rank_source="text",
                            rank=1,
                        )
                    ],
                    quality_configured=True,
                    minimum_confidence=0.0,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=2)

        self.assertEqual(ranking.ranking_mode, "confidence")
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["b", "a"])
        self.assertAlmostEqual(ranking.hits[0].confidence or 0.0, 0.85)
        self.assertTrue(all(hit.raw_score == hit.confidence for hit in ranking.hits))

        report = export_federated_search(
            ServiceConfig(
                workspace=self.root / "workspace",
                results_directory=self.results,
            ),
            PreparedSearch(
                query_type="image_text",
                text="query",
                image_path=str(self.root / "query.jpg"),
                image_vector=[1.0],
                text_vector=[1.0],
                embedding_sources={"image": "cache", "text": "cache"},
            ),
            collections,
            top_k=2,
            image_weight=0.5,
            text_weight=0.5,
            tags=None,
            tag_mode="all",
        )
        self.assertTrue(
            all(
                result["raw_score"] == result["confidence"]
                for result in report["results"]
            )
        )
        manifest = json.loads(
            (Path(report["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertTrue(
            all(
                result["raw_score"] == result["confidence"]
                for result in manifest["results"]
            )
        )

    def test_configured_collections_use_the_strictest_shared_thresholds(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "a",
                            0.1,
                            "a",
                            self.source_a,
                            confidence=0.55,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    minimum_confidence=0.3,
                    possible_confidence=0.5,
                    high_confidence=0.75,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[],
                    quality_configured=True,
                    minimum_confidence=0.6,
                    possible_confidence=0.7,
                    high_confidence=0.8,
                ),
            ),
        ]
        ranking = rank_federated_hits(collections, top_k=10)
        self.assertEqual(ranking.hits, [])
        self.assertEqual(ranking.status, "no_reliable_match")
        self.assertEqual(ranking.filtered_count, 1)

    def test_configured_collections_rank_by_null_calibrated_confidence(self):
        offsets = {"library-a": -0.10, "library-b": 0.10}
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "a",
                            0.1,
                            "a",
                            self.source_a,
                            confidence=0.82,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    collection_calibration_mode="null_mean_offset_v1",
                    collection_confidence_offsets=offsets,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "b",
                            0.2,
                            "b",
                            self.source_b,
                            confidence=0.75,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    collection_calibration_mode="null_mean_offset_v1",
                    collection_confidence_offsets=offsets,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=2)

        self.assertEqual([hit.doc_id for hit in ranking.hits], ["b", "a"])
        self.assertEqual([hit.confidence for hit in ranking.hits], [0.75, 0.82])
        self.assertEqual(
            [hit.ranking_confidence for hit in ranking.hits],
            [0.85, 0.72],
        )
        diagnostics = federated_search_module._quality_diagnostics(
            collections,
            ranking,
        )
        self.assertEqual(
            diagnostics["collection_calibration"]["offsets"],
            offsets,
        )
        report = export_federated_search(
            ServiceConfig(
                workspace=self.root / "workspace-calibrated",
                results_directory=self.results,
            ),
            PreparedSearch(
                query_type="text",
                text="query",
                text_vector=[1.0],
                embedding_sources={"text": "cache"},
            ),
            collections,
            top_k=2,
            image_weight=0.5,
            text_weight=0.5,
            tags=None,
            tag_mode="all",
        )
        self.assertEqual(
            [result["ranking_confidence"] for result in report["results"]],
            [0.85, 0.72],
        )
        manifest = json.loads(
            (Path(report["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertEqual(
            [result["ranking_confidence"] for result in manifest["results"]],
            [0.85, 0.72],
        )

    def test_collection_calibration_unknown_library_uses_identity_offset(self):
        configured_offsets = {"library-a": -0.10}
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "a",
                            0.1,
                            "a",
                            self.source_a,
                            confidence=0.80,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    collection_calibration_mode="null_mean_offset_v1",
                    collection_confidence_offsets=configured_offsets,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "b",
                            0.2,
                            "b",
                            self.source_b,
                            confidence=0.75,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    collection_calibration_mode="null_mean_offset_v1",
                    collection_confidence_offsets=configured_offsets,
                ),
            ),
        ]

        ranking = rank_federated_hits(collections, top_k=2)

        self.assertEqual([hit.doc_id for hit in ranking.hits], ["b", "a"])
        diagnostics = federated_search_module._quality_diagnostics(
            collections,
            ranking,
        )
        self.assertEqual(
            diagnostics["collection_calibration"]["unknown_library_ids"],
            ["library-b"],
        )

    def test_configured_collections_use_strictest_shared_raw_score_threshold(self):
        for query_type in ("text", "image"):
            with self.subTest(query_type=query_type):
                collections = [
                    LibraryCandidateSet(
                        self.library_a,
                        PreparedSearchCandidates(
                            query_type=query_type,
                            hits=[
                                candidate(
                                    "a",
                                    0.10,
                                    "a",
                                    self.source_a,
                                    confidence=0.95,
                                    raw_score=0.10,
                                    rank_source=query_type,
                                )
                            ],
                            quality_configured=True,
                            minimum_confidence=0.0,
                            minimum_score=0.40,
                            score_gap=1.0,
                        ),
                    ),
                    LibraryCandidateSet(
                        self.library_b,
                        PreparedSearchCandidates(
                            query_type=query_type,
                            hits=[
                                candidate(
                                    "b",
                                    0.25,
                                    "b",
                                    self.source_b,
                                    confidence=0.90,
                                    raw_score=0.25,
                                    rank_source=query_type,
                                )
                            ],
                            quality_configured=True,
                            minimum_confidence=0.0,
                            minimum_score=0.20,
                            score_gap=1.0,
                        ),
                    ),
                ]

                ranking = rank_federated_hits(collections, top_k=5)

                self.assertEqual([hit.doc_id for hit in ranking.hits], ["a"])
                self.assertEqual(ranking.filtered_count, 1)

        combined_collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "a",
                            0.30,
                            "a",
                            self.source_a,
                            confidence=0.70,
                            rank_source="image",
                        )
                    ],
                    text_hits=[
                        candidate(
                            "a",
                            0.30,
                            "a",
                            self.source_a,
                            confidence=0.70,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    minimum_confidence=0.0,
                    minimum_score=0.70,
                    score_gap=1.0,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[
                        candidate(
                            "b",
                            0.20,
                            "b",
                            self.source_b,
                            confidence=0.80,
                            rank_source="image",
                        )
                    ],
                    text_hits=[
                        candidate(
                            "b",
                            0.20,
                            "b",
                            self.source_b,
                            confidence=0.80,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    minimum_confidence=0.0,
                    minimum_score=0.80,
                    score_gap=1.0,
                ),
            ),
        ]
        combined = rank_federated_hits(combined_collections, top_k=5)
        self.assertEqual([hit.doc_id for hit in combined.hits], ["b"])
        self.assertEqual(combined.filtered_count, 1)

    def test_configured_collections_apply_shared_top_confidence_band(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "best",
                            0.1,
                            "best",
                            self.source_a,
                            confidence=0.90,
                            rank_source="text",
                        ),
                        candidate(
                            "near",
                            0.2,
                            "near",
                            self.source_duplicate,
                            confidence=0.87,
                            rank_source="text",
                        ),
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    max_confidence_drop=0.20,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "tail",
                            0.3,
                            "tail",
                            self.source_b,
                            confidence=0.80,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=1.0,
                    max_confidence_drop=0.05,
                ),
            ),
        ]
        ranking = rank_federated_hits(collections, top_k=10)
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["best", "near"])
        self.assertEqual(ranking.filtered_count, 1)

    def test_configured_collections_apply_strictest_shared_score_gap(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "best",
                            0.1,
                            "best",
                            self.source_a,
                            confidence=0.90,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=0.05,
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[
                        candidate(
                            "tail",
                            0.2,
                            "tail",
                            self.source_b,
                            confidence=0.82,
                            rank_source="text",
                        )
                    ],
                    quality_configured=True,
                    score_gap=0.20,
                ),
            ),
        ]
        ranking = rank_federated_hits(collections, top_k=10)
        self.assertEqual([hit.doc_id for hit in ranking.hits], ["best"])
        self.assertEqual(ranking.filtered_count, 1)

    def test_zero_weight_channel_does_not_add_legacy_federated_results(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="image_text",
                    image_hits=[candidate("image", 0.1, "image", self.source_a)],
                    text_hits=[candidate("text", 0.01, "text", self.source_b)],
                ),
            )
        ]
        hits = aggregate_federated_hits(
            collections,
            image_weight=1.0,
            text_weight=0.0,
        )
        self.assertEqual([hit.doc_id for hit in hits], ["image"])

    def test_exported_report_and_manifest_include_library_attribution(self):
        collections = [
            LibraryCandidateSet(
                self.library_a,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[candidate("a", 0.10, "a" * 64, self.source_a)],
                ),
            ),
            LibraryCandidateSet(
                self.library_b,
                PreparedSearchCandidates(
                    query_type="text",
                    hits=[candidate("b", 0.20, "b" * 64, self.source_b)],
                ),
            ),
        ]
        report = export_federated_search(
            ServiceConfig(
                workspace=self.root / "workspace",
                results_directory=self.results,
            ),
            PreparedSearch(
                query_type="text",
                text="query",
                text_vector=[1.0],
                embedding_sources={"text": "api"},
                request_ids=["request-1"],
            ),
            collections,
            top_k=2,
            image_weight=0.5,
            text_weight=0.5,
            tags=None,
            tag_mode="all",
        )
        self.assertEqual(report["library_ids"], ["library-a", "library-b"])
        self.assertEqual(report["search_quality"]["max_confidence_drop"], 1.0)
        self.assertEqual(
            [hit["library_id"] for hit in report["results"]],
            ["library-a", "library-b"],
        )
        self.assertEqual(
            [hit["sha256"] for hit in report["results"]],
            ["a" * 64, "b" * 64],
        )
        manifest = json.loads(
            (Path(report["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertEqual(manifest["results"][0]["library_name"], "Library A")
        self.assertEqual(manifest["results"][0]["sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
