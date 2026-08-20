# Direct execution bootstraps the repository root before importing runtime modules.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from image_vector_service.models import normalize_cosine_distance, normalize_rrf_score
from image_vector_service.rank_fusion import (
    DEFAULT_SCORE_GAP,
    MINIMUM_RESULT_CONFIDENCE,
)
from image_vector_service.search_quality import (
    FUSION_V1,
    FUSION_V2,
    FUSION_V2_DEFAULTS,
    FUSION_V2_RANGES,
)

evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)
dataset_split = importlib.import_module(
    "tests.search_quality.split" if __package__ else "split"
)
collection_calibration = importlib.import_module(
    "tests.search_quality.collection_calibration"
    if __package__
    else "collection_calibration"
)

CONFIG_SCHEMA_VERSION = 2
SUPPORTED_CONFIG_SCHEMA_VERSIONS = frozenset({1, 2})
DEFAULT_MAX_FALSE_POSITIVE_RATE = 0.10
DEFAULT_MAX_RECALL_DROP = 0.03
DEFAULT_MAX_CONFIDENCE_DROP = 1.0
MAX_THRESHOLD_CANDIDATES = 128
MAX_SCORE_GAP_CANDIDATES = 16
MAX_CONFIDENCE_DROP_CANDIDATES = 16
MAX_PARAMETER_COMBINATIONS = (
    MAX_THRESHOLD_CANDIDATES * MAX_SCORE_GAP_CANDIDATES * MAX_CONFIDENCE_DROP_CANDIDATES
)
SELECTION_EMPIRICAL = "empirical"
SELECTION_ROBUST_INNER_CV = "robust_inner_cv"
SUPPORTED_SELECTION_STRATEGIES = frozenset(
    {SELECTION_EMPIRICAL, SELECTION_ROBUST_INNER_CV}
)
DEFAULT_SELECTION_STRATEGY = SELECTION_EMPIRICAL
ROBUST_INNER_FOLD_SEED = "zvec-search-quality-inner-cv-v1"
ROBUST_MAX_INNER_FOLDS = 5
ROBUST_THRESHOLD_MARGIN_FRACTION = 0.05
ROBUST_SCORE_GAP = DEFAULT_SCORE_GAP
ROBUST_MAX_CONFIDENCE_DROP = 0.10


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: str | Path, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _score_semantics(run: dict[str, Any], mode: str) -> str:
    semantics = run.get("score_semantics", {})
    if not isinstance(semantics, dict):
        raise ValueError("run score_semantics must be an object")
    value = semantics.get(mode)
    if value not in {"lower_is_better", "higher_is_better"}:
        raise ValueError(
            f"run score_semantics must define lower/higher behavior for {mode}"
        )
    return str(value)


def _score_field(run: dict[str, Any], mode: str) -> str:
    score_fields = run.get("score_fields")
    if score_fields is None:
        return "score"
    if not isinstance(score_fields, dict):
        raise ValueError("run score_fields must be an object")
    value = score_fields.get(mode)
    if value not in {"score", "raw_score", "normalized_score", "confidence"}:
        raise ValueError(f"run score_fields has no supported field for {mode}")
    return str(value)


def _result_score(
    result: dict[str, Any], case_id: str, score_field: str = "score"
) -> float:
    value = result.get(score_field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"run case {case_id}: every result needs a numeric {score_field}"
        )
    score = float(value)
    if not math.isfinite(score):
        raise ValueError(f"run case {case_id}: result {score_field} must be finite")
    return score


def _passes(score: float, threshold: float, semantics: str) -> bool:
    if semantics == "lower_is_better":
        return score <= threshold
    return score >= threshold


def _offline_confidence_sort_key(
    item: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
    confidence: float,
    *,
    mode: str,
) -> tuple[float, float, int, str, str]:
    """Mirror the stable production confidence ordering for captured results."""

    raw_score = result.get("raw_score")
    if (
        isinstance(raw_score, bool)
        or not isinstance(raw_score, (int, float))
        or not math.isfinite(float(raw_score))
    ):
        raw_score_key = math.inf
    else:
        raw_score_key = float(raw_score)
        if mode == "combined":
            raw_score_key = -raw_score_key
    source_rank = result.get("rank")
    if (
        isinstance(source_rank, bool)
        or not isinstance(source_rank, int)
        or source_rank <= 0
    ):
        source_rank = 2**31 - 1
    library_id = collection_calibration.result_library_id(item, case, result) or ""
    return (
        -confidence,
        raw_score_key,
        source_rank,
        library_id,
        str(result.get("image_id") or ""),
    )


