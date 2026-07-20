from __future__ import annotations

import errno
import os
import shutil
import tempfile
import threading
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import cast
from unittest.mock import ANY, Mock, patch

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.image_scanner import StagedScanResult
from image_vector_service.models import ImageRecord, IndexReport
from image_vector_service.scan_staging import ScanStaging, StagedImageRecord
from image_vector_service.service import ImageVectorService

# Zvec native logging is process-global and its file handle intentionally stays
# open for the process lifetime.  Keep that handle outside per-test workspaces
# so Windows can remove each temporary Collection deterministically.
os.environ.setdefault(
    "ZVEC_IMAGE_LOG_DIR",
    str(Path(tempfile.gettempdir()) / "zvec-native-test-logs"),
)


class _EmbeddingClient:
    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.request_count = 0
        self.active = 0
        self.peak_active = 0
        self._lock = threading.Lock()

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        with self._lock:
            self.request_count += 1
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            request_id = self.request_count
        try:
            return EmbeddingResponse(
                [
                    [float(index + 1), *([0.0] * (self.dimension - 1))]
                    for index, _path in enumerate(image_paths)
                ],
                f"streaming-{request_id}",
                {"images": len(image_paths)},
            )
        finally:
            with self._lock:
                self.active -= 1


class _InsertedCollector:
    def __init__(self, limit: int = 10_000) -> None:
        self.limit = limit
        self.candidate_count = 0
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def offer(self, content_hash: str, entries: list[dict[str, object]]) -> None:
        selected = entries[: max(0, self.limit - self.candidate_count)]
        if not selected:
            return
        self.calls.append((content_hash, selected))
        self.candidate_count += len(selected)


class _SyntheticStagedScan:
    def __init__(self, count: int, root_id: str) -> None:
        self.count = count
        self.root_id = root_id
        self.max_batch = 0
        self.page_count = 0

    def iter_staged_records_by_sha256(
        self,
        *,
        batch_size: int,
    ) -> Iterable[list[StagedImageRecord]]:
        for offset in range(0, self.count, batch_size):
            end = min(self.count, offset + batch_size)
            page = [
                _synthetic_record(index, self.root_id) for index in range(offset, end)
            ]
            self.max_batch = max(self.max_batch, len(page))
            self.page_count += 1
            yield page


class StreamingIndexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec-stream-index-")
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(
        self,
        client: _EmbeddingClient,
        *,
        batch_size: int = 5,
    ) -> ImageVectorService:
        return ImageVectorService(
            config=ServiceConfig(
                workspace=self.root / "workspace",
                batch_size=batch_size,
                scan_concurrency=4,
                embedding_concurrency=2,
            ),
            embedding_client=client,
        )

    def test_duplicate_group_embeds_once_writes_bounded_and_callbacks_once(
        self,
    ) -> None:
        _write_duplicate_files(self.images, 601)
        client = _EmbeddingClient()
        service = self._service(client)
        collector = _InsertedCollector()
        write_batch_sizes: list[int] = []
        original_upsert = service.repository.upsert_records

        def capture_upsert(
            records: list[ImageRecord],
            vector: list[float],
            tags: Iterable[str] = (),
        ) -> tuple[list[str], dict[str, str]]:
            write_batch_sizes.append(len(records))
            return original_upsert(records, vector, tags)

        try:
            with (
                patch(
                    "image_vector_service.image_scanner.scan_folder",
                    side_effect=AssertionError("legacy scanner must not be called"),
                ),
                patch.object(
                    service.repository,
                    "upsert_records",
                    side_effect=capture_upsert,
                ),
            ):
                report = service._index(
                    str(self.images),
                    recursive=True,
                    sync_deleted=False,
                    verify_hash=False,
                    dry_run=False,
                    allow_scope_change=False,
                    tags=None,
                    inserted_commit_callback=collector.offer,
                )
        finally:
            service.close()

        self.assertEqual(report.inserted, 601)
        self.assertEqual(report.failed, 0)
        self.assertEqual(client.request_count, 1)
        self.assertTrue(write_batch_sizes)
        self.assertLessEqual(max(write_batch_sizes), 256)
        self.assertEqual(len(collector.calls), 1)
        self.assertEqual(len(collector.calls[0][1]), 601)
        self.assertEqual(
            list((self.root / "workspace" / ".scan-staging").glob("*.sqlite3")),
            [],
        )

    def test_existing_duplicate_vector_read_failure_never_calls_model_again(
        self,
    ) -> None:
        source = self.images / "source.png"
        Image.new("RGB", (8, 8), (12, 34, 56)).save(source)
        client = _EmbeddingClient()
        service = self._service(client)
        try:
            first = service.index_folder(str(self.images))
            _link_or_copy(source, self.images / "duplicate.png")
            request_count = client.request_count

            def fail_vector_read(doc_ids: Iterable[str]):
                ids = list(doc_ids)
                return {}, {doc_id: "disk read failed" for doc_id in ids}

            with patch.object(
                service.repository,
                "fetch_vectors",
                side_effect=fail_vector_read,
            ):
                second = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.deferred, 1)
        self.assertTrue(second.needs_attention)
        self.assertEqual(client.request_count, request_count)

    def test_duplicate_callback_respects_bounded_auto_tag_limit(self) -> None:
        _write_duplicate_files(self.images, 601)
        client = _EmbeddingClient()
        service = self._service(client)
        collector = _InsertedCollector(limit=100)
        try:
            report = service._index(
                str(self.images),
                recursive=True,
                sync_deleted=False,
                verify_hash=False,
                dry_run=False,
                allow_scope_change=False,
                tags=None,
                inserted_commit_callback=collector.offer,
            )
        finally:
            service.close()

        self.assertEqual(report.inserted, 601)
        self.assertEqual(client.request_count, 1)
        self.assertEqual(collector.candidate_count, 100)
        self.assertEqual(len(collector.calls), 1)
        callback_ids = {
            str(entry["doc_id"])
            for _content_hash, entries in collector.calls
            for entry in entries
        }
        self.assertEqual(len(callback_ids), 100)

    def test_pipeline_exception_retires_complete_staging(self) -> None:
        Image.new("RGB", (8, 8), (90, 80, 70)).save(self.images / "source.png")
        client = _EmbeddingClient()
        service = self._service(client)
        try:
            with (
                patch.object(
                    service,
                    "_run_embedding_pipeline",
                    side_effect=RuntimeError("synthetic pipeline failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "synthetic pipeline failure"),
            ):
                service.index_folder(str(self.images))

            artifacts = list(
                (self.root / "workspace" / ".scan-staging").glob("*.sqlite3")
            )
            self.assertEqual(artifacts, [])
        finally:
            service.close()

    def test_pipeline_cancellation_retires_complete_staging(self) -> None:
        Image.new("RGB", (8, 8), (10, 20, 30)).save(self.images / "source.png")

        class ExpectedCancellation(RuntimeError):
            pass

        service = self._service(_EmbeddingClient())
        try:
            with (
                patch.object(
                    service,
                    "_run_embedding_pipeline",
                    side_effect=ExpectedCancellation("cancel requested"),
                ),
                self.assertRaisesRegex(ExpectedCancellation, "cancel requested"),
            ):
                service.index_folder(str(self.images))

            self.assertEqual(
                list((self.root / "workspace" / ".scan-staging").glob("*.sqlite3")),
                [],
            )
        finally:
            service.close()

    def test_service_startup_retires_validated_crash_scan_artifacts(self) -> None:
        staging_directory = self.root / "workspace" / ".scan-staging"
        scanning = ScanStaging.create(staging_directory, root_id="old-root-a")
        scanning_path = scanning.path
        scanning.close()
        ready = ScanStaging.create(staging_directory, root_id="old-root-b")
        ready.mark_ready(
            scanned=0,
            supported=0,
            skipped=0,
            peak_in_flight=0,
            complete=True,
            failure_count=0,
        )
        ready_path = ready.path
        ready.close()

        service = self._service(_EmbeddingClient())
        try:
            self.assertFalse(scanning_path.exists())
            self.assertFalse(ready_path.exists())
        finally:
            service.close()

    def test_service_startup_cleanup_failure_warns_without_blocking(self) -> None:
        staging_directory = self.root / "workspace" / ".scan-staging"
        ready = ScanStaging.create(staging_directory, root_id="old-root")
        ready.mark_ready(
            scanned=0,
            supported=0,
            skipped=0,
            peak_in_flight=0,
            complete=True,
            failure_count=0,
        )
        ready_path = ready.path
        ready.close()
        logger = Mock()
        logger.handlers = []

        with (
            patch(
                "image_vector_service.scan_staging._remove_staging_files",
                side_effect=PermissionError("synthetic locked staging"),
            ),
            patch(
                "image_vector_service.service.get_app_logger",
                return_value=logger,
            ),
        ):
            service = self._service(_EmbeddingClient())
            try:
                self.assertTrue(ready_path.exists())
                self.assertEqual(service.stats()["tracked_files"], 0)
            finally:
                service.close()

        logger.warning.assert_any_call(
            "scan_staging_retirement_warning warning=%s",
            ANY,
        )
        ready.discard()

    def test_sync_keeps_seen_but_temporarily_invalid_source(self) -> None:
        source = self.images / "source.jpg"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(source)
        client = _EmbeddingClient()
        service = self._service(client)
        try:
            first = service.index_folder(str(self.images))
            doc_id = service.state.list_entries()[0]["doc_id"]
            source.write_bytes(b"temporarily incomplete image")
            second = service.sync_folder(str(self.images))
            entry_after = service.state.get(str(doc_id))
        finally:
            service.close()

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.failed, 1)
        self.assertEqual(second.would_delete, 0)
        self.assertEqual(second.deleted, 0)
        self.assertIsNotNone(entry_after)

    def test_storage_halt_counts_each_unsubmitted_group_once(self) -> None:
        _write_unique_files(self.images, 300)
        client = _EmbeddingClient()
        service = self._service(client, batch_size=5)
        try:
            with patch.object(
                service.repository,
                "upsert_record_vectors",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                report = service.index_folder(str(self.images))
        finally:
            service.close()

        self.assertEqual(report.inserted, 0)
        self.assertEqual(report.deferred, 300)
        self.assertEqual(report.failure_counts, {"systemic": 1})
        self.assertTrue(report.needs_attention)

    def test_hundred_thousand_unchanged_records_use_page_bounded_state_reads(
        self,
    ) -> None:
        count = 100_000
        root_id = "synthetic-root"
        client = _EmbeddingClient()
        service = self._service(client)
        scan = _SyntheticStagedScan(count, root_id)
        observed_batch_sizes: list[int] = []

        def get_many(doc_ids: Iterable[str]):
            ids = list(doc_ids)
            observed_batch_sizes.append(len(ids))
            return {
                doc_id: _synthetic_entry(int(doc_id.removeprefix("doc-")), root_id)
                for doc_id in ids
            }

        try:
            report = IndexReport(root_path=str(self.images))
            with patch.object(service.state, "get_many", side_effect=get_many):
                plan_count = len(
                    list(
                        service._iter_staged_group_plans(
                            cast(StagedScanResult, scan),
                            self.images,
                            False,
                            report,
                            None,
                        )
                    )
                )
        finally:
            service.close()

        self.assertEqual(plan_count, 0)
        self.assertEqual(report.unchanged, count)
        self.assertEqual(scan.max_batch, 256)
        self.assertEqual(scan.page_count, (count + 255) // 256)
        self.assertEqual(len(observed_batch_sizes), scan.page_count)
        self.assertLessEqual(max(observed_batch_sizes), 256)
        self.assertEqual(client.request_count, 0)


def _write_duplicate_files(root: Path, count: int) -> None:
    source = root / "image-0000.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(source)
    for index in range(1, count):
        _link_or_copy(source, root / f"image-{index:04d}.png")


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _write_unique_files(root: Path, count: int) -> None:
    for index in range(count):
        Image.new(
            "RGB",
            (8, 8),
            (index % 251, (index * 3) % 251, (index * 7) % 251),
        ).save(root / f"image-{index:04d}.png")


def _synthetic_record(index: int, root_id: str) -> StagedImageRecord:
    name = f"image-{index:07d}.jpg"
    return StagedImageRecord(
        sequence=index,
        doc_id=f"doc-{index}",
        root_id=root_id,
        relative_path=name,
        file_name=name,
        extension="jpg",
        mime_type="image/jpeg",
        sha256=f"{index:064x}",
        size_bytes=100,
        mtime_ns=1_000,
        width=8,
        height=8,
        fast_unchanged=True,
    )


def _synthetic_entry(index: int, root_id: str) -> dict[str, object]:
    record = _synthetic_record(index, root_id)
    return {
        "doc_id": record.doc_id,
        "root_id": record.root_id,
        "relative_path": record.relative_path,
        "file_name": record.file_name,
        "extension": record.extension,
        "mime_type": record.mime_type,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "mtime_ns": record.mtime_ns,
        "width": record.width,
        "height": record.height,
        "tags": [],
        # Files directly under the selected root inherit that folder's cleaned
        # name under the current folder-tag contract.
        "folder_tags": ["images"],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


if __name__ == "__main__":
    unittest.main()
