"""Bounded priority scheduling and single-flight for RAW selection.

Four fixed worker lanes isolate embedded ARW, JPEG, PNG and full ARW work.
Each lane owns a strictly bounded priority heap; there is no executor-owned
unbounded queue.  Scope generations cancel queued work and provide a guard
that a cache writer can hold across its final atomic replace.
"""

from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable, Hashable, Mapping
from concurrent.futures import CancelledError, Future
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import IntEnum
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")

ARW_EMBED_WORKERS = 12
JPG_THUMB_WORKERS = 12
PNG_THUMB_WORKERS = 6
ARW_FULL_WORKERS = 3

_DEFAULT_SCOPE = "global"
_QUEUE_CAPACITY_MULTIPLIER = 4
_POOL_ARW_EMBED = "arw_embed"
_POOL_JPG_THUMB = "jpg_thumbnail"
_POOL_PNG_THUMB = "png_thumbnail"
_POOL_ARW_FULL = "arw_full"
_POOL_NAMES = (
    _POOL_ARW_EMBED,
    _POOL_JPG_THUMB,
    _POOL_PNG_THUMB,
    _POOL_ARW_FULL,
)


class TaskPriority(IntEnum):
    """Fixed requirement order; lower numeric values run first."""

    CURRENT = 0
    COMPARE = 1
    VISIBLE = 2
    OVERSCAN = 3
    BACKGROUND = 4


_PRIORITY_NAMES = {
    "current": TaskPriority.CURRENT,
    "compare": TaskPriority.COMPARE,
    "visible": TaskPriority.VISIBLE,
    "overscan": TaskPriority.OVERSCAN,
    "background": TaskPriority.BACKGROUND,
}


class SchedulerError(RuntimeError):
    """Base class for structured scheduler failures."""


class SchedulerQueueFull(SchedulerError):
    """A bounded lane cannot admit work without evicting equal/higher priority."""


class SchedulerShutdown(SchedulerError):
    """Work was submitted after shutdown began."""


class SchedulerShutdownTimeout(SchedulerError):
    """One or more fixed workers did not join within the requested timeout."""


class StaleGeneration(CancelledError):
    """The work belongs to a scope generation that is no longer current."""


@dataclass(frozen=True, slots=True)
class GenerationToken:
    scope: Hashable
    generation: int


@dataclass(frozen=True, slots=True)
class PoolMetrics:
    workers: int
    queue_capacity: int
    queued: int
    running: int
    submitted: int
    completed: int
    failed: int
    cancelled: int
    rejected: int
    evicted: int
    peak_queued: int
    peak_running: int


@dataclass(slots=True)
class ScheduledTask(Generic[T]):
    """A scheduled future plus its immutable generation and priority."""

    future: Future[T]
    token: GenerationToken
    priority: TaskPriority
    joined_single_flight: bool
    _cancel: Callable[[], bool]

    def cancel(self) -> bool:
        return self._cancel()

    def result(self, timeout: float | None = None) -> T:
        return self.future.result(timeout=timeout)


@dataclass(slots=True)
class _PoolTask(Generic[T]):
    task_id: int
    priority: TaskPriority
    sequence: int
    token: GenerationToken
    fn: Callable[[], T]
    future: Future[T]
    state: str = "queued"


