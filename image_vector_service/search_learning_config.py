from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from .learning_ranker import (
    LearningRanker,
    RankingModel,
    RankingModelError,
    ranking_model_from_mapping,
)
from .search_features import SUPPORTED_QUERY_TYPES, normalize_query_type

SEARCH_LEARNING_DIRECTORY: Final = "search-learning"
ACTIVE_CONFIG_FILENAME: Final = "active.json"
ACTIVE_CONFIG_SCHEMA_VERSION: Final = 1
CALIBRATION_SCHEMA_VERSION: Final = 1
HARD_MINIMUM_CONFIDENCE: Final = 0.20
MAX_CONFIG_FILE_BYTES: Final = 1_048_576


class SearchLearningConfigurationError(ValueError):
    """Raised by strict parsers for an invalid learning artifact."""


class ConfidenceCalibrator(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def minimum_confidence(self) -> float: ...

    def calibrate(self, score: float) -> float: ...

    def to_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class PlattCalibrator:
    a: float
    b: float
    minimum_confidence: float
    method: str = field(default="platt", init=False)

    def __post_init__(self) -> None:
        a = _finite_number(self.a, "platt.a")
        b = _finite_number(self.b, "platt.b")
        threshold = _confidence(self.minimum_confidence, "minimum_confidence")
        if a < 0.0:
            raise SearchLearningConfigurationError(
                "platt.a cannot be negative because calibration input is "
                "higher-is-better confidence."
            )
        object.__setattr__(self, "a", a)
        object.__setattr__(self, "b", b)
        object.__setattr__(self, "minimum_confidence", threshold)

    def calibrate(self, score: float) -> float:
        value = _confidence(score, "calibration score")
        decision = self.a * value + self.b
        if decision >= 0.0:
            return 1.0 / (1.0 + math.exp(-decision))
        exponential = math.exp(decision)
        return exponential / (1.0 + exponential)

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "a": self.a,
            "b": self.b,
            "minimum_confidence": self.minimum_confidence,
        }


@dataclass(frozen=True)
class IsotonicCalibrator:
    points: tuple[tuple[float, float], ...]
    minimum_confidence: float
    method: str = field(default="isotonic", init=False)

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise SearchLearningConfigurationError(
                "isotonic.points must contain at least two points."
            )
        normalized: list[tuple[float, float]] = []
        previous_x: float | None = None
        previous_y: float | None = None
        for index, point in enumerate(self.points):
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise SearchLearningConfigurationError(
                    f"isotonic.points[{index}] must be an [x, y] pair."
                )
            x = _confidence(point[0], f"isotonic.points[{index}][0]")
            y = _confidence(point[1], f"isotonic.points[{index}][1]")
            if previous_x is not None and x <= previous_x:
                raise SearchLearningConfigurationError(
                    "isotonic point x values must be strictly increasing."
                )
            if previous_y is not None and y < previous_y:
                raise SearchLearningConfigurationError(
                    "isotonic point y values must be non-decreasing."
                )
            normalized.append((x, y))
            previous_x = x
            previous_y = y
        object.__setattr__(self, "points", tuple(normalized))
        object.__setattr__(
            self,
            "minimum_confidence",
            _confidence(self.minimum_confidence, "minimum_confidence"),
        )

    def calibrate(self, score: float) -> float:
        value = _confidence(score, "calibration score")
        if value <= self.points[0][0]:
            return self.points[0][1]
        if value >= self.points[-1][0]:
            return self.points[-1][1]
        for left, right in pairwise(self.points):
            if value > right[0]:
                continue
            span = right[0] - left[0]
            ratio = (value - left[0]) / span
            return left[1] + ratio * (right[1] - left[1])
        # The endpoints above and exhaustive intervals make this unreachable,
        # but an identity-safe return keeps inference resilient to refactors.
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "points": [list(point) for point in self.points],
            "minimum_confidence": self.minimum_confidence,
        }


