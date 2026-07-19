"""Transactional history for active-learning review decisions.

The review workflow performs real tag and search-feedback writes in other
services.  This module deliberately knows nothing about models or collections;
it only records the intended decision, the result of each local write, and the
information needed to undo those writes after a process restart.

The caller supplies the SQLite path.  It may be the library state database or
an independent database.  The store does not change ``PRAGMA user_version`` so
it can coexist with the existing library schema.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, cast

ReviewDecision = Literal["accept", "edit", "reject", "skip"]
ReviewEntryStatus = Literal["pending", "applied", "failed", "skipped", "conflict"]
ReviewUndoStatus = Literal[
    "not_requested",
    "not_required",
    "pending",
    "undone",
    "failed",
    "conflict",
]
ReviewBatchStatus = Literal[
    "running",
    "completed",
    "partial",
    "failed",
    "interrupted",
    "undoing",
    "undone",
    "undo_partial",
]

ACTIVE_LEARNING_REVIEW_SCHEMA_VERSION: Final = 1
MAX_REVIEW_BATCH_SIZE: Final = 10_000
MAX_SNAPSHOT_BYTES: Final = 1024 * 1024
MAX_METADATA_BYTES: Final = 128 * 1024

_DECISIONS: Final = frozenset({"accept", "edit", "reject", "skip"})
_ENTRY_OUTCOMES: Final = frozenset({"applied", "failed", "skipped", "conflict"})
_UNDO_OUTCOMES: Final = frozenset({"undone", "failed", "conflict"})
_TERMINAL_BATCH_STATUSES: Final = frozenset(
    {"completed", "partial", "failed", "interrupted", "undone", "undo_partial"}
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS active_learning_review_batches (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    queue_id TEXT NOT NULL,
    library_id TEXT NOT NULL DEFAULT '',
    operation_id TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'running', 'completed', 'partial', 'failed', 'interrupted',
            'undoing', 'undone', 'undo_partial'
        )
    ),
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    applied_count INTEGER NOT NULL DEFAULT 0 CHECK (applied_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    conflict_count INTEGER NOT NULL DEFAULT 0 CHECK (conflict_count >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    undo_started_at TEXT,
    undone_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_active_learning_review_batches_latest
    ON active_learning_review_batches(sequence DESC);
CREATE INDEX IF NOT EXISTS idx_active_learning_review_batches_library_latest
    ON active_learning_review_batches(library_id, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_active_learning_review_batches_status_latest
    ON active_learning_review_batches(status, sequence DESC);

CREATE TABLE IF NOT EXISTS active_learning_review_entries (
    batch_id TEXT NOT NULL REFERENCES active_learning_review_batches(batch_id)
        ON DELETE CASCADE,
    item_order INTEGER NOT NULL CHECK (item_order >= 1),
    doc_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (
        decision IN ('accept', 'edit', 'reject', 'skip')
    ),
    requested_labels_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'applied', 'failed', 'skipped', 'conflict')
    ),
    before_snapshot_json TEXT,
    after_snapshot_json TEXT,
    feedback_event_id TEXT,
    requires_undo INTEGER NOT NULL DEFAULT 0 CHECK (requires_undo IN (0, 1)),
    error_code TEXT,
    error_message TEXT,
    applied_at TEXT,
    undo_status TEXT NOT NULL DEFAULT 'not_requested' CHECK (
        undo_status IN (
            'not_requested', 'not_required', 'pending', 'undone',
            'failed', 'conflict'
        )
    ),
    undo_error_code TEXT,
    undo_error_message TEXT,
    undone_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, doc_id),
    UNIQUE (batch_id, item_order)
);

CREATE INDEX IF NOT EXISTS idx_active_learning_review_entries_status
    ON active_learning_review_entries(batch_id, status, item_order);
CREATE INDEX IF NOT EXISTS idx_active_learning_review_entries_undo
    ON active_learning_review_entries(batch_id, undo_status, item_order);
"""


class ActiveLearningReviewStoreError(RuntimeError):
    """The review history database could not complete an operation."""


class ActiveLearningReviewValidationError(ValueError):
    """A review batch, item, outcome, or snapshot is invalid."""


class ActiveLearningReviewStateError(RuntimeError):
    """The requested transition is not valid for the persisted state."""


@dataclass(frozen=True, slots=True)
class ActiveLearningReviewItem:
    """One requested decision in a review batch."""

    doc_id: str
    decision: ReviewDecision
    labels: Sequence[str] = ()


