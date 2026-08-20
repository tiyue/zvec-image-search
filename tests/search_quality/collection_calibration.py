from __future__ import annotations

import importlib
import math
from statistics import fmean
from typing import Any

evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)
collection_fairness = importlib.import_module(
    "tests.search_quality.collection_fairness" if __package__ else "collection_fairness"
)

MODE = "null_mean_offset_v1"
FALLBACK = "identity"
FAIRNESS_TOP_K = 10
MIN_PAIRWISE_ACCURACY = 0.80
MIN_WORST_DIRECTIONAL_ACCURACY = 0.50
STRENGTH_CANDIDATES = tuple(index / 20.0 for index in range(61))
EPSILON = 1e-12


def _finite_confidence(result: dict[str, Any]) -> float | None:
    value = result.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _scope_library_ids(item: dict[str, Any], case: dict[str, Any]) -> tuple[str, ...]:
    raw_ids = case.get("library_ids")
    if raw_ids is None:
        raw_ids = item.get("library_scope", {}).get("library_ids", [])
    if not isinstance(raw_ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_ids
    ):
        return ()
    values = tuple(value.strip() for value in raw_ids)
    if len(values) != len(set(values)):
        return ()
    return values


def result_library_id(
    item: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    scope_ids = _scope_library_ids(item, case)
    if not scope_ids:
        return None
    explicit = result.get("library_id")
    if isinstance(explicit, str) and explicit.strip() in scope_ids:
        return explicit.strip()
    image_id = result.get("image_id")
    if not isinstance(image_id, str) or not image_id.strip():
        return None
    matches = [
        library_id for library_id in scope_ids if image_id.startswith(f"{library_id}:")
    ]
    if len(matches) == 1:
        return matches[0]
    if len(scope_ids) == 1:
        return scope_ids[0]
    return None


def offsets_for(configuration: dict[str, Any] | None, mode: str) -> dict[str, float]:
    if not isinstance(configuration, dict) or configuration.get("mode") != MODE:
        return {}
    modes = configuration.get("modes")
    if not isinstance(modes, dict):
        return {}
    mode_config = modes.get(mode)
    if not isinstance(mode_config, dict):
        return {}
    libraries = mode_config.get("libraries")
    if not isinstance(libraries, dict):
        return {}
    offsets: dict[str, float] = {}
    for library_id, value in libraries.items():
        if not isinstance(library_id, str) or not isinstance(value, dict):
            continue
        offset = value.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            continue
        numeric = float(offset)
        if math.isfinite(numeric):
            offsets[library_id] = numeric
    return offsets


def ranking_confidence(
    item: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
    configuration: dict[str, Any] | None,
    *,
    fallback_confidence: float,
) -> float:
    library_id = result_library_id(item, case, result)
    offset = offsets_for(configuration, str(item["mode"])).get(library_id or "", 0.0)
    return min(1.0, max(0.0, fallback_confidence + offset))


def apply_to_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    configuration: dict[str, Any] | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not isinstance(configuration, dict) or configuration.get("mode") != MODE:
        return pairs
    adjusted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item, case in pairs:
        next_case = dict(case)
        decorated: list[tuple[float, int, dict[str, Any]]] = []
        for position, result in enumerate(case["results"]):
            confidence = _finite_confidence(result)
            if confidence is None:
                return pairs
            score = ranking_confidence(
                item,
                case,
                result,
                configuration,
                fallback_confidence=confidence,
            )
            decorated.append((score, position, dict(result)))
        decorated.sort(key=lambda value: (-value[0], value[1]))
        next_case["results"] = [
            {**result, "rank": rank, "ranking_confidence": score}
            for rank, (score, _position, result) in enumerate(decorated, start=1)
        ]
        adjusted.append((item, next_case))
    return adjusted


def _truncated_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], top_k: int
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    truncated = []
    for item, case in pairs:
        next_case = dict(case)
        next_case["results"] = [
            {**result, "rank": rank}
            for rank, result in enumerate(case["results"][:top_k], start=1)
        ]
        truncated.append((item, next_case))
    return truncated