@dataclass(frozen=True)
class CalibrationDecision:
    input_score: float
    confidence: float
    minimum_confidence: float
    hard_minimum_confidence: float
    effective_minimum_confidence: float
    passes_threshold: bool
    calibration_version: str
    method: str
    scope: str
    query_type: str
    collection_id: str | None
    fallback: bool
    fallback_reason: str | None = None

    def diagnostics(self) -> dict[str, object]:
        return {
            "calibration_version": self.calibration_version,
            "calibration_method": self.method,
            "calibration_scope": self.scope,
            "calibration_query_type": self.query_type,
            "calibration_collection_id": self.collection_id,
            "calibration_fallback": self.fallback,
            "calibration_fallback_reason": self.fallback_reason,
            "input_score": self.input_score,
            "confidence": self.confidence,
            "configured_minimum_confidence": self.minimum_confidence,
            "hard_minimum_confidence": self.hard_minimum_confidence,
            "effective_minimum_confidence": self.effective_minimum_confidence,
            "passes_threshold": self.passes_threshold,
        }


@dataclass(frozen=True)
class CalibrationRegistry:
    calibration_version: str = "identity"
    query_types: Mapping[str, ConfidenceCalibrator] = field(default_factory=dict)
    collections: Mapping[str, Mapping[str, ConfidenceCalibrator]] = field(
        default_factory=dict
    )
    global_calibrator: ConfidenceCalibrator | None = None
    fallback_reason: str | None = "calibration_not_configured"

    def __post_init__(self) -> None:
        version = str(self.calibration_version).strip()
        if not version or len(version) > 128:
            raise SearchLearningConfigurationError(
                "calibration_version must be a non-empty string of at most "
                "128 characters."
            )
        object.__setattr__(self, "calibration_version", version)
        query_types = _normalize_calibrator_modes(self.query_types, "query_types")
        collections: dict[str, Mapping[str, ConfidenceCalibrator]] = {}
        for raw_library_id, raw_modes in self.collections.items():
            library_id = str(raw_library_id).strip()
            if not library_id or len(library_id) > 256:
                raise SearchLearningConfigurationError(
                    "Collection calibration ids must be non-empty and at most "
                    "256 characters."
                )
            collections[library_id] = MappingProxyType(
                _normalize_calibrator_modes(
                    raw_modes,
                    f"collections.{library_id}",
                    allow_global=True,
                )
            )
        object.__setattr__(self, "query_types", MappingProxyType(query_types))
        object.__setattr__(self, "collections", MappingProxyType(collections))

    @property
    def configured(self) -> bool:
        return bool(self.query_types or self.collections or self.global_calibrator)

    def calibrate(
        self,
        score: float,
        *,
        query_type: str,
        collection_id: str | None = None,
    ) -> CalibrationDecision:
        safe_score = _confidence(score, "calibration score")
        mode = normalize_query_type(query_type)
        resolved_collection = str(collection_id).strip() if collection_id else None
        calibrator: ConfidenceCalibrator | None = None
        scope = "identity"
        if resolved_collection and resolved_collection in self.collections:
            collection_modes = self.collections[resolved_collection]
            calibrator = collection_modes.get(mode) or collection_modes.get("global")
            if calibrator is not None:
                scope = "collection"
        if calibrator is None:
            calibrator = self.query_types.get(mode)
            if calibrator is not None:
                scope = "query_type"
        if calibrator is None and self.global_calibrator is not None:
            calibrator = self.global_calibrator
            scope = "global"

        if calibrator is None:
            threshold = HARD_MINIMUM_CONFIDENCE
            return CalibrationDecision(
                input_score=safe_score,
                confidence=safe_score,
                minimum_confidence=0.0,
                hard_minimum_confidence=HARD_MINIMUM_CONFIDENCE,
                effective_minimum_confidence=threshold,
                passes_threshold=safe_score >= threshold,
                calibration_version=self.calibration_version,
                method="identity",
                scope=scope,
                query_type=mode,
                collection_id=resolved_collection,
                fallback=True,
                fallback_reason=self.fallback_reason or "calibration_unavailable",
            )
        try:
            confidence = _confidence(
                calibrator.calibrate(safe_score), "calibrated confidence"
            )
        except (ArithmeticError, SearchLearningConfigurationError, ValueError) as exc:
            threshold = HARD_MINIMUM_CONFIDENCE
            return CalibrationDecision(
                input_score=safe_score,
                confidence=safe_score,
                minimum_confidence=0.0,
                hard_minimum_confidence=HARD_MINIMUM_CONFIDENCE,
                effective_minimum_confidence=threshold,
                passes_threshold=safe_score >= threshold,
                calibration_version=self.calibration_version,
                method="identity",
                scope="identity",
                query_type=mode,
                collection_id=resolved_collection,
                fallback=True,
                fallback_reason=f"calibration_failed:{exc.__class__.__name__}",
            )
        threshold = max(HARD_MINIMUM_CONFIDENCE, calibrator.minimum_confidence)
        return CalibrationDecision(
            input_score=safe_score,
            confidence=confidence,
            minimum_confidence=calibrator.minimum_confidence,
            hard_minimum_confidence=HARD_MINIMUM_CONFIDENCE,
            effective_minimum_confidence=threshold,
            passes_threshold=confidence >= threshold,
            calibration_version=self.calibration_version,
            method=calibrator.method,
            scope=scope,
            query_type=mode,
            collection_id=resolved_collection,
            fallback=False,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_version": self.calibration_version,
            "query_types": {
                mode: calibrator.to_dict()
                for mode, calibrator in self.query_types.items()
            },
            "collections": {
                library_id: {
                    mode: calibrator.to_dict() for mode, calibrator in modes.items()
                }
                for library_id, modes in self.collections.items()
            },
        }
        if self.global_calibrator is not None:
            result["global"] = self.global_calibrator.to_dict()
        return result


