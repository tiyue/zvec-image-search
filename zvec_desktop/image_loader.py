"""Bounded, cache-aware Pillow loading for the pure-Python desktop UI."""

from __future__ import annotations

import os
import stat
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import TypeAlias

from PIL import Image, ImageOps, UnidentifiedImageError

from .image_geometry import (
    ImageFitMode,
    ImageGeometryError,
    ImageSize,
    SizeLike,
    coerce_fit_mode,
    coerce_image_size,
    fit_image,
)

PathLike: TypeAlias = str | os.PathLike[str]

DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DECODE_PIXELS = 40_000_000
DEFAULT_MAX_RENDER_PIXELS = 16_777_216
DEFAULT_MAX_RENDER_DIMENSION = 16_384
DEFAULT_CACHE_ITEMS = 16
DEFAULT_CACHE_PIXELS = 24_000_000


class ImageLoadErrorCode(str, Enum):
    """Stable machine-readable failure categories for UI presentation."""

    INVALID_PATH = "invalid_path"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    PERMISSION_DENIED = "permission_denied"
    FILE_TOO_LARGE = "file_too_large"
    PIXEL_LIMIT_EXCEEDED = "pixel_limit_exceeded"
    INVALID_IMAGE_SIZE = "invalid_image_size"
    INVALID_TARGET_SIZE = "invalid_target_size"
    INVALID_FIT_MODE = "invalid_fit_mode"
    DECODE_FAILED = "decode_failed"
    RENDER_FAILED = "render_failed"
    FILE_CHANGED = "file_changed"
    RESOURCE_LIMIT = "resource_limit"
    LOADER_CLOSED = "loader_closed"


@dataclass(frozen=True, slots=True)
class ImageLoadError:
    """A display-safe error; low-level exceptions remain available by type only."""

    code: ImageLoadErrorCode
    message: str
    path: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class ImageLoadResult:
    """The non-throwing result returned to the UI for every image request."""

    image: Image.Image | None
    error: ImageLoadError | None
    path: Path | None = None
    source_size: SizeLike | None = None
    oriented_size: SizeLike | None = None
    image_format: str | None = None
    exif_orientation: int = 1
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.image is not None and self.error is None


@dataclass(frozen=True, slots=True)
class ImageCacheInfo:
    hits: int
    misses: int
    entries: int
    pixels: int
    evictions: int


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    path: Path
    size: int
    modified_ns: int

    @property
    def key(self) -> tuple[str, int, int]:
        return str(self.path), self.size, self.modified_ns


@dataclass(slots=True)
class _DecodedImage:
    image: Image.Image
    source_size: SizeLike
    oriented_size: SizeLike
    image_format: str | None
    exif_orientation: int

    @property
    def pixels(self) -> int:
        return self.image.width * self.image.height

    def independent_copy(self) -> _DecodedImage:
        image = self.image.copy()
        image.load()
        return _DecodedImage(
            image=image,
            source_size=self.source_size,
            oriented_size=self.oriented_size,
            image_format=self.image_format,
            exif_orientation=self.exif_orientation,
        )


class _ThreadSafeImageLRU:
    """A pixel- and entry-bounded LRU whose values never escape by reference."""

    def __init__(self, max_items: int, max_pixels: int):
        self._max_items = max_items
        self._max_pixels = max_pixels
        self._entries: OrderedDict[tuple[str, int, int], _DecodedImage] = OrderedDict()
        self._pixels = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = threading.RLock()

    def get(self, key: tuple[str, int, int]) -> _DecodedImage | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value.independent_copy()

    def put(self, key: tuple[str, int, int], value: _DecodedImage) -> None:
        if self._max_items == 0 or value.pixels > self._max_pixels:
            return
        stored = value.independent_copy()
        with self._lock:
            # A changed file gets a new fingerprint. Remove older versions of
            # the same path immediately rather than waiting for normal eviction.
            path_key = key[0]
            for stale_key in tuple(self._entries):
                if stale_key[0] == path_key and stale_key != key:
                    self._remove(stale_key)
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._pixels -= previous.pixels
                previous.image.close()
            self._entries[key] = stored
            self._pixels += stored.pixels
            while (
                len(self._entries) > self._max_items or self._pixels > self._max_pixels
            ):
                oldest_key = next(iter(self._entries))
                self._remove(oldest_key)
                self._evictions += 1

    def _remove(self, key: tuple[str, int, int]) -> None:
        value = self._entries.pop(key)
        self._pixels -= value.pixels
        value.image.close()

    def clear(self) -> None:
        with self._lock:
            values = tuple(self._entries.values())
            self._entries.clear()
            self._pixels = 0
        for value in values:
            value.image.close()

    def info(self) -> ImageCacheInfo:
        with self._lock:
            return ImageCacheInfo(
                hits=self._hits,
                misses=self._misses,
                entries=len(self._entries),
                pixels=self._pixels,
                evictions=self._evictions,
            )


