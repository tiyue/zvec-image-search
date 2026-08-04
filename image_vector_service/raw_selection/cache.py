"""Thread-safe persistent derived cache with guarded atomic commits."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DECODE_VERSION = "decode-v3"
_HEADER_PREFIX_BYTES = 16

CacheReadStatus = Literal["hit", "miss", "error"]
CacheWriteStatus = Literal["written", "stale", "error"]
EpochGuard = Callable[[object], AbstractContextManager[bool]]


@dataclass(frozen=True, slots=True)
class CacheReadResult:
    status: CacheReadStatus
    data: bytes | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CacheWriteResult:
    status: CacheWriteStatus
    bytes_written: int = 0
    error: str | None = None

    @property
    def written(self) -> bool:
        return self.status == "written"


@dataclass(frozen=True, slots=True)
class CacheClearResult:
    removed: int
    errors: tuple[str, ...] = ()


def _cache_key(
    normalized_path: str,
    file_size: int,
    mtime_ns: int,
    kind: str,
    *,
    file_identity: str | None = None,
    extra: str = "",
) -> str:
    digest = hashlib.sha256(
        (
            f"{normalized_path}\0{file_size}\0{mtime_ns}\0{file_identity or ''}"
            f"\0{_DECODE_VERSION}\0{kind}\0{extra}"
        ).encode()
    ).hexdigest()
    return digest


def _validated_kind(kind: str) -> str:
    if (
        not isinstance(kind, str)
        or not kind
        or len(kind) > 64
        or any(not (character.isalnum() or character in "_-") for character in kind)
    ):
        raise ValueError("cache kind must contain only letters, digits, '_' or '-'")
    return kind


def _header_values(
    normalized_path: str,
    file_size: int,
    mtime_ns: int,
    file_identity: str | None,
) -> dict[str, object]:
    return {
        "path": normalized_path,
        "size": file_size,
        "mtime_ns": mtime_ns,
        "file_identity": file_identity,
        "decode_version": _DECODE_VERSION,
    }


def _disk_error(exc: OSError) -> str:
    detail = exc.strerror or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {detail}"


class DerivedCache:
    """Directory-backed cache that never owns or modifies source images.

    Operations can run concurrently.  Clear operations exclude readers and
    writers without serializing unrelated cache writes.  An epoch-aware write
    creates and flushes its temporary file first, then holds the supplied
    generation guard across only the final ``os.replace``.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition(threading.RLock())
        self._active_operations = 0
        self._clearing = False

    def get(
        self,
        *,
        normalized_path: str,
        file_size: int,
        mtime_ns: int,
        kind: str,
        file_identity: str | None = None,
        extra: str = "",
    ) -> bytes | None:
        result = self.get_detailed(
            normalized_path=normalized_path,
            file_size=file_size,
            mtime_ns=mtime_ns,
            kind=kind,
            file_identity=file_identity,
            extra=extra,
        )
        return result.data if result.status == "hit" else None

    def get_detailed(
        self,
        *,
        normalized_path: str,
        file_size: int,
        mtime_ns: int,
        kind: str,
        file_identity: str | None = None,
        extra: str = "",
    ) -> CacheReadResult:
        selected_kind = _validated_kind(kind)
        key = _cache_key(
            normalized_path,
            file_size,
            mtime_ns,
            selected_kind,
            file_identity=file_identity,
            extra=extra,
        )
        path = self.root / selected_kind / f"{key}.dat"
        with self._operation():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return CacheReadResult("miss")
            except OSError as exc:
                return CacheReadResult("error", error=_disk_error(exc))
        decoded = _decode_entry(data)
        if decoded is None:
            return CacheReadResult("error", error="cache entry is invalid")
        header, payload = decoded
        expected = _header_values(
            normalized_path,
            file_size,
            mtime_ns,
            file_identity,
        )
        if any(header.get(field) != value for field, value in expected.items()):
            return CacheReadResult("miss")
        return CacheReadResult("hit", data=payload)

    def put(
        self,
        payload: bytes,
        *,
        normalized_path: str,
        file_size: int,
        mtime_ns: int,
        kind: str,
        file_identity: str | None = None,
        extra: str = "",
        epoch: object | None = None,
        epoch_guard: EpochGuard | None = None,
    ) -> CacheWriteResult:
        """Write one entry, optionally rejecting a stale generation atomically."""

        if not isinstance(payload, bytes):
            raise TypeError("cache payload must be bytes")
        if (epoch is None) != (epoch_guard is None):
            raise ValueError("epoch and epoch_guard must be supplied together")
        selected_kind = _validated_kind(kind)
        key = _cache_key(
            normalized_path,
            file_size,
            mtime_ns,
            selected_kind,
            file_identity=file_identity,
            extra=extra,
        )
        header = json.dumps(
            _header_values(
                normalized_path,
                file_size,
                mtime_ns,
                file_identity,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        blob = f"{len(header):0{_HEADER_PREFIX_BYTES}d}".encode() + header + payload
        kind_dir = self.root / selected_kind
        final = kind_dir / f"{key}.dat"
        temporary_path: Path | None = None
        with self._operation():
            try:
                kind_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=kind_dir,
                    delete=False,
                    suffix=".tmp",
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(blob)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                if epoch_guard is None:
                    os.replace(temporary_path, final)
                else:
                    with epoch_guard(epoch) as current:
                        if not current:
                            return CacheWriteResult("stale")
                        os.replace(temporary_path, final)
                temporary_path = None
                return CacheWriteResult("written", bytes_written=len(payload))
            except OSError as exc:
                return CacheWriteResult("error", error=_disk_error(exc))
            finally:
                if temporary_path is not None:
                    with suppress(OSError):
                        temporary_path.unlink(missing_ok=True)

    def clear(self) -> int:
        """Legacy count-only form; use ``clear_detailed`` for disk errors."""

        return self.clear_detailed().removed

    def clear_detailed(self) -> CacheClearResult:
        return self._clear_matching(lambda _header: True)

    def clear_source(
        self,
        normalized_path: str,
        *,
        file_identity: str | None = None,
    ) -> CacheClearResult:
        """Remove every derived entry for one source, optionally one identity."""

        def matches(header: dict[str, object]) -> bool:
            if header.get("path") != normalized_path:
                return False
            return file_identity is None or header.get("file_identity") == file_identity

        return self._clear_matching(matches)

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._condition:
            while self._clearing:
                self._condition.wait()
            self._active_operations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._condition.notify_all()

    def _clear_matching(
        self,
        predicate: Callable[[dict[str, object]], bool],
    ) -> CacheClearResult:
        removed = 0
        errors: list[str] = []
        with self._condition:
            self._clearing = True
            while self._active_operations > 0:
                self._condition.wait()
            try:
                try:
                    kind_directories = tuple(self.root.iterdir())
                except FileNotFoundError:
                    return CacheClearResult(0)
                except OSError as exc:
                    return CacheClearResult(0, (_disk_error(exc),))
                for kind_dir in kind_directories:
                    if not kind_dir.is_dir():
                        continue
                    try:
                        entries = tuple(kind_dir.iterdir())
                    except OSError as exc:
                        errors.append(_disk_error(exc))
                        continue
                    for entry in entries:
                        if entry.suffix.casefold() == ".tmp":
                            should_remove = True
                        elif entry.suffix.casefold() == ".dat":
                            try:
                                decoded = _decode_entry(entry.read_bytes())
                            except OSError as exc:
                                errors.append(_disk_error(exc))
                                continue
                            should_remove = decoded is not None and predicate(
                                decoded[0]
                            )
                        else:
                            continue
                        if not should_remove:
                            continue
                        try:
                            entry.unlink()
                            removed += 1
                        except OSError as exc:
                            errors.append(_disk_error(exc))
            finally:
                self._clearing = False
                self._condition.notify_all()
        return CacheClearResult(removed, tuple(errors))


def _decode_entry(data: bytes) -> tuple[dict[str, object], bytes] | None:
    if len(data) < _HEADER_PREFIX_BYTES:
        return None
    try:
        header_length = int(data[:_HEADER_PREFIX_BYTES])
    except ValueError:
        return None
    header_end = _HEADER_PREFIX_BYTES + header_length
    if header_length < 1 or len(data) < header_end:
        return None
    raw_header = data[_HEADER_PREFIX_BYTES:header_end]
    try:
        decoded = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Read legacy decode-v1 headers only for source-specific cleanup.  The
        # decode-version comparison below deliberately prevents cache hits.
        try:
            path, size, mtime_ns = raw_header.decode("utf-8").split("\0", 2)
            header: dict[str, object] = {
                "path": path,
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "file_identity": None,
                "decode_version": "decode-v1",
            }
        except (UnicodeDecodeError, ValueError):
            return None
        return header, data[header_end:]
    if not isinstance(decoded, dict):
        return None
    return decoded, data[header_end:]


__all__ = [
    "CacheClearResult",
    "CacheReadResult",
    "CacheWriteResult",
    "DerivedCache",
    "EpochGuard",
]
