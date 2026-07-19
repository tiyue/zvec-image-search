from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path

from zvec_desktop.single_instance import (
    ACTIVATE_INSTANCE_EVENT_NAME,
    EXIT_INSTANCE_EVENT_NAME,
    EXIT_RUNNING_INSTANCE_ARGUMENT,
    SINGLE_INSTANCE_MUTEX_NAME,
    InstanceDisposition,
    PosixFileLock,
    SingleInstanceCallbackError,
    SingleInstanceCoordinator,
    SingleInstanceFileLockError,
    SingleInstanceNativeError,
    SingleInstanceStateError,
    TkSingleInstancePump,
)


class _FakeNativeApi:
    def __init__(self, *, created_new: bool) -> None:
        self.created_new = created_new
        self.open_missing_count = 0
        self.open_always_missing = False
        self.open_error: SingleInstanceNativeError | None = None
        self.wait_error: SingleInstanceNativeError | None = None
        self.wait_results: queue.Queue[int] = queue.Queue()
        self.wait_delivered = threading.Event()
        self.created_mutexes: list[tuple[str, bool]] = []
        self.created_events: list[str] = []
        self.opened_events: list[str] = []
        self.signaled_handles: list[object] = []
        self.closed_handles: list[object] = []
        self.released_mutexes: list[object] = []

    def create_mutex(self, name: str, initially_owned: bool) -> tuple[object, bool]:
        self.created_mutexes.append((name, initially_owned))
        return "mutex", self.created_new

    def release_mutex(self, handle: object) -> None:
        self.released_mutexes.append(handle)

    def create_auto_reset_event(self, name: str) -> object:
        self.created_events.append(name)
        return f"event:{name}"

    def open_event(self, name: str) -> object | None:
        self.opened_events.append(name)
        if self.open_error is not None:
            raise self.open_error
        if self.open_always_missing:
            return None
        if self.open_missing_count > 0:
            self.open_missing_count -= 1
            return None
        return f"opened:{name}"

    def signal_event(self, handle: object) -> None:
        self.signaled_handles.append(handle)

    def wait_any(self, handles: Sequence[object], timeout_ms: int) -> int | None:
        if self.wait_error is not None:
            error = self.wait_error
            self.wait_error = None
            raise error
        try:
            result = self.wait_results.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return None
        self.wait_delivered.set()
        return result

    def close_handle(self, handle: object) -> None:
        self.closed_handles.append(handle)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _FakeFileLock:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.acquired

    def close(self) -> None:
        self.close_calls += 1


class _FakeTkRoot:
    def __init__(self) -> None:
        self.owner_thread = threading.get_ident()
        self.callbacks: dict[str, Callable[[], None]] = {}
        self.cancelled: list[str] = []
        self._next = 0

    def after(self, _milliseconds: int, callback: Callable[[], None]) -> str:
        self.assert_ui_thread()
        self._next += 1
        handle = f"after-{self._next}"
        self.callbacks[handle] = callback
        return handle

    def after_cancel(self, identifier: str) -> None:
        self.assert_ui_thread()
        self.cancelled.append(identifier)
        self.callbacks.pop(identifier, None)

    def run_next(self) -> None:
        self.assert_ui_thread()
        handle = next(iter(self.callbacks))
        callback = self.callbacks.pop(handle)
        callback()

    def assert_ui_thread(self) -> None:
        if threading.get_ident() != self.owner_thread:
            raise AssertionError("A background waiter touched the Tk root")


