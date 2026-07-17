from __future__ import annotations

import errno
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, cast


def _open_binary(path: Path, flags: int, mode: str) -> BinaryIO:
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    try:
        return cast(BinaryIO, os.fdopen(descriptor, mode))
    except BaseException:
        os.close(descriptor)
        raise


class BackendInstanceLockError(RuntimeError):
    """Structured startup error raised when one config already owns a backend."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


class BackendInstanceLock:
    """Cross-process, cross-platform advisory lock for one backend config.

    The lock file is intentionally retained after release. Removing a lock file can
    race with a new owner and allow two processes to lock different inodes. The OS
    lock, rather than file existence or the informational JSON, is authoritative.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.metadata_path = Path(f"{self.path}.owner.json")
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, metadata: dict[str, Any]) -> None:
        if self._handle is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = _open_binary(
                self.path,
                os.O_RDWR | os.O_CREAT,
                "r+b",
            )
        except OSError as exc:
            raise BackendInstanceLockError(
                "instance_lock_io_error",
                "The backend instance lock could not be opened.",
                details={"lock_path": str(self.path), "reason": str(exc)},
            ) from exc

        try:
            # Windows byte locks are mandatory. Keep owner JSON in a sidecar so a
            # losing process can still report which instance owns the lock.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._lock_handle(handle)
        except OSError as exc:
            owner = self._read_metadata()
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BackendInstanceLockError(
                    "backend_already_running",
                    "Another backend already owns this configuration.",
                    details={
                        "lock_path": str(self.path),
                        **({"owner": owner} if owner else {}),
                    },
                ) from exc
            raise BackendInstanceLockError(
                "instance_lock_io_error",
                "The backend instance lock could not be acquired.",
                details={"lock_path": str(self.path), "reason": str(exc)},
            ) from exc

        try:
            encoded = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            metadata_handle = _open_binary(
                self.metadata_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                "wb",
            )
            with metadata_handle:
                metadata_handle.write(encoded)
                metadata_handle.flush()
                os.fsync(metadata_handle.fileno())
        except OSError as exc:
            with suppress(OSError):
                self._unlock_handle(handle)
            handle.close()
            raise BackendInstanceLockError(
                "instance_lock_io_error",
                "The backend instance lock metadata could not be written.",
                details={
                    "lock_path": str(self.path),
                    "metadata_path": str(self.metadata_path),
                    "reason": str(exc),
                },
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        with suppress(OSError):
            handle.seek(0)
            self._unlock_handle(handle)
        handle.close()

    def __enter__(self) -> BackendInstanceLock:
        if self._handle is None:
            raise RuntimeError("BackendInstanceLock.acquire() must be called first.")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def _read_metadata(self) -> dict[str, Any] | None:
        try:
            with self.metadata_path.open("rb") as handle:
                raw = handle.read(65_537)
            if len(raw) > 65_536:
                return None
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl = cast(Any, __import__("fcntl"))

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl = cast(Any, __import__("fcntl"))

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
