from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Sized
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from .config import ConfigurationError, ServiceConfig
from .models import (
    ExportedHit,
    FileFailure,
    SearchHit,
    SearchReport,
    SearchSortMode,
)
from .search_result_store import (
    RESULT_STORE_FILENAME,
    SearchResultStoreError,
    iter_result_file_references,
    parse_result_store_reference,
    write_result_store,
)
from .search_result_store import (
    result_count as stored_result_count,
)

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
RESULT_OWNERSHIP_MARKER = ".zvec-search-result.json"
RESULT_OWNERSHIP_KIND = "zvec-search-result"
RESULT_OWNERSHIP_SCHEMA_VERSION = 1
RESULT_MANIFEST_SCHEMA_VERSION = 3
RESULT_MANIFEST_INLINE_LIMIT = 15
RESULT_FAILURE_PREVIEW_LIMIT = 200
RESULT_HASH_MEMORY_LIMIT = 4_096
RESULT_DIRECTORY_NAME_PATTERN = re.compile(
    r"^.+_(?P<timestamp>\d{8}_\d{6}_\d{3})(?:_(?P<suffix>\d{2}))?$",
    re.UNICODE,
)


def search_report_payload(
    report: SearchReport,
    *,
    result_limit: int | None = None,
) -> dict[str, Any]:
    """Serialize a report while optionally bounding its inline result preview."""

    if result_limit is not None and (
        isinstance(result_limit, bool)
        or not isinstance(result_limit, int)
        or result_limit < 0
    ):
        raise ValueError("result_limit must be a non-negative integer or None")
    selected = report.results if result_limit is None else report.results[:result_limit]
    selected_failures = (
        report.copy_failures
        if result_limit is None
        else report.copy_failures[:RESULT_FAILURE_PREVIEW_LIMIT]
    )
    failure_total = max(report.copy_failure_count, len(report.copy_failures))
    payload: dict[str, Any] = {
        "query_type": report.query_type,
        "output_dir": report.output_dir,
        "result_count": report.result_count,
        "results": [asdict(item) for item in selected],
        "copy_failures": [asdict(item) for item in selected_failures],
        "copy_failure_count": failure_total,
        "request_ids": list(report.request_ids),
        "usage": list(report.usage),
        "embedding_sources": dict(report.embedding_sources),
        "library_ids": list(report.library_ids),
        "library_names": list(report.library_names),
        "result_storage": report.result_storage,
        "status": report.status,
        "candidate_count": report.candidate_count,
        "filtered_count": report.filtered_count,
        "latency_ms": report.latency_ms,
        "ranking_mode": report.ranking_mode,
        "sort_mode": report.sort_mode,
        "show_low_confidence": report.show_low_confidence,
        "low_confidence_override": report.low_confidence_override,
    }
    if result_limit is not None or report.results_truncated:
        payload["results_truncated"] = (
            report.results_truncated or report.result_count > len(selected)
        )
        payload["results_inline_count"] = len(selected)
    if result_limit is not None or failure_total > len(selected_failures):
        payload["copy_failures_truncated"] = failure_total > len(selected_failures)
    if report.ranking_diagnostics is not None:
        payload["ranking_diagnostics"] = report.ranking_diagnostics
    if report.search_quality is not None:
        payload["search_quality"] = report.search_quality
    return payload


def _safe_name(value: str, limit: int = 60) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    value = (value or "搜索")[:limit].rstrip(" .")
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value


def _result_directory_name(query_type: str, query: dict[str, Any]) -> str:
    now = datetime.now(BEIJING_TIMEZONE)
    timestamp = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    if query_type in {"text", "tag"}:
        return f"{_safe_name(str(query.get('text') or '搜索'))}_{timestamp}"
    if query_type == "image_text":
        return f"{_safe_name(str(query.get('text') or '搜索'))}_图文搜索_{timestamp}"
    return f"图片搜索_{timestamp}"


def _unique_result_directory(results_path: Path, base_name: str) -> Path:
    candidate = results_path / base_name
    suffix = 2
    while candidate.exists():
        candidate = results_path / f"{base_name}_{suffix:02d}"
        suffix += 1
    return candidate


def _safe_logical_relative_path(value: str) -> bool:
    if not value:
        return False
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    return not (
        windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    )


