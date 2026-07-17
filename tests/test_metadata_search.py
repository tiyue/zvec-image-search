from __future__ import annotations

import unittest

from image_vector_service.metadata_search import (
    fuse_combined_metadata_hits,
    fuse_text_metadata_hits,
    weighted_rrf_channels,
)
from image_vector_service.models import SearchHit


def _hit(
    doc_id: str,
    rank: int,
    source: str,
    *,
    distance: float = 0.2,
    library_id: str = "",
    matched_tags: tuple[str, ...] = (),
) -> SearchHit:
    return SearchHit(
        doc_id=doc_id,
        distance=distance,
        rank=rank,
        rank_source=source,  # type: ignore[arg-type]
        fields={
            "library_id": library_id,
            "sha256": doc_id,
            "root_id": "root",
            "relative_path": f"{doc_id}.jpg",
        },
        matched_tags=matched_tags,
    )


class MetadataSearchTest(unittest.TestCase):
    def test_text_search_falls_back_to_existing_vector_order(self):
        vector = [_hit("a", 1, "text"), _hit("b", 2, "text")]

        hits, diagnostics = fuse_text_metadata_hits(vector, [])

        self.assertEqual([hit.doc_id for hit in hits], ["a", "b"])
        self.assertEqual(hits[0].raw_score, hits[0].distance)
        self.assertEqual(diagnostics["mode"], "distance")
        self.assertFalse(diagnostics["enabled"])
        self.assertEqual(diagnostics["weights"], {"text": 1.0})

    def test_metadata_channel_can_promote_cross_channel_agreement(self):
        vector = [_hit("visual", 1, "text"), _hit("shared", 2, "text")]
        metadata = [_hit("shared", 1, "metadata"), _hit("description", 2, "metadata")]

        hits, diagnostics = fuse_text_metadata_hits(vector, metadata)

        self.assertEqual(hits[0].doc_id, "shared")
        self.assertEqual(hits[0].rank_source, "fused")
        self.assertIsNotNone(hits[0].metadata_confidence)
        self.assertEqual(hits[0].metadata_rank, 1)
        self.assertEqual(
            diagnostics["weights"],
            {"metadata": 0.4, "text": 0.6},
        )
        self.assertEqual(diagnostics["extra_embedding_requests"], 0)

    def test_tag_weight_stays_seventy_percent_without_metadata(self):
        vector = [_hit("vector", 1, "text"), _hit("tagged", 2, "text")]
        tags = [_hit("tagged", 1, "tag", matched_tags=("坐姿",))]

        hits, diagnostics = fuse_text_metadata_hits(vector, [], tags)

        self.assertEqual(hits[0].doc_id, "tagged")
        self.assertEqual(
            diagnostics["weights"],
            {"tag": 0.7, "text": 0.3},
        )
        self.assertEqual(hits[0].matched_tags, ("坐姿",))

    def test_tag_remainder_is_split_when_metadata_exists(self):
        vector = [_hit("shared", 2, "text")]
        metadata = [_hit("shared", 1, "metadata")]
        tags = [_hit("shared", 1, "tag", matched_tags=("大凤",))]

        hits, diagnostics = fuse_text_metadata_hits(vector, metadata, tags)

        self.assertEqual(hits[0].doc_id, "shared")
        self.assertEqual(
            diagnostics["weights"],
            {"metadata": 0.15, "tag": 0.7, "text": 0.15},
        )
        self.assertEqual(hits[0].matched_tags, ("大凤",))

    def test_combined_search_preserves_public_image_text_weights(self):
        image = [_hit("image", 1, "image")]
        text = [_hit("text", 1, "text")]
        metadata = [_hit("metadata", 1, "metadata")]

        _hits, diagnostics = fuse_combined_metadata_hits(
            image,
            text,
            metadata,
            image_weight=0.6,
            text_weight=0.4,
        )

        self.assertEqual(
            diagnostics["weights"],
            {"image": 0.6, "metadata": 0.16, "text": 0.24},
        )

    def test_empty_or_invalid_channel_configuration_is_safe(self):
        hits, diagnostics = weighted_rrf_channels(
            {"metadata": []},
            {"metadata": 1.0},
        )
        self.assertEqual(hits, [])
        self.assertFalse(diagnostics["enabled"])
        with self.assertRaisesRegex(ValueError, "Unsupported search channels"):
            weighted_rrf_channels({"unknown": []}, {"unknown": 1.0})
        with self.assertRaisesRegex(ValueError, "non-negative"):
            weighted_rrf_channels(
                {"metadata": [_hit("a", 1, "metadata")]},
                {"metadata": -1.0},
            )

    def test_same_doc_id_in_different_libraries_is_not_collapsed(self):
        hits, _diagnostics = weighted_rrf_channels(
            {
                "metadata": [
                    _hit("same", 1, "metadata", library_id="one"),
                    _hit("same", 2, "metadata", library_id="two"),
                ]
            },
            {"metadata": 1.0},
        )

        self.assertEqual(len(hits), 2)
        self.assertEqual(
            {hit.fields["library_id"] for hit in hits},
            {"one", "two"},
        )


if __name__ == "__main__":
    unittest.main()
