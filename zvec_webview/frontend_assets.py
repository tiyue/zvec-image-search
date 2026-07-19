"""Validate the self-contained Vite build consumed by every desktop package."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

VITE_MANIFEST: Final = PurePosixPath(".vite/manifest.json")
_MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
_HTML_REFERENCE = re.compile(
    r"\b(?:src|href)\s*=\s*([\"'])(?P<value>.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
_REMOTE_REFERENCE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)
_CSS_REMOTE_REFERENCE = re.compile(
    r"(?:@import\s+(?:url\()?|url\()\s*[\"']?(?:https?:)?//",
    re.IGNORECASE,
)


class FrontendAssetError(RuntimeError):
    """The generated frontend is absent, unsafe, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class FrontendBuild:
    """Validated Vite entry point and its complete local resource closure."""

    root: Path
    manifest_path: Path
    entry_key: str
    entry_file: str
    files: tuple[Path, ...]

    @property
    def relative_files(self) -> tuple[str, ...]:
        return tuple(path.relative_to(self.root).as_posix() for path in self.files)

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest_path.relative_to(self.root).as_posix(),
            "entry_key": self.entry_key,
            "entry_file": self.entry_file,
            "files": list(self.relative_files),
        }


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FrontendAssetError(f"Vite manifest {label} must be an object.")
    return value


def _strings(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise FrontendAssetError(f"Vite manifest {label} must be a string array.")
    if not all(isinstance(item, str) and item for item in value):
        raise FrontendAssetError(f"Vite manifest {label} contains an invalid path.")
    return tuple(value)


def _relative_path(value: str, *, label: str) -> PurePosixPath:
    if (
        not value
        or "\0" in value
        or "\\" in value
        or value.startswith(("/", "//"))
        or _REMOTE_REFERENCE.match(value)
        or re.match(r"^[a-z][a-z0-9+.-]*:", value, re.IGNORECASE)
    ):
        raise FrontendAssetError(f"{label} is not a local relative path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "node_modules" in {part.casefold() for part in path.parts}
    ):
        raise FrontendAssetError(f"{label} is unsafe: {value!r}")
    return path


def _required_file(root: Path, relative: PurePosixPath, *, label: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FrontendAssetError(f"{label} is missing: {relative.as_posix()}") from exc
    if not resolved.is_file() or candidate.is_symlink() or resolved.stat().st_size <= 0:
        raise FrontendAssetError(f"{label} is not a non-empty regular file: {relative}")
    return resolved


def _html_local_references(index: Path) -> set[str]:
    try:
        source = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FrontendAssetError(f"Unable to read Vite index.html: {exc}") from exc
    references: set[str] = set()
    for match in _HTML_REFERENCE.finditer(source):
        raw = html.unescape(match.group("value")).strip()
        if not raw or raw.startswith(("#", "data:")):
            continue
        if _REMOTE_REFERENCE.match(raw) or re.match(
            r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE
        ):
            raise FrontendAssetError(
                f"Vite index.html references a remote/CDN resource: {raw!r}"
            )
        without_suffix = raw.split("#", 1)[0].split("?", 1)[0]
        while without_suffix.startswith("./"):
            without_suffix = without_suffix[2:]
        references.add(_relative_path(without_suffix, label="HTML resource").as_posix())
    return references


def validate_frontend_build(directory: str | Path) -> FrontendBuild:
    """Validate one Vite output without assuming hashed JS or CSS filenames."""

    supplied = Path(directory).expanduser().absolute()
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise FrontendAssetError(
            f"Frontend build directory is missing: {supplied}"
        ) from exc
    if not root.is_dir() or supplied.is_symlink():
        raise FrontendAssetError(f"Frontend build directory is unsafe: {supplied}")
    if any(
        "node_modules" in {part.casefold() for part in path.relative_to(root).parts}
        for path in root.rglob("*")
    ):
        raise FrontendAssetError("Frontend build must not contain node_modules.")

    index = _required_file(root, PurePosixPath("index.html"), label="Vite entry HTML")
    manifest_path = _required_file(root, VITE_MANIFEST, label="Vite manifest")
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise FrontendAssetError("Vite manifest is too large.")
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrontendAssetError(f"Unable to read Vite manifest: {exc}") from exc
    manifest = _object(manifest_value, label="root")

    entries: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in manifest.items():
        if not isinstance(key, str):
            raise FrontendAssetError("Vite manifest keys must be strings.")
        record = _object(value, label=f"entry {key!r}")
        if record.get("isEntry") is True and (
            key == "index.html" or record.get("src") == "index.html"
        ):
            entries.append((key, record))
    if len(entries) != 1:
        raise FrontendAssetError(
            "Vite manifest must contain exactly one index.html application entry."
        )

    entry_key, entry = entries[0]
    discovered: dict[str, Path] = {
        "index.html": index,
        VITE_MANIFEST.as_posix(): manifest_path,
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise FrontendAssetError(f"Vite manifest import cycle detected at {key!r}.")
        raw_record = manifest.get(key)
        record = _object(raw_record, label=f"entry {key!r}")
        visiting.add(key)
        file_value = record.get("file")
        if not isinstance(file_value, str):
            raise FrontendAssetError(f"Vite manifest entry {key!r} has no output file.")
        resources = (
            (file_value,)
            + _strings(record.get("css"), label=f"entry {key!r} css")
            + _strings(record.get("assets"), label=f"entry {key!r} assets")
        )
        for value in resources:
            relative = _relative_path(value, label=f"Vite resource for {key!r}")
            discovered[relative.as_posix()] = _required_file(
                root, relative, label="Vite resource"
            )
        imports = _strings(
            record.get("imports"), label=f"entry {key!r} imports"
        ) + _strings(
            record.get("dynamicImports"),
            label=f"entry {key!r} dynamic imports",
        )
        for imported in imports:
            if imported not in manifest:
                raise FrontendAssetError(
                    "Vite manifest entry "
                    f"{key!r} references unknown import {imported!r}."
                )
            visit(imported)
        visiting.remove(key)
        visited.add(key)

    visit(entry_key)
    html_references = _html_local_references(index)
    entry_file = str(entry["file"])
    direct_resources = {entry_file, *_strings(entry.get("css"), label="entry css")}
    missing_references = sorted(direct_resources - html_references)
    if missing_references:
        raise FrontendAssetError(
            "Vite index.html does not reference its manifest entry resources: "
            + ", ".join(missing_references)
        )
    for reference in html_references:
        discovered[reference] = _required_file(
            root, PurePosixPath(reference), label="HTML resource"
        )

    for relative, path in discovered.items():
        if relative.casefold().endswith(".css"):
            try:
                css = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise FrontendAssetError(
                    f"Unable to read bundled CSS {relative}: {exc}"
                ) from exc
            if _CSS_REMOTE_REFERENCE.search(css):
                raise FrontendAssetError(
                    f"Bundled CSS references a remote/CDN resource: {relative}"
                )

    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    expected_files = set(discovered)
    if actual_files != expected_files:
        extras = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise FrontendAssetError(
            "Vite output differs from its manifest resource closure; "
            f"extra={extras[:5]}, missing={missing[:5]}"
        )

    files = tuple(discovered[key] for key in sorted(discovered, key=str.casefold))
    return FrontendBuild(root, manifest_path, entry_key, entry_file, files)


__all__ = [
    "FrontendAssetError",
    "FrontendBuild",
    "VITE_MANIFEST",
    "validate_frontend_build",
]
