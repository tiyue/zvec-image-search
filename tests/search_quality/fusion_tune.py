from __future__ import annotations

import argparse
import copy
import itertools
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from image_vector_service.rank_fusion import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_SCORE_GAP,
)
from image_vector_service.search_quality import FUSION_V2_DEFAULTS  # noqa: E402

if TYPE_CHECKING or __package__:
    from . import draft_report as draft_report_module
    from . import evaluate as evaluate_module
else:  # pragma: no cover - direct script execution
    import draft_report as draft_report_module
    import evaluate as evaluate_module

REPORT_SCHEMA_VERSION = 1
REPORT_KIND = "zvec-search-quality-combined-fusion-tuning-draft"
DEFAULT_TOP_K = 10
DEFAULT_MAX_FALSE_RETURN_RATE = 0.10
DEFAULT_MAX_RECALL_DROP = 0.03
DEFAULT_MAX_REPORTED_CANDIDATES = 10
MAX_PARAMETER_SETS = 1_000_000

DEFAULT_GRIDS: dict[str, tuple[float, ...]] = {
    "image_weight": (0.20, 0.35, 0.50, 0.65, 0.80),
    "agreement_reward": (0.00, 0.04, 0.08, 0.12),
    "rank_decay": (5.0, 10.0, 20.0),
    "weak_channel_floor": (0.30, 0.45, 0.60),
    "weak_channel_penalty": (0.00, 0.04, 0.08, 0.12),
    "minimum_confidence": (
        0.25,
        0.40,
        0.50,
        0.55,
        0.60,
        0.65,
        0.70,
        0.725,
        0.75,
        0.775,
        0.80,
        0.825,
        0.85,
        0.875,
        0.90,
        0.925,
        0.95,
    ),
    "score_gap": (0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 1.0),
}


@dataclass(frozen=True)
class FusionParameters:
    image_weight: float
    text_weight: float
    agreement_reward: float
    rank_decay: float
    weak_channel_floor: float
    weak_channel_penalty: float


@dataclass(frozen=True)
class TuningParameters:
    image_weight: float
    text_weight: float
    agreement_reward: float
    rank_decay: float
    weak_channel_floor: float
    weak_channel_penalty: float
    minimum_confidence: float
    score_gap: float
    top_k: int


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _optional_confidence_and_rank(
    result: dict[str, Any],
    *,
    source: str,
    case_id: str,
) -> tuple[float | None, int | None]:
    confidence_name = f"{source}_confidence"
    rank_name = f"{source}_rank"
    has_confidence = confidence_name in result
    has_rank = rank_name in result
    if not has_confidence and not has_rank:
        return None, None
    if not has_confidence or not has_rank:
        raise ValueError(
            f"run case {case_id}: {confidence_name} and {rank_name} must both be "
            "present or both be omitted for an absent channel"
        )
    confidence = result[confidence_name]
    rank = result[rank_name]
    if confidence is None and rank is None:
        return None, None
    if confidence is None or rank is None:
        raise ValueError(
            f"run case {case_id}: {confidence_name} and {rank_name} must both be "
            "numeric or both be null"
        )
    numeric_confidence = _finite_number(
        confidence,
        label=f"run case {case_id}: {confidence_name}",
    )
    if not 0.0 <= numeric_confidence <= 1.0:
        raise ValueError(
            f"run case {case_id}: {confidence_name} must be between zero and one"
        )
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError(f"run case {case_id}: {rank_name} must be a positive integer")
    return numeric_confidence, rank


def _validate_candidate_diagnostics(case: dict[str, Any]) -> None:
    case_id = str(case["id"])
    for result in case["results"]:
        image_confidence, _image_rank = _optional_confidence_and_rank(
            result,
            source="image",
            case_id=case_id,
        )
        text_confidence, _text_rank = _optional_confidence_and_rank(
            result,
            source="text",
            case_id=case_id,
        )
        if image_confidence is None and text_confidence is None:
            raise ValueError(
                f"run case {case_id}: a result needs at least one channel diagnostic"
            )
        rank = result.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError(f"run case {case_id}: every result needs a positive rank")


