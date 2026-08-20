"""Durable manual clustering rules and reversible operation history.

This module is deliberately independent from search, embedding, and model
clients.  It persists existing cluster snapshots, user-authored must-link /
must-not-link rules, and the before/after state required by a later service
layer to apply or undo merge, split, and identity-tag operations safely.

The store never mutates image metadata itself.  Callers first record an
operation, apply each item through the existing domain service, record every
item outcome independently, then complete the batch.  A process restart marks
unfinished work as interrupted/needs-attention instead of guessing success.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, TypeVar, cast

from .image_clustering import (
    CLUSTER_SNAPSHOT_SCHEMA_VERSION,
    IDENTITY_CATEGORIES,
    ClusterEdge,
    ClusterItemState,
    ClusterSnapshot,
    EdgeKind,
    IdentityAnchor,
    ImageCluster,
    load_cluster_snapshot,
)

JsonObject = dict[str, Any]
_StreamRow = TypeVar("_StreamRow")
ManualRuleKind = Literal["must_link", "must_not_link"]
ClusterOperationType = Literal["merge", "split", "apply_identity", "undo"]

CLUSTER_OPERATION_SCHEMA_VERSION: Final = 1
MAX_OPERATION_ITEMS: Final = 10_000
MAX_STATE_JSON_BYTES: Final = 2 * 1024 * 1024
MAX_METADATA_JSON_BYTES: Final = 64 * 1024
MAX_ERROR_TEXT: Final = 1_024

_RULE_KINDS: Final = frozenset({"must_link", "must_not_link"})
_NORMAL_OPERATION_TYPES: Final = frozenset({"merge", "split", "apply_identity"})
_OPERATION_TYPES: Final = frozenset({*_NORMAL_OPERATION_TYPES, "undo"})
_ACTIVE_BATCH_STATUSES: Final = frozenset({"pending", "running"})
_TERMINAL_BATCH_STATUSES: Final = frozenset(
    {"succeeded", "partial", "failed", "cancelled", "interrupted"}
)
_NORMAL_ITEM_RESULTS: Final = frozenset({"applied", "failed", "skipped", "conflict"})
_UNDO_ITEM_RESULTS: Final = frozenset({"undone", "undo_failed", "skipped", "conflict"})
_TERMINAL_ITEM_STATUSES: Final = frozenset(
    {*_NORMAL_ITEM_RESULTS, *_UNDO_ITEM_RESULTS, "interrupted"}
)
_UNDO_STATUSES: Final = frozenset(
    {
        "unavailable",
        "available",
        "running",
        "succeeded",
        "partial",
        "failed",
        "needs_attention",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cluster_operation_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);

INSERT OR IGNORE INTO cluster_operation_meta (singleton, schema_version)
VALUES (1, 1);

CREATE TABLE IF NOT EXISTS cluster_snapshot_versions (
    snapshot_version TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    cluster_count INTEGER NOT NULL CHECK (cluster_count >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_snapshot_active_library
    ON cluster_snapshot_versions(library_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_cluster_snapshot_library_time
    ON cluster_snapshot_versions(library_id, created_at DESC, snapshot_version DESC);

CREATE TABLE IF NOT EXISTS cluster_snapshot_clusters (
    snapshot_version TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count >= 0),
    representative_doc_id TEXT NOT NULL,
    cluster_type TEXT NOT NULL CHECK (
        cluster_type IN ('single', 'exact', 'perceptual', 'semantic')
    ),
    has_exact INTEGER NOT NULL CHECK (has_exact IN (0, 1)),
    has_perceptual INTEGER NOT NULL CHECK (has_perceptual IN (0, 1)),
    has_semantic INTEGER NOT NULL CHECK (has_semantic IN (0, 1)),
    edge_kinds_json TEXT NOT NULL,
    identity_anchors_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, cluster_id),
    FOREIGN KEY (snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_snapshot_items (
    snapshot_version TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    snapshot_order INTEGER NOT NULL CHECK (snapshot_order >= 0),
    cluster_member_order INTEGER NOT NULL CHECK (cluster_member_order >= 0),
    PRIMARY KEY (snapshot_version, doc_id),
    FOREIGN KEY (snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version)
        ON DELETE CASCADE,
    FOREIGN KEY (snapshot_version, cluster_id)
        REFERENCES cluster_snapshot_clusters(snapshot_version, cluster_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_snapshot_items_cluster
    ON cluster_snapshot_items(
        snapshot_version, cluster_id, cluster_member_order, doc_id
    );

CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_snapshot_items_order
    ON cluster_snapshot_items(snapshot_version, snapshot_order);

CREATE INDEX IF NOT EXISTS idx_cluster_snapshot_cluster_page
    ON cluster_snapshot_clusters(
        snapshot_version, member_count DESC, cluster_id
    );

CREATE TABLE IF NOT EXISTS cluster_snapshot_edges (
    snapshot_version TEXT NOT NULL,
    left_doc_id TEXT NOT NULL,
    right_doc_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('exact', 'perceptual', 'semantic', 'legacy')),
    score REAL,
    PRIMARY KEY (snapshot_version, left_doc_id, right_doc_id, kind),
    FOREIGN KEY (snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_snapshot_failures (
    snapshot_version TEXT NOT NULL,
    failure_index INTEGER NOT NULL CHECK (failure_index >= 0),
    doc_id TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, failure_index),
    FOREIGN KEY (snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_snapshot_failures_doc
    ON cluster_snapshot_failures(snapshot_version, doc_id, failure_index);

-- Large clustering runs are materialized here in bounded batches.  These
-- tables are deliberately separate from canonical snapshots: an interrupted
-- run can be removed without exposing a partial snapshot to readers.
CREATE TABLE IF NOT EXISTS cluster_stream_runs (
    run_id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'finalizing')),
    target_snapshot_version TEXT
);

CREATE TABLE IF NOT EXISTS cluster_stream_items (
    run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    doc_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    perceptual_hash TEXT,
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    changed INTEGER NOT NULL DEFAULT 1 CHECK (changed IN (0, 1)),
    component INTEGER,
    snapshot_order INTEGER,
    cluster_member_order INTEGER,
    PRIMARY KEY (run_id, ordinal),
    UNIQUE (run_id, doc_id),
    FOREIGN KEY (run_id) REFERENCES cluster_stream_runs(run_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_stream_items_sha
    ON cluster_stream_items(run_id, valid, sha256, doc_id);
CREATE INDEX IF NOT EXISTS idx_cluster_stream_items_component
    ON cluster_stream_items(run_id, valid, component, doc_id);
CREATE INDEX IF NOT EXISTS idx_cluster_stream_items_order
    ON cluster_stream_items(run_id, valid, snapshot_order);

CREATE TABLE IF NOT EXISTS cluster_stream_evidence (
    run_id TEXT NOT NULL,
    item_ordinal INTEGER NOT NULL,
    evidence_index INTEGER NOT NULL CHECK (evidence_index >= 0),
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    confidence REAL NOT NULL,
    PRIMARY KEY (run_id, item_ordinal, evidence_index),
    FOREIGN KEY (run_id, item_ordinal)
        REFERENCES cluster_stream_items(run_id, ordinal)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_stream_evidence_component
    ON cluster_stream_evidence(run_id, item_ordinal, category, normalized_value);

CREATE TABLE IF NOT EXISTS cluster_stream_edges (
    run_id TEXT NOT NULL,
    left_ordinal INTEGER NOT NULL,
    right_ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('exact', 'perceptual', 'semantic', 'legacy')),
    score REAL,
    PRIMARY KEY (run_id, left_ordinal, right_ordinal, kind),
    CHECK (left_ordinal < right_ordinal),
    FOREIGN KEY (run_id, left_ordinal)
        REFERENCES cluster_stream_items(run_id, ordinal)
        ON DELETE CASCADE,
    FOREIGN KEY (run_id, right_ordinal)
        REFERENCES cluster_stream_items(run_id, ordinal)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_stream_edges_left
    ON cluster_stream_edges(run_id, left_ordinal);
CREATE INDEX IF NOT EXISTS idx_cluster_stream_edges_right
    ON cluster_stream_edges(run_id, right_ordinal);

CREATE TABLE IF NOT EXISTS cluster_stream_components (
    run_id TEXT NOT NULL,
    component INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count > 0),
    representative_doc_id TEXT NOT NULL,
    has_exact INTEGER NOT NULL DEFAULT 0 CHECK (has_exact IN (0, 1)),
    has_perceptual INTEGER NOT NULL DEFAULT 0 CHECK (has_perceptual IN (0, 1)),
    has_semantic INTEGER NOT NULL DEFAULT 0 CHECK (has_semantic IN (0, 1)),
    identity_anchors_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, component),
    UNIQUE (run_id, cluster_id),
    FOREIGN KEY (run_id) REFERENCES cluster_stream_runs(run_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_stream_failures (
    run_id TEXT NOT NULL,
    failure_index INTEGER NOT NULL CHECK (failure_index >= 0),
    doc_id TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (run_id, failure_index),
    FOREIGN KEY (run_id) REFERENCES cluster_stream_runs(run_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_operation_batches (
    operation_id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('merge', 'split', 'apply_identity', 'undo')
    ),
    source_snapshot_version TEXT NOT NULL,
    result_snapshot_version TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'running', 'succeeded', 'partial', 'failed',
            'cancelled', 'interrupted'
        )
    ),
    undo_status TEXT NOT NULL CHECK (
        undo_status IN (
            'unavailable', 'available', 'running', 'succeeded', 'partial',
            'failed', 'needs_attention'
        )
    ),
    undo_of_operation_id TEXT,
    undone_by_operation_id TEXT,
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    succeeded_count INTEGER NOT NULL DEFAULT 0 CHECK (succeeded_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    before_snapshot_json TEXT NOT NULL,
    after_snapshot_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY (source_snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version),
    FOREIGN KEY (result_snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version),
    FOREIGN KEY (undo_of_operation_id)
        REFERENCES cluster_operation_batches(operation_id),
    FOREIGN KEY (undone_by_operation_id)
        REFERENCES cluster_operation_batches(operation_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_operations_library_time
    ON cluster_operation_batches(library_id, submitted_at DESC, operation_id DESC);
CREATE INDEX IF NOT EXISTS idx_cluster_operations_status_time
    ON cluster_operation_batches(status, submitted_at DESC, operation_id DESC);

CREATE TABLE IF NOT EXISTS cluster_operation_items (
    operation_id TEXT NOT NULL,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    item_key TEXT NOT NULL,
    left_doc_id TEXT,
    right_doc_id TEXT,
    doc_id TEXT,
    cluster_id TEXT,
    identity_category TEXT CHECK (
        identity_category IS NULL OR identity_category IN (
            'real_person', 'cosplayer', 'character', 'work'
        )
    ),
    identity_value TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'applied', 'failed', 'skipped', 'conflict',
            'undone', 'undo_failed', 'interrupted'
        )
    ),
    before_json TEXT NOT NULL,
    after_json TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, item_index),
    UNIQUE (operation_id, item_key),
    FOREIGN KEY (operation_id) REFERENCES cluster_operation_batches(operation_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_manual_rules (
    rule_id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    snapshot_version TEXT NOT NULL,
    rule_kind TEXT NOT NULL CHECK (rule_kind IN ('must_link', 'must_not_link')),
    left_doc_id TEXT NOT NULL,
    right_doc_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    created_by_operation_id TEXT,
    revoked_by_operation_id TEXT,
    note TEXT,
    FOREIGN KEY (snapshot_version)
        REFERENCES cluster_snapshot_versions(snapshot_version),
    FOREIGN KEY (created_by_operation_id)
        REFERENCES cluster_operation_batches(operation_id),
    FOREIGN KEY (revoked_by_operation_id)
        REFERENCES cluster_operation_batches(operation_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_rules_library_time
    ON cluster_manual_rules(library_id, created_at DESC, rule_id DESC);
CREATE INDEX IF NOT EXISTS idx_cluster_rules_library_kind_active
    ON cluster_manual_rules(library_id, rule_kind, active, left_doc_id, right_doc_id);
"""


