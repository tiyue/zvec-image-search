"""Validated search workflow for the pure-Python desktop client.

The persistent backend only accepts query images from its private staging
directory.  This module owns that staging copy, builds the exact backend job
payload, and keeps polling/cancellation logic out of Tk event handlers.
"""

from __future__ import annotations

import math
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .backend_api import JsonObject

SearchQueryType = Literal["text", "image", "combined", "tag"]
SearchMode = Literal["semantic", "tags"]
SearchSortMode = Literal["relevance", "confidence", "diverse", "legacy"]
TagMode = Literal["all", "any"]

_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)
_SUCCESS_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})
_MAX_QUERY_IMAGE_BYTES = 256 * 1024 * 1024
_MAX_TEXT_CHARACTERS = 4096
_MAX_TAGS = 100
_MAX_TAG_CHARACTERS = 256
_MAX_LIBRARIES = 100
_MAX_TECHNICAL_COUNT = 2**31 - 1


class SearchServiceError(RuntimeError):
    """Base class for desktop-side search workflow failures."""


class SearchValidationError(SearchServiceError, ValueError):
    """The search request cannot be represented by the backend contract."""


class SearchProtocolError(SearchServiceError):
    """A nominal backend job response is missing required search fields."""


class SearchWaitTimeout(SearchServiceError):
    """A submitted search did not finish inside the caller's wait window."""

    def __init__(self, job_id: str, timeout_seconds: float) -> None:
        self.job_id = job_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Search job {job_id} did not finish within {timeout_seconds:g} seconds."
        )


class SearchClient(Protocol):
    """Narrow backend client contract used by :class:`SearchService`."""

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> JsonObject: ...

    def get_job(self, job_id: str) -> JsonObject: ...

    def cancel_job(self, job_id: str) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """User-facing search options before query-image staging."""

    text: str | None = None
    image_path: str | Path | None = None
    library_ids: tuple[str, ...] = ()
    top_k: int = 15
    candidate_k: int = 50
    tags: tuple[str, ...] = ()
    tag_mode: TagMode = "all"
    image_weight: float = 0.5
    text_weight: float = 0.5
    include_self: bool = False
    show_low_confidence: bool = False
    diversify_results: bool = True
    search_mode: SearchMode = "semantic"
    sort_mode: SearchSortMode = "confidence"


@dataclass(frozen=True, slots=True)
class NormalizedSearchRequest:
    """Canonical values ready to convert into a backend job payload."""

    query_type: SearchQueryType
    text: str | None
    image_path: Path | None
    library_ids: tuple[str, ...]
    top_k: int
    candidate_k: int
    tags: tuple[str, ...]
    tag_mode: TagMode
    image_weight: float
    text_weight: float
    include_self: bool
    show_low_confidence: bool
    diversify_results: bool
    search_mode: SearchMode
    sort_mode: SearchSortMode


