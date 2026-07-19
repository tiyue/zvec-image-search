"""Opaque, bounded image registry used by the local webview gateway.

The browser never receives an absolute filesystem path.  Trusted Python code
registers a validated image and gives the HTML layer an unguessable identifier;
the gateway then serves a size-bounded, browser-friendly representation.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

ImageVariant = Literal["thumbnail", "preview"]

_VARIANT_SIZES: Final[dict[ImageVariant, tuple[int, int]]] = {
    "thumbnail": (640, 640),
    "preview": (1920, 1920),
}
_MAX_SOURCE_BYTES: Final = 512 * 1024 * 1024
_MAX_PIXELS: Final = 200_000_000
_DISK_CACHE_SCHEMA: Final = 1
_MAX_DISK_PAYLOAD_BYTES: Final = 32 * 1024 * 1024
_DEFAULT_DISK_CACHE_BYTES: Final = 2 * 1024 * 1024 * 1024
_DEFAULT_DISK_CACHE_ENTRIES: Final = 100_000
_DISK_PRUNE_INTERVAL: Final = 256
_DISK_PRUNE_COOLDOWN_SECONDS: Final = 10 * 60
_STALE_TEMP_SECONDS: Final = 24 * 60 * 60
_MAX_RECENT_DISK_ENTRIES: Final = 10_000


class ImageRegistryError(RuntimeError):
    """A registered image cannot be resolved or rendered safely."""


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """Encoded image bytes plus the HTTP metadata required by the gateway."""

    content: bytes
    content_type: str
    etag: str


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """Non-sensitive metadata safe to return to the HTML application."""

    image_id: str
    name: str
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _RegisteredImage:
    image_id: str
    path: Path
    size_bytes: int
    mtime_ns: int
    width: int
    height: int


@dataclass(slots=True)
class _RenderFlight:
    """Share one render between concurrent HTTP requests for the same image."""

    event: threading.Event
    payload: ImagePayload | None = None
    error: BaseException | None = None


class ImageRegistry:
    """Thread-safe registry with bounded decoded-image caching.

    ``max_entries`` bounds path metadata and ``max_cache_bytes`` bounds encoded
    previews.  Both limits matter for libraries with hundreds of thousands of
    images because the webview process is intended to remain responsive during
    long indexing and annotation jobs.
    """

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        max_cache_bytes: int = 64 * 1024 * 1024,
        cache_directory: str | Path | None = None,
        max_disk_cache_bytes: int = _DEFAULT_DISK_CACHE_BYTES,
        max_disk_cache_entries: int = _DEFAULT_DISK_CACHE_ENTRIES,
        max_render_workers: int = 2,
    ) -> None:
        if isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_cache_bytes, bool) or max_cache_bytes < 1024 * 1024:
            raise ValueError("max_cache_bytes must be at least 1 MiB")
        if isinstance(max_disk_cache_bytes, bool) or max_disk_cache_bytes < 1024 * 1024:
            raise ValueError("max_disk_cache_bytes must be at least 1 MiB")
        if isinstance(max_disk_cache_entries, bool) or max_disk_cache_entries < 1:
            raise ValueError("max_disk_cache_entries must be a positive integer")
        if isinstance(max_render_workers, bool) or not 1 <= max_render_workers <= 16:
            raise ValueError("max_render_workers must be between 1 and 16")
        self._max_entries = max_entries
        self._max_cache_bytes = max_cache_bytes
        self._max_disk_cache_bytes = max_disk_cache_bytes
        self._max_disk_cache_entries = max_disk_cache_entries
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _RegisteredImage] = OrderedDict()
        self._by_path: dict[Path, str] = {}
        self._cache: OrderedDict[tuple[str, ImageVariant, int, int], ImagePayload] = (
            OrderedDict()
        )
        self._cache_bytes = 0
        self._flights: dict[tuple[str, ImageVariant, int, int], _RenderFlight] = {}
        self._render_slots = threading.BoundedSemaphore(max_render_workers)
        self._disk_cache_root = _prepare_disk_cache(cache_directory)
        self._disk_writes_since_prune = 0
        self._disk_prune_running = False
        self._last_disk_prune = 0.0
        self._recent_disk_entries: OrderedDict[Path, None] = OrderedDict()
        self._initial_disk_prune_scheduled = False
        self._disk_prune_thread: threading.Thread | None = None
        self._closed = False

    @property
    def disk_cache_directory(self) -> Path | None:
        """Return the dedicated cache directory, never an original-image root."""

        return self._disk_cache_root

    def register(self, path: str | Path) -> ImageMetadata:
        """Register one existing regular image and return only safe metadata."""

        resolved, stat_result = _source_stat(path)

        with self._lock:
            existing_id = self._by_path.get(resolved)
            if existing_id is not None and existing_id in self._entries:
                existing = self._entries[existing_id]
                if (
                    existing.size_bytes == stat_result.st_size
                    and existing.mtime_ns == stat_result.st_mtime_ns
                ):
                    self._entries.move_to_end(existing_id)
                    return _metadata(existing)

        width, height, stat_result = _stable_dimensions(resolved, stat_result)

        with self._lock:
            existing_id = self._by_path.get(resolved)
            if existing_id is not None and existing_id in self._entries:
                existing = self._entries.pop(existing_id)
                if (
                    existing.size_bytes != stat_result.st_size
                    or existing.mtime_ns != stat_result.st_mtime_ns
                ):
                    self._drop_payloads_locked(existing_id)
                refreshed = _RegisteredImage(
                    existing_id,
                    resolved,
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    width,
                    height,
                )
                self._entries[existing_id] = refreshed
                return _metadata(refreshed)
            image_id = secrets.token_urlsafe(24)
            while image_id in self._entries:
                image_id = secrets.token_urlsafe(24)
            self._entries[image_id] = _RegisteredImage(
                image_id,
                resolved,
                stat_result.st_size,
                stat_result.st_mtime_ns,
                width,
                height,
            )
            self._by_path[resolved] = image_id
            self._evict_entries_locked()
            return _metadata(self._entries[image_id])

    def resolve(self, image_id: str) -> Path:
        """Resolve an opaque identifier for trusted native actions only."""

        normalized = _image_id(image_id)
        with self._lock:
            entry = self._entries.pop(normalized, None)
            if entry is None:
                raise ImageRegistryError("图片引用已失效，请刷新页面。")
            self._entries[normalized] = entry
        try:
            resolved = entry.path.resolve(strict=True)
        except OSError as exc:
            self.forget(normalized)
            raise ImageRegistryError("图片已被移动或删除。") from exc
        if resolved != entry.path or not resolved.is_file() or resolved.is_symlink():
            self.forget(normalized)
            raise ImageRegistryError("图片路径发生变化，请刷新页面。")
        return resolved

    def payload(self, image_id: str, variant: ImageVariant) -> ImagePayload:
        """Return a bounded thumbnail or preview without exposing its path."""

        if variant not in _VARIANT_SIZES:
            raise ImageRegistryError("不支持的图片预览尺寸。")
        normalized = _image_id(image_id)
        return self._payload(normalized, variant, attempt=0)

    def _payload(
        self,
        image_id: str,
        variant: ImageVariant,
        *,
        attempt: int,
    ) -> ImagePayload:
        path = self.resolve(image_id)
        _resolved, stat_result = _source_stat(path)
        cache_key = (image_id, variant, stat_result.st_mtime_ns, stat_result.st_size)
        with self._lock:
            expected_entry = self._entries.get(image_id)
            if expected_entry is None:
                raise ImageRegistryError("图片引用已失效，请刷新页面。")
            cached = self._cache.pop(cache_key, None)
            if cached is not None:
                self._cache[cache_key] = cached
                return cached
            flight = self._flights.get(cache_key)
            owner = flight is None
            if flight is None:
                flight = _RenderFlight(threading.Event())
                self._flights[cache_key] = flight

        if not owner:
            flight.event.wait()
            if flight.error is not None:
                raise ImageRegistryError(str(flight.error)) from flight.error
            if flight.payload is None:
                raise ImageRegistryError("图片预览生成失败。")
            return flight.payload

        try:
            disk_path = self._disk_path(path, variant, stat_result)
            payload = (
                _read_disk_payload(disk_path, variant, stat_result.st_mtime_ns)
                if disk_path is not None
                else None
            )
            if payload is not None and disk_path is not None:
                self._mark_disk_recent(disk_path, schedule_initial_prune=True)
            if payload is None:
                with self._render_slots:
                    payload = _render_image(path, variant, stat_result.st_mtime_ns)
            _verified_path, refreshed_stat = _source_stat(path)
            if (
                refreshed_stat.st_mtime_ns != stat_result.st_mtime_ns
                or refreshed_stat.st_size != stat_result.st_size
            ):
                if attempt >= 1:
                    raise ImageRegistryError("图片在生成预览期间反复变化，请稍后重试。")
                retried = self._payload(image_id, variant, attempt=attempt + 1)
                with self._lock:
                    flight.payload = retried
                return retried
            with self._lock:
                if self._entries.get(image_id) is not expected_entry:
                    raise ImageRegistryError("图片引用已失效，请刷新页面。")
            if (
                disk_path is not None
                and not disk_path.is_file()
                and _write_disk_payload(disk_path, payload.content)
            ):
                self._mark_disk_recent(disk_path, schedule_initial_prune=False)
                self._note_disk_write()
            with self._lock:
                if self._entries.get(image_id) is not expected_entry:
                    raise ImageRegistryError("图片引用已失效，请刷新页面。")
                self._cache[cache_key] = payload
                self._cache_bytes += len(payload.content)
                self._evict_cache_locked()
                flight.payload = payload
            return payload
        except BaseException as exc:
            with self._lock:
                flight.error = exc
            raise
        finally:
            with self._lock:
                self._flights.pop(cache_key, None)
                flight.event.set()

    def forget(self, image_id: str) -> None:
        """Drop one identifier and any cached variants."""

        normalized = _image_id(image_id)
        with self._lock:
            entry = self._entries.pop(normalized, None)
            if entry is not None and self._by_path.get(entry.path) == normalized:
                self._by_path.pop(entry.path, None)
            self._drop_payloads_locked(normalized)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_path.clear()
            self._cache.clear()
            self._cache_bytes = 0

    def close(self) -> None:
        """Release in-memory state; persistent thumbnails remain reusable."""

        with self._lock:
            self._closed = True
            prune_thread = self._disk_prune_thread
        self.clear()
        if prune_thread is not None and prune_thread is not threading.current_thread():
            prune_thread.join(timeout=2.0)

    def prune_disk_cache(self) -> None:
        """Remove least-recently-used thumbnails beyond configured bounds."""

        root = self._disk_cache_root
        if root is None or not root.is_dir():
            return
        entries: list[tuple[int, int, Path]] = []
        total_bytes = 0
        try:
            candidates = tuple(root.glob("*/*.cache"))
            temporary_files = tuple(root.glob("*/*.tmp"))
        except OSError:
            return
        stale_before = time.time() - _STALE_TEMP_SECONDS
        for temporary in temporary_files:
            try:
                if (
                    _safe_cache_file(root, temporary)
                    and temporary.stat().st_mtime < stale_before
                ):
                    temporary.unlink(missing_ok=True)
            except OSError:
                continue
        with self._lock:
            recent = frozenset(self._recent_disk_entries)
        recent_timestamp = time.time_ns()
        for candidate in candidates:
            try:
                if not _safe_cache_file(root, candidate) or not candidate.is_file():
                    continue
                stat_result = candidate.stat()
            except OSError:
                continue
            size = max(0, stat_result.st_size)
            total_bytes += size
            last_used = (
                recent_timestamp if candidate in recent else stat_result.st_mtime_ns
            )
            entries.append((last_used, size, candidate))
        entries.sort(key=lambda item: (item[0], item[2].name.casefold()))
        remaining = len(entries)
        for _last_used, size, candidate in entries:
            if (
                total_bytes <= self._max_disk_cache_bytes
                and remaining <= self._max_disk_cache_entries
            ):
                break
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                continue
            with self._lock:
                self._recent_disk_entries.pop(candidate, None)
            total_bytes -= size
            remaining -= 1

    def _evict_entries_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            image_id, entry = self._entries.popitem(last=False)
            if self._by_path.get(entry.path) == image_id:
                self._by_path.pop(entry.path, None)
            self._drop_payloads_locked(image_id)

    def _evict_cache_locked(self) -> None:
        while self._cache_bytes > self._max_cache_bytes and self._cache:
            _key, payload = self._cache.popitem(last=False)
            self._cache_bytes -= len(payload.content)

    def _drop_payloads_locked(self, image_id: str) -> None:
        for key in tuple(self._cache):
            if key[0] == image_id:
                payload = self._cache.pop(key)
                self._cache_bytes -= len(payload.content)

    def _disk_path(
        self,
        path: Path,
        variant: ImageVariant,
        stat_result: os.stat_result,
    ) -> Path | None:
        root = self._disk_cache_root
        if root is None or variant != "thumbnail":
            return None
        normalized_path = os.path.normcase(str(path))
        digest = hashlib.sha256(
            (
                f"{_DISK_CACHE_SCHEMA}\0{normalized_path}\0"
                f"{stat_result.st_mtime_ns}\0{stat_result.st_size}\0{variant}"
            ).encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        shard = root / digest[:2]
        if _is_link_like(shard):
            return None
        try:
            shard.resolve().relative_to(root)
        except (OSError, ValueError):
            return None
        return shard / f"{digest}.cache"

    def _mark_disk_recent(self, path: Path, *, schedule_initial_prune: bool) -> None:
        with self._lock:
            self._recent_disk_entries.pop(path, None)
            self._recent_disk_entries[path] = None
            while len(self._recent_disk_entries) > _MAX_RECENT_DISK_ENTRIES:
                self._recent_disk_entries.popitem(last=False)
            initial_prune = (
                schedule_initial_prune and not self._initial_disk_prune_scheduled
            )
            if schedule_initial_prune:
                self._initial_disk_prune_scheduled = True
        if initial_prune:
            self._schedule_disk_prune(force=True)

    def _note_disk_write(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._disk_writes_since_prune += 1
        self._schedule_disk_prune(force=False)

    def _schedule_disk_prune(self, *, force: bool) -> None:
        with self._lock:
            if self._closed or self._disk_prune_running:
                return
            if not force and (
                self._disk_writes_since_prune < _DISK_PRUNE_INTERVAL
                or time.monotonic() - self._last_disk_prune
                < _DISK_PRUNE_COOLDOWN_SECONDS
            ):
                return
            self._disk_writes_since_prune = 0
            self._disk_prune_running = True
            thread = threading.Thread(
                target=self._prune_disk_cache_worker,
                name="zvec-thumbnail-cache-prune",
                daemon=True,
            )
            self._disk_prune_thread = thread
        thread.start()

    def _prune_disk_cache_worker(self) -> None:
        try:
            self.prune_disk_cache()
        finally:
            with self._lock:
                self._last_disk_prune = time.monotonic()
                self._disk_prune_running = False
                self._disk_prune_thread = None


def _render_image(path: Path, variant: ImageVariant, mtime_ns: int) -> ImagePayload:
    try:
        with Image.open(path) as source:
            width, height = source.size
            if width < 1 or height < 1 or width * height > _MAX_PIXELS:
                raise ImageRegistryError("图片尺寸无效或像素数量过大。")
            # Ask JPEG decoders for a smaller intermediate image before the
            # unavoidable EXIF transpose. This avoids decoding full 20-80 MP
            # photographs merely to produce a 640 px tile.
            if str(source.format or "").upper() in {"JPEG", "MPO"}:
                source.draft("RGB", _VARIANT_SIZES[variant])
            image = ImageOps.exif_transpose(source)
            image.thumbnail(_VARIANT_SIZES[variant], Image.Resampling.LANCZOS)
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            output = io.BytesIO()
            if has_alpha:
                png_options = (
                    {"compress_level": 3}
                    if variant == "thumbnail"
                    else {"optimize": True}
                )
                image.convert("RGBA").save(output, format="PNG", **png_options)
                content_type = "image/png"
            else:
                jpeg_options = (
                    {"quality": 80, "optimize": False, "progressive": False}
                    if variant == "thumbnail"
                    else {"quality": 88, "optimize": True, "progressive": True}
                )
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    **jpeg_options,
                )
                content_type = "image/jpeg"
    except ImageRegistryError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ImageRegistryError("图片格式损坏或暂不支持预览。") from exc
    content = output.getvalue()
    if not content:
        raise ImageRegistryError("图片预览生成失败。")
    etag = f'"{mtime_ns:x}-{len(content):x}-{variant}"'
    return ImagePayload(content=content, content_type=content_type, etag=etag)


def _prepare_disk_cache(cache_directory: str | Path | None) -> Path | None:
    if cache_directory is None:
        return None
    root = (
        Path(cache_directory).expanduser().resolve()
        / f"thumbnails-v{_DISK_CACHE_SCHEMA}"
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Preview remains usable when a cache directory is temporarily
        # unavailable; the in-memory cache still protects repeated requests.
        return None
    if not root.is_dir() or _is_link_like(root):
        return None
    return root


def _source_stat(path: str | Path) -> tuple[Path, os.stat_result]:
    candidate = Path(path).expanduser()
    if _is_link_like(candidate):
        raise ImageRegistryError("图片必须是普通文件，不能是符号链接。")
    try:
        resolved = candidate.resolve(strict=True)
        stat_result = resolved.stat()
    except OSError as exc:
        raise ImageRegistryError("图片不存在或无法读取。") from exc
    if not resolved.is_file() or _is_link_like(resolved):
        raise ImageRegistryError("图片必须是普通文件，不能是符号链接。")
    if stat_result.st_size <= 0:
        raise ImageRegistryError("图片文件为空。")
    if stat_result.st_size > _MAX_SOURCE_BYTES:
        raise ImageRegistryError("图片文件过大，无法安全预览。")
    return resolved, stat_result


def _stable_dimensions(
    path: Path, initial_stat: os.stat_result
) -> tuple[int, int, os.stat_result]:
    """Read dimensions only from a stable file snapshot, retrying once."""

    stat_result = initial_stat
    for attempt in range(2):
        width, height = _image_dimensions(path)
        try:
            refreshed = path.stat()
        except OSError as exc:
            raise ImageRegistryError("图片已无法读取。") from exc
        if (
            refreshed.st_size == stat_result.st_size
            and refreshed.st_mtime_ns == stat_result.st_mtime_ns
        ):
            return width, height, refreshed
        stat_result = refreshed
        if attempt == 0:
            continue
    raise ImageRegistryError("图片在读取期间发生变化，请重试。")


def _metadata(entry: _RegisteredImage) -> ImageMetadata:
    return ImageMetadata(
        entry.image_id,
        entry.path.name,
        entry.size_bytes,
        entry.width,
        entry.height,
    )


def _read_disk_payload(
    path: Path,
    variant: ImageVariant,
    mtime_ns: int,
) -> ImagePayload | None:
    try:
        if not path.is_file() or _is_link_like(path) or _is_link_like(path.parent):
            return None
        size = path.stat().st_size
        if size <= 0 or size > _MAX_DISK_PAYLOAD_BYTES:
            path.unlink(missing_ok=True)
            return None
        content = path.read_bytes()
        if len(content) != size:
            raise OSError("thumbnail cache changed during read")
        with Image.open(io.BytesIO(content)) as cached:
            cached.verify()
            image_format = str(cached.format or "").upper()
        if image_format == "JPEG":
            content_type = "image/jpeg"
        elif image_format == "PNG":
            content_type = "image/png"
        else:
            raise ValueError("unsupported thumbnail cache format")
        return ImagePayload(
            content=content,
            content_type=content_type,
            etag=f'"{mtime_ns:x}-{len(content):x}-{variant}"',
        )
    except (OSError, UnidentifiedImageError, ValueError):
        with suppress(OSError):
            path.unlink(missing_ok=True)
        return None


def _write_disk_payload(path: Path, content: bytes) -> bool:
    if not content or len(content) > _MAX_DISK_PAYLOAD_BYTES:
        return False
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_link_like(path.parent):
            return False
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read display-oriented dimensions without decoding the full preview."""

    try:
        with Image.open(path) as source:
            width, height = source.size
            orientation = int(source.getexif().get(274, 1) or 1)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ImageRegistryError("图片格式损坏或暂不支持预览。") from exc
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    if width < 1 or height < 1 or width * height > _MAX_PIXELS:
        raise ImageRegistryError("图片尺寸无效或像素数量过大。")
    return width, height


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _safe_cache_file(root: Path, candidate: Path) -> bool:
    if _is_link_like(candidate) or _is_link_like(candidate.parent):
        return False
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _image_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImageRegistryError("图片引用无效。")
    normalized = value.strip()
    if len(normalized) > 128 or any(character.isspace() for character in normalized):
        raise ImageRegistryError("图片引用无效。")
    return normalized


__all__ = [
    "ImageMetadata",
    "ImagePayload",
    "ImageRegistry",
    "ImageRegistryError",
    "ImageVariant",
]
