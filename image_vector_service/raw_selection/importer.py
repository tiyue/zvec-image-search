"""File and folder import for the ARW selection module.

Import only registers source-file references — it does not copy originals,
read full file content, compute SHA-256 or perform RAW decode. Path
enumeration skips reparse points, symlinks and entries that would cause
recursion loops. Only explicitly supported extensions are registered.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .db import AssetRecord, RawSelectionDB

SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".arw"})

# Other camera RAW formats that must be skipped with a safe count.
SKIPPED_RAW_EXTENSIONS = frozenset(
    {".cr2", ".cr3", ".nef", ".raf", ".dng", ".orf", ".rw2", ".pef", ".arw"}
) - frozenset({".arw"})  # .arw is supported

# Batch size for DB commits
_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class ImportResult:
    registered: int = 0
    skipped_unsupported: int = 0
    skipped_raw_formats: int = 0
    skipped_reparse: int = 0
    errors: int = 0
    error_details: tuple[str, ...] = field(default_factory=tuple)


def normalize_path(path: str | Path) -> str:
    """Return a case-normalized absolute path string."""
    resolved = Path(path).expanduser().resolve()
    return os.path.normcase(str(resolved))


def _is_reparse_point(path: str | Path) -> bool:
    """Return True for symlinks, junctions and other reparse points."""
    spath = str(path)
    if os.path.islink(spath):
        return True
    try:
        st = os.lstat(spath)
    except OSError:
        return True
    # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    file_attrs = getattr(st, "st_file_attributes", 0)
    if file_attrs & 0x400:
        return True
    reparse_tag = getattr(st, "st_reparse_tag", None)
    return bool(reparse_tag is not None and reparse_tag != 0)


def _scan_image_files(
    root: str | Path,
    *,
    _seen_dirs: set[str] | None = None,
) -> Iterator[tuple[Path, str, str, bool]]:
    """Yield (path, normalized_path, extension, is_supported) for image files.

    Recursively walks *root*, skipping reparse points and directories
    already visited (to prevent symlink loops). Yields supported files
    with ``is_supported=True`` and unsupported RAW formats (CR2, NEF, etc.)
    with ``is_supported=False`` so the caller can count them.
    """
    if _seen_dirs is None:
        _seen_dirs = set()

    root_path = Path(root).resolve()
    root_key = os.path.normcase(str(root_path))
    if root_key in _seen_dirs:
        return
    _seen_dirs.add(root_key)

    try:
        entries = list(os.scandir(root_path))
    except (OSError, PermissionError):
        return

    for entry in entries:
        try:
            if entry.is_symlink() or _is_reparse_point(entry.path):
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _scan_image_files(
                    entry.path, _seen_dirs=_seen_dirs
                )
            elif entry.is_file(follow_symlinks=False):
                # Skip macOS metadata/resource-fork artifacts (._NAME).
                if entry.name.startswith("._"):
                    continue
                ext = Path(entry.name).suffix.casefold()
                if ext in SUPPORTED_EXTENSIONS:
                    norm = normalize_path(entry.path)
                    yield (Path(entry.path), norm, ext, True)
                elif ext in SKIPPED_RAW_EXTENSIONS:
                    yield (Path(entry.path), "", ext, False)
        except OSError:
            continue


def _stat_file(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


class AssetImporter:
    """Scans files/folders and registers source-file references in the DB."""

    def __init__(self, db: RawSelectionDB) -> None:
        self._db = db

    def import_files(
        self,
        project_id: str,
        file_paths: Sequence[str | Path],
    ) -> ImportResult:
        """Register individual files into a project."""
        assets: list[AssetRecord] = []
        skipped_unsupported = 0
        skipped_raw_formats = 0
        errors: list[str] = []
        error_count = 0

        for raw_path in file_paths:
            p = Path(raw_path)
            ext = p.suffix.casefold()
            if ext not in SUPPORTED_EXTENSIONS:
                if ext in SKIPPED_RAW_EXTENSIONS:
                    skipped_raw_formats += 1
                else:
                    skipped_unsupported += 1
                continue

            if _is_reparse_point(p):
                errors.append(f"Reparse point skipped: {p}")
                error_count += 1
                continue

            st = _stat_file(p)
            if st is None:
                errors.append(f"Cannot stat file: {p}")
                error_count += 1
                continue

            norm = normalize_path(p)
            asset_id = self._db.upsert_asset(
                normalized_path=norm,
                file_name=p.name,
                extension=ext,
                file_size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
            asset = self._db.get_asset(asset_id)
            if asset is not None:
                assets.append(asset)

        registered = self._db.add_members_batch(project_id, assets)
        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            errors=error_count,
            error_details=tuple(errors),
        )

    def import_folder(
        self,
        project_id: str,
        folder_path: str | Path,
    ) -> ImportResult:
        """Recursively scan a folder and register supported files.

        Path enumeration does not read file content, compute SHA-256 or
        perform RAW decode. Uses batch transactions for persistence.
        """
        root = Path(folder_path).resolve()
        if not root.is_dir():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {root}",),
            )

        if _is_reparse_point(root):
            return ImportResult(
                errors=1,
                error_details=(f"Root is a reparse point: {root}",),
            )

        registered = 0
        skipped_unsupported = 0
        skipped_raw_formats = 0
        skipped_reparse = 0
        error_details: list[str] = []
        error_count = 0
        batch: list[AssetRecord] = []

        for file_path, norm_path, ext, is_supported in _scan_image_files(root):
            if not is_supported:
                skipped_raw_formats += 1
                continue

            st = _stat_file(file_path)
            if st is None:
                error_count += 1
                error_details.append(f"Cannot stat: {file_path}")
                continue

            asset_id = self._db.upsert_asset(
                normalized_path=norm_path,
                file_name=file_path.name,
                extension=ext,
                file_size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
            asset = self._db.get_asset(asset_id)
            if asset is not None:
                batch.append(asset)

            if len(batch) >= _BATCH_SIZE:
                registered += self._db.add_members_batch(project_id, batch)
                batch.clear()

        # Flush remaining batch
        if batch:
            registered += self._db.add_members_batch(project_id, batch)

        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            skipped_reparse=skipped_reparse,
            errors=error_count,
            error_details=tuple(error_details),
        )

    def import_folder_incremental(
        self,
        project_id: str,
        folder_path: str | Path,
        *,
        on_batch: Callable[[int, int], None] | None = None,
    ) -> ImportResult:
        """Import folder with incremental callback for progress reporting.

        Calls *on_batch(registered_so_far, total_seen_so_far)* after each
        batch commit, enabling the caller to enter the workspace as soon as
        the first batch is available.
        """

        root = Path(folder_path).resolve()
        if not root.is_dir():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {root}",),
            )

        if _is_reparse_point(root):
            return ImportResult(
                errors=1,
                error_details=(f"Root is a reparse point: {root}",),
            )

        registered = 0
        seen = 0
        skipped_unsupported = 0
        skipped_raw_formats = 0
        error_details: list[str] = []
        error_count = 0
        batch: list[AssetRecord] = []

        for file_path, norm_path, ext, is_supported in _scan_image_files(root):
            seen += 1
            if not is_supported:
                skipped_raw_formats += 1
                continue

            st = _stat_file(file_path)
            if st is None:
                error_count += 1
                error_details.append(f"Cannot stat: {file_path}")
                continue

            asset_id = self._db.upsert_asset(
                normalized_path=norm_path,
                file_name=file_path.name,
                extension=ext,
                file_size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
            asset = self._db.get_asset(asset_id)
            if asset is not None:
                batch.append(asset)

            if len(batch) >= _BATCH_SIZE:
                registered += self._db.add_members_batch(project_id, batch)
                batch.clear()
                if on_batch is not None:
                    on_batch(registered, seen)

        if batch:
            registered += self._db.add_members_batch(project_id, batch)
            if on_batch is not None:
                on_batch(registered, seen)

        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            errors=error_count,
            error_details=tuple(error_details),
        )
