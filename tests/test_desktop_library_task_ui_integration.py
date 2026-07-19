from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import Any, cast
from unittest import mock

from tests import test_desktop_search_ui_integration as search_fixture
from zvec_desktop.app import DesktopLaunchOptions
from zvec_desktop.backend_host import BackendHost, BackendRuntime
from zvec_desktop.credentials import CredentialStore
from zvec_desktop.library_tasks import (
    IndexRequest,
    LibraryRequest,
    LibraryTaskOutcome,
    LibraryTaskService,
    SubmittedLibraryTask,
)
from zvec_desktop.runtime_controller import BackendRuntimeController


class _LibraryTaskService:
    def __init__(self, behaviours: list[str | BaseException]) -> None:
        self.behaviours = deque(behaviours)
        self.requests: list[LibraryRequest] = []
        self.cancel_seen = threading.Event()
        self.release_blocked = threading.Event()
        self.two_waiters_started = threading.Event()
        self._active_waiters = 0
        self.peak_active_waiters = 0
        self._waiter_lock = threading.Lock()

    def submit(self, request: LibraryRequest) -> SubmittedLibraryTask:
        self.requests.append(request)
        job_id = f"library-job-{len(self.requests)}"
        return SubmittedLibraryTask(
            command=request.command,
            job_id=job_id,
            params=request.to_params(),
            submitted_job={
                "id": job_id,
                "command": request.command,
                "params": request.to_params(),
                "status": "queued",
                "progress": {"message": "Queued."},
            },
        )

    def wait(
        self,
        submission: SubmittedLibraryTask,
        **kwargs: Any,
    ) -> LibraryTaskOutcome:
        behaviour = self.behaviours.popleft()
        on_progress = kwargs.get("on_progress")
        cancel_event = cast(threading.Event, kwargs.get("cancel_event"))
        running = {
            "id": submission.job_id,
            "command": submission.command,
            "params": dict(submission.params),
            "status": "running",
            "progress": {
                "message": "正在处理图片",
                "current": 2,
                "total": 10,
                "failed": 1,
            },
            "failure_count": 1,
        }
        if callable(on_progress):
            on_progress(running)
        if isinstance(behaviour, BaseException):
            raise behaviour
        if behaviour == "block":
            with self._waiter_lock:
                self._active_waiters += 1
                self.peak_active_waiters = max(
                    self.peak_active_waiters,
                    self._active_waiters,
                )
                if self._active_waiters >= 2:
                    self.two_waiters_started.set()
            try:
                if not self.release_blocked.wait(timeout=5):
                    raise TimeoutError("blocked desktop task was not released")
            finally:
                with self._waiter_lock:
                    self._active_waiters -= 1
        if behaviour == "cancel":
            deadline = time.monotonic() + 5
            while not cancel_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not cancel_event.is_set():
                raise TimeoutError("cancel was not requested")
            self.cancel_seen.set()
            terminal = {
                **running,
                "status": "cancelled",
                "progress": {"message": "Cancelled."},
                "result": None,
                "error": None,
            }
            if callable(on_progress):
                on_progress(terminal)
            return LibraryTaskOutcome(submission, "cancelled", terminal, None, None)

        result = {
            "inserted": 8,
            "updated": 1,
            "failed": 1,
            "quarantined": 0,
        }
        terminal = {
            **running,
            "status": "partial",
            "progress": {"message": "Completed with item failures.", "failed": 1},
            "result": result,
            "error": None,
            "failure_count": 1,
        }
        if callable(on_progress):
            on_progress(terminal)
        return LibraryTaskOutcome(submission, "partial", terminal, result, None)


