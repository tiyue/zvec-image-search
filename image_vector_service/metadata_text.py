"""Build deterministic, review-safe text for metadata embeddings.

The visual annotation payload contains more information than may be safe to
index.  This module deliberately keeps the policy narrow: user tags, folder
tags and accepted model tags are trusted; structured model output is included
only when the corresponding value has been accepted. Pending proposals and
rejected tags never enter the generated text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .auto_tagging_assets import FIELD_TAG_LABELS

DEFAULT_METADATA_TEXT_MAX_CHARS = 2_000
METADATA_TEXT_SCHEMA_VERSION = 1

_ENTITY_SECTIONS = (
    ("character", "角色"),
    ("work", "作品"),
)
_FIELD_SECTIONS = (
    ("pose", "姿势"),
    ("action", "动作"),
    ("expression", "神态"),
    ("clothing", "服装"),
    ("prop", "道具"),
    ("scene", "场景"),
)
_TAG_SOURCES = (
    ("manual_tags", "tags"),
    ("folder_tags", "folder_tags"),
    ("accepted_auto_tags", "accepted_auto_tags"),
)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_SEPARATOR_RE = re.compile(r"[\s、，,；;：:]+$")


@dataclass(frozen=True)
class MetadataTextResult:
    """One canonical metadata text value and the digest of its UTF-8 bytes."""

    text: str
    sha256: str
    truncated: bool
    source_counts: Mapping[str, int]

    @property
    def empty(self) -> bool:
        return not self.text


def build_metadata_text(
    entry: Mapping[str, Any] | None,
    annotation: Mapping[str, Any] | None,
    *,
    max_chars: int = DEFAULT_METADATA_TEXT_MAX_CHARS,
) -> MetadataTextResult:
    """Combine accepted image metadata into stable embedding input text.

    ``entry`` is an :class:`~image_vector_service.state.IndexState` entry and
    ``annotation`` is its document annotation.  The function is intentionally
    independent of SQLite and Zvec so schema migration and background backfill
    code can use the exact same canonicalization.

    A structured field or entity is included only when its display value
    already exists among the accepted/manual/folder tags.  This remains true
    after review completion because one review action may accept only a subset
    of the model proposal.
    """

    normalized_entry = _optional_mapping(entry, "entry")
    normalized_annotation = _optional_mapping(annotation, "annotation")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer.")

    accepted_tags, tag_source_counts = _accepted_tags(
        normalized_entry,
        normalized_annotation,
    )
    accepted_keys = {_comparison_key(value) for value in accepted_tags}
    structured = _structured_annotation(normalized_annotation)
    annotation_accepted = (
        _normalize_text(normalized_annotation.get("status")) == "accepted"
    )

    sections: list[tuple[str, tuple[str, ...]]] = []
    consumed_keys: set[str] = set()
    entity_count = 0
    field_count = 0

    entities = _mapping_value(
        structured.get("entities"),
        normalized_annotation.get("entities"),
    )
    for entity_type, title in _ENTITY_SECTIONS:
        values = _accepted_entity_names(
            entities.get(entity_type),
            accepted_keys=accepted_keys,
        )
        values = _without_consumed(values, consumed_keys)
        if values:
            sections.append((title, values))
            entity_count += len(values)

    fields = _mapping_value(
        structured.get("fields"),
        normalized_annotation.get("fields"),
    )
    for field_name, title in _FIELD_SECTIONS:
        values = _accepted_field_labels(
            fields.get(field_name),
            accepted_keys=accepted_keys,
        )
        values = _without_consumed(values, consumed_keys)
        if values:
            sections.append((title, values))
            field_count += len(values)

    remaining_tags = _without_consumed(accepted_tags, consumed_keys)
    if remaining_tags:
        sections.append(("标签", remaining_tags))

    description = ""
    if annotation_accepted:
        description = _normalize_text(
            structured.get(
                "description",
                normalized_annotation.get("description"),
            )
        )
        if description:
            sections.append(("描述", (description,)))

    full_text = "\n".join(f"{title}：{'、'.join(values)}" for title, values in sections)
    text, truncated = _truncate(full_text, max_chars)
    source_counts = {
        **tag_source_counts,
        "entities": entity_count,
        "fields": field_count,
        "description": int(bool(description)),
    }
    return MetadataTextResult(
        text=text,
        sha256=metadata_text_sha256(text),
        truncated=truncated,
        source_counts=source_counts,
    )


def metadata_text_sha256(text: str) -> str:
    """Return the lowercase SHA-256 digest used by metadata backfill."""

    if not isinstance(text, str):
        raise TypeError("metadata text must be a string.")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_mapping(
    value: Mapping[str, Any] | None,
    label: str,
) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping or None.")
    return value


def _accepted_tags(
    entry: Mapping[str, Any],
    annotation: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[str, int]]:
    """Merge trusted tag sources in provenance priority order."""

    accepted: list[str] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    sources = [
        *((name, entry.get(key)) for name, key in _TAG_SOURCES),
        ("annotation_accepted_tags", annotation.get("accepted_tags")),
    ]
    for source_name, raw_values in sources:
        contributed = 0
        for value in _stable_text_values(raw_values):
            key = _comparison_key(value)
            if key in seen:
                continue
            seen.add(key)
            accepted.append(value)
            contributed += 1
        counts[source_name] = contributed
    return tuple(accepted), counts


def _structured_annotation(annotation: Mapping[str, Any]) -> Mapping[str, Any]:
    structured = annotation.get("structured")
    return structured if isinstance(structured, Mapping) else {}


def _mapping_value(primary: Any, fallback: Any) -> Mapping[str, Any]:
    if isinstance(primary, Mapping):
        return primary
    return fallback if isinstance(fallback, Mapping) else {}


def _accepted_entity_names(
    raw_entities: Any,
    *,
    accepted_keys: set[str],
) -> tuple[str, ...]:
    values: list[str] = []
    for raw_entity in _iter_items(raw_entities):
        if isinstance(raw_entity, Mapping):
            name = _normalize_text(raw_entity.get("name"))
        else:
            name = _normalize_text(raw_entity)
        if not name:
            continue
        explicitly_accepted = _comparison_key(name) in accepted_keys
        # Review does not rewrite an older suggested/conflict state.  The tag
        # stores the actual user decision, so it is the sole inclusion gate.
        if not explicitly_accepted:
            continue
        values.append(name)
    return _stable_unique(values)


def _accepted_field_labels(
    raw_field: Any,
    *,
    accepted_keys: set[str],
) -> tuple[str, ...]:
    if not isinstance(raw_field, Mapping):
        return ()

    candidates: dict[str, tuple[str, set[str]]] = {}
    for label in _normalized_values(raw_field.get("labels")):
        key = _comparison_key(label)
        candidates[key] = (label, {key})
    for code in _normalized_values(raw_field.get("values")):
        label = _normalize_text(FIELD_TAG_LABELS.get(code, code))
        if not label:
            continue
        label_key = _comparison_key(label)
        existing = candidates.get(label_key)
        keys = set(existing[1]) if existing is not None else {label_key}
        keys.add(_comparison_key(code))
        candidates[label_key] = (existing[0] if existing is not None else label, keys)

    selected = [
        label
        for _key, (label, keys) in sorted(candidates.items())
        if bool(keys & accepted_keys)
    ]
    return _stable_unique(selected)


def _stable_text_values(value: Any) -> tuple[str, ...]:
    return tuple(sorted(_normalized_values(value), key=_comparison_key))


def _normalized_values(value: Any) -> tuple[str, ...]:
    values = [
        normalized
        for item in _iter_items(value)
        if (normalized := _normalize_text(item))
    ]
    return _stable_unique(values)


def _iter_items(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, Mapping)):
        return (value,)
    if isinstance(value, Iterable):
        return value
    return ()


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        " "
        if character.isspace()
        else ""
        if unicodedata.category(character) in {"Cc", "Cf"}
        else character
        for character in normalized
    )
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for value in values:
        key = _comparison_key(value)
        if key:
            unique.setdefault(key, value)
    return tuple(unique[key] for key in sorted(unique))


def _without_consumed(
    values: Iterable[str],
    consumed_keys: set[str],
) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        key = _comparison_key(value)
        if not key or key in consumed_keys:
            continue
        consumed_keys.add(key)
        result.append(value)
    return tuple(result)


def _comparison_key(value: str) -> str:
    return _normalize_text(value).casefold()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars == 1:
        return "…", True
    prefix = _TRAILING_SEPARATOR_RE.sub("", text[: max_chars - 1].rstrip())
    if not prefix:
        return "…", True
    return f"{prefix}…", True
