from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from .config import ConfigurationError
from .models import (
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_POSSIBLE_THRESHOLD,
    SearchHit,
    classify_match_state,
    normalize_cosine_distance,
    normalize_rrf_score,
)
from .rank_fusion import DEFAULT_MAX_CONFIDENCE_DROP, DEFAULT_SCORE_GAP

QUALITY_CONFIG_FILENAME = "search-quality.json"
QUALITY_CONFIG_SCHEMA_VERSION = 2
SUPPORTED_QUALITY_CONFIG_SCHEMA_VERSIONS = frozenset({1, 2})
QualityMode = Literal["text", "image", "combined"]
FUSION_V1 = "confidence_v1"
FUSION_V2 = "confidence_v2"
FUSION_V2_DEFAULTS = {
    "agreement_reward": 0.08,
    "rank_decay": 10.0,
    "weak_channel_floor": 0.45,
    "weak_channel_penalty": 0.08,
}
FUSION_V2_RANGES: dict[str, tuple[float, float, bool]] = {
    "agreement_reward": (0.0, 0.25, True),
    "rank_decay": (0.0, 100.0, False),
    "weak_channel_floor": (0.0, 1.0, True),
    "weak_channel_penalty": (0.0, 0.5, True),
}
COLLECTION_CALIBRATION_NONE = "none"
COLLECTION_CALIBRATION_NULL_MEAN = "null_mean_offset_v1"
COLLECTION_CALIBRATION_FALLBACK = "identity"


@dataclass(frozen=True)
class CollectionCalibrationLibrary:
    baseline_confidence: float
    offset: float
    sample_count: int


@dataclass(frozen=True)
class CollectionCalibrationMode:
    reference_confidence: float
    libraries: dict[str, CollectionCalibrationLibrary] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionCalibration:
    mode: str = COLLECTION_CALIBRATION_NONE
    fallback: str = COLLECTION_CALIBRATION_FALLBACK
    strength: float = 0.0
    top_k: int = 10
    modes: dict[QualityMode, CollectionCalibrationMode] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.mode == COLLECTION_CALIBRATION_NULL_MEAN

    def offsets_for(self, mode: QualityMode) -> dict[str, float]:
        value = self.modes.get(mode)
        if value is None:
            return {}
        return {
            library_id: library.offset
            for library_id, library in value.libraries.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fallback": self.fallback,
            "strength": self.strength,
            "top_k": self.top_k,
            "modes": {
                mode: {
                    "reference_confidence": value.reference_confidence,
                    "libraries": {
                        library_id: asdict(library)
                        for library_id, library in value.libraries.items()
                    },
                }
                for mode, value in self.modes.items()
            },
        }


@dataclass(frozen=True)
class SearchQualityThresholds:
    high: float = DEFAULT_HIGH_THRESHOLD
    possible: float = DEFAULT_POSSIBLE_THRESHOLD
    minimum_score: float | None = None
    minimum_confidence: float = 0.0
    score_gap: float = DEFAULT_SCORE_GAP
    max_confidence_drop: float = DEFAULT_MAX_CONFIDENCE_DROP

    @property
    def minimum(self) -> float:
        """Compatibility alias for the normalized filtering threshold."""
        return self.minimum_confidence

    def validate(self, name: str) -> None:
        values = {
            "high": self.high,
            "possible": self.possible,
            "minimum_confidence": self.minimum_confidence,
            "score_gap": self.score_gap,
            "max_confidence_drop": self.max_confidence_drop,
        }
        for field_name, value in values.items():
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ConfigurationError(
                    f"search-quality.json {name}.{field_name} must be between 0 and 1."
                )
        if self.score_gap <= 0.0:
            raise ConfigurationError(
                f"search-quality.json {name}.score_gap must be greater than zero."
            )
        if self.possible > self.high:
            raise ConfigurationError(
                "search-quality.json thresholds must satisfy "
                f"{name}.possible <= {name}.high."
            )
        if self.minimum_score is None:
            return
        if not math.isfinite(self.minimum_score) or self.minimum_score < 0:
            raise ConfigurationError(
                f"search-quality.json {name}.minimum_score must be non-negative."
            )
        if name in {"text", "image"} and self.minimum_score > 2:
            raise ConfigurationError(
                f"search-quality.json {name}.minimum_score must be at most 2."
            )


