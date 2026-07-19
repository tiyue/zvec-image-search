"""Deterministic, incremental clustering over already-indexed image features.

The module is intentionally storage- and model-agnostic.  Callers supply the
SHA-256 digest, optional perceptual hash, and the embedding already stored in
Zvec.  A production integration should inject a semantic-neighbour provider
backed by the existing Collection; this module never generates embeddings or
calls an external model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast

CLUSTER_SNAPSHOT_SCHEMA_VERSION: Final = 2
IDENTITY_CATEGORIES: Final = frozenset(
    {"real_person", "cosplayer", "character", "work"}
)
BLOCKED_PROPAGATION_CATEGORIES: Final = frozenset(
    {"action", "expression", "scene", "camera", "composition"}
)
_HEX: Final = frozenset("0123456789abcdef")
_SOURCE_PRIORITY: Final = {
    "manual": 0,
    "folder": 1,
    "accepted": 2,
    "model": 3,
    "inherited": 4,
}

EdgeKind = Literal["exact", "perceptual", "semantic", "legacy"]


class ImageClusteringError(RuntimeError):
    """Base error for invalid clustering inputs or snapshots."""


class ClusteringCancelled(ImageClusteringError):
    """Raised when the caller requests cancellation between bounded units."""


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    category: str
    value: str
    source: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        category = self.category.strip().casefold()
        value = self.value.strip()
        source = self.source.strip().casefold()
        confidence = float(self.confidence)
        if category not in IDENTITY_CATEGORIES:
            raise ValueError(f"Unsupported identity category: {self.category}")
        if not value:
            raise ValueError("Identity evidence value must not be empty.")
        if source not in _SOURCE_PRIORITY:
            raise ValueError(f"Unsupported identity source: {self.source}")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("Identity evidence confidence must be between 0 and 1.")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ImageClusterInput:
    doc_id: str
    sha256: str
    embedding: tuple[float, ...] | None = None
    perceptual_hash: str | None = None
    identity_evidence: tuple[IdentityEvidence, ...] = ()

    @property
    def fingerprint(self) -> str:
        embedding_digest = ""
        if self.embedding is not None:
            embedding_digest = hashlib.sha256(
                "\x1f".join(value.hex() for value in self.embedding).encode("ascii")
            ).hexdigest()
        payload = {
            "sha256": self.sha256,
            "perceptual_hash": self.perceptual_hash or "",
            "embedding": embedding_digest,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticNeighbor:
    doc_id: str
    similarity: float


SemanticNeighborValue: TypeAlias = (
    SemanticNeighbor | Mapping[str, object] | tuple[str, float]
)
SemanticNeighborProvider: TypeAlias = Callable[
    [ImageClusterInput, int], Iterable[SemanticNeighborValue]
]


@dataclass(frozen=True, slots=True)
class ImageClusteringConfig:
    algorithm_version: str = "image-cluster-v1"
    embedding_version: str = "unknown"
    semantic_similarity_threshold: float = 0.88
    semantic_top_k: int = 12
    perceptual_hash_distance: int = 6
    enable_perceptual_hash: bool = True
    enable_exact_hash: bool = True
    enable_semantic: bool = True
    external_embedding_lookup: bool = False
    embedding_dimension: int | None = None

    def __post_init__(self) -> None:
        if not self.algorithm_version.strip():
            raise ValueError("algorithm_version must not be empty.")
        if not self.embedding_version.strip():
            raise ValueError("embedding_version must not be empty.")
        threshold = float(self.semantic_similarity_threshold)
        if not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0:
            raise ValueError("semantic_similarity_threshold must be between -1 and 1.")
        if isinstance(self.semantic_top_k, bool) or not 1 <= self.semantic_top_k <= 200:
            raise ValueError("semantic_top_k must be between 1 and 200.")
        if (
            isinstance(self.perceptual_hash_distance, bool)
            or not 0 <= self.perceptual_hash_distance <= 64
        ):
            raise ValueError("perceptual_hash_distance must be between 0 and 64.")
        if self.embedding_dimension is not None and (
            isinstance(self.embedding_dimension, bool) or self.embedding_dimension <= 0
        ):
            raise ValueError("embedding_dimension must be a positive integer.")
        if not isinstance(self.enable_exact_hash, bool):
            raise ValueError("enable_exact_hash must be a boolean.")
        if not isinstance(self.enable_perceptual_hash, bool):
            raise ValueError("enable_perceptual_hash must be a boolean.")
        if not isinstance(self.enable_semantic, bool):
            raise ValueError("enable_semantic must be a boolean.")
        if not isinstance(self.external_embedding_lookup, bool):
            raise ValueError("external_embedding_lookup must be a boolean.")
        object.__setattr__(self, "algorithm_version", self.algorithm_version.strip())
        object.__setattr__(self, "embedding_version", self.embedding_version.strip())
        object.__setattr__(self, "semantic_similarity_threshold", threshold)


@dataclass(frozen=True, slots=True)
class ClusterFailure:
    doc_id: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ClusterEdge:
    left_doc_id: str
    right_doc_id: str
    kind: EdgeKind
    score: float | None = None

    def __post_init__(self) -> None:
        left, right = sorted((self.left_doc_id, self.right_doc_id))
        if not left or left == right:
            raise ValueError("Cluster edges require two distinct document ids.")
        if self.kind not in {"exact", "perceptual", "semantic", "legacy"}:
            raise ValueError(f"Unsupported cluster edge kind: {self.kind}")
        if self.score is not None and not math.isfinite(float(self.score)):
            raise ValueError("Cluster edge score must be finite.")
        object.__setattr__(self, "left_doc_id", left)
        object.__setattr__(self, "right_doc_id", right)
        object.__setattr__(
            self, "score", None if self.score is None else float(self.score)
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "left_doc_id": self.left_doc_id,
            "right_doc_id": self.right_doc_id,
            "kind": self.kind,
        }
        if self.score is not None:
            payload["score"] = self.score
        return payload


@dataclass(frozen=True, slots=True)
class ClusterItemState:
    doc_id: str
    fingerprint: str
    cluster_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "doc_id": self.doc_id,
            "fingerprint": self.fingerprint,
            "cluster_id": self.cluster_id,
        }


@dataclass(frozen=True, slots=True)
class IdentityAnchor:
    category: str
    value: str
    source: str
    confidence: float
    support: int
    conflict: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "support": self.support,
            "conflict": self.conflict,
        }


@dataclass(frozen=True, slots=True)
class ImageCluster:
    cluster_id: str
    member_doc_ids: tuple[str, ...]
    edge_kinds: tuple[EdgeKind, ...]
    identity_anchors: tuple[IdentityAnchor, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "member_doc_ids": list(self.member_doc_ids),
            "edge_kinds": list(self.edge_kinds),
            "identity_anchors": [anchor.to_dict() for anchor in self.identity_anchors],
        }


@dataclass(frozen=True, slots=True)
class ClusterSnapshot:
    algorithm_version: str
    embedding_version: str
    items: tuple[ClusterItemState, ...]
    edges: tuple[ClusterEdge, ...]
    clusters: tuple[ImageCluster, ...]
    schema_version: int = CLUSTER_SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "embedding_version": self.embedding_version,
            "items": [item.to_dict() for item in self.items],
            "edges": [edge.to_dict() for edge in self.edges],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ClusterSnapshot:
        return load_cluster_snapshot(payload)


@dataclass(frozen=True, slots=True)
class ClusterRunResult:
    snapshot: ClusterSnapshot
    failures: tuple[ClusterFailure, ...]
    input_count: int
    clustered_count: int
    new_or_changed_count: int
    removed_count: int
    reused_edge_count: int
    semantic_query_count: int
    api_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "failures": [failure.to_dict() for failure in self.failures],
            "input_count": self.input_count,
            "clustered_count": self.clustered_count,
            "new_or_changed_count": self.new_or_changed_count,
            "removed_count": self.removed_count,
            "reused_edge_count": self.reused_edge_count,
            "semantic_query_count": self.semantic_query_count,
            "api_requests": self.api_requests,
        }


@dataclass(frozen=True, slots=True)
class ClusterDetail:
    cluster: ImageCluster
    items: tuple[ClusterItemState, ...]
    edges: tuple[ClusterEdge, ...]


@dataclass(frozen=True, slots=True)
class IdentityPropagationSuggestion:
    cluster_id: str
    doc_id: str
    category: str
    value: str
    anchor_source: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "doc_id": self.doc_id,
            "category": self.category,
            "value": self.value,
            "anchor_source": self.anchor_source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class IdentityPropagationPlan:
    suggestions: tuple[IdentityPropagationSuggestion, ...]
    skipped_conflicts: tuple[dict[str, str], ...]
    allowed_categories: tuple[str, ...] = tuple(sorted(IDENTITY_CATEGORIES))
    blocked_categories: tuple[str, ...] = tuple(sorted(BLOCKED_PROPAGATION_CATEGORIES))
    api_requests: int = 0


class _DisjointSet:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}
        self.rank = {value: 0 for value in self.parent}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


@dataclass(slots=True)
class _BKNode:
    value: int
    bit_length: int
    doc_ids: list[str] = field(default_factory=list)
    children: dict[int, _BKNode] = field(default_factory=dict)


class _HammingBKTree:
    """Exact Hamming-radius lookup without an all-pairs perceptual scan."""

    def __init__(self) -> None:
        self._roots: dict[int, _BKNode] = {}

    def query(self, value: int, bit_length: int, radius: int) -> list[tuple[str, int]]:
        root = self._roots.get(bit_length)
        if root is None:
            return []
        matches: list[tuple[str, int]] = []
        pending = [root]
        while pending:
            node = pending.pop()
            distance = (value ^ node.value).bit_count()
            if distance <= radius:
                matches.extend((doc_id, distance) for doc_id in node.doc_ids)
            lower = distance - radius
            upper = distance + radius
            pending.extend(
                child
                for edge_distance, child in node.children.items()
                if lower <= edge_distance <= upper
            )
        return matches

    def add(self, value: int, bit_length: int, doc_id: str) -> None:
        root = self._roots.get(bit_length)
        if root is None:
            self._roots[bit_length] = _BKNode(value, bit_length, [doc_id])
            return
        node = root
        while True:
            distance = (value ^ node.value).bit_count()
            if distance == 0:
                node.doc_ids.append(doc_id)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _BKNode(value, bit_length, [doc_id])
                return
            node = child


class ImageClusterService:
    """Build an incremental graph and deterministic connected components."""

    def __init__(
        self,
        config: ImageClusteringConfig,
        semantic_neighbor_provider: SemanticNeighborProvider | None = None,
    ) -> None:
        self.config = config
        self.semantic_neighbor_provider = semantic_neighbor_provider

    def run(
        self,
        items: Iterable[ImageClusterInput | Mapping[str, object]],
        *,
        previous_snapshot: ClusterSnapshot | Mapping[str, object] | None = None,
        cancel_check: Callable[[], object] | None = None,
    ) -> ClusterRunResult:
        failures: list[ClusterFailure] = []
        normalized: list[ImageClusterInput] = []
        input_count = 0
        for index, raw in enumerate(items):
            input_count += 1
            _check_cancel(cancel_check)
            try:
                item, item_failures = _coerce_item(
                    raw,
                    self.config,
                    allow_missing_embedding=(
                        self.semantic_neighbor_provider is not None
                        and self.config.external_embedding_lookup
                        or not self.config.enable_semantic
                    ),
                )
            except (TypeError, ValueError) as exc:
                doc_id = _failure_doc_id(raw, index)
                failures.append(
                    ClusterFailure(
                        doc_id,
                        "invalid_cluster_input",
                        str(exc) or exc.__class__.__name__,
                    )
                )
                continue
            normalized.append(item)
            failures.extend(item_failures)

        normalized, duplicate_failures = _remove_duplicate_doc_ids(normalized)
        failures.extend(duplicate_failures)
        normalized.sort(key=lambda item: item.doc_id)
        by_id = {item.doc_id: item for item in normalized}
        previous = (
            None
            if previous_snapshot is None
            else previous_snapshot
            if isinstance(previous_snapshot, ClusterSnapshot)
            else load_cluster_snapshot(previous_snapshot)
        )
        compatible_previous = previous is not None and (
            previous.algorithm_version == self.config.algorithm_version
            and previous.embedding_version == self.config.embedding_version
        )
        previous_items = (
            {item.doc_id: item for item in previous.items} if previous else {}
        )
        changed = {
            item.doc_id
            for item in normalized
            if not compatible_previous
            or item.doc_id not in previous_items
            or previous_items[item.doc_id].fingerprint != item.fingerprint
        }
        removed_count = len(set(previous_items) - set(by_id)) if previous else 0

        dsu = _DisjointSet(by_id)
        edges: dict[tuple[str, str, str], ClusterEdge] = {}
        reused_edge_count = 0
        if compatible_previous and previous is not None:
            for edge in previous.edges:
                if (
                    edge.left_doc_id in by_id
                    and edge.right_doc_id in by_id
                    and edge.left_doc_id not in changed
                    and edge.right_doc_id not in changed
                ):
                    _store_edge(edges, dsu, edge)
                    reused_edge_count += 1

        if self.config.enable_exact_hash:
            _add_exact_edges(normalized, edges, dsu, cancel_check)
        if self.config.enable_perceptual_hash:
            _add_perceptual_edges(
                normalized,
                self.config.perceptual_hash_distance,
                edges,
                dsu,
                cancel_check,
            )

        semantic_query_count = 0
        semantic_targets = (
            []
            if not self.config.enable_semantic
            else normalized
            if not compatible_previous
            else [by_id[doc_id] for doc_id in sorted(changed)]
        )
        default_provider = _default_neighbor_provider(normalized)
        provider = self.semantic_neighbor_provider or default_provider
        for item in semantic_targets:
            _check_cancel(cancel_check)
            if item.embedding is None and not (
                self.semantic_neighbor_provider is not None
                and self.config.external_embedding_lookup
            ):
                continue
            semantic_query_count += 1
            try:
                neighbors = provider(item, self.config.semantic_top_k)
                for neighbor_index, raw_neighbor in enumerate(neighbors):
                    _check_cancel(cancel_check)
                    if neighbor_index >= self.config.semantic_top_k:
                        break
                    neighbor = _coerce_neighbor(raw_neighbor)
                    if neighbor.doc_id == item.doc_id or neighbor.doc_id not in by_id:
                        continue
                    if neighbor.similarity < self.config.semantic_similarity_threshold:
                        continue
                    _store_edge(
                        edges,
                        dsu,
                        ClusterEdge(
                            item.doc_id,
                            neighbor.doc_id,
                            "semantic",
                            neighbor.similarity,
                        ),
                    )
            except ClusteringCancelled:
                raise
            except Exception as exc:
                failures.append(
                    ClusterFailure(
                        item.doc_id,
                        "semantic_neighbor_failed",
                        str(exc) or exc.__class__.__name__,
                    )
                )

        components: dict[str, list[str]] = defaultdict(list)
        for doc_id in sorted(by_id):
            components[dsu.find(doc_id)].append(doc_id)
        component_members = sorted(
            (tuple(sorted(members)) for members in components.values()),
            key=lambda members: members[0],
        )
        cluster_ids = _assign_cluster_ids(
            component_members,
            previous,
            self.config.algorithm_version,
            self.config.embedding_version,
            by_id,
        )
        item_cluster = {
            doc_id: cluster_ids[members]
            for members in component_members
            for doc_id in members
        }
        sorted_edges = tuple(
            sorted(
                edges.values(),
                key=lambda edge: (
                    edge.left_doc_id,
                    edge.right_doc_id,
                    edge.kind,
                    -2.0 if edge.score is None else -edge.score,
                ),
            )
        )
        edges_by_component: dict[str, list[ClusterEdge]] = defaultdict(list)
        for edge in sorted_edges:
            edges_by_component[item_cluster[edge.left_doc_id]].append(edge)
        clusters = tuple(
            ImageCluster(
                cluster_id=cluster_ids[members],
                member_doc_ids=members,
                edge_kinds=tuple(
                    sorted(
                        {edge.kind for edge in edges_by_component[cluster_ids[members]]}
                    )
                ),
                identity_anchors=_identity_anchors(
                    [by_id[doc_id] for doc_id in members]
                ),
            )
            for members in sorted(
                component_members,
                key=lambda value: (cluster_ids[value], value),
            )
        )
        snapshot = ClusterSnapshot(
            algorithm_version=self.config.algorithm_version,
            embedding_version=self.config.embedding_version,
            items=tuple(
                ClusterItemState(
                    item.doc_id, item.fingerprint, item_cluster[item.doc_id]
                )
                for item in normalized
            ),
            edges=sorted_edges,
            clusters=clusters,
        )
        return ClusterRunResult(
            snapshot=snapshot,
            failures=tuple(
                sorted(
                    failures,
                    key=lambda value: (value.doc_id, value.code, value.message),
                )
            ),
            input_count=input_count,
            clustered_count=len(normalized),
            new_or_changed_count=len(changed),
            removed_count=removed_count,
            reused_edge_count=reused_edge_count,
            semantic_query_count=semantic_query_count,
        )


def list_clusters(
    snapshot: ClusterSnapshot | Mapping[str, object],
    *,
    offset: int = 0,
    limit: int = 100,
) -> tuple[int, tuple[ImageCluster, ...]]:
    """Return one deterministic page without binding the core to an HTTP API."""

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    loaded = (
        snapshot
        if isinstance(snapshot, ClusterSnapshot)
        else load_cluster_snapshot(snapshot)
    )
    ordered = tuple(
        sorted(
            loaded.clusters,
            key=lambda cluster: (-len(cluster.member_doc_ids), cluster.cluster_id),
        )
    )
    return len(ordered), ordered[offset : offset + limit]


def cluster_detail(
    snapshot: ClusterSnapshot | Mapping[str, object], cluster_id: str
) -> ClusterDetail | None:
    loaded = (
        snapshot
        if isinstance(snapshot, ClusterSnapshot)
        else load_cluster_snapshot(snapshot)
    )
    target = next(
        (cluster for cluster in loaded.clusters if cluster.cluster_id == cluster_id),
        None,
    )
    if target is None:
        return None
    members = set(target.member_doc_ids)
    return ClusterDetail(
        cluster=target,
        items=tuple(item for item in loaded.items if item.doc_id in members),
        edges=tuple(
            edge
            for edge in loaded.edges
            if edge.left_doc_id in members and edge.right_doc_id in members
        ),
    )


def apply_manual_cluster_rules(
    snapshot: ClusterSnapshot | Mapping[str, object],
    rules: Iterable[Mapping[str, object]],
) -> ClusterSnapshot:
    """Apply durable must-link/must-not-link rules without model calls.

    Must-not-link is enforced across entire components, not merely by deleting
    one direct edge. This prevents an excluded image from rejoining through an
    indirect semantic neighbour on the next incremental clustering run.
    """

    loaded = (
        snapshot
        if isinstance(snapshot, ClusterSnapshot)
        else load_cluster_snapshot(snapshot)
    )
    doc_ids = {item.doc_id for item in loaded.items}
    must_link: set[tuple[str, str]] = set()
    must_not_link: set[tuple[str, str]] = set()
    for raw in rules:
        if raw.get("active") is False:
            continue
        kind = str(raw.get("rule_kind") or "").strip().lower().replace("-", "_")
        left, right = sorted(
            (
                str(raw.get("left_doc_id") or "").strip(),
                str(raw.get("right_doc_id") or "").strip(),
            )
        )
        if kind not in {"must_link", "must_not_link"}:
            raise ImageClusteringError(f"Unsupported manual cluster rule: {kind}")
        if not left or left == right:
            raise ImageClusteringError("Manual cluster rules need two documents.")
        if left not in doc_ids or right not in doc_ids:
            # Rules can outlive deleted documents. They remain durable in the
            # store but do not break clustering of the current library.
            continue
        target = must_link if kind == "must_link" else must_not_link
        target.add((left, right))
    conflict = must_link & must_not_link
    if conflict:
        raise ImageClusteringError(
            "A document pair cannot be both must-link and must-not-link."
        )
    if not must_link and not must_not_link:
        return loaded

    parent = {doc_id: doc_id for doc_id in doc_ids}
    rank = {doc_id: 0 for doc_id in doc_ids}
    members = {doc_id: {doc_id} for doc_id in doc_ids}
    forbidden: dict[str, set[str]] = defaultdict(set)
    for left, right in must_not_link:
        forbidden[left].add(right)
        forbidden[right].add(left)

    def find(doc_id: str) -> str:
        root = parent[doc_id]
        if root != doc_id:
            parent[doc_id] = find(root)
        return parent[doc_id]

    def components_conflict(left_root: str, right_root: str) -> bool:
        left_members = members[left_root]
        right_members = members[right_root]
        if len(left_members) > len(right_members):
            left_members, right_members = right_members, left_members
        return any(forbidden[doc_id] & right_members for doc_id in left_members)

    def union(left: str, right: str, *, forced: bool = False) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return True
        if components_conflict(left_root, right_root):
            if forced:
                raise ImageClusteringError(
                    "A must-link rule conflicts with an active must-not-link rule."
                )
            return False
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        members[left_root].update(members.pop(right_root))
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1
        return True

    for left, right in sorted(must_link):
        union(left, right, forced=True)
    for edge in loaded.edges:
        if (edge.left_doc_id, edge.right_doc_id) in must_not_link:
            continue
        union(edge.left_doc_id, edge.right_doc_id)

    components: dict[str, list[str]] = defaultdict(list)
    for doc_id in sorted(doc_ids):
        components[find(doc_id)].append(doc_id)
    component_members = tuple(
        sorted((tuple(values) for values in components.values()), key=lambda item: item)
    )
    old_clusters = {cluster.cluster_id: cluster for cluster in loaded.clusters}
    old_members = {
        cluster.cluster_id: frozenset(cluster.member_doc_ids)
        for cluster in loaded.clusters
    }
    old_cluster_by_doc = {
        doc_id: cluster.cluster_id
        for cluster in loaded.clusters
        for doc_id in cluster.member_doc_ids
    }
    cluster_ids: dict[tuple[str, ...], str] = {}
    for component in component_members:
        member_set = frozenset(component)
        source_ids = {old_cluster_by_doc[doc_id] for doc_id in component}
        preserved = next(
            (
                cluster_id
                for cluster_id in source_ids
                if old_members[cluster_id] == member_set
            ),
            None,
        )
        cluster_ids[component] = preserved or _manual_cluster_id(
            loaded,
            component,
        )
    cluster_by_doc = {
        doc_id: cluster_ids[component]
        for component in component_members
        for doc_id in component
    }
    edge_map: dict[tuple[str, str, str], ClusterEdge] = {}
    for edge in loaded.edges:
        if cluster_by_doc[edge.left_doc_id] != cluster_by_doc[edge.right_doc_id]:
            continue
        if (edge.left_doc_id, edge.right_doc_id) in must_not_link:
            continue
        edge_map[(edge.left_doc_id, edge.right_doc_id, edge.kind)] = edge
    for left, right in must_link:
        if cluster_by_doc[left] == cluster_by_doc[right]:
            edge_map.setdefault(
                (left, right, "legacy"),
                ClusterEdge(left, right, "legacy", 1.0),
            )
    sorted_edges = tuple(
        sorted(
            edge_map.values(),
            key=lambda edge: (edge.left_doc_id, edge.right_doc_id, edge.kind),
        )
    )
    edges_by_cluster: dict[str, list[ClusterEdge]] = defaultdict(list)
    for edge in sorted_edges:
        edges_by_cluster[cluster_by_doc[edge.left_doc_id]].append(edge)
    clusters: list[ImageCluster] = []
    for component in component_members:
        cluster_id = cluster_ids[component]
        source_ids = {old_cluster_by_doc[doc_id] for doc_id in component}
        whole_sources = all(
            old_members[source_id].issubset(component) for source_id in source_ids
        )
        anchors = (
            _combined_manual_identity_anchors(
                [old_clusters[source_id] for source_id in sorted(source_ids)],
                len(component),
            )
            if whole_sources
            else ()
        )
        clusters.append(
            ImageCluster(
                cluster_id=cluster_id,
                member_doc_ids=component,
                edge_kinds=tuple(
                    sorted({edge.kind for edge in edges_by_cluster[cluster_id]})
                ),
                identity_anchors=anchors,
            )
        )
    return ClusterSnapshot(
        schema_version=loaded.schema_version,
        algorithm_version=loaded.algorithm_version,
        embedding_version=loaded.embedding_version,
        items=tuple(
            ClusterItemState(
                item.doc_id,
                item.fingerprint,
                cluster_by_doc[item.doc_id],
            )
            for item in loaded.items
        ),
        edges=sorted_edges,
        clusters=tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id)),
    )


def _manual_cluster_id(snapshot: ClusterSnapshot, members: Sequence[str]) -> str:
    digest = hashlib.sha256(
        "\x1f".join(
            (
                snapshot.algorithm_version,
                snapshot.embedding_version,
                "manual-rules-v1",
                *members,
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"cluster-{digest}"


def _combined_manual_identity_anchors(
    clusters: Sequence[ImageCluster],
    member_count: int,
) -> tuple[IdentityAnchor, ...]:
    grouped: dict[tuple[str, str], list[IdentityAnchor]] = defaultdict(list)
    category_values: dict[str, set[str]] = defaultdict(set)
    for cluster in clusters:
        for anchor in cluster.identity_anchors:
            key = (anchor.category, _identity_key(anchor.value))
            grouped[key].append(anchor)
            category_values[anchor.category].add(key[1])
    output: list[IdentityAnchor] = []
    for (category, _value_key), values in sorted(grouped.items()):
        best = min(
            values,
            key=lambda anchor: (
                _SOURCE_PRIORITY.get(anchor.source, 99),
                -anchor.confidence,
                anchor.value,
            ),
        )
        output.append(
            IdentityAnchor(
                category=category,
                value=best.value,
                source=best.source,
                confidence=max(value.confidence for value in values),
                support=min(member_count, sum(value.support for value in values)),
                conflict=(
                    len(category_values[category]) > 1
                    or any(value.conflict for value in values)
                ),
            )
        )
    return tuple(output)


def build_identity_propagation_plan(
    snapshot: ClusterSnapshot | Mapping[str, object],
    items: Iterable[ImageClusterInput | Mapping[str, object]],
) -> IdentityPropagationPlan:
    """Create conservative identity-only suggestions without mutating tags.

    Any existing conflicting identity value blocks propagation for that category.
    Non-identity fields are rejected during input normalization and therefore can
    never enter the output plan.
    """

    loaded = (
        snapshot
        if isinstance(snapshot, ClusterSnapshot)
        else load_cluster_snapshot(snapshot)
    )
    normalized: dict[str, ImageClusterInput] = {}
    for raw in items:
        try:
            item, _failures = _coerce_item(raw, None)
        except (TypeError, ValueError):
            continue
        normalized[item.doc_id] = item
    suggestions: list[IdentityPropagationSuggestion] = []
    conflicts: list[dict[str, str]] = []
    for cluster in loaded.clusters:
        for anchor in cluster.identity_anchors:
            if anchor.conflict or anchor.category not in IDENTITY_CATEGORIES:
                continue
            for doc_id in cluster.member_doc_ids:
                target_item = normalized.get(doc_id)
                if target_item is None:
                    continue
                existing = [
                    evidence
                    for evidence in target_item.identity_evidence
                    if evidence.category == anchor.category
                ]
                if any(
                    _identity_key(value.value) == _identity_key(anchor.value)
                    for value in existing
                ):
                    continue
                if existing:
                    conflicts.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "doc_id": doc_id,
                            "category": anchor.category,
                            "reason": "existing_identity_conflict",
                        }
                    )
                    continue
                suggestions.append(
                    IdentityPropagationSuggestion(
                        cluster.cluster_id,
                        doc_id,
                        anchor.category,
                        anchor.value,
                        anchor.source,
                        anchor.confidence,
                    )
                )
    return IdentityPropagationPlan(
        suggestions=tuple(
            sorted(
                suggestions,
                key=lambda value: (
                    value.cluster_id,
                    value.doc_id,
                    value.category,
                    _identity_key(value.value),
                ),
            )
        ),
        skipped_conflicts=tuple(
            sorted(
                conflicts,
                key=lambda value: (
                    value["cluster_id"],
                    value["doc_id"],
                    value["category"],
                ),
            )
        ),
    )


def load_cluster_snapshot(payload: Mapping[str, object]) -> ClusterSnapshot:
    """Read current snapshots and the compact cluster-only legacy v1 shape."""

    try:
        schema_version = int(str(payload.get("schema_version", 1)))
    except (TypeError, ValueError) as exc:
        raise ImageClusteringError("Invalid cluster snapshot schema_version.") from exc
    if schema_version not in {1, CLUSTER_SNAPSHOT_SCHEMA_VERSION}:
        raise ImageClusteringError(
            f"Unsupported cluster snapshot schema_version: {schema_version}"
        )
    algorithm_version = str(payload.get("algorithm_version") or "legacy-v1")
    embedding_version = str(payload.get("embedding_version") or "unknown")
    raw_clusters = payload.get("clusters", [])
    clusters: list[ImageCluster] = []
    if isinstance(raw_clusters, Mapping):
        cluster_values: Sequence[object] = [
            {"cluster_id": key, "member_doc_ids": value}
            for key, value in raw_clusters.items()
        ]
    elif isinstance(raw_clusters, Sequence) and not isinstance(
        raw_clusters, (str, bytes)
    ):
        cluster_values = raw_clusters
    else:
        raise ImageClusteringError(
            "Cluster snapshot clusters must be a list or object."
        )
    for raw in cluster_values:
        if not isinstance(raw, Mapping):
            raise ImageClusteringError("Cluster snapshot contains an invalid cluster.")
        cluster_id = str(raw.get("cluster_id") or raw.get("id") or "").strip()
        members_value = raw.get("member_doc_ids", raw.get("members", []))
        members = _string_tuple(members_value, "cluster members")
        if not cluster_id or not members:
            raise ImageClusteringError("Cluster id and members must not be empty.")
        edge_kinds = tuple(
            cast(EdgeKind, value)
            for value in _string_tuple(raw.get("edge_kinds", []), "edge kinds")
            if value in {"exact", "perceptual", "semantic", "legacy"}
        )
        anchors = tuple(
            _coerce_anchor(value)
            for value in _mapping_sequence(raw.get("identity_anchors", []), "anchors")
        )
        clusters.append(
            ImageCluster(
                cluster_id,
                tuple(sorted(set(members))),
                edge_kinds
                or (("legacy",) if schema_version == 1 and len(members) > 1 else ()),
                anchors,
            )
        )

    raw_items = payload.get("items", [])
    item_values: list[Mapping[str, object]] = []
    if isinstance(raw_items, Mapping):
        for doc_id, value in raw_items.items():
            if isinstance(value, Mapping):
                item_values.append({"doc_id": doc_id, **value})
            else:
                item_values.append({"doc_id": doc_id, "fingerprint": value})
    elif isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        item_values = _mapping_sequence(raw_items, "items")
    else:
        raise ImageClusteringError("Cluster snapshot items must be a list or object.")
    cluster_by_doc = {
        doc_id: cluster.cluster_id
        for cluster in clusters
        for doc_id in cluster.member_doc_ids
    }
    parsed_items: dict[str, ClusterItemState] = {}
    for raw in item_values:
        doc_id = str(raw.get("doc_id") or "").strip()
        cluster_id = str(
            raw.get("cluster_id") or cluster_by_doc.get(doc_id) or ""
        ).strip()
        fingerprint = str(raw.get("fingerprint") or raw.get("sha256") or "").strip()
        if doc_id and cluster_id:
            parsed_items[doc_id] = ClusterItemState(doc_id, fingerprint, cluster_id)
    for doc_id, cluster_id in cluster_by_doc.items():
        parsed_items.setdefault(doc_id, ClusterItemState(doc_id, "", cluster_id))

    raw_edges = payload.get("edges", [])
    edges: list[ClusterEdge] = []
    if isinstance(raw_edges, Sequence) and not isinstance(raw_edges, (str, bytes)):
        for raw in raw_edges:
            if not isinstance(raw, Mapping):
                raise ImageClusteringError("Cluster snapshot contains an invalid edge.")
            edges.append(
                ClusterEdge(
                    str(raw.get("left_doc_id") or raw.get("left") or ""),
                    str(raw.get("right_doc_id") or raw.get("right") or ""),
                    str(raw.get("kind") or "legacy"),  # type: ignore[arg-type]
                    _optional_float(raw.get("score")),
                )
            )
    elif raw_edges not in (None, []):
        raise ImageClusteringError("Cluster snapshot edges must be a list.")
    if schema_version == 1 and not edges:
        for cluster in clusters:
            if len(cluster.member_doc_ids) > 1:
                first = cluster.member_doc_ids[0]
                edges.extend(
                    ClusterEdge(first, member, "legacy")
                    for member in cluster.member_doc_ids[1:]
                )
    return ClusterSnapshot(
        algorithm_version=algorithm_version,
        embedding_version=embedding_version,
        items=tuple(sorted(parsed_items.values(), key=lambda item: item.doc_id)),
        edges=tuple(
            sorted(
                edges, key=lambda edge: (edge.left_doc_id, edge.right_doc_id, edge.kind)
            )
        ),
        clusters=tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id)),
    )


def read_cluster_snapshot(path: Path) -> ClusterSnapshot:
    """Read a bounded UTF-8 snapshot without modifying library state."""

    resolved = path.expanduser().resolve()
    try:
        if resolved.stat().st_size > 64 * 1024 * 1024:
            raise ImageClusteringError("Cluster snapshot exceeds the 64 MiB limit.")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except ImageClusteringError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageClusteringError(
            f"Unable to read cluster snapshot {resolved}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ImageClusteringError("Cluster snapshot root must be a JSON object.")
    return load_cluster_snapshot(payload)


def write_cluster_snapshot(path: Path, snapshot: ClusterSnapshot) -> Path:
    """Atomically persist one snapshot; an interrupted write keeps the old file."""

    resolved = path.expanduser().resolve()
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, resolved)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ImageClusteringError(
            f"Unable to write cluster snapshot {resolved}: {exc}"
        ) from exc
    return resolved


def _coerce_item(
    raw: ImageClusterInput | Mapping[str, object],
    config: ImageClusteringConfig | None,
    *,
    allow_missing_embedding: bool = False,
) -> tuple[ImageClusterInput, list[ClusterFailure]]:
    if isinstance(raw, ImageClusterInput):
        raw = {
            "doc_id": raw.doc_id,
            "sha256": raw.sha256,
            "embedding": raw.embedding,
            "perceptual_hash": raw.perceptual_hash,
            "identity_evidence": [
                evidence.to_dict() for evidence in raw.identity_evidence
            ],
        }
    if isinstance(raw, Mapping):
        doc_id = str(raw.get("doc_id") or "").strip()
        sha256 = str(raw.get("sha256") or "").strip().casefold()
        if not doc_id:
            raise ValueError("doc_id must not be empty.")
        if len(sha256) != 64 or any(character not in _HEX for character in sha256):
            raise ValueError("sha256 must be a 64-character hexadecimal digest.")
        failures = []
        embedding: tuple[float, ...] | None
        raw_embedding = raw.get("embedding")
        if raw_embedding is None:
            embedding = None
            if not allow_missing_embedding:
                failures.append(
                    ClusterFailure(
                        doc_id,
                        "missing_embedding",
                        "Indexed embedding is unavailable.",
                    )
                )
        else:
            try:
                if not isinstance(raw_embedding, Iterable) or isinstance(
                    raw_embedding, (str, bytes)
                ):
                    raise TypeError
                embedding = tuple(float(str(value)) for value in raw_embedding)
                if not embedding or any(
                    not math.isfinite(value) for value in embedding
                ):
                    raise ValueError
                if (
                    config
                    and config.embedding_dimension is not None
                    and len(embedding) != config.embedding_dimension
                ):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                embedding = None
                failures.append(
                    ClusterFailure(
                        doc_id,
                        "invalid_embedding",
                        "Indexed embedding is invalid or has the wrong dimension.",
                    )
                )
        perceptual_hash = _normalize_perceptual_hash(raw.get("perceptual_hash"))
        evidence_values: list[IdentityEvidence] = []
        for evidence_raw in _mapping_sequence(
            raw.get("identity_evidence", []), "identity evidence"
        ):
            category = (
                str(
                    evidence_raw.get("category")
                    or evidence_raw.get("entity_type")
                    or ""
                )
                .strip()
                .casefold()
            )
            if category in BLOCKED_PROPAGATION_CATEGORIES:
                # Full annotation payloads may contain action/expression/etc.
                # They are valid metadata, but deliberately outside this
                # identity-only propagation boundary.
                continue
            try:
                evidence_values.append(_coerce_identity_evidence(evidence_raw))
            except (TypeError, ValueError) as exc:
                failures.append(
                    ClusterFailure(
                        doc_id,
                        "invalid_identity_evidence",
                        str(exc) or exc.__class__.__name__,
                    )
                )
        evidence = tuple(evidence_values)
        item = ImageClusterInput(doc_id, sha256, embedding, perceptual_hash, evidence)
    else:
        raise TypeError("Cluster input must be an ImageClusterInput or mapping.")
    if not item.doc_id.strip():
        raise ValueError("doc_id must not be empty.")
    if len(item.sha256) != 64 or any(
        character not in _HEX for character in item.sha256.casefold()
    ):
        raise ValueError("sha256 must be a 64-character hexadecimal digest.")
    return item, failures


def _remove_duplicate_doc_ids(
    items: Sequence[ImageClusterInput],
) -> tuple[list[ImageClusterInput], list[ClusterFailure]]:
    counts = Counter(item.doc_id for item in items)
    duplicates = {doc_id for doc_id, count in counts.items() if count > 1}
    failures = [
        ClusterFailure(
            doc_id, "duplicate_doc_id", "Duplicate document id was excluded."
        )
        for doc_id in sorted(duplicates)
    ]
    return [item for item in items if item.doc_id not in duplicates], failures


def _add_exact_edges(
    items: Sequence[ImageClusterInput],
    edges: dict[tuple[str, str, str], ClusterEdge],
    dsu: _DisjointSet,
    cancel_check: Callable[[], object] | None,
) -> None:
    by_sha: dict[str, list[str]] = defaultdict(list)
    for item in items:
        _check_cancel(cancel_check)
        by_sha[item.sha256].append(item.doc_id)
    for members in by_sha.values():
        if len(members) < 2:
            continue
        first = min(members)
        for member in sorted(members):
            if member != first:
                _store_edge(edges, dsu, ClusterEdge(first, member, "exact", 1.0))


def _add_perceptual_edges(
    items: Sequence[ImageClusterInput],
    radius: int,
    edges: dict[tuple[str, str, str], ClusterEdge],
    dsu: _DisjointSet,
    cancel_check: Callable[[], object] | None,
) -> None:
    tree = _HammingBKTree()
    for item in items:
        _check_cancel(cancel_check)
        if item.perceptual_hash is None:
            continue
        value = int(item.perceptual_hash, 16)
        bits = len(item.perceptual_hash) * 4
        for other, distance in tree.query(value, bits, radius):
            score = 1.0 - (distance / bits)
            _store_edge(
                edges,
                dsu,
                ClusterEdge(item.doc_id, other, "perceptual", score),
            )
        tree.add(value, bits, item.doc_id)


def _default_neighbor_provider(
    items: Sequence[ImageClusterInput],
) -> SemanticNeighborProvider:
    candidates = tuple(item for item in items if item.embedding is not None)

    def provide(item: ImageClusterInput, top_k: int) -> Iterable[SemanticNeighbor]:
        if item.embedding is None:
            return ()
        scored: list[SemanticNeighbor] = []
        for candidate in candidates:
            if candidate.doc_id == item.doc_id or candidate.embedding is None:
                continue
            similarity = _cosine_similarity(item.embedding, candidate.embedding)
            if similarity is not None:
                scored.append(SemanticNeighbor(candidate.doc_id, similarity))
        scored.sort(key=lambda value: (-value.similarity, value.doc_id))
        return tuple(scored[:top_k])

    return provide


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _store_edge(
    edges: dict[tuple[str, str, str], ClusterEdge],
    dsu: _DisjointSet,
    edge: ClusterEdge,
) -> None:
    key = (edge.left_doc_id, edge.right_doc_id, edge.kind)
    previous = edges.get(key)
    if previous is None or (
        edge.score is not None
        and (previous.score is None or edge.score > previous.score)
    ):
        edges[key] = edge
    dsu.union(edge.left_doc_id, edge.right_doc_id)


def _assign_cluster_ids(
    components: Sequence[tuple[str, ...]],
    previous: ClusterSnapshot | None,
    algorithm_version: str,
    embedding_version: str,
    items: Mapping[str, ImageClusterInput],
) -> dict[tuple[str, ...], str]:
    previous_by_doc = (
        {item.doc_id: item.cluster_id for item in previous.items} if previous else {}
    )
    candidates: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    for members in components:
        counts = Counter(
            previous_by_doc[doc_id] for doc_id in members if doc_id in previous_by_doc
        )
        for cluster_id, overlap in counts.items():
            candidates.append(
                (-overlap, -len(members), members[0], cluster_id, members)
            )
    assigned: dict[tuple[str, ...], str] = {}
    used_ids: set[str] = set()
    for _overlap, _size, _first, cluster_id, members in sorted(candidates):
        if members in assigned or cluster_id in used_ids:
            continue
        assigned[members] = cluster_id
        used_ids.add(cluster_id)
    for members in components:
        if members in assigned:
            continue
        digest = hashlib.sha256(
            json.dumps(
                {
                    "algorithm_version": algorithm_version,
                    "embedding_version": embedding_version,
                    "members": [
                        {"doc_id": doc_id, "sha256": items[doc_id].sha256}
                        for doc_id in members
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        cluster_id = f"clu_{digest}"
        suffix = 1
        while cluster_id in used_ids:
            suffix += 1
            cluster_id = f"clu_{digest}_{suffix}"
        assigned[members] = cluster_id
        used_ids.add(cluster_id)
    return assigned


def _identity_anchors(items: Sequence[ImageClusterInput]) -> tuple[IdentityAnchor, ...]:
    by_category: dict[str, list[IdentityEvidence]] = defaultdict(list)
    for item in items:
        for evidence in item.identity_evidence:
            by_category[evidence.category].append(evidence)
    anchors: list[IdentityAnchor] = []
    for category in sorted(by_category):
        values: dict[str, list[IdentityEvidence]] = defaultdict(list)
        display: dict[str, str] = {}
        for evidence in by_category[category]:
            key = _identity_key(evidence.value)
            values[key].append(evidence)
            display.setdefault(key, evidence.value)
        ranked: list[tuple[int, int, float, str, str, list[IdentityEvidence]]] = []
        for key, evidence_values in values.items():
            best_priority = min(
                _SOURCE_PRIORITY[value.source] for value in evidence_values
            )
            support = len(evidence_values)
            confidence = max(value.confidence for value in evidence_values)
            ranked.append(
                (
                    best_priority,
                    -support,
                    -confidence,
                    key,
                    display[key],
                    evidence_values,
                )
            )
        best = min(ranked)
        evidence_values = best[-1]
        best_source = min(
            evidence_values,
            key=lambda value: (
                _SOURCE_PRIORITY[value.source],
                -value.confidence,
                _identity_key(value.value),
            ),
        ).source
        anchors.append(
            IdentityAnchor(
                category=category,
                value=best[4],
                source=best_source,
                confidence=max(value.confidence for value in evidence_values),
                support=len(evidence_values),
                conflict=len(values) > 1,
            )
        )
    return tuple(anchors)


def _coerce_identity_evidence(raw: Mapping[str, object]) -> IdentityEvidence:
    return IdentityEvidence(
        category=str(raw.get("category") or raw.get("entity_type") or ""),
        value=str(raw.get("value") or raw.get("name") or raw.get("tag") or ""),
        source=str(raw.get("source") or "model"),
        confidence=float(str(raw.get("confidence", 1.0))),
    )


def _coerce_anchor(raw: Mapping[str, object]) -> IdentityAnchor:
    return IdentityAnchor(
        category=str(raw.get("category") or ""),
        value=str(raw.get("value") or ""),
        source=str(raw.get("source") or "model"),
        confidence=float(str(raw.get("confidence", 0.0))),
        support=int(str(raw.get("support", 1))),
        conflict=bool(raw.get("conflict", False)),
    )


def _coerce_neighbor(raw: SemanticNeighborValue) -> SemanticNeighbor:
    if isinstance(raw, SemanticNeighbor):
        neighbor = raw
    elif isinstance(raw, Mapping):
        neighbor = SemanticNeighbor(
            str(raw.get("doc_id") or "").strip(),
            float(str(raw.get("similarity", 0.0))),
        )
    else:
        neighbor = SemanticNeighbor(str(raw[0]).strip(), float(raw[1]))
    if not neighbor.doc_id:
        raise ValueError("Semantic neighbor doc_id must not be empty.")
    if not math.isfinite(neighbor.similarity) or not -1.0 <= neighbor.similarity <= 1.0:
        raise ValueError("Semantic neighbor similarity must be between -1 and 1.")
    return neighbor


def _normalize_perceptual_hash(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().casefold()
    if (
        len(normalized) < 4
        or len(normalized) > 64
        or any(character not in _HEX for character in normalized)
    ):
        raise ValueError("perceptual_hash must be 4 to 64 hexadecimal characters.")
    return normalized


def _failure_doc_id(raw: object, index: int) -> str:
    if isinstance(raw, Mapping):
        value = str(raw.get("doc_id") or "").strip()
        if value:
            return value
    return f"input-{index + 1}"


def _check_cancel(cancel_check: Callable[[], object] | None) -> None:
    if cancel_check is None:
        return
    result = cancel_check()
    if result:
        raise ClusteringCancelled("Image clustering was cancelled.")


def _identity_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _mapping_sequence(value: object, label: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a list.")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} must contain objects.")
        result.append(item)
    return result


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ImageClusteringError(f"{label} must be a list.")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    number = float(str(value))
    if not math.isfinite(number):
        raise ImageClusteringError("Cluster edge score must be finite.")
    return number
