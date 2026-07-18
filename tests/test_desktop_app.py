from __future__ import annotations

import io
import sys
import types
import unittest
from collections.abc import Callable
from unittest import mock

from zvec_desktop import app
from zvec_desktop.single_instance import (
    InstanceDisposition,
    InstanceStartResult,
    SingleInstanceError,
    SingleInstanceNativeError,
)


class _FakeRoot:
    def __init__(self) -> None:
        self.deiconify_calls = 0
        self.lift_calls = 0
        self.focus_calls = 0

    def after(self, _milliseconds: int, _callback: Callable[[], None]) -> str:
        return "after-1"

    def after_cancel(self, _identifier: str) -> None:
        return

    def deiconify(self) -> None:
        self.deiconify_calls += 1

    def lift(self) -> None:
        self.lift_calls += 1

    def focus_force(self) -> None:
        self.focus_calls += 1


class _FakeWindow:
    def __init__(self, *, run_error: Exception | None = None) -> None:
        self.root = _FakeRoot()
        self.run_error = run_error
        self.run_calls = 0
        self.close_calls = 0
        self.on_run: Callable[[], None] | None = None

    def run(self) -> None:
        self.run_calls += 1
        if self.on_run is not None:
            self.on_run()
        if self.run_error is not None:
            raise self.run_error

    def close(self) -> None:
        self.close_calls += 1


class _PublicWindow(_FakeWindow):
    def __init__(self, *, run_error: Exception | None = None) -> None:
        super().__init__(run_error=run_error)
        self.show_calls = 0

    def show_window(self) -> None:
        self.show_calls += 1


class _FakeCoordinator:
    def __init__(self, result: InstanceStartResult) -> None:
        self.result = result
        self.start_requests: list[bool] = []
        self.close_calls = 0

    def start(self, *, request_existing_exit: bool = False) -> InstanceStartResult:
        self.start_requests.append(request_existing_exit)
        return self.result

    def close(self) -> None:
        self.close_calls += 1


class _FakePump:
    def __init__(
        self,
        root: object,
        coordinator: object,
        *,
        on_activate: Callable[[], None],
        on_exit: Callable[[], None],
        on_error: Callable[[SingleInstanceError], None] | None = None,
        **_kwargs: object,
    ) -> None:
        self.root = root
        self.coordinator = coordinator
        self.on_activate = on_activate
        self.on_exit = on_exit
        self.on_error = on_error
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class DesktopAppOptionTest(unittest.TestCase):
    def test_exit_running_instance_switch_is_part_of_launch_options(self) -> None:
        options = app.parse_options(["--exit-running-instance"])
        self.assertTrue(options.exit_running_instance)


