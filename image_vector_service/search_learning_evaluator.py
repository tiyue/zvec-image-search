"""Local fixed-set evaluator for learned search-ranking candidates.

The evaluator consumes a versioned, human-labelled feature pack.  It performs
no search, filesystem image access, embedding generation, or external API call.
Missing packs keep candidates pending; malformed packs fail closed.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .learning_ranker import RankingModel, ranking_model_from_mapping
from .search_features import (
    FEATURE_SCHEMA_VERSION,
    SearchFeatureError,
    SearchFeatures,
    normalize_query_type,
)
from .search_learning_config import (
    HARD_MINIMUM_CONFIDENCE,
    load_search_learning,
)

FIXED_EVALUATION_FILENAME: Final = "fixed-evaluation.json"
FIXED_EVALUATION_SCHEMA_VERSION: Final = 1
MAX_EVALUATION_FILE_BYTES: Final = 16 * 1024 * 1024
MAX_CASES: Final = 10_000
MAX_CANDIDATES_PER_CASE: Final = 500
MIN_FIXED_CASES: Final = 10


class FixedEvaluationError(RuntimeError):
    """Base error for a local fixed evaluation pack."""

    code = "fixed_evaluator_failed"


class FixedEvaluationUnavailable(FixedEvaluationError):
    """No fixed pack is installed; candidate activation must remain pending."""

    code = "fixed_evaluator_unavailable"


class FixedEvaluationPackError(FixedEvaluationError, ValueError):
    """A present evaluation pack is malformed or unsafe."""

    code = "fixed_evaluation_pack_invalid"


@dataclass(frozen=True, slots=True)
class FixedCandidate:
    doc_id: str
    library_id: str
    relevant: bool
    fallback_score: float
    fallback_rank: int
    features: SearchFeatures


@dataclass(frozen=True, slots=True)
class FixedCase:
    query_id: str
    query_type: str
    has_answer: bool
    latency_ms: float
    candidates: tuple[FixedCandidate, ...]


@dataclass(frozen=True, slots=True)
class FixedEvaluationPack:
    evaluation_set_id: str
    cases: tuple[FixedCase, ...]

    @property
    def no_answer_count(self) -> int:
        return sum(not item.has_answer for item in self.cases)

    @property
    def candidate_count(self) -> int:
        return sum(len(item.candidates) for item in self.cases)


class LocalFixedEvaluationEvaluator:
    """Callable evaluator injected into :class:`SearchLearningService`."""

    def __init__(self, config_home: str | Path) -> None:
        self.config_home = Path(config_home).expanduser().resolve()
        self.path = self.config_home / "search-learning" / FIXED_EVALUATION_FILENAME

    def status(self) -> dict[str, object]:
        try:
            pack = self.load()
        except FixedEvaluationUnavailable:
            return {
                "available": False,
                "status": "missing",
                "file": FIXED_EVALUATION_FILENAME,
            }
        except FixedEvaluationPackError as exc:
            return {
                "available": False,
                "status": "invalid",
                "file": FIXED_EVALUATION_FILENAME,
                "error_code": exc.code,
            }
        return {
            "available": True,
            "status": "ready",
            "file": FIXED_EVALUATION_FILENAME,
            "evaluation_set_id": pack.evaluation_set_id,
            "case_count": len(pack.cases),
            "no_answer_count": pack.no_answer_count,
            "candidate_count": pack.candidate_count,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        }

    def load(self) -> FixedEvaluationPack:
        return self._load_path(self.path, missing_is_unavailable=True)[0]

    def install(self, source_path: str | Path) -> dict[str, object]:
        """Validate and atomically install a user-selected fixed pack."""

        source = Path(source_path).expanduser()
        if not source.is_absolute():
            raise FixedEvaluationPackError(
                "Fixed evaluation source must be an absolute path."
            )
        source = source.resolve()
        pack, payload = self._load_path(source, missing_is_unavailable=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise FixedEvaluationPackError(
                "Could not install the fixed evaluation pack."
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return {
            "installed": True,
            "evaluation_set_id": pack.evaluation_set_id,
            "case_count": len(pack.cases),
            "no_answer_count": pack.no_answer_count,
            "candidate_count": pack.candidate_count,
            "external_api_calls": 0,
        }

    def _load_path(
        self,
        path: Path,
        *,
        missing_is_unavailable: bool,
    ) -> tuple[FixedEvaluationPack, Mapping[str, object]]:
        if not path.is_file():
            if missing_is_unavailable:
                raise FixedEvaluationUnavailable(
                    "No fixed search-learning evaluation pack is installed."
                )
            raise FixedEvaluationPackError(
                "The selected fixed evaluation pack does not exist."
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise FixedEvaluationPackError(
                "Could not inspect the fixed evaluation pack."
            ) from exc
        if size <= 0 or size > MAX_EVALUATION_FILE_BYTES:
            raise FixedEvaluationPackError(
                "The fixed evaluation pack has an invalid size."
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FixedEvaluationPackError(
                "The fixed evaluation pack is unreadable."
            ) from exc
        pack = fixed_evaluation_pack_from_mapping(payload)
        if not isinstance(payload, Mapping):  # Kept explicit for type narrowing.
            raise FixedEvaluationPackError("Fixed evaluation pack must be an object.")
        return pack, payload

    def __call__(self, model_payload: Mapping[str, Any]) -> dict[str, object]:
        pack = self.load()
        candidate = ranking_model_from_mapping(model_payload)
        bundle = load_search_learning(self.config_home)
        current = (
            bundle.ranker_model if bundle.enabled and not bundle.shadow_mode else None
        )
        current_metrics = _evaluate_model(pack, current)
        candidate_metrics = _evaluate_model(pack, candidate)
        return {
            "fixed_evaluation_set": True,
            "evaluation_set_id": pack.evaluation_set_id,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "current_model_version": (
                current.model_version if current is not None else "fallback"
            ),
            "candidate_model_version": candidate.model_version,
            "current": current_metrics,
            "candidate": candidate_metrics,
        }


def fixed_evaluation_pack_from_mapping(value: object) -> FixedEvaluationPack:
    if not isinstance(value, Mapping):
        raise FixedEvaluationPackError("Fixed evaluation pack must be an object.")
    allowed = {
        "schema_version",
        "fixed_evaluation_set",
        "evaluation_set_id",
        "feature_schema_version",
        "generated_at",
        "cases",
    }
    unknown = set(value) - allowed
    if unknown:
        raise FixedEvaluationPackError(
            "Fixed evaluation pack has unknown fields: " + ", ".join(sorted(unknown))
        )
    if value.get("schema_version") != FIXED_EVALUATION_SCHEMA_VERSION:
        raise FixedEvaluationPackError(
            "Unsupported fixed evaluation pack schema version."
        )
    if value.get("fixed_evaluation_set") is not True:
        raise FixedEvaluationPackError("fixed_evaluation_set must be explicitly true.")
    if value.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise FixedEvaluationPackError(
            "Fixed evaluation features are incompatible with this application."
        )
    evaluation_set_id = _short_text(
        value.get("evaluation_set_id"), "evaluation_set_id", maximum=128
    )
    raw_cases = value.get("cases")
    if (
        not isinstance(raw_cases, list)
        or not MIN_FIXED_CASES <= len(raw_cases) <= MAX_CASES
    ):
        raise FixedEvaluationPackError(
            f"Fixed evaluation pack must contain {MIN_FIXED_CASES} to "
            f"{MAX_CASES} cases."
        )
    cases = tuple(
        _case_from_mapping(item, index) for index, item in enumerate(raw_cases)
    )
    query_ids = [item.query_id for item in cases]
    if len(set(query_ids)) != len(query_ids):
        raise FixedEvaluationPackError("Fixed evaluation query ids must be unique.")
    if not any(item.has_answer for item in cases) or not any(
        not item.has_answer for item in cases
    ):
        raise FixedEvaluationPackError(
            "Fixed evaluation pack requires answered and no-answer cases."
        )
    if not any(item.candidates for item in cases):
        raise FixedEvaluationPackError(
            "Fixed evaluation pack must contain at least one candidate."
        )
    return FixedEvaluationPack(evaluation_set_id=evaluation_set_id, cases=cases)


def _case_from_mapping(value: object, index: int) -> FixedCase:
    if not isinstance(value, Mapping):
        raise FixedEvaluationPackError(f"cases[{index}] must be an object.")
    allowed = {"query_id", "query_type", "has_answer", "latency_ms", "candidates"}
    unknown = set(value) - allowed
    if unknown:
        raise FixedEvaluationPackError(
            f"cases[{index}] has unknown fields: " + ", ".join(sorted(unknown))
        )
    query_id = _short_text(value.get("query_id"), f"cases[{index}].query_id")
    try:
        query_type = normalize_query_type(
            _short_text(value.get("query_type"), f"cases[{index}].query_type")
        )
    except SearchFeatureError as exc:
        raise FixedEvaluationPackError(str(exc)) from exc
    has_answer = value.get("has_answer")
    if not isinstance(has_answer, bool):
        raise FixedEvaluationPackError(f"cases[{index}].has_answer must be a boolean.")
    latency_ms = _non_negative_number(
        value.get("latency_ms"), f"cases[{index}].latency_ms"
    )
    raw_candidates = value.get("candidates")
    if (
        not isinstance(raw_candidates, list)
        or len(raw_candidates) > MAX_CANDIDATES_PER_CASE
    ):
        raise FixedEvaluationPackError(
            f"cases[{index}].candidates must contain at most "
            f"{MAX_CANDIDATES_PER_CASE} items."
        )
    candidates = tuple(
        _candidate_from_mapping(item, index, candidate_index, query_type)
        for candidate_index, item in enumerate(raw_candidates)
    )
    keys = [(item.library_id, item.doc_id) for item in candidates]
    ranks = [item.fallback_rank for item in candidates]
    if len(set(keys)) != len(keys) or len(set(ranks)) != len(ranks):
        raise FixedEvaluationPackError(
            f"cases[{index}] has duplicate candidates or fallback ranks."
        )
    has_relevant = any(item.relevant for item in candidates)
    if has_answer != has_relevant:
        raise FixedEvaluationPackError(
            f"cases[{index}].has_answer does not match its relevance labels."
        )
    return FixedCase(
        query_id=query_id,
        query_type=query_type,
        has_answer=has_answer,
        latency_ms=latency_ms,
        candidates=candidates,
    )


def _candidate_from_mapping(
    value: object,
    case_index: int,
    candidate_index: int,
    query_type: str,
) -> FixedCandidate:
    prefix = f"cases[{case_index}].candidates[{candidate_index}]"
    if not isinstance(value, Mapping):
        raise FixedEvaluationPackError(f"{prefix} must be an object.")
    allowed = {
        "doc_id",
        "library_id",
        "relevant",
        "fallback_score",
        "fallback_rank",
        "features",
    }
    unknown = set(value) - allowed
    if unknown:
        raise FixedEvaluationPackError(
            f"{prefix} has unknown fields: " + ", ".join(sorted(unknown))
        )
    relevant = value.get("relevant")
    if not isinstance(relevant, bool):
        raise FixedEvaluationPackError(f"{prefix}.relevant must be a boolean.")
    fallback_rank = value.get("fallback_rank")
    if (
        isinstance(fallback_rank, bool)
        or not isinstance(fallback_rank, int)
        or fallback_rank < 1
    ):
        raise FixedEvaluationPackError(
            f"{prefix}.fallback_rank must be a positive integer."
        )
    raw_features = value.get("features")
    if not isinstance(raw_features, Mapping):
        raise FixedEvaluationPackError(f"{prefix}.features must be an object.")
    try:
        features = SearchFeatures.from_mapping(raw_features)
    except SearchFeatureError as exc:
        raise FixedEvaluationPackError(f"{prefix}: {exc}") from exc
    if features.query_type != query_type:
        raise FixedEvaluationPackError(
            f"{prefix}.features.query_type does not match its case."
        )
    fallback_score = _unit_number(
        value.get("fallback_score"), f"{prefix}.fallback_score"
    )
    return FixedCandidate(
        doc_id=_short_text(value.get("doc_id"), f"{prefix}.doc_id", maximum=512),
        library_id=_short_text(
            value.get("library_id"), f"{prefix}.library_id", maximum=256
        ),
        relevant=relevant,
        fallback_score=fallback_score,
        fallback_rank=fallback_rank,
        features=features,
    )


def _evaluate_model(
    pack: FixedEvaluationPack,
    model: RankingModel | None,
) -> dict[str, object]:
    relevant_total = 0
    relevant_returned = 0
    returned_total = 0
    no_answer_total = 0
    no_answer_false_returns = 0
    collection_biases: list[float] = []
    latencies: list[float] = []

    for case in pack.cases:
        started = time.perf_counter()
        scored: list[tuple[FixedCandidate, float]] = []
        for item in case.candidates:
            score = (
                model.predict_score(item.features)
                if model is not None
                else item.fallback_score
            )
            scored.append((item, score))
        ordered = sorted(
            scored,
            key=(
                (lambda row: (-row[1], row[0].fallback_rank, row[0].library_id))
                if model is not None
                else (lambda row: row[0].fallback_rank)
            ),
        )
        selected = [row for row in ordered if row[1] >= HARD_MINIMUM_CONFIDENCE][:15]
        elapsed_ms = (time.perf_counter() - started) * 1_000.0
        latencies.append(case.latency_ms + elapsed_ms)

        relevant_total += sum(item.relevant for item in case.candidates)
        relevant_returned += sum(item.relevant for item, _score in selected)
        returned_total += len(selected)
        if not case.has_answer:
            no_answer_total += 1
            no_answer_false_returns += bool(selected)
        else:
            collection_biases.append(_collection_bias(case.candidates, selected))

    return {
        "precision_at_15": (
            relevant_returned / returned_total if returned_total else 1.0
        ),
        "recall_at_15": (relevant_returned / relevant_total if relevant_total else 0.0),
        "no_answer_false_positive_rate": (
            no_answer_false_returns / no_answer_total if no_answer_total else 0.0
        ),
        "cross_collection_bias": (
            sum(collection_biases) / len(collection_biases)
            if collection_biases
            else 0.0
        ),
        "p95_latency_ms": _percentile_95(latencies),
        "external_api_calls": 0,
        "returned_count": returned_total,
        "relevant_returned_count": relevant_returned,
        "case_count": len(pack.cases),
    }


def _collection_bias(
    candidates: Sequence[FixedCandidate],
    selected: Sequence[tuple[FixedCandidate, float]],
) -> float:
    expected_counts: dict[str, int] = defaultdict(int)
    for item in candidates:
        if item.relevant:
            expected_counts[item.library_id] += 1
    observed_counts: dict[str, int] = defaultdict(int)
    for item, _score in selected:
        observed_counts[item.library_id] += 1
    libraries = set(expected_counts) | set(observed_counts)
    expected_total = sum(expected_counts.values())
    observed_total = sum(observed_counts.values())
    if expected_total <= 0 or observed_total <= 0:
        return 1.0 if expected_total != observed_total else 0.0
    return 0.5 * sum(
        abs(
            expected_counts[library_id] / expected_total
            - observed_counts[library_id] / observed_total
        )
        for library_id in libraries
    )


def _percentile_95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _short_text(value: object, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixedEvaluationPackError(f"{name} must be non-empty text.")
    normalized = value.strip()
    if len(normalized) > maximum or any(
        character in normalized for character in "\r\n\x00"
    ):
        raise FixedEvaluationPackError(f"{name} is too long or unsafe.")
    return normalized


def _non_negative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixedEvaluationPackError(f"{name} must be numeric.")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise FixedEvaluationPackError(f"{name} must be finite and non-negative.")
    return number


def _unit_number(value: object, name: str) -> float:
    number = _non_negative_number(value, name)
    if number > 1.0:
        raise FixedEvaluationPackError(f"{name} must be between zero and one.")
    return number