@dataclass(frozen=True)
class SearchLearningBundle:
    configured: bool
    enabled: bool
    shadow_mode: bool
    ranker_model: RankingModel | None
    calibrations: CalibrationRegistry
    ranking_fallback_reason: str | None
    calibration_fallback_reason: str | None
    source_directory: str

    def build_ranker(self) -> LearningRanker:
        return LearningRanker(
            self.ranker_model if self.enabled else None,
            fallback_reason=(
                self.ranking_fallback_reason
                or ("learning_disabled" if not self.enabled else None)
            ),
        )

    def diagnostics(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "shadow_mode": self.shadow_mode,
            "ranking_model_version": (
                self.ranker_model.model_version if self.ranker_model else None
            ),
            "ranking_fallback": self.ranker_model is None or not self.enabled,
            "ranking_fallback_reason": self.ranking_fallback_reason,
            "calibration_version": self.calibrations.calibration_version,
            "calibration_configured": self.calibrations.configured,
            "calibration_fallback_reason": self.calibration_fallback_reason,
            "feature_schema_version": (
                self.ranker_model.feature_schema_version if self.ranker_model else None
            ),
        }


def load_search_learning(config_home: Path) -> SearchLearningBundle:
    """Load active learning artifacts without breaking the base search path.

    Strict parser functions below intentionally raise useful validation errors.
    This production loader converts every file, JSON, schema, and compatibility
    problem into explicit fallback diagnostics instead.
    """

    directory = Path(config_home) / SEARCH_LEARNING_DIRECTORY
    active_path = directory / ACTIVE_CONFIG_FILENAME
    if not active_path.is_file():
        return _fallback_bundle(directory, configured=False, reason="not_configured")
    try:
        active = _read_json_object(active_path)
        manifest = _parse_active_manifest(active)
    except (OSError, SearchLearningConfigurationError, json.JSONDecodeError) as exc:
        return _fallback_bundle(
            directory,
            configured=True,
            reason=f"active_config_invalid:{exc.__class__.__name__}",
        )
    if not manifest.enabled:
        return _fallback_bundle(directory, configured=True, reason="learning_disabled")

    ranker_model: RankingModel | None = None
    ranking_reason: str | None = None
    if manifest.ranker is None:
        ranking_reason = "ranker_not_configured"
    else:
        try:
            ranker_path = _resolve_artifact(directory, manifest.ranker)
            ranker_model = ranking_model_from_mapping(_read_json_object(ranker_path))
        except (
            OSError,
            json.JSONDecodeError,
            RankingModelError,
            SearchLearningConfigurationError,
        ) as exc:
            ranking_reason = f"ranker_invalid:{exc.__class__.__name__}"

    calibrations = CalibrationRegistry()
    calibration_reason: str | None = None
    if manifest.calibration is None:
        calibration_reason = "calibration_not_configured"
    else:
        try:
            calibration_path = _resolve_artifact(directory, manifest.calibration)
            calibrations = calibration_registry_from_mapping(
                _read_json_object(calibration_path)
            )
        except (
            OSError,
            json.JSONDecodeError,
            SearchLearningConfigurationError,
        ) as exc:
            calibration_reason = f"calibration_invalid:{exc.__class__.__name__}"
            calibrations = CalibrationRegistry(fallback_reason=calibration_reason)

    return SearchLearningBundle(
        configured=True,
        enabled=True,
        shadow_mode=manifest.shadow_mode,
        ranker_model=ranker_model,
        calibrations=calibrations,
        ranking_fallback_reason=ranking_reason,
        calibration_fallback_reason=calibration_reason,
        source_directory=str(directory),
    )


