"""Low-memory clustering for large, already-indexed image libraries.

Unlike :mod:`image_clustering`, this engine never materializes the complete
input, edge graph, or ``ClusterSnapshot`` in Python.  Inputs and graph edges
are staged in SQLite in bounded batches.  Connectivity uses dense integer
ordinals and compact arrays; the canonical normalized snapshot is then
streamed directly into :class:`ClusterOperationStore`.

Semantic neighbours must come from an injected provider backed by vectors
already stored in Zvec.  This module has no model client and reports zero API
requests.  Perceptual matching uses a compact BK-tree with a configurable
per-item result cap.  Its expected lookup cost is sub-linear for dispersed
hashes but, like every metric tree, has an O(n^2) adversarial worst case; the
bounded edge fan-out prevents dense hash groups from exhausting memory.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .cluster_operation_store import (
    ClusterOperationStore,
    ClusterOperationValidationError,
    ClusterSnapshotStreamCluster,
    ClusterSnapshotStreamEdge,
    ClusterSnapshotStreamFailure,
    ClusterSnapshotStreamItem,
    ClusterSnapshotVersionRecord,
)
from .image_clustering import (
    ClusterFailure,
    ClusteringCancelled,
    EdgeKind,
    IdentityAnchor,
    ImageClusteringConfig,
    ImageClusterInput,
    SemanticNeighbor,
    SemanticNeighborProvider,
    SemanticNeighborValue,
    _coerce_item,
    _coerce_neighbor,
)

_EDGE_KINDS: Final = frozenset({"exact", "perceptual", "semantic", "legacy"})
_SOURCE_PRIORITY: Final = {
    "manual": 0,
    "folder": 1,
    "accepted": 2,
    "model": 3,
    "inherited": 4,
}
_PRIORITY_SOURCE: Final = {value: key for key, value in _SOURCE_PRIORITY.items()}
DEFAULT_LARGE_CLUSTER_STAGING_BATCH_SIZE: Final = 512
MAX_LARGE_CLUSTER_STAGING_BATCH_SIZE: Final = 4_096
_DEFAULT_PERCEPTUAL_NEIGHBORS: Final = 32
_MAX_FAILURE_TEXT: Final = 1_024

ProgressCallback = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class LargeClusterRunSummary:
    """Bounded result metadata; the complete snapshot remains in SQLite."""

    snapshot_version: str
    snapshot_sha256: str
    input_count: int
    clustered_count: int
    cluster_count: int
    edge_count: int
    failure_count: int
    new_or_changed_count: int
    removed_count: int
    reused_edge_count: int
    semantic_query_count: int
    perceptual_truncated_queries: int
    max_materialized_batch: int
    staging_batch_size: int
    api_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_version": self.snapshot_version,
            "snapshot_sha256": self.snapshot_sha256,
            "input_count": self.input_count,
            "clustered_count": self.clustered_count,
            "cluster_count": self.cluster_count,
            "edge_count": self.edge_count,
            "failure_count": self.failure_count,
            "new_or_changed_count": self.new_or_changed_count,
            "removed_count": self.removed_count,
            "reused_edge_count": self.reused_edge_count,
            "semantic_query_count": self.semantic_query_count,
            "perceptual_truncated_queries": self.perceptual_truncated_queries,
            "max_materialized_batch": self.max_materialized_batch,
            "staging_batch_size": self.staging_batch_size,
            "api_requests": self.api_requests,
        }


@dataclass(slots=True)
class _RunMetrics:
    input_count: int = 0
    failure_count: int = 0
    semantic_query_count: int = 0
    perceptual_truncated_queries: int = 0
    max_materialized_batch: int = 0

    def observe_batch(self, size: int) -> None:
        self.max_materialized_batch = max(self.max_materialized_batch, size)


class _CompactDisjointSet:
    """Dense integer DSU requiring about five bytes per staged ordinal."""

    def __init__(self, size: int) -> None:
        if size < 0 or size > 0xFFFFFFFF:
            raise ValueError("Dense clustering ordinal count is out of range")
        self.parent = array("I", range(size))
        self.rank = bytearray(size)

    def find(self, value: int) -> int:
        parent = self.parent
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


class _CompactHammingBKTree:
    """BK-tree storing integer ordinals instead of document-id strings."""

    def __init__(self) -> None:
        self._roots: dict[int, int] = {}
        self._values: list[int] = []
        self._bits = array("H")
        self._item_ordinals = array("I")
        self._first_child = array("i")
        self._next_sibling = array("i")
        self._edge_distance = array("H")
        self._duplicate_head = array("i")
        self._duplicate_ordinals = array("I")
        self._duplicate_next = array("i")

    def add(self, value: int, bit_length: int, item_ordinal: int) -> None:
        root = self._roots.get(bit_length)
        if root is None:
            self._roots[bit_length] = self._append_node(
                value, bit_length, item_ordinal, edge_distance=0
            )
            return
        node = root
        while True:
            distance = (value ^ self._values[node]).bit_count()
            if distance == 0:
                duplicate = len(self._duplicate_ordinals)
                self._duplicate_ordinals.append(item_ordinal)
                self._duplicate_next.append(self._duplicate_head[node])
                self._duplicate_head[node] = duplicate
                return
            child = self._first_child[node]
            while child >= 0 and self._edge_distance[child] != distance:
                child = self._next_sibling[child]
            if child >= 0:
                node = child
                continue
            created = self._append_node(
                value, bit_length, item_ordinal, edge_distance=distance
            )
            self._next_sibling[created] = self._first_child[node]
            self._first_child[node] = created
            return

    def query(
        self, value: int, bit_length: int, radius: int, limit: int
    ) -> tuple[list[tuple[int, int]], bool]:
        root = self._roots.get(bit_length)
        if root is None:
            return [], False
        matches: list[tuple[int, int]] = []
        pending = [root]
        truncated = False
        while pending:
            node = pending.pop()
            distance = (value ^ self._values[node]).bit_count()
            if distance <= radius:
                matches.append((self._item_ordinals[node], distance))
                duplicate = self._duplicate_head[node]
                while duplicate >= 0:
                    if len(matches) >= limit:
                        truncated = True
                        return matches[:limit], truncated
                    matches.append((self._duplicate_ordinals[duplicate], distance))
                    duplicate = self._duplicate_next[duplicate]
                if len(matches) >= limit:
                    truncated = True
                    return matches[:limit], truncated
            lower = distance - radius
            upper = distance + radius
            child = self._first_child[node]
            while child >= 0:
                if lower <= self._edge_distance[child] <= upper:
                    pending.append(child)
                child = self._next_sibling[child]
        return matches, truncated

    def _append_node(
        self, value: int, bit_length: int, item_ordinal: int, *, edge_distance: int
    ) -> int:
        node = len(self._values)
        self._values.append(value)
        self._bits.append(bit_length)
        self._item_ordinals.append(item_ordinal)
        self._first_child.append(-1)
        self._next_sibling.append(-1)
        self._edge_distance.append(edge_distance)
        self._duplicate_head.append(-1)
        return node


class LargeImageClusterEngine:
    """Build a normalized snapshot with bounded Python materialization."""

    def __init__(
        self,
        store: ClusterOperationStore,
        config: ImageClusteringConfig,
        semantic_neighbor_provider: SemanticNeighborProvider | None = None,
        *,
        batch_size: int = DEFAULT_LARGE_CLUSTER_STAGING_BATCH_SIZE,
        max_perceptual_neighbors: int = _DEFAULT_PERCEPTUAL_NEIGHBORS,
    ) -> None:
        if not isinstance(store, ClusterOperationStore):
            raise TypeError("store must be a ClusterOperationStore")
        if not isinstance(config, ImageClusteringConfig):
            raise TypeError("config must be ImageClusteringConfig")
        if (
            isinstance(batch_size, bool)
            or not 1 <= batch_size <= MAX_LARGE_CLUSTER_STAGING_BATCH_SIZE
        ):
            raise ValueError(
                "batch_size must be between 1 and "
                f"{MAX_LARGE_CLUSTER_STAGING_BATCH_SIZE}"
            )
        if (
            isinstance(max_perceptual_neighbors, bool)
            or not 1 <= max_perceptual_neighbors <= 2_000
        ):
            raise ValueError("max_perceptual_neighbors must be between 1 and 2000")
        if config.enable_semantic and semantic_neighbor_provider is None:
            raise ValueError(
                "Large semantic clustering requires an existing-vector "
                "neighbor provider"
            )
        if config.enable_semantic and not config.external_embedding_lookup:
            raise ValueError(
                "Large semantic clustering requires external_embedding_lookup=True"
            )
        self.store = store
        self.config = config
        self.semantic_neighbor_provider = semantic_neighbor_provider
        self.batch_size = batch_size
        self.max_perceptual_neighbors = max_perceptual_neighbors

    @property
    def external_api_calls(self) -> int:
        """The engine consumes persisted vectors and never calls a model."""

        return 0

    def run(
        self,
        library_id: str,
        items: Iterable[ImageClusterInput | Mapping[str, object]],
        *,
        previous_snapshot_version: str | None = None,
        snapshot_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        activate: bool = True,
        cancel_check: Callable[[], object] | None = None,
        progress: ProgressCallback | None = None,
    ) -> LargeClusterRunSummary:
        """Cluster one input stream and persist only a compact result summary."""

        library = _safe_identifier(library_id, "library_id")
        version = _safe_identifier(
            snapshot_version or f"cluster-snapshot-{uuid.uuid4().hex}",
            "snapshot_version",
        )
        run_id = f"cluster-stream-{uuid.uuid4().hex}"
        metrics = _RunMetrics()
        previous = self._validate_previous(library, previous_snapshot_version)
        self._start_run(run_id, library)
        try:
            self._stage_inputs(run_id, items, metrics, cancel_check, progress)
            compatible_previous = bool(
                previous
                and previous.algorithm_version == self.config.algorithm_version
                and previous.embedding_version == self.config.embedding_version
            )
            new_or_changed_count, removed_count, reused_edge_count = (
                self._prepare_incremental_state(
                    run_id,
                    previous,
                    compatible=compatible_previous,
                    cancel_check=cancel_check,
                )
            )
            if self.config.enable_exact_hash:
                self._stage_exact_edges(run_id, metrics, cancel_check, progress)
            if self.config.enable_perceptual_hash:
                self._stage_perceptual_edges(run_id, metrics, cancel_check, progress)
            if self.config.enable_semantic:
                self._stage_semantic_edges(run_id, metrics, cancel_check, progress)
            cluster_count = self._build_components(
                run_id, metrics, cancel_check, progress
            )
            self._set_run_status(run_id, "finalizing")
            clustered_count = self._scalar(
                "SELECT COUNT(*) FROM cluster_stream_items "
                "WHERE run_id = ? AND valid = 1",
                (run_id,),
            )
            edge_count = self._scalar(
                "SELECT COUNT(*) FROM cluster_stream_edges WHERE run_id = ?",
                (run_id,),
            )
            snapshot_metadata = {
                **dict(metadata or {}),
                "engine": "large-image-cluster-stream-v1",
                "staging_batch_size": self.batch_size,
                "max_materialized_batch": metrics.max_materialized_batch,
                "perceptual_neighbor_cap": self.max_perceptual_neighbors,
                "perceptual_truncated_queries": (metrics.perceptual_truncated_queries),
                "api_requests": 0,
            }
            record = self.store.save_snapshot_stream(
                library,
                algorithm_version=self.config.algorithm_version,
                embedding_version=self.config.embedding_version,
                clusters=self._stream_clusters(run_id),
                items=self._stream_items(run_id),
                edges=self._stream_edges(run_id),
                failures=self._stream_failures(run_id),
                snapshot_version=version,
                metadata=snapshot_metadata,
                activate=activate,
                batch_size=self.batch_size,
            )
            self._cleanup_run(run_id)
            _report(progress, f"Clustering snapshot ready: {clustered_count} images")
            return LargeClusterRunSummary(
                snapshot_version=record.snapshot_version,
                snapshot_sha256=record.snapshot_sha256,
                input_count=metrics.input_count,
                clustered_count=clustered_count,
                cluster_count=cluster_count,
                edge_count=edge_count,
                failure_count=metrics.failure_count,
                new_or_changed_count=new_or_changed_count,
                removed_count=removed_count,
                reused_edge_count=reused_edge_count,
                semantic_query_count=metrics.semantic_query_count,
                perceptual_truncated_queries=metrics.perceptual_truncated_queries,
                max_materialized_batch=metrics.max_materialized_batch,
                staging_batch_size=self.batch_size,
            )
        except BaseException:
            # Cancellation, malformed providers, disk errors, and unexpected
            # failures all leave the previously active snapshot untouched.
            self._cleanup_run(run_id)
            raise

    def _validate_previous(
        self, library_id: str, snapshot_version: str | None
    ) -> ClusterSnapshotVersionRecord | None:
        if snapshot_version is None:
            return None
        record = self.store.snapshot_version(snapshot_version)
        if record.library_id != library_id:
            raise ClusterOperationValidationError(
                "Previous cluster snapshot belongs to another library"
            )
        return record

    def _stage_inputs(
        self,
        run_id: str,
        values: Iterable[ImageClusterInput | Mapping[str, object]],
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
        progress: ProgressCallback | None,
    ) -> None:
        item_batch: list[tuple[int, ImageClusterInput, tuple[ClusterFailure, ...]]] = []
        failure_batch: list[ClusterFailure] = []
        next_ordinal = 0
        for index, raw in enumerate(values):
            _check_cancel(cancel_check)
            metrics.input_count += 1
            try:
                item, item_failures = _coerce_item(
                    raw,
                    self.config,
                    allow_missing_embedding=(
                        self.config.external_embedding_lookup
                        or not self.config.enable_semantic
                    ),
                )
                _validate_staged_item(item)
            except (TypeError, ValueError, OverflowError) as exc:
                failure_batch.append(
                    ClusterFailure(
                        _failure_doc_id(raw, index),
                        "invalid_cluster_input",
                        str(exc) or exc.__class__.__name__,
                    )
                )
            else:
                item_batch.append((next_ordinal, item, tuple(item_failures)))
                next_ordinal += 1
            if (
                len(item_batch) >= self.batch_size
                or len(failure_batch) >= self.batch_size
            ):
                self._flush_inputs(run_id, item_batch, failure_batch, metrics)
            if metrics.input_count % 2_000 == 0:
                _report(progress, f"Staging clustering inputs {metrics.input_count}")
        self._flush_inputs(run_id, item_batch, failure_batch, metrics)

    def _flush_inputs(
        self,
        run_id: str,
        item_batch: list[tuple[int, ImageClusterInput, tuple[ClusterFailure, ...]]],
        failure_batch: list[ClusterFailure],
        metrics: _RunMetrics,
    ) -> None:
        if not item_batch and not failure_batch:
            return
        metrics.observe_batch(len(item_batch))
        metrics.observe_batch(len(failure_batch))
        pending_failures: list[ClusterFailure] = []
        with self._write_connection() as connection:
            for failure in failure_batch:
                pending_failures.append(failure)
                if len(pending_failures) >= self.batch_size:
                    metrics.observe_batch(len(pending_failures))
                    self._insert_failures(connection, run_id, pending_failures, metrics)
                    pending_failures.clear()
            for ordinal, item, item_failures in item_batch:
                cursor = connection.execute(
                    """
                    INSERT INTO cluster_stream_items (
                        run_id, ordinal, doc_id, sha256, fingerprint,
                        perceptual_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, doc_id) DO NOTHING
                    """,
                    (
                        run_id,
                        ordinal,
                        item.doc_id,
                        item.sha256.casefold(),
                        item.fingerprint,
                        item.perceptual_hash,
                    ),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        """
                        UPDATE cluster_stream_items SET valid = 0
                        WHERE run_id = ? AND doc_id = ?
                        """,
                        (run_id, item.doc_id),
                    )
                    pending_failures.append(
                        ClusterFailure(
                            item.doc_id,
                            "duplicate_doc_id",
                            "Duplicate document id was excluded.",
                        )
                    )
                    if len(pending_failures) >= self.batch_size:
                        metrics.observe_batch(len(pending_failures))
                        self._insert_failures(
                            connection, run_id, pending_failures, metrics
                        )
                        pending_failures.clear()
                    continue
                for failure in item_failures:
                    pending_failures.append(failure)
                    if len(pending_failures) >= self.batch_size:
                        metrics.observe_batch(len(pending_failures))
                        self._insert_failures(
                            connection, run_id, pending_failures, metrics
                        )
                        pending_failures.clear()
                evidence_rows: list[tuple[object, ...]] = []
                for evidence_index, evidence in enumerate(item.identity_evidence):
                    if len(evidence.value) > 256 or any(
                        character in evidence.value for character in "\r\n\x00"
                    ):
                        pending_failures.append(
                            ClusterFailure(
                                item.doc_id,
                                "invalid_identity_evidence",
                                "Identity evidence is too long or unsafe.",
                            )
                        )
                        if len(pending_failures) >= self.batch_size:
                            metrics.observe_batch(len(pending_failures))
                            self._insert_failures(
                                connection, run_id, pending_failures, metrics
                            )
                            pending_failures.clear()
                        continue
                    evidence_rows.append(
                        (
                            run_id,
                            ordinal,
                            evidence_index,
                            evidence.category,
                            evidence.value,
                            " ".join(evidence.value.casefold().split()),
                            evidence.source,
                            _SOURCE_PRIORITY[evidence.source],
                            evidence.confidence,
                        )
                    )
                if evidence_rows:
                    connection.executemany(
                        """
                        INSERT INTO cluster_stream_evidence (
                            run_id, item_ordinal, evidence_index, category,
                            value, normalized_value, source, source_priority,
                            confidence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        evidence_rows,
                    )
            metrics.observe_batch(len(pending_failures))
            self._insert_failures(connection, run_id, pending_failures, metrics)
        item_batch.clear()
        failure_batch.clear()

    def _prepare_incremental_state(
        self,
        run_id: str,
        previous: ClusterSnapshotVersionRecord | None,
        *,
        compatible: bool,
        cancel_check: Callable[[], object] | None,
    ) -> tuple[int, int, int]:
        _check_cancel(cancel_check)
        if previous is None:
            return (
                self._scalar(
                    "SELECT COUNT(*) FROM cluster_stream_items "
                    "WHERE run_id = ? AND valid = 1",
                    (run_id,),
                ),
                0,
                0,
            )
        with self._write_connection() as connection:
            if compatible:
                connection.execute(
                    """
                    UPDATE cluster_stream_items AS current SET changed = 0
                    WHERE current.run_id = ? AND current.valid = 1 AND EXISTS (
                        SELECT 1 FROM cluster_snapshot_items AS previous
                        WHERE previous.snapshot_version = ?
                          AND previous.doc_id = current.doc_id
                          AND previous.fingerprint = current.fingerprint
                    )
                    """,
                    (run_id, previous.snapshot_version),
                )
                enabled_kinds: list[str] = []
                if self.config.enable_exact_hash:
                    enabled_kinds.append("exact")
                if self.config.enable_perceptual_hash:
                    enabled_kinds.append("perceptual")
                if self.config.enable_semantic:
                    enabled_kinds.extend(("semantic", "legacy"))
                if enabled_kinds:
                    placeholders = ",".join("?" for _ in enabled_kinds)
                    before = connection.total_changes
                    connection.execute(
                        f"""
                        INSERT OR IGNORE INTO cluster_stream_edges (
                            run_id, left_ordinal, right_ordinal, kind, score
                        )
                        SELECT ?,
                               MIN(left_current.ordinal, right_current.ordinal),
                               MAX(left_current.ordinal, right_current.ordinal),
                               edge.kind, edge.score
                        FROM cluster_snapshot_edges AS edge
                        JOIN cluster_snapshot_items AS left_previous
                          ON left_previous.snapshot_version = edge.snapshot_version
                         AND left_previous.doc_id = edge.left_doc_id
                        JOIN cluster_snapshot_items AS right_previous
                          ON right_previous.snapshot_version = edge.snapshot_version
                         AND right_previous.doc_id = edge.right_doc_id
                        JOIN cluster_stream_items AS left_current
                          ON left_current.run_id = ?
                         AND left_current.doc_id = edge.left_doc_id
                         AND left_current.valid = 1
                         AND left_current.changed = 0
                        JOIN cluster_stream_items AS right_current
                          ON right_current.run_id = ?
                         AND right_current.doc_id = edge.right_doc_id
                         AND right_current.valid = 1
                         AND right_current.changed = 0
                        WHERE edge.snapshot_version = ?
                          AND edge.kind IN ({placeholders})
                        """,
                        (
                            run_id,
                            run_id,
                            run_id,
                            previous.snapshot_version,
                            *enabled_kinds,
                        ),
                    )
                    reused = connection.total_changes - before
                else:
                    reused = 0
            else:
                reused = 0
            changed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_stream_items "
                    "WHERE run_id = ? AND valid = 1 AND changed = 1",
                    (run_id,),
                ).fetchone()[0]
            )
            removed = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cluster_snapshot_items AS previous
                    WHERE previous.snapshot_version = ? AND NOT EXISTS (
                        SELECT 1 FROM cluster_stream_items AS current
                        WHERE current.run_id = ? AND current.valid = 1
                          AND current.doc_id = previous.doc_id
                    )
                    """,
                    (previous.snapshot_version, run_id),
                ).fetchone()[0]
            )
        return changed, removed, reused

    def _stage_exact_edges(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
        progress: ProgressCallback | None,
    ) -> None:
        edge_batch: list[tuple[int, int, str, float | None]] = []
        current_sha: str | None = None
        root_ordinal = -1
        processed = 0
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT ordinal, sha256 FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1
                ORDER BY sha256, doc_id
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    processed += 1
                    sha256 = str(row["sha256"])
                    ordinal = int(row["ordinal"])
                    if sha256 != current_sha:
                        current_sha = sha256
                        root_ordinal = ordinal
                    elif ordinal != root_ordinal:
                        left, right = sorted((root_ordinal, ordinal))
                        edge_batch.append((left, right, "exact", 1.0))
                    if len(edge_batch) >= self.batch_size:
                        self._insert_edges(run_id, edge_batch, metrics)
                if processed % 10_000 == 0:
                    _report(progress, f"Clustering exact hashes {processed}")
        self._insert_edges(run_id, edge_batch, metrics)

    def _stage_perceptual_edges(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
        progress: ProgressCallback | None,
    ) -> None:
        tree = _CompactHammingBKTree()
        edge_batch: list[tuple[int, int, str, float | None]] = []
        processed = 0
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT ordinal, perceptual_hash FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1 AND perceptual_hash IS NOT NULL
                ORDER BY ordinal
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    processed += 1
                    ordinal = int(row["ordinal"])
                    perceptual_hash = str(row["perceptual_hash"])
                    bit_length = len(perceptual_hash) * 4
                    value = int(perceptual_hash, 16)
                    matches, truncated = tree.query(
                        value,
                        bit_length,
                        self.config.perceptual_hash_distance,
                        self.max_perceptual_neighbors,
                    )
                    metrics.observe_batch(len(matches))
                    if truncated:
                        metrics.perceptual_truncated_queries += 1
                    for other, distance in matches:
                        left, right = sorted((ordinal, other))
                        edge_batch.append(
                            (
                                left,
                                right,
                                "perceptual",
                                1.0 - (distance / bit_length),
                            )
                        )
                    tree.add(value, bit_length, ordinal)
                    if len(edge_batch) >= self.batch_size:
                        self._insert_edges(run_id, edge_batch, metrics)
                if processed % 10_000 == 0:
                    _report(progress, f"Clustering perceptual hashes {processed}")
        self._insert_edges(run_id, edge_batch, metrics)

    def _stage_semantic_edges(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
        progress: ProgressCallback | None,
    ) -> None:
        provider = self.semantic_neighbor_provider
        if provider is None:  # Guarded in __init__; keeps the type narrow here.
            return
        edge_batch: list[tuple[int, int, str, float | None]] = []
        failure_batch: list[ClusterFailure] = []
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT ordinal, doc_id, sha256, perceptual_hash
                FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1 AND changed = 1
                ORDER BY ordinal
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    item = ImageClusterInput(
                        doc_id=str(row["doc_id"]),
                        sha256=str(row["sha256"]),
                        embedding=None,
                        perceptual_hash=(
                            str(row["perceptual_hash"])
                            if row["perceptual_hash"] is not None
                            else None
                        ),
                    )
                    metrics.semantic_query_count += 1
                    try:
                        candidates = _bounded_neighbors(
                            provider(item, self.config.semantic_top_k),
                            self.config.semantic_top_k,
                            cancel_check,
                        )
                        metrics.observe_batch(len(candidates))
                        by_id = self._lookup_ordinals(
                            run_id, [neighbor.doc_id for neighbor in candidates]
                        )
                        for neighbor in candidates:
                            if (
                                neighbor.doc_id == item.doc_id
                                or neighbor.similarity
                                < self.config.semantic_similarity_threshold
                            ):
                                continue
                            other = by_id.get(neighbor.doc_id)
                            if other is None:
                                continue
                            left, right = sorted((int(row["ordinal"]), other))
                            if left == right:
                                continue
                            edge_batch.append(
                                (left, right, "semantic", neighbor.similarity)
                            )
                    except ClusteringCancelled:
                        raise
                    except Exception as exc:
                        failure_batch.append(
                            ClusterFailure(
                                item.doc_id,
                                "semantic_neighbor_failed",
                                str(exc) or exc.__class__.__name__,
                            )
                        )
                    if len(edge_batch) >= self.batch_size:
                        self._insert_edges(run_id, edge_batch, metrics)
                    if len(failure_batch) >= self.batch_size:
                        self._flush_failures(run_id, failure_batch, metrics)
                if metrics.semantic_query_count % 2_000 == 0:
                    _report(
                        progress,
                        "Clustering semantic neighbours "
                        f"{metrics.semantic_query_count}",
                    )
        self._insert_edges(run_id, edge_batch, metrics)
        self._flush_failures(run_id, failure_batch, metrics)

    def _build_components(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
        progress: ProgressCallback | None,
    ) -> int:
        capacity = self._scalar(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 "
            "FROM cluster_stream_items WHERE run_id = ?",
            (run_id,),
        )
        dsu = _CompactDisjointSet(capacity)
        processed_edges = 0
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT left_ordinal, right_ordinal FROM cluster_stream_edges
                WHERE run_id = ? ORDER BY left_ordinal, right_ordinal, kind
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    dsu.union(int(row["left_ordinal"]), int(row["right_ordinal"]))
                    processed_edges += 1
                if processed_edges % 20_000 == 0:
                    _report(progress, f"Resolving cluster graph {processed_edges}")

        update_batch: list[tuple[int, str, int]] = []
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT ordinal FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1 ORDER BY ordinal
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    ordinal = int(row["ordinal"])
                    update_batch.append((dsu.find(ordinal), run_id, ordinal))
                if len(update_batch) >= self.batch_size:
                    self._update_components(update_batch, metrics)
        self._update_components(update_batch, metrics)
        del dsu

        self._materialize_component_rows(run_id, metrics, cancel_check)
        self._materialize_component_edge_kinds(run_id)
        self._materialize_component_anchors(run_id, metrics)
        self._assign_snapshot_orders(run_id, metrics, cancel_check)
        return self._scalar(
            "SELECT COUNT(*) FROM cluster_stream_components WHERE run_id = ?",
            (run_id,),
        )

    def _materialize_component_rows(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
    ) -> None:
        component_batch: list[tuple[object, ...]] = []
        current_component: int | None = None
        representative = ""
        member_count = 0
        digest: Any = None

        def finish_component() -> None:
            nonlocal member_count
            if current_component is None or digest is None:
                return
            component_batch.append(
                (
                    run_id,
                    current_component,
                    f"clu_{digest.hexdigest()[:20]}",
                    member_count,
                    representative,
                )
            )
            if len(component_batch) >= self.batch_size:
                self._insert_component_rows(component_batch, metrics)

        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT component, doc_id, sha256 FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1
                ORDER BY component, doc_id
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    component = int(row["component"])
                    if component != current_component:
                        finish_component()
                        current_component = component
                        representative = str(row["doc_id"])
                        member_count = 0
                        digest = hashlib.sha256()
                        _digest_field(digest, self.config.algorithm_version)
                        _digest_field(digest, self.config.embedding_version)
                    member_count += 1
                    _digest_field(digest, str(row["doc_id"]))
                    _digest_field(digest, str(row["sha256"]))
        finish_component()
        self._insert_component_rows(component_batch, metrics)

    def _materialize_component_edge_kinds(self, run_id: str) -> None:
        with self._write_connection() as connection:
            for kind, column in (
                ("exact", "has_exact"),
                ("perceptual", "has_perceptual"),
                ("semantic", "has_semantic"),
            ):
                connection.execute(
                    f"""
                    UPDATE cluster_stream_components SET {column} = 1
                    WHERE run_id = ? AND component IN (
                        SELECT DISTINCT item.component
                        FROM cluster_stream_edges AS edge
                        JOIN cluster_stream_items AS item
                          ON item.run_id = edge.run_id
                         AND item.ordinal = edge.left_ordinal
                        WHERE edge.run_id = ? AND edge.kind = ?
                    )
                    """,
                    (run_id, run_id, kind),
                )

    def _materialize_component_anchors(self, run_id: str, metrics: _RunMetrics) -> None:
        updates: list[tuple[str, str, int]] = []
        current_component: int | None = None
        anchors: list[dict[str, object]] = []

        def flush_component() -> None:
            if current_component is None:
                return
            updates.append(
                (
                    json.dumps(
                        anchors,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    run_id,
                    current_component,
                )
            )
            if len(updates) >= self.batch_size:
                self._update_anchor_rows(updates, metrics)

        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                WITH grouped AS (
                    SELECT item.component AS component,
                           evidence.category AS category,
                           evidence.normalized_value AS normalized_value,
                           MIN(evidence.value) AS display_value,
                           COUNT(*) AS support,
                           MAX(evidence.confidence) AS confidence,
                           MIN(evidence.source_priority) AS source_priority
                    FROM cluster_stream_evidence AS evidence
                    JOIN cluster_stream_items AS item
                      ON item.run_id = evidence.run_id
                     AND item.ordinal = evidence.item_ordinal
                    WHERE evidence.run_id = ? AND item.valid = 1
                    GROUP BY item.component, evidence.category,
                             evidence.normalized_value
                ), ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY component, category
                               ORDER BY source_priority, support DESC,
                                        confidence DESC, normalized_value
                           ) AS choice,
                           COUNT(*) OVER (
                               PARTITION BY component, category
                           ) AS variant_count
                    FROM grouped
                )
                SELECT component, category, display_value, support,
                       confidence, source_priority, variant_count
                FROM ranked WHERE choice = 1
                ORDER BY component, category
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    component = int(row["component"])
                    if component != current_component:
                        flush_component()
                        current_component = component
                        anchors = []
                    anchors.append(
                        {
                            "category": str(row["category"]),
                            "value": str(row["display_value"]),
                            "source": _PRIORITY_SOURCE.get(
                                int(row["source_priority"]), "model"
                            ),
                            "confidence": float(row["confidence"]),
                            "support": int(row["support"]),
                            "conflict": int(row["variant_count"]) > 1,
                        }
                    )
        flush_component()
        self._update_anchor_rows(updates, metrics)

    def _assign_snapshot_orders(
        self,
        run_id: str,
        metrics: _RunMetrics,
        cancel_check: Callable[[], object] | None,
    ) -> None:
        updates: list[tuple[int, str, int]] = []
        snapshot_order = 0
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT ordinal FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1 ORDER BY doc_id
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    updates.append((snapshot_order, run_id, int(row["ordinal"])))
                    snapshot_order += 1
                if len(updates) >= self.batch_size:
                    self._update_snapshot_orders(updates, metrics)
        self._update_snapshot_orders(updates, metrics)

        member_updates: list[tuple[int, str, int]] = []
        current_component: int | None = None
        member_order = 0
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT component, ordinal FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1 ORDER BY component, doc_id
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                metrics.observe_batch(len(rows))
                for row in rows:
                    _check_cancel(cancel_check)
                    component = int(row["component"])
                    if component != current_component:
                        current_component = component
                        member_order = 0
                    member_updates.append((member_order, run_id, int(row["ordinal"])))
                    member_order += 1
                if len(member_updates) >= self.batch_size:
                    self._update_member_orders(member_updates, metrics)
        self._update_member_orders(member_updates, metrics)

    def _stream_clusters(self, run_id: str) -> Iterator[ClusterSnapshotStreamCluster]:
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT cluster_id, member_count, representative_doc_id,
                       has_exact, has_perceptual, has_semantic,
                       identity_anchors_json
                FROM cluster_stream_components WHERE run_id = ?
                ORDER BY cluster_id
                """,
                (run_id,),
            )
            for row in cursor:
                edge_kinds = cast(
                    tuple[EdgeKind, ...],
                    tuple(
                        kind
                        for kind, enabled in (
                            ("exact", row["has_exact"]),
                            ("perceptual", row["has_perceptual"]),
                            ("semantic", row["has_semantic"]),
                        )
                        if bool(enabled)
                    ),
                )
                anchors = tuple(
                    IdentityAnchor(
                        category=str(value["category"]),
                        value=str(value["value"]),
                        source=str(value["source"]),
                        confidence=float(value["confidence"]),
                        support=int(value["support"]),
                        conflict=bool(value["conflict"]),
                    )
                    for value in json.loads(str(row["identity_anchors_json"]))
                )
                yield ClusterSnapshotStreamCluster(
                    cluster_id=str(row["cluster_id"]),
                    member_count=int(row["member_count"]),
                    representative_doc_id=str(row["representative_doc_id"]),
                    edge_kinds=edge_kinds,
                    identity_anchors=anchors,
                )

    def _stream_items(self, run_id: str) -> Iterator[ClusterSnapshotStreamItem]:
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT item.doc_id, item.fingerprint, component.cluster_id,
                       item.snapshot_order, item.cluster_member_order
                FROM cluster_stream_items AS item
                JOIN cluster_stream_components AS component
                  ON component.run_id = item.run_id
                 AND component.component = item.component
                WHERE item.run_id = ? AND item.valid = 1
                ORDER BY item.snapshot_order
                """,
                (run_id,),
            )
            for row in cursor:
                yield ClusterSnapshotStreamItem(
                    doc_id=str(row["doc_id"]),
                    fingerprint=str(row["fingerprint"]),
                    cluster_id=str(row["cluster_id"]),
                    snapshot_order=int(row["snapshot_order"]),
                    cluster_member_order=int(row["cluster_member_order"]),
                )

    def _stream_edges(self, run_id: str) -> Iterator[ClusterSnapshotStreamEdge]:
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT CASE WHEN left_item.doc_id < right_item.doc_id
                            THEN left_item.doc_id
                            ELSE right_item.doc_id END AS left_doc_id,
                       CASE WHEN left_item.doc_id < right_item.doc_id
                            THEN right_item.doc_id
                            ELSE left_item.doc_id END AS right_doc_id,
                       edge.kind, edge.score
                FROM cluster_stream_edges AS edge
                JOIN cluster_stream_items AS left_item
                  ON left_item.run_id = edge.run_id
                 AND left_item.ordinal = edge.left_ordinal
                JOIN cluster_stream_items AS right_item
                  ON right_item.run_id = edge.run_id
                 AND right_item.ordinal = edge.right_ordinal
                WHERE edge.run_id = ?
                ORDER BY left_doc_id, right_doc_id, edge.kind,
                         COALESCE(edge.score, -2.0) DESC
                """,
                (run_id,),
            )
            for row in cursor:
                yield ClusterSnapshotStreamEdge(
                    left_doc_id=str(row["left_doc_id"]),
                    right_doc_id=str(row["right_doc_id"]),
                    kind=cast(EdgeKind, str(row["kind"])),
                    score=(float(row["score"]) if row["score"] is not None else None),
                )

    def _stream_failures(self, run_id: str) -> Iterator[ClusterSnapshotStreamFailure]:
        with self._read_connection() as connection:
            cursor = connection.execute(
                """
                SELECT doc_id, code, message FROM cluster_stream_failures
                WHERE run_id = ? ORDER BY failure_index
                """,
                (run_id,),
            )
            for row in cursor:
                yield ClusterSnapshotStreamFailure(
                    doc_id=str(row["doc_id"]),
                    code=str(row["code"]),
                    message=str(row["message"]),
                )

    def _insert_edges(
        self,
        run_id: str,
        values: list[tuple[int, int, str, float | None]],
        metrics: _RunMetrics,
    ) -> None:
        if not values:
            return
        while values:
            batch = values[: self.batch_size]
            del values[: self.batch_size]
            metrics.observe_batch(len(batch))
            with self._write_connection() as connection:
                connection.executemany(
                    """
                    INSERT INTO cluster_stream_edges (
                        run_id, left_ordinal, right_ordinal, kind, score
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, left_ordinal, right_ordinal, kind)
                    DO UPDATE SET score = CASE
                        WHEN excluded.score IS NULL THEN score
                        WHEN score IS NULL OR excluded.score > score
                            THEN excluded.score
                        ELSE score END
                    """,
                    [(run_id, *row) for row in batch],
                )

    def _flush_failures(
        self,
        run_id: str,
        values: list[ClusterFailure],
        metrics: _RunMetrics,
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            self._insert_failures(connection, run_id, values, metrics)
        values.clear()

    @staticmethod
    def _insert_failures(
        connection: sqlite3.Connection,
        run_id: str,
        values: Sequence[ClusterFailure],
        metrics: _RunMetrics,
    ) -> None:
        if not values:
            return
        start = metrics.failure_count
        rows = [
            (
                run_id,
                start + index,
                _safe_failure_field(value.doc_id, maximum=512, fallback="unknown"),
                _safe_failure_field(
                    value.code, maximum=128, fallback="clustering_failed"
                ),
                _safe_failure_field(
                    value.message,
                    maximum=_MAX_FAILURE_TEXT,
                    fallback="Clustering item failed.",
                ),
            )
            for index, value in enumerate(values)
        ]
        connection.executemany(
            """
            INSERT INTO cluster_stream_failures (
                run_id, failure_index, doc_id, code, message
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        metrics.failure_count += len(rows)

    def _insert_component_rows(
        self, values: list[tuple[object, ...]], metrics: _RunMetrics
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            connection.executemany(
                """
                INSERT INTO cluster_stream_components (
                    run_id, component, cluster_id, member_count,
                    representative_doc_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )
        values.clear()

    def _update_components(
        self, values: list[tuple[int, str, int]], metrics: _RunMetrics
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            connection.executemany(
                """
                UPDATE cluster_stream_items SET component = ?
                WHERE run_id = ? AND ordinal = ?
                """,
                values,
            )
        values.clear()

    def _update_anchor_rows(
        self, values: list[tuple[str, str, int]], metrics: _RunMetrics
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            connection.executemany(
                """
                UPDATE cluster_stream_components SET identity_anchors_json = ?
                WHERE run_id = ? AND component = ?
                """,
                values,
            )
        values.clear()

    def _update_snapshot_orders(
        self, values: list[tuple[int, str, int]], metrics: _RunMetrics
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            connection.executemany(
                """
                UPDATE cluster_stream_items SET snapshot_order = ?
                WHERE run_id = ? AND ordinal = ?
                """,
                values,
            )
        values.clear()

    def _update_member_orders(
        self, values: list[tuple[int, str, int]], metrics: _RunMetrics
    ) -> None:
        if not values:
            return
        metrics.observe_batch(len(values))
        with self._write_connection() as connection:
            connection.executemany(
                """
                UPDATE cluster_stream_items SET cluster_member_order = ?
                WHERE run_id = ? AND ordinal = ?
                """,
                values,
            )
        values.clear()

    def _lookup_ordinals(self, run_id: str, doc_ids: Sequence[str]) -> dict[str, int]:
        unique = tuple(dict.fromkeys(doc_id for doc_id in doc_ids if doc_id))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT doc_id, ordinal FROM cluster_stream_items
                WHERE run_id = ? AND valid = 1
                  AND doc_id IN ({placeholders})
                """,
                (run_id, *unique),
            ).fetchall()
        return {str(row["doc_id"]): int(row["ordinal"]) for row in rows}

    def _start_run(self, run_id: str, library_id: str) -> None:
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_stream_runs (
                    run_id, library_id, created_at, status
                ) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'building')
                """,
                (run_id, library_id),
            )

    def _set_run_status(self, run_id: str, status: str) -> None:
        if status not in {"building", "finalizing"}:
            raise ValueError("Invalid clustering stream status")
        with self._write_connection() as connection:
            connection.execute(
                "UPDATE cluster_stream_runs SET status = ? WHERE run_id = ?",
                (status, run_id),
            )

    def _cleanup_run(self, run_id: str) -> None:
        try:
            with self._write_connection() as connection:
                connection.execute(
                    "DELETE FROM cluster_stream_runs WHERE run_id = ?", (run_id,)
                )
        except sqlite3.Error:
            # A later store initialization performs the same cascade cleanup.
            pass

    def _scalar(self, sql: str, parameters: Sequence[object]) -> int:
        with self._read_connection() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return int(row[0]) if row is not None else 0

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.store.path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.store.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection
    except BaseException:
        # A setup failure happens before the read/write context managers gain
        # ownership, so they cannot close this partially initialized handle.
        connection.close()
        raise


def _validate_staged_item(item: ImageClusterInput) -> None:
    if (
        not item.doc_id
        or len(item.doc_id) > 512
        or any(character in item.doc_id for character in "\r\n\x00")
    ):
        raise ValueError("doc_id is too long or unsafe")
    if len(item.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in item.sha256.casefold()
    ):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")


def _safe_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > 128 or any(
        character.isspace() or character in "\r\n\x00" for character in normalized
    ):
        raise ValueError(f"{name} is too long or unsafe")
    return normalized


def _safe_failure_field(value: object, *, maximum: int, fallback: str) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    return (normalized or fallback)[:maximum]


def _failure_doc_id(raw: object, index: int) -> str:
    if isinstance(raw, Mapping):
        doc_id = str(raw.get("doc_id") or "").strip()
        if doc_id:
            return doc_id
    if isinstance(raw, ImageClusterInput) and raw.doc_id.strip():
        return raw.doc_id.strip()
    return f"input-{index + 1}"


def _bounded_neighbors(
    values: Iterable[SemanticNeighborValue],
    top_k: int,
    cancel_check: Callable[[], object] | None,
) -> list[SemanticNeighbor]:
    by_doc_id: dict[str, SemanticNeighbor] = {}
    for index, raw in enumerate(values):
        _check_cancel(cancel_check)
        if index >= top_k:
            break
        neighbor = _coerce_neighbor(raw)
        previous = by_doc_id.get(neighbor.doc_id)
        if previous is None or neighbor.similarity > previous.similarity:
            by_doc_id[neighbor.doc_id] = neighbor
    return list(by_doc_id.values())


def _check_cancel(cancel_check: Callable[[], object] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ClusteringCancelled("Image clustering was cancelled.")


def _report(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _digest_field(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


__all__ = ["LargeClusterRunSummary", "LargeImageClusterEngine"]
