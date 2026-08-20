from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .learning_ranker import RankingInput
from .models import SearchHit, classify_match_state
from .search_features import (
    SearchFeatureError,
    SearchFeatures,
    TagMatchEvidence,
    extract_search_features,
)
from .search_learning_config import SearchLearningBundle


@dataclass(frozen=True, slots=True)
class SearchLearningApplication:
    hits: list[SearchHit]
    diagnostics: dict[str, object]


def apply_search_learning(
    hits: Sequence[SearchHit],
    *,
    bundle: SearchLearningBundle,
    query_type: str,
    default_library_id: str = "local",
    collection_sizes: Mapping[str, int] | None = None,
) -> SearchLearningApplication:
    """Calibrate and optionally rerank one already-fused candidate batch.

    All failures are converted to per-batch fallback diagnostics.  The caller's
    order and score contract remain usable even if a feature row, calibration,
    or learned model is damaged.
    """

    source = list(hits)
    if not source:
        return SearchLearningApplication(
            [],
            {
                **bundle.diagnostics(),
                "candidate_count": 0,
                "applied": False,
            },
        )
    sizes = dict(collection_sizes or {})
    calibrated: list[SearchHit] = []
    features: list[SearchFeatures] = []
    ranking_inputs: list[RankingInput] = []
    calibration_diagnostics: list[dict[str, object]] = []
    feature_failures = 0

    for index, hit in enumerate(source, 1):
        library_id = str(hit.fields.get("library_id") or default_library_id).strip()
        library_id = library_id or "local"
        base_confidence = float(
            hit.ranking_confidence
            if hit.ranking_confidence is not None
            else hit.confidence
            if hit.confidence is not None
            else 0.0
        )
        decision = bundle.calibrations.calibrate(
            base_confidence,
            query_type=query_type,
            collection_id=library_id,
        )
        calibration_diagnostics.append(decision.diagnostics())
        calibrated_threshold = (
            decision.effective_minimum_confidence
            if bundle.calibrations.configured
            else None
        )
        updated = replace(
            hit,
            confidence=decision.confidence,
            match_state=classify_match_state(decision.confidence),
            calibrated_minimum_confidence=calibrated_threshold,
            calibration_version=decision.calibration_version,
            calibration_scope=decision.scope,
            calibration_fallback=decision.fallback,
        )
        try:
            feature = extract_search_features(
                updated,
                query_type=query_type,
                tag_evidence=_tag_evidence(updated.fields, updated.matched_tags),
                collection_rank=hit.rank or index,
                collection_size=max(0, int(sizes.get(library_id, 0))),
                duplicate_group_size=_positive_int(
                    updated.fields.get("duplicate_group_size"), default=1
                ),
                identity_match=_unit_signal(updated.fields.get("identity_match")),
                work_match=_unit_signal(updated.fields.get("work_match")),
                action_match=_unit_signal(updated.fields.get("action_match")),
                expression_match=_unit_signal(updated.fields.get("expression_match")),
                scene_match=_unit_signal(updated.fields.get("scene_match")),
                vector_raw_score=hit.distance,
                vector_confidence=decision.confidence,
                image_text_agreement=hit.rank_agreement,
            )
        except (ArithmeticError, SearchFeatureError, TypeError, ValueError):
            feature_failures += 1
            # A conservative vector-only row keeps the batch rankable while
            # retaining an explicit fallback count in diagnostics.
            feature = extract_search_features(
                updated,
                query_type=query_type,
                collection_rank=hit.rank or index,
                collection_size=max(0, int(sizes.get(library_id, 0))),
                vector_raw_score=max(0.0, float(hit.distance)),
                vector_confidence=decision.confidence,
            )
        calibrated.append(updated)
        features.append(feature)
        ranking_inputs.append(
            RankingInput(
                candidate_id=f"{hit.doc_id}#{index}",
                library_id=library_id,
                features=feature,
                fallback_score=decision.confidence,
                fallback_rank=index,
            )
        )

    ranker = bundle.build_ranker()
    ranking = ranker.rank(ranking_inputs, shadow_mode=bundle.shadow_mode)
    by_candidate = {
        item.candidate_id: (hit, feature)
        for item, hit, feature in zip(ranking_inputs, calibrated, features, strict=True)
    }
    learned_hits: list[SearchHit] = []
    for prediction in ranking.predictions:
        hit, feature = by_candidate[prediction.candidate_id]
        learned_hits.append(
            replace(
                hit,
                ranking_confidence=(
                    prediction.ranking_score
                    if ranking.applied
                    else hit.ranking_confidence
                ),
                ranking_model_version=prediction.ranking_model_version,
                ranking_score=prediction.ranking_score,
                feature_schema_version=prediction.feature_schema_version,
                ranking_fallback=prediction.ranking_fallback,
                ranking_fallback_reason=prediction.ranking_fallback_reason,
                search_features=feature.to_dict(),
            )
        )
    scopes = sorted(
        {str(item["calibration_scope"]) for item in calibration_diagnostics}
    )
    methods = sorted(
        {str(item["calibration_method"]) for item in calibration_diagnostics}
    )
    return SearchLearningApplication(
        learned_hits,
        {
            **bundle.diagnostics(),
            **ranking.diagnostics(),
            "calibration_scopes": scopes,
            "calibration_methods": methods,
            "calibration_fallback_count": sum(
                bool(item["calibration_fallback"]) for item in calibration_diagnostics
            ),
            "feature_fallback_count": feature_failures,
        },
    )


def _tag_evidence(
    fields: Mapping[str, Any], matched_tags: Sequence[str]
) -> TagMatchEvidence:
    raw = fields.get("_tag_evidence")
    if not isinstance(raw, Mapping):
        return TagMatchEvidence(
            vector=frozenset(matched_tags),
            query_tag_count=len(matched_tags),
        )
    return TagMatchEvidence.from_mapping(raw, query_tag_count=len(matched_tags))


def _unit_signal(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    return 0.0


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
