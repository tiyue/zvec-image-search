from __future__ import annotations

import errno
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import (
    DashScopeError,
    EmbeddingResponse,
    ImageInputError,
)
from image_vector_service.image_scanner import inspect_image, scan_folder
from image_vector_service.service import ImageVectorService


class _ConcurrentEmbeddingClient:
    def __init__(self, dimension: int, delay: float = 0.03) -> None:
        self.dimension = dimension
        self.delay = delay
        self.request_count = 0
        self.active = 0
        self.peak_active = 0
        self._lock = threading.Lock()

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_number = self.request_count
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            time.sleep(self.delay)
            vectors = [_vector(self.dimension, path.name) for path in image_paths]
            return EmbeddingResponse(
                vectors,
                f"concurrent-{request_number}",
                {"images": len(image_paths)},
            )
        finally:
            with self._lock:
                self.active -= 1


class _SelectiveInputErrorClient(_ConcurrentEmbeddingClient):
    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_number = self.request_count
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            if any("bad" in path.name for path in image_paths):
                raise ImageInputError("invalid image payload")
            vectors = [_vector(self.dimension, path.name) for path in image_paths]
            return EmbeddingResponse(
                vectors,
                f"selective-{request_number}",
                {"images": len(image_paths)},
            )
        finally:
            with self._lock:
                self.active -= 1


class _RetryableErrorClient(_ConcurrentEmbeddingClient):
    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_number = self.request_count
        if any(path.name == "image-000.png" for path in image_paths):
            raise DashScopeError(
                "temporary service outage",
                status_code=503,
                code="ServiceUnavailable",
                splittable=False,
            )
        return EmbeddingResponse(
            [_vector(self.dimension, path.name) for path in image_paths],
            f"retryable-{request_number}",
            {"images": len(image_paths)},
        )


class _BlockingEmbeddingClient(_ConcurrentEmbeddingClient):
    def __init__(self, dimension: int) -> None:
        super().__init__(dimension, delay=0)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            request_number = self.request_count
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release embedding request")
            return EmbeddingResponse(
                [_vector(self.dimension, path.name) for path in image_paths],
                f"blocking-{request_number}",
                {"images": len(image_paths)},
            )
        finally:
            self.finished.set()


class _PipelineCancelled(RuntimeError):
    pass


class BoundedImageScannerTest(unittest.TestCase):
    def test_inspection_is_concurrent_but_submission_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zvec_scan_pool_") as temporary:
            root = Path(temporary)
            _write_images(root, 40)
            active = 0
            peak_active = 0
            lock = threading.Lock()

            def delayed_inspection(path: Path, scan_root: Path, root_id: str):
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                try:
                    time.sleep(0.02)
                    return inspect_image(path, scan_root, root_id)
                finally:
                    with lock:
                        active -= 1

            with patch(
                "image_vector_service.image_scanner.inspect_image",
                side_effect=delayed_inspection,
            ):
                result = scan_folder(
                    root,
                    "bounded-root",
                    max_workers=4,
                    max_in_flight=6,
                )

        self.assertEqual(len(result.records), 40)
        self.assertGreater(peak_active, 1)
        self.assertLessEqual(peak_active, 4)
        self.assertLessEqual(result.peak_in_flight, 6)

    def test_incremental_state_lookup_is_batched_instead_of_n_plus_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zvec_scan_lookup_batch_") as temporary:
            root = Path(temporary)
            _write_images(root, 40)
            initial = scan_folder(root, "lookup-root", max_workers=2)
            previous = {
                record.doc_id: record.state_dict() for record in initial.records
            }
            lookup_calls: list[tuple[str, ...]] = []

            def lookup_many(doc_ids):
                requested = tuple(doc_ids)
                lookup_calls.append(requested)
                return {
                    doc_id: previous[doc_id]
                    for doc_id in requested
                    if doc_id in previous
                }

            with patch(
                "image_vector_service.image_scanner.inspect_image",
                side_effect=AssertionError("unchanged files must not be inspected"),
            ):
                result = scan_folder(
                    root,
                    "lookup-root",
                    previous_lookup=lambda _doc_id: (_ for _ in ()).throw(
                        AssertionError("single-row lookup must not be used")
                    ),
                    previous_lookup_many=lookup_many,
                    lookup_batch_size=7,
                    max_workers=2,
                )

        self.assertEqual(len(result.records), 40)
        self.assertEqual(len(result.fast_unchanged_ids), 40)
        self.assertEqual(len(lookup_calls), 6)
        self.assertTrue(all(len(batch) <= 7 for batch in lookup_calls))


class IndexPipelineIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="zvec_index_pipeline_"))
        self.images = self.temporary / "images"
        self.images.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def _service(
        self,
        client: object,
        *,
        batch_size: int = 1,
        results_inside_images: bool = False,
        cancel_check=None,
    ) -> ImageVectorService:
        results = self.images / "app-results" if results_inside_images else None
        config = ServiceConfig(
            workspace=self.temporary / "workspace",
            results_directory=results,
            batch_size=batch_size,
            scan_concurrency=4,
            embedding_concurrency=2,
        )
        return ImageVectorService(
            config=config,
            embedding_client=client,
            cancel_check=cancel_check,
        )

    def test_embedding_requests_are_bounded_and_commits_stay_on_owner_thread(
        self,
    ) -> None:
        _write_images(self.images, 12)
        client = _ConcurrentEmbeddingClient(1024)
        service = self._service(client)
        owner_thread = threading.get_ident()
        state_set_many = service.state.set_many
        repository_upsert = service.repository.upsert_records

        def checked_state_set_many(entries):
            self.assertEqual(threading.get_ident(), owner_thread)
            return state_set_many(entries)

        def checked_repository_upsert(records, vector, tags):
            self.assertEqual(threading.get_ident(), owner_thread)
            return repository_upsert(records, vector, tags)

        try:
            with (
                patch.object(
                    service.state,
                    "set_many",
                    side_effect=checked_state_set_many,
                ),
                patch.object(
                    service.repository,
                    "upsert_records",
                    side_effect=checked_repository_upsert,
                ),
            ):
                report = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(report.inserted, 12)
        self.assertEqual(report.failed, 0)
        self.assertGreater(client.peak_active, 1)
        self.assertLessEqual(client.peak_active, 2)
        self.assertLessEqual(report.embedding_peak_in_flight, 2)
        self.assertLessEqual(
            report.embedding_peak_bytes,
            service.config.max_inflight_request_bytes,
        )

    def test_completed_network_batches_share_one_local_bulk_commit(self) -> None:
        _write_images(self.images, 40)
        client = _ConcurrentEmbeddingClient(1024, delay=0)
        service = self._service(client, batch_size=1)
        try:
            with patch.object(
                service.repository,
                "upsert_record_vectors",
                wraps=service.repository.upsert_record_vectors,
            ) as bulk_upsert:
                report = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(report.inserted, 40)
        self.assertEqual(report.failed, 0)
        # Forty one-image API responses are buffered into one local Zvec write.
        self.assertEqual(bulk_upsert.call_count, 1)

    def test_one_bad_image_is_quarantined_and_other_images_continue(self) -> None:
        _write_images(self.images, 4)
        Image.new("RGB", (24, 24), (255, 0, 255)).save(self.images / "bad.png")
        client = _SelectiveInputErrorClient(1024, delay=0)
        service = self._service(client, batch_size=5, results_inside_images=True)
        try:
            first = service.index_folder(str(self.images))
            second = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(first.inserted, 4)
        self.assertEqual(first.failed, 1)
        self.assertEqual(first.failure_counts, {"item": 1})
        self.assertEqual(first.quarantined, 1)
        self.assertEqual(second.scanned, 5)
        self.assertEqual(second.failed, 1)
        self.assertEqual(second.quarantined, 0)
        blobs_directory = self.images / "app-results" / "failed-images" / "blobs"
        blobs = list(blobs_directory.rglob("*.*"))
        self.assertEqual(len(blobs), 1)
        manifest_lines = Path(first.failure_manifest).read_text("utf-8").splitlines()
        self.assertEqual(len(manifest_lines), 1)
        entry = json.loads(manifest_lines[0])
        self.assertEqual(entry["kind"], "item")
        self.assertEqual(entry["stage"], "embedding")
        self.assertTrue(Path(entry["blob_path"]).is_file())

    def test_storage_failure_sets_attention_without_quarantining_library(self) -> None:
        _write_images(self.images, 3)
        client = _ConcurrentEmbeddingClient(1024, delay=0)
        service = self._service(client, batch_size=3)
        try:
            with patch.object(
                service.repository,
                "upsert_record_vectors",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                report = service.index_folder(str(self.images))
            stats = service.stats()
        finally:
            service.close()

        self.assertTrue(report.needs_attention)
        self.assertEqual(report.failure_counts, {"systemic": 1})
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(report.deferred, 3)
        self.assertEqual(stats["tracked_files"], 0)
        self.assertFalse(
            any(
                path.is_file()
                for path in (
                    self.temporary
                    / "workspace"
                    / "search_results"
                    / "failed-images"
                    / "blobs"
                ).rglob("*")
            )
        )

    def test_retryable_batch_failure_does_not_stop_or_copy_healthy_image(self) -> None:
        _write_images(self.images, 4)
        client = _RetryableErrorClient(1024, delay=0)
        service = self._service(client, batch_size=1)
        try:
            report = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(report.inserted, 3)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failure_counts, {"retryable": 1})
        self.assertFalse(report.needs_attention)
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(report.deferred, 0)
        self.assertTrue(Path(report.failure_manifest).is_file())
        self.assertEqual(report.failures[0].quarantined_path, "")

    def test_embedding_cancellation_returns_without_waiting_for_network(self) -> None:
        _write_images(self.images, 1)
        client = _BlockingEmbeddingClient(1024)
        cancel_requested = threading.Event()

        def cancel_check() -> None:
            if cancel_requested.is_set():
                raise _PipelineCancelled("cancel requested")

        service = self._service(client, cancel_check=cancel_check)

        def request_cancel_after_start() -> None:
            if client.started.wait(timeout=2):
                cancel_requested.set()

        canceller = threading.Thread(
            target=request_cancel_after_start,
            daemon=True,
        )
        canceller.start()
        started_at = time.monotonic()
        try:
            with self.assertRaisesRegex(_PipelineCancelled, "cancel requested"):
                service.index_folder(str(self.images))
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 1.0)
            self.assertFalse(client.finished.is_set())
            self.assertEqual(service.state.count(), 0)

            # A request already inside the HTTP layer may finish later, but its
            # Future is detached and can never reach the owner-thread commit.
            client.release.set()
            self.assertTrue(client.finished.wait(timeout=2))
            self.assertEqual(service.state.count(), 0)
            self.assertEqual(service.repository.doc_count, 0)
        finally:
            client.release.set()
            service.close()

    def test_quarantine_disk_full_is_reported_without_stopping_batch(self) -> None:
        _write_images(self.images, 3)
        Image.new("RGB", (24, 24), (255, 0, 255)).save(self.images / "bad.png")
        client = _SelectiveInputErrorClient(1024, delay=0)
        service = self._service(client, batch_size=4)
        try:
            with patch(
                "image_vector_service.failure_sink.shutil.copy2",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                report = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(report.inserted, 3)
        self.assertEqual(report.failed, 1)
        self.assertTrue(report.needs_attention)
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(report.quarantine_copy_failures, 1)
        self.assertIn("disk full", report.failures[0].copy_error)

    def test_report_caps_failure_details_but_manifest_keeps_every_failure(self) -> None:
        for index in range(105):
            (self.images / f"broken-{index:03}.jpg").write_bytes(
                f"not-an-image-{index}".encode()
            )
        client = _ConcurrentEmbeddingClient(1024, delay=0)
        service = self._service(client)
        try:
            report = service.index_folder(str(self.images))
            row = service.state.connection.execute(
                "SELECT failed_count, failure_manifest FROM index_runs "
                "WHERE run_id = ?",
                (report.index_run_id,),
            ).fetchone()
        finally:
            service.close()

        self.assertEqual(report.failed, 105)
        self.assertEqual(len(report.failures), 100)
        self.assertEqual(report.failure_counts, {"item": 105})
        manifest_lines = Path(report.failure_manifest).read_text("utf-8").splitlines()
        self.assertEqual(len(manifest_lines), 105)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["failed_count"]), 105)
        self.assertEqual(str(row["failure_manifest"]), report.failure_manifest)


def _write_images(root: Path, count: int) -> None:
    for index in range(count):
        Image.new(
            "RGB",
            (20 + index % 3, 20 + index % 5),
            ((index * 37) % 256, (index * 67) % 256, (index * 97) % 256),
        ).save(root / f"image-{index:03}.png")


def _vector(dimension: int, value: str) -> list[float]:
    seed = sum(value.encode("utf-8")) % 997
    return [seed / 997.0, *([0.0] * (dimension - 1))]


if __name__ == "__main__":
    unittest.main()