@dataclass(frozen=True, slots=True)
class SubmittedSearch:
    """A backend search plus the private image copy it may still need."""

    job_id: str
    query_type: SearchQueryType
    staged_image: Path | None
    submitted_job: JsonObject


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Terminal backend state and the exported manifest, when successful."""

    submission: SubmittedSearch
    status: str
    job: JsonObject
    result: JsonObject | None
    error: JsonObject | None
    output_directory: Path | None
    manifest_path: Path | None

    @property
    def successful(self) -> bool:
        return self.status in _SUCCESS_STATUSES


ProgressCallback = Callable[[JsonObject], None]


class SearchService:
    """Submit and monitor backend searches without touching the UI thread."""

    def __init__(
        self,
        client: SearchClient,
        query_root: str | Path,
        *,
        max_query_image_bytes: int = _MAX_QUERY_IMAGE_BYTES,
    ) -> None:
        if isinstance(max_query_image_bytes, bool) or max_query_image_bytes < 1:
            raise ValueError("max_query_image_bytes must be a positive integer")
        self._client = client
        self._query_root = Path(query_root).expanduser().resolve()
        self._max_query_image_bytes = max_query_image_bytes

    @property
    def query_root(self) -> Path:
        return self._query_root

    def submit(self, request: SearchRequest) -> SubmittedSearch:
        """Validate, stage an optional image, and submit one search job."""

        normalized = normalize_search_request(request)
        staged_image: Path | None = None
        try:
            if normalized.image_path is not None:
                staged_image = self._stage_query_image(normalized.image_path)
            params = _job_params(normalized, staged_image)
            job = self._client.submit_job("search", params)
            job_id = _job_id(job)
        except Exception:
            _unlink_if_present(staged_image)
            raise
        return SubmittedSearch(
            job_id=job_id,
            query_type=normalized.query_type,
            staged_image=staged_image,
            submitted_job=job,
        )

    def wait(
        self,
        submission: SubmittedSearch,
        *,
        timeout: float = 300.0,
        poll_interval: float = 0.25,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SearchOutcome:
        """Wait for a terminal state from a background worker thread.

        A local cancellation request is forwarded to the backend, but polling
        continues until the backend confirms a terminal state.  This prevents
        deleting the staged query image while a running job may still read it.
        """

        timeout_value = _positive_finite(timeout, "timeout")
        interval = _positive_finite(poll_interval, "poll_interval")
        deadline = time.monotonic() + timeout_value
        cancellation_sent = False
        last_signature: tuple[str, str] | None = None

        while True:
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and not cancellation_sent
            ):
                cancelled_job = self._client.cancel_job(submission.job_id)
                cancellation_sent = True
                if on_progress is not None:
                    on_progress(cancelled_job)

            job = self._client.get_job(submission.job_id)
            status = _job_status(job)
            signature = (status, _progress_signature(job))
            if on_progress is not None and signature != last_signature:
                on_progress(job)
                last_signature = signature
            if status in _TERMINAL_STATUSES:
                try:
                    return _outcome(submission, job, status)
                finally:
                    _unlink_if_present(submission.staged_image)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The backend may still be using the image.  Deliberately retain
                # it so a later wait/cancel call can finish safely.
                raise SearchWaitTimeout(submission.job_id, timeout_value)
            time.sleep(min(interval, remaining))

    def cancel(self, submission: SubmittedSearch) -> JsonObject:
        """Request backend cancellation without killing its worker process."""

        job = self._client.cancel_job(submission.job_id)
        status = _job_status(job)
        if status in _TERMINAL_STATUSES:
            _unlink_if_present(submission.staged_image)
        return job

    def discard_staged_image(self, submission: SubmittedSearch) -> None:
        """Remove a known-unused staging file after external terminal proof."""

        _unlink_if_present(submission.staged_image)

    def _stage_query_image(self, source: Path) -> Path:
        try:
            resolved_source = source.expanduser().resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise SearchValidationError("查询图片不存在或无法读取。") from exc
        if not resolved_source.is_file():
            raise SearchValidationError("查询图片必须是文件。")
        try:
            size = resolved_source.stat().st_size
        except OSError as exc:
            raise SearchValidationError("无法读取查询图片大小。") from exc
        if size <= 0:
            raise SearchValidationError("查询图片不能为空文件。")
        if size > self._max_query_image_bytes:
            raise SearchValidationError(
                f"查询图片不能超过 {self._max_query_image_bytes // (1024 * 1024)} MiB。"
            )

        try:
            self._query_root.mkdir(parents=True, exist_ok=True)
            root = self._query_root.resolve(strict=True)
        except OSError as exc:
            raise SearchServiceError("无法创建后端查询图片暂存目录。") from exc
        if not root.is_dir():
            raise SearchServiceError("后端查询图片暂存路径不是文件夹。")

        suffix = _safe_suffix(resolved_source.suffix)
        target = root / f"query-{uuid.uuid4().hex}{suffix}"
        try:
            with (
                resolved_source.open("rb") as source_stream,
                target.open("xb") as target_stream,
            ):
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            shutil.copystat(resolved_source, target, follow_symlinks=True)
            copied_size = target.stat().st_size
        except OSError as exc:
            _unlink_if_present(target)
            raise SearchServiceError("复制查询图片到后端暂存目录失败。") from exc
        if copied_size != size:
            _unlink_if_present(target)
            raise SearchServiceError("查询图片复制不完整，请重试。")
        return target.resolve(strict=True)


def normalize_search_request(request: SearchRequest) -> NormalizedSearchRequest:
    """Validate and canonicalise one public search request."""

    if not isinstance(request, SearchRequest):
        raise TypeError("request must be a SearchRequest")
    text = _optional_trimmed(request.text, "搜索文字", _MAX_TEXT_CHARACTERS)
    image_path = Path(request.image_path) if request.image_path is not None else None
    search_mode = request.search_mode
    if search_mode not in {"semantic", "tags"}:
        raise SearchValidationError("搜索模式必须是 semantic 或 tags。")
    if search_mode == "tags":
        if text is None or image_path is not None:
            raise SearchValidationError("标签搜索必须提供文字，并且不能同时提供图片。")
        query_type: SearchQueryType = "tag"
    elif text is not None and image_path is not None:
        query_type = "combined"
    elif text is not None:
        query_type = "text"
    elif image_path is not None:
        query_type = "image"
    else:
        raise SearchValidationError("请输入搜索文字、选择查询图片，或同时提供两者。")

    top_k = _positive_integer(request.top_k, "top_k")
    candidate_k = max(
        top_k,
        _positive_integer(request.candidate_k, "candidate_k"),
    )
    library_ids = _unique_values(
        request.library_ids,
        "library_ids",
        maximum_items=_MAX_LIBRARIES,
        maximum_characters=128,
    )
    tags = _unique_values(
        request.tags,
        "tags",
        maximum_items=_MAX_TAGS,
        maximum_characters=_MAX_TAG_CHARACTERS,
    )
    if request.tag_mode not in {"all", "any"}:
        raise SearchValidationError("tag_mode 必须是 all 或 any。")
    if request.sort_mode not in {"relevance", "confidence", "diverse", "legacy"}:
        raise SearchValidationError(
            "sort_mode must be relevance, confidence, diverse, or legacy."
        )
    image_weight = _non_negative_finite(request.image_weight, "image_weight")
    text_weight = _non_negative_finite(request.text_weight, "text_weight")
    if query_type == "combined":
        total_weight = image_weight + text_weight
        if total_weight <= 0:
            raise SearchValidationError("图文搜索的图片权重和文字权重不能同时为零。")
        image_weight /= total_weight
        text_weight /= total_weight

    for name, value in (
        ("include_self", request.include_self),
        ("show_low_confidence", request.show_low_confidence),
        ("diversify_results", request.diversify_results),
    ):
        if not isinstance(value, bool):
            raise SearchValidationError(f"{name} 必须是布尔值。")

    return NormalizedSearchRequest(
        query_type=query_type,
        text=text,
        image_path=image_path,
        library_ids=library_ids,
        top_k=top_k,
        candidate_k=candidate_k,
        tags=tags,
        tag_mode=request.tag_mode,
        image_weight=image_weight,
        text_weight=text_weight,
        include_self=request.include_self,
        show_low_confidence=request.show_low_confidence,
        diversify_results=request.diversify_results,
        search_mode=search_mode,
        sort_mode=request.sort_mode,
    )


def _job_params(
    request: NormalizedSearchRequest, staged_image: Path | None
) -> JsonObject:
    params: JsonObject = {
        "top_k": request.top_k,
        "candidate_k": request.candidate_k,
        "tag_mode": request.tag_mode,
        "image_weight": request.image_weight,
        "text_weight": request.text_weight,
        "include_self": request.include_self,
        "show_low_confidence": request.show_low_confidence,
        "diversify_results": request.diversify_results,
        "search_mode": request.search_mode,
        "sort_mode": request.sort_mode,
    }
    if request.text is not None:
        params["text"] = request.text
    if staged_image is not None:
        params["image"] = str(staged_image)
    if request.library_ids:
        params["library_ids"] = list(request.library_ids)
    if request.tags:
        params["tags"] = list(request.tags)
    return params


def _outcome(
    submission: SubmittedSearch, job: JsonObject, status: str
) -> SearchOutcome:
    raw_result = job.get("result")
    result = raw_result if isinstance(raw_result, dict) else None
    raw_error = job.get("error")
    error = raw_error if isinstance(raw_error, dict) else None
    if status in _SUCCESS_STATUSES and result is None:
        raise SearchProtocolError(
            f"搜索任务 {submission.job_id} 已完成，但没有返回结果对象。"
        )

    output_directory: Path | None = None
    manifest_path: Path | None = None
    if result is not None and result.get("output_dir") is not None:
        raw_output = result["output_dir"]
        if not isinstance(raw_output, str) or not raw_output.strip():
            raise SearchProtocolError("搜索结果中的 output_dir 无效。")
        candidate = Path(raw_output).expanduser()
        if not candidate.is_absolute():
            raise SearchProtocolError("搜索结果中的 output_dir 必须是绝对路径。")
        output_directory = candidate.resolve()
        candidate_manifest = output_directory / "results.json"
        if candidate_manifest.is_file():
            manifest_path = candidate_manifest
        elif status in _SUCCESS_STATUSES:
            raise SearchProtocolError("搜索任务已完成，但 results.json 不存在。")

    return SearchOutcome(
        submission=submission,
        status=status,
        job=job,
        result=result,
        error=error,
        output_directory=output_directory,
        manifest_path=manifest_path,
    )


def _job_id(job: Mapping[str, Any]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value.strip():
        raise SearchProtocolError("后端没有返回有效的搜索任务 ID。")
    return value.strip()


def _job_status(job: Mapping[str, Any]) -> str:
    value = job.get("status")
    if not isinstance(value, str) or not value.strip():
        raise SearchProtocolError("后端搜索任务没有有效状态。")
    return value.strip().lower()


def _progress_signature(job: Mapping[str, Any]) -> str:
    progress = job.get("progress")
    return repr(progress)


def _optional_trimmed(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SearchValidationError(f"{name}必须是字符串。")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise SearchValidationError(f"{name}不能超过 {maximum} 个字符。")
    return normalized


def _unique_values(
    values: Sequence[str],
    name: str,
    *,
    maximum_items: int,
    maximum_characters: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SearchValidationError(f"{name} 必须是字符串序列。")
    if len(values) > maximum_items:
        raise SearchValidationError(f"{name} 最多包含 {maximum_items} 项。")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SearchValidationError(f"{name} 不能包含空值。")
        normalized = value.strip()
        if len(normalized) > maximum_characters:
            raise SearchValidationError(
                f"{name} 的每一项不能超过 {maximum_characters} 个字符。"
            )
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _positive_integer(value: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_TECHNICAL_COUNT
    ):
        raise SearchValidationError(f"{name} must be a positive integer.")
    return value


def _non_negative_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchValidationError(f"{name} 必须是数字。")
    normalized = float(value)
    if normalized < 0 or not math.isfinite(normalized):
        raise SearchValidationError(f"{name} 必须是非负有限数字。")
    return normalized


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _safe_suffix(value: str) -> str:
    suffix = value.lower()
    if not suffix or len(suffix) > 16:
        return ".img"
    if suffix[0] != "." or not suffix[1:].isalnum():
        return ".img"
    return suffix


def _unlink_if_present(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Stale query files are harmless and live in an owned temporary folder.
        # A later host cleanup can retry without masking the original operation.
        return


__all__ = [
    "NormalizedSearchRequest",
    "SearchClient",
    "SearchMode",
    "SearchOutcome",
    "SearchProtocolError",
    "SearchQueryType",
    "SearchRequest",
    "SearchService",
    "SearchServiceError",
    "SearchValidationError",
    "SearchWaitTimeout",
    "SubmittedSearch",
    "TagMode",
    "normalize_search_request",
]
