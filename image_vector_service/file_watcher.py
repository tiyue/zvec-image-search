"""Watchdog-based file system watcher for automatic incremental indexing.

The watcher monitors indexed library roots and persists file change events
to the ``fs_change_queue`` table in :class:`IndexState`.  After a configurable
debounce period with no new events for a given root, the watcher fires a
callback so the backend can submit an incremental index+auto-tag job.

Events for unsupported file extensions are silently ignored.  Repeated events
for the same file are de-duplicated by ``INSERT … ON CONFLICT`` in the state
layer, keeping only the latest event type and timestamp.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .image_scanner import SUPPORTED_EXTENSIONS

_logger = logging.getLogger(__name__)


class FileChangeWatcher:
    """Watch indexed roots, persist change events, debounce triggers."""

    def __init__(
        self,
        *,
        debounce_seconds: float,
        on_changes_settled: Callable[[str], None],
        enqueue_change: Callable[[str, str, str], None],
    ) -> None:
        self._debounce_seconds = max(0.5, debounce_seconds)
        self._on_changes_settled = on_changes_settled
        self._enqueue_change = enqueue_change
        self._observer: Observer | None = None
        self._timers: dict[str, threading.Timer] = {}
        self._timers_lock = threading.Lock()
        self._overflowed = False
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def start(self, roots: list[tuple[str, Path, bool]]) -> None:
        """Start watching *roots*.

        Each tuple is ``(root_id, image_root, recursive)``.
        """
        if self._running:
            return
        self._overflowed = False
        self._observer = Observer(timeout=1.0)
        for root_id, image_root, recursive in roots:
            handler = _ChangeEventHandler(
                root_id=root_id,
                root_path=image_root,
                enqueue_change=self._enqueue_change,
                watcher=self,
            )
            self._observer.schedule(
                handler,
                str(image_root),
                recursive=recursive,
            )
            _logger.info(
                "watcher scheduled root_id=%s path=%s recursive=%s",
                root_id,
                image_root,
                recursive,
            )
        self._observer.start()
        self._running = True

    def schedule_pending(self, root_id: str) -> None:
        """Debounce a persisted change queue after watcher startup."""

        if self._running:
            self._schedule_debounce(root_id)

    def stop(self) -> None:
        self._running = False
        with self._timers_lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _schedule_debounce(self, root_id: str) -> None:
        with self._timers_lock:
            existing = self._timers.pop(root_id, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                self._debounce_seconds,
                self._fire_settled,
                args=(root_id,),
            )
            timer.daemon = True
            self._timers[root_id] = timer
            timer.start()

    def _fire_settled(self, root_id: str) -> None:
        with self._timers_lock:
            self._timers.pop(root_id, None)
        if not self._running:
            return
        try:
            self._on_changes_settled(root_id)
        except Exception:
            _logger.exception(
                "watcher on_changes_settled callback failed for root %s",
                root_id,
            )

    def _mark_overflow(self) -> None:
        self._overflowed = True
        _logger.warning(
            "watcher buffer overflow detected; a full scan fallback may be needed"
        )


class _ChangeEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        *,
        root_id: str,
        root_path: Path,
        enqueue_change: Callable[[str, str, str], None],
        watcher: FileChangeWatcher,
    ) -> None:
        super().__init__()
        self._root_id = root_id
        self._root_path = root_path
        self._enqueue_change = enqueue_change
        self._watcher = watcher

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, "created")

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, "modified")

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, "deleted")

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.dest_path, "created")
        self._enqueue(event.src_path, "deleted")

    def _enqueue(self, path_str: str, event_type: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        try:
            resolved = path.resolve()
            relative = str(resolved.relative_to(self._root_path.resolve()))
        except (OSError, ValueError):
            return
        try:
            self._enqueue_change(self._root_id, relative, event_type)
        except Exception:
            _logger.debug(
                "watcher enqueue_change failed for %s (%s)",
                relative,
                event_type,
                exc_info=True,
            )
        self._watcher._schedule_debounce(self._root_id)
