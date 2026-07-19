from __future__ import annotations

import argparse
import math
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


@dataclass(frozen=True, slots=True)
class RankerExample:
    label: int
    features: Mapping[str, float]
    weight: float = 1.0
    group_id: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], index: int) -> RankerExample:
        raw_label = payload.get("label")
        if isinstance(raw_label, bool):
            label = int(raw_label)
        elif isinstance(raw_label, int) and raw_label in {0, 1}:
            label = raw_label
        else:
            raise SearchLearningToolError(f"examples[{index}].label must be 0 or 1.")
        raw_features = payload.get("features")
        if not isinstance(raw_features, Mapping) or not raw_features:
            raise SearchLearningToolError(
                f"examples[{index}].features must be a non-empty object."
            )
        features: dict[str, float] = {}
        for raw_name, raw_value in raw_features.items():
            name = str(raw_name).strip()
            if not name or len(name) > 128:
                raise SearchLearningToolError(
                    f"examples[{index}] has an invalid feature name."
                )
            features[name] = finite_number(
                raw_value, f"examples[{index}].features.{name}"
            )
        weight = positive_number(
            payload.get("weight", 1.0), f"examples[{index}].weight"
        )
        group_id = str(
            payload.get("group_id")
            or payload.get("session_id")
            or payload.get("query_id")
            or index
        )
        return cls(label=label, features=features, weight=weight, group_id=group_id)


def _metrics(
    examples: Sequence[RankerExample], intercept: float, weights: Mapping[str, float]
) -> dict[str, float | int]:
    if not examples:
        return {"sample_count": 0, "log_loss": 0.0, "brier_score": 0.0, "accuracy": 0.0}
    weight_sum = 0.0
    log_loss = 0.0
    brier = 0.0
    correct = 0.0
    for example in examples:
        score = intercept + sum(
            weights.get(name, 0.0) * value for name, value in example.features.items()
        )
        probability = min(1.0 - 1e-12, max(1e-12, stable_sigmoid(score)))
        label = float(example.label)
        log_loss -= example.weight * (
            label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability)
        )
        brier += example.weight * (probability - label) ** 2
        correct += example.weight * ((probability >= 0.5) == bool(example.label))
        weight_sum += example.weight
    return {
        "sample_count": len(examples),
        "log_loss": log_loss / weight_sum,
        "brier_score": brier / weight_sum,
        "accuracy": correct / weight_sum,
    }


def train_ranker(
    examples: Sequence[RankerExample],
    *,
    model_version: str,
    epochs: int = 500,
    learning_rate: float = 0.08,
    l2: float = 0.002,
    validation_ratio: float = 0.2,
    seed: str = "zvec-search-learning-v1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit a deterministic weighted logistic model without runtime dependencies."""

    if len(examples) < 4:
        raise SearchLearningToolError(
            "Ranker training requires at least four examples."
        )
    if {example.label for example in examples} != {0, 1}:
        raise SearchLearningToolError(
            "Ranker training requires positive and negative labels."
        )
    if not model_version.strip() or len(model_version) > 128:
        raise SearchLearningToolError("model_version must be a non-empty short string.")
    if isinstance(epochs, bool) or epochs < 1 or epochs > 100_000:
        raise SearchLearningToolError("epochs must be between 1 and 100000.")
    learning_rate = positive_number(learning_rate, "learning_rate")
    l2 = finite_number(l2, "l2")
    if l2 < 0:
        raise SearchLearningToolError("l2 cannot be negative.")

    mask = deterministic_validation_mask(
        (example.group_id for example in examples), ratio=validation_ratio, seed=seed
    )
    training = [
        example
        for example, held_out in zip(examples, mask, strict=True)
        if not held_out
    ]
    validation = [
        example for example, held_out in zip(examples, mask, strict=True) if held_out
    ]
    # Small corpora can hash entirely into one side. Preserve a usable, explicit
    # holdout without allowing the same group to leak across both sets.
    if not training or {item.label for item in training} != {0, 1}:
        training = list(examples)
        validation = []

    feature_names = sorted({name for item in training for name in item.features})
    weights = dict.fromkeys(feature_names, 0.0)
    positive_weight = sum(item.weight for item in training if item.label == 1)
    negative_weight = sum(item.weight for item in training if item.label == 0)
    intercept = math.log(max(positive_weight, 1e-9) / max(negative_weight, 1e-9))
    total_weight = sum(item.weight for item in training)

    for epoch in range(epochs):
        intercept_gradient = 0.0
        gradients = dict.fromkeys(feature_names, 0.0)
        for item in training:
            linear = intercept + sum(
                weights.get(name, 0.0) * value for name, value in item.features.items()
            )
            error = (stable_sigmoid(linear) - item.label) * item.weight
            intercept_gradient += error
            for name, value in item.features.items():
                if name in gradients:
                    gradients[name] += error * value
        step = learning_rate / math.sqrt(epoch + 1.0)
        intercept -= step * intercept_gradient / total_weight
        for name in feature_names:
            gradient = gradients[name] / total_weight + l2 * weights[name]
            weights[name] -= step * gradient

    rounded_weights = {name: round(value, 12) for name, value in weights.items()}
    model = {
        "schema_version": 1,
        "model_version": model_version.strip(),
        "feature_schema_version": 1,
        "kind": "logistic",
        "intercept": round(intercept, 12),
        "weights": rounded_weights,
    }
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": model["model_version"],
        "feature_count": len(feature_names),
        "training": _metrics(training, intercept, rounded_weights),
        "validation": _metrics(validation, intercept, rounded_weights),
        "validation_group_count": len({item.group_id for item in validation}),
        "activation_allowed": False,
        "activation_note": (
            "The candidate must pass the frozen search-quality comparison gate "
            "before activation."
        ),
    }
    return model, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a compact Zvec search ranker.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=0.08)
    parser.add_argument("--l2", type=float, default=0.002)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--seed", default="zvec-search-learning-v1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        examples = [
            RankerExample.from_mapping(record, index)
            for index, record in enumerate(load_records(args.input))
        ]
        model, report = train_ranker(
            examples,
            model_version=args.model_version,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
            l2=args.l2,
            validation_ratio=args.validation_ratio,
            seed=args.seed,
        )
        atomic_write_json(args.output, model)
        if args.report is not None:
            atomic_write_json(args.report, report)
    except (OSError, SearchLearningToolError) as exc:
        _parser().error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
