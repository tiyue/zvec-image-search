from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from .models import (
    RRF_RANK_CONSTANT,
    RankSource,
    SearchHit,
    classify_match_state,
    normalize_rrf_score,
)
from .tag_search import (
    TagCatalog,
    TagExpansionTooBroadError,
    TagSearchPlan,
    normalize_tag_search_text,
)

HYBRID_TAG_WEIGHT = 0.70
HYBRID_VECTOR_WEIGHT = 0.30
HYBRID_MIN_CANDIDATES = 50
HYBRID_MAX_CANDIDATES = 100

_QUERY_SEPARATOR = re.compile(r"[\s,，、;；|/\\]+")


@dataclass(frozen=True)
class HybridTagIntent:
    """Accepted-tag intent detected inside one natural-language query."""

    mode: str = "semantic"
    fragments: tuple[str, ...] = ()
    matched_tags: tuple[str, ...] = ()
    coverage: float = 0.0
    tag_weight: float = 0.0
    vector_weight: float = 1.0
    plan: TagSearchPlan | None = None

    @property
    def enabled(self) -> bool:
        return self.plan is not None and bool(self.matched_tags)

    def diagnostics(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "fragments": list(self.fragments),
            "matched_tags": list(self.matched_tags),
            "coverage": round(self.coverage, 6),
            "tag_weight": self.tag_weight,
            "vector_weight": self.vector_weight,
            "accepted_tags_only": True,
            "extra_embedding_requests": 0,
        }


def detect_hybrid_tag_intent(text: str, catalog: TagCatalog) -> HybridTagIntent:
    """Find explicit or embedded accepted tags without guessing identities.

    Space-delimited fragments keep the existing fuzzy/alias behavior. For
    compact Chinese input such as ``原神刻晴坐姿``, the longest non-overlapping
    catalog tags are extracted from each fragment. A broad one-character
    expansion is ignored for hybrid routing rather than failing semantic search.
    Explicit tag-only search continues to report that validation error.
    """

    query = str(text).strip()
    if not query:
        return HybridTagIntent()

    tokens = tuple(value for value in _QUERY_SEPARATOR.split(query) if value)
    if not tokens:
        tokens = (query,)

    fragments: list[str] = []
    matched_tags: list[str] = []
    matched_characters = 0
    total_characters = sum(len(normalize_tag_search_text(token)) for token in tokens)

    for token in tokens:
        try:
            token_plan = catalog.resolve(token, mode="any")
        except TagExpansionTooBroadError:
            token_plan = None
        if (
            token_plan is not None
            and token_plan.matched_tags
            and _is_strong_token_match(token, token_plan)
        ):
            fragments.append(token)
            matched_tags.extend(token_plan.matched_tags)
            matched_characters += len(normalize_tag_search_text(token))
            continue

        contained = _longest_non_overlapping_catalog_tags(token, catalog)
        if not contained:
            continue
        fragments.extend(contained)
        matched_tags.extend(contained)
        matched_characters += sum(
            len(normalize_tag_search_text(value)) for value in contained
        )

    fragments = list(dict.fromkeys(fragments))
    if not fragments:
        return HybridTagIntent()
    try:
        plan = catalog.resolve(fragments, mode="all")
    except TagExpansionTooBroadError:
        return HybridTagIntent()
    if plan.matches_nothing or not plan.matched_tags:
        return HybridTagIntent()

    coverage = min(
        1.0,
        matched_characters / max(1, total_characters),
    )
    return HybridTagIntent(
        mode="tag_dominant" if coverage >= 0.60 else "mixed",
        fragments=tuple(fragments),
        matched_tags=tuple(dict.fromkeys((*matched_tags, *plan.matched_tags))),
        coverage=coverage,
        tag_weight=HYBRID_TAG_WEIGHT,
        vector_weight=HYBRID_VECTOR_WEIGHT,
        plan=plan,
    )


