from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService


class _EmbeddingClient:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.request_count = 0

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        self.request_count += 1
        return EmbeddingResponse(
            vectors=[self._image_vector(path) for path in image_paths],
            request_id=f"image-{self.request_count}",
            usage={"images": len(image_paths)},
        )

    def embed_text(self, text: str) -> EmbeddingResponse:
        self.request_count += 1
        normalized = text.casefold()
        rgb = (
            1.0 if "red" in normalized else 0.0,
            1.0 if "green" in normalized else 0.0,
            1.0 if "blue" in normalized else 0.0,
        )
        return EmbeddingResponse(
            vectors=[self._vector(rgb)],
            request_id=f"text-{self.request_count}",
            usage={"text": text},
        )

    def _image_vector(self, path: Path) -> list[float]:
        with Image.open(path) as image:
            red, green, blue = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return self._vector((red / 255.0, green / 255.0, blue / 255.0))

    def _vector(self, rgb: tuple[float, float, float]) -> list[float]:
        return [*rgb, 0.01, *([0.0] * (self.dimension - 4))]


class MetadataRetrievalIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_metadata_retrieval_"))
        self.images = self.root / "images"
        self.images.mkdir()
        Image.new("RGB", (24, 24), (255, 0, 0)).save(self.images / "red.png")
        Image.new("RGB", (24, 24), (0, 0, 255)).save(self.images / "blue.png")
        self.config = ServiceConfig(workspace=self.root)
        self.client = _EmbeddingClient(self.config.dimension)
        self.service = ImageVectorService(
            config=self.config,
            embedding_client=self.client,
        )
        report = self.service.index_folder(str(self.images))
        self.assertEqual(report.inserted, 2)

    def tearDown(self) -> None:
        self.service.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _accept_descriptions(self) -> None:
        descriptions = {
            "red.png": "blue costume portrait",
            "blue.png": "red costume portrait",
        }
        for entry in self.service.state.list_entries():
            self.service.state.set_document_annotation(
                doc_id=str(entry["doc_id"]),
                source_sha256=str(entry["sha256"]),
                cache_key=None,
                status="accepted",
                accepted_tags=entry.get("accepted_auto_tags", ()),
                description=descriptions[str(entry["file_name"])],
            )

    def test_backfill_enables_local_third_channel_without_search_api_cost(self) -> None:
        self._accept_descriptions()

        report = self.service.backfill_metadata_embeddings(max_images=10)

        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(report["api_requests"], 2)
        for entry in self.service.state.list_entries():
            metadata = self.service.repository.fetch_metadata(str(entry["doc_id"]))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertRegex(
                str(metadata["metadata_text_hash"]),
                r"^[0-9a-f]{64}$",
            )

        before_search = self.client.request_count
        search = self.service.search_by_text("blue", top_k=2)

        self.assertEqual(self.client.request_count, before_search + 1)
        self.assertEqual(search.ranking_mode, "visual_metadata")
        assert search.search_quality is not None
        diagnostics = search.search_quality["metadata_search"]
        self.assertTrue(diagnostics["enabled"])
        self.assertEqual(diagnostics["extra_embedding_requests"], 0)
        self.assertGreater(diagnostics["candidate_count"], 0)
        self.assertTrue(
            any(result.metadata_rank is not None for result in search.results)
        )

        prepared = self.service.prepare_search_query(text="blue")
        prepared_request_count = self.client.request_count
        candidates = self.service.query_prepared_search(prepared, candidate_k=2)
        self.assertEqual(self.client.request_count, prepared_request_count)
        self.assertEqual(len(candidates.metadata_hits), 2)
        self.assertTrue(candidates.metadata_search["enabled"])

    def test_backfill_is_resumable_and_combined_search_reuses_text_vector(self) -> None:
        self._accept_descriptions()
        first = self.service.backfill_metadata_embeddings(max_images=1)
        self.assertEqual(first["succeeded"], 1)
        self.assertEqual(first["remaining"], 1)

        second = self.service.backfill_metadata_embeddings(max_images=10)
        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(second["already_current"], 1)
        self.assertEqual(second["remaining"], 0)

        request_count = self.client.request_count
        report = self.service.search_by_image_and_text(
            str(self.images / "red.png"),
            "blue",
            top_k=2,
            image_weight=0.6,
            text_weight=0.4,
        )
        # The indexed query image is reused and the text is embedded once. The
        # description channel is a second local Zvec query, not another API call.
        self.assertEqual(self.client.request_count, request_count + 1)
        self.assertEqual(report.ranking_mode, "weighted_rrf_with_metadata")
        assert report.search_quality is not None
        self.assertTrue(report.search_quality["metadata_search"]["enabled"])
        self.assertTrue(
            any(result.metadata_rank is not None for result in report.results)
        )

        no_op = self.service.backfill_metadata_embeddings(max_images=10)
        self.assertEqual(no_op["succeeded"], 0)
        self.assertEqual(no_op["already_current"], 2)
        self.assertEqual(no_op["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
