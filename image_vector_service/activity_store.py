"""Durable, bounded job history and structured activity logging.

The activity database is deliberately global to ConfigHome instead of being
stored in a library workspace.  Writes are serialized through one bounded
background queue so logging can never block indexing or model workers.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal

from zvec_webview.diagnostics import _sanitize_text as _sanitize_diagnostic_text

JsonObject = dict[str, Any]
LogLevel = Literal["debug", "info", "warning", "error"]

ACTIVITY_DATABASE_NAME: Final = "activity.sqlite3"
ACTIVITY_SCHEMA_VERSION: Final = 1

_LOG_LEVELS: Final = frozenset({"debug", "info", "warning", "error"})
_ACTIVE_JOB_STATUSES: Final = frozenset(
    {"queued", "pending", "running", "cancelling", "cancel_requested"}
)
_RECOVERABLE_JOB_STATUSES: Final = frozenset(
    {"queued", "pending", "running", "cancelling"}
)
_SENSITIVE_KEY: Final = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|session|token)",
    re.IGNORECASE,
)
_MODEL_CONTENT_KEY: Final = re.compile(
    r"(?:^|[_-])(?:prompt|response|completion|base64|binary|image[_-]?data|"
    r"request[_-]?body|response[_-]?body)(?:$|[_-])",
    re.IGNORECASE,
)
_EPHEMERAL_IMAGE_URL_KEY: Final = re.compile(
    r"^(?:thumbnail|image|preview)[_-]?url$",
    re.IGNORECASE,
)
_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DETAIL_KEY: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_PATH_KEY: Final = re.compile(
    r"(?:^|[_.:-])(?:path|file|directory|root)(?:$|[_.:-])", re.IGNORECASE
)
# A local path can live below any POSIX root, not only familiar directories
# such as /home or /tmp.  Match only at the start of text or after a clear
# separator.  Looking only at the character before ``/`` would misclassify
# valid Unicode relative paths such as ``角色/图片.jpg``.
_POSIX_ABSOLUTE_PATH: Final = re.compile(
    r"(^|[\s\"'=({\[;,，；：（【「『])/(?!/)(?:[^/\s\r\n\"'<>|]+/)*"
    r"[^/\s\r\n\"'<>|]+"
)
_DATA_URI: Final = re.compile(
    r"(?i)data:(?:image|application)/[^\s,;]+(?:;[^\s,]+)*;base64,[A-Za-z0-9+/=]+"
)
_LONG_ENCODED_VALUE: Final = re.compile(r"[A-Za-z0-9+/=]{256,}")
_BEARER_TOKEN: Final = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_INLINE_MODEL_CONTENT: Final = re.compile(
    r"(?i)\b((?:model[_-]?)?(?:prompt|response|completion))\s*[:=]"
    r"\s*[^;\r\n]+"
)
_KNOWN_SECRET_ENVIRONMENT_VARIABLES: Final = (
    "DASHSCOPE_API_KEY",
    "ZVEC_BACKEND_SESSION_TOKEN",
    "ZVEC_SESSION_TOKEN",
    "OPENAI_API_KEY",
)
_JOB_COLUMNS: Final = (
    "job_id",
    "task_type",
    "library_id",
    "library_name",
    "status",
    "submitted_at",
    "started_at",
    "finished_at",
    "processed",
    "total",
    "failed",
    "progress",
    "message",
    "result_summary",
    "error_code",
    "error_message",
    "updated_at",
)
_OPERATION_COLUMNS: Final = (
    "sequence",
    "timestamp",
    "level",
    "category",
    "event",
    "source",
    "library_id",
    "library_name",
    "job_id",
    "operation_id",
    "message",
    "details_json",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_history (
    job_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    library_id TEXT,
    library_name TEXT,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed >= 0),
    total INTEGER NOT NULL DEFAULT 0 CHECK (total >= 0),
    failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    message TEXT,
    result_summary TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_history_time
    ON job_history(submitted_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS idx_job_history_status_time
    ON job_history(status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_history_task_time
    ON job_history(task_type, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_history_library_time
    ON job_history(library_id, submitted_at DESC);

CREATE TABLE IF NOT EXISTS operation_logs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    category TEXT NOT NULL,
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    library_id TEXT,
    library_name TEXT,
    job_id TEXT,
    operation_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_operation_logs_time
    ON operation_logs(timestamp DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_level_category_time
    ON operation_logs(level, category, timestamp DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_category_time
    ON operation_logs(category, timestamp DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_library_time
    ON operation_logs(library_id, timestamp DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_job_time
    ON operation_logs(job_id, timestamp DESC, sequence DESC);
"""

_UPSERT_JOB = """
INSERT INTO job_history (
    job_id, task_type, library_id, library_name, status,
    submitted_at, started_at, finished_at, processed, total, failed,
    progress, message, result_summary, error_code, error_message, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id) DO UPDATE SET
    task_type = excluded.task_type,
    library_id = COALESCE(excluded.library_id, job_history.library_id),
    library_name = COALESCE(excluded.library_name, job_history.library_name),
    status = excluded.status,
    submitted_at = job_history.submitted_at,
    started_at = COALESCE(excluded.started_at, job_history.started_at),
    finished_at = CASE
        WHEN excluded.status IN (
            'queued', 'pending', 'running', 'cancelling', 'cancel_requested'
        ) THEN excluded.finished_at
        ELSE COALESCE(excluded.finished_at, job_history.finished_at)
    END,
    processed = excluded.processed,
    total = excluded.total,
    failed = excluded.failed,
    progress = excluded.progress,
    message = excluded.message,
    result_summary = excluded.result_summary,
    error_code = excluded.error_code,
    error_message = excluded.error_message,
    updated_at = excluded.updated_at
WHERE NOT (
       job_history.status IN (
           'succeeded', 'partial', 'needs_attention', 'failed', 'cancelled'
       )
       AND excluded.status IN (
           'queued', 'pending', 'running', 'cancelling', 'cancel_requested'
       )
    )
    AND NOT (
        job_history.status IN ('running', 'cancelling', 'cancel_requested')
        AND excluded.status IN ('queued', 'pending')
    )
    AND NOT (
        job_history.status IN ('cancelling', 'cancel_requested')
        AND excluded.status = 'running'
    )
"""