class _BoundedPriorityPool:
    """Fixed threads consuming a bounded priority heap."""

    def __init__(
        self,
        name: str,
        *,
        workers: int,
        queue_capacity: int,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.name = name
        self.workers = workers
        self.queue_capacity = queue_capacity
        self._condition = threading.Condition(threading.Lock())
        self._heap: list[tuple[int, int, int]] = []
        self._queued: dict[int, _PoolTask[Any]] = {}
        self._running: dict[int, _PoolTask[Any]] = {}
        self._next_task_id = 0
        self._next_sequence = 0
        self._shutdown = False
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._rejected = 0
        self._evicted = 0
        self._peak_queued = 0
        self._peak_running = 0
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"rs-{name}-{index + 1}",
                daemon=False,
            )
            for index in range(workers)
        )
        for thread in self._threads:
            thread.start()

    def submit(
        self,
        fn: Callable[[], T],
        *,
        priority: TaskPriority,
        token: GenerationToken,
        future: Future[T] | None = None,
    ) -> int:
        selected_future = future or Future[T]()
        evicted_future: Future[Any] | None = None
        with self._condition:
            if self._shutdown:
                raise SchedulerShutdown(f"scheduler lane {self.name} is shut down")
            self._discard_externally_cancelled_locked()
            if len(self._queued) >= self.queue_capacity:
                evicted = self._eviction_candidate_locked(priority)
                if evicted is None:
                    self._rejected += 1
                    raise SchedulerQueueFull(
                        f"scheduler lane {self.name} queue is full"
                    )
                self._queued.pop(evicted.task_id, None)
                evicted.state = "cancelled"
                self._cancelled += 1
                self._evicted += 1
                evicted_future = evicted.future

            self._next_task_id += 1
            self._next_sequence += 1
            task = _PoolTask(
                task_id=self._next_task_id,
                priority=priority,
                sequence=self._next_sequence,
                token=token,
                fn=fn,
                future=selected_future,
            )
            self._queued[task.task_id] = task
            heapq.heappush(
                self._heap,
                (int(task.priority), task.sequence, task.task_id),
            )
            self._submitted += 1
            self._peak_queued = max(self._peak_queued, len(self._queued))
            self._condition.notify()
        if evicted_future is not None:
            evicted_future.cancel()
        return task.task_id

    def promote(self, task_id: int, priority: TaskPriority) -> bool:
        with self._condition:
            task = self._queued.get(task_id)
            if task is None or priority >= task.priority:
                return False
            task.priority = priority
            heapq.heappush(
                self._heap,
                (int(task.priority), task.sequence, task.task_id),
            )
            self._condition.notify()
            return True

    def cancel_task(self, task_id: int) -> bool:
        future: Future[Any] | None = None
        with self._condition:
            task = self._queued.pop(task_id, None)
            if task is None:
                return False
            task.state = "cancelled"
            self._cancelled += 1
            future = task.future
            self._condition.notify_all()
        return future.cancel()

    def cancel_where(self, predicate: Callable[[GenerationToken], bool]) -> int:
        futures: list[Future[Any]] = []
        with self._condition:
            for task_id, task in tuple(self._queued.items()):
                if not predicate(task.token):
                    continue
                self._queued.pop(task_id, None)
                task.state = "cancelled"
                self._cancelled += 1
                futures.append(task.future)
            if futures:
                self._condition.notify_all()
        for future in futures:
            future.cancel()
        return len(futures)

    def metrics(self) -> PoolMetrics:
        with self._condition:
            self._discard_externally_cancelled_locked()
            return PoolMetrics(
                workers=self.workers,
                queue_capacity=self.queue_capacity,
                queued=len(self._queued),
                running=len(self._running),
                submitted=self._submitted,
                completed=self._completed,
                failed=self._failed,
                cancelled=self._cancelled,
                rejected=self._rejected,
                evicted=self._evicted,
                peak_queued=self._peak_queued,
                peak_running=self._peak_running,
            )

    def wait_idle(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._queued or self._running:
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0.0:
                    return False
                self._condition.wait(timeout=remaining)
                self._discard_externally_cancelled_locked()
            return True

    def shutdown(
        self,
        *,
        wait: bool,
        cancel_queued: bool,
        timeout: float | None,
    ) -> None:
        futures: list[Future[Any]] = []
        with self._condition:
            if not self._shutdown:
                self._shutdown = True
                if cancel_queued:
                    for task in self._queued.values():
                        task.state = "cancelled"
                        futures.append(task.future)
                    self._cancelled += len(futures)
                    self._queued.clear()
                self._condition.notify_all()
        for future in futures:
            future.cancel()
        if not wait:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            thread.join(timeout=remaining)
        alive = [thread.name for thread in self._threads if thread.is_alive()]
        if alive:
            raise SchedulerShutdownTimeout(
                f"scheduler lane {self.name} did not join: {', '.join(alive)}"
            )

    def _eviction_candidate_locked(
        self,
        incoming: TaskPriority,
    ) -> _PoolTask[Any] | None:
        candidates = [
            task
            for task in self._queued.values()
            if task.state == "queued" and task.priority > incoming
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: (int(task.priority), -task.sequence))

    def _discard_externally_cancelled_locked(self) -> None:
        for task_id, task in tuple(self._queued.items()):
            if not task.future.cancelled():
                continue
            self._queued.pop(task_id, None)
            task.state = "cancelled"
            self._cancelled += 1
        if len(self._heap) > len(self._queued) * 2 + 32:
            self._heap = [
                (int(task.priority), task.sequence, task.task_id)
                for task in self._queued.values()
            ]
            heapq.heapify(self._heap)

    def _next_task_locked(self) -> _PoolTask[Any] | None:
        while self._heap:
            priority, sequence, task_id = heapq.heappop(self._heap)
            task = self._queued.get(task_id)
            if task is None or task.state != "queued":
                continue
            if int(task.priority) != priority or task.sequence != sequence:
                continue
            self._queued.pop(task_id, None)
            task.state = "running"
            self._running[task_id] = task
            self._peak_running = max(self._peak_running, len(self._running))
            return task
        return None

    def _worker(self) -> None:
        while True:
            with self._condition:
                task = self._next_task_locked()
                while task is None:
                    if self._shutdown:
                        return
                    self._condition.wait()
                    self._discard_externally_cancelled_locked()
                    task = self._next_task_locked()

            if not task.future.set_running_or_notify_cancel():
                with self._condition:
                    self._running.pop(task.task_id, None)
                    task.state = "cancelled"
                    self._cancelled += 1
                    self._condition.notify_all()
                continue

            failed = False
            cancelled = False
            try:
                result = task.fn()
            except BaseException as exc:
                cancelled = isinstance(exc, CancelledError)
                failed = not cancelled
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)
            finally:
                with self._condition:
                    self._running.pop(task.task_id, None)
                    task.state = (
                        "cancelled"
                        if cancelled
                        else "failed"
                        if failed
                        else "completed"
                    )
                    if cancelled:
                        self._cancelled += 1
                    elif failed:
                        self._failed += 1
                    else:
                        self._completed += 1
                    self._condition.notify_all()