class _BoundedHashDeduplicator:
    """Keep small searches in memory and spill large SHA sets to SQLite."""

    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / ".result-hashes.sqlite3.tmp"
        self._memory: set[str] = set()
        self._connection: sqlite3.Connection | None = None

    def remember(self, sha256: str) -> bool:
        if not sha256:
            return True
        if self._connection is None and len(self._memory) < RESULT_HASH_MEMORY_LIMIT:
            if sha256 in self._memory:
                return False
            self._memory.add(sha256)
            return True
        connection = self._ensure_connection()
        try:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO seen_hashes(sha256) VALUES (?)",
                (sha256,),
            )
        except sqlite3.Error as exc:
            raise SearchResultStoreError(
                f"Could not deduplicate search results: {exc}"
            ) from exc
        return cursor.rowcount == 1

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
        for candidate in (
            self._path,
            self._path.with_name(f"{self._path.name}-journal"),
            self._path.with_name(f"{self._path.name}-wal"),
            self._path.with_name(f"{self._path.name}-shm"),
        ):
            # An undeclared temporary file makes later cleanup fail closed;
            # it is safer to leave it than to broaden ownership rules.
            with suppress(OSError):
                candidate.unlink(missing_ok=True)

    def forget(self, sha256: str) -> None:
        if not sha256:
            return
        if self._connection is None:
            self._memory.discard(sha256)
            return
        try:
            self._connection.execute(
                "DELETE FROM seen_hashes WHERE sha256 = ?", (sha256,)
            )
        except sqlite3.Error as exc:
            raise SearchResultStoreError(
                f"Could not update search-result deduplication: {exc}"
            ) from exc

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self._path), timeout=30.0)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute(
                "CREATE TABLE seen_hashes (sha256 TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO seen_hashes(sha256) VALUES (?)",
                ((value,) for value in self._memory),
            )
            self._memory.clear()
            self._connection = connection
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise SearchResultStoreError(
                f"Could not initialize search-result deduplication: {exc}"
            ) from exc


