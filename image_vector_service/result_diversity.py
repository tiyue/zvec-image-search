from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .models import SearchHit

DEFAULT_MAX_RESULTS_PER_SERIES = 2
DEFAULT_DIVERSITY_PREFIX = 20


@dataclass(frozen=True)
class DiversityRanking:
    hits: list[SearchHit]
    enabled: bool
    max_per_series: int
    protected_results: int
    suppressed_count: int
    relaxed: bool

    def diagnostics(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "grouping": "library/root/top_level_folder",
            "max_per_series": self.max_per_series,
            "protected_results": self.protected_results,
            "suppressed_count": self.suppressed_count,
            "relaxed": self.relaxed,
        }


def diversify_search_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
    enabled: bool = True,
    max_per_series: int = DEFAULT_MAX_RESULTS_PER_SERIES,
    protected_results: int = DEFAULT_DIVERSITY_PREFIX,
) -> DiversityRanking:
    """Protect the leading results from being flooded by one image series.

    The cap is soft only when the candidate set has too few distinct series to
    satisfy the requested result count. Direct files under an indexed root are
    treated as independent items instead of one giant artificial series.
    """

    if top_k < 1:
        raise ValueError("top_k must be positive.")
    if max_per_series < 1:
        raise ValueError("max_per_series must be positive.")
    if protected_results < 1:
        raise ValueError("protected_results must be positive.")

    unique = _deduplicate(hits)
    if not enabled:
        return DiversityRanking(
            hits=_rerank(unique[:top_k]),
            enabled=False,
            max_per_series=max_per_series,
            protected_results=min(top_k, protected_results),
            suppressed_count=0,
            relaxed=False,
        )

    target_prefix = min(top_k, protected_results)
    selected_indices: list[int] = []
    deferred_indices: list[int] = []
    group_counts: dict[str, int] = {}
    for index, hit in enumerate(unique):
        if len(selected_indices) >= target_prefix:
            break
        group = series_group_key(hit)
        if group_counts.get(group, 0) >= max_per_series:
            deferred_indices.append(index)
            continue
        selected_indices.append(index)
        group_counts[group] = group_counts.get(group, 0) + 1

    relaxed = len(selected_indices) < target_prefix
    if relaxed:
        for index in deferred_indices:
            selected_indices.append(index)
            if len(selected_indices) >= target_prefix:
                break

    selected_set = set(selected_indices)
    for index in range(len(unique)):
        if len(selected_indices) >= top_k:
            break
        if index in selected_set:
            continue
        selected_indices.append(index)
        selected_set.add(index)

    selected = [unique[index] for index in selected_indices[:top_k]]
    return DiversityRanking(
        hits=_rerank(selected),
        enabled=True,
        max_per_series=max_per_series,
        protected_results=target_prefix,
        suppressed_count=len(deferred_indices),
        relaxed=relaxed,
    )


def series_group_key(hit: SearchHit) -> str:
    library_id = _comparison_text(str(hit.fields.get("library_id") or ""))
    root_id = _comparison_text(str(hit.fields.get("root_id") or ""))
    relative_path = str(hit.fields.get("relative_path") or "").replace("\\", "/")
    parts = tuple(
        part for part in PurePosixPath(relative_path).parts if part not in {"", "."}
    )
    if len(parts) < 2:
        return f"{library_id}\0{root_id}\0__file__\0{hit.doc_id}"
    top_level_folder = _comparison_text(parts[0])
    return f"{library_id}\0{root_id}\0{top_level_folder}"


def _deduplicate(hits: list[SearchHit]) -> list[SearchHit]:
    results: list[SearchHit] = []
    seen_documents: set[tuple[str, str]] = set()
    seen_hashes: set[str] = set()
    for hit in hits:
        document_key = (str(hit.fields.get("library_id") or ""), hit.doc_id)
        if document_key in seen_documents:
            continue
        sha256 = str(hit.fields.get("sha256") or "").casefold()
        if sha256 and sha256 in seen_hashes:
            continue
        seen_documents.add(document_key)
        if sha256:
            seen_hashes.add(sha256)
        results.append(hit)
    return results


def _rerank(hits: list[SearchHit]) -> list[SearchHit]:
    return [replace(hit, rank=rank) for rank, hit in enumerate(hits, start=1)]


def _comparison_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()
