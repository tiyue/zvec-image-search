from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from .config import ServiceConfig
from .library_config import LibraryDefinition
from .metadata_search import (
    fuse_combined_metadata_hits,
    fuse_text_metadata_hits,
)
from .models import (
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_POSSIBLE_THRESHOLD,
    PreparedSearch,
    PreparedSearchCandidates,
    RankSource,
    ResolvedSearchHit,
    SearchHit,
    SearchSortMode,
)
from .rank_fusion import (
    DEFAULT_MAX_CONFIDENCE_DROP,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_SCORE_GAP,
    DEFAULT_SOURCE_REWARD,
    MINIMUM_RESULT_CONFIDENCE,
    confidence_rank,
    normalize_sort_mode,
    sort_confidence_hits,
    sort_mode_uses_diversity,
)
from .result_diversity import diversify_search_hits
from .result_exporter import export_results
from .search_learning_config import SearchLearningBundle, load_search_learning


@dataclass(frozen=True)
class LibraryCandidateSet:
    library: LibraryDefinition
    candidates: PreparedSearchCandidates


@dataclass(frozen=True)
class FederatedRanking:
    hits: list[SearchHit]
    status: str
    candidate_count: int
    filtered_count: int
    ranking_mode: str
    sort_mode: SearchSortMode = "confidence"
    ranking_diagnostics: dict[str, object] = field(default_factory=dict)
    diversity: dict[str, object] = field(default_factory=dict)


def aggregate_federated_hits(
    collections: list[LibraryCandidateSet],
    *,
    image_weight: float = 0.5,
    text_weight: float = 0.5,
) -> list[SearchHit]:
    if not collections:
        return []
    query_types = {collection.candidates.query_type for collection in collections}
    if len(query_types) != 1:
        raise ValueError("Federated candidate query types do not match.")
    query_type = next(iter(query_types))
    if query_type == "image_text":
        ordered = _fuse_combined(collections, image_weight, text_weight)
    elif query_type == "text" and any(
        collection.candidates.tag_hits or collection.candidates.metadata_hits
        for collection in collections
    ):
        vector_hits = _global_channel_hits(collections, "hits", "text")
        metadata_hits = _global_channel_hits(
            collections,
            "metadata_hits",
            "metadata",
        )
        tag_hits = _global_channel_hits(collections, "tag_hits", "tag")
        ordered, _diagnostics = fuse_text_metadata_hits(
            vector_hits,
            metadata_hits,
            tag_hits,
        )
    elif query_type in {"text", "image", "tag"}:
        ordered = sorted(
            (
                _decorate(collection.library, item)
                for collection in collections
                for item in collection.candidates.hits
            ),
            key=lambda item: (
                item.distance,
                str(item.fields["library_id"]),
                item.doc_id,
            ),
        )
    else:
        raise ValueError(f"Unsupported federated query type: {query_type}")
    return _deduplicate_and_rank(ordered)


