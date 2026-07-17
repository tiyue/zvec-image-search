from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from .models import (
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_POSSIBLE_THRESHOLD,
    RRF_RANK_CONSTANT,
    RankSource,
    SearchHit,
    classify_match_state,
    normalize_rrf_score,
)

MAX_CONFIDENCE_CANDIDATES = 50
DEFAULT_MIN_CONFIDENCE = 0.25
DEFAULT_SCORE_GAP = 0.20
DEFAULT_MAX_CONFIDENCE_DROP = 1.0
DEFAULT_SOURCE_REWARD = 0.05
DEFAULT_AGREEMENT_REWARD = 0.08
DEFAULT_RANK_DECAY = 10.0
DEFAULT_WEAK_CHANNEL_FLOOR = 0.45
DEFAULT_WEAK_CHANNEL_PENALTY = 0.08
FUSION_MODES = {"confidence_v1", "confidence_v2"}


@dataclass(frozen=True)
class ConfidenceRanking:
    hits: list[SearchHit]
    status: str
    candidate_count: int
    filtered_count: int


def weighted_rrf(
    image_hits: list[SearchHit],
    text_hits: list[SearchHit],
    image_weight: float,
    text_weight: float,
    rank_constant: int = RRF_RANK_CONSTANT,
) -> list[SearchHit]:
    if (
        not math.isfinite(image_weight)
        or not math.isfinite(text_weight)
        or image_weight < 0
        or text_weight < 0
        or image_weight + text_weight <= 0
    ):
        raise ValueError("Search weights must be non-negative and not both zero.")

    total_weight = image_weight + text_weight
    image_weight /= total_weight
    text_weight /= total_weight

    scores: dict[str, float] = {}
    hits: dict[str, SearchHit] = {}
    diagnostics: dict[str, dict[str, float | int]] = {}
    for source, weight, collection in (
        ("image", image_weight, image_hits),
        ("text", text_weight, text_hits),
    ):
        if weight <= 0.0:
            continue
        for rank, hit in enumerate(collection, start=1):
            scores[hit.doc_id] = scores.get(hit.doc_id, 0.0) + weight / (
                rank_constant + rank
            )
            hits.setdefault(hit.doc_id, hit)
            detail = diagnostics.setdefault(hit.doc_id, {})
            channel_confidence = _confidence(hit)
            if channel_confidence is not None:
                detail[f"{source}_confidence"] = channel_confidence
            detail[f"{source}_rank"] = rank

    ordered = sorted(scores, key=lambda doc_id: scores[doc_id], reverse=True)
    results = []
    for rank, doc_id in enumerate(ordered, start=1):
        raw_score = scores[doc_id]
        normalized_score = normalize_rrf_score(raw_score, rank_constant)
        detail = diagnostics[doc_id]
        image_rank = cast(int | None, detail.get("image_rank"))
        text_rank = cast(int | None, detail.get("text_rank"))
        results.append(
            replace(
                hits[doc_id],
                rank=rank,
                fused_score=raw_score,
                raw_score=raw_score,
                normalized_score=normalized_score,
                confidence=normalized_score,
                match_state=classify_match_state(normalized_score),
                rank_source="fused",
                image_confidence=cast(float | None, detail.get("image_confidence")),
                text_confidence=cast(float | None, detail.get("text_confidence")),
                image_rank=image_rank,
                text_rank=text_rank,
                rank_agreement=(
                    math.exp(-abs(image_rank - text_rank) / DEFAULT_RANK_DECAY)
                    if image_rank is not None and text_rank is not None
                    else None
                ),
            )
        )
    return results


