"""Persistent derived-image cache for the ARW selection module.

Implements requirement 8.4: cache keys include the normalized source
path, file size, ``mtime_ns`` and a decode-version tag; writes are
atomic (temp file + ``os.replace``); cache entries whose source version
no longer matches are treated as misses and regenerated.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

_DECODE_VERSION = "decode-v1"


def _cache_key(
    normalized_path: str,
    file_size: int,
    mtime_ns: int,
    kind: str,
    *,
    extra: str = "",
) -> str:
    digest = hashlib.sha256(
        f"{normalized_path}\0{file_size}\0{mtime_ns}\0{_DECODE_VERSION}"
        f"\0{kind}\0{extra}".encode()
    ).hexdigest()
    return digest


class DerivedCache:
    """Directory-backed cache for derived image bytes.

    Entries are plain files named by a content-derived hash under
    ``root/<kind>/``. Writes are atomic; reads verify freshness by
    comparing the caller-supplied source version against the stored
    metadata line, so a changed or replaced source invalidates old
    entries automatically.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get(
        self,
        *,
        normalized_path: str,
        file_size: int,
        mtime_ns: int,
        kind: str,
        extra: str = "",
    ) -> bytes | None:
        """Return cached bytes if a fresh entry exists, else None."""
        key = _cache_key(normalized_path, file_size, mtime_ns, kind, extra=extra)
        path = self.root / kind / f"{key}.dat"
        try:
            data = path.read_bytes()
        except OSError:
            return None
        # Format: 16-byte ASCII length prefix + header + payload.
        if len(data) < 16:
            return None
        try:
            header_len = int(data[:16])
        except ValueError:
            return None
        header_end = 16 + header_len
        if len(data) < header_end:
            return None
        header = data[16:header_end].decode("utf-8", errors="replace")
        expected = f"{normalized_path}\0{file_size}\0{mtime_ns}"
        if header != expected:
            return None
        return data[header_end:]

    def put(
        self,
        payload: bytes,
        *,
        normalized_path: str,
        file_size: int,
        mtime_ns: int,
        kind: str,
        extra: str = "",
    ) -> None:
        """Atomically store payload under the versioned cache key."""
        key = _cache_key(normalized_path, file_size, mtime_ns, kind, extra=extra)
        kind_dir = self.root / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        final = kind_dir / f"{key}.dat"
        header = f"{normalized_path}\0{file_size}\0{mtime_ns}".encode()
        blob = f"{len(header):016d}".encode() + header + payload
        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=kind_dir, delete=False, suffix=".tmp"
            ) as tmp:
                tmp_name = tmp.name
                tmp.write(blob)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, final)
        except OSError:
            # Space shortage or permission failure: stop creating this
            # cache entry without touching other entries.
            if tmp_name is not None:
                with suppress(OSError):
                    os.unlink(tmp_name)

    def clear(self) -> int:
        """Delete all cache entries. Returns the number removed."""
        removed = 0
        with self._lock:
            for kind_dir in self.root.iterdir():
                if not kind_dir.is_dir():
                    continue
                for entry in kind_dir.iterdir():
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError:
                        continue
        return removed