def rank_federated_hits(
    collections: list[LibraryCandidateSet],
    *,
    top_k: int,
    image_weight: float = 0.5,
    text_weight: float = 0.5,
    min_confidence: float | None = None,
    possible_confidence: float | None = None,
    high_confidence: float | None = None,
    score_gap: float | None = None,
    max_confidence_drop: float | None = None,
    source_reward: float = DEFAULT_SOURCE_REWARD,
    max_candidates: int | None = None,
    show_low_confidence: bool = False,
    diversify_results: bool = True,
    sort_mode: SearchSortMode = "confidence",
    search_learning: SearchLearningBundle | None = None,
) -> FederatedRanking:
    if top_k < 1:
        raise ValueError("top_k must be positive.")
    resolved_sort_mode = normalize_sort_mode(sort_mode)
    query_type = _query_type(collections)
    confidence_candidates = _confidence_candidates(collections, query_type)
    quality_configured = bool(collections) and all(
        collection.candidates.quality_configured for collection in collections
    )
    learning_configured = bool(search_learning and search_learning.configured)
    if resolved_sort_mode == "legacy" or not (
        quality_configured or learning_configured
    ):
        return _legacy_ranking(
            collections,
            confidence_candidates,
            query_type=query_type,
            top_k=top_k,
            image_weight=image_weight,
            text_weight=text_weight,
            diversify_results=diversify_results,
            sort_mode="legacy",
            requested_sort_mode=resolved_sort_mode,
            show_low_confidence=show_low_confidence,
        )
    configured_minimum, configured_possible, configured_high = (
        _quality_thresholds(collections)
        if quality_configured
        else (
            MINIMUM_RESULT_CONFIDENCE,
            DEFAULT_POSSIBLE_THRESHOLD,
            DEFAULT_HIGH_THRESHOLD,
        )
    )
    configured_minimum_score = (
        _quality_minimum_score(collections, query_type) if quality_configured else None
    )
    fusion_mode, fusion_options = (
        _fusion_settings(collections) if quality_configured else ("confidence_v1", {})
    )
    collection_confidence_offsets, collection_calibration_diagnostics = (
        _collection_calibration_settings(collections)
    )
    configured_score_gap = (
        _quality_score_gap(collections) if quality_configured else 1.0
    )
    configured_max_confidence_drop = (
        _quality_max_confidence_drop(collections) if quality_configured else 1.0
    )
    confidence_ranking = confidence_rank(
        confidence_candidates,
        query_type=query_type,
        top_k=top_k,
        image_weight=image_weight,
        text_weight=text_weight,
        min_confidence=(
            configured_minimum if min_confidence is None else min_confidence
        ),
        minimum_score=configured_minimum_score,
        possible_confidence=(
            configured_possible if possible_confidence is None else possible_confidence
        ),
        high_confidence=(
            configured_high if high_confidence is None else high_confidence
        ),
        score_gap=configured_score_gap if score_gap is None else score_gap,
        max_confidence_drop=(
            configured_max_confidence_drop
            if max_confidence_drop is None
            else max_confidence_drop
        ),
        source_reward=source_reward,
        fusion_mode=fusion_mode,
        **fusion_options,
        max_candidates=max_candidates,
        show_low_confidence=show_low_confidence,
        collection_confidence_offsets=(
            collection_confidence_offsets
            if quality_configured and collection_calibration_diagnostics["configured"]
            else None
        ),
        sort_mode=resolved_sort_mode,
        search_learning=search_learning if learning_configured else None,
        default_library_id="federated",
        collection_sizes={
            collection.library.library_id: collection.candidates.collection_size
            for collection in collections
        },
    )
    if confidence_ranking is not None:
        diversity_enabled = sort_mode_uses_diversity(
            resolved_sort_mode,
            legacy_diversity=diversify_results,
        )
        diversity = diversify_search_hits(
            confidence_ranking.hits,
            top_k=top_k,
            enabled=diversity_enabled,
        )
        learning_diagnostics = confidence_ranking.diagnostics.get("search_learning")
        learned_applied = bool(
            isinstance(learning_diagnostics, dict)
            and learning_diagnostics.get("applied")
        )
        return FederatedRanking(
            hits=diversity.hits,
            status=confidence_ranking.status,
            candidate_count=confidence_ranking.candidate_count,
            filtered_count=confidence_ranking.filtered_count
            + max(0, len(confidence_ranking.hits) - len(diversity.hits)),
            ranking_mode=(
                "learned"
                if learned_applied
                else "confidence_v2"
                if fusion_mode == "confidence_v2"
                else "confidence"
            ),
            sort_mode=resolved_sort_mode,
            ranking_diagnostics={
                **confidence_ranking.diagnostics,
                "quality_configured": quality_configured,
                "requested_diversity": diversify_results,
                "effective_diversity": diversity_enabled,
            },
            diversity=diversity.diagnostics(),
        )

    return _legacy_ranking(
        collections,
        confidence_candidates,
        query_type=query_type,
        top_k=top_k,
        image_weight=image_weight,
        text_weight=text_weight,
        diversify_results=diversify_results,
        sort_mode="legacy",
        requested_sort_mode=resolved_sort_mode,
        show_low_confidence=show_low_confidence,
    )