class ActiveLearningReviewStore:
    """SQLite-backed active-learning review history and undo journal.

    Every public mutating method uses ``BEGIN IMMEDIATE`` and updates the batch
    counters in the same transaction as the entry.  A failed item is therefore
    recorded without changing the state of its siblings; the caller can keep
    processing the rest of the batch.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        sqlite_timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not math.isfinite(sqlite_timeout_seconds) or sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be positive and finite")
        self._path = Path(database_path).expanduser().resolve()
        self._sqlite_timeout_seconds = float(sqlite_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_connection() as connection:
                connection.executescript(_SCHEMA)
        except ActiveLearningReviewStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ActiveLearningReviewStoreError(
                f"Unable to initialize active-learning review history: {exc}"
            ) from exc

    @property
    def path(self) -> Path:
        return self._path

    def start_batch(
        self,
        *,
        queue_id: str,
        items: Sequence[ActiveLearningReviewItem | Mapping[str, object]],
        library_id: str = "",
        operation_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a running batch and all pending entries."""

        normalized_queue_id = _required_id(queue_id, "queue_id", maximum=256)
        normalized_library_id = (
            _optional_id(library_id, "library_id", maximum=256) or ""
        )
        normalized_operation_id = _optional_id(
            operation_id, "operation_id", maximum=256
        )
        normalized_batch_id = (
            _required_id(batch_id, "batch_id", maximum=256)
            if batch_id is not None
            else f"active-review-{uuid.uuid4().hex}"
        )
        normalized_items = [_normalize_item(item) for item in items]
        if not normalized_items:
            raise ActiveLearningReviewValidationError(
                "A review batch must contain at least one item."
            )
        if len(normalized_items) > MAX_REVIEW_BATCH_SIZE:
            raise ActiveLearningReviewValidationError(
                f"A review batch cannot exceed {MAX_REVIEW_BATCH_SIZE} items."
            )
        doc_ids = [item.doc_id for item in normalized_items]
        if len(set(doc_ids)) != len(doc_ids):
            raise ActiveLearningReviewValidationError(
                "A review batch cannot contain duplicate doc_id values."
            )
        metadata_json = _json_mapping(
            metadata or {}, "metadata", maximum_bytes=MAX_METADATA_BYTES
        )
        now = self._now()
        with self._write_connection() as connection:
            existing = connection.execute(
                "SELECT 1 FROM active_learning_review_batches WHERE batch_id = ?",
                (normalized_batch_id,),
            ).fetchone()
            if existing is not None:
                raise ActiveLearningReviewStateError(
                    f"Review batch already exists: {normalized_batch_id}"
                )
            connection.execute(
                """
                INSERT INTO active_learning_review_batches (
                    batch_id, queue_id, library_id, operation_id, schema_version,
                    status, total_count, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    normalized_batch_id,
                    normalized_queue_id,
                    normalized_library_id,
                    normalized_operation_id,
                    ACTIVE_LEARNING_REVIEW_SCHEMA_VERSION,
                    len(normalized_items),
                    metadata_json,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO active_learning_review_entries (
                    batch_id, item_order, doc_id, decision,
                    requested_labels_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    (
                        normalized_batch_id,
                        index,
                        item.doc_id,
                        item.decision,
                        json.dumps(
                            list(item.labels), ensure_ascii=False, separators=(",", ":")
                        ),
                        now,
                    )
                    for index, item in enumerate(normalized_items, start=1)
                ),
            )
        return self.get_batch(normalized_batch_id)

    def record_entry_outcome(
        self,
        batch_id: str,
        doc_id: str,
        *,
        status: ReviewEntryStatus,
        before_snapshot: Mapping[str, object] | None = None,
        after_snapshot: Mapping[str, object] | None = None,
        feedback_event_id: str | None = None,
        requires_undo: bool | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Record one terminal item outcome without affecting other items.

        Repeating the exact same terminal status is idempotent.  Replacing an
        already terminal outcome is rejected so the undo journal cannot be
        silently rewritten.
        """

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        normalized_doc_id = _required_id(doc_id, "doc_id", maximum=2048)
        if status not in _ENTRY_OUTCOMES:
            raise ActiveLearningReviewValidationError(
                f"Unsupported review entry status: {status}"
            )
        before_json = _optional_snapshot(before_snapshot, "before_snapshot")
        after_json = _optional_snapshot(after_snapshot, "after_snapshot")
        normalized_feedback_id = _optional_id(
            feedback_event_id, "feedback_event_id", maximum=256
        )
        normalized_error_code, normalized_error_message = _outcome_error(
            status, error_code, error_message
        )
        has_undo_payload = normalized_feedback_id is not None or (
            before_json is not None and after_json is not None
        )
        if requires_undo is None:
            normalized_requires_undo = bool(
                has_undo_payload and status in {"applied", "failed", "conflict"}
            )
        elif not isinstance(requires_undo, bool):
            raise ActiveLearningReviewValidationError(
                "requires_undo must be a boolean when provided."
            )
        else:
            normalized_requires_undo = requires_undo
        if normalized_requires_undo and not has_undo_payload:
            raise ActiveLearningReviewValidationError(
                "Undoable entries require a feedback_event_id or both snapshots."
            )

        now = self._now()
        with self._write_connection() as connection:
            batch = self._require_batch(connection, normalized_batch_id)
            if batch["status"] != "running":
                raise ActiveLearningReviewStateError(
                    f"Cannot record an item while batch status is {batch['status']}."
                )
            row = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND doc_id = ?
                """,
                (normalized_batch_id, normalized_doc_id),
            ).fetchone()
            if row is None:
                raise ActiveLearningReviewValidationError(
                    f"Review item was not found: {normalized_doc_id}"
                )
            if row["status"] != "pending":
                same_outcome = (
                    row["status"] == status
                    and row["before_snapshot_json"] == before_json
                    and row["after_snapshot_json"] == after_json
                    and row["feedback_event_id"] == normalized_feedback_id
                    and bool(row["requires_undo"]) == normalized_requires_undo
                    and row["error_code"] == normalized_error_code
                    and row["error_message"] == normalized_error_message
                )
                if same_outcome:
                    return self._entry_row(row)
                raise ActiveLearningReviewStateError(
                    "Review item already has a different terminal outcome: "
                    f"{row['status']}"
                )
            connection.execute(
                """
                UPDATE active_learning_review_entries SET
                    status = ?, before_snapshot_json = ?,
                    after_snapshot_json = ?, feedback_event_id = ?,
                    requires_undo = ?, error_code = ?, error_message = ?,
                    applied_at = ?, updated_at = ?
                WHERE batch_id = ? AND doc_id = ?
                """,
                (
                    status,
                    before_json,
                    after_json,
                    normalized_feedback_id,
                    int(normalized_requires_undo),
                    normalized_error_code,
                    normalized_error_message,
                    now,
                    now,
                    normalized_batch_id,
                    normalized_doc_id,
                ),
            )
            self._refresh_counts(connection, normalized_batch_id, now)
            updated = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND doc_id = ?
                """,
                (normalized_batch_id, normalized_doc_id),
            ).fetchone()
            assert updated is not None
            return self._entry_row(updated)

    def finish_batch(self, batch_id: str) -> dict[str, Any]:
        """Finish a batch after every item has a recorded terminal outcome."""

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        now = self._now()
        with self._write_connection() as connection:
            batch = self._require_batch(connection, normalized_batch_id)
            if batch["status"] in _TERMINAL_BATCH_STATUSES:
                return self._batch_row(connection, batch)
            if batch["status"] != "running":
                raise ActiveLearningReviewStateError(
                    f"Cannot finish batch while status is {batch['status']}."
                )
            self._refresh_counts(connection, normalized_batch_id, now)
            counts = self._entry_counts(connection, normalized_batch_id)
            if counts["pending"]:
                raise ActiveLearningReviewStateError(
                    f"Review batch still has {counts['pending']} pending items."
                )
            final_status = _final_review_status(counts)
            connection.execute(
                """
                UPDATE active_learning_review_batches
                SET status = ?, finished_at = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (final_status, now, now, normalized_batch_id),
            )
            updated = self._require_batch(connection, normalized_batch_id)
            return self._batch_row(connection, updated)

    def begin_undo(self, batch_id: str) -> dict[str, Any]:
        """Start or resume undo and return entries in reverse apply order."""

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        now = self._now()
        with self._write_connection() as connection:
            batch = self._require_batch(connection, normalized_batch_id)
            if batch["status"] == "undone":
                raise ActiveLearningReviewStateError("Review batch is already undone.")
            if batch["status"] not in {
                "completed",
                "partial",
                "failed",
                "interrupted",
                "undo_partial",
                "undoing",
            }:
                raise ActiveLearningReviewStateError(
                    f"Cannot undo batch while status is {batch['status']}."
                )
            undoable = connection.execute(
                """
                SELECT COUNT(*) FROM active_learning_review_entries
                WHERE batch_id = ? AND requires_undo = 1
                  AND undo_status != 'undone'
                """,
                (normalized_batch_id,),
            ).fetchone()[0]
            if int(undoable) == 0:
                raise ActiveLearningReviewStateError(
                    "Review batch has no remaining side effects to undo."
                )
            connection.execute(
                """
                UPDATE active_learning_review_entries
                SET undo_status = 'not_required', updated_at = ?
                WHERE batch_id = ? AND requires_undo = 0
                  AND undo_status = 'not_requested'
                """,
                (now, normalized_batch_id),
            )
            connection.execute(
                """
                UPDATE active_learning_review_entries
                SET undo_status = 'pending', undo_error_code = NULL,
                    undo_error_message = NULL, updated_at = ?
                WHERE batch_id = ? AND requires_undo = 1
                  AND undo_status != 'undone'
                """,
                (now, normalized_batch_id),
            )
            connection.execute(
                """
                UPDATE active_learning_review_batches
                SET status = 'undoing', undo_started_at = COALESCE(undo_started_at, ?),
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (now, now, normalized_batch_id),
            )
            rows = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND undo_status = 'pending'
                ORDER BY item_order DESC
                """,
                (normalized_batch_id,),
            ).fetchall()
        return {
            "batch": self.get_batch(normalized_batch_id),
            "entries": [self._entry_row(row) for row in rows],
        }

    def record_undo_outcome(
        self,
        batch_id: str,
        doc_id: str,
        *,
        status: Literal["undone", "failed", "conflict"],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Record one undo result; failures do not block remaining entries."""

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        normalized_doc_id = _required_id(doc_id, "doc_id", maximum=2048)
        if status not in _UNDO_OUTCOMES:
            raise ActiveLearningReviewValidationError(
                f"Unsupported undo status: {status}"
            )
        normalized_error_code, normalized_error_message = _undo_error(
            status, error_code, error_message
        )
        now = self._now()
        with self._write_connection() as connection:
            batch = self._require_batch(connection, normalized_batch_id)
            if batch["status"] != "undoing":
                raise ActiveLearningReviewStateError(
                    f"Cannot record undo while batch status is {batch['status']}."
                )
            row = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND doc_id = ?
                """,
                (normalized_batch_id, normalized_doc_id),
            ).fetchone()
            if row is None:
                raise ActiveLearningReviewValidationError(
                    f"Review item was not found: {normalized_doc_id}"
                )
            if not bool(row["requires_undo"]):
                raise ActiveLearningReviewStateError(
                    "Review item did not record an undoable side effect."
                )
            if row["undo_status"] != "pending":
                same_outcome = (
                    row["undo_status"] == status
                    and row["undo_error_code"] == normalized_error_code
                    and row["undo_error_message"] == normalized_error_message
                )
                if same_outcome:
                    return self._entry_row(row)
                raise ActiveLearningReviewStateError(
                    "Undo item already has a different terminal outcome: "
                    f"{row['undo_status']}"
                )
            connection.execute(
                """
                UPDATE active_learning_review_entries SET
                    undo_status = ?, undo_error_code = ?,
                    undo_error_message = ?, undone_at = ?, updated_at = ?
                WHERE batch_id = ? AND doc_id = ?
                """,
                (
                    status,
                    normalized_error_code,
                    normalized_error_message,
                    now if status == "undone" else None,
                    now,
                    normalized_batch_id,
                    normalized_doc_id,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND doc_id = ?
                """,
                (normalized_batch_id, normalized_doc_id),
            ).fetchone()
            assert updated is not None
            return self._entry_row(updated)

    def finish_undo(self, batch_id: str) -> dict[str, Any]:
        """Finish undo after every pending rollback has an outcome."""

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        now = self._now()
        with self._write_connection() as connection:
            batch = self._require_batch(connection, normalized_batch_id)
            if batch["status"] == "undone":
                return self._batch_row(connection, batch)
            if batch["status"] != "undoing":
                raise ActiveLearningReviewStateError(
                    f"Cannot finish undo while status is {batch['status']}."
                )
            undo_counts = self._undo_counts(connection, normalized_batch_id)
            if undo_counts["pending"]:
                raise ActiveLearningReviewStateError(
                    f"Undo still has {undo_counts['pending']} pending items."
                )
            final_status = (
                "undo_partial"
                if undo_counts["failed"] or undo_counts["conflict"]
                else "undone"
            )
            connection.execute(
                """
                UPDATE active_learning_review_batches
                SET status = ?, updated_at = ?, undone_at = ?
                WHERE batch_id = ?
                """,
                (
                    final_status,
                    now,
                    now if final_status == "undone" else None,
                    normalized_batch_id,
                ),
            )
            updated = self._require_batch(connection, normalized_batch_id)
            return self._batch_row(connection, updated)

    def recover_incomplete(self) -> dict[str, Any]:
        """Mark work left active by a stopped process without replaying writes.

        Integration should call this once when it owns startup recovery.  It is
        intentionally not automatic in ``__init__`` because a second process
        opening the same database must not interrupt the current owner.
        """

        now = self._now()
        recovered_review: list[str] = []
        recovered_undo: list[str] = []
        with self._write_connection() as connection:
            running = connection.execute(
                """
                SELECT batch_id FROM active_learning_review_batches
                WHERE status = 'running' ORDER BY sequence
                """
            ).fetchall()
            for row in running:
                batch_id = str(row["batch_id"])
                connection.execute(
                    """
                    UPDATE active_learning_review_entries SET
                        status = 'failed', error_code = 'review_interrupted',
                        error_message = 'Review stopped before this item completed.',
                        applied_at = ?, updated_at = ?
                    WHERE batch_id = ? AND status = 'pending'
                    """,
                    (now, now, batch_id),
                )
                self._refresh_counts(connection, batch_id, now)
                connection.execute(
                    """
                    UPDATE active_learning_review_batches SET
                        status = 'interrupted', error_code = 'review_interrupted',
                        error_message = 'Review process stopped before completion.',
                        finished_at = ?, updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (now, now, batch_id),
                )
                recovered_review.append(batch_id)

            undoing = connection.execute(
                """
                SELECT batch_id FROM active_learning_review_batches
                WHERE status = 'undoing' ORDER BY sequence
                """
            ).fetchall()
            for row in undoing:
                batch_id = str(row["batch_id"])
                connection.execute(
                    """
                    UPDATE active_learning_review_entries SET
                        undo_status = 'failed',
                        undo_error_code = 'undo_interrupted',
                        undo_error_message = 'Undo stopped before this item completed.',
                        updated_at = ?
                    WHERE batch_id = ? AND undo_status = 'pending'
                    """,
                    (now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE active_learning_review_batches SET
                        status = 'undo_partial', error_code = 'undo_interrupted',
                        error_message = 'Undo process stopped before completion.',
                        updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (now, batch_id),
                )
                recovered_undo.append(batch_id)
        return {
            "review_batches": recovered_review,
            "undo_batches": recovered_undo,
            "recovered_count": len(recovered_review) + len(recovered_undo),
        }

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        with self._read_connection() as connection:
            row = self._require_batch(connection, normalized_batch_id)
            return self._batch_row(connection, row)

    def get_entry(self, batch_id: str, doc_id: str) -> dict[str, Any]:
        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        normalized_doc_id = _required_id(doc_id, "doc_id", maximum=2048)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM active_learning_review_entries
                WHERE batch_id = ? AND doc_id = ?
                """,
                (normalized_batch_id, normalized_doc_id),
            ).fetchone()
        if row is None:
            raise ActiveLearningReviewValidationError(
                f"Review item was not found: {normalized_doc_id}"
            )
        return self._entry_row(row)

    def latest_applied_entry(
        self, doc_id: str, *, library_id: str | None = None
    ) -> dict[str, Any] | None:
        """Return the latest still-effective successful review for ``doc_id``.

        An entry whose undo completed is no longer effective.  A partially
        failed undo remains effective and is returned so queue regeneration
        does not ask the user to review the same image again prematurely.
        """

        normalized_doc_id = _required_id(doc_id, "doc_id", maximum=2048)
        parameters: list[object] = [normalized_doc_id]
        library_filter = ""
        if library_id is not None:
            library_filter = " AND b.library_id = ?"
            parameters.append(_optional_id(library_id, "library_id", maximum=256) or "")
        with self._read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT e.*, b.sequence AS batch_sequence,
                    b.library_id AS review_library_id,
                    b.queue_id AS review_queue_id,
                    b.status AS batch_status
                FROM active_learning_review_entries AS e
                JOIN active_learning_review_batches AS b
                  ON b.batch_id = e.batch_id
                WHERE e.doc_id = ? AND e.status = 'applied'
                  AND e.undo_status != 'undone' AND b.status != 'undone'
                  {library_filter}
                ORDER BY b.sequence DESC, e.item_order DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        return self._joined_entry_row(row) if row is not None else None

    def list_applied_doc_ids(
        self,
        *,
        library_id: str | None = None,
        limit: int = 1000,
        after_doc_id: str | None = None,
    ) -> dict[str, Any]:
        """Page through distinct doc ids with a still-effective applied review."""

        normalized_limit = _limit(limit, maximum=10_000)
        filters = [
            "e.status = 'applied'",
            "e.undo_status != 'undone'",
            "b.status != 'undone'",
        ]
        parameters: list[object] = []
        if library_id is not None:
            filters.append("b.library_id = ?")
            parameters.append(_optional_id(library_id, "library_id", maximum=256) or "")
        if after_doc_id is not None:
            filters.append("e.doc_id > ?")
            parameters.append(_required_id(after_doc_id, "after_doc_id", maximum=2048))
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT e.doc_id
                FROM active_learning_review_entries AS e
                JOIN active_learning_review_batches AS b
                  ON b.batch_id = e.batch_id
                WHERE {" AND ".join(filters)}
                ORDER BY e.doc_id ASC LIMIT ?
                """,
                (*parameters, normalized_limit + 1),
            ).fetchall()
        visible = rows[:normalized_limit]
        items = [str(row["doc_id"]) for row in visible]
        return {
            "items": items,
            "has_more": len(rows) > normalized_limit,
            "next_cursor": (
                items[-1] if len(rows) > normalized_limit and items else None
            ),
        }

    def list_applied_entries(
        self,
        batch_id: str,
        *,
        limit: int = 1000,
        before_order: int | None = None,
        undoable_only: bool = True,
    ) -> dict[str, Any]:
        """List applied entries in reverse order with their undo snapshots."""

        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        normalized_limit = _limit(limit, maximum=10_000)
        if not isinstance(undoable_only, bool):
            raise ActiveLearningReviewValidationError(
                "undoable_only must be a boolean."
            )
        filters = ["batch_id = ?", "status = 'applied'"]
        parameters: list[object] = [normalized_batch_id]
        if undoable_only:
            filters.extend(("requires_undo = 1", "undo_status != 'undone'"))
        if before_order is not None:
            if isinstance(before_order, bool) or before_order < 1:
                raise ActiveLearningReviewValidationError(
                    "before_order must be a positive integer."
                )
            filters.append("item_order < ?")
            parameters.append(before_order)
        with self._read_connection() as connection:
            self._require_batch(connection, normalized_batch_id)
            rows = connection.execute(
                f"""
                SELECT * FROM active_learning_review_entries
                WHERE {" AND ".join(filters)}
                ORDER BY item_order DESC LIMIT ?
                """,
                (*parameters, normalized_limit + 1),
            ).fetchall()
        visible = rows[:normalized_limit]
        return {
            "items": [self._entry_row(row) for row in visible],
            "has_more": len(rows) > normalized_limit,
            "next_cursor": (
                int(visible[-1]["item_order"])
                if len(rows) > normalized_limit and visible
                else None
            ),
        }

    def list_batches(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        library_id: str | None = None,
        status: ReviewBatchStatus | None = None,
    ) -> dict[str, Any]:
        normalized_limit = _limit(limit)
        filters: list[str] = []
        parameters: list[object] = []
        if before_sequence is not None:
            if isinstance(before_sequence, bool) or before_sequence < 1:
                raise ActiveLearningReviewValidationError(
                    "before_sequence must be a positive integer."
                )
            filters.append("sequence < ?")
            parameters.append(before_sequence)
        if library_id is not None:
            filters.append("library_id = ?")
            parameters.append(_optional_id(library_id, "library_id", maximum=256) or "")
        if status is not None:
            if status not in {
                "running",
                "completed",
                "partial",
                "failed",
                "interrupted",
                "undoing",
                "undone",
                "undo_partial",
            }:
                raise ActiveLearningReviewValidationError(
                    f"Unsupported review batch status: {status}"
                )
            filters.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM active_learning_review_batches{where}
                ORDER BY sequence DESC LIMIT ?
                """,
                (*parameters, normalized_limit + 1),
            ).fetchall()
            visible = rows[:normalized_limit]
            items = [self._batch_row(connection, row) for row in visible]
        return {
            "items": items,
            "has_more": len(rows) > normalized_limit,
            "next_cursor": (
                int(visible[-1]["sequence"])
                if len(rows) > normalized_limit and visible
                else None
            ),
        }

    def list_entries(
        self,
        batch_id: str,
        *,
        limit: int = 100,
        after_order: int | None = None,
        status: ReviewEntryStatus | None = None,
        undo_status: ReviewUndoStatus | None = None,
        reverse: bool = False,
    ) -> dict[str, Any]:
        normalized_batch_id = _required_id(batch_id, "batch_id", maximum=256)
        normalized_limit = _limit(limit, maximum=1000)
        filters = ["batch_id = ?"]
        parameters: list[object] = [normalized_batch_id]
        operator = "<" if reverse else ">"
        if after_order is not None:
            if isinstance(after_order, bool) or after_order < 0:
                raise ActiveLearningReviewValidationError(
                    "after_order must be a non-negative integer."
                )
            filters.append(f"item_order {operator} ?")
            parameters.append(after_order)
        if status is not None:
            if status not in {"pending", "applied", "failed", "skipped", "conflict"}:
                raise ActiveLearningReviewValidationError(
                    f"Unsupported review entry status: {status}"
                )
            filters.append("status = ?")
            parameters.append(status)
        if undo_status is not None:
            if undo_status not in {
                "not_requested",
                "not_required",
                "pending",
                "undone",
                "failed",
                "conflict",
            }:
                raise ActiveLearningReviewValidationError(
                    f"Unsupported review undo status: {undo_status}"
                )
            filters.append("undo_status = ?")
            parameters.append(undo_status)
        order = "DESC" if reverse else "ASC"
        with self._read_connection() as connection:
            self._require_batch(connection, normalized_batch_id)
            rows = connection.execute(
                f"""
                SELECT * FROM active_learning_review_entries
                WHERE {" AND ".join(filters)}
                ORDER BY item_order {order} LIMIT ?
                """,
                (*parameters, normalized_limit + 1),
            ).fetchall()
        visible = rows[:normalized_limit]
        return {
            "items": [self._entry_row(row) for row in visible],
            "has_more": len(rows) > normalized_limit,
            "next_cursor": (
                int(visible[-1]["item_order"])
                if len(rows) > normalized_limit and visible
                else None
            ),
        }

    def _refresh_counts(
        self, connection: sqlite3.Connection, batch_id: str, now: str
    ) -> None:
        counts = self._entry_counts(connection, batch_id)
        connection.execute(
            """
            UPDATE active_learning_review_batches SET
                processed_count = ?, applied_count = ?, failed_count = ?,
                skipped_count = ?, conflict_count = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (
                counts["processed"],
                counts["applied"],
                counts["failed"],
                counts["skipped"],
                counts["conflict"],
                now,
                batch_id,
            ),
        )

    @staticmethod
    def _entry_counts(connection: sqlite3.Connection, batch_id: str) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) AS processed,
                SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN status = 'conflict' THEN 1 ELSE 0 END) AS conflict
            FROM active_learning_review_entries WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        assert row is not None
        keys = ("pending", "processed", "applied", "failed", "skipped", "conflict")
        return {key: int(row[key] or 0) for key in keys}

    @staticmethod
    def _undo_counts(connection: sqlite3.Connection, batch_id: str) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN undo_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN undo_status = 'undone' THEN 1 ELSE 0 END) AS undone,
                SUM(CASE WHEN undo_status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN undo_status = 'conflict' THEN 1 ELSE 0 END) AS conflict,
                SUM(CASE WHEN undo_status = 'not_required' THEN 1 ELSE 0 END)
                    AS not_required
            FROM active_learning_review_entries WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        assert row is not None
        keys = ("pending", "undone", "failed", "conflict", "not_required")
        return {key: int(row[key] or 0) for key in keys}

    @staticmethod
    def _require_batch(connection: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM active_learning_review_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ActiveLearningReviewValidationError(
                f"Review batch was not found: {batch_id}"
            )
        return row

    def _batch_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        undo_counts = self._undo_counts(connection, str(row["batch_id"]))
        return {
            "sequence": int(row["sequence"]),
            "batch_id": str(row["batch_id"]),
            "queue_id": str(row["queue_id"]),
            "library_id": str(row["library_id"]),
            "operation_id": row["operation_id"],
            "schema_version": int(row["schema_version"]),
            "status": str(row["status"]),
            "total_count": int(row["total_count"]),
            "processed_count": int(row["processed_count"]),
            "applied_count": int(row["applied_count"]),
            "failed_count": int(row["failed_count"]),
            "skipped_count": int(row["skipped_count"]),
            "conflict_count": int(row["conflict_count"]),
            "metadata": _decode_mapping(row["metadata_json"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": row["finished_at"],
            "undo_started_at": row["undo_started_at"],
            "undone_at": row["undone_at"],
            "undo_counts": undo_counts,
            "undo_available": bool(
                row["status"] != "undone"
                and (
                    undo_counts["pending"]
                    + undo_counts["failed"]
                    + undo_counts["conflict"]
                    + undo_counts["undone"]
                    > 0
                    or connection.execute(
                        """
                         SELECT 1 FROM active_learning_review_entries
                         WHERE batch_id = ? AND requires_undo = 1
                           AND undo_status != 'undone' LIMIT 1
                         """,
                        (row["batch_id"],),
                    ).fetchone()
                    is not None
                )
            ),
        }

    @staticmethod
    def _entry_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": str(row["batch_id"]),
            "item_order": int(row["item_order"]),
            "doc_id": str(row["doc_id"]),
            "decision": str(row["decision"]),
            "requested_labels": _decode_list(row["requested_labels_json"]),
            "status": str(row["status"]),
            "before_snapshot": _decode_optional_mapping(row["before_snapshot_json"]),
            "after_snapshot": _decode_optional_mapping(row["after_snapshot_json"]),
            "feedback_event_id": row["feedback_event_id"],
            "requires_undo": bool(row["requires_undo"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "applied_at": row["applied_at"],
            "undo_status": str(row["undo_status"]),
            "undo_error_code": row["undo_error_code"],
            "undo_error_message": row["undo_error_message"],
            "undone_at": row["undone_at"],
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _joined_entry_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = cls._entry_row(row)
        result.update(
            {
                "batch_sequence": int(row["batch_sequence"]),
                "library_id": str(row["review_library_id"]),
                "queue_id": str(row["review_queue_id"]),
                "batch_status": str(row["batch_status"]),
            }
        )
        return result

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        serialized = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return serialized.replace("+00:00", "Z")

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._sqlite_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={max(1, int(self._sqlite_timeout_seconds * 1000))}"
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open()
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except (
                ActiveLearningReviewValidationError,
                ActiveLearningReviewStateError,
            ):
                if connection is not None:
                    connection.rollback()
                raise
            except (OSError, sqlite3.Error) as exc:
                if connection is not None:
                    connection.rollback()
                raise ActiveLearningReviewStoreError(
                    f"Active-learning review database write failed: {exc}"
                ) from exc
            finally:
                if connection is not None:
                    connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open()
                yield connection
            except (
                ActiveLearningReviewValidationError,
                ActiveLearningReviewStateError,
            ):
                raise
            except (OSError, sqlite3.Error) as exc:
                raise ActiveLearningReviewStoreError(
                    f"Active-learning review database read failed: {exc}"
                ) from exc
            finally:
                if connection is not None:
                    connection.close()


def _normalize_item(
    value: ActiveLearningReviewItem | Mapping[str, object],
) -> ActiveLearningReviewItem:
    raw_doc_id: object
    raw_decision: object
    raw_labels: object
    if isinstance(value, ActiveLearningReviewItem):
        raw_doc_id = value.doc_id
        raw_decision = value.decision
        raw_labels = value.labels
    elif isinstance(value, Mapping):
        raw_doc_id = value.get("doc_id")
        raw_decision = value.get("decision")
        raw_labels = value.get("labels", ())
    else:
        raise ActiveLearningReviewValidationError(
            "Review items must be ActiveLearningReviewItem values or mappings."
        )
    doc_id = _required_id(raw_doc_id, "doc_id", maximum=2048)
    decision = str(raw_decision or "").strip().lower()
    if decision not in _DECISIONS:
        raise ActiveLearningReviewValidationError(
            f"Unsupported active-learning decision: {decision}"
        )
    labels = _labels(raw_labels)
    if decision in {"reject", "skip"} and labels:
        raise ActiveLearningReviewValidationError(
            f"{decision} review decisions cannot contain labels."
        )
    return ActiveLearningReviewItem(
        doc_id=doc_id,
        decision=cast(ReviewDecision, decision),
        labels=labels,
    )


def _labels(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ActiveLearningReviewValidationError("labels must be an array of strings.")
    labels: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ActiveLearningReviewValidationError("labels must contain strings.")
        label = " ".join(raw.split())
        if not label:
            continue
        if len(label) > 256:
            raise ActiveLearningReviewValidationError(
                "A review label cannot exceed 256 characters."
            )
        key = label.casefold()
        if key not in seen:
            seen.add(key)
            labels.append(label)
    if len(labels) > 256:
        raise ActiveLearningReviewValidationError(
            "A review item cannot contain more than 256 labels."
        )
    return tuple(labels)


def _required_id(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ActiveLearningReviewValidationError(f"{name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ActiveLearningReviewValidationError(f"{name} must not be empty.")
    if len(normalized) > maximum:
        raise ActiveLearningReviewValidationError(
            f"{name} cannot exceed {maximum} characters."
        )
    if any(ord(character) < 32 for character in normalized):
        raise ActiveLearningReviewValidationError(
            f"{name} cannot contain control characters."
        )
    return normalized


def _optional_id(value: object, name: str, *, maximum: int) -> str | None:
    if value is None or value == "":
        return None
    return _required_id(value, name, maximum=maximum)


def _optional_snapshot(value: Mapping[str, object] | None, name: str) -> str | None:
    if value is None:
        return None
    return _json_mapping(value, name, maximum_bytes=MAX_SNAPSHOT_BYTES)


def _json_mapping(value: object, name: str, *, maximum_bytes: int) -> str:
    if not isinstance(value, Mapping):
        raise ActiveLearningReviewValidationError(f"{name} must be an object.")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ActiveLearningReviewValidationError(
            f"{name} must contain JSON-compatible finite values."
        ) from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ActiveLearningReviewValidationError(
            f"{name} exceeds the {maximum_bytes}-byte limit."
        )
    return encoded


def _outcome_error(
    status: str, error_code: str | None, error_message: str | None
) -> tuple[str | None, str | None]:
    if status == "failed":
        return (
            _optional_text(error_code, "error_code", 256) or "item_failed",
            _optional_text(error_message, "error_message", 4096)
            or "The review item failed.",
        )
    if status == "conflict":
        return (
            _optional_text(error_code, "error_code", 256) or "item_conflict",
            _optional_text(error_message, "error_message", 4096)
            or "The review item conflicts with current state.",
        )
    return None, None


def _undo_error(
    status: str, error_code: str | None, error_message: str | None
) -> tuple[str | None, str | None]:
    if status == "failed":
        return (
            _optional_text(error_code, "undo_error_code", 256) or "undo_failed",
            _optional_text(error_message, "undo_error_message", 4096)
            or "The review item could not be undone.",
        )
    if status == "conflict":
        return (
            _optional_text(error_code, "undo_error_code", 256) or "undo_conflict",
            _optional_text(error_message, "undo_error_message", 4096)
            or "Current state conflicts with the saved review snapshot.",
        )
    return None, None


def _optional_text(value: object, name: str, maximum: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ActiveLearningReviewValidationError(f"{name} must be a string.")
    normalized = " ".join(value.split())
    if len(normalized) > maximum:
        raise ActiveLearningReviewValidationError(
            f"{name} cannot exceed {maximum} characters."
        )
    return normalized or None


def _decode_mapping(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise ActiveLearningReviewStoreError(
            "Persisted review object is invalid."
        ) from exc
    if not isinstance(decoded, dict):
        raise ActiveLearningReviewStoreError("Persisted review object is invalid.")
    return cast(dict[str, Any], decoded)


def _decode_optional_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    return _decode_mapping(value)


def _decode_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise ActiveLearningReviewStoreError(
            "Persisted review labels are invalid."
        ) from exc
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ActiveLearningReviewStoreError("Persisted review labels are invalid.")
    return cast(list[str], decoded)


def _final_review_status(counts: Mapping[str, int]) -> ReviewBatchStatus:
    if counts["failed"] or counts["conflict"]:
        if counts["applied"] or counts["skipped"]:
            return "partial"
        return "failed"
    return "completed"


def _limit(value: int, *, maximum: int = 500) -> int:
    invalid = (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    )
    if invalid:
        raise ActiveLearningReviewValidationError(
            f"limit must be an integer between 1 and {maximum}."
        )
    return value