def export_results(
    config: ServiceConfig,
    query_type: str,
    hits: Iterable[SearchHit],
    top_k: int,
    query: dict[str, Any],
    request_ids: list[str],
    usage: list[dict[str, Any]],
    resolve_source: Callable[[SearchHit], Path],
    embedding_sources: dict[str, str] | None = None,
    exclude_path: str | None = None,
    library_ids: list[str] | None = None,
    library_names: list[str] | None = None,
    mode: str = "independent",
    search_quality: dict[str, Any] | None = None,
    status: str = "ok",
    candidate_count: int | None = None,
    filtered_count: int | None = None,
    latency_ms: float = 0.0,
    ranking_mode: str = "distance",
    sort_mode: SearchSortMode = "confidence",
    ranking_diagnostics: dict[str, object] | None = None,
    show_low_confidence: bool = False,
    low_confidence_override: bool = False,
    copy_files: bool = True,
    report_result_limit: int | None = None,
    exclude_sha256: str | None = None,
) -> SearchReport:
    """Publish a search session and optionally copy its source images.

    ``copy_files=True`` preserves the historical CLI/export behavior. Desktop
    search can set it to ``False`` and retain only a bounded report preview.
    Source-only publication trusts the indexed logical identity and performs
    filesystem checks only when a page is displayed, avoiding one stat per hit.
    """

    if not isinstance(copy_files, bool):
        raise ValueError("copy_files must be a boolean")
    if report_result_limit is not None and (
        isinstance(report_result_limit, bool)
        or not isinstance(report_result_limit, int)
        or report_result_limit < 0
    ):
        raise ValueError("report_result_limit must be a non-negative integer or None")
    effective_report_limit = (
        RESULT_MANIFEST_INLINE_LIMIT
        if not copy_files and report_result_limit is None
        else report_result_limit
    )
    config.results_path.mkdir(parents=True, exist_ok=True)
    directory_name = _result_directory_name(query_type, query)
    output_dir = _unique_result_directory(config.results_path, directory_name)
    output_dir.mkdir(parents=False, exist_ok=False)

    normalized_exclude = (
        os.path.normcase(str(Path(exclude_path).resolve())) if exclude_path else None
    )
    deduplicator = _BoundedHashDeduplicator(output_dir)
    report_results: list[ExportedHit] = []
    failures: list[FileFailure] = []
    failure_count = 0
    exported_count = 0
    inspected_count = 0
    input_count_hint = len(hits) if isinstance(hits, Sized) else None
    configured_library_ids = library_ids or []
    configured_library_names = library_names or []

    def record_failure(path: str, error: str) -> None:
        nonlocal failure_count
        failure_count += 1
        if len(failures) < RESULT_FAILURE_PREVIEW_LIMIT:
            failures.append(FileFailure(path, error))

    def iter_exported_payloads() -> Iterable[dict[str, Any]]:
        nonlocal exported_count, inspected_count
        for hit in hits:
            if exported_count >= top_k:
                break
            inspected_count += 1
            root_id = str(hit.fields.get("root_id") or "").strip()
            relative_path = str(hit.fields.get("relative_path") or "").strip()
            library_id = str(hit.fields.get("library_id") or "").strip()
            if not library_id:
                if len(configured_library_ids) == 1:
                    library_id = configured_library_ids[0]
                elif config.library_id:
                    library_id = config.library_id
            library_name = str(hit.fields.get("library_name") or "").strip()
            if not library_name and len(configured_library_names) == 1:
                library_name = configured_library_names[0]
            if not copy_files and (
                not library_id
                or not root_id
                or not _safe_logical_relative_path(relative_path)
            ):
                record_failure(
                    relative_path,
                    "source-only result has incomplete library/root/path identity",
                )
                continue
            sha256 = str(hit.fields.get("sha256") or "")
            if exclude_sha256 and sha256 == exclude_sha256:
                continue
            if not deduplicator.remember(sha256):
                continue

            copied_file: str | None = None
            if copy_files:
                try:
                    source = resolve_source(hit)
                except (OSError, ValueError, ConfigurationError) as exc:
                    deduplicator.forget(sha256)
                    record_failure(relative_path, str(exc))
                    continue
                normalized_source = (
                    os.path.normcase(str(source.resolve())) if source else ""
                )
                if normalized_exclude and normalized_source == normalized_exclude:
                    deduplicator.forget(sha256)
                    continue
                if not source.is_file():
                    deduplicator.forget(sha256)
                    record_failure(relative_path, "source image no longer exists")
                    continue
                display_score = (
                    hit.fused_score
                    if hit.fused_score is not None
                    else float(hit.confidence or 0.0)
                    if hit.rank_source == "tag"
                    else float(hit.raw_score or 0.0)
                    if hit.rank_source == "fused"
                    else hit.distance
                )
                value_label = (
                    "rrf"
                    if hit.fused_score is not None
                    else "tag"
                    if hit.rank_source == "tag"
                    else "confidence"
                    if hit.rank_source == "fused"
                    else "distance"
                )
                destination_name = (
                    f"{exported_count + 1:03d}_{value_label}_{display_score:.6f}_"
                    f"{_safe_name(source.stem, 80)}{source.suffix.lower()}"
                )
                destination = output_dir / destination_name
                if destination.exists():
                    destination = output_dir / (
                        f"{destination.stem}_{hit.doc_id[:8]}{destination.suffix}"
                    )
                try:
                    shutil.copy2(source, destination)
                except OSError as exc:
                    deduplicator.forget(sha256)
                    record_failure(str(source), str(exc))
                    continue
                copied_file = destination.name

            exported_count += 1
            exported_hit = ExportedHit(
                rank=exported_count,
                distance=hit.distance,
                fused_score=hit.fused_score,
                raw_score=float(
                    hit.raw_score if hit.raw_score is not None else hit.distance
                ),
                normalized_score=float(hit.normalized_score or 0.0),
                confidence=float(hit.confidence or 0.0),
                ranking_confidence=hit.ranking_confidence,
                match_state=hit.match_state or "weak",
                rank_source=hit.rank_source or "text",
                image_confidence=hit.image_confidence,
                text_confidence=hit.text_confidence,
                metadata_confidence=hit.metadata_confidence,
                image_rank=hit.image_rank,
                text_rank=hit.text_rank,
                metadata_rank=hit.metadata_rank,
                rank_agreement=hit.rank_agreement,
                root_id=root_id,
                relative_path=relative_path,
                copied_file=copied_file,
                doc_id=hit.doc_id,
                tags=[str(tag) for tag in (hit.fields.get("tags") or [])],
                matched_tags=list(hit.matched_tags),
                sha256=sha256 or None,
                library_id=library_id,
                library_name=library_name,
                ranking_model_version=hit.ranking_model_version,
                ranking_score=hit.ranking_score,
                feature_schema_version=hit.feature_schema_version,
                ranking_fallback=hit.ranking_fallback,
                ranking_fallback_reason=hit.ranking_fallback_reason,
                calibrated_minimum_confidence=hit.calibrated_minimum_confidence,
                calibration_version=hit.calibration_version,
                calibration_scope=hit.calibration_scope,
                calibration_fallback=hit.calibration_fallback,
                search_features=(
                    dict(hit.search_features)
                    if hit.search_features is not None
                    else None
                ),
            )
            if (
                effective_report_limit is None
                or len(report_results) < effective_report_limit
            ):
                report_results.append(exported_hit)
            yield asdict(exported_hit)

    created_at = datetime.now().astimezone().isoformat()
    session_id = uuid4().hex
    try:
        try:
            result_store = write_result_store(
                output_dir / RESULT_STORE_FILENAME,
                session_id=session_id,
                created_at=created_at,
                results=iter_exported_payloads(),
            )
        except SearchResultStoreError as exc:
            raise ConfigurationError(
                f"Could not write paged search results: {exc}"
            ) from exc
    finally:
        deduplicator.close()

    authoritative_candidate_count = (
        candidate_count
        if candidate_count is not None
        else input_count_hint
        if input_count_hint is not None
        else inspected_count
    )
    report = SearchReport(
        query_type=query_type,
        output_dir=str(output_dir),
        result_count=result_store.result_count,
        results=report_results,
        results_truncated=result_store.result_count > len(report_results),
        copy_failures=failures,
        copy_failure_count=failure_count,
        request_ids=[item for item in request_ids if item],
        usage=usage,
        embedding_sources=embedding_sources or {},
        library_ids=configured_library_ids,
        library_names=configured_library_names,
        result_storage="copied" if copy_files else "source_only",
        status=status,
        candidate_count=authoritative_candidate_count,
        filtered_count=(
            max(0, authoritative_candidate_count - result_store.result_count)
            if filtered_count is None
            else filtered_count
        ),
        latency_ms=max(0.0, float(latency_ms)),
        ranking_mode=ranking_mode,
        sort_mode=sort_mode,
        ranking_diagnostics=ranking_diagnostics,
        show_low_confidence=show_low_confidence,
        low_confidence_override=low_confidence_override,
        search_quality=search_quality,
    )

    # Keep a small inline first-page preview for older tools while making the
    # immutable SQLite sidecar authoritative for counts and arbitrary pages.
    # Building the manifest summary explicitly avoids ``SearchReport.to_dict``
    # recursively duplicating every result in memory.
    report_summary = search_report_payload(
        report, result_limit=RESULT_MANIFEST_INLINE_LIMIT
    )
    manifest = {
        "manifest_schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
        "result_storage": report.result_storage,
        "query_type": query_type,
        "query": query,
        "model": config.model,
        "mode": mode,
        "dimension": config.dimension,
        "metric": config.metric,
        "distance_semantics": "lower_is_more_similar",
        "rrf_semantics": "higher_is_better_when_present",
        "score_semantics": {
            "raw_score": (
                "zvec cosine distance (1 - cosine similarity; lower is better), "
                "weighted RRF for image/text or accepted-tag/text fusion, or "
                "fused confidence for configured combined ranking; tag-only "
                "search records a deterministic fuzzy-tag distance"
            ),
            "normalized_score": (
                "bounded [0,1] similarity indicator; cosine uses 1 - raw/2 and "
                "weighted RRF uses raw*(rank_constant+1)"
            ),
            "confidence": ("bounded relevance confidence; not a probability"),
            "ranking_confidence": (
                "primary ordering value; falls back to confidence when absent"
            ),
            "ranking_score": (
                "versioned local ranker score; never an external model response"
            ),
            "calibrated_minimum_confidence": (
                "per-candidate calibrated abstention threshold with a 0.20 hard floor"
            ),
            "sort_order": (
                "confidence descending, then raw_score in the documented query "
                "direction; normalized_score is not used for ordering"
            ),
            "match_state": "high, possible, or weak according to query-type thresholds",
            "rank_source": "image, text, metadata, fused, or tag",
            "image_confidence": "image-channel confidence when available",
            "text_confidence": "text-channel confidence when available",
            "metadata_confidence": (
                "description-vector channel confidence when available"
            ),
            "image_rank": "one-based image-channel candidate rank when available",
            "text_rank": "one-based text-channel candidate rank when available",
            "metadata_rank": (
                "one-based description-vector candidate rank when available"
            ),
            "rank_agreement": (
                "bounded [0,1] agreement derived from image/text rank distance"
            ),
        },
        **({"search_quality": search_quality} if search_quality is not None else {}),
        "created_at": created_at,
        **report_summary,
        "result_store": result_store.to_dict(),
    }
    manifest_path = output_dir / "results.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_manifest.replace(manifest_path)
    marker_path = output_dir / RESULT_OWNERSHIP_MARKER
    temporary_marker = marker_path.with_suffix(marker_path.suffix + ".tmp")
    temporary_marker.write_text(
        json.dumps(
            {
                "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
                "kind": RESULT_OWNERSHIP_KIND,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_marker.replace(marker_path)
    return report


def _is_reparse_directory(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _validate_owned_result_directory(path: Path, root: Path) -> str | None:
    if _is_reparse_directory(path):
        return "symbolic links and junctions are never deleted"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return f"could not resolve path: {exc}"
    if resolved.parent != root:
        return "path escaped results directory"

    marker_path = path / RESULT_OWNERSHIP_MARKER
    manifest_path = path / "results.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"missing or invalid ownership marker: {exc}"
    if not isinstance(marker, dict) or marker != {
        "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
        "kind": RESULT_OWNERSHIP_KIND,
    }:
        return "ownership marker contract mismatch"

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"missing or invalid results manifest: {exc}"
    if not isinstance(manifest, dict):
        return "results manifest must be an object"
    output_dir = manifest.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        return "results manifest has no output_dir"
    try:
        manifest_directory = Path(output_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        return f"results manifest output_dir is invalid: {exc}"
    if manifest_directory != resolved:
        return "results manifest output_dir does not match the directory"
    if not isinstance(manifest.get("results"), list):
        return "results manifest has no results array"
    return None


def clean_result_directories(
    results_path: Path, older_than_days: int, dry_run: bool
) -> dict[str, object]:
    if older_than_days < 0:
        raise ValueError("older_than_days cannot be negative.")
    results_path.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - older_than_days * 86400
    root = results_path.resolve()
    candidates: list[Path] = []
    skipped: list[dict[str, str]] = []
    for path in results_path.iterdir():
        try:
            if not path.is_dir() or path.stat().st_mtime >= cutoff:
                continue
        except OSError as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
            continue
        reason = _validate_cleanup_candidate(path, root)
        if reason is not None:
            skipped.append({"path": str(path), "reason": reason})
            continue
        candidates.append(path)
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if not dry_run:
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                if resolved.parent != root:
                    raise OSError("path escaped results directory")
                reason = _validate_cleanup_candidate(resolved, root)
                if reason is not None:
                    skipped.append({"path": str(resolved), "reason": reason})
                    continue
                _delete_cleanup_candidate(resolved, root)
                deleted.append(str(resolved))
            except OSError as exc:
                failures.append({"path": str(path), "error": str(exc)})
    return {
        "results_path": str(results_path),
        "older_than_days": older_than_days,
        "dry_run": dry_run,
        "matched": len(candidates),
        "deleted": len(deleted),
        "deleted_paths": deleted,
        "skipped": len(skipped),
        "skipped_paths": skipped,
        "failures": failures,
    }


def cleanup_search_results(
    results_path: Path,
    *,
    keep_latest: int = 3,
    dry_run: bool = False,
) -> dict[str, object]:
    """Delete only conclusively owned historical search result directories.

    A directory must be a direct child of ``results_path``, use the generated
    timestamp naming contract, and contain both a valid ownership marker and a
    self-consistent results manifest. In-progress searches do not yet have both
    files and are therefore skipped automatically.
    """

    if isinstance(keep_latest, bool) or not isinstance(keep_latest, int):
        raise ValueError("keep_latest must be an integer.")
    if not 1 <= keep_latest <= 100:
        raise ValueError("keep_latest must be between 1 and 100.")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean.")

    results_path.mkdir(parents=True, exist_ok=True)
    root = results_path.resolve()
    owned: list[tuple[float, Path]] = []
    skipped: list[dict[str, str]] = []
    try:
        children = list(results_path.iterdir())
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot inspect the results directory: {results_path}"
        ) from exc

    for path in children:
        try:
            if not path.is_dir():
                continue
            name_match = RESULT_DIRECTORY_NAME_PATTERN.fullmatch(path.name)
            if name_match is None:
                skipped.append(
                    {
                        "path": str(path),
                        "reason": "directory name is not a generated search result",
                    }
                )
                continue
            reason = _validate_cleanup_candidate(path, root)
            if reason is not None:
                skipped.append({"path": str(path), "reason": reason})
                continue
            try:
                sort_key = _result_directory_sort_key(name_match)
            except ValueError:
                skipped.append(
                    {
                        "path": str(path),
                        "reason": "directory timestamp is invalid",
                    }
                )
                continue
            owned.append((sort_key, path))
        except OSError as exc:
            skipped.append({"path": str(path), "reason": str(exc)})

    owned.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    retained = [path for _mtime, path in owned[:keep_latest]]
    candidates = [path for _mtime, path in owned[keep_latest:]]
    for path in retained:
        skipped.append(
            {
                "path": str(path),
                "reason": f"retained as one of the latest {keep_latest} searches",
            }
        )

    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if not dry_run:
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                if resolved.parent != root:
                    raise OSError("path escaped results directory")
                if RESULT_DIRECTORY_NAME_PATTERN.fullmatch(resolved.name) is None:
                    skipped.append(
                        {
                            "path": str(resolved),
                            "reason": "directory name changed before deletion",
                        }
                    )
                    continue
                _delete_cleanup_candidate(resolved, root)
                deleted.append(str(resolved))
            except OSError as exc:
                failures.append({"path": str(path), "error": str(exc)})

    return {
        "results_path": str(results_path),
        "keep_latest": keep_latest,
        "dry_run": dry_run,
        "owned": len(owned),
        "matched": len(candidates),
        "deleted": len(deleted),
        "deleted_paths": deleted,
        "would_delete_paths": [str(path) for path in candidates] if dry_run else [],
        "retained": len(retained),
        "retained_paths": [str(path) for path in retained],
        "skipped": len(skipped),
        "skipped_paths": skipped,
        "failed": len(failures),
        "failures": failures,
    }


def _result_directory_sort_key(match: re.Match[str]) -> float:
    generated = datetime.strptime(match.group("timestamp"), "%Y%m%d_%H%M%S_%f").replace(
        tzinfo=BEIJING_TIMEZONE
    )
    suffix = int(match.group("suffix") or 0)
    return generated.timestamp() + suffix / 1_000_000


def _validate_cleanup_candidate(path: Path, root: Path) -> str | None:
    reason = _validate_owned_result_directory(path, root)
    if reason is not None:
        return reason
    manifest_path = path / "results.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - base check
        return f"missing or invalid results manifest: {exc}"
    if not isinstance(manifest, dict):  # pragma: no cover - base check
        return "results manifest must be an object"
    declared, declaration_error = _cleanup_declared_names_from_manifest(path, manifest)
    if declaration_error is not None:
        return declaration_error

    try:
        children = list(path.iterdir())
    except OSError as exc:
        return f"could not inspect directory contents: {exc}"
    actual = {child.name for child in children}
    undeclared = sorted(actual - declared)
    if undeclared:
        return "directory contains undeclared content: " + ", ".join(undeclared[:5])
    missing = sorted(declared - actual)
    if missing:
        return "directory is missing declared content: " + ", ".join(missing[:5])
    for child in children:
        if _is_reparse_directory(child):
            return f"declared content is a link or junction: {child.name}"
        try:
            if not child.is_file():
                return f"declared content is not a regular file: {child.name}"
        except OSError as exc:
            return f"could not inspect declared content {child.name}: {exc}"
    return None


def _cleanup_declared_names(
    raw_results: list[Any],
    *,
    source_only: bool = False,
) -> tuple[set[str], str | None]:
    declared = {RESULT_OWNERSHIP_MARKER, "results.json"}
    for index, result in enumerate(raw_results):
        if not isinstance(result, dict):
            return declared, f"results[{index}] must be an object"
        copied_file = result.get("copied_file")
        if source_only and copied_file is None:
            continue
        if (
            not isinstance(copied_file, str)
            or not copied_file.strip()
            or copied_file != Path(copied_file).name
            or "/" in copied_file
            or "\\" in copied_file
        ):
            return (
                declared,
                f"results[{index}].copied_file is not a direct file name",
            )
        if copied_file in declared:
            return declared, f"results[{index}].copied_file is duplicated or reserved"
        declared.add(copied_file)
    return declared, None


def _cleanup_declared_names_from_manifest(
    result_directory: Path,
    manifest: dict[str, Any],
) -> tuple[set[str], str | None]:
    storage_mode, storage_error = _cleanup_result_storage_mode(manifest)
    if storage_error is not None:
        return {RESULT_OWNERSHIP_MARKER, "results.json"}, storage_error
    source_only = storage_mode == "source_only"
    raw_store = manifest.get("result_store")
    if raw_store is None:
        raw_results = manifest.get("results")
        if not isinstance(raw_results, list):
            return (
                {RESULT_OWNERSHIP_MARKER, "results.json"},
                "results manifest has no results array",
            )
        return _cleanup_declared_names(raw_results, source_only=source_only)

    declared = {
        RESULT_OWNERSHIP_MARKER,
        "results.json",
        RESULT_STORE_FILENAME,
    }
    try:
        reference = parse_result_store_reference(raw_store)
        store_path = result_directory / reference.path
        actual_count = stored_result_count(store_path, reference.session_id)
        if actual_count != reference.result_count:
            return declared, "result store count does not match its manifest reference"
        manifest_count = manifest.get("result_count")
        if manifest_count != actual_count:
            return declared, "result_count does not match the result store"
        seen_count = 0
        for index, copied_file in enumerate(
            iter_result_file_references(store_path, session_id=reference.session_id)
        ):
            seen_count = index + 1
            if copied_file is None:
                if not source_only:
                    return (
                        declared,
                        f"stored result {index + 1} has no copied filename",
                    )
                continue
            if (
                not copied_file.strip()
                or copied_file != Path(copied_file).name
                or "/" in copied_file
                or "\\" in copied_file
            ):
                return declared, f"stored result {index + 1} has an unsafe filename"
            if copied_file in declared:
                return (
                    declared,
                    f"stored result {index + 1} filename is duplicated or reserved",
                )
            declared.add(copied_file)
        if seen_count != actual_count:
            return declared, "result store row count does not match its session count"
    except SearchResultStoreError as exc:
        return declared, f"invalid result store: {exc}"
    return declared, None


def _cleanup_result_storage_mode(
    manifest: dict[str, Any],
) -> tuple[str, str | None]:
    raw_version = manifest.get("manifest_schema_version")
    if raw_version is None or (
        isinstance(raw_version, int)
        and not isinstance(raw_version, bool)
        and raw_version in {1, 2}
    ):
        return "copied", None
    if raw_version != RESULT_MANIFEST_SCHEMA_VERSION:
        return "", f"unsupported result manifest schema_version: {raw_version!r}"
    mode = manifest.get("result_storage")
    if mode not in {"copied", "source_only"}:
        return "", "result manifest schema 3 has invalid result_storage"
    return str(mode), None


def _delete_cleanup_candidate(path: Path, root: Path) -> None:
    """Delete only manifest-declared files, then remove the empty directory."""

    reason = _validate_cleanup_candidate(path, root)
    if reason is not None:
        raise OSError(reason)
    try:
        manifest = json.loads((path / "results.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise OSError("results manifest must be an object")
        declared, declaration_error = _cleanup_declared_names_from_manifest(
            path, manifest
        )
        if declaration_error is not None:
            raise OSError(declaration_error)
        # Result copies go first. Ownership evidence is removed last, so a partial
        # filesystem failure remains recognisable and is never mistaken for a
        # successfully deleted directory.
        ordered = sorted(declared - {RESULT_OWNERSHIP_MARKER, "results.json"})
        ordered.extend(("results.json", RESULT_OWNERSHIP_MARKER))
        for name in ordered:
            candidate = path / name
            if _is_reparse_directory(candidate) or not candidate.is_file():
                raise OSError(f"declared content changed before deletion: {name}")
            candidate.unlink()
        # If new user content appears after validation, rmdir fails rather than
        # recursively deleting that unowned content.
        path.rmdir()
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise OSError(f"results manifest changed before deletion: {exc}") from exc
