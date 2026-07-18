"""Thread-safe runtime controller for the pure-Python desktop client.

``BackendHost`` and ``BackendApiClient`` intentionally expose synchronous APIs.
That keeps their process and HTTP contracts straightforward, but none of those
methods may run on Tk's main thread.  This module owns a small worker pool and a
thread-safe event queue between those synchronous services and the UI.

The controller never invokes UI callbacks from a worker thread.  A Tk window can
drive event delivery safely with a short recurring callback::

    def pump_backend_events() -> None:
        controller.drain_events(render_backend_event)
        root.after(50, pump_backend_events)

Long-running job polls sleep only on worker threads.  Cancelling a backend job
uses a separate worker, so it remains responsive while another worker polls.
"""

from __future__ import annotations

import copy
import math
import threading
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from .backend_api import (
    BackendApiClient,
    BackendApiError,
    BackendHttpError,
    BackendProtocolError,
    BackendTimeoutError,
    BackendTransportError,
    JsonObject,
)
from .backend_host import (
    BackendBusyError,
    BackendConfigurationError,
    BackendHost,
    BackendHostError,
    BackendRuntime,
    BackendStartupError,
)

_T = TypeVar("_T")

_DEFAULT_POLL_INTERVAL = 0.4
_DEFAULT_TRANSIENT_RETRIES = 3
_DEFAULT_EVENT_CAPACITY = 2048
_DEFAULT_WORKERS = 8

_VALID_JOB_STATUSES = frozenset(
    {
        "queued",
        "running",
        "pausing",
        "paused",
        "needs_attention",
        "cancelling",
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    }
)
_POLL_STOP_STATUSES = frozenset(
    {"paused", "needs_attention", "succeeded", "partial", "failed", "cancelled"}
)


class RuntimeState(str, Enum):
    """Observable lifecycle state of the owned backend."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"
    DISPOSED = "disposed"


class RuntimeEventKind(str, Enum):
    """Kinds of events consumed by the desktop main thread."""

    STATE_CHANGED = "state_changed"
    RUNTIME_READY = "runtime_ready"
    HEALTH_UPDATED = "health_updated"
    JOB_UPDATED = "job_updated"
    JOBS_LISTED = "jobs_listed"
    WARNING = "warning"
    ERROR = "error"


class RuntimeErrorCategory(str, Enum):
    """Stable categories suitable for choosing a UI recovery action."""

    NOT_READY = "not_ready"
    DISPOSED = "disposed"
    CONFIGURATION = "configuration"
    STARTUP = "startup"
    BUSY = "busy"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    HTTP = "http"
    PROTOCOL = "protocol"
    VALIDATION = "validation"
    HOST = "host"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class RuntimeErrorInfo:
    """Serializable error details shown by the UI without parsing text."""

    operation: str
    category: RuntimeErrorCategory
    message: str
    recoverable: bool
    exception_type: str
    code: str | None = None
    status_code: int | None = None
    details: Any = None


class RuntimeOperationError(RuntimeError):
    """Future failure paired with the same structured error emitted to the UI."""

    def __init__(self, info: RuntimeErrorInfo) -> None:
        self.info = info
        super().__init__(info.message)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One immutable controller notification.

    ``job`` and ``payload`` are deep copies of JSON data returned by the backend,
    preventing UI code from mutating values used by polling workers.
    """

    kind: RuntimeEventKind
    state: RuntimeState
    operation: str
    occurred_at: datetime
    runtime: BackendRuntime | None = None
    job: JsonObject | None = None
    jobs: tuple[JsonObject, ...] = ()
    payload: JsonObject | None = None
    error: RuntimeErrorInfo | None = None


class _ControllerStateError(RuntimeError):
    pass


class _PollingStopped(RuntimeError):
    pass