def confidence_rank(
    hits: list[SearchHit],
    *,
    query_type: str,
    top_k: int,
    image_weight: float = 0.5,
    text_weight: float = 0.5,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    minimum_score: float | None = None,
    possible_confidence: float = DEFAULT_POSSIBLE_THRESHOLD,
    high_confidence: float = DEFAULT_HIGH_THRESHOLD,
    score_gap: float = DEFAULT_SCORE_GAP,
    max_confidence_drop: float = DEFAULT_MAX_CONFIDENCE_DROP,
    source_reward: float = DEFAULT_SOURCE_REWARD,
    fusion_mode: str = "confidence_v1",
    agreement_reward: float = DEFAULT_AGREEMENT_REWARD,
    rank_decay: float = DEFAULT_RANK_DECAY,
    weak_channel_floor: float = DEFAULT_WEAK_CHANNEL_FLOOR,
    weak_channel_penalty: float = DEFAULT_WEAK_CHANNEL_PENALTY,
    max_candidates: int = MAX_CONFIDENCE_CANDIDATES,
    show_low_confidence: bool = False,
    collection_confidence_offsets: Mapping[str, float] | None = None,
) -> ConfidenceRanking | None:
    """Rank comparable confidence scores, or request the legacy fallback.

    Confidence is an opt-in contract. A mixed candidate set containing even one
    legacy hit is deliberately not compared on the new scale; callers should use
    their previous distance/RRF behavior instead.
    """
    _validate_confidence_options(
        query_type=query_type,
        top_k=top_k,
        image_weight=image_weight,
        text_weight=text_weight,
        min_confidence=min_confidence,
        minimum_score=minimum_score,
        possible_confidence=possible_confidence,
        high_confidence=high_confidence,
        score_gap=score_gap,
        max_confidence_drop=max_confidence_drop,
        source_reward=source_reward,
        fusion_mode=fusion_mode,
        agreement_reward=agreement_reward,
        rank_decay=rank_decay,
        weak_channel_floor=weak_channel_floor,
        weak_channel_penalty=weak_channel_penalty,
        max_candidates=max_candidates,
    )
    if any(_confidence(hit) is None for hit in hits):
        return None
    resolved_collection_offsets = _validated_collection_offsets(
        collection_confidence_offsets
    )

    fused = _fuse_confidence_sources(
        hits,
        query_type=query_type,
        image_weight=image_weight,
        text_weight=text_weight,
        source_reward=source_reward,
        fusion_mode=fusion_mode,
        agreement_reward=agreement_reward,
        rank_decay=rank_decay,
        weak_channel_floor=weak_channel_floor,
        weak_channel_penalty=weak_channel_penalty,
        possible_confidence=possible_confidence,
        high_confidence=high_confidence,
    )
    if collection_confidence_offsets is not None:
        fused = [
            replace(
                hit,
                ranking_confidence=_ranking_confidence(
                    hit,
                    resolved_collection_offsets,
                ),
            )
            for hit in fused
        ]
    ordered = sorted(
        fused,
        key=lambda hit: _confidence_sort_key(hit, resolved_collection_offsets),
    )
    distinct: list[SearchHit] = []
    seen_documents: set[tuple[str, str]] = set()
    seen_hashes: set[str] = set()
    for hit in ordered:
        document_key = _document_key(hit)
        if document_key in seen_documents:
            continue
        seen_documents.add(document_key)
        sha256 = str(hit.fields.get("sha256") or "")
        if sha256 and sha256 in seen_hashes:
            continue
        if sha256:
            seen_hashes.add(sha256)
        distinct.append(hit)
        if len(distinct) >= max_candidates:
            break

    candidate_count = len(distinct)
    if show_low_confidence:
        selected = [
            replace(hit, rank=rank)
            for rank, hit in enumerate(distinct[:top_k], start=1)
        ]
        return ConfidenceRanking(
            hits=selected,
            status="low_confidence_override" if selected else "no_reliable_match",
            candidate_count=candidate_count,
            filtered_count=candidate_count - len(selected),
        )

    passing: list[SearchHit] = []
    for hit in distinct:
        confidence = _confidence(hit)
        assert confidence is not None
        if not _passes_minimum_score(hit, query_type, minimum_score):
            continue
        if confidence < min_confidence:
            continue
        passing.append(hit)

    gap_limited: list[SearchHit] = []
    top_ranking_confidence: float | None = None
    previous_ranking_confidence: float | None = None
    for hit in passing:
        ranking_confidence = _ranking_confidence(hit, resolved_collection_offsets)
        if top_ranking_confidence is None:
            top_ranking_confidence = ranking_confidence
        elif top_ranking_confidence - ranking_confidence > max_confidence_drop + 1e-12:
            break
        if (
            previous_ranking_confidence is not None
            and previous_ranking_confidence - ranking_confidence >= score_gap
        ):
            break
        gap_limited.append(hit)
        previous_ranking_confidence = ranking_confidence

    selected = [
        replace(hit, rank=rank) for rank, hit in enumerate(gap_limited[:top_k], start=1)
    ]
    return ConfidenceRanking(
        hits=selected,
        status="ok" if selected else "no_reliable_match",
        candidate_count=candidate_count,
        filtered_count=candidate_count - len(selected),
    )


