from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from collections import deque
from concurrent.futures import Future
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest import mock

from PIL import Image

from zvec_desktop.app import DesktopLaunchOptions
from zvec_desktop.backend_host import BackendHost, BackendRuntime
from zvec_desktop.credentials import CredentialStore
from zvec_desktop.runtime_controller import (
    BackendRuntimeController,
    RuntimeErrorCategory,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeOperationError,
    RuntimeState,
)
from zvec_desktop.search_service import (
    SearchOutcome,
    SearchRequest,
    SubmittedSearch,
)


def _completed(value: Any = None, error: BaseException | None = None) -> Future[Any]:
    future: Future[Any] = Future()
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)
    return future


class _CredentialStore:
    persistent = True

    def __init__(self, secret: str | None = "saved-key") -> None:
        self.secret = secret
        self.saved: list[str] = []

    def has_secret(self) -> bool:
        return self.secret is not None

    def read_secret(self) -> str | None:
        return self.secret

    def save_secret(self, secret: str) -> None:
        self.saved.append(secret)
        self.secret = secret

    def delete_secret(self) -> None:
        self.secret = None


class _RuntimeController:
    def __init__(self, runtime: BackendRuntime) -> None:
        self.runtime = runtime
        self.state = RuntimeState.STOPPED
        self.events: deque[RuntimeEvent] = deque()
        self.credentials: list[str] = []
        self.busy = False
        self.stop_calls = 0
        self.start_calls = 0
        self.defer_next_start = False
        self.deferred_start: Future[BackendRuntime] | None = None
        self.callback_threads: list[int] = []

    @property
    def is_ready(self) -> bool:
        return self.state is RuntimeState.READY

    def start(self) -> Future[BackendRuntime]:
        self.start_calls += 1
        if self.defer_next_start:
            self.defer_next_start = False
            self.state = RuntimeState.STARTING
            self.deferred_start = Future()
            return self.deferred_start
        self.state = RuntimeState.READY
        now = datetime.now(timezone.utc)
        self.events.extend(
            (
                RuntimeEvent(
                    RuntimeEventKind.STATE_CHANGED,
                    RuntimeState.READY,
                    "start",
                    now,
                ),
                RuntimeEvent(
                    RuntimeEventKind.RUNTIME_READY,
                    RuntimeState.READY,
                    "start",
                    now,
                    runtime=self.runtime,
                ),
            )
        )
        return cast(Future[BackendRuntime], _completed(self.runtime))

    def configure_credentials(
        self, dash_scope_api_key: str, _api_url: str | None = None
    ) -> Future[dict[str, Any]]:
        self.credentials.append(dash_scope_api_key)
        return cast(
            Future[dict[str, Any]],
            _completed({"credentials_configured": True}),
        )

    def list_jobs(
        self,
        *,
        active: bool | None = None,
        limit: int = 100,
    ) -> Future[dict[str, Any]]:
        del active, limit
        now = datetime.now(timezone.utc)
        self.events.append(
            RuntimeEvent(
                RuntimeEventKind.JOBS_LISTED,
                self.state,
                "list_jobs",
                now,
                jobs=(),
                payload={"jobs": [], "count": 0},
            )
        )
        return cast(Future[dict[str, Any]], _completed({"jobs": [], "count": 0}))

    def cancel_job(self, job_id: str) -> Future[dict[str, Any]]:
        return cast(
            Future[dict[str, Any]],
            _completed({"id": job_id, "command": "index", "status": "cancelling"}),
        )

    def stop(self, *, force: bool = False) -> Future[None]:
        del force
        self.stop_calls += 1
        if self.busy:
            info = RuntimeErrorInfo(
                operation="stop",
                category=RuntimeErrorCategory.BUSY,
                message="backend busy",
                recoverable=True,
                exception_type="BackendBusyError",
                details={"active_job_ids": ["index-1"]},
            )
            return cast(Future[None], _completed(error=RuntimeOperationError(info)))
        self.state = RuntimeState.STOPPED
        return cast(Future[None], _completed(None))

    def drain_events(
        self,
        callback: Any = None,
        *,
        max_events: int = 200,
    ) -> tuple[RuntimeEvent, ...]:
        drained: list[RuntimeEvent] = []
        for _ in range(min(max_events, len(self.events))):
            event = self.events.popleft()
            drained.append(event)
            if callback is not None:
                self.callback_threads.append(threading.get_ident())
                callback(event)
        return tuple(drained)

    def dispose(self, *, force: bool = False) -> Future[None]:
        del force
        if self.busy:
            info = RuntimeErrorInfo(
                operation="stop",
                category=RuntimeErrorCategory.BUSY,
                message="backend busy",
                recoverable=True,
                exception_type="BackendBusyError",
                details={"active_job_ids": ["search-1"]},
            )
            return cast(Future[None], _completed(error=RuntimeOperationError(info)))
        self.state = RuntimeState.DISPOSED
        return cast(Future[None], _completed(None))


