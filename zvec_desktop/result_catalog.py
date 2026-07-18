from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

CONFIG_SCHEMA_VERSION = 3
DEFAULT_PAGE_SIZE = 15
MAX_PAGE_SIZE = 500
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
IMAGE_SUFFIXES = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)


class ResultCatalogError(RuntimeError):
    """Raised when desktop result data cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class LibraryRecord:
    """The subset of one schema-v3 library needed by the desktop gallery."""

    library_id: str
    name: str
    image_root: Path
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    """Validated desktop-facing launcher configuration."""

    config_path: Path
    results_directory: Path
    default_library_id: str
    libraries: tuple[LibraryRecord, ...]

    @property
    def libraries_by_id(self) -> dict[str, LibraryRecord]:
        return {library.library_id: library for library in self.libraries}


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One displayable image from a manifest or an arbitrary image folder."""

    display_path: Path
    copied_path: Path | None
    original_path: Path | None
    name: str
    rank: int
    relative_path: str
    library_name: str
    tags: tuple[str, ...] = ()
    matched_tags: tuple[str, ...] = ()
    confidence: float = 0.0
    match_state: str = "weak"
    rank_source: str = "folder"
    library_id: str = ""
    doc_id: str = ""


@dataclass(frozen=True, slots=True)
class SearchResultPage:
    """A stable page of results suitable for a 15-tile desktop gallery."""

    items: tuple[SearchResult, ...]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    summary: str
    source_label: str
    manifest_path: Path | None = None
    source_folder: Path | None = None
    query_type: str = "folder"
    status: str = "ok"

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


