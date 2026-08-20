from __future__ import annotations

import os
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from image_vector_service.backend_server import _LibraryWorker
from image_vector_service.file_watcher import _disable_windows_last_access_notifications
from image_vector_service.library_config import LibraryDefinition


class _WatcherState:
    def __init__(self, *, registered: bool = False, pending_changes: int = 1) -> None:
        self.registered = registered
        self.pending_changes = pending_changes
        self.recovery_calls: list[str] = []

    def root_for_path(self, _path: str) -> dict[str, Any] | None:
        if not self.registered:
            return None
        return {"root_id": "root-1", "recursive": True}

    def root_path(self, root_id: str) -> str | None:
        return "D:\\images" if root_id == "root-1" else None

    def count_pending_changes(self, root_id: str) -> int:
        return self.pending_changes if root_id == "root-1" else 0

    def recover_interrupted_changes(self, root_id: str) -> dict[str, int]:
        self.recovery_calls.append(root_id)
        return {"changes": 0, "index_runs": 0}

    def enqueue_change(
        self,
        _root_id: str,
        _relative_path: str,
        _event_type: str,
    ) -> None:
        return None


class _WatcherConfig:
    watcher_debounce_seconds = 5.0


class _WatcherService:
    def __init__(
        self,
        *,
        registered_root: bool = False,
        pending_changes: int = 1,
    ) -> None:
        self.state = _WatcherState(
            registered=registered_root,
            pending_changes=pending_changes,
        )
        self.config = _WatcherConfig()
        self.invoked = threading.Event()
        self.completed = threading.Event()
        self.invocation_count = 0
        self.external_processing_confirmed: bool | None = None

    def index_and_auto_tag_incremental(
        self,
        *,
        folder_path: str,
        root_id: str,
        external_processing_confirmed: bool = False,
    ) -> dict[str, Any]:
        self.external_processing_confirmed = external_processing_confirmed
        self.invocation_count += 1
        self.invoked.set()
        if not external_processing_confirmed:
            raise RuntimeError(
                "External image processing must be explicitly confirmed."
            )
        self.state.pending_changes = max(0, self.state.pending_changes - 1)
        if self.state.pending_changes == 0:
            self.completed.set()
        return {"folder_path": folder_path, "root_id": root_id}

    def close(self) -> None:
        return None


class _RecordingWatcher:
    def __init__(self, **_kwargs: Any) -> None:
        self.roots: list[tuple[str, Path, bool]] = []
        self.pending_roots: list[str] = []

    def start(self, roots: list[tuple[str, Path, bool]]) -> None:
        self.roots = roots

    def schedule_pending(self, root_id: str) -> None:
        self.pending_roots.append(root_id)

    def stop(self) -> None:
        return None


class BackendAutoIndexWatcherTest(unittest.TestCase):
    @staticmethod
    def _worker(
        service: _WatcherService,
        *,
        check_cancel: Callable[[str], None] = lambda _job_id: None,
    ) -> _LibraryWorker:
        library = LibraryDefinition(
            library_id="library-1",
            name="Library",
            image_root=Path("D:\\images"),
            workspace=Path("D:\\workspace"),
            auto_index_enabled=True,
        )
        worker = _LibraryWorker(
            library,
            lambda _library, _progress, _cancel: service,
            lambda _job_id, _library, _message: None,
            check_cancel,
            queue_capacity=4,
            cooperative_batch_size=10,
        )
        return worker

    def test_settled_changes_authorize_the_enabled_automatic_pipeline(self) -> None:
        service = _WatcherService()
        worker = self._worker(service)

        worker.start()
        try:
            self.assertTrue(worker._ready.wait(timeout=2))
            self.assertTrue(worker.ready)

            worker._on_changes_settled("root-1")

            self.assertTrue(service.invoked.wait(timeout=2))
            self.assertTrue(service.external_processing_confirmed)
            self.assertEqual(service.state.pending_changes, 0)
        finally:
            worker.close()

    def test_settled_changes_do_not_require_a_registered_user_job(self) -> None:
        service = _WatcherService()

        def reject_unregistered_job(job_id: str) -> None:
            raise KeyError(job_id)

        worker = self._worker(service, check_cancel=reject_unregistered_job)

        worker.start()
        try:
            self.assertTrue(worker._ready.wait(timeout=2))
            self.assertTrue(worker.ready)

            worker._on_changes_settled("root-1")

            self.assertTrue(service.invoked.wait(timeout=2))
            self.assertEqual(service.state.pending_changes, 0)
        finally:
            worker.close()

    def test_startup_reschedules_persisted_pending_changes(self) -> None:
        service = _WatcherService(registered_root=True)
        worker = self._worker(service)

        with patch(
            "image_vector_service.file_watcher.FileChangeWatcher",
            _RecordingWatcher,
        ):
            worker.start()
            try:
                self.assertTrue(worker._ready.wait(timeout=2))
                self.assertTrue(worker.ready, worker.startup_error)
                self.assertEqual(service.state.recovery_calls, ["root-1"])
                self.assertEqual(worker._watcher.pending_roots, ["root-1"])
            finally:
                worker.close()

    def test_successful_auto_batches_continue_until_backlog_is_empty(self) -> None:
        service = _WatcherService(pending_changes=3)
        worker = self._worker(service)

        worker.start()
        try:
            self.assertTrue(worker._ready.wait(timeout=2))
            worker._on_changes_settled("root-1")

            self.assertTrue(service.completed.wait(timeout=2))
            self.assertEqual(service.invocation_count, 3)
            self.assertEqual(service.state.pending_changes, 0)
        finally:
            worker.close()

    @unittest.skipUnless(os.name == "nt", "requires watchdog Windows API")
    def test_windows_watcher_ignores_last_access_only_notifications(self) -> None:
        from watchdog.observers import winapi

        original = int(winapi.WATCHDOG_FILE_NOTIFY_FLAGS)
        try:
            winapi.WATCHDOG_FILE_NOTIFY_FLAGS = original | int(
                winapi.FILE_NOTIFY_CHANGE_LAST_ACCESS
            )

            changed = _disable_windows_last_access_notifications()

            self.assertTrue(changed)
            self.assertEqual(
                int(winapi.WATCHDOG_FILE_NOTIFY_FLAGS)
                & int(winapi.FILE_NOTIFY_CHANGE_LAST_ACCESS),
                0,
            )
        finally:
            winapi.WATCHDOG_FILE_NOTIFY_FLAGS = original


if __name__ == "__main__":
    unittest.main()
