from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from image_vector_service.annotation_service import FLASH_MODEL, _cache_key
from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService
from image_vector_service.vision_tagging_client import (
    VisionTaggingResponse,
    VisionTransportError,
    VisionUsage,
)
from tests.test_annotation_service import annotation_payload


def _vector(dimension: int, path: Path) -> list[float]:
    seed = sum(path.name.encode("utf-8")) % 997
    return [seed / 997.0, *([0.0] * (dimension - 1))]


class ImmediateEmbeddingClient:
    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.request_count = 0
        self._lock = threading.Lock()

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_id = self.request_count
        return EmbeddingResponse(
            [_vector(self.dimension, path) for path in paths],
            f"embedding-{request_id}",
            {"images": len(paths)},
        )


class OverlapEmbeddingClient(ImmediateEmbeddingClient):
    def __init__(self, dimension: int = 1024) -> None:
        super().__init__(dimension)
        self.active = 0
        self.second_started = threading.Event()
        self.release_second = threading.Event()
        self._active_lock = threading.Lock()

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_id = self.request_count
        with self._active_lock:
            self.active += 1
        try:
            if request_id == 1:
                if not self.second_started.wait(timeout=3):
                    raise TimeoutError("second embedding request did not start")
            else:
                self.second_started.set()
                if not self.release_second.wait(timeout=5):
                    raise TimeoutError("Flash request did not overlap embedding")
            return EmbeddingResponse(
                [_vector(self.dimension, path) for path in paths],
                f"overlap-{request_id}",
                {"images": len(paths)},
            )
        finally:
            with self._active_lock:
                self.active -= 1

    def is_active(self) -> bool:
        with self._active_lock:
            return self.active > 0


class _VisionClientBase:
    calls: list[str] = []
    lock = threading.Lock()

    def __init__(self, config: object, *, budget_tracker: object = None) -> None:
        self.config = config
        self.budget_tracker = budget_tracker
        self.request_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    def _success(self, path: Path) -> VisionTaggingResponse:
        self.request_count += 1
        with type(self).lock:
            type(self).calls.append(path.name)
        usage = VisionUsage(1_000, 100, 1_100, {"prompt_tokens": 1_000})
        if self.budget_tracker is not None:
            self.budget_tracker.authorize_next_image()
            self.budget_tracker.record(usage)
        pricing = getattr(self.config, "pricing", None)
        cost = (
            pricing.cost(usage.input_tokens, usage.output_tokens) if pricing else None
        )
        return VisionTaggingResponse(
            annotation_payload(character=None, work=None),
            f"vision-{path.name}",
            usage,
            cost,
        )


class OverlapVisionClient(_VisionClientBase):
    embedding_client: OverlapEmbeddingClient | None = None
    overlap_started = threading.Event()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.overlap_started = threading.Event()

    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        client = type(self).embedding_client
        if client is not None and client.is_active():
            type(self).overlap_started.set()
        if client is not None:
            client.release_second.set()
        return self._success(Path(path))


class SelectiveVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        self.request_count += 1
        with type(self).lock:
            type(self).calls.append(Path(path).name)
        if Path(path).name.startswith("item"):
            raise RuntimeError("synthetic invalid image")
        if Path(path).name.startswith("retry"):
            raise VisionTransportError(
                "synthetic rate limit",
                status_code=429,
                code="RateLimit",
            )
        self.request_count -= 1
        return self._success(Path(path))


class MutatingVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        Image.new("RGB", (71, 53), (1, 2, 3)).save(path)
        return self._success(Path(path))


class MutateFirstDuplicateVisionClient(_VisionClientBase):
    call_count = 0
    mutated_name = ""

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.call_count = 0
        cls.mutated_name = ""

    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        type(self).call_count += 1
        if type(self).call_count == 1:
            type(self).mutated_name = Path(path).name
            Image.new("RGB", (69, 41), (3, 2, 1)).save(path)
        return self._success(Path(path))


class MutatingPlusVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        model = str(getattr(self.config, "model", ""))
        if model == "qwen3-vl-plus":
            Image.new("RGB", (73, 47), (4, 5, 6)).save(path)
            return self._success(Path(path))
        response = self._success(Path(path))
        payload = annotation_payload(
            character=None,
            work=None,
            plus_recommended=True,
        )
        return VisionTaggingResponse(
            payload,
            response.request_id,
            response.usage,
            response.cost_yuan,
        )


class BlockingVisionClient(_VisionClientBase):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.finished = threading.Event()

    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        type(self).started.set()
        try:
            if not type(self).release.wait(timeout=5):
                raise TimeoutError("test did not release visual request")
            return self._success(Path(path))
        finally:
            type(self).finished.set()


class PipelineCancelled(RuntimeError):
    pass


class IndexAndAutoTagPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="zvec_combined_pipeline_"))
        self.root = self.temporary / "images"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def _service(
        self,
        embedding_client: object,
        *,
        cancel_check: object = None,
    ) -> ImageVectorService:
        return ImageVectorService(
            config=ServiceConfig(
                workspace=self.temporary / "workspace",
                batch_size=1,
                embedding_concurrency=2,
                auto_tag_concurrency=2,
            ),
            embedding_client=embedding_client,
            cancel_check=cancel_check,
        )

    @staticmethod
    def _write(path: Path, color: tuple[int, int, int]) -> None:
        Image.new("RGB", (24, 24), color).save(path)

    def test_real_model_calls_overlap_and_all_persistence_stays_on_owner(self) -> None:
        self._write(self.root / "old.png", (10, 10, 10))
        initial_client = ImmediateEmbeddingClient()
        service = self._service(initial_client)
        try:
            service.index_folder(str(self.root))
            self._write(self.root / "alpha.png", (200, 10, 10))
            shutil.copy2(self.root / "alpha.png", self.root / "alpha-copy.png")
            self._write(self.root / "beta.png", (10, 200, 10))
            overlap_client = OverlapEmbeddingClient()
            service._embedding_client = overlap_client
            OverlapVisionClient.reset()
            OverlapVisionClient.embedding_client = overlap_client
            owner_thread = threading.get_ident()

            def owner_only(original):
                def checked(*args, **kwargs):
                    self.assertEqual(threading.get_ident(), owner_thread)
                    return original(*args, **kwargs)

                return checked

            with (
                patch(
                    "image_vector_service.annotation_service."
                    "DashScopeVisionTaggingClient",
                    OverlapVisionClient,
                ),
                patch.object(
                    service.repository,
                    "upsert_records",
                    side_effect=owner_only(service.repository.upsert_records),
                ),
                patch.object(
                    service.state,
                    "set_many",
                    side_effect=owner_only(service.state.set_many),
                ),
                patch.object(
                    service.state,
                    "set_document_annotation",
                    side_effect=owner_only(service.state.set_document_annotation),
                ),
                patch.object(
                    service.auto_tag_cache,
                    "set",
                    side_effect=owner_only(service.auto_tag_cache.set),
                ),
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertTrue(OverlapVisionClient.overlap_started.is_set())
            self.assertTrue(report["pipeline"]["overlap_observed"])
            self.assertEqual(report["index"]["inserted"], 3)
            self.assertEqual(report["auto_tag"]["candidate_count"], 3)
            self.assertEqual(report["auto_tag"]["unique_image_count"], 2)
            self.assertEqual(report["auto_tag"]["succeeded"], 3)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(overlap_client.request_count, 2)
            self.assertEqual(len(OverlapVisionClient.calls), 2)
            old_entry = service.state.find_entry_for_path(self.root / "old.png")
            assert old_entry is not None
            self.assertIsNone(
                service.state.get_document_annotation(str(old_entry["doc_id"]))
            )
        finally:
            service.close()

    def test_model_failures_do_not_rollback_index_or_copy_retryable_source(
        self,
    ) -> None:
        self._write(self.root / "item.png", (200, 10, 10))
        self._write(self.root / "retry.png", (10, 200, 10))
        self._write(self.root / "success.png", (10, 10, 200))
        service = self._service(ImmediateEmbeddingClient())
        SelectiveVisionClient.reset()
        try:
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                SelectiveVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertEqual(report["index"]["inserted"], 3)
            self.assertEqual(report["index"]["failed"], 0)
            self.assertEqual(report["auto_tag"]["succeeded"], 1)
            self.assertEqual(report["auto_tag"]["failed"], 2)
            self.assertEqual(report["failed"], 2)
            self.assertFalse(report["needs_attention"])
            self.assertEqual(report["quarantined"], 1)
            manifest = Path(report["failure_manifest"])
            entries = [
                json.loads(line)
                for line in manifest.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                {entry["kind"] for entry in entries}, {"item", "retryable"}
            )
            item_entry = next(entry for entry in entries if entry["kind"] == "item")
            retry_entry = next(
                entry for entry in entries if entry["kind"] == "retryable"
            )
            self.assertTrue(Path(item_entry["blob_path"]).is_file())
            self.assertEqual(retry_entry["blob_path"], "")
            self.assertEqual(service.stats()["tracked_files"], 3)
        finally:
            service.close()

    def test_source_change_before_finalize_does_not_pollute_cache_or_blob(self) -> None:
        source = self.root / "mutate.png"
        self._write(source, (40, 80, 120))
        service = self._service(ImmediateEmbeddingClient())
        try:
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                MutatingVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            cache_count = int(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0]
            )
            self.assertEqual(cache_count, 0)
            self.assertEqual(report["auto_tag"]["succeeded"], 0)
            self.assertEqual(report["auto_tag"]["failed"], 1)
            self.assertEqual(report["quarantined"], 0)
            blob_root = service.config.results_path / "failed-images" / "blobs"
            self.assertFalse(blob_root.exists())
        finally:
            service.close()

    def test_changed_duplicate_is_skipped_while_stable_copy_is_retried(self) -> None:
        first = self.root / "duplicate-a.png"
        second = self.root / "duplicate-b.png"
        self._write(first, (80, 120, 160))
        shutil.copy2(first, second)
        service = self._service(ImmediateEmbeddingClient())
        MutateFirstDuplicateVisionClient.reset()
        try:
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                MutateFirstDuplicateVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertEqual(MutateFirstDuplicateVisionClient.call_count, 2)
            self.assertEqual(report["index"]["inserted"], 2)
            self.assertEqual(report["auto_tag"]["succeeded"], 1)
            self.assertEqual(report["auto_tag"]["failed"], 1)
            self.assertEqual(report["quarantined"], 0)
            self.assertEqual(
                set(MutateFirstDuplicateVisionClient.calls),
                {first.name, second.name},
            )
            self.assertIn(
                MutateFirstDuplicateVisionClient.mutated_name,
                {first.name, second.name},
            )
            # Directory enumeration order is not a cross-platform contract.
            # Assert against the duplicate the model test double actually changed.
            mutated = self.root / MutateFirstDuplicateVisionClient.mutated_name
            stable = second if mutated == first else first
            mutated_entry = service.state.find_entry_for_path(mutated)
            stable_entry = service.state.find_entry_for_path(stable)
            assert mutated_entry is not None
            assert stable_entry is not None
            mutated_annotation = service.state.get_document_annotation(
                str(mutated_entry["doc_id"])
            )
            stable_annotation = service.state.get_document_annotation(
                str(stable_entry["doc_id"])
            )
            assert mutated_annotation is not None
            assert stable_annotation is not None
            mutated_stat = mutated.stat()
            stable_stat = stable.stat()
            self.assertNotEqual(
                (mutated_stat.st_size, mutated_stat.st_mtime_ns),
                (
                    int(mutated_entry["size_bytes"]),
                    int(mutated_entry["mtime_ns"]),
                ),
            )
            self.assertEqual(
                (stable_stat.st_size, stable_stat.st_mtime_ns),
                (
                    int(stable_entry["size_bytes"]),
                    int(stable_entry["mtime_ns"]),
                ),
            )
            self.assertEqual(mutated_annotation["status"], "failed")
            self.assertEqual(stable_annotation["status"], "pending_review")
        finally:
            service.close()

    def test_plus_source_change_cannot_write_plus_cache_or_proposal(self) -> None:
        source = self.root / "plus-mutate.png"
        self._write(source, (30, 60, 90))
        service = self._service(ImmediateEmbeddingClient())
        MutatingPlusVisionClient.reset()
        try:
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                MutatingPlusVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            cache_rows = service.auto_tag_cache.connection.execute(
                "SELECT model, status FROM auto_tag_cache ORDER BY model"
            ).fetchall()
            self.assertEqual(
                [(str(row["model"]), str(row["status"])) for row in cache_rows],
                [("qwen3-vl-flash", "succeeded")],
            )
            self.assertEqual(report["auto_tag"]["plus_requests"], 1)
            self.assertEqual(report["auto_tag"]["succeeded"], 0)
            self.assertEqual(report["auto_tag"]["failed"], 1)
            self.assertEqual(report["auto_tag"]["proposals"], [])
            self.assertEqual(report["quarantined"], 0)
        finally:
            service.close()

    def test_cancellation_discards_late_visual_result_without_state_write(self) -> None:
        source = self.root / "cancel.png"
        self._write(source, (90, 50, 10))
        cancel_requested = threading.Event()

        def cancel_check() -> None:
            if cancel_requested.is_set():
                raise PipelineCancelled("cancel requested")

        service = self._service(
            ImmediateEmbeddingClient(),
            cancel_check=cancel_check,
        )
        BlockingVisionClient.reset()

        def request_cancel_after_start() -> None:
            if BlockingVisionClient.started.wait(timeout=3):
                cancel_requested.set()

        canceller = threading.Thread(target=request_cancel_after_start, daemon=True)
        canceller.start()
        try:
            with (
                patch(
                    "image_vector_service.annotation_service."
                    "DashScopeVisionTaggingClient",
                    BlockingVisionClient,
                ),
                self.assertRaises(PipelineCancelled),
            ):
                service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            entry = service.state.find_entry_for_path(source)
            assert entry is not None
            self.assertIsNone(
                service.state.get_document_annotation(str(entry["doc_id"]))
            )
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
            cache_key = _cache_key(str(entry["sha256"]), FLASH_MODEL)
            while_worker_runs = service.auto_tag_cache.claim(cache_key)
            self.assertFalse(while_worker_runs.is_leader)
            BlockingVisionClient.release.set()
            self.assertTrue(BlockingVisionClient.finished.wait(timeout=3))
            deadline = time.monotonic() + 3
            while True:
                after_abort = service.auto_tag_cache.claim(cache_key)
                if after_abort.is_leader:
                    break
                if time.monotonic() >= deadline:
                    self.fail("stream abort did not release its auto-tag flight")
                time.sleep(0.01)
            after_abort.release()
            self.assertIsNone(
                service.state.get_document_annotation(str(entry["doc_id"]))
            )
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
        finally:
            BlockingVisionClient.release.set()
            canceller.join(timeout=2)
            service.close()


if __name__ == "__main__":
    unittest.main()
