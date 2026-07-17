from __future__ import annotations

import unittest

from image_vector_service.hybrid_search import (
    HYBRID_TAG_WEIGHT,
    HYBRID_VECTOR_WEIGHT,
    detect_hybrid_tag_intent,
    fuse_text_and_tag_hits,
    hybrid_candidate_count,
)
from image_vector_service.models import SearchHit
from image_vector_service.tag_aliases import TagAliasDictionary, TagAliasGroup
from image_vector_service.tag_search import TagCatalog


def _hit(
    doc_id: str,
    confidence: float,
    *,
    source: str,
    matched_tags: tuple[str, ...] = (),
) -> SearchHit:
    return SearchHit(
        doc_id=doc_id,
        distance=1.0 - confidence,
        raw_score=1.0 - confidence,
        normalized_score=confidence,
        confidence=confidence,
        fields={
            "root_id": "root",
            "relative_path": f"set/{doc_id}.jpg",
            "sha256": doc_id,
            "tags": list(matched_tags),
        },
        rank_source=source,  # type: ignore[arg-type]
        matched_tags=matched_tags,
    )


class HybridSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        aliases = TagAliasDictionary([TagAliasGroup("雷电将军", ("雷神", "影"))])
        self.catalog = TagCatalog(
            ["原神", "刻晴", "坐姿", "红色", "礼服", "雷电将军"],
            aliases=aliases,
        )

    def test_explicit_fuzzy_tag_is_tag_dominant(self) -> None:
        intent = detect_hybrid_tag_intent("神", self.catalog)

        self.assertTrue(intent.enabled)
        self.assertEqual(intent.mode, "tag_dominant")
        self.assertIn("原神", intent.matched_tags)
        self.assertEqual(intent.tag_weight, HYBRID_TAG_WEIGHT)
        self.assertEqual(intent.vector_weight, HYBRID_VECTOR_WEIGHT)

    def test_compact_chinese_query_extracts_non_overlapping_tags(self) -> None:
        intent = detect_hybrid_tag_intent("原神刻晴坐姿", self.catalog)

        self.assertTrue(intent.enabled)
        self.assertEqual(intent.fragments, ("原神", "刻晴", "坐姿"))
        self.assertFalse(intent.plan.matches_nothing if intent.plan else True)

    def test_alias_token_uses_shared_dictionary(self) -> None:
        intent = detect_hybrid_tag_intent("雷神", self.catalog)

        self.assertTrue(intent.enabled)
        self.assertIn("雷电将军", intent.matched_tags)

    def test_descriptive_query_stays_semantic(self) -> None:
        intent = detect_hybrid_tag_intent("逆光下飘动的长发", self.catalog)

        self.assertFalse(intent.enabled)
        self.assertEqual(intent.vector_weight, 1.0)
        self.assertEqual(intent.tag_weight, 0.0)

    def test_tag_hit_outranks_strong_vector_only_hit(self) -> None:
        fused = fuse_text_and_tag_hits(
            [
                _hit("vector-only", 0.95, source="text"),
                _hit("both", 0.70, source="text"),
            ],
            [
                _hit("both", 1.0, source="tag", matched_tags=("坐姿",)),
                _hit("tag-only", 0.90, source="tag", matched_tags=("坐姿",)),
            ],
        )

        self.assertEqual(
            [hit.doc_id for hit in fused], ["both", "tag-only", "vector-only"]
        )
        self.assertEqual(fused[0].rank_source, "fused")
        self.assertEqual(fused[0].matched_tags, ("坐姿",))
        self.assertAlmostEqual(fused[0].confidence or 0.0, 1.0)
        self.assertAlmostEqual(fused[2].confidence or 0.0, 0.3)

    def test_candidate_pool_stays_between_fifty_and_one_hundred(self) -> None:
        self.assertEqual(hybrid_candidate_count(10, 1_000), 50)
        self.assertEqual(hybrid_candidate_count(30, 1_000), 100)
        self.assertEqual(hybrid_candidate_count(150, 1_000), 150)
        self.assertEqual(hybrid_candidate_count(10, 12), 12)


if __name__ == "__main__":
    unittest.main()