def _legacy_ranking(
    collections: list[LibraryCandidateSet],
    confidence_candidates: list[SearchHit],
    *,
    query_type: str,
    top_k: int,
    image_weight: float,
    text_weight: float,
    diversify_results: bool,
    sort_mode: SearchSortMode = "legacy",
    requested_sort_mode: SearchSortMode | None = None,
    show_low_confidence: bool = False,
) -> FederatedRanking:
    legacy = aggregate_federated_hits(
        collections,
        image_weight=image_weight,
        text_weight=text_weight,
    )
    filtered = sort_confidence_hits(
        legacy,
        query_type=query_type,
        top_k=top_k,
        min_confidence=0.0,
        score_gap=1.0,
        max_confidence_drop=1.0,
        max_candidates=max(1, len(legacy)),
        show_low_confidence=show_low_confidence,
        sort_mode="legacy",
    )
    legacy_hits = legacy if filtered is None else filtered.hits
    diversity_enabled = sort_mode_uses_diversity(
        sort_mode,
        legacy_diversity=diversify_results,
    )
    diversity = diversify_search_hits(
        legacy_hits,
        top_k=top_k,
        enabled=diversity_enabled,
    )
    selected = diversity.hits
    if query_type == "tag":
        status = "ok" if selected else "no_reliable_match"
        ranking_mode = "tag_match"
    else:
        status = "legacy_fallback"
        has_metadata = any(
            collection.candidates.metadata_hits for collection in collections
        )
        has_tags = any(collection.candidates.tag_hits for collection in collections)
        ranking_mode = (
            "weighted_rrf_with_metadata"
            if query_type == "image_text" and has_metadata
            else "weighted_rrf"
            if query_type == "image_text"
            else "hybrid_tag_visual_metadata"
            if query_type == "text" and has_tags and has_metadata
            else "hybrid_tag_vector"
            if query_type == "text" and has_tags
            else "visual_metadata"
            if query_type == "text" and has_metadata
            else "distance"
        )
    return FederatedRanking(
        hits=selected,
        status=status,
        candidate_count=(len(legacy) if filtered is None else filtered.candidate_count),
        filtered_count=max(0, len(legacy) - len(selected)),
        ranking_mode=ranking_mode,
        sort_mode=sort_mode,
        ranking_diagnostics={
            **({} if filtered is None else filtered.diagnostics),
            "fallback": True,
            "requested_sort_mode": requested_sort_mode or sort_mode,
            "effective_sort_mode": sort_mode,
            "requested_diversity": diversify_results,
            "effective_diversity": diversity_enabled,
        },
        diversity=diversity.diagnostics(),
    )


def _quality_thresholds(
    collections: list[LibraryCandidateSet],
) -> tuple[float, float, float]:
    if not collections:
        return (
            DEFAULT_MIN_CONFIDENCE,
            DEFAULT_POSSIBLE_THRESHOLD,
            DEFAULT_HIGH_THRESHOLD,
        )
    return (
        max(collection.candidates.minimum_confidence for collection in collections),
        max(collection.candidates.possible_confidence for collection in collections),
        max(collection.candidates.high_confidence for collection in collections),
    )


def _fusion_settings(
    collections: list[LibraryCandidateSet],
) -> tuple[str, dict[str, float]]:
    if not collections:
        return "confidence_v1", {}
    if any(
        collection.candidates.fusion_mode != "confidence_v2"
        for collection in collections
    ):
        return "confidence_v1", {}
    first = collections[0].candidates.fusion_options
    if any(collection.candidates.fusion_options != first for collection in collections):
        return "confidence_v1", {}
    return "confidence_v2", dict(first)


