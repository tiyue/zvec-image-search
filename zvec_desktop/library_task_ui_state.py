"""Display-independent form and task-center state for the Python desktop UI."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .library_tasks import (
    DEFAULT_AUTO_TAG_BUDGET_CNY,
    DEFAULT_AUTO_TAG_MAX_IMAGES,
    DEFAULT_AUTO_TAG_MODEL,
    IndexAndAutoTagRequest,
    IndexRequest,
    LibraryRequest,
    LibraryTaskValidationError,
    RootsRequest,
    StatsRequest,
    SyncRequest,
)

LibraryTaskAction = Literal["index", "sync", "index_and_auto_tag", "stats", "roots"]

_TAG_SEPARATOR = re.compile(r"[,，、;；\n\r]+")
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)


@dataclass(frozen=True, slots=True)
class LibraryTaskFormValues:
    """One immutable snapshot of the visible library-task controls."""

    library_id: str
    manual_tags: str = ""
    recursive: bool = True
    verify_hash: bool = False
    model: str = DEFAULT_AUTO_TAG_MODEL
    max_images: str = str(DEFAULT_AUTO_TAG_MAX_IMAGES)
    max_budget_cny: str = str(DEFAULT_AUTO_TAG_BUDGET_CNY)
    external_processing_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class TaskView:
    job_id: str
    title: str
    library: str
    status: str
    status_text: str
    progress_text: str
    failed_count: int
    message: str
    active: bool
    cancellable: bool


class TaskCenterModel:
    """Bounded task snapshots with terminal-state regression protection."""

    def __init__(self, capacity: int = 50) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._jobs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    @property
    def jobs(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._jobs[job_id]) for job_id in self._order)

    def get(self, job_id: str) -> dict[str, Any] | None:
        value = self._jobs.get(job_id)
        return copy.deepcopy(value) if value is not None else None

    def update(self, job: Mapping[str, Any]) -> bool:
        normalized = _normalized_job(job)
        job_id = normalized["id"]
        existing = self._jobs.get(job_id)
        if existing is not None:
            if task_is_terminal(existing) and not task_is_terminal(normalized):
                return False
            if existing == normalized:
                return False
        else:
            self._order.insert(0, job_id)
        self._jobs[job_id] = normalized
        self._trim()
        return True

    def remove(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        with suppress(ValueError):
            self._order.remove(job_id)

    def _trim(self) -> None:
        while len(self._order) > self._capacity:
            removable = next(
                (
                    job_id
                    for job_id in reversed(self._order)
                    if task_is_terminal(self._jobs[job_id])
                ),
                None,
            )
            if removable is None:
                return
            self.remove(removable)


def build_library_task_request(
    action: LibraryTaskAction,
    values: LibraryTaskFormValues,
) -> LibraryRequest:
    """Validate one task form before any backend request is sent."""

    if not isinstance(values, LibraryTaskFormValues):
        raise TypeError("values must be LibraryTaskFormValues")
    if action not in {"index", "sync", "index_and_auto_tag", "stats", "roots"}:
        raise LibraryTaskValidationError("请选择有效的图库操作。")

    library_id = values.library_id.strip()
    if not library_id:
        raise LibraryTaskValidationError("请先选择一个图库。", field_name="library_id")
    tags = parse_manual_tags(values.manual_tags)
    if action == "index":
        return IndexRequest(
            library_id,
            recursive=values.recursive,
            verify_hash=values.verify_hash,
            tags=tags or None,
        )
    if action == "sync":
        return SyncRequest(
            library_id,
            recursive=values.recursive,
            verify_hash=values.verify_hash,
        )
    if action == "stats":
        return StatsRequest(library_id)
    if action == "roots":
        return RootsRequest(library_id)

    model = values.model.strip() or None
    max_images = _positive_integer(values.max_images, "最大图片数")
    budget = _optional_positive_float(values.max_budget_cny, "预算")
    return IndexAndAutoTagRequest(
        library_id,
        recursive=values.recursive,
        verify_hash=values.verify_hash,
        tags=tags or None,
        model=model,
        max_images=max_images,
        max_budget_cny=budget,
        external_processing_confirmed=values.external_processing_confirmed,
    )


def parse_manual_tags(value: str) -> tuple[str, ...]:
    """Split comma/newline input while retaining first-seen display spelling."""

    if not isinstance(value, str):
        raise LibraryTaskValidationError("人工标签必须是文本。", field_name="tags")
    result: list[str] = []
    seen: set[str] = set()
    for item in _TAG_SEPARATOR.split(value):
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def project_task(job: Mapping[str, Any]) -> TaskView:
    normalized = _normalized_job(job)
    status = str(normalized.get("status") or "").strip().lower()
    params = normalized.get("params")
    progress = normalized.get("progress")
    library = ""
    if isinstance(progress, Mapping):
        name = progress.get("library_name")
        if isinstance(name, str) and name.strip():
            library = name.strip()
    if not library and isinstance(params, Mapping):
        library_id = params.get("library_id")
        if isinstance(library_id, str):
            library = library_id
    failed = task_failure_count(normalized)
    return TaskView(
        job_id=normalized["id"],
        title=task_title(str(normalized.get("command") or "")),
        library=library,
        status=status,
        status_text=task_status_text(status),
        progress_text=_progress_text(progress, failed),
        failed_count=failed,
        message=_task_message(normalized),
        active=status not in _TERMINAL_STATUSES,
        cancellable=(
            status not in _TERMINAL_STATUSES and status not in {"cancelling", "pausing"}
        ),
    )


def task_is_terminal(job: Mapping[str, Any]) -> bool:
    return str(job.get("status") or "").strip().lower() in _TERMINAL_STATUSES


def task_failure_count(job: Mapping[str, Any]) -> int:
    candidates = [_non_negative_integer(job.get("failure_count"))]
    progress = job.get("progress")
    if isinstance(progress, Mapping):
        candidates.append(_non_negative_integer(progress.get("failed")))
    result = job.get("result")
    if isinstance(result, Mapping):
        candidates.append(_non_negative_integer(result.get("failed")))
        for key in ("index", "auto_tag"):
            nested = result.get(key)
            if isinstance(nested, Mapping):
                candidates.append(_non_negative_integer(nested.get("failed")))
    return max(candidates)


def task_result_text(job: Mapping[str, Any]) -> str:
    result = job.get("result")
    error = job.get("error")
    payload: Any
    if isinstance(result, (Mapping, list)):
        payload = result
    elif isinstance(error, Mapping):
        payload = {"error": dict(error)}
    else:
        payload = {
            "status": str(job.get("status") or ""),
            "message": _task_message(job),
        }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def resolve_failure_directory(
    results_directory: str | Path,
    job: Mapping[str, Any],
) -> Path | None:
    """Resolve only the application's owned failed-images directory."""

    result = job.get("result")
    if not isinstance(result, Mapping):
        return None
    if _non_negative_integer(result.get("quarantined")) <= 0:
        return None
    raw_manifest = result.get("failure_manifest")
    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        return None
    try:
        root = (
            Path(results_directory).expanduser().resolve() / "failed-images"
        ).resolve(strict=True)
        manifest = Path(raw_manifest).expanduser().resolve(strict=True)
        manifest.relative_to(root)
    except (OSError, ValueError):
        return None
    if not root.is_dir() or not manifest.is_file():
        return None
    return root