class ClusterOperationStoreError(RuntimeError):
    """Base error for durable cluster-operation storage."""


class ClusterOperationStoreUnavailable(ClusterOperationStoreError):
    """The SQLite database could not be initialized or accessed."""


class ClusterOperationValidationError(ClusterOperationStoreError, ValueError):
    """A rule, snapshot, or operation violates the durable contract."""


class ClusterOperationNotFound(ClusterOperationStoreError, LookupError):
    """The requested snapshot, operation, item, or rule does not exist."""


class InvalidClusterRuleCursor(ClusterOperationValidationError):
    """A rule pagination cursor is malformed or belongs to another query."""


@dataclass(frozen=True, slots=True)
class ClusterOperationItemInput:
    """One independently recoverable mutation inside a manual operation."""

    item_key: str
    left_doc_id: str | None = None
    right_doc_id: str | None = None
    doc_id: str | None = None
    cluster_id: str | None = None
    identity_category: str | None = None
    identity_value: str | None = None
    before: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClusterSnapshotVersionRecord:
    snapshot_version: str
    library_id: str
    schema_version: int
    algorithm_version: str
    embedding_version: str
    snapshot_sha256: str
    created_at: str
    active: bool
    item_count: int
    edge_count: int
    cluster_count: int
    metadata: Mapping[str, Any]

    def to_dict(self) -> JsonObject:
        return {
            "snapshot_version": self.snapshot_version,
            "library_id": self.library_id,
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "embedding_version": self.embedding_version,
            "snapshot_sha256": self.snapshot_sha256,
            "created_at": self.created_at,
            "active": self.active,
            "item_count": self.item_count,
            "edge_count": self.edge_count,
            "cluster_count": self.cluster_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClusterSnapshotStreamCluster:
    """One normalized cluster row produced without a full snapshot object."""

    cluster_id: str
    member_count: int
    representative_doc_id: str
    edge_kinds: tuple[EdgeKind, ...] = ()
    identity_anchors: tuple[IdentityAnchor, ...] = ()


@dataclass(frozen=True, slots=True)
class ClusterSnapshotStreamItem:
    """One normalized member row in deterministic snapshot order."""

    doc_id: str
    fingerprint: str
    cluster_id: str
    snapshot_order: int
    cluster_member_order: int


@dataclass(frozen=True, slots=True)
class ClusterSnapshotStreamEdge:
    """One normalized edge row; endpoints are canonicalized by the store."""

    left_doc_id: str
    right_doc_id: str
    kind: EdgeKind
    score: float | None = None


@dataclass(frozen=True, slots=True)
class ClusterSnapshotStreamFailure:
    """A bounded per-item failure persisted with the resulting snapshot."""

    doc_id: str
    code: str
    message: str


class ClusterOperationStore:
    """SQLite-backed snapshots, manual constraints, and reversible batches."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        recover_interrupted: bool = True,
        redactions: Sequence[str] = (),
    ) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._redactions = tuple(
            value for value in (str(item) for item in redactions) if value
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_connection() as connection:
                connection.executescript(_SCHEMA)
                schema_row = connection.execute(
                    "SELECT schema_version FROM cluster_operation_meta "
                    "WHERE singleton = 1"
                ).fetchone()
                if (
                    schema_row is None
                    or int(schema_row["schema_version"])
                    != CLUSTER_OPERATION_SCHEMA_VERSION
                ):
                    raise ClusterOperationStoreUnavailable(
                        "Unsupported cluster operation database schema."
                    )
                self._recover_streaming_runs(connection)
                if recover_interrupted:
                    self._recover_interrupted(connection)
        except (OSError, sqlite3.Error) as exc:
            raise ClusterOperationStoreUnavailable(
                "Unable to initialize the cluster operation database."
            ) from exc

    @property
    def external_api_calls(self) -> int:
        """Persistence is entirely local and can never issue a model request."""

        return 0

    def save_snapshot(
        self,
        library_id: str,
        snapshot: ClusterSnapshot | Mapping[str, object],
        *,
        snapshot_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        activate: bool = True,
    ) -> ClusterSnapshotVersionRecord:
        library = _identifier(library_id, "library_id")
        normalized = (
            snapshot
            if isinstance(snapshot, ClusterSnapshot)
            else load_cluster_snapshot(snapshot)
        )
        version = _identifier(
            snapshot_version or _generated_id("cluster-snapshot"),
            "snapshot_version",
        )
        if not isinstance(activate, bool):
            raise ClusterOperationValidationError("activate must be a boolean")
        snapshot_payload = normalized.to_dict()
        snapshot_sha256 = hashlib.sha256(
            _canonical_json(snapshot_payload, maximum=128 * 1024 * 1024).encode("utf-8")
        ).hexdigest()
        metadata_json = _safe_json(metadata or {}, maximum=MAX_METADATA_JSON_BYTES)
        cluster_member_orders: dict[tuple[str, str], int] = {}
        for cluster in normalized.clusters:
            if not cluster.member_doc_ids:
                raise ClusterOperationValidationError(
                    "Stored clusters must contain at least one document."
                )
            for member_order, doc_id in enumerate(cluster.member_doc_ids):
                key = (cluster.cluster_id, doc_id)
                if key in cluster_member_orders:
                    raise ClusterOperationValidationError(
                        "Cluster snapshots cannot repeat a member document."
                    )
                cluster_member_orders[key] = member_order
        missing_members = [
            item.doc_id
            for item in normalized.items
            if (item.cluster_id, item.doc_id) not in cluster_member_orders
        ]
        if missing_members:
            raise ClusterOperationValidationError(
                "Snapshot items must belong to their declared cluster: "
                + ", ".join(missing_members[:5])
            )
        created_at = self._now()
        try:
            with self._write_connection() as connection:
                existing = connection.execute(
                    """
                    SELECT 1 FROM cluster_snapshot_versions
                    WHERE snapshot_version = ?
                    """,
                    (version,),
                ).fetchone()
                if existing is not None:
                    raise ClusterOperationValidationError(
                        f"Cluster snapshot version already exists: {version}"
                    )
                if activate:
                    connection.execute(
                        "UPDATE cluster_snapshot_versions SET active = 0 "
                        "WHERE library_id = ? AND active = 1",
                        (library,),
                    )
                connection.execute(
                    """
                    INSERT INTO cluster_snapshot_versions (
                        snapshot_version, library_id, schema_version,
                        algorithm_version, embedding_version, snapshot_sha256,
                        created_at, active, item_count, edge_count, cluster_count,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        library,
                        normalized.schema_version,
                        normalized.algorithm_version,
                        normalized.embedding_version,
                        snapshot_sha256,
                        created_at,
                        int(activate),
                        len(normalized.items),
                        len(normalized.edges),
                        len(normalized.clusters),
                        metadata_json,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO cluster_snapshot_clusters (
                        snapshot_version, cluster_id, member_count,
                        representative_doc_id, cluster_type, has_exact,
                        has_perceptual, has_semantic, edge_kinds_json,
                        identity_anchors_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            version,
                            _identifier(cluster.cluster_id, "cluster_id"),
                            len(cluster.member_doc_ids),
                            _bounded_text(
                                cluster.member_doc_ids[0],
                                "representative_doc_id",
                                maximum=512,
                            ),
                            _inferred_cluster_type(cluster),
                            int("exact" in cluster.edge_kinds),
                            int("perceptual" in cluster.edge_kinds),
                            int("semantic" in cluster.edge_kinds),
                            _safe_json(list(cluster.edge_kinds), maximum=16 * 1024),
                            _safe_json(
                                [
                                    anchor.to_dict()
                                    for anchor in cluster.identity_anchors
                                ],
                                maximum=256 * 1024,
                            ),
                        )
                        for cluster in normalized.clusters
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO cluster_snapshot_items (
                        snapshot_version, doc_id, fingerprint, cluster_id,
                        snapshot_order, cluster_member_order
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            version,
                            _bounded_text(item.doc_id, "doc_id", maximum=512),
                            _bounded_text(item.fingerprint, "fingerprint", maximum=256),
                            _identifier(item.cluster_id, "cluster_id"),
                            snapshot_order,
                            cluster_member_orders[(item.cluster_id, item.doc_id)],
                        )
                        for snapshot_order, item in enumerate(normalized.items)
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO cluster_snapshot_edges (
                        snapshot_version, left_doc_id, right_doc_id, kind, score
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            version,
                            _bounded_text(edge.left_doc_id, "left_doc_id", maximum=512),
                            _bounded_text(
                                edge.right_doc_id, "right_doc_id", maximum=512
                            ),
                            edge.kind,
                            edge.score,
                        )
                        for edge in normalized.edges
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ClusterOperationValidationError(
                "Cluster snapshot violates the normalized storage contract."
            ) from exc
        return self.snapshot_version(version)

    def save_snapshot_stream(
        self,
        library_id: str,
        *,
        algorithm_version: str,
        embedding_version: str,
        clusters: Iterable[ClusterSnapshotStreamCluster],
        items: Iterable[ClusterSnapshotStreamItem],
        edges: Iterable[ClusterSnapshotStreamEdge],
        failures: Iterable[ClusterSnapshotStreamFailure] = (),
        snapshot_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        activate: bool = True,
        batch_size: int = 512,
        schema_version: int = CLUSTER_SNAPSHOT_SCHEMA_VERSION,
    ) -> ClusterSnapshotVersionRecord:
        """Persist normalized snapshot streams with bounded materialization.

        The caller must provide deterministic streams.  Only ``batch_size``
        rows are retained at once and the new version remains invisible until
        every cluster, item, edge, and failure has passed validation.  A
        failed iterator or SQLite write rolls the transaction back atomically.
        """

        library = _identifier(library_id, "library_id")
        algorithm = _bounded_text(algorithm_version, "algorithm_version", maximum=256)
        embedding = _bounded_text(embedding_version, "embedding_version", maximum=256)
        version = _identifier(
            snapshot_version or _generated_id("cluster-snapshot"),
            "snapshot_version",
        )
        if not isinstance(activate, bool):
            raise ClusterOperationValidationError("activate must be a boolean")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version <= 0
        ):
            raise ClusterOperationValidationError(
                "schema_version must be a positive integer"
            )
        stream_batch_size = _limit(batch_size, maximum=4_096)
        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("streamed", True)
        metadata_json = _safe_json(metadata_payload, maximum=MAX_METADATA_JSON_BYTES)
        created_at = self._now()
        digest = hashlib.sha256()
        digest.update(
            _canonical_json(
                {
                    "schema_version": schema_version,
                    "algorithm_version": algorithm,
                    "embedding_version": embedding,
                },
                maximum=4 * 1024,
            ).encode("utf-8")
        )
        cluster_count = 0
        item_count = 0
        edge_count = 0
        failure_count = 0
        declared_members = 0
        try:
            with self._write_connection() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM cluster_snapshot_versions "
                        "WHERE snapshot_version = ?",
                        (version,),
                    ).fetchone()
                    is not None
                ):
                    raise ClusterOperationValidationError(
                        f"Cluster snapshot version already exists: {version}"
                    )
                connection.execute(
                    """
                    INSERT INTO cluster_snapshot_versions (
                        snapshot_version, library_id, schema_version,
                        algorithm_version, embedding_version, snapshot_sha256,
                        created_at, active, item_count, edge_count, cluster_count,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)
                    """,
                    (
                        version,
                        library,
                        schema_version,
                        algorithm,
                        embedding,
                        "0" * 64,
                        created_at,
                        metadata_json,
                    ),
                )
                for cluster_batch in _iter_batches(clusters, stream_batch_size):
                    values: list[tuple[Any, ...]] = []
                    for cluster in cluster_batch:
                        if not isinstance(cluster, ClusterSnapshotStreamCluster):
                            raise ClusterOperationValidationError(
                                "clusters must contain ClusterSnapshotStreamCluster"
                            )
                        cluster_id = _identifier(cluster.cluster_id, "cluster_id")
                        member_count = _positive_int(
                            cluster.member_count, "member_count"
                        )
                        representative = _bounded_text(
                            cluster.representative_doc_id,
                            "representative_doc_id",
                            maximum=512,
                        )
                        edge_kinds = tuple(dict.fromkeys(cluster.edge_kinds))
                        if any(
                            kind not in {"exact", "perceptual", "semantic", "legacy"}
                            for kind in edge_kinds
                        ):
                            raise ClusterOperationValidationError(
                                "Cluster edge kinds are invalid"
                            )
                        anchors_json = _safe_json(
                            [anchor.to_dict() for anchor in cluster.identity_anchors],
                            maximum=256 * 1024,
                        )
                        cluster_type = (
                            "single"
                            if member_count == 1
                            else "exact"
                            if "exact" in edge_kinds
                            else "perceptual"
                            if "perceptual" in edge_kinds
                            else "semantic"
                        )
                        cluster_row = (
                            version,
                            cluster_id,
                            member_count,
                            representative,
                            cluster_type,
                            int("exact" in edge_kinds),
                            int("perceptual" in edge_kinds),
                            int("semantic" in edge_kinds),
                            _safe_json(list(edge_kinds), maximum=16 * 1024),
                            anchors_json,
                        )
                        values.append(cluster_row)
                        declared_members += member_count
                        _stream_hash_update(digest, "cluster", cluster_row[1:])
                    connection.executemany(
                        """
                        INSERT INTO cluster_snapshot_clusters (
                            snapshot_version, cluster_id, member_count,
                            representative_doc_id, cluster_type, has_exact,
                            has_perceptual, has_semantic, edge_kinds_json,
                            identity_anchors_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    cluster_count += len(values)

                for item_batch in _iter_batches(items, stream_batch_size):
                    item_values: list[tuple[Any, ...]] = []
                    for item in item_batch:
                        if not isinstance(item, ClusterSnapshotStreamItem):
                            raise ClusterOperationValidationError(
                                "items must contain ClusterSnapshotStreamItem"
                            )
                        item_row = (
                            version,
                            _bounded_text(item.doc_id, "doc_id", maximum=512),
                            _bounded_text(item.fingerprint, "fingerprint", maximum=256),
                            _identifier(item.cluster_id, "cluster_id"),
                            _non_negative_int(item.snapshot_order, "snapshot_order"),
                            _non_negative_int(
                                item.cluster_member_order,
                                "cluster_member_order",
                            ),
                        )
                        item_values.append(item_row)
                        _stream_hash_update(digest, "item", item_row[1:])
                    connection.executemany(
                        """
                        INSERT INTO cluster_snapshot_items (
                            snapshot_version, doc_id, fingerprint, cluster_id,
                            snapshot_order, cluster_member_order
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        item_values,
                    )
                    item_count += len(item_values)

                if declared_members != item_count:
                    raise ClusterOperationValidationError(
                        "Cluster member counts do not match the item stream"
                    )

                for edge_batch in _iter_batches(edges, stream_batch_size):
                    edge_values: list[tuple[Any, ...]] = []
                    for raw_edge in edge_batch:
                        if not isinstance(raw_edge, ClusterSnapshotStreamEdge):
                            raise ClusterOperationValidationError(
                                "edges must contain ClusterSnapshotStreamEdge"
                            )
                        edge = ClusterEdge(
                            raw_edge.left_doc_id,
                            raw_edge.right_doc_id,
                            raw_edge.kind,
                            raw_edge.score,
                        )
                        edge_row = (
                            version,
                            _bounded_text(edge.left_doc_id, "left_doc_id", maximum=512),
                            _bounded_text(
                                edge.right_doc_id, "right_doc_id", maximum=512
                            ),
                            edge.kind,
                            edge.score,
                        )
                        edge_values.append(edge_row)
                        _stream_hash_update(digest, "edge", edge_row[1:])
                    connection.executemany(
                        """
                        INSERT INTO cluster_snapshot_edges (
                            snapshot_version, left_doc_id, right_doc_id, kind, score
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        edge_values,
                    )
                    edge_count += len(edge_values)

                missing_endpoint = connection.execute(
                    """
                    SELECT 1 FROM cluster_snapshot_edges AS edge
                    LEFT JOIN cluster_snapshot_items AS left_item
                      ON left_item.snapshot_version = edge.snapshot_version
                     AND left_item.doc_id = edge.left_doc_id
                    LEFT JOIN cluster_snapshot_items AS right_item
                      ON right_item.snapshot_version = edge.snapshot_version
                     AND right_item.doc_id = edge.right_doc_id
                    WHERE edge.snapshot_version = ?
                      AND (left_item.doc_id IS NULL OR right_item.doc_id IS NULL)
                    LIMIT 1
                    """,
                    (version,),
                ).fetchone()
                if missing_endpoint is not None:
                    raise ClusterOperationValidationError(
                        "Snapshot edges must reference streamed items"
                    )

                for failure_batch in _iter_batches(failures, stream_batch_size):
                    failure_values: list[tuple[Any, ...]] = []
                    for failure in failure_batch:
                        if not isinstance(failure, ClusterSnapshotStreamFailure):
                            raise ClusterOperationValidationError(
                                "failures must contain ClusterSnapshotStreamFailure"
                            )
                        failure_values.append(
                            (
                                version,
                                failure_count + len(failure_values),
                                _bounded_text(
                                    failure.doc_id, "failure.doc_id", maximum=512
                                ),
                                _bounded_text(
                                    failure.code, "failure.code", maximum=128
                                ),
                                self._safe_error(failure.message)
                                or "Clustering item failed.",
                            )
                        )
                    connection.executemany(
                        """
                        INSERT INTO cluster_snapshot_failures (
                            snapshot_version, failure_index, doc_id, code, message
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        failure_values,
                    )
                    failure_count += len(failure_values)

                snapshot_sha256 = digest.hexdigest()
                if activate:
                    connection.execute(
                        "UPDATE cluster_snapshot_versions SET active = 0 "
                        "WHERE library_id = ? AND active = 1",
                        (library,),
                    )
                metadata_payload["failure_count"] = failure_count
                connection.execute(
                    """
                    UPDATE cluster_snapshot_versions
                    SET snapshot_sha256 = ?, active = ?, item_count = ?,
                        edge_count = ?, cluster_count = ?, metadata_json = ?
                    WHERE snapshot_version = ?
                    """,
                    (
                        snapshot_sha256,
                        int(activate),
                        item_count,
                        edge_count,
                        cluster_count,
                        _safe_json(metadata_payload, maximum=MAX_METADATA_JSON_BYTES),
                        version,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ClusterOperationValidationError(
                "Streamed snapshot violates the normalized storage contract."
            ) from exc
        return self.snapshot_version(version)

    def page_snapshot_failures(
        self, snapshot_version: str, *, offset: int = 0, limit: int = 100
    ) -> JsonObject:
        """Return bounded per-item failures for a streamed clustering run."""

        record = self.snapshot_version(snapshot_version)
        page_offset = _non_negative_int(offset, "offset")
        page_limit = _limit(limit, maximum=1_000)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_snapshot_failures "
                    "WHERE snapshot_version = ?",
                    (record.snapshot_version,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT doc_id, code, message FROM cluster_snapshot_failures
                WHERE snapshot_version = ? ORDER BY failure_index
                LIMIT ? OFFSET ?
                """,
                (record.snapshot_version, page_limit, page_offset),
            ).fetchall()
        return {
            "snapshot_version": record.snapshot_version,
            "total_count": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(rows) < total,
            "items": [dict(row) for row in rows],
            "api_requests": 0,
        }

    def activate_snapshot(self, snapshot_version: str) -> ClusterSnapshotVersionRecord:
        version = _identifier(snapshot_version, "snapshot_version")
        record = self.snapshot_version(version)
        with self._write_connection() as connection:
            connection.execute(
                "UPDATE cluster_snapshot_versions SET active = 0 "
                "WHERE library_id = ? AND active = 1",
                (record.library_id,),
            )
            connection.execute(
                "UPDATE cluster_snapshot_versions SET active = 1 "
                "WHERE snapshot_version = ?",
                (version,),
            )
        return self.snapshot_version(version)

    def snapshot_version(self, snapshot_version: str) -> ClusterSnapshotVersionRecord:
        version = _identifier(snapshot_version, "snapshot_version")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_snapshot_versions WHERE snapshot_version = ?",
                (version,),
            ).fetchone()
        if row is None:
            raise ClusterOperationNotFound("Cluster snapshot version was not found")
        return _snapshot_record(row)

    def active_snapshot_version(
        self, library_id: str
    ) -> ClusterSnapshotVersionRecord | None:
        library = _identifier(library_id, "library_id")
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM cluster_snapshot_versions
                WHERE library_id = ? AND active = 1
                """,
                (library,),
            ).fetchone()
        return _snapshot_record(row) if row is not None else None

    def load_snapshot(self, snapshot_version: str) -> ClusterSnapshot:
        record = self.snapshot_version(snapshot_version)
        with self._read_connection() as connection:
            cluster_rows = connection.execute(
                """
                SELECT * FROM cluster_snapshot_clusters
                WHERE snapshot_version = ? ORDER BY cluster_id
                """,
                (record.snapshot_version,),
            ).fetchall()
            item_rows = connection.execute(
                """
                SELECT * FROM cluster_snapshot_items
                WHERE snapshot_version = ? ORDER BY snapshot_order
                """,
                (record.snapshot_version,),
            ).fetchall()
            member_rows = connection.execute(
                """
                SELECT cluster_id, doc_id FROM cluster_snapshot_items
                WHERE snapshot_version = ?
                ORDER BY cluster_id, cluster_member_order, doc_id
                """,
                (record.snapshot_version,),
            ).fetchall()
            edge_rows = connection.execute(
                """
                SELECT * FROM cluster_snapshot_edges
                WHERE snapshot_version = ?
                ORDER BY left_doc_id, right_doc_id, kind
                """,
                (record.snapshot_version,),
            ).fetchall()
        members: dict[str, list[str]] = {}
        for row in member_rows:
            members.setdefault(str(row["cluster_id"]), []).append(str(row["doc_id"]))
        clusters = tuple(
            ImageCluster(
                cluster_id=str(row["cluster_id"]),
                member_doc_ids=tuple(members.get(str(row["cluster_id"]), [])),
                edge_kinds=tuple(json.loads(row["edge_kinds_json"])),
                identity_anchors=tuple(
                    IdentityAnchor(
                        category=str(anchor["category"]),
                        value=str(anchor["value"]),
                        source=str(anchor["source"]),
                        confidence=float(anchor["confidence"]),
                        support=int(anchor["support"]),
                        conflict=bool(anchor["conflict"]),
                    )
                    for anchor in json.loads(row["identity_anchors_json"])
                ),
            )
            for row in cluster_rows
        )
        return ClusterSnapshot(
            schema_version=record.schema_version,
            algorithm_version=record.algorithm_version,
            embedding_version=record.embedding_version,
            items=tuple(
                ClusterItemState(
                    doc_id=str(row["doc_id"]),
                    fingerprint=str(row["fingerprint"]),
                    cluster_id=str(row["cluster_id"]),
                )
                for row in item_rows
            ),
            edges=tuple(
                ClusterEdge(
                    left_doc_id=str(row["left_doc_id"]),
                    right_doc_id=str(row["right_doc_id"]),
                    # The SQLite CHECK constraint only permits EdgeKind values.
                    kind=cast(EdgeKind, str(row["kind"])),
                    score=(float(row["score"]) if row["score"] is not None else None),
                )
                for row in edge_rows
            ),
            clusters=clusters,
        )

    def page_snapshot_items(
        self, snapshot_version: str, *, offset: int = 0, limit: int = 256
    ) -> JsonObject:
        record = self.snapshot_version(snapshot_version)
        page_offset = _non_negative_int(offset, "offset")
        page_limit = _limit(limit, maximum=2_000)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT doc_id, fingerprint, cluster_id
                FROM cluster_snapshot_items
                WHERE snapshot_version = ? AND snapshot_order >= ?
                ORDER BY snapshot_order LIMIT ?
                """,
                (record.snapshot_version, page_offset, page_limit),
            ).fetchall()
        return {
            "snapshot_version": record.snapshot_version,
            "total_count": record.item_count,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(rows) < record.item_count,
            "items": [dict(row) for row in rows],
        }

    def page_clusters(
        self,
        snapshot_version: str,
        *,
        offset: int = 0,
        limit: int = 100,
        cluster_type: str | None = None,
    ) -> JsonObject:
        """Read one cluster page directly from SQLite without loading a snapshot."""

        record = self.snapshot_version(snapshot_version)
        page_offset = _non_negative_int(offset, "offset")
        page_limit = _limit(limit, maximum=1_000)
        normalized_type = _cluster_type_filter(cluster_type)
        type_sql, type_parameters = _cluster_type_sql(normalized_type)
        where = "snapshot_version = ?" + type_sql
        parameters: tuple[Any, ...] = (
            record.snapshot_version,
            *type_parameters,
        )
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM cluster_snapshot_clusters
                    WHERE {where}
                    """,
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT cluster_id, member_count, representative_doc_id,
                       cluster_type, edge_kinds_json, identity_anchors_json
                FROM cluster_snapshot_clusters WHERE {where}
                ORDER BY member_count DESC, cluster_id
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_limit, page_offset),
            ).fetchall()
        return {
            "snapshot_version": record.snapshot_version,
            "cluster_type": normalized_type,
            "total": total,
            "total_count": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(rows) < total,
            "items": [_cluster_summary_row(row) for row in rows],
            "api_requests": 0,
        }

    def cluster_detail(
        self,
        snapshot_version: str,
        cluster_id: str,
        *,
        offset: int = 0,
        limit: int = 256,
        edge_offset: int = 0,
        edge_limit: int = 1_000,
    ) -> JsonObject:
        """Read one bounded cluster detail page and its internal edges in SQL."""

        record = self.snapshot_version(snapshot_version)
        cluster = _identifier(cluster_id, "cluster_id")
        member_offset = _non_negative_int(offset, "offset")
        member_limit = _limit(limit, maximum=2_000)
        bounded_edge_offset = _non_negative_int(edge_offset, "edge_offset")
        bounded_edge_limit = _limit(edge_limit, maximum=5_000)
        with self._read_connection() as connection:
            cluster_row = connection.execute(
                """
                SELECT cluster_id, member_count, representative_doc_id,
                       cluster_type, edge_kinds_json, identity_anchors_json
                FROM cluster_snapshot_clusters
                WHERE snapshot_version = ? AND cluster_id = ?
                """,
                (record.snapshot_version, cluster),
            ).fetchone()
            if cluster_row is None:
                raise ClusterOperationNotFound("Image cluster was not found")
            member_rows = connection.execute(
                """
                SELECT doc_id, fingerprint, cluster_id
                FROM cluster_snapshot_items
                WHERE snapshot_version = ? AND cluster_id = ?
                ORDER BY cluster_member_order, doc_id LIMIT ? OFFSET ?
                """,
                (
                    record.snapshot_version,
                    cluster,
                    member_limit,
                    member_offset,
                ),
            ).fetchall()
            edge_where = """
                e.snapshot_version = ?
                AND left_item.cluster_id = ?
                AND right_item.cluster_id = ?
            """
            edge_parameters = (record.snapshot_version, cluster, cluster)
            edge_total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM cluster_snapshot_edges AS e
                    JOIN cluster_snapshot_items AS left_item
                      ON left_item.snapshot_version = e.snapshot_version
                     AND left_item.doc_id = e.left_doc_id
                    JOIN cluster_snapshot_items AS right_item
                      ON right_item.snapshot_version = e.snapshot_version
                     AND right_item.doc_id = e.right_doc_id
                    WHERE {edge_where}
                    """,
                    edge_parameters,
                ).fetchone()[0]
            )
            edge_rows = connection.execute(
                f"""
                SELECT e.left_doc_id, e.right_doc_id, e.kind, e.score
                FROM cluster_snapshot_edges AS e
                JOIN cluster_snapshot_items AS left_item
                  ON left_item.snapshot_version = e.snapshot_version
                 AND left_item.doc_id = e.left_doc_id
                JOIN cluster_snapshot_items AS right_item
                  ON right_item.snapshot_version = e.snapshot_version
                 AND right_item.doc_id = e.right_doc_id
                WHERE {edge_where}
                ORDER BY e.left_doc_id, e.right_doc_id, e.kind
                LIMIT ? OFFSET ?
                """,
                (*edge_parameters, bounded_edge_limit, bounded_edge_offset),
            ).fetchall()
        member_total = int(cluster_row["member_count"])
        return {
            "snapshot_version": record.snapshot_version,
            "cluster": _cluster_summary_row(cluster_row),
            "members": {
                "total_count": member_total,
                "offset": member_offset,
                "limit": member_limit,
                "has_more": member_offset + len(member_rows) < member_total,
                "items": [dict(row) for row in member_rows],
            },
            "edges": {
                "total_count": edge_total,
                "offset": bounded_edge_offset,
                "limit": bounded_edge_limit,
                "has_more": bounded_edge_offset + len(edge_rows) < edge_total,
                "items": [dict(row) for row in edge_rows],
            },
            "api_requests": 0,
        }

    def add_rule(
        self,
        *,
        library_id: str,
        snapshot_version: str,
        rule_kind: ManualRuleKind | str,
        left_doc_id: str,
        right_doc_id: str,
        operation_id: str | None = None,
        note: str | None = None,
    ) -> JsonObject:
        library = _identifier(library_id, "library_id")
        version = _identifier(snapshot_version, "snapshot_version")
        kind = _rule_kind(rule_kind)
        left, right = _document_pair(left_doc_id, right_doc_id)
        operation = _optional_identifier(operation_id, "operation_id")
        safe_note = _optional_text(note, "note", maximum=512)
        with self._write_connection() as connection:
            self._validate_snapshot_library(connection, version, library)
            self._validate_documents(connection, version, (left, right))
            if operation:
                self._validate_operation_library(connection, operation, library)
            existing = connection.execute(
                """
                SELECT * FROM cluster_manual_rules
                WHERE library_id = ? AND rule_kind = ?
                  AND left_doc_id = ? AND right_doc_id = ? AND active = 1
                """,
                (library, kind, left, right),
            ).fetchone()
            if existing is not None:
                return {**_rule_row(existing), "created": False}
            opposite = "must_not_link" if kind == "must_link" else "must_link"
            conflict = connection.execute(
                """
                SELECT rule_id FROM cluster_manual_rules
                WHERE library_id = ? AND rule_kind = ?
                  AND left_doc_id = ? AND right_doc_id = ? AND active = 1
                """,
                (library, opposite, left, right),
            ).fetchone()
            if conflict is not None:
                raise ClusterOperationValidationError(
                    "An active opposite manual rule already exists for this pair."
                )
            rule_id = _generated_id("cluster-rule")
            connection.execute(
                """
                INSERT INTO cluster_manual_rules (
                    rule_id, library_id, snapshot_version, rule_kind,
                    left_doc_id, right_doc_id, active, created_at,
                    created_by_operation_id, note
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    rule_id,
                    library,
                    version,
                    kind,
                    left,
                    right,
                    self._now(),
                    operation,
                    safe_note,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cluster_manual_rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
        assert row is not None
        return {**_rule_row(row), "created": True}

    def revoke_rule(
        self, rule_id: str, *, operation_id: str | None = None
    ) -> JsonObject:
        rule = _identifier(rule_id, "rule_id")
        operation = _optional_identifier(operation_id, "operation_id")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_manual_rules WHERE rule_id = ?", (rule,)
            ).fetchone()
            if row is None:
                raise ClusterOperationNotFound("Cluster manual rule was not found")
            if operation:
                self._validate_operation_library(
                    connection, operation, str(row["library_id"])
                )
            if bool(row["active"]):
                connection.execute(
                    """
                    UPDATE cluster_manual_rules SET active = 0, revoked_at = ?,
                        revoked_by_operation_id = ? WHERE rule_id = ?
                    """,
                    (self._now(), operation, rule),
                )
            updated = connection.execute(
                "SELECT * FROM cluster_manual_rules WHERE rule_id = ?", (rule,)
            ).fetchone()
        assert updated is not None
        return _rule_row(updated)

    def list_rules(
        self,
        library_id: str,
        *,
        rule_kind: ManualRuleKind | str | None = None,
        snapshot_version: str | None = None,
        active_only: bool = True,
        cursor: str | None = None,
        limit: int = 100,
    ) -> JsonObject:
        library = _identifier(library_id, "library_id")
        if not isinstance(active_only, bool):
            raise ClusterOperationValidationError("active_only must be a boolean")
        kind = _rule_kind(rule_kind) if rule_kind is not None else None
        version = (
            _identifier(snapshot_version, "snapshot_version")
            if snapshot_version is not None
            else None
        )
        page_limit = _limit(limit, maximum=500)
        cursor_values = _decode_rule_cursor(cursor) if cursor else None
        clauses = ["library_id = ?"]
        parameters: list[Any] = [library]
        if kind is not None:
            clauses.append("rule_kind = ?")
            parameters.append(kind)
        if version is not None:
            clauses.append("snapshot_version = ?")
            parameters.append(version)
        if active_only:
            clauses.append("active = 1")
        if cursor_values is not None:
            clauses.append("(created_at < ? OR (created_at = ? AND rule_id < ?))")
            parameters.extend([cursor_values[0], cursor_values[0], cursor_values[1]])
        where = " AND ".join(clauses)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM cluster_manual_rules WHERE {where}
                ORDER BY created_at DESC, rule_id DESC LIMIT ?
                """,
                (*parameters, page_limit + 1),
            ).fetchall()
        visible = rows[:page_limit]
        return {
            "items": [_rule_row(row) for row in visible],
            "has_more": len(rows) > page_limit,
            "next_cursor": (
                _encode_rule_cursor(
                    str(visible[-1]["created_at"]), str(visible[-1]["rule_id"])
                )
                if len(rows) > page_limit and visible
                else None
            ),
        }

    def create_operation(
        self,
        *,
        library_id: str,
        operation_type: ClusterOperationType | str,
        source_snapshot_version: str,
        items: Sequence[ClusterOperationItemInput],
        before_snapshot: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> JsonObject:
        operation = _operation_type(operation_type, allow_undo=False)
        library = _identifier(library_id, "library_id")
        version = _identifier(source_snapshot_version, "source_snapshot_version")
        operation_key = _identifier(
            operation_id or _generated_id("cluster-op"), "operation_id"
        )
        normalized_items = self._normalize_operation_items(operation, items)
        before_json = _safe_json(before_snapshot, maximum=MAX_STATE_JSON_BYTES)
        metadata_json = _safe_json(metadata or {}, maximum=MAX_METADATA_JSON_BYTES)
        now = self._now()
        with self._write_connection() as connection:
            self._validate_snapshot_library(connection, version, library)
            self._insert_operation(
                connection,
                operation_id=operation_key,
                library_id=library,
                operation_type=operation,
                source_snapshot_version=version,
                undo_of_operation_id=None,
                before_json=before_json,
                metadata_json=metadata_json,
                items=normalized_items,
                submitted_at=now,
            )
        return self.operation(operation_key)

    def start_operation(self, operation_id: str) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        with self._write_connection() as connection:
            row = self._operation_row(connection, operation)
            status = str(row["status"])
            if status == "pending":
                connection.execute(
                    """
                    UPDATE cluster_operation_batches
                    SET status = 'running', started_at = ? WHERE operation_id = ?
                    """,
                    (self._now(), operation),
                )
            elif status != "running":
                raise ClusterOperationValidationError(
                    f"Operation cannot start from terminal status: {status}"
                )
        return self.operation(operation)

    def record_item_result(
        self,
        operation_id: str,
        item_index: int,
        *,
        status: str,
        after: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        index = _non_negative_int(item_index, "item_index")
        after_json = (
            _safe_json(after, maximum=MAX_STATE_JSON_BYTES)
            if after is not None
            else None
        )
        with self._write_connection() as connection:
            batch = self._operation_row(connection, operation)
            operation_type = str(batch["operation_type"])
            allowed = (
                _UNDO_ITEM_RESULTS if operation_type == "undo" else _NORMAL_ITEM_RESULTS
            )
            if status not in allowed:
                raise ClusterOperationValidationError(
                    f"Invalid item result {status!r} for {operation_type}."
                )
            if str(batch["status"]) not in _ACTIVE_BATCH_STATUSES:
                raise ClusterOperationValidationError(
                    "Items cannot be changed after an operation is terminal."
                )
            item = connection.execute(
                """
                SELECT * FROM cluster_operation_items
                WHERE operation_id = ? AND item_index = ?
                """,
                (operation, index),
            ).fetchone()
            if item is None:
                raise ClusterOperationNotFound("Cluster operation item was not found")
            current_status = str(item["status"])
            if current_status in _TERMINAL_ITEM_STATUSES:
                if current_status == status:
                    return _operation_item_row(item)
                raise ClusterOperationValidationError(
                    "A terminal operation item cannot be overwritten."
                )
            now = self._now()
            if str(batch["status"]) == "pending":
                connection.execute(
                    """
                    UPDATE cluster_operation_batches
                    SET status = 'running', started_at = ? WHERE operation_id = ?
                    """,
                    (now, operation),
                )
            connection.execute(
                """
                UPDATE cluster_operation_items SET status = ?, after_json = ?,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE operation_id = ? AND item_index = ?
                """,
                (
                    status,
                    after_json,
                    _optional_identifier(error_code, "error_code"),
                    self._safe_error(error_message),
                    now,
                    operation,
                    index,
                ),
            )
            self._refresh_counts(connection, operation)
            updated = connection.execute(
                """
                SELECT * FROM cluster_operation_items
                WHERE operation_id = ? AND item_index = ?
                """,
                (operation, index),
            ).fetchone()
        assert updated is not None
        return _operation_item_row(updated)

    def complete_operation(
        self,
        operation_id: str,
        *,
        after_snapshot: Mapping[str, Any],
        result_snapshot_version: str | None = None,
    ) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        after_json = _safe_json(after_snapshot, maximum=MAX_STATE_JSON_BYTES)
        result_version = (
            _identifier(result_snapshot_version, "result_snapshot_version")
            if result_snapshot_version is not None
            else None
        )
        with self._write_connection() as connection:
            batch = self._operation_row(connection, operation)
            if str(batch["status"]) not in _ACTIVE_BATCH_STATUSES:
                raise ClusterOperationValidationError(
                    "Only pending or running operations can be completed."
                )
            pending = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cluster_operation_items
                    WHERE operation_id = ? AND status = 'pending'
                    """,
                    (operation,),
                ).fetchone()[0]
            )
            if pending:
                raise ClusterOperationValidationError(
                    "Every operation item needs an explicit result before completion."
                )
            if result_version is not None:
                self._validate_snapshot_library(
                    connection, result_version, str(batch["library_id"])
                )
            counts = self._item_counts(connection, operation)
            final_status = _batch_status(str(batch["operation_type"]), counts)
            operation_type = str(batch["operation_type"])
            undo_status = "unavailable"
            if operation_type != "undo" and counts["succeeded"] > 0:
                undo_status = "available"
            connection.execute(
                """
                UPDATE cluster_operation_batches
                SET status = ?, result_snapshot_version = ?, finished_at = ?,
                    succeeded_count = ?, failed_count = ?, skipped_count = ?,
                    after_snapshot_json = ?, undo_status = ?
                WHERE operation_id = ?
                """,
                (
                    final_status,
                    result_version,
                    self._now(),
                    counts["succeeded"],
                    counts["failed"],
                    counts["skipped"],
                    after_json,
                    undo_status,
                    operation,
                ),
            )
            if operation_type == "undo":
                self._finish_original_undo(
                    connection,
                    original_id=str(batch["undo_of_operation_id"]),
                    undo_id=operation,
                    final_status=final_status,
                )
        return self.operation(operation)

    def fail_operation(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        code = _identifier(error_code, "error_code")
        now = self._now()
        with self._write_connection() as connection:
            batch = self._operation_row(connection, operation)
            if str(batch["status"]) not in _ACTIVE_BATCH_STATUSES:
                raise ClusterOperationValidationError(
                    "Only an unfinished operation can be failed."
                )
            pending_status = (
                "undo_failed" if str(batch["operation_type"]) == "undo" else "failed"
            )
            connection.execute(
                """
                UPDATE cluster_operation_items SET status = ?,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'pending'
                """,
                (
                    pending_status,
                    code,
                    self._safe_error(error_message),
                    now,
                    operation,
                ),
            )
            counts = self._item_counts(connection, operation)
            connection.execute(
                """
                UPDATE cluster_operation_batches SET status = 'failed',
                    finished_at = ?, succeeded_count = ?, failed_count = ?,
                    skipped_count = ?, error_code = ?, error_message = ?,
                    undo_status = CASE
                        WHEN ? > 0 THEN 'needs_attention' ELSE 'unavailable' END
                WHERE operation_id = ?
                """,
                (
                    now,
                    counts["succeeded"],
                    counts["failed"],
                    counts["skipped"],
                    code,
                    self._safe_error(error_message),
                    counts["succeeded"],
                    operation,
                ),
            )
            if str(batch["operation_type"]) == "undo":
                self._finish_original_undo(
                    connection,
                    original_id=str(batch["undo_of_operation_id"]),
                    undo_id=operation,
                    final_status="failed",
                )
        return self.operation(operation)

    def begin_undo(self, operation_id: str) -> JsonObject:
        original_id = _identifier(operation_id, "operation_id")
        undo_id = _generated_id("cluster-undo")
        now = self._now()
        with self._write_connection() as connection:
            original = self._operation_row(connection, original_id)
            if str(original["operation_type"]) == "undo":
                raise ClusterOperationValidationError(
                    "An undo operation cannot itself be used as an undo source."
                )
            if str(original["status"]) not in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "interrupted",
            }:
                raise ClusterOperationValidationError(
                    "Only a terminal operation with applied items can be undone."
                )
            if str(original["undo_status"]) not in {
                "available",
                "partial",
                "failed",
                "needs_attention",
            }:
                raise ClusterOperationValidationError(
                    "This cluster operation is not currently undoable."
                )
            already_undone_rows = connection.execute(
                """
                SELECT DISTINCT item.item_key
                FROM cluster_operation_items AS item
                JOIN cluster_operation_batches AS undo
                  ON undo.operation_id = item.operation_id
                WHERE undo.undo_of_operation_id = ?
                  AND item.status = 'undone'
                """,
                (original_id,),
            ).fetchall()
            already_undone = {str(row["item_key"]) for row in already_undone_rows}
            source_items = connection.execute(
                """
                SELECT * FROM cluster_operation_items
                WHERE operation_id = ? AND status = 'applied'
                ORDER BY item_index
                """,
                (original_id,),
            ).fetchall()
            source_items = [
                row
                for row in source_items
                if str(row["item_key"]) not in already_undone
            ]
            if not source_items:
                raise ClusterOperationValidationError(
                    "The operation has no remaining applied items to undo."
                )
            items = [
                {
                    "item_key": str(row["item_key"]),
                    "left_doc_id": row["left_doc_id"],
                    "right_doc_id": row["right_doc_id"],
                    "doc_id": row["doc_id"],
                    "cluster_id": row["cluster_id"],
                    "identity_category": row["identity_category"],
                    "identity_value": row["identity_value"],
                    "before_json": _safe_json(
                        {
                            "current": json.loads(row["after_json"] or "{}"),
                            "restore": json.loads(row["before_json"]),
                        },
                        maximum=MAX_STATE_JSON_BYTES,
                    ),
                }
                for row in source_items
            ]
            connection.execute(
                """
                INSERT INTO cluster_operation_batches (
                    operation_id, library_id, operation_type,
                    source_snapshot_version, status, undo_status,
                    undo_of_operation_id, submitted_at, total_count,
                    before_snapshot_json, metadata_json
                ) VALUES (?, ?, 'undo', ?, 'pending', 'unavailable', ?, ?, ?, ?, '{}')
                """,
                (
                    undo_id,
                    original["library_id"],
                    original["result_snapshot_version"]
                    or original["source_snapshot_version"],
                    original_id,
                    now,
                    len(items),
                    original["after_snapshot_json"] or "{}",
                ),
            )
            connection.executemany(
                """
                INSERT INTO cluster_operation_items (
                    operation_id, item_index, item_key, left_doc_id,
                    right_doc_id, doc_id, cluster_id, identity_category,
                    identity_value, status, before_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        undo_id,
                        index,
                        item["item_key"],
                        item["left_doc_id"],
                        item["right_doc_id"],
                        item["doc_id"],
                        item["cluster_id"],
                        item["identity_category"],
                        item["identity_value"],
                        item["before_json"],
                        now,
                    )
                    for index, item in enumerate(items)
                ],
            )
            connection.execute(
                """
                UPDATE cluster_operation_batches SET undo_status = 'running',
                    undone_by_operation_id = ? WHERE operation_id = ?
                """,
                (undo_id, original_id),
            )
        return self.operation(undo_id)

    def operation(self, operation_id: str) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_operation_batches WHERE operation_id = ?",
                (operation,),
            ).fetchone()
        if row is None:
            raise ClusterOperationNotFound("Cluster operation was not found")
        return _operation_batch_row(row)

    def operation_items(
        self, operation_id: str, *, offset: int = 0, limit: int = 256
    ) -> JsonObject:
        operation = _identifier(operation_id, "operation_id")
        self.operation(operation)
        page_offset = _non_negative_int(offset, "offset")
        page_limit = _limit(limit, maximum=2_000)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_operation_items "
                    "WHERE operation_id = ?",
                    (operation,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT * FROM cluster_operation_items WHERE operation_id = ?
                ORDER BY item_index LIMIT ? OFFSET ?
                """,
                (operation, page_limit, page_offset),
            ).fetchall()
        return {
            "operation_id": operation,
            "total_count": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(rows) < total,
            "items": [_operation_item_row(row) for row in rows],
        }

    def list_operations(
        self,
        library_id: str,
        *,
        status: str | None = None,
        operation_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> JsonObject:
        library = _identifier(library_id, "library_id")
        if status is not None and status not in (
            _ACTIVE_BATCH_STATUSES | _TERMINAL_BATCH_STATUSES
        ):
            raise ClusterOperationValidationError("Invalid operation status filter")
        kind = (
            _operation_type(operation_type, allow_undo=True)
            if operation_type is not None
            else None
        )
        page_offset = _non_negative_int(offset, "offset")
        page_limit = _limit(limit, maximum=500)
        clauses = ["library_id = ?"]
        parameters: list[Any] = [library]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if kind is not None:
            clauses.append("operation_type = ?")
            parameters.append(kind)
        where = " AND ".join(clauses)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM cluster_operation_batches WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM cluster_operation_batches WHERE {where}
                ORDER BY submitted_at DESC, operation_id DESC LIMIT ? OFFSET ?
                """,
                (*parameters, page_limit, page_offset),
            ).fetchall()
        return {
            "items": [_operation_batch_row(row) for row in rows],
            "total_count": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(rows) < total,
        }

    def stats(self) -> JsonObject:
        with self._read_connection() as connection:
            snapshots = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_snapshot_versions"
                ).fetchone()[0]
            )
            rules = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_manual_rules WHERE active = 1"
                ).fetchone()[0]
            )
            operations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_operation_batches"
                ).fetchone()[0]
            )
        return {
            "schema_version": CLUSTER_OPERATION_SCHEMA_VERSION,
            "snapshots": snapshots,
            "active_rules": rules,
            "operations": operations,
            "external_api_calls": 0,
        }

    def _normalize_operation_items(
        self,
        operation_type: str,
        items: Sequence[ClusterOperationItemInput],
    ) -> list[JsonObject]:
        if (
            isinstance(items, (str, bytes))
            or not 1 <= len(items) <= MAX_OPERATION_ITEMS
        ):
            raise ClusterOperationValidationError(
                f"An operation requires 1 to {MAX_OPERATION_ITEMS} items."
            )
        normalized: list[JsonObject] = []
        seen_keys: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, ClusterOperationItemInput):
                raise ClusterOperationValidationError(
                    f"items[{index}] must be ClusterOperationItemInput."
                )
            item_key = _bounded_text(
                item.item_key, f"items[{index}].item_key", maximum=256
            )
            if item_key in seen_keys:
                raise ClusterOperationValidationError(
                    "Operation item keys must be unique"
                )
            seen_keys.add(item_key)
            left = _optional_text(
                item.left_doc_id, f"items[{index}].left_doc_id", maximum=512
            )
            right = _optional_text(
                item.right_doc_id, f"items[{index}].right_doc_id", maximum=512
            )
            doc_id = _optional_text(item.doc_id, f"items[{index}].doc_id", maximum=512)
            cluster_id = _optional_identifier(
                item.cluster_id, f"items[{index}].cluster_id"
            )
            category = (
                str(item.identity_category).strip().casefold()
                if item.identity_category is not None
                else None
            )
            value = _optional_text(
                item.identity_value,
                f"items[{index}].identity_value",
                maximum=256,
            )
            if operation_type in {"merge", "split"}:
                if left is None or right is None or left == right:
                    raise ClusterOperationValidationError(
                        f"{operation_type} items require two distinct document ids."
                    )
                left, right = sorted((left, right))
                if category is not None or value is not None:
                    raise ClusterOperationValidationError(
                        f"{operation_type} items cannot carry identity tags."
                    )
            elif operation_type == "apply_identity":
                if doc_id is None:
                    raise ClusterOperationValidationError(
                        "apply_identity items require doc_id."
                    )
                if category not in IDENTITY_CATEGORIES:
                    raise ClusterOperationValidationError(
                        "Identity category must be one of real_person, cosplayer, "
                        "character, or work."
                    )
                if value is None:
                    raise ClusterOperationValidationError(
                        "apply_identity items require a non-empty identity value."
                    )
                if left is not None or right is not None:
                    raise ClusterOperationValidationError(
                        "apply_identity items cannot carry document-pair fields."
                    )
            normalized.append(
                {
                    "item_key": item_key,
                    "left_doc_id": left,
                    "right_doc_id": right,
                    "doc_id": doc_id,
                    "cluster_id": cluster_id,
                    "identity_category": category,
                    "identity_value": value,
                    "before_json": _safe_json(
                        item.before, maximum=MAX_STATE_JSON_BYTES
                    ),
                }
            )
        return normalized

    def _insert_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        library_id: str,
        operation_type: str,
        source_snapshot_version: str,
        undo_of_operation_id: str | None,
        before_json: str,
        metadata_json: str,
        items: Sequence[Mapping[str, Any]],
        submitted_at: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO cluster_operation_batches (
                    operation_id, library_id, operation_type,
                    source_snapshot_version, status, undo_status,
                    undo_of_operation_id, submitted_at, total_count,
                    before_snapshot_json, metadata_json
                ) VALUES (?, ?, ?, ?, 'pending', 'unavailable', ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    library_id,
                    operation_type,
                    source_snapshot_version,
                    undo_of_operation_id,
                    submitted_at,
                    len(items),
                    before_json,
                    metadata_json,
                ),
            )
            connection.executemany(
                """
                INSERT INTO cluster_operation_items (
                    operation_id, item_index, item_key, left_doc_id,
                    right_doc_id, doc_id, cluster_id, identity_category,
                    identity_value, status, before_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        operation_id,
                        index,
                        item["item_key"],
                        item["left_doc_id"],
                        item["right_doc_id"],
                        item["doc_id"],
                        item["cluster_id"],
                        item["identity_category"],
                        item["identity_value"],
                        item["before_json"],
                        submitted_at,
                    )
                    for index, item in enumerate(items)
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ClusterOperationValidationError(
                "Cluster operation id or item keys already exist."
            ) from exc

    @staticmethod
    def _operation_row(
        connection: sqlite3.Connection, operation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM cluster_operation_batches WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ClusterOperationNotFound("Cluster operation was not found")
        return row

    @staticmethod
    def _validate_snapshot_library(
        connection: sqlite3.Connection, snapshot_version: str, library_id: str
    ) -> None:
        row = connection.execute(
            "SELECT library_id FROM cluster_snapshot_versions "
            "WHERE snapshot_version = ?",
            (snapshot_version,),
        ).fetchone()
        if row is None:
            raise ClusterOperationNotFound("Cluster snapshot version was not found")
        if str(row["library_id"]) != library_id:
            raise ClusterOperationValidationError(
                "Cluster snapshot belongs to a different library."
            )

    @staticmethod
    def _validate_documents(
        connection: sqlite3.Connection,
        snapshot_version: str,
        doc_ids: Sequence[str],
    ) -> None:
        placeholders = ",".join("?" for _ in doc_ids)
        rows = connection.execute(
            f"""
            SELECT doc_id FROM cluster_snapshot_items
            WHERE snapshot_version = ? AND doc_id IN ({placeholders})
            """,
            (snapshot_version, *doc_ids),
        ).fetchall()
        found = {str(row["doc_id"]) for row in rows}
        missing = sorted(set(doc_ids) - found)
        if missing:
            raise ClusterOperationValidationError(
                "Manual rules reference documents absent from the snapshot: "
                + ", ".join(missing[:5])
            )

    @staticmethod
    def _validate_operation_library(
        connection: sqlite3.Connection, operation_id: str, library_id: str
    ) -> None:
        row = connection.execute(
            "SELECT library_id FROM cluster_operation_batches WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ClusterOperationNotFound("Cluster operation was not found")
        if str(row["library_id"]) != library_id:
            raise ClusterOperationValidationError(
                "Cluster operation belongs to a different library."
            )

    def _refresh_counts(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> None:
        counts = self._item_counts(connection, operation_id)
        connection.execute(
            """
            UPDATE cluster_operation_batches SET succeeded_count = ?,
                failed_count = ?, skipped_count = ? WHERE operation_id = ?
            """,
            (
                counts["succeeded"],
                counts["failed"],
                counts["skipped"],
                operation_id,
            ),
        )

    @staticmethod
    def _item_counts(
        connection: sqlite3.Connection, operation_id: str
    ) -> dict[str, int]:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count FROM cluster_operation_items
            WHERE operation_id = ? GROUP BY status
            """,
            (operation_id,),
        ).fetchall()
        values = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "succeeded": values.get("applied", 0) + values.get("undone", 0),
            "failed": (
                values.get("failed", 0)
                + values.get("undo_failed", 0)
                + values.get("conflict", 0)
                + values.get("interrupted", 0)
            ),
            "skipped": values.get("skipped", 0),
        }

    @staticmethod
    def _finish_original_undo(
        connection: sqlite3.Connection,
        *,
        original_id: str,
        undo_id: str,
        final_status: str,
    ) -> None:
        undone_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM cluster_operation_items
                WHERE operation_id = ? AND status = 'undone'
                """,
                (undo_id,),
            ).fetchone()[0]
        )
        if final_status == "succeeded":
            undo_status = "succeeded"
        elif undone_count:
            # A partially completed rollback changed real state.  Surface it
            # as recoverable attention instead of a generic failed operation.
            undo_status = "needs_attention"
        elif final_status == "failed":
            undo_status = "failed"
        else:
            undo_status = "needs_attention"
        connection.execute(
            """
            UPDATE cluster_operation_batches
            SET undo_status = ?, undone_by_operation_id = ?
            WHERE operation_id = ?
            """,
            (undo_status, undo_id, original_id),
        )

    def _recover_interrupted(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        rows = connection.execute(
            """
            SELECT operation_id, operation_type, undo_of_operation_id
            FROM cluster_operation_batches WHERE status IN ('pending', 'running')
            """
        ).fetchall()
        if not rows:
            return
        operation_ids = [str(row["operation_id"]) for row in rows]
        placeholders = ",".join("?" for _ in operation_ids)
        connection.execute(
            f"""
            UPDATE cluster_operation_items SET status = 'interrupted',
                error_code = 'process_restarted',
                error_message = 'Process stopped before this item completed.',
                updated_at = ?
            WHERE operation_id IN ({placeholders}) AND status = 'pending'
            """,
            (now, *operation_ids),
        )
        for row in rows:
            operation_id = str(row["operation_id"])
            counts = self._item_counts(connection, operation_id)
            connection.execute(
                """
                UPDATE cluster_operation_batches SET status = 'interrupted',
                    finished_at = ?, succeeded_count = ?, failed_count = ?,
                    skipped_count = ?, error_code = 'process_restarted',
                    error_message =
                        'The previous process stopped before the operation completed.',
                    undo_status = CASE
                        WHEN operation_type = 'undo' THEN 'unavailable'
                        ELSE 'needs_attention' END
                WHERE operation_id = ?
                """,
                (
                    now,
                    counts["succeeded"],
                    counts["failed"],
                    counts["skipped"],
                    operation_id,
                ),
            )
            if str(row["operation_type"]) == "undo" and row["undo_of_operation_id"]:
                connection.execute(
                    """
                    UPDATE cluster_operation_batches
                    SET undo_status = 'needs_attention',
                        undone_by_operation_id = ? WHERE operation_id = ?
                    """,
                    (operation_id, row["undo_of_operation_id"]),
                )

    @staticmethod
    def _recover_streaming_runs(connection: sqlite3.Connection) -> None:
        """Remove crash leftovers before any canonical snapshot is exposed."""

        targets = connection.execute(
            """
            SELECT target_snapshot_version FROM cluster_stream_runs
            WHERE target_snapshot_version IS NOT NULL
            """
        ).fetchall()
        for row in targets:
            connection.execute(
                "DELETE FROM cluster_snapshot_versions WHERE snapshot_version = ?",
                (str(row["target_snapshot_version"]),),
            )
        # Child staging rows are removed through foreign-key cascades.
        connection.execute("DELETE FROM cluster_stream_runs")

    def _safe_error(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).replace("\x00", " ").split())
        for secret in self._redactions:
            text = text.replace(secret, "[REDACTED]")
        return text[:MAX_ERROR_TEXT] or None

    def _now(self) -> str:
        return _timestamp(self._clock())

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            raise ClusterOperationStoreUnavailable(
                "Unable to open the cluster operation database."
            ) from exc
        try:
            yield connection
        except sqlite3.Error as exc:
            raise ClusterOperationStoreUnavailable(
                "Unable to read the cluster operation database."
            ) from exc
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            raise ClusterOperationStoreUnavailable(
                "Unable to open the cluster operation database for writing."
            ) from exc
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise ClusterOperationStoreUnavailable(
                    "Unable to open the cluster operation database for writing."
                ) from exc
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            return connection
        except BaseException:
            # sqlite3.Connection.__exit__ only commits or rolls back; it does
            # not close the handle.  Explicitly close partially configured
            # connections so a failed PRAGMA cannot leak a Windows file lock.
            connection.close()
            raise


def _snapshot_record(row: sqlite3.Row) -> ClusterSnapshotVersionRecord:
    return ClusterSnapshotVersionRecord(
        snapshot_version=str(row["snapshot_version"]),
        library_id=str(row["library_id"]),
        schema_version=int(row["schema_version"]),
        algorithm_version=str(row["algorithm_version"]),
        embedding_version=str(row["embedding_version"]),
        snapshot_sha256=str(row["snapshot_sha256"]),
        created_at=str(row["created_at"]),
        active=bool(row["active"]),
        item_count=int(row["item_count"]),
        edge_count=int(row["edge_count"]),
        cluster_count=int(row["cluster_count"]),
        metadata=json.loads(row["metadata_json"]),
    )


def _cluster_summary_row(row: sqlite3.Row) -> JsonObject:
    return {
        "cluster_id": row["cluster_id"],
        "cluster_type": row["cluster_type"],
        "member_count": int(row["member_count"]),
        "representative_doc_id": row["representative_doc_id"],
        "edge_kinds": json.loads(row["edge_kinds_json"]),
        "identity_anchors": json.loads(row["identity_anchors_json"]),
    }


def _rule_row(row: sqlite3.Row) -> JsonObject:
    return {
        "rule_id": row["rule_id"],
        "library_id": row["library_id"],
        "snapshot_version": row["snapshot_version"],
        "rule_kind": row["rule_kind"],
        "left_doc_id": row["left_doc_id"],
        "right_doc_id": row["right_doc_id"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
        "created_by_operation_id": row["created_by_operation_id"],
        "revoked_by_operation_id": row["revoked_by_operation_id"],
        "note": row["note"],
    }


def _operation_batch_row(row: sqlite3.Row) -> JsonObject:
    return {
        "operation_id": row["operation_id"],
        "library_id": row["library_id"],
        "operation_type": row["operation_type"],
        "source_snapshot_version": row["source_snapshot_version"],
        "result_snapshot_version": row["result_snapshot_version"],
        "status": row["status"],
        "undo_status": row["undo_status"],
        "undo_of_operation_id": row["undo_of_operation_id"],
        "undone_by_operation_id": row["undone_by_operation_id"],
        "submitted_at": row["submitted_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "total_count": int(row["total_count"]),
        "succeeded_count": int(row["succeeded_count"]),
        "failed_count": int(row["failed_count"]),
        "skipped_count": int(row["skipped_count"]),
        "before_snapshot": json.loads(row["before_snapshot_json"]),
        "after_snapshot": (
            json.loads(row["after_snapshot_json"])
            if row["after_snapshot_json"] is not None
            else None
        ),
        "metadata": json.loads(row["metadata_json"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


def _operation_item_row(row: sqlite3.Row) -> JsonObject:
    return {
        "operation_id": row["operation_id"],
        "item_index": int(row["item_index"]),
        "item_key": row["item_key"],
        "left_doc_id": row["left_doc_id"],
        "right_doc_id": row["right_doc_id"],
        "doc_id": row["doc_id"],
        "cluster_id": row["cluster_id"],
        "identity_category": row["identity_category"],
        "identity_value": row["identity_value"],
        "status": row["status"],
        "before": json.loads(row["before_json"]),
        "after": (
            json.loads(row["after_json"]) if row["after_json"] is not None else None
        ),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "updated_at": row["updated_at"],
    }


def _batch_status(operation_type: str, counts: Mapping[str, int]) -> str:
    succeeded = int(counts["succeeded"])
    failed = int(counts["failed"])
    skipped = int(counts["skipped"])
    if succeeded and failed:
        return "partial"
    if failed:
        return "failed"
    if succeeded or skipped:
        return "succeeded"
    raise ClusterOperationValidationError(
        f"The {operation_type} operation has no terminal item results."
    )


def _inferred_cluster_type(cluster: ImageCluster) -> str:
    if len(cluster.member_doc_ids) == 1:
        return "single"
    edge_kinds = set(cluster.edge_kinds)
    if "exact" in edge_kinds:
        return "exact"
    if "perceptual" in edge_kinds:
        return "perceptual"
    return "semantic"


def _cluster_type_filter(value: object) -> str:
    if value is None:
        return "all"
    if not isinstance(value, str):
        raise ClusterOperationValidationError("cluster_type must be text")
    normalized = value.strip().casefold() or "all"
    if normalized not in {
        "all",
        "exact",
        "perceptual",
        "semantic",
        "single",
        "near_duplicate",
    }:
        raise ClusterOperationValidationError(
            "cluster_type must be all, exact, perceptual, semantic, single, "
            "or near_duplicate"
        )
    return normalized


def _cluster_type_sql(cluster_type: str) -> tuple[str, tuple[Any, ...]]:
    if cluster_type == "single":
        return " AND member_count = 1", ()
    if cluster_type == "exact":
        return " AND member_count > 1 AND has_exact = 1", ()
    if cluster_type == "perceptual":
        return " AND member_count > 1 AND has_perceptual = 1", ()
    if cluster_type == "semantic":
        return " AND member_count > 1 AND has_semantic = 1", ()
    if cluster_type == "near_duplicate":
        return " AND member_count > 1 AND (has_exact = 1 OR has_perceptual = 1)", ()
    return " AND member_count > 1", ()


def _operation_type(value: object, *, allow_undo: bool) -> str:
    if not isinstance(value, str):
        raise ClusterOperationValidationError("operation_type must be text")
    normalized = value.strip().casefold()
    allowed = _OPERATION_TYPES if allow_undo else _NORMAL_OPERATION_TYPES
    if normalized not in allowed:
        raise ClusterOperationValidationError(
            "operation_type must be merge, split, or apply_identity"
            + (", or undo" if allow_undo else "")
        )
    return normalized


def _rule_kind(value: object) -> str:
    if not isinstance(value, str):
        raise ClusterOperationValidationError("rule_kind must be text")
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in _RULE_KINDS:
        raise ClusterOperationValidationError(
            "rule_kind must be must_link or must_not_link"
        )
    return normalized


def _document_pair(left: object, right: object) -> tuple[str, str]:
    left_text = _bounded_text(left, "left_doc_id", maximum=512)
    right_text = _bounded_text(right, "right_doc_id", maximum=512)
    if left_text == right_text:
        raise ClusterOperationValidationError(
            "Manual rules require two distinct document ids."
        )
    return tuple(sorted((left_text, right_text)))  # type: ignore[return-value]


def _identifier(value: object, name: str) -> str:
    text = _bounded_text(value, name, maximum=128)
    if any(character.isspace() for character in text):
        raise ClusterOperationValidationError(f"{name} must not contain whitespace")
    return text


def _optional_identifier(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClusterOperationValidationError(f"{name} must be non-empty text")
    text = value.strip()
    if len(text) > maximum or any(character in text for character in "\r\n\x00"):
        raise ClusterOperationValidationError(f"{name} is too long or unsafe")
    return text


def _optional_text(value: object, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name, maximum=maximum)


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClusterOperationValidationError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ClusterOperationValidationError(f"{name} must be a positive integer")
    return value


def _limit(value: object, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ClusterOperationValidationError(f"limit must be between 1 and {maximum}")
    return value


def _safe_json(value: object, *, maximum: int) -> str:
    if not isinstance(value, Mapping) and not isinstance(value, (list, tuple)):
        raise ClusterOperationValidationError(
            "Stored state must be a JSON object or list"
        )
    return _canonical_json(value, maximum=maximum)


def _canonical_json(value: object, *, maximum: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClusterOperationValidationError(
            "Stored state must be valid JSON"
        ) from exc
    if len(encoded) > maximum:
        raise ClusterOperationValidationError(
            f"Stored state exceeds the {maximum}-byte limit"
        )
    return encoded.decode("utf-8")


def _iter_batches(
    values: Iterable[_StreamRow], batch_size: int
) -> Iterator[list[_StreamRow]]:
    batch: list[_StreamRow] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _stream_hash_update(digest: Any, row_kind: str, values: Sequence[object]) -> None:
    digest.update(row_kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(_canonical_json(list(values), maximum=1024 * 1024).encode("utf-8"))
    digest.update(b"\n")


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ClusterOperationValidationError("timestamp must not be empty")
        return text
    if not isinstance(value, datetime):
        raise ClusterOperationValidationError("timestamp must be datetime or text")
    normalized = (
        value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    )
    return (
        normalized.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _generated_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _encode_rule_cursor(created_at: str, rule_id: str) -> str:
    payload = json.dumps(
        ["cluster_rules_v1", created_at, rule_id], separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_rule_cursor(cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 2_048:
        raise InvalidClusterRuleCursor("Invalid cluster rule cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidClusterRuleCursor("Invalid cluster rule cursor") from exc
    if (
        not isinstance(value, list)
        or len(value) != 3
        or value[0] != "cluster_rules_v1"
        or not all(isinstance(item, str) and item for item in value[1:])
    ):
        raise InvalidClusterRuleCursor("Invalid cluster rule cursor")
    return str(value[1]), str(value[2])


__all__ = [
    "CLUSTER_OPERATION_SCHEMA_VERSION",
    "ClusterOperationItemInput",
    "ClusterOperationNotFound",
    "ClusterOperationStore",
    "ClusterOperationStoreError",
    "ClusterOperationStoreUnavailable",
    "ClusterOperationValidationError",
    "ClusterSnapshotStreamCluster",
    "ClusterSnapshotStreamEdge",
    "ClusterSnapshotStreamFailure",
    "ClusterSnapshotStreamItem",
    "ClusterSnapshotVersionRecord",
    "InvalidClusterRuleCursor",
]