class WindowsSingleInstanceCoordinatorTest(unittest.TestCase):
    def test_names_and_exit_argument_keep_upgrade_compatibility(self) -> None:
        self.assertEqual(SINGLE_INSTANCE_MUTEX_NAME, r"Local\Zvec.ImageSearch.Desktop")
        self.assertEqual(
            ACTIVATE_INSTANCE_EVENT_NAME,
            r"Local\Zvec.ImageSearch.Desktop.Activate",
        )
        self.assertEqual(
            EXIT_INSTANCE_EVENT_NAME,
            r"Local\Zvec.ImageSearch.Desktop.Exit",
        )
        self.assertEqual(EXIT_RUNNING_INSTANCE_ARGUMENT, "--exit-running-instance")

    def test_second_instance_sends_only_payload_free_activation_and_exits(self) -> None:
        native = _FakeNativeApi(created_new=False)
        coordinator = SingleInstanceCoordinator(native_api=native)
        result = coordinator.start()

        self.assertEqual(result.disposition, InstanceDisposition.ACTIVATED_EXISTING)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.signal_sent)
        self.assertEqual(native.opened_events, [ACTIVATE_INSTANCE_EVENT_NAME])
        self.assertEqual(
            native.signaled_handles,
            [f"opened:{ACTIVATE_INSTANCE_EVENT_NAME}"],
        )
        self.assertEqual(native.created_events, [])
        self.assertEqual(
            native.closed_handles, [f"opened:{ACTIVATE_INSTANCE_EVENT_NAME}", "mutex"]
        )
        coordinator.close()

    def test_exit_switch_signals_existing_instance(self) -> None:
        native = _FakeNativeApi(created_new=False)
        coordinator = SingleInstanceCoordinator(native_api=native)
        result = coordinator.start(request_existing_exit=True)
        self.assertEqual(result.disposition, InstanceDisposition.EXIT_REQUESTED)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(native.opened_events, [EXIT_INSTANCE_EVENT_NAME])

    def test_exit_switch_without_existing_instance_returns_three_and_no_ui(
        self,
    ) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(native_api=native)
        result = coordinator.start(request_existing_exit=True)
        self.assertEqual(result.disposition, InstanceDisposition.NO_RUNNING_INSTANCE)
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.should_run_ui)
        self.assertEqual(native.created_events, [])
        self.assertEqual(native.released_mutexes, ["mutex"])
        coordinator.close()

    def test_activation_retries_until_first_instance_creates_event(self) -> None:
        native = _FakeNativeApi(created_new=False)
        native.open_missing_count = 2
        clock = _FakeClock()
        coordinator = SingleInstanceCoordinator(
            native_api=native,
            signal_timeout=1.0,
            signal_retry_interval=0.05,
            clock=clock,
            sleeper=clock.sleep,
        )
        result = coordinator.start()
        self.assertEqual(result.disposition, InstanceDisposition.ACTIVATED_EXISTING)
        self.assertEqual(len(native.opened_events), 3)
        self.assertEqual(clock.sleeps, [0.05, 0.05])

    def test_missing_event_returns_structured_signal_failure(self) -> None:
        native = _FakeNativeApi(created_new=False)
        native.open_always_missing = True
        clock = _FakeClock()
        coordinator = SingleInstanceCoordinator(
            native_api=native,
            signal_timeout=0.1,
            signal_retry_interval=0.05,
            clock=clock,
            sleeper=clock.sleep,
        )
        result = coordinator.start()
        self.assertEqual(result.disposition, InstanceDisposition.SIGNAL_FAILED)
        self.assertEqual(result.exit_code, 2)
        self.assertIsInstance(result.error, SingleInstanceNativeError)

    def test_start_is_single_use_and_close_is_idempotent(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(native_api=native, native_wait_ms=10)
        result = coordinator.start()
        self.assertTrue(result.should_run_ui)
        with self.assertRaises(SingleInstanceStateError):
            coordinator.start()
        coordinator.close()
        coordinator.close()
        self.assertEqual(native.released_mutexes, ["mutex"])


class TkSingleInstancePumpTest(unittest.TestCase):
    def test_native_waiter_never_touches_tk_and_callbacks_run_on_ui_thread(
        self,
    ) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(native_api=native, native_wait_ms=10)
        result = coordinator.start()
        self.assertTrue(result.should_run_ui)
        root = _FakeTkRoot()
        callback_threads: list[tuple[str, int]] = []
        pump = TkSingleInstancePump(
            root,
            coordinator,
            on_activate=lambda: callback_threads.append(
                ("activate", threading.get_ident())
            ),
            on_exit=lambda: callback_threads.append(("exit", threading.get_ident())),
            poll_interval_ms=10,
        )
        pump.start()

        native.wait_delivered.clear()
        native.wait_results.put(0)
        self.assertTrue(native.wait_delivered.wait(timeout=1))
        root.run_next()
        native.wait_delivered.clear()
        native.wait_results.put(1)
        self.assertTrue(native.wait_delivered.wait(timeout=1))
        root.run_next()

        self.assertEqual(
            callback_threads,
            [
                ("activate", root.owner_thread),
                ("exit", root.owner_thread),
            ],
        )
        pump.stop()
        coordinator.close()

    def test_native_wait_error_is_reported_on_the_tk_thread(self) -> None:
        native = _FakeNativeApi(created_new=True)
        native.wait_error = SingleInstanceNativeError("wait", 55)
        coordinator = SingleInstanceCoordinator(native_api=native, native_wait_ms=10)
        coordinator.start()
        root = _FakeTkRoot()
        reported: list[object] = []
        pump = TkSingleInstancePump(
            root,
            coordinator,
            on_activate=lambda: None,
            on_exit=lambda: None,
            on_error=reported.append,
            poll_interval_ms=10,
        )
        pump.start()
        deadline = time.monotonic() + 1
        while native.wait_error is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        root.run_next()
        self.assertEqual(len(reported), 1)
        self.assertIsInstance(reported[0], SingleInstanceNativeError)
        self.assertIs(pump.last_error, reported[0])
        pump.stop()
        coordinator.close()

    def test_callback_error_is_structured_without_breaking_polling(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(native_api=native, native_wait_ms=10)
        coordinator.start()
        root = _FakeTkRoot()
        reported: list[object] = []
        pump = TkSingleInstancePump(
            root,
            coordinator,
            on_activate=lambda: (_ for _ in ()).throw(ValueError("activate failed")),
            on_exit=lambda: None,
            on_error=reported.append,
            poll_interval_ms=10,
        )
        pump.start()
        native.wait_results.put(0)
        self.assertTrue(native.wait_delivered.wait(timeout=1))
        root.run_next()
        self.assertEqual(len(reported), 1)
        self.assertIsInstance(reported[0], SingleInstanceCallbackError)
        self.assertIsInstance(pump.last_error, SingleInstanceCallbackError)
        self.assertTrue(pump.running)
        pump.stop()
        coordinator.close()

    def test_exit_callback_may_close_coordinator_without_rescheduling_tk(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(native_api=native, native_wait_ms=10)
        coordinator.start()
        root = _FakeTkRoot()
        pump = TkSingleInstancePump(
            root,
            coordinator,
            on_activate=lambda: None,
            on_exit=coordinator.close,
            poll_interval_ms=10,
        )
        pump.start()
        native.wait_results.put(1)
        self.assertTrue(native.wait_delivered.wait(timeout=1))
        root.run_next()
        self.assertTrue(coordinator.is_closed)
        self.assertFalse(pump.running)
        self.assertEqual(root.callbacks, {})


class NonWindowsFallbackTest(unittest.TestCase):
    def test_file_lock_primary_runs_without_an_activation_channel(self) -> None:
        lock = _FakeFileLock(acquired=True)
        coordinator = SingleInstanceCoordinator(
            platform_name="posix",
            file_lock=lock,
        )
        result = coordinator.start()
        self.assertEqual(result.disposition, InstanceDisposition.PRIMARY)
        self.assertTrue(result.should_run_ui)
        coordinator.close()
        self.assertEqual(lock.close_calls, 1)

    def test_file_lock_secondary_reports_explicit_degradation(self) -> None:
        lock = _FakeFileLock(acquired=False)
        coordinator = SingleInstanceCoordinator(
            platform_name="posix",
            file_lock=lock,
        )
        result = coordinator.start()
        self.assertEqual(
            result.disposition,
            InstanceDisposition.ALREADY_RUNNING_NO_SIGNAL,
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIsInstance(result.error, SingleInstanceFileLockError)
        coordinator.close()

    def test_exit_request_with_no_file_locked_instance_returns_three(self) -> None:
        lock = _FakeFileLock(acquired=True)
        coordinator = SingleInstanceCoordinator(
            platform_name="posix",
            file_lock=lock,
        )
        result = coordinator.start(request_existing_exit=True)
        self.assertEqual(result.disposition, InstanceDisposition.NO_RUNNING_INSTANCE)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(lock.close_calls, 1)

    @unittest.skipIf(os.name == "nt", "real flock is exercised on non-Windows CI")
    def test_real_file_lock_is_empty_owner_only_and_reacquirable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime" / "desktop.lock"
            first = SingleInstanceCoordinator(
                platform_name="posix",
                file_lock=PosixFileLock(path),
            )
            second = SingleInstanceCoordinator(
                platform_name="posix",
                file_lock=PosixFileLock(path),
            )
            self.assertTrue(first.start().should_run_ui)
            self.assertEqual(
                second.start().disposition,
                InstanceDisposition.ALREADY_RUNNING_NO_SIGNAL,
            )
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            second.close()
            first.close()
            third = SingleInstanceCoordinator(
                platform_name="posix",
                file_lock=PosixFileLock(path),
            )
            self.assertTrue(third.start().should_run_ui)
            third.close()


if __name__ == "__main__":
    unittest.main()
