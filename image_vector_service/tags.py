from __future__ import annotations

from collections.abc import Iterable


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