class DesktopAppLifecycleTest(unittest.TestCase):
    def test_secondary_instance_activates_existing_without_loading_window(self) -> None:
        coordinator = _FakeCoordinator(
            InstanceStartResult(
                InstanceDisposition.ACTIVATED_EXISTING,
                exit_code=0,
                signal_sent=True,
            )
        )
        create_window = mock.Mock()
        with (
            mock.patch.object(
                app, "SingleInstanceCoordinator", return_value=coordinator
            ),
            mock.patch.object(app, "_create_window", create_window),
        ):
            exit_code = app.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(coordinator.start_requests, [False])
        self.assertEqual(coordinator.close_calls, 1)
        create_window.assert_not_called()

    def test_exit_switch_signals_existing_without_loading_window(self) -> None:
        coordinator = _FakeCoordinator(
            InstanceStartResult(
                InstanceDisposition.EXIT_REQUESTED,
                exit_code=0,
                signal_sent=True,
            )
        )
        create_window = mock.Mock()
        with (
            mock.patch.object(
                app, "SingleInstanceCoordinator", return_value=coordinator
            ),
            mock.patch.object(app, "_create_window", create_window),
        ):
            exit_code = app.main(["--exit-running-instance"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(coordinator.start_requests, [True])
        self.assertEqual(coordinator.close_calls, 1)
        create_window.assert_not_called()

    def test_primary_starts_pump_and_routes_activate_and_exit_on_ui_loop(self) -> None:
        coordinator = _FakeCoordinator(
            InstanceStartResult(InstanceDisposition.PRIMARY, exit_code=0)
        )
        window = _PublicWindow()
        pumps: list[_FakePump] = []

        def create_pump(
            root: object,
            received_coordinator: object,
            *,
            on_activate: Callable[[], None],
            on_exit: Callable[[], None],
            on_error: Callable[[SingleInstanceError], None] | None = None,
        ) -> _FakePump:
            pump = _FakePump(
                root,
                received_coordinator,
                on_activate=on_activate,
                on_exit=on_exit,
                on_error=on_error,
            )
            pumps.append(pump)
            return pump

        def exercise_callbacks() -> None:
            self.assertEqual(pumps[0].start_calls, 1)
            pumps[0].on_activate()
            pumps[0].on_exit()

        window.on_run = exercise_callbacks
        with (
            mock.patch.object(
                app, "SingleInstanceCoordinator", return_value=coordinator
            ),
            mock.patch.object(app, "_create_window", return_value=window),
            mock.patch.object(app, "TkSingleInstancePump", side_effect=create_pump),
        ):
            exit_code = app.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(window.run_calls, 1)
        self.assertEqual(window.show_calls, 1)
        self.assertEqual(window.close_calls, 1)
        self.assertEqual(pumps[0].stop_calls, 1)
        self.assertEqual(coordinator.close_calls, 1)

    def test_activation_falls_back_to_safe_root_operations(self) -> None:
        window = _FakeWindow()

        app._activate_window(window)

        self.assertEqual(window.root.deiconify_calls, 1)
        self.assertEqual(window.root.lift_calls, 1)
        self.assertEqual(window.root.focus_calls, 1)

    def test_pump_and_coordinator_are_closed_when_window_run_fails(self) -> None:
        coordinator = _FakeCoordinator(
            InstanceStartResult(InstanceDisposition.PRIMARY, exit_code=0)
        )
        window = _FakeWindow(run_error=RuntimeError("main loop failed"))
        pumps: list[_FakePump] = []

        def create_pump(
            root: object,
            received_coordinator: object,
            *,
            on_activate: Callable[[], None],
            on_exit: Callable[[], None],
            on_error: Callable[[SingleInstanceError], None] | None = None,
        ) -> _FakePump:
            pump = _FakePump(
                root,
                received_coordinator,
                on_activate=on_activate,
                on_exit=on_exit,
                on_error=on_error,
            )
            pumps.append(pump)
            return pump

        standard_error = io.StringIO()
        with (
            mock.patch.object(
                app, "SingleInstanceCoordinator", return_value=coordinator
            ),
            mock.patch.object(app, "_create_window", return_value=window),
            mock.patch.object(app, "TkSingleInstancePump", side_effect=create_pump),
            mock.patch("sys.stderr", standard_error),
        ):
            exit_code = app.main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(pumps[0].stop_calls, 1)
        self.assertEqual(coordinator.close_calls, 1)
        self.assertIn("main loop failed", standard_error.getvalue())

    def test_signal_failure_returns_contract_code_and_reports_error(self) -> None:
        error = SingleInstanceNativeError("OpenEventW", 2)
        coordinator = _FakeCoordinator(
            InstanceStartResult(
                InstanceDisposition.SIGNAL_FAILED,
                exit_code=2,
                error=error,
            )
        )
        standard_error = io.StringIO()
        with (
            mock.patch.object(
                app, "SingleInstanceCoordinator", return_value=coordinator
            ),
            mock.patch("sys.stderr", standard_error),
        ):
            exit_code = app.main([])

        self.assertEqual(exit_code, 2)
        self.assertEqual(coordinator.close_calls, 1)
        self.assertIn("OpenEventW", standard_error.getvalue())

    def test_window_factory_injects_a_real_tray_service_boundary(self) -> None:
        options = app.DesktopLaunchOptions()
        tray = object()
        captured: dict[str, object] = {}
        tray_module = types.ModuleType("zvec_desktop.tray")
        ui_module = types.ModuleType("zvec_desktop.ui")

        def create_tray() -> object:
            return tray

        def create_window(
            received_options: app.DesktopLaunchOptions,
            *,
            tray_service: object,
        ) -> object:
            captured["options"] = received_options
            captured["tray"] = tray_service
            return "window"

        tray_module.TrayIconService = create_tray  # type: ignore[attr-defined]
        ui_module.ZvecDesktopWindow = create_window  # type: ignore[attr-defined]
        with mock.patch.dict(
            sys.modules,
            {
                "zvec_desktop.tray": tray_module,
                "zvec_desktop.ui": ui_module,
            },
        ):
            window = app._create_window(options)

        self.assertEqual(window, "window")
        self.assertIs(captured["options"], options)
        self.assertIs(captured["tray"], tray)


if __name__ == "__main__":
    unittest.main()
