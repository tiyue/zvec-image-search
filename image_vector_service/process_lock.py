from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from .config import ConfigurationError


class ProcessLock:
    """A non-blocking cross-process lock for one local Collection."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except OSError as exc:
            handle.close()
            raise ConfigurationError(
                "Another image service process is already using this workspace."
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()
