from __future__ import annotations

import gc
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.dashscope_client import (
    DashScopeEmbeddingClient,
    DashScopeError,
    EmbeddingResponse,
)
from image_vector_service.image_data_uri import EncodedImageDataUri
from image_vector_service.models import FileFailure
from image_vector_service.result_exporter import (
    RESULT_MANIFEST_SCHEMA_VERSION,
    RESULT_OWNERSHIP_KIND,
    RESULT_OWNERSHIP_MARKER,
    RESULT_OWNERSHIP_SCHEMA_VERSION,
)
from image_vector_service.search_result_store import RESULT_STORE_FILENAME
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


class GlobalErrorClient:
    def __init__(self):
        self.request_count = 0

    def embed_images(self, _image_paths):
        self.request_count += 1
        raise DashScopeError(
            "invalid API key",
            status_code=401,
            code="InvalidApiKey",
            splittable=False,
        )


class DashScopeEncodingTest(unittest.TestCase):
    def test_embedding_client_uses_bounded_shared_data_uri_encoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "large.jpg"
            image_path.write_bytes(b"placeholder")
            client = DashScopeEmbeddingClient(ServiceConfig(workspace=root))
            encoded = EncodedImageDataUri(
                data_uri="data:image/jpeg;base64,ZmFrZQ==",
                mime_type="image/jpeg",
                source_size_bytes=15_844_736,
                encoded_payload_bytes=3_519_361,
                data_uri_bytes=4_692_507,
                width=4096,
                height=2732,
                transformed=True,
            )
            expected = EmbeddingResponse(
                vectors=[[0.0] * client.config.dimension],
                request_id="bounded-image",
                usage={},
            )
            with (
                patch(
                    "image_vector_service.dashscope_client.encode_image_data_uri",
                    return_value=encoded,
                ) as encode,
                patch.object(client, "_embed", return_value=expected) as embed,
            ):
                actual = client.embed_images([image_path])

        self.assertIs(actual, expected)
        encode.assert_called_once_with(
            image_path,
            max_source_bytes=client.config.max_source_image_bytes,
            max_data_uri_bytes=client.config.max_image_bytes,
            max_output_dimension=4096,
            force_transcode=False,
        )
        embed.assert_called_once_with([{"image": encoded.data_uri}])

    def test_embedding_client_recompresses_once_after_provider_size_rejection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "large.jpg"
            image_path.write_bytes(b"placeholder")
            client = DashScopeEmbeddingClient(ServiceConfig(workspace=root))
            first = EncodedImageDataUri(
                "data:image/jpeg;base64,Zmlyc3Q=",
                "image/jpeg",
                12_000_000,
                7_000_000,
                9_300_000,
                4096,
                2732,
                True,
            )
            second = EncodedImageDataUri(
                "data:image/jpeg;base64,c2Vjb25k",
                "image/jpeg",
                12_000_000,
                5_000_000,
                6_700_000,
                3072,
                2048,
                True,
            )
            expected = EmbeddingResponse(
                vectors=[[0.0] * client.config.dimension],
                request_id="recompressed",
                usage={},
            )
            size_error = DashScopeError(
                "image size should be [0, 10240KB]",
                status_code=400,
                code="InvalidParameter",
                splittable=True,
            )
            with (
                patch(
                    "image_vector_service.dashscope_client.encode_image_data_uri",
                    side_effect=[first, second],
                ) as encode,
                patch.object(
                    client,
                    "_embed",
                    side_effect=[size_error, expected],
                ) as embed,
            ):
                actual = client.embed_images([image_path])

        self.assertIs(actual, expected)
        self.assertEqual(embed.call_count, 2)
        self.assertEqual(encode.call_count, 2)
        retry_kwargs = encode.call_args_list[1].kwargs
        self.assertEqual(retry_kwargs["max_data_uri_bytes"], 7 * 1024 * 1024)
        self.assertEqual(retry_kwargs["max_output_dimension"], 3072)
        self.assertTrue(retry_kwargs["force_transcode"])

    def test_embedding_client_does_not_recompress_non_size_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "image.jpg"
            image_path.write_bytes(b"placeholder")
            client = DashScopeEmbeddingClient(ServiceConfig(workspace=root))
            encoded = EncodedImageDataUri(
                "data:image/jpeg;base64,ZmFrZQ==",
                "image/jpeg",
                100,
                4,
                31,
                16,
                16,
                False,
            )
            auth_error = DashScopeError(
                "invalid API key",
                status_code=401,
                code="InvalidApiKey",
            )
            with (
                patch(
                    "image_vector_service.dashscope_client.encode_image_data_uri",
                    return_value=encoded,
                ) as encode,
                patch.object(client, "_embed", side_effect=auth_error),
                self.assertRaises(DashScopeError),
            ):
                client.embed_images([image_path])

        self.assertEqual(encode.call_count, 1)


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

        cls.config = ServiceConfig(
            workspace=cls.temp_dir,
            library_id="library-main",
            library_image_root=cls.images_dir,
        )
        cls.fake_client = FakeEmbeddingClient(cls.config.dimension)
        cls.service = ImageVectorService(
            config=cls.config,
            embedding_client=cls.fake_client,
        )

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
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

        with patch(
            "image_vector_service.image_scanner.file_sha256",
            side_effect=AssertionError("unchanged files should not be rehashed"),
        ):
            fast = self.service.index_folder(str(self.images_dir))
        self.assertEqual(fast.unchanged, 3)

    def test_02_text_search_exports_new_directory(self):
        request_count = self.fake_client.request_count
        report = self.service.search_by_text("red image", top_k=2)
        output_dir = Path(report.output_dir)
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(report.result_count, 2)
        self.assertEqual(report.result_storage, "copied")
        self.assertTrue(all(item.copied_file for item in report.results))
        self.assertTrue((output_dir / "results.json").is_file())
        manifest = json.loads((output_dir / "results.json").read_text("utf-8"))
        self.assertEqual(manifest["query_type"], "text")
        self.assertEqual(manifest["ranking_mode"], "distance")
        self.assertEqual(len(manifest["results"]), 2)
        self.assertEqual(
            manifest["manifest_schema_version"], RESULT_MANIFEST_SCHEMA_VERSION
        )
        self.assertEqual(manifest["result_store"]["result_count"], 2)
        self.assertTrue((output_dir / manifest["result_store"]["path"]).is_file())
        self.assertIn("score_semantics", manifest)
        self.assertFalse(manifest["search_quality"]["configured"])
        for result in manifest["results"]:
            self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["rank_source"], "text")
            self.assertEqual(result["raw_score"], result["distance"])
            self.assertGreaterEqual(result["normalized_score"], 0.0)
            self.assertLessEqual(result["normalized_score"], 1.0)
            self.assertEqual(result["confidence"], result["normalized_score"])
            self.assertIn(result["match_state"], {"high", "possible", "weak"})
        self.assertRegex(output_dir.name, r"^red image_\d{8}_\d{6}_\d{3}$")
        self.assertEqual(report.embedding_sources, {"text": "api"})
        self.assertEqual(report.ranking_mode, "distance")
        self.assertEqual(self.fake_client.request_count, request_count + 1)

        cached = self.service.search_by_text("red image", top_k=1)
        self.assertEqual(cached.embedding_sources, {"text": "cache"})
        self.assertEqual(self.fake_client.request_count, request_count + 1)

    def test_03_image_and_combined_search(self):
        query_image = str(self.images_dir / "red.png")
        request_count = self.fake_client.request_count
        image_report = self.service.search_by_image(query_image, top_k=2)
        self.assertEqual(image_report.result_count, 2)
        self.assertNotIn(
            "red.png",
            [item.relative_path for item in image_report.results],
        )
        self.assertRegex(
            Path(image_report.output_dir).name,
            r"^图片搜索_\d{8}_\d{6}_\d{3}$",
        )
        self.assertEqual(image_report.embedding_sources, {"image": "index"})
        self.assertTrue(
            all(item.rank_source == "image" for item in image_report.results)
        )
        self.assertEqual(self.fake_client.request_count, request_count)

        combined = self.service.search_by_image_and_text(
            query_image,
            "red",
            top_k=2,
            image_weight=0.6,
            text_weight=0.4,
        )
        self.assertEqual(combined.result_count, 2)
        self.assertEqual(combined.ranking_mode, "weighted_rrf")
        self.assertTrue(all(item.fused_score is not None for item in combined.results))
        self.assertTrue(all(item.rank_source == "fused" for item in combined.results))
        self.assertTrue(
            all(item.raw_score == item.fused_score for item in combined.results)
        )
        self.assertEqual(combined.search_quality["fusion"]["mode"], "confidence_v1")
        self.assertTrue(
            all(item.image_confidence is not None for item in combined.results)
        )
        self.assertTrue(
            all(item.text_confidence is not None for item in combined.results)
        )
        self.assertTrue(all(item.image_rank is not None for item in combined.results))
        self.assertTrue(all(item.text_rank is not None for item in combined.results))
        self.assertRegex(
            Path(combined.output_dir).name,
            r"^red_图文搜索_\d{8}_\d{6}_\d{3}$",
        )
        self.assertEqual(combined.embedding_sources, {"image": "index", "text": "api"})

        prepared = self.service.prepare_search_query(text="red")
        prepared_request_count = self.fake_client.request_count
        first_candidates = self.service.query_prepared_search(prepared, candidate_k=1)
        expanded_candidates = self.service.query_prepared_search(
            prepared, candidate_k=3
        )
        self.assertEqual(self.fake_client.request_count, prepared_request_count)
        self.assertEqual(first_candidates.candidate_k, 1)
        self.assertEqual(first_candidates.collection_size, 3)
        self.assertEqual(first_candidates.next_candidate_k, 2)
        self.assertFalse(first_candidates.quality_configured)
        self.assertEqual(first_candidates.minimum_confidence, 0.0)
        self.assertEqual(expanded_candidates.candidate_k, 3)
        self.assertIsNone(expanded_candidates.next_candidate_k)
        self.assertTrue(
            all(item.hit.rank_source == "text" for item in expanded_candidates.hits)
        )

        staged_copy = self.temp_dir / "staged-red.png"
        shutil.copy2(query_image, staged_copy)
        staged_report = self.service.search_by_image(str(staged_copy), top_k=3)
        self.assertNotIn(
            "red.png",
            [item.relative_path for item in staged_report.results],
        )
        self.assertEqual(staged_report.embedding_sources, {"image": "index"})

        with patch("image_vector_service.result_exporter.shutil.copy2") as copy_file:
            source_only_image = self.service.search_by_image(
                str(staged_copy),
                top_k=3,
                copy_files=False,
                report_result_limit=1,
            )
            source_only_combined = self.service.search_by_image_and_text(
                str(staged_copy),
                "red",
                top_k=3,
                copy_files=False,
                report_result_limit=1,
            )

        copy_file.assert_not_called()
        for source_only in (source_only_image, source_only_combined):
            self.assertEqual(source_only.result_storage, "source_only")
            self.assertEqual(source_only.result_count, 2)
            self.assertEqual(len(source_only.results), 1)
            self.assertTrue(source_only.results_truncated)
            self.assertIsNone(source_only.results[0].copied_file)
            self.assertNotEqual(source_only.results[0].relative_path, "red.png")
            self.assertEqual(
                {path.name for path in Path(source_only.output_dir).iterdir()},
                {RESULT_OWNERSHIP_MARKER, "results.json", RESULT_STORE_FILENAME},
            )

        # Federated search prepares the staged image once, then asks every
        # Collection for candidates.  The per-Collection path must therefore
        # exclude by content hash and still return the requested usable budget.
        prepared_image = self.service.prepare_search_query(image_path=str(staged_copy))
        prepared_candidates = self.service.query_prepared_search(
            prepared_image,
            candidate_k=2,
        )
        self.assertIsNotNone(prepared_image.image_sha256)
        self.assertEqual(len(prepared_candidates.hits), 2)
        self.assertNotIn(
            prepared_image.image_sha256,
            [item.hit.fields.get("sha256") for item in prepared_candidates.hits],
        )
        self.assertEqual(
            {item.hit.fields.get("relative_path") for item in prepared_candidates.hits},
            {"red_variant.jpg", "blue.webp"},
        )

        external = self.temp_dir / "query-only.png"
        Image.new("RGB", (16, 16), (200, 10, 10)).save(external)
        first = self.service.search_by_image(str(external), top_k=1)
        request_count = self.fake_client.request_count
        second = self.service.search_by_image(str(external), top_k=1)
        self.assertEqual(first.embedding_sources, {"image": "api"})
        self.assertEqual(second.embedding_sources, {"image": "cache"})
        self.assertEqual(self.fake_client.request_count, request_count)

    def test_04_cache_management_cleanup_and_application_log(self):
        stats = self.service.stats()
        self.assertGreaterEqual(stats["embedding_cache"]["entries"], 3)

        old_result = self.config.results_path / "old-result"
        old_result.mkdir()
        (old_result / "results.json").write_text(
            json.dumps({"output_dir": str(old_result), "results": []}),
            encoding="utf-8",
        )
        (old_result / RESULT_OWNERSHIP_MARKER).write_text(
            json.dumps(
                {
                    "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
                    "kind": RESULT_OWNERSHIP_KIND,
                }
            ),
            encoding="utf-8",
        )
        unowned = self.config.results_path / "photos-must-survive"
        unowned.mkdir()
        (unowned / "family.jpg").write_bytes(b"not-a-result")
        old_time = time.time() - 8 * 86400
        os.utime(old_result, (old_time, old_time))
        os.utime(unowned, (old_time, old_time))
        preview = self.service.clean_results(7, dry_run=True)
        self.assertEqual(preview["matched"], 1)
        self.assertEqual(preview["skipped"], 1)
        self.assertTrue(old_result.is_dir())
        cleaned = self.service.clean_results(7)
        self.assertEqual(cleaned["deleted"], 1)
        self.assertFalse(old_result.exists())
        self.assertTrue((unowned / "family.jpg").is_file())

        log_path = self.config.log_dir / "image-service.log"
        self.assertTrue(log_path.is_file())
        self.assertNotIn("red image", log_path.read_text(encoding="utf-8"))

        cleared = self.service.clear_embedding_cache()
        self.assertGreaterEqual(cleared["deleted"], 3)
        self.assertEqual(self.service.stats()["embedding_cache"]["entries"], 0)

    def test_05_global_api_error_is_not_split(self):
        Image.new("RGB", (20, 20), (0, 255, 0)).save(self.images_dir / "green.png")
        Image.new("RGB", (20, 20), (255, 255, 0)).save(self.images_dir / "yellow.png")
        error_client = GlobalErrorClient()
        original_client = self.service._embedding_client
        self.service._embedding_client = error_client
        try:
            report = self.service.index_folder(str(self.images_dir))
            self.assertEqual(error_client.request_count, 1)
            self.assertTrue(report.needs_attention)
            self.assertEqual(report.deferred, 2)
            self.assertEqual(report.failure_counts.get("systemic"), 1)
            self.assertTrue(Path(report.failure_manifest).is_file())
            self.assertEqual(report.quarantined, 0)
        finally:
            self.service._embedding_client = original_client
            (self.images_dir / "green.png").unlink()
            (self.images_dir / "yellow.png").unlink()

    def test_06_sync_is_fail_closed_and_supports_dry_run(self):
        incomplete = MagicMock()
        incomplete.scanned = 0
        incomplete.supported = 0
        incomplete.skipped = 0
        incomplete.peak_in_flight = 0
        incomplete.warnings = []
        incomplete.failure_count = 0
        incomplete.failures = [FileFailure(str(self.images_dir), "permission denied")]
        incomplete.record_count = 0
        incomplete.complete = False
        incomplete.iter_staged_records_by_sha256.return_value = iter(())
        incomplete.count_stale_doc_ids.return_value = 0
        with patch(
            "image_vector_service.service.scan_folder_to_staging",
            return_value=incomplete,
        ):
            report = self.service.sync_folder(str(self.images_dir))
        self.assertTrue(report.sync_aborted)
        self.assertEqual(report.deleted, 0)
        self.assertEqual(self.service.stats()["tracked_files"], 3)

        blue = self.images_dir / "blue.webp"
        blue.unlink()
        dry_run = self.service.sync_folder(str(self.images_dir), dry_run=True)
        self.assertEqual(dry_run.would_delete, 1)
        self.assertEqual(dry_run.deleted, 0)
        self.assertEqual(self.service.stats()["tracked_files"], 3)

        report = self.service.sync_folder(str(self.images_dir))
        self.assertEqual(report.deleted, 1)
        self.assertEqual(self.service.stats()["tracked_files"], 2)

    def test_07_scope_change_and_concurrent_service_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            self.service.sync_folder(str(self.images_dir), recursive=False)
        with self.assertRaises(ConfigurationError):
            ImageVectorService(
                config=self.config,
                embedding_client=FakeEmbeddingClient(self.config.dimension),
            )

    def test_08_new_collection_resets_stale_state(self):
        self.service.repository.collection.destroy()
        self.service.close()
        replacement = ImageVectorService(
            config=self.config,
            embedding_client=self.fake_client,
        )
        self.service = replacement
        self.__class__.service = replacement
        self.assertTrue(replacement.state_reset)
        self.assertEqual(replacement.stats()["tracked_files"], 0)

    def test_09_limits_and_error_classification(self):
        with self.assertRaises(ValueError):
            self.service.search_by_text("red", top_k=0)
        # The default service has no artificial result-count ceiling. Actual
        # work is still bounded by the Collection document count.
        self.service._validate_top_k(1_000_001)
        self.assertTrue(
            DashScopeEmbeddingClient._is_splittable_input_error(
                400, "InvalidImage", "image format is invalid"
            )
        )
        self.assertFalse(
            DashScopeEmbeddingClient._is_splittable_input_error(
                401, "InvalidApiKey", "invalid API key"
            )
        )


class ImageTagSearchTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="zvec_image_tag_test_"))
        self.warm_dir = self.temp_dir / "warm-images"
        self.cool_dir = self.temp_dir / "cool-images"
        self.warm_dir.mkdir()
        self.cool_dir.mkdir()
        Image.new("RGB", (16, 16), (255, 0, 0)).save(self.warm_dir / "red.png")
        Image.new("RGB", (16, 16), (0, 0, 255)).save(self.cool_dir / "blue.png")
        self.config = ServiceConfig(workspace=self.temp_dir / "workspace")
        self.client = FakeEmbeddingClient(self.config.dimension)
        self.service = ImageVectorService(
            config=self.config,
            embedding_client=self.client,
        )

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _tags_for(self, path: Path) -> list[str]:
        entry = self.service.state.find_entry_for_path(path)
        self.assertIsNotNone(entry)
        assert entry is not None
        return list(entry["tags"])

    def _folder_tags_for(self, path: Path) -> list[str]:
        entry = self.service.state.find_entry_for_path(path)
        self.assertIsNotNone(entry)
        assert entry is not None
        return list(entry["folder_tags"])

    def test_folder_tags_are_local_and_remove_technical_metadata(self):
        metadata_folder = self.warm_dir / "原神-刻晴-120P-1.2GB"
        metadata_folder.mkdir()
        image = metadata_folder / "keqing.png"
        Image.new("RGB", (18, 18), (200, 120, 180)).save(image)

        report = self.service.index_folder(str(self.warm_dir))

        self.assertEqual(report.inserted, 2)
        self.assertTrue(report.index_run_id)
        self.assertEqual(self.service.state.latest_index_run_id(), report.index_run_id)
        self.assertEqual(
            len(self.service.state.entries_for_index_run(report.index_run_id)), 2
        )
        self.assertEqual(
            self._folder_tags_for(self.warm_dir / "red.png"), ["warm-images"]
        )
        self.assertEqual(self._folder_tags_for(image), ["原神-刻晴"])
        self.assertEqual(self._tags_for(image), [])
        self.assertNotIn("120P", self.service.state.list_effective_tags())
        self.assertNotIn("1.2GB", self.service.state.list_effective_tags())

    def test_index_tags_apply_only_to_new_documents(self):
        warm = self.service.index_folder(
            str(self.warm_dir), tags=["warm", "O'Reilly", "warm"]
        )
        self.assertEqual(warm.inserted, 1)
        self.assertEqual(
            self._tags_for(self.warm_dir / "red.png"), ["warm", "O'Reilly"]
        )

        Image.new("RGB", (18, 18), (255, 160, 0)).save(self.warm_dir / "orange.png")
        request_count = self.client.request_count
        second = self.service.index_folder(str(self.warm_dir), tags=["featured"])
        self.assertEqual(second.inserted, 1)
        self.assertEqual(second.updated, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(self.client.request_count, request_count + 1)
        self.assertEqual(
            self._tags_for(self.warm_dir / "red.png"),
            ["warm", "O'Reilly"],
        )
        self.assertEqual(self._tags_for(self.warm_dir / "orange.png"), ["featured"])
        self.assertTrue(all(root["tags"] == [] for root in self.service.list_roots()))

    def test_existing_document_content_update_preserves_tags(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        red = self.warm_dir / "red.png"
        Image.new("RGB", (21, 17), (0, 255, 0)).save(red)

        request_count = self.client.request_count
        updated = self.service.index_folder(str(self.warm_dir), tags=["featured"])
        self.assertEqual(updated.inserted, 0)
        self.assertEqual(updated.updated, 1)
        self.assertEqual(self.client.request_count, request_count + 1)
        self.assertEqual(self._tags_for(red), ["warm"])
        self.assertIn(
            "No new documents were indexed; requested tags were not applied.",
            updated.warnings,
        )

    def test_same_sha_rebuild_preserves_old_tags_and_tags_only_new_document(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        original = self.warm_dir / "red.png"
        existing = self.service.state.find_entry_for_path(original)
        self.assertIsNotNone(existing)
        assert existing is not None
        succeeded, failures = self.service.repository.delete([str(existing["doc_id"])])
        self.assertEqual(succeeded, [existing["doc_id"]])
        self.assertEqual(failures, {})
        self.service.validate_documents = True
        duplicate = self.warm_dir / "red-copy.png"
        shutil.copy2(original, duplicate)

        request_count = self.client.request_count
        rebuilt = self.service.index_folder(str(self.warm_dir), tags=["featured"])
        self.assertEqual(rebuilt.inserted, 1)
        self.assertEqual(rebuilt.updated, 1)
        self.assertEqual(self.client.request_count, request_count + 1)
        self.assertEqual(self._tags_for(original), ["warm"])
        self.assertEqual(self._tags_for(duplicate), ["featured"])

    def test_tags_none_does_not_reuse_legacy_root_tags(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        root_id = str(self.service.list_roots()[0]["root_id"])
        self.service.state.set_root_tags(root_id, ["legacy-root-default"])
        Image.new("RGB", (18, 18), (255, 160, 0)).save(self.warm_dir / "orange.png")
        report = self.service.index_folder(str(self.warm_dir), tags=None)
        self.assertEqual(report.inserted, 1)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(self._tags_for(self.warm_dir / "red.png"), ["warm"])
        self.assertEqual(self._tags_for(self.warm_dir / "orange.png"), [])
        self.assertTrue(all(root["tags"] == [] for root in self.service.list_roots()))

    def test_legacy_root_tag_metadata_is_cleared_by_next_index(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        root_id = str(self.service.list_roots()[0]["root_id"])
        self.service.state.set_root_tags(root_id, ["legacy-root-default"])
        self.service.close()
        self.service = ImageVectorService(
            config=self.config,
            embedding_client=self.client,
        )
        self.service.index_folder(str(self.warm_dir))
        self.assertTrue(all(root["tags"] == [] for root in self.service.list_roots()))

    def test_sync_dry_run_does_not_clear_legacy_root_tag_metadata(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        root_id = str(self.service.list_roots()[0]["root_id"])
        self.service.state.set_root_tags(root_id, ["legacy-root-default"])

        report = self.service.sync_folder(str(self.warm_dir), dry_run=True)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(self.service.list_roots()[0]["tags"], ["legacy-root-default"])

    def test_clear_tags_updates_existing_documents_without_embedding(self):
        Image.new("RGB", (18, 18), (255, 160, 0)).save(self.warm_dir / "orange.png")
        self.service.index_folder(str(self.warm_dir), tags=["warm"])
        request_count = self.client.request_count

        cleared = self.service.index_folder(str(self.warm_dir), tags=[])
        self.assertEqual(cleared.updated, 2)
        self.assertEqual(cleared.inserted, 0)
        self.assertEqual(self.client.request_count, request_count)
        self.assertEqual(self._tags_for(self.warm_dir / "red.png"), [])
        self.assertEqual(self._tags_for(self.warm_dir / "orange.png"), [])
        self.assertTrue(all(root["tags"] == [] for root in self.service.list_roots()))

        repeated = self.service.index_folder(str(self.warm_dir), tags=[])
        self.assertEqual(repeated.updated, 0)
        self.assertEqual(repeated.unchanged, 2)
        self.assertEqual(self.client.request_count, request_count)

    def test_tag_filters_keep_original_document_tags(self):
        self.service.index_folder(str(self.warm_dir), tags=["warm", "O'Reilly", "warm"])
        self.service.index_folder(str(self.cool_dir), tags=["cool"])
        request_count = self.client.request_count
        unchanged = self.service.index_folder(
            str(self.warm_dir), tags=["featured", "O'Reilly"]
        )
        self.assertEqual(unchanged.unchanged, 1)
        self.assertEqual(unchanged.updated, 0)
        self.assertEqual(self.client.request_count, request_count)

        all_tags = self.service.search_by_text(
            "red",
            top_k=10,
            tags=["warm", "O'Reilly"],
        )
        self.assertEqual(all_tags.result_count, 1)
        self.assertEqual(
            all_tags.results[0].tags,
            ["warm", "O'Reilly", "warm-images"],
        )

        missing_combination = self.service.search_by_text(
            "red",
            top_k=10,
            tags=["featured", "cool"],
        )
        self.assertEqual(missing_combination.result_count, 0)

        any_tag = self.service.search_by_text(
            "red",
            top_k=10,
            tags=["warm", "cool"],
            tag_mode="any",
        )
        self.assertEqual(any_tag.result_count, 2)
        self.assertEqual(
            {tuple(result.tags) for result in any_tag.results},
            {
                ("warm", "O'Reilly", "warm-images"),
                ("cool", "cool-images"),
            },
        )

    def test_tag_filters_use_partial_contains_matching(self):
        self.service.index_folder(str(self.warm_dir), tags=["原神", "刻晴"])

        prefix = self.service.search_by_text("red", top_k=10, tags=["原"])
        suffix = self.service.search_by_text("red", top_k=10, tags=["神"])
        all_fragments = self.service.search_by_text(
            "red", top_k=10, tags=["原", "晴"], tag_mode="all"
        )
        missing = self.service.search_by_text("red", top_k=10, tags=["不存在"])

        self.assertEqual(prefix.result_count, 1)
        self.assertEqual(suffix.result_count, 1)
        self.assertEqual(all_fragments.result_count, 1)
        self.assertEqual(missing.result_count, 0)

    def test_semantic_text_automatically_fuses_accepted_tags(self):
        self.service.index_folder(str(self.warm_dir), tags=["坐姿"])
        self.service.index_folder(str(self.cool_dir), tags=["风景"])
        request_count = self.client.request_count

        report = self.service.search_by_text(
            "坐姿 blue",
            top_k=2,
            diversify_results=False,
        )

        self.assertEqual(self.client.request_count, request_count + 1)
        self.assertEqual(report.ranking_mode, "hybrid_tag_vector")
        self.assertEqual(report.result_count, 2)
        self.assertTrue(report.results[0].relative_path.endswith("red.png"))
        self.assertIn("坐姿", report.results[0].matched_tags)
        self.assertEqual(report.results[0].rank_source, "fused")
        manifest = json.loads(
            (Path(report.output_dir) / "results.json").read_text("utf-8")
        )
        self.assertTrue(manifest["search_quality"]["hybrid_search"]["enabled"])
        self.assertEqual(
            manifest["search_quality"]["hybrid_search"]["extra_embedding_requests"],
            0,
        )
        self.assertEqual(manifest["ranking_mode"], "hybrid_tag_vector")

    def test_tag_only_search_is_fuzzy_and_never_calls_embedding(self):
        self.service.index_folder(
            str(self.warm_dir),
            tags=["\u539f\u795e", "\u523b\u6674"],
        )
        self.service.index_folder(str(self.cool_dir), tags=["\u5d29\u574f"])
        request_count = self.client.request_count

        with patch.object(
            self.service.state,
            "entries_for_any_effective_tags",
            side_effect=AssertionError("tag search must use bounded SQL Top-N"),
        ):
            prefix = self.service.search_by_tags("\u539f", top_k=10)
            suffix = self.service.search_by_tags("\u795e", top_k=10)
            missing = self.service.search_by_tags("\u4e0d\u5b58\u5728", top_k=10)

        self.assertEqual(prefix.result_count, 1)
        self.assertEqual(suffix.result_count, 1)
        self.assertEqual(missing.result_count, 0)
        self.assertEqual(missing.status, "no_reliable_match")
        self.assertEqual(self.client.request_count, request_count)
        for report in (prefix, suffix):
            self.assertEqual(report.query_type, "tag")
            self.assertEqual(report.ranking_mode, "tag_match")
            self.assertEqual(report.embedding_sources, {})
            self.assertEqual(report.request_ids, [])
            self.assertEqual(report.results[0].rank_source, "tag")
            self.assertEqual(report.results[0].matched_tags, ["\u539f\u795e"])
            manifest = json.loads(
                (Path(report.output_dir) / "results.json").read_text("utf-8")
            )
            self.assertEqual(manifest["query"]["search_mode"], "tags")
            self.assertEqual(manifest["ranking_mode"], "tag_match")


if __name__ == "__main__":
    unittest.main()
