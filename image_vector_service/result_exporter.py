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


def _safe_name(value: str, limit: int = 60) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    value = (value or "搜索")[:limit].rstrip(" .")
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value


def _result_directory_name(query_type: str, query: dict[str, Any]) -> str:
    now = datetime.now(BEIJING_TIMEZONE)
    timestamp = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    if query_type == "text":
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

        display_score = hit.fused_score if hit.fused_score is not None else hit.distance
        rank = len(exported) + 1
        value_label = "rrf" if hit.fused_score is not None else "distance"
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
                root_id=root_id,
                relative_path=relative_path,
                copied_file=destination.name,
                doc_id=hit.doc_id,
                tags=[str(tag) for tag in (hit.fields.get("tags") or [])],
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
    )
    manifest = {
        "query_type": query_type,
        "query": query,
        "model": config.model,
        "mode": "independent",
        "dimension": config.dimension,
        "metric": config.metric,
        "distance_semantics": "lower_is_more_similar",
        "rrf_semantics": "higher_is_better_when_present",
        "created_at": datetime.now().astimezone().isoformat(),
        **report.to_dict(),
    }
    manifest_path = output_dir / "results.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_manifest.replace(manifest_path)
    return report


def clean_result_directories(
    results_path: Path, older_than_days: int, dry_run: bool
) -> dict[str, object]:
    if older_than_days < 0:
        raise ValueError("older_than_days cannot be negative.")
    results_path.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - older_than_days * 86400
    candidates = [
        path
        for path in results_path.iterdir()
        if path.is_dir() and path.stat().st_mtime < cutoff
    ]
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if not dry_run:
        root = results_path.resolve()
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
        "failures": failures,
    }
