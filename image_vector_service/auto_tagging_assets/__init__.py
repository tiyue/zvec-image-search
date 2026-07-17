"""Active versioned prompt and vocabulary for visual auto-tagging."""

from .v3 import (
    AUTO_TAGGING_SCHEMA_VERSION,
    CATEGORY_TAGS,
    ENTITY_STATES,
    ENTITY_TYPES,
    FIELD_SPECS,
    FIELD_TAG_LABELS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    FieldSpec,
)

__all__ = [
    "AUTO_TAGGING_SCHEMA_VERSION",
    "CATEGORY_TAGS",
    "ENTITY_TYPES",
    "ENTITY_STATES",
    "FIELD_SPECS",
    "FIELD_TAG_LABELS",
    "FieldSpec",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
]
