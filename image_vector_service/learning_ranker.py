from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .search_features import (
    FEATURE_SCHEMA_VERSION,
    NUMERIC_FEATURE_NAMES,
    SearchFeatures,
)

RANKING_MODEL_SCHEMA_VERSION: Final = 1
SUPPORTED_RANKING_MODEL_KINDS: Final = frozenset({"logistic", "linear"})


class RankingModelError(ValueError):
    """Raised when a learned ranker violates its versioned JSON contract."""


@dataclass(frozen=True)
class RankingModel:
    schema_version: int
    model_version: str
    feature_schema_version: int
    intercept: float
    weights: Mapping[str, float]
    kind: str = "logistic"

    def __post_init__(self) -> None:
        if self.schema_version != RANKING_MODEL_SCHEMA_VERSION:
            raise RankingModelError(
                "Unsupported ranking model schema version: "
                f"{self.schema_version}; expected {RANKING_MODEL_SCHEMA_VERSION}."
            )
        version = str(self.model_version).strip()
        if not version or len(version) > 128:
            raise RankingModelError(
                "model_version must be a non-empty string of at most 128 characters."
            )
        object.__setattr__(self, "model_version", version)
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise RankingModelError(
                "Ranking model feature_schema_version is incompatible: "
                f"{self.feature_schema_version}; expected {FEATURE_SCHEMA_VERSION}."
            )
        kind = str(self.kind).strip().lower()
        if kind not in SUPPORTED_RANKING_MODEL_KINDS:
            raise RankingModelError("Ranking model kind must be logistic or linear.")
        object.__setattr__(self, "kind", kind)
        intercept = _finite_number(self.intercept, "intercept")
        object.__setattr__(self, "intercept", intercept)
        if not isinstance(self.weights, Mapping) or not self.weights:
            raise RankingModelError("Ranking model weights must be a non-empty object.")
        unknown = set(self.weights) - set(NUMERIC_FEATURE_NAMES)
        if unknown:
            raise RankingModelError(
                "Ranking model uses unknown features: " + ", ".join(sorted(unknown))
            )
        normalized_weights = {
            str(name): _finite_number(value, f"weights.{name}")
            for name, value in self.weights.items()
        }
        object.__setattr__(self, "weights", MappingProxyType(normalized_weights))

    def decision_function(self, features: SearchFeatures) -> float:
        self._validate_features(features)
        values = features.numeric_values()
        result = self.intercept + math.fsum(
            weight * values[name] for name, weight in self.weights.items()
        )
        if not math.isfinite(result):
            raise RankingModelError("Ranking model produced a non-finite decision.")
        return result

    def predict_score(self, features: SearchFeatures) -> float:
        decision = self.decision_function(features)
        if self.kind == "linear":
            return _unit_interval(decision)
        return _sigmoid(decision)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "kind": self.kind,
            "intercept": self.intercept,
            "weights": dict(self.weights),
        }

    def _validate_features(self, features: SearchFeatures) -> None:
        if not isinstance(features, SearchFeatures):
            raise RankingModelError("features must be a SearchFeatures instance.")
        if features.feature_schema_version != self.feature_schema_version:
            raise RankingModelError(
                "Feature row version does not match the ranking model."
            )


@dataclass(frozen=True)
class RankingInput:
    candidate_id: str
    library_id: str
    features: SearchFeatures
    fallback_score: float
    fallback_rank: int

    def __post_init__(self) -> None:
        candidate_id = str(self.candidate_id).strip()
        library_id = str(self.library_id).strip()
        if not candidate_id:
            raise RankingModelError("candidate_id must not be empty.")
        if not library_id:
            raise RankingModelError("library_id must not be empty.")
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "library_id", library_id)
        if not isinstance(self.features, SearchFeatures):
            raise RankingModelError("features must be a SearchFeatures instance.")
        object.__setattr__(
            self,
            "fallback_score",
            _unit_interval(_finite_number(self.fallback_score, "fallback_score")),
        )
        if (
            isinstance(self.fallback_rank, bool)
            or not isinstance(self.fallback_rank, int)
            or self.fallback_rank < 1
        ):
            raise RankingModelError("fallback_rank must be a positive integer.")


@dataclass(frozen=True)
class RankingPrediction:
    candidate_id: str
    library_id: str
    fallback_rank: int
    learned_rank: int
    displayed_rank: int
    ranking_score: float
    ranking_model_version: str
    feature_schema_version: int
    ranking_fallback: bool
    ranking_fallback_reason: str | None = None

    def diagnostics(self) -> dict[str, object]:
        return {
            "ranking_model_version": self.ranking_model_version,
            "ranking_score": self.ranking_score,
            "feature_schema_version": self.feature_schema_version,
            "ranking_fallback": self.ranking_fallback,
            "ranking_fallback_reason": self.ranking_fallback_reason,
            "fallback_rank": self.fallback_rank,
            "learned_rank": self.learned_rank,
            "displayed_rank": self.displayed_rank,
        }


@dataclass(frozen=True)
class RankingBatch:
    predictions: tuple[RankingPrediction, ...]
    model_version: str
    feature_schema_version: int
    applied: bool
    shadow_mode: bool
    fallback: bool
    fallback_reason: str | None

    def diagnostics(self) -> dict[str, object]:
        changed = sum(
            prediction.fallback_rank != prediction.learned_rank
            for prediction in self.predictions
        )
        return {
            "ranking_model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "applied": self.applied,
            "shadow_mode": self.shadow_mode,
            "ranking_fallback": self.fallback,
            "ranking_fallback_reason": self.fallback_reason,
            "candidate_count": len(self.predictions),
            "rank_changed_count": changed,
        }


