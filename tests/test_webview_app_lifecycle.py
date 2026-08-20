from __future__ import annotations

import sys
import threading
import time
import unittest
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from zvec_webview import app
from zvec_webview.single_instance import (
    InstanceDisposition,
    InstanceSignal,
    InstanceStartResult,
)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not satisfied before the timeout")


class _EventHook:
    def __init__(self) -> None:
        self.handlers: list[Callable[..., Any]] = []

    def __iadd__(self, handler: Callable[..., Any]) -> _EventHook:
        self.handlers.append(handler)
        return self

    def fire(self) -> tuple[Any, ...]:
        return tuple(handler() for handler in tuple(self.handlers))


class _FakeWindow:
    def __init__(self) -> None:
        self.events = SimpleNamespace(closing=_EventHook())
        self.hide_calls = 0
        self.show_calls = 0
        self.restore_calls = 0
        self.destroy_calls = 0
        self.evaluated_scripts: list[str] = []

    def hide(self) -> None:
        self.hide_calls += 1

    def show(self) -> None:
        self.show_calls += 1

    def restore(self) -> None:
        self.restore_calls += 1

    def destroy(self) -> None:
        self.destroy_calls += 1

    def evaluate_js(self, script: str) -> None:
        self.evaluated_scripts.append(script)


class _FakeWebview:
    def __init__(self) -> None:
        self.window = _FakeWindow()
        self.create_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.start_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.start_hook: Callable[[], None] | None = None

    def create_window(self, *args: Any, **kwargs: Any) -> _FakeWindow:
        self.create_calls.append((args, kwargs))
        return self.window

    def start(self, *args: Any, **kwargs: Any) -> None:
        self.start_calls.append((args, kwargs))
        if args and callable(args[0]):
            threading.Thread(target=args[0], daemon=True).start()
        if self.start_hook is not None:
            self.start_hook()


class _FakeFacade:
    def __init__(self, config_home: Path, *, active_jobs: bool) -> None:
        self.config_home = config_home
        self.image_registry = object()
        self.active_jobs = active_jobs
        self.has_active_jobs_calls = 0
        self._lock = threading.Lock()

    def has_active_jobs(self) -> bool:
        with self._lock:
            self.has_active_jobs_calls += 1
            return self.active_jobs

    def set_active_jobs(self, value: bool) -> None:
        with self._lock:
            self.active_jobs = value


class _FakeRuntime:
    def __init__(
        self,
        config_home: Path,
        *,
        active_jobs: bool,
        lifecycle: list[str],
    ) -> None:
        self.facade = _FakeFacade(config_home, active_jobs=active_jobs)
        self.lifecycle = lifecycle
        self.start_calls = 0
        self.close_calls: list[bool] = []

    def start(self) -> SimpleNamespace:
        self.start_calls += 1
        return SimpleNamespace(
            address=SimpleNamespace(url="http://127.0.0.1:43123/token/"),
            backend_started_async=True,
        )

    def close(self, *, force: bool = False) -> None:
        self.close_calls.append(force)
        self.lifecycle.append("runtime.close")


class _RuntimeFactory:
    def __init__(
        self,
        config_home: Path,
        *,
        active_jobs: bool,
        lifecycle: list[str],
    ) -> None:
        self.config_home = config_home
        self.active_jobs = active_jobs
        self.lifecycle = lifecycle
        self.calls: list[Path | None] = []
        self.instances: list[_FakeRuntime] = []

    def __call__(self, config_path: Path | None) -> _FakeRuntime:
        self.calls.append(config_path)
        runtime = _FakeRuntime(
            self.config_home,
            active_jobs=self.active_jobs,
            lifecycle=self.lifecycle,
        )
        self.instances.append(runtime)
        return runtime


class _FakeBridge:
    def __init__(
        self,
        registry: object,
        *,
        request_exit: Callable[[], dict[str, Any]],
    ) -> None:
        self.registry = registry
        self.request_exit = request_exit
        self.window: _FakeWindow | None = None

    def attach_window(self, window: _FakeWindow) -> None:
        self.window = window

    def exit_application(self) -> dict[str, Any]:
        return self.request_exit()