def task_title(command: str) -> str:
    return {
        "index": "建立索引",
        "index_and_auto_tag": "索引并自动标注",
        "sync": "同步图库",
        "stats": "图库统计",
        "roots": "索引根目录",
        "search": "搜索",
    }.get(command, command or "后台任务")


def task_status_text(status: str) -> str:
    return {
        "queued": "排队中",
        "running": "运行中",
        "pausing": "正在暂停",
        "paused": "已暂停",
        "needs_attention": "需要处理",
        "cancelling": "正在取消",
        "succeeded": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, status or "准备中")


def _normalized_job(job: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(job, Mapping):
        raise TypeError("job must be a mapping")
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job must contain a non-empty id")
    result = copy.deepcopy(dict(job))
    result["id"] = job_id.strip()
    return result


def _progress_text(progress: Any, failed: int) -> str:
    values: list[str] = []
    if isinstance(progress, Mapping):
        current = _non_negative_integer(progress.get("current"))
        total = _non_negative_integer(progress.get("total"))
        if total > 0:
            values.append(f"{min(current, total)}/{total}")
    if failed:
        values.append(f"失败 {failed}")
    return " · ".join(values)


def _task_message(job: Mapping[str, Any]) -> str:
    progress = job.get("progress")
    if isinstance(progress, Mapping):
        message = progress.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    error = job.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


def _positive_integer(value: str, name: str) -> int:
    try:
        normalized = int(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise LibraryTaskValidationError(f"{name}必须是正整数。") from exc
    if normalized < 1:
        raise LibraryTaskValidationError(f"{name}必须是正整数。")
    return normalized


def _optional_positive_float(value: str, name: str) -> float | None:
    if not isinstance(value, str):
        raise LibraryTaskValidationError(f"{name}必须是数字。")
    text = value.strip()
    if not text:
        return None
    try:
        normalized = float(text)
    except ValueError as exc:
        raise LibraryTaskValidationError(f"{name}必须是正数。") from exc
    if normalized <= 0:
        raise LibraryTaskValidationError(f"{name}必须是正数。")
    return normalized


def _non_negative_integer(value: Any) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


__all__ = [
    "LibraryTaskAction",
    "LibraryTaskFormValues",
    "TaskCenterModel",
    "TaskView",
    "build_library_task_request",
    "parse_manual_tags",
    "project_task",
    "resolve_failure_directory",
    "task_failure_count",
    "task_is_terminal",
    "task_result_text",
    "task_status_text",
    "task_title",
]