def calibration_registry_from_mapping(
    value: Mapping[str, object],
) -> CalibrationRegistry:
    if not isinstance(value, Mapping):
        raise SearchLearningConfigurationError(
            "Calibration artifact must be a JSON object."
        )
    top_level_modes = set(value) & set(SUPPORTED_QUERY_TYPES)
    allowed = {
        "schema_version",
        "calibration_version",
        "global",
        "query_types",
        "collections",
        *SUPPORTED_QUERY_TYPES,
    }
    unknown = set(value) - allowed
    if unknown:
        raise SearchLearningConfigurationError(
            "Calibration artifact has unknown fields: " + ", ".join(sorted(unknown))
        )
    for required in ("schema_version", "calibration_version"):
        if required not in value:
            raise SearchLearningConfigurationError(
                f"Calibration artifact is missing {required}."
            )
    schema_version = _strict_integer(value["schema_version"], "schema_version")
    if schema_version != CALIBRATION_SCHEMA_VERSION:
        raise SearchLearningConfigurationError(
            "Unsupported calibration schema version: "
            f"{schema_version}; expected {CALIBRATION_SCHEMA_VERSION}."
        )
    calibration_version = _strict_string(
        value["calibration_version"], "calibration_version"
    )
    raw_query_types = value.get("query_types", {})
    if not isinstance(raw_query_types, Mapping):
        raise SearchLearningConfigurationError("query_types must be an object.")
    normalized_query_types: dict[str, ConfidenceCalibrator] = {}
    for raw_mode, raw_calibrator in raw_query_types.items():
        mode = _normalize_mode_key(raw_mode, "query_types")
        normalized_query_types[mode] = calibrator_from_mapping(raw_calibrator)
    for raw_mode in top_level_modes:
        mode = normalize_query_type(str(raw_mode))
        if mode in normalized_query_types:
            raise SearchLearningConfigurationError(
                f"Calibration mode {mode!r} is defined twice."
            )
        normalized_query_types[mode] = calibrator_from_mapping(value[raw_mode])

    raw_collections = value.get("collections", {})
    if not isinstance(raw_collections, Mapping):
        raise SearchLearningConfigurationError("collections must be an object.")
    collections: dict[str, dict[str, ConfidenceCalibrator]] = {}
    for raw_library_id, raw_modes in raw_collections.items():
        library_id = _strict_string(raw_library_id, "collection id")
        if not isinstance(raw_modes, Mapping):
            raise SearchLearningConfigurationError(
                f"collections.{library_id} must be an object."
            )
        parsed_modes: dict[str, ConfidenceCalibrator] = {}
        for raw_mode, raw_calibrator in raw_modes.items():
            mode = (
                "global"
                if raw_mode == "global"
                else _normalize_mode_key(raw_mode, f"collections.{library_id}")
            )
            if mode in parsed_modes:
                raise SearchLearningConfigurationError(
                    f"collections.{library_id}.{mode} is defined twice."
                )
            parsed_modes[mode] = calibrator_from_mapping(raw_calibrator)
        if not parsed_modes:
            raise SearchLearningConfigurationError(
                f"collections.{library_id} must not be empty."
            )
        collections[library_id] = parsed_modes

    global_calibrator = (
        calibrator_from_mapping(value["global"]) if "global" in value else None
    )
    if not normalized_query_types and not collections and global_calibrator is None:
        raise SearchLearningConfigurationError(
            "Calibration artifact must define a query, Collection, or global model."
        )
    return CalibrationRegistry(
        calibration_version=calibration_version,
        query_types=normalized_query_types,
        collections=collections,
        global_calibrator=global_calibrator,
        fallback_reason=None,
    )