@dataclass(frozen=True)
class SearchQualityConfig:
    text: SearchQualityThresholds = SearchQualityThresholds()
    image: SearchQualityThresholds = SearchQualityThresholds()
    combined: SearchQualityThresholds = SearchQualityThresholds()
    fusion_mode: str = FUSION_V1
    fusion_options: dict[str, float] = field(default_factory=dict)
    collection_calibration: CollectionCalibration = CollectionCalibration()
    configured: bool = False
    source_path: str = ""

    def thresholds_for(self, mode: QualityMode) -> SearchQualityThresholds:
        return getattr(self, mode)

    def to_dict(self) -> dict[str, Any]:
        fusion: dict[str, Any] = {"mode": self.fusion_mode}
        fusion.update(self.fusion_options)
        result = {
            "schema_version": QUALITY_CONFIG_SCHEMA_VERSION,
            "configured": self.configured,
            "source_path": self.source_path,
            "text": asdict(self.text),
            "image": asdict(self.image),
            "combined": asdict(self.combined),
            "fusion": fusion,
        }
        if self.collection_calibration.configured:
            result["collection_calibration"] = self.collection_calibration.to_dict()
        return result


def load_search_quality(workspace: Path) -> SearchQualityConfig:
    path = workspace / QUALITY_CONFIG_FILENAME
    if not path.exists():
        return SearchQualityConfig(source_path=str(path))
    if not path.is_file():
        raise ConfigurationError(f"Search quality config is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"Invalid search quality config {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("search-quality.json must contain a JSON object.")
    _reject_unknown(
        payload,
        {
            "schema_version",
            "text",
            "image",
            "combined",
            "diagnostics",
            "thresholds",
            "kind",
            "constraints",
            "coverage",
            "dataset",
            "generated_at",
            "run",
            "fusion",
            "collection_calibration",
            "holdout_split",
        },
        "root",
    )
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_QUALITY_CONFIG_SCHEMA_VERSIONS
    ):
        raise ConfigurationError(
            "search-quality.json schema_version must be one of "
            f"{sorted(SUPPORTED_QUALITY_CONFIG_SCHEMA_VERSIONS)}."
        )
    missing = [name for name in ("text", "image", "combined") if name not in payload]
    if missing:
        raise ConfigurationError(
            "search-quality.json is missing threshold sections: " + ", ".join(missing)
        )
    fusion_mode, fusion_options = _parse_fusion(
        payload.get("fusion"), schema_version=int(schema_version)
    )
    collection_calibration = _parse_collection_calibration(
        payload.get("collection_calibration"),
        schema_version=int(schema_version),
    )
    config = SearchQualityConfig(
        text=_parse_thresholds(
            "text", payload["text"], schema_version=int(schema_version)
        ),
        image=_parse_thresholds(
            "image", payload["image"], schema_version=int(schema_version)
        ),
        combined=_parse_thresholds(
            "combined", payload["combined"], schema_version=int(schema_version)
        ),
        fusion_mode=fusion_mode,
        fusion_options=fusion_options,
        collection_calibration=collection_calibration,
        configured=True,
        source_path=str(path),
    )
    modes: tuple[QualityMode, ...] = ("text", "image", "combined")
    for name in modes:
        config.thresholds_for(name).validate(name)
    return config


def annotate_search_hits(
    hits: list[SearchHit],
    thresholds: SearchQualityThresholds,
) -> list[SearchHit]:
    results = []
    for hit in hits:
        normalized_score = float(hit.normalized_score or 0.0)
        results.append(
            replace(
                hit,
                confidence=normalized_score,
                match_state=classify_match_state(
                    normalized_score,
                    high=thresholds.high,
                    possible=thresholds.possible,
                ),
            )
        )
    return results


def filter_search_hits(
    hits: list[SearchHit], thresholds: SearchQualityThresholds, mode: QualityMode
) -> list[SearchHit]:
    results = []
    for hit in hits:
        if float(hit.confidence or 0.0) < thresholds.minimum_confidence:
            continue
        if thresholds.minimum_score is not None:
            raw_score = float(hit.raw_score or 0.0)
            if mode == "combined":
                if raw_score < thresholds.minimum_score:
                    continue
            elif raw_score > thresholds.minimum_score:
                continue
        results.append(hit)
    return results


def _parse_thresholds(
    name: str, value: Any, *, schema_version: int
) -> SearchQualityThresholds:
    if not isinstance(value, dict):
        raise ConfigurationError(f"search-quality.json {name} must be an object.")
    _reject_unknown(
        value,
        {
            "high",
            "possible",
            "minimum",
            "minimum_score",
            "minimum_confidence",
            "score_gap",
            *({"max_confidence_drop"} if schema_version >= 2 else set()),
        },
        name,
    )
    mode = cast(QualityMode, name)
    minimum_score = (
        _number(name, "minimum_score", value["minimum_score"])
        if "minimum_score" in value
        else None
    )
    if "minimum_confidence" in value:
        minimum_confidence = _number(
            name, "minimum_confidence", value["minimum_confidence"]
        )
    elif "minimum" in value:
        minimum_confidence = _number(name, "minimum", value["minimum"])
    elif minimum_score is not None:
        minimum_confidence = (
            normalize_rrf_score(minimum_score)
            if mode == "combined"
            else normalize_cosine_distance(minimum_score)
        )
    else:
        minimum_confidence = 0.0
    return SearchQualityThresholds(
        high=_number(name, "high", value.get("high", DEFAULT_HIGH_THRESHOLD)),
        possible=_number(
            name,
            "possible",
            value.get("possible", DEFAULT_POSSIBLE_THRESHOLD),
        ),
        minimum_score=minimum_score,
        minimum_confidence=minimum_confidence,
        score_gap=_number(
            name,
            "score_gap",
            value.get("score_gap", DEFAULT_SCORE_GAP),
        ),
        max_confidence_drop=_number(
            name,
            "max_confidence_drop",
            value.get("max_confidence_drop", DEFAULT_MAX_CONFIDENCE_DROP),
        ),
    )


def _parse_fusion(
    value: Any,
    *,
    schema_version: int,
) -> tuple[str, dict[str, float]]:
    if value is None:
        return FUSION_V1, {}
    if not isinstance(value, dict):
        raise ConfigurationError("search-quality.json fusion must be an object.")
    _reject_unknown(value, {"mode", *FUSION_V2_DEFAULTS}, "fusion")
    mode = value.get("mode", FUSION_V1)
    if mode not in {FUSION_V1, FUSION_V2}:
        raise ConfigurationError(
            "search-quality.json fusion.mode must be confidence_v1 or confidence_v2."
        )
    if schema_version < 2:
        return FUSION_V1, {}
    options = dict(FUSION_V2_DEFAULTS)
    for name, (minimum, maximum, inclusive_minimum) in FUSION_V2_RANGES.items():
        if name in value:
            options[name] = _number("fusion", name, value[name])
        option = options[name]
        minimum_valid = option >= minimum if inclusive_minimum else option > minimum
        if not minimum_valid or option > maximum:
            lower = "[" if inclusive_minimum else "("
            raise ConfigurationError(
                f"search-quality.json fusion.{name} must be in "
                f"{lower}{minimum}, {maximum}]."
            )
    return str(mode), options if mode == FUSION_V2 else {}


def _parse_collection_calibration(
    value: Any,
    *,
    schema_version: int,
) -> CollectionCalibration:
    if value is None:
        return CollectionCalibration()
    if schema_version < 2:
        raise ConfigurationError(
            "search-quality.json collection_calibration requires schema_version 2."
        )
    if not isinstance(value, dict):
        raise ConfigurationError(
            "search-quality.json collection_calibration must be an object."
        )
    _reject_unknown(
        value,
        {"mode", "fallback", "strength", "top_k", "modes"},
        "collection_calibration",
    )
    mode = value.get("mode")
    if mode != COLLECTION_CALIBRATION_NULL_MEAN:
        raise ConfigurationError(
            "search-quality.json collection_calibration.mode must be "
            f"{COLLECTION_CALIBRATION_NULL_MEAN}."
        )
    fallback = value.get("fallback", COLLECTION_CALIBRATION_FALLBACK)
    if fallback != COLLECTION_CALIBRATION_FALLBACK:
        raise ConfigurationError(
            "search-quality.json collection_calibration.fallback must be identity."
        )
    strength = _number(
        "collection_calibration",
        "strength",
        value.get("strength", 1.0),
    )
    if not math.isfinite(strength) or not 0.0 <= strength <= 10.0:
        raise ConfigurationError(
            "search-quality.json collection_calibration.strength must be between "
            "0 and 10."
        )
    top_k = value.get("top_k", 10)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
        raise ConfigurationError(
            "search-quality.json collection_calibration.top_k must be an integer "
            "between 1 and 50."
        )
    raw_modes = value.get("modes")
    if not isinstance(raw_modes, dict) or not raw_modes:
        raise ConfigurationError(
            "search-quality.json collection_calibration.modes must be a non-empty "
            "object."
        )
    unknown_modes = sorted(set(raw_modes) - {"text", "image", "combined"})
    if unknown_modes:
        raise ConfigurationError(
            "search-quality.json collection_calibration.modes has unknown fields: "
            + ", ".join(unknown_modes)
        )
    modes: dict[QualityMode, CollectionCalibrationMode] = {}
    for raw_mode, raw_mode_value in raw_modes.items():
        mode_name = cast(QualityMode, raw_mode)
        context = f"collection_calibration.modes.{mode_name}"
        if not isinstance(raw_mode_value, dict):
            raise ConfigurationError(
                f"search-quality.json {context} must be an object."
            )
        _reject_unknown(
            raw_mode_value,
            {"reference_confidence", "libraries"},
            context,
        )
        reference = _number(
            context,
            "reference_confidence",
            raw_mode_value.get("reference_confidence"),
        )
        if not math.isfinite(reference) or not 0.0 <= reference <= 1.0:
            raise ConfigurationError(
                f"search-quality.json {context}.reference_confidence must be "
                "between 0 and 1."
            )
        raw_libraries = raw_mode_value.get("libraries")
        if not isinstance(raw_libraries, dict) or not raw_libraries:
            raise ConfigurationError(
                f"search-quality.json {context}.libraries must be a non-empty object."
            )
        libraries: dict[str, CollectionCalibrationLibrary] = {}
        for raw_library_id, raw_library in raw_libraries.items():
            if not isinstance(raw_library_id, str) or not raw_library_id.strip():
                raise ConfigurationError(
                    f"search-quality.json {context}.libraries needs non-empty ids."
                )
            library_id = raw_library_id.strip()
            library_context = f"{context}.libraries.{library_id}"
            if not isinstance(raw_library, dict):
                raise ConfigurationError(
                    f"search-quality.json {library_context} must be an object."
                )
            _reject_unknown(
                raw_library,
                {"baseline_confidence", "offset", "sample_count"},
                library_context,
            )
            baseline = _number(
                library_context,
                "baseline_confidence",
                raw_library.get("baseline_confidence"),
            )
            offset = _number(
                library_context,
                "offset",
                raw_library.get("offset"),
            )
            sample_count = raw_library.get("sample_count")
            if not math.isfinite(baseline) or not 0.0 <= baseline <= 1.0:
                raise ConfigurationError(
                    f"search-quality.json {library_context}.baseline_confidence "
                    "must be between 0 and 1."
                )
            if not math.isfinite(offset) or not -1.0 <= offset <= 1.0:
                raise ConfigurationError(
                    f"search-quality.json {library_context}.offset must be between "
                    "-1 and 1."
                )
            if (
                isinstance(sample_count, bool)
                or not isinstance(sample_count, int)
                or sample_count < 1
            ):
                raise ConfigurationError(
                    f"search-quality.json {library_context}.sample_count must be a "
                    "positive integer."
                )
            libraries[library_id] = CollectionCalibrationLibrary(
                baseline_confidence=baseline,
                offset=offset,
                sample_count=sample_count,
            )
        modes[mode_name] = CollectionCalibrationMode(
            reference_confidence=reference,
            libraries=libraries,
        )
    return CollectionCalibration(
        mode=COLLECTION_CALIBRATION_NULL_MEAN,
        fallback=COLLECTION_CALIBRATION_FALLBACK,
        strength=strength,
        top_k=top_k,
        modes=modes,
    )


def _number(section: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"search-quality.json {section}.{name} must be a number."
        )
    return float(value)


def _reject_unknown(payload: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigurationError(
            f"search-quality.json {name} has unknown fields: " + ", ".join(unknown)
        )