def _quality_score_gap(collections: list[LibraryCandidateSet]) -> float:
    if not collections:
        return DEFAULT_SCORE_GAP
    return min(collection.candidates.score_gap for collection in collections)


def _quality_minimum_score(
    collections: list[LibraryCandidateSet], query_type: str
) -> float | None:
    configured = [
        collection.candidates.minimum_score
        for collection in collections
        if collection.candidates.minimum_score is not None
    ]
    if not configured:
        return None
    if query_type == "image_text":
        return max(configured)
    return min(configured)


def _quality_max_confidence_drop(collections: list[LibraryCandidateSet]) -> float:
    if not collections:
        return DEFAULT_MAX_CONFIDENCE_DROP
    return min(collection.candidates.max_confidence_drop for collection in collections)


def _collection_calibration_settings(
    collections: list[LibraryCandidateSet],
) -> tuple[dict[str, float], dict[str, object]]:
    if not collections:
        return {}, {"configured": False, "mode": "none"}
    modes = {
        collection.candidates.collection_calibration_mode for collection in collections
    }
    if modes == {"none"}:
        return {}, {"configured": False, "mode": "none"}
    if modes != {"null_mean_offset_v1"}:
        return {}, {
            "configured": False,
            "mode": "none",
            "fallback_reason": (
                "collection_calibration_not_configured_identically_for_all_libraries"
            ),
        }
    first = collections[0].candidates.collection_confidence_offsets
    if any(
        collection.candidates.collection_confidence_offsets != first
        for collection in collections[1:]
    ):
        return {}, {
            "configured": False,
            "mode": "none",
            "fallback_reason": (
                "collection_calibration_offsets_not_identical_for_all_libraries"
            ),
        }
    library_ids = [collection.library.library_id for collection in collections]
    offsets = {
        library_id: float(first.get(library_id, 0.0)) for library_id in library_ids
    }
    unknown = [library_id for library_id in library_ids if library_id not in first]
    return offsets, {
        "configured": True,
        "mode": "null_mean_offset_v1",
        "fallback": "identity",
        "offsets": offsets,
        "unknown_library_ids": unknown,
    }


def export_federated_search(
    config: ServiceConfig,
    prepared: PreparedSearch,
    collections: list[LibraryCandidateSet],
    *,
    top_k: int,
    image_weight: float,
    text_weight: float,
    tags: list[str] | None,
    tag_mode: str,
    latency_ms: float = 0.0,
    show_low_confidence: bool = False,
    diversify_results: bool = True,
    sort_mode: SearchSortMode = "confidence",
) -> dict:
    search_learning = load_search_learning(config.config_home_path)
    ranking = rank_federated_hits(
        collections,
        top_k=top_k,
        image_weight=image_weight,
        text_weight=text_weight,
        show_low_confidence=show_low_confidence,
        diversify_results=diversify_results,
        sort_mode=sort_mode,
        search_learning=search_learning if search_learning.configured else None,
    )
    libraries = [collection.library for collection in collections]
    query: dict[str, object] = {
        "library_ids": [library.library_id for library in libraries],
        "search_mode": prepared.search_mode,
        "tags": tags or [],
        "tag_mode": tag_mode,
        "show_low_confidence": show_low_confidence,
        "diversify_results": diversify_results,
        "sort_mode": ranking.sort_mode,
    }
    if prepared.text is not None:
        query["text"] = prepared.text
    if prepared.image_path is not None:
        query["image"] = Path(prepared.image_path).name
    if prepared.query_type == "image_text":
        query["image_weight"] = image_weight
        query["text_weight"] = text_weight
    quality_diagnostics = _quality_diagnostics(
        collections, ranking, show_low_confidence=show_low_confidence
    )
    report = export_results(
        config,
        query_type=prepared.query_type,
        hits=ranking.hits,
        top_k=top_k,
        query=query,
        request_ids=prepared.request_ids,
        usage=prepared.usage,
        resolve_source=lambda hit: Path(str(hit.fields["_source_path"])),
        embedding_sources=prepared.embedding_sources,
        exclude_path=None if prepared.image_path is None else prepared.image_path,
        library_ids=[library.library_id for library in libraries],
        library_names=[library.name for library in libraries],
        mode="federated",
        status=ranking.status,
        candidate_count=ranking.candidate_count,
        filtered_count=ranking.filtered_count,
        latency_ms=latency_ms,
        search_quality=quality_diagnostics,
        ranking_mode=ranking.ranking_mode,
        sort_mode=ranking.sort_mode,
        ranking_diagnostics=ranking.ranking_diagnostics,
        show_low_confidence=show_low_confidence,
        low_confidence_override=ranking.status == "low_confidence_override",
    )
    result = report.to_dict()
    result["search_quality"] = quality_diagnostics
    return result


