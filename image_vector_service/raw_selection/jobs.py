"""Bounded in-process job state for progressive RAW import and export."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_MAX_RETAINED_JOBS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RawSelectionJob:
    id: str
    kind: str
    project_id: str
    log_id: str | None
    status: str = "queued"
    phase: str = "queued"
    progress: dict[str, int] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now_iso)
    completed_at: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def mark_running(self, phase: str) -> None:
        with self._lock:
            self.status = "running"
            self.phase = phase

    def update_progress(self, phase: str, values: Mapping[str, int]) -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES:
                return
            self.phase = phase
            self.progress.update({key: int(value) for key, value in values.items()})

    def finish(self, result: Mapping[str, Any]) -> None:
        with self._lock:
            self.status = "cancelled" if self.cancel_event.is_set() else "completed"
            self.phase = self.status
            self.result = dict(result)
            self.completed_at = _now_iso()

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.phase = "failed"
            self.error = error
            self.completed_at = _now_iso()

    def request_cancel(self) -> bool:
        with self._lock:
            if self.status in _TERMINAL_STATUSES:
                return False
            self.cancel_event.set()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "project_id": self.project_id,
                "log_id": self.log_id,
                "status": self.status,
                "phase": self.phase,
                "progress": dict(self.progress),
                "result": dict(self.result) if self.result is not None else None,
                "error": self.error,
                "created_at": self.created_at,
                "completed_at": self.completed_at,
            }


class RawSelectionJobRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: OrderedDict[str, RawSelectionJob] = OrderedDict()
        self._closed = False

    def start(
        self,
        *,
        kind: str,
        project_id: str,
        log_id: str | None,
        worker: Callable[[RawSelectionJob], None],
    ) -> RawSelectionJob:
        with self._lock:
            if self._closed:
                raise RuntimeError("RAW selection job registry is closed")
            job = RawSelectionJob(
                id=uuid.uuid4().hex,
                kind=kind,
                project_id=project_id,
                log_id=log_id,
            )
            self._jobs[job.id] = job
            self._trim_locked()

        def run() -> None:
            try:
                worker(job)
            except Exception as exc:
                job.fail(str(exc) or exc.__class__.__name__)

        thread = threading.Thread(
            target=run,
            name=f"raw-selection-{kind}-{job.id[:8]}",
            daemon=True,
        )
        job.thread = thread
        thread.start()
        return job

    def get(self, job_id: str) -> RawSelectionJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool | None:
        job = self.get(job_id)
        if job is None:
            return None
        return job.request_cancel()

    def cancel_project(self, project_id: str) -> int:
        with self._lock:
            jobs = tuple(
                job for job in self._jobs.values() if job.project_id == project_id
            )
        return sum(1 for job in jobs if job.request_cancel())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            jobs = tuple(self._jobs.values())
        for job in jobs:
            job.request_cancel()
        for job in jobs:
            thread = job.thread
            if thread is not None and thread.is_alive():
                thread.join()

    def _trim_locked(self) -> None:
        if len(self._jobs) <= _MAX_RETAINED_JOBS:
            return
        for job_id, job in tuple(self._jobs.items()):
            if len(self._jobs) <= _MAX_RETAINED_JOBS:
                break
            if job.snapshot()["status"] in _TERMINAL_STATUSES:
                self._jobs.pop(job_id, None)


__all__ = ["RawSelectionJob", "RawSelectionJobRegistry"]
