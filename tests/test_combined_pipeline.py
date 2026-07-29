from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService
from image_vector_service.vision_tagging_client import (
    VisionTaggingResponse,
    VisionUsage,
)
from tests.test_annotation_service import annotation_payload


def _vector(path: Path, dimension: int = 1024) -> list[float]:
    seed = sum(path.name.encode("utf-8")) % 997
    return [seed / 997.0, *([0.0] * (dimension - 1))]


class _ImmediateEmbeddingClient:
    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.request_count = 0
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_id = self.request_count
            self.calls.append(tuple(path.name for path in paths))
        return EmbeddingResponse(
            vectors=[_vector(path, self.dimension) for path in paths],
            request_id=f"embedding-{request_id}",
            usage={"images": len(paths)},
        )


class _OverlapEmbeddingClient(_ImmediateEmbeddingClient):
    """Keep the second embedding request alive until Flash actually starts."""

    def __init__(self, dimension: int = 1024) -> None:
        super().__init__(dimension)
        self.second_started = threading.Event()
        self.flash_started = threading.Event()
        self._active = 0
        self._active_lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._active_lock:
            return self._active

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_id = self.request_count
            self.calls.append(tuple(path.name for path in paths))
        with self._active_lock:
            self._active += 1
        try:
            if request_id == 1:
                if not self.second_started.wait(timeout=5):
                    raise TimeoutError("second embedding request did not start")
            else:
                self.second_started.set()
                if not self.flash_started.wait(timeout=5):
                    raise TimeoutError("Flash did not overlap the embedding request")
            return EmbeddingResponse(
                vectors=[_vector(path, self.dimension) for path in paths],
                request_id=f"overlap-{request_id}",
                usage={"images": len(paths)},
            )
        finally:
            with self._active_lock:
                self._active -= 1


class _BlockingEmbeddingClient(_ImmediateEmbeddingClient):
    def __init__(self, dimension: int = 1024) -> None:
        super().__init__(dimension)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_id = self.request_count
            self.calls.append(tuple(path.name for path in paths))
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise TimeoutError("cancellation test did not release embedding worker")
            return EmbeddingResponse(
                vectors=[_vector(path, self.dimension) for path in paths],
                request_id=f"late-embedding-{request_id}",
                usage={"images": len(paths)},
            )
        finally:
            self.finished.set()


class _VisionClientBase:
    calls: list[str] = []
    _calls_lock = threading.Lock()

    def __init__(self, config: object, *, budget_tracker: object = None) -> None:
        self.config = config
        self.budget_tracker = budget_tracker
        self.request_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    def _record(self, path: Path) -> None:
        self.request_count += 1
        with type(self)._calls_lock:
            type(self).calls.append(path.name)

    def _success(self, path: Path) -> VisionTaggingResponse:
        self._record(path)
        usage = VisionUsage(1_000, 100, 1_100, {"prompt_tokens": 1_000})
        if self.budget_tracker is not None:
            self.budget_tracker.authorize_next_image()
            self.budget_tracker.record(usage)
        pricing = getattr(self.config, "pricing", None)
        cost = (
            pricing.cost(usage.input_tokens, usage.output_tokens)
            if pricing is not None
            else None
        )
        return VisionTaggingResponse(
            annotation=annotation_payload(character=None, work=None),
            request_id=f"vision-{path.name}",
            usage=usage,
            cost_yuan=cost,
        )


class _RecordingVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        return self._success(Path(path))


class _OverlapVisionClient(_VisionClientBase):
    embedding_client: _OverlapEmbeddingClient | None = None
    overlap_seen = threading.Event()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.overlap_seen = threading.Event()

    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        client = type(self).embedding_client
        if client is not None and client.active > 0:
            type(self).overlap_seen.set()
        if client is not None:
            client.flash_started.set()
        return self._success(Path(path))


class _SelectiveFailureVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        source = Path(path)
        if source.name.startswith("bad"):
            self._record(source)
            raise RuntimeError("synthetic per-image model failure")
        return self._success(source)


