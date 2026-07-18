"""Pure search-form helpers shared by the tkinter UI and its tests.

Keeping validation and progress formatting independent from tkinter makes the
desktop behaviour testable on machines without a graphical display.  The
backend-facing validation remains authoritative in :mod:`search_service`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .search_service import (
    SearchMode,
    SearchRequest,
    SearchValidationError,
    normalize_search_request,
)

SEMANTIC_MODE_LABEL = "智能语义"
TAG_MODE_LABEL = "标签模糊"
SEARCH_MODE_LABELS = (SEMANTIC_MODE_LABEL, TAG_MODE_LABEL)

_MODE_BY_LABEL: dict[str, SearchMode] = {
    SEMANTIC_MODE_LABEL: "semantic",
    TAG_MODE_LABEL: "tags",
}


@dataclass(frozen=True, slots=True)
class SearchFormValues:
    """Snapshot of the search controls before a background task starts."""

    query_text: str
    query_image: str
    mode_label: str
    all_libraries: bool
    selected_library_ids: tuple[str, ...]
    top_k: int = 15
    candidate_k: int = 50


def build_search_request(
    values: SearchFormValues,
    *,
    enabled_library_ids: Sequence[str],
) -> SearchRequest:
    """Build and validate one immutable request from the visible controls."""

    if not isinstance(values, SearchFormValues):
        raise TypeError("values must be SearchFormValues")
    try:
        search_mode = _MODE_BY_LABEL[values.mode_label]
    except KeyError as exc:
        raise SearchValidationError("请选择有效的搜索方式。") from exc

    available = _unique_non_empty(enabled_library_ids, "enabled_library_ids")
    selected = _unique_non_empty(
        values.selected_library_ids,
        "selected_library_ids",
    )
    unknown = tuple(
        library_id for library_id in selected if library_id not in available
    )
    if unknown:
        raise SearchValidationError(
            f"选择的图库已经不存在或已停用：{', '.join(unknown)}"
        )
    if not values.all_libraries and not selected:
        raise SearchValidationError("请至少选择一个图库，或启用“全部图库”。")

    image_text = values.query_image.strip()
    image_path = Path(image_text).expanduser() if image_text else None
    request = SearchRequest(
        text=values.query_text,
        image_path=image_path,
        # An empty library list is the backend's explicit “all enabled
        # libraries” contract.  It also picks up a newly enabled library without
        # requiring the already-open window to rebuild a stale selection.
        library_ids=() if values.all_libraries else selected,
        top_k=values.top_k,
        candidate_k=values.candidate_k,
        search_mode=search_mode,
    )
    normalize_search_request(request)
    return request


def format_search_progress(job: Mapping[str, Any]) -> str:
    """Turn a backend job snapshot into a short, stable Chinese status line."""

    status = str(job.get("status") or "").strip().lower()
    status_text = {
        "queued": "搜索已排队",
        "running": "正在搜索",
        "pausing": "搜索正在暂停",
        "paused": "搜索已暂停",
        "needs_attention": "搜索完成，部分结果需要确认",
        "cancelling": "正在取消搜索",
        "cancelled": "搜索已取消",
        "succeeded": "搜索完成",
        "partial": "搜索完成，部分图库未成功",
        "failed": "搜索失败",
    }.get(status, "正在处理搜索任务")

    progress = job.get("progress")
    if not isinstance(progress, Mapping):
        return status_text

    detail = _progress_detail(progress)
    return f"{status_text} · {detail}" if detail else status_text


def outcome_error_text(
    status: str,
    error: Mapping[str, Any] | None,
) -> str:
    """Return a useful message without exposing an entire backend payload."""

    normalized_status = status.strip().lower()
    if normalized_status == "cancelled":
        return "搜索已取消，原有结果保持不变。"
    if error is None:
        return f"搜索未完成（状态：{normalized_status or '未知'}），原有结果保持不变。"
    message = error.get("message")
    code = error.get("code")
    message_text = str(message).strip() if message is not None else ""
    code_text = str(code).strip() if code is not None else ""
    detail = message_text or "后端没有提供错误说明。"
    if code_text:
        detail = f"{detail}（{code_text}）"
    return f"搜索失败：{detail} 原有结果保持不变。"


def _unique_non_empty(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SearchValidationError(f"{name} 必须是字符串序列。")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SearchValidationError(f"{name} 不能包含空值。")
        normalized = value.strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _progress_detail(progress: Mapping[str, Any]) -> str:
    for key in ("message", "stage", "library_name", "current_file"):
        value = progress.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    current = _progress_number(progress, "current", "done", "processed")
    total = _progress_number(progress, "total", "count", "candidate_count")
    if current is not None and total is not None and total > 0:
        return f"{current}/{total}"
    if current is not None:
        return f"已处理 {current}"
    return ""


def _progress_number(progress: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


__all__ = [
    "SEARCH_MODE_LABELS",
    "SEMANTIC_MODE_LABEL",
    "TAG_MODE_LABEL",
    "SearchFormValues",
    "build_search_request",
    "format_search_progress",
    "outcome_error_text",
]