class _SearchService:
    def __init__(self, outcome: SearchOutcome | BaseException) -> None:
        self.outcome = outcome
        self.requests: list[SearchRequest] = []

    def submit(self, request: SearchRequest) -> SubmittedSearch:
        self.requests.append(request)
        return SubmittedSearch("search-1", "text", None, {"id": "search-1"})

    def wait(self, submission: SubmittedSearch, **kwargs: Any) -> SearchOutcome:
        on_progress = kwargs.get("on_progress")
        if callable(on_progress):
            on_progress(
                {
                    "id": submission.job_id,
                    "status": "running",
                    "progress": {"current": 25, "total": 50},
                }
            )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@unittest.skipUnless(os.name == "nt", "tkinter integration tests are Windows-only")
class DesktopSearchUiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import tkinter  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"tkinter unavailable: {exc}")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_path, self.initial_manifest, self.search_manifest = (
            self._create_fixture(self.root)
        )
        runtime_root = self.root / "runtime"
        runtime_root.mkdir()
        self.runtime = BackendRuntime(
            base_url="http://127.0.0.1:12345/",
            host="127.0.0.1",
            port=12345,
            pid=123,
            instance_id="test",
            config_fingerprint="fingerprint",
            runtime_directory=runtime_root,
            libraries_manifest=runtime_root / "libraries.json",
            query_root=runtime_root / "query",
        )

    def test_backend_events_saved_key_and_success_load_stay_on_main_thread(
        self,
    ) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = _RuntimeController(self.runtime)
        submission = SubmittedSearch("search-1", "text", None, {"id": "search-1"})
        service = _SearchService(
            SearchOutcome(
                submission=submission,
                status="succeeded",
                job={"id": "search-1", "status": "succeeded"},
                result={"output_dir": str(self.search_manifest.parent)},
                error=None,
                output_directory=self.search_manifest.parent,
                manifest_path=self.search_manifest,
            )
        )
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(Any, service),
                credential_store=cast(CredentialStore, _CredentialStore()),
            )
            self._pump_until(window, lambda: window._search_service is not None)
            self.assertEqual(controller.credentials, ["saved-key"])
            self.assertEqual(set(controller.callback_threads), {threading.get_ident()})

            window._query_text_var.set("人物动作")
            window.start_search()
            self._pump_until(
                window,
                lambda: window._source_path == self.search_manifest,
            )
            self.assertEqual(len(service.requests), 1)
            self.assertEqual(
                (service.requests[0].top_k, service.requests[0].candidate_k), (15, 50)
            )
            self.assertIsNotNone(window._page)
            assert window._page is not None
            self.assertEqual(window._page.manifest_path, self.search_manifest)
            self.assertEqual(window._items[0].name, "新结果.png")
            self.assertFalse(window._search_running)
        except Exception:
            if window is None:
                self.skipTest("tkinter display is unavailable")
            raise
        finally:
            if window is not None:
                self._close_window(window)

    def test_failed_search_keeps_old_gallery_and_busy_close_keeps_window(self) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = _RuntimeController(self.runtime)
        service = _SearchService(RuntimeError("simulated model failure"))
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(Any, service),
                credential_store=cast(CredentialStore, _CredentialStore(None)),
            )
            self._pump_until(
                window,
                lambda: window._page is not None and window._search_service is not None,
            )
            old_page = window._page
            old_items = window._items
            window._query_text_var.set("不存在的内容")
            with mock.patch("zvec_desktop.ui.messagebox.showerror"):
                window.start_search()
                self._pump_until(window, lambda: not window._search_running)
            self.assertIs(window._page, old_page)
            self.assertEqual(window._items, old_items)
            self.assertIn("原有结果保持不变", window._status_text.get())

            controller.busy = True
            with mock.patch("zvec_desktop.ui.messagebox.showwarning") as warning:
                window.close()
            warning.assert_called_once()
            self.assertFalse(window._closed)
            self.assertFalse(window._close_pending)
            self.assertIn("窗口已保持打开", window._status_text.get())

            controller.busy = False
            self._close_window(window)
            self.assertTrue(window._closed)
            window = None
        except Exception:
            if window is None:
                self.skipTest("tkinter display is unavailable")
            raise
        finally:
            if window is not None:
                with suppress(Exception):
                    controller.busy = False
                    self._close_window(window)

    def test_restart_generation_applies_changes_made_during_start(self) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = _RuntimeController(self.runtime)
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(
                    Any, _SearchService(RuntimeError("unused"))
                ),
                credential_store=cast(CredentialStore, _CredentialStore(None)),
            )
            self._pump_until(window, lambda: window._search_service is not None)

            controller.defer_next_start = True
            window._request_backend_restart("首次设置")
            deferred = controller.deferred_start
            self.assertIsNotNone(deferred)
            self.assertTrue(window._restart_pending)

            window._request_backend_restart("启动期间的新设置")
            assert deferred is not None
            deferred.set_result(self.runtime)
            self._pump_until(window, lambda: not window._restart_pending)

            self.assertEqual(controller.start_calls, 3)
            self.assertEqual(controller.stop_calls, 2)
            self.assertEqual(controller.state, RuntimeState.READY)
            self.assertIn("启动期间的新设置", window._status_text.get())

            # A late restart completion after Exit must not launch another backend.
            previous_starts = controller.start_calls
            window._restart_pending = True
            window._close_pending = True
            window._finish_backend_restart(None, None)
            self.assertFalse(window._restart_pending)
            self.assertEqual(controller.start_calls, previous_starts)
            window._close_pending = False
        except Exception:
            if window is None:
                self.skipTest("tkinter display is unavailable")
            raise
        finally:
            if window is not None:
                controller.busy = False
                self._close_window(window)

    def test_four_product_pages_and_deferred_settings_restart(self) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        controller = _RuntimeController(self.runtime)
        window: ZvecDesktopWindow | None = None
        try:
            window = ZvecDesktopWindow(
                DesktopLaunchOptions(config_path=self.config_path),
                backend_host=cast(BackendHost, mock.Mock()),
                runtime_controller=cast(BackendRuntimeController, controller),
                search_service_factory=lambda _runtime: cast(
                    Any, _SearchService(RuntimeError("unused"))
                ),
                credential_store=cast(CredentialStore, _CredentialStore(None)),
            )
            self._pump_until(window, lambda: window._search_service is not None)
            tab_labels = [
                window._notebook.tab(tab_id, "text")
                for tab_id in window._notebook.tabs()
            ]
            self.assertEqual(
                tab_labels,
                ["图片搜索", "图库任务", "智能整理", "设置"],
            )
            self.assertIsNotNone(window._organize_panel)
            self.assertIsNotNone(window._settings_panel)
            self.assertEqual(window._selected_organize_library_id(), "people")

            controller.busy = True
            with mock.patch("zvec_desktop.ui.messagebox.showwarning") as warning:
                window._request_backend_restart("模型配置已更新")
            warning.assert_called_once()
            self.assertTrue(window._restart_pending)
            self.assertIsNotNone(window._restart_retry_poll)
            self.assertIn("自动应用新设置", window._status_text.get())

            controller.busy = False
            assert window._restart_retry_poll is not None
            window.root.after_cancel(window._restart_retry_poll)
            window._restart_retry_poll = None
            window._retry_pending_backend_restart()
            self.assertFalse(window._restart_pending)
            self.assertEqual(controller.state, RuntimeState.READY)
            self.assertGreaterEqual(controller.stop_calls, 2)
            self.assertIn("后端已重新启动", window._status_text.get())
        except Exception:
            if window is None:
                self.skipTest("tkinter display is unavailable")
            raise
        finally:
            if window is not None:
                controller.busy = False
                self._close_window(window)

    @staticmethod
    def _pump_until(
        window: Any,
        predicate: Any,
        timeout: float = 8.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            window.root.update()
            time.sleep(0.01)
        if not predicate():
            raise AssertionError("desktop condition did not become true before timeout")

    @staticmethod
    def _settle(window: Any, duration: float = 0.35) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            window.root.update()
            time.sleep(0.01)

    @classmethod
    def _close_window(cls, window: Any) -> None:
        cls._settle(window)
        # The reusable dispatcher intentionally has non-blocking production
        # shutdown.  Tests wait for its private worker pool so one Tcl interpreter
        # cannot leak callback references into the following interpreter.
        window._image_dispatcher._executor.shutdown(  # noqa: SLF001
            wait=True,
            cancel_futures=True,
        )
        window.close()

    @staticmethod
    def _create_fixture(root: Path) -> tuple[Path, Path, Path]:
        images = root / "images"
        results = root / "results"
        workspace = root / "workspace"
        initial = results / "initial"
        searched = results / "searched"
        for folder in (images, results, workspace, initial, searched):
            folder.mkdir(parents=True, exist_ok=True)

        initial_image = images / "旧结果.png"
        new_image = images / "新结果.png"
        Image.new("RGB", (120, 80), "#335577").save(initial_image)
        Image.new("RGB", (80, 120), "#775533").save(new_image)
        (initial / initial_image.name).write_bytes(initial_image.read_bytes())
        (searched / new_image.name).write_bytes(new_image.read_bytes())

        def manifest(folder: Path, name: str) -> Path:
            path = folder / "results.json"
            path.write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-18T12:00:00+08:00",
                        "query_type": "text",
                        "status": "ok",
                        "library_ids": ["people"],
                        "results": [
                            {
                                "rank": 1,
                                "copied_file": name,
                                "relative_path": name,
                                "library_id": "people",
                                "library_name": "人物图库",
                                "confidence": 0.9,
                                "rank_source": "text",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return path

        initial_manifest = manifest(initial, initial_image.name)
        search_manifest = manifest(searched, new_image.name)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "results_directory": str(results),
                    "default_library_id": "people",
                    "libraries": [
                        {
                            "id": "people",
                            "name": "人物图库",
                            "image_root": str(images),
                            "workspace_directory": str(workspace),
                            "enabled": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return config, initial_manifest, search_manifest


if __name__ == "__main__":
    unittest.main()