def _fuse_confidence_sources(
    hits: list[SearchHit],
    *,
    query_type: str,
    image_weight: float,
    text_weight: float,
    source_reward: float,
    fusion_mode: str,
    agreement_reward: float,
    rank_decay: float,
    weak_channel_floor: float,
    weak_channel_penalty: float,
    possible_confidence: float,
    high_confidence: float,
) -> list[SearchHit]:
    if fusion_mode == "confidence_v2" and query_type == "image_text":
        return _fuse_confidence_v2(
            hits,
            image_weight=image_weight,
            text_weight=text_weight,
            agreement_reward=agreement_reward,
            rank_decay=rank_decay,
            weak_channel_floor=weak_channel_floor,
            weak_channel_penalty=weak_channel_penalty,
            possible_confidence=possible_confidence,
            high_confidence=high_confidence,
        )

    source_ranks = _source_ranks(hits)
    grouped: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        grouped.setdefault(_document_key(hit), []).append(hit)

    results: list[SearchHit] = []
    for values in grouped.values():
        representative = min(values, key=_confidence_sort_key)
        if query_type != "image_text":
            confidence = max(_required_confidence(value) for value in values)
            results.append(
                replace(
                    representative,
                    normalized_score=confidence,
                    confidence=confidence,
                    match_state=classify_match_state(
                        confidence,
                        high=high_confidence,
                        possible=possible_confidence,
                    ),
                    rank_source="image" if query_type == "image" else "text",
                )
            )
            continue

        by_source: dict[str, SearchHit] = {}
        for value in values:
            source = str(getattr(value, "rank_source", "") or "")
            current = by_source.get(source)
            if current is None or _required_confidence(value) > _required_confidence(
                current
            ):
                by_source[source] = value
        rank_source: RankSource
        direct_fused = by_source.get("fused")
        if direct_fused is not None:
            confidence = _required_confidence(direct_fused)
            representative = direct_fused
            rank_source = "fused"
        else:
            source_values: list[tuple[float, float]] = []
            if "image" in by_source and image_weight > 0:
                source_values.append(
                    (image_weight, _required_confidence(by_source["image"]))
                )
            if "text" in by_source and text_weight > 0:
                source_values.append(
                    (text_weight, _required_confidence(by_source["text"]))
                )
            if not source_values:
                continue
            else:
                available_weight = sum(weight for weight, _ in source_values)
                confidence = (
                    sum(weight * value for weight, value in source_values)
                    / available_weight
                )
                dual_source = (
                    image_weight > 0
                    and text_weight > 0
                    and {"image", "text"}.issubset(by_source)
                )
                if dual_source:
                    confidence = min(1.0, confidence + source_reward)
                if dual_source:
                    rank_source = "fused"
                elif "image" in by_source and image_weight > 0:
                    rank_source = "image"
                elif "text" in by_source and text_weight > 0:
                    rank_source = "text"
                else:
                    continue
        image_hit = by_source.get("image")
        text_hit = by_source.get("text")
        image_rank = source_ranks.get(id(image_hit)) if image_hit is not None else None
        text_rank = source_ranks.get(id(text_hit)) if text_hit is not None else None
        results.append(
            replace(
                representative,
                # A configured combined result is ranked on the fused
                # confidence scale.  Keep raw_score on that same declared
                # scale instead of leaking the representative channel's
                # cosine distance into manifests and quality captures.
                raw_score=confidence,
                normalized_score=confidence,
                confidence=confidence,
                match_state=classify_match_state(
                    confidence,
                    high=high_confidence,
                    possible=possible_confidence,
                ),
                rank_source=rank_source,
                image_confidence=(
                    _required_confidence(image_hit) if image_hit is not None else None
                ),
                text_confidence=(
                    _required_confidence(text_hit) if text_hit is not None else None
                ),
                image_rank=image_rank,
                text_rank=text_rank,
                rank_agreement=(
                    math.exp(-abs(image_rank - text_rank) / DEFAULT_RANK_DECAY)
                    if image_rank is not None and text_rank is not None
                    else None
                ),
            )
        )
    return results