@dataclass(slots=True)
class _Flight:
    future: Future[Any]
    token: GenerationToken
    priority: TaskPriority
    pool: _BoundedPriorityPool
    task_id: int | None = None


class DecodeScheduler:
    """Own fixed bounded lanes, generations and single-flight state."""

    def __init__(
        self,
        *,
        worker_counts: Mapping[str, int] | None = None,
        queue_capacities: Mapping[str, int] | None = None,
    ) -> None:
        defaults = {
            _POOL_ARW_EMBED: ARW_EMBED_WORKERS,
            _POOL_JPG_THUMB: JPG_THUMB_WORKERS,
            _POOL_PNG_THUMB: PNG_THUMB_WORKERS,
            _POOL_ARW_FULL: ARW_FULL_WORKERS,
        }
        selected_workers = _validated_lane_values(
            worker_counts,
            defaults,
            label="worker_counts",
        )
        default_capacities = {
            name: workers * _QUEUE_CAPACITY_MULTIPLIER
            for name, workers in selected_workers.items()
        }
        selected_capacities = _validated_lane_values(
            queue_capacities,
            default_capacities,
            label="queue_capacities",
        )
        self._pools = {
            name: _BoundedPriorityPool(
                name,
                workers=selected_workers[name],
                queue_capacity=selected_capacities[name],
            )
            for name in _POOL_NAMES
        }
        self._lock = threading.RLock()
        self._generations: dict[Hashable, int] = {_DEFAULT_SCOPE: 0}
        self._inflight: dict[tuple[Any, ...], _Flight] = {}
        self._shutdown = False

    @property
    def generation(self) -> int:
        return self.capture_generation().generation

    def capture_generation(self, scope: Hashable = _DEFAULT_SCOPE) -> GenerationToken:
        _validate_scope(scope)
        with self._lock:
            generation = self._generations.setdefault(scope, 0)
            return GenerationToken(scope, generation)

    def is_current(self, token: GenerationToken) -> bool:
        with self._lock:
            return self._is_current_locked(token)

    @contextmanager
    def generation_guard(self, token: object):
        """Hold generation stable across a cache writer's final replace."""

        if not isinstance(token, GenerationToken):
            yield False
            return
        with self._lock:
            yield self._is_current_locked(token)

    def bump_generation(
        self,
        scope: Hashable = _DEFAULT_SCOPE,
        *,
        cancel_queued: bool = True,
    ) -> GenerationToken:
        _validate_scope(scope)
        with self._lock:
            generation = self._generations.setdefault(scope, 0) + 1
            self._generations[scope] = generation
        if cancel_queued:
            for pool in self._pools.values():
                pool.cancel_where(
                    lambda token: token.scope == scope and token.generation < generation
                )
        return GenerationToken(scope, generation)

    def cancel_scope(self, scope: Hashable) -> GenerationToken:
        return self.bump_generation(scope, cancel_queued=True)

    def submit(
        self,
        extension: str,
        kind: str,
        fn: Callable[[], T],
        *,
        priority: TaskPriority | str | int = TaskPriority.BACKGROUND,
        scope: Hashable = _DEFAULT_SCOPE,
        token: GenerationToken | None = None,
    ) -> ScheduledTask[T]:
        selected_priority = _coerce_priority(priority)
        selected_token = token or self.capture_generation(scope)
        self._validate_submission_token(scope, selected_token)
        pool = self._pool_for(extension, kind)

        def guarded() -> T:
            if not self.is_current(selected_token):
                raise StaleGeneration(
                    f"scope generation is stale: {selected_token.generation}"
                )
            result = fn()
            if not self.is_current(selected_token):
                raise StaleGeneration(
                    f"scope generation is stale: {selected_token.generation}"
                )
            return result

        future: Future[T] = Future()
        task_id = pool.submit(
            guarded,
            priority=selected_priority,
            token=selected_token,
            future=future,
        )
        if not self.is_current(selected_token):
            pool.cancel_task(task_id)
        return ScheduledTask(
            future=future,
            token=selected_token,
            priority=selected_priority,
            joined_single_flight=False,
            _cancel=lambda: pool.cancel_task(task_id),
        )

    def submit_single_flight(
        self,
        key: Hashable,
        extension: str,
        kind: str,
        fn: Callable[[], T],
        *,
        priority: TaskPriority | str | int = TaskPriority.VISIBLE,
        scope: Hashable = _DEFAULT_SCOPE,
        token: GenerationToken | None = None,
    ) -> ScheduledTask[T]:
        hash(key)
        selected_priority = _coerce_priority(priority)
        selected_token = token or self.capture_generation(scope)
        self._validate_submission_token(scope, selected_token)
        pool = self._pool_for(extension, kind)
        flight_key = (
            selected_token.scope,
            selected_token.generation,
            pool.name,
            kind.casefold(),
            key,
        )

        task_to_promote: int | None = None
        created = False
        with self._lock:
            existing = self._inflight.get(flight_key)
            if existing is not None:
                if selected_priority < existing.priority:
                    existing.priority = selected_priority
                    task_to_promote = existing.task_id
                future = cast(Future[T], existing.future)
            else:
                future = Future()
                existing = _Flight(
                    future=future,
                    token=selected_token,
                    priority=selected_priority,
                    pool=pool,
                )
                self._inflight[flight_key] = existing
                created = True

        if not created:
            if task_to_promote is not None:
                existing.pool.promote(task_to_promote, selected_priority)
            return ScheduledTask(
                future=cast(Future[T], existing.future),
                token=selected_token,
                priority=selected_priority,
                joined_single_flight=True,
                _cancel=lambda: False,
            )

        def guarded() -> T:
            if not self.is_current(selected_token):
                raise StaleGeneration(
                    f"scope generation is stale: {selected_token.generation}"
                )
            result = fn()
            if not self.is_current(selected_token):
                raise StaleGeneration(
                    f"scope generation is stale: {selected_token.generation}"
                )
            return result

        try:
            task_id = pool.submit(
                guarded,
                priority=selected_priority,
                token=selected_token,
                future=future,
            )
        except BaseException as exc:
            with self._lock:
                if self._inflight.get(flight_key) is existing:
                    self._inflight.pop(flight_key, None)
            if not future.done():
                future.set_exception(exc)
            raise

        with self._lock:
            existing.task_id = task_id
            promoted_priority = existing.priority
        if promoted_priority < selected_priority:
            pool.promote(task_id, promoted_priority)

        def release(_future: Future[Any]) -> None:
            with self._lock:
                if self._inflight.get(flight_key) is existing:
                    self._inflight.pop(flight_key, None)

        future.add_done_callback(release)
        if not self.is_current(selected_token):
            pool.cancel_task(task_id)
        return ScheduledTask(
            future=future,
            token=selected_token,
            priority=promoted_priority,
            joined_single_flight=False,
            _cancel=lambda: pool.cancel_task(task_id),
        )

    def run_single_flight(
        self,
        key: Hashable,
        extension: str,
        kind: str,
        fn: Callable[[], T],
        *,
        priority: TaskPriority | str | int = TaskPriority.VISIBLE,
        scope: Hashable = _DEFAULT_SCOPE,
        token: GenerationToken | None = None,
        timeout: float | None = None,
    ) -> T:
        scheduled = self.submit_single_flight(
            key,
            extension,
            kind,
            fn,
            priority=priority,
            scope=scope,
            token=token,
        )
        return scheduled.result(timeout=timeout)

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            inflight = len(self._inflight)
            generations = len(self._generations)
            shutdown = self._shutdown
        return {
            "shutdown": shutdown,
            "inflight_single_flight": inflight,
            "generation_scopes": generations,
            "pools": {
                name: asdict(pool.metrics()) for name, pool in self._pools.items()
            },
        }

    def has_pending_work(self) -> bool:
        metrics = self.metrics()
        return any(
            int(pool["queued"]) > 0 or int(pool["running"]) > 0
            for pool in metrics["pools"].values()
        )

    def wait_idle(self, timeout: float | None = None) -> bool:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        for pool in self._pools.values():
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if not pool.wait_idle(remaining):
                return False
        return True

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_queued: bool = True,
        timeout: float | None = None,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                for scope in tuple(self._generations):
                    self._generations[scope] += 1
        deadline = None if timeout is None else time.monotonic() + timeout
        for pool in self._pools.values():
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            pool.shutdown(
                wait=wait,
                cancel_queued=cancel_queued,
                timeout=remaining,
            )

    def _pool_for(self, extension: str, kind: str) -> _BoundedPriorityPool:
        ext = extension.casefold()
        selected_kind = kind.casefold()
        if selected_kind in {"full", "full_decode", "look"} and ext == ".arw":
            return self._pools[_POOL_ARW_FULL]
        if ext == ".arw":
            return self._pools[_POOL_ARW_EMBED]
        if ext == ".png":
            return self._pools[_POOL_PNG_THUMB]
        return self._pools[_POOL_JPG_THUMB]

    def _validate_submission_token(
        self,
        scope: Hashable,
        token: GenerationToken,
    ) -> None:
        _validate_scope(scope)
        if token.scope != scope:
            raise ValueError("generation token scope does not match submission scope")
        with self._lock:
            if self._shutdown:
                raise SchedulerShutdown("scheduler is shut down")
            if not self._is_current_locked(token):
                raise StaleGeneration(f"scope generation is stale: {token.generation}")

    def _is_current_locked(self, token: GenerationToken) -> bool:
        return (
            not self._shutdown
            and self._generations.get(token.scope, 0) == token.generation
        )