def _filtered_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    threshold: float,
    semantics: str,
    *,
    mode: str,
    score_field: str,
    score_gap: float,
    max_confidence_drop: float = DEFAULT_MAX_CONFIDENCE_DROP,
    collection_calibration_config: dict[str, Any] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    filtered = []
    collection_offsets = collection_calibration.offsets_for(
        collection_calibration_config,
        mode,
    )
    production = _production_threshold(
        mode,
        {
            "value": threshold,
            "score_gap": score_gap,
            "max_confidence_drop": max_confidence_drop,
        },
        score_field,
    )
    effective_minimum_confidence = max(
        MINIMUM_RESULT_CONFIDENCE,
        production["minimum_confidence"],
    )
    production_score_field = (
        "raw_score"
        if score_field in {"normalized_score", "confidence"}
        else score_field
    )
    production_semantics = (
        "higher_is_better"
        if score_field in {"normalized_score", "confidence"} and mode == "combined"
        else "lower_is_better"
        if score_field in {"normalized_score", "confidence"}
        else semantics
    )
    for item, case in pairs:
        next_case = dict(case)
        passing: list[tuple[dict[str, Any], float]] = []
        for result in case["results"]:
            # Confidence/normalized captures deploy minimum_score=0 only as a
            # compatibility field; runtime filtering is performed on the
            # actual raw score plus Collection-adjusted ranking confidence.
            # Applying the original unadjusted confidence threshold here would
            # incorrectly discard candidates that a positive null-calibration
            # offset legitimately rescues.
            # Older confidence-v2 captures predate additive raw_score output.
            # Their deployed minimum_score is the documented 0.0 compatibility
            # value, so absence means "no raw gate" rather than invalid data.
            missing_legacy_raw_score = (
                score_field in {"normalized_score", "confidence"}
                and "raw_score" not in result
            )
            if not missing_legacy_raw_score and not _passes(
                _result_score(result, str(case["id"]), production_score_field),
                production["minimum_score"],
                production_semantics,
            ):
                continue
            confidence = _ranking_confidence(
                item,
                case,
                result,
                mode=mode,
                score_field=score_field,
                configuration=collection_calibration_config,
                collection_offsets=collection_offsets,
            )
            # Mirror rank_fusion.sort_confidence_hits: every normal result must
            # satisfy both the invariant 20% floor and the configured floor.
            if confidence < effective_minimum_confidence:
                continue
            passing.append((result, confidence))
        passing.sort(
            key=lambda value: _offline_confidence_sort_key(
                item,
                case,
                value[0],
                value[1],
                mode=mode,
            )
        )
        results: list[dict[str, Any]] = []
        top_confidence: float | None = None
        previous_confidence: float | None = None
        for result, confidence in passing:
            if top_confidence is None:
                top_confidence = confidence
            elif top_confidence - confidence > max_confidence_drop + 1e-12:
                break
            if (
                previous_confidence is not None
                and previous_confidence - confidence >= score_gap
            ):
                break
            results.append({**result, "rank": len(results) + 1})
            previous_confidence = confidence
        next_case["results"] = results
        filtered.append((item, next_case))
    return filtered


def _score_confidence(mode: str, score_field: str, score: float) -> float:
    if score_field in {"normalized_score", "confidence"}:
        return min(1.0, max(0.0, score))
    if mode == "combined":
        return normalize_rrf_score(score)
    return normalize_cosine_distance(score)


def _ranking_confidence(
    item: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    mode: str,
    score_field: str,
    configuration: dict[str, Any] | None,
    collection_offsets: dict[str, float] | None = None,
) -> float:
    explicit = result.get("ranking_confidence")
    if (
        configuration is not None
        and not isinstance(explicit, bool)
        and isinstance(explicit, (int, float))
        and math.isfinite(float(explicit))
    ):
        return min(1.0, max(0.0, float(explicit)))
    fallback = _score_confidence(
        mode,
        score_field,
        _result_score(result, str(case["id"]), score_field),
    )
    offsets = (
        collection_offsets
        if collection_offsets is not None
        else collection_calibration.offsets_for(configuration, mode)
    )
    library_id = collection_calibration.result_library_id(item, case, result)
    offset = offsets.get(library_id or "", 0.0)
    return min(1.0, max(0.0, fallback + offset))


def _sample_candidates(
    values: set[float],
    maximum: int,
    *,
    required: set[float] | None = None,
) -> list[float]:
    candidates = sorted(values)
    if len(candidates) <= maximum:
        return candidates

    required_values = sorted((required or set()).intersection(values))
    if len(required_values) >= maximum:
        candidates = required_values
        required_values = []

    slots = maximum - len(required_values)
    if slots <= 0:
        return required_values[:maximum]

    remaining = [value for value in candidates if value not in required_values]
    if len(remaining) <= slots:
        return sorted({*required_values, *remaining})
    if slots == 1:
        sampled = [remaining[-1]]
    else:
        last = len(remaining) - 1
        indexes = {round(index * last / (slots - 1)) for index in range(slots)}
        sampled = [remaining[index] for index in sorted(indexes)]
    return sorted({*required_values, *sampled})


def _candidate_score_gaps(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    score_field: str,
    collection_calibration_config: dict[str, Any] | None = None,
) -> list[float]:
    values = {DEFAULT_SCORE_GAP, 1.0}
    collection_offsets = collection_calibration.offsets_for(
        collection_calibration_config,
        mode,
    )
    for item, case in pairs:
        confidences = sorted(
            (
                _ranking_confidence(
                    item,
                    case,
                    result,
                    mode=mode,
                    score_field=score_field,
                    configuration=collection_calibration_config,
                    collection_offsets=collection_offsets,
                )
                for result in case["results"]
            ),
            reverse=True,
        )
        values.update(
            previous - current
            for previous, current in zip(confidences, confidences[1:], strict=False)
            if 0.0 < previous - current <= 1.0
        )
    return _sample_candidates(
        values,
        MAX_SCORE_GAP_CANDIDATES,
        required={DEFAULT_SCORE_GAP, 1.0},
    )