def _fuse_confidence_v2(
    hits: list[SearchHit],
    *,
    image_weight: float,
    text_weight: float,
    agreement_reward: float,
    rank_decay: float,
    weak_channel_floor: float,
    weak_channel_penalty: float,
    possible_confidence: float,
    high_confidence: float,
) -> list[SearchHit]:
    """Fuse calibrated channel confidence without converting ranks into relevance.

    Rank agreement is only a bounded, evidence-scaled reward. A weak or missing
    channel never receives that reward, and can reduce confidence by at most the
    configured penalty so that a useful dominant modality is not destroyed.
    """
    grouped: dict[tuple[str, str], list[tuple[SearchHit, int]]] = {}
    source_positions: dict[tuple[str, str], int] = {}
    for hit in hits:
        source = str(hit.rank_source or "")
        library_id = str(hit.fields.get("library_id") or "")
        position_key = (library_id, source)
        source_positions[position_key] = source_positions.get(position_key, 0) + 1
        source_rank = hit.rank if hit.rank > 0 else source_positions[position_key]
        grouped.setdefault(_document_key(hit), []).append((hit, source_rank))

    total_weight = image_weight + text_weight
    normalized_image_weight = image_weight / total_weight
    normalized_text_weight = text_weight / total_weight
    results: list[SearchHit] = []
    for values in grouped.values():
        by_source: dict[str, tuple[SearchHit, int]] = {}
        for value, source_rank in values:
            source = str(value.rank_source or "")
            if source not in {"image", "text"}:
                continue
            current = by_source.get(source)
            if current is None or (_required_confidence(value), -source_rank) > (
                _required_confidence(current[0]),
                -current[1],
            ):
                by_source[source] = (value, source_rank)

        if not by_source:
            # Directly fused candidates are retained for compatibility with
            # callers that mix pre-fused and per-channel hits.
            representative, _ = max(
                values,
                key=lambda value: (_required_confidence(value[0]), -value[1]),
            )
            confidence = _required_confidence(representative)
            results.append(
                _with_fusion_diagnostics(
                    representative,
                    confidence=confidence,
                    possible_confidence=possible_confidence,
                    high_confidence=high_confidence,
                )
            )
            continue

        image = by_source.get("image")
        text = by_source.get("text")
        if image_weight == 0 or text_weight == 0:
            active_source = "image" if image_weight > 0 else "text"
            active = image if image_weight > 0 else text
            if active is None:
                continue
            representative, _active_rank = active
            confidence = _required_confidence(representative)
            results.append(
                _with_fusion_diagnostics(
                    representative,
                    confidence=confidence,
                    possible_confidence=possible_confidence,
                    high_confidence=high_confidence,
                    image_confidence=(
                        _required_confidence(image[0]) if image is not None else None
                    ),
                    text_confidence=(
                        _required_confidence(text[0]) if text is not None else None
                    ),
                    image_rank=image[1] if image is not None else None,
                    text_rank=text[1] if text is not None else None,
                    rank_agreement=(
                        math.exp(-abs(image[1] - text[1]) / rank_decay)
                        if image is not None and text is not None
                        else None
                    ),
                    rank_source=cast(RankSource, active_source),
                )
            )
            continue
        if image is None or text is None:
            source, (representative, source_rank) = next(iter(by_source.items()))
            confidence = max(
                0.0,
                _required_confidence(representative) - weak_channel_penalty,
            )
            results.append(
                _with_fusion_diagnostics(
                    representative,
                    confidence=confidence,
                    possible_confidence=possible_confidence,
                    high_confidence=high_confidence,
                    image_confidence=(
                        _required_confidence(representative)
                        if source == "image"
                        else None
                    ),
                    text_confidence=(
                        _required_confidence(representative)
                        if source == "text"
                        else None
                    ),
                    image_rank=source_rank if source == "image" else None,
                    text_rank=source_rank if source == "text" else None,
                    rank_source=cast(RankSource, source),
                )
            )
            continue

        image_hit, image_rank = image
        text_hit, text_rank = text
        image_confidence = _required_confidence(image_hit)
        text_confidence = _required_confidence(text_hit)
        weighted_confidence = (
            normalized_image_weight * image_confidence
            + normalized_text_weight * text_confidence
        )
        weaker_confidence = min(image_confidence, text_confidence)
        stronger_confidence = max(image_confidence, text_confidence)
        rank_agreement = math.exp(-abs(image_rank - text_rank) / rank_decay)
        if weaker_confidence >= weak_channel_floor:
            confidence = weighted_confidence + (
                agreement_reward * weaker_confidence * rank_agreement
            )
        else:
            weak_ratio = (
                weaker_confidence / weak_channel_floor
                if weak_channel_floor > 0
                else 1.0
            )
            protected_confidence = stronger_confidence - (
                weak_channel_penalty * (1.0 - weak_ratio)
            )
            confidence = max(weighted_confidence, protected_confidence)
        confidence = min(1.0, max(0.0, confidence))
        representative = max(
            (image_hit, text_hit),
            key=lambda value: (
                _required_confidence(value),
                -value.distance,
                value.doc_id,
            ),
        )
        results.append(
            _with_fusion_diagnostics(
                representative,
                confidence=confidence,
                possible_confidence=possible_confidence,
                high_confidence=high_confidence,
                image_confidence=image_confidence,
                text_confidence=text_confidence,
                image_rank=image_rank,
                text_rank=text_rank,
                rank_agreement=rank_agreement,
                rank_source="fused",
            )
        )
    return results


