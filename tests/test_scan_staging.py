from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import image_vector_service.scan_staging as scan_staging_module
from image_vector_service.image_scanner import (
    scan_folder,
    scan_folder_to_staging,
)
from image_vector_service.models import ImageRecord
from image_vector_service.scan_staging import (
    MAX_SCAN_WRITE_BATCH_SIZE,
    SCAN_STAGING_APPLICATION_ID,
    SCAN_STAGING_PREFIX,
    SCAN_STAGING_SCHEMA_VERSION,
    SCAN_STAGING_SUFFIX,
    ScanIterationStats,
    ScanPathKey,
    ScanSha256Key,
    ScanStaging,
    SeenScanDocument,
    StagedImageRecord,
    discover_scan_staging,
    retire_orphaned_scan_staging,
)


class ScanStagingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec-scan-staging-")
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "library-root-private"
        self.root.mkdir()
        self.staging_directory = self.workspace / "staging"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_are_portable_keyset_paged_and_grouped_across_batches(
        self,
    ) -> None:
        staging = ScanStaging.create(self.staging_directory, root_id="root-a")
        try:
            first = _staged_record(0, root=self.root, sha256="a" * 64)
            second = _staged_record(1, root=self.root, sha256="b" * 64)
            duplicate = _staged_record(2, root=self.root, sha256="a" * 64)
            staging.append_batch(
                seen_documents=(_seen(first), _seen(second)),
                records=(first, second),
            )
            staging.append_batch(
                seen_documents=(_seen(duplicate),),
                records=(duplicate,),
            )
            staging.mark_ready(
                scanned=3,
                supported=3,
                skipped=0,
                peak_in_flight=2,
                complete=True,
                failure_count=0,
            )

            path_stats = ScanIterationStats()
            path_records = [
                record
                for batch in staging.iter_records_by_path(
                    batch_size=2,
                    stats=path_stats,
                )
                for record in batch
            ]
            sha_records = [
                record
                for batch in staging.iter_records_by_sha256(batch_size=2)
                for record in batch
            ]
            groups = [
                group
                for batch in staging.iter_content_groups(batch_size=1)
                for group in batch
            ]
            chunks = list(staging.iter_content_chunks(chunk_size=1))

            self.assertEqual(
                [item.doc_id for item in path_records],
                ["doc-0", "doc-1", "doc-2"],
            )
            self.assertEqual(
                [item.sha256 for item in sha_records],
                ["a" * 64, "a" * 64, "b" * 64],
            )
            self.assertEqual(
                [(item.sha256, item.member_count) for item in groups],
                [("a" * 64, 2), ("b" * 64, 1)],
            )
            self.assertEqual(groups[0].representative.doc_id, "doc-0")
            self.assertEqual(groups[0].fast_unchanged_count, 0)
            self.assertEqual(
                [
                    (
                        chunk.sha256,
                        [record.doc_id for record in chunk.records],
                        chunk.is_first,
                        chunk.is_last,
                    )
                    for chunk in chunks
                ],
                [
                    ("a" * 64, ["doc-0"], True, False),
                    ("a" * 64, ["doc-2"], False, True),
                    ("b" * 64, ["doc-1"], True, True),
                ],
            )
            resumed = list(
                staging.iter_content_chunks(
                    chunk_size=1,
                    after=ScanSha256Key(
                        "a" * 64,
                        "root-a",
                        "0000000.jpg",
                        "doc-0",
                    ),
                )
            )
            self.assertFalse(resumed[0].is_first)
            self.assertEqual(path_stats.select_count, 3)
            self.assertEqual(path_stats.max_materialized_rows, 2)
            rebuilt = path_records[0].to_image_record(self.root)
            self.assertEqual(
                Path(rebuilt.absolute_path).resolve(),
                (self.root / "0000000.jpg").resolve(),
            )
        finally:
            staging.discard()

    def test_staging_schema_and_payload_never_persist_absolute_source_root(
        self,
    ) -> None:
        staging = ScanStaging.create(self.staging_directory, root_id="root-a")
        record = _staged_record(0, root=self.root, sha256="c" * 64)
        try:
            staging.append_batch(
                seen_documents=(_seen(record),),
                records=(record,),
            )
            staging.mark_ready(
                scanned=1,
                supported=1,
                skipped=0,
                peak_in_flight=1,
                complete=True,
                failure_count=0,
            )
            staging.close()

            connection = sqlite3.connect(staging.path)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(scan_records)"
                    ).fetchall()
                }
                relative_path = connection.execute(
                    "SELECT relative_path FROM scan_records"
                ).fetchone()[0]
                metadata_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(scan_metadata)"
                    ).fetchall()
                }
            finally:
                connection.close()

            self.assertNotIn("absolute_path", columns)
            self.assertNotIn("root_path", metadata_columns)
            self.assertEqual(relative_path, "0000000.jpg")
            self.assertNotIn(
                str(self.root),
                staging.path.read_bytes().decode("latin-1"),
            )
        finally:
            staging.discard()

    def test_seen_relation_anti_joins_state_entries_in_keyset_batches(self) -> None:
        state_path = self.workspace / "state.sqlite3"
        connection = sqlite3.connect(state_path)
        try:
            connection.execute(
                "CREATE TABLE entries(doc_id TEXT PRIMARY KEY, root_id TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO entries(doc_id, root_id) VALUES (?, ?)",
                (
                    ("doc-0", "root-a"),
                    ("doc-1", "root-a"),
                    ("doc-2", "root-a"),
                    ("doc-other", "root-b"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        staging = ScanStaging.create(self.staging_directory, root_id="root-a")
        try:
            seen = (
                SeenScanDocument("doc-0", "root-a", "0.jpg"),
                SeenScanDocument("doc-2", "root-a", "2.jpg"),
            )
            staging.append_batch(seen_documents=seen)
            stats = ScanIterationStats()
            stale = [
                doc_id
                for batch in staging.iter_stale_doc_ids(
                    state_path,
                    root_id="root-a",
                    batch_size=1,
                    stats=stats,
                )
                for doc_id in batch
            ]
            count_stats = ScanIterationStats()
            stale_count = staging.count_stale_doc_ids(
                state_path,
                root_id="root-a",
                stats=count_stats,
            )
            self.assertEqual(stale, ["doc-1"])
            self.assertEqual(stale_count, 1)
            self.assertEqual(stats.select_count, 2)
            self.assertEqual(stats.max_materialized_rows, 1)
            self.assertEqual(count_stats.select_count, 1)
        finally:
            staging.discard()

    def test_low_level_reader_can_validate_ready_staging_before_retirement(
        self,
    ) -> None:
        staging = ScanStaging.create(self.staging_directory, root_id="root-a")
        record = _staged_record(0, root=self.root, sha256="d" * 64)
        staging.append_batch(
            seen_documents=(_seen(record),),
            records=(record,),
        )
        staging.mark_ready(
            scanned=1,
            supported=1,
            skipped=0,
            peak_in_flight=1,
            complete=True,
            failure_count=0,
        )
        path = staging.path
        staging.close()

        recovered = ScanStaging.open(path)
        try:
            self.assertEqual(recovered.metadata().status, "ready")
            self.assertEqual(recovered.record_count(), 1)
            self.assertEqual(recovered.content_group_count(), 1)
        finally:
            recovered.discard()

    def test_orphan_retirement_removes_validated_scanning_and_ready_work(
        self,
    ) -> None:
        scanning = ScanStaging.create(self.staging_directory, root_id="root-a")
        ready = ScanStaging.create(self.staging_directory, root_id="root-b")
        ready.mark_ready(
            scanned=0,
            supported=0,
            skipped=0,
            peak_in_flight=0,
            complete=True,
            failure_count=0,
        )
        scanning.close()
        ready.close()

        artifacts = discover_scan_staging(
            self.staging_directory,
            abandoned_after_seconds=30,
            now=time.time(),
        )
        by_status = {artifact.status: artifact for artifact in artifacts}
        self.assertFalse(by_status["scanning"].abandoned)
        self.assertFalse(by_status["ready"].abandoned)

        removed = retire_orphaned_scan_staging(self.staging_directory)
        self.assertEqual(set(removed), {scanning.path, ready.path})
        self.assertFalse(scanning.path.exists())
        self.assertFalse(ready.path.exists())

    def test_invalid_prefixed_file_is_reported_but_never_auto_deleted(self) -> None:
        path = self.staging_directory / (
            f"{SCAN_STAGING_PREFIX}{'f' * 32}{SCAN_STAGING_SUFFIX}"
        )
        path.parent.mkdir(parents=True)
        path.write_text("not sqlite", encoding="utf-8")

        artifacts = discover_scan_staging(self.staging_directory)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].status, "invalid")
        unknown = Path(f"{path}.backup")
        unknown.write_text("operator backup", encoding="utf-8")
        warnings: list[str] = []
        self.assertEqual(
            retire_orphaned_scan_staging(
                self.staging_directory,
                warning_handler=warnings.append,
            ),
            [],
        )
        self.assertTrue(path.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(any("validation failed" in warning for warning in warnings))

    def test_wrong_application_id_is_never_deleted(self) -> None:
        path = self.staging_directory / (
            f"{SCAN_STAGING_PREFIX}{'e' * 32}{SCAN_STAGING_SUFFIX}"
        )
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA application_id=123")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()

        warnings: list[str] = []
        self.assertEqual(
            retire_orphaned_scan_staging(
                self.staging_directory,
                warning_handler=warnings.append,
            ),
            [],
        )
        self.assertTrue(path.exists())
        self.assertTrue(any("validation failed" in warning for warning in warnings))

    def test_incomplete_schema_is_never_deleted(self) -> None:
        run_id = "d" * 32
        path = self.staging_directory / (
            f"{SCAN_STAGING_PREFIX}{run_id}{SCAN_STAGING_SUFFIX}"
        )
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute(f"PRAGMA application_id={SCAN_STAGING_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCAN_STAGING_SCHEMA_VERSION}")
            connection.execute("CREATE TABLE scan_metadata(run_id TEXT NOT NULL);")
            connection.execute(
                "INSERT INTO scan_metadata(run_id) VALUES (?)",
                (run_id,),
            )
            connection.commit()
        finally:
            connection.close()

        warnings: list[str] = []
        self.assertEqual(
            retire_orphaned_scan_staging(
                self.staging_directory,
                warning_handler=warnings.append,
            ),
            [],
        )
        self.assertTrue(path.exists())
        self.assertTrue(any("validation failed" in warning for warning in warnings))

    def test_reparse_candidate_is_never_deleted(self) -> None:
        staging = ScanStaging.create(self.staging_directory, root_id="root-a")
        staging.mark_ready(
            scanned=0,
            supported=0,
            skipped=0,
            peak_in_flight=0,
            complete=True,
            failure_count=0,
        )
        path = staging.path
        staging.close()

        warnings: list[str] = []
        with patch(
            "image_vector_service.scan_staging._is_reparse_or_symlink",
            return_value=True,
        ):
            removed = retire_orphaned_scan_staging(
                self.staging_directory,
                warning_handler=warnings.append,
            )

        self.assertEqual(removed, [])
        self.assertTrue(path.exists())
        self.assertTrue(any("validation failed" in warning for warning in warnings))
        staging.discard()


class StreamingImageScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec-stream-scan-")
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "images"
        self.root.mkdir()
        self.staging_directory = self.workspace / "staging"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scanner_batches_previous_lookup_and_does_not_materialize_library(
        self,
    ) -> None:
        for index in range(45):
            Image.new("RGB", (4, 4), (index, 0, 0)).save(self.root / f"{index:03d}.png")
        initial = scan_folder(self.root, "root-a", max_workers=2)
        previous = {record.doc_id: record.state_dict() for record in initial.records}
        lookup_calls: list[tuple[str, ...]] = []

        def lookup_many(doc_ids: Iterable[str]):
            normalized = tuple(str(value) for value in doc_ids)
            lookup_calls.append(normalized)
            return {doc_id: previous[doc_id] for doc_id in normalized}

        result = scan_folder_to_staging(
            self.root,
            "root-a",
            staging_directory=self.staging_directory,
            previous_lookup=lambda _doc_id: self.fail("N+1 lookup was used"),
            previous_lookup_many=lookup_many,
            max_workers=2,
            lookup_batch_size=10,
            write_batch_size=100,
        )
        try:
            metadata = result.staging.metadata()
            self.assertEqual(result.record_count, 45)
            self.assertEqual(metadata.seen_count, 45)
            self.assertEqual(metadata.fast_unchanged_count, 45)
            self.assertEqual(len(lookup_calls), 5)
            self.assertLessEqual(max(map(len, lookup_calls)), 10)
            records = [
                record
                for batch in result.iter_image_records_by_path(
                    self.root,
                    batch_size=17,
                )
                for record in batch
            ]
            self.assertEqual(len(records), 45)
            self.assertTrue(
                all(Path(record.absolute_path).is_file() for record in records)
            )
        finally:
            result.discard()

    def test_cancellation_removes_partial_staging(self) -> None:
        for index in range(3):
            Image.new("RGB", (4, 4), (index, 0, 0)).save(self.root / f"{index}.png")

        class ExpectedCancellation(RuntimeError):
            pass

        def cancel() -> None:
            raise ExpectedCancellation("cancel requested")

        with self.assertRaisesRegex(ExpectedCancellation, "cancel requested"):
            scan_folder_to_staging(
                self.root,
                "root-a",
                staging_directory=self.staging_directory,
                cancel_check=cancel,
                write_batch_size=100,
            )
        self.assertEqual(discover_scan_staging(self.staging_directory), [])

    def test_next_scan_retires_crash_artifacts_and_rebuilds_from_current_root(
        self,
    ) -> None:
        scanning = ScanStaging.create(self.staging_directory, root_id="old-root-a")
        scanning_path = scanning.path
        scanning.close()
        ready = ScanStaging.create(self.staging_directory, root_id="old-root-b")
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

        result = scan_folder_to_staging(
            self.root,
            "current-root",
            staging_directory=self.staging_directory,
            abandoned_after_seconds=10**9,
            write_batch_size=100,
        )
        try:
            self.assertFalse(scanning_path.exists())
            self.assertFalse(ready_path.exists())
            self.assertEqual(result.staging.metadata().root_id, "current-root")
        finally:
            result.discard()

    def test_cleanup_failure_warns_but_does_not_block_the_next_scan(self) -> None:
        orphan = ScanStaging.create(self.staging_directory, root_id="old-root")
        orphan.mark_ready(
            scanned=0,
            supported=0,
            skipped=0,
            peak_in_flight=0,
            complete=True,
            failure_count=0,
        )
        orphan_path = orphan.path
        orphan.close()
        original_remove = scan_staging_module._remove_staging_files

        def fail_only_orphan(path: Path, **kwargs: object) -> None:
            if path == orphan_path:
                raise PermissionError("synthetic locked staging")
            original_remove(path, **kwargs)

        with patch(
            "image_vector_service.scan_staging._remove_staging_files",
            side_effect=fail_only_orphan,
        ):
            result = scan_folder_to_staging(
                self.root,
                "current-root",
                staging_directory=self.staging_directory,
                write_batch_size=100,
            )
            try:
                self.assertEqual(result.staging.metadata().status, "ready")
                self.assertTrue(
                    any("cleanup failed" in warning for warning in result.warnings)
                )
            finally:
                result.discard()

        self.assertTrue(orphan_path.exists())
        retire_orphaned_scan_staging(self.staging_directory)

    def test_invalid_image_is_seen_but_not_emitted_as_a_valid_record(self) -> None:
        invalid = self.root / "broken.jpg"
        invalid.write_text("not an image", encoding="utf-8")
        result = scan_folder_to_staging(
            self.root,
            "root-a",
            staging_directory=self.staging_directory,
            write_batch_size=100,
        )
        try:
            metadata = result.staging.metadata()
            self.assertEqual(metadata.seen_count, 1)
            self.assertEqual(metadata.record_count, 0)
            self.assertEqual(result.failure_count, 1)
            seen = [
                doc_id
                for batch in result.staging.iter_seen_doc_ids(
                    root_id="root-a",
                    batch_size=1,
                )
                for doc_id in batch
            ]
            self.assertEqual(len(seen), 1)
        finally:
            result.discard()


class ScanStagingScaleTests(unittest.TestCase):
    def test_large_duplicate_group_is_reloaded_in_bounded_pages(self) -> None:
        count = 1_201
        digest = "e" * 64
        with tempfile.TemporaryDirectory(prefix="zvec-scan-duplicates-") as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            staging = ScanStaging.create(
                Path(temporary) / "staging",
                root_id="duplicate-root",
            )
            try:
                for offset in range(0, count, MAX_SCAN_WRITE_BATCH_SIZE):
                    end = min(count, offset + MAX_SCAN_WRITE_BATCH_SIZE)
                    records = [
                        _staged_record(
                            index,
                            root=root,
                            root_id="duplicate-root",
                            sha256=digest,
                        )
                        for index in range(offset, end)
                    ]
                    staging.append_batch(
                        seen_documents=[_seen(record) for record in records],
                        records=records,
                    )
                staging.mark_ready(
                    scanned=count,
                    supported=count,
                    skipped=0,
                    peak_in_flight=8,
                    complete=True,
                    failure_count=0,
                )

                groups = [
                    group
                    for page in staging.iter_content_groups(batch_size=100)
                    for group in page
                ]
                stats = ScanIterationStats()
                members = [
                    record.doc_id
                    for page in staging.iter_records_for_sha256(
                        digest,
                        batch_size=257,
                        stats=stats,
                    )
                    for record in page
                ]

                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0].member_count, count)
                self.assertEqual(len(members), count)
                self.assertEqual(len(set(members)), count)
                self.assertEqual(stats.max_materialized_rows, 257)
                self.assertEqual(stats.select_count, 6)

                resume_key = ScanPathKey(
                    "duplicate-root",
                    "0000599.jpg",
                    "doc-599",
                )
                resumed_count = sum(
                    len(page)
                    for page in staging.iter_records_for_sha256(
                        digest,
                        batch_size=300,
                        after=resume_key,
                    )
                )
                self.assertEqual(resumed_count, count - 600)
            finally:
                staging.discard()

    def test_one_hundred_thousand_rows_remain_batch_bounded(self) -> None:
        self._assert_scale(100_000)

    @unittest.skipUnless(
        os.environ.get("ZVEC_RUN_MILLION_SCAN_STAGING_TEST") == "1",
        "set ZVEC_RUN_MILLION_SCAN_STAGING_TEST=1 for the million-row gate",
    )
    def test_one_million_rows_remain_batch_bounded(self) -> None:
        self._assert_scale(1_000_000)

    def _assert_scale(self, count: int) -> None:
        with tempfile.TemporaryDirectory(prefix="zvec-scan-scale-") as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            staging = ScanStaging.create(
                Path(temporary) / "staging",
                root_id="scale-root",
            )
            try:
                max_materialized = 0
                for offset in range(0, count, MAX_SCAN_WRITE_BATCH_SIZE):
                    end = min(count, offset + MAX_SCAN_WRITE_BATCH_SIZE)
                    records = [
                        _staged_record(
                            index,
                            root=root,
                            root_id="scale-root",
                            sha256=f"{index % 997:064x}",
                        )
                        for index in range(offset, end)
                    ]
                    seen = [_seen(record) for record in records]
                    max_materialized = max(
                        max_materialized,
                        len(records),
                        len(seen),
                    )
                    staging.append_batch(
                        seen_documents=seen,
                        records=records,
                    )
                staging.mark_ready(
                    scanned=count,
                    supported=count,
                    skipped=0,
                    peak_in_flight=8,
                    complete=True,
                    failure_count=0,
                )

                stats = ScanIterationStats()
                read_count = sum(
                    len(batch)
                    for batch in staging.iter_records_by_sha256(
                        batch_size=MAX_SCAN_WRITE_BATCH_SIZE,
                        stats=stats,
                    )
                )
                metadata = staging.metadata()
                expected_pages = (
                    count + MAX_SCAN_WRITE_BATCH_SIZE - 1
                ) // MAX_SCAN_WRITE_BATCH_SIZE
                self.assertEqual(read_count, count)
                self.assertEqual(staging.content_group_count(), 997)
                self.assertEqual(metadata.write_batch_count, expected_pages)
                self.assertEqual(metadata.max_write_batch, MAX_SCAN_WRITE_BATCH_SIZE)
                self.assertEqual(max_materialized, MAX_SCAN_WRITE_BATCH_SIZE)
                self.assertEqual(stats.select_count, expected_pages + 1)
                self.assertEqual(
                    stats.max_materialized_rows,
                    MAX_SCAN_WRITE_BATCH_SIZE,
                )
            finally:
                staging.discard()


def _staged_record(
    index: int,
    *,
    root: Path,
    root_id: str = "root-a",
    sha256: str,
) -> StagedImageRecord:
    name = f"{index:07d}.jpg"
    record = ImageRecord(
        doc_id=f"doc-{index}",
        root_id=root_id,
        relative_path=name,
        absolute_path=str(root / name),
        file_name=name,
        extension="jpg",
        mime_type="image/jpeg",
        sha256=sha256,
        size_bytes=100 + index,
        mtime_ns=1_000 + index,
        width=16,
        height=16,
    )
    return StagedImageRecord.from_image_record(index, record)


def _seen(record: StagedImageRecord) -> SeenScanDocument:
    return SeenScanDocument(
        doc_id=record.doc_id,
        root_id=record.root_id,
        relative_path=record.relative_path,
        fast_unchanged=record.fast_unchanged,
    )


if __name__ == "__main__":
    unittest.main()
