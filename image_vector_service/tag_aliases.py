from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TAG_ALIAS_SCHEMA_VERSION = 1
MAX_ALIAS_TEXT_LENGTH = 256


class TagAliasError(Exception):
    """Base error for tag-alias configuration and persistence."""


class TagAliasFormatError(TagAliasError, ValueError):
    """Raised when an alias manifest or API value has an invalid shape."""


class TagAliasConflictError(TagAliasError, ValueError):
    """Raised when one normalized term belongs to more than one group."""


class TagAliasFileError(TagAliasError, OSError):
    """Raised when an alias manifest cannot be read or atomically saved."""


@dataclass(frozen=True)
class TagAliasGroup:
    """One canonical tag and its accepted alternative spellings."""

    canonical: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical = _display_text(self.canonical, "canonical")
        if isinstance(self.aliases, str):
            raise TagAliasFormatError("aliases must be an array of strings.")

        normalized_canonical = normalize_tag_alias_text(canonical)
        aliases: dict[str, str] = {}
        try:
            values = iter(self.aliases)
        except TypeError as exc:
            raise TagAliasFormatError("aliases must be an array of strings.") from exc
        for index, value in enumerate(values):
            alias = _display_text(value, f"aliases[{index}]")
            normalized = normalize_tag_alias_text(alias)
            if normalized == normalized_canonical:
                continue
            aliases.setdefault(normalized, alias)

        object.__setattr__(self, "canonical", canonical)
        object.__setattr__(
            self,
            "aliases",
            tuple(sorted(aliases.values(), key=_display_sort_key)),
        )

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.canonical, *self.aliases)

    def to_dict(self) -> dict[str, object]:
        return {"canonical": self.canonical, "aliases": list(self.aliases)}


class TagAliasDictionary:
    """Immutable alias dictionary with normalized global conflict detection."""

    def __init__(self, groups: Iterable[TagAliasGroup] = ()) -> None:
        prepared: list[TagAliasGroup] = []
        owners: dict[str, TagAliasGroup] = {}
        try:
            values = iter(groups)
        except TypeError as exc:
            raise TagAliasFormatError("groups must be an array.") from exc

        for index, value in enumerate(values):
            if not isinstance(value, TagAliasGroup):
                raise TagAliasFormatError(f"groups[{index}] must be a TagAliasGroup.")
            for term in value.terms:
                normalized = normalize_tag_alias_text(term)
                owner = owners.get(normalized)
                if owner is not None:
                    raise TagAliasConflictError(
                        f"Tag alias {term!r} conflicts with canonical tag "
                        f"{owner.canonical!r}."
                    )
                owners[normalized] = value
            prepared.append(value)

        self._groups = tuple(
            sorted(prepared, key=lambda group: _display_sort_key(group.canonical))
        )
        self._by_term = {
            normalize_tag_alias_text(term): group
            for group in self._groups
            for term in group.terms
        }
        self._by_canonical = {
            normalize_tag_alias_text(group.canonical): group for group in self._groups
        }

    @property
    def groups(self) -> tuple[TagAliasGroup, ...]:
        return self._groups

    @property
    def count(self) -> int:
        return len(self._groups)

    def canonical_for(self, term: str) -> str | None:
        group = self._by_term.get(normalize_tag_alias_text(term))
        return None if group is None else group.canonical

    def equivalent_terms(self, term: str) -> tuple[str, ...]:
        """Return the exact alias group for a term, or the normalized term itself."""

        display = _display_text(term, "term")
        group = self._by_term.get(normalize_tag_alias_text(display))
        return (display,) if group is None else group.terms

    def normalized_equivalent_terms(self, term: str) -> frozenset[str]:
        return frozenset(
            normalize_tag_alias_text(value) for value in self.equivalent_terms(term)
        )

    def upsert(self, canonical: str, aliases: Iterable[str] = ()) -> TagAliasDictionary:
        group = TagAliasGroup(canonical=canonical, aliases=_alias_tuple(aliases))
        normalized = normalize_tag_alias_text(group.canonical)
        retained = [
            item
            for item in self._groups
            if normalize_tag_alias_text(item.canonical) != normalized
        ]
        return TagAliasDictionary([*retained, group])

    def delete(self, canonical: str) -> tuple[TagAliasDictionary, bool]:
        normalized = normalize_tag_alias_text(canonical)
        if normalized not in self._by_canonical:
            return self, False
        return (
            TagAliasDictionary(
                group
                for group in self._groups
                if normalize_tag_alias_text(group.canonical) != normalized
            ),
            True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": TAG_ALIAS_SCHEMA_VERSION,
            "groups": [group.to_dict() for group in self._groups],
        }

    def list_response(self) -> dict[str, object]:
        """Return the stable shape used by the backend ``tag_alias_list`` command."""

        return {
            "groups": [group.to_dict() for group in self._groups],
            "count": self.count,
        }

    def save(self, path: str | Path) -> None:
        _atomic_write_json(Path(path), self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> TagAliasDictionary:
        if not isinstance(payload, Mapping):
            raise TagAliasFormatError("Tag alias file must contain a JSON object.")
        unknown = set(payload) - {"schema_version", "groups"}
        if unknown:
            raise TagAliasFormatError(
                "Tag alias file contains unknown fields: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
        if payload.get("schema_version") != TAG_ALIAS_SCHEMA_VERSION:
            raise TagAliasFormatError(
                f"Tag alias schema_version must be {TAG_ALIAS_SCHEMA_VERSION}."
            )
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list):
            raise TagAliasFormatError("Tag alias groups must be an array.")

        groups: list[TagAliasGroup] = []
        for index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, Mapping):
                raise TagAliasFormatError(f"groups[{index}] must be an object.")
            unknown_group = set(raw_group) - {"canonical", "aliases"}
            if unknown_group:
                raise TagAliasFormatError(
                    f"groups[{index}] contains unknown fields: "
                    + ", ".join(sorted(str(value) for value in unknown_group))
                )
            if "canonical" not in raw_group or "aliases" not in raw_group:
                raise TagAliasFormatError(
                    f"groups[{index}] requires canonical and aliases."
                )
            raw_aliases = raw_group["aliases"]
            if not isinstance(raw_aliases, list):
                raise TagAliasFormatError(f"groups[{index}].aliases must be an array.")
            groups.append(
                TagAliasGroup(
                    canonical=raw_group["canonical"],
                    aliases=tuple(raw_aliases),
                )
            )
        return cls(groups)

    @classmethod
    def load(cls, path: str | Path, *, missing_ok: bool = False) -> TagAliasDictionary:
        manifest = Path(path)
        if not manifest.exists():
            if missing_ok:
                return cls()
            raise TagAliasFileError(f"Tag alias file is missing: {manifest}")
        if not manifest.is_file():
            raise TagAliasFileError(f"Tag alias path is not a file: {manifest}")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TagAliasFileError(
                f"Tag alias file is not valid UTF-8 JSON: {manifest}"
            ) from exc
        return cls.from_dict(payload)