def fuse_text_and_tag_hits(
    vector_hits: list[SearchHit],
    tag_hits: list[SearchHit],
    *,
    tag_weight: float = HYBRID_TAG_WEIGHT,
    vector_weight: float = HYBRID_VECTOR_WEIGHT,
    rank_constant: int = RRF_RANK_CONSTANT,
) -> list[SearchHit]:
    """Fuse tag and vector ranks without claiming calibrated relevance."""

    _validate_weights(tag_weight, vector_weight)
    if rank_constant < 0:
        raise ValueError("rank_constant cannot be negative.")
    if not tag_hits:
        return list(vector_hits)

    vector_by_key = _best_by_document(vector_hits)
    tag_by_key = _best_by_document(tag_hits)
    keys = set(vector_by_key) | set(tag_by_key)
    fused: list[tuple[float, float, float, int, int, SearchHit]] = []
    for key in keys:
        vector_hit = vector_by_key.get(key)
        tag_hit = tag_by_key.get(key)
        vector_confidence = _confidence(vector_hit)
        tag_confidence = _confidence(tag_hit)
        vector_rank = max(1, vector_hit.rank) if vector_hit is not None else 0
        tag_rank = max(1, tag_hit.rank) if tag_hit is not None else 0
        raw_score = (
            vector_weight / (rank_constant + vector_rank)
            if vector_hit is not None
            else 0.0
        ) + (tag_weight / (rank_constant + tag_rank) if tag_hit is not None else 0.0)
        score = normalize_rrf_score(raw_score, rank_constant)
        representative = vector_hit or tag_hit
        assert representative is not None
        matched_tags = tuple(
            dict.fromkeys(
                (
                    *(vector_hit.matched_tags if vector_hit is not None else ()),
                    *(tag_hit.matched_tags if tag_hit is not None else ()),
                )
            )
        )
        rank_source: RankSource = (
            "fused"
            if vector_hit is not None and tag_hit is not None
            else "tag"
            if tag_hit is not None
            else "text"
        )
        result = replace(
            representative,
            distance=1.0 - score,
            fused_score=raw_score,
            raw_score=raw_score,
            normalized_score=score,
            confidence=score,
            match_state=classify_match_state(score),
            rank_source=rank_source,
            text_confidence=(vector_confidence if vector_hit is not None else None),
            matched_tags=matched_tags,
        )
        fused.append(
            (
                score,
                tag_confidence,
                vector_confidence,
                tag_rank if tag_hit is not None else 2**31 - 1,
                vector_rank if vector_hit is not None else 2**31 - 1,
                result,
            )
        )

    fused.sort(
        key=lambda value: (
            -value[0],
            -value[1],
            -value[2],
            value[3],
            value[4],
            str(value[5].fields.get("library_id") or ""),
            value[5].doc_id,
        )
    )
    return [replace(value[5], rank=rank) for rank, value in enumerate(fused, start=1)]


def hybrid_candidate_count(top_k: int, collection_size: int) -> int:
    if top_k < 1 or collection_size < 0:
        raise ValueError("top_k must be positive and collection_size non-negative.")
    if collection_size == 0:
        return 0
    preferred = max(HYBRID_MIN_CANDIDATES, min(HYBRID_MAX_CANDIDATES, top_k * 4))
    return min(collection_size, max(top_k, preferred))


def _longest_non_overlapping_catalog_tags(
    token: str,
    catalog: TagCatalog,
) -> tuple[str, ...]:
    normalized_token = normalize_tag_search_text(token)
    candidates: list[tuple[int, int, str]] = []
    for tag in catalog.tags:
        normalized_tag = normalize_tag_search_text(tag)
        # Single-character containment is too ambiguous for compact prose.
        # It remains supported when the user enters that character explicitly.
        if len(normalized_tag) < 2:
            continue
        start = normalized_token.find(normalized_tag)
        while start >= 0:
            candidates.append((start, start + len(normalized_tag), tag))
            start = normalized_token.find(normalized_tag, start + 1)

    selected: list[tuple[int, int, str]] = []
    occupied: set[int] = set()
    for start, end, tag in sorted(
        candidates,
        key=lambda value: (-(value[1] - value[0]), value[0], value[2]),
    ):
        positions = set(range(start, end))
        if positions & occupied:
            continue
        selected.append((start, end, tag))
        occupied.update(positions)
    selected.sort(key=lambda value: (value[0], value[1], value[2]))
    return tuple(value[2] for value in selected)


def _is_strong_token_match(token: str, plan: TagSearchPlan) -> bool:
    normalized_token = normalize_tag_search_text(token)
    normalized_matches = {normalize_tag_search_text(tag) for tag in plan.matched_tags}
    if normalized_token in normalized_matches:
        return True
    normalized_equivalents = {
        normalize_tag_search_text(value)
        for expansion in plan.expansions
        for value in expansion.expanded_terms
    }
    if normalized_matches & normalized_equivalents:
        return True
    # Chinese tag queries are commonly entered as short contained fragments
    # such as “原” or “神”. Keep that established behavior while preventing
    # generic ASCII prose such as “red image” from matching a folder named
    # “images” and unexpectedly taking over semantic ranking.
    return any("\u3400" <= character <= "\u9fff" for character in token)


def _best_by_document(hits: list[SearchHit]) -> dict[tuple[str, str], SearchHit]:
    values: dict[tuple[str, str], SearchHit] = {}
    for hit in hits:
        key = (str(hit.fields.get("library_id") or ""), hit.doc_id)
        current = values.get(key)
        if current is None or (_confidence(hit), -hit.rank) > (
            _confidence(current),
            -current.rank,
        ):
            values[key] = hit
    return values


def _confidence(hit: SearchHit | None) -> float:
    if hit is None:
        return 0.0
    value = hit.confidence
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _validate_weights(tag_weight: float, vector_weight: float) -> None:
    if (
        not math.isfinite(tag_weight)
        or not math.isfinite(vector_weight)
        or tag_weight < 0
        or vector_weight < 0
        or not math.isclose(tag_weight + vector_weight, 1.0, abs_tol=1e-9)
    ):
        raise ValueError(
            "Hybrid tag and vector weights must be non-negative and sum to 1."
        )