def _quality_diagnostics(
    collections: list[LibraryCandidateSet],
    ranking: FederatedRanking,
    *,
    show_low_confidence: bool = False,
) -> dict[str, object]:
    configured = bool(collections) and all(
        collection.candidates.quality_configured for collection in collections
    )
    fusion_mode, fusion_options = _fusion_settings(collections)
    _collection_offsets, collection_calibration = _collection_calibration_settings(
        collections
    )
    diagnostics: dict[str, object] = {
        "configured": configured,
        "ranking_mode": ranking.ranking_mode,
        "sort_mode": ranking.sort_mode,
        "sorting": dict(ranking.ranking_diagnostics),
        "score_gap": _quality_score_gap(collections),
        "max_confidence_drop": _quality_max_confidence_drop(collections),
        "source_reward": DEFAULT_SOURCE_REWARD,
        "hybrid_search": {
            "enabled": any(
                bool(collection.candidates.tag_hits) for collection in collections
            ),
            "collections": {
                collection.library.library_id: dict(collection.candidates.hybrid_search)
                for collection in collections
                if collection.candidates.hybrid_search
            },
            "extra_embedding_requests": 0,
        },
        "metadata_search": {
            "enabled": any(
                bool(collection.candidates.metadata_hits) for collection in collections
            ),
            "collections": {
                collection.library.library_id: dict(
                    collection.candidates.metadata_search
                )
                for collection in collections
                if collection.candidates.metadata_search
            },
            "candidate_count": sum(
                len(collection.candidates.metadata_hits) for collection in collections
            ),
            "extra_embedding_requests": 0,
            "calibrated": False,
        },
        "diversity": dict(ranking.diversity),
        "fusion": {"mode": fusion_mode, **fusion_options},
        "collection_calibration": collection_calibration,
        "max_candidates": ranking.ranking_diagnostics.get("candidate_limit"),
        "candidate_k_per_library": {
            collection.library.library_id: collection.candidates.candidate_k
            for collection in collections
        },
        "next_candidate_k_per_library": {
            collection.library.library_id: collection.candidates.next_candidate_k
            for collection in collections
        },
    }
    search_learning = ranking.ranking_diagnostics.get("search_learning")
    if isinstance(search_learning, dict):
        diagnostics["search_learning"] = dict(search_learning)
    if configured:
        minimum, possible, high = _quality_thresholds(collections)
        diagnostics["thresholds"] = {
            "minimum": max(MINIMUM_RESULT_CONFIDENCE, minimum),
            "configured_minimum": minimum,
            "hard_minimum": MINIMUM_RESULT_CONFIDENCE,
            "minimum_score": _quality_minimum_score(
                collections, _query_type(collections)
            ),
            "possible": possible,
            "high": high,
        }
        if fusion_mode != "confidence_v2" and any(
            collection.candidates.fusion_mode == "confidence_v2"
            for collection in collections
        ):
            diagnostics["fusion_fallback_reason"] = (
                "fusion_v2_not_configured_identically_for_all_libraries"
            )
    else:
        diagnostics["fallback_reason"] = (
            "search_quality_not_configured_for_all_libraries"
        )
    if show_low_confidence:
        low_confidence_override = ranking.status == "low_confidence_override"
        diagnostics["low_confidence_requested"] = True
        diagnostics["low_confidence_override"] = low_confidence_override
        diagnostics["filtering_overridden"] = low_confidence_override
    return diagnostics


