"""Low-blocking, durable policy for scheduling expensive Zvec optimization.

Zvec ``Collection.optimize()`` is a compaction/maintenance operation, not a
per-image durability boundary.  Calling it synchronously after every small
write makes interactive work wait behind increasingly expensive full-index
maintenance.  This module records cheap mutation counters in a separate local
SQLite database and decides when a later, idle worker should optimize.

The module deliberately does not import the repository or job manager.  The
service remains responsible for calling ``repository.optimize()`` after both
the policy decision and the runtime gate allow it.  Policy I/O is fail-closed:
an unavailable policy database suppresses optional optimization but never
fails the image operation that recorded the mutation.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, Protocol

OPTIMIZE_POLICY_SCHEMA_VERSION: Final = 1
DEFAULT_CHANGE_THRESHOLD: Final = 2_000
DEFAULT_DELETE_COUNT_THRESHOLD: Final = 500
DEFAULT_DELETE_RATIO_THRESHOLD: Final = 0.05
DEFAULT_DELETE_RATIO_MINIMUM_COUNT: Final = 100
DEFAULT_MAX_INTERVAL: Final = timedelta(hours=12)
DEFAULT_FAILURE_BACKOFF_INITIAL: Final = timedelta(minutes=1)
DEFAULT_FAILURE_BACKOFF_MAXIMUM: Final = timedelta(hours=1)
DEFAULT_SQLITE_TIMEOUT_SECONDS: Final = 0.25
MAX_OPERATION_ID_LENGTH: Final = 512
MAX_ATTEMPT_ID_LENGTH: Final = 512
MAX_PENDING_COUNT: Final = 9_000_000_000_000_000_000
MAX_ERROR_TEXT: Final = 800

OptimizeDecisionReason = Literal[
    "policy_unavailable",
    "no_pending_changes",
    "below_threshold",
    "failure_backoff",
    "change_threshold",
    "delete_count_threshold",
    "delete_ratio_threshold",
    "maximum_interval",
]
OptimizeGateReason = Literal[
    "ready",
    "cancelled",
    "queue_busy",
    "adapter_error",
]

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|passwd|secret|token)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_MODEL_KEY = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{6,}")
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\r\n\t\"'<>|]+")
_UNC_PATH = re.compile(r"\\\\[^\\\s]+\\[^\r\n\t\"'<>|]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s/]+/)+[^\s]*")


@dataclass(frozen=True)
class OptimizePolicyConfig:
    """Thresholds controlling when accumulated maintenance becomes worthwhile."""

    change_threshold: int = DEFAULT_CHANGE_THRESHOLD
    delete_count_threshold: int = DEFAULT_DELETE_COUNT_THRESHOLD
    delete_ratio_threshold: float = DEFAULT_DELETE_RATIO_THRESHOLD
    delete_ratio_minimum_count: int = DEFAULT_DELETE_RATIO_MINIMUM_COUNT
    max_interval: timedelta = DEFAULT_MAX_INTERVAL
    failure_backoff_initial: timedelta = DEFAULT_FAILURE_BACKOFF_INITIAL
    failure_backoff_maximum: timedelta = DEFAULT_FAILURE_BACKOFF_MAXIMUM
    sqlite_timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.change_threshold <= 0:
            raise ValueError("change_threshold must be positive")
        if self.delete_count_threshold <= 0:
            raise ValueError("delete_count_threshold must be positive")
        if not 0 < self.delete_ratio_threshold <= 1:
            raise ValueError("delete_ratio_threshold must be between 0 and 1")
        if self.delete_ratio_minimum_count <= 0:
            raise ValueError("delete_ratio_minimum_count must be positive")
        if self.max_interval <= timedelta(0):
            raise ValueError("max_interval must be positive")
        if self.failure_backoff_initial <= timedelta(0):
            raise ValueError("failure_backoff_initial must be positive")
        if self.failure_backoff_maximum < self.failure_backoff_initial:
            raise ValueError(
                "failure_backoff_maximum must not be shorter than the initial delay"
            )
        if not math.isfinite(self.sqlite_timeout_seconds):
            raise ValueError("sqlite_timeout_seconds must be finite")
        if self.sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be positive")


@dataclass(frozen=True)
class OptimizePolicyStatus:
    """Current durable counters and diagnostic state."""

    available: bool
    store_id: str = ""
    pending_changes: int = 0
    pending_deletes: int = 0
    generation: int = 0
    pending_since: datetime | None = None
    last_optimized_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_attempt_error: str = ""
    consecutive_failures: int = 0
    runtime_error: str = ""

    @property
    def total_pending(self) -> int:
        return self.pending_changes + self.pending_deletes


@dataclass(frozen=True)
class OptimizeDecision:
    """Immutable snapshot used to account for a later optimize result safely."""

    should_optimize: bool
    reason: OptimizeDecisionReason
    store_id: str
    generation: int
    pending_changes: int
    pending_deletes: int
    document_count: int
    delete_ratio: float
    evaluated_at: datetime
    eligible_reason: OptimizeDecisionReason | None = None
    retry_after_seconds: float | None = None
    policy_error: str = ""

    @property
    def total_pending(self) -> int:
        return self.pending_changes + self.pending_deletes


@dataclass(frozen=True)
class OptimizePolicyWriteResult:
    """Non-throwing outcome for optional policy persistence."""

    ok: bool
    applied: bool = False
    duplicate: bool = False
    error: str = ""


@dataclass(frozen=True)
class OptimizeGateDecision:
    """Result of the final cancellation/queue-idle check."""

    allowed: bool
    reason: OptimizeGateReason
    error: str = ""


class OptimizeRuntimeAdapter(Protocol):
    """Minimal service/job-manager view required immediately before optimize."""

    def cancellation_requested(self) -> bool:
        """Return true when the owning job should stop before maintenance."""

    def queue_is_idle(self) -> bool:
        """Return true only when no higher-priority work is waiting."""


def evaluate_optimize_gate(adapter: OptimizeRuntimeAdapter) -> OptimizeGateDecision:
    """Fail closed if cancellation, queued work, or adapter failure is observed."""

    try:
        if adapter.cancellation_requested():
            return OptimizeGateDecision(False, "cancelled")
        if not adapter.queue_is_idle():
            return OptimizeGateDecision(False, "queue_busy")
    except Exception as exc:
        return OptimizeGateDecision(
            False,
            "adapter_error",
            _sanitize_error(exc),
        )
    return OptimizeGateDecision(True, "ready")


class OptimizePolicyStore:
    """Durable mutation counters used to defer expensive Zvec maintenance.

    Each library should use its own small database, normally next to the
    library state database.  ``operation_id`` values make mutation accounting
    retry-safe without storing user paths or task payloads.
    """

    def __init__(
        self,
        path: Path,
        config: OptimizePolicyConfig | None = None,
    ) -> None:
        self.path = Path(path)
        self.config = config or OptimizePolicyConfig()
        self._initialized = False
        self._runtime_error = ""
        self._initialize_safely()

    @property
    def available(self) -> bool:
        return self._initialized and not self._runtime_error

    @property
    def runtime_error(self) -> str:
        return self._runtime_error

    def mark_changes(
        self,
        operation_id: str,
        *,
        changes: int = 0,
        deletes: int = 0,
        now: datetime | None = None,
    ) -> OptimizePolicyWriteResult:
        """Accumulate one mutation exactly once.

        ``changes`` counts inserts/updates while ``deletes`` counts removals.
        Reusing an operation ID with the same counts is a successful no-op;
        reusing it with different counts is rejected without changing totals.
        """

        operation_digest = _identifier_digest(
            operation_id,
            field="operation_id",
            maximum=MAX_OPERATION_ID_LENGTH,
        )
        _validate_count(changes, "changes")
        _validate_count(deletes, "deletes")
        observed_at = _timestamp(now)
        if changes == 0 and deletes == 0:
            return OptimizePolicyWriteResult(ok=True)
        if not self._ensure_initialized():
            return self._failed_write()

        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT changed_count, deleted_count "
                    "FROM optimize_change_operations WHERE operation_digest = ?",
                    (operation_digest,),
                ).fetchone()
                if duplicate is not None:
                    connection.commit()
                    if (
                        int(duplicate["changed_count"]) == changes
                        and int(duplicate["deleted_count"]) == deletes
                    ):
                        self._clear_runtime_error()
                        return OptimizePolicyWriteResult(
                            ok=True,
                            duplicate=True,
                        )
                    return OptimizePolicyWriteResult(
                        ok=False,
                        error="operation_id was reused with different mutation counts",
                    )

                state = self._state_row(connection)
                pending_changes = _checked_add(int(state["pending_changes"]), changes)
                pending_deletes = _checked_add(int(state["pending_deletes"]), deletes)
                generation = _checked_add(int(state["generation"]), 1)
                pending_since = state["pending_since_at"]
                if pending_since is None:
                    pending_since = observed_at
                connection.execute(
                    "INSERT INTO optimize_change_operations("
                    "operation_digest, generation, changed_count, deleted_count, "
                    "recorded_at) VALUES(?, ?, ?, ?, ?)",
                    (
                        operation_digest,
                        generation,
                        changes,
                        deletes,
                        observed_at,
                    ),
                )
                connection.execute(
                    "UPDATE optimize_policy_state SET "
                    "pending_changes = ?, pending_deletes = ?, generation = ?, "
                    "pending_since_at = ? WHERE singleton = 1",
                    (
                        pending_changes,
                        pending_deletes,
                        generation,
                        pending_since,
                    ),
                )
                connection.commit()
            self._clear_runtime_error()
            return OptimizePolicyWriteResult(ok=True, applied=True)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._record_runtime_error(exc)
            return self._failed_write()

    def status(self) -> OptimizePolicyStatus:
        """Read policy state; return an unavailable snapshot on local I/O failure."""

        if not self._ensure_initialized():
            return OptimizePolicyStatus(
                available=False,
                runtime_error=self._runtime_error,
            )
        try:
            with closing(self._connect()) as connection:
                row = self._state_row(connection)
            self._clear_runtime_error()
            return _status_from_row(row)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._record_runtime_error(exc)
            return OptimizePolicyStatus(
                available=False,
                runtime_error=self._runtime_error,
            )

    def should_optimize(
        self,
        document_count: int,
        *,
        now: datetime | None = None,
    ) -> OptimizeDecision:
        """Return a durable snapshot and the reason maintenance is (not) due."""

        _validate_count(document_count, "document_count")
        evaluated_at = _as_utc_datetime(now)
        status = self.status()
        if not status.available:
            return OptimizeDecision(
                should_optimize=False,
                reason="policy_unavailable",
                store_id="",
                generation=0,
                pending_changes=0,
                pending_deletes=0,
                document_count=document_count,
                delete_ratio=0.0,
                evaluated_at=evaluated_at,
                policy_error=status.runtime_error,
            )

        delete_denominator = document_count + status.pending_deletes
        delete_ratio = (
            status.pending_deletes / delete_denominator
            if delete_denominator > 0
            else 0.0
        )
        eligible_reason = self._eligible_reason(
            status,
            delete_ratio=delete_ratio,
            now=evaluated_at,
        )
        if eligible_reason in {"no_pending_changes", "below_threshold"}:
            return self._decision(
                status,
                document_count,
                delete_ratio,
                evaluated_at,
                reason=eligible_reason,
            )

        retry_after = self._failure_retry_after(status, evaluated_at)
        if retry_after > 0:
            return self._decision(
                status,
                document_count,
                delete_ratio,
                evaluated_at,
                reason="failure_backoff",
                eligible_reason=eligible_reason,
                retry_after_seconds=retry_after,
            )
        return self._decision(
            status,
            document_count,
            delete_ratio,
            evaluated_at,
            reason=eligible_reason,
            should_optimize=True,
            eligible_reason=eligible_reason,
        )

    def mark_attempt(
        self,
        decision: OptimizeDecision,
        attempt_id: str,
        *,
        now: datetime | None = None,
    ) -> OptimizePolicyWriteResult:
        """Persist the start of one optimize attempt before calling Zvec."""

        attempt_digest = _identifier_digest(
            attempt_id,
            field="attempt_id",
            maximum=MAX_ATTEMPT_ID_LENGTH,
        )
        attempted_at = _timestamp(now)
        validation = self._validate_decision(decision)
        if validation is not None:
            return validation
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = self._state_row(connection)
                if decision.generation <= int(state["last_success_generation"]):
                    connection.commit()
                    return OptimizePolicyWriteResult(ok=True, duplicate=True)
                existing = connection.execute(
                    "SELECT generation, status FROM optimize_attempts "
                    "WHERE attempt_digest = ?",
                    (attempt_digest,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    if int(existing["generation"]) == decision.generation:
                        return OptimizePolicyWriteResult(ok=True, duplicate=True)
                    return OptimizePolicyWriteResult(
                        ok=False,
                        error="attempt_id was reused for another policy generation",
                    )
                connection.execute(
                    "INSERT INTO optimize_attempts("
                    "attempt_digest, generation, status, attempted_at) "
                    "VALUES(?, ?, 'running', ?)",
                    (attempt_digest, decision.generation, attempted_at),
                )
                connection.execute(
                    "UPDATE optimize_policy_state SET last_attempt_at = ?, "
                    "last_attempt_generation = ? WHERE singleton = 1",
                    (attempted_at, decision.generation),
                )
                connection.commit()
            self._clear_runtime_error()
            return OptimizePolicyWriteResult(ok=True, applied=True)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._record_runtime_error(exc)
            return self._failed_write()

    def mark_success(
        self,
        decision: OptimizeDecision,
        *,
        attempt_id: str | None = None,
        now: datetime | None = None,
    ) -> OptimizePolicyWriteResult:
        """Acknowledge only the mutations included in ``decision``.

        Mutations recorded while optimize was running remain pending.  The
        generation guard makes repeated success callbacks harmless.
        """

        attempt_digest = (
            _identifier_digest(
                attempt_id,
                field="attempt_id",
                maximum=MAX_ATTEMPT_ID_LENGTH,
            )
            if attempt_id is not None
            else None
        )
        finished_at = _timestamp(now)
        validation = self._validate_decision(decision)
        if validation is not None:
            return validation
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = self._state_row(connection)
                last_success = int(state["last_success_generation"])
                if decision.generation <= last_success:
                    connection.commit()
                    return OptimizePolicyWriteResult(ok=True, duplicate=True)
                if decision.generation > int(state["generation"]):
                    connection.rollback()
                    return OptimizePolicyWriteResult(
                        ok=False,
                        error="decision generation is newer than durable policy state",
                    )
                if attempt_digest is not None:
                    existing_attempt = connection.execute(
                        "SELECT generation, status FROM optimize_attempts "
                        "WHERE attempt_digest = ?",
                        (attempt_digest,),
                    ).fetchone()
                    if (
                        existing_attempt is not None
                        and int(existing_attempt["generation"]) != decision.generation
                    ):
                        connection.rollback()
                        return OptimizePolicyWriteResult(
                            ok=False,
                            error=(
                                "attempt_id was reused for another policy generation"
                            ),
                        )
                    if (
                        existing_attempt is not None
                        and str(existing_attempt["status"]) == "failed"
                    ):
                        connection.rollback()
                        return OptimizePolicyWriteResult(
                            ok=False,
                            error=(
                                "a failed optimize attempt cannot be marked successful"
                            ),
                        )
                # Recompute the residual from generation-keyed operations.  It
                # remains correct even if callbacks arrive out of order and
                # avoids subtracting a stale snapshot from newer mutations.
                residual = connection.execute(
                    "SELECT COALESCE(SUM(changed_count), 0), "
                    "COALESCE(SUM(deleted_count), 0), MIN(recorded_at) "
                    "FROM optimize_change_operations WHERE generation > ?",
                    (decision.generation,),
                ).fetchone()
                if residual is None:
                    raise sqlite3.DatabaseError(
                        "optimize operation ledger could not be summarized"
                    )
                pending_changes = int(residual[0])
                pending_deletes = int(residual[1])
                pending_since = residual[2]
                if (pending_changes or pending_deletes) and pending_since is None:
                    pending_since = finished_at
                connection.execute(
                    "UPDATE optimize_policy_state SET "
                    "pending_changes = ?, pending_deletes = ?, "
                    "pending_since_at = ?, last_optimized_at = ?, "
                    "last_attempt_at = ?, last_attempt_generation = ?, "
                    "last_success_generation = ?, last_error = '', "
                    "consecutive_failures = 0 WHERE singleton = 1",
                    (
                        pending_changes,
                        pending_deletes,
                        pending_since,
                        finished_at,
                        finished_at,
                        decision.generation,
                        decision.generation,
                    ),
                )
                if attempt_digest is not None:
                    connection.execute(
                        "INSERT INTO optimize_attempts("
                        "attempt_digest, generation, status, attempted_at, finished_at"
                        ") VALUES(?, ?, 'succeeded', ?, ?) "
                        "ON CONFLICT(attempt_digest) DO UPDATE SET "
                        "status = 'succeeded', finished_at = excluded.finished_at",
                        (
                            attempt_digest,
                            decision.generation,
                            finished_at,
                            finished_at,
                        ),
                    )
                connection.commit()
            self._clear_runtime_error()
            return OptimizePolicyWriteResult(ok=True, applied=True)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._record_runtime_error(exc)
            return self._failed_write()

    def mark_failure(
        self,
        decision: OptimizeDecision,
        error: object,
        *,
        attempt_id: str,
        now: datetime | None = None,
    ) -> OptimizePolicyWriteResult:
        """Record a sanitized, retry-safe failure without dropping mutations."""

        attempt_digest = _identifier_digest(
            attempt_id,
            field="attempt_id",
            maximum=MAX_ATTEMPT_ID_LENGTH,
        )
        failed_at = _timestamp(now)
        safe_error = _sanitize_error(error)
        validation = self._validate_decision(decision)
        if validation is not None:
            return validation
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = self._state_row(connection)
                if decision.generation <= int(state["last_success_generation"]):
                    connection.commit()
                    return OptimizePolicyWriteResult(ok=True, duplicate=True)
                existing = connection.execute(
                    "SELECT generation, status FROM optimize_attempts "
                    "WHERE attempt_digest = ?",
                    (attempt_digest,),
                ).fetchone()
                if existing is not None:
                    if int(existing["generation"]) != decision.generation:
                        connection.rollback()
                        return OptimizePolicyWriteResult(
                            ok=False,
                            error="attempt_id was reused for another policy generation",
                        )
                    if str(existing["status"]) in {"failed", "succeeded"}:
                        connection.commit()
                        return OptimizePolicyWriteResult(ok=True, duplicate=True)
                    connection.execute(
                        "UPDATE optimize_attempts SET status = 'failed', "
                        "finished_at = ? WHERE attempt_digest = ?",
                        (failed_at, attempt_digest),
                    )
                else:
                    connection.execute(
                        "INSERT INTO optimize_attempts("
                        "attempt_digest, generation, status, attempted_at, finished_at"
                        ") VALUES(?, ?, 'failed', ?, ?)",
                        (
                            attempt_digest,
                            decision.generation,
                            failed_at,
                            failed_at,
                        ),
                    )
                connection.execute(
                    "UPDATE optimize_policy_state SET last_attempt_at = ?, "
                    "last_attempt_generation = ?, last_error = ?, "
                    "consecutive_failures = consecutive_failures + 1 "
                    "WHERE singleton = 1",
                    (failed_at, decision.generation, safe_error),
                )
                connection.commit()
            self._clear_runtime_error()
            return OptimizePolicyWriteResult(ok=True, applied=True)
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._record_runtime_error(exc)
            return self._failed_write()

    def _eligible_reason(
        self,
        status: OptimizePolicyStatus,
        *,
        delete_ratio: float,
        now: datetime,
    ) -> OptimizeDecisionReason:
        if status.total_pending == 0:
            return "no_pending_changes"
        if status.total_pending >= self.config.change_threshold:
            return "change_threshold"
        if status.pending_deletes >= self.config.delete_count_threshold:
            return "delete_count_threshold"
        if (
            status.pending_deletes >= self.config.delete_ratio_minimum_count
            and delete_ratio >= self.config.delete_ratio_threshold
        ):
            return "delete_ratio_threshold"
        if (
            status.pending_since is not None
            and now - status.pending_since >= self.config.max_interval
        ):
            return "maximum_interval"
        return "below_threshold"

    def _failure_retry_after(
        self,
        status: OptimizePolicyStatus,
        now: datetime,
    ) -> float:
        if status.consecutive_failures <= 0 or status.last_attempt_at is None:
            return 0.0
        exponent = min(status.consecutive_failures - 1, 62)
        delay_seconds = min(
            self.config.failure_backoff_initial.total_seconds() * (2**exponent),
            self.config.failure_backoff_maximum.total_seconds(),
        )
        retry_at = status.last_attempt_at + timedelta(seconds=delay_seconds)
        return max(0.0, (retry_at - now).total_seconds())

    def _decision(
        self,
        status: OptimizePolicyStatus,
        document_count: int,
        delete_ratio: float,
        evaluated_at: datetime,
        *,
        reason: OptimizeDecisionReason,
        should_optimize: bool = False,
        eligible_reason: OptimizeDecisionReason | None = None,
        retry_after_seconds: float | None = None,
    ) -> OptimizeDecision:
        return OptimizeDecision(
            should_optimize=should_optimize,
            reason=reason,
            store_id=status.store_id,
            generation=status.generation,
            pending_changes=status.pending_changes,
            pending_deletes=status.pending_deletes,
            document_count=document_count,
            delete_ratio=delete_ratio,
            evaluated_at=evaluated_at,
            eligible_reason=eligible_reason,
            retry_after_seconds=retry_after_seconds,
        )

    def _validate_decision(
        self,
        decision: OptimizeDecision,
    ) -> OptimizePolicyWriteResult | None:
        if not self._ensure_initialized():
            return self._failed_write()
        status = self.status()
        if not status.available:
            return self._failed_write()
        if not decision.store_id or decision.store_id != status.store_id:
            return OptimizePolicyWriteResult(
                ok=False,
                error="decision belongs to another optimize policy store",
            )
        if decision.generation < 0:
            return OptimizePolicyWriteResult(
                ok=False,
                error="decision generation must not be negative",
            )
        return None

    def _initialize_safely(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect(create_parent=False)) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, OPTIMIZE_POLICY_SCHEMA_VERSION}:
                    raise sqlite3.DatabaseError(
                        f"unsupported optimize policy schema version {version}"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS optimize_policy_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        store_id TEXT NOT NULL,
                        pending_changes INTEGER NOT NULL DEFAULT 0
                            CHECK(pending_changes >= 0),
                        pending_deletes INTEGER NOT NULL DEFAULT 0
                            CHECK(pending_deletes >= 0),
                        generation INTEGER NOT NULL DEFAULT 0
                            CHECK(generation >= 0),
                        pending_since_at REAL,
                        last_optimized_at REAL,
                        last_attempt_at REAL,
                        last_attempt_generation INTEGER,
                        last_success_generation INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0
                            CHECK(consecutive_failures >= 0)
                    );
                    CREATE TABLE IF NOT EXISTS optimize_change_operations (
                        operation_digest TEXT PRIMARY KEY,
                        generation INTEGER NOT NULL UNIQUE,
                        changed_count INTEGER NOT NULL CHECK(changed_count >= 0),
                        deleted_count INTEGER NOT NULL CHECK(deleted_count >= 0),
                        recorded_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_optimize_changes_generation
                        ON optimize_change_operations(generation);
                    CREATE TABLE IF NOT EXISTS optimize_attempts (
                        attempt_digest TEXT PRIMARY KEY,
                        generation INTEGER NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN ('running', 'succeeded', 'failed')),
                        attempted_at REAL NOT NULL,
                        finished_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_optimize_attempts_generation
                        ON optimize_attempts(generation, attempted_at);
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO optimize_policy_state("
                    "singleton, store_id) VALUES(1, ?)",
                    (uuid.uuid4().hex,),
                )
                connection.execute(
                    f"PRAGMA user_version = {OPTIMIZE_POLICY_SCHEMA_VERSION}"
                )
                connection.commit()
            self._initialized = True
            self._clear_runtime_error()
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            self._initialized = False
            self._record_runtime_error(exc)

    def _ensure_initialized(self) -> bool:
        if not self._initialized:
            self._initialize_safely()
        return self._initialized

    def _connect(self, *, create_parent: bool = True) -> sqlite3.Connection:
        if create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.config.sqlite_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        timeout_milliseconds = max(
            1,
            int(self.config.sqlite_timeout_seconds * 1000),
        )
        connection.execute(f"PRAGMA busy_timeout = {timeout_milliseconds}")
        return connection

    @staticmethod
    def _state_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM optimize_policy_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("optimize policy state row is missing")
        return row

    def _record_runtime_error(self, error: object) -> None:
        self._runtime_error = _sanitize_error(error)

    def _clear_runtime_error(self) -> None:
        self._runtime_error = ""

    def _failed_write(self) -> OptimizePolicyWriteResult:
        return OptimizePolicyWriteResult(
            ok=False,
            error=self._runtime_error or "optimize policy persistence is unavailable",
        )


def _status_from_row(row: sqlite3.Row) -> OptimizePolicyStatus:
    return OptimizePolicyStatus(
        available=True,
        store_id=str(row["store_id"]),
        pending_changes=int(row["pending_changes"]),
        pending_deletes=int(row["pending_deletes"]),
        generation=int(row["generation"]),
        pending_since=_datetime_from_timestamp(row["pending_since_at"]),
        last_optimized_at=_datetime_from_timestamp(row["last_optimized_at"]),
        last_attempt_at=_datetime_from_timestamp(row["last_attempt_at"]),
        last_attempt_error=_sanitize_error(str(row["last_error"])),
        consecutive_failures=int(row["consecutive_failures"]),
    )


def _identifier_digest(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_count(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    if value > MAX_PENDING_COUNT:
        raise ValueError(f"{field} is too large")


def _checked_add(current: int, increment: int) -> int:
    result = current + increment
    if result > MAX_PENDING_COUNT:
        raise ValueError("pending optimize mutation count is too large")
    return result


def _timestamp(value: datetime | None) -> float:
    return _as_utc_datetime(value).timestamp()


def _as_utc_datetime(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("datetime values must include timezone information")
    return resolved.astimezone(timezone.utc)


def _datetime_from_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        raise TypeError("persisted timestamp has an invalid type")
    return datetime.fromtimestamp(float(value), timezone.utc)


def _sanitize_error(error: object) -> str:
    try:
        text = str(error) or error.__class__.__name__
    except Exception:
        text = error.__class__.__name__
    # Explicit environment values are removed before generic patterns so a
    # custom key format cannot escape the normal token recognizers.
    for name in (
        "DASHSCOPE_API_KEY",
        "ZVEC_BACKEND_TOKEN",
        "ZVEC_GATEWAY_TOKEN",
    ):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _BEARER_TOKEN.sub("Bearer <redacted>", text)
    text = _MODEL_KEY.sub("<redacted>", text)
    text = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _WINDOWS_PATH.sub("<local-path>", text)
    text = _UNC_PATH.sub("<local-path>", text)
    text = _POSIX_PATH.sub("<local-path>", text)
    text = " ".join(text.replace("\x00", " ").split())
    return text[:MAX_ERROR_TEXT] or error.__class__.__name__