class ResultCatalog:
    """Read-only adapter between existing result files and the Python UI."""

    def __init__(self, config: CatalogConfig) -> None:
        self.config = config
        # Folder browsing may contain hundreds of thousands of images. Keep one
        # sorted path snapshot so changing gallery pages does not rescan the tree.
        self._folder_cache: dict[Path, tuple[Path, ...]] = {}

    @property
    def results_directory(self) -> Path:
        return self.config.results_directory

    @property
    def libraries(self) -> tuple[LibraryRecord, ...]:
        return self.config.libraries

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> ResultCatalog:
        """Load a schema-v3 config, using ConfigHome/config.json by default."""

        config_path = _default_config_path() if path is None else Path(path)
        config_path = config_path.expanduser().resolve()
        payload = _read_json_object(
            config_path, label="desktop configuration", limit=MAX_CONFIG_BYTES
        )
        if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise ResultCatalogError(
                f"Desktop configuration schema_version must be "
                f"{CONFIG_SCHEMA_VERSION}: {config_path}"
            )

        results_directory = _absolute_path(
            payload.get("results_directory"), "results_directory"
        )
        default_library_id = _non_empty_text(
            payload.get("default_library_id"), "default_library_id"
        )
        raw_libraries = payload.get("libraries")
        if not isinstance(raw_libraries, list) or not raw_libraries:
            raise ResultCatalogError(
                "Configuration requires a non-empty libraries array."
            )

        libraries: list[LibraryRecord] = []
        seen_ids: set[str] = set()
        for index, raw_library in enumerate(raw_libraries):
            if not isinstance(raw_library, Mapping):
                raise ResultCatalogError(f"libraries[{index}] must be an object.")
            library_id = _non_empty_text(
                raw_library.get("id"), f"libraries[{index}].id"
            )
            if library_id in seen_ids:
                raise ResultCatalogError(f"Duplicate library id: {library_id}")
            name = _non_empty_text(raw_library.get("name"), f"libraries[{index}].name")
            enabled = raw_library.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ResultCatalogError(
                    f"libraries[{index}].enabled must be a boolean."
                )
            libraries.append(
                LibraryRecord(
                    library_id=library_id,
                    name=name,
                    image_root=_absolute_path(
                        raw_library.get("image_root"),
                        f"libraries[{index}].image_root",
                    ),
                    enabled=enabled,
                )
            )
            seen_ids.add(library_id)

        if default_library_id not in seen_ids:
            raise ResultCatalogError(
                "default_library_id does not identify a configured library."
            )
        return cls(
            CatalogConfig(
                config_path=config_path,
                results_directory=results_directory,
                default_library_id=default_library_id,
                libraries=tuple(libraries),
            )
        )

    def load_latest(
        self, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> SearchResultPage:
        """Load the newest valid immediate-child ``results.json`` manifest."""

        page, page_size = _validate_pagination(page, page_size)
        manifest_path, payload = self._latest_manifest()
        return self._page_from_manifest(
            manifest_path, payload, page=page, page_size=page_size
        )

    def load_manifest(
        self,
        manifest_path: str | Path,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SearchResultPage:
        """Load one explicit result manifest below the configured results root."""

        page, page_size = _validate_pagination(page, page_size)
        requested = Path(manifest_path).expanduser()
        if requested.name.casefold() != "results.json":
            raise ResultCatalogError("A result manifest must be named results.json.")
        if _is_link_like(requested):
            raise ResultCatalogError(f"Result manifest must not be a link: {requested}")
        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ResultCatalogError(
                f"Result manifest does not exist: {requested}"
            ) from exc
        if not resolved.is_file():
            raise ResultCatalogError(f"Result manifest is not a file: {resolved}")
        try:
            resolved.relative_to(self.results_directory.resolve())
        except ValueError as exc:
            raise ResultCatalogError(
                "Result manifest must be inside the configured results directory."
            ) from exc
        if _is_link_like(resolved.parent):
            raise ResultCatalogError(
                f"Result manifest directory must not be a link: {resolved.parent}"
            )
        payload = _read_json_object(
            resolved, label="search result manifest", limit=MAX_MANIFEST_BYTES
        )
        return self._page_from_manifest(
            resolved, payload, page=page, page_size=page_size
        )

    def _page_from_manifest(
        self,
        manifest_path: Path,
        payload: Mapping[str, Any],
        *,
        page: int,
        page_size: int,
    ) -> SearchResultPage:
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ResultCatalogError(
                f"Manifest results must be an array: {manifest_path}"
            )

        results: list[SearchResult] = []
        for index, raw_result in enumerate(raw_results):
            if not isinstance(raw_result, Mapping):
                raise ResultCatalogError(
                    f"Manifest results[{index}] must be an object: {manifest_path}"
                )
            result = self._manifest_result(
                raw_result,
                index=index,
                manifest_path=manifest_path,
                manifest=payload,
            )
            if result is not None:
                results.append(result)

        return _page_of(
            results,
            page=page,
            page_size=page_size,
            manifest_path=manifest_path,
            source_label=manifest_path.parent.name,
            query_type=_optional_text(payload.get("query_type"), "unknown"),
            status=_optional_text(payload.get("status"), "ok"),
        )

    def from_folder(
        self,
        folder: str | Path,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> SearchResultPage:
        """Build a recursively sorted gallery page from any image directory."""

        page, page_size = _validate_pagination(page, page_size)
        _raise_if_cancelled(cancelled)
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ResultCatalogError(f"Image folder does not exist: {root}")

        image_paths = self._folder_cache.get(root)
        if image_paths is None:
            image_paths = tuple(_images_below(root, cancelled=cancelled))
            _raise_if_cancelled(cancelled)
            self._folder_cache[root] = image_paths
        _raise_if_cancelled(cancelled)

        # Slice the cached paths before constructing rich result records. This
        # keeps each page O(page_size) even when a library contains 100,000+
        # images, while the global offset preserves stable one-based ranks.
        total_items = len(image_paths)
        start = (page - 1) * page_size
        page_paths = image_paths[start : start + page_size]
        results = [
            SearchResult(
                display_path=image_path,
                copied_path=None,
                original_path=image_path,
                name=image_path.name,
                rank=index,
                relative_path=image_path.relative_to(root).as_posix(),
                library_name=root.name,
            )
            for index, image_path in enumerate(page_paths, start=start + 1)
        ]
        return _page_of_slice(
            results,
            total_items=total_items,
            page=page,
            page_size=page_size,
            source_folder=root,
            source_label=root.name,
            query_type="folder",
            status="ok",
        )

    def clear_folder_cache(self, folder: str | Path | None = None) -> None:
        """Forget one folder scan, or every scan when ``folder`` is omitted."""

        if folder is None:
            self._folder_cache.clear()
            return
        self._folder_cache.pop(Path(folder).expanduser().resolve(), None)

    def _latest_manifest(self) -> tuple[Path, dict[str, Any]]:
        root = self.results_directory
        if not root.is_dir():
            raise ResultCatalogError(f"Results directory does not exist: {root}")

        candidates: list[tuple[float, int, str, Path, dict[str, Any]]] = []
        errors: list[str] = []
        try:
            directories = list(root.iterdir())
        except OSError as exc:
            raise ResultCatalogError(
                f"Could not list results directory: {root}"
            ) from exc

        for directory in directories:
            if not directory.is_dir() or _is_link_like(directory):
                continue
            manifest_path = directory / "results.json"
            if not manifest_path.is_file() or _is_link_like(manifest_path):
                continue
            try:
                payload = _read_json_object(
                    manifest_path,
                    label="search result manifest",
                    limit=MAX_MANIFEST_BYTES,
                )
                modified_ns = manifest_path.stat().st_mtime_ns
            except (OSError, ResultCatalogError) as exc:
                # A partially written/corrupt directory must not hide the last
                # complete search result from the gallery.
                errors.append(str(exc))
                continue
            created_timestamp = _created_timestamp(payload.get("created_at"))
            candidates.append(
                (
                    created_timestamp,
                    modified_ns,
                    directory.name.casefold(),
                    manifest_path,
                    payload,
                )
            )

        if not candidates:
            detail = f" Last error: {errors[-1]}" if errors else ""
            raise ResultCatalogError(
                f"No valid results.json was found below {root}.{detail}"
            )
        candidates.sort(key=lambda item: item[:3], reverse=True)
        for _created, _modified, _name, manifest_path, payload in candidates:
            try:
                raw_results = payload.get("results")
                if not isinstance(raw_results, list):
                    raise ResultCatalogError(
                        f"Manifest results must be an array: {manifest_path}"
                    )
                displayable = 0
                for index, raw_result in enumerate(raw_results):
                    if not isinstance(raw_result, Mapping):
                        raise ResultCatalogError(
                            f"Manifest results[{index}] must be an object: "
                            f"{manifest_path}"
                        )
                    if (
                        self._manifest_result(
                            raw_result,
                            index=index,
                            manifest_path=manifest_path,
                            manifest=payload,
                        )
                        is not None
                    ):
                        displayable += 1
                if raw_results and displayable != len(raw_results):
                    raise ResultCatalogError(
                        f"Manifest references unavailable result images: "
                        f"{manifest_path}"
                    )
            except ResultCatalogError as exc:
                # A valid JSON object can still be an incomplete manifest while
                # an exporter is writing it. Fall back to the newest complete
                # search instead of blanking the gallery.
                errors.append(str(exc))
                continue
            return manifest_path, payload

        detail = f" Last error: {errors[-1]}" if errors else ""
        raise ResultCatalogError(
            f"No complete results.json was found below {root}.{detail}"
        )

    def _manifest_result(
        self,
        raw: Mapping[str, Any],
        *,
        index: int,
        manifest_path: Path,
        manifest: Mapping[str, Any],
    ) -> SearchResult | None:
        label = f"results[{index}]"
        copied_relative = _safe_relative_path(
            raw.get("copied_file"), f"{label}.copied_file", required=True
        )
        assert copied_relative is not None
        copied_candidate = _resolve_below(
            manifest_path.parent,
            copied_relative,
            f"{label}.copied_file",
        )
        copied_path = copied_candidate if copied_candidate.is_file() else None

        relative_path = _safe_relative_path(
            raw.get("relative_path"), f"{label}.relative_path", required=False
        )
        library = self._result_library(raw, manifest)
        original_path: Path | None = None
        if relative_path is not None and library is not None:
            original_candidate = _resolve_below(
                library.image_root,
                relative_path,
                f"{label}.relative_path",
            )
            if original_candidate.is_file():
                original_path = original_candidate

        display_path = original_path or copied_path
        if display_path is None:
            return None

        rank = raw.get("rank", index + 1)
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ResultCatalogError(f"{label}.rank must be a positive integer.")
        confidence = _finite_number(raw.get("confidence", 0.0), f"{label}.confidence")
        return SearchResult(
            display_path=display_path,
            copied_path=copied_path,
            original_path=original_path,
            name=display_path.name,
            rank=rank,
            relative_path=(relative_path.as_posix() if relative_path else ""),
            library_name=(
                _optional_text(raw.get("library_name"), library.name if library else "")
            ),
            tags=_string_tuple(raw.get("tags"), f"{label}.tags"),
            matched_tags=_string_tuple(
                raw.get("matched_tags"), f"{label}.matched_tags"
            ),
            confidence=confidence,
            match_state=_optional_text(raw.get("match_state"), "weak"),
            rank_source=_optional_text(raw.get("rank_source"), "unknown"),
            library_id=(library.library_id if library else ""),
            doc_id=_optional_text(raw.get("doc_id"), ""),
        )

    def _result_library(
        self, raw: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> LibraryRecord | None:
        by_id = self.config.libraries_by_id
        raw_id = raw.get("library_id")
        if isinstance(raw_id, str) and raw_id.strip():
            return by_id.get(raw_id.strip())

        manifest_ids = manifest.get("library_ids")
        if isinstance(manifest_ids, list):
            configured = [
                by_id[value.strip()]
                for value in manifest_ids
                if isinstance(value, str) and value.strip() in by_id
            ]
            if len(configured) == 1:
                return configured[0]

        raw_name = raw.get("library_name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip().casefold()
            matches = [
                library for library in self.libraries if library.name.casefold() == name
            ]
            if len(matches) == 1:
                return matches[0]

        if len(self.libraries) == 1:
            return self.libraries[0]
        return by_id.get(self.config.default_library_id)


def _default_config_path() -> Path:
    configured = os.getenv("ZVEC_CONFIG_HOME") or os.getenv("ZVEC_DOCKER_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser() / "config.json"
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"], "zvec-image-search", "config.json")
    return Path.home() / ".zvec-image-search" / "config.json"


def _read_json_object(path: Path, *, label: str, limit: int) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size > limit:
            raise ResultCatalogError(
                f"{label.capitalize()} is too large ({size} bytes): {path}"
            )
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except ResultCatalogError:
        raise
    except OSError as exc:
        raise ResultCatalogError(f"Could not read {label}: {path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResultCatalogError(f"Invalid JSON in {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ResultCatalogError(f"{label.capitalize()} must contain a JSON object.")
    return payload


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ResultCatalogError(f"{label} must be a non-empty absolute path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ResultCatalogError(f"{label} must be an absolute path: {value}")
    return path.resolve()


def _non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultCatalogError(f"{label} must be a non-empty string.")
    return value.strip()


def _optional_text(value: Any, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ResultCatalogError(f"{label} must be an array of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ResultCatalogError(f"{label} must contain only strings.")
        if item.strip():
            result.append(item.strip())
    return tuple(result)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultCatalogError(f"{label} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ResultCatalogError(f"{label} must be a finite number.")
    return number


def _safe_relative_path(
    value: Any, label: str, *, required: bool
) -> PurePosixPath | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ResultCatalogError(f"{label} must be a non-empty relative path.")
        return None
    if not isinstance(value, str):
        raise ResultCatalogError(f"{label} must be a relative path string.")

    # Check both syntaxes so a Windows path is still rejected on Linux CI and
    # a POSIX absolute path is rejected on Windows.
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    if windows_path.is_absolute() or windows_path.drive or posix_path.is_absolute():
        raise ResultCatalogError(f"{label} must not be an absolute path: {value}")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ResultCatalogError(f"{label} contains unsafe path traversal: {value}")
    return posix_path


def _resolve_below(root: Path, relative: PurePosixPath, label: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ResultCatalogError(
            f"{label} escaped its allowed image directory."
        ) from exc
    return candidate


def _validate_pagination(page: int, page_size: int) -> tuple[int, int]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ResultCatalogError("page must be a positive integer.")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_PAGE_SIZE
    ):
        raise ResultCatalogError(
            f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}."
        )
    return page, page_size


def _page_of(
    results: list[SearchResult],
    *,
    page: int,
    page_size: int,
    manifest_path: Path | None = None,
    source_folder: Path | None = None,
    source_label: str,
    query_type: str,
    status: str,
) -> SearchResultPage:
    total_items = len(results)
    start = (page - 1) * page_size
    return _page_of_slice(
        results[start : start + page_size],
        total_items=total_items,
        page=page,
        page_size=page_size,
        manifest_path=manifest_path,
        source_folder=source_folder,
        source_label=source_label,
        query_type=query_type,
        status=status,
    )


def _page_of_slice(
    results: list[SearchResult],
    *,
    total_items: int,
    page: int,
    page_size: int,
    manifest_path: Path | None = None,
    source_folder: Path | None = None,
    source_label: str,
    query_type: str,
    status: str,
) -> SearchResultPage:
    """Create page metadata for an already sliced result sequence."""

    total_pages = math.ceil(total_items / page_size) if total_items else 0
    return SearchResultPage(
        items=tuple(results),
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        summary=(
            f"共 {total_items} 张图片 · 第 {page}/{total_pages} 页"
            if total_pages
            else "共 0 张图片"
        ),
        source_label=source_label,
        manifest_path=manifest_path,
        source_folder=source_folder,
        query_type=query_type,
        status=status,
    )


def _created_timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return float("-inf")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError("Image folder scan was cancelled.")


def _images_below(
    root: Path, *, cancelled: Callable[[], bool] | None = None
) -> list[Path]:
    images: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        _raise_if_cancelled(cancelled)
        current_path = Path(current)
        directory_names[:] = [
            name for name in directory_names if not _is_link_like(current_path / name)
        ]
        for file_name in file_names:
            _raise_if_cancelled(cancelled)
            candidate = current_path / file_name
            if candidate.suffix.casefold() not in IMAGE_SUFFIXES or _is_link_like(
                candidate
            ):
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                images.append(resolved)
    _raise_if_cancelled(cancelled)
    images.sort(key=lambda path: path.relative_to(root).as_posix().casefold())
    _raise_if_cancelled(cancelled)
    return images