class _BridgeFactory:
    def __init__(self) -> None:
        self.instances: list[_FakeBridge] = []

    def __call__(
        self,
        registry: object,
        *,
        request_exit: Callable[[], dict[str, Any]],
    ) -> _FakeBridge:
        bridge = _FakeBridge(registry, request_exit=request_exit)
        self.instances.append(bridge)
        return bridge


class _FakeCoordinator:
    def __init__(
        self,
        result: InstanceStartResult,
        *,
        signals: tuple[InstanceSignal, ...],
        lifecycle: list[str],
    ) -> None:
        self.result = result
        self.lifecycle = lifecycle
        self.start_requests: list[bool] = []
        self.close_calls = 0
        self.closed = False
        self.drained = threading.Event()
        self._signals = deque(signals)
        self._lock = threading.Lock()

    def start(self, *, request_existing_exit: bool = False) -> InstanceStartResult:
        self.start_requests.append(request_existing_exit)
        return self.result

    def drain_notifications(self, *, maximum: int = 64) -> tuple[InstanceSignal, ...]:
        with self._lock:
            signals: list[InstanceSignal] = []
            for _ in range(maximum):
                if not self._signals:
                    break
                signals.append(self._signals.popleft())
        if signals:
            self.drained.set()
        return tuple(signals)

    def push(self, signal: InstanceSignal) -> None:
        with self._lock:
            self._signals.append(signal)

    def take_background_error(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.lifecycle.append("coordinator.close")


class _CoordinatorFactory:
    def __init__(
        self,
        result: InstanceStartResult,
        *,
        signals: tuple[InstanceSignal, ...],
        lifecycle: list[str],
    ) -> None:
        self.result = result
        self.signals = signals
        self.lifecycle = lifecycle
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.instances: list[_FakeCoordinator] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeCoordinator:
        self.calls.append((args, kwargs))
        coordinator = _FakeCoordinator(
            self.result,
            signals=self.signals,
            lifecycle=self.lifecycle,
        )
        self.instances.append(coordinator)
        return coordinator


class _AppHarness:
    def __init__(
        self,
        case: unittest.TestCase,
        *,
        disposition: InstanceDisposition = InstanceDisposition.PRIMARY,
        exit_code: int = 0,
        signals: tuple[InstanceSignal, ...] = (),
        active_jobs: bool = False,
    ) -> None:
        temporary = TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.config_home = Path(temporary.name)
        self.config_path = self.config_home / "config.json"
        self.lifecycle: list[str] = []
        self.runtime_factory = _RuntimeFactory(
            self.config_home,
            active_jobs=active_jobs,
            lifecycle=self.lifecycle,
        )
        self.bridge_factory = _BridgeFactory()
        self.coordinator_factory = _CoordinatorFactory(
            InstanceStartResult(
                disposition,
                exit_code=exit_code,
                signal_sent=disposition
                in {
                    InstanceDisposition.ACTIVATED_EXISTING,
                    InstanceDisposition.EXIT_REQUESTED,
                },
            ),
            signals=signals,
            lifecycle=self.lifecycle,
        )
        self.webview = _FakeWebview()

    @property
    def runtime(self) -> _FakeRuntime:
        return self.runtime_factory.instances[0]

    @property
    def bridge(self) -> _FakeBridge:
        return self.bridge_factory.instances[0]

    @property
    def coordinator(self) -> _FakeCoordinator:
        return self.coordinator_factory.instances[0]

    def run(self, *extra_args: str) -> int:
        argv = ["--config", str(self.config_path), *extra_args]
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(app, "_validate_windows_x64", return_value=None)
            )
            stack.enter_context(
                patch.object(app, "_report_startup_error", return_value=None)
            )
            stack.enter_context(
                patch.object(app, "PreviewRuntime", self.runtime_factory)
            )
            stack.enter_context(patch.object(app, "NativeBridge", self.bridge_factory))
            stack.enter_context(
                patch.object(
                    app,
                    "SingleInstanceCoordinator",
                    self.coordinator_factory,
                    create=True,
                )
            )
            stack.enter_context(patch.object(app, "webview", self.webview, create=True))
            stack.enter_context(patch.dict(sys.modules, {"webview": self.webview}))
            return app.main(argv)


