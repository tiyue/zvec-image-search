from __future__ import annotations

import hashlib
import os
from pathlib import Path

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


def _iter_files(root: Path, recursive: bool):
    if not recursive:
        for entry in os.scandir(root):
            if entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
        return

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


def inspect_image(path: Path, root: Path) -> ImageRecord:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported extension: {extension or '<none>'}")

    actual_format, mime_type, width, height = inspect_image_content(path)

    stat = path.stat()
    resolved = path.resolve()
    return ImageRecord(
        doc_id=document_id(resolved),
        root_path=normalize_path(root),
        relative_path=str(resolved.relative_to(root.resolve())),
        absolute_path=str(resolved),
        file_name=resolved.name,
        extension=extension.lstrip("."),
        mime_type=mime_type,
        sha256=file_sha256(resolved),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        width=width,
        height=height,
    )


def scan_folder(folder_path: str | Path, recursive: bool = True) -> ScanResult:
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    result = ScanResult()
    for path in _iter_files(root, recursive):
        result.scanned += 1
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            result.skipped += 1
            continue
        result.seen_supported_ids.add(document_id(path))
        try:
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
