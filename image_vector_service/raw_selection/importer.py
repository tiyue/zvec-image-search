"""File and folder import for the ARW selection module.

Import only registers source-file references — it does not copy originals,
read full file content, compute SHA-256 or perform RAW decode. Path
enumeration skips reparse points, symlinks and entries that would cause
recursion loops. Only explicitly supported extensions are registered.
"""

from __future__ import annotations

import io
import os
import stat
import struct
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from .db import AssetRecord, AssetRegistration, RawSelectionDB

SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".arw"})
_ARW_METADATA_VERSION = "arw-metadata-v2-container-orientation"

# Other camera RAW formats that must be skipped with a safe count.
SKIPPED_RAW_EXTENSIONS = frozenset(
    {".cr2", ".cr3", ".nef", ".raf", ".dng", ".orf", ".rw2", ".pef", ".arw"}
) - frozenset({".arw"})  # .arw is supported

# Batch size for DB commits
_BATCH_SIZE = 200
_INCREMENTAL_BATCH_SIZE = 24
_METADATA_BATCH_SIZE = 32
_SUPPORTED_ARW_MAKE = "sony"
_SUPPORTED_ARW_MODEL = "ILCE-7M4"
_TIFF_ASCII = 2
_TIFF_MAKE_TAG = 0x010F
_TIFF_MODEL_TAG = 0x0110
_MAX_TIFF_IFD_OFFSET = 16 * 1024 * 1024
_MAX_TIFF_ASCII_BYTES = 1024


@dataclass(frozen=True, slots=True)
class ImportResult:
    registered: int = 0
    skipped_unsupported: int = 0
    skipped_raw_formats: int = 0
    skipped_unsupported_camera: int = 0
    skipped_reparse: int = 0
    errors: int = 0
    error_details: tuple[str, ...] = field(default_factory=tuple)
    unsupported_camera_details: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    shot_time: str | None
    width: int
    height: int


def normalize_path(path: str | Path) -> str:
    """Return a case-normalized absolute path string."""
    resolved = Path(path).expanduser().resolve()
    return os.path.normcase(str(resolved))


def read_arw_camera_identity(path: str | Path) -> tuple[str, str]:
    """Read TIFF make/model fields without decoding RAW image pixels."""

    with Path(path).open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8 or header[:2] not in {b"II", b"MM"}:
            raise ValueError("ARW has an invalid TIFF header")
        endian = "<" if header[:2] == b"II" else ">"
        if struct.unpack(f"{endian}H", header[2:4])[0] != 42:
            raise ValueError("ARW uses an unsupported TIFF layout")
        ifd_offset = struct.unpack(f"{endian}I", header[4:8])[0]
        if ifd_offset < 8 or ifd_offset > _MAX_TIFF_IFD_OFFSET:
            raise ValueError("ARW has an invalid metadata offset")

        handle.seek(ifd_offset)
        count_bytes = handle.read(2)
        if len(count_bytes) != 2:
            raise ValueError("ARW metadata is truncated")
        entry_count = struct.unpack(f"{endian}H", count_bytes)[0]
        if entry_count > 4096:
            raise ValueError("ARW metadata contains too many entries")

        values: dict[int, str] = {}
        for _ in range(entry_count):
            entry = handle.read(12)
            if len(entry) != 12:
                raise ValueError("ARW metadata entry is truncated")
            tag, value_type, value_count = struct.unpack(f"{endian}HHI", entry[:8])
            if tag not in {_TIFF_MAKE_TAG, _TIFF_MODEL_TAG}:
                continue
            if (
                value_type != _TIFF_ASCII
                or not 1 <= value_count <= _MAX_TIFF_ASCII_BYTES
            ):
                raise ValueError("ARW camera identity has an invalid type")
            if value_count <= 4:
                raw_value = entry[8 : 8 + value_count]
            else:
                value_offset = struct.unpack(f"{endian}I", entry[8:12])[0]
                if value_offset > _MAX_TIFF_IFD_OFFSET:
                    raise ValueError("ARW camera identity offset is invalid")
                return_position = handle.tell()
                handle.seek(value_offset)
                raw_value = handle.read(value_count)
                handle.seek(return_position)
                if len(raw_value) != value_count:
                    raise ValueError("ARW camera identity is truncated")
            values[tag] = raw_value.rstrip(b"\0 ").decode("ascii", errors="strict")

    make = values.get(_TIFF_MAKE_TAG, "").strip()
    model = values.get(_TIFF_MODEL_TAG, "").strip()
    if not make or not model:
        raise ValueError("ARW camera make/model metadata is missing")
    return make, model