def _candidate_max_confidence_drops(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    score_field: str,
    collection_calibration_config: dict[str, Any] | None = None,
) -> list[float]:
    values = {0.0, DEFAULT_MAX_CONFIDENCE_DROP}
    required = {0.0, DEFAULT_MAX_CONFIDENCE_DROP}
    collection_offsets = collection_calibration.offsets_for(
        collection_calibration_config,
        mode,
    )
    for item, case in pairs:
        confidences = [
            (
                result,
                _ranking_confidence(
                    item,
                    case,
                    result,
                    mode=mode,
                    score_field=score_field,
                    configuration=collection_calibration_config,
                    collection_offsets=collection_offsets,
                ),
            )
            for result in case["results"]
        ]
        confidences.sort(key=lambda pair: pair[1], reverse=True)
        if not confidences:
            continue
        top_confidence = confidences[0][1]
        relevant_ids = {relevant["image_id"] for relevant in item["relevant_images"]}
        for result, confidence in confidences:
            drop = min(1.0, max(0.0, top_confidence - confidence))
            values.add(drop)
            if result["image_id"] in relevant_ids:
                required.add(drop)
    return _sample_candidates(
        values,
        MAX_CONFIDENCE_DROP_CANDIDATES,
        required=required,
    )


def _baseline_mode_metrics(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    baseline_report: dict[str, Any] | None,
) -> dict[str, Any]:
    if baseline_report is None:
        return evaluate._metric_block(pairs)
    if (
        baseline_report.get("schema_version") != 1
        or baseline_report.get("kind") != "zvec-search-quality-evaluation"
    ):
        raise ValueError("baseline must be a completed evaluation report")
    by_mode = baseline_report.get("by_mode")
    if not isinstance(by_mode, dict) or not isinstance(by_mode.get(mode), dict):
        raise ValueError(f"baseline report has no metrics for mode {mode}")
    metrics = by_mode[mode]
    current_counts = evaluate._metric_block(pairs)
    for field in ("answerable_cases", "no_answer_cases"):
        if metrics.get(field) != current_counts[field]:
            raise ValueError(
                f"baseline report {mode} coverage does not match calibration cases"
            )
    return metrics


def _validate_baseline_identity(
    dataset: dict[str, Any],
    coverage: dict[str, int],
    baseline_report: dict[str, Any] | None,
) -> None:
    if baseline_report is None:
        return
    if (
        baseline_report.get("schema_version") != evaluate.REPORT_SCHEMA_VERSION
        or baseline_report.get("kind") != "zvec-search-quality-evaluation"
    ):
        raise ValueError("baseline must be a completed evaluation report")
    identity = baseline_report.get("dataset")
    if not isinstance(identity, dict):
        raise ValueError("baseline report has no dataset identity")
    fingerprint = identity.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(
            "baseline report has no dataset fingerprint; regenerate it before "
            "calibration"
        )
    if identity.get("fingerprint_algorithm") != evaluate.DATASET_FINGERPRINT_ALGORITHM:
        raise ValueError("baseline report uses an unsupported fingerprint algorithm")
    if identity.get("name") != dataset.get("name"):
        raise ValueError("baseline report uses a different dataset")
    if fingerprint != evaluate.dataset_fingerprint(dataset):
        raise ValueError("baseline report dataset fingerprint does not match")
    if baseline_report.get("coverage") != coverage:
        raise ValueError("baseline report coverage does not match calibration coverage")


def _candidate_thresholds(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    score_field: str,
    semantics: str,
) -> list[float]:
    values = {
        _result_score(result, str(case["id"]), score_field)
        for _item, case in pairs
        for result in case["results"]
    }
    if not values:
        raise ValueError("calibration requires scored candidate results")
    boundary = min(values) if semantics == "lower_is_better" else max(values)
    reject_all = math.nextafter(
        boundary,
        -math.inf if semantics == "lower_is_better" else math.inf,
    )
    required: set[float] = set()
    if math.isfinite(reject_all):
        values.add(reject_all)
        required.add(reject_all)
    return _sample_candidates(
        values,
        MAX_THRESHOLD_CANDIDATES,
        required=required,
    )