def calibrator_from_mapping(value: object) -> ConfidenceCalibrator:
    if not isinstance(value, Mapping):
        raise SearchLearningConfigurationError("Calibrator must be an object.")
    method = value.get("method")
    if method == "platt":
        _reject_unknown(value, {"method", "a", "b", "minimum_confidence"}, "platt")
        _require_fields(value, {"method", "a", "b", "minimum_confidence"}, "platt")
        return PlattCalibrator(
            a=_finite_number(value["a"], "platt.a"),
            b=_finite_number(value["b"], "platt.b"),
            minimum_confidence=_confidence(
                value["minimum_confidence"], "platt.minimum_confidence"
            ),
        )
    if method == "isotonic":
        _reject_unknown(
            value,
            {"method", "points", "minimum_confidence"},
            "isotonic",
        )
        _require_fields(
            value,
            {"method", "points", "minimum_confidence"},
            "isotonic",
        )
        raw_points = value["points"]
        if not isinstance(raw_points, list):
            raise SearchLearningConfigurationError("isotonic.points must be an array.")
        points: list[tuple[float, float]] = []
        for index, raw_point in enumerate(raw_points):
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                raise SearchLearningConfigurationError(
                    f"isotonic.points[{index}] must be an [x, y] array."
                )
            points.append(
                (
                    _confidence(raw_point[0], f"isotonic.points[{index}][0]"),
                    _confidence(raw_point[1], f"isotonic.points[{index}][1]"),
                )
            )
        return IsotonicCalibrator(
            points=tuple(points),
            minimum_confidence=_confidence(
                value["minimum_confidence"], "isotonic.minimum_confidence"
            ),
        )
    raise SearchLearningConfigurationError(
        "Calibrator method must be platt or isotonic."
    )


@dataclass(frozen=True)
class _ActiveManifest:
    enabled: bool
    shadow_mode: bool
    ranker: str | None
    calibration: str | None


def _parse_active_manifest(value: Mapping[str, object]) -> _ActiveManifest:
    allowed = {
        "schema_version",
        "enabled",
        "shadow_mode",
        "ranker",
        "calibration",
    }
    _reject_unknown(value, allowed, "active config")
    _require_fields(value, {"schema_version", "enabled"}, "active config")
    schema_version = _strict_integer(value["schema_version"], "schema_version")
    if schema_version != ACTIVE_CONFIG_SCHEMA_VERSION:
        raise SearchLearningConfigurationError(
            "Unsupported active config schema version: "
            f"{schema_version}; expected {ACTIVE_CONFIG_SCHEMA_VERSION}."
        )
    enabled = _strict_boolean(value["enabled"], "enabled")
    shadow_mode = _strict_boolean(value.get("shadow_mode", True), "shadow_mode")
    ranker = _optional_string(value.get("ranker"), "ranker")
    calibration = _optional_string(value.get("calibration"), "calibration")
    return _ActiveManifest(
        enabled=enabled,
        shadow_mode=shadow_mode,
        ranker=ranker,
        calibration=calibration,
    )


