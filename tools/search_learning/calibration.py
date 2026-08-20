from __future__ import annotations

import argparse
import bisect
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import (
    SearchLearningToolError,
    atomic_write_json,
    deterministic_validation_mask,
    finite_number,
    load_records,
    positive_number,
    stable_sigmoid,
)

HARD_MINIMUM_CONFIDENCE = 0.20


@dataclass(frozen=True, slots=True)
class CalibrationExample:
    score: float
    label: int
    query_type: str
    collection_id: str = ""
    group_id: str = ""
    weight: float = 1.0
    rank: int = 0
    has_answer: bool | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], index: int) -> CalibrationExample:
        raw_label = payload.get("label")
        if isinstance(raw_label, bool):
            label = int(raw_label)
        elif isinstance(raw_label, int) and raw_label in {0, 1}:
            label = raw_label
        else:
            raise SearchLearningToolError(f"examples[{index}].label must be 0 or 1.")
        raw_score = payload.get(
            "score", payload.get("ranking_score", payload.get("confidence"))
        )
        score = finite_number(raw_score, f"examples[{index}].score")
        query_type = str(payload.get("query_type") or "global").strip().lower()
        if not query_type or len(query_type) > 64:
            raise SearchLearningToolError(f"examples[{index}].query_type is invalid.")
        collection_id = str(
            payload.get("collection_id") or payload.get("library_id") or ""
        ).strip()
        if len(collection_id) > 128:
            raise SearchLearningToolError(
                f"examples[{index}].collection_id is too long."
            )
        group_id = str(
            payload.get("group_id")
            or payload.get("session_id")
            or payload.get("query_id")
            or index
        )
        weight = positive_number(
            payload.get("weight", 1.0), f"examples[{index}].weight"
        )
        raw_rank = payload.get("rank", 0)
        if isinstance(raw_rank, bool) or not isinstance(raw_rank, int) or raw_rank < 0:
            raise SearchLearningToolError(
                f"examples[{index}].rank must be a non-negative integer."
            )
        raw_has_answer = payload.get("has_answer")
        if raw_has_answer is not None and not isinstance(raw_has_answer, bool):
            raise SearchLearningToolError(
                f"examples[{index}].has_answer must be a boolean or null."
            )
        return cls(
            score=score,
            label=label,
            query_type=query_type,
            collection_id=collection_id,
            group_id=group_id,
            weight=weight,
            rank=raw_rank,
            has_answer=raw_has_answer,
        )


def _fit_platt(
    examples: Sequence[CalibrationExample], *, epochs: int = 800
) -> dict[str, Any]:
    positives = sum(item.weight for item in examples if item.label == 1)
    negatives = sum(item.weight for item in examples if item.label == 0)
    if positives <= 0 or negatives <= 0:
        raise SearchLearningToolError(
            "Calibration requires both positive and negative examples."
        )
    a = 1.0
    b = math.log(positives / negatives)
    total_weight = positives + negatives
    for epoch in range(epochs):
        gradient_a = 0.0
        gradient_b = 0.0
        for item in examples:
            error = (stable_sigmoid(a * item.score + b) - item.label) * item.weight
            gradient_a += error * item.score
            gradient_b += error
        step = 0.08 / math.sqrt(epoch + 1.0)
        a -= step * gradient_a / total_weight
        b -= step * gradient_b / total_weight
    return {"method": "platt", "a": round(a, 12), "b": round(b, 12)}


def _fit_isotonic(examples: Sequence[CalibrationExample]) -> dict[str, Any]:
    grouped: list[tuple[float, float, float]] = []
    by_score: dict[float, list[CalibrationExample]] = defaultdict(list)
    for item in examples:
        by_score[item.score].append(item)
    for score in sorted(by_score):
        group = by_score[score]
        weight = sum(item.weight for item in group)
        positive = sum(item.weight * item.label for item in group)
        grouped.append((score, positive, weight))

    # Pool-adjacent-violators produces a deterministic monotonic mapping.
    blocks: list[dict[str, float]] = []
    for score, positive, weight in grouped:
        blocks.append(
            {
                "x_weighted": score * weight,
                "positive": positive,
                "weight": weight,
            }
        )
        while len(blocks) >= 2:
            left = blocks[-2]
            right = blocks[-1]
            if left["positive"] / left["weight"] <= right["positive"] / right["weight"]:
                break
            blocks[-2:] = [
                {
                    "x_weighted": left["x_weighted"] + right["x_weighted"],
                    "positive": left["positive"] + right["positive"],
                    "weight": left["weight"] + right["weight"],
                }
            ]
    points = [
        [
            round(block["x_weighted"] / block["weight"], 12),
            round(block["positive"] / block["weight"], 12),
        ]
        for block in blocks
    ]
    if len(points) < 2:
        raise SearchLearningToolError(
            "Isotonic calibration needs at least two distinct monotonic blocks."
        )
    return {"method": "isotonic", "points": points}


