from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ServiceConfig
from .models import ExportedHit, FileFailure, SearchHit, SearchReport


def _safe_name(value: str, limit: int = 60) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return (value or "query")[:limit]


def export_results(
    config: ServiceConfig,
    query_type: str,
    hits: list[SearchHit],
    top_k: int,
    query: dict[str, Any],
    request_ids: list[str],
    usage: list[dict[str, Any]],
    exclude_path: str | None = None,
) -> SearchReport:
    config.results_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    query_label = str(query.get("text") or Path(query.get("image") or "query").stem)
    directory_name = (
        f"{timestamp}_{query_type}_{_safe_name(query_label)}_{uuid.uuid4().hex[:6]}"
    )
    output_dir = config.results_path / directory_name
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
        source = Path(str(hit.fields.get("absolute_path") or ""))
        normalized_source = os.path.normcase(str(source.resolve())) if source else ""
        if normalized_exclude and normalized_source == normalized_exclude:
            continue
        sha256 = str(hit.fields.get("sha256") or "")
        if sha256 and sha256 in seen_hashes:
            continue
        if not source.is_file():
            failures.append(FileFailure(str(source), "source image no longer exists"))
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
                source_path=str(source),
                copied_path=str(destination),
                doc_id=hit.doc_id,
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
    (output_dir / "results.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