def _validated_lane_values(
    supplied: Mapping[str, int] | None,
    defaults: Mapping[str, int],
    *,
    label: str,
) -> dict[str, int]:
    values = dict(defaults)
    if supplied is not None:
        unknown = set(supplied) - set(_POOL_NAMES)
        if unknown:
            raise ValueError(f"{label} contains unknown lanes: {sorted(unknown)}")
        values.update(supplied)
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label}[{name!r}] must be a positive integer")
    return values


def _validate_scope(scope: Hashable) -> None:
    try:
        hash(scope)
    except TypeError as exc:
        raise TypeError("scope must be hashable") from exc


def _coerce_priority(value: TaskPriority | str | int) -> TaskPriority:
    if isinstance(value, TaskPriority):
        return value
    if isinstance(value, str):
        selected = _PRIORITY_NAMES.get(value.casefold())
        if selected is None:
            raise ValueError(f"unknown task priority: {value}")
        return selected
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("priority must be TaskPriority, string or integer")
    try:
        return TaskPriority(value)
    except ValueError as exc:
        raise ValueError(f"unknown task priority: {value}") from exc


__all__ = [
    "ARW_EMBED_WORKERS",
    "ARW_FULL_WORKERS",
    "DecodeScheduler",
    "GenerationToken",
    "JPG_THUMB_WORKERS",
    "PNG_THUMB_WORKERS",
    "PoolMetrics",
    "ScheduledTask",
    "SchedulerError",
    "SchedulerQueueFull",
    "SchedulerShutdown",
    "SchedulerShutdownTimeout",
    "StaleGeneration",
    "TaskPriority",
]
