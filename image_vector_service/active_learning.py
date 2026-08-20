"""Deterministic active-learning review selection for image search metadata.

Selection is local and reproducible.  It combines already-computed conflict,
cluster-outlier, and ranking-disagreement signals; it never calls a model or
modifies a library.  Decisions are immutable queue metadata that a later
integration can translate into manual-tag or relevance-feedback operations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, cast

ACTIVE_LEARNING_QUEUE_SCHEMA_VERSION: Final = 1
UncertaintyReason = Literal[
    "identity_conflict",
    "cluster_outlier",
    "ranking_disagreement",
    "combined_uncertainty",
    "low_information",
]
LearningDecisionValue = Literal["accept", "reject", "edit", "skip"]
LearningCandidateKind = Literal[
    "tag_review",
    "search_result",
    "cluster_membership",
]
_CANDIDATE_KINDS: Final = {
    "tag_review",
    "search_result",
    "cluster_membership",
}


class ActiveLearningError(ValueError):
    """Raised when active-learning inputs or decisions are invalid."""


@dataclass(frozen=True, slots=True)
class ActiveLearningConfig:
    total_budget: int = 25
    target_per_group: int = 2
    max_per_group: int = 3
    max_per_query: int = 5
    conflict_weight: float = 0.50
    outlier_weight: float = 0.30
    ranking_disagreement_weight: float = 0.20
    reason_threshold: float = 0.25
    selection_version: str = "active-learning-v1"

    def __post_init__(self) -> None:
        if isinstance(self.total_budget, bool) or not 20 <= self.total_budget <= 30:
            raise ValueError("total_budget must be between 20 and 30.")
        if (
            isinstance(self.target_per_group, bool)
            or not 1 <= self.target_per_group <= 3
        ):
            raise ValueError("target_per_group must be between 1 and 3.")
        if isinstance(self.max_per_group, bool) or not 2 <= self.max_per_group <= 3:
            raise ValueError("max_per_group must be 2 or 3.")
        if self.target_per_group > self.max_per_group:
            raise ValueError("target_per_group cannot exceed max_per_group.")
        if isinstance(self.max_per_query, bool) or not 1 <= self.max_per_query <= 5:
            raise ValueError("max_per_query must be between 1 and 5.")
        weights = (
            float(self.conflict_weight),
            float(self.outlier_weight),
            float(self.ranking_disagreement_weight),
        )
        if any(not math.isfinite(value) or value < 0.0 for value in weights):
            raise ValueError("Active-learning weights must be finite and non-negative.")
        if sum(weights) <= 0.0:
            raise ValueError("At least one active-learning weight must be positive.")
        threshold = float(self.reason_threshold)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("reason_threshold must be between 0 and 1.")
        if not self.selection_version.strip():
            raise ValueError("selection_version must not be empty.")
        object.__setattr__(self, "conflict_weight", weights[0])
        object.__setattr__(self, "outlier_weight", weights[1])
        object.__setattr__(self, "ranking_disagreement_weight", weights[2])
        object.__setattr__(self, "reason_threshold", threshold)
        object.__setattr__(self, "selection_version", self.selection_version.strip())


@dataclass(frozen=True, slots=True)
class ActiveLearningCandidate:
    doc_id: str
    group_id: str
    candidate_kind: LearningCandidateKind = "tag_review"
    library_id: str = ""
    query_id: str = ""
    source_sha256: str = ""
    tag_snapshot: tuple[str, ...] = ()
    conflict_score: float = 0.0
    outlier_score: float = 0.0
    ranking_disagreement: float = 0.0
    relative_path: str = ""


@dataclass(frozen=True, slots=True)
class ActiveLearningFailure:
    doc_id: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ActiveLearningQueueItem:
    rank: int
    doc_id: str
    group_id: str
    candidate_kind: LearningCandidateKind
    library_id: str
    query_id: str
    source_sha256: str
    tag_snapshot: tuple[str, ...]
    relative_path: str
    uncertainty_score: float
    reasons: tuple[UncertaintyReason, ...]
    conflict_score: float
    outlier_score: float
    ranking_disagreement: float

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "doc_id": self.doc_id,
            "group_id": self.group_id,
            "candidate_kind": self.candidate_kind,
            "library_id": self.library_id,
            "query_id": self.query_id,
            "source_sha256": self.source_sha256,
            "tag_snapshot": list(self.tag_snapshot),
            "relative_path": self.relative_path,
            "uncertainty_score": self.uncertainty_score,
            "reasons": list(self.reasons),
            "conflict_score": self.conflict_score,
            "outlier_score": self.outlier_score,
            "ranking_disagreement": self.ranking_disagreement,
        }


@dataclass(frozen=True, slots=True)
class LearningDecision:
    doc_id: str
    decision: LearningDecisionValue
    labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "decision": self.decision,
            "labels": list(self.labels),
        }


@dataclass(frozen=True, slots=True)
class ActiveLearningQueue:
    queue_id: str
    selection_version: str
    items: tuple[ActiveLearningQueueItem, ...]
    failures: tuple[ActiveLearningFailure, ...] = ()
    decisions: tuple[LearningDecision, ...] = ()
    candidate_count: int = 0
    schema_version: int = ACTIVE_LEARNING_QUEUE_SCHEMA_VERSION
    api_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "selection_version": self.selection_version,
            "candidate_count": self.candidate_count,
            "selected_count": len(self.items),
            "items": [item.to_dict() for item in self.items],
            "failures": [failure.to_dict() for failure in self.failures],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "api_requests": self.api_requests,
        }


@dataclass(frozen=True, slots=True)
class _AssessedCandidate:
    candidate: ActiveLearningCandidate
    uncertainty_score: float
    reasons: tuple[UncertaintyReason, ...]


def build_active_learning_queue(
    candidates: Iterable[ActiveLearningCandidate | Mapping[str, object]],
    config: ActiveLearningConfig | None = None,
) -> ActiveLearningQueue:
    """Select a bounded, order-independent review queue.

    The first pass aims for two representatives per high-uncertainty group; a
    second pass may add one more.  Query and total limits are enforced while
    selecting, so a single search or cluster cannot consume the whole budget.
    """

    resolved = config or ActiveLearningConfig()
    raw_candidates = list(candidates)
    normalized: list[ActiveLearningCandidate] = []
    failures: list[ActiveLearningFailure] = []
    for index, raw in enumerate(raw_candidates):
        try:
            normalized.append(_coerce_candidate(raw))
        except (TypeError, ValueError) as exc:
            failures.append(
                ActiveLearningFailure(
                    _failure_doc_id(raw, index),
                    "invalid_active_learning_candidate",
                    str(exc) or exc.__class__.__name__,
                )
            )

    counts = Counter(candidate.doc_id for candidate in normalized)
    duplicate_ids = {doc_id for doc_id, count in counts.items() if count > 1}
    normalized = [
        candidate for candidate in normalized if candidate.doc_id not in duplicate_ids
    ]
    failures.extend(
        ActiveLearningFailure(
            doc_id,
            "duplicate_doc_id",
            "Duplicate active-learning candidate was excluded.",
        )
        for doc_id in sorted(duplicate_ids)
    )

    assessed = [_assess(candidate, resolved) for candidate in normalized]
    assessed.sort(key=_candidate_sort_key)
    groups: dict[str, list[_AssessedCandidate]] = defaultdict(list)
    for candidate in assessed:
        groups[candidate.candidate.group_id].append(candidate)
    ordered_groups = sorted(
        groups,
        key=lambda group_id: _group_sort_key(group_id, groups[group_id]),
    )

    selected: list[_AssessedCandidate] = []
    selected_ids: set[str] = set()
    query_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()

    def take_from_group(group_id: str, target: int) -> None:
        for assessed_candidate in groups[group_id]:
            if len(selected) >= resolved.total_budget:
                return
            if group_counts[group_id] >= target:
                return
            candidate = assessed_candidate.candidate
            if candidate.doc_id in selected_ids:
                continue
            query_bucket = candidate.query_id or f"__doc__:{candidate.doc_id}"
            if query_counts[query_bucket] >= resolved.max_per_query:
                continue
            selected.append(assessed_candidate)
            selected_ids.add(candidate.doc_id)
            group_counts[group_id] += 1
            query_counts[query_bucket] += 1

    for group_id in ordered_groups:
        take_from_group(group_id, resolved.target_per_group)
        if len(selected) >= resolved.total_budget:
            break
    if len(selected) < resolved.total_budget:
        for group_id in ordered_groups:
            take_from_group(group_id, resolved.max_per_group)
            if len(selected) >= resolved.total_budget:
                break

    selected.sort(key=_candidate_sort_key)
    queue_items = tuple(
        ActiveLearningQueueItem(
            rank=index,
            doc_id=value.candidate.doc_id,
            group_id=value.candidate.group_id,
            candidate_kind=value.candidate.candidate_kind,
            library_id=value.candidate.library_id,
            query_id=value.candidate.query_id,
            source_sha256=value.candidate.source_sha256,
            tag_snapshot=value.candidate.tag_snapshot,
            relative_path=value.candidate.relative_path,
            uncertainty_score=value.uncertainty_score,
            reasons=value.reasons,
            conflict_score=value.candidate.conflict_score,
            outlier_score=value.candidate.outlier_score,
            ranking_disagreement=value.candidate.ranking_disagreement,
        )
        for index, value in enumerate(selected, start=1)
    )
    queue_id = _queue_id(resolved, queue_items)
    return ActiveLearningQueue(
        queue_id=queue_id,
        selection_version=resolved.selection_version,
        items=queue_items,
        failures=tuple(
            sorted(
                failures, key=lambda value: (value.doc_id, value.code, value.message)
            )
        ),
        candidate_count=len(raw_candidates),
    )


def record_learning_decision(
    queue: ActiveLearningQueue,
    doc_id: str,
    decision: LearningDecisionValue,
    *,
    labels: Iterable[str] = (),
) -> ActiveLearningQueue:
    """Return a new queue with one validated, idempotently replaced decision."""

    normalized_doc_id = str(doc_id).strip()
    if normalized_doc_id not in {item.doc_id for item in queue.items}:
        raise ActiveLearningError(f"Unknown queue document: {normalized_doc_id}")
    if decision not in {"accept", "reject", "edit", "skip"}:
        raise ActiveLearningError(f"Unsupported learning decision: {decision}")
    normalized_labels = _normalize_labels(labels)
    if decision in {"reject", "skip"} and normalized_labels:
        raise ActiveLearningError(f"{decision} decisions cannot contain labels.")
    updated = {
        value.doc_id: value
        for value in queue.decisions
        if value.doc_id != normalized_doc_id
    }
    updated[normalized_doc_id] = LearningDecision(
        normalized_doc_id, decision, normalized_labels
    )
    return replace(
        queue,
        decisions=tuple(updated[key] for key in sorted(updated)),
    )


def active_learning_queue_from_mapping(
    payload: Mapping[str, object],
) -> ActiveLearningQueue:
    """Load a persisted queue without trusting arbitrary JSON fields."""

    try:
        schema_version = int(str(payload.get("schema_version", 0)))
    except (TypeError, ValueError) as exc:
        raise ActiveLearningError("Invalid active-learning schema_version.") from exc
    if schema_version != ACTIVE_LEARNING_QUEUE_SCHEMA_VERSION:
        raise ActiveLearningError(
            f"Unsupported active-learning schema_version: {schema_version}"
        )
    queue_id = str(payload.get("queue_id") or "").strip()
    selection_version = str(payload.get("selection_version") or "").strip()
    if not queue_id or not selection_version:
        raise ActiveLearningError("Queue id and selection version are required.")

    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        raise ActiveLearningError("Queue items must be an array.")
    items: list[ActiveLearningQueueItem] = []
    for index, raw in enumerate(raw_items, 1):
        if not isinstance(raw, Mapping):
            raise ActiveLearningError(f"Queue item {index} must be an object.")
        reasons = tuple(str(value) for value in raw.get("reasons", []))
        if any(
            reason
            not in {
                "identity_conflict",
                "cluster_outlier",
                "ranking_disagreement",
                "combined_uncertainty",
                "low_information",
            }
            for reason in reasons
        ):
            raise ActiveLearningError(f"Queue item {index} has invalid reasons.")
        items.append(
            ActiveLearningQueueItem(
                rank=int(str(raw.get("rank", index))),
                doc_id=str(raw.get("doc_id") or "").strip(),
                group_id=str(raw.get("group_id") or "").strip(),
                candidate_kind=_candidate_kind(raw.get("candidate_kind")),
                library_id=str(raw.get("library_id") or "").strip(),
                query_id=str(raw.get("query_id") or "").strip(),
                source_sha256=_source_sha256(raw.get("source_sha256")),
                tag_snapshot=_normalize_labels(
                    _string_array(raw.get("tag_snapshot", ()), "tag_snapshot")
                ),
                relative_path=str(raw.get("relative_path") or ""),
                uncertainty_score=_bounded_score(
                    raw.get("uncertainty_score"), "uncertainty_score"
                ),
                reasons=cast(tuple[UncertaintyReason, ...], reasons),
                conflict_score=_bounded_score(
                    raw.get("conflict_score"), "conflict_score"
                ),
                outlier_score=_bounded_score(raw.get("outlier_score"), "outlier_score"),
                ranking_disagreement=_bounded_score(
                    raw.get("ranking_disagreement"), "ranking_disagreement"
                ),
            )
        )
    if any(not item.doc_id or not item.group_id for item in items):
        raise ActiveLearningError("Queue items require doc_id and group_id.")

    raw_failures = payload.get("failures", [])
    if not isinstance(raw_failures, list):
        raise ActiveLearningError("Queue failures must be an array.")
    failures = tuple(
        ActiveLearningFailure(
            str(raw.get("doc_id") or ""),
            str(raw.get("code") or "invalid"),
            str(raw.get("message") or ""),
        )
        for raw in raw_failures
        if isinstance(raw, Mapping)
    )
    raw_decisions = payload.get("decisions", [])
    if not isinstance(raw_decisions, list):
        raise ActiveLearningError("Queue decisions must be an array.")
    decisions: list[LearningDecision] = []
    for raw in raw_decisions:
        if not isinstance(raw, Mapping):
            raise ActiveLearningError("Queue decisions must contain objects.")
        decision = str(raw.get("decision") or "")
        if decision not in {"accept", "reject", "edit", "skip"}:
            raise ActiveLearningError("Queue decision is unsupported.")
        labels = _normalize_labels(cast(Iterable[str], raw.get("labels", [])))
        decisions.append(
            LearningDecision(
                str(raw.get("doc_id") or "").strip(),
                cast(LearningDecisionValue, decision),
                labels,
            )
        )
    return ActiveLearningQueue(
        queue_id=queue_id,
        selection_version=selection_version,
        items=tuple(items),
        failures=failures,
        decisions=tuple(decisions),
        candidate_count=int(str(payload.get("candidate_count", len(items)))),
        schema_version=schema_version,
        api_requests=int(str(payload.get("api_requests", 0))),
    )


def read_active_learning_queue(path: Path) -> ActiveLearningQueue:
    resolved = path.expanduser().resolve()
    try:
        if resolved.stat().st_size > 16 * 1024 * 1024:
            raise ActiveLearningError("Active-learning queue exceeds 16 MiB.")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except ActiveLearningError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActiveLearningError(
            f"Unable to read active-learning queue: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ActiveLearningError("Active-learning queue root must be an object.")
    return active_learning_queue_from_mapping(payload)


def write_active_learning_queue(path: Path, queue: ActiveLearningQueue) -> Path:
    resolved = path.expanduser().resolve()
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(queue.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, resolved)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ActiveLearningError(
            f"Unable to write active-learning queue: {exc}"
        ) from exc
    return resolved


def _coerce_candidate(
    raw: ActiveLearningCandidate | Mapping[str, object],
) -> ActiveLearningCandidate:
    if isinstance(raw, ActiveLearningCandidate):
        candidate = raw
    elif isinstance(raw, Mapping):
        candidate = ActiveLearningCandidate(
            doc_id=str(raw.get("doc_id") or ""),
            group_id=str(raw.get("group_id") or raw.get("cluster_id") or ""),
            candidate_kind=_candidate_kind(raw.get("candidate_kind")),
            library_id=str(raw.get("library_id") or ""),
            query_id=str(raw.get("query_id") or ""),
            source_sha256=_source_sha256(raw.get("source_sha256")),
            tag_snapshot=_normalize_labels(
                _string_array(raw.get("tag_snapshot", ()), "tag_snapshot")
            ),
            conflict_score=float(str(raw.get("conflict_score", 0.0))),
            outlier_score=float(str(raw.get("outlier_score", 0.0))),
            ranking_disagreement=float(str(raw.get("ranking_disagreement", 0.0))),
            relative_path=str(raw.get("relative_path") or ""),
        )
    else:
        raise TypeError("Active-learning candidate must be a dataclass or mapping.")
    doc_id = candidate.doc_id.strip()
    group_id = candidate.group_id.strip()
    query_id = candidate.query_id.strip()
    candidate_kind = _candidate_kind(candidate.candidate_kind)
    library_id = candidate.library_id.strip()
    source_sha256 = _source_sha256(candidate.source_sha256)
    tag_snapshot = _normalize_labels(candidate.tag_snapshot)
    if not doc_id:
        raise ValueError("doc_id must not be empty.")
    if not group_id:
        raise ValueError("group_id must not be empty.")
    scores = (
        float(candidate.conflict_score),
        float(candidate.outlier_score),
        float(candidate.ranking_disagreement),
    )
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in scores):
        raise ValueError("Uncertainty signals must be finite values between 0 and 1.")
    return ActiveLearningCandidate(
        doc_id=doc_id,
        group_id=group_id,
        candidate_kind=candidate_kind,
        library_id=library_id,
        query_id=query_id,
        source_sha256=source_sha256,
        tag_snapshot=tag_snapshot,
        conflict_score=scores[0],
        outlier_score=scores[1],
        ranking_disagreement=scores[2],
        relative_path=candidate.relative_path.strip(),
    )


def _bounded_score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActiveLearningError(f"{name} must be a number between 0 and 1.")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ActiveLearningError(f"{name} must be a number between 0 and 1.")
    return result


def _assess(
    candidate: ActiveLearningCandidate,
    config: ActiveLearningConfig,
) -> _AssessedCandidate:
    weight_total = (
        config.conflict_weight
        + config.outlier_weight
        + config.ranking_disagreement_weight
    )
    score = (
        candidate.conflict_score * config.conflict_weight
        + candidate.outlier_score * config.outlier_weight
        + candidate.ranking_disagreement * config.ranking_disagreement_weight
    ) / weight_total
    score = round(max(0.0, min(1.0, score)), 12)
    reasons: list[UncertaintyReason] = []
    if candidate.conflict_score >= config.reason_threshold:
        reasons.append("identity_conflict")
    if candidate.outlier_score >= config.reason_threshold:
        reasons.append("cluster_outlier")
    if candidate.ranking_disagreement >= config.reason_threshold:
        reasons.append("ranking_disagreement")
    if not reasons:
        reasons.append("combined_uncertainty" if score > 0.0 else "low_information")
    return _AssessedCandidate(candidate, score, tuple(reasons))


def _candidate_sort_key(value: _AssessedCandidate) -> tuple[object, ...]:
    candidate = value.candidate
    return (
        -value.uncertainty_score,
        -candidate.conflict_score,
        -candidate.outlier_score,
        -candidate.ranking_disagreement,
        candidate.group_id,
        candidate.query_id,
        candidate.doc_id,
    )


def _group_sort_key(
    group_id: str, values: Sequence[_AssessedCandidate]
) -> tuple[object, ...]:
    top = sorted((value.uncertainty_score for value in values), reverse=True)[:2]
    average = sum(top) / len(top)
    return (-average, -max(top), group_id)


def _queue_id(
    config: ActiveLearningConfig, items: Sequence[ActiveLearningQueueItem]
) -> str:
    payload = {
        "selection_version": config.selection_version,
        "limits": {
            "total": config.total_budget,
            "target_per_group": config.target_per_group,
            "max_per_group": config.max_per_group,
            "max_per_query": config.max_per_query,
        },
        "items": [
            {
                "doc_id": item.doc_id,
                "candidate_kind": item.candidate_kind,
                "source_sha256": item.source_sha256,
                "score": item.uncertainty_score,
                "reasons": item.reasons,
            }
            for item in items
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"alq_{digest}"


def _normalize_labels(labels: Iterable[str]) -> tuple[str, ...]:
    values: dict[str, str] = {}
    for raw in labels:
        value = str(raw).strip()
        if not value:
            continue
        if any(ord(character) < 32 for character in value):
            raise ActiveLearningError(
                "Decision labels cannot contain control characters."
            )
        values.setdefault(value.casefold(), value)
    return tuple(values[key] for key in sorted(values))


def _candidate_kind(value: object) -> LearningCandidateKind:
    normalized = str(value or "tag_review").strip().lower()
    if normalized not in _CANDIDATE_KINDS:
        raise ActiveLearningError(f"Unsupported candidate_kind: {normalized}")
    return cast(LearningCandidateKind, normalized)


def _source_sha256(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized and (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ActiveLearningError("source_sha256 must be a 64-character hex digest.")
    return normalized


def _string_array(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ActiveLearningError(f"{name} must be an array of strings.")
    return tuple(str(item) for item in value)


def _failure_doc_id(raw: object, index: int) -> str:
    if isinstance(raw, Mapping):
        value = str(raw.get("doc_id") or "").strip()
        if value:
            return value
    return f"input-{index + 1}"
