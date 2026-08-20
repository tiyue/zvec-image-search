from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from types import MappingProxyType
from typing import Any, Final

from .models import SearchHit

FEATURE_SCHEMA_VERSION: Final = 1

# This tuple is the on-disk and inference contract.  New numeric features must
# ship with a new schema version instead of silently changing the meaning or
# order of an existing training row.
NUMERIC_FEATURE_NAMES: Final = (
    "vector_raw_score",
    "vector_confidence",
    "tag_match_score",
    "manual_tag_matches",
    "folder_tag_matches",
    "alias_tag_matches",
    "model_high_confidence_tag_matches",
    "model_tag_matches",
    "vector_tag_matches",
    "identity_match",
    "work_match",
    "action_match",
    "expression_match",
    "scene_match",
    "image_text_agreement",
    "collection_rank",
    "collection_size",
    "duplicate_group_size",
)

SUPPORTED_QUERY_TYPES: Final = frozenset(
    {
        "text",
        "image",
        "combined",
        "tag",
        "identity",
        "identity_combined",
        "action",
        "expression",
        "scene",
        "action_expression",
    }
)

_QUERY_TYPE_ALIASES: Final = {
    "image_text": "combined",
    "image-text": "combined",
    "mix": "combined",
    "mixed": "combined",
    "tags": "tag",
    "person": "identity",
    "character": "identity",
    "cosplayer": "identity",
    "pose": "action",
    "emotion": "expression",
}

# Initial evidence strengths from the approved design.  They are used only to
# create a stable tag-match feature; the learning model remains free to learn
# separate weights for each source count.
DEFAULT_TAG_SOURCE_WEIGHTS: Final[Mapping[str, float]] = MappingProxyType(
    {
        "manual": 1.00,
        "folder": 0.90,
        "alias": 0.90,
        "model_high_confidence": 0.70,
        "model": 0.45,
        "vector": 0.25,
    }
)


class SearchFeatureError(ValueError):
    """Raised when a feature row does not satisfy the versioned contract."""


@dataclass(frozen=True)
class TagMatchEvidence:
    """Matched query tags grouped by their strongest known evidence source.

    A tag may appear in more than one source.  ``weighted_score`` counts that
    tag once, using the strongest source, so copied/model tags cannot inflate a
    result merely by repeating the same value in multiple metadata fields.
    """

    manual: frozenset[str] = frozenset()
    folder: frozenset[str] = frozenset()
    alias: frozenset[str] = frozenset()
    model_high_confidence: frozenset[str] = frozenset()
    model: frozenset[str] = frozenset()
    vector: frozenset[str] = frozenset()
    query_tag_count: int = 0

    def __post_init__(self) -> None:
        for name in DEFAULT_TAG_SOURCE_WEIGHTS:
            value = getattr(self, name)
            normalized = frozenset(tag for raw in value if (tag := str(raw).strip()))
            object.__setattr__(self, name, normalized)
        if (
            isinstance(self.query_tag_count, bool)
            or not isinstance(self.query_tag_count, int)
            or self.query_tag_count < 0
        ):
            raise SearchFeatureError("query_tag_count must be a non-negative integer.")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        query_tag_count: int = 0,
    ) -> TagMatchEvidence:
        unknown = set(value) - set(DEFAULT_TAG_SOURCE_WEIGHTS)
        if unknown:
            raise SearchFeatureError(
                "Unknown tag evidence sources: " + ", ".join(sorted(unknown))
            )
        normalized: dict[str, frozenset[str]] = {}
        for name in DEFAULT_TAG_SOURCE_WEIGHTS:
            raw_values = value.get(name, ())
            if isinstance(raw_values, str):
                raw_values = (raw_values,)
            if not isinstance(raw_values, Iterable):
                raise SearchFeatureError(
                    f"Tag evidence source {name!r} must be an iterable of strings."
                )
            normalized[name] = frozenset(str(item) for item in raw_values)
        return cls(query_tag_count=query_tag_count, **normalized)

    def source_count(self, source: str) -> int:
        if source not in DEFAULT_TAG_SOURCE_WEIGHTS:
            raise SearchFeatureError(f"Unknown tag evidence source: {source}")
        return len(getattr(self, source))

    @property
    def unique_matches(self) -> frozenset[str]:
        return frozenset(
            tag
            for source in DEFAULT_TAG_SOURCE_WEIGHTS
            for tag in getattr(self, source)
        )

    def weighted_score(
        self,
        weights: Mapping[str, float] = DEFAULT_TAG_SOURCE_WEIGHTS,
    ) -> float:
        _validate_source_weights(weights)
        strongest: dict[str, float] = {}
        for source in DEFAULT_TAG_SOURCE_WEIGHTS:
            source_weight = float(weights[source])
            for tag in getattr(self, source):
                strongest[tag] = max(strongest.get(tag, 0.0), source_weight)
        denominator = max(1, self.query_tag_count, len(strongest))
        return _unit_interval(sum(strongest.values()) / denominator)