class LearningRanker:
    """Fail-safe pure-Python learned ranker.

    A missing or rejected model preserves the exact fallback order.  Shadow
    mode computes and reports learned ranks but also preserves that order, so a
    candidate model can be evaluated before it affects the user.
    """

    def __init__(
        self,
        model: RankingModel | None,
        *,
        fallback_reason: str | None = None,
    ) -> None:
        self.model = model
        self.fallback_reason = str(fallback_reason).strip() if fallback_reason else None

    def predict(
        self,
        features: SearchFeatures,
        *,
        fallback_score: float,
    ) -> tuple[float, bool, str | None]:
        safe_fallback = _unit_interval(_finite_number(fallback_score, "fallback_score"))
        if self.model is None:
            return safe_fallback, True, self.fallback_reason or "model_unavailable"
        try:
            return self.model.predict_score(features), False, None
        except (ArithmeticError, RankingModelError, ValueError) as exc:
            # Learning must never turn a valid base search into an error.  Do
            # not expose unbounded exception text in result diagnostics.
            reason = f"inference_failed:{exc.__class__.__name__}"
            return safe_fallback, True, reason

    def rank(
        self,
        candidates: Sequence[RankingInput],
        *,
        shadow_mode: bool = False,
    ) -> RankingBatch:
        rows = list(candidates)
        _validate_unique_candidates(rows)
        scored: list[tuple[RankingInput, float, bool, str | None]] = []
        for item in rows:
            score, fallback, reason = self.predict(
                item.features,
                fallback_score=item.fallback_score,
            )
            scored.append((item, score, fallback, reason))

        fallback = self.model is None or any(row[2] for row in scored)
        fallback_reason = next(
            (row[3] for row in scored if row[3]),
            self.fallback_reason if fallback else None,
        )
        if fallback:
            learned_order = sorted(scored, key=lambda row: row[0].fallback_rank)
        else:
            learned_order = sorted(
                scored,
                key=lambda row: (
                    -row[1],
                    row[0].fallback_rank,
                    row[0].library_id,
                    row[0].candidate_id,
                ),
            )
        learned_ranks = {
            (row[0].library_id, row[0].candidate_id): rank
            for rank, row in enumerate(learned_order, start=1)
        }
        displayed_order = (
            sorted(scored, key=lambda row: row[0].fallback_rank)
            if shadow_mode or fallback
            else learned_order
        )
        model_version = self.model.model_version if self.model else "fallback"
        predictions = tuple(
            RankingPrediction(
                candidate_id=row[0].candidate_id,
                library_id=row[0].library_id,
                fallback_rank=row[0].fallback_rank,
                learned_rank=learned_ranks[(row[0].library_id, row[0].candidate_id)],
                displayed_rank=displayed_rank,
                ranking_score=row[1],
                ranking_model_version=model_version,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                ranking_fallback=fallback,
                ranking_fallback_reason=fallback_reason,
            )
            for displayed_rank, row in enumerate(displayed_order, start=1)
        )
        return RankingBatch(
            predictions=predictions,
            model_version=model_version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            applied=bool(rows) and not shadow_mode and not fallback,
            shadow_mode=shadow_mode,
            fallback=fallback,
            fallback_reason=fallback_reason,
        )


def ranking_model_from_mapping(value: Mapping[str, object]) -> RankingModel:
    if not isinstance(value, Mapping):
        raise RankingModelError("Ranking model must be a JSON object.")
    allowed = {
        "schema_version",
        "model_version",
        "feature_schema_version",
        "kind",
        "intercept",
        "weights",
    }
    unknown = set(value) - allowed
    if unknown:
        raise RankingModelError(
            "Ranking model has unknown fields: " + ", ".join(sorted(unknown))
        )
    required = allowed - {"kind"}
    missing = required - set(value)
    if missing:
        raise RankingModelError(
            "Ranking model is missing fields: " + ", ".join(sorted(missing))
        )
    weights = value["weights"]
    if not isinstance(weights, Mapping):
        raise RankingModelError("Ranking model weights must be an object.")
    return RankingModel(
        schema_version=_strict_integer(value["schema_version"], "schema_version"),
        model_version=_strict_string(value["model_version"], "model_version"),
        feature_schema_version=_strict_integer(
            value["feature_schema_version"], "feature_schema_version"
        ),
        kind=_strict_string(value.get("kind", "logistic"), "kind"),
        intercept=_finite_number(value["intercept"], "intercept"),
        weights={
            _strict_string(name, "weight name"): _finite_number(
                weight, f"weights.{name}"
            )
            for name, weight in weights.items()
        },
    )


def _validate_unique_candidates(candidates: Sequence[RankingInput]) -> None:
    keys: set[tuple[str, str]] = set()
    ranks: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, RankingInput):
            raise RankingModelError("Every candidate must be a RankingInput.")
        key = (candidate.library_id, candidate.candidate_id)
        if key in keys:
            raise RankingModelError(f"Duplicate ranking candidate: {key!r}")
        if candidate.fallback_rank in ranks:
            raise RankingModelError(
                f"Duplicate fallback_rank: {candidate.fallback_rank}"
            )
        keys.add(key)
        ranks.add(candidate.fallback_rank)


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RankingModelError(f"{name} must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise RankingModelError(f"{name} must be finite.")
    return normalized


def _strict_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RankingModelError(f"{name} must be an integer.")
    return value


def _strict_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RankingModelError(f"{name} must be a non-empty string.")
    return value.strip()


def _unit_interval(value: float) -> float:
    return min(1.0, max(0.0, value))
