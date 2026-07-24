"""Durable, idempotent write-ahead queue for SQLite/Zvec consistency.

The image service has two local persistence engines: SQLite owns the library
catalog and Zvec owns the searchable document.  A process can stop after one
engine has committed but before the other one has, so a rollback-only design
cannot guarantee recovery.  This module records the complete, already
generated Zvec payload before either side is changed.  Replaying an upsert or
delete is idempotent and never needs to call an embedding model again.

The outbox deliberately stores only collection fields, relative library paths
and float32 vectors.  It has no generic metadata/details column in which API
keys, prompts, model responses or absolute source paths could accidentally be
persisted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import uuid
from array import array
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Final, Literal

CollectionWriteAction = Literal["upsert", "delete"]
CollectionWriteStatus = Literal["pending", "applying", "applied", "failed"]
StateWriteAction = Literal["upsert", "delete", "none"]

OUTBOX_SCHEMA_VERSION: Final = 1
DEFAULT_CLAIM_LIMIT: Final = 256
MAX_CLAIM_LIMIT: Final = 1_000
MAX_OPERATION_ITEMS: Final = 10_000
MAX_JSON_BYTES: Final = 256 * 1024
MAX_VECTOR_DIMENSION: Final = 65_536
MAX_ERROR_TEXT: Final = 2_000
DEFAULT_APPLIED_OPERATION_RETENTION: Final = 1_000
DEFAULT_PRUNE_BATCH_SIZE: Final = 500

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_VECTOR_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|prompt|response|"
    r"secret|session|token)",
    re.IGNORECASE,
)
_INLINE_SECRET = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|cookie|password|passwd|secret|token)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)
_LOCAL_PATHS = (
    re.compile(r"(?i)\bfile:///[^ \t\r\n\"']+"),
    re.compile(r"(?i)\b[a-z]:[\\/][^ \t\r\n\"'<>|]+"),
    re.compile(r"\\\\[^\\\s]+\\[^ \t\r\n\"'<>|]+"),
    # Unix absolute paths must start at the beginning of the value.  The
    # previous lookbehind variant false-positived on CJK relative paths such
    # as ``作品/子目录/1.jpg`` because the CJK character before a ``/`` is
    # outside [A-Za-z0-9_.-].
    re.compile(r"^/(?:[^\s/]+/)+[^\s]*"),
)

_COLLECTION_FIELDS: Final = frozenset(
    {
        "root_id",
        "relative_path",
        "file_name",
        "extension",
        "mime_type",
        "sha256",
        "size_bytes",
        "mtime_ns",
        "width",
        "height",
        "model",
        "tags",
        "metadata_text",
        "metadata_text_hash",
    }
)
_REQUIRED_COLLECTION_FIELDS: Final = _COLLECTION_FIELDS
_REQUIRED_VECTOR_NAMES: Final = frozenset({"embedding", "metadata_embedding"})
_STATE_FIELDS: Final = frozenset(
    {
        "doc_id",
        "root_id",
        "relative_path",
        "parent_directory",
        "file_name",
        "extension",
        "mime_type",
        "sha256",
        "size_bytes",
        "mtime_ns",
        "width",
        "height",
        "tags",
        "folder_tags",
        "accepted_auto_tags",
        "inherited_tags",
    }
)
_REQUIRED_STATE_FIELDS: Final = frozenset(
    {
        "doc_id",
        "root_id",
        "relative_path",
        "file_name",
        "extension",
        "mime_type",
        "sha256",
        "size_bytes",
        "mtime_ns",
        "width",
        "height",
    }
)


class CollectionWriteOutboxError(RuntimeError):
    """Base error for a durable collection write."""


class CollectionWriteValidationError(ValueError):
    """A write cannot be persisted or replayed safely."""


class CollectionWriteConflictError(CollectionWriteOutboxError):
    """An idempotency key was reused with a different payload."""


@dataclass(frozen=True)
class CollectionWriteItem:
    """One exact Zvec mutation and its corresponding SQLite mutation."""

    doc_id: str
    action: CollectionWriteAction
    collection_fields: Mapping[str, Any] | None = None
    vectors: Mapping[str, Sequence[float]] = field(default_factory=dict)
    state_entry: Mapping[str, Any] | None = None
    delete_state: bool = True
    item_id: str | None = None


@dataclass(frozen=True)
class ReplayCollectionWrite:
    """A claimed item containing everything needed for zero-model replay."""

    sequence: int
    operation_id: str
    item_id: str
    doc_id: str
    action: CollectionWriteAction
    state_action: StateWriteAction
    collection_fields: dict[str, Any] | None
    vectors: dict[str, list[float]]
    state_entry: dict[str, Any] | None
    attempt_count: int


@dataclass(frozen=True)
class CollectionWriteFailure:
    """Bounded error and retry policy for one failed item."""

    code: str
    message: str
    retryable: bool = True
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class CollectionWriteOperation:
    operation_id: str
    operation_kind: str
    status: CollectionWriteStatus
    item_count: int
    pending_count: int
    applied_count: int
    failed_count: int
    created_at: str
    updated_at: str
    completed_at: str | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class _EncodedItem:
    item_id: str
    doc_id: str
    action: CollectionWriteAction
    state_action: StateWriteAction
    fields_json: str | None
    vector_manifest_json: str | None
    vectors_blob: bytes | None
    state_payload_json: str | None
    payload_sha256: str


class CollectionWriteOutbox:
    """Transactional outbox stored inside the library state database.

    One ``IndexState`` owns one SQLite connection and therefore one writer.
    ``BEGIN IMMEDIATE`` makes claims atomic if a recovery utility opens a
    second connection to the same WAL database.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        redactions: Sequence[str] = (),
        recover_interrupted: bool = True,
    ) -> None:
        self.connection = connection
        # All public records are decoded by column name. IndexState already
        # uses sqlite3.Row; enforcing it here also keeps standalone recovery
        # tools from depending on a caller-specific row factory.
        self.connection.row_factory = sqlite3.Row
        environment_redactions = tuple(
            value
            for name in ("DASHSCOPE_API_KEY", "ALIBABA_CLOUD_API_KEY")
            if (value := os.getenv(name, "").strip())
        )
        self._redactions = tuple(
            dict.fromkeys(
                value
                for value in (*redactions, *environment_redactions)
                if isinstance(value, str) and value
            )
        )
        self._initialize_schema()
        self.recovered_interrupted_count = (
            self.recover_interrupted() if recover_interrupted else 0
        )

    @staticmethod
    def new_operation_id(prefix: str = "collection-write") -> str:
        normalized = prefix.strip().lower()
        if not _KIND.fullmatch(normalized):
            raise CollectionWriteValidationError("Invalid operation id prefix.")
        return f"{normalized}:{uuid.uuid4().hex}"

    def add_redactions(self, *values: str) -> None:
        additions = tuple(value for value in values if isinstance(value, str) and value)
        self._redactions = tuple(dict.fromkeys((*self._redactions, *additions)))

    def enqueue(
        self,
        operation_id: str,
        operation_kind: str,
        items: Iterable[CollectionWriteItem],
    ) -> CollectionWriteOperation:
        """Persist a complete batch before either storage engine is changed.

        Reusing ``operation_id`` with the exact same payload is a successful
        no-op.  Reusing it for different data fails loudly instead of silently
        corrupting the recovery log.
        """

        normalized_operation_id = _checked_identifier(operation_id, "operation_id")
        normalized_kind = str(operation_kind).strip().lower()
        if not _KIND.fullmatch(normalized_kind):
            raise CollectionWriteValidationError(
                "operation_kind must be a lowercase stable identifier."
            )
        materialized = list(items)
        if not materialized:
            raise CollectionWriteValidationError(
                "A collection write operation must contain at least one item."
            )
        if len(materialized) > MAX_OPERATION_ITEMS:
            raise CollectionWriteValidationError(
                "A collection write operation cannot exceed "
                f"{MAX_OPERATION_ITEMS} items."
            )
        encoded = [_encode_item(item) for item in materialized]
        item_ids = [item.item_id for item in encoded]
        if len(set(item_ids)) != len(item_ids):
            raise CollectionWriteValidationError(
                "item_id values must be unique within an operation."
            )
        operation_digest = _operation_digest(normalized_kind, encoded)
        now = _utc_now()

        with self._write_transaction():
            existing = self.connection.execute(
                "SELECT payload_sha256 FROM collection_write_operations "
                "WHERE operation_id = ?",
                (normalized_operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != operation_digest:
                    raise CollectionWriteConflictError(
                        "The collection write idempotency key already belongs to "
                        "a different payload."
                    )
                operation = self._operation_row(normalized_operation_id)
                if operation is None:  # pragma: no cover - guarded by SELECT above
                    raise CollectionWriteOutboxError(
                        "The existing collection write operation disappeared."
                    )
                return operation

            self.connection.execute(
                """
                INSERT INTO collection_write_operations(
                    operation_id, operation_kind, status, item_count,
                    pending_count, applied_count, failed_count, payload_sha256,
                    created_at, updated_at
                ) VALUES(?, ?, 'pending', ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    normalized_operation_id,
                    normalized_kind,
                    len(encoded),
                    len(encoded),
                    operation_digest,
                    now,
                    now,
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO collection_write_items(
                    operation_id, item_id, doc_id, action, state_action, status,
                    fields_json, vector_manifest_json, vectors_blob,
                    state_payload_json, payload_sha256, retryable, attempt_count,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                [
                    (
                        normalized_operation_id,
                        item.item_id,
                        item.doc_id,
                        item.action,
                        item.state_action,
                        item.fields_json,
                        item.vector_manifest_json,
                        item.vectors_blob,
                        item.state_payload_json,
                        item.payload_sha256,
                        now,
                        now,
                    )
                    for item in encoded
                ],
            )
            operation = self._operation_row(normalized_operation_id)
            if operation is None:  # pragma: no cover - inserted in this transaction
                raise CollectionWriteOutboxError(
                    "The new collection write operation was not persisted."
                )
            return operation

    def peek_recoverable(
        self,
        *,
        limit: int = DEFAULT_CLAIM_LIMIT,
        after_sequence: int = 0,
        now: str | None = None,
    ) -> list[ReplayCollectionWrite]:
        """Read one keyset page without changing retry state."""

        normalized_limit = _checked_limit(limit)
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise CollectionWriteValidationError("after_sequence must be an integer.")
        if after_sequence < 0:
            raise CollectionWriteValidationError("after_sequence cannot be negative.")
        timestamp = now or _utc_now()
        rows = self.connection.execute(
            """
            SELECT * FROM collection_write_items
            WHERE sequence > ? AND (
                status = 'pending' OR (
                    status = 'failed' AND retryable = 1 AND
                    (next_retry_at IS NULL OR next_retry_at <= ?)
                )
            )
            ORDER BY sequence
            LIMIT ?
            """,
            (after_sequence, timestamp, normalized_limit),
        ).fetchall()
        return [_decode_replay_row(row) for row in rows]

    def iter_recoverable(
        self, *, page_size: int = DEFAULT_CLAIM_LIMIT, now: str | None = None
    ) -> Iterator[list[ReplayCollectionWrite]]:
        """Enumerate recovery work in bounded keyset pages."""

        normalized_page_size = _checked_limit(page_size)
        after_sequence = 0
        timestamp = now or _utc_now()
        while True:
            page = self.peek_recoverable(
                limit=normalized_page_size,
                after_sequence=after_sequence,
                now=timestamp,
            )
            if not page:
                return
            yield page
            after_sequence = page[-1].sequence

    def claim_recoverable(
        self,
        *,
        limit: int = DEFAULT_CLAIM_LIMIT,
        now: str | None = None,
    ) -> list[ReplayCollectionWrite]:
        """Atomically claim a bounded batch for Zvec/SQLite replay."""

        normalized_limit = _checked_limit(limit)
        timestamp = now or _utc_now()
        with self._write_transaction():
            rows = self.connection.execute(
                """
                SELECT * FROM collection_write_items
                WHERE status = 'pending' OR (
                    status = 'failed' AND retryable = 1 AND
                    (next_retry_at IS NULL OR next_retry_at <= ?)
                )
                ORDER BY sequence
                LIMIT ?
                """,
                (timestamp, normalized_limit),
            ).fetchall()
            if not rows:
                return []
            sequences = [int(row["sequence"]) for row in rows]
            operation_ids = {str(row["operation_id"]) for row in rows}
            for chunk in _chunks(sequences):
                placeholders = ",".join("?" for _ in chunk)
                self.connection.execute(
                    f"""
                    UPDATE collection_write_items
                    SET status = 'applying', attempt_count = attempt_count + 1,
                        last_attempt_at = ?, updated_at = ?, error_code = NULL,
                        error_message = NULL, next_retry_at = NULL
                    WHERE sequence IN ({placeholders})
                    """,
                    (timestamp, timestamp, *chunk),
                )
            self.connection.executemany(
                "UPDATE collection_write_operations SET status = 'applying', "
                "updated_at = ?, completed_at = NULL WHERE operation_id = ?",
                [(timestamp, operation_id) for operation_id in operation_ids],
            )
            claimed_rows = self._rows_by_sequence(sequences)
            return [_decode_replay_row(row) for row in claimed_rows]

    def claim_operation(
        self,
        operation_id: str,
        *,
        limit: int = MAX_CLAIM_LIMIT,
        now: str | None = None,
    ) -> list[ReplayCollectionWrite]:
        """Claim one freshly enqueued operation without stealing older work.

        Normal startup recovery intentionally walks the global sequence.  A
        live index/sync commit, however, already knows the operation it just
        durably enqueued.  Claiming by id keeps that write deterministic even
        if an independently retryable historical item becomes eligible at the
        same instant.
        """

        normalized_operation_id = _checked_identifier(operation_id, "operation_id")
        normalized_limit = _checked_limit(limit)
        timestamp = now or _utc_now()
        with self._write_transaction():
            rows = self.connection.execute(
                """
                SELECT * FROM collection_write_items
                WHERE operation_id = ? AND (
                    status = 'pending' OR (
                        status = 'failed' AND retryable = 1 AND
                        (next_retry_at IS NULL OR next_retry_at <= ?)
                    )
                )
                ORDER BY sequence
                LIMIT ?
                """,
                (normalized_operation_id, timestamp, normalized_limit),
            ).fetchall()
            if not rows:
                return []
            sequences = [int(row["sequence"]) for row in rows]
            for chunk in _chunks(sequences):
                placeholders = ",".join("?" for _ in chunk)
                self.connection.execute(
                    f"""
                    UPDATE collection_write_items
                    SET status = 'applying', attempt_count = attempt_count + 1,
                        last_attempt_at = ?, updated_at = ?, error_code = NULL,
                        error_message = NULL, next_retry_at = NULL
                    WHERE sequence IN ({placeholders})
                    """,
                    (timestamp, timestamp, *chunk),
                )
            self.connection.execute(
                "UPDATE collection_write_operations SET status = 'applying', "
                "updated_at = ?, completed_at = NULL WHERE operation_id = ?",
                (timestamp, normalized_operation_id),
            )
            claimed_rows = self._rows_by_sequence(sequences)
            return [_decode_replay_row(row) for row in claimed_rows]

    def mark_applied(
        self,
        operation_id: str,
        item_ids: Iterable[str],
        *,
        release_payload: bool = True,
    ) -> int:
        """Mark successfully replayed items and optionally release large vectors."""

        normalized_operation_id = _checked_identifier(operation_id, "operation_id")
        normalized_ids = _checked_item_ids(item_ids)
        if not normalized_ids:
            return 0
        now = _utc_now()
        with self._write_transaction():
            self._require_items(normalized_operation_id, normalized_ids)
            changed = 0
            for chunk in _chunks(normalized_ids):
                placeholders = ",".join("?" for _ in chunk)
                payload_sql = (
                    ", fields_json = NULL, vector_manifest_json = NULL, "
                    "vectors_blob = NULL, state_payload_json = NULL"
                    if release_payload
                    else ""
                )
                cursor = self.connection.execute(
                    f"""
                    UPDATE collection_write_items
                    SET status = 'applied', retryable = 0, next_retry_at = NULL,
                        error_code = NULL, error_message = NULL, applied_at = ?,
                        updated_at = ? {payload_sql}
                    WHERE operation_id = ? AND item_id IN ({placeholders})
                        AND status != 'applied'
                    """,
                    (now, now, normalized_operation_id, *chunk),
                )
                changed += max(0, int(cursor.rowcount))
            self._refresh_operation(normalized_operation_id, now)
            return changed

    def mark_failed(
        self,
        operation_id: str,
        failures: Mapping[str, CollectionWriteFailure],
    ) -> int:
        """Persist item failures without allowing secrets or paths into SQLite."""

        normalized_operation_id = _checked_identifier(operation_id, "operation_id")
        normalized_failures: list[tuple[str, CollectionWriteFailure]] = []
        for raw_item_id, failure in failures.items():
            item_id = _checked_identifier(raw_item_id, "item_id")
            if not isinstance(failure, CollectionWriteFailure):
                raise CollectionWriteValidationError(
                    "failures values must be CollectionWriteFailure instances."
                )
            normalized_failures.append((item_id, failure))
        if not normalized_failures:
            return 0
        self._require_unique(item_id for item_id, _failure in normalized_failures)
        now = _utc_now()
        with self._write_transaction():
            self._require_items(
                normalized_operation_id,
                [item_id for item_id, _failure in normalized_failures],
            )
            changed = 0
            for item_id, failure in normalized_failures:
                error_code = _checked_error_code(failure.code)
                error_message = self._safe_error(failure.message)
                next_retry_at = _retry_at(now, failure.retry_after_seconds)
                cursor = self.connection.execute(
                    """
                    UPDATE collection_write_items
                    SET status = 'failed', retryable = ?, next_retry_at = ?,
                        error_code = ?, error_message = ?, updated_at = ?,
                        applied_at = NULL
                    WHERE operation_id = ? AND item_id = ? AND status != 'applied'
                    """,
                    (
                        int(failure.retryable),
                        next_retry_at if failure.retryable else None,
                        error_code,
                        error_message,
                        now,
                        normalized_operation_id,
                        item_id,
                    ),
                )
                changed += max(0, int(cursor.rowcount))
            self._refresh_operation(normalized_operation_id, now)
            return changed

    def requeue(
        self,
        operation_id: str,
        item_ids: Iterable[str],
        *,
        include_nonretryable: bool = False,
    ) -> int:
        """Explicitly make failed items eligible for another idempotent replay."""

        normalized_operation_id = _checked_identifier(operation_id, "operation_id")
        normalized_ids = _checked_item_ids(item_ids)
        if not normalized_ids:
            return 0
        now = _utc_now()
        with self._write_transaction():
            self._require_items(normalized_operation_id, normalized_ids)
            changed = 0
            for chunk in _chunks(normalized_ids):
                placeholders = ",".join("?" for _ in chunk)
                retry_filter = "" if include_nonretryable else "AND retryable = 1"
                cursor = self.connection.execute(
                    f"""
                    UPDATE collection_write_items
                    SET status = 'pending', retryable = 1, next_retry_at = NULL,
                        error_code = NULL, error_message = NULL, updated_at = ?
                    WHERE operation_id = ? AND item_id IN ({placeholders})
                        AND status = 'failed' {retry_filter}
                    """,
                    (now, normalized_operation_id, *chunk),
                )
                changed += max(0, int(cursor.rowcount))
            self._refresh_operation(normalized_operation_id, now)
            return changed

    def recover_interrupted(self) -> int:
        """Return only interrupted in-flight items to the pending queue.

        The recovery index means hundreds of thousands of already applied rows
        are not read or rewritten during startup.
        """

        now = _utc_now()
        with self._write_transaction():
            rows = self.connection.execute(
                "SELECT DISTINCT operation_id FROM collection_write_items "
                "WHERE status = 'applying'"
            ).fetchall()
            if not rows:
                return 0
            operation_ids = [str(row["operation_id"]) for row in rows]
            cursor = self.connection.execute(
                """
                UPDATE collection_write_items
                SET status = 'pending', retryable = 1, next_retry_at = NULL,
                    error_code = 'process_restarted',
                    error_message =
                        'Process stopped before the durable write completed.',
                    updated_at = ?
                WHERE status = 'applying'
                """,
                (now,),
            )
            for operation_id in operation_ids:
                self._refresh_operation(operation_id, now)
            return max(0, int(cursor.rowcount))

    def get_operation(self, operation_id: str) -> CollectionWriteOperation | None:
        return self._operation_row(_checked_identifier(operation_id, "operation_id"))

    def pending_count(self) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM collection_write_items
            WHERE status = 'pending' OR (
                status = 'failed' AND retryable = 1 AND
                (next_retry_at IS NULL OR next_retry_at <= ?)
            )
            """,
            (_utc_now(),),
        ).fetchone()
        return int(row[0])

    def prune_applied(
        self,
        *,
        keep_latest: int = DEFAULT_APPLIED_OPERATION_RETENTION,
        batch_size: int = DEFAULT_PRUNE_BATCH_SIZE,
    ) -> dict[str, int]:
        """Delete one bounded page of obsolete, fully applied operations.

        Applied payloads are no longer needed for replay after both storage
        engines and the outbox completion marker have committed.  Retaining a
        recent diagnostic window is useful, but retaining every successful
        item forever would make the state database grow with the lifetime
        number of imported images.  Failed, pending, and applying operations
        are deliberately outside this query and can never be pruned here.
        """

        if (
            isinstance(keep_latest, bool)
            or not isinstance(keep_latest, int)
            or keep_latest < 0
        ):
            raise CollectionWriteValidationError(
                "keep_latest must be a non-negative integer."
            )
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 10_000
        ):
            raise CollectionWriteValidationError(
                "batch_size must be between 1 and 10000."
            )

        with self._write_transaction():
            rows = self.connection.execute(
                "SELECT operation_id FROM collection_write_operations "
                "WHERE status = 'applied' ORDER BY sequence DESC "
                "LIMIT ? OFFSET ?",
                (batch_size, keep_latest),
            ).fetchall()
            operation_ids = [str(row["operation_id"]) for row in rows]
            if not operation_ids:
                return {"operations": 0, "items": 0}

            item_count = 0
            for chunk in _chunks(operation_ids):
                placeholders = ",".join("?" for _ in chunk)
                row = self.connection.execute(
                    "SELECT COUNT(*) FROM collection_write_items "
                    f"WHERE operation_id IN ({placeholders})",
                    chunk,
                ).fetchone()
                item_count += int(row[0]) if row is not None else 0
                self.connection.execute(
                    "DELETE FROM collection_write_operations "
                    f"WHERE status = 'applied' AND operation_id IN ({placeholders})",
                    chunk,
                )
            return {"operations": len(operation_ids), "items": item_count}

    def discard_all_for_collection_rebind(self) -> int:
        """Drop writes for a Collection UUID that is no longer attached.

        ``IndexState.ensure_collection_uuid`` calls this in the same SQLite
        transaction as the state reset.  Replaying a document into a different
        Collection would be more dangerous than discarding the stale queue.
        """

        row = self.connection.execute(
            "SELECT COUNT(*) FROM collection_write_operations"
        ).fetchone()
        count = int(row[0])
        self.connection.execute("DELETE FROM collection_write_items")
        self.connection.execute("DELETE FROM collection_write_operations")
        return count

    def _initialize_schema(self) -> None:
        # Foreign keys must be enabled before BEGIN. IndexState opens a private
        # connection, so changing this connection-local pragma is safe.
        if not self.connection.in_transaction:
            self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS collection_write_operations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE,
                operation_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'applying', 'applied', 'failed')
                ),
                item_count INTEGER NOT NULL,
                pending_count INTEGER NOT NULL,
                applied_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_collection_write_operations_status
                ON collection_write_operations(status, sequence);

            CREATE TABLE IF NOT EXISTS collection_write_items (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('upsert', 'delete')),
                state_action TEXT NOT NULL CHECK (
                    state_action IN ('upsert', 'delete', 'none')
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'applying', 'applied', 'failed')
                ),
                fields_json TEXT,
                vector_manifest_json TEXT,
                vectors_blob BLOB,
                state_payload_json TEXT,
                payload_sha256 TEXT NOT NULL,
                retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_attempt_at TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                applied_at TEXT,
                UNIQUE(operation_id, item_id),
                FOREIGN KEY(operation_id) REFERENCES collection_write_operations(
                    operation_id
                ) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_collection_write_items_recovery
                ON collection_write_items(
                    status, retryable, next_retry_at, sequence
                );
            CREATE INDEX IF NOT EXISTS idx_collection_write_items_operation_status
                ON collection_write_items(operation_id, status, sequence);
            """
        )
        # This is an additive state-schema extension. Keeping a dedicated marker
        # allows later releases to migrate it without forcing a vector schema
        # migration or touching existing entries.
        schema_key = "collection_write_outbox_schema_version"
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (schema_key,)
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                (schema_key, str(OUTBOX_SCHEMA_VERSION)),
            )
        elif str(row["value"]) != str(OUTBOX_SCHEMA_VERSION):
            raise CollectionWriteOutboxError(
                f"Unsupported collection write outbox schema version: {row['value']}"
            )
        self.connection.commit()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        if self.connection.in_transaction:
            raise CollectionWriteOutboxError(
                "Cannot start an outbox write inside another SQLite transaction."
            )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _rows_by_sequence(self, sequences: Sequence[int]) -> list[sqlite3.Row]:
        rows_by_sequence: dict[int, sqlite3.Row] = {}
        for chunk in _chunks(sequences):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM collection_write_items "
                f"WHERE sequence IN ({placeholders})",
                chunk,
            ).fetchall()
            rows_by_sequence.update({int(row["sequence"]): row for row in rows})
        return [rows_by_sequence[sequence] for sequence in sequences]

    def _require_items(self, operation_id: str, item_ids: Sequence[str]) -> None:
        found: set[str] = set()
        for chunk in _chunks(item_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT item_id FROM collection_write_items "
                f"WHERE operation_id = ? AND item_id IN ({placeholders})",
                (operation_id, *chunk),
            ).fetchall()
            found.update(str(row["item_id"]) for row in rows)
        missing = [item_id for item_id in item_ids if item_id not in found]
        if missing:
            raise CollectionWriteValidationError(
                f"Unknown collection write item: {missing[0]}"
            )

    @staticmethod
    def _require_unique(values: Iterable[str]) -> None:
        materialized = list(values)
        if len(materialized) != len(set(materialized)):
            raise CollectionWriteValidationError("Duplicate item_id value.")

    def _refresh_operation(self, operation_id: str, now: str) -> None:
        counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM collection_write_items "
                "WHERE operation_id = ? GROUP BY status",
                (operation_id,),
            )
        }
        pending_count = counts.get("pending", 0) + counts.get("applying", 0)
        applied_count = counts.get("applied", 0)
        failed_count = counts.get("failed", 0)
        if counts.get("applying", 0):
            status: CollectionWriteStatus = "applying"
        elif counts.get("pending", 0):
            status = "pending"
        elif failed_count:
            status = "failed"
        else:
            status = "applied"
        error = self.connection.execute(
            "SELECT error_code, error_message FROM collection_write_items "
            "WHERE operation_id = ? AND status = 'failed' "
            "ORDER BY updated_at DESC, sequence DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE collection_write_operations
            SET status = ?, pending_count = ?, applied_count = ?, failed_count = ?,
                updated_at = ?, completed_at = ?, error_code = ?, error_message = ?
            WHERE operation_id = ?
            """,
            (
                status,
                pending_count,
                applied_count,
                failed_count,
                now,
                now if status in {"applied", "failed"} else None,
                str(error["error_code"]) if error and error["error_code"] else None,
                (
                    str(error["error_message"])
                    if error and error["error_message"]
                    else None
                ),
                operation_id,
            ),
        )

    def _operation_row(self, operation_id: str) -> CollectionWriteOperation | None:
        row = self.connection.execute(
            "SELECT * FROM collection_write_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return CollectionWriteOperation(
            operation_id=str(row["operation_id"]),
            operation_kind=str(row["operation_kind"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            item_count=int(row["item_count"]),
            pending_count=int(row["pending_count"]),
            applied_count=int(row["applied_count"]),
            failed_count=int(row["failed_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
            error_code=(
                str(row["error_code"]) if row["error_code"] is not None else None
            ),
            error_message=(
                str(row["error_message"]) if row["error_message"] is not None else None
            ),
        )

    def _safe_error(self, value: str) -> str:
        text = str(value)
        for secret in self._redactions:
            text = text.replace(secret, "<redacted>")
        for pattern in _INLINE_SECRET:
            text = pattern.sub(_redact_secret_match, text)
        for pattern in _LOCAL_PATHS:
            text = pattern.sub("<local-path>", text)
        text = text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
        text = "".join(character if ord(character) >= 32 else " " for character in text)
        return text.strip()[:MAX_ERROR_TEXT] or "Collection write failed."


def _encode_item(item: CollectionWriteItem) -> _EncodedItem:
    if not isinstance(item, CollectionWriteItem):
        raise CollectionWriteValidationError(
            "items must contain CollectionWriteItem instances."
        )
    doc_id = _checked_identifier(item.doc_id, "doc_id")
    item_id = _checked_identifier(item.item_id or doc_id, "item_id")
    if item.action not in {"upsert", "delete"}:
        raise CollectionWriteValidationError("action must be upsert or delete.")

    if item.action == "delete":
        if item.collection_fields is not None or item.vectors or item.state_entry:
            raise CollectionWriteValidationError(
                "A delete item must not contain an upsert payload."
            )
        state_action: StateWriteAction = "delete" if item.delete_state else "none"
        fields_json = None
        manifest_json = None
        vectors_blob = None
        state_json = None
    else:
        if item.collection_fields is None:
            raise CollectionWriteValidationError(
                "An upsert item requires exact collection fields."
            )
        collection_fields = _validated_collection_fields(item.collection_fields)
        fields_json = _canonical_json(collection_fields)
        manifest_json, vectors_blob = _encode_vectors(item.vectors)
        if item.state_entry is not None:
            state_entry = _validated_state_entry(item.state_entry, doc_id)
            state_json = _canonical_json(state_entry)
            state_action = "upsert"
        else:
            state_json = None
            state_action = "none"

    digest = hashlib.sha256()
    for part in (
        item_id.encode("utf-8"),
        doc_id.encode("utf-8"),
        item.action.encode("ascii"),
        state_action.encode("ascii"),
        (fields_json or "").encode("utf-8"),
        (manifest_json or "").encode("utf-8"),
        vectors_blob or b"",
        (state_json or "").encode("utf-8"),
    ):
        digest.update(len(part).to_bytes(8, "little"))
        digest.update(part)
    return _EncodedItem(
        item_id=item_id,
        doc_id=doc_id,
        action=item.action,
        state_action=state_action,
        fields_json=fields_json,
        vector_manifest_json=manifest_json,
        vectors_blob=vectors_blob,
        state_payload_json=state_json,
        payload_sha256=digest.hexdigest(),
    )


def _validated_collection_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CollectionWriteValidationError("collection_fields must be an object.")
    keys = {str(key) for key in value}
    unknown = sorted(keys - _COLLECTION_FIELDS)
    missing = sorted(_REQUIRED_COLLECTION_FIELDS - keys)
    if unknown:
        raise CollectionWriteValidationError(
            f"Unsupported or sensitive collection field: {unknown[0]}"
        )
    if missing:
        raise CollectionWriteValidationError(
            f"Missing collection field required for replay: {missing[0]}"
        )
    result = _json_object(value, "collection_fields")
    _validate_relative_path(result.get("relative_path"), "relative_path")
    _validate_sha256(result.get("sha256"), "sha256")
    metadata_hash = result.get("metadata_text_hash")
    if metadata_hash not in {None, ""}:
        _validate_sha256(metadata_hash, "metadata_text_hash")
    if not isinstance(result.get("tags"), list) or not all(
        isinstance(tag, str) for tag in result["tags"]
    ):
        raise CollectionWriteValidationError("tags must be an array of strings.")
    return result


def _validated_state_entry(value: Mapping[str, Any], doc_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CollectionWriteValidationError("state_entry must be an object.")
    keys = {str(key) for key in value}
    unknown = sorted(keys - _STATE_FIELDS)
    missing = sorted(_REQUIRED_STATE_FIELDS - keys)
    if unknown:
        raise CollectionWriteValidationError(
            f"Unsupported or sensitive state field: {unknown[0]}"
        )
    if missing:
        raise CollectionWriteValidationError(
            f"Missing state field required for replay: {missing[0]}"
        )
    result = _json_object(value, "state_entry")
    if result.get("doc_id") != doc_id:
        raise CollectionWriteValidationError(
            "state_entry.doc_id must match the collection document id."
        )
    _validate_relative_path(result.get("relative_path"), "state_entry.relative_path")
    _validate_sha256(result.get("sha256"), "state_entry.sha256")
    for name in ("tags", "folder_tags", "accepted_auto_tags", "inherited_tags"):
        tags = result.get(name, [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise CollectionWriteValidationError(
                f"state_entry.{name} must be an array of strings."
            )
    return result


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if _SENSITIVE_KEY.search(key):
            raise CollectionWriteValidationError(
                f"{name} contains a sensitive field that cannot be persisted."
            )
        result[key] = _json_value(raw_value, f"{name}.{key}", depth=0)
    encoded = _canonical_json(result).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise CollectionWriteValidationError(f"{name} is too large.")
    return result


def _json_value(value: Any, name: str, *, depth: int) -> Any:
    if depth > 4:
        raise CollectionWriteValidationError(f"{name} is nested too deeply.")
    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str):
            if "\x00" in value:
                raise CollectionWriteValidationError(
                    f"{name} contains a NUL character."
                )
            if any(pattern.search(value) for pattern in _INLINE_SECRET):
                raise CollectionWriteValidationError(
                    f"{name} appears to contain a credential."
                )
            if any(pattern.search(value) for pattern in _LOCAL_PATHS):
                raise CollectionWriteValidationError(
                    f"{name} contains an absolute local path."
                )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CollectionWriteValidationError(f"{name} must be finite.")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                raise CollectionWriteValidationError(
                    f"{name} contains a sensitive field that cannot be persisted."
                )
            result[key] = _json_value(raw_value, f"{name}.{key}", depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item, f"{name}[]", depth=depth + 1) for item in value]
    raise CollectionWriteValidationError(f"{name} is not JSON-compatible.")


def _encode_vectors(vectors: Mapping[str, Sequence[float]]) -> tuple[str, bytes]:
    if not isinstance(vectors, Mapping):
        raise CollectionWriteValidationError("vectors must be an object.")
    names = {str(name) for name in vectors}
    missing = sorted(_REQUIRED_VECTOR_NAMES - names)
    unknown = sorted(names - _REQUIRED_VECTOR_NAMES)
    if missing:
        raise CollectionWriteValidationError(
            f"Missing vector required for replay: {missing[0]}"
        )
    if unknown:
        raise CollectionWriteValidationError(f"Unsupported vector: {unknown[0]}")
    values = array("f")
    manifest: list[dict[str, int | str]] = []
    for name in sorted(names):
        if not _VECTOR_NAME.fullmatch(name):
            raise CollectionWriteValidationError("Invalid vector name.")
        raw_vector = vectors[name]
        if isinstance(raw_vector, str | bytes | bytearray):
            raise CollectionWriteValidationError(f"Vector {name} must be numeric.")
        vector = [float(component) for component in raw_vector]
        if not 1 <= len(vector) <= MAX_VECTOR_DIMENSION:
            raise CollectionWriteValidationError(
                f"Vector {name} has an invalid dimension."
            )
        if any(not math.isfinite(component) for component in vector):
            raise CollectionWriteValidationError(f"Vector {name} must be finite.")
        offset = len(values)
        values.extend(vector)
        manifest.append({"name": name, "offset": offset, "length": len(vector)})
    if sys.byteorder != "little":  # pragma: no cover - Windows/Linux are little-endian
        values.byteswap()
    return _canonical_json(manifest), values.tobytes()


def _decode_vectors(manifest_json: str, payload: bytes) -> dict[str, list[float]]:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise CollectionWriteOutboxError("Invalid persisted vector manifest.") from exc
    values = array("f")
    try:
        values.frombytes(payload)
    except ValueError as exc:
        raise CollectionWriteOutboxError("Invalid persisted vector payload.") from exc
    if sys.byteorder != "little":  # pragma: no cover - Windows/Linux are little-endian
        values.byteswap()
    result: dict[str, list[float]] = {}
    if not isinstance(manifest, list):
        raise CollectionWriteOutboxError("Invalid persisted vector manifest.")
    for item in manifest:
        if not isinstance(item, dict):
            raise CollectionWriteOutboxError("Invalid persisted vector manifest item.")
        name = str(item.get("name", ""))
        offset = item.get("offset")
        length = item.get("length")
        if (
            not _VECTOR_NAME.fullmatch(name)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or offset < 0
            or length < 1
            or offset + length > len(values)
            or name in result
        ):
            raise CollectionWriteOutboxError("Invalid persisted vector bounds.")
        result[name] = list(values[offset : offset + length])
    if set(result) != _REQUIRED_VECTOR_NAMES:
        raise CollectionWriteOutboxError("Persisted replay vectors are incomplete.")
    return result


def _decode_replay_row(row: sqlite3.Row) -> ReplayCollectionWrite:
    action = str(row["action"])
    state_action = str(row["state_action"])
    fields_json = row["fields_json"]
    manifest_json = row["vector_manifest_json"]
    vectors_blob = row["vectors_blob"]
    state_json = row["state_payload_json"]
    if action == "upsert":
        if fields_json is None or manifest_json is None or vectors_blob is None:
            raise CollectionWriteOutboxError(
                "A pending upsert no longer has a replay payload."
            )
        try:
            collection_fields = json.loads(str(fields_json))
            state_entry = (
                json.loads(str(state_json)) if state_json is not None else None
            )
        except json.JSONDecodeError as exc:
            raise CollectionWriteOutboxError("Invalid persisted replay JSON.") from exc
        if not isinstance(collection_fields, dict) or (
            state_entry is not None and not isinstance(state_entry, dict)
        ):
            raise CollectionWriteOutboxError("Invalid persisted replay object.")
        vectors = _decode_vectors(str(manifest_json), bytes(vectors_blob))
    elif action == "delete":
        collection_fields = None
        vectors = {}
        state_entry = None
    else:
        raise CollectionWriteOutboxError("Invalid persisted collection write action.")
    if state_action not in {"upsert", "delete", "none"}:
        raise CollectionWriteOutboxError("Invalid persisted state write action.")
    return ReplayCollectionWrite(
        sequence=int(row["sequence"]),
        operation_id=str(row["operation_id"]),
        item_id=str(row["item_id"]),
        doc_id=str(row["doc_id"]),
        action=action,  # type: ignore[arg-type]
        state_action=state_action,  # type: ignore[arg-type]
        collection_fields=collection_fields,
        vectors=vectors,
        state_entry=state_entry,
        attempt_count=int(row["attempt_count"]),
    )


def _operation_digest(operation_kind: str, items: Sequence[_EncodedItem]) -> str:
    digest = hashlib.sha256(operation_kind.encode("ascii"))
    for item in items:
        digest.update(bytes.fromhex(item.payload_sha256))
    return digest.hexdigest()


def _checked_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise CollectionWriteValidationError(f"{name} must be a string.")
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise CollectionWriteValidationError(f"{name} is invalid.")
    return normalized


def _checked_item_ids(values: Iterable[str]) -> list[str]:
    result = [_checked_identifier(value, "item_id") for value in values]
    if len(result) != len(set(result)):
        raise CollectionWriteValidationError("Duplicate item_id value.")
    return result


def _checked_error_code(value: Any) -> str:
    if not isinstance(value, str):
        raise CollectionWriteValidationError("Failure code must be a string.")
    normalized = value.strip().lower()
    if not _KIND.fullmatch(normalized):
        raise CollectionWriteValidationError("Failure code is invalid.")
    return normalized


def _checked_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollectionWriteValidationError("limit must be an integer.")
    if not 1 <= value <= MAX_CLAIM_LIMIT:
        raise CollectionWriteValidationError(
            f"limit must be between 1 and {MAX_CLAIM_LIMIT}."
        )
    return value


def _validate_relative_path(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CollectionWriteValidationError(f"{name} must be a POSIX relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise CollectionWriteValidationError(f"{name} must stay below its root.")


def _validate_sha256(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value.lower()):
        raise CollectionWriteValidationError(f"{name} must be a SHA-256 digest.")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _retry_at(now: str, seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise CollectionWriteValidationError(
            "retry_after_seconds must be a finite non-negative number."
        )
    normalized = float(seconds)
    if not math.isfinite(normalized) or normalized < 0:
        raise CollectionWriteValidationError(
            "retry_after_seconds must be a finite non-negative number."
        )
    parsed = datetime.fromisoformat(now)
    return (parsed + timedelta(seconds=normalized)).isoformat(timespec="milliseconds")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _chunks(values: Sequence[Any], size: int = 800) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _redact_secret_match(match: re.Match[str]) -> str:
    label = match.group(1) if match.lastindex else "secret"
    return f"{label}=<redacted>"


__all__ = [
    "CollectionWriteAction",
    "CollectionWriteConflictError",
    "CollectionWriteFailure",
    "CollectionWriteItem",
    "CollectionWriteOperation",
    "CollectionWriteOutbox",
    "CollectionWriteOutboxError",
    "CollectionWriteStatus",
    "CollectionWriteValidationError",
    "OUTBOX_SCHEMA_VERSION",
    "ReplayCollectionWrite",
    "StateWriteAction",
]