class BackendRuntimeController:
    """Run backend lifecycle and job operations away from Tk's main thread.

    Public I/O methods return immediately with a :class:`Future`.  UI code should
    prefer :meth:`drain_events` for progress and errors because future callbacks
    execute on worker threads.
    """

    def __init__(
        self,
        host: BackendHost | None = None,
        *,
        host_factory: Callable[[], BackendHost] | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        transient_retries: int = _DEFAULT_TRANSIENT_RETRIES,
        max_workers: int = _DEFAULT_WORKERS,
        event_capacity: int = _DEFAULT_EVENT_CAPACITY,
    ) -> None:
        if host is not None and host_factory is not None:
            raise ValueError("host and host_factory are mutually exclusive")
        self._host = host or (
            host_factory() if host_factory is not None else BackendHost()
        )
        self._poll_interval = _positive_finite(poll_interval, "poll_interval")
        if (
            isinstance(transient_retries, bool)
            or not isinstance(transient_retries, int)
            or transient_retries < 0
        ):
            raise ValueError("transient_retries must be a non-negative integer")
        self._transient_retries = transient_retries
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers < 2
        ):
            raise ValueError("max_workers must be at least 2")
        if (
            isinstance(event_capacity, bool)
            or not isinstance(event_capacity, int)
            or event_capacity < 16
        ):
            raise ValueError("event_capacity must be at least 16")

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="zvec-desktop-backend",
        )
        self._state_lock = threading.RLock()
        self._event_lock = threading.Lock()
        self._events: deque[RuntimeEvent] = deque(maxlen=event_capacity)
        self._state = RuntimeState.STOPPED
        self._disposed = False
        self._start_future: Future[BackendRuntime] | None = None
        self._stop_future: Future[None] | None = None
        self._poll_stop = threading.Event()

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    @property
    def runtime(self) -> BackendRuntime | None:
        """Return current connection metadata without starting the backend."""

        return self._host.runtime

    @property
    def is_ready(self) -> bool:
        with self._state_lock:
            return self._state is RuntimeState.READY and self._host.is_running

    @property
    def pending_event_count(self) -> int:
        with self._event_lock:
            return len(self._events)

    def drain_events(
        self,
        callback: Callable[[RuntimeEvent], None] | None = None,
        *,
        max_events: int = 200,
    ) -> tuple[RuntimeEvent, ...]:
        """Remove pending events and optionally deliver them on the caller thread.

        Calling this method from a ``root.after`` handler guarantees that
        ``callback`` runs on Tk's main thread.  Worker threads only enqueue data.
        """

        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 1
        ):
            raise ValueError("max_events must be a positive integer")
        drained: list[RuntimeEvent] = []
        with self._event_lock:
            for _ in range(min(max_events, len(self._events))):
                drained.append(self._events.popleft())
        if callback is not None:
            for event in drained:
                callback(event)
        return tuple(drained)

    def start(self) -> Future[BackendRuntime]:
        """Start and authenticate the backend on a worker thread."""

        with self._state_lock:
            rejected = self._reject_if_disposed_locked("start")
            if rejected is not None:
                return rejected
            if self._state is RuntimeState.READY and self._host.is_running:
                runtime = self._host.runtime
                if runtime is not None:
                    return _completed_future(runtime)
            if self._start_future is not None and not self._start_future.done():
                return self._start_future
            if self._state is RuntimeState.STOPPING:
                return self._rejected_future_locked(
                    "start",
                    _ControllerStateError(
                        "Backend is stopping; retry after it reaches the stopped state."
                    ),
                )
            self._poll_stop.clear()
            self._set_state_locked(RuntimeState.STARTING, "start")
            future = self._executor.submit(self._start_worker)
            self._start_future = future
            future.add_done_callback(self._clear_start_future)
            return future

    def health(self) -> Future[JsonObject]:
        """Fetch backend health without blocking the calling thread."""

        return self._submit_ready_operation("health", self._health_worker)

    def configure_credentials(
        self,
        dash_scope_api_key: str,
        api_url: str | None = None,
    ) -> Future[JsonObject]:
        """Configure in-memory credentials without retaining the API key here."""

        return self._submit_ready_operation(
            "configure_credentials",
            lambda client: client.configure_credentials(dash_scope_api_key, api_url),
        )

    def list_jobs(
        self,
        *,
        active: bool | None = None,
        limit: int = 100,
    ) -> Future[JsonObject]:
        """List and validate jobs on a worker thread."""

        def work(client: BackendApiClient) -> JsonObject:
            payload = client.list_jobs(active=active, limit=limit)
            raw_jobs = payload["jobs"]
            assert isinstance(raw_jobs, list)
            jobs = tuple(_validated_job(job) for job in raw_jobs)
            copied_payload = _copy_json(payload)
            self._emit(
                RuntimeEventKind.JOBS_LISTED,
                "list_jobs",
                jobs=jobs,
                payload=copied_payload,
            )
            return copied_payload

        return self._submit_ready_operation("list_jobs", work)

    def submit_job(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
        *,
        wait_for_completion: bool = True,
        poll_interval: float | None = None,
    ) -> Future[JsonObject]:
        """Submit a job and optionally poll until it pauses or becomes terminal."""

        interval = self._validated_poll_interval(poll_interval)
        # Snapshot caller-owned parameters before handing them to another thread.
        # Search forms often reuse and mutate one dictionary for the next request.
        params_snapshot = copy.deepcopy(dict(params)) if params is not None else None

        def work(client: BackendApiClient) -> JsonObject:
            job = _validated_job(client.submit_job(command, params_snapshot))
            self._emit(RuntimeEventKind.JOB_UPDATED, "submit_job", job=job)
            if not wait_for_completion or _job_stops_polling(job):
                return job
            return self._poll_job_worker(
                client,
                job["id"],
                interval,
                operation="submit_job",
                initial_job=job,
            )

        return self._submit_ready_operation("submit_job", work)

    def poll_job(
        self,
        job_id: str,
        *,
        poll_interval: float | None = None,
    ) -> Future[JsonObject]:
        """Poll one job without occupying Tk's event loop."""

        interval = self._validated_poll_interval(poll_interval)
        return self._submit_ready_operation(
            "poll_job",
            lambda client: self._poll_job_worker(
                client,
                job_id,
                interval,
                operation="poll_job",
            ),
        )

    def cancel_job(self, job_id: str) -> Future[JsonObject]:
        """Request remote cancellation while other workers continue polling."""

        def work(client: BackendApiClient) -> JsonObject:
            job = _validated_job(client.cancel_job(job_id), expected_job_id=job_id)
            self._emit(RuntimeEventKind.JOB_UPDATED, "cancel_job", job=job)
            return job

        return self._submit_ready_operation("cancel_job", work)

    def stop(self, *, force: bool = False) -> Future[None]:
        """Safely stop the backend on a worker, preserving active work by default."""

        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        with self._state_lock:
            rejected = self._reject_if_disposed_locked("stop")
            if rejected is not None:
                return rejected
            if self._state is RuntimeState.STOPPED and not self._host.is_running:
                return _completed_future(None)
            if self._stop_future is not None and not self._stop_future.done():
                return self._stop_future
            start_future = self._start_future
            self._set_state_locked(RuntimeState.STOPPING, "stop")
            future = self._executor.submit(
                self._stop_worker,
                force,
                start_future,
            )
            self._stop_future = future
            future.add_done_callback(self._clear_stop_future)
            return future

    def dispose(self, *, force: bool = False) -> Future[None]:
        """Stop safely and release worker threads after successful shutdown.

        If the backend reports active jobs, the returned future fails with a
        recoverable ``busy`` error and the controller remains usable.  Callers can
        cancel/wait for those jobs and retry, or explicitly pass ``force=True``.
        """

        with self._state_lock:
            if self._disposed:
                return _completed_future(None)
        future = self.stop(force=force)

        def finish_dispose(completed: Future[None]) -> None:
            if completed.cancelled() or completed.exception() is not None:
                return
            with self._state_lock:
                self._disposed = True
                self._poll_stop.set()
                self._set_state_locked(RuntimeState.DISPOSED, "dispose")
            # ``wait=False`` is required because this callback commonly executes on
            # one of the executor's own workers.
            self._executor.shutdown(wait=False, cancel_futures=True)

        future.add_done_callback(finish_dispose)
        return future

    close = dispose

    def _start_worker(self) -> BackendRuntime:
        try:
            runtime = self._host.start()
        except Exception as exc:
            with self._state_lock:
                if self._state is not RuntimeState.STOPPING:
                    self._set_state_locked(RuntimeState.ERROR, "start")
            raise self._operation_error("start", exc) from exc
        with self._state_lock:
            if self._state is RuntimeState.STARTING:
                self._set_state_locked(RuntimeState.READY, "start")
        self._emit(RuntimeEventKind.RUNTIME_READY, "start", runtime=runtime)
        return runtime

    def _health_worker(self, client: BackendApiClient) -> JsonObject:
        payload = _copy_json(client.get_health())
        self._emit(RuntimeEventKind.HEALTH_UPDATED, "health", payload=payload)
        return payload

    def _stop_worker(
        self,
        force: bool,
        start_future: Future[BackendRuntime] | None,
    ) -> None:
        try:
            if start_future is not None and not start_future.done():
                start_future.result()
            self._host.stop(force=force)
        except Exception as exc:
            with self._state_lock:
                next_state = (
                    RuntimeState.READY if self._host.is_running else RuntimeState.ERROR
                )
                self._set_state_locked(next_state, "stop")
            raise self._operation_error("stop", exc) from exc
        self._poll_stop.set()
        with self._state_lock:
            self._set_state_locked(RuntimeState.STOPPED, "stop")

    def _submit_ready_operation(
        self,
        operation: str,
        work: Callable[[BackendApiClient], _T],
    ) -> Future[_T]:
        with self._state_lock:
            rejected = self._reject_if_disposed_locked(operation)
            if rejected is not None:
                return rejected
            if self._state is not RuntimeState.READY or not self._host.is_running:
                if self._state is RuntimeState.READY and not self._host.is_running:
                    self._set_state_locked(RuntimeState.ERROR, operation)
                return self._rejected_future_locked(
                    operation,
                    _ControllerStateError(
                        "Backend is not ready. Start it and wait for the ready event."
                    ),
                )
        return self._executor.submit(self._run_ready_operation, operation, work)

    def _run_ready_operation(
        self,
        operation: str,
        work: Callable[[BackendApiClient], _T],
    ) -> _T:
        try:
            return work(self._host.client)
        except _PollingStopped as exc:
            raise self._operation_error(operation, exc, emit=False) from exc
        except RuntimeOperationError:
            raise
        except Exception as exc:
            if not self._host.is_running and isinstance(
                exc, (BackendHostError, BackendTransportError)
            ):
                with self._state_lock:
                    self._set_state_locked(RuntimeState.ERROR, operation)
            raise self._operation_error(operation, exc) from exc

    def _poll_job_worker(
        self,
        client: BackendApiClient,
        job_id: str,
        interval: float,
        *,
        operation: str,
        initial_job: JsonObject | None = None,
    ) -> JsonObject:
        normalized_job_id = _validated_job_id(job_id)
        last_job = initial_job
        transient_failures = 0
        while True:
            if self._poll_stop.is_set():
                raise _PollingStopped(
                    "Local polling stopped because the backend controller stopped."
                )
            try:
                job = _validated_job(
                    client.get_job(normalized_job_id),
                    expected_job_id=normalized_job_id,
                )
                transient_failures = 0
            except (BackendTimeoutError, BackendTransportError) as exc:
                if transient_failures >= self._transient_retries:
                    raise
                transient_failures += 1
                self._emit_error(
                    operation,
                    exc,
                    kind=RuntimeEventKind.WARNING,
                    recoverable=True,
                )
                if self._poll_stop.wait(interval):
                    raise _PollingStopped(
                        "Local polling stopped because the backend controller stopped."
                    ) from exc
                continue

            # Equality coalescing prevents a long-running unchanged job from filling
            # the UI event queue several times per second.
            if last_job != job or _job_stops_polling(job):
                self._emit(RuntimeEventKind.JOB_UPDATED, operation, job=job)
                last_job = job
            if _job_stops_polling(job):
                return job
            if self._poll_stop.wait(interval):
                raise _PollingStopped(
                    "Local polling stopped because the backend controller stopped."
                )

    def _validated_poll_interval(self, value: float | None) -> float:
        if value is None:
            return self._poll_interval
        return _positive_finite(value, "poll_interval")

    def _operation_error(
        self,
        operation: str,
        exc: Exception,
        *,
        emit: bool = True,
    ) -> RuntimeOperationError:
        info = _error_info(operation, exc)
        if emit:
            self._emit(
                RuntimeEventKind.ERROR,
                operation,
                error=info,
            )
        return RuntimeOperationError(info)

    def _emit_error(
        self,
        operation: str,
        exc: Exception,
        *,
        kind: RuntimeEventKind,
        recoverable: bool | None = None,
    ) -> None:
        info = _error_info(operation, exc, recoverable=recoverable)
        self._emit(kind, operation, error=info)

    def _emit(
        self,
        kind: RuntimeEventKind,
        operation: str,
        *,
        runtime: BackendRuntime | None = None,
        job: JsonObject | None = None,
        jobs: tuple[JsonObject, ...] = (),
        payload: JsonObject | None = None,
        error: RuntimeErrorInfo | None = None,
    ) -> None:
        event = RuntimeEvent(
            kind=kind,
            state=self.state,
            operation=operation,
            occurred_at=datetime.now(timezone.utc),
            runtime=runtime,
            job=_copy_json(job) if job is not None else None,
            jobs=tuple(_copy_json(item) for item in jobs),
            payload=_copy_json(payload) if payload is not None else None,
            error=error,
        )
        with self._event_lock:
            self._events.append(event)

    def _set_state_locked(self, state: RuntimeState, operation: str) -> None:
        if self._state is state:
            return
        self._state = state
        event = RuntimeEvent(
            kind=RuntimeEventKind.STATE_CHANGED,
            state=state,
            operation=operation,
            occurred_at=datetime.now(timezone.utc),
        )
        with self._event_lock:
            self._events.append(event)

    def _reject_if_disposed_locked(self, operation: str) -> Future[Any] | None:
        if not self._disposed:
            return None
        return self._rejected_future_locked(
            operation,
            _ControllerStateError("Backend controller has been disposed."),
        )

    def _rejected_future_locked(
        self,
        operation: str,
        exc: Exception,
    ) -> Future[Any]:
        error = self._operation_error(operation, exc)
        future: Future[Any] = Future()
        future.set_exception(error)
        return future

    def _clear_start_future(self, future: Future[BackendRuntime]) -> None:
        with self._state_lock:
            if self._start_future is future:
                self._start_future = None

    def _clear_stop_future(self, future: Future[None]) -> None:
        with self._state_lock:
            if self._stop_future is future:
                self._stop_future = None