def _fairness_metrics(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    report = collection_fairness.evaluate_collection_fairness(
        _truncated_pairs(pairs, FAIRNESS_TOP_K)
    )
    metrics = report["metrics"]
    pairwise = metrics.get("cross_collection_pairwise_accuracy")
    worst = metrics.get("worst_directional_pairwise_accuracy")
    if report["status"] == "measured" and worst is None:
        worst = 1.0
    return {
        "status": report["status"],
        "pairwise_accuracy": pairwise,
        "worst_directional_pairwise_accuracy": worst,
        "comparisons": report["coverage"]["cross_collection_comparisons"],
        "assessable_cases": report["coverage"]["assessable_cases"],
    }


def _configuration_for_strength(
    baselines: dict[str, dict[str, list[float]]], strength: float
) -> dict[str, Any]:
    modes: dict[str, Any] = {}
    for mode, libraries in sorted(baselines.items()):
        means = {library_id: fmean(values) for library_id, values in libraries.items()}
        reference = fmean(means.values())
        modes[mode] = {
            "reference_confidence": reference,
            "libraries": {
                library_id: {
                    "baseline_confidence": baseline,
                    "offset": strength * (reference - baseline),
                    "sample_count": len(libraries[library_id]),
                }
                for library_id, baseline in sorted(means.items())
            },
        }
    return {
        "mode": MODE,
        "fallback": FALLBACK,
        "strength": strength,
        "top_k": FAIRNESS_TOP_K,
        "modes": modes,
    }


def calibrate(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    max_recall_drop: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    baselines: dict[str, dict[str, list[float]]] = {}
    for item, case in pairs:
        if item["query_type"] != "no-answer":
            continue
        for result in case["results"]:
            confidence = _finite_confidence(result)
            library_id = result_library_id(item, case, result)
            if confidence is None or library_id is None:
                return None, {
                    "mode": "none",
                    "reason": "no_answer_candidates_lack_confidence_or_library_id",
                }
            mode_baselines = baselines.setdefault(str(item["mode"]), {})
            mode_baselines.setdefault(library_id, []).append(confidence)

    required_modes = {"text", "image", "combined"}
    if set(baselines) != required_modes or any(
        len(libraries) < 2 for libraries in baselines.values()
    ):
        return None, {
            "mode": "none",
            "reason": "needs_no_answer_candidates_from_two_libraries_per_mode",
        }

    baseline_metrics = evaluate._metric_block(pairs)
    baseline_precision = float(baseline_metrics["strict_precision_at_5"])
    baseline_recall = float(baseline_metrics["recall_at_5"])
    candidates: list[dict[str, Any]] = []
    for strength in STRENGTH_CANDIDATES:
        configuration = _configuration_for_strength(baselines, strength)
        adjusted = apply_to_pairs(pairs, configuration)
        metrics = evaluate._metric_block(adjusted)
        fairness = _fairness_metrics(adjusted)
        precision = float(metrics["strict_precision_at_5"])
        recall = float(metrics["recall_at_5"])
        recall_drop = max(0.0, baseline_recall - recall)
        eligible = (
            precision + EPSILON >= baseline_precision
            and recall_drop <= max_recall_drop + EPSILON
        )
        pairwise = fairness["pairwise_accuracy"]
        worst = fairness["worst_directional_pairwise_accuracy"]
        candidate_gate_feasible = (
            eligible
            and fairness["status"] == "measured"
            and isinstance(pairwise, (int, float))
            and float(pairwise) + EPSILON >= MIN_PAIRWISE_ACCURACY
            and isinstance(worst, (int, float))
            and float(worst) + EPSILON >= MIN_WORST_DIRECTIONAL_ACCURACY
        )
        candidates.append(
            {
                "configuration": configuration,
                "metrics": metrics,
                "fairness": fairness,
                "recall_at_5_drop": recall_drop,
                "eligible": eligible,
                "gate_feasible": candidate_gate_feasible,
            }
        )

    eligible_candidates = [
        candidate for candidate in candidates if candidate["eligible"]
    ]
    if not eligible_candidates:
        return None, {"mode": "none", "reason": "no_non_degrading_offset"}
    gate_feasible_candidates = [
        candidate for candidate in eligible_candidates if candidate["gate_feasible"]
    ]
    pool = gate_feasible_candidates or eligible_candidates

    def objective(candidate: dict[str, Any]) -> tuple[float, ...]:
        metrics = candidate["metrics"]
        fairness = candidate["fairness"]
        pairwise = fairness["pairwise_accuracy"]
        worst = fairness["worst_directional_pairwise_accuracy"]
        quality = (
            float(metrics["strict_precision_at_5"]),
            float(metrics["recall_at_5"]),
            float(metrics["mrr"]),
        )
        fairness_values = (
            float(pairwise) if isinstance(pairwise, (int, float)) else -1.0,
            float(worst) if isinstance(worst, (int, float)) else -1.0,
        )
        strength = float(candidate["configuration"]["strength"])
        if gate_feasible_candidates:
            return (*quality, *fairness_values, -strength)
        return (*fairness_values, *quality, -strength)

    selected = max(pool, key=objective)
    configuration = selected["configuration"]
    diagnostics = {
        "mode": MODE,
        "source": "calibration_no_answer_candidates",
        "fallback": FALLBACK,
        "candidate_strength_count": len(candidates),
        "gate_feasible_candidate_count": len(gate_feasible_candidates),
        "baseline": {
            "metrics": baseline_metrics,
            "fairness": _fairness_metrics(pairs),
        },
        "selected": {
            "strength": configuration["strength"],
            "metrics": selected["metrics"],
            "fairness": selected["fairness"],
            "recall_at_5_drop": selected["recall_at_5_drop"],
        },
        "api_requests_added": 0,
    }
    if float(configuration["strength"]) <= 0.0:
        return None, {**diagnostics, "mode": "none", "reason": "identity_selected"}
    return configuration, diagnostics