class TagAliasStore:
    """Thread-safe file-backed facade suitable for backend command handlers."""

    def __init__(self, path: str | Path, aliases: TagAliasDictionary) -> None:
        self.path = Path(path)
        self._aliases = aliases
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: str | Path, *, missing_ok: bool = True) -> TagAliasStore:
        return cls(
            path,
            TagAliasDictionary.load(path, missing_ok=missing_ok),
        )

    def snapshot(self) -> TagAliasDictionary:
        with self._lock:
            return self._aliases

    def list_groups(self) -> dict[str, object]:
        with self._lock:
            return self._aliases.list_response()

    def upsert(self, canonical: str, aliases: Iterable[str]) -> TagAliasGroup:
        with self._lock:
            updated = self._aliases.upsert(canonical, aliases)
            updated.save(self.path)
            self._aliases = updated
            normalized = normalize_tag_alias_text(canonical)
            return next(
                group
                for group in updated.groups
                if normalize_tag_alias_text(group.canonical) == normalized
            )

    def delete(self, canonical: str) -> bool:
        with self._lock:
            updated, deleted = self._aliases.delete(canonical)
            if not deleted:
                return False
            updated.save(self.path)
            self._aliases = updated
            return True


def normalize_tag_alias_text(value: str) -> str:
    """Normalize a canonical tag or alias for identity comparisons."""

    display = _display_text(value, "tag alias")
    return display.casefold()


def _display_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TagAliasFormatError(f"{field} must be a string.")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise TagAliasFormatError(f"{field} cannot be empty.")
    if len(normalized) > MAX_ALIAS_TEXT_LENGTH:
        raise TagAliasFormatError(
            f"{field} cannot exceed {MAX_ALIAS_TEXT_LENGTH} characters."
        )
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise TagAliasFormatError(f"{field} cannot contain control characters.")
    return normalized


def _alias_tuple(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TagAliasFormatError("aliases must be an array of strings.")
    try:
        return tuple(values)
    except TypeError as exc:
        raise TagAliasFormatError("aliases must be an array of strings.") from exc


def _display_sort_key(value: str) -> tuple[str, str]:
    return (normalize_tag_alias_text(value), value)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    parent = path.parent
    temporary: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        serialized = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            + "\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise TagAliasFileError(f"Failed to save tag alias file: {path}") from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