def apply_calibrator(calibrator: Mapping[str, Any], score: float) -> float:
    method = str(calibrator.get("method") or "").lower()
    if method == "platt":
        a = finite_number(calibrator.get("a"), "calibrator.a")
        b = finite_number(calibrator.get("b"), "calibrator.b")
        return stable_sigmoid(a * score + b)
    if method != "isotonic":
        raise SearchLearningToolError(f"Unsupported calibration method: {method!r}")
    raw_points = calibrator.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise SearchLearningToolError(
            "Isotonic calibrator requires at least two points."
        )
    points: list[tuple[float, float]] = []
    previous_x = -math.inf
    previous_y = 0.0
    for index, point in enumerate(raw_points):
        if not isinstance(point, list) or len(point) != 2:
            raise SearchLearningToolError(f"calibrator.points[{index}] is invalid.")
        x = finite_number(point[0], f"calibrator.points[{index}][0]")
        y = finite_number(point[1], f"calibrator.points[{index}][1]")
        if x <= previous_x or not 0.0 <= y <= 1.0 or y < previous_y:
            raise SearchLearningToolError(
                "Isotonic points must have increasing x and non-decreasing y in [0, 1]."
            )
        points.append((x, y))
        previous_x, previous_y = x, y
    if score <= points[0][0]:
        return points[0][1]
    if score >= points[-1][0]:
        return points[-1][1]
    xs = [point[0] for point in points]
    right_index = bisect.bisect_right(xs, score)
    left = points[right_index - 1]
    right = points[right_index]
    ratio = (score - left[0]) / (right[0] - left[0])
    return left[1] + ratio * (right[1] - left[1])


def _brier(
    examples: Sequence[CalibrationExample], calibrator: Mapping[str, Any]
) -> float:
    total = sum(item.weight for item in examples)
    if total <= 0:
        return 0.0
    return (
        sum(
            item.weight * (apply_calibrator(calibrator, item.score) - item.label) ** 2
            for item in examples
        )
        / total
    )


def _select_threshold(
    examples: Sequence[CalibrationExample], calibrator: Mapping[str, Any]
) -> float:
    pairs = [
        (apply_calibrator(calibrator, item.score), item.label, item.weight)
        for item in examples
    ]
    positive_total = sum(weight for _, label, weight in pairs if label == 1)
    negative_total = sum(weight for _, label, weight in pairs if label == 0)
    if positive_total <= 0 or negative_total <= 0:
        return HARD_MINIMUM_CONFIDENCE
    no_answer_groups = {item.group_id for item in examples if item.has_answer is False}

    def measures(threshold: float) -> tuple[float, float, float]:
        true_positive = sum(
            weight
            for score, label, weight in pairs
            if score >= threshold and label == 1
        )
        false_positive = sum(
            weight
            for score, label, weight in pairs
            if score >= threshold and label == 0
        )
        selected = true_positive + false_positive
        precision = true_positive / selected if selected else 1.0
        recall = true_positive / positive_total
        if no_answer_groups:
            false_returned = {
                item.group_id
                for item in examples
                if item.has_answer is False
                and apply_calibrator(calibrator, item.score) >= threshold
            }
            false_positive_rate = len(false_returned) / len(no_answer_groups)
        else:
            false_positive_rate = false_positive / negative_total
        return precision, recall, false_positive_rate

    baseline_recall = measures(HARD_MINIMUM_CONFIDENCE)[1]
    minimum_recall = max(0.0, baseline_recall - 0.03)
    candidates = sorted(
        {HARD_MINIMUM_CONFIDENCE, 1.0, *(score for score, _, _ in pairs)}
    )
    eligible: list[tuple[float, float, float]] = []
    for threshold in candidates:
        if threshold < HARD_MINIMUM_CONFIDENCE:
            continue
        precision, recall, false_positive_rate = measures(threshold)
        if false_positive_rate <= 0.10 + 1e-12 and recall >= minimum_recall - 1e-12:
            eligible.append((precision, recall, threshold))
    if not eligible:
        return HARD_MINIMUM_CONFIDENCE
    # Precision is primary; recall wins ties. A higher threshold is preferred
    # only after both quality metrics tie.
    return round(max(eligible, key=lambda item: (item[0], item[1], item[2]))[2], 12)