@dataclass(frozen=True)
class SearchFeatures:
    """One stable, JSON-serializable learning-to-rank feature row."""

    feature_schema_version: int
    vector_raw_score: float
    vector_confidence: float
    tag_match_score: float
    manual_tag_matches: float
    folder_tag_matches: float
    alias_tag_matches: float
    model_high_confidence_tag_matches: float
    model_tag_matches: float
    vector_tag_matches: float
    identity_match: float
    work_match: float
    action_match: float
    expression_match: float
    scene_match: float
    image_text_agreement: float
    collection_rank: float
    collection_size: float
    duplicate_group_size: float
    query_type: str

    def __post_init__(self) -> None:
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise SearchFeatureError(
                "Unsupported feature schema version: "
                f"{self.feature_schema_version}; expected {FEATURE_SCHEMA_VERSION}."
            )
        object.__setattr__(self, "query_type", normalize_query_type(self.query_type))
        for name in NUMERIC_FEATURE_NAMES:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SearchFeatureError(f"Feature {name} must be numeric.")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise SearchFeatureError(f"Feature {name} must be finite.")
            object.__setattr__(self, name, normalized)
        for name in (
            "vector_raw_score",
            "manual_tag_matches",
            "folder_tag_matches",
            "alias_tag_matches",
            "model_high_confidence_tag_matches",
            "model_tag_matches",
            "vector_tag_matches",
            "collection_rank",
            "collection_size",
        ):
            if getattr(self, name) < 0.0:
                raise SearchFeatureError(f"Feature {name} cannot be negative.")
        if self.duplicate_group_size < 1.0:
            raise SearchFeatureError(
                "Feature duplicate_group_size must be at least one."
            )
        for name in (
            "vector_confidence",
            "tag_match_score",
            "identity_match",
            "work_match",
            "action_match",
            "expression_match",
            "scene_match",
            "image_text_agreement",
        ):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise SearchFeatureError(
                    f"Feature {name} must be between zero and one."
                )

    def numeric_values(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in NUMERIC_FEATURE_NAMES}

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SearchFeatures:
        allowed = {item.name for item in fields(cls)}
        unknown = set(value) - allowed
        missing = allowed - set(value)
        if unknown:
            raise SearchFeatureError(
                "Feature row has unknown fields: " + ", ".join(sorted(unknown))
            )
        if missing:
            raise SearchFeatureError(
                "Feature row is missing fields: " + ", ".join(sorted(missing))
            )
        return cls(**dict(value))  # type: ignore[arg-type]


