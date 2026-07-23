from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .logical_paths import logical_document_id
from .models import FileFailure, ImageRecord, ScanResult
from .scan_staging import (
    DEFAULT_ABANDONED_AFTER_SECONDS,
    DEFAULT_SCAN_WRITE_BATCH_SIZE,
    MAX_SCAN_WRITE_BATCH_SIZE,
    MIN_SCAN_WRITE_BATCH_SIZE,
    ScanContentChunk,
    ScanIterationStats,
    ScanPathKey,
    ScanSha256Key,
    ScanStaging,
    SeenScanDocument,
    StagedImageRecord,
    cleanup_scan_staging,
)

SUPPORTED_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".dib": "image/bmp",
    ".icns": "image/icns",
    ".sgi": "image/sgi",
}

SUPPORTED_PIL_FORMATS = {
    "JPEG",
    "PNG",
    "WEBP",
    "BMP",
    "TIFF",
    "ICO",
    "ICNS",
    "SGI",
}

PIL_FORMAT_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "ICO": "image/x-icon",
    "ICNS": "image/icns",
    "SGI": "image/sgi",
}


@dataclass(slots=True)
class StagedScanResult:
    """Bounded in-memory summary backed by a transient SQLite scan."""

    staging: ScanStaging
    scanned: int = 0
    supported: int = 0
    skipped: int = 0
    peak_in_flight: int = 0
    complete: bool = True
    failure_count: int = 0
    failures: list[FileFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return self.staging.record_count()

    @property
    def staging_path(self) -> Path:
        return self.staging.path

    def iter_staged_records_by_path(
        self,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        return self.staging.iter_records_by_path(
            batch_size=batch_size,
            after=after,
            stats=stats,
        )

    def iter_staged_records_by_sha256(
        self,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        return self.staging.iter_records_by_sha256(
            batch_size=batch_size,
            after=after,
            stats=stats,
        )

    def iter_image_records_by_path(
        self,
        root_path: str | Path,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[ImageRecord]]:
        for batch in self.iter_staged_records_by_path(
            batch_size=batch_size,
            after=after,
            stats=stats,
        ):
            yield [record.to_image_record(root_path) for record in batch]

    def iter_content_chunks(
        self,
        *,
        chunk_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[ScanContentChunk]:
        return self.staging.iter_content_chunks(
            chunk_size=chunk_size,
            after=after,
            stats=stats,
        )

    def iter_staged_records_for_sha256(
        self,
        sha256: str,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        return self.staging.iter_records_for_sha256(
            sha256,
            batch_size=batch_size,
            after=after,
            stats=stats,
        )

    def iter_image_records_for_sha256(
        self,
        sha256: str,
        root_path: str | Path,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[ImageRecord]]:
        for batch in self.iter_staged_records_for_sha256(
            sha256,
            batch_size=batch_size,
            after=after,
            stats=stats,
        ):
            yield [record.to_image_record(root_path) for record in batch]

    def iter_image_records_for_sha256s(
        self,
        sha256_values: Sequence[str],
        root_path: str | Path,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[ImageRecord]]:
        for batch in self.staging.iter_records_for_sha256s(
            sha256_values,
            batch_size=batch_size,
            after=after,
            stats=stats,
        ):
            yield [record.to_image_record(root_path) for record in batch]

    def count_stale_doc_ids(
        self,
        state_database: str | Path,
        *,
        root_id: str,
        stats: ScanIterationStats | None = None,
    ) -> int:
        return self.staging.count_stale_doc_ids(
            state_database,
            root_id=root_id,
            stats=stats,
        )

    def discard(self) -> None:
        self.staging.discard()


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_files(
    root: Path,
    recursive: bool,
    result: ScanResult,
    *,
    excluded_roots: tuple[Path, ...],
    failure_handler: Callable[[FileFailure], None] | None,
):
    if not recursive:
        try:
            entries = os.scandir(root)
        except OSError as exc:
            result.complete = False
            _record_scan_failure(
                result,
                FileFailure(
                    str(root),
                    f"directory scan failed: {exc}",
                    stage="scan_directory",
                ),
                failure_handler,
            )
            return
        with entries:
            for entry in entries:
                try:
                    path = Path(entry.path)
                    if _is_excluded(path, excluded_roots) or entry.is_symlink():
                        result.skipped += 1
                    elif entry.is_file(follow_symlinks=False):
                        yield path
                except OSError as exc:
                    result.complete = False
                    _record_scan_failure(
                        result,
                        FileFailure(entry.path, str(exc), stage="scan_entry"),
                        failure_handler,
                    )
        return

    pending = [root]
    while pending:
        directory = pending.pop()
        if _is_excluded(directory, excluded_roots):
            result.skipped += 1
            continue
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            result.complete = False
            _record_scan_failure(
                result,
                FileFailure(
                    str(directory),
                    f"directory scan failed: {exc}",
                    stage="scan_directory",
                ),
                failure_handler,
            )
            continue
        with entries:
            for entry in entries:
                try:
                    path = Path(entry.path)
                    if _is_excluded(path, excluded_roots):
                        result.skipped += 1
                        continue
                    if entry.is_symlink():
                        result.skipped += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
                except OSError as exc:
                    result.complete = False
                    _record_scan_failure(
                        result,
                        FileFailure(entry.path, str(exc), stage="scan_entry"),
                        failure_handler,
                    )


def inspect_image(path: Path, root: Path, root_id: str) -> ImageRecord:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported extension: {extension or '<none>'}")

    stat_before = path.stat()
    _actual_format, mime_type, width, height = inspect_image_content(path)
    sha256 = file_sha256(path)
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (
        stat_before.st_size,
        stat_before.st_mtime_ns,
    ):
        raise RuntimeError("file changed while it was being scanned; run index again")
    resolved = path.resolve()
    relative_path = str(resolved.relative_to(root.resolve()))
    return ImageRecord(
        doc_id=logical_document_id(root_id, relative_path),
        root_id=root_id,
        relative_path=relative_path,
        absolute_path=str(resolved),
        file_name=resolved.name,
        extension=extension.lstrip("."),
        mime_type=mime_type,
        sha256=sha256,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        width=width,
        height=height,
    )


def scan_folder(
    folder_path: str | Path,
    root_id: str,
    recursive: bool = True,
    previous_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    previous_lookup_many: (
        Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
    ) = None,
    verify_hash: bool = False,
    cancel_check: Callable[[], None] | None = None,
    max_workers: int = 4,
    max_in_flight: int | None = None,
    lookup_batch_size: int = 256,
    excluded_roots: Iterable[str | Path] = (),
    failure_handler: Callable[[FileFailure], None] | None = None,
) -> ScanResult:
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    if max_workers < 1:
        raise ValueError("max_workers must be positive.")
    if lookup_batch_size < 1:
        raise ValueError("lookup_batch_size must be positive.")
    in_flight_limit = max_in_flight or max_workers * 2
    if in_flight_limit < max_workers:
        raise ValueError("max_in_flight must be at least max_workers.")
    exclusions = tuple(Path(value).expanduser().resolve() for value in excluded_roots)

    result = ScanResult()
    if _is_excluded(root, exclusions):
        result.warnings.append(f"Skipped excluded scan root: {root}")
        return result

    completed_records: list[tuple[int, ImageRecord]] = []
    futures: dict[Future[ImageRecord], tuple[int, Path]] = {}

    def collect(*, drain_all: bool = False) -> None:
        while futures:
            done, _pending = wait(
                futures,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                sequence, source_path = futures.pop(future)
                try:
                    completed_records.append((sequence, future.result()))
                except Exception as exc:
                    _record_scan_failure(
                        result,
                        FileFailure(
                            str(source_path),
                            str(exc) or exc.__class__.__name__,
                            stage="inspect_image",
                        ),
                        failure_handler,
                    )
            if not drain_all:
                return

    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="zvec-image-scan",
    )
    try:
        source = enumerate(
            _iter_files(
                root,
                recursive,
                result,
                excluded_roots=exclusions,
                failure_handler=failure_handler,
            )
        )
        while True:
            raw_batch: list[tuple[int, Path]] = []
            for _index in range(lookup_batch_size):
                try:
                    raw_batch.append(next(source))
                except StopIteration:
                    break
            if not raw_batch:
                break

            prepared: list[tuple[int, Path, Path, str, str, os.stat_result]] = []
            for sequence, path in raw_batch:
                if cancel_check is not None:
                    cancel_check()
                result.scanned += 1
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    result.skipped += 1
                    continue
                result.supported += 1
                try:
                    resolved = path.resolve()
                    relative_path = str(resolved.relative_to(root))
                    doc_id = logical_document_id(root_id, relative_path)
                    result.seen_supported_ids.add(doc_id)
                    prepared.append(
                        (
                            sequence,
                            path,
                            resolved,
                            relative_path,
                            doc_id,
                            path.stat(),
                        )
                    )
                except Exception as exc:
                    _record_scan_failure(
                        result,
                        FileFailure(
                            str(path),
                            str(exc) or exc.__class__.__name__,
                            stage="scan_metadata",
                        ),
                        failure_handler,
                    )

            previous_by_id: Mapping[str, Mapping[str, Any]] = {}
            lookup_error: Exception | None = None
            if prepared and previous_lookup_many is not None:
                try:
                    previous_by_id = previous_lookup_many(item[4] for item in prepared)
                except Exception as exc:  # keep failures item-scoped
                    lookup_error = exc

            for sequence, path, resolved, relative_path, doc_id, stat in prepared:
                try:
                    if lookup_error is not None:
                        raise lookup_error
                    previous = (
                        previous_by_id.get(doc_id)
                        if previous_lookup_many is not None
                        else previous_lookup(doc_id)
                        if previous_lookup is not None
                        else None
                    )
                    if (
                        previous
                        and not verify_hash
                        and int(previous["size_bytes"]) == stat.st_size
                        and int(previous["mtime_ns"]) == stat.st_mtime_ns
                        and previous["root_id"] == root_id
                        and previous["relative_path"] == relative_path
                    ):
                        values = {
                            key: previous[key]
                            for key in ImageRecord.__dataclass_fields__
                            if key != "absolute_path"
                        }
                        completed_records.append(
                            (
                                sequence,
                                ImageRecord(absolute_path=str(resolved), **values),
                            )
                        )
                        result.fast_unchanged_ids.add(doc_id)
                    else:
                        futures[executor.submit(inspect_image, path, root, root_id)] = (
                            sequence,
                            path,
                        )
                        result.peak_in_flight = max(
                            result.peak_in_flight,
                            len(futures),
                        )
                        if len(futures) >= in_flight_limit:
                            collect()
                except Exception as exc:
                    _record_scan_failure(
                        result,
                        FileFailure(
                            str(path),
                            str(exc) or exc.__class__.__name__,
                            stage="scan_metadata",
                        ),
                        failure_handler,
                    )
        collect(drain_all=True)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    result.records.extend(
        record
        for _sequence, record in sorted(
            completed_records,
            key=lambda item: item[0],
        )
    )
    return result


def scan_folder_to_staging(
    folder_path: str | Path,
    root_id: str,
    *,
    staging_directory: str | Path,
    recursive: bool = True,
    previous_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    previous_lookup_many: (
        Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
    ) = None,
    verify_hash: bool = False,
    cancel_check: Callable[[], None] | None = None,
    max_workers: int = 4,
    max_in_flight: int | None = None,
    lookup_batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
    write_batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
    run_id: str | None = None,
    excluded_roots: Iterable[str | Path] = (),
    failure_handler: Callable[[FileFailure], None] | None = None,
    abandoned_after_seconds: float = DEFAULT_ABANDONED_AFTER_SECONDS,
) -> StagedScanResult:
    """Scan a root into durable SQLite pages without retaining the library.

    The returned staging object remains open so the index pipeline can consume
    path- or SHA-ordered keyset pages. The caller must call ``discard`` after a
    successful commit. A scan exception/cancellation removes its partial file;
    process crashes may leave scratch data that is validated and safely retired
    before the next scan. The scan is then rebuilt with current parameters.
    """

    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    if max_workers < 1:
        raise ValueError("max_workers must be positive.")
    if isinstance(lookup_batch_size, bool) or not isinstance(lookup_batch_size, int):
        raise ValueError("lookup_batch_size must be an integer.")
    if not 1 <= lookup_batch_size <= MAX_SCAN_WRITE_BATCH_SIZE:
        raise ValueError(
            f"lookup_batch_size must be between 1 and {MAX_SCAN_WRITE_BATCH_SIZE}."
        )
    if isinstance(write_batch_size, bool) or not isinstance(write_batch_size, int):
        raise ValueError("write_batch_size must be an integer.")
    if not MIN_SCAN_WRITE_BATCH_SIZE <= write_batch_size <= MAX_SCAN_WRITE_BATCH_SIZE:
        raise ValueError(
            "write_batch_size must be between "
            f"{MIN_SCAN_WRITE_BATCH_SIZE} and {MAX_SCAN_WRITE_BATCH_SIZE}."
        )
    in_flight_limit = max_in_flight or max_workers * 2
    if in_flight_limit < max_workers:
        raise ValueError("max_in_flight must be at least max_workers.")

    staging_parent = Path(staging_directory).expanduser().resolve()
    result = ScanResult()
    cleanup_scan_staging(
        staging_parent,
        abandoned_after_seconds=abandoned_after_seconds,
        include_ready=True,
        include_scanning=True,
        warning_handler=result.warnings.append,
    )
    staging = ScanStaging.create(staging_parent, root_id=root_id, run_id=run_id)
    exclusions = tuple(Path(value).expanduser().resolve() for value in excluded_roots)
    seen_buffer: list[SeenScanDocument] = []
    record_buffer: list[StagedImageRecord] = []
    futures: dict[Future[ImageRecord], tuple[int, Path]] = {}

    def flush(*, force: bool = False) -> None:
        while (
            len(seen_buffer) >= write_batch_size
            or len(record_buffer) >= write_batch_size
            or (force and (seen_buffer or record_buffer))
        ):
            seen_count = min(write_batch_size, len(seen_buffer))
            record_count = min(write_batch_size, len(record_buffer))
            seen_batch = tuple(seen_buffer[:seen_count])
            record_batch = tuple(record_buffer[:record_count])
            staging.append_batch(
                seen_documents=seen_batch,
                records=record_batch,
                scanned=result.scanned,
                supported=result.supported,
                skipped=result.skipped,
                peak_in_flight=result.peak_in_flight,
                complete=result.complete,
                failure_count=result.failure_count,
            )
            del seen_buffer[:seen_count]
            del record_buffer[:record_count]

    def collect(*, drain_all: bool = False) -> None:
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                sequence, source_path = futures.pop(future)
                try:
                    record_buffer.append(
                        StagedImageRecord.from_image_record(
                            sequence,
                            future.result(),
                        )
                    )
                except Exception as exc:
                    _record_scan_failure(
                        result,
                        FileFailure(
                            str(source_path),
                            str(exc) or exc.__class__.__name__,
                            stage="inspect_image",
                        ),
                        failure_handler,
                    )
            flush()
            if not drain_all:
                return

    try:
        if _is_excluded(root, exclusions):
            result.warnings.append(f"Skipped excluded scan root: {root}")
        else:
            executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="zvec-image-scan",
            )
            try:
                source = enumerate(
                    _iter_files(
                        root,
                        recursive,
                        result,
                        excluded_roots=exclusions,
                        failure_handler=failure_handler,
                    )
                )
                while True:
                    raw_batch: list[tuple[int, Path]] = []
                    for _index in range(lookup_batch_size):
                        try:
                            raw_batch.append(next(source))
                        except StopIteration:
                            break
                    if not raw_batch:
                        break

                    prepared: list[
                        tuple[int, Path, Path, str, str, os.stat_result]
                    ] = []
                    for sequence, path in raw_batch:
                        if cancel_check is not None:
                            cancel_check()
                        result.scanned += 1
                        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                            result.skipped += 1
                            continue
                        result.supported += 1
                        try:
                            resolved = path.resolve()
                            relative_path = str(resolved.relative_to(root))
                            doc_id = logical_document_id(root_id, relative_path)
                            prepared.append(
                                (
                                    sequence,
                                    path,
                                    resolved,
                                    relative_path,
                                    doc_id,
                                    path.stat(),
                                )
                            )
                        except Exception as exc:
                            _record_scan_failure(
                                result,
                                FileFailure(
                                    str(path),
                                    str(exc) or exc.__class__.__name__,
                                    stage="scan_metadata",
                                ),
                                failure_handler,
                            )

                    previous_by_id: Mapping[str, Mapping[str, Any]] = {}
                    lookup_error: Exception | None = None
                    if prepared and previous_lookup_many is not None:
                        try:
                            previous_by_id = previous_lookup_many(
                                tuple(item[4] for item in prepared)
                            )
                        except Exception as exc:
                            lookup_error = exc

                    for (
                        sequence,
                        path,
                        resolved,
                        relative_path,
                        doc_id,
                        stat,
                    ) in prepared:
                        seen_index = len(seen_buffer)
                        seen_buffer.append(
                            SeenScanDocument(
                                doc_id=doc_id,
                                root_id=root_id,
                                relative_path=relative_path,
                            )
                        )
                        try:
                            if lookup_error is not None:
                                raise lookup_error
                            previous = (
                                previous_by_id.get(doc_id)
                                if previous_lookup_many is not None
                                else previous_lookup(doc_id)
                                if previous_lookup is not None
                                else None
                            )
                            if (
                                previous
                                and not verify_hash
                                and int(previous["size_bytes"]) == stat.st_size
                                and int(previous["mtime_ns"]) == stat.st_mtime_ns
                                and previous["root_id"] == root_id
                                and previous["relative_path"] == relative_path
                            ):
                                values = {
                                    key: previous[key]
                                    for key in ImageRecord.__dataclass_fields__
                                    if key != "absolute_path"
                                }
                                unchanged = ImageRecord(
                                    absolute_path=str(resolved),
                                    **values,
                                )
                                record_buffer.append(
                                    StagedImageRecord.from_image_record(
                                        sequence,
                                        unchanged,
                                        fast_unchanged=True,
                                    )
                                )
                                seen_buffer[seen_index] = SeenScanDocument(
                                    doc_id=doc_id,
                                    root_id=root_id,
                                    relative_path=relative_path,
                                    fast_unchanged=True,
                                )
                            else:
                                futures[
                                    executor.submit(inspect_image, path, root, root_id)
                                ] = (sequence, path)
                                result.peak_in_flight = max(
                                    result.peak_in_flight,
                                    len(futures),
                                )
                                if len(futures) >= in_flight_limit:
                                    collect()
                        except Exception as exc:
                            _record_scan_failure(
                                result,
                                FileFailure(
                                    str(path),
                                    str(exc) or exc.__class__.__name__,
                                    stage="scan_metadata",
                                ),
                                failure_handler,
                            )
                    flush()
                collect(drain_all=True)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

        flush(force=True)
        staging.mark_ready(
            scanned=result.scanned,
            supported=result.supported,
            skipped=result.skipped,
            peak_in_flight=result.peak_in_flight,
            complete=result.complete,
            failure_count=result.failure_count,
        )
    except BaseException as exc:
        cancelled = (
            isinstance(exc, CancelledError)
            or "cancel" in (f"{exc.__class__.__name__} {exc}").lower()
        )
        with suppress(Exception):
            staging.mark_failed(cancelled=cancelled)
        # Preserve the original scan/cancellation error. If Windows still has
        # a transient handle open, the terminal scratch artifact remains
        # discoverable and the next startup/scan cleanup will retire it.
        with suppress(Exception):
            staging.discard()
        raise

    return StagedScanResult(
        staging=staging,
        scanned=result.scanned,
        supported=result.supported,
        skipped=result.skipped,
        peak_in_flight=result.peak_in_flight,
        complete=result.complete,
        failure_count=result.failure_count,
        failures=list(result.failures),
        warnings=list(result.warnings),
    )


def _record_scan_failure(
    result: ScanResult,
    failure: FileFailure,
    failure_handler: Callable[[FileFailure], None] | None,
) -> None:
    result.add_failure(failure)
    if failure_handler is not None:
        try:
            failure_handler(failure)
        except Exception as exc:
            result.warnings.append(
                "Could not persist one scan failure: "
                f"{str(exc) or exc.__class__.__name__}"
            )


def _is_excluded(path: Path, excluded_roots: tuple[Path, ...]) -> bool:
    if not excluded_roots:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return any(resolved == root or root in resolved.parents for root in excluded_roots)


def inspect_query_image(image_path: str | Path) -> Path:
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported image extension: {extension or '<none>'}")
    inspect_image_content(path)
    return path


def inspect_image_content(path: Path) -> tuple[str, str, int, int]:
    with Image.open(path) as image:
        actual_format = (image.format or "").upper()
        width, height = image.size
        image.verify()
    if actual_format not in SUPPORTED_PIL_FORMATS:
        raise ValueError(f"unsupported image content: {actual_format or 'unknown'}")
    return actual_format, PIL_FORMAT_MIME[actual_format], width, height