def _fit_group(
    examples: Sequence[CalibrationExample], *, seed: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(examples) < 4 or {item.label for item in examples} != {0, 1}:
        raise SearchLearningToolError(
            "Each calibration group requires positive and negative examples."
        )
    method = "platt"
    comparison: dict[str, float] = {}
    if len(examples) >= 500:
        mask = deterministic_validation_mask(
            (item.group_id for item in examples), ratio=0.2, seed=seed
        )
        training = [
            item for item, held_out in zip(examples, mask, strict=True) if not held_out
        ]
        validation = [
            item for item, held_out in zip(examples, mask, strict=True) if held_out
        ]
        if (
            validation
            and {item.label for item in training} == {0, 1}
            and {item.label for item in validation} == {0, 1}
        ):
            platt = _fit_platt(training)
            comparison["platt_brier"] = _brier(validation, platt)
            try:
                isotonic = _fit_isotonic(training)
            except SearchLearningToolError:
                pass
            else:
                comparison["isotonic_brier"] = _brier(validation, isotonic)
                if comparison["isotonic_brier"] + 1e-6 < comparison["platt_brier"]:
                    method = "isotonic"
    calibrator = (
        _fit_isotonic(examples) if method == "isotonic" else _fit_platt(examples)
    )
    calibrator["minimum_confidence"] = _select_threshold(examples, calibrator)
    report = {
        "sample_count": len(examples),
        "positive_count": sum(item.label for item in examples),
        "method": method,
        "brier_score": _brier(examples, calibrator),
        "minimum_confidence": calibrator["minimum_confidence"],
        **comparison,
    }
    return calibrator, report


def build_calibration_registry(
    examples: Sequence[CalibrationExample],
    *,
    calibration_version: str,
    minimum_query_samples: int = 20,
    minimum_collection_samples: int = 50,
    seed: str = "zvec-search-calibration-v1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(examples) < 4 or {item.label for item in examples} != {0, 1}:
        raise SearchLearningToolError(
            "Calibration requires at least four positive and negative examples."
        )
    if not calibration_version.strip() or len(calibration_version) > 128:
        raise SearchLearningToolError(
            "calibration_version must be a non-empty short string."
        )
    if minimum_query_samples < 4 or minimum_collection_samples < 4:
        raise SearchLearningToolError("Minimum group sizes must be at least four.")

    global_calibrator, global_report = _fit_group(examples, seed=f"{seed}:global")
    query_types: dict[str, Any] = {}
    query_reports: dict[str, Any] = {}
    grouped_queries: dict[str, list[CalibrationExample]] = defaultdict(list)
    for item in examples:
        grouped_queries[item.query_type].append(item)
    for query_type, group in sorted(grouped_queries.items()):
        if len(group) < minimum_query_samples or {item.label for item in group} != {
            0,
            1,
        }:
            continue
        query_types[query_type], query_reports[query_type] = _fit_group(
            group, seed=f"{seed}:query:{query_type}"
        )

    collection_groups: dict[tuple[str, str], list[CalibrationExample]] = defaultdict(
        list
    )
    for item in examples:
        if item.collection_id:
            collection_groups[(item.collection_id, item.query_type)].append(item)
    collections: dict[str, dict[str, Any]] = {}
    collection_reports: dict[str, dict[str, Any]] = {}
    for (collection_id, query_type), group in sorted(collection_groups.items()):
        if len(group) < minimum_collection_samples or {
            item.label for item in group
        } != {0, 1}:
            continue
        calibrator, group_report = _fit_group(
            group, seed=f"{seed}:collection:{collection_id}:{query_type}"
        )
        collections.setdefault(collection_id, {})[query_type] = calibrator
        collection_reports.setdefault(collection_id, {})[query_type] = group_report

    registry = {
        "schema_version": 1,
        "calibration_version": calibration_version.strip(),
        "global": global_calibrator,
        "query_types": query_types,
        "collections": collections,
    }
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibration_version": registry["calibration_version"],
        "hard_minimum_confidence": HARD_MINIMUM_CONFIDENCE,
        "global": global_report,
        "query_types": query_reports,
        "collections": collection_reports,
        "activation_allowed": False,
        "activation_note": (
            "Use a frozen validation set and the release quality gate "
            "before activation."
        ),
    }
    return registry, report


def calibration_metrics(
    examples: Sequence[CalibrationExample],
    calibrator: Mapping[str, Any],
    *,
    bins: int = 10,
) -> dict[str, Any]:
    if bins < 2 or bins > 100:
        raise SearchLearningToolError("bins must be between 2 and 100.")
    if not examples:
        raise SearchLearningToolError("Evaluation requires at least one example.")
    rows = [
        (apply_calibrator(calibrator, item.score), item.label, item.weight, item)
        for item in examples
    ]
    total_weight = sum(row[2] for row in rows)
    brier = (
        sum(weight * (score - label) ** 2 for score, label, weight, _ in rows)
        / total_weight
    )
    reliability: list[dict[str, Any]] = []
    ece = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            row
            for row in rows
            if (
                lower <= row[0] <= upper
                if index == bins - 1
                else lower <= row[0] < upper
            )
        ]
        bucket_weight = sum(row[2] for row in bucket)
        if bucket_weight <= 0:
            continue
        mean_confidence = sum(row[0] * row[2] for row in bucket) / bucket_weight
        observed_rate = sum(row[1] * row[2] for row in bucket) / bucket_weight
        ece += bucket_weight / total_weight * abs(mean_confidence - observed_rate)
        reliability.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(bucket),
                "mean_confidence": mean_confidence,
                "observed_relevance": observed_rate,
            }
        )

    threshold = max(
        HARD_MINIMUM_CONFIDENCE,
        finite_number(
            calibrator.get("minimum_confidence", HARD_MINIMUM_CONFIDENCE),
            "minimum_confidence",
        ),
    )
    selected = [row for row in rows if row[0] >= threshold]
    positives = sum(row[2] for row in rows if row[1] == 1)
    true_positive = sum(row[2] for row in selected if row[1] == 1)
    false_positive = sum(row[2] for row in selected if row[1] == 0)
    negative_total = sum(row[2] for row in rows if row[1] == 0)
    groups: dict[str, list[tuple[float, int, float, CalibrationExample]]] = defaultdict(
        list
    )
    for row in rows:
        groups[row[3].group_id].append(row)
    no_answer_groups = {item.group_id for item in examples if item.has_answer is False}
    false_return_groups = {
        item.group_id
        for score, _label, _weight, item in rows
        if item.has_answer is False and score >= threshold
    }
    top15_rows = []
    for group in groups.values():
        top15_rows.extend(
            sorted(group, key=lambda row: (-row[0], row[3].rank or 2**31))[:15]
        )
    top15_selected = [row for row in top15_rows if row[0] >= threshold]
    top15_true = sum(row[2] for row in top15_selected if row[1] == 1)
    top15_total = sum(row[2] for row in top15_selected)
    return {
        "sample_count": len(examples),
        "group_count": len(groups),
        "brier_score": brier,
        "expected_calibration_error": ece,
        "minimum_confidence": threshold,
        "precision": true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1.0,
        "recall": true_positive / positives if positives else 0.0,
        "false_positive_rate": false_positive / negative_total
        if negative_total
        else 0.0,
        "no_answer_false_return_rate": (
            len(false_return_groups) / len(no_answer_groups)
            if no_answer_groups
            else None
        ),
        "precision_at_15": top15_true / top15_total if top15_total else 1.0,
        "recall_at_15": top15_true / positives if positives else 0.0,
        "average_returned_count": len(selected) / max(1, len(groups)),
        "reliability": reliability,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fit versioned Platt/isotonic confidence calibration."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--calibration-version", required=True)
    parser.add_argument("--minimum-query-samples", type=int, default=20)
    parser.add_argument("--minimum-collection-samples", type=int, default=50)
    parser.add_argument("--seed", default="zvec-search-calibration-v1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        examples = [
            CalibrationExample.from_mapping(record, index)
            for index, record in enumerate(load_records(args.input))
        ]
        registry, report = build_calibration_registry(
            examples,
            calibration_version=args.calibration_version,
            minimum_query_samples=args.minimum_query_samples,
            minimum_collection_samples=args.minimum_collection_samples,
            seed=args.seed,
        )
        atomic_write_json(args.output, registry)
        if args.report is not None:
            atomic_write_json(args.report, report)
    except (OSError, SearchLearningToolError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
