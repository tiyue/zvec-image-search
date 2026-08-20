from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from .folder_name_tags import (
    DEFAULT_FOLDER_NAME_TAG_POLICY,
    FolderNameTagPolicy,
    folder_name_tags_for_relative_path,
)

_IMAGE_COUNT_PATTERN = re.compile(
    r"(?i)\d+(?:\.\d+)?\s*(?:p|张|枚|幅|图|pics?|pictures?|images?|files?)"
)
_FILE_SIZE_PATTERN = re.compile(r"(?i)\d+(?:\.\d+)?\s*(?:bytes?|[kmgt](?:i?b)?)")
_FOLDER_SEPARATOR_PATTERN = re.compile(r"[\s_\-–—·+|/,，;；、]+")
_BRACKET_PATTERN = re.compile(r"[\[\](){}<>【】（）「」『』《》]")


def normalize_tags(tags: Iterable[str] | None) -> tuple[str, ...]:
    if tags is None:
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for value in tags:
        if not isinstance(value, str):
            raise ValueError("Tags must be strings.")
        tag = value.strip()
        if not tag:
            raise ValueError("Tags cannot be empty.")
        if any(ord(character) < 32 for character in tag):
            raise ValueError("Tags cannot contain control characters.")
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
    return tuple(normalized)


def clean_generated_tag(value: str) -> str:
    """Normalize a generated tag and remove count/size metadata fragments.

    This is intentionally used only for deterministic or model-generated tags.
    User-authored legacy tags are not silently rewritten.
    """

    if not isinstance(value, str):
        raise ValueError("Tags must be strings.")
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if not cleaned:
        return ""
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError("Tags cannot contain control characters.")

    cleaned = _BRACKET_PATTERN.sub("-", cleaned)
    cleaned = _IMAGE_COUNT_PATTERN.sub("-", cleaned)
    cleaned = _FILE_SIZE_PATTERN.sub("-", cleaned)
    cleaned = _FOLDER_SEPARATOR_PATTERN.sub("-", cleaned).strip("-")
    return cleaned


def is_technical_metadata_tag(value: str) -> bool:
    """Return whether a generated tag contains only count/size metadata."""

    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return False
    without_brackets = _BRACKET_PATTERN.sub("-", normalized)
    without_metadata = _IMAGE_COUNT_PATTERN.sub("-", without_brackets)
    without_metadata = _FILE_SIZE_PATTERN.sub("-", without_metadata)
    return not _FOLDER_SEPARATOR_PATTERN.sub("", without_metadata).strip("-")


def folder_tags_for_image(image_path: Path, root_path: Path) -> tuple[str, ...]:
    """Derive cleaned tags from the nearest meaningful parent folder.

    The lookup never walks above the configured image root. This keeps a path
    such as ``作品/120P-1.2GB/001.jpg`` useful by falling back to ``作品`` while
    avoiding accidental tags from host-specific parent directories.
    """

    image = image_path.expanduser().resolve()
    root = root_path.expanduser().resolve()
    try:
        relative = image.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Image is outside the configured root: {image}") from exc

    return folder_tags_for_relative_path(relative.as_posix(), root.name)


def folder_tags_for_relative_path(
    relative_path: str,
    root_name: str,
    *,
    policy: FolderNameTagPolicy = DEFAULT_FOLDER_NAME_TAG_POLICY,
) -> tuple[str, ...]:
    """Derive deterministic folder-source tags from a portable relative path."""

    return folder_name_tags_for_relative_path(
        relative_path,
        root_name,
        policy=policy,
    )


def build_tags_filter(tags: Iterable[str] | None, mode: str = "all") -> str | None:
    if mode not in {"all", "any"}:
        raise ValueError("tag_mode must be 'all' or 'any'.")
    normalized = normalize_tags(tags)
    if not normalized:
        return None
    operator = "CONTAIN_ALL" if mode == "all" else "CONTAIN_ANY"
    values = ",".join(_quote_filter_string(tag) for tag in normalized)
    return f"tags {operator} ({values})"


def _quote_filter_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
