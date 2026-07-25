from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from time import monotonic, perf_counter
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from .activity_store import ActivityStore
from .backend_instance_lock import BackendInstanceLock
from .config import RuntimeCredentials, ServiceConfig, default_config_home
from .federated_search import LibraryCandidateSet, export_federated_search
from .large_cluster_adapter import DEFAULT_CLUSTER_TYPES
from .large_library_policy import (
    build_large_library_policy_snapshot,
    canonical_large_library_policy_sha256,
)
from .library_browser import LibraryBrowser
from .library_config import (
    LibraryCatalog,
    LibraryDefinition,
    load_library_catalog,
)
from .models import SearchReport
from .rank_fusion import confidence_candidate_limit
from .result_exporter import search_report_payload
from .service import ImageVectorService

ServiceFactory = Callable[[Callable[[str], None], Callable[[], None]], Any]
LibraryServiceFactory = Callable[
    [LibraryDefinition, Callable[[str], None], Callable[[], None]], Any
]

_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)
_JOB_PATH = re.compile(r"^/v1/jobs/([0-9a-f]{32})/?$")
_PROGRESS_NUMBERS = re.compile(r"(?P<current>\d+)\s*/\s*(?P<total>\d+)")
_MAX_REQUEST_BYTES = 1024 * 1024
_PROTOCOL_VERSION = 2
_ACTIVITY_JOB_HISTORY_EXCLUDED_COMMANDS = frozenset(
    {
        "search",
        "stats",
        "roots",
        "libraries",
        "folder_list",
        "folder_images",
        "folder_delete_preview",
        "auto_tag_estimate",
        "auto_tag_pending",
        "tag_alias_list",
    }
)
_ACTIVITY_PROGRESS_INTERVAL_SECONDS = 0.75
_DEFAULT_LIBRARY_QUEUE_CAPACITY = 64
_MAX_LIBRARY_QUEUE_CAPACITY = 10_000
_QUEUE_RETRY_AFTER_SECONDS = 1
_DEFAULT_COOPERATIVE_BATCH_SIZE = 200
_MAX_COOPERATIVE_BATCH_SIZE = 10_000
_DEFAULT_COOPERATIVE_MAX_INTERVAL_SECONDS = 0.25
_DEFAULT_COOPERATIVE_MAX_CALLS = 4
_DEFAULT_IDLE_MAINTENANCE_DELAY_SECONDS = 2.0
_DEFAULT_RESULT_PREVIEW_PAGE_SIZE = 15
_CAPABILITIES = {
    "multi_library": True,
    "federated_search": True,
    "session_credentials": True,
    "low_confidence_override": True,
    "folder_tags": True,
    "folder_browser": True,
    "folder_delete_two_phase": True,
    "manual_tag_batch": True,
    "manual_tag_undo": True,
    "search_results_cleanup": True,
    "fuzzy_tag_search": True,
    "tag_only_search": True,
    "hybrid_tag_vector_search": True,
    "metadata_embedding_search": True,
    "result_diversity": True,
    "search_sort_modes": ["confidence", "relevance", "diverse", "legacy"],
    "index_and_auto_tag": True,
    "auto_tagging": True,
    "auto_tag_pending": True,
    "auto_tag_review": True,
    "auto_tag_review_batch": True,
    "auto_tag_identity_batch_review": True,
    "auto_tag_review_undo": True,
    "auto_tag_policy_migrate": True,
    "metadata_embedding_backfill": True,
    "tag_alias_list": True,
    "tag_alias_upsert": True,
    "tag_alias_delete": True,
    "image_clustering": True,
    "incremental_clustering": True,
    "cluster_manual_merge_split": True,
    "cluster_identity_propagation": True,
    "cluster_operation_undo": True,
    "active_learning": True,
    "active_learning_review_undo": True,
    "learning_external_api_requests": 0,
    "concurrent_jobs": True,
    "job_list": True,
    "partial_jobs": True,
    "persistent_backend_session": True,
    "graceful_shutdown": True,
    "bounded_priority_queue": True,
    "cooperative_batch_scheduling": True,
    "source_only_search_results": True,
    "large_library_policy_diagnostics": True,
    # Reserved protocol flags.  The first concurrent-jobs release exposes the
    # state names to clients, while pause/resume and failure paging remain off.
    "job_pause_resume": False,
    "failure_paging": False,
}

try:
    _APP_VERSION = version("zvec-image-search")
except PackageNotFoundError:
    _APP_VERSION = "0.4.0"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class BackendRequestError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            error["details"] = self.details
        return error


class JobCancelled(RuntimeError):
    pass


class _JobPriority(IntEnum):
    HIGH = 0
    INTERACTIVE = 1
    BATCH = 2


_PRIORITY_LABELS = {
    _JobPriority.HIGH: "high",
    _JobPriority.INTERACTIVE: "interactive",
    _JobPriority.BATCH: "batch",
}

# Weighted scheduling keeps interactive search responsive without starving a
# previously queued batch forever. FIFO order is preserved inside each lane.
_PRIORITY_SCHEDULE = (
    _JobPriority.HIGH,
    _JobPriority.HIGH,
    _JobPriority.HIGH,
    _JobPriority.HIGH,
    _JobPriority.INTERACTIVE,
    _JobPriority.INTERACTIVE,
    _JobPriority.BATCH,
)
_HIGH_PRIORITY_COMMANDS = frozenset({"search", "folder_list", "folder_images"})
_BATCH_PRIORITY_COMMANDS = frozenset(
    {
        "index",
        "sync",
        "index_and_auto_tag",
        "auto_tag",
        "auto_tag_policy_migrate",
        "folder_tag_backfill",
        "metadata_backfill",
        "cluster_images",
        "active_learning_queue",
    }
)


class _LibraryQueueFull(RuntimeError):
    def __init__(self, *, library_id: str, capacity: int, depth: int) -> None:
        super().__init__(f"Library queue is full: {library_id}")
        self.library_id = library_id
        self.capacity = capacity
        self.depth = depth


@dataclass(frozen=True)
class _LibrarySubmission:
    future: Future[Any]
    queue_position: int | None


@dataclass
class BackendJob:
    id: str
    command: str
    params: dict[str, Any]
    submitted_at: str = field(default_factory=_utc_now)
    status: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    progress: dict[str, Any] | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "params": _json_safe(self.params),
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
            "progress": _json_safe(self.progress),
            "result": _json_safe(self.result),
            "error": _json_safe(self.error),
            "failure_count": self.failure_count,
        }


@dataclass(frozen=True)
class _LibraryCall:
    job_id: str
    callback: Callable[[Any], Any]
    future: Future[Any]
    priority: _JobPriority


