from __future__ import annotations

import json
import math
import os
import stat
import threading
from collections import OrderedDict
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
DEFAULT_MANIFEST_CACHE_ENTRIES = 8
DEFAULT_MANIFEST_CACHE_RESULTS = 100_000
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
    raw_score: float = 0.0
    normalized_score: float = 0.0
    confidence: float = 0.0
    ranking_confidence: float | None = None
    match_state: str = "weak"
    rank_source: str = "folder"
    image_confidence: float | None = None
    text_confidence: float | None = None
    metadata_confidence: float | None = None
    image_rank: int | None = None
    text_rank: int | None = None
    metadata_rank: int | None = None
    rank_agreement: float | None = None
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
    sort_mode: str = "legacy"
    ranking_diagnostics: dict[str, Any] | None = None

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


@dataclass(frozen=True, slots=True)
class _FileVersion:
    mtime_ns: int
    size: int


@dataclass(frozen=True, slots=True)
class _ManifestSnapshot:
    path: Path
    version: _FileVersion
    results: tuple[SearchResult, ...]
    raw_result_count: int
    source_label: str
    query_type: str
    status: str
    sort_mode: str
    ranking_diagnostics: dict[str, Any]
    created_timestamp: float


@dataclass(frozen=True, slots=True)
class _LatestDiscovery:
    root_version: _FileVersion
    directory_versions: tuple[tuple[Path, _FileVersion], ...]
    manifest_versions: tuple[tuple[Path, _FileVersion], ...]
    selected_path: Path


