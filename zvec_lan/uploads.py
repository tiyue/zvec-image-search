"""Bounded-memory storage for unbounded-size query-image uploads."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_UPLOAD_TTL_SECONDS = 24 * 60 * 60
DEFAULT_WRITE_CHUNK_BYTES = 256 * 1024
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
DEFAULT_SPACE_CHECK_INTERVAL_BYTES = 8 * 1024 * 1024
_SAFE_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{16,160}$")


class UploadError(RuntimeError):
    """Base class for safe upload errors."""

    code = "upload_error"


class InvalidUpload(UploadError):
    code = "invalid_upload"


class EmptyUpload(InvalidUpload):
    code = "empty_upload"


class UploadCancelled(UploadError):
    code = "upload_cancelled"


class UploadStorageError(UploadError):
    code = "upload_storage_error"


class QueryImageNotFound(UploadError):
    code = "query_image_not_found"


@dataclass(frozen=True, slots=True)
class QueryImage:
    """Internal handle for one completed temporary query image."""

    query_image_id: str
    owner_id: str
    path: Path
    display_name: str
    content_type: str | None
    size_bytes: int
    created_at: float
    last_accessed_at: float


class QueryImageStore:
    """Stream query-image chunks to disk without an artificial size cap."""

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        stale_after_seconds: float = DEFAULT_UPLOAD_TTL_SECONDS,
        write_chunk_bytes: int = DEFAULT_WRITE_CHUNK_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        space_check_interval_bytes: int = DEFAULT_SPACE_CHECK_INTERVAL_BYTES,
        free_space_provider: Callable[[Path], int] | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if isinstance(write_chunk_bytes, bool) or write_chunk_bytes < 1:
            raise ValueError("write_chunk_bytes must be positive")
        if isinstance(min_free_bytes, bool) or min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        if (
            isinstance(space_check_interval_bytes, bool)
            or space_check_interval_bytes < 1
        ):
            raise ValueError("space_check_interval_bytes must be positive")
        root = (
            Path(directory).expanduser()
            if directory is not None
            else Path(tempfile.gettempdir()) / "zvec-lan-query-images"
        )
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UploadStorageError("query-image storage is unavailable") from exc
        self._root = root
        self._stale_after = float(stale_after_seconds)
        self._write_chunk_bytes = write_chunk_bytes
        self._min_free_bytes = min_free_bytes
        self._space_check_interval_bytes = space_check_interval_bytes
        self._free_space_provider = free_space_provider or _free_space_bytes
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._images: dict[str, QueryImage] = {}
        self._active_parts: set[Path] = set()
        # Reservations contain only bytes that an active upload has not written
        # yet.  The shared lock makes the disk check and reservation atomic, so
        # concurrent Content-Length requests cannot all spend the same space.
        self._reservations: dict[Path, int] = {}
        self._generation = 0
        self._lock = threading.RLock()
        self._recover_stale_files()

    @property
    def directory(self) -> Path:
        return self._root

    def store(
        self,
        chunks: Iterable[bytes],
        *,
        owner_id: str,
        display_name: str,
        content_type: str | None,
        expected_size_bytes: int | None = None,
    ) -> QueryImage:
        """Persist bounded chunks and atomically publish a completed upload."""

        if expected_size_bytes is not None and (
            isinstance(expected_size_bytes, bool) or expected_size_bytes < 0
        ):
            raise ValueError("expected_size_bytes must be non-negative")

        self.cleanup_stale()
        query_image_id = self._new_id()
        partial_path = self._root / f"zvec-query-{query_image_id}.part"
        complete_path = self._root / f"zvec-query-{query_image_id}.bin"
        normalized_name = _safe_display_name(display_name)
        normalized_type = _safe_content_type(content_type)
        total = 0
        with self._lock:
            generation = self._generation
            self._active_parts.add(partial_path)
        try:
            try:
                if expected_size_bytes is not None:
                    self._reserve_exact(partial_path, expected_size_bytes)
                with partial_path.open("xb", buffering=0) as stream:
                    bytes_since_space_check = 0
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise InvalidUpload("upload chunks must be bytes")
                        if not chunk:
                            continue
                        view = memoryview(chunk)
                        for offset in range(0, len(view), self._write_chunk_bytes):
                            block = view[offset : offset + self._write_chunk_bytes]
                            self._require_generation(generation)
                            if expected_size_bytes is None:
                                self._reserve_rolling(partial_path, len(block))
                            elif total + len(block) > expected_size_bytes:
                                raise InvalidUpload(
                                    "upload exceeds its declared content length"
                                )
                            elif (
                                bytes_since_space_check + len(block)
                                > self._space_check_interval_bytes
                            ):
                                # A known-length upload already owns all of its
                                # future bytes.  Periodic rechecks only detect an
                                # unrelated process consuming the safety margin.
                                self._verify_space_budget()
                                bytes_since_space_check = 0
                            written = stream.write(block)
                            if written != len(block):
                                raise UploadStorageError(
                                    "query-image storage write was incomplete"
                                )
                            total += written
                            bytes_since_space_check += written
                            self._consume_reservation(partial_path, written)
                if total == 0:
                    raise EmptyUpload("query image is empty")
                if expected_size_bytes is not None and total != expected_size_bytes:
                    raise InvalidUpload("upload does not match its declared length")
                self._require_generation(generation)
                os.replace(partial_path, complete_path)
            except UploadError:
                raise
            except (BrokenPipeError, ConnectionError, EOFError) as exc:
                raise UploadCancelled("query-image upload was interrupted") from exc
            except OSError as exc:
                raise UploadStorageError("query image could not be stored") from exc
        except BaseException:
            with suppress(OSError):
                partial_path.unlink()
            with suppress(OSError):
                complete_path.unlink()
            raise
        finally:
            with self._lock:
                self._reservations.pop(partial_path, None)
                self._active_parts.discard(partial_path)
        now = self._clock()
        image = QueryImage(
            query_image_id=query_image_id,
            owner_id=owner_id,
            path=complete_path,
            display_name=normalized_name,
            content_type=normalized_type,
            size_bytes=total,
            created_at=now,
            last_accessed_at=now,
        )
        with self._lock:
            self._images[query_image_id] = image
        return image

    def get(self, query_image_id: str, *, owner_id: str) -> QueryImage:
        normalized_id = _validated_upload_id(query_image_id)
        now = self._clock()
        with self._lock:
            image = self._images.get(normalized_id)
            if image is None or image.owner_id != owner_id:
                raise QueryImageNotFound("query image was not found")
            if now - image.last_accessed_at >= self._stale_after:
                self._images.pop(normalized_id, None)
                expired = image
            else:
                refreshed = replace(image, last_accessed_at=now)
                self._images[normalized_id] = refreshed
                return refreshed
        with suppress(OSError):
            expired.path.unlink()
        raise QueryImageNotFound("query image was not found")

    def delete(self, query_image_id: str, *, owner_id: str) -> None:
        normalized_id = _validated_upload_id(query_image_id)
        with self._lock:
            image = self._images.get(normalized_id)
            if image is None or image.owner_id != owner_id:
                raise QueryImageNotFound("query image was not found")
            self._images.pop(normalized_id, None)
        try:
            image.path.unlink(missing_ok=True)
        except OSError as exc:
            raise UploadStorageError("query image could not be removed") from exc

    def delete_owner(self, owner_id: str) -> int:
        """Revoke all completed query images owned by one bearer session."""

        with self._lock:
            images = tuple(
                image for image in self._images.values() if image.owner_id == owner_id
            )
            for image in images:
                self._images.pop(image.query_image_id, None)
        for image in images:
            with suppress(OSError):
                image.path.unlink()
        return len(images)

    def cleanup_stale(self) -> int:
        """Remove expired registered images and recovered orphan files."""

        now = self._clock()
        with self._lock:
            expired = tuple(
                image
                for image in self._images.values()
                if now - image.last_accessed_at >= self._stale_after
            )
            for image in expired:
                self._images.pop(image.query_image_id, None)
        removed = 0
        for image in expired:
            with suppress(OSError):
                image.path.unlink()
                removed += 1
        cutoff = now - self._stale_after
        for pattern in ("zvec-query-*.part", "zvec-query-*.bin"):
            for candidate in self._root.glob(pattern):
                with self._lock:
                    if candidate in self._active_parts or any(
                        image.path == candidate for image in self._images.values()
                    ):
                        continue
                try:
                    if pattern.endswith(".part") or candidate.stat().st_mtime <= cutoff:
                        candidate.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed

    def close(self) -> None:
        """Cancel active writes and remove files owned by this process."""

        with self._lock:
            self._generation += 1
            images = tuple(self._images.values())
            self._images.clear()
            active_parts = tuple(self._active_parts)
            for partial_path in active_parts:
                self._reservations.pop(partial_path, None)
        for image in images:
            with suppress(OSError):
                image.path.unlink()
        # Unix can unlink an open file immediately; Windows will remove it in
        # the store() failure cleanup after the interrupted socket unblocks.
        for partial_path in active_parts:
            with suppress(OSError):
                partial_path.unlink()

    def _recover_stale_files(self) -> None:
        now = self._clock()
        cutoff = now - self._stale_after
        for partial in self._root.glob("zvec-query-*.part"):
            with suppress(OSError):
                partial.unlink()
        for complete in self._root.glob("zvec-query-*.bin"):
            try:
                if complete.stat().st_mtime <= cutoff:
                    complete.unlink()
            except OSError:
                continue

    def _new_id(self) -> str:
        for _attempt in range(32):
            candidate = _validated_upload_id(self._id_factory())
            with self._lock:
                collision = candidate in self._images
            if (
                not collision
                and not (self._root / f"zvec-query-{candidate}.bin").exists()
            ):
                return candidate
        raise UploadStorageError("query-image id could not be allocated")

    def _reserve_exact(self, partial_path: Path, size_bytes: int) -> None:
        if size_bytes == 0:
            return
        with self._lock:
            available = self._available_unreserved_bytes_locked()
            if available < size_bytes:
                raise UploadStorageError(
                    "query-image storage does not have sufficient free space"
                )
            self._reservations[partial_path] = size_bytes

    def _reserve_rolling(self, partial_path: Path, required_bytes: int) -> None:
        """Provide an unknown-length stream with a bounded rolling reservation."""

        with self._lock:
            current = self._reservations.get(partial_path, 0)
            if current >= required_bytes:
                return
            available = self._available_unreserved_bytes_locked()
            if available < required_bytes:
                raise UploadStorageError(
                    "query-image storage does not have sufficient free space"
                )
            desired = max(self._space_check_interval_bytes, required_bytes)
            granted = min(desired, available)
            self._reservations[partial_path] = current + granted

    def _consume_reservation(self, partial_path: Path, written: int) -> None:
        with self._lock:
            remaining = self._reservations.get(partial_path, 0) - written
            if remaining > 0:
                self._reservations[partial_path] = remaining
            else:
                self._reservations.pop(partial_path, None)

    def _verify_space_budget(self) -> None:
        with self._lock:
            if self._available_unreserved_bytes_locked() < 0:
                raise UploadStorageError(
                    "query-image storage safety margin is no longer available"
                )

    def _available_unreserved_bytes_locked(self) -> int:
        try:
            free_bytes = self._free_space_provider(self._root)
        except OSError as exc:
            raise UploadStorageError(
                "query-image free space could not be determined"
            ) from exc
        if isinstance(free_bytes, bool) or not isinstance(free_bytes, int):
            raise UploadStorageError("query-image free space is invalid")
        reserved = sum(self._reservations.values())
        return free_bytes - self._min_free_bytes - reserved

    def _require_generation(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                raise UploadCancelled("query-image upload was interrupted")


def _validated_upload_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_UPLOAD_ID.fullmatch(value) is None:
        raise QueryImageNotFound("query image was not found")
    return value


def _safe_display_name(value: str) -> str:
    if not isinstance(value, str):
        return "query-image"
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    leaf = "".join(
        character
        for character in leaf
        if ord(character) >= 32 and ord(character) != 127
    )
    return leaf[:255] or "query-image"


def _safe_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 128
        or "\r" in normalized
        or "\n" in normalized
    ):
        return None
    return normalized


def _free_space_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free