class _CapacityVisionClient(_VisionClientBase):
    release = threading.Event()
    workers_started = threading.Event()
    expected_workers = 2
    active = 0
    peak_active = 0
    _active_lock = threading.Lock()

    @classmethod
    def reset(cls, expected_workers: int = 2) -> None:
        super().reset()
        cls.release = threading.Event()
        cls.workers_started = threading.Event()
        cls.expected_workers = expected_workers
        cls.active = 0
        cls.peak_active = 0

    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        with type(self)._active_lock:
            type(self).active += 1
            type(self).peak_active = max(type(self).peak_active, type(self).active)
            if type(self).active >= type(self).expected_workers:
                type(self).workers_started.set()
        try:
            if not type(self).release.wait(timeout=5):
                raise TimeoutError("capacity test did not release model workers")
            return self._success(Path(path))
        finally:
            with type(self)._active_lock:
                type(self).active -= 1


class _CancellationVisionClient(_VisionClientBase):
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
                raise TimeoutError("cancellation test did not release model worker")
            return self._success(Path(path))
        finally:
            type(self).finished.set()


class _MutatingVisionClient(_VisionClientBase):
    def tag_image(self, path: Path, *, context: object) -> VisionTaggingResponse:
        source = Path(path)
        Image.new("RGB", (67, 41), (1, 2, 3)).save(source)
        return self._success(source)


class _PipelineCancelled(RuntimeError):
    pass


class CombinedPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="zvec_combined_contract_"))
        self.root = self.temporary / "images"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def _service(
        self,
        embedding_client: object,
        *,
        cancel_check: object = None,
        **overrides: Any,
    ) -> ImageVectorService:
        values: dict[str, Any] = {
            "workspace": self.temporary / "workspace",
            "batch_size": 1,
            "embedding_concurrency": 2,
            "auto_tag_concurrency": 2,
        }
        values.update(overrides)
        return ImageVectorService(
            config=ServiceConfig(**values),
            embedding_client=embedding_client,
            cancel_check=cancel_check,
        )

    @staticmethod
    def _write_image(
        path: Path,
        color: tuple[int, int, int],
        *,
        size: tuple[int, int] = (24, 24),
    ) -> None:
        Image.new("RGB", size, color).save(path)

    @staticmethod
    def _annotation_for(
        service: ImageVectorService, path: Path
    ) -> dict[str, Any] | None:
        entry = service.state.find_entry_for_path(path)
        assert entry is not None
        return service.state.get_document_annotation(str(entry["doc_id"]))

    def test_embedding_and_flash_overlap_while_persistence_stays_on_owner(self) -> None:
        self._write_image(self.root / "alpha.png", (220, 20, 20))
        self._write_image(self.root / "beta.png", (20, 220, 20))
        embedding = _OverlapEmbeddingClient()
        service = self._service(embedding)
        _OverlapVisionClient.reset()
        _OverlapVisionClient.embedding_client = embedding
        owner_thread = threading.get_ident()
        persistence_calls: list[tuple[str, int]] = []
        persistence_lock = threading.Lock()

        def checked(label: str, original: Any) -> Any:
            def invoke(*args: Any, **kwargs: Any) -> Any:
                with persistence_lock:
                    persistence_calls.append((label, threading.get_ident()))
                return original(*args, **kwargs)

            return invoke

        persistence_methods = (
            ("repository.upsert_records", service.repository, "upsert_records"),
            ("repository.set_tag_catalog", service.repository, "set_tag_catalog"),
            ("repository.optimize", service.repository, "optimize"),
            ("state.ensure_root", service.state, "ensure_root"),
            ("state.begin_index_run", service.state, "begin_index_run"),
            ("state.set_many", service.state, "set_many"),
            (
                "state.record_index_run_entries",
                service.state,
                "record_index_run_entries",
            ),
            ("state.record_root", service.state, "record_root"),
            ("state.finish_index_run", service.state, "finish_index_run"),
            (
                "state.set_document_annotation",
                service.state,
                "set_document_annotation",
            ),
            ("cache.set", service.auto_tag_cache, "set"),
        )
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "image_vector_service.model_services.aliyun.vision."
                        "AliyunVisionTaggingProvider",
                        _OverlapVisionClient,
                    )
                )
                for label, target, method_name in persistence_methods:
                    original = getattr(target, method_name)
                    stack.enter_context(
                        patch.object(
                            target,
                            method_name,
                            side_effect=checked(label, original),
                        )
                    )
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertTrue(_OverlapVisionClient.overlap_seen.is_set())
            self.assertTrue(report["pipeline"]["overlap_observed"])
            self.assertGreaterEqual(report["pipeline"]["embedding_peak_in_flight"], 2)
            self.assertGreaterEqual(report["pipeline"]["flash_peak_in_flight"], 1)
            self.assertTrue(persistence_calls)
            self.assertEqual(
                {thread_id for _label, thread_id in persistence_calls},
                {owner_thread},
            )
            self.assertIn(
                "cache.set",
                {label for label, _thread_id in persistence_calls},
            )
            self.assertIn(
                "state.set_document_annotation",
                {label for label, _thread_id in persistence_calls},
            )
        finally:
            embedding.flash_started.set()
            service.close()

    def test_only_inserted_documents_are_offered_for_auto_tagging(self) -> None:
        stable = self.root / "stable.png"
        updated = self.root / "updated.png"
        inserted = self.root / "inserted.png"
        self._write_image(stable, (10, 20, 30))
        self._write_image(updated, (40, 50, 60))
        service = self._service(_ImmediateEmbeddingClient())
        try:
            initial = service.index_folder(str(self.root))
            self.assertEqual(initial.inserted, 2)
            self._write_image(updated, (200, 10, 30), size=(31, 29))
            self._write_image(inserted, (10, 200, 30))
            _RecordingVisionClient.reset()

            with patch(
                "image_vector_service.model_services.aliyun.vision.AliyunVisionTaggingProvider",
                _RecordingVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertEqual(report["index"]["inserted"], 1)
            self.assertEqual(report["index"]["updated"], 1)
            self.assertEqual(report["index"]["unchanged"], 1)
            self.assertEqual(report["auto_tag"]["candidate_count"], 1)
            self.assertEqual(_RecordingVisionClient.calls, [inserted.name])
            self.assertIsNone(self._annotation_for(service, stable))
            self.assertIsNone(self._annotation_for(service, updated))
            inserted_annotation = self._annotation_for(service, inserted)
            assert inserted_annotation is not None
            inserted_entry = service.state.find_entry_for_path(inserted)
            assert inserted_entry is not None
            self.assertEqual(inserted_annotation["status"], "accepted")
            self.assertEqual(inserted_annotation["proposed_tags"], [])
            self.assertEqual(inserted_entry["accepted_auto_tags"], ["Cosplay"])
            self.assertIn("Cosplay", inserted_entry["effective_tags"])
        finally:
            service.close()

    def test_one_sha_uses_one_embedding_and_one_flash_for_multiple_proposals(
        self,
    ) -> None:
        first = self.root / "copy-a.png"
        second = self.root / "copy-b.png"
        self._write_image(first, (80, 120, 160))
        shutil.copy2(first, second)
        embedding = _ImmediateEmbeddingClient()
        service = self._service(embedding)
        _RecordingVisionClient.reset()
        try:
            with patch(
                "image_vector_service.model_services.aliyun.vision.AliyunVisionTaggingProvider",
                _RecordingVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertEqual(report["index"]["inserted"], 2)
            self.assertEqual(embedding.request_count, 1)
            self.assertEqual(sum(len(call) for call in embedding.calls), 1)
            self.assertEqual(len(_RecordingVisionClient.calls), 1)
            self.assertEqual(report["auto_tag"]["candidate_count"], 2)
            self.assertEqual(report["auto_tag"]["unique_image_count"], 1)
            self.assertEqual(report["auto_tag"]["succeeded"], 2)
            self.assertEqual(len(report["auto_tag"]["proposals"]), 2)
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                1,
            )
            for path in (first, second):
                annotation = self._annotation_for(service, path)
                entry = service.state.find_entry_for_path(path)
                assert annotation is not None
                assert entry is not None
                self.assertEqual(annotation["status"], "accepted")
                self.assertEqual(annotation["proposed_tags"], [])
                self.assertEqual(entry["accepted_auto_tags"], ["Cosplay"])
                self.assertIn("Cosplay", entry["effective_tags"])
        finally:
            service.close()

    def test_one_model_failure_does_not_stop_other_images(self) -> None:
        bad = self.root / "bad.png"
        good_a = self.root / "good-a.png"
        good_b = self.root / "good-b.png"
        self._write_image(bad, (200, 20, 20))
        self._write_image(good_a, (20, 200, 20))
        self._write_image(good_b, (20, 20, 200))
        service = self._service(_ImmediateEmbeddingClient())
        _SelectiveFailureVisionClient.reset()
        try:
            with patch(
                "image_vector_service.model_services.aliyun.vision.AliyunVisionTaggingProvider",
                _SelectiveFailureVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertEqual(report["index"]["inserted"], 3)
            self.assertEqual(report["index"]["failed"], 0)
            self.assertEqual(report["auto_tag"]["failed"], 1)
            self.assertEqual(report["auto_tag"]["succeeded"], 2)
            self.assertEqual(report["failed"], 1)
            self.assertFalse(report["needs_attention"])
            self.assertEqual(self._annotation_for(service, bad)["status"], "failed")
            for path in (good_a, good_b):
                annotation = self._annotation_for(service, path)
                entry = service.state.find_entry_for_path(path)
                assert annotation is not None
                assert entry is not None
                self.assertEqual(annotation["status"], "accepted")
                self.assertEqual(annotation["proposed_tags"], [])
                self.assertEqual(entry["accepted_auto_tags"], ["Cosplay"])
                self.assertIn("Cosplay", entry["effective_tags"])
        finally:
            service.close()

    def test_pending_futures_and_bytes_are_bounded_by_configuration(self) -> None:
        for index in range(8):
            self._write_image(
                self.root / f"image-{index}.png",
                ((index * 31) % 255, (index * 61) % 255, (index * 97) % 255),
            )
        max_inflight_bytes = 2_200_000
        service = self._service(
            _ImmediateEmbeddingClient(),
            embedding_concurrency=3,
            auto_tag_concurrency=2,
            max_image_bytes=256 * 1024,
            max_request_bytes=512 * 1024,
            max_inflight_request_bytes=max_inflight_bytes,
        )
        _CapacityVisionClient.reset(expected_workers=2)

        def release_when_workers_are_full() -> None:
            if _CapacityVisionClient.workers_started.wait(timeout=5):
                _CapacityVisionClient.release.set()

        releaser = threading.Thread(target=release_when_workers_are_full, daemon=True)
        releaser.start()
        try:
            with patch(
                "image_vector_service.model_services.aliyun.vision.AliyunVisionTaggingProvider",
                _CapacityVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertTrue(_CapacityVisionClient.workers_started.is_set())
            self.assertLessEqual(_CapacityVisionClient.peak_active, 2)
            self.assertLessEqual(
                report["index"]["embedding_peak_in_flight"],
                service.config.embedding_concurrency,
            )
            self.assertLessEqual(
                report["index"]["embedding_peak_bytes"],
                max_inflight_bytes,
            )
            self.assertLessEqual(
                report["pipeline"]["flash_peak_pending"],
                report["pipeline"]["flash_queue_capacity"],
            )
            self.assertLessEqual(
                report["pipeline"]["flash_peak_pending_bytes"],
                max_inflight_bytes,
            )
            # Each Flash request reserves about 1 MiB. This cap must prevent a
            # third pending request even though the count-based queue allows four.
            self.assertLessEqual(report["pipeline"]["flash_peak_pending"], 2)
        finally:
            _CapacityVisionClient.release.set()
            releaser.join(timeout=2)
            service.close()

    def test_cancellation_discards_a_late_model_result(self) -> None:
        source = self.root / "cancel.png"
        self._write_image(source, (90, 50, 10))
        cancel_requested = threading.Event()

        def cancel_check() -> None:
            if cancel_requested.is_set():
                raise _PipelineCancelled("cancel requested")

        service = self._service(
            _ImmediateEmbeddingClient(),
            cancel_check=cancel_check,
        )
        _CancellationVisionClient.reset()

        def request_cancel_after_model_starts() -> None:
            if _CancellationVisionClient.started.wait(timeout=5):
                cancel_requested.set()

        canceller = threading.Thread(
            target=request_cancel_after_model_starts,
            daemon=True,
        )
        canceller.start()
        try:
            with (
                patch(
                    "image_vector_service.model_services.aliyun.vision."
                    "AliyunVisionTaggingProvider",
                    _CancellationVisionClient,
                ),
                self.assertRaises(_PipelineCancelled),
            ):
                service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertIsNone(self._annotation_for(service, source))
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
            _CancellationVisionClient.release.set()
            self.assertTrue(_CancellationVisionClient.finished.wait(timeout=5))
            self.assertIsNone(self._annotation_for(service, source))
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
        finally:
            _CancellationVisionClient.release.set()
            canceller.join(timeout=2)
            service.close()

    def test_cancellation_does_not_wait_for_or_commit_late_embedding(self) -> None:
        source = self.root / "blocked-embedding.png"
        self._write_image(source, (70, 80, 90))
        embedding = _BlockingEmbeddingClient()
        cancel_requested = threading.Event()
        cancel_times: list[float] = []

        def cancel_check() -> None:
            if cancel_requested.is_set():
                raise _PipelineCancelled("cancel blocked embedding")

        service = self._service(embedding, cancel_check=cancel_check)

        def request_cancel_after_embedding_starts() -> None:
            if embedding.started.wait(timeout=5):
                cancel_times.append(perf_counter())
                cancel_requested.set()

        canceller = threading.Thread(
            target=request_cancel_after_embedding_starts,
            daemon=True,
        )
        canceller.start()
        try:
            with (
                patch.object(
                    service.repository,
                    "upsert_records",
                    wraps=service.repository.upsert_records,
                ) as upsert_records,
                patch.object(
                    service.state,
                    "set_many",
                    wraps=service.state.set_many,
                ) as set_many,
            ):
                with self.assertRaises(_PipelineCancelled):
                    service.index_and_auto_tag_folder(
                        str(self.root),
                        max_images=20,
                        external_processing_confirmed=True,
                    )

                self.assertTrue(cancel_times)
                self.assertLess(perf_counter() - cancel_times[0], 1.0)
                self.assertEqual(upsert_records.call_count, 0)
                self.assertEqual(set_many.call_count, 0)
                self.assertEqual(service.repository.doc_count, 0)
                self.assertEqual(service.state.count(), 0)

                # Releasing the detached worker produces a valid response, but
                # the cancelled owner has discarded its Future and must not
                # apply that response later on a worker thread.
                embedding.release.set()
                self.assertTrue(embedding.finished.wait(timeout=5))
                self.assertEqual(upsert_records.call_count, 0)
                self.assertEqual(set_many.call_count, 0)
                self.assertEqual(service.repository.doc_count, 0)
                self.assertEqual(service.state.count(), 0)
                self.assertEqual(
                    service.auto_tag_cache.connection.execute(
                        "SELECT COUNT(*) FROM auto_tag_cache"
                    ).fetchone()[0],
                    0,
                )
        finally:
            embedding.release.set()
            canceller.join(timeout=2)
            service.close()

    def test_source_change_does_not_cache_old_sha_or_quarantine_new_bytes(self) -> None:
        source = self.root / "mutated.png"
        self._write_image(source, (40, 80, 120))
        old_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        service = self._service(_ImmediateEmbeddingClient())
        _MutatingVisionClient.reset()
        try:
            with patch(
                "image_vector_service.model_services.aliyun.vision.AliyunVisionTaggingProvider",
                _MutatingVisionClient,
            ):
                report = service.index_and_auto_tag_folder(
                    str(self.root),
                    max_images=20,
                    external_processing_confirmed=True,
                )

            self.assertNotEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(), old_sha
            )
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache WHERE sha256 = ?",
                    (old_sha,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(report["auto_tag"]["succeeded"], 0)
            self.assertEqual(report["auto_tag"]["failed"], 1)
            self.assertEqual(report["quarantined"], 0)
            blob_root = service.config.results_path / "failed-images" / "blobs"
            self.assertFalse(blob_root.exists() and any(blob_root.rglob("*")))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
