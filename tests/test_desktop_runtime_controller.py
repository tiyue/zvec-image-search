from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import deque
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from zvec_desktop.backend_api import BackendTransportError, JsonObject
from zvec_desktop.backend_host import (
    BackendBusyError,
    BackendConfigurationError,
    BackendHost,
    BackendHostError,
    BackendRuntime,
)
from zvec_desktop.runtime_controller import (
    BackendRuntimeController,
    RuntimeErrorCategory,
    RuntimeEventKind,
    RuntimeOperationError,
    RuntimeState,
)


def _job(job_id: str, status: str, *, current: int = 0) -> JsonObject:
    return {
        "id": job_id,
        "command": "index",
        "status": status,
        "progress": {"current": current, "total": 2, "failed": 0},
        "failure_count": 0,
    }


class _FakeApiClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.job_id = "a" * 32
        self.submit_result = _job(self.job_id, "queued")
        self.submit_entered = threading.Event()
        self.submit_gate: threading.Event | None = None
        self.poll_results: deque[JsonObject | Exception] = deque(
            [_job(self.job_id, "succeeded", current=2)]
        )
        self.listed_jobs: list[JsonObject] = [_job(self.job_id, "running", current=1)]
        self.credentials: tuple[str, str | None] | None = None

    def get_health(self) -> JsonObject:
        return {"status": "ok", "service_ready": True, "worker_alive": True}

    def configure_credentials(self, key: str, api_url: str | None = None) -> JsonObject:
        self.credentials = (key, api_url)
        return {"credentials_configured": True}

    def submit_job(
        self,
        command: str,
        params: Any = None,
    ) -> JsonObject:
        if not command:
            raise ValueError("command must be non-empty")
        self.submit_entered.set()
        if self.submit_gate is not None and not self.submit_gate.wait(2):
            raise TimeoutError("test submit gate timed out")
        return dict(self.submit_result)

    def get_job(self, job_id: str) -> JsonObject:
        if job_id != self.job_id:
            raise AssertionError(f"unexpected job ID: {job_id}")
        with self._lock:
            if len(self.poll_results) > 1:
                result = self.poll_results.popleft()
            else:
                result = self.poll_results[0]
        if isinstance(result, Exception):
            raise result
        return dict(result)

    def list_jobs(self, *, active: bool | None, limit: int) -> JsonObject:
        del active
        selected = [dict(job) for job in self.listed_jobs[:limit]]
        return {"jobs": selected, "count": len(selected), "total_count": len(selected)}

    def cancel_job(self, job_id: str) -> JsonObject:
        return _job(job_id, "cancelling", current=1)


class _FakeHost:
    def __init__(self, root: Path) -> None:
        self.api = _FakeApiClient()
        self._runtime = BackendRuntime(
            base_url="http://127.0.0.1:12345/",
            host="127.0.0.1",
            port=12345,
            pid=101,
            instance_id="test-instance",
            config_fingerprint="f" * 64,
            runtime_directory=root,
            libraries_manifest=root / "libraries.json",
            query_root=root / "query",
        )
        self.running = False
        self.busy = False
        self.start_entered = threading.Event()
        self.start_gate: threading.Event | None = None
        self.start_errors: deque[Exception] = deque()
        self.start_calls = 0
        self.stop_calls: list[bool] = []

    @property
    def runtime(self) -> BackendRuntime | None:
        return self._runtime if self.running else None

    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def client(self) -> Any:
        if not self.running:
            raise BackendHostError("backend is not running")
        return self.api

    def start(self) -> BackendRuntime:
        self.start_calls += 1
        self.start_entered.set()
        if self.start_gate is not None and not self.start_gate.wait(2):
            raise TimeoutError("test start gate timed out")
        if self.start_errors:
            raise self.start_errors.popleft()
        self.running = True
        return self._runtime

    def stop(self, *, force: bool = False) -> None:
        self.stop_calls.append(force)
        if self.busy and not force:
            raise BackendBusyError([self.api.job_id])
        self.running = False


class BackendRuntimeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = _FakeHost(self.root)
        self.controller = BackendRuntimeController(
            cast(BackendHost, self.host),
            poll_interval=0.01,
            max_workers=4,
        )
        self.addCleanup(self._cleanup_controller)

    def _cleanup_controller(self) -> None:
        with suppress(Exception):
            self.controller.dispose(force=True).result(timeout=2)

    def _start(self) -> None:
        self.controller.start().result(timeout=2)
        self.controller.drain_events()

    def test_start_is_non_blocking_and_callbacks_run_only_while_draining(self) -> None:
        gate = threading.Event()
        self.host.start_gate = gate
        callback_threads: list[int] = []
        main_thread = threading.get_ident()

        started_at = time.monotonic()
        future = self.controller.start()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertTrue(self.host.start_entered.wait(1))
        self.assertFalse(future.done())
        self.assertEqual(self.controller.state, RuntimeState.STARTING)
        self.assertEqual(callback_threads, [])

        first_events = self.controller.drain_events(
            lambda _event: callback_threads.append(threading.get_ident())
        )
        self.assertEqual(
            [event.kind for event in first_events],
            [RuntimeEventKind.STATE_CHANGED],
        )
        gate.set()
        runtime = future.result(timeout=2)
        remaining = self.controller.drain_events(
            lambda _event: callback_threads.append(threading.get_ident())
        )

        self.assertEqual(runtime, self.host.runtime)
        self.assertEqual(self.controller.state, RuntimeState.READY)
        self.assertIn(
            RuntimeEventKind.RUNTIME_READY,
            {event.kind for event in remaining},
        )
        self.assertTrue(callback_threads)
        self.assertEqual(set(callback_threads), {main_thread})

    def test_submit_and_poll_emit_coalesced_progress_until_terminal(self) -> None:
        self._start()
        self.host.api.poll_results = deque(
            [
                _job(self.host.api.job_id, "running", current=1),
                _job(self.host.api.job_id, "running", current=1),
                _job(self.host.api.job_id, "succeeded", current=2),
            ]
        )

        completed = self.controller.submit_job("index", {"library_id": "main"})
        result = completed.result(timeout=2)
        events = self.controller.drain_events()
        statuses = [
            event.job["status"]
            for event in events
            if event.kind is RuntimeEventKind.JOB_UPDATED and event.job is not None
        ]

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(statuses, ["queued", "running", "succeeded"])

    def test_slow_job_submission_never_blocks_the_ui_owner_thread(self) -> None:
        self._start()
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.host.api.submit_gate = gate

        started_at = time.monotonic()
        future = self.controller.submit_job("index", {"library_id": "main"})
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertTrue(self.host.api.submit_entered.wait(1))
        self.assertFalse(future.done())
        gate.set()
        self.assertEqual(future.result(timeout=2)["status"], "succeeded")

    def test_transient_poll_failure_warns_then_recovers(self) -> None:
        self._start()
        self.host.api.poll_results = deque(
            [
                BackendTransportError(
                    "GET", "http://127.0.0.1/job", "temporary disconnect"
                ),
                _job(self.host.api.job_id, "succeeded", current=2),
            ]
        )

        result = self.controller.poll_job(self.host.api.job_id).result(timeout=2)
        events = self.controller.drain_events()
        warning = next(
            event for event in events if event.kind is RuntimeEventKind.WARNING
        )

        self.assertEqual(result["status"], "succeeded")
        assert warning.error is not None
        self.assertEqual(warning.error.category, RuntimeErrorCategory.TRANSPORT)
        self.assertTrue(warning.error.recoverable)

    def test_health_credentials_list_and_cancel_are_background_operations(self) -> None:
        self._start()

        health = self.controller.health().result(timeout=2)
        configured = self.controller.configure_credentials(
            "secret", "https://example.invalid/api"
        ).result(timeout=2)
        listed = self.controller.list_jobs(active=True, limit=25).result(timeout=2)
        cancelled = self.controller.cancel_job(self.host.api.job_id).result(timeout=2)
        events = self.controller.drain_events()

        self.assertEqual(health["status"], "ok")
        self.assertTrue(configured["credentials_configured"])
        self.assertEqual(
            self.host.api.credentials,
            ("secret", "https://example.invalid/api"),
        )
        self.assertEqual(listed["count"], 1)
        self.assertEqual(cancelled["status"], "cancelling")
        kinds = {event.kind for event in events}
        self.assertIn(RuntimeEventKind.HEALTH_UPDATED, kinds)
        self.assertIn(RuntimeEventKind.JOBS_LISTED, kinds)
        self.assertIn(RuntimeEventKind.JOB_UPDATED, kinds)

    def test_safe_stop_preserves_busy_backend_and_returns_structured_error(
        self,
    ) -> None:
        self._start()
        self.host.busy = True

        future = self.controller.stop()
        with self.assertRaises(RuntimeOperationError) as raised:
            future.result(timeout=2)

        info = raised.exception.info
        self.assertEqual(info.category, RuntimeErrorCategory.BUSY)
        self.assertTrue(info.recoverable)
        self.assertEqual(info.details, {"active_job_ids": [self.host.api.job_id]})
        self.assertEqual(self.controller.state, RuntimeState.READY)
        self.assertTrue(self.host.running)

        errors = [
            event
            for event in self.controller.drain_events()
            if event.kind is RuntimeEventKind.ERROR
        ]
        self.assertEqual(len(errors), 1)
        self.host.busy = False

    def test_busy_dispose_keeps_controller_attached_until_retry_succeeds(self) -> None:
        self._start()
        self.host.busy = True

        with self.assertRaises(RuntimeOperationError):
            self.controller.dispose().result(timeout=2)

        # The session token belongs to this host/controller.  A busy shutdown must
        # keep it available instead of orphaning an unauthenticated backend.
        self.assertEqual(self.controller.state, RuntimeState.READY)
        self.assertEqual(self.controller.health().result(timeout=2)["status"], "ok")

        self.host.busy = False
        self.controller.dispose().result(timeout=2)
        self.assertEqual(self.controller.state, RuntimeState.DISPOSED)

    def test_start_failure_is_recoverable_and_start_can_be_retried(self) -> None:
        self.host.start_errors.append(BackendConfigurationError("invalid config"))

        with self.assertRaises(RuntimeOperationError) as raised:
            self.controller.start().result(timeout=2)

        self.assertEqual(
            raised.exception.info.category,
            RuntimeErrorCategory.CONFIGURATION,
        )
        self.assertTrue(raised.exception.info.recoverable)
        self.assertEqual(self.controller.state, RuntimeState.ERROR)

        runtime = self.controller.start().result(timeout=2)
        self.assertEqual(runtime, self.host.runtime)
        self.assertEqual(self.controller.state, RuntimeState.READY)
        self.assertEqual(self.host.start_calls, 2)

    def test_unknown_job_status_fails_as_recoverable_protocol_error(self) -> None:
        self._start()
        self.host.api.poll_results = deque([_job(self.host.api.job_id, "mystery")])

        with self.assertRaises(RuntimeOperationError) as raised:
            self.controller.poll_job(self.host.api.job_id).result(timeout=2)

        self.assertEqual(raised.exception.info.category, RuntimeErrorCategory.PROTOCOL)
        self.assertTrue(raised.exception.info.recoverable)

    def test_unexpected_process_exit_marks_state_error_and_allows_restart(self) -> None:
        self._start()
        self.host.running = False

        with self.assertRaises(RuntimeOperationError) as raised:
            self.controller.list_jobs().result(timeout=2)

        self.assertEqual(raised.exception.info.category, RuntimeErrorCategory.NOT_READY)
        self.assertEqual(self.controller.state, RuntimeState.ERROR)
        self.controller.start().result(timeout=2)
        self.assertEqual(self.controller.state, RuntimeState.READY)

    def test_stop_while_starting_does_not_block_caller_or_race_lifecycle(
        self,
    ) -> None:
        gate = threading.Event()
        self.host.start_gate = gate
        start_future = self.controller.start()
        self.assertTrue(self.host.start_entered.wait(1))

        before = time.monotonic()
        stop_future = self.controller.stop()
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertEqual(self.controller.state, RuntimeState.STOPPING)

        gate.set()
        start_future.result(timeout=2)
        stop_future.result(timeout=2)
        self.assertEqual(self.controller.state, RuntimeState.STOPPED)
        self.assertFalse(self.host.running)

    def test_dispose_releases_controller_and_rejects_new_work(self) -> None:
        self._start()
        self.controller.dispose().result(timeout=2)

        self.assertEqual(self.controller.state, RuntimeState.DISPOSED)
        with self.assertRaises(RuntimeOperationError) as raised:
            self.controller.start().result(timeout=2)
        self.assertEqual(raised.exception.info.category, RuntimeErrorCategory.DISPOSED)
        self.assertFalse(raised.exception.info.recoverable)

    def test_drain_events_validates_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.drain_events(max_events=0)

    def test_operation_return_type_is_future(self) -> None:
        operation = self.controller.start()
        self.assertIsInstance(operation, Future)
        operation.result(timeout=2)


if __name__ == "__main__":
    unittest.main()