_INSERT_OPERATION = """
INSERT INTO operation_logs (
    timestamp, level, category, event, source, library_id, library_name,
    job_id, operation_id, message, details_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_OBSERVED_JOB = """
INSERT INTO job_history (
    job_id, task_type, library_id, library_name, status,
    submitted_at, started_at, finished_at, processed, total, failed,
    progress, message, result_summary, error_code, error_message, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id) DO NOTHING
"""


class InvalidActivityCursor(ValueError):
    """A pagination cursor is malformed or belongs to another endpoint."""


class ActivityStoreUnavailable(RuntimeError):
    """The activity database could not be opened for a read operation."""


@dataclass(frozen=True, slots=True)
class JobHistoryRecord:
    """One complete job snapshot suitable for an idempotent upsert."""

    job_id: str
    task_type: str
    status: str
    library_id: str | None = None
    library_name: str | None = None
    submitted_at: datetime | str | None = None
    started_at: datetime | str | None = None
    finished_at: datetime | str | None = None
    processed: int = 0
    total: int = 0
    failed: int = 0
    progress: float | int | None = None
    message: str | None = None
    result_summary: Any = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class OperationLogRecord:
    """One structured, bounded activity event."""

    level: LogLevel
    category: str
    event: str
    source: str
    message: str
    timestamp: datetime | str | None = None
    library_id: str | None = None
    library_name: str | None = None
    job_id: str | None = None
    operation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _QueuedWrite:
    kind: Literal["job", "observed_job", "operation", "maintenance"]
    payload: tuple[Any, ...]
    priority: int
    level: str | None = None
    coalesce_key: str | None = None


@dataclass(frozen=True, slots=True)
class _EnqueueResult:
    accepted: bool
    evicted: _QueuedWrite | None = None
    coalesced: bool = False


class _BoundedWriteQueue:
    """Small condition-backed deque with priority eviction and job coalescing."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._items: deque[_QueuedWrite] = deque()
        self._active = 0
        self._closed = False
        self._condition = threading.Condition()

    @property
    def depth(self) -> int:
        with self._condition:
            return len(self._items)

    def put(self, item: _QueuedWrite) -> _EnqueueResult:
        with self._condition:
            if self._closed:
                return _EnqueueResult(False)
            if item.coalesce_key is not None:
                for index, queued in enumerate(self._items):
                    if queued.coalesce_key == item.coalesce_key:
                        self._items[index] = item
                        return _EnqueueResult(True, coalesced=True)
            if len(self._items) < self._capacity:
                self._items.append(item)
                self._condition.notify()
                return _EnqueueResult(True)

            candidate_index: int | None = None
            candidate_priority = item.priority
            for index, queued in enumerate(self._items):
                if queued.priority < candidate_priority:
                    candidate_index = index
                    candidate_priority = queued.priority
            if candidate_index is None:
                return _EnqueueResult(False)
            evicted = self._items[candidate_index]
            del self._items[candidate_index]
            self._items.append(item)
            self._condition.notify()
            return _EnqueueResult(True, evicted=evicted)

    def get_batch(self, maximum: int) -> list[_QueuedWrite] | None:
        with self._condition:
            while not self._items and not self._closed:
                self._condition.wait()
            if not self._items and self._closed:
                return None
            count = min(maximum, len(self._items))
            batch = [self._items.popleft() for _ in range(count)]
            self._active += count
            return batch

    def complete(self, count: int) -> None:
        with self._condition:
            self._active -= count
            self._condition.notify_all()

    def flush(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else _monotonic() + timeout
        with self._condition:
            while self._items or self._active:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class ActivityStore:
    """ConfigHome activity database with non-blocking, failure-isolated writes."""

    def __init__(
        self,
        config_home: str | Path,
        *,
        queue_capacity: int = 2_048,
        write_batch_size: int = 32,
        log_retention_days: int = 30,
        log_max_rows: int = 50_000,
        job_max_rows: int = 10_000,
        cleanup_batch_size: int = 500,
        cleanup_every_writes: int = 500,
        details_max_bytes: int = 16 * 1024,
        diagnostic_mode: bool = False,
        redactions: Sequence[str] = (),
        sqlite_timeout_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        recover_interrupted: bool = True,
        auto_start: bool = True,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if not 1 <= write_batch_size <= queue_capacity:
            raise ValueError("write_batch_size must be between 1 and queue_capacity")
        for name, value in (
            ("log_retention_days", log_retention_days),
            ("log_max_rows", log_max_rows),
            ("job_max_rows", job_max_rows),
            ("cleanup_batch_size", cleanup_batch_size),
            ("cleanup_every_writes", cleanup_every_writes),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if not 1_024 <= details_max_bytes <= 256 * 1024:
            raise ValueError("details_max_bytes must be between 1 KiB and 256 KiB")
        if sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be positive")
        if not isinstance(recover_interrupted, bool):
            raise TypeError("recover_interrupted must be a boolean")

        self._config_home = Path(config_home).expanduser().resolve()
        self._path = self._config_home / ACTIVITY_DATABASE_NAME
        self._queue = _BoundedWriteQueue(queue_capacity)
        self._write_batch_size = write_batch_size
        self._log_retention_days = log_retention_days
        self._log_max_rows = log_max_rows
        self._job_max_rows = job_max_rows
        self._cleanup_batch_size = cleanup_batch_size
        self._cleanup_every_writes = cleanup_every_writes
        self._details_max_bytes = details_max_bytes
        self._diagnostic_mode = diagnostic_mode
        self._sqlite_timeout_seconds = sqlite_timeout_seconds
        self._recover_interrupted_on_start = recover_interrupted
        self._clock = clock or _utc_now
        environment_redactions = tuple(
            env_secret
            for name in _KNOWN_SECRET_ENVIRONMENT_VARIABLES
            if (env_secret := os.getenv(name, "").strip())
        )
        self._redactions = tuple(
            dict.fromkeys(
                value
                for value in (*redactions, *environment_redactions)
                if isinstance(value, str) and value
            )
        )
        self._metrics_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._metrics: dict[str, int] = {
            "submitted_jobs": 0,
            "submitted_operations": 0,
            "coalesced_jobs": 0,
            "written_jobs": 0,
            "written_operations": 0,
            "write_failures": 0,
            "input_failures": 0,
            "filtered_debug": 0,
            "dropped_jobs": 0,
            "dropped_debug": 0,
            "dropped_info": 0,
            "dropped_warning": 0,
            "dropped_error": 0,
        }
        self._pending_drop_counts = {
            "debug": 0,
            "info": 0,
            "warning": 0,
            "error": 0,
            "job": 0,
        }
        self._last_error: str | None = None
        self._available = False
        self._closed = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._recovered_jobs = 0
        self._writes_since_cleanup = 0
        needs_more_cleanup = False

        try:
            self._config_home.mkdir(parents=True, exist_ok=True)
            connection = self._open_connection()
            try:
                self._initialize_schema(connection)
                if self._recover_interrupted_on_start:
                    self._recovered_jobs = self._recover_interrupted(connection)
                needs_more_cleanup = self._prune_once(connection)
            finally:
                connection.close()
            self._available = True
        except (OSError, sqlite3.Error) as exc:
            self._remember_error(exc)

        if needs_more_cleanup:
            self._queue.put(
                _QueuedWrite("maintenance", (), priority=-1, coalesce_key="maintenance")
            )
        if auto_start:
            self.start()

    def __enter__(self) -> ActivityStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def config_home(self) -> Path:
        return self._config_home

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        return self._available

    @property
    def recovered_jobs(self) -> int:
        return self._recovered_jobs

    def redactions(self) -> tuple[str, ...]:
        """Return a snapshot for trusted in-process diagnostic sinks."""

        with self._lifecycle_lock:
            return tuple(self._redactions)

    def start(self) -> bool:
        """Start the writer once; unavailable stores remain safe no-ops."""

        with self._lifecycle_lock:
            if self._started:
                return self._available and not self._closed
            if self._closed or not self._available:
                return False
            self._started = True
            self._thread = threading.Thread(
                target=self._writer_main,
                name="zvec-activity-writer",
                daemon=True,
            )
            self._thread.start()
            return True

    def add_redactions(self, *values: str) -> None:
        """Add process-local secrets that future records must never persist.

        Credentials can change while the desktop process stays open.  Keeping
        the redaction set append-only ensures an old or newly saved credential
        is still removed from later exception and frontend diagnostic text.
        """

        additions = tuple(value for value in values if isinstance(value, str) and value)
        if not additions:
            return
        with self._lifecycle_lock:
            self._redactions = tuple(dict.fromkeys((*self._redactions, *additions)))

    def record_job(self, record: JobHistoryRecord | Mapping[str, Any]) -> bool:
        """Queue an authoritative backend snapshot without blocking."""

        return self._queue_job(record, observed=False)

    def record_observed_job(
        self,
        record: JobHistoryRecord | Mapping[str, Any],
    ) -> bool:
        """Insert a non-authoritative observer snapshot only when absent.

        This exists for facade contract tests and deliberately uses
        ``ON CONFLICT DO NOTHING``.  A UI poll, reconnect, or synthesized
        failure can therefore seed an otherwise empty fake history, but can
        never update a row written by the backend that owns ``backend.lock``.
        """

        return self._queue_job(record, observed=True)

    def _queue_job(
        self,
        record: JobHistoryRecord | Mapping[str, Any],
        *,
        observed: bool,
    ) -> bool:
        """Normalize and queue one authoritative or observer-only snapshot."""

        if self._closed or not self._available:
            return False
        try:
            payload = self._normalize_job(record)
        except (TypeError, ValueError):
            self._increment_metric("input_failures")
            return False
        item = _QueuedWrite(
            "observed_job" if observed else "job",
            payload,
            # Warning/error activity must survive queue pressure.  A job
            # snapshot remains more important than ordinary info chatter, but
            # may be evicted to retain actionable failures.
            priority=15,
            coalesce_key=(
                f"observed-job:{payload[0]}" if observed else f"job:{payload[0]}"
            ),
        )
        result = self._queue.put(item)
        if result.accepted:
            self._increment_metric("submitted_jobs")
            if result.coalesced:
                self._increment_metric("coalesced_jobs")
        else:
            self._note_dropped("job")
        if result.evicted is not None:
            self._note_evicted(result.evicted)
        return result.accepted

    def record_operation(self, record: OperationLogRecord | Mapping[str, Any]) -> bool:
        """Queue one sanitized activity event without blocking the caller."""

        if self._closed or not self._available:
            return False
        try:
            payload, level = self._normalize_operation(record)
        except (TypeError, ValueError):
            self._increment_metric("input_failures")
            return False
        if level == "debug" and not self._diagnostic_mode:
            self._increment_metric("filtered_debug")
            return False
        item = _QueuedWrite(
            "operation",
            payload,
            priority=_level_priority(level),
            level=level,
        )
        result = self._queue.put(item)
        if result.accepted:
            self._increment_metric("submitted_operations")
        else:
            self._note_dropped(level)
        if result.evicted is not None:
            self._note_evicted(result.evicted)
        return result.accepted

    def log(
        self,
        *,
        level: LogLevel,
        category: str,
        event: str,
        source: str,
        message: str,
        timestamp: datetime | str | None = None,
        library_id: str | None = None,
        library_name: str | None = None,
        job_id: str | None = None,
        operation_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Convenience wrapper for callers that do not need a dataclass."""

        return self.record_operation(
            OperationLogRecord(
                level=level,
                category=category,
                event=event,
                source=source,
                message=message,
                timestamp=timestamp,
                library_id=library_id,
                library_name=library_name,
                job_id=job_id,
                operation_id=operation_id,
                details=details or {},
            )
        )

    def request_retention_cleanup(
        self, *, wait: bool = False, timeout: float = 5.0
    ) -> bool:
        """Request one bounded retention pass on the writer thread."""

        if self._closed or not self._available:
            return False
        accepted = self._queue.put(
            _QueuedWrite("maintenance", (), priority=-1, coalesce_key="maintenance")
        ).accepted
        if not accepted:
            return False
        return self.flush(timeout) if wait else True

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Wait for accepted writes; normal record calls themselves never wait."""

        if not self._available:
            return False
        if not self._started:
            self.start()
        return self._queue.flush(timeout)

    def close(self, *, flush: bool = True, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            if flush and self._available:
                self.flush(timeout)
            self._closed = True
            self._queue.close()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

    def stats(self) -> JsonObject:
        """Return bounded health counters without exposing logged content."""

        with self._metrics_lock:
            metrics = dict(self._metrics)
            pending = dict(self._pending_drop_counts)
            last_error = self._last_error
        return {
            "available": self._available,
            "started": self._started,
            "closed": self._closed,
            "queue_depth": self._queue.depth,
            "recovered_jobs": self._recovered_jobs,
            "recover_interrupted_on_start": self._recover_interrupted_on_start,
            "pending_drop_counts": pending,
            "last_error": last_error,
            **metrics,
        }

    def list_job_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: str | Sequence[str] | None = None,
        task_type: str | Sequence[str] | None = None,
        library_id: str | None = None,
        query: str | None = None,
    ) -> JsonObject:
        """Return active jobs first, then stable reverse-chronological history."""

        limit = _validated_limit(limit)
        filters, parameters = self._job_filters(
            status=status,
            task_type=task_type,
            library_id=library_id,
            query=query,
        )
        base_where = f" WHERE {' AND '.join(filters)}" if filters else ""
        active_values = ", ".join("?" for _ in _ACTIVE_JOB_STATUSES)
        ranked = f"""
            SELECT {", ".join(_JOB_COLUMNS)},
                CASE WHEN status IN ({active_values}) THEN 0 ELSE 1 END
                    AS active_rank,
                COALESCE(finished_at, started_at, submitted_at, updated_at)
                    AS sort_time
            FROM job_history{base_where}
        """
        ranked_parameters: list[Any] = [*_ACTIVE_JOB_STATUSES, *parameters]
        cursor_clause = ""
        if cursor:
            rank, sort_time, job_id = _decode_cursor(cursor, "job_history", 3)
            if (
                rank not in {0, 1}
                or not isinstance(sort_time, str)
                or not isinstance(job_id, str)
            ):
                raise InvalidActivityCursor("Invalid job-history cursor payload")
            cursor_clause = """
                WHERE active_rank > ?
                   OR (active_rank = ? AND sort_time < ?)
                   OR (active_rank = ? AND sort_time = ? AND job_id < ?)
            """
            ranked_parameters.extend([rank, rank, sort_time, rank, sort_time, job_id])
        sql = f"""
            SELECT * FROM ({ranked}) AS ranked_jobs
            {cursor_clause}
            ORDER BY active_rank ASC, sort_time DESC, job_id DESC
            LIMIT ?
        """
        total_sql = f"SELECT COUNT(*) FROM job_history{base_where}"
        connection = self._read_connection()
        try:
            rows = connection.execute(sql, (*ranked_parameters, limit + 1)).fetchall()
            total_count = int(connection.execute(total_sql, parameters).fetchone()[0])
        except sqlite3.Error as exc:
            self._remember_error(exc)
            raise ActivityStoreUnavailable("Unable to query job history") from exc
        finally:
            connection.close()
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [self._job_row(row) for row in visible]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                "job_history",
                [int(last["active_rank"]), str(last["sort_time"]), last["job_id"]],
            )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_count": total_count,
        }

    def list_operation_logs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        level: str | Sequence[str] | None = None,
        category: str | Sequence[str] | None = None,
        library_id: str | None = None,
        job_id: str | None = None,
        query: str | None = None,
    ) -> JsonObject:
        """Return stable reverse-chronological structured activity events."""

        limit = _validated_limit(limit)
        filters, parameters = self._operation_filters(
            level=level,
            category=category,
            library_id=library_id,
            job_id=job_id,
            query=query,
        )
        if cursor:
            timestamp, sequence = _decode_cursor(cursor, "operation_logs", 2)
            if not isinstance(timestamp, str) or not isinstance(sequence, int):
                raise InvalidActivityCursor("Invalid activity-log cursor payload")
            filters.append("(timestamp < ? OR (timestamp = ? AND sequence < ?))")
            parameters.extend([timestamp, timestamp, sequence])
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"""
            SELECT {", ".join(_OPERATION_COLUMNS)}
            FROM operation_logs{where}
            ORDER BY timestamp DESC, sequence DESC
            LIMIT ?
        """

        count_filters, count_parameters = self._operation_filters(
            level=level,
            category=category,
            library_id=library_id,
            job_id=job_id,
            query=query,
        )
        count_where = f" WHERE {' AND '.join(count_filters)}" if count_filters else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(sql, (*parameters, limit + 1)).fetchall()
            total_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM operation_logs{count_where}",
                    count_parameters,
                ).fetchone()[0]
            )
        except sqlite3.Error as exc:
            self._remember_error(exc)
            raise ActivityStoreUnavailable("Unable to query activity logs") from exc
        finally:
            connection.close()
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [self._operation_row(row) for row in visible]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                "operation_logs", [last["timestamp"], int(last["sequence"])]
            )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_count": total_count,
        }

    def _writer_main(self) -> None:
        connection: sqlite3.Connection | None = None
        while True:
            batch = self._queue.get_batch(self._write_batch_size)
            if batch is None:
                break
            try:
                if connection is None:
                    connection = self._open_connection()
                self._write_batch(connection, batch)
                self._persist_drop_summary(connection)
            except (OSError, sqlite3.Error) as exc:
                self._increment_metric("write_failures")
                self._remember_error(exc)
                if connection is not None:
                    connection.close()
                    connection = None
            finally:
                self._queue.complete(len(batch))
        if connection is not None:
            connection.close()

    def _write_batch(
        self, connection: sqlite3.Connection, batch: Sequence[_QueuedWrite]
    ) -> None:
        jobs = 0
        operations = 0
        needs_more_cleanup = False
        with connection:
            for item in batch:
                if item.kind == "job":
                    connection.execute(_UPSERT_JOB, item.payload)
                    jobs += 1
                elif item.kind == "observed_job":
                    connection.execute(_INSERT_OBSERVED_JOB, item.payload)
                    jobs += 1
                elif item.kind == "operation":
                    connection.execute(_INSERT_OPERATION, item.payload)
                    operations += 1
                else:
                    needs_more_cleanup = (
                        self._prune_once(connection) or needs_more_cleanup
                    )
            self._writes_since_cleanup += jobs + operations
        if jobs:
            self._increment_metric("written_jobs", jobs)
        if operations:
            self._increment_metric("written_operations", operations)
        if self._writes_since_cleanup >= self._cleanup_every_writes:
            needs_more_cleanup = self._prune_once(connection) or needs_more_cleanup
            self._writes_since_cleanup = 0
        if needs_more_cleanup:
            self._queue.put(
                _QueuedWrite("maintenance", (), priority=-1, coalesce_key="maintenance")
            )

    def _persist_drop_summary(self, connection: sqlite3.Connection) -> None:
        counts = self._pending_drop_snapshot()
        if not any(counts.values()):
            return
        details = self._sanitize_details(counts)
        payload = (
            self._now(),
            "warning",
            "backend",
            "activity_queue_dropped",
            "activity_store",
            None,
            None,
            None,
            None,
            "Activity events were dropped because the bounded writer queue was full.",
            details,
        )
        with connection:
            connection.execute(_INSERT_OPERATION, payload)
        self._ack_pending_drops(counts)
        self._increment_metric("written_operations")

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version={ACTIVITY_SCHEMA_VERSION}")
        connection.commit()

    def _recover_interrupted(self, connection: sqlite3.Connection) -> int:
        now = self._now()
        placeholders = ", ".join("?" for _ in _RECOVERABLE_JOB_STATUSES)
        with connection:
            cursor = connection.execute(
                f"""
                UPDATE job_history
                SET status = 'interrupted',
                    finished_at = COALESCE(finished_at, ?),
                    message = 'Backend restarted before this task completed.',
                    error_code = 'backend_restarted',
                    error_message =
                        'The previous backend process stopped unexpectedly.',
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (now, now, *_RECOVERABLE_JOB_STATUSES),
            )
            recovered = max(0, int(cursor.rowcount))
            if recovered:
                details = json.dumps(
                    {"recovered_jobs": recovered},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    _INSERT_OPERATION,
                    (
                        now,
                        "warning",
                        "backend",
                        "jobs_interrupted_after_restart",
                        "activity_store",
                        None,
                        None,
                        None,
                        None,
                        f"Marked {recovered} unfinished job(s) as interrupted.",
                        details,
                    ),
                )
        return recovered

    def _prune_once(self, connection: sqlite3.Connection) -> bool:
        cutoff = _format_timestamp(
            self._clock() - timedelta(days=self._log_retention_days)
        )
        with connection:
            connection.execute(
                """
                DELETE FROM operation_logs
                WHERE sequence IN (
                    SELECT sequence FROM operation_logs
                    WHERE timestamp < ?
                    ORDER BY timestamp ASC, sequence ASC
                    LIMIT ?
                )
                """,
                (cutoff, self._cleanup_batch_size),
            )
            log_count = int(
                connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
            )
            log_overage = min(
                max(0, log_count - self._log_max_rows), self._cleanup_batch_size
            )
            if log_overage:
                connection.execute(
                    """
                    DELETE FROM operation_logs
                    WHERE sequence IN (
                        SELECT sequence FROM operation_logs
                        ORDER BY timestamp ASC, sequence ASC
                        LIMIT ?
                    )
                    """,
                    (log_overage,),
                )

            job_count = int(
                connection.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
            )
            job_overage = min(
                max(0, job_count - self._job_max_rows), self._cleanup_batch_size
            )
            if job_overage:
                active = tuple(_ACTIVE_JOB_STATUSES)
                placeholders = ", ".join("?" for _ in active)
                connection.execute(
                    f"""
                    DELETE FROM job_history
                    WHERE job_id IN (
                        SELECT job_id FROM job_history
                        WHERE status NOT IN ({placeholders})
                        ORDER BY COALESCE(finished_at, started_at, submitted_at) ASC,
                                 job_id ASC
                        LIMIT ?
                    )
                    """,
                    (*active, job_overage),
                )
            old_logs_remain = connection.execute(
                "SELECT 1 FROM operation_logs WHERE timestamp < ? LIMIT 1",
                (cutoff,),
            ).fetchone()
            log_count = int(
                connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
            )
            terminal_job_remains = None
            if job_count > self._job_max_rows:
                active = tuple(_ACTIVE_JOB_STATUSES)
                placeholders = ", ".join("?" for _ in active)
                terminal_job_remains = connection.execute(
                    f"""
                    SELECT 1 FROM job_history
                    WHERE status NOT IN ({placeholders})
                    LIMIT 1
                    """,
                    active,
                ).fetchone()
        return bool(
            old_logs_remain or log_count > self._log_max_rows or terminal_job_remains
        )

    def _normalize_job(
        self, record: JobHistoryRecord | Mapping[str, Any]
    ) -> tuple[Any, ...]:
        now = self._now()
        if isinstance(record, JobHistoryRecord):
            values: Mapping[str, Any] = {
                name: getattr(record, name)
                for name in JobHistoryRecord.__dataclass_fields__
            }
        elif isinstance(record, Mapping):
            values = record
        else:
            raise TypeError("job record must be a mapping or JobHistoryRecord")

        params = values.get("params")
        params = params if isinstance(params, Mapping) else {}
        progress_value = values.get("progress")
        progress_mapping = progress_value if isinstance(progress_value, Mapping) else {}
        result = values.get("result")
        result_mapping = result if isinstance(result, Mapping) else {}
        error = values.get("error")
        error_mapping = error if isinstance(error, Mapping) else {}

        job_id = _required_identifier(
            values.get("job_id") or values.get("id"), "job_id"
        )
        task_type = _required_identifier(
            values.get("task_type") or values.get("command"), "task_type"
        )
        status = _required_identifier(values.get("status"), "status")
        library_id = _optional_identifier(
            values.get("library_id") or params.get("library_id")
        )
        library_name = self._optional_text(
            values.get("library_name") or params.get("library_name"), 256
        )

        processed = _nonnegative_integer(
            _first_alias(
                values,
                progress_mapping,
                result_mapping,
                keys=("processed", "current", "completed", "processed_count"),
            )
        )
        total = _nonnegative_integer(
            _first_alias(
                values,
                progress_mapping,
                result_mapping,
                keys=("total", "candidate_count", "total_count"),
            )
        )
        failed_value = _first_alias(
            values,
            progress_mapping,
            result_mapping,
            keys=("failed", "failure_count", "failed_count"),
        )
        failed = _nonnegative_integer(failed_value)
        progress = _normalized_progress(progress_value, processed, total)
        message = self._optional_text(
            values.get("message") or progress_mapping.get("message"), 2_000
        )
        result_summary = values.get("result_summary")
        if result_summary is None and result is not None:
            result_summary = result
        summary = self._summary_text(result_summary, 4_000)
        error_code = self._optional_text(
            values.get("error_code") or error_mapping.get("code"), 128
        )
        error_message = self._optional_text(
            values.get("error_message") or error_mapping.get("message"), 2_000
        )

        return (
            job_id,
            task_type,
            library_id,
            library_name,
            status,
            _canonical_timestamp(values.get("submitted_at"), now),
            _optional_timestamp(values.get("started_at")),
            _optional_timestamp(values.get("finished_at")),
            processed,
            total,
            failed,
            progress,
            message,
            summary,
            error_code,
            error_message,
            now,
        )

    def _normalize_operation(
        self, record: OperationLogRecord | Mapping[str, Any]
    ) -> tuple[tuple[Any, ...], str]:
        if isinstance(record, OperationLogRecord):
            values: Mapping[str, Any] = {
                name: getattr(record, name)
                for name in OperationLogRecord.__dataclass_fields__
            }
        elif isinstance(record, Mapping):
            values = record
        else:
            raise TypeError("operation record must be a mapping or OperationLogRecord")
        level_value = values.get("level")
        if not isinstance(level_value, str) or level_value not in _LOG_LEVELS:
            raise ValueError("invalid activity level")
        details = values.get("details", {})
        if not isinstance(details, Mapping):
            raise TypeError("activity details must be a mapping")
        payload = (
            _canonical_timestamp(values.get("timestamp"), self._now()),
            level_value,
            _required_identifier(values.get("category"), "category"),
            _required_identifier(values.get("event"), "event"),
            _required_identifier(values.get("source"), "source"),
            _optional_identifier(values.get("library_id")),
            self._optional_text(values.get("library_name"), 256),
            _optional_identifier(values.get("job_id")),
            _optional_identifier(values.get("operation_id")),
            self._required_text(values.get("message"), 4_000),
            self._sanitize_details(details),
        )
        return payload, level_value

    def _sanitize_details(self, details: Mapping[str, Any]) -> str:
        sanitized: JsonObject = {}
        truncated = False
        for index, (raw_key, raw_value) in enumerate(details.items()):
            if index >= 30:
                truncated = True
                break
            key = str(raw_key)
            if not _DETAIL_KEY.fullmatch(key):
                key = f"field_{index + 1}"
            if _EPHEMERAL_IMAGE_URL_KEY.fullmatch(key):
                # ImageRegistry IDs are process-local capabilities.  Persist
                # only library_id + relative_path and mint a fresh URL when the
                # activity page is read.
                continue
            if _SENSITIVE_KEY.search(key) or _MODEL_CONTENT_KEY.search(key):
                value: Any = "<redacted>"
            elif (
                isinstance(raw_value, str)
                and _PATH_KEY.search(key)
                and _looks_absolute_path(raw_value)
            ):
                value = "<local-path>"
            elif isinstance(raw_value, str) and _PATH_KEY.search(key):
                # A field explicitly identified as a relative path may contain
                # any language or punctuation.  It has already failed the
                # absolute-path check above, so avoid reinterpreting an inner
                # slash as the start of a host path.
                value = self._clean_text(
                    raw_value,
                    1_000,
                    redact_local_paths=False,
                )
            else:
                value = self._sanitize_detail_value(raw_value, depth=0)
            candidate = {**sanitized, key: value}
            encoded = _json_dumps(candidate)
            if len(encoded.encode("utf-8")) > self._details_max_bytes:
                truncated = True
                break
            sanitized = candidate
        if truncated:
            sanitized["_truncated"] = True
        encoded = _json_dumps(sanitized)
        while len(encoded.encode("utf-8")) > self._details_max_bytes:
            removable = next(
                (key for key in reversed(sanitized) if key != "_truncated"), None
            )
            if removable is None:
                return '{"_truncated":true}'
            sanitized.pop(removable)
            sanitized["_truncated"] = True
            encoded = _json_dumps(sanitized)
        return encoded

    def _sanitize_detail_value(self, value: Any, *, depth: int) -> Any:
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                return "<non-finite>"
            return value
        if isinstance(value, str):
            return self._clean_text(value, 1_000)
        if isinstance(value, bytes | bytearray | memoryview):
            return "<binary omitted>"
        if depth >= 3:
            return "<nested details omitted>"
        if isinstance(value, Mapping):
            result: JsonObject = {}
            for index, (raw_key, nested_value) in enumerate(value.items()):
                if index >= 20:
                    result["_truncated"] = True
                    break
                key = str(raw_key)
                if not _DETAIL_KEY.fullmatch(key):
                    key = f"field_{index + 1}"
                if _EPHEMERAL_IMAGE_URL_KEY.fullmatch(key):
                    continue
                if _SENSITIVE_KEY.search(key) or _MODEL_CONTENT_KEY.search(key):
                    result[key] = "<redacted>"
                elif (
                    isinstance(nested_value, str)
                    and _PATH_KEY.search(key)
                    and _looks_absolute_path(nested_value)
                ):
                    result[key] = "<local-path>"
                elif isinstance(nested_value, str) and _PATH_KEY.search(key):
                    result[key] = self._clean_text(
                        nested_value,
                        1_000,
                        redact_local_paths=False,
                    )
                else:
                    result[key] = self._sanitize_detail_value(
                        nested_value, depth=depth + 1
                    )
            return result
        if isinstance(value, Sequence):
            sequence_result = [
                self._sanitize_detail_value(item, depth=depth + 1)
                for item in value[:50]
            ]
            if len(value) > 50:
                sequence_result.append("<truncated>")
            return sequence_result
        return self._clean_text(str(value), 500)

    def _summary_text(self, value: Any, maximum: int) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return self._clean_text(value, maximum)
        if isinstance(value, Mapping):
            return self._clean_text(self._sanitize_details(value), maximum)
        if isinstance(value, bytes | bytearray | memoryview):
            return "<binary omitted>"
        return self._clean_text(str(value), maximum)

    def _required_text(self, value: Any, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("required activity text is missing")
        return self._clean_text(value, maximum)

    def _optional_text(self, value: Any, maximum: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        cleaned = self._clean_text(value, maximum)
        return cleaned or None

    def _clean_text(
        self,
        value: str,
        maximum: int,
        *,
        redact_local_paths: bool = True,
    ) -> str:
        # Reuse the frontend diagnostic sanitizer with extra look-ahead before
        # truncation so a secret crossing the final boundary is still redacted.
        safety_limit = min(64 * 1024, max(maximum + 1_024, maximum * 4))
        bounded = value[:safety_limit]
        bounded = _DATA_URI.sub("<encoded-data omitted>", bounded)
        bounded = _LONG_ENCODED_VALUE.sub("<encoded-data omitted>", bounded)
        # Apply this before newline normalization.  The shared sanitizer turns a
        # newline into the two characters ``\\n``; without this first pass, the
        # trailing ``n`` can hide the word boundary before ``Bearer``.
        bounded = _BEARER_TOKEN.sub("Bearer <redacted>", bounded)
        bounded = _INLINE_MODEL_CONTENT.sub(r"\1=<redacted>", bounded)
        cleaned = _sanitize_diagnostic_text(
            bounded,
            "activity",
            safety_limit,
            self._redactions,
        )
        if redact_local_paths:
            cleaned = _POSIX_ABSOLUTE_PATH.sub(_redact_posix_path, cleaned)
        if len(cleaned) > maximum:
            cleaned = cleaned[: max(0, maximum - 1)].rstrip() + "…"
        return cleaned

    def _job_filters(
        self,
        *,
        status: str | Sequence[str] | None,
        task_type: str | Sequence[str] | None,
        library_id: str | None,
        query: str | None,
    ) -> tuple[list[str], list[Any]]:
        filters: list[str] = []
        parameters: list[Any] = []
        _append_multi_filter(filters, parameters, "status", status)
        _append_multi_filter(filters, parameters, "task_type", task_type)
        if library_id:
            filters.append("library_id = ?")
            parameters.append(library_id.strip()[:128])
        if query and query.strip():
            pattern = _like_pattern(query)
            filters.append(
                "("
                + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\'"
                    for column in (
                        "job_id",
                        "task_type",
                        "library_id",
                        "library_name",
                        "message",
                        "error_code",
                        "error_message",
                    )
                )
                + ")"
            )
            parameters.extend([pattern] * 7)
        return filters, parameters

    def _operation_filters(
        self,
        *,
        level: str | Sequence[str] | None,
        category: str | Sequence[str] | None,
        library_id: str | None,
        job_id: str | None,
        query: str | None,
    ) -> tuple[list[str], list[Any]]:
        filters: list[str] = []
        parameters: list[Any] = []
        _append_multi_filter(filters, parameters, "level", level)
        _append_multi_filter(filters, parameters, "category", category)
        if library_id:
            filters.append("library_id = ?")
            parameters.append(library_id.strip()[:128])
        if job_id:
            filters.append("job_id = ?")
            parameters.append(job_id.strip()[:128])
        if query and query.strip():
            pattern = _like_pattern(query)
            filters.append(
                "("
                + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\'"
                    for column in (
                        "message",
                        "event",
                        "source",
                        "library_name",
                        "job_id",
                        "operation_id",
                        "details_json",
                    )
                )
                + ")"
            )
            parameters.extend([pattern] * 7)
        return filters, parameters

    def _job_row(self, row: sqlite3.Row) -> JsonObject:
        item = {column: row[column] for column in _JOB_COLUMNS}
        raw_summary = item["result_summary"]
        if isinstance(raw_summary, str):
            try:
                decoded_summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                decoded_summary = None
            item["result_summary"] = (
                decoded_summary
                if isinstance(decoded_summary, dict)
                else {"summary": raw_summary}
            )
        item["id"] = item["job_id"]
        item["command"] = item["task_type"]
        item["failure_count"] = item["failed"]
        item["is_active"] = int(row["active_rank"]) == 0
        return item

    @staticmethod
    def _operation_row(row: sqlite3.Row) -> JsonObject:
        item = {
            column: row[column]
            for column in _OPERATION_COLUMNS
            if column != "details_json"
        }
        try:
            details = json.loads(str(row["details_json"]))
        except (TypeError, ValueError):
            details = {"_unavailable": True}
        item["details"] = details if isinstance(details, dict) else {}
        return item

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._sqlite_timeout_seconds,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            f"PRAGMA busy_timeout={max(1, int(self._sqlite_timeout_seconds * 1_000))}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        if not self._available or not self._path.is_file():
            raise ActivityStoreUnavailable("Activity database is unavailable")
        try:
            connection = sqlite3.connect(
                self._path,
                timeout=self._sqlite_timeout_seconds,
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            timeout_milliseconds = max(1, int(self._sqlite_timeout_seconds * 1_000))
            connection.execute(f"PRAGMA busy_timeout={timeout_milliseconds}")
            return connection
        except sqlite3.Error as exc:
            self._remember_error(exc)
            raise ActivityStoreUnavailable("Activity database is unavailable") from exc

    def _now(self) -> str:
        return _format_timestamp(self._clock())

    def _increment_metric(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[name] += amount

    def _note_dropped(self, level: str) -> None:
        metric = "dropped_jobs" if level == "job" else f"dropped_{level}"
        with self._metrics_lock:
            if metric in self._metrics:
                self._metrics[metric] += 1
            self._pending_drop_counts[level] += 1

    def _note_evicted(self, item: _QueuedWrite) -> None:
        level = item.level if item.kind == "operation" else "job"
        self._note_dropped(level or "info")

    def _pending_drop_snapshot(self) -> dict[str, int]:
        with self._metrics_lock:
            return dict(self._pending_drop_counts)

    def _ack_pending_drops(self, counts: Mapping[str, int]) -> None:
        with self._metrics_lock:
            for key, count in counts.items():
                self._pending_drop_counts[key] = max(
                    0, self._pending_drop_counts[key] - count
                )

    def _remember_error(self, error: BaseException) -> None:
        # Never retain attacker-controlled multiline or absolute-path text in
        # health diagnostics.  The same sanitizer used for persisted events is
        # intentionally applied here as well.
        try:
            text = self._clean_text(f"{error.__class__.__name__}: {error}", 500)
        except Exception:
            text = error.__class__.__name__
        with self._metrics_lock:
            self._last_error = text


def _required_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _optional_identifier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _required_identifier(value, "identifier")


def _first_alias(*mappings: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for mapping in mappings:
        for key in keys:
            value = mapping.get(key)
            if value is not None:
                return value
    return None


def _nonnegative_integer(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), 2**63 - 1))
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalized_progress(value: Any, processed: int, total: int) -> float:
    if isinstance(value, Mapping):
        value = value.get("progress", value.get("fraction", value.get("percent")))
    if isinstance(value, bool):
        value = None
    if isinstance(value, int | float):
        progress = float(value)
        if progress > 1 and progress <= 100:
            progress /= 100
        if progress == progress and progress not in {float("inf"), float("-inf")}:
            return max(0.0, min(progress, 1.0))
    if total > 0:
        return max(0.0, min(processed / total, 1.0))
    return 0.0


def _canonical_timestamp(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    parsed = _parse_timestamp(value)
    return _format_timestamp(parsed) if parsed is not None else fallback


def _optional_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = _parse_timestamp(value)
    return _format_timestamp(parsed) if parsed is not None else None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    # Kept behind a function to make queue timeout behavior straightforward to
    # patch in focused tests without changing process-wide time functions.
    import time

    return time.monotonic()


def _level_priority(level: str) -> int:
    return {"debug": 0, "info": 10, "warning": 20, "error": 30}[level]


def _validated_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ValueError("limit must be an integer between 1 and 200")
    return value


def _append_multi_filter(
    filters: list[str],
    parameters: list[Any],
    column: str,
    value: str | Sequence[str] | None,
) -> None:
    if value is None:
        return
    raw_values = [value] if isinstance(value, str) else list(value)
    if any(not isinstance(item, str) for item in raw_values):
        raise ValueError(f"{column} filters must be strings")
    values = [item.strip()[:128] for item in raw_values if item.strip()]
    if not values:
        return
    filters.append(f"{column} IN ({', '.join('?' for _ in values)})")
    parameters.extend(values)


def _like_pattern(value: str) -> str:
    bounded = value.strip()[:256]
    escaped = bounded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _looks_absolute_path(value: str) -> bool:
    candidate = value.strip()
    return bool(
        candidate.startswith(("/", "\\\\"))
        or re.match(r"^[A-Za-z]:[\\/]", candidate)
        or candidate.lower().startswith("file://")
    )


def _redact_posix_path(match: re.Match[str]) -> str:
    """Keep the delimiter while replacing one absolute POSIX path."""

    return f"{match.group(1)}<local-path>"


def _encode_cursor(kind: str, values: Sequence[Any]) -> str:
    raw = json.dumps(
        {"v": 1, "k": kind, "p": list(values)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, kind: str, arity: int) -> list[Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1_024:
        raise InvalidActivityCursor("Invalid activity cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidActivityCursor("Invalid activity cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("k") != kind
        or not isinstance(payload.get("p"), list)
        or len(payload["p"]) != arity
    ):
        raise InvalidActivityCursor("Invalid activity cursor")
    return payload["p"]


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


__all__ = [
    "ACTIVITY_DATABASE_NAME",
    "ACTIVITY_SCHEMA_VERSION",
    "ActivityStore",
    "ActivityStoreUnavailable",
    "InvalidActivityCursor",
    "JobHistoryRecord",
    "OperationLogRecord",
]
