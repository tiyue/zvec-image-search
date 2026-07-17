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
from .models import ExportedHit, FileFailure, SearchHit, SearchReport

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
            "confidence": (
                "same bounded indicator as normalized_score; not a calibrated "
                "probability"
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
        reason = _validate_owned_result_directory(path, root)
        if reason is not None:
            skipped.append({"path": str(path), "reason": reason})
            continue
        candidates.append(path)
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if not dry_run:
        for path in candidates:
            resolved = path.resolve()
            if resolved.parent != root:
                failures.append(
                    {"path": str(path), "error": "path escaped results directory"}
                )
                continue
            try:
                shutil.rmtree(resolved)
                deleted.append(str(resolved))
            except OSError as exc:
                failures.append({"path": str(resolved), "error": str(exc)})
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