def _failure(
    code: ImageLoadErrorCode,
    message: str,
    *,
    path: Path | str | None = None,
    exception: BaseException | None = None,
) -> ImageLoadResult:
    return ImageLoadResult(
        image=None,
        error=ImageLoadError(
            code=code,
            message=message,
            path=str(path) if path is not None else None,
            exception_type=type(exception).__name__ if exception is not None else None,
        ),
        path=path if isinstance(path, Path) else None,
    )


class BoundedImageLoader:
    """Synchronously load and fit images with bounded resource consumption.

    The class is safe to call from multiple worker threads.  It returns a new
    fitted Pillow image on every successful call; callers may therefore close
    their result without corrupting the cache or another view.
    """

    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_decode_pixels: int = DEFAULT_MAX_DECODE_PIXELS,
        max_render_pixels: int = DEFAULT_MAX_RENDER_PIXELS,
        max_render_dimension: int = DEFAULT_MAX_RENDER_DIMENSION,
        cache_items: int = DEFAULT_CACHE_ITEMS,
        max_cache_pixels: int = DEFAULT_CACHE_PIXELS,
    ):
        positive = {
            "max_file_bytes": max_file_bytes,
            "max_decode_pixels": max_decode_pixels,
            "max_render_pixels": max_render_pixels,
            "max_render_dimension": max_render_dimension,
            "max_cache_pixels": max_cache_pixels,
        }
        for name, value in positive.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(cache_items, bool) or not isinstance(cache_items, int):
            raise ValueError("cache_items must be a non-negative integer")
        if cache_items < 0:
            raise ValueError("cache_items must be a non-negative integer")

        self._max_file_bytes = max_file_bytes
        self._max_decode_pixels = max_decode_pixels
        self._max_render_pixels = max_render_pixels
        self._max_render_dimension = max_render_dimension
        self._cache = _ThreadSafeImageLRU(cache_items, max_cache_pixels)
        self._state_lock = threading.Lock()
        self._closed = False

    @property
    def cache_info(self) -> ImageCacheInfo:
        return self._cache.info()

    def clear_cache(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._cache.clear()

    def __enter__(self) -> BoundedImageLoader:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def load(
        self,
        path: PathLike,
        target_size: ImageSize | SizeLike,
        mode: ImageFitMode | str = ImageFitMode.CONTAIN,
    ) -> ImageLoadResult:
        """Load, orient and fit one image without propagating file errors."""

        with self._state_lock:
            if self._closed:
                return _failure(
                    ImageLoadErrorCode.LOADER_CLOSED,
                    "image loader is closed",
                    path=str(path),
                )

        target_or_error = self._validate_target(target_size)
        if isinstance(target_or_error, ImageLoadResult):
            return target_or_error
        target = target_or_error
        try:
            fit_mode = coerce_fit_mode(mode)
        except ImageGeometryError as exc:
            return _failure(
                ImageLoadErrorCode.INVALID_FIT_MODE,
                str(exc),
                path=str(path),
                exception=exc,
            )

        normalized_or_error = self._normalize_path(path)
        if isinstance(normalized_or_error, ImageLoadResult):
            return normalized_or_error
        normalized = normalized_or_error

        # Retry once when a file is replaced while Pillow is decoding it. This
        # is common when an indexer writes thumbnails atomically in parallel.
        for attempt in range(2):
            fingerprint_or_error = self._fingerprint(normalized)
            if isinstance(fingerprint_or_error, ImageLoadResult):
                return fingerprint_or_error
            fingerprint = fingerprint_or_error

            try:
                decoded = self._cache.get(fingerprint.key)
            except MemoryError as exc:
                self._cache.clear()
                return _failure(
                    ImageLoadErrorCode.RESOURCE_LIMIT,
                    "not enough memory to copy the cached image",
                    path=normalized,
                    exception=exc,
                )
            except Exception:
                # Cache corruption or a failed Pillow copy must not make the
                # source image unavailable. Drop the cache and decode normally.
                self._cache.clear()
                decoded = None
            from_cache = decoded is not None
            if decoded is None:
                decoded_or_error = self._decode(normalized)
                if isinstance(decoded_or_error, ImageLoadResult):
                    return decoded_or_error
                decoded = decoded_or_error

            current_or_error = self._fingerprint(normalized)
            if isinstance(current_or_error, ImageLoadResult):
                decoded.image.close()
                return current_or_error
            if current_or_error.key != fingerprint.key:
                decoded.image.close()
                if attempt == 0:
                    continue
                return _failure(
                    ImageLoadErrorCode.FILE_CHANGED,
                    "image changed repeatedly while it was being decoded",
                    path=normalized,
                )

            if not from_cache:
                # Coordinate insertion with close(): either close clears this
                # entry afterwards, or a closed loader skips insertion entirely.
                with self._state_lock:
                    can_cache = not self._closed
                    if can_cache:
                        try:
                            self._cache.put(fingerprint.key, decoded)
                        except Exception:
                            # Caching is an optimization. A memory allocation or
                            # copy failure must not discard a decoded image that
                            # can still be rendered for the current request.
                            self._cache.clear()
            try:
                rendered = fit_image(decoded.image, target, fit_mode)
            except MemoryError as exc:
                return _failure(
                    ImageLoadErrorCode.RESOURCE_LIMIT,
                    "not enough memory to render the image",
                    path=normalized,
                    exception=exc,
                )
            except (ImageGeometryError, OSError, ValueError) as exc:
                return _failure(
                    ImageLoadErrorCode.RENDER_FAILED,
                    "image could not be rendered",
                    path=normalized,
                    exception=exc,
                )
            finally:
                decoded.image.close()

            return ImageLoadResult(
                image=rendered,
                error=None,
                path=normalized,
                source_size=decoded.source_size,
                oriented_size=decoded.oriented_size,
                image_format=decoded.image_format,
                exif_orientation=decoded.exif_orientation,
                from_cache=from_cache,
            )

        return _failure(
            ImageLoadErrorCode.FILE_CHANGED,
            "image changed while it was being decoded",
            path=normalized,
        )

    def _validate_target(
        self, target_size: ImageSize | SizeLike
    ) -> ImageSize | ImageLoadResult:
        try:
            target = coerce_image_size(target_size, label="target size")
        except ImageGeometryError as exc:
            return _failure(
                ImageLoadErrorCode.INVALID_TARGET_SIZE,
                str(exc),
                exception=exc,
            )
        if (
            target.width > self._max_render_dimension
            or target.height > self._max_render_dimension
            or target.pixels > self._max_render_pixels
        ):
            return _failure(
                ImageLoadErrorCode.INVALID_TARGET_SIZE,
                "target size exceeds the configured render limit",
            )
        return target

    @staticmethod
    def _normalize_path(path: PathLike) -> Path | ImageLoadResult:
        if isinstance(path, str) and not path.strip():
            return _failure(
                ImageLoadErrorCode.INVALID_PATH, "image path must not be empty"
            )
        try:
            candidate = Path(path).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            return _failure(
                ImageLoadErrorCode.NOT_FOUND,
                "image file does not exist",
                path=str(path),
                exception=exc,
            )
        except PermissionError as exc:
            return _failure(
                ImageLoadErrorCode.PERMISSION_DENIED,
                "image file cannot be accessed",
                path=str(path),
                exception=exc,
            )
        except (OSError, TypeError, ValueError) as exc:
            return _failure(
                ImageLoadErrorCode.INVALID_PATH,
                "image path is invalid",
                path=str(path),
                exception=exc,
            )
        return candidate

    def _fingerprint(self, path: Path) -> _FileFingerprint | ImageLoadResult:
        try:
            details = path.stat()
        except FileNotFoundError as exc:
            return _failure(
                ImageLoadErrorCode.NOT_FOUND,
                "image file no longer exists",
                path=path,
                exception=exc,
            )
        except PermissionError as exc:
            return _failure(
                ImageLoadErrorCode.PERMISSION_DENIED,
                "image file cannot be accessed",
                path=path,
                exception=exc,
            )
        except OSError as exc:
            return _failure(
                ImageLoadErrorCode.INVALID_PATH,
                "image file metadata could not be read",
                path=path,
                exception=exc,
            )
        if not stat.S_ISREG(details.st_mode):
            return _failure(
                ImageLoadErrorCode.NOT_A_FILE,
                "image path is not a regular file",
                path=path,
            )
        if details.st_size <= 0:
            return _failure(
                ImageLoadErrorCode.DECODE_FAILED,
                "image file is empty",
                path=path,
            )
        if details.st_size > self._max_file_bytes:
            return _failure(
                ImageLoadErrorCode.FILE_TOO_LARGE,
                "image file exceeds the configured byte limit",
                path=path,
            )
        return _FileFingerprint(path, details.st_size, details.st_mtime_ns)

    def _decode(self, path: Path) -> _DecodedImage | ImageLoadResult:
        oriented: Image.Image | None = None
        try:
            # Do not mutate Python's process-global warning filters here: loads
            # run concurrently. Pillow's own bomb exception remains active and
            # the stricter per-loader pixel check happens before pixel decoding.
            with Image.open(path) as source:
                source_size = source.size
                width, height = source_size
                if width <= 0 or height <= 0:
                    return _failure(
                        ImageLoadErrorCode.INVALID_IMAGE_SIZE,
                        "image reports an empty size",
                        path=path,
                    )
                if width * height > self._max_decode_pixels:
                    return _failure(
                        ImageLoadErrorCode.PIXEL_LIMIT_EXCEEDED,
                        "image exceeds the configured decode pixel limit",
                        path=path,
                    )
                image_format = source.format
                raw_orientation = source.getexif().get(274, 1)
                try:
                    exif_orientation = int(raw_orientation or 1)
                except (TypeError, ValueError):
                    exif_orientation = 1
                if exif_orientation not in range(1, 9):
                    exif_orientation = 1

                oriented = ImageOps.exif_transpose(source)
                oriented.load()
                if oriented.width * oriented.height > self._max_decode_pixels:
                    return _failure(
                        ImageLoadErrorCode.PIXEL_LIMIT_EXCEEDED,
                        "oriented image exceeds the decode pixel limit",
                        path=path,
                    )
                # Pillow's ``exif_transpose`` returns an independent image,
                # including when no rotation is required. Transfer ownership
                # instead of making a second full-resolution copy; this cuts
                # the peak memory of large previews substantially. Keep the
                # defensive copy for older/custom Pillow implementations that
                # may return the source object itself.
                if oriented is source:
                    detached = source.copy()
                    detached.load()
                else:
                    detached = oriented
                    oriented = None
                return _DecodedImage(
                    image=detached,
                    source_size=source_size,
                    oriented_size=detached.size,
                    image_format=image_format,
                    exif_orientation=exif_orientation,
                )
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            return _failure(
                ImageLoadErrorCode.PIXEL_LIMIT_EXCEEDED,
                "image exceeds Pillow's safe decode limit",
                path=path,
                exception=exc,
            )
        except PermissionError as exc:
            return _failure(
                ImageLoadErrorCode.PERMISSION_DENIED,
                "image file cannot be read",
                path=path,
                exception=exc,
            )
        except MemoryError as exc:
            return _failure(
                ImageLoadErrorCode.RESOURCE_LIMIT,
                "not enough memory to decode the image",
                path=path,
                exception=exc,
            )
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            return _failure(
                ImageLoadErrorCode.DECODE_FAILED,
                "file is not a supported or valid image",
                path=path,
                exception=exc,
            )
        except Exception as exc:  # Defensive UI boundary; never catch cancellation.
            return _failure(
                ImageLoadErrorCode.DECODE_FAILED,
                "image decoding failed unexpectedly",
                path=path,
                exception=exc,
            )
        finally:
            if oriented is not None:
                oriented.close()


class AsyncImageLoader:
    """Small executor wrapper suitable for responsive desktop view models."""

    def __init__(
        self,
        loader: BoundedImageLoader | None = None,
        *,
        max_workers: int = 4,
    ):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise ValueError("max_workers must be a positive integer")
        if max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        self._loader = loader or BoundedImageLoader()
        self._owns_loader = loader is None
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="zvec-image"
        )
        self._state_lock = threading.Lock()
        self._closed = False

    @property
    def loader(self) -> BoundedImageLoader:
        return self._loader

    def load(
        self,
        path: PathLike,
        target_size: ImageSize | SizeLike,
        mode: ImageFitMode | str = ImageFitMode.CONTAIN,
    ) -> Future[ImageLoadResult]:
        """Schedule one load; completion callbacks run on a worker thread."""

        with self._state_lock:
            if self._closed:
                completed: Future[ImageLoadResult] = Future()
                completed.set_result(
                    _failure(
                        ImageLoadErrorCode.LOADER_CLOSED,
                        "asynchronous image loader is closed",
                        path=str(path),
                    )
                )
                return completed
            return self._executor.submit(self._loader.load, path, target_size, mode)

    # Explicit spelling reads naturally at call sites while ``load`` keeps the
    # API compact. Both return the same Future object.
    load_async = load

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if self._owns_loader:
            self._loader.close()

    close = shutdown

    def __enter__(self) -> AsyncImageLoader:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.shutdown()
