from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

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


def normalize_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def document_id(path: Path) -> str:
    normalized = normalize_path(path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_files(root: Path, recursive: bool, result: ScanResult):
    if not recursive:
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            result.complete = False
            result.failures.append(
                FileFailure(str(root), f"directory scan failed: {exc}")
            )
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    result.skipped += 1
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError as exc:
                result.complete = False
                result.failures.append(FileFailure(entry.path, str(exc)))
        return

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            result.complete = False
            result.failures.append(
                FileFailure(str(directory), f"directory scan failed: {exc}")
            )
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    result.skipped += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError as exc:
                result.complete = False
                result.failures.append(FileFailure(entry.path, str(exc)))


def inspect_image(path: Path, root: Path) -> ImageRecord:
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
    return ImageRecord(
        doc_id=document_id(resolved),
        root_path=normalize_path(root),
        relative_path=str(resolved.relative_to(root.resolve())),
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
    recursive: bool = True,
    previous_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    verify_hash: bool = False,
) -> ScanResult:
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    result = ScanResult()
    for path in _iter_files(root, recursive, result):
        result.scanned += 1
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            result.skipped += 1
            continue
        result.supported += 1
        doc_id = document_id(path)
        result.seen_supported_ids.add(doc_id)
        try:
            previous = previous_lookup(doc_id) if previous_lookup else None
            stat = path.stat()
            if (
                previous
                and not verify_hash
                and int(previous["size_bytes"]) == stat.st_size
                and int(previous["mtime_ns"]) == stat.st_mtime_ns
                and previous["root_path"] == normalize_path(root)
                and previous["absolute_path"] == str(path.resolve())
            ):
                record = ImageRecord(
                    **{key: previous[key] for key in ImageRecord.__dataclass_fields__}
                )
                result.records.append(record)
                result.fast_unchanged_ids.add(doc_id)
            else:
                result.records.append(inspect_image(path, root))
        except Exception as exc:
            result.failures.append(FileFailure(str(path), str(exc)))
    return result


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