def _query_type(collections: list[LibraryCandidateSet]) -> str:
    if not collections:
        return "text"
    query_types = {collection.candidates.query_type for collection in collections}
    if len(query_types) != 1:
        raise ValueError("Federated candidate query types do not match.")
    query_type = next(iter(query_types))
    if query_type not in {"text", "image", "image_text", "tag"}:
        raise ValueError(f"Unsupported federated query type: {query_type}")
    return query_type


def _confidence_candidates(
    collections: list[LibraryCandidateSet], query_type: str
) -> list[SearchHit]:
    if query_type in {"image", "tag"}:
        return [
            replace(
                _decorate(collection.library, value),
                rank_source=cast(RankSource, query_type),
            )
            for collection in collections
            for value in collection.candidates.hits
        ]
    if query_type == "text":
        return [
            replace(
                _decorate(collection.library, value),
                rank_source=cast(RankSource, source),
            )
            for collection in collections
            for source, values in (
                ("text", collection.candidates.hits),
                ("tag", collection.candidates.tag_hits),
            )
            for value in values
        ]
    return [
        replace(
            _decorate(collection.library, value),
            rank_source=cast(RankSource, source),
        )
        for collection in collections
        for source, values in (
            ("image", collection.candidates.image_hits),
            ("text", collection.candidates.text_hits),
        )
        for value in values
    ]


def _fuse_combined(
    collections: list[LibraryCandidateSet],
    image_weight: float,
    text_weight: float,
    rank_constant: int = 60,
) -> list[SearchHit]:
    del rank_constant  # The shared fusion contract uses RRF_RANK_CONSTANT.
    hits, _diagnostics = fuse_combined_metadata_hits(
        _global_channel_hits(collections, "image_hits", "image"),
        _global_channel_hits(collections, "text_hits", "text"),
        _global_channel_hits(collections, "metadata_hits", "metadata"),
        image_weight=image_weight,
        text_weight=text_weight,
    )
    return hits


def _global_channel_hits(
    collections: list[LibraryCandidateSet],
    attribute: str,
    source: RankSource,
) -> list[SearchHit]:
    """Decorate and globally rerank one comparable channel across Collections."""

    values = [
        _decorate(collection.library, item)
        for collection in collections
        for item in getattr(collection.candidates, attribute)
    ]
    values.sort(
        key=lambda item: (
            item.distance,
            str(item.fields.get("library_id") or ""),
            item.doc_id,
        )
    )
    seen: set[tuple[str, str]] = set()
    results: list[SearchHit] = []
    for value in values:
        key = (str(value.fields.get("library_id") or ""), value.doc_id)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            replace(
                value,
                rank=len(results) + 1,
                rank_source=source,
            )
        )
    return results


def _decorate(library: LibraryDefinition, value: ResolvedSearchHit) -> SearchHit:
    return replace(
        value.hit,
        fields={
            **value.hit.fields,
            "library_id": library.library_id,
            "library_name": library.name,
            "_source_path": value.source_path,
        },
    )


def _deduplicate_and_rank(values: list[SearchHit]) -> list[SearchHit]:
    seen_hashes: set[str] = set()
    seen_documents: set[tuple[str, str]] = set()
    results: list[SearchHit] = []
    for value in values:
        library_id = str(value.fields.get("library_id") or "")
        document_key = (library_id, value.doc_id)
        if document_key in seen_documents:
            continue
        seen_documents.add(document_key)
        content_hash = str(value.fields.get("sha256") or "")
        if content_hash and content_hash in seen_hashes:
            continue
        if content_hash:
            seen_hashes.add(content_hash)
        results.append(replace(value, rank=len(results) + 1))
    return results