def _read_json_object(path: Path) -> Mapping[str, object]:
    size = path.stat().st_size
    if size > MAX_CONFIG_FILE_BYTES:
        raise SearchLearningConfigurationError(
            f"Learning artifact exceeds {MAX_CONFIG_FILE_BYTES} bytes."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SearchLearningConfigurationError(
            f"Learning artifact must contain a JSON object: {path.name}"
        )
    return payload


def _resolve_artifact(directory: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute():
        raise SearchLearningConfigurationError(
            "Learning artifact references must be relative paths."
        )
    directory_resolved = directory.resolve()
    resolved = (directory / relative).resolve()
    try:
        resolved.relative_to(directory_resolved)
    except ValueError as exc:
        raise SearchLearningConfigurationError(
            "Learning artifact reference escapes the search-learning directory."
        ) from exc
    if resolved.suffix.lower() != ".json":
        raise SearchLearningConfigurationError(
            "Learning artifact references must point to JSON files."
        )
    return resolved


def _fallback_bundle(
    directory: Path,
    *,
    configured: bool,
    reason: str,
) -> SearchLearningBundle:
    return SearchLearningBundle(
        configured=configured,
        enabled=False,
        shadow_mode=True,
        ranker_model=None,
        calibrations=CalibrationRegistry(fallback_reason=reason),
        ranking_fallback_reason=reason,
        calibration_fallback_reason=reason,
        source_directory=str(directory),
    )


def _normalize_calibrator_modes(
    value: Mapping[str, ConfidenceCalibrator],
    context: str,
    *,
    allow_global: bool = False,
) -> dict[str, ConfidenceCalibrator]:
    if not isinstance(value, Mapping):
        raise SearchLearningConfigurationError(f"{context} must be an object.")
    result: dict[str, ConfidenceCalibrator] = {}
    for raw_mode, calibrator in value.items():
        mode = (
            "global"
            if allow_global and raw_mode == "global"
            else _normalize_mode_key(raw_mode, context)
        )
        if mode in result:
            raise SearchLearningConfigurationError(
                f"{context}.{mode} is defined more than once."
            )
        if not isinstance(calibrator, (PlattCalibrator, IsotonicCalibrator)):
            raise SearchLearningConfigurationError(
                f"{context}.{mode} is not a supported calibrator."
            )
        result[mode] = calibrator
    return result


def _normalize_mode_key(value: object, context: str) -> str:
    try:
        return normalize_query_type(_strict_string(value, f"{context} mode"))
    except ValueError as exc:
        raise SearchLearningConfigurationError(str(exc)) from exc


def _reject_unknown(
    value: Mapping[str, object], allowed: set[str], context: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise SearchLearningConfigurationError(
            f"{context} has unknown fields: " + ", ".join(sorted(unknown))
        )


def _require_fields(
    value: Mapping[str, object], required: set[str], context: str
) -> None:
    missing = required - set(value)
    if missing:
        raise SearchLearningConfigurationError(
            f"{context} is missing fields: " + ", ".join(sorted(missing))
        )


def _confidence(value: object, name: str) -> float:
    normalized = _finite_number(value, name)
    if not 0.0 <= normalized <= 1.0:
        raise SearchLearningConfigurationError(f"{name} must be between zero and one.")
    return normalized


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchLearningConfigurationError(f"{name} must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise SearchLearningConfigurationError(f"{name} must be finite.")
    return normalized


def _strict_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SearchLearningConfigurationError(f"{name} must be an integer.")
    return value


def _strict_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchLearningConfigurationError(f"{name} must be a non-empty string.")
    return value.strip()


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _strict_string(value, name)


def _strict_boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise SearchLearningConfigurationError(f"{name} must be a boolean.")
    return value