def _select_threshold(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    semantics: str,
    score_field: str,
    baseline_metrics: dict[str, Any],
    *,
    max_false_positive_rate: float,
    max_recall_drop: float,
    selection_strategy: str = DEFAULT_SELECTION_STRATEGY,
    collection_calibration_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if selection_strategy not in SUPPORTED_SELECTION_STRATEGIES:
        raise ValueError(
            f"unsupported calibration selection strategy: {selection_strategy}"
        )
    baseline_recall = baseline_metrics.get("recall_at_5")
    if not isinstance(baseline_recall, (int, float)):
        raise ValueError(f"mode {mode} needs answerable baseline cases")
    if baseline_metrics.get("no_answer_false_return_rate") is None:
        raise ValueError(f"mode {mode} needs at least one annotated no-answer case")

    feasible: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    candidates = _candidate_thresholds(pairs, score_field, semantics)
    if selection_strategy == SELECTION_ROBUST_INNER_CV:
        # With only a few dozen cases, jointly learning three discontinuous
        # cut-offs is unstable. Keep the two list-shape controls fixed and use
        # the annotated cases to learn only the score threshold.
        score_gaps = [ROBUST_SCORE_GAP]
        max_confidence_drops = [ROBUST_MAX_CONFIDENCE_DROP]
    else:
        score_gaps = _candidate_score_gaps(
            mode,
            pairs,
            score_field,
            collection_calibration_config,
        )
        max_confidence_drops = _candidate_max_confidence_drops(
            mode,
            pairs,
            score_field,
            collection_calibration_config,
        )
    parameter_combinations = (
        len(candidates) * len(score_gaps) * len(max_confidence_drops)
    )
    if parameter_combinations > MAX_PARAMETER_COMBINATIONS:
        raise AssertionError("calibration candidate sampling exceeded its budget")
    for threshold in candidates:
        for score_gap in score_gaps:
            for max_confidence_drop in max_confidence_drops:
                metrics = evaluate._metric_block(
                    _filtered_pairs(
                        pairs,
                        threshold,
                        semantics,
                        mode=mode,
                        score_field=score_field,
                        score_gap=score_gap,
                        max_confidence_drop=max_confidence_drop,
                        collection_calibration_config=(collection_calibration_config),
                    )
                )
                precision = metrics["strict_precision_at_5"]
                returned_precision = metrics["precision_at_5"]
                recall = metrics["recall_at_5"]
                mrr = metrics["mrr"]
                false_positive_rate = metrics["no_answer_false_return_rate"]
                average_result_count = metrics["average_result_count"]
                if not all(
                    isinstance(value, (int, float))
                    for value in (
                        precision,
                        returned_precision,
                        recall,
                        mrr,
                        false_positive_rate,
                        average_result_count,
                    )
                ):
                    continue
                recall_drop = max(0.0, float(baseline_recall) - float(recall))
                if float(false_positive_rate) >= max_false_positive_rate:
                    continue
                if recall_drop > max_recall_drop + 1e-12:
                    continue
                # Do not emit an abstain-everywhere configuration when the captured
                # pool contains no correctly retrieved item at this operating point.
                if float(precision) <= 0.0:
                    continue
                if selection_strategy == SELECTION_ROBUST_INNER_CV:
                    # Avoid optimizing selective precision by returning an
                    # increasingly tiny list. Once standard P@5, recall and MRR
                    # tie, prefer fewer returned candidates and the stricter
                    # threshold. A small, explicit recall margin is applied
                    # below after the negative boundary is known.
                    conservative_threshold = (
                        -threshold if semantics == "lower_is_better" else threshold
                    )
                    objective: tuple[float, ...] = (
                        float(precision),
                        float(recall),
                        float(mrr),
                        -float(false_positive_rate),
                        -float(average_result_count),
                        conservative_threshold,
                        float(returned_precision),
                    )
                else:
                    less_aggressive = (
                        threshold if semantics == "lower_is_better" else -threshold
                    )
                    objective = (
                        float(precision),
                        float(recall),
                        float(mrr),
                        float(returned_precision),
                        -float(false_positive_rate),
                        -float(average_result_count),
                        max_confidence_drop,
                        score_gap,
                        less_aggressive,
                    )
                feasible.append(
                    (
                        objective,
                        {
                            "value": threshold,
                            "score_gap": score_gap,
                            "max_confidence_drop": max_confidence_drop,
                            "operator": (
                                "<=" if semantics == "lower_is_better" else ">="
                            ),
                            "score_semantics": semantics,
                            "metrics": metrics,
                            "recall_at_5_drop": recall_drop,
                            "baseline": {
                                "strict_precision_at_5": baseline_metrics.get(
                                    "strict_precision_at_5"
                                ),
                                "precision_at_5": baseline_metrics.get(
                                    "precision_at_5"
                                ),
                                "recall_at_5": baseline_recall,
                                "no_answer_false_return_rate": baseline_metrics.get(
                                    "no_answer_false_return_rate"
                                ),
                            },
                            "evaluated_thresholds": len(candidates),
                            "evaluated_score_gaps": len(score_gaps),
                            "evaluated_max_confidence_drops": len(max_confidence_drops),
                            "evaluated_parameter_pairs": len(candidates)
                            * len(score_gaps),
                            "evaluated_parameter_combinations": (
                                parameter_combinations
                            ),
                            "selection_strategy": selection_strategy,
                        },
                    )
                )
    if not feasible:
        raise ValueError(
            f"No {mode} threshold satisfies false-positive < "
            f"{max_false_positive_rate:.3f} and Recall@5 drop <= "
            f"{max_recall_drop:.3f}."
        )
    return max(feasible, key=lambda item: item[0])[1]


def _no_answer_boundary(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    semantics: str,
    score_field: str,
) -> float:
    best_scores = []
    for item, case in pairs:
        if item["query_type"] != "no-answer":
            continue
        scores = [
            _result_score(result, str(case["id"]), score_field)
            for result in case["results"]
        ]
        if not scores:
            continue
        best_scores.append(
            min(scores) if semantics == "lower_is_better" else max(scores)
        )
    if not best_scores:
        raise ValueError(
            "robust calibration requires scored candidates for no-answer cases"
        )
    return min(best_scores) if semantics == "lower_is_better" else max(best_scores)


def _apply_robust_threshold_margin(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    selected: dict[str, Any],
    semantics: str,
    score_field: str,
    baseline_metrics: dict[str, Any],
    *,
    max_false_positive_rate: float,
    max_recall_drop: float,
    collection_calibration_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    original = float(selected["value"])
    boundary = _no_answer_boundary(pairs, semantics, score_field)
    if semantics == "lower_is_better":
        separated = original < boundary
    else:
        separated = original > boundary
    if not separated:
        raise ValueError(
            f"robust {mode} threshold has no separation from no-answer candidates"
        )

    # Use only five percent of the observed positive-to-negative separation as
    # recall headroom. The remaining 95% is deliberately reserved as a
    # rejection buffer instead of placing the threshold on a sampled boundary.
    adjusted_value = original + ROBUST_THRESHOLD_MARGIN_FRACTION * (boundary - original)
    adjusted = copy.deepcopy(selected)
    adjusted["value"] = adjusted_value
    metrics = evaluate._metric_block(
        _filtered_pairs(
            pairs,
            adjusted_value,
            semantics,
            mode=mode,
            score_field=score_field,
            score_gap=float(adjusted["score_gap"]),
            max_confidence_drop=float(adjusted["max_confidence_drop"]),
            collection_calibration_config=collection_calibration_config,
        )
    )
    baseline_recall = baseline_metrics.get("recall_at_5")
    recall = metrics.get("recall_at_5")
    false_positive_rate = metrics.get("no_answer_false_return_rate")
    if not isinstance(baseline_recall, (int, float)) or not isinstance(
        recall, (int, float)
    ):
        raise ValueError(f"robust {mode} calibration needs answerable cases")
    if not isinstance(false_positive_rate, (int, float)):
        raise ValueError(f"robust {mode} calibration needs no-answer cases")
    recall_drop = max(0.0, float(baseline_recall) - float(recall))
    if float(false_positive_rate) >= max_false_positive_rate:
        raise ValueError(
            f"robust {mode} threshold margin violates false-positive limit"
        )
    if recall_drop > max_recall_drop + 1e-12:
        raise ValueError(f"robust {mode} threshold margin violates recall limit")
    adjusted["metrics"] = metrics
    adjusted["recall_at_5_drop"] = recall_drop
    adjusted["threshold_margin"] = {
        "source_value": original,
        "no_answer_boundary": boundary,
        "fraction_toward_no_answer_boundary": ROBUST_THRESHOLD_MARGIN_FRACTION,
        "reserved_rejection_fraction": 1.0 - ROBUST_THRESHOLD_MARGIN_FRACTION,
    }
    return adjusted


def _inner_fold_rank(mode: str, state: str, case_id: str) -> str:
    value = "\0".join((ROBUST_INNER_FOLD_SEED, mode, state, case_id))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stratified_inner_folds(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[list[tuple[dict[str, Any], dict[str, Any]]]]:
    answerable = [pair for pair in pairs if pair[0]["query_type"] != "no-answer"]
    no_answer = [pair for pair in pairs if pair[0]["query_type"] == "no-answer"]
    fold_count = min(
        ROBUST_MAX_INNER_FOLDS,
        len(answerable),
        len(no_answer),
    )
    if fold_count < 2:
        raise ValueError(
            f"robust {mode} calibration needs at least two answerable and two "
            "no-answer cases for inner cross-validation"
        )
    folds: list[list[tuple[dict[str, Any], dict[str, Any]]]] = [
        [] for _ in range(fold_count)
    ]
    for state, group in (("answerable", answerable), ("no-answer", no_answer)):
        ordered = sorted(
            group,
            key=lambda pair: _inner_fold_rank(
                mode,
                state,
                str(pair[0]["id"]),
            ),
        )
        for index, pair in enumerate(ordered):
            folds[index % fold_count].append(pair)
    return folds


def _robust_inner_cross_validation(
    mode: str,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    semantics: str,
    score_field: str,
    *,
    max_false_positive_rate: float,
    max_recall_drop: float,
    collection_calibration_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    folds = _stratified_inner_folds(mode, pairs)
    out_of_fold: list[tuple[dict[str, Any], dict[str, Any]]] = []
    fold_reports = []
    selected_values = []
    for index, holdout in enumerate(folds, start=1):
        holdout_ids = {str(item["id"]) for item, _case in holdout}
        training = [pair for pair in pairs if str(pair[0]["id"]) not in holdout_ids]
        training_baseline = evaluate._metric_block(training)
        selected = _select_threshold(
            mode,
            training,
            semantics,
            score_field,
            training_baseline,
            max_false_positive_rate=max_false_positive_rate,
            max_recall_drop=max_recall_drop,
            selection_strategy=SELECTION_ROBUST_INNER_CV,
            collection_calibration_config=collection_calibration_config,
        )
        selected = _apply_robust_threshold_margin(
            mode,
            training,
            selected,
            semantics,
            score_field,
            training_baseline,
            max_false_positive_rate=max_false_positive_rate,
            max_recall_drop=max_recall_drop,
            collection_calibration_config=collection_calibration_config,
        )
        selected_values.append(float(selected["value"]))
        filtered_holdout = _filtered_pairs(
            holdout,
            float(selected["value"]),
            semantics,
            mode=mode,
            score_field=score_field,
            score_gap=float(selected["score_gap"]),
            max_confidence_drop=float(selected["max_confidence_drop"]),
            collection_calibration_config=collection_calibration_config,
        )
        out_of_fold.extend(filtered_holdout)
        holdout_baseline = evaluate._metric_block(holdout)
        holdout_metrics = evaluate._metric_block(filtered_holdout)
        holdout_recall_drop = max(
            0.0,
            float(holdout_baseline["recall_at_5"])
            - float(holdout_metrics["recall_at_5"]),
        )
        fold_reports.append(
            {
                "fold": index,
                "training_cases": len(training),
                "holdout_cases": len(holdout),
                "holdout_answerable_cases": holdout_metrics["answerable_cases"],
                "holdout_no_answer_cases": holdout_metrics["no_answer_cases"],
                "selected": {
                    "value": selected["value"],
                    "score_gap": selected["score_gap"],
                    "max_confidence_drop": selected["max_confidence_drop"],
                    "threshold_margin": selected["threshold_margin"],
                },
                "holdout_metrics": holdout_metrics,
                "holdout_recall_at_5_drop": holdout_recall_drop,
            }
        )

    baseline = evaluate._metric_block(pairs)
    metrics = evaluate._metric_block(out_of_fold)
    baseline_recall = baseline.get("recall_at_5")
    recall = metrics.get("recall_at_5")
    false_positive_rate = metrics.get("no_answer_false_return_rate")
    if not isinstance(baseline_recall, (int, float)) or not isinstance(
        recall, (int, float)
    ):
        raise ValueError(f"robust {mode} inner CV needs answerable cases")
    if not isinstance(false_positive_rate, (int, float)):
        raise ValueError(f"robust {mode} inner CV needs no-answer cases")
    recall_drop = max(0.0, float(baseline_recall) - float(recall))
    passed = (
        float(false_positive_rate) < max_false_positive_rate
        and recall_drop <= max_recall_drop + 1e-12
    )
    report = {
        "method": "deterministic_stratified_inner_k_fold",
        "seed": ROBUST_INNER_FOLD_SEED,
        "fold_count": len(folds),
        "out_of_fold_metrics": metrics,
        "out_of_fold_recall_at_5_drop": recall_drop,
        "constraints_passed": passed,
        "selected_value_stability": {
            "minimum": min(selected_values),
            "maximum": max(selected_values),
            "span": max(selected_values) - min(selected_values),
        },
        "folds": fold_reports,
        "api_requests_added": 0,
        "limitation": (
            "Inner-CV metrics measure calibration-sample stability only; the small "
            "no-answer count is not a population-level false-positive guarantee."
        ),
    }
    if not passed:
        raise ValueError(
            f"Robust {mode} inner cross-validation failed: false-positive "
            f"rate={float(false_positive_rate):.6f}, Recall@5 "
            f"drop={recall_drop:.6f}."
        )
    return report


def _production_threshold(
    mode: str,
    selected: dict[str, Any],
    score_field: str,
) -> dict[str, float]:
    minimum_score = float(selected["value"])
    if score_field in {"normalized_score", "confidence"}:
        return {
            "minimum_score": 0.0,
            "minimum_confidence": min(1.0, max(0.0, minimum_score)),
            "score_gap": float(selected["score_gap"]),
            "max_confidence_drop": float(selected["max_confidence_drop"]),
        }
    minimum_confidence = (
        normalize_rrf_score(minimum_score)
        if mode == "combined"
        else normalize_cosine_distance(minimum_score)
    )
    return {
        "minimum_score": minimum_score,
        "minimum_confidence": minimum_confidence,
        "score_gap": float(selected["score_gap"]),
        "max_confidence_drop": float(selected["max_confidence_drop"]),
    }


def _fusion_configuration(
    run: dict[str, Any],
    *,
    fusion_mode: str | None,
    fusion_options: dict[str, float] | None,
    fusion_source: str | None,
) -> tuple[dict[str, Any], str]:
    captured = run.get("search_quality")
    if not isinstance(captured, dict):
        capture = run.get("capture")
        captured = capture.get("search_quality") if isinstance(capture, dict) else None
    captured_fusion = captured.get("fusion") if isinstance(captured, dict) else None
    if fusion_mode is None and isinstance(captured_fusion, dict):
        fusion_mode = captured_fusion.get("mode")
        fusion_options = {
            name: captured_fusion[name]
            for name in FUSION_V2_DEFAULTS
            if name in captured_fusion
        }
        fusion_source = "captured_run.search_quality.fusion"
    resolved_mode = fusion_mode or FUSION_V1
    if resolved_mode not in {FUSION_V1, FUSION_V2}:
        raise ValueError("fusion_mode must be confidence_v1 or confidence_v2")
    supplied = fusion_options or {}
    unknown = sorted(set(supplied) - set(FUSION_V2_DEFAULTS))
    if unknown:
        raise ValueError("unknown fusion options: " + ", ".join(unknown))
    if resolved_mode == FUSION_V1:
        if supplied:
            raise ValueError("fusion options require fusion_mode=confidence_v2")
        return {"mode": FUSION_V1}, fusion_source or "default_confidence_v1"
    options = dict(FUSION_V2_DEFAULTS)
    options.update(supplied)
    for name, (minimum, maximum, inclusive_minimum) in FUSION_V2_RANGES.items():
        value = options[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"fusion option {name} must be numeric")
        numeric = float(value)
        minimum_valid = numeric >= minimum if inclusive_minimum else numeric > minimum
        if not minimum_valid or numeric > maximum:
            lower = "[" if inclusive_minimum else "("
            raise ValueError(
                f"fusion option {name} must be in {lower}{minimum}, {maximum}]"
            )
        options[name] = numeric
    return (
        {"mode": FUSION_V2, **options},
        fusion_source or "explicit_api",
    )


def calibrate_dataset(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    allow_synthetic: bool = False,
    baseline_report: dict[str, Any] | None = None,
    max_false_positive_rate: float = DEFAULT_MAX_FALSE_POSITIVE_RATE,
    max_recall_drop: float = DEFAULT_MAX_RECALL_DROP,
    dataset_source: str | None = None,
    run_source: str | None = None,
    fusion_mode: str | None = None,
    fusion_options: dict[str, float] | None = None,
    fusion_source: str | None = None,
    selection_strategy: str = DEFAULT_SELECTION_STRATEGY,
) -> dict[str, Any]:
    if not 0 < max_false_positive_rate <= 1:
        raise ValueError("max_false_positive_rate must be in (0, 1]")
    if not 0 <= max_recall_drop <= 1:
        raise ValueError("max_recall_drop must be in [0, 1]")
    if selection_strategy not in SUPPORTED_SELECTION_STRATEGIES:
        raise ValueError("selection_strategy must be empirical or robust_inner_cv")
    pairs, coverage = evaluate._selected_pairs(
        dataset,
        run,
        allow_synthetic=allow_synthetic,
    )
    _validate_baseline_identity(dataset, coverage, baseline_report)
    fusion, resolved_fusion_source = _fusion_configuration(
        run,
        fusion_mode=fusion_mode,
        fusion_options=fusion_options,
        fusion_source=fusion_source,
    )
    collection_calibration_config, collection_calibration_diagnostics = (
        collection_calibration.calibrate(
            pairs,
            max_recall_drop=max_recall_drop,
        )
    )
    if collection_calibration_config is not None:
        pairs = collection_calibration.apply_to_pairs(
            pairs,
            collection_calibration_config,
        )
    thresholds = {}
    score_fields = {}
    inner_cross_validation = {}
    for mode in sorted(evaluate.MODES):
        mode_pairs = [(item, case) for item, case in pairs if item["mode"] == mode]
        if not mode_pairs:
            raise ValueError(f"calibration dataset has no cases for mode {mode}")
        semantics = _score_semantics(run, mode)
        score_field = _score_field(run, mode)
        if (
            mode == "combined"
            and fusion["mode"] == FUSION_V2
            and score_field not in {"confidence", "normalized_score"}
        ):
            raise ValueError(
                "confidence_v2 calibration requires combined score_field to be "
                "confidence or normalized_score"
            )
        score_fields[mode] = score_field
        baseline_metrics = _baseline_mode_metrics(mode, mode_pairs, baseline_report)
        selected = _select_threshold(
            mode,
            mode_pairs,
            semantics,
            score_field,
            baseline_metrics,
            max_false_positive_rate=max_false_positive_rate,
            max_recall_drop=max_recall_drop,
            selection_strategy=selection_strategy,
            collection_calibration_config=collection_calibration_config,
        )
        if selection_strategy == SELECTION_ROBUST_INNER_CV:
            selected = _apply_robust_threshold_margin(
                mode,
                mode_pairs,
                selected,
                semantics,
                score_field,
                baseline_metrics,
                max_false_positive_rate=max_false_positive_rate,
                max_recall_drop=max_recall_drop,
                collection_calibration_config=collection_calibration_config,
            )
            inner_cross_validation[mode] = _robust_inner_cross_validation(
                mode,
                mode_pairs,
                semantics,
                score_field,
                max_false_positive_rate=max_false_positive_rate,
                max_recall_drop=max_recall_drop,
                collection_calibration_config=collection_calibration_config,
            )
        thresholds[mode] = selected
        thresholds[mode]["score_field"] = score_field
    constraints = {
        "false_positive_rate": {
            "operator": "<",
            "value": max_false_positive_rate,
        },
        "recall_at_5_drop": {
            "operator": "<=",
            "value": max_recall_drop,
        },
        "objective": (
            "maximize standard fixed-denominator Precision@5 with fixed robust "
            "list-shape controls and deterministic inner-CV"
            if selection_strategy == SELECTION_ROBUST_INNER_CV
            else "maximize standard fixed-denominator Precision@5 "
            "(strict_precision_at_5)"
        ),
        "dynamic_top_k": (
            "threshold_then_top_confidence_band_then_confidence_gap_then_"
            "requested_top_k"
        ),
    }
    configuration = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "kind": "zvec-search-quality-thresholds",
        "text": _production_threshold("text", thresholds["text"], score_fields["text"]),
        "image": _production_threshold(
            "image", thresholds["image"], score_fields["image"]
        ),
        "combined": _production_threshold(
            "combined", thresholds["combined"], score_fields["combined"]
        ),
        "fusion": fusion,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"name": dataset["name"], "source": dataset_source},
        "run": {"name": run["name"], "source": run_source},
        "coverage": coverage,
        "constraints": constraints,
        "thresholds": thresholds,
        "diagnostics": {
            "constraints": constraints,
            "thresholds": thresholds,
            "selection": {
                "strategy": selection_strategy,
                "candidate_only": selection_strategy == SELECTION_ROBUST_INNER_CV,
                "inner_cross_validation": inner_cross_validation,
                "api_requests_added": 0,
            },
            "fusion": {
                "source": resolved_fusion_source,
                "configuration": fusion,
            },
            "collection_calibration": collection_calibration_diagnostics,
        },
    }
    if collection_calibration_config is not None:
        configuration["collection_calibration"] = collection_calibration_config
    return configuration


def calibrate_files(
    dataset_path: str | Path,
    run_path: str | Path,
    *,
    baseline_path: str | Path | None = None,
    split_manifest_path: str | Path | None = None,
    allow_synthetic: bool = False,
    max_false_positive_rate: float = DEFAULT_MAX_FALSE_POSITIVE_RATE,
    max_recall_drop: float = DEFAULT_MAX_RECALL_DROP,
    fusion_mode: str | None = None,
    fusion_options: dict[str, float] | None = None,
    fusion_source: str | None = None,
    selection_strategy: str = DEFAULT_SELECTION_STRATEGY,
) -> dict[str, Any]:
    dataset = _read_json(dataset_path)
    run = _read_json(run_path)
    split_manifest = (
        _read_json(split_manifest_path) if split_manifest_path is not None else None
    )
    if split_manifest is not None:
        dataset, run = dataset_split.prepare_calibration_inputs(
            dataset,
            run,
            split_manifest,
            allow_synthetic=allow_synthetic,
        )
    else:
        dataset_split._validate_formal_run_capture(
            run,
            dataset,
            role="calibration",
            allow_synthetic=allow_synthetic,
        )
    configuration = calibrate_dataset(
        dataset,
        run,
        baseline_report=_read_json(baseline_path) if baseline_path else None,
        allow_synthetic=allow_synthetic,
        max_false_positive_rate=max_false_positive_rate,
        max_recall_drop=max_recall_drop,
        dataset_source=str(Path(dataset_path)),
        run_source=str(Path(run_path)),
        fusion_mode=fusion_mode,
        fusion_options=fusion_options,
        fusion_source=fusion_source,
        selection_strategy=selection_strategy,
    )
    if split_manifest is not None:
        configuration["holdout_split"] = dataset_split.calibration_provenance(
            split_manifest
        )
    return configuration


def apply_thresholds(
    dataset: dict[str, Any],
    run: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    if (
        configuration.get("schema_version") not in SUPPORTED_CONFIG_SCHEMA_VERSIONS
        or configuration.get("kind") != "zvec-search-quality-thresholds"
    ):
        raise ValueError("configuration must be search-quality schema v1 or v2")
    items = {item["id"]: item for item in evaluate.validate_dataset(dataset)}
    evaluate.validate_run(run)
    thresholds = configuration.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("configuration has no thresholds")
    collection_calibration_config = configuration.get("collection_calibration")
    if collection_calibration_config is not None and not isinstance(
        collection_calibration_config, dict
    ):
        raise ValueError("configuration collection_calibration must be an object")
    filtered = copy.deepcopy(run)
    for case in filtered["cases"]:
        item = items.get(case["id"])
        if item is None:
            continue
        mode = item["mode"]
        threshold = thresholds.get(mode)
        if not isinstance(threshold, dict):
            raise ValueError(f"configuration has no threshold for mode {mode}")
        value = threshold.get("value")
        semantics = threshold.get("score_semantics")
        score_field = threshold.get("score_field", _score_field(run, mode))
        score_gap = threshold.get("score_gap", DEFAULT_SCORE_GAP)
        max_confidence_drop = threshold.get(
            "max_confidence_drop",
            DEFAULT_MAX_CONFIDENCE_DROP,
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"threshold for {mode} must be numeric")
        if semantics != _score_semantics(run, mode):
            raise ValueError(f"threshold semantics mismatch for mode {mode}")
        if score_field != _score_field(run, mode):
            raise ValueError(f"threshold score field mismatch for mode {mode}")
        if (
            isinstance(score_gap, bool)
            or not isinstance(score_gap, (int, float))
            or not 0.0 < float(score_gap) <= 1.0
        ):
            raise ValueError(f"score_gap for {mode} must be in (0, 1]")
        if (
            isinstance(max_confidence_drop, bool)
            or not isinstance(max_confidence_drop, (int, float))
            or not 0.0 <= float(max_confidence_drop) <= 1.0
        ):
            raise ValueError(f"max_confidence_drop for {mode} must be in [0, 1]")
        pair = _filtered_pairs(
            [(item, case)],
            float(value),
            semantics,
            mode=mode,
            score_field=score_field,
            score_gap=float(score_gap),
            max_confidence_drop=float(max_confidence_drop),
            collection_calibration_config=collection_calibration_config,
        )[0]
        case["results"] = pair[1]["results"]
    filtered["name"] = f"{run['name']} + calibrated-thresholds-gaps-and-bands"
    return filtered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate per-mode search thresholds from annotated cases"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--baseline")
    parser.add_argument(
        "--split-manifest",
        help=(
            "verified deterministic split manifest; only its calibration role is used"
        ),
    )
    parser.add_argument("--output", default="search-quality.json")
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument(
        "--max-false-positive-rate",
        type=float,
        default=DEFAULT_MAX_FALSE_POSITIVE_RATE,
    )
    parser.add_argument(
        "--max-recall-drop",
        type=float,
        default=DEFAULT_MAX_RECALL_DROP,
    )
    parser.add_argument("--fusion-mode", choices=(FUSION_V1, FUSION_V2))
    parser.add_argument(
        "--selection-strategy",
        choices=tuple(sorted(SUPPORTED_SELECTION_STRATEGIES)),
        default=DEFAULT_SELECTION_STRATEGY,
        help=(
            "empirical preserves the legacy joint search; robust_inner_cv emits "
            "an offline candidate that must pass deterministic inner-CV"
        ),
    )
    parser.add_argument("--agreement-reward", type=float)
    parser.add_argument("--rank-decay", type=float)
    parser.add_argument("--weak-channel-floor", type=float)
    parser.add_argument("--weak-channel-penalty", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    fusion_options = {
        name: value
        for name, value in {
            "agreement_reward": args.agreement_reward,
            "rank_decay": args.rank_decay,
            "weak_channel_floor": args.weak_channel_floor,
            "weak_channel_penalty": args.weak_channel_penalty,
        }.items()
        if value is not None
    }
    configuration = calibrate_files(
        args.dataset,
        args.run,
        baseline_path=args.baseline,
        split_manifest_path=args.split_manifest,
        allow_synthetic=args.allow_synthetic,
        max_false_positive_rate=args.max_false_positive_rate,
        max_recall_drop=args.max_recall_drop,
        fusion_mode=args.fusion_mode,
        fusion_options=fusion_options,
        fusion_source="explicit_cli" if args.fusion_mode is not None else None,
        selection_strategy=args.selection_strategy,
    )
    _write_json(args.output, configuration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
