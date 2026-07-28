from __future__ import annotations

import queue
import threading
import time
import unittest
from collections.abc import Sequence

from zvec_webview.single_instance import (
    InstanceDisposition,
    InstanceSignal,
    SingleInstanceCoordinator,
    SingleInstanceNativeError,
    SingleInstanceStateError,
    SingleInstanceUnsupportedError,
    instance_object_names,
)


class _FakeNativeApi:
    def __init__(self, *, created_new: bool) -> None:
        self.created_new = created_new
        self.open_missing_count = 0
        self.open_always_missing = False
        self.wait_results: queue.Queue[int] = queue.Queue()
        self.wait_delivered = threading.Event()
        self.created_mutexes: list[tuple[str, bool]] = []
        self.created_events: list[str] = []
        self.opened_events: list[str] = []
        self.signaled_handles: list[object] = []
        self.released_mutexes: list[object] = []
        self.closed_handles: list[object] = []
        self.cleanup_operations: list[tuple[str, object]] = []

    def create_mutex(self, name: str, initially_owned: bool) -> tuple[object, bool]:
        self.created_mutexes.append((name, initially_owned))
        return "mutex", self.created_new

    def release_mutex(self, handle: object) -> None:
        self.released_mutexes.append(handle)
        self.cleanup_operations.append(("release", handle))

    def create_auto_reset_event(self, name: str) -> object:
        self.created_events.append(name)
        return f"event:{name}"

    def open_event(self, name: str) -> object | None:
        self.opened_events.append(name)
        if self.open_always_missing:
            return None
        if self.open_missing_count > 0:
            self.open_missing_count -= 1
            return None
        return f"opened:{name}"

    def signal_event(self, handle: object) -> None:
        self.signaled_handles.append(handle)

    def wait_any(self, handles: Sequence[object], timeout_ms: int) -> int | None:
        try:
            result = self.wait_results.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return None
        self.wait_delivered.set()
        return result

    def close_handle(self, handle: object) -> None:
        self.closed_handles.append(handle)
        self.cleanup_operations.append(("close", handle))


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class InstanceObjectNamesTest(unittest.TestCase):
    def test_names_use_local_namespace_and_hide_normalized_path(self) -> None:
        first = instance_object_names(r"C:\Users\Alice\AppData\Local\Zvec\config")
        equivalent = instance_object_names(
            r"c:/users/alice/appdata/local/zvec/other/../config/"
        )

        self.assertEqual(first, equivalent)
        self.assertRegex(first.scope_digest, r"^[0-9a-f]{24}$")
        for name in (first.mutex, first.activate_event, first.exit_event):
            self.assertTrue(name.startswith(r"Local\Zvec.ImageSearch.WebView."))
            self.assertNotIn("Alice", name)
            self.assertNotIn("config", name.casefold())

    def test_distinct_config_scopes_do_not_share_named_objects(self) -> None:
        first = instance_object_names(r"C:\Zvec\profile-a")
        second = instance_object_names(r"C:\Zvec\profile-b")

        self.assertNotEqual(first.scope_digest, second.scope_digest)
        self.assertNotEqual(first.mutex, second.mutex)
        self.assertNotEqual(first.activate_event, second.activate_event)
        self.assertNotEqual(first.exit_event, second.exit_event)


