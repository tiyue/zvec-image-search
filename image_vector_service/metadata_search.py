from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

from .models import (
    RRF_RANK_CONSTANT,
    RankSource,
    SearchHit,
    classify_match_state,
    normalize_rrf_score,
)

TEXT_VISUAL_WEIGHT = 0.60
TEXT_METADATA_WEIGHT = 0.40
TAG_WEIGHT = 0.70
COMBINED_TEXT_VISUAL_SHARE = 0.60
COMBINED_METADATA_SHARE = 0.40

_SUPPORTED_CHANNELS = frozenset({"image", "text", "metadata", "tag"})


def fuse_text_metadata_hits(
    vector_hits: Sequence[SearchHit],
    metadata_hits: Sequence[SearchHit],
    tag_hits: Sequence[SearchHit] = (),
    *,
    rank_constant: int = RRF_RANK_CONSTANT,
) -> tuple[list[SearchHit], dict[str, object]]:
    """Fuse text-to-image, description-vector, and accepted-tag candidates.

    The accepted-tag channel keeps the existing 70% share when it is active.
    The remaining share is split between the visual and metadata channels only
    when metadata vectors are actually available. This preserves the old 70/30
    behavior for Collections that have not been backfilled yet.
    """

    if not metadata_hits and not tag_hits:
        return list(vector_hits), {
            "enabled": False,
            "channels": ["text"] if vector_hits else [],
            "weights": {"text": 1.0} if vector_hits else {},
            "metadata_candidate_count": 0,
            "extra_embedding_requests": 0,
            "mode": "distance",
        }

    available_vector_channels = int(bool(vector_hits)) + int(bool(metadata_hits))
    if tag_hits:
        remaining = 1.0 - TAG_WEIGHT
        if available_vector_channels == 2:
            weights = {
                "tag": TAG_WEIGHT,
                "text": remaining * 0.5,
                "metadata": remaining * 0.5,
            }
        elif metadata_hits:
            weights = {"tag": TAG_WEIGHT, "metadata": remaining}
        else:
            weights = {"tag": TAG_WEIGHT, "text": remaining}
    elif metadata_hits and vector_hits:
        weights = {
            "text": TEXT_VISUAL_WEIGHT,
            "metadata": TEXT_METADATA_WEIGHT,
        }
    elif metadata_hits:
        weights = {"metadata": 1.0}
    else:
        weights = {"text": 1.0}

    channels = {
        "text": list(vector_hits),
        "metadata": list(metadata_hits),
        "tag": list(tag_hits),
    }
    hits, diagnostics = weighted_rrf_channels(
        channels,
        weights,
        rank_constant=rank_constant,
    )
    diagnostics["mode"] = (
        "hybrid_tag_visual_metadata"
        if tag_hits and metadata_hits
        else "hybrid_tag_vector"
        if tag_hits
        else "visual_metadata"
        if metadata_hits
        else "distance"
    )
    return hits, diagnostics


def fuse_combined_metadata_hits(
    image_hits: Sequence[SearchHit],
    text_hits: Sequence[SearchHit],
    metadata_hits: Sequence[SearchHit],
    *,
    image_weight: float,
    text_weight: float,
    rank_constant: int = RRF_RANK_CONSTANT,
) -> tuple[list[SearchHit], dict[str, object]]:
    """Fuse image, text-to-image, and text-to-description candidates.

    ``image_weight`` and ``text_weight`` retain their public meaning. The text
    side is split into visual and description channels only when description
    candidates exist; otherwise this is equivalent to the previous two-channel
    weighted RRF.
    """

    _validate_non_negative_weights(
        {"image": image_weight, "text": text_weight},
        require_positive_total=True,
    )
    if metadata_hits and text_weight > 0:
        weights = {
            "image": image_weight,
            "text": text_weight * COMBINED_TEXT_VISUAL_SHARE,
            "metadata": text_weight * COMBINED_METADATA_SHARE,
        }
    else:
        weights = {"image": image_weight, "text": text_weight}
    hits, diagnostics = weighted_rrf_channels(
        {
            "image": list(image_hits),
            "text": list(text_hits),
            "metadata": list(metadata_hits),
        },
        weights,
        rank_constant=rank_constant,
    )
    diagnostics["mode"] = (
        "weighted_rrf_with_metadata" if metadata_hits else "weighted_rrf"
    )
    return hits, diagnostics


