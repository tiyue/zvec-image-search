from __future__ import annotations

import gc
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService


class FakeEmbeddingClient:
    def __init__(self, dimension: int):
        self.dimension = dimension
        self.request_count = 0

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        self.request_count += 1
        vectors = [self._image_vector(path) for path in image_paths]
        return EmbeddingResponse(
            vectors=vectors,
            request_id=f"fake-image-{self.request_count}",
            usage={"images": len(image_paths)},
        )

    def embed_text(self, text: str) -> EmbeddingResponse:
        self.request_count += 1
        lowered = text.lower()
        rgb = (
            1.0 if "red" in lowered or "红" in lowered else 0.0,
            1.0 if "green" in lowered or "绿" in lowered else 0.0,
            1.0 if "blue" in lowered or "蓝" in lowered else 0.0,
        )
        return EmbeddingResponse(
            vectors=[self._vector(rgb)],
            request_id=f"fake-text-{self.request_count}",
            usage={"text": text},
        )

    def _image_vector(self, path: Path) -> list[float]:
        with Image.open(path) as image:
            pixel = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return self._vector(tuple(channel / 255.0 for channel in pixel))

    def _vector(self, rgb: tuple[float, float, float]) -> list[float]:
        return [*rgb, 0.01, *([0.0] * (self.dimension - 4))]


class ImageVectorServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="zvec_image_service_test_"))
        cls.images_dir = cls.temp_dir / "images"
        cls.images_dir.mkdir()
        Image.new("RGB", (32, 32), (255, 0, 0)).save(cls.images_dir / "red.png")
        Image.new("RGB", (40, 24), (240, 20, 20)).save(
            cls.images_dir / "red_variant.jpg"
        )
        Image.new("RGB", (32, 32), (0, 0, 255)).save(cls.images_dir / "blue.webp")
        (cls.images_dir / "notes.txt").write_text("not an image", encoding="utf-8")
        (cls.images_dir / "broken.jpg").write_bytes(b"not a jpeg")

        cls.config = ServiceConfig(workspace=cls.temp_dir)
        cls.fake_client = FakeEmbeddingClient(cls.config.dimension)
        cls.service = ImageVectorService(
            config=cls.config,
            embedding_client=cls.fake_client,
        )

    @classmethod
    def tearDownClass(cls):
        del cls.service
        gc.collect()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_01_index_and_incremental_rerun(self):
        report = self.service.index_folder(str(self.images_dir))
        self.assertEqual(report.scanned, 5)
        self.assertEqual(report.inserted, 3)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(self.service.stats()["tracked_files"], 3)

        request_count = self.fake_client.request_count
        second = self.service.index_folder(str(self.images_dir))
        self.assertEqual(second.unchanged, 3)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.updated, 0)
        self.assertEqual(self.fake_client.request_count, request_count)

    def test_02_text_search_exports_new_directory(self):
        report = self.service.search_by_text("red image", top_k=2)
        output_dir = Path(report.output_dir)
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(report.result_count, 2)
        self.assertTrue((output_dir / "results.json").is_file())
        manifest = json.loads((output_dir / "results.json").read_text("utf-8"))
        self.assertEqual(manifest["query_type"], "text")
        self.assertEqual(len(manifest["results"]), 2)

    def test_03_image_and_combined_search(self):
        query_image = str(self.images_dir / "red.png")
        image_report = self.service.search_by_image(query_image, top_k=2)
        self.assertEqual(image_report.result_count, 2)
        self.assertNotIn(
            str((self.images_dir / "red.png").resolve()),
            [item.source_path for item in image_report.results],
        )

        combined = self.service.search_by_image_and_text(
            query_image,
            "red",
            top_k=2,
            image_weight=0.6,
            text_weight=0.4,
        )
        self.assertEqual(combined.result_count, 2)
        self.assertTrue(all(item.fused_score is not None for item in combined.results))

    def test_04_sync_removes_missing_file(self):
        blue = self.images_dir / "blue.webp"
        blue.unlink()
        report = self.service.sync_folder(str(self.images_dir))
        self.assertEqual(report.deleted, 1)
        self.assertEqual(self.service.stats()["tracked_files"], 2)


if __name__ == "__main__":
    unittest.main()
