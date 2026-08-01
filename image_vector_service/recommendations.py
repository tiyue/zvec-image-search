"""Pure, local recommendation selection with vector diversity.

This module deliberately has no database, HTTP, or embedding-model dependency.
Callers supply already indexed candidates and may persist the returned selections
through ``recommendation_store``.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

RecommendationSlot = Literal["quality", "recent", "low_exposure", "random"]

SLOT_QUOTAS: Mapping[RecommendationSlot, int] = MappingProxyType(
    {"quality": 5, "recent": 4, "low_exposure": 4, "random": 2}
)
_HISTORY_WINDOWS = (60, 45, 30, 15, 0)
_MAX_ALBUM_ITEMS = 3
_MAX_CHARACTER_ITEMS = 5
_MIN_TECHNICAL_PIXELS = 2_000_000
_MIN_ASPECT_RATIO = 0.25
_MAX_ASPECT_RATIO = 4.0


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    """One path-free indexed candidate supplied by a library worker."""

    candidate_id: str
    doc_id: str
    sha256: str
    width: int
    height: int
    mtime_ns: int
    exposure_count: int = 0
    album_id: str | None = None
    character: str | None = None
    vector: tuple[float, ...] | None = None

    @property
    def is_technically_usable(self) -> bool:
        if self.width <= 0 or self.height <= 0:
            return False
        if self.width * self.height < _MIN_TECHNICAL_PIXELS:
            return False
        ratio = self.width / self.height
        return _MIN_ASPECT_RATIO <= ratio <= _MAX_ASPECT_RATIO


@dataclass(frozen=True, slots=True)
class RecommendationItem:
    candidate: RecommendationCandidate
    slot: RecommendationSlot
    score: float


@dataclass(frozen=True, slots=True)
class RecommendationSelection:
    items: tuple[RecommendationItem, ...]
    status: Literal["complete", "partial"]
    history_window: int
    counts_by_slot: Mapping[str, int]


def select_recommendations(
    candidates: Iterable[RecommendationCandidate],
    *,
    recent_sha256: Iterable[str] = (),
    rng_seed: int = 0,
) -> RecommendationSelection:
    """Select at most fifteen unique images using fixed pool quotas.

    The caller supplies recent exposures newest first.  We preserve the newest
    exclusion window that can still yield all fifteen items, then relax it in
    the product-defined 60/45/30/15/0 sequence.  Vector similarity only ever
    adjusts an already local ranking; missing vectors never trigger a model
    call and remain eligible.
    """

    normalized = _normalized_candidates(candidates)
    history = _normalized_history(recent_sha256)
    best: RecommendationSelection | None = None
    windows = _HISTORY_WINDOWS
    for window in windows:
        excluded = frozenset(history[:window])
        result = _select_once(normalized, excluded, window, rng_seed)
        if result.status == "complete":
            return result
        if best is None or len(result.items) >= len(best.items):
            best = result
    assert best is not None
    return best


def _normalized_candidates(
    candidates: Iterable[RecommendationCandidate],
) -> tuple[RecommendationCandidate, ...]:
    unique: dict[str, RecommendationCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, RecommendationCandidate):
            raise TypeError("candidates must contain RecommendationCandidate values")
        candidate_id = candidate.candidate_id.strip()
        doc_id = candidate.doc_id.strip()
        sha256 = candidate.sha256.strip()
        if not candidate_id or not doc_id or not sha256:
            continue
        if candidate_id not in unique:
            unique[candidate_id] = candidate
    return tuple(unique.values())


def _normalized_history(recent_doc_ids: Iterable[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for value in recent_doc_ids:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
        if len(values) == 60:
            break
    return tuple(values)


def _select_once(
    candidates: tuple[RecommendationCandidate, ...],
    excluded_sha256: frozenset[str],
    history_window: int,
    rng_seed: int,
) -> RecommendationSelection:
    selected: list[RecommendationItem] = []
    selected_doc_ids: set[str] = set()
    selected_sha256: set[str] = set()
    album_counts: Counter[str] = Counter()
    character_counts: Counter[str] = Counter()

    for slot, quota in SLOT_QUOTAS.items():
        for _index in range(quota):
            choice = _best_candidate(
                candidates,
                slot=slot,
                excluded_sha256=excluded_sha256,
                selected=selected,
                selected_doc_ids=selected_doc_ids,
                selected_sha256=selected_sha256,
                album_counts=album_counts,
                character_counts=character_counts,
                rng_seed=rng_seed,
            )
            if choice is None:
                break
            candidate, score = choice
            selected.append(RecommendationItem(candidate, slot, score))
            selected_doc_ids.add(candidate.candidate_id)
            selected_sha256.add(candidate.sha256)
            if group := _group_key(candidate.album_id):
                album_counts[group] += 1
            if character := _group_key(candidate.character):
                character_counts[character] += 1

    while len(selected) < sum(SLOT_QUOTAS.values()):
        choice = _best_candidate(
            candidates,
            slot="random",
            excluded_sha256=excluded_sha256,
            selected=selected,
            selected_doc_ids=selected_doc_ids,
            selected_sha256=selected_sha256,
            album_counts=album_counts,
            character_counts=character_counts,
            rng_seed=rng_seed,
        )
        if choice is None:
            break
        candidate, score = choice
        selected.append(RecommendationItem(candidate, "random", score))
        selected_doc_ids.add(candidate.candidate_id)
        selected_sha256.add(candidate.sha256)
        if group := _group_key(candidate.album_id):
            album_counts[group] += 1
        if character := _group_key(candidate.character):
            character_counts[character] += 1

    counts = Counter(item.slot for item in selected)
    materialized_counts: Mapping[str, int] = MappingProxyType(
        {str(slot): counts.get(slot, 0) for slot in SLOT_QUOTAS}
    )
    return RecommendationSelection(
        items=tuple(selected),
        status="complete" if len(selected) == sum(SLOT_QUOTAS.values()) else "partial",
        history_window=history_window,
        counts_by_slot=materialized_counts,
    )


def _best_candidate(
    candidates: tuple[RecommendationCandidate, ...],
    *,
    slot: RecommendationSlot,
    excluded_sha256: frozenset[str],
    selected: list[RecommendationItem],
    selected_doc_ids: set[str],
    selected_sha256: set[str],
    album_counts: Counter[str],
    character_counts: Counter[str],
    rng_seed: int,
) -> tuple[RecommendationCandidate, float] | None:
    ranked = _ranked_for_slot(candidates, slot, rng_seed)
    best: tuple[RecommendationCandidate, float, int] | None = None
    total = max(1, len(ranked))
    for rank, candidate in enumerate(ranked):
        if candidate.sha256 in excluded_sha256:
            continue
        if (
            candidate.candidate_id in selected_doc_ids
            or candidate.sha256 in selected_sha256
        ):
            continue
        album = _group_key(candidate.album_id)
        if album is not None and album_counts[album] >= _MAX_ALBUM_ITEMS:
            continue
        character = _group_key(candidate.character)
        if (
            character is not None
            and character_counts[character] >= _MAX_CHARACTER_ITEMS
        ):
            continue
        score = _base_score(candidate, slot, rank, total)
        score -= _maximum_diversity_penalty(candidate, selected)
        # Stable rank resolution avoids process-random order when scores tie.
        if best is None or score > best[1] or (score == best[1] and rank < best[2]):
            best = (candidate, score, rank)
    return None if best is None else (best[0], best[1])


def _ranked_for_slot(
    candidates: tuple[RecommendationCandidate, ...],
    slot: RecommendationSlot,
    rng_seed: int,
) -> tuple[RecommendationCandidate, ...]:
    if slot == "quality":
        return tuple(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.is_technically_usable
                ),
                key=lambda candidate: (
                    candidate.vector is None,
                    -(candidate.width * candidate.height),
                    -candidate.mtime_ns,
                    candidate.candidate_id,
                ),
            )
        )
    if slot == "recent":
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (-candidate.mtime_ns, candidate.candidate_id),
            )
        )
    if slot == "low_exposure":
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    max(0, candidate.exposure_count),
                    -candidate.mtime_ns,
                    candidate.candidate_id,
                ),
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                _seeded_order(candidate.candidate_id, rng_seed),
                candidate.candidate_id,
            ),
        )
    )


def _base_score(
    candidate: RecommendationCandidate,
    slot: RecommendationSlot,
    rank: int,
    total: int,
) -> float:
    rank_score = 1.0 - 0.25 * (rank / total)
    if slot == "quality" and candidate.vector is not None:
        return rank_score + 0.15
    return rank_score


def _maximum_diversity_penalty(
    candidate: RecommendationCandidate,
    selected: list[RecommendationItem],
) -> float:
    largest = 0.0
    for item in selected:
        similarity = _cosine_similarity(candidate.vector, item.candidate.vector)
        if similarity is None:
            continue
        largest = max(largest, _similarity_penalty(similarity))
    return largest


def _cosine_similarity(
    left: tuple[float, ...] | None,
    right: tuple[float, ...] | None,
) -> float | None:
    if left is None or right is None or not left or len(left) != len(right):
        return None
    try:
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(dot) or left_norm == 0 or right_norm == 0:
        return None
    similarity = dot / (left_norm * right_norm)
    return similarity if math.isfinite(similarity) else None


def _similarity_penalty(similarity: float) -> float:
    if similarity < 0.85:
        return 0.0
    if similarity >= 0.95:
        return 1.0
    return 0.10 + 0.90 * ((similarity - 0.85) / 0.10)


def _seeded_order(doc_id: str, seed: int) -> int:
    payload = f"{seed}:{doc_id}".encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _group_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