def _source_ranks(hits: list[SearchHit]) -> dict[int, int]:
    positions: dict[tuple[str, str], int] = {}
    ranks: dict[int, int] = {}
    for hit in hits:
        source = str(hit.rank_source or "")
        key = (str(hit.fields.get("library_id") or ""), source)
        positions[key] = positions.get(key, 0) + 1
        ranks[id(hit)] = hit.rank if hit.rank > 0 else positions[key]
    return ranks


def _with_fusion_diagnostics(
    hit: SearchHit,
    *,
    confidence: float,
    possible_confidence: float,
    high_confidence: float,
    image_confidence: float | None = None,
    text_confidence: float | None = None,
    image_rank: int | None = None,
    text_rank: int | None = None,
    rank_agreement: float | None = None,
    rank_source: RankSource = "fused",
) -> SearchHit:
    return replace(
        hit,
        raw_score=confidence,
        normalized_score=confidence,
        confidence=confidence,
        match_state=classify_match_state(
            confidence,
            high=high_confidence,
            possible=possible_confidence,
        ),
        rank_source=rank_source,
        image_confidence=image_confidence,
        text_confidence=text_confidence,
        image_rank=image_rank,
        text_rank=text_rank,
        rank_agreement=rank_agreement,
    )


def _document_key(hit: SearchHit) -> tuple[str, str]:
    return (str(hit.fields.get("library_id") or ""), hit.doc_id)


def _valid_rank_source(value: object) -> RankSource:
    if isinstance(value, str) and value in {"image", "text", "fused"}:
        return cast(RankSource, value)
    return "fused"


