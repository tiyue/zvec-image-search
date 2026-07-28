"""Deterministic folder-name tags shared by indexing and batch backfill."""

from __future__ import annotations

import re
import unicodedata
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

_BLACKLIST_PREFIXES = tuple(
    unicodedata.normalize("NFKC", value).casefold()
    for value in DEFAULT_FOLDER_NAME_TAG_BLACKLIST
)
_BRACKETED_METADATA_PATTERN = re.compile(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）")
_IMAGE_EXTENSION_SUFFIX_PATTERN = re.compile(
    r"_(?:jpe?g|png|gif|bmp|webp|tiff?|svg|ico|heic|heif|avif|raw|cr2|nef|arw|dng|psd)$",
    re.IGNORECASE,
)
_IMAGE_COUNT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:P|张|枚|图|pics?|pictures?|images?|files?)",
    re.IGNORECASE,
)
_FILE_SIZE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:bytes?|[KMGT](?:i?B)?)",
    re.IGNORECASE,
)
_WHITESPACE_PATTERN = re.compile(r"\s+")
MAX_FOLDER_NAME_TAGS = 64


def folder_name_tags_for_relative_path(
    relative_path: str,
    root_name: str,
) -> tuple[str, ...]:
    """Derive tags from the nearest meaningful folder without escaping the root."""

    portable = PurePosixPath(str(relative_path).replace("\\", "/"))
    if portable.is_absolute() or not portable.name or ".." in portable.parts:
        raise ValueError("Image relative path is invalid.")

    parent_names = portable.parts[:-1]
    for name in (*reversed(parent_names), root_name):
        tags = derive_folder_name_tags(name)
        if tags:
            return tags
    return ()


def derive_folder_name_tags(folder_name: str) -> tuple[str, ...]:
    """Return normalized tags for one folder, or an empty tuple for fallback."""

    if not isinstance(folder_name, str):
        raise ValueError("Folder name must be a string.")
    cleaned = unicodedata.normalize("NFKC", folder_name).strip()
    if not cleaned:
        return ()
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError("Folder name cannot contain control characters.")

    cleaned = _BRACKETED_METADATA_PATTERN.sub(" ", cleaned)
    cleaned = _IMAGE_EXTENSION_SUFFIX_PATTERN.sub("", cleaned.strip())
    cleaned = _IMAGE_COUNT_PATTERN.sub(" ", cleaned)
    cleaned = _FILE_SIZE_PATTERN.sub(" ", cleaned)
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip(" -_")
    if not cleaned or not _contains_han_or_kana(cleaned):
        return ()

    folded = cleaned.casefold()
    if any(folded.startswith(prefix) for prefix in _BLACKLIST_PREFIXES):
        return ()

    tags: list[str] = []
    seen: set[str] = set()
    for segment in cleaned.split(" "):
        if not segment:
            continue
        segment_folded = segment.casefold()
        if any(segment_folded.startswith(prefix) for prefix in _BLACKLIST_PREFIXES):
            continue
        normalized = _strip_symbols(segment)
        _append_unique(tags, seen, normalized)
        if _contains_han(segment) and _contains_ascii_alnum(segment):
            _append_unique(tags, seen, "".join(filter(_is_han, segment)))
        if len(tags) >= MAX_FOLDER_NAME_TAGS:
            break
    return tuple(tags)


def _append_unique(tags: list[str], seen: set[str], value: str) -> None:
    if not value or value in seen or len(tags) >= MAX_FOLDER_NAME_TAGS:
        return
    seen.add(value)
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
    "MAX_FOLDER_NAME_TAGS",
    "derive_folder_name_tags",
    "folder_name_tags_for_relative_path",
]