def _combined_draft_pairs(
    dataset: dict[str, Any],
    run: dict[str, Any],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    items = evaluate_module.validate_dataset(dataset)
    cases = evaluate_module.validate_run(run)
    if run.get("draft") is not True or run.get("baseline_eligible") is not False:
        raise ValueError(
            "combined fusion tuning accepts draft captures only; run must declare "
            "draft=true and baseline_eligible=false"
        )

    combined_items = [item for item in items if item["mode"] == "combined"]
    if not combined_items:
        raise ValueError("dataset has no combined-mode cases")
    non_pending = [
        str(item["id"])
        for item in combined_items
        if item["annotation"]["status"] != "pending"
    ]
    if non_pending:
        raise ValueError(
            "combined fusion draft tuning accepts pending AI suggestions only; "
            f"found non-pending cases: {', '.join(non_pending)}"
        )
    missing = [str(item["id"]) for item in combined_items if item["id"] not in cases]
    if missing:
        raise ValueError("run is missing combined draft cases: " + ", ".join(missing))
    known_ids = {str(item["id"]) for item in items}
    unknown = sorted(set(cases) - known_ids)
    if unknown:
        raise ValueError("run contains unknown cases: " + ", ".join(unknown))

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    answerable = 0
    no_answer = 0
    suggested_labels = 0
    for item in combined_items:
        suggestions = draft_report_module._suggested_images(item)
        draft_item = copy.deepcopy(item)
        draft_item["relevant_images"] = suggestions
        case = cases[str(item["id"])]
        _validate_candidate_diagnostics(case)
        pairs.append((draft_item, case))
        suggested_labels += len(suggestions)
        if item["query_type"] == "no-answer":
            no_answer += 1
        else:
            answerable += 1
    if answerable < 1 or no_answer < 1:
        raise ValueError(
            "combined fusion tuning needs at least one answerable and one no-answer "
            "combined-mode case"
        )
    selected_ids = {str(item["id"]) for item in combined_items}
    return pairs, {
        "dataset_total": len(items),
        "run_case_count": len(cases),
        "combined_cases": len(combined_items),
        "answerable": answerable,
        "no_answer": no_answer,
        "pending": len(combined_items),
        "human_verified": 0,
        "suggested_relevance_labels": suggested_labels,
        "ignored_non_combined_run_cases": len(set(cases) - selected_ids),
    }


def _fused_confidence(
    result: dict[str, Any],
    parameters: FusionParameters,
    *,
    case_id: str,
) -> float:
    image_confidence, image_rank = _optional_confidence_and_rank(
        result,
        source="image",
        case_id=case_id,
    )
    text_confidence, text_rank = _optional_confidence_and_rank(
        result,
        source="text",
        case_id=case_id,
    )
    if image_confidence is None or text_confidence is None:
        available = (
            image_confidence if image_confidence is not None else text_confidence
        )
        assert available is not None
        return max(0.0, available - parameters.weak_channel_penalty)

    assert image_rank is not None and text_rank is not None
    weighted_confidence = (
        parameters.image_weight * image_confidence
        + parameters.text_weight * text_confidence
    )
    weaker_confidence = min(image_confidence, text_confidence)
    stronger_confidence = max(image_confidence, text_confidence)
    rank_agreement = math.exp(-abs(image_rank - text_rank) / parameters.rank_decay)
    if weaker_confidence >= parameters.weak_channel_floor:
        confidence = weighted_confidence + (
            parameters.agreement_reward * weaker_confidence * rank_agreement
        )
    else:
        weak_ratio = (
            weaker_confidence / parameters.weak_channel_floor
            if parameters.weak_channel_floor > 0.0
            else 1.0
        )
        protected_confidence = stronger_confidence - (
            parameters.weak_channel_penalty * (1.0 - weak_ratio)
        )
        confidence = max(weighted_confidence, protected_confidence)
    return min(1.0, max(0.0, confidence))


def _rank_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    parameters: FusionParameters,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    ranked_pairs = []
    for item, case in pairs:
        case_id = str(case["id"])
        scored = [
            (
                _fused_confidence(result, parameters, case_id=case_id),
                int(result["rank"]),
                str(result["image_id"]),
                result,
            )
            for result in case["results"]
        ]
        scored.sort(key=lambda value: (-value[0], value[1], value[2]))
        next_case = dict(case)
        next_case["results"] = [
            {
                **result,
                "rank": rank,
                "score": confidence,
                "raw_score": confidence,
                "normalized_score": confidence,
                "confidence": confidence,
            }
            for rank, (confidence, _old_rank, _image_id, result) in enumerate(
                scored,
                start=1,
            )
        ]
        ranked_pairs.append((item, next_case))
    return ranked_pairs


def _dynamic_top_k(
    ranked_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    minimum_confidence: float,
    score_gap: float,
    top_k: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    filtered_pairs = []
    for item, case in ranked_pairs:
        passing = [
            result
            for result in case["results"]
            if float(result["confidence"]) >= minimum_confidence
        ]
        selected: list[dict[str, Any]] = []
        previous_confidence: float | None = None
        for result in passing:
            confidence = float(result["confidence"])
            if (
                previous_confidence is not None
                and previous_confidence - confidence >= score_gap
            ):
                break
            selected.append({**result, "rank": len(selected) + 1})
            previous_confidence = confidence
            if len(selected) >= top_k:
                break
        next_case = dict(case)
        next_case["results"] = selected
        next_case["status"] = "ok" if selected else "no_reliable_match"
        next_case["candidate_count"] = len(case["results"])
        next_case["filtered_count"] = len(case["results"]) - len(selected)
        filtered_pairs.append((item, next_case))
    return filtered_pairs


def _default_parameters(top_k: int) -> TuningParameters:
    return TuningParameters(
        image_weight=0.5,
        text_weight=0.5,
        agreement_reward=float(FUSION_V2_DEFAULTS["agreement_reward"]),
        rank_decay=float(FUSION_V2_DEFAULTS["rank_decay"]),
        weak_channel_floor=float(FUSION_V2_DEFAULTS["weak_channel_floor"]),
        weak_channel_penalty=float(FUSION_V2_DEFAULTS["weak_channel_penalty"]),
        minimum_confidence=DEFAULT_MIN_CONFIDENCE,
        score_gap=DEFAULT_SCORE_GAP,
        top_k=top_k,
    )


def _fusion_parameters(parameters: TuningParameters) -> FusionParameters:
    return FusionParameters(
        image_weight=parameters.image_weight,
        text_weight=parameters.text_weight,
        agreement_reward=parameters.agreement_reward,
        rank_decay=parameters.rank_decay,
        weak_channel_floor=parameters.weak_channel_floor,
        weak_channel_penalty=parameters.weak_channel_penalty,
    )


def _evaluate_parameters(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    parameters: TuningParameters,
) -> dict[str, Any]:
    ranked = _rank_pairs(pairs, _fusion_parameters(parameters))
    filtered = _dynamic_top_k(
        ranked,
        minimum_confidence=parameters.minimum_confidence,
        score_gap=parameters.score_gap,
        top_k=parameters.top_k,
    )
    return evaluate_module._metric_block(filtered)


def _validate_grid(name: str, values: Sequence[float]) -> tuple[float, ...]:
    if not values:
        raise ValueError(f"grid {name} must contain at least one value")
    resolved = tuple(
        sorted({_finite_number(value, label=f"grid {name}") for value in values})
    )
    ranges = {
        "image_weight": (0.0, 1.0, False, False),
        "agreement_reward": (0.0, 0.25, True, True),
        "rank_decay": (0.0, 100.0, False, True),
        "weak_channel_floor": (0.0, 1.0, True, True),
        "weak_channel_penalty": (0.0, 0.5, True, True),
        "minimum_confidence": (0.0, 1.0, True, True),
        "score_gap": (0.0, 1.0, False, True),
    }
    minimum, maximum, include_minimum, include_maximum = ranges[name]
    for value in resolved:
        minimum_valid = value >= minimum if include_minimum else value > minimum
        maximum_valid = value <= maximum if include_maximum else value < maximum
        if not minimum_valid or not maximum_valid:
            left = "[" if include_minimum else "("
            right = "]" if include_maximum else ")"
            raise ValueError(
                f"grid {name} values must be in {left}{minimum}, {maximum}{right}"
            )
    return resolved


def _resolve_grids(
    overrides: Mapping[str, Sequence[float]] | None,
) -> dict[str, tuple[float, ...]]:
    unknown = sorted(set(overrides or {}) - set(DEFAULT_GRIDS))
    if unknown:
        raise ValueError("unknown tuning grids: " + ", ".join(unknown))
    return {
        name: _validate_grid(name, (overrides or {}).get(name, defaults))
        for name, defaults in DEFAULT_GRIDS.items()
    }


def _required_metric(metrics: dict[str, Any], name: str) -> float:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"combined tuning metrics require numeric {name}")
    return float(value)


def _parameter_deviation(
    parameters: TuningParameters,
    defaults: TuningParameters,
) -> float:
    scales = {
        "image_weight": 1.0,
        "agreement_reward": 0.25,
        "rank_decay": 100.0,
        "weak_channel_floor": 1.0,
        "weak_channel_penalty": 0.5,
        "minimum_confidence": 1.0,
        "score_gap": 1.0,
    }
    return sum(
        abs(float(getattr(parameters, name)) - float(getattr(defaults, name))) / scale
        for name, scale in scales.items()
    )


def _objective(
    metrics: dict[str, Any],
    parameters: TuningParameters,
    defaults: TuningParameters,
) -> tuple[float, ...]:
    precision = _required_metric(metrics, "precision_at_5")
    recall = _required_metric(metrics, "recall_at_5")
    false_return = _required_metric(metrics, "no_answer_false_return_rate")
    result_count = _required_metric(metrics, "average_result_count")
    parameter_values = (
        parameters.image_weight,
        parameters.agreement_reward,
        parameters.rank_decay,
        parameters.weak_channel_floor,
        parameters.weak_channel_penalty,
        parameters.minimum_confidence,
        parameters.score_gap,
    )
    return (
        precision,
        recall,
        -false_return,
        -result_count,
        -_parameter_deviation(parameters, defaults),
        *(-value for value in parameter_values),
    )


def _strict_objective(
    metrics: dict[str, Any],
    parameters: TuningParameters,
    defaults: TuningParameters,
) -> tuple[float, ...]:
    """Select a non-production diagnostic without changing schema-v1 objectives."""
    strict_precision = _required_metric(metrics, "strict_precision_at_5")
    recall = _required_metric(metrics, "recall_at_5")
    mrr = _required_metric(metrics, "mrr")
    returned_precision = _required_metric(metrics, "precision_at_5")
    false_return = _required_metric(metrics, "no_answer_false_return_rate")
    result_count = _required_metric(metrics, "average_result_count")
    parameter_values = (
        parameters.image_weight,
        parameters.agreement_reward,
        parameters.rank_decay,
        parameters.weak_channel_floor,
        parameters.weak_channel_penalty,
        parameters.minimum_confidence,
        parameters.score_gap,
    )
    return (
        strict_precision,
        recall,
        mrr,
        returned_precision,
        -false_return,
        -result_count,
        -_parameter_deviation(parameters, defaults),
        *(-value for value in parameter_values),
    )


def _candidate(
    parameters: TuningParameters,
    metrics: dict[str, Any],
    *,
    baseline_recall: float,
) -> dict[str, Any]:
    return {
        "parameters": asdict(parameters),
        "metrics": metrics,
        "recall_at_5_drop": max(
            0.0,
            baseline_recall - _required_metric(metrics, "recall_at_5"),
        ),
    }


def _metric_difference(
    defaults: dict[str, Any],
    tuned: dict[str, Any],
) -> dict[str, dict[str, float]]:
    fields = (
        "precision_at_5",
        "strict_precision_at_5",
        "recall_at_5",
        "mrr",
        "no_answer_false_return_rate",
        "average_result_count",
    )
    return {
        name: {
            "default": _required_metric(defaults, name),
            "tuned": _required_metric(tuned, name),
            "delta": _required_metric(tuned, name) - _required_metric(defaults, name),
        }
        for name in fields
    }


def _parameter_difference(
    defaults: TuningParameters,
    tuned: TuningParameters,
) -> dict[str, dict[str, float | int]]:
    values: dict[str, dict[str, float | int]] = {}
    for name, default in asdict(defaults).items():
        tuned_value = getattr(tuned, name)
        values[name] = {
            "default": default,
            "tuned": tuned_value,
            "delta": tuned_value - default,
        }
    return values


def tune_combined_fusion(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    grids: Mapping[str, Sequence[float]] | None = None,
    top_k: int = DEFAULT_TOP_K,
    max_false_return_rate: float = DEFAULT_MAX_FALSE_RETURN_RATE,
    max_recall_drop: float = DEFAULT_MAX_RECALL_DROP,
    max_reported_candidates: int = DEFAULT_MAX_REPORTED_CANDIDATES,
    dataset_source: str | None = None,
    run_source: str | None = None,
) -> dict[str, Any]:
    if top_k < evaluate_module.TOP_K:
        raise ValueError(f"top_k must be at least {evaluate_module.TOP_K} for Recall@5")
    if not 0.0 < max_false_return_rate <= 1.0:
        raise ValueError("max_false_return_rate must be in (0, 1]")
    if not 0.0 <= max_recall_drop <= 1.0:
        raise ValueError("max_recall_drop must be in [0, 1]")
    if max_reported_candidates < 1:
        raise ValueError("max_reported_candidates must be positive")

    pairs, coverage = _combined_draft_pairs(dataset, run)
    resolved_grids = _resolve_grids(grids)
    evaluated_parameter_sets = math.prod(
        len(values) for values in resolved_grids.values()
    )
    if evaluated_parameter_sets > MAX_PARAMETER_SETS:
        raise ValueError(
            f"tuning grid has {evaluated_parameter_sets} parameter sets; maximum is "
            f"{MAX_PARAMETER_SETS}"
        )

    defaults = _default_parameters(top_k)
    default_metrics = _evaluate_parameters(pairs, defaults)
    baseline_recall = _required_metric(default_metrics, "recall_at_5")
    if default_metrics.get("no_answer_false_return_rate") is None:
        raise ValueError("combined tuning needs no-answer cases")

    eligible_count = 0
    reported: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    strict_best: tuple[tuple[float, ...], dict[str, Any]] | None = None
    core_names = (
        "image_weight",
        "agreement_reward",
        "rank_decay",
        "weak_channel_floor",
        "weak_channel_penalty",
    )
    core_grids = [resolved_grids[name] for name in core_names]
    for core_values in itertools.product(*core_grids):
        image_weight, agreement_reward, rank_decay, weak_floor, weak_penalty = (
            float(value) for value in core_values
        )
        fusion = FusionParameters(
            image_weight=image_weight,
            text_weight=1.0 - image_weight,
            agreement_reward=agreement_reward,
            rank_decay=rank_decay,
            weak_channel_floor=weak_floor,
            weak_channel_penalty=weak_penalty,
        )
        ranked = _rank_pairs(pairs, fusion)
        for minimum_confidence, score_gap in itertools.product(
            resolved_grids["minimum_confidence"],
            resolved_grids["score_gap"],
        ):
            filtered = _dynamic_top_k(
                ranked,
                minimum_confidence=minimum_confidence,
                score_gap=score_gap,
                top_k=top_k,
            )
            metrics = evaluate_module._metric_block(filtered)
            recall = _required_metric(metrics, "recall_at_5")
            false_return = _required_metric(
                metrics,
                "no_answer_false_return_rate",
            )
            recall_drop = max(0.0, baseline_recall - recall)
            if false_return >= max_false_return_rate:
                continue
            if recall_drop > max_recall_drop + 1e-12:
                continue
            parameters = TuningParameters(
                image_weight=image_weight,
                text_weight=1.0 - image_weight,
                agreement_reward=agreement_reward,
                rank_decay=rank_decay,
                weak_channel_floor=weak_floor,
                weak_channel_penalty=weak_penalty,
                minimum_confidence=float(minimum_confidence),
                score_gap=float(score_gap),
                top_k=top_k,
            )
            candidate = _candidate(
                parameters,
                metrics,
                baseline_recall=baseline_recall,
            )
            reported.append((_objective(metrics, parameters, defaults), candidate))
            strict_value = _strict_objective(metrics, parameters, defaults)
            if strict_best is None or strict_value > strict_best[0]:
                strict_best = (strict_value, candidate)
            eligible_count += 1
            if len(reported) > max_reported_candidates * 2:
                reported.sort(key=lambda value: value[0], reverse=True)
                del reported[max_reported_candidates:]
    reported.sort(key=lambda value: value[0], reverse=True)
    candidates = [
        value for _objective_value, value in reported[:max_reported_candidates]
    ]
    best = candidates[0] if candidates else None
    best_strict = strict_best[1] if strict_best is not None else None
    difference = None
    if best is not None:
        tuned_parameters = TuningParameters(**best["parameters"])
        difference = {
            "parameters": _parameter_difference(defaults, tuned_parameters),
            "metrics": _metric_difference(default_metrics, best["metrics"]),
        }
    strict_difference = None
    if best_strict is not None:
        strict_parameters = TuningParameters(**best_strict["parameters"])
        strict_difference = {
            "parameters": _parameter_difference(defaults, strict_parameters),
            "metrics": _metric_difference(default_metrics, best_strict["metrics"]),
        }

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "draft": True,
        "baseline_eligible": False,
        "production_config_generated": False,
        "label_source": draft_report_module.LABEL_SOURCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warning": (
            "Parameters were selected against unverified AI-assisted suggestions. "
            "This artifact is not a production search-quality configuration, formal "
            "baseline, calibration input, or release gate."
        ),
        "dataset": {"name": dataset["name"], "source": dataset_source},
        "run": {"name": run["name"], "source": run_source},
        "coverage": coverage,
        "constraints": {
            "no_answer_false_return_rate": {
                "operator": "<",
                "value": max_false_return_rate,
            },
            "recall_at_5_drop_from_default_v2": {
                "operator": "<=",
                "value": max_recall_drop,
            },
            "primary_objective": "maximize_legacy_returned_precision_at_5",
            "strict_diagnostic_objective": "maximize_strict_precision_at_5",
            "tie_breakers": [
                "maximize_recall_at_5",
                "minimize_no_answer_false_return_rate",
                "minimize_average_result_count",
                "minimize_parameter_distance_from_default_v2",
                "deterministic_numeric_parameter_order",
            ],
            "dynamic_top_k": "minimum_confidence_then_score_gap_then_top_k",
        },
        "default_v2": {
            "parameters": asdict(defaults),
            "metrics": default_metrics,
            "baseline_role": "Recall@5 comparison reference",
        },
        "grid_search": {
            "grids": {name: list(values) for name, values in resolved_grids.items()},
            "evaluated_parameter_sets": evaluated_parameter_sets,
            "eligible_parameter_sets": eligible_count,
            "reported_candidate_limit": max_reported_candidates,
        },
        "best_candidate": best,
        "difference_from_default_v2": difference,
        "best_strict_candidate": best_strict,
        "strict_difference_from_default_v2": strict_difference,
        "eligible_candidates": candidates,
    }