def _confidence(hit: SearchHit) -> float | None:
    value = getattr(hit, "confidence", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


def _required_confidence(hit: SearchHit) -> float:
    value = _confidence(hit)
    if value is None:
        raise ValueError("Search hit is missing a finite confidence score.")
    return value


def _passes_minimum_score(
    hit: SearchHit,
    query_type: str,
    minimum_score: float | None,
) -> bool:
    if minimum_score is None:
        return True
    raw_score = hit.raw_score
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        return False
    value = float(raw_score)
    if not math.isfinite(value):
        return False
    if query_type == "image_text":
        return value >= minimum_score
    return value <= minimum_score


def _confidence_sort_key(
    hit: SearchHit,
    collection_confidence_offsets: Mapping[str, float] | None = None,
) -> tuple[float, float, str, str]:
    return (
        -_ranking_confidence(hit, collection_confidence_offsets or {}),
        hit.distance,
        str(hit.fields.get("library_id") or ""),
        hit.doc_id,
    )


def _ranking_confidence(
    hit: SearchHit,
    collection_confidence_offsets: Mapping[str, float],
) -> float:
    if hit.ranking_confidence is not None:
        return float(hit.ranking_confidence)
    library_id = str(hit.fields.get("library_id") or "")
    offset = collection_confidence_offsets.get(library_id, 0.0)
    return min(1.0, max(0.0, _required_confidence(hit) + offset))


def _validated_collection_offsets(
    values: Mapping[str, float] | None,
) -> dict[str, float]:
    if values is None:
        return {}
    offsets: dict[str, float] = {}
    for library_id, value in values.items():
        if not isinstance(library_id, str) or not library_id.strip():
            raise ValueError("Collection confidence offset ids must be non-empty.")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not -1.0 <= float(value) <= 1.0
        ):
            raise ValueError(
                "Collection confidence offsets must be finite values between -1 and 1."
            )
        offsets[library_id.strip()] = float(value)
    return offsets


def _validate_confidence_options(
    *,
    query_type: str,
    top_k: int,
    image_weight: float,
    text_weight: float,
    min_confidence: float,
    minimum_score: float | None,
    possible_confidence: float,
    high_confidence: float,
    score_gap: float,
    max_confidence_drop: float,
    source_reward: float,
    fusion_mode: str,
    agreement_reward: float,
    rank_decay: float,
    weak_channel_floor: float,
    weak_channel_penalty: float,
    max_candidates: int,
) -> None:
    if query_type not in {"text", "image", "image_text"}:
        raise ValueError(f"Unsupported confidence query type: {query_type}")
    if top_k < 1:
        raise ValueError("top_k must be positive.")
    if max_candidates < 1 or max_candidates > MAX_CONFIDENCE_CANDIDATES:
        raise ValueError(
            f"max_candidates must be between 1 and {MAX_CONFIDENCE_CANDIDATES}."
        )
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be between zero and one.")
    if minimum_score is not None:
        if (
            isinstance(minimum_score, bool)
            or not isinstance(minimum_score, (int, float))
            or not math.isfinite(float(minimum_score))
            or minimum_score < 0
        ):
            raise ValueError("minimum_score must be a finite non-negative number.")
        if query_type in {"text", "image"} and minimum_score > 2:
            raise ValueError(
                "minimum_score must be at most two for cosine-distance queries."
            )
    if not 0 <= possible_confidence <= high_confidence <= 1:
        raise ValueError(
            "confidence display thresholds must satisfy 0 <= possible <= high <= 1."
        )
    if not 0 < score_gap <= 1:
        raise ValueError("score_gap must be greater than zero and at most one.")
    if not 0 <= max_confidence_drop <= 1:
        raise ValueError("max_confidence_drop must be between zero and one.")
    if not 0 <= source_reward <= 1:
        raise ValueError("source_reward must be between zero and one.")
    if fusion_mode not in FUSION_MODES:
        raise ValueError(f"Unsupported confidence fusion mode: {fusion_mode}")
    if not 0 <= agreement_reward <= 0.25:
        raise ValueError("agreement_reward must be between zero and 0.25.")
    if not 0 < rank_decay <= 100:
        raise ValueError("rank_decay must be greater than zero and at most 100.")
    if not 0 <= weak_channel_floor <= 1:
        raise ValueError("weak_channel_floor must be between zero and one.")
    if not 0 <= weak_channel_penalty <= 0.5:
        raise ValueError("weak_channel_penalty must be between zero and 0.5.")
    if (
        not math.isfinite(image_weight)
        or not math.isfinite(text_weight)
        or image_weight < 0
        or text_weight < 0
        or image_weight + text_weight <= 0
    ):
        raise ValueError("Search weights must be non-negative and not both zero.")