class SingleInstanceCoordinatorTest(unittest.TestCase):
    scope = r"C:\Users\Test\AppData\Local\Zvec"

    def test_primary_creates_scoped_mutex_and_auto_reset_events(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )

        result = coordinator.start()

        self.assertEqual(result.disposition, InstanceDisposition.PRIMARY)
        self.assertTrue(result.should_run_ui)
        self.assertTrue(coordinator.is_primary)
        self.assertEqual(native.created_mutexes, [(coordinator.names.mutex, True)])
        self.assertEqual(
            native.created_events,
            [
                coordinator.names.activate_event,
                coordinator.names.exit_event,
            ],
        )
        coordinator.close()

    def test_secondary_sends_activate_without_creating_primary_events(self) -> None:
        native = _FakeNativeApi(created_new=False)
        coordinator = SingleInstanceCoordinator(self.scope, native_api=native)

        result = coordinator.start()

        self.assertEqual(
            result.disposition,
            InstanceDisposition.ACTIVATED_EXISTING,
        )
        self.assertTrue(result.signal_sent)
        self.assertFalse(result.should_run_ui)
        self.assertEqual(native.opened_events, [coordinator.names.activate_event])
        self.assertEqual(
            native.signaled_handles,
            [f"opened:{coordinator.names.activate_event}"],
        )
        self.assertEqual(native.created_events, [])
        self.assertEqual(
            native.closed_handles,
            [f"opened:{coordinator.names.activate_event}", "mutex"],
        )
        coordinator.close()

    def test_secondary_can_request_existing_exit(self) -> None:
        native = _FakeNativeApi(created_new=False)
        coordinator = SingleInstanceCoordinator(self.scope, native_api=native)

        result = coordinator.start(request_existing_exit=True)

        self.assertEqual(result.disposition, InstanceDisposition.EXIT_REQUESTED)
        self.assertTrue(result.signal_sent)
        self.assertEqual(native.opened_events, [coordinator.names.exit_event])
        self.assertEqual(
            native.signaled_handles,
            [f"opened:{coordinator.names.exit_event}"],
        )
        coordinator.close()

    def test_exit_request_without_running_instance_does_not_start_ui(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(self.scope, native_api=native)

        result = coordinator.start(request_existing_exit=True)

        self.assertEqual(
            result.disposition,
            InstanceDisposition.NO_RUNNING_INSTANCE,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.should_run_ui)
        self.assertEqual(native.created_events, [])
        self.assertEqual(native.released_mutexes, ["mutex"])
        self.assertEqual(native.closed_handles, ["mutex"])
        coordinator.close()

    def test_secondary_retries_until_primary_events_exist(self) -> None:
        native = _FakeNativeApi(created_new=False)
        native.open_missing_count = 2
        clock = _FakeClock()
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            signal_timeout=1.0,
            signal_retry_interval=0.05,
            clock=clock,
            sleeper=clock.sleep,
        )

        result = coordinator.start()

        self.assertEqual(
            result.disposition,
            InstanceDisposition.ACTIVATED_EXISTING,
        )
        self.assertEqual(len(native.opened_events), 3)
        self.assertEqual(clock.sleeps, [0.05, 0.05])
        coordinator.close()

    def test_missing_primary_event_returns_bounded_signal_failure(self) -> None:
        native = _FakeNativeApi(created_new=False)
        native.open_always_missing = True
        clock = _FakeClock()
        coordinator = SingleInstanceCoordinator(
            self.scope,
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
        self.assertEqual(clock.sleeps, [0.05, 0.05])
        self.assertEqual(native.closed_handles, ["mutex"])
        coordinator.close()

    def test_waiter_queues_signals_until_owner_drains_them(self) -> None:
        native = _FakeNativeApi(created_new=True)
        # Model an Activate event that became signalled before the waiter and
        # eventual WebView window were ready.
        native.wait_results.put(0)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )
        coordinator.start()

        self.assertTrue(native.wait_delivered.wait(timeout=1.0))
        native.wait_delivered.clear()
        native.wait_results.put(1)
        self.assertTrue(native.wait_delivered.wait(timeout=1.0))

        self.assertEqual(
            coordinator.drain_notifications(),
            (InstanceSignal.ACTIVATE, InstanceSignal.EXIT),
        )
        self.assertEqual(coordinator.drain_notifications(), ())
        coordinator.close()

    def test_close_stops_waiter_before_events_and_mutex_are_released(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )
        coordinator.start()

        coordinator.close()
        coordinator.close()

        activate_handle = f"event:{coordinator.names.activate_event}"
        exit_handle = f"event:{coordinator.names.exit_event}"
        self.assertEqual(
            native.cleanup_operations,
            [
                ("close", activate_handle),
                ("close", exit_handle),
                ("release", "mutex"),
                ("close", "mutex"),
            ],
        )
        self.assertTrue(coordinator.is_closed)
        self.assertFalse(coordinator.is_primary)

    def test_start_is_single_use_and_closed_coordinator_cannot_restart(self) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )
        coordinator.start()
        with self.assertRaises(SingleInstanceStateError):
            coordinator.start()
        coordinator.close()
        with self.assertRaises(SingleInstanceStateError):
            coordinator.start()

    def test_non_windows_import_is_safe_and_start_is_explicit(self) -> None:
        coordinator = SingleInstanceCoordinator(
            self.scope,
            platform_name="posix",
        )

        with self.assertRaises(SingleInstanceUnsupportedError):
            coordinator.start()
        coordinator.close()

    def test_notification_limit_is_validated_and_preserves_remaining_signals(
        self,
    ) -> None:
        native = _FakeNativeApi(created_new=True)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )
        coordinator.start()
        native.wait_results.put(0)
        self.assertTrue(native.wait_delivered.wait(timeout=1.0))
        native.wait_delivered.clear()
        native.wait_results.put(1)
        self.assertTrue(native.wait_delivered.wait(timeout=1.0))

        self.assertEqual(
            coordinator.drain_notifications(maximum=1),
            (InstanceSignal.ACTIVATE,),
        )
        self.assertEqual(
            coordinator.drain_notifications(),
            (InstanceSignal.EXIT,),
        )
        with self.assertRaises(ValueError):
            coordinator.drain_notifications(maximum=0)
        coordinator.close()

    def test_waiter_error_is_reported_without_calling_client_code(self) -> None:
        class _FailingWaitApi(_FakeNativeApi):
            def wait_any(
                self, handles: Sequence[object], timeout_ms: int
            ) -> int | None:
                raise SingleInstanceNativeError("wait", 55)

        native = _FailingWaitApi(created_new=True)
        coordinator = SingleInstanceCoordinator(
            self.scope,
            native_api=native,
            native_wait_ms=10,
        )
        coordinator.start()

        deadline = time.monotonic() + 1.0
        error = None
        while error is None and time.monotonic() < deadline:
            error = coordinator.take_background_error()
            if error is None:
                time.sleep(0.01)

        self.assertIsInstance(error, SingleInstanceNativeError)
        self.assertEqual(coordinator.drain_notifications(), ())
        coordinator.close()


if __name__ == "__main__":
    unittest.main()