class _BoundedPriorityCallQueue:
    """A bounded weighted-priority queue for one Collection owner thread."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lanes: dict[_JobPriority, deque[_LibraryCall]] = {
            priority: deque() for priority in _JobPriority
        }
        self._size = 0
        self._schedule_index = 0
        self._closed = False
        self._condition = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def depth(self) -> int:
        with self._condition:
            return self._size

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def put(self, call: _LibraryCall, *, library_id: str) -> int:
        with self._condition:
            if self._closed:
                raise RuntimeError("Library worker is shutting down.")
            if self._size >= self._capacity:
                raise _LibraryQueueFull(
                    library_id=library_id,
                    capacity=self._capacity,
                    depth=self._size,
                )
            self._lanes[call.priority].append(call)
            self._size += 1
            position = self._position_locked(call.job_id)
            if position is None:  # pragma: no cover - guarded by the insertion above
                raise RuntimeError("Queued library call has no scheduling position.")
            self._condition.notify()
            return position

    def get(self, timeout: float | None = None) -> _LibraryCall | None:
        with self._condition:
            deadline = None if timeout is None else monotonic() + max(0.0, timeout)
            while self._size == 0 and not self._closed:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._size == 0:
                return None
            call, self._schedule_index = self._pop_next(
                self._lanes,
                self._schedule_index,
            )
            self._size -= 1
            return call

    def take_high_priority(self) -> _LibraryCall | None:
        """Take one queued read-only/high-priority call without blocking.

        Long batch work calls this only from the Collection owner thread.  It
        deliberately ignores interactive and batch lanes because many commands
        in those lanes mutate Collection state and therefore are not safe to
        interleave with a partially completed index transaction.
        """

        with self._condition:
            lane = self._lanes[_JobPriority.HIGH]
            if not lane:
                return None
            call = lane.popleft()
            self._size -= 1
            return call

    def position(self, job_id: str) -> int | None:
        with self._condition:
            return self._position_locked(job_id)

    def remove(self, job_id: str) -> Future[Any] | None:
        with self._condition:
            for lane in self._lanes.values():
                for call in tuple(lane):
                    if call.job_id != job_id:
                        continue
                    lane.remove(call)
                    self._size -= 1
                    return call.future
            return None

    def close(self) -> tuple[Future[Any], ...]:
        with self._condition:
            if self._closed:
                return ()
            self._closed = True
            pending = tuple(
                call.future for lane in self._lanes.values() for call in lane
            )
            for lane in self._lanes.values():
                lane.clear()
            self._size = 0
            self._condition.notify_all()
            return pending

    def _position_locked(self, job_id: str) -> int | None:
        lanes = {priority: deque(values) for priority, values in self._lanes.items()}
        schedule_index = self._schedule_index
        for position in range(1, self._size + 1):
            call, schedule_index = self._pop_next(lanes, schedule_index)
            if call.job_id == job_id:
                return position
        return None

    @staticmethod
    def _pop_next(
        lanes: dict[_JobPriority, deque[_LibraryCall]],
        schedule_index: int,
    ) -> tuple[_LibraryCall, int]:
        for offset in range(len(_PRIORITY_SCHEDULE)):
            index = (schedule_index + offset) % len(_PRIORITY_SCHEDULE)
            priority = _PRIORITY_SCHEDULE[index]
            lane = lanes[priority]
            if lane:
                return lane.popleft(), (index + 1) % len(_PRIORITY_SCHEDULE)
        raise RuntimeError("Priority queue size is inconsistent with its lanes.")


def _job_priority(command: str) -> _JobPriority:
    if command in _HIGH_PRIORITY_COMMANDS:
        return _JobPriority.HIGH
    if command in _BATCH_PRIORITY_COMMANDS:
        return _JobPriority.BATCH
    return _JobPriority.INTERACTIVE


def _resolve_library_queue_capacity(value: int | None) -> int:
    candidate: int | str
    if value is None:
        configured = os.getenv("ZVEC_LIBRARY_QUEUE_CAPACITY", "").strip()
        candidate = configured or _DEFAULT_LIBRARY_QUEUE_CAPACITY
    else:
        candidate = value
    if isinstance(candidate, bool):
        raise ValueError("library_queue_capacity must be an integer")
    try:
        capacity = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("library_queue_capacity must be an integer") from exc
    if not 1 <= capacity <= _MAX_LIBRARY_QUEUE_CAPACITY:
        raise ValueError(
            "library_queue_capacity must be between 1 and "
            f"{_MAX_LIBRARY_QUEUE_CAPACITY}"
        )
    return capacity


def _resolve_cooperative_batch_size(value: int | None) -> int:
    candidate: int | str
    if value is None:
        configured = os.getenv("ZVEC_COOPERATIVE_BATCH_SIZE", "").strip()
        candidate = configured or _DEFAULT_COOPERATIVE_BATCH_SIZE
    else:
        candidate = value
    if isinstance(candidate, bool):
        raise ValueError("cooperative_batch_size must be an integer")
    try:
        batch_size = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("cooperative_batch_size must be an integer") from exc
    if not 1 <= batch_size <= _MAX_COOPERATIVE_BATCH_SIZE:
        raise ValueError(
            "cooperative_batch_size must be between 1 and "
            f"{_MAX_COOPERATIVE_BATCH_SIZE}"
        )
    return batch_size


def _queue_full_error(
    error: _LibraryQueueFull,
    message: str,
) -> BackendRequestError:
    return BackendRequestError(
        "queue_full",
        message,
        status=HTTPStatus.TOO_MANY_REQUESTS,
        details={
            "library_id": error.library_id,
            "queue_depth": error.depth,
            "queue_capacity": error.capacity,
            "retry_after_seconds": _QUEUE_RETRY_AFTER_SECONDS,
        },
    )


class _LibraryWorker:
    def __init__(
        self,
        library: LibraryDefinition,
        service_factory: LibraryServiceFactory,
        report_progress: Callable[[str, LibraryDefinition, str], None],
        check_cancel: Callable[[str], None],
        *,
        queue_capacity: int,
        cooperative_batch_size: int,
    ) -> None:
        self.library = library
        self._service_factory = service_factory
        self._report_job_progress = report_progress
        self._check_job_cancel = check_cancel
        self._queue = _BoundedPriorityCallQueue(queue_capacity)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._startup_error: dict[str, Any] | None = None
        self._recovery_report: dict[str, Any] | None = None
        self._service: Any = None
        self._current_job_id: str | None = None
        self._current_priority: _JobPriority | None = None
        self._cooperative_batch_size = cooperative_batch_size
        self._cooperative_checkpoints = 0
        self._cooperative_last_yield = monotonic()
        self._cooperative_depth = 0
        self._watcher: Any = None
        self._thread = threading.Thread(
            target=self._main,
            name=f"zvec-library-{library.library_id}",
            daemon=True,
        )

    @property
    def startup_error(self) -> dict[str, Any] | None:
        return self._startup_error

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._startup_error is None

    @property
    def recovery_report(self) -> dict[str, Any] | None:
        return self._recovery_report

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def queue_depth(self) -> int:
        return self._queue.depth

    @property
    def queue_capacity(self) -> int:
        return self._queue.capacity

    def cancellation_requested(self) -> bool:
        """Expose shutdown state to optional idle Collection maintenance."""

        return self._stop.is_set()

    def queue_is_idle(self) -> bool:
        """Return true only when no user call is waiting for this Collection."""

        return self._queue.depth == 0

    def start(self) -> None:
        self._thread.start()

    def submit(
        self,
        job_id: str,
        callback: Callable[[Any], Any],
        *,
        priority: _JobPriority,
        future: Future[Any] | None = None,
    ) -> _LibrarySubmission:
        submitted_future = future or Future()
        if self._stop.is_set():
            submitted_future.set_exception(
                RuntimeError("Library worker is shutting down.")
            )
            return _LibrarySubmission(submitted_future, None)
        if self._startup_error is not None:
            submitted_future.set_exception(
                RuntimeError(
                    str(self._startup_error.get("message") or "startup failed")
                )
            )
            return _LibrarySubmission(submitted_future, None)
        position = self._queue.put(
            _LibraryCall(job_id, callback, submitted_future, priority),
            library_id=self.library.library_id,
        )
        return _LibrarySubmission(submitted_future, position)

    def queue_position(self, job_id: str) -> int | None:
        return self._queue.position(job_id)

    def cancel_queued(self, job_id: str) -> bool:
        future = self._queue.remove(job_id)
        if future is None:
            return False
        future.cancel()
        return True

    def close(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            with suppress(Exception):
                self._watcher.stop()
            self._watcher = None
        for future in self._queue.close():
            future.cancel()
        if self._thread.ident is None:
            return
        self._thread.join()

    def _start_file_watcher(self, service: Any) -> None:
        from .file_watcher import FileChangeWatcher
        from .logical_paths import normalize_path

        image_root = self.library.image_root
        if image_root is None:
            return
        normalized = normalize_path(image_root)
        root_info = service.state.root_for_path(normalized)
        if root_info is None:
            return
        root_id = str(root_info["root_id"])
        recursive = bool(root_info["recursive"])
        self._watcher = FileChangeWatcher(
            debounce_seconds=service.config.watcher_debounce_seconds,
            on_changes_settled=self._on_changes_settled,
            enqueue_change=service.state.enqueue_change,
        )
        self._watcher.start([(root_id, Path(normalized), recursive)])

    def _on_changes_settled(self, root_id: str) -> None:
        if self._stop.is_set() or not self._ready.is_set():
            return
        if self._startup_error is not None:
            return
        job_id = f"auto-index-{root_id}-{int(monotonic() * 1000)}"

        def callback(service: Any) -> Any:
            root_path = service.state.root_path(root_id)
            if root_path is None:
                return {}
            return service.index_and_auto_tag_incremental(
                folder_path=root_path,
                root_id=root_id,
            )

        try:
            self.submit(job_id, callback, priority=_JobPriority.BATCH)
        except Exception:
            pass

    def _main(self) -> None:
        service: Any = None
        try:
            service = self._service_factory(
                self.library,
                self._report_progress,
                self._check_cancel,
            )
            self._service = service
            configure_maintenance = getattr(
                service,
                "configure_optimize_runtime",
                None,
            )
            if callable(configure_maintenance):
                configure_maintenance(self, externally_managed=True)
            recover = getattr(service, "recover_folder_deletions", None)
            if callable(recover):
                recovery = recover(library_id=self.library.library_id)
                if isinstance(recovery, dict):
                    self._recovery_report = _json_safe(recovery)
            if (
                getattr(self.library, "auto_index_enabled", False)
                and self.library.image_root is not None
            ):
                self._start_file_watcher(service)
            self._ready.set()
            while True:
                timeout = self._idle_maintenance_wait_timeout(service)
                call = self._queue.get(timeout=timeout)
                if call is None:
                    if self._stop.is_set() or self._queue.closed:
                        return
                    self._run_idle_maintenance(service)
                    continue
                self._execute_call(call, service)
        except Exception as exc:
            self._startup_error = {
                "code": "service_initialization_failed",
                "message": str(exc) or exc.__class__.__name__,
                "details": {
                    "type": exc.__class__.__name__,
                    "library_id": self.library.library_id,
                },
            }
            self._ready.set()
            self._fail_pending(exc)
        finally:
            if self._watcher is not None:
                with suppress(Exception):
                    self._watcher.stop()
                self._watcher = None
            if service is not None:
                with suppress(Exception):
                    service.close()
            self._service = None

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._queue.close():
            if not future.done():
                future.set_exception(error)

    def _run_idle_maintenance(self, service: Any) -> None:
        if not self.queue_is_idle():
            return
        pending = getattr(service, "has_pending_optimize_maintenance", None)
        if callable(pending):
            try:
                if not pending():
                    return
            except Exception:
                return
        maintenance = getattr(service, "run_idle_maintenance", None)
        if callable(maintenance):
            # The user Future was completed by _execute_call before this point.
            # Maintenance is best effort and must never terminate the worker.
            with suppress(Exception):
                maintenance()

    @staticmethod
    def _idle_maintenance_wait_timeout(service: Any) -> float | None:
        wait_hint = getattr(service, "optimize_maintenance_wait_seconds", None)
        if callable(wait_hint):
            try:
                seconds = wait_hint()
                if seconds is None:
                    return None
                normalized = max(0.0, float(seconds))
                return (
                    _DEFAULT_IDLE_MAINTENANCE_DELAY_SECONDS
                    if normalized == 0
                    else normalized
                )
            except Exception:
                return None
        pending = getattr(service, "has_pending_optimize_maintenance", None)
        if not callable(pending):
            return None
        try:
            if pending():
                return _DEFAULT_IDLE_MAINTENANCE_DELAY_SECONDS
        except Exception:
            return None
        return None

    def _report_progress(self, message: str) -> None:
        self._check_cancel()
        if self._current_job_id is not None:
            self._report_job_progress(self._current_job_id, self.library, message)

    def _check_cancel(self) -> None:
        if self._stop.is_set():
            raise JobCancelled("Library worker shutdown requested.")
        if self._current_job_id is not None:
            self._check_job_cancel(self._current_job_id)
        self._cooperate_if_due()
        if self._current_job_id is not None:
            # A queued search can take long enough for cancellation to arrive
            # while the outer batch is suspended.  Recheck before resuming it.
            self._check_job_cancel(self._current_job_id)

    def _execute_call(self, call: _LibraryCall, service: Any) -> None:
        previous_job_id = self._current_job_id
        previous_priority = self._current_priority
        self._current_job_id = call.job_id
        self._current_priority = call.priority
        try:
            if not call.future.set_running_or_notify_cancel():
                return
            self._check_cancel()
            call.future.set_result(call.callback(service))
        except BaseException as exc:
            if not call.future.done():
                call.future.set_exception(exc)
        finally:
            self._current_job_id = previous_job_id
            self._current_priority = previous_priority

    def _cooperate_if_due(self) -> None:
        """Run bounded urgent reads between safe checkpoints of a batch job.

        Every callback still executes on this worker's single owner thread.  A
        time threshold keeps model/network waits responsive, while the work-unit
        threshold avoids taking the queue lock for every file in a fast scan.
        """

        if (
            self._cooperative_depth
            or self._current_priority is not _JobPriority.BATCH
            or threading.get_ident() != self._thread.ident
        ):
            return
        self._cooperative_checkpoints += 1
        now = monotonic()
        if (
            self._cooperative_checkpoints < self._cooperative_batch_size
            and now - self._cooperative_last_yield
            < _DEFAULT_COOPERATIVE_MAX_INTERVAL_SECONDS
        ):
            return
        self._cooperative_checkpoints = 0
        self._cooperative_last_yield = now
        self._cooperative_depth += 1
        try:
            for _index in range(_DEFAULT_COOPERATIVE_MAX_CALLS):
                call = self._queue.take_high_priority()
                if call is None:
                    break
                self._execute_call(call, self._service)
        finally:
            self._cooperative_depth -= 1


class BackendJobManager:
    """Coordinates jobs while each library service stays on its own worker thread."""

    def __init__(
        self,
        *,
        instance_id: str,
        config_fingerprint: str,
        config: ServiceConfig | None = None,
        query_root: str | Path | None = None,
        service_factory: ServiceFactory | None = None,
        library_service_factory: LibraryServiceFactory | None = None,
        library_catalog: LibraryCatalog | None = None,
        activity_store: ActivityStore | None = None,
        owns_activity_store: bool = False,
        library_queue_capacity: int | None = None,
        cooperative_batch_size: int | None = None,
    ) -> None:
        if owns_activity_store and activity_store is None:
            raise ValueError("owns_activity_store requires an activity_store")
        self._instance_id = instance_id
        self._config_fingerprint = config_fingerprint
        self._config = config or ServiceConfig()
        self._credentials = self._config.runtime_credentials or RuntimeCredentials()
        self._config = replace(self._config, runtime_credentials=self._credentials)
        # Keep the native default inside the selected workspace. Desktop callers pass
        # an isolated per-process staging directory explicitly.
        self._query_root = Path(
            query_root
            if query_root is not None
            else self._config.workspace / "query-staging"
        ).expanduser()
        self._catalog = library_catalog or load_library_catalog(None, self._config)
        self._jobs: dict[str, BackendJob] = {}
        self._jobs_lock = threading.RLock()
        self._activity = activity_store
        self._owns_activity = owns_activity_store
        self._activity_lock = threading.Lock()
        self._activity_progress_times: dict[str, float] = {}
        self._activity_health_state: str | None = None
        self._stop_requested = threading.Event()
        self._started = False
        self._closed = False
        self._library_queue_capacity = _resolve_library_queue_capacity(
            library_queue_capacity
        )
        self._cooperative_batch_size = _resolve_cooperative_batch_size(
            cooperative_batch_size
        )
        factory = library_service_factory or self._adapt_service_factory(
            service_factory
        )
        self._library_workers = {
            library.library_id: _LibraryWorker(
                library,
                factory,
                self._report_progress,
                self._check_cancel,
                queue_capacity=self._library_queue_capacity,
                cooperative_batch_size=self._cooperative_batch_size,
            )
            for library in self._catalog.enabled
        }
        # Single-library calls run directly on their Collection-owned worker.
        # This executor is only for control-plane and federated jobs that must
        # coordinate more than one Collection without blocking HTTP threads.
        self._control_executor = ThreadPoolExecutor(
            max_workers=max(2, len(self._library_workers)),
            thread_name_prefix="zvec-backend-control",
        )
        self._job_futures: dict[str, Future[Any]] = {}
        self._job_worker_ids: dict[str, str] = {}

    def _adapt_service_factory(
        self, service_factory: ServiceFactory | None
    ) -> LibraryServiceFactory:
        if service_factory is not None:
            return lambda _library, progress, cancel: service_factory(progress, cancel)

        def create(
            library: LibraryDefinition,
            progress: Callable[[str], None],
            cancel_check: Callable[[], None],
        ) -> ImageVectorService:
            config = replace(
                self._config,
                workspace=library.workspace,
                library_id=library.library_id,
                library_image_root=library.image_root,
                results_directory=self._catalog.federated_results_directory,
            )
            return ImageVectorService(
                config=config,
                progress=progress,
                cancel_check=cancel_check,
            )

        return create

    def _activity_log(
        self,
        *,
        level: Literal["debug", "info", "warning", "error"],
        event: str,
        message: str,
        category: str = "backend",
        library_id: str | None = None,
        library_name: str | None = None,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Persist one bounded backend event without affecting primary work."""

        activity = self._activity
        if activity is None:
            return
        with suppress(Exception):
            activity.log(
                level=level,
                category=category,
                event=event,
                source="backend",
                message=message,
                library_id=library_id,
                library_name=library_name,
                job_id=job_id,
                operation_id=job_id,
                details=details or {},
            )

    def _job_library_context(
        self, snapshot: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        params = snapshot.get("params")
        params = params if isinstance(params, dict) else {}
        library_id = params.get("library_id") or self._catalog.default_library_id
        if not isinstance(library_id, str) or not library_id:
            return None, None
        library = self._catalog.by_id.get(library_id)
        return library_id, library.name if library is not None else None

    def _record_job_history(
        self,
        snapshot: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        """Best-effort durable snapshot; high-frequency progress is throttled."""

        activity = self._activity
        command = str(snapshot.get("command") or "")
        job_id = str(snapshot.get("id") or "")
        if (
            activity is None
            or not command
            or not job_id
            or command in _ACTIVITY_JOB_HISTORY_EXCLUDED_COMMANDS
        ):
            return
        status = str(snapshot.get("status") or "unknown")
        now = monotonic()
        with self._activity_lock:
            if not force and status == "running":
                previous = self._activity_progress_times.get(job_id, 0.0)
                if now - previous < _ACTIVITY_PROGRESS_INTERVAL_SECONDS:
                    return
                self._activity_progress_times[job_id] = now
            if status in _TERMINAL_STATUSES:
                self._activity_progress_times.pop(job_id, None)
        library_id, library_name = self._job_library_context(snapshot)
        record = dict(snapshot)
        record["library_id"] = library_id
        record["library_name"] = library_name
        with suppress(Exception):
            activity.record_job(record)

    def _record_image_failures(self, snapshot: dict[str, Any]) -> None:
        """Persist bounded per-image failures even when no UI is connected."""

        result = snapshot.get("result")
        result = result if isinstance(result, dict) else {}
        raw_failures = result.get("error_images")
        if not isinstance(raw_failures, list):
            raw_failures = result.get("failures")
        if not isinstance(raw_failures, list):
            return
        library_id, library_name = self._job_library_context(snapshot)
        job_id = str(snapshot.get("id") or "") or None
        task_type = str(snapshot.get("command") or "unknown")
        logged = 0
        for item in raw_failures[:200]:
            if not isinstance(item, dict):
                continue
            relative_path = _safe_activity_relative_path(item.get("relative_path"))
            details: dict[str, Any] = {
                "task_type": task_type,
                "name": str(item.get("name") or item.get("file_name") or ""),
                "reason": str(
                    item.get("reason")
                    or item.get("error")
                    or item.get("message")
                    or item.get("code")
                    or ""
                ),
            }
            if relative_path:
                details["relative_path"] = relative_path
            self._activity_log(
                level="warning",
                category="image_failure",
                event="image_failed",
                message=str(
                    item.get("reason")
                    or item.get("error")
                    or item.get("message")
                    or item.get("code")
                    or "One image failed; the batch continued."
                ),
                library_id=library_id,
                library_name=library_name,
                job_id=job_id,
                details=details,
            )
            logged += 1
        if len(raw_failures) > logged:
            self._activity_log(
                level="warning",
                category="image_failure",
                event="image_failure_log_truncated",
                message="Additional image failures remain in the task result manifest.",
                library_id=library_id,
                library_name=library_name,
                job_id=job_id,
                details={
                    "task_type": task_type,
                    "logged_count": logged,
                    "omitted_count": len(raw_failures) - logged,
                },
            )

    def _record_health_transition(
        self,
        status: str,
        *,
        error_library_ids: list[str],
        worker_alive: bool,
    ) -> None:
        with self._activity_lock:
            if self._activity_health_state == status:
                return
            self._activity_health_state = status
        level: Literal["info", "warning", "error"] = (
            "error" if status == "degraded" else "info"
        )
        event = {
            "ok": "backend_workers_ready",
            "starting": "backend_workers_starting",
            "degraded": "backend_workers_degraded",
        }.get(status, "backend_worker_state_changed")
        message = {
            "ok": "图库工作线程已就绪。",
            "starting": "图库工作线程正在初始化。",
            "degraded": "一个或多个图库工作线程不可用。",
        }.get(status, f"图库工作线程状态变为 {status}。")
        self._activity_log(
            level=level,
            event=event,
            message=message,
            details={
                "status": status,
                "worker_alive": worker_alive,
                "library_count": len(self._library_workers),
                "error_count": len(error_library_ids),
                "error_library_ids": error_library_ids,
            },
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for worker in self._library_workers.values():
            worker.start()
        self._activity_log(
            level="info",
            event="backend_workers_started",
            message="图库工作线程已启动。",
            details={"library_count": len(self._library_workers)},
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._activity_log(
            level="info",
            event="backend_workers_stopping",
            message="图库工作线程正在安全停止。",
            details={"library_count": len(self._library_workers)},
        )
        self._stop_requested.set()
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.status not in _TERMINAL_STATUSES:
                    job.cancel_requested = True
                    if job.status == "queued":
                        job.status = "cancelled"
                        job.finished_at = job.finished_at or _utc_now()
                        job.progress = {
                            **(job.progress or {}),
                            "message": "Cancelled during backend shutdown.",
                            "updated_at": job.finished_at,
                        }
                    elif job.status == "running":
                        job.status = "cancelling"
        try:
            if self._started:
                for worker in self._library_workers.values():
                    worker.close()
            self._control_executor.shutdown(wait=True, cancel_futures=False)
        finally:
            self._activity_log(
                level="info",
                event="backend_workers_stopped",
                message="图库工作线程已停止。",
                details={"library_count": len(self._library_workers)},
            )
            if self._owns_activity and self._activity is not None:
                self._activity.close(timeout=2.0)

    def health(self) -> tuple[HTTPStatus, dict[str, Any]]:
        coordinator_alive = self._started and not self._closed
        errors = {
            library_id: worker.startup_error
            for library_id, worker in self._library_workers.items()
            if worker.startup_error is not None
        }
        all_ready = bool(self._library_workers) and all(
            worker.ready for worker in self._library_workers.values()
        )
        all_alive = all(worker.alive for worker in self._library_workers.values())
        if errors:
            status_text = "degraded"
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif not all_ready:
            status_text = "starting"
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif not coordinator_alive or not all_alive:
            status_text = "degraded"
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status_text = "ok"
            status = HTTPStatus.OK
        with self._jobs_lock:
            queue_depth = sum(
                1 for job in self._jobs.values() if job.status == "queued"
            )
            running_jobs = sum(
                1 for job in self._jobs.values() if job.status == "running"
            )
        payload: dict[str, Any] = {
            **self.version_info(),
            "status": status_text,
            "service_ready": status == HTTPStatus.OK,
            "worker_alive": coordinator_alive and all_alive,
            "queue_depth": queue_depth,
            "running_jobs": running_jobs,
            "default_library_id": self._catalog.default_library_id,
            "libraries": [
                {
                    **library.to_dict(),
                    "ready": self._library_workers[library.library_id].ready
                    if library.enabled
                    else False,
                    "queue_depth": (
                        self._library_workers[library.library_id].queue_depth
                        if library.enabled
                        else 0
                    ),
                    "queue_capacity": (
                        self._library_workers[library.library_id].queue_capacity
                        if library.enabled
                        else self._library_queue_capacity
                    ),
                    "folder_delete_recovery": (
                        self._library_workers[library.library_id].recovery_report
                        if library.enabled
                        else None
                    ),
                }
                for library in self._catalog.libraries
            ],
        }
        if errors:
            payload["error"] = {
                "code": "library_initialization_failed",
                "message": "One or more library services failed to initialize.",
                "details": errors,
            }
        self._record_health_transition(
            status_text,
            error_library_ids=sorted(errors),
            worker_alive=bool(payload["worker_alive"]),
        )
        return status, payload

    def version_info(self) -> dict[str, Any]:
        large_library_policy = build_large_library_policy_snapshot(
            queue_capacity_per_library=self._library_queue_capacity,
            queue_max_capacity_per_library=_MAX_LIBRARY_QUEUE_CAPACITY,
            queue_retry_after_seconds=_QUEUE_RETRY_AFTER_SECONDS,
            cooperative_checkpoint_items=self._cooperative_batch_size,
            cooperative_max_checkpoint_items=_MAX_COOPERATIVE_BATCH_SIZE,
            cooperative_max_interval_seconds=(
                _DEFAULT_COOPERATIVE_MAX_INTERVAL_SECONDS
            ),
            cooperative_max_high_priority_calls=_DEFAULT_COOPERATIVE_MAX_CALLS,
            result_preview_page_size=_DEFAULT_RESULT_PREVIEW_PAGE_SIZE,
            optimize_idle_grace_seconds=(_DEFAULT_IDLE_MAINTENANCE_DELAY_SECONDS),
        )
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "app": "zvec-image-search",
            "version": _APP_VERSION,
            "instance_id": self._instance_id,
            "config_fingerprint": self._config_fingerprint,
            "capabilities": dict(_CAPABILITIES),
            "credentials_configured": bool(self._config.api_key),
            "library_queue_capacity": self._library_queue_capacity,
            "cooperative_batch_size": self._cooperative_batch_size,
            "large_library_policy": large_library_policy,
            "large_library_policy_sha256": (
                canonical_large_library_policy_sha256(large_library_policy)
            ),
        }

    def configure_credentials(self, payload: dict[str, Any]) -> dict[str, bool]:
        unknown = set(payload) - {"dashscope_api_key", "api_url"}
        if unknown:
            raise BackendRequestError(
                "unknown_fields",
                "The credentials request contains unsupported fields.",
                details={"fields": sorted(unknown)},
            )
        api_key = payload.get("dashscope_api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise BackendRequestError(
                "invalid_credentials",
                "dashscope_api_key must be a non-empty string.",
            )
        api_url = payload.get("api_url")
        if api_url is not None:
            if not isinstance(api_url, str) or not api_url.strip():
                raise BackendRequestError(
                    "invalid_credentials",
                    "api_url must be a non-empty HTTP(S) URL when supplied.",
                )
            api_url = api_url.strip()
            parsed = urlsplit(api_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise BackendRequestError(
                    "invalid_credentials",
                    "api_url must be an HTTP(S) URL without embedded credentials.",
                )
        normalized_key = api_key.strip()
        if self._activity is not None:
            with suppress(Exception):
                self._activity.add_redactions(normalized_key)
        self._credentials.configure(normalized_key, api_url)
        return {"credentials_configured": True}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        command, params = _normalize_job(payload, self._query_root)
        if command in {"index_and_auto_tag", "auto_tag_estimate", "auto_tag"}:
            try:
                params["model"] = (
                    self._config.model_configuration.resolve_auto_tag_model(
                        params.get("model")
                    )
                )
            except ValueError as exc:
                raise BackendRequestError("invalid_params", str(exc)) from exc
        self._validate_library_selection(command, params)
        job = BackendJob(id=uuid.uuid4().hex, command=command, params=params)
        with self._jobs_lock:
            # This check and insertion share the shutdown lock boundary, so a
            # successful idle shutdown cannot race with one last accepted job.
            if self._stop_requested.is_set():
                raise BackendRequestError(
                    "service_unavailable",
                    "The backend is shutting down.",
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            self._jobs[job.id] = job
        try:
            self._schedule(job)
        except BaseException:
            with self._jobs_lock:
                self._jobs.pop(job.id, None)
                self._job_futures.pop(job.id, None)
                self._job_worker_ids.pop(job.id, None)
            raise
        with self._jobs_lock:
            snapshot = self._job_view_locked(job)
        self._record_job_history(snapshot, force=True)
        return snapshot

    def request_shutdown_if_idle(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {"if_idle"}
        if unknown:
            raise BackendRequestError(
                "unknown_fields",
                "The shutdown request contains unsupported fields.",
                details={"fields": sorted(unknown)},
            )
        if payload.get("if_idle") is not True:
            raise BackendRequestError(
                "invalid_request",
                "if_idle must be true; active jobs are never terminated implicitly.",
            )
        with self._jobs_lock:
            active_job_ids = sorted(
                job.id
                for job in self._jobs.values()
                if job.status not in _TERMINAL_STATUSES
            )
            if active_job_ids:
                raise BackendRequestError(
                    "backend_busy",
                    "The backend still has active jobs.",
                    status=HTTPStatus.CONFLICT,
                    details={"active_job_ids": active_job_ids},
                )
            self._stop_requested.set()
        return {
            "accepted": True,
            "status": "shutting_down",
            "instance_id": self._instance_id,
        }

    def list_jobs(
        self, *, active: bool | None = None, limit: int = 100
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise BackendRequestError(
                "invalid_params", "limit must be between 1 and 500."
            )
        with self._jobs_lock:
            jobs = sorted(
                self._jobs.values(), key=lambda item: item.submitted_at, reverse=True
            )
            if active is not None:
                jobs = [
                    job
                    for job in jobs
                    if (job.status not in _TERMINAL_STATUSES) == active
                ]
            selected = jobs[:limit]
            return {
                "jobs": [self._job_view_locked(job) for job in selected],
                "count": len(selected),
                "total_count": len(jobs),
            }

    def get(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise BackendRequestError(
                    "job_not_found",
                    "The requested job does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                )
            return self._job_view_locked(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise BackendRequestError(
                    "job_not_found",
                    "The requested job does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                )
            if job.status in _TERMINAL_STATUSES:
                raise BackendRequestError(
                    "job_already_finished",
                    f"Job is already {job.status}.",
                    status=HTTPStatus.CONFLICT,
                    details={"status": job.status},
                )
            job.cancel_requested = True
            previous_progress = job.progress or {}
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = _utc_now()
                job.progress = {
                    **previous_progress,
                    "message": "Cancelled before execution.",
                    "updated_at": job.finished_at,
                }
            else:
                job.status = "cancelling"
                job.progress = {
                    **previous_progress,
                    "message": "Cancellation requested.",
                    "updated_at": _utc_now(),
                }
            future = self._job_futures.get(job_id)
            worker_id = self._job_worker_ids.get(job_id)
            result = self._job_view_locked(job)
        worker = self._library_workers.get(worker_id or "")
        removed = worker.cancel_queued(job_id) if worker is not None else False
        self._record_job_history(result, force=True)
        if future is not None and result["status"] == "cancelled" and not removed:
            future.cancel()
        return result

    def _job_view_locked(self, job: BackendJob) -> dict[str, Any]:
        """Return one UI snapshot with a live pending position when available."""

        payload = job.to_dict()
        priority = _job_priority(job.command)
        payload["queue_priority"] = _PRIORITY_LABELS[priority]
        queue_position: int | None = None
        if job.status == "queued":
            worker_id = self._job_worker_ids.get(job.id)
            worker = self._library_workers.get(worker_id or "")
            if worker is not None:
                queue_position = worker.queue_position(job.id)
        payload["queue_position"] = queue_position
        return payload

    def _schedule(self, job: BackendJob) -> None:
        if job.command == "libraries":
            future = self._control_executor.submit(
                self._run_control_job, job.id, lambda: self._catalog.to_dict()
            )
        elif job.command in {"folder_list", "folder_images"}:
            library = self._single_library(job.params)
            future = self._control_executor.submit(
                self._run_browse_job,
                job.id,
                library,
            )
        elif job.command == "search":
            libraries = self._search_libraries(job.params)
            if len(libraries) > 1:
                future = self._control_executor.submit(
                    self._run_federated_job, job.id, libraries
                )
            else:
                library = libraries[0]
                self._schedule_library_job(
                    job,
                    library,
                    job.id,
                    lambda service: self._run_library_job(job.id, service, library),
                )
                return
        else:
            library = self._single_library(job.params)
            self._schedule_library_job(
                job,
                library,
                job.id,
                lambda service: self._run_library_job(job.id, service, library),
            )
            return
        with self._jobs_lock:
            self._job_futures[job.id] = future
        future.add_done_callback(
            lambda completed: self._complete_job(job.id, completed)
        )

    def _schedule_library_job(
        self,
        job: BackendJob,
        library: LibraryDefinition,
        job_id: str,
        callback: Callable[[Any], Any],
    ) -> None:
        worker = self._library_workers[library.library_id]
        future: Future[Any] = Future()
        future.add_done_callback(
            lambda completed: self._complete_job(job.id, completed)
        )
        with self._jobs_lock:
            self._job_futures[job.id] = future
            self._job_worker_ids[job.id] = library.library_id
        try:
            worker.submit(
                job_id,
                callback,
                priority=_job_priority(job.command),
                future=future,
            )
        except _LibraryQueueFull as exc:
            with self._jobs_lock:
                self._job_futures.pop(job.id, None)
                self._job_worker_ids.pop(job.id, None)
            raise _queue_full_error(
                exc,
                "The selected library has reached its pending-job capacity.",
            ) from exc

    def _begin_job(self, job_id: str) -> BackendJob:
        with self._jobs_lock:
            job = self._jobs[job_id]
            if job.status == "cancelled" or job.cancel_requested:
                raise JobCancelled("Job cancellation requested.")
            job.status = "running"
            job.started_at = job.started_at or _utc_now()
            job.progress = {
                "message": "Running.",
                "updated_at": _utc_now(),
            }
            snapshot = job.to_dict()
        self._record_job_history(snapshot, force=True)
        return job

    def _run_control_job(self, job_id: str, callback: Callable[[], Any]) -> Any:
        self._begin_job(job_id)
        self._check_cancel(job_id)
        return callback()

    def _run_library_job(
        self, job_id: str, service: Any, library: LibraryDefinition
    ) -> Any:
        job = self._begin_job(job_id)
        result = self._execute_single(service, library, job.command, job.params)
        self._check_cancel(job_id)
        return _attribute_result(
            result,
            library,
            result_preview_limit=15 if job.command == "search" else None,
        )

    def _run_browse_job(
        self,
        job_id: str,
        library: LibraryDefinition,
    ) -> Any:
        job = self._begin_job(job_id)
        self._check_cancel(job_id)
        with LibraryBrowser(
            library_id=library.library_id,
            state_path=library.workspace / "image_collection.state.sqlite3",
        ) as browser:
            if job.command == "folder_list":
                result = browser.list_folders(
                    root_id=job.params["root_id"],
                    query=job.params["query"],
                    offset=job.params["offset"],
                    limit=job.params["limit"],
                )
            elif job.command == "folder_images":
                result = browser.folder_images(
                    job.params["folder_key"],
                    include_subfolders=job.params["include_subfolders"],
                    offset=job.params["offset"],
                    limit=job.params["limit"],
                )
            else:  # pragma: no cover - guarded by _schedule
                raise AssertionError(f"Unsupported browse command: {job.command}")
        self._check_cancel(job_id)
        return _attribute_result(result, library)

    def _run_federated_job(
        self, job_id: str, libraries: list[LibraryDefinition]
    ) -> Any:
        job = self._begin_job(job_id)
        return _bounded_search_result(
            self._execute_federated_search(job, libraries), 15
        )

    def _complete_job(self, job_id: str, future: Future[Any]) -> None:
        failure_type: str | None = None
        try:
            result = future.result()
        except JobCancelled:
            with self._jobs_lock:
                job = self._jobs[job_id]
                job.status = "cancelled"
                job.result = None
                job.error = None
                job.progress = {
                    **(job.progress or {}),
                    "message": "Cancelled.",
                    "updated_at": _utc_now(),
                }
        except BackendRequestError as exc:
            with self._jobs_lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.status = "failed"
                job.result = None
                job.error = exc.to_dict()
                failure_type = exc.__class__.__name__
                job.progress = {
                    **(job.progress or {}),
                    "message": "Failed.",
                    "updated_at": _utc_now(),
                }
        except BaseException as exc:
            with self._jobs_lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.status = "failed"
                job.result = None
                job.error = {
                    "code": "job_failed",
                    "message": str(exc) or exc.__class__.__name__,
                    "details": {"type": exc.__class__.__name__},
                }
                failure_type = exc.__class__.__name__
                job.progress = {
                    **(job.progress or {}),
                    "message": "Failed.",
                    "updated_at": _utc_now(),
                }
        else:
            safe_result = _json_safe(result)
            failure_count = _result_failure_count(safe_result)
            needs_attention = bool(
                isinstance(safe_result, dict)
                and safe_result.get("needs_attention") is True
            )
            with self._jobs_lock:
                job = self._jobs[job_id]
                if job.status == "cancelled":
                    return
                job.result = safe_result
                job.failure_count = failure_count
                job.status = (
                    "needs_attention"
                    if needs_attention
                    else "partial"
                    if failure_count
                    else "succeeded"
                )
                completion_progress = dict(job.progress or {})
                processed, total = _result_progress_counts(safe_result)
                if processed is not None:
                    completion_progress["current"] = processed
                if total is not None:
                    completion_progress["total"] = total
                job.progress = {
                    **completion_progress,
                    "message": (
                        "Completed and needs attention."
                        if needs_attention
                        else "Completed with item failures."
                        if failure_count
                        else "Completed."
                    ),
                    "failed": failure_count,
                    "updated_at": _utc_now(),
                }
        finally:
            with self._jobs_lock:
                job = self._jobs[job_id]
                if job.status in _TERMINAL_STATUSES:
                    job.finished_at = job.finished_at or _utc_now()
                self._job_futures.pop(job_id, None)
                self._job_worker_ids.pop(job_id, None)
                snapshot = job.to_dict()
            self._record_job_history(snapshot, force=True)
            if snapshot.get("status") in {
                "partial",
                "needs_attention",
                "failed",
            }:
                self._record_image_failures(snapshot)
            if failure_type is not None:
                library_id, library_name = self._job_library_context(snapshot)
                self._activity_log(
                    level="error",
                    event="backend_job_exception",
                    message="图库任务因后端异常失败。",
                    library_id=library_id,
                    library_name=library_name,
                    job_id=job_id,
                    details={
                        "task_type": str(snapshot.get("command") or "unknown"),
                        "error_type": failure_type,
                    },
                )

    def _execute_federated_search(
        self, job: BackendJob, libraries: list[LibraryDefinition]
    ) -> dict:
        started_at = perf_counter()
        params = job.params
        primary = libraries[0]
        prepared = self._call_library(
            job.id,
            primary,
            lambda service: (
                service.prepare_search_query(
                    text=params["text"],
                    image_path=params["image"],
                    search_mode="tags",
                )
                if params["search_mode"] == "tags"
                else service.prepare_search_query(
                    text=params["text"],
                    image_path=params["image"],
                )
            ),
        )
        candidate_k = params["candidate_k"]
        if self._config.max_top_k is not None:
            candidate_k = min(self._config.max_top_k, candidate_k)
        futures: list[tuple[LibraryDefinition, Future[Any]]] = []
        try:
            for library in libraries:
                submission = self._library_workers[library.library_id].submit(
                    job.id,
                    lambda service: service.query_prepared_search(
                        prepared,
                        candidate_k=candidate_k,
                        include_self=params["include_self"],
                        tags=params["tags"],
                        tag_mode=params["tag_mode"],
                    ),
                    priority=_JobPriority.HIGH,
                )
                futures.append((library, submission.future))
        except _LibraryQueueFull as exc:
            for _library, future in futures:
                future.cancel()
            raise _queue_full_error(
                exc,
                "A library required by federated search has reached capacity.",
            ) from exc
        collections = [
            LibraryCandidateSet(library=library, candidates=future.result())
            for library, future in futures
        ]
        self._check_cancel(job.id)
        config = replace(
            self._config,
            results_directory=self._catalog.federated_results_directory,
        )
        return export_federated_search(
            config,
            prepared,
            collections,
            top_k=params["top_k"],
            image_weight=params["image_weight"],
            text_weight=params["text_weight"],
            tags=params["tags"],
            tag_mode=params["tag_mode"],
            latency_ms=(perf_counter() - started_at) * 1000,
            show_low_confidence=params["show_low_confidence"],
            diversify_results=params["diversify_results"],
            sort_mode=params["sort_mode"],
            result_limit=_DEFAULT_RESULT_PREVIEW_PAGE_SIZE,
            copy_files=False,
            report_result_limit=_DEFAULT_RESULT_PREVIEW_PAGE_SIZE,
        )

    def _execute_single(
        self,
        service: Any,
        library: LibraryDefinition,
        command: str,
        params: dict[str, Any],
    ) -> Any:
        if command in {"index", "folder_tag_backfill"}:
            folder = self._library_folder(library, params["folder"])
            return service.index_folder(
                folder,
                recursive=params["recursive"],
                verify_hash=params["verify_hash"],
                tags=params.get("tags"),
            )
        if command == "index_and_auto_tag":
            folder = self._library_folder(library, params["folder"])
            return service.index_and_auto_tag_folder(
                folder,
                recursive=params["recursive"],
                verify_hash=params["verify_hash"],
                tags=params["tags"],
                model=params["model"],
                max_images=params["max_images"],
                max_budget_cny=params["max_budget_cny"],
                external_processing_confirmed=(params["external_processing_confirmed"]),
            )
        if command == "auto_tag_estimate":
            return service.estimate_auto_tags(
                scope=params["scope"],
                model=params["model"],
                max_images=params["max_images"],
                max_budget_cny=params["max_budget_cny"],
            )
        if command == "auto_tag_pending":
            pending_args: dict[str, Any] = {
                "offset": params["offset"],
                "limit": params["limit"],
            }
            default_filters = {
                "latest_index_only": False,
                "review_state": "all",
                "character": "",
                "work": "",
                "action": "",
                "expression": "",
            }
            if params["filters"] != default_filters:
                pending_args["filters"] = params["filters"]
            return service.pending_auto_tags(**pending_args)
        if command == "auto_tag":
            return service.auto_tag_images(
                scope=params["scope"],
                model=params["model"],
                max_images=params["max_images"],
                max_budget_cny=params["max_budget_cny"],
                external_processing_confirmed=(params["external_processing_confirmed"]),
            )
        if command == "auto_tag_review":
            return service.review_auto_tags(params["decisions"])
        if command == "auto_tag_review_batch":
            return service.review_auto_tags_batch(
                proposal_ids=params["proposal_ids"],
                accepted_tags_by_proposal=params["accepted_tags_by_proposal"],
                exclude_identity_tags=params["exclude_identity_tags"],
                acceptance_mode=params["acceptance_mode"],
                batch_confirmation=params["batch_confirmation"],
            )
        if command == "auto_tag_review_undo":
            return service.undo_latest_auto_tag_review_batch()
        if command == "auto_tag_policy_migrate":
            return service.reconcile_pending_identity_tags(dry_run=params["dry_run"])
        if command == "manual_tag_batch":
            return service.manual_tag_batch(
                library_id=library.library_id,
                selection=params["selection"],
                operation=params["operation"],
                tags=params["tags"],
            )
        if command == "manual_tag_undo":
            return service.undo_latest_manual_tag_batch()
        if command == "search_results_cleanup":
            return service.cleanup_search_results(
                keep_latest=params["keep_latest"],
                dry_run=params["dry_run"],
            )
        if command == "folder_delete_preview":
            return service.preview_folder_deletion(
                library_id=library.library_id,
                folder_key=params["folder_key"],
                include_subfolders=params["include_subfolders"],
            )
        if command == "folder_delete_commit":
            return service.commit_folder_deletion(
                library_id=library.library_id,
                operation_id=params["operation_id"],
                confirmation_token=params["confirmation_token"],
                confirm=params["confirm"],
            )
        if command == "metadata_backfill":
            return service.backfill_metadata_embeddings(
                max_images=params["max_images"],
            )
        if command == "tag_alias_list":
            return service.list_tag_aliases()
        if command == "tag_alias_upsert":
            return service.upsert_tag_alias(
                params["canonical_name"],
                params["aliases"],
            )
        if command == "tag_alias_delete":
            return service.delete_tag_alias(params["canonical_name"])
        if command == "cluster_images":
            return service.cluster_images(
                scope=params["scope"],
                cluster_types=params["cluster_types"],
            )
        if command == "cluster_list":
            return service.list_image_clusters(
                offset=params["offset"],
                limit=params["limit"],
                cluster_type=params["cluster_type"],
            )
        if command == "cluster_detail":
            return service.image_cluster_detail(
                params["cluster_id"],
                offset=params["offset"],
                limit=params["limit"],
            )
        if command == "cluster_merge":
            return service.merge_image_clusters(params["cluster_ids"])
        if command == "cluster_split":
            return service.split_image_cluster(
                params["cluster_id"],
                params["doc_ids"],
            )
        if command == "cluster_apply_identity":
            return service.apply_image_cluster_identity(
                params["cluster_id"],
                identity_category=params["identity_category"],
                identity_value=params["identity_value"],
            )
        if command == "cluster_undo":
            return service.undo_latest_cluster_operation()
        if command == "active_learning_queue":
            return service.build_active_learning_review_queue(
                review_budget=params["review_budget"]
            )
        if command == "active_learning_review":
            return service.review_active_learning_queue(
                queue_id=params["queue_id"],
                decisions=params["decisions"],
            )
        if command == "active_learning_review_undo":
            return service.undo_latest_active_learning_review()
        if command == "sync":
            folder = self._library_folder(library, params["folder"])
            return service.sync_folder(
                folder,
                recursive=params["recursive"],
                verify_hash=params["verify_hash"],
                dry_run=params["dry_run"],
                allow_scope_change=params["allow_scope_change"],
            )
        if command == "search":
            image = params["image"]
            text = params["text"]
            if image is not None:
                image = _validate_query_image(image, self._query_root)
            common = {
                "top_k": params["top_k"],
                "tags": params["tags"],
                "tag_mode": params["tag_mode"],
                "show_low_confidence": params["show_low_confidence"],
                "diversify_results": params["diversify_results"],
                "sort_mode": params["sort_mode"],
                "copy_files": False,
                "report_result_limit": _DEFAULT_RESULT_PREVIEW_PAGE_SIZE,
            }
            if params["search_mode"] == "tags":
                return service.search_by_tags(text, **common)
            if image is not None and text is not None:
                return service.search_by_image_and_text(
                    image_path=image,
                    text=text,
                    image_weight=params["image_weight"],
                    text_weight=params["text_weight"],
                    include_self=params["include_self"],
                    **common,
                )
            if image is not None:
                return service.search_by_image(
                    image_path=image,
                    include_self=params["include_self"],
                    **common,
                )
            return service.search_by_text(text, **common)
        if command == "stats":
            return service.stats()
        if command == "roots":
            return service.list_roots()
        if command == "cache_clear":
            return service.clear_embedding_cache()
        if command == "clean_results":
            return service.clean_results(
                params["days"],
                dry_run=params["dry_run"],
            )
        raise AssertionError(f"Unsupported normalized command: {command}")

    def _call_library(
        self,
        job_id: str,
        library: LibraryDefinition,
        callback: Callable[[Any], Any],
    ) -> Any:
        try:
            submission = self._library_workers[library.library_id].submit(
                job_id,
                callback,
                priority=_JobPriority.HIGH,
            )
        except _LibraryQueueFull as exc:
            raise _queue_full_error(
                exc,
                "A library required by federated search has reached capacity.",
            ) from exc
        return submission.future.result()

    def _report_progress(
        self, job_id: str, library: LibraryDefinition, message: str
    ) -> None:
        self._check_cancel(job_id)
        with self._jobs_lock:
            job = self._jobs[job_id]
            item: dict[str, Any] = {
                "message": str(message),
                "updated_at": _utc_now(),
            }
            match = _PROGRESS_NUMBERS.search(str(message))
            if match:
                item["current"] = int(match.group("current"))
                item["total"] = int(match.group("total"))
            previous = job.progress or {}
            per_library = dict(previous.get("libraries") or {})
            per_library[library.library_id] = {
                **item,
                "library_name": library.name,
            }
            job.progress = {
                **previous,
                **item,
                "library_id": library.library_id,
                "library_name": library.name,
                "libraries": per_library,
            }
            snapshot = job.to_dict()
        self._record_job_history(snapshot)

    def _check_cancel(self, job_id: str) -> None:
        with self._jobs_lock:
            if self._stop_requested.is_set():
                raise JobCancelled("Backend shutdown requested.")
            if self._jobs[job_id].cancel_requested:
                raise JobCancelled("Job cancellation requested.")

    def _validate_library_selection(self, command: str, params: dict[str, Any]) -> None:
        if command == "libraries":
            return
        if command == "search":
            self._search_libraries(params)
        else:
            self._single_library(params)

    def _single_library(self, params: dict[str, Any]) -> LibraryDefinition:
        library_id = params.get("library_id") or self._catalog.default_library_id
        return self._enabled_library(str(library_id))

    def _search_libraries(self, params: dict[str, Any]) -> list[LibraryDefinition]:
        requested = params.get("library_ids") or []
        if not requested:
            default = self._catalog.default
            return [
                default,
                *(
                    library
                    for library in self._catalog.enabled
                    if library.library_id != default.library_id
                ),
            ]
        result: list[LibraryDefinition] = []
        seen: set[str] = set()
        for library_id in requested:
            if library_id in seen:
                continue
            seen.add(library_id)
            result.append(self._enabled_library(library_id))
        return result

    def _enabled_library(self, library_id: str) -> LibraryDefinition:
        library = self._catalog.by_id.get(library_id)
        if library is None:
            raise BackendRequestError(
                "library_not_found",
                f"Unknown library_id: {library_id}",
                details={"library_id": library_id},
            )
        if not library.enabled:
            raise BackendRequestError(
                "library_disabled",
                f"Library is disabled: {library_id}",
                details={"library_id": library_id},
            )
        return library

    @staticmethod
    def _library_folder(library: LibraryDefinition, requested: str | None) -> str:
        if library.image_root is None:
            if requested is None:
                raise ValueError("folder is required for the legacy default library.")
            return requested
        configured = library.image_root.resolve()
        if (
            requested is not None
            and Path(requested).expanduser().resolve() != configured
        ):
            raise ValueError("folder must match the selected library image_root.")
        return str(configured)


class BackendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        manager: BackendJobManager,
        token: str,
        instance_lock: BackendInstanceLock,
    ) -> None:
        self.manager = manager
        self.auth_token = token
        self._instance_lock = instance_lock
        self._shutdown_started = threading.Event()
        self._shutdown_lock = threading.Lock()
        super().__init__(server_address, BackendRequestHandler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            # Keep this guarantee on the server itself so embedded callers that do
            # not use serve_backend() still close Collection-owning workers.
            self.manager.close()

    def shutdown_async(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_started.is_set():
                return
            self._shutdown_started.set()
        threading.Thread(
            target=self.shutdown,
            name="zvec-backend-shutdown",
            daemon=True,
        ).start()

    def server_close(self) -> None:
        try:
            # Keep backend.lock until every Collection worker and the owned
            # ActivityStore are closed.  Embedded callers may invoke
            # server_close() without first entering serve_forever().
            self.manager.close()
        finally:
            try:
                super().server_close()
            finally:
                self._instance_lock.release()


class BackendRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZvecBackend/1"

    @property
    def backend(self) -> BackendHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802
        if not self._authenticate():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            status, payload = self.backend.manager.health()
            self._send_json(status, payload)
            return
        if path == "/version":
            self._send_json(HTTPStatus.OK, self.backend.manager.version_info())
            return
        if path == "/v1/jobs":

            def list_jobs() -> tuple[HTTPStatus, dict[str, Any]]:
                options = _job_list_query(parsed.query)
                return HTTPStatus.OK, self.backend.manager.list_jobs(**options)

            self._run_request(list_jobs)
            return
        match = _JOB_PATH.fullmatch(path)
        if match:
            self._run_request(
                lambda: (
                    HTTPStatus.OK,
                    {"job": self.backend.manager.get(match.group(1))},
                )
            )
            return
        self._send_error(
            BackendRequestError(
                "not_found",
                "The requested endpoint does not exist.",
                status=HTTPStatus.NOT_FOUND,
            )
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._authenticate():
            return
        path = urlsplit(self.path).path
        if path == "/v1/control/shutdown":
            try:
                payload = self._read_json_object()
                response = self.backend.manager.request_shutdown_if_idle(payload)
            except BackendRequestError as exc:
                self._send_error(exc)
            except Exception:
                self._send_error(
                    BackendRequestError(
                        "internal_error",
                        "The backend could not process the request.",
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                )
            else:
                try:
                    # HTTP/1.1 handlers otherwise keep servicing this already
                    # accepted socket after serve_forever() stops. Explicitly
                    # close it once the complete 202 response has been flushed.
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.ACCEPTED,
                        response,
                        extra_headers={"Connection": "close"},
                    )
                    self.wfile.flush()
                finally:
                    # BaseServer.shutdown() must run outside the serve_forever
                    # thread. Starting it after the response also prevents a
                    # successful request from being cut off mid-body.
                    self.backend.shutdown_async()
            return
        if path != "/v1/jobs":
            self._send_error(
                BackendRequestError(
                    "not_found",
                    "The requested endpoint does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                )
            )
            return

        def submit() -> tuple[HTTPStatus, dict[str, Any]]:
            payload = self._read_json_object()
            job = self.backend.manager.submit(payload)
            return HTTPStatus.ACCEPTED, {"job": job}

        self._run_request(submit)

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authenticate():
            return
        if urlsplit(self.path).path != "/v1/session/credentials":
            self._send_error(
                BackendRequestError(
                    "not_found",
                    "The requested endpoint does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                )
            )
            return

        def configure() -> tuple[HTTPStatus, dict[str, Any]]:
            payload = self._read_json_object()
            return HTTPStatus.OK, self.backend.manager.configure_credentials(payload)

        self._run_request(configure)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authenticate():
            return
        match = _JOB_PATH.fullmatch(urlsplit(self.path).path)
        if not match:
            self._send_error(
                BackendRequestError(
                    "not_found",
                    "The requested endpoint does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                )
            )
            return
        self._run_request(
            lambda: (
                HTTPStatus.ACCEPTED,
                {"job": self.backend.manager.cancel(match.group(1))},
            )
        )

    def _run_request(
        self, callback: Callable[[], tuple[HTTPStatus, dict[str, Any]]]
    ) -> None:
        try:
            status, payload = callback()
        except BackendRequestError as exc:
            self._send_error(exc)
        except Exception:
            self._send_error(
                BackendRequestError(
                    "internal_error",
                    "The backend could not process the request.",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )
        else:
            self._send_json(status, payload)

    def _authenticate(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, supplied = header.partition(" ")
        authorized = (
            separator == " "
            and scheme.lower() == "bearer"
            and bool(supplied)
            and hmac.compare_digest(supplied, self.backend.auth_token)
        )
        if authorized:
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {
                "error": {
                    "code": "unauthorized",
                    "message": "A valid Bearer token is required.",
                }
            },
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _read_json_object(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise BackendRequestError(
                "unsupported_media_type",
                "Content-Type must be application/json.",
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise BackendRequestError(
                "length_required",
                "Content-Length is required.",
                status=HTTPStatus.LENGTH_REQUIRED,
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BackendRequestError(
                "invalid_content_length", "Content-Length is invalid."
            ) from exc
        if length < 0 or length > _MAX_REQUEST_BYTES:
            raise BackendRequestError(
                "request_too_large",
                f"The JSON body must not exceed {_MAX_REQUEST_BYTES} bytes.",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendRequestError(
                "invalid_json", "The request body must be valid UTF-8 JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise BackendRequestError(
                "invalid_request", "The JSON body must be an object."
            )
        return payload

    def _send_error(self, error: BackendRequestError) -> None:
        headers: dict[str, str] | None = None
        if error.code == "queue_full" and error.details is not None:
            retry_after = error.details.get("retry_after_seconds")
            if isinstance(retry_after, int) and retry_after > 0:
                headers = {"Retry-After": str(retry_after)}
        self._send_json(
            error.status,
            {"error": error.to_dict()},
            extra_headers=headers,
        )

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _normalized_identity(
    value: str | None, *, name: str, generated: str, max_length: int
) -> str:
    if value is None:
        return generated
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when supplied.")
    normalized = value.strip()
    if len(normalized) > max_length or any(ord(char) < 32 for char in normalized):
        raise ValueError(
            f"{name} must contain at most {max_length} printable characters."
        )
    return normalized


def _generated_config_fingerprint(
    config: ServiceConfig,
    query_root: str | Path | None,
    libraries_config: str | Path | None,
) -> str:
    effective_query_root = Path(
        query_root if query_root is not None else config.workspace / "query-staging"
    ).expanduser()
    identity = {
        "workspace": str(config.workspace.expanduser().resolve()),
        "results_directory": str(config.results_path),
        "query_root": str(effective_query_root.resolve()),
        "libraries_config": (
            str(Path(libraries_config).expanduser().resolve())
            if libraries_config is not None
            else None
        ),
        "model": config.model,
        "dimension": config.dimension,
        "metric": config.metric,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _default_config_home() -> Path:
    return default_config_home()


def _resolved_instance_lock_path(value: str | Path | None) -> Path:
    if value is not None:
        if not str(value).strip():
            raise ValueError("instance_lock_path must not be empty when supplied.")
        return Path(value).expanduser().resolve()
    configured = os.getenv("ZVEC_BACKEND_LOCK_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_config_home() / "backend" / "backend.lock"


def create_backend_server(
    *,
    host: str,
    port: int,
    token: str,
    instance_id: str | None = None,
    config_fingerprint: str | None = None,
    instance_lock_path: str | Path | None = None,
    config: ServiceConfig | None = None,
    query_root: str | Path | None = None,
    service_factory: ServiceFactory | None = None,
    library_service_factory: LibraryServiceFactory | None = None,
    libraries_config: str | Path | None = None,
    library_catalog: LibraryCatalog | None = None,
    activity_store: ActivityStore | None = None,
    activity_config_home: str | Path | None = None,
    library_queue_capacity: int | None = None,
    cooperative_batch_size: int | None = None,
) -> tuple[BackendHTTPServer, BackendJobManager]:
    if not token:
        raise ValueError("A non-empty backend Bearer token is required.")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535.")
    if activity_store is not None and activity_config_home is not None:
        raise ValueError(
            "activity_store and activity_config_home cannot be supplied together."
        )
    resolved_config = config or ServiceConfig()
    if resolved_config.config_home is None:
        resolved_config = replace(
            resolved_config,
            config_home=(
                Path(activity_config_home).expanduser().resolve()
                if activity_config_home is not None
                else _default_config_home()
            ),
        )
    resolved_instance_id = _normalized_identity(
        instance_id,
        name="instance_id",
        generated=uuid.uuid4().hex,
        max_length=128,
    )
    resolved_fingerprint = _normalized_identity(
        config_fingerprint,
        name="config_fingerprint",
        generated=_generated_config_fingerprint(
            resolved_config, query_root, libraries_config
        ),
        max_length=256,
    )
    instance_lock = BackendInstanceLock(
        _resolved_instance_lock_path(instance_lock_path)
    )
    instance_lock.acquire(
        {
            "pid": os.getpid(),
            "instance_id": resolved_instance_id,
            "config_fingerprint": resolved_fingerprint,
            "acquired_at": _utc_now(),
        }
    )

    manager: BackendJobManager | None = None
    server: BackendHTTPServer | None = None
    resolved_activity = activity_store
    owns_activity = False
    try:
        # Loading catalogs and constructing workers happens only after the process
        # lock is held. A second desktop process therefore cannot open Collections.
        if resolved_activity is None and activity_config_home is not None:
            resolved_activity = ActivityStore(
                activity_config_home,
                # This process owns backend.lock, so no older backend can still
                # be running the jobs that crash recovery will mark interrupted.
                recover_interrupted=True,
            )
            owns_activity = True
        resolved_catalog = library_catalog or load_library_catalog(
            libraries_config, resolved_config
        )
        manager = BackendJobManager(
            instance_id=resolved_instance_id,
            config_fingerprint=resolved_fingerprint,
            config=resolved_config,
            query_root=query_root,
            service_factory=service_factory,
            library_service_factory=library_service_factory,
            library_catalog=resolved_catalog,
            activity_store=resolved_activity,
            owns_activity_store=owns_activity,
            library_queue_capacity=library_queue_capacity,
            cooperative_batch_size=cooperative_batch_size,
        )
        server = BackendHTTPServer((host, port), manager, token, instance_lock)
        manager.start()
        return server, manager
    except BaseException:
        if manager is not None:
            with suppress(Exception):
                manager.close()
        elif owns_activity and resolved_activity is not None:
            with suppress(Exception):
                resolved_activity.close(timeout=2.0)
        if server is not None:
            with suppress(Exception):
                server.server_close()
        else:
            instance_lock.release()
        raise


def serve_backend(
    *,
    host: str,
    port: int,
    token: str,
    instance_id: str | None = None,
    config_fingerprint: str | None = None,
    instance_lock_path: str | Path | None = None,
    config: ServiceConfig | None = None,
    query_root: str | Path | None = None,
    libraries_config: str | Path | None = None,
) -> None:
    server, manager = create_backend_server(
        host=host,
        port=port,
        token=token,
        instance_id=instance_id,
        config_fingerprint=config_fingerprint,
        instance_lock_path=instance_lock_path,
        config=config,
        query_root=query_root,
        libraries_config=libraries_config,
        activity_config_home=_default_config_home(),
    )
    version_info = manager.version_info()
    print(
        json.dumps(
            {
                "event": "backend_listening",
                "host": server.server_address[0],
                "port": server.server_address[1],
                "instance_id": version_info["instance_id"],
                "config_fingerprint": version_info["config_fingerprint"],
                "large_library_policy": version_info["large_library_policy"],
                "large_library_policy_sha256": version_info[
                    "large_library_policy_sha256"
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        manager.close()


def _normalize_job(
    payload: dict[str, Any], query_root: Path
) -> tuple[str, dict[str, Any]]:
    unknown = set(payload) - {"command", "params"}
    if unknown:
        raise BackendRequestError(
            "unknown_fields",
            "The request contains unsupported fields.",
            details={"fields": sorted(unknown)},
        )
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise BackendRequestError(
            "invalid_command", "command must be a non-empty string."
        )
    command = command.strip()
    supported = {
        "index",
        "sync",
        "search",
        "stats",
        "roots",
        "cache_clear",
        "clean_results",
        "libraries",
        "folder_list",
        "folder_images",
        "folder_delete_preview",
        "folder_delete_commit",
        "manual_tag_batch",
        "manual_tag_undo",
        "search_results_cleanup",
        "folder_tag_backfill",
        "index_and_auto_tag",
        "auto_tag_estimate",
        "auto_tag_pending",
        "auto_tag",
        "auto_tag_review",
        "auto_tag_review_batch",
        "auto_tag_review_undo",
        "auto_tag_policy_migrate",
        "metadata_backfill",
        "tag_alias_list",
        "tag_alias_upsert",
        "tag_alias_delete",
        "cluster_images",
        "cluster_list",
        "cluster_detail",
        "cluster_merge",
        "cluster_split",
        "cluster_apply_identity",
        "cluster_undo",
        "active_learning_queue",
        "active_learning_review",
        "active_learning_review_undo",
    }
    if command not in supported:
        raise BackendRequestError(
            "unsupported_command",
            f"Unsupported command: {command}",
            details={"supported": sorted(supported)},
        )
    raw_params = payload.get("params", {})
    if not isinstance(raw_params, dict):
        raise BackendRequestError("invalid_params", "params must be an object.")
    params = dict(raw_params)

    if command == "libraries":
        _reject_unknown(params, set())
        return command, {}

    if command == "folder_list":
        _reject_unknown(
            params,
            {"library_id", "root_id", "query", "offset", "limit"},
        )
        query = params.get("query", "")
        if not isinstance(query, str) or len(query.strip()) > 256:
            raise BackendRequestError(
                "invalid_params", "query must be a string of at most 256 characters."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "root_id": _optional_string(params, "root_id"),
            "query": query.strip(),
            "offset": _non_negative_integer(params, "offset", 0),
            "limit": _bounded_positive_integer(params, "limit", 200, 1_000),
        }

    if command == "folder_images":
        _reject_unknown(
            params,
            {
                "library_id",
                "folder_key",
                "include_subfolders",
                "offset",
                "limit",
            },
        )
        folder_key = _required_string(params, "folder_key")
        if len(folder_key) > 8_192:
            raise BackendRequestError(
                "invalid_params", "folder_key must be at most 8192 characters."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder_key": folder_key,
            "include_subfolders": _boolean(params, "include_subfolders", False),
            "offset": _non_negative_integer(params, "offset", 0),
            "limit": _bounded_positive_integer(params, "limit", 100, 1_000),
        }

    if command == "folder_delete_preview":
        _reject_unknown(
            params,
            {"library_id", "folder_key", "include_subfolders"},
        )
        folder_key = _required_string(params, "folder_key")
        if len(folder_key) > 8_192:
            raise BackendRequestError(
                "invalid_params", "folder_key must be at most 8192 characters."
            )
        include_subfolders = _boolean(params, "include_subfolders", True)
        if not include_subfolders:
            raise BackendRequestError(
                "invalid_params",
                "Folder deletion must include every descendant folder.",
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder_key": folder_key,
            "include_subfolders": True,
        }

    if command == "folder_delete_commit":
        _reject_unknown(
            params,
            {
                "library_id",
                "operation_id",
                "confirmation_token",
                "confirm",
            },
        )
        operation_id = _required_string(params, "operation_id").lower()
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise BackendRequestError(
                "invalid_params", "operation_id must be a 32-character hex id."
            )
        confirmation_token = _required_string(params, "confirmation_token")
        if len(confirmation_token) > 512:
            raise BackendRequestError(
                "invalid_params", "confirmation_token must be at most 512 characters."
            )
        confirm = _boolean(params, "confirm", False)
        if not confirm:
            raise BackendRequestError(
                "confirmation_required",
                "Explicit folder deletion confirmation is required.",
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "operation_id": operation_id,
            "confirmation_token": confirmation_token,
            "confirm": True,
        }

    if command == "manual_tag_batch":
        _reject_unknown(
            params,
            {"library_id", "selection", "operation", "tags"},
        )
        selection = _manual_tag_selection(params.get("selection"))
        operation = params.get("operation")
        if not isinstance(operation, str) or operation.strip().lower() not in {
            "add",
            "remove",
            "replace_manual",
        }:
            raise BackendRequestError(
                "invalid_params",
                "operation must be add, remove, or replace_manual.",
            )
        normalized_operation = operation.strip().lower()
        tags = _manual_tags(params.get("tags"))
        if normalized_operation in {"add", "remove"} and not tags:
            raise BackendRequestError(
                "invalid_params",
                f"{normalized_operation} requires at least one tag.",
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "selection": selection,
            "operation": normalized_operation,
            "tags": tags,
        }

    if command == "manual_tag_undo":
        _reject_unknown(params, {"library_id"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
        }

    if command == "search_results_cleanup":
        _reject_unknown(params, {"library_id", "keep_latest", "dry_run"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "keep_latest": _bounded_positive_integer(params, "keep_latest", 3, 100),
            "dry_run": _boolean(params, "dry_run", False),
        }

    if command == "index":
        _reject_unknown(
            params, {"library_id", "folder", "recursive", "verify_hash", "tags"}
        )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder": _optional_string(params, "folder"),
            "recursive": _boolean(params, "recursive", True),
            "verify_hash": _boolean(params, "verify_hash", False),
            "tags": _tags(params.get("tags")),
        }
    if command == "index_and_auto_tag":
        _reject_unknown(
            params,
            {
                "library_id",
                "folder",
                "recursive",
                "verify_hash",
                "tags",
                "model",
                "max_images",
                "max_budget_cny",
                "external_processing_confirmed",
            },
        )
        model = _optional_model(params.get("model"))
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder": _optional_string(params, "folder"),
            "recursive": _boolean(params, "recursive", True),
            "verify_hash": _boolean(params, "verify_hash", False),
            "tags": _tags(params.get("tags")),
            "model": model,
            "max_images": _bounded_positive_integer(params, "max_images", 200, 10_000),
            "max_budget_cny": _optional_positive_number(params, "max_budget_cny"),
            "external_processing_confirmed": _boolean(
                params,
                "external_processing_confirmed",
                False,
            ),
        }
    if command == "folder_tag_backfill":
        _reject_unknown(params, {"library_id", "folder", "recursive", "verify_hash"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder": _optional_string(params, "folder"),
            "recursive": _boolean(params, "recursive", True),
            "verify_hash": _boolean(params, "verify_hash", False),
        }
    if command in {"auto_tag_estimate", "auto_tag"}:
        _reject_unknown(
            params,
            {
                "library_id",
                "scope",
                "model",
                "max_images",
                "max_budget_cny",
                "external_processing_confirmed",
            },
        )
        scope = str(params.get("scope", "untagged"))
        if scope not in {
            "latest_index_run",
            "untagged",
            "failed",
            "failed_all",
            "all",
        }:
            raise BackendRequestError(
                "invalid_params",
                "scope must be latest_index_run, untagged, failed, failed_all, or all.",
            )
        model = _optional_model(params.get("model"))
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "scope": scope,
            "model": model,
            "max_images": _bounded_positive_integer(params, "max_images", 200, 10_000),
            "max_budget_cny": _optional_positive_number(params, "max_budget_cny"),
            "external_processing_confirmed": _boolean(
                params,
                "external_processing_confirmed",
                False,
            ),
        }
    if command == "auto_tag_pending":
        _reject_unknown(
            params,
            {
                "library_id",
                "offset",
                "limit",
                "filters",
                "latest_index_only",
                "latest_only",
                "character",
                "work",
                "action",
                "expression",
                "review_state",
                "review_status",
            },
        )
        raw_filters = params.get("filters", {})
        if not isinstance(raw_filters, dict):
            raise BackendRequestError("invalid_params", "filters must be an object.")
        filters = dict(raw_filters)
        for name in (
            "latest_index_only",
            "latest_only",
            "character",
            "work",
            "action",
            "expression",
            "review_state",
            "review_status",
        ):
            if name in params:
                if name in filters:
                    raise BackendRequestError(
                        "invalid_params",
                        f"{name} cannot be specified both in params and filters.",
                    )
                filters[name] = params[name]
        allowed_filters = {
            "latest_index_only",
            "latest_only",
            "character",
            "work",
            "action",
            "expression",
            "review_state",
            "review_status",
        }
        unknown_filters = set(filters) - allowed_filters
        if unknown_filters:
            raise BackendRequestError(
                "invalid_params",
                "filters contains unsupported fields.",
                details={"fields": sorted(unknown_filters)},
            )
        latest_value = filters.get(
            "latest_index_only", filters.get("latest_only", False)
        )
        if not isinstance(latest_value, bool):
            raise BackendRequestError(
                "invalid_params", "latest_index_only must be a boolean."
            )
        review_state = (
            str(
                filters.get("review_state", filters.get("review_status", "all"))
                or "all"
            )
            .strip()
            .lower()
        )
        review_state = {"pending": "all", "pending_review": "all"}.get(
            review_state, review_state
        )
        if review_state not in {
            "all",
            "low_risk",
            "identity",
            "conflict",
            "failed",
        }:
            raise BackendRequestError(
                "invalid_params",
                "review_state must be all, low_risk, identity, conflict, or failed.",
            )
        normalized_filters: dict[str, Any] = {
            "latest_index_only": latest_value,
            "review_state": review_state,
        }
        for name in ("character", "work", "action", "expression"):
            value = filters.get(name, "")
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value.strip()) > 128:
                raise BackendRequestError(
                    "invalid_params",
                    f"{name} must be a string of at most 128 characters.",
                )
            normalized_filters[name] = value.strip()
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "offset": _non_negative_integer(params, "offset", 0),
            "limit": _bounded_positive_integer(params, "limit", 100, 500),
            "filters": normalized_filters,
        }
    if command == "auto_tag_review":
        _reject_unknown(params, {"library_id", "decisions"})
        decisions = params.get("decisions")
        if (
            not isinstance(decisions, list)
            or not decisions
            or not all(isinstance(decision, dict) for decision in decisions)
        ):
            raise BackendRequestError(
                "invalid_params", "decisions must be a non-empty array of objects."
            )
        if len(decisions) > 1_000:
            raise BackendRequestError(
                "invalid_params", "decisions can contain at most 1000 items."
            )
        normalized_decisions = []
        for decision in decisions:
            doc_id = decision.get("doc_id", decision.get("proposal_id"))
            action = decision.get("action", decision.get("decision"))
            tags = decision.get("tags", decision.get("accepted_tags"))
            if not isinstance(action, str) or action.strip().lower() not in {
                "accept",
                "edit",
                "reject",
                "manual",
            }:
                raise BackendRequestError(
                    "invalid_params",
                    "decision must be accept, edit, reject, or manual.",
                )
            normalized_action = action.strip().lower()
            if normalized_action == "manual" and (
                not isinstance(tags, list)
                or not tags
                or not all(isinstance(tag, str) and tag.strip() for tag in tags)
            ):
                raise BackendRequestError(
                    "invalid_params",
                    "manual decisions require a non-empty accepted_tags array.",
                )
            confirmed_identity_tags = decision.get("confirmed_identity_tags", [])
            if not isinstance(confirmed_identity_tags, list) or not all(
                isinstance(tag, str) for tag in confirmed_identity_tags
            ):
                raise BackendRequestError(
                    "invalid_params",
                    "confirmed_identity_tags must be an array of strings.",
                )
            normalized_decision = {
                "doc_id": doc_id,
                "action": normalized_action,
                "tags": tags,
            }
            if "confirmed_identity_tags" in decision:
                normalized_decision["confirmed_identity_tags"] = confirmed_identity_tags
            normalized_decisions.append(normalized_decision)
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "decisions": normalized_decisions,
        }
    if command == "auto_tag_review_batch":
        _reject_unknown(
            params,
            {
                "library_id",
                "proposal_ids",
                "accepted_tags_by_proposal",
                "exclude_identity_tags",
                "acceptance_mode",
                "batch_confirmation",
            },
        )
        proposal_ids = params.get("proposal_ids")
        if (
            not isinstance(proposal_ids, list)
            or not proposal_ids
            or not all(
                isinstance(proposal_id, str) and proposal_id.strip()
                for proposal_id in proposal_ids
            )
        ):
            raise BackendRequestError(
                "invalid_params",
                "proposal_ids must be a non-empty array of strings.",
            )
        if len(proposal_ids) > 1_000:
            raise BackendRequestError(
                "invalid_params", "proposal_ids can contain at most 1000 items."
            )
        normalized_ids = list(dict.fromkeys(value.strip() for value in proposal_ids))
        accepted = params.get("accepted_tags_by_proposal", {})
        if not isinstance(accepted, dict):
            raise BackendRequestError(
                "invalid_params", "accepted_tags_by_proposal must be an object."
            )
        unknown_ids = set(accepted) - set(normalized_ids)
        if unknown_ids:
            raise BackendRequestError(
                "invalid_params",
                "accepted_tags_by_proposal contains unknown proposal ids.",
                details={"proposal_ids": sorted(unknown_ids)},
            )
        normalized_accepted: dict[str, list[str]] = {}
        for proposal_id, tags in accepted.items():
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) for tag in tags
            ):
                raise BackendRequestError(
                    "invalid_params",
                    "accepted_tags_by_proposal values must be arrays of strings.",
                )
            normalized_accepted[proposal_id] = tags
        raw_mode = params.get("acceptance_mode")
        if raw_mode is None:
            exclude_identity = _boolean(params, "exclude_identity_tags", True)
            acceptance_mode = "low_risk_only" if exclude_identity else "recommended"
        else:
            if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in {
                "low_risk_only",
                "recommended",
                "all_non_conflicting",
            }:
                raise BackendRequestError(
                    "invalid_params",
                    "acceptance_mode must be low_risk_only, recommended, or "
                    "all_non_conflicting.",
                )
            acceptance_mode = raw_mode.strip().lower()
            exclude_identity = _boolean(
                params,
                "exclude_identity_tags",
                acceptance_mode == "low_risk_only",
            )
        if exclude_identity != (acceptance_mode == "low_risk_only"):
            raise BackendRequestError(
                "invalid_params",
                "exclude_identity_tags conflicts with acceptance_mode.",
            )
        batch_confirmation = _boolean(params, "batch_confirmation", False)
        if acceptance_mode != "low_risk_only" and not batch_confirmation:
            raise BackendRequestError(
                "invalid_params",
                "Identity-inclusive batch review requires batch_confirmation=true.",
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "proposal_ids": normalized_ids,
            "accepted_tags_by_proposal": normalized_accepted,
            "exclude_identity_tags": exclude_identity,
            "acceptance_mode": acceptance_mode,
            "batch_confirmation": batch_confirmation,
        }
    if command == "auto_tag_review_undo":
        _reject_unknown(params, {"library_id"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
        }
    if command == "auto_tag_policy_migrate":
        _reject_unknown(params, {"library_id", "dry_run"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "dry_run": _boolean(params, "dry_run", False),
        }
    if command == "metadata_backfill":
        _reject_unknown(params, {"library_id", "max_images"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "max_images": _bounded_positive_integer(
                params,
                "max_images",
                200,
                10_000,
            ),
        }
    if command == "tag_alias_list":
        _reject_unknown(params, {"library_id"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
        }
    if command in {"tag_alias_upsert", "tag_alias_delete"}:
        _reject_unknown(
            params,
            {"library_id", "canonical_name", "canonical", "aliases"},
        )
        if "canonical_name" in params and "canonical" in params:
            raise BackendRequestError(
                "invalid_params",
                "canonical_name and canonical cannot be supplied together.",
            )
        raw_canonical = params.get("canonical_name", params.get("canonical"))
        if not isinstance(raw_canonical, str) or not raw_canonical.strip():
            raise BackendRequestError(
                "invalid_params", "canonical_name must be a non-empty string."
            )
        canonical_name = raw_canonical.strip()
        if len(canonical_name) > 256:
            raise BackendRequestError(
                "invalid_params", "canonical_name must be at most 256 characters."
            )
        if command == "tag_alias_delete":
            if "aliases" in params:
                raise BackendRequestError(
                    "invalid_params", "tag_alias_delete does not accept aliases."
                )
            return command, {
                "library_id": _optional_string(params, "library_id"),
                "canonical_name": canonical_name,
            }
        aliases = params.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise BackendRequestError(
                "invalid_params", "aliases must be an array of strings."
            )
        if len(aliases) > 100:
            raise BackendRequestError(
                "invalid_params", "aliases can contain at most 100 items."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "canonical_name": canonical_name,
            "aliases": aliases,
        }
    if command == "cluster_images":
        _reject_unknown(params, {"library_id", "scope", "cluster_types"})
        raw_scope = params.get("scope", "new_or_changed")
        scope = raw_scope.strip().lower() if isinstance(raw_scope, str) else ""
        if scope not in {"new_or_changed", "all"}:
            raise BackendRequestError(
                "invalid_params", "scope must be new_or_changed or all."
            )
        raw_types = params.get("cluster_types", list(DEFAULT_CLUSTER_TYPES))
        if not isinstance(raw_types, list) or not raw_types:
            raise BackendRequestError(
                "invalid_params", "cluster_types must be a non-empty array."
            )
        if not all(isinstance(item, str) and item.strip() for item in raw_types):
            raise BackendRequestError(
                "invalid_params", "cluster_types must contain non-empty strings."
            )
        cluster_types = list(dict.fromkeys(item.strip().lower() for item in raw_types))
        supported_types = {"exact", "perceptual", "semantic", "near_duplicate"}
        unknown_types = sorted(set(cluster_types) - supported_types)
        if unknown_types:
            raise BackendRequestError(
                "invalid_params",
                "cluster_types contains unsupported values.",
                details={
                    "values": unknown_types,
                    "supported": sorted(supported_types),
                },
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "scope": scope,
            "cluster_types": cluster_types,
        }
    if command == "cluster_list":
        _reject_unknown(params, {"library_id", "offset", "limit", "cluster_type"})
        raw_type = params.get("cluster_type")
        cluster_type = None
        if raw_type is not None:
            cluster_type = raw_type.strip().lower() if isinstance(raw_type, str) else ""
            if cluster_type not in {
                "all",
                "exact",
                "perceptual",
                "semantic",
                "single",
                "near_duplicate",
            }:
                raise BackendRequestError(
                    "invalid_params",
                    "cluster_type must be all, exact, perceptual, semantic, "
                    "single, or near_duplicate.",
                )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "offset": _non_negative_integer(params, "offset", 0),
            "limit": _bounded_positive_integer(params, "limit", 100, 500),
            "cluster_type": cluster_type,
        }
    if command == "cluster_detail":
        _reject_unknown(params, {"library_id", "cluster_id", "offset", "limit"})
        cluster_id = _required_string(params, "cluster_id")
        if len(cluster_id) > 256:
            raise BackendRequestError(
                "invalid_params", "cluster_id must be at most 256 characters."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "cluster_id": cluster_id,
            "offset": _non_negative_integer(params, "offset", 0),
            "limit": _bounded_positive_integer(params, "limit", 20, 2_000),
        }
    if command == "cluster_merge":
        _reject_unknown(params, {"library_id", "cluster_ids"})
        cluster_ids = _bounded_unique_strings(
            params.get("cluster_ids"),
            "cluster_ids",
            minimum=2,
            maximum=100,
            maximum_characters=256,
        )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "cluster_ids": cluster_ids,
        }
    if command == "cluster_split":
        _reject_unknown(params, {"library_id", "cluster_id", "doc_ids"})
        cluster_id = _required_string(params, "cluster_id")
        if len(cluster_id) > 256:
            raise BackendRequestError(
                "invalid_params", "cluster_id must be at most 256 characters."
            )
        doc_ids = _bounded_unique_strings(
            params.get("doc_ids"),
            "doc_ids",
            minimum=1,
            maximum=10_000,
            maximum_characters=1_024,
        )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "cluster_id": cluster_id,
            "doc_ids": doc_ids,
        }
    if command == "cluster_apply_identity":
        _reject_unknown(
            params,
            {
                "library_id",
                "cluster_id",
                "identity_category",
                "identity_value",
            },
        )
        cluster_id = _required_string(params, "cluster_id")
        if len(cluster_id) > 256:
            raise BackendRequestError(
                "invalid_params", "cluster_id must be at most 256 characters."
            )
        category = _required_string(params, "identity_category").casefold()
        if category not in {"real_person", "cosplayer", "character", "work"}:
            raise BackendRequestError(
                "invalid_params",
                "identity_category must be real_person, cosplayer, character, or work.",
            )
        value = _required_string(params, "identity_value")
        if len(value) > 256:
            raise BackendRequestError(
                "invalid_params", "identity_value must be at most 256 characters."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "cluster_id": cluster_id,
            "identity_category": category,
            "identity_value": value,
        }
    if command == "cluster_undo":
        _reject_unknown(params, {"library_id"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
        }
    if command == "active_learning_queue":
        _reject_unknown(params, {"library_id", "review_budget"})
        review_budget = _bounded_positive_integer(params, "review_budget", 25, 30)
        if review_budget < 20:
            raise BackendRequestError(
                "invalid_params", "review_budget must be between 20 and 30."
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "review_budget": review_budget,
        }
    if command == "active_learning_review":
        _reject_unknown(params, {"library_id", "queue_id", "decisions"})
        queue_id = _required_string(params, "queue_id")
        if len(queue_id) > 256:
            raise BackendRequestError(
                "invalid_params", "queue_id must be at most 256 characters."
            )
        raw_decisions = params.get("decisions")
        if not isinstance(raw_decisions, list) or not raw_decisions:
            raise BackendRequestError(
                "invalid_params", "decisions must be a non-empty array."
            )
        if len(raw_decisions) > 1_000 or not all(
            isinstance(item, dict) for item in raw_decisions
        ):
            raise BackendRequestError(
                "invalid_params", "decisions must contain at most 1000 objects."
            )
        learning_decisions: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for index, raw_decision in enumerate(raw_decisions):
            assert isinstance(raw_decision, dict)
            _reject_unknown(raw_decision, {"doc_id", "decision", "labels"})
            doc_id = _required_string(raw_decision, "doc_id")
            if len(doc_id) > 1_024:
                raise BackendRequestError(
                    "invalid_params",
                    f"decisions[{index}].doc_id is too long.",
                )
            if doc_id in seen_doc_ids:
                raise BackendRequestError(
                    "invalid_params", "decisions contains duplicate doc_id values."
                )
            seen_doc_ids.add(doc_id)
            raw_action = raw_decision.get("decision")
            action = raw_action.strip().lower() if isinstance(raw_action, str) else ""
            if action not in {"accept", "reject", "edit", "skip"}:
                raise BackendRequestError(
                    "invalid_params",
                    f"decisions[{index}].decision is unsupported.",
                )
            labels = _tags(raw_decision.get("labels"))
            if action == "edit" and not labels:
                raise BackendRequestError(
                    "invalid_params", "edit decisions require labels."
                )
            if action != "edit" and labels:
                raise BackendRequestError(
                    "invalid_params", "Only edit decisions may include labels."
                )
            learning_decisions.append(
                {"doc_id": doc_id, "decision": action, "labels": labels}
            )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "queue_id": queue_id,
            "decisions": learning_decisions,
        }
    if command == "active_learning_review_undo":
        _reject_unknown(params, {"library_id"})
        return command, {
            "library_id": _optional_string(params, "library_id"),
        }
    if command == "sync":
        _reject_unknown(
            params,
            {
                "folder",
                "recursive",
                "verify_hash",
                "dry_run",
                "allow_scope_change",
                "library_id",
            },
        )
        return command, {
            "library_id": _optional_string(params, "library_id"),
            "folder": _optional_string(params, "folder"),
            "recursive": _boolean(params, "recursive", True),
            "verify_hash": _boolean(params, "verify_hash", False),
            "dry_run": _boolean(params, "dry_run", False),
            "allow_scope_change": _boolean(params, "allow_scope_change", False),
        }
    if command == "search":
        _reject_unknown(
            params,
            {
                "text",
                "image",
                "top_k",
                "candidate_k",
                "tags",
                "tag_mode",
                "image_weight",
                "text_weight",
                "include_self",
                "show_low_confidence",
                "diversify_results",
                "sort_mode",
                "search_mode",
                "library_id",
                "library_ids",
            },
        )
        text = params.get("text")
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                raise BackendRequestError(
                    "invalid_params", "text must be a non-empty string when supplied."
                )
            text = text.strip()
        image = params.get("image")
        if image is not None:
            if not isinstance(image, str) or not image.strip():
                raise BackendRequestError(
                    "invalid_params", "image must be a non-empty string when supplied."
                )
            image = _validate_query_image(image, query_root)
        if text is None and image is None:
            raise BackendRequestError(
                "invalid_params", "search requires text, image, or both."
            )
        search_mode = params.get("search_mode", "semantic")
        if not isinstance(search_mode, str) or search_mode not in {
            "semantic",
            "tags",
        }:
            raise BackendRequestError(
                "invalid_params",
                "search_mode must be 'semantic' or 'tags'.",
            )
        if search_mode == "tags" and (text is None or image is not None):
            raise BackendRequestError(
                "invalid_params",
                "tag-only search requires text and does not accept an image.",
            )
        tag_mode = params.get("tag_mode", "all")
        if tag_mode not in {"all", "any"}:
            raise BackendRequestError(
                "invalid_params", "tag_mode must be 'all' or 'any'."
            )
        top_k = _positive_integer(params, "top_k", 10)
        requested_candidate_k = (
            None
            if params.get("candidate_k") is None
            else _positive_integer(params, "candidate_k", 1)
        )
        raw_sort_mode = params.get("sort_mode", "confidence")
        sort_mode = (
            raw_sort_mode.strip().lower() if isinstance(raw_sort_mode, str) else ""
        )
        if sort_mode not in {
            "relevance",
            "confidence",
            "diverse",
            "legacy",
        }:
            raise BackendRequestError(
                "invalid_params",
                "sort_mode must be relevance, confidence, diverse, or legacy.",
            )
        return command, {
            "library_ids": _search_library_ids(params),
            "text": text,
            "image": image,
            "search_mode": search_mode,
            "top_k": top_k,
            "candidate_k": confidence_candidate_limit(
                top_k,
                requested_candidate_k,
            ),
            "tags": _tags(params.get("tags")),
            "tag_mode": tag_mode,
            "image_weight": _number(params, "image_weight", 0.5),
            "text_weight": _number(params, "text_weight", 0.5),
            "include_self": _boolean(params, "include_self", False),
            "show_low_confidence": _boolean(params, "show_low_confidence", False),
            "diversify_results": _boolean(params, "diversify_results", True),
            "sort_mode": sort_mode,
        }
    if command in {"stats", "roots", "cache_clear"}:
        _reject_unknown(params, {"library_id"})
        return command, {"library_id": _optional_string(params, "library_id")}

    _reject_unknown(params, {"library_id", "days", "dry_run"})
    return command, {
        "library_id": _optional_string(params, "library_id"),
        "days": _non_negative_integer(params, "days", 7),
        "dry_run": _boolean(params, "dry_run", False),
    }


def _safe_activity_relative_path(value: Any) -> str:
    """Return one bounded portable relative path for durable activity logs."""

    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = value.replace("\\", "/").strip("/")
    if not candidate or ":" in candidate.split("/", 1)[0]:
        return ""
    parts = PurePosixPath(candidate).parts
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    normalized = "/".join(parts)
    return normalized if len(normalized) <= 4_096 else ""


def _validate_query_image(value: str, query_root: Path) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise BackendRequestError(
            "invalid_query_image",
            "The query image path must be absolute.",
        )
    try:
        root = query_root.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise BackendRequestError(
            "query_root_unavailable",
            "The configured query image directory is unavailable.",
        ) from exc
    if not root.is_dir():
        raise BackendRequestError(
            "query_root_unavailable",
            "The configured query image path is not a directory.",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise BackendRequestError(
            "invalid_query_image",
            "The query image must be an existing file.",
        ) from exc
    if resolved.parent != root or not resolved.is_file():
        raise BackendRequestError(
            "invalid_query_image",
            "The query image must be a direct child file of the query directory.",
        )
    return str(resolved)


def _reject_unknown(params: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(params) - allowed
    if unknown:
        raise BackendRequestError(
            "unknown_params",
            "params contains unsupported fields.",
            details={"fields": sorted(unknown)},
        )


def _required_string(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BackendRequestError(
            "invalid_params", f"{name} must be a non-empty string."
        )
    return value.strip()


def _optional_string(params: dict[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BackendRequestError(
            "invalid_params", f"{name} must be a non-empty string or null."
        )
    return value.strip()


def _bounded_unique_strings(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
    maximum_characters: int,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BackendRequestError(
            "invalid_params", f"{name} must be an array of strings."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = raw.strip()
        if (
            not item
            or len(item) > maximum_characters
            or any(ord(character) < 32 for character in item)
        ):
            raise BackendRequestError(
                "invalid_params",
                f"{name} values must contain 1 to {maximum_characters} "
                "characters without control characters.",
            )
        if item not in seen:
            seen.add(item)
            normalized.append(item)
        else:
            raise BackendRequestError(
                "invalid_params", f"{name} must not contain duplicate values."
            )
    if not minimum <= len(normalized) <= maximum:
        raise BackendRequestError(
            "invalid_params",
            f"{name} must contain {minimum} to {maximum} unique values.",
        )
    return normalized


def _optional_model(value: Any) -> str | None:
    """Normalize an optional model id before catalog-level validation."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BackendRequestError(
            "invalid_params",
            "model must be a non-empty string or null.",
        )
    normalized = value.strip()
    has_control_character = any(character in "\r\n\x00" for character in normalized)
    if len(normalized) > 128 or has_control_character:
        raise BackendRequestError(
            "invalid_params",
            "model must be at most 128 characters and contain no control characters.",
        )
    return normalized


def _search_library_ids(params: dict[str, Any]) -> list[str]:
    single = _optional_string(params, "library_id")
    value = params.get("library_ids")
    if single is not None and value not in (None, []):
        raise BackendRequestError(
            "invalid_params", "library_id and library_ids cannot be combined."
        )
    if single is not None:
        return [single]
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(library_id, str) and library_id.strip() for library_id in value
    ):
        raise BackendRequestError(
            "invalid_params", "library_ids must be an array of non-empty strings."
        )
    return [library_id.strip() for library_id in value]


def _boolean(params: dict[str, Any], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise BackendRequestError("invalid_params", f"{name} must be a boolean.")
    return value


def _positive_integer(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BackendRequestError(
            "invalid_params", f"{name} must be a positive integer."
        )
    return value


def _bounded_positive_integer(
    params: dict[str, Any], name: str, default: int, maximum: int
) -> int:
    value = _positive_integer(params, name, default)
    if value > maximum:
        raise BackendRequestError(
            "invalid_params",
            f"{name} must be at most {maximum}.",
        )
    return value


def _non_negative_integer(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackendRequestError(
            "invalid_params", f"{name} must be a non-negative integer."
        )
    return value


def _number(params: dict[str, Any], name: str, default: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendRequestError("invalid_params", f"{name} must be a number.")
    return float(value)


def _optional_positive_number(params: dict[str, Any], name: str) -> float | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise BackendRequestError(
            "invalid_params", f"{name} must be a positive number or null."
        )
    return float(value)


def _tags(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
        raise BackendRequestError(
            "invalid_params", "tags must be an array of strings or null."
        )
    return value


def _manual_tags(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
        raise BackendRequestError("invalid_params", "tags must be an array of strings.")
    if len(value) > 1_000:
        raise BackendRequestError(
            "invalid_params", "tags can contain at most 1000 items."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in value:
        cleaned = tag.strip()
        if (
            not cleaned
            or len(cleaned) > 4_096
            or any(ord(character) < 32 for character in cleaned)
        ):
            raise BackendRequestError(
                "invalid_params",
                "tags must contain non-empty strings of at most 4096 characters "
                "without control characters.",
            )
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _manual_tag_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackendRequestError("invalid_params", "selection must be an object.")
    mode = value.get("mode")
    if not isinstance(mode, str):
        raise BackendRequestError(
            "invalid_params", "selection.mode must be selected or folder."
        )
    normalized_mode = mode.strip().lower()
    if normalized_mode == "selected":
        _reject_unknown(value, {"mode", "doc_ids"})
        raw_ids = value.get("doc_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or not all(isinstance(doc_id, str) and doc_id.strip() for doc_id in raw_ids)
        ):
            raise BackendRequestError(
                "invalid_params",
                "selected mode requires a non-empty doc_ids array.",
            )
        if len(raw_ids) > 10_000:
            raise BackendRequestError(
                "invalid_params", "doc_ids can contain at most 10000 items."
            )
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            doc_id = raw_id.strip()
            if len(doc_id) > 256:
                raise BackendRequestError(
                    "invalid_params", "document ids must be at most 256 characters."
                )
            if doc_id not in seen:
                seen.add(doc_id)
                normalized_ids.append(doc_id)
        return {"mode": "selected", "doc_ids": normalized_ids}
    if normalized_mode == "folder":
        _reject_unknown(
            value,
            {"mode", "folder_key", "include_subfolders", "excluded_doc_ids"},
        )
        folder_key = value.get("folder_key")
        if (
            not isinstance(folder_key, str)
            or not folder_key.strip()
            or len(folder_key.strip()) > 8_192
        ):
            raise BackendRequestError(
                "invalid_params",
                "folder selection requires a valid folder_key.",
            )
        include_subfolders = value.get("include_subfolders", False)
        if not isinstance(include_subfolders, bool):
            raise BackendRequestError(
                "invalid_params", "include_subfolders must be a boolean."
            )
        raw_excluded = value.get("excluded_doc_ids", [])
        if not isinstance(raw_excluded, list) or not all(
            isinstance(doc_id, str) and doc_id.strip() for doc_id in raw_excluded
        ):
            raise BackendRequestError(
                "invalid_params", "excluded_doc_ids must be an array of strings."
            )
        if len(raw_excluded) > 10_000:
            raise BackendRequestError(
                "invalid_params",
                "excluded_doc_ids can contain at most 10000 items.",
            )
        excluded: list[str] = []
        seen = set()
        for raw_id in raw_excluded:
            doc_id = raw_id.strip()
            if len(doc_id) > 256:
                raise BackendRequestError(
                    "invalid_params", "document ids must be at most 256 characters."
                )
            if doc_id not in seen:
                seen.add(doc_id)
                excluded.append(doc_id)
        return {
            "mode": "folder",
            "folder_key": folder_key.strip(),
            "include_subfolders": include_subfolders,
            "excluded_doc_ids": excluded,
        }
    raise BackendRequestError(
        "invalid_params", "selection.mode must be selected or folder."
    )


def _attribute_result(
    value: Any,
    library: LibraryDefinition,
    *,
    result_preview_limit: int | None = None,
) -> Any:
    result = (
        _bounded_search_result(value, result_preview_limit)
        if result_preview_limit is not None
        else _json_safe(value)
    )
    if not isinstance(result, dict):
        return result
    attributed = dict(result)
    if not attributed.get("library_id"):
        attributed["library_id"] = library.library_id
    if not attributed.get("library_name"):
        attributed["library_name"] = library.name
    raw_hits = attributed.get("results")
    if isinstance(raw_hits, list):
        hits = []
        for raw_hit in raw_hits:
            if not isinstance(raw_hit, dict):
                hits.append(raw_hit)
                continue
            hit = dict(raw_hit)
            if not hit.get("library_id"):
                hit["library_id"] = library.library_id
            if not hit.get("library_name"):
                hit["library_name"] = library.name
            hits.append(hit)
        attributed["results"] = hits
    if not attributed.get("library_ids"):
        attributed["library_ids"] = [library.library_id]
    if not attributed.get("library_names"):
        attributed["library_names"] = [library.name]
    return attributed


def _bounded_search_result(value: Any, result_limit: int) -> Any:
    """Build a search-job summary without serializing every exported hit."""

    if isinstance(value, SearchReport):
        return _json_safe(search_report_payload(value, result_limit=result_limit))

    if isinstance(value, dict):
        raw = value
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            return _json_safe(value)
        converted = to_dict()
        if not isinstance(converted, dict):
            return _json_safe(converted)
        raw = converted

    bounded = {
        str(key): _json_safe(item) for key, item in raw.items() if key != "results"
    }
    raw_results = raw.get("results")
    if isinstance(raw_results, list):
        preview = raw_results[:result_limit]
        bounded["results"] = _json_safe(preview)
        declared_count = bounded.get("result_count")
        total_count = (
            declared_count
            if isinstance(declared_count, int) and not isinstance(declared_count, bool)
            else len(raw_results)
        )
        bounded["result_count"] = total_count
        bounded["results_inline_count"] = len(preview)
        bounded["results_truncated"] = total_count > len(preview)
    return bounded


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    return str(value)


def _result_failure_count(value: Any) -> int:
    """Read common partial-success fields without coupling jobs to report types."""

    if not isinstance(value, dict):
        return 0
    failed = value.get("failed")
    if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
        return failed
    failures = value.get("failures")
    if isinstance(failures, list):
        return len(failures)
    return 0


def _result_progress_counts(value: Any) -> tuple[int | None, int | None]:
    """Read final progress counters when a report exposes them explicitly."""

    if not isinstance(value, dict):
        return None, None

    def count(*keys: str) -> int | None:
        for key in keys:
            candidate = value.get(key)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                return candidate
        return None

    return (
        count("processed", "processed_count", "completed", "current"),
        count("total", "total_count", "candidate_count", "selected"),
    )


def _job_list_query(query: str) -> dict[str, Any]:
    values = parse_qs(query, keep_blank_values=True)
    unknown = set(values) - {"active", "limit"}
    if unknown:
        raise BackendRequestError(
            "unknown_fields",
            "The job-list query contains unsupported fields.",
            details={"fields": sorted(unknown)},
        )
    if any(len(items) != 1 for items in values.values()):
        raise BackendRequestError(
            "invalid_params", "Job-list query parameters cannot be repeated."
        )
    active: bool | None = None
    if "active" in values:
        raw_active = values["active"][0].strip().lower()
        if raw_active not in {"true", "false"}:
            raise BackendRequestError("invalid_params", "active must be true or false.")
        active = raw_active == "true"
    limit = 100
    if "limit" in values:
        try:
            limit = int(values["limit"][0])
        except ValueError as exc:
            raise BackendRequestError(
                "invalid_params", "limit must be an integer."
            ) from exc
    return {"active": active, "limit": limit}