def extract_search_features(
    hit: SearchHit,
    *,
    query_type: str,
    tag_evidence: TagMatchEvidence | None = None,
    collection_rank: int | None = None,
    collection_size: int = 0,
    duplicate_group_size: int = 1,
    identity_match: float | bool = 0.0,
    work_match: float | bool = 0.0,
    action_match: float | bool = 0.0,
    expression_match: float | bool = 0.0,
    scene_match: float | bool = 0.0,
    vector_raw_score: float | None = None,
    vector_confidence: float | None = None,
    image_text_agreement: float | None = None,
) -> SearchFeatures:
    """Extract one row without accessing the filesystem, SQLite, or an API.

    ``SearchHit.raw_score`` changes meaning after rank fusion.  The vector raw
    feature therefore deliberately uses ``distance``, whose Zvec cosine
    contract is stable (lower is better), while ``confidence`` remains the
    comparable higher-is-better feature.
    """

    if not isinstance(hit, SearchHit):
        raise SearchFeatureError("hit must be a SearchHit.")
    evidence = tag_evidence or TagMatchEvidence(
        vector=frozenset(hit.matched_tags),
        query_tag_count=len(hit.matched_tags),
    )
    resolved_rank = hit.rank if collection_rank is None else collection_rank
    if isinstance(resolved_rank, bool) or not isinstance(resolved_rank, int):
        raise SearchFeatureError("collection_rank must be an integer.")
    if (
        isinstance(collection_size, bool)
        or not isinstance(collection_size, int)
        or collection_size < 0
    ):
        raise SearchFeatureError("collection_size must be a non-negative integer.")
    if (
        isinstance(duplicate_group_size, bool)
        or not isinstance(duplicate_group_size, int)
        or duplicate_group_size < 1
    ):
        raise SearchFeatureError("duplicate_group_size must be a positive integer.")

    raw_vector_value = hit.distance if vector_raw_score is None else vector_raw_score
    confidence_value = (
        hit.confidence if vector_confidence is None else vector_confidence
    )
    agreement_value = (
        hit.rank_agreement or 0.0
        if image_text_agreement is None
        else image_text_agreement
    )
    return SearchFeatures(
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        vector_raw_score=max(
            0.0,
            _finite_float(raw_vector_value, "vector_raw_score"),
        ),
        vector_confidence=_unit_interval(
            _finite_float(confidence_value, "vector_confidence")
        ),
        tag_match_score=evidence.weighted_score(),
        manual_tag_matches=float(evidence.source_count("manual")),
        folder_tag_matches=float(evidence.source_count("folder")),
        alias_tag_matches=float(evidence.source_count("alias")),
        model_high_confidence_tag_matches=float(
            evidence.source_count("model_high_confidence")
        ),
        model_tag_matches=float(evidence.source_count("model")),
        vector_tag_matches=float(evidence.source_count("vector")),
        identity_match=_match_strength(identity_match, "identity_match"),
        work_match=_match_strength(work_match, "work_match"),
        action_match=_match_strength(action_match, "action_match"),
        expression_match=_match_strength(expression_match, "expression_match"),
        scene_match=_match_strength(scene_match, "scene_match"),
        image_text_agreement=_unit_interval(
            _finite_float(agreement_value, "image_text_agreement")
        ),
        collection_rank=float(max(0, resolved_rank)),
        collection_size=float(collection_size),
        duplicate_group_size=float(duplicate_group_size),
        query_type=query_type,
    )


def normalize_query_type(value: str) -> str:
    if not isinstance(value, str):
        raise SearchFeatureError("query_type must be a string.")
    normalized = value.strip().lower().replace(" ", "_")
    normalized = _QUERY_TYPE_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_QUERY_TYPES:
        raise SearchFeatureError(
            f"Unsupported query_type {value!r}; expected one of "
            + ", ".join(sorted(SUPPORTED_QUERY_TYPES))
            + "."
        )
    return normalized


def _validate_source_weights(weights: Mapping[str, float]) -> None:
    if set(weights) != set(DEFAULT_TAG_SOURCE_WEIGHTS):
        raise SearchFeatureError(
            "Tag source weights must define exactly: "
            + ", ".join(DEFAULT_TAG_SOURCE_WEIGHTS)
            + "."
        )
    for name, value in weights.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise SearchFeatureError(
                f"Tag source weight {name!r} must be finite and between zero and one."
            )


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchFeatureError(f"{name} must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise SearchFeatureError(f"{name} must be finite.")
    return normalized


def _match_strength(value: float | bool, name: str) -> float:
    if isinstance(value, bool):
        return float(value)
    return _unit_interval(_finite_float(value, name))


def _unit_interval(value: float) -> float:
    return min(1.0, max(0.0, value))