class ResultCatalog:
    """Read-only adapter between existing result files and the Python UI."""

    def __init__(
        self,
        config: CatalogConfig,
        *,
        manifest_cache_entries: int = DEFAULT_MANIFEST_CACHE_ENTRIES,
        manifest_cache_results: int = DEFAULT_MANIFEST_CACHE_RESULTS,
    ) -> None:
        if isinstance(manifest_cache_entries, bool) or manifest_cache_entries < 1:
            raise ValueError("manifest_cache_entries must be positive")
        if isinstance(manifest_cache_results, bool) or manifest_cache_results < 1:
            raise ValueError("manifest_cache_results must be positive")
        self.config = config
        self._libraries_by_id = config.libraries_by_id
        # Folder browsing may contain hundreds of thousands of images. Keep one
        # sorted path snapshot so changing gallery pages does not rescan the tree.
        self._folder_cache: dict[Path, tuple[Path, ...]] = {}
        self._manifest_cache_entries = manifest_cache_entries
        self._manifest_cache_results_limit = manifest_cache_results
        self._manifest_cache_results = 0
        self._manifest_cache: OrderedDict[Path, _ManifestSnapshot] = OrderedDict()
        self._cache_condition = threading.Condition(threading.RLock())
        self._manifest_loading: set[Path] = set()
        self._latest_loading = False
        self._latest_discovery: _LatestDiscovery | None = None
        self._cache_generation = 0

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
        snapshot = self._latest_snapshot()
        snapshot = self._refresh_snapshot_for_page(snapshot, page, page_size)
        if (
            snapshot.raw_result_count
            and len(snapshot.results) != snapshot.raw_result_count
        ):
            # The selected search became incomplete without changing its
            # manifest (for example, an exported image was deleted). Rediscover
            # so an older complete search can be shown instead.
            self.clear_manifest_cache(snapshot.path)
            snapshot = self._latest_snapshot()
        return self._page_from_snapshot(snapshot, page=page, page_size=page_size)

    def load_manifest(
        self,
        manifest_path: str | Path,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SearchResultPage:
        """Load one explicit result manifest below the configured results root."""

        page, page_size = _validate_pagination(page, page_size)
        resolved = self._resolve_manifest_path(manifest_path)
        snapshot = self._manifest_snapshot(resolved)
        snapshot = self._refresh_snapshot_for_page(snapshot, page, page_size)
        return self._page_from_snapshot(snapshot, page=page, page_size=page_size)

    def _resolve_manifest_path(self, manifest_path: str | Path) -> Path:
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
        return resolved

    def _snapshot_from_payload(
        self,
        manifest_path: Path,
        version: _FileVersion,
        payload: Mapping[str, Any],
    ) -> _ManifestSnapshot:
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

        return _ManifestSnapshot(
            path=manifest_path,
            version=version,
            results=tuple(results),
            raw_result_count=len(raw_results),
            source_label=manifest_path.parent.name,
            query_type=_optional_text(payload.get("query_type"), "unknown"),
            status=_optional_text(payload.get("status"), "ok"),
            sort_mode=_optional_text(payload.get("sort_mode"), "legacy"),
            ranking_diagnostics=(
                dict(payload["ranking_diagnostics"])
                if isinstance(payload.get("ranking_diagnostics"), Mapping)
                else {}
            ),
            created_timestamp=_created_timestamp(payload.get("created_at")),
        )

    def _page_from_snapshot(
        self,
        snapshot: _ManifestSnapshot,
        *,
        page: int,
        page_size: int,
    ) -> SearchResultPage:
        start = (page - 1) * page_size
        return _page_of_slice(
            list(snapshot.results[start : start + page_size]),
            total_items=len(snapshot.results),
            page=page,
            page_size=page_size,
            manifest_path=snapshot.path,
            source_label=snapshot.source_label,
            query_type=snapshot.query_type,
            status=snapshot.status,
            sort_mode=snapshot.sort_mode,
            ranking_diagnostics=snapshot.ranking_diagnostics,
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

    def clear_manifest_cache(self, manifest_path: str | Path | None = None) -> None:
        """Clear parsed manifests and invalidate latest-result discovery."""

        resolved = (
            None
            if manifest_path is None
            else Path(manifest_path).expanduser().resolve(strict=False)
        )
        with self._cache_condition:
            self._cache_generation += 1
            self._latest_discovery = None
            if resolved is None:
                self._manifest_cache.clear()
                self._manifest_cache_results = 0
            else:
                self._discard_manifest_locked(resolved)
            self._cache_condition.notify_all()

    def _manifest_snapshot(self, manifest_path: Path) -> _ManifestSnapshot:
        unstable_reads = 0
        while unstable_reads < 3:
            try:
                version = _file_version(manifest_path, require_file=True)
            except ResultCatalogError:
                with self._cache_condition:
                    self._discard_manifest_locked(manifest_path)
                raise

            with self._cache_condition:
                cached = self._manifest_cache.get(manifest_path)
                if cached is not None and cached.version == version:
                    self._manifest_cache.move_to_end(manifest_path)
                    return cached
                if cached is not None:
                    self._discard_manifest_locked(manifest_path)
                if manifest_path in self._manifest_loading:
                    self._cache_condition.wait()
                    continue
                self._manifest_loading.add(manifest_path)
                generation = self._cache_generation

            try:
                payload = _read_json_object(
                    manifest_path,
                    label="search result manifest",
                    limit=MAX_MANIFEST_BYTES,
                )
                snapshot = self._snapshot_from_payload(manifest_path, version, payload)
                after = _file_version(manifest_path, require_file=True)
            except Exception:
                with self._cache_condition:
                    self._manifest_loading.discard(manifest_path)
                    self._cache_condition.notify_all()
                raise

            stable = after == version
            with self._cache_condition:
                self._manifest_loading.discard(manifest_path)
                if stable and generation == self._cache_generation:
                    self._cache_manifest_locked(snapshot)
                self._cache_condition.notify_all()
            if stable:
                return snapshot
            unstable_reads += 1

        raise ResultCatalogError(
            f"Result manifest changed repeatedly while being read: {manifest_path}"
        )

    def _cache_manifest_locked(self, snapshot: _ManifestSnapshot) -> None:
        self._discard_manifest_locked(snapshot.path)
        result_count = len(snapshot.results)
        if result_count > self._manifest_cache_results_limit:
            return
        while self._manifest_cache and (
            len(self._manifest_cache) >= self._manifest_cache_entries
            or self._manifest_cache_results + result_count
            > self._manifest_cache_results_limit
        ):
            _path, evicted = self._manifest_cache.popitem(last=False)
            self._manifest_cache_results -= len(evicted.results)
        self._manifest_cache[snapshot.path] = snapshot
        self._manifest_cache_results += result_count

    def _discard_manifest_locked(self, manifest_path: Path) -> None:
        cached = self._manifest_cache.pop(manifest_path, None)
        if cached is not None:
            self._manifest_cache_results -= len(cached.results)

    def _refresh_snapshot_for_page(
        self,
        snapshot: _ManifestSnapshot,
        page: int,
        page_size: int,
    ) -> _ManifestSnapshot:
        start = (page - 1) * page_size
        page_items = snapshot.results[start : start + page_size]
        if all(_result_path_is_current(item) for item in page_items):
            return snapshot
        # Result files are external to results.json and can be removed without
        # changing the manifest fingerprint. Reparse once so original/copy
        # fallback and total counts remain correct.
        self.clear_manifest_cache(snapshot.path)
        return self._manifest_snapshot(snapshot.path)

    def _latest_snapshot(self) -> _ManifestSnapshot:
        while True:
            with self._cache_condition:
                discovery = self._latest_discovery
            if discovery is not None and self._latest_discovery_is_current(discovery):
                try:
                    return self._manifest_snapshot(discovery.selected_path)
                except ResultCatalogError:
                    self.clear_manifest_cache(discovery.selected_path)

            with self._cache_condition:
                if self._latest_loading:
                    self._cache_condition.wait()
                    continue
                self._latest_loading = True
                generation = self._cache_generation

            try:
                discovered, snapshot, cacheable = self._discover_latest()
                stable = self._latest_discovery_is_current(discovered)
            except Exception:
                with self._cache_condition:
                    self._latest_loading = False
                    self._cache_condition.notify_all()
                raise

            with self._cache_condition:
                self._latest_loading = False
                if stable and cacheable and generation == self._cache_generation:
                    self._latest_discovery = discovered
                self._cache_condition.notify_all()
            if stable:
                return snapshot

    def _discover_latest(
        self,
    ) -> tuple[_LatestDiscovery, _ManifestSnapshot, bool]:
        root = self.results_directory
        if not root.is_dir():
            raise ResultCatalogError(f"Results directory does not exist: {root}")

        root_version = _file_version(root, require_file=False)
        candidates: list[tuple[float, int, str, _ManifestSnapshot]] = []
        directory_versions: list[tuple[Path, _FileVersion]] = []
        manifest_versions: list[tuple[Path, _FileVersion]] = []
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
            try:
                directory_versions.append(
                    (directory, _file_version(directory, require_file=False))
                )
            except ResultCatalogError as exc:
                errors.append(str(exc))
                continue
            manifest_path = directory / "results.json"
            if not manifest_path.is_file() or _is_link_like(manifest_path):
                continue
            try:
                manifest_version = _file_version(manifest_path, require_file=True)
                manifest_versions.append((manifest_path, manifest_version))
                snapshot = self._manifest_snapshot(manifest_path)
                if (
                    snapshot.raw_result_count
                    and len(snapshot.results) != snapshot.raw_result_count
                ):
                    with self._cache_condition:
                        self._discard_manifest_locked(manifest_path)
                    raise ResultCatalogError(
                        f"Manifest references unavailable result images: "
                        f"{manifest_path}"
                    )
            except ResultCatalogError as exc:
                # A partially written/corrupt directory must not hide the last
                # complete search result from the gallery.
                errors.append(str(exc))
                continue
            candidates.append(
                (
                    snapshot.created_timestamp,
                    manifest_version.mtime_ns,
                    directory.name.casefold(),
                    snapshot,
                )
            )

        if not candidates:
            detail = f" Last error: {errors[-1]}" if errors else ""
            raise ResultCatalogError(
                f"No valid results.json was found below {root}.{detail}"
            )
        candidates.sort(key=lambda item: item[:3], reverse=True)
        selected = candidates[0][3]
        discovery = _LatestDiscovery(
            root_version=root_version,
            directory_versions=tuple(directory_versions),
            manifest_versions=tuple(manifest_versions),
            selected_path=selected.path,
        )
        # Do not negative-cache discovery while any candidate is incomplete or
        # malformed. An unchanged manifest can become displayable when its
        # exported image arrives moments later.
        return discovery, selected, not errors

    def _latest_discovery_is_current(self, discovery: _LatestDiscovery) -> bool:
        if _file_version_or_none(self.results_directory, require_file=False) != (
            discovery.root_version
        ):
            return False
        for path, version in discovery.directory_versions:
            if _file_version_or_none(path, require_file=False) != version:
                return False
        for path, version in discovery.manifest_versions:
            if _file_version_or_none(path, require_file=True) != version:
                return False
        return True

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
        raw_score = _finite_number(
            raw.get("raw_score", raw.get("distance", 0.0)),
            f"{label}.raw_score",
        )
        normalized_score = _finite_number(
            raw.get("normalized_score", raw.get("confidence", 0.0)),
            f"{label}.normalized_score",
        )
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
            raw_score=raw_score,
            normalized_score=normalized_score,
            confidence=confidence,
            ranking_confidence=_optional_finite_number(
                raw.get("ranking_confidence"), f"{label}.ranking_confidence"
            ),
            match_state=_optional_text(raw.get("match_state"), "weak"),
            rank_source=_optional_text(raw.get("rank_source"), "unknown"),
            image_confidence=_optional_finite_number(
                raw.get("image_confidence"), f"{label}.image_confidence"
            ),
            text_confidence=_optional_finite_number(
                raw.get("text_confidence"), f"{label}.text_confidence"
            ),
            metadata_confidence=_optional_finite_number(
                raw.get("metadata_confidence"), f"{label}.metadata_confidence"
            ),
            image_rank=_optional_positive_integer(
                raw.get("image_rank"), f"{label}.image_rank"
            ),
            text_rank=_optional_positive_integer(
                raw.get("text_rank"), f"{label}.text_rank"
            ),
            metadata_rank=_optional_positive_integer(
                raw.get("metadata_rank"), f"{label}.metadata_rank"
            ),
            rank_agreement=_optional_finite_number(
                raw.get("rank_agreement"), f"{label}.rank_agreement"
            ),
            library_id=(library.library_id if library else ""),
            doc_id=_optional_text(raw.get("doc_id"), ""),
        )

    def _result_library(
        self, raw: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> LibraryRecord | None:
        by_id = self._libraries_by_id
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


def _file_version(path: Path, *, require_file: bool) -> _FileVersion:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ResultCatalogError(f"Result path does not exist: {path}") from exc
    if require_file and not stat.S_ISREG(metadata.st_mode):
        raise ResultCatalogError(f"Result manifest is not a file: {path}")
    if not require_file and not stat.S_ISDIR(metadata.st_mode):
        raise ResultCatalogError(f"Results path is not a directory: {path}")
    return _FileVersion(metadata.st_mtime_ns, metadata.st_size)


def _file_version_or_none(path: Path, *, require_file: bool) -> _FileVersion | None:
    try:
        return _file_version(path, require_file=require_file)
    except ResultCatalogError:
        return None


def _result_path_is_current(result: SearchResult) -> bool:
    path = result.display_path
    try:
        return (
            path.is_file()
            and not _is_link_like(path)
            and path.resolve(strict=True) == path
        )
    except OSError:
        return False


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


def _optional_finite_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label)


def _optional_positive_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResultCatalogError(f"{label} must be a positive integer.")
    return value


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
    sort_mode: str = "legacy",
    ranking_diagnostics: dict[str, Any] | None = None,
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
        sort_mode=sort_mode,
        ranking_diagnostics=ranking_diagnostics,
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
