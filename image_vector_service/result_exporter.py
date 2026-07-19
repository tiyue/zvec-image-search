from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ConfigurationError, ServiceConfig
from .models import (
    ExportedHit,
    FileFailure,
    SearchHit,
    SearchReport,
    SearchSortMode,
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
RESULT_DIRECTORY_NAME_PATTERN = re.compile(
    r"^.+_(?P<timestamp>\d{8}_\d{6}_\d{3})(?:_(?P<suffix>\d{2}))?$",
    re.UNICODE,
)


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


def export_results(
    config: ServiceConfig,
    query_type: str,
    hits: list[SearchHit],
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
) -> SearchReport:
    config.results_path.mkdir(parents=True, exist_ok=True)
    directory_name = _result_directory_name(query_type, query)
    output_dir = _unique_result_directory(config.results_path, directory_name)
    output_dir.mkdir(parents=False, exist_ok=False)

    normalized_exclude = (
        os.path.normcase(str(Path(exclude_path).resolve())) if exclude_path else None
    )
    seen_hashes: set[str] = set()
    exported: list[ExportedHit] = []
    failures: list[FileFailure] = []

    for hit in hits:
        if len(exported) >= top_k:
            break
        root_id = str(hit.fields.get("root_id") or "")
        relative_path = str(hit.fields.get("relative_path") or "")
        try:
            source = resolve_source(hit)
        except (OSError, ValueError, ConfigurationError) as exc:
            failures.append(FileFailure(relative_path, str(exc)))
            continue
        normalized_source = os.path.normcase(str(source.resolve())) if source else ""
        if normalized_exclude and normalized_source == normalized_exclude:
            continue
        sha256 = str(hit.fields.get("sha256") or "")
        if sha256 and sha256 in seen_hashes:
            continue
        if not source.is_file():
            failures.append(FileFailure(relative_path, "source image no longer exists"))
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
        rank = len(exported) + 1
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
            f"{rank:03d}_{value_label}_{display_score:.6f}_"
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
            failures.append(FileFailure(str(source), str(exc)))
            continue

        seen_hashes.add(sha256)
        exported.append(
            ExportedHit(
                rank=rank,
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
                copied_file=destination.name,
                doc_id=hit.doc_id,
                tags=[str(tag) for tag in (hit.fields.get("tags") or [])],
                matched_tags=list(hit.matched_tags),
                sha256=sha256 or None,
                library_id=str(hit.fields.get("library_id") or ""),
                library_name=str(hit.fields.get("library_name") or ""),
                ranking_model_version=hit.ranking_model_version,
                ranking_score=hit.ranking_score,
                feature_schema_version=hit.feature_schema_version,
                ranking_fallback=hit.ranking_fallback,
                ranking_fallback_reason=hit.ranking_fallback_reason,
                calibrated_minimum_confidence=(hit.calibrated_minimum_confidence),
                calibration_version=hit.calibration_version,
                calibration_scope=hit.calibration_scope,
                calibration_fallback=hit.calibration_fallback,
                search_features=(
                    dict(hit.search_features)
                    if hit.search_features is not None
                    else None
                ),
            )
        )

    report = SearchReport(
        query_type=query_type,
        output_dir=str(output_dir),
        result_count=len(exported),
        results=exported,
        copy_failures=failures,
        request_ids=[item for item in request_ids if item],
        usage=usage,
        embedding_sources=embedding_sources or {},
        library_ids=library_ids or [],
        library_names=library_names or [],
        status=status,
        candidate_count=len(hits) if candidate_count is None else candidate_count,
        filtered_count=(
            max(0, len(hits) - len(exported))
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
    manifest = {
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
        "created_at": datetime.now().astimezone().isoformat(),
        **report.to_dict(),
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
    raw_results = manifest.get("results") if isinstance(manifest, dict) else None
    if not isinstance(raw_results, list):  # pragma: no cover - base check
        return "results manifest has no results array"

    declared, declaration_error = _cleanup_declared_names(raw_results)
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
) -> tuple[set[str], str | None]:
    declared = {RESULT_OWNERSHIP_MARKER, "results.json"}
    for index, result in enumerate(raw_results):
        if not isinstance(result, dict):
            return declared, f"results[{index}] must be an object"
        copied_file = result.get("copied_file")
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


def _delete_cleanup_candidate(path: Path, root: Path) -> None:
    """Delete only manifest-declared files, then remove the empty directory."""

    reason = _validate_cleanup_candidate(path, root)
    if reason is not None:
        raise OSError(reason)
    try:
        manifest = json.loads((path / "results.json").read_text(encoding="utf-8"))
        raw_results = manifest["results"]
        declared, declaration_error = _cleanup_declared_names(raw_results)
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
