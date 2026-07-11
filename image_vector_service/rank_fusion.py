from __future__ import annotations

from dataclasses import replace

from .models import SearchHit


def weighted_rrf(
    image_hits: list[SearchHit],
    text_hits: list[SearchHit],
    image_weight: float,
    text_weight: float,
    rank_constant: int = 60,
) -> list[SearchHit]:
    if image_weight < 0 or text_weight < 0 or image_weight + text_weight <= 0:
        raise ValueError("Search weights must be non-negative and not both zero.")

    total_weight = image_weight + text_weight
    image_weight /= total_weight
    text_weight /= total_weight

    scores: dict[str, float] = {}
    hits: dict[str, SearchHit] = {}
    for weight, collection in (
        (image_weight, image_hits),
        (text_weight, text_hits),
    ):
        for rank, hit in enumerate(collection, start=1):
            scores[hit.doc_id] = scores.get(hit.doc_id, 0.0) + weight / (
                rank_constant + rank
            )
            hits.setdefault(hit.doc_id, hit)

    ordered = sorted(scores, key=lambda doc_id: scores[doc_id], reverse=True)
    return [
        replace(
            hits[doc_id],
            rank=rank,
            fused_score=scores[doc_id],
        )
        for rank, doc_id in enumerate(ordered, start=1)
    ]