def weighted_rrf_channels(
    channels: Mapping[str, Sequence[SearchHit]],
    weights: Mapping[str, float],
    *,
    rank_constant: int = RRF_RANK_CONSTANT,
) -> tuple[list[SearchHit], dict[str, object]]:
    """Fuse any supported local retrieval channels using weighted RRF.

    Empty channels are removed before weights are normalized. A partially
    backfilled Collection therefore falls back to its available channels rather
    than treating missing metadata vectors as negative evidence.
    """

    unknown_channels = (set(channels) | set(weights)) - _SUPPORTED_CHANNELS
    if unknown_channels:
        raise ValueError(
            f"Unsupported search channels: {', '.join(sorted(unknown_channels))}"
        )
    if rank_constant < 0:
        raise ValueError("rank_constant cannot be negative.")
    _validate_non_negative_weights(weights, require_positive_total=False)

    active = {
        name: list(values)
        for name, values in channels.items()
        if values and float(weights.get(name, 0.0)) > 0.0
    }
    if not active:
        return [], {
            "enabled": False,
            "channels": [],
            "weights": {},
            "extra_embedding_requests": 0,
        }
    active_weight_total = sum(float(weights[name]) for name in active)
    if active_weight_total <= 0:
        raise ValueError("Active search channel weights must sum to a positive value.")
    normalized_weights = {
        name: float(weights[name]) / active_weight_total for name in active
    }

    scores: dict[tuple[str, str], float] = {}
    representatives: dict[tuple[str, str], SearchHit] = {}
    channel_hits: dict[tuple[str, str], dict[str, SearchHit]] = {}
    channel_ranks: dict[tuple[str, str], dict[str, int]] = {}
    matched_tags: dict[tuple[str, str], list[str]] = {}

    for channel, values in active.items():
        best = _best_by_document(values)
        for fallback_rank, (key, hit) in enumerate(best.items(), start=1):
            rank = hit.rank if hit.rank > 0 else fallback_rank
            scores[key] = scores.get(key, 0.0) + normalized_weights[channel] / (
                rank_constant + rank
            )
            by_channel = channel_hits.setdefault(key, {})
            by_channel[channel] = hit
            channel_ranks.setdefault(key, {})[channel] = rank
            current = representatives.get(key)
            if current is None or _representative_key(hit) > _representative_key(
                current
            ):
                representatives[key] = hit
            values_for_key = matched_tags.setdefault(key, [])
            values_for_key.extend(hit.matched_tags)

    ordered_keys = sorted(
        scores,
        key=lambda key: (
            -scores[key],
            -len(channel_hits[key]),
            key[0],
            key[1],
        ),
    )
    results: list[SearchHit] = []
    for output_rank, key in enumerate(ordered_keys, start=1):
        raw_score = scores[key]
        normalized_score = normalize_rrf_score(raw_score, rank_constant)
        by_channel = channel_hits[key]
        ranks = channel_ranks[key]
        representative = representatives[key]
        rank_source: RankSource = (
            "fused" if len(by_channel) > 1 else _rank_source(next(iter(by_channel)))
        )
        results.append(
            replace(
                representative,
                rank=output_rank,
                distance=1.0 - normalized_score,
                fused_score=raw_score,
                raw_score=raw_score,
                normalized_score=normalized_score,
                confidence=normalized_score,
                match_state=classify_match_state(normalized_score),
                rank_source=rank_source,
                image_confidence=_channel_confidence(by_channel.get("image")),
                text_confidence=_channel_confidence(by_channel.get("text")),
                metadata_confidence=_channel_confidence(by_channel.get("metadata")),
                image_rank=ranks.get("image"),
                text_rank=ranks.get("text"),
                metadata_rank=ranks.get("metadata"),
                rank_agreement=_rank_agreement(ranks),
                matched_tags=tuple(dict.fromkeys(matched_tags[key])),
            )
        )

    return results, {
        "enabled": "metadata" in active,
        "channels": sorted(active),
        "weights": {
            name: round(value, 6) for name, value in sorted(normalized_weights.items())
        },
        "metadata_candidate_count": len(active.get("metadata", ())),
        "extra_embedding_requests": 0,
    }


def _best_by_document(
    values: Sequence[SearchHit],
) -> dict[tuple[str, str], SearchHit]:
    result: dict[tuple[str, str], SearchHit] = {}
    for hit in values:
        key = (str(hit.fields.get("library_id") or ""), hit.doc_id)
        current = result.get(key)
        if current is None or _representative_key(hit) > _representative_key(current):
            result[key] = hit
    return result


def _representative_key(hit: SearchHit) -> tuple[float, int, float]:
    confidence = _channel_confidence(hit) or 0.0
    rank = hit.rank if hit.rank > 0 else 2**31 - 1
    return (confidence, -rank, -float(hit.distance))


def _channel_confidence(hit: SearchHit | None) -> float | None:
    if hit is None:
        return None
    value = hit.confidence
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    return min(1.0, max(0.0, normalized))


def _rank_agreement(ranks: Mapping[str, int]) -> float | None:
    values = list(ranks.values())
    if len(values) < 2:
        return None
    return math.exp(-(max(values) - min(values)) / 10.0)


def _rank_source(channel: str) -> RankSource:
    if channel not in _SUPPORTED_CHANNELS:
        raise ValueError(f"Unsupported search channel: {channel}")
    return cast(RankSource, channel)


def _validate_non_negative_weights(
    weights: Mapping[str, float],
    *,
    require_positive_total: bool,
) -> None:
    total = 0.0
    for name, value in weights.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError(f"Search channel weight {name!r} must be non-negative.")
        total += float(value)
    if require_positive_total and total <= 0:
        raise ValueError("Search channel weights cannot all be zero.")