def _validated_job(
    value: Any,
    *,
    expected_job_id: str | None = None,
) -> JsonObject:
    if not isinstance(value, dict):
        raise BackendProtocolError(
            "GET",
            "backend-job",
            "the job response must be a JSON object",
        )
    job_id = value.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise BackendProtocolError(
            "GET",
            "backend-job",
            "the job response did not contain a non-empty ID",
        )
    if expected_job_id is not None and job_id != expected_job_id:
        raise BackendProtocolError(
            "GET",
            "backend-job",
            f"the backend returned job {job_id!r} instead of {expected_job_id!r}",
        )
    command = value.get("command")
    if not isinstance(command, str) or not command:
        raise BackendProtocolError(
            "GET",
            "backend-job",
            f"backend job {job_id!r} did not contain a command",
        )
    status = value.get("status")
    if not isinstance(status, str) or status.lower() not in _VALID_JOB_STATUSES:
        raise BackendProtocolError(
            "GET",
            "backend-job",
            f"backend job {job_id!r} returned unknown status {status!r}",
        )
    return _copy_json(value)


def _validated_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be non-empty")
    return job_id.strip()


def _job_stops_polling(job: Mapping[str, Any]) -> bool:
    status = job.get("status")
    return isinstance(status, str) and status.lower() in _POLL_STOP_STATUSES