def tune_combined_fusion_files(
    dataset_path: str | Path,
    run_path: str | Path,
    **options: Any,
) -> dict[str, Any]:
    return tune_combined_fusion(
        evaluate_module._read_json(dataset_path),
        evaluate_module._read_json(run_path),
        dataset_source=str(Path(dataset_path)),
        run_source=str(Path(run_path)),
        **options,
    )


def _format_difference(value: float | int) -> str:
    if isinstance(value, int):
        return f"{value:+d}"
    return f"{value:+.4f}"


def render_markdown(report: dict[str, Any]) -> str:
    best = report["best_candidate"]
    lines = [
        "# Combined fusion tuning draft",
        "",
        "**DRAFT ONLY — NOT BASELINE ELIGIBLE — NOT A PRODUCTION CONFIG.**",
        "",
        report["warning"],
        "",
        f"- Dataset: `{report['dataset']['name']}`",
        f"- Run: `{report['run']['name']}`",
        f"- Combined cases: {report['coverage']['combined_cases']}",
        f"- Answerable / no-answer: {report['coverage']['answerable']} / "
        f"{report['coverage']['no_answer']}",
        f"- Parameter sets evaluated: "
        f"{report['grid_search']['evaluated_parameter_sets']}",
        f"- Eligible parameter sets: "
        f"{report['grid_search']['eligible_parameter_sets']}",
        "",
        "The Recall@5 constraint is measured against the recomputed production "
        "`confidence_v2` defaults on the same captured candidate pool.",
        "",
    ]
    if best is None:
        lines.extend(
            [
                "## No eligible draft parameters",
                "",
                "No grid point satisfied both the no-answer false-return and "
                "Recall@5 constraints. Expand the candidate capture or revise the "
                "experimental grid; do not promote a failing point.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Best eligible draft parameters",
            "",
            "| Parameter | Default v2 | Tuned draft | Difference |",
            "|---|---:|---:|---:|",
        ]
    )
    for name, value in report["difference_from_default_v2"]["parameters"].items():
        lines.append(
            f"| `{name}` | {evaluate_module._format_metric(value['default'])} | "
            f"{evaluate_module._format_metric(value['tuned'])} | "
            f"{_format_difference(value['delta'])} |"
        )
    lines.extend(
        [
            "",
            "## Exploratory metric difference",
            "",
            "| Metric | Default v2 | Tuned draft | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    labels = {
        "precision_at_5": "Returned precision (legacy precision_at_5)",
        "strict_precision_at_5": "Strict Precision@5",
        "recall_at_5": "Recall@5",
        "mrr": "MRR",
        "no_answer_false_return_rate": "No-answer false-return rate",
        "average_result_count": "Average result count",
    }
    for name, value in report["difference_from_default_v2"]["metrics"].items():
        lines.append(
            f"| {labels[name]} | "
            f"{evaluate_module._format_metric(value['default'])} | "
            f"{evaluate_module._format_metric(value['tuned'])} | "
            f"{_format_difference(value['delta'])} |"
        )
    strict_best = report.get("best_strict_candidate")
    strict_difference = report.get("strict_difference_from_default_v2")
    if strict_best is not None and isinstance(strict_difference, dict):
        lines.extend(
            [
                "",
                "## Best strict-Precision diagnostic",
                "",
                (
                    "This separate draft candidate maximizes fixed-denominator "
                    "`strict_precision_at_5` without changing the schema-v1 legacy "
                    "selection objective or production gate."
                ),
                "",
                "| Parameter | Default v2 | Strict diagnostic | Difference |",
                "|---|---:|---:|---:|",
            ]
        )
        for name, value in strict_difference["parameters"].items():
            lines.append(
                f"| `{name}` | "
                f"{evaluate_module._format_metric(value['default'])} | "
                f"{evaluate_module._format_metric(value['tuned'])} | "
                f"{_format_difference(value['delta'])} |"
            )
        lines.extend(
            [
                "",
                "| Metric | Default v2 | Strict diagnostic | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for name, value in strict_difference["metrics"].items():
            lines.append(
                f"| {labels[name]} | "
                f"{evaluate_module._format_metric(value['default'])} | "
                f"{evaluate_module._format_metric(value['tuned'])} | "
                f"{_format_difference(value['delta'])} |"
            )
    lines.extend(
        [
            "",
            "## Safety boundary",
            "",
            "This report intentionally has a dedicated draft `kind`, includes "
            "`draft: true` and `baseline_eligible: false`, and omits the production "
            "`text`, `image`, and `combined` threshold sections. The production "
            "loader must reject it if it is renamed to `search-quality.json`.",
        ]
    )
    return "\n".join(lines)


def _parse_grid(value: str) -> tuple[float, ...]:
    try:
        values = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "grid values must be comma-separated numbers"
        ) from exc
    if not values:
        raise argparse.ArgumentTypeError("grid must contain at least one number")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Draft-only grid tuning for confidence_v2 combined search; never writes "
            "a production-loadable search-quality configuration"
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--max-false-return-rate",
        type=float,
        default=DEFAULT_MAX_FALSE_RETURN_RATE,
    )
    parser.add_argument(
        "--max-recall-drop",
        type=float,
        default=DEFAULT_MAX_RECALL_DROP,
    )
    parser.add_argument(
        "--max-reported-candidates",
        type=int,
        default=DEFAULT_MAX_REPORTED_CANDIDATES,
    )
    parser.add_argument("--image-weights", type=_parse_grid)
    parser.add_argument("--agreement-rewards", type=_parse_grid)
    parser.add_argument("--rank-decays", type=_parse_grid)
    parser.add_argument("--weak-channel-floors", type=_parse_grid)
    parser.add_argument("--weak-channel-penalties", type=_parse_grid)
    parser.add_argument("--minimum-confidences", type=_parse_grid)
    parser.add_argument("--score-gaps", type=_parse_grid)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cli_grids = {
        name: value
        for name, value in {
            "image_weight": args.image_weights,
            "agreement_reward": args.agreement_rewards,
            "rank_decay": args.rank_decays,
            "weak_channel_floor": args.weak_channel_floors,
            "weak_channel_penalty": args.weak_channel_penalties,
            "minimum_confidence": args.minimum_confidences,
            "score_gap": args.score_gaps,
        }.items()
        if value is not None
    }
    report = tune_combined_fusion_files(
        args.dataset,
        args.run,
        grids=cli_grids,
        top_k=args.top_k,
        max_false_return_rate=args.max_false_return_rate,
        max_recall_drop=args.max_recall_drop,
        max_reported_candidates=args.max_reported_candidates,
    )
    evaluate_module._write_json(args.output, report)
    if args.markdown:
        evaluate_module._write_text(args.markdown, render_markdown(report))
    return 0 if report["best_candidate"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
