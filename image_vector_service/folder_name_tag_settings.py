"""Persistent global settings for folder-name tagging."""

from __future__ import annotations

import json
import os
import unicodedata
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from .folder_name_tags import (
    DEFAULT_FOLDER_NAME_TAG_BLACKLIST,
    FolderNameTagPolicy,
)

FOLDER_NAME_TAG_SETTINGS_FILENAME = "folder-name-tagging.json"
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_BLACKLIST_ENTRIES = 256
MAX_BLACKLIST_ENTRY_LENGTH = 512


class FolderNameTagSettingsStore:
    def __init__(self, config_home: Path) -> None:
        self.path = (
            config_home.expanduser().resolve() / FOLDER_NAME_TAG_SETTINGS_FILENAME
        )

    def load(self) -> dict[str, Any]:
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("settings file is missing or too large")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("settings must be an object")
            blacklist = _normalize_blacklist(payload.get("blacklist"))
            policy = FolderNameTagPolicy(blacklist)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            policy = FolderNameTagPolicy(DEFAULT_FOLDER_NAME_TAG_BLACKLIST)
            return _view(policy, using_defaults=True)
        return _view(policy, using_defaults=False)

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("Folder-name tag settings must be an object.")
        unknown = set(payload) - {"blacklist"}
        if unknown:
            raise ValueError(
                "Folder-name tag settings contain unsupported fields: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
        policy = FolderNameTagPolicy(_normalize_blacklist(payload.get("blacklist")))
        document = {
            "schema_version": 1,
            "blacklist": list(policy.blacklist),
        }
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > MAX_SETTINGS_BYTES:
            raise ValueError("Folder-name tag settings are too large.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return _view(policy, using_defaults=False)

    def policy(self) -> FolderNameTagPolicy:
        return FolderNameTagPolicy(self.load()["blacklist"])


def _normalize_blacklist(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("blacklist must be a non-empty array")
    if len(value) > MAX_BLACKLIST_ENTRIES:
        raise ValueError(
            f"blacklist cannot contain more than {MAX_BLACKLIST_ENTRIES} entries"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("blacklist entries must be strings")
        rule = " ".join(unicodedata.normalize("NFKC", raw).strip().split())
        if not rule or len(rule) > MAX_BLACKLIST_ENTRY_LENGTH:
            raise ValueError("blacklist entries must be non-empty and bounded")
        if any(ord(character) < 32 for character in rule):
            raise ValueError("blacklist entries cannot contain control characters")
        key = rule.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(rule)
    if not normalized:
        raise ValueError("blacklist cannot be empty")
    return tuple(normalized)


def _view(policy: FolderNameTagPolicy, *, using_defaults: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "blacklist": list(policy.blacklist),
        "revision": policy.revision,
        "using_defaults": using_defaults,
    }


__all__ = [
    "FOLDER_NAME_TAG_SETTINGS_FILENAME",
    "FolderNameTagSettingsStore",
    "MAX_BLACKLIST_ENTRIES",
    "MAX_SETTINGS_BYTES",
]