class WebviewAppLifecycleTest(unittest.TestCase):
    def test_parser_uses_public_product_name(self) -> None:
        parser = app.build_parser()

        self.assertEqual(parser.prog, "YaoLens")
        self.assertNotIn("zvec-webview-preview", parser.format_help().casefold())

    def test_resident_task_maintenance_does_not_start_runtime_or_instance(self) -> None:
        with (
            patch.object(app, "_validate_windows_x64", return_value=None),
            patch.object(app, "install_resident_task") as install,
            patch.object(app, "remove_resident_task") as remove,
            patch.object(app, "SingleInstanceCoordinator") as coordinator,
        ):
            self.assertEqual(app.main(["--install-resident-task"]), 0)
            install.assert_called_once_with(Path(sys.executable).resolve().parent)
            remove.assert_not_called()
            coordinator.assert_not_called()

            self.assertEqual(app.main(["--remove-resident-task"]), 0)
            remove.assert_called_once_with()
            coordinator.assert_not_called()

    def test_secondary_exits_without_constructing_runtime(self) -> None:
        harness = _AppHarness(
            self,
            disposition=InstanceDisposition.ACTIVATED_EXISTING,
            exit_code=0,
        )

        result = harness.run()

        self.assertEqual(result, 0)
        self.assertEqual(harness.runtime_factory.instances, [])
        self.assertEqual(harness.webview.create_calls, [])
        self.assertEqual(harness.coordinator.start_requests, [False])
        self.assertEqual(harness.coordinator.close_calls, 1)

    def test_exit_running_instance_is_forwarded_and_uses_result_exit_code(
        self,
    ) -> None:
        harness = _AppHarness(
            self,
            disposition=InstanceDisposition.NO_RUNNING_INSTANCE,
            exit_code=3,
        )

        result = harness.run("--exit-running-instance")

        self.assertEqual(result, 3)
        self.assertEqual(harness.coordinator.start_requests, [True])
        self.assertEqual(harness.runtime_factory.instances, [])
        self.assertEqual(harness.coordinator.close_calls, 1)

    def test_start_hidden_is_passed_to_create_window(self) -> None:
        harness = _AppHarness(self)

        result = harness.run("--start-hidden")

        self.assertEqual(result, 0)
        self.assertEqual(len(harness.webview.create_calls), 1)
        args, kwargs = harness.webview.create_calls[0]
        self.assertEqual(args[0], "YaoLens")
        self.assertIs(kwargs["hidden"], True)

    def test_titlebar_closing_hides_without_closing_runtime(self) -> None:
        harness = _AppHarness(self)
        observed: dict[str, Any] = {}

        def close_window() -> None:
            observed["results"] = harness.webview.window.events.closing.fire()
            observed["runtime_closes"] = tuple(harness.runtime.close_calls)
            observed["hide_calls"] = harness.webview.window.hide_calls

        harness.webview.start_hook = close_window
        result = harness.run()

        self.assertEqual(result, 0)
        self.assertEqual(observed["results"], (False,))
        self.assertEqual(observed["hide_calls"], 1)
        self.assertEqual(observed["runtime_closes"], ())
        self.assertEqual(harness.webview.window.destroy_calls, 0)

    def test_activate_queued_before_window_attach_is_not_lost(self) -> None:
        harness = _AppHarness(self, signals=(InstanceSignal.ACTIVATE,))

        def wait_for_activation() -> None:
            _wait_until(
                lambda: (
                    harness.webview.window.show_calls == 1
                    and harness.webview.window.restore_calls == 1
                )
            )

        harness.webview.start_hook = wait_for_activation
        result = harness.run("--start-hidden")

        self.assertEqual(result, 0)
        self.assertTrue(harness.coordinator.drained.is_set())
        self.assertEqual(harness.webview.window.show_calls, 1)
        self.assertEqual(harness.webview.window.restore_calls, 1)

    def test_activate_after_window_attach_shows_and_restores(self) -> None:
        harness = _AppHarness(self)

        def activate_window() -> None:
            harness.coordinator.push(InstanceSignal.ACTIVATE)
            _wait_until(
                lambda: (
                    harness.webview.window.show_calls == 1
                    and harness.webview.window.restore_calls == 1
                )
            )

        harness.webview.start_hook = activate_window
        result = harness.run("--start-hidden")

        self.assertEqual(result, 0)
        self.assertEqual(harness.webview.window.show_calls, 1)
        self.assertEqual(harness.webview.window.restore_calls, 1)

    def test_idle_explicit_exit_closes_runtime_and_destroys_window(self) -> None:
        harness = _AppHarness(self)
        observed: dict[str, Any] = {}

        def exit_application() -> None:
            observed["result"] = harness.bridge.exit_application()
            _wait_until(lambda: harness.webview.window.destroy_calls == 1)

        harness.webview.start_hook = exit_application
        result = harness.run()

        self.assertEqual(result, 0)
        self.assertEqual(observed["result"], {"ok": True, "closing": True})
        self.assertGreaterEqual(len(harness.runtime.close_calls), 1)
        self.assertTrue(all(force is False for force in harness.runtime.close_calls))
        self.assertEqual(harness.webview.window.destroy_calls, 1)
        self.assertEqual(harness.coordinator.close_calls, 1)
        self.assertLess(
            harness.lifecycle.index("runtime.close"),
            harness.lifecycle.index("coordinator.close"),
        )

    def test_busy_settings_exit_keeps_runtime_and_instance_alive(self) -> None:
        harness = _AppHarness(self, active_jobs=True)
        observed: dict[str, Any] = {}

        def request_busy_exit() -> None:
            observed["result"] = harness.bridge.exit_application()
            observed["runtime_closes"] = tuple(harness.runtime.close_calls)
            observed["destroy_calls"] = harness.webview.window.destroy_calls
            observed["coordinator_closes"] = harness.coordinator.close_calls
            harness.runtime.facade.set_active_jobs(False)

        harness.webview.start_hook = request_busy_exit
        result = harness.run()

        self.assertEqual(result, 0)
        self.assertIs(observed["result"]["busy"], True)
        self.assertEqual(observed["runtime_closes"], ())
        self.assertEqual(observed["destroy_calls"], 0)
        self.assertEqual(observed["coordinator_closes"], 0)

    def test_busy_exit_signal_keeps_runtime_and_instance_alive(self) -> None:
        harness = _AppHarness(
            self,
            signals=(InstanceSignal.EXIT,),
            active_jobs=True,
        )
        observed: dict[str, Any] = {}

        def wait_for_exit_signal() -> None:
            _wait_until(lambda: harness.runtime.facade.has_active_jobs_calls > 0)
            observed["runtime_closes"] = tuple(harness.runtime.close_calls)
            observed["destroy_calls"] = harness.webview.window.destroy_calls
            observed["coordinator_closes"] = harness.coordinator.close_calls
            harness.runtime.facade.set_active_jobs(False)

        harness.webview.start_hook = wait_for_exit_signal
        result = harness.run()

        self.assertEqual(result, 0)
        self.assertTrue(harness.coordinator.drained.is_set())
        self.assertEqual(observed["runtime_closes"], ())
        self.assertEqual(observed["destroy_calls"], 0)
        self.assertEqual(observed["coordinator_closes"], 0)

    def test_coordinator_is_cleaned_when_webview_start_fails(self) -> None:
        harness = _AppHarness(self)

        def fail_start() -> None:
            raise RuntimeError("synthetic WebView failure")

        harness.webview.start_hook = fail_start
        result = harness.run()

        self.assertEqual(result, 1)
        self.assertEqual(harness.coordinator.close_calls, 1)
        self.assertGreaterEqual(len(harness.runtime.close_calls), 1)


if __name__ == "__main__":
    unittest.main()
