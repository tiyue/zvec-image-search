from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from PIL import Image

from .logical_paths import logical_document_id
from .models import FileFailure, ImageRecord, ScanResult

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


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
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
    verify_hash: bool = False,
    cancel_check: Callable[[], None] | None = None,
    max_workers: int = 4,
    max_in_flight: int | None = None,
    excluded_roots: Iterable[str | Path] = (),
    failure_handler: Callable[[FileFailure], None] | None = None,
) -> ScanResult:
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    if max_workers < 1:
        raise ValueError("max_workers must be positive.")
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
        for sequence, path in enumerate(
            _iter_files(
                root,
                recursive,
                result,
                excluded_roots=exclusions,
                failure_handler=failure_handler,
            )
        ):
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
                previous = previous_lookup(doc_id) if previous_lookup else None
                stat = path.stat()
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