def unsupported_arw_camera(path: str | Path) -> str | None:
    """Return an explanation when *path* is not a supported Sony A7M4 ARW."""

    make, model = read_arw_camera_identity(path)
    if make.casefold() == _SUPPORTED_ARW_MAKE and model.upper() == _SUPPORTED_ARW_MODEL:
        return None
    return f"Unsupported ARW camera: {make} {model}".strip()


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
    _scan_counts: dict[str, int] | None = None,
    _scan_errors: list[str] | None = None,
) -> Iterator[tuple[Path, str, str, bool]]:
    """Yield (path, normalized_path, extension, is_supported) for image files.

    Recursively walks *root*, skipping reparse points and directories
    already visited (to prevent symlink loops). Yields supported files
    with ``is_supported=True`` and unsupported RAW formats (CR2, NEF, etc.)
    with ``is_supported=False`` so the caller can count them.
    """
    if _seen_dirs is None:
        _seen_dirs = set()
    if _scan_counts is None:
        _scan_counts = {"reparse": 0}
    if _scan_errors is None:
        _scan_errors = []

    root_path = Path(root).resolve()
    root_key = os.path.normcase(str(root_path))
    if root_key in _seen_dirs:
        return
    _seen_dirs.add(root_key)

    try:
        entries = list(os.scandir(root_path))
    except (OSError, PermissionError) as exc:
        _scan_errors.append(f"Cannot scan directory: {root_path} ({exc})")
        return
    entries.sort(key=lambda entry: os.path.normcase(entry.name))

    for entry in entries:
        try:
            if entry.is_symlink() or _is_reparse_point(entry.path):
                _scan_counts["reparse"] += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _scan_image_files(
                    entry.path,
                    _seen_dirs=_seen_dirs,
                    _scan_counts=_scan_counts,
                    _scan_errors=_scan_errors,
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
        value = os.lstat(path)
    except OSError:
        return None
    return value if stat.S_ISREG(value.st_mode) else None


def _classify_arw(path: Path, extension: str) -> tuple[str, str | None]:
    if extension != ".arw":
        return "supported", None
    try:
        unsupported = unsupported_arw_camera(path)
    except (OSError, UnicodeError, ValueError) as exc:
        return "error", f"Cannot verify ARW camera metadata: {path} ({exc})"
    if unsupported is not None:
        return "unsupported_camera", f"{unsupported}: {path}"
    return "supported", None


def source_file_identity(value: os.stat_result) -> str | None:
    """Return the stable file identity exposed by the current platform."""
    device = int(getattr(value, "st_dev", 0))
    inode = int(getattr(value, "st_ino", 0))
    if device == 0 and inode == 0:
        return None
    return f"{device:x}:{inode:x}"


def _source_version_matches(asset: AssetRecord, value: os.stat_result) -> bool:
    identity = source_file_identity(value)
    return (
        int(value.st_size) == asset.file_size
        and int(value.st_mtime_ns) == asset.mtime_ns
        and (
            asset.file_identity is None
            or identity is None
            or identity == asset.file_identity
        )
    )


def _normalize_exif_datetime(value: object) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")


def _metadata_from_pillow(
    image: Image.Image,
    *,
    orientation_override: int | None = None,
) -> ImageMetadata:
    width, height = image.size
    shot_time = None
    orientation = 1
    has_orientation = False
    try:
        exif = image.getexif()
        has_orientation = 274 in exif
        orientation = int(exif.get(274, 1))
        for tag in (36867, 36868, 306):
            shot_time = _normalize_exif_datetime(exif.get(tag))
            if shot_time is not None:
                break
    except (AttributeError, TypeError, ValueError):
        pass
    if (
        not has_orientation
        and orientation_override is not None
        and 1 <= orientation_override <= 8
    ):
        orientation = orientation_override
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    return ImageMetadata(shot_time=shot_time, width=width, height=height)


def extract_lightweight_metadata(asset: AssetRecord) -> ImageMetadata:
    """Read dimensions and capture time without running a full RAW postprocess."""
    path = Path(asset.normalized_path)
    if asset.extension.casefold() != ".arw":
        with Image.open(path) as image:
            return _metadata_from_pillow(image)

    from .decoder import extract_arw_embedded_jpeg, read_arw_orientation

    with Image.open(io.BytesIO(extract_arw_embedded_jpeg(path))) as image:
        return _metadata_from_pillow(
            image,
            orientation_override=read_arw_orientation(path),
        )


class AssetImporter:
    """Scans files/folders and registers source-file references in the DB."""

    def __init__(self, db: RawSelectionDB) -> None:
        self._db = db
        self._metadata_stop = threading.Event()
        self._metadata_wake = threading.Event()
        self._metadata_condition = threading.Condition()
        self._metadata_thread: threading.Thread | None = None
        self._db.ensure_arw_metadata_version(_ARW_METADATA_VERSION)
        self._db.reset_processing_metadata()
        if self._db.count_unfinished_metadata():
            self._schedule_metadata()

    def close(self) -> None:
        self._metadata_stop.set()
        self._metadata_wake.set()
        thread = self._metadata_thread
        if thread is not None:
            thread.join()

    def wait_for_metadata(self, timeout: float = 5.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if self._db.count_unfinished_metadata() == 0:
            return True
        self._schedule_metadata()
        deadline = time.monotonic() + timeout
        with self._metadata_condition:
            while self._db.count_unfinished_metadata() > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._metadata_condition.wait(remaining)
        return True

    def schedule_metadata(self) -> None:
        """Wake the metadata worker after an external source-version refresh."""

        self._schedule_metadata()

    def _schedule_metadata(self) -> None:
        with self._metadata_condition:
            if self._metadata_thread is None:
                self._metadata_thread = threading.Thread(
                    target=self._metadata_loop,
                    name="raw-selection-metadata",
                    daemon=True,
                )
                self._metadata_thread.start()
            self._metadata_wake.set()

    def _metadata_loop(self) -> None:
        while not self._metadata_stop.is_set():
            self._metadata_wake.wait()
            self._metadata_wake.clear()
            if self._metadata_stop.is_set():
                break
            while not self._metadata_stop.is_set():
                pending = self._db.list_assets_needing_metadata(
                    limit=_METADATA_BATCH_SIZE
                )
                if not pending:
                    break
                for asset in pending:
                    if self._metadata_stop.is_set():
                        break
                    self._extract_one_metadata(asset)
                    with self._metadata_condition:
                        self._metadata_condition.notify_all()
            with self._metadata_condition:
                self._metadata_condition.notify_all()

    def _extract_one_metadata(self, asset: AssetRecord) -> None:
        before = _stat_file(Path(asset.normalized_path))
        if before is not None and asset.file_identity is None:
            identity = source_file_identity(before)
            if identity is not None:
                self._db.set_asset_identity_if_missing(
                    asset.id,
                    file_size=asset.file_size,
                    mtime_ns=asset.mtime_ns,
                    file_identity=identity,
                )
                refreshed = self._db.get_asset(asset.id)
                if refreshed is not None:
                    asset = refreshed
        if before is not None and not _source_version_matches(asset, before):
            self._db.refresh_asset_source_version(
                asset,
                file_size=int(before.st_size),
                mtime_ns=int(before.st_mtime_ns),
                file_identity=source_file_identity(before),
            )
            self._metadata_wake.set()
            return
        if not self._db.claim_asset_metadata(asset):
            return
        if before is None:
            self._db.update_asset_metadata(
                asset,
                shot_time=None,
                width=None,
                height=None,
                metadata_status="failed",
            )
            return

        try:
            metadata = extract_lightweight_metadata(asset)
        except Exception:
            self._db.update_asset_metadata(
                asset,
                shot_time=None,
                width=None,
                height=None,
                metadata_status="failed",
            )
            return

        after = _stat_file(Path(asset.normalized_path))
        if after is None:
            self._db.update_asset_metadata(
                asset,
                shot_time=None,
                width=None,
                height=None,
                metadata_status="failed",
            )
            return
        if not _source_version_matches(asset, after):
            self._db.refresh_asset_source_version(
                asset,
                file_size=int(after.st_size),
                mtime_ns=int(after.st_mtime_ns),
                file_identity=source_file_identity(after),
            )
            self._metadata_wake.set()
            return
        self._db.update_asset_metadata(
            asset,
            shot_time=metadata.shot_time,
            width=metadata.width,
            height=metadata.height,
            metadata_status="ready",
        )

    def import_files(
        self,
        project_id: str,
        file_paths: Sequence[str | Path],
    ) -> ImportResult:
        """Register individual files into a project."""
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        registrations: list[AssetRegistration] = []
        skipped_unsupported = 0
        skipped_raw_formats = 0
        skipped_unsupported_camera = 0
        skipped_reparse = 0
        errors: list[str] = []
        unsupported_camera_details: list[str] = []
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
                skipped_reparse += 1
                continue

            st = _stat_file(p)
            if st is None:
                errors.append(f"Cannot stat file: {p}")
                error_count += 1
                continue

            classification, detail = _classify_arw(p, ext)
            if classification == "unsupported_camera":
                skipped_unsupported_camera += 1
                if detail is not None:
                    unsupported_camera_details.append(detail)
                continue
            if classification == "error":
                error_count += 1
                if detail is not None:
                    errors.append(detail)
                continue

            norm = normalize_path(p)
            registrations.append(
                AssetRegistration(
                    normalized_path=norm,
                    file_name=p.name,
                    extension=ext,
                    file_size=int(st.st_size),
                    mtime_ns=int(st.st_mtime_ns),
                    file_identity=source_file_identity(st),
                )
            )

        registered, assets = self._db.register_assets_batch(project_id, registrations)
        if assets:
            self._schedule_metadata()
        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            skipped_unsupported_camera=skipped_unsupported_camera,
            skipped_reparse=skipped_reparse,
            errors=error_count,
            error_details=tuple(errors),
            unsupported_camera_details=tuple(unsupported_camera_details),
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
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        requested_root = Path(folder_path).expanduser()
        if not requested_root.exists():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {requested_root}",),
            )
        if _is_reparse_point(requested_root):
            return ImportResult(
                errors=1,
                error_details=(f"Root is a reparse point: {requested_root}",),
            )
        root = requested_root.resolve()
        if not root.is_dir():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {root}",),
            )

        registered = 0
        skipped_unsupported = 0
        skipped_raw_formats = 0
        skipped_unsupported_camera = 0
        error_details: list[str] = []
        unsupported_camera_details: list[str] = []
        error_count = 0
        batch: list[AssetRegistration] = []
        scan_counts = {"reparse": 0}
        scan_errors: list[str] = []

        for file_path, norm_path, ext, is_supported in _scan_image_files(
            root,
            _scan_counts=scan_counts,
            _scan_errors=scan_errors,
        ):
            if not is_supported:
                skipped_raw_formats += 1
                continue

            st = _stat_file(file_path)
            if st is None:
                error_count += 1
                error_details.append(f"Cannot stat: {file_path}")
                continue

            classification, detail = _classify_arw(file_path, ext)
            if classification == "unsupported_camera":
                skipped_unsupported_camera += 1
                if detail is not None:
                    unsupported_camera_details.append(detail)
                continue
            if classification == "error":
                error_count += 1
                if detail is not None:
                    error_details.append(detail)
                continue

            batch.append(
                AssetRegistration(
                    normalized_path=norm_path,
                    file_name=file_path.name,
                    extension=ext,
                    file_size=int(st.st_size),
                    mtime_ns=int(st.st_mtime_ns),
                    file_identity=source_file_identity(st),
                )
            )

            if len(batch) >= _BATCH_SIZE:
                added, assets = self._db.register_assets_batch(project_id, batch)
                registered += added
                batch.clear()
                if assets:
                    self._schedule_metadata()

        # Flush remaining batch
        if batch:
            added, assets = self._db.register_assets_batch(project_id, batch)
            registered += added
            if assets:
                self._schedule_metadata()

        error_details.extend(scan_errors)
        error_count += len(scan_errors)
        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            skipped_unsupported_camera=skipped_unsupported_camera,
            skipped_reparse=scan_counts["reparse"],
            errors=error_count,
            error_details=tuple(error_details),
            unsupported_camera_details=tuple(unsupported_camera_details),
        )

    def import_folder_incremental(
        self,
        project_id: str,
        folder_path: str | Path,
        *,
        on_batch: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ImportResult:
        """Import folder with incremental callback for progress reporting.

        Calls *on_batch(registered_so_far, total_seen_so_far)* after each
        batch commit, enabling the caller to enter the workspace as soon as
        the first batch is available.
        """

        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        requested_root = Path(folder_path).expanduser()
        if not requested_root.exists():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {requested_root}",),
            )
        if _is_reparse_point(requested_root):
            return ImportResult(
                errors=1,
                error_details=(f"Root is a reparse point: {requested_root}",),
            )
        root = requested_root.resolve()
        if not root.is_dir():
            return ImportResult(
                errors=1,
                error_details=(f"Not a directory: {root}",),
            )

        registered = 0
        seen = 0
        skipped_unsupported = 0
        skipped_raw_formats = 0
        skipped_unsupported_camera = 0
        error_details: list[str] = []
        unsupported_camera_details: list[str] = []
        error_count = 0
        batch: list[AssetRegistration] = []
        scan_counts = {"reparse": 0}
        scan_errors: list[str] = []

        for file_path, norm_path, ext, is_supported in _scan_image_files(
            root,
            _scan_counts=scan_counts,
            _scan_errors=scan_errors,
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            seen += 1
            if not is_supported:
                skipped_raw_formats += 1
                continue

            st = _stat_file(file_path)
            if st is None:
                error_count += 1
                error_details.append(f"Cannot stat: {file_path}")
                continue

            classification, detail = _classify_arw(file_path, ext)
            if classification == "unsupported_camera":
                skipped_unsupported_camera += 1
                if detail is not None:
                    unsupported_camera_details.append(detail)
                continue
            if classification == "error":
                error_count += 1
                if detail is not None:
                    error_details.append(detail)
                continue

            batch.append(
                AssetRegistration(
                    normalized_path=norm_path,
                    file_name=file_path.name,
                    extension=ext,
                    file_size=int(st.st_size),
                    mtime_ns=int(st.st_mtime_ns),
                    file_identity=source_file_identity(st),
                )
            )

            if len(batch) >= _INCREMENTAL_BATCH_SIZE:
                added, assets = self._db.register_assets_batch(project_id, batch)
                registered += added
                batch.clear()
                if assets:
                    self._schedule_metadata()
                if on_batch is not None:
                    on_batch(registered, seen)

        if batch and not (cancel_event is not None and cancel_event.is_set()):
            added, assets = self._db.register_assets_batch(project_id, batch)
            registered += added
            if assets:
                self._schedule_metadata()
            if on_batch is not None:
                on_batch(registered, seen)

        error_details.extend(scan_errors)
        error_count += len(scan_errors)
        return ImportResult(
            registered=registered,
            skipped_unsupported=skipped_unsupported,
            skipped_raw_formats=skipped_raw_formats,
            skipped_unsupported_camera=skipped_unsupported_camera,
            skipped_reparse=scan_counts["reparse"],
            errors=error_count,
            error_details=tuple(error_details),
            unsupported_camera_details=tuple(unsupported_camera_details),
        )