@unittest.skipUnless(os.name == "nt", "tkinter integration tests are Windows-only")
class DesktopLibraryTaskUiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path, _initial, _searched = (
            search_fixture.DesktopSearchUiIntegrationTest._create_fixture(self.root)
        )
        runtime_root = self.root / "runtime"
        runtime_root.mkdir()
        self.runtime = BackendRuntime(
            base_url="http://127.0.0.1:12345/",
            host="127.0.0.1",
            port=12345,
            pid=123,
            instance_id="tasks-test",
            config_fingerprint="fingerprint",
            runtime_directory=runtime_root,
            libraries_manifest=runtime_root / "libraries.json",
            query_root=runtime_root / "query",
        )

    def test_task_submission_progress_result_and_failure_recovery(self) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = search_fixture._RuntimeController(self.runtime)
        service = _LibraryTaskService(["partial", RuntimeError("task exploded")])
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(
                    Any, search_fixture._SearchService(RuntimeError("unused"))
                ),
                library_task_service_factory=lambda _runtime: cast(
                    LibraryTaskService, service
                ),
                credential_store=cast(
                    CredentialStore, search_fixture._CredentialStore(None)
                ),
            )
            self._pump_until(window, lambda: window._library_task_service is not None)
            window._notebook.select(1)
            window._manual_tags_var.set("本次新增, 写真")
            window.start_library_task("index")
            self._pump_until(
                window,
                lambda: (
                    (job := window._task_center.get("library-job-1")) is not None
                    and job.get("status") == "partial"
                ),
            )
            self.assertIsInstance(service.requests[0], IndexRequest)
            assert isinstance(service.requests[0], IndexRequest)
            self.assertEqual(service.requests[0].tags, ("本次新增", "写真"))
            selected = window._task_center.get("library-job-1")
            assert selected is not None
            self.assertEqual(selected["failure_count"], 1)
            self.assertIn("inserted", window._task_result.get("1.0", "end"))
            self.assertEqual(window._task_empty_state.winfo_manager(), "")
            self.assertEqual(
                tuple(window._task_tree.item("library-job-1", "tags")),
                ("attention",),
            )

            window.start_library_task("stats")
            self._pump_until(
                window,
                lambda: any(
                    job.get("status") == "failed"
                    and job.get("error", {}).get("message") == "task exploded"
                    for job in window._task_center.jobs
                ),
            )
            self.assertTrue(window.root.winfo_exists())
            self.assertEqual(str(window._task_action_buttons[0]["state"]), "normal")
            self.assertIn("软件仍可继续使用", window._status_text.get())
            self.assertEqual(set(controller.callback_threads), {threading.get_ident()})
        finally:
            if window is not None:
                search_fixture.DesktopSearchUiIntegrationTest._close_window(window)

    def test_owned_task_cancel_does_not_affect_other_tasks_or_close_window(
        self,
    ) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = search_fixture._RuntimeController(self.runtime)
        service = _LibraryTaskService(["cancel"])
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(
                    Any, search_fixture._SearchService(RuntimeError("unused"))
                ),
                library_task_service_factory=lambda _runtime: cast(
                    LibraryTaskService, service
                ),
                credential_store=cast(
                    CredentialStore, search_fixture._CredentialStore(None)
                ),
            )
            self._pump_until(window, lambda: window._library_task_service is not None)
            window.start_library_task("index")
            self._pump_until(
                window,
                lambda: "library-job-1" in window._active_task_cancels,
            )
            window._task_tree.selection_set("library-job-1")
            window.cancel_selected_task()
            self.assertIn("其他任务会继续运行", window._status_text.get())
            self.assertTrue(service.cancel_seen.wait(5))
            self._pump_until(
                window,
                lambda: (
                    (job := window._task_center.get("library-job-1")) is not None
                    and job.get("status") == "cancelled"
                ),
            )
            self.assertTrue(window.root.winfo_exists())
            self.assertIn("任务已取消", window._status_text.get())
        finally:
            if window is not None:
                search_fixture.DesktopSearchUiIntegrationTest._close_window(window)

    def test_two_task_watchers_run_concurrently_and_actions_remain_available(
        self,
    ) -> None:
        """The UI may queue more work while earlier backend jobs are active."""

        from zvec_desktop.ui import ZvecDesktopWindow

        controller = search_fixture._RuntimeController(self.runtime)
        service = _LibraryTaskService(["block", "block"])
        self.addCleanup(service.release_blocked.set)
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(
                    Any, search_fixture._SearchService(RuntimeError("unused"))
                ),
                library_task_service_factory=lambda _runtime: cast(
                    LibraryTaskService, service
                ),
                credential_store=cast(
                    CredentialStore, search_fixture._CredentialStore(None)
                ),
            )
            self._pump_until(window, lambda: window._library_task_service is not None)

            window.start_library_task("index")
            window.start_library_task("stats")

            self.assertTrue(service.two_waiters_started.wait(timeout=5))
            self.assertGreaterEqual(service.peak_active_waiters, 2)
            self.assertEqual(len(service.requests), 2)
            self.assertTrue(
                all(
                    str(button["state"]) == "normal"
                    for button in window._task_action_buttons
                )
            )
            self.assertTrue(window.root.winfo_exists())

            service.release_blocked.set()
            self._pump_until(
                window,
                lambda: all(
                    (job := window._task_center.get(job_id)) is not None
                    and job.get("status") == "partial"
                    for job_id in ("library-job-1", "library-job-2")
                ),
            )
        finally:
            service.release_blocked.set()
            if window is not None:
                search_fixture.DesktopSearchUiIntegrationTest._close_window(window)

    @staticmethod
    def _pump_until(window: Any, predicate: Any, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            window.root.update()
            time.sleep(0.01)
        if not predicate():
            raise AssertionError("desktop task condition did not complete in time")


if __name__ == "__main__":
    unittest.main()
