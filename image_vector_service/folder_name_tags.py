"""Deterministic folder-name tags shared by indexing and batch backfill."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

DEFAULT_FOLDER_NAME_TAG_BLACKLIST = (
    "自摄",
    "自拍",
    "自撮",
    "4K",
    "映画",
    "Vol",
    "图包",
    "加冕",
    "月份",
    "舰长",
    "正片",
    "V",
    "cosplay打赏群 预览目录（部分） – Telegraph_files",
    "‹",
    "自拍+小视频",
    "小视频",
    "Perohub",
    "订阅",
    "表情包",
    "日期",
)

_BRACKETED_METADATA_PATTERN = re.compile(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）")
_IMAGE_EXTENSION_SUFFIX_PATTERN = re.compile(r"_[A-Za-z0-9]{1,4}$")
_IMAGE_COUNT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:P|张|枚|图|pics?|pictures?|images?|files?)",
    re.IGNORECASE,
)
_FILE_SIZE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:bytes?|[KMGT](?:i?B)?)",
    re.IGNORECASE,
)
_FULL_BRACKETED_TAG_PATTERN = re.compile(r"^(?:\[[^\]]*\]|【[^】]*】|】)$")
_FULL_EXTENSION_TAG_PATTERN = re.compile(r"^_[A-Za-z0-9]{1,4}$")
_FULL_TECHNICAL_TAG_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*(?:P|张|枚|图|pics?|pictures?|images?|files?)"
    r"|\d+(?:\.\d+)?\s*(?:bytes?|[KMGT](?:i?B)?))$",
    re.IGNORECASE,
)
_WHITESPACE_PATTERN = re.compile(r"\s+")
MAX_FOLDER_NAME_TAGS = 64


def _normalized_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(
        " ", unicodedata.normalize("NFKC", value).strip()
    )


@dataclass(frozen=True)
class ExistingTagCleanup:
    tags: tuple[str, ...]
    removed_blacklist: int = 0
    removed_legacy: int = 0
    removed_duplicates: int = 0


class FolderNameTagPolicy:
    """Validated, immutable blacklist rules and their stable revision."""

    def __init__(self, blacklist: Iterable[str] = DEFAULT_FOLDER_NAME_TAG_BLACKLIST):
        normalized: list[str] = []
        seen: set[str] = set()
        for value in blacklist:
            if not isinstance(value, str):
                raise ValueError("Folder-name tag blacklist entries must be strings.")
            rule = _normalized_text(value)
            if not rule:
                raise ValueError("Folder-name tag blacklist entries cannot be empty.")
            if any(ord(character) < 32 for character in rule):
                raise ValueError(
                    "Folder-name tag blacklist entries cannot contain "
                    "control characters."
                )
            key = rule.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(rule)
        if not normalized:
            raise ValueError("Folder-name tag blacklist cannot be empty.")
        self.blacklist = tuple(normalized)
        self._prefixes = tuple(
            value.casefold() for value in self.blacklist if " " not in value
        )
        self._whole_folders = frozenset(
            clean_folder_name(value).casefold()
            for value in self.blacklist
            if " " in value and clean_folder_name(value)
        )
        encoded = json.dumps(
            self.blacklist,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.revision = sha256(encoded).hexdigest()

    def blocks_folder(self, cleaned_folder_name: str) -> bool:
        return _normalized_text(cleaned_folder_name).casefold() in self._whole_folders

    def blocks_tag(self, value: str) -> bool:
        folded = _normalized_text(value).casefold()
        if not folded:
            return False
        if folded in self._whole_folders:
            return True
        return any(folded.startswith(prefix) for prefix in self._prefixes)


def clean_folder_name(folder_name: str) -> str:
    if not isinstance(folder_name, str):
        raise ValueError("Folder name must be a string.")
    cleaned = _normalized_text(folder_name)
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError("Folder name cannot contain control characters.")
    cleaned = _BRACKETED_METADATA_PATTERN.sub(" ", cleaned).replace("】", " ")
    cleaned = _IMAGE_EXTENSION_SUFFIX_PATTERN.sub("", cleaned.strip())
    cleaned = _IMAGE_COUNT_PATTERN.sub(" ", cleaned)
    cleaned = _FILE_SIZE_PATTERN.sub(" ", cleaned)
    return _WHITESPACE_PATTERN.sub(" ", cleaned).strip(" -_")


DEFAULT_FOLDER_NAME_TAG_POLICY = FolderNameTagPolicy()


def folder_name_tags_for_relative_path(
    relative_path: str,
    root_name: str,
    *,
    policy: FolderNameTagPolicy = DEFAULT_FOLDER_NAME_TAG_POLICY,
) -> tuple[str, ...]:
    """Derive tags from the image folder with at most one parent fallback."""

    portable = PurePosixPath(str(relative_path).replace("\\", "/"))
    if portable.is_absolute() or not portable.name or ".." in portable.parts:
        raise ValueError("Image relative path is invalid.")

    parent_names = portable.parts[:-1]
    if parent_names:
        candidates = [parent_names[-1]]
        candidates.append(parent_names[-2] if len(parent_names) > 1 else root_name)
    else:
        candidates = [root_name]
    for name in candidates:
        tags = derive_folder_name_tags(name, policy=policy)
        if tags:
            return tags
    return ()


def derive_folder_name_tags(
    folder_name: str,
    *,
    policy: FolderNameTagPolicy = DEFAULT_FOLDER_NAME_TAG_POLICY,
) -> tuple[str, ...]:
    """Return normalized tags for one folder, or an empty tuple for fallback."""

    cleaned = clean_folder_name(folder_name)
    if not cleaned or not _contains_han_or_kana(cleaned):
        return ()
    if policy.blocks_folder(cleaned):
        return ()

    tags: list[str] = []
    seen: set[str] = set()
    for segment in cleaned.split(" "):
        if not segment or policy.blocks_tag(segment):
            continue
        normalized = _strip_symbols(segment)
        _append_unique(tags, seen, normalized)
        if _contains_han(segment) and _contains_ascii_alnum(segment):
            _append_unique(tags, seen, "".join(filter(_is_han, segment)))
        if len(tags) >= MAX_FOLDER_NAME_TAGS:
            break
    return tuple(tags)


def clean_existing_tags(
    values: Iterable[str] | None,
    *,
    policy: FolderNameTagPolicy = DEFAULT_FOLDER_NAME_TAG_POLICY,
) -> ExistingTagCleanup:
    """Clean only manual/inherited tags while retaining first display spelling."""

    tags: list[str] = []
    seen: set[str] = set()
    removed_blacklist = 0
    removed_legacy = 0
    removed_duplicates = 0
    for raw in values or ():
        if not isinstance(raw, str):
            raise ValueError("Tags must be strings.")
        tag = _normalized_text(raw)
        if not tag:
            removed_legacy += 1
            continue
        if any(ord(character) < 32 for character in tag):
            raise ValueError("Tags cannot contain control characters.")
        if _is_legacy_dirty_tag(tag):
            removed_legacy += 1
            continue
        if policy.blocks_tag(tag):
            removed_blacklist += 1
            continue
        key = tag.casefold()
        if key in seen:
            removed_duplicates += 1
            continue
        seen.add(key)
        tags.append(tag)
    return ExistingTagCleanup(
        tags=tuple(tags),
        removed_blacklist=removed_blacklist,
        removed_legacy=removed_legacy,
        removed_duplicates=removed_duplicates,
    )


def merge_effective_tags(*sources: Iterable[str]) -> tuple[str, ...]:
    """Case-insensitive effective-view dedupe without mutating source fields."""

    merged: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for value in source:
            tag = value.strip()
            key = unicodedata.normalize("NFKC", tag).casefold()
            if not tag or key in seen:
                continue
            seen.add(key)
            merged.append(tag)
    return tuple(merged)


def _is_legacy_dirty_tag(value: str) -> bool:
    return bool(
        _FULL_BRACKETED_TAG_PATTERN.fullmatch(value)
        or _FULL_EXTENSION_TAG_PATTERN.fullmatch(value)
        or _FULL_TECHNICAL_TAG_PATTERN.fullmatch(value)
    )


def _append_unique(tags: list[str], seen: set[str], value: str) -> None:
    key = value.casefold()
    if not value or key in seen or len(tags) >= MAX_FOLDER_NAME_TAGS:
        return
    seen.add(key)
    tags.append(value)


def _strip_symbols(value: str) -> str:
    return "".join(
        character
        for character in value
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _contains_ascii_alnum(value: str) -> bool:
    return any(character.isascii() and character.isalnum() for character in value)


def _contains_han(value: str) -> bool:
    return any(_is_han(character) for character in value)


def _contains_han_or_kana(value: str) -> bool:
    return any(_is_han(character) or _is_kana(character) for character in value)


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _is_kana(character: str) -> bool:
    codepoint = ord(character)
    return 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF


__all__ = [
    "DEFAULT_FOLDER_NAME_TAG_BLACKLIST",
    "DEFAULT_FOLDER_NAME_TAG_POLICY",
    "ExistingTagCleanup",
    "FolderNameTagPolicy",
    "MAX_FOLDER_NAME_TAGS",
    "clean_existing_tags",
    "clean_folder_name",
    "derive_folder_name_tags",
    "folder_name_tags_for_relative_path",
    "merge_effective_tags",
]