def _copy_json(value: Mapping[str, Any]) -> JsonObject:
    # Backend data is JSON-compatible. ``deepcopy`` also keeps this helper usable
    # with the lightweight dictionaries supplied by unit-test fakes.
    return copy.deepcopy(dict(value))


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _completed_future(value: _T) -> Future[_T]:
    future: Future[_T] = Future()
    future.set_result(value)
    return future


def _error_info(
    operation: str,
    exc: Exception,
    *,
    recoverable: bool | None = None,
) -> RuntimeErrorInfo:
    category = RuntimeErrorCategory.INTERNAL
    code: str | None = None
    status_code: int | None = None
    details: Any = None
    default_recoverable = False

    if isinstance(exc, BackendBusyError):
        category = RuntimeErrorCategory.BUSY
        details = {"active_job_ids": list(exc.active_job_ids)}
        default_recoverable = True
    elif isinstance(exc, BackendConfigurationError):
        category = RuntimeErrorCategory.CONFIGURATION
        default_recoverable = True
    elif isinstance(exc, BackendStartupError):
        category = RuntimeErrorCategory.STARTUP
        default_recoverable = True
    elif isinstance(exc, BackendTimeoutError):
        category = RuntimeErrorCategory.TIMEOUT
        default_recoverable = True
    elif isinstance(exc, BackendTransportError):
        category = RuntimeErrorCategory.TRANSPORT
        default_recoverable = True
    elif isinstance(exc, BackendHttpError):
        category = RuntimeErrorCategory.HTTP
        code = exc.code
        status_code = exc.status_code
        details = exc.details
        default_recoverable = True
    elif isinstance(exc, BackendProtocolError):
        category = RuntimeErrorCategory.PROTOCOL
        status_code = exc.status_code
        default_recoverable = True
    elif isinstance(exc, _ControllerStateError):
        if "disposed" in str(exc).lower():
            category = RuntimeErrorCategory.DISPOSED
            default_recoverable = False
        else:
            category = RuntimeErrorCategory.NOT_READY
            default_recoverable = True
    elif isinstance(exc, _PollingStopped):
        category = RuntimeErrorCategory.NOT_READY
        default_recoverable = True
    elif isinstance(exc, ValueError):
        category = RuntimeErrorCategory.VALIDATION
        default_recoverable = True
    elif isinstance(exc, BackendHostError):
        category = RuntimeErrorCategory.HOST
        default_recoverable = True
    elif isinstance(exc, BackendApiError):
        category = RuntimeErrorCategory.PROTOCOL
        default_recoverable = True

    return RuntimeErrorInfo(
        operation=operation,
        category=category,
        message=str(exc) or type(exc).__name__,
        recoverable=default_recoverable if recoverable is None else recoverable,
        exception_type=type(exc).__name__,
        code=code,
        status_code=status_code,
        details=copy.deepcopy(details),
    )


__all__ = [
    "BackendRuntimeController",
    "RuntimeErrorCategory",
    "RuntimeErrorInfo",
    "RuntimeEvent",
    "RuntimeEventKind",
    "RuntimeOperationError",
    "RuntimeState",
]
