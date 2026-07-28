"""Validated search workflow for the local application host.

The persistent backend only accepts query images from its private staging
directory.  This module owns that staging copy, builds the exact backend job
payload, and keeps polling/cancellation logic out of Tk event handlers.
"""

from __future__ import annotations

import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from image_vector_service.image_data_uri import DASHSCOPE_DATA_URI_TARGET_BYTES

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
_MAX_LAN_DECODE_PIXELS = 40_000_000
_MAX_LAN_OUTPUT_DIMENSION = 4096
_MIN_LAN_OUTPUT_DIMENSION = 64
_LAN_JPEG_QUALITIES = (90, 82, 74, 66, 58, 50)
_MAX_LAN_RESIZE_ROUNDS = 10
_SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".dib",
        ".icns",
        ".sgi",
    }
)
_IMAGE_SUFFIX_BY_FORMAT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "ICO": ".ico",
    "ICNS": ".icns",
    "SGI": ".sgi",
}


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
        max_query_image_bytes: int | None = _MAX_QUERY_IMAGE_BYTES,
        model_source_image_bytes: int = _MAX_QUERY_IMAGE_BYTES,
        max_image_data_uri_bytes: int = DASHSCOPE_DATA_URI_TARGET_BYTES,
        max_decode_pixels: int = _MAX_LAN_DECODE_PIXELS,
        max_output_dimension: int = _MAX_LAN_OUTPUT_DIMENSION,
    ) -> None:
        if max_query_image_bytes is not None and (
            isinstance(max_query_image_bytes, bool) or max_query_image_bytes < 1
        ):
            raise ValueError("max_query_image_bytes must be a positive integer or None")
        for name, value in (
            ("model_source_image_bytes", model_source_image_bytes),
            ("max_image_data_uri_bytes", max_image_data_uri_bytes),
            ("max_decode_pixels", max_decode_pixels),
            ("max_output_dimension", max_output_dimension),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._client = client
        self._query_root = Path(query_root).expanduser().resolve()
        self._max_query_image_bytes = max_query_image_bytes
        self._model_source_image_bytes = model_source_image_bytes
        self._max_image_data_uri_bytes = max_image_data_uri_bytes
        self._max_decode_pixels = max_decode_pixels
        self._max_output_dimension = max_output_dimension

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
            source_stat = resolved_source.stat()
        except OSError as exc:
            raise SearchValidationError("无法读取查询图片大小。") from exc
        size = source_stat.st_size
        if size <= 0:
            raise SearchValidationError("查询图片不能为空文件。")
        if (
            self._max_query_image_bytes is not None
            and size > self._max_query_image_bytes
        ):
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

        # ``None`` is reserved for the LAN service. Its HTTP upload is already
        # streamed to disk without a byte cap, but the model client still has a
        # separate source-read ceiling. Oversized sources are decoded from the
        # file handle and converted into a bounded JPEG before backend submit;
        # the original upload is never rewritten or loaded wholesale as bytes.
        if (
            self._max_query_image_bytes is None
            and size > self._model_source_image_bytes
        ):
            return self._stage_transcoded_lan_image(
                resolved_source,
                source_stat=source_stat,
                root=root,
            )

        suffix = _safe_suffix(resolved_source.suffix)
        if (
            self._max_query_image_bytes is None
            and suffix not in _SUPPORTED_IMAGE_SUFFIXES
        ):
            # LAN uploads intentionally use opaque ``.bin`` storage names.
            # Detect only enough metadata to give the backend a supported
            # extension; pixel data remains streaming-copy-only in this branch.
            suffix = _detect_image_suffix(
                resolved_source,
                expected_stat=source_stat,
            )
        target = root / f"query-{uuid.uuid4().hex}{suffix}"
        try:
            with (
                resolved_source.open("rb") as source_stream,
                target.open("xb") as target_stream,
            ):
                opened_stat = os.fstat(source_stream.fileno())
                if _stat_signature(opened_stat) != _stat_signature(source_stat):
                    raise SearchValidationError("查询图片在处理时发生变化，请重试。")
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
                closed_stat = os.fstat(source_stream.fileno())
            shutil.copystat(resolved_source, target, follow_symlinks=True)
            copied_size = target.stat().st_size
            final_stat = resolved_source.stat()
        except SearchValidationError:
            _unlink_if_present(target)
            raise
        except OSError as exc:
            _unlink_if_present(target)
            raise SearchServiceError("复制查询图片到后端暂存目录失败。") from exc
        if (
            copied_size != size
            or _stat_signature(closed_stat) != _stat_signature(source_stat)
            or _stat_signature(final_stat) != _stat_signature(source_stat)
        ):
            _unlink_if_present(target)
            raise SearchValidationError("查询图片在复制时发生变化，请重试。")
        return target.resolve(strict=True)

    def _stage_transcoded_lan_image(
        self,
        source: Path,
        *,
        source_stat: os.stat_result,
        root: Path,
    ) -> Path:
        """Create a model-safe LAN query copy with bounded decode memory."""

        target = root / f"query-{uuid.uuid4().hex}.jpg"
        prepared: Image.Image | None = None
        try:
            try:
                source_stream = source.open("rb")
            except OSError as exc:
                raise SearchValidationError("无法读取查询图片。") from exc
            with source_stream:
                opened_stat = os.fstat(source_stream.fileno())
                if _stat_signature(opened_stat) != _stat_signature(source_stat):
                    raise SearchValidationError("查询图片在处理时发生变化，请重试。")
                prepared = _prepare_lan_image(
                    source_stream,
                    max_decode_pixels=self._max_decode_pixels,
                    max_output_dimension=self._max_output_dimension,
                )
                _write_bounded_jpeg(
                    prepared,
                    target,
                    model_source_limit=self._model_source_image_bytes,
                    data_uri_limit=self._max_image_data_uri_bytes,
                )
                closed_stat = os.fstat(source_stream.fileno())
            try:
                final_stat = source.stat()
            except OSError as exc:
                raise SearchValidationError(
                    "查询图片在处理时发生变化，请重试。"
                ) from exc
            if _stat_signature(closed_stat) != _stat_signature(
                source_stat
            ) or _stat_signature(final_stat) != _stat_signature(source_stat):
                raise SearchValidationError("查询图片在处理时发生变化，请重试。")
            _verify_staged_jpeg(target)
            return target.resolve(strict=True)
        except (SearchServiceError, SearchValidationError):
            _unlink_if_present(target)
            raise
        except MemoryError as exc:
            _unlink_if_present(target)
            raise SearchServiceError("处理查询图片时内存不足。") from exc
        except OSError as exc:
            _unlink_if_present(target)
            raise SearchServiceError("保存查询图片暂存副本失败。") from exc
        except Exception as exc:
            _unlink_if_present(target)
            raise SearchServiceError("处理查询图片暂存副本失败。") from exc
        finally:
            if prepared is not None:
                prepared.close()


def _detect_image_suffix(
    source: Path,
    *,
    expected_stat: os.stat_result,
) -> str:
    """Identify an opaque LAN upload without reading its complete payload."""

    try:
        with source.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if _stat_signature(opened_stat) != _stat_signature(expected_stat):
                raise SearchValidationError("查询图片在处理时发生变化，请重试。")
            with Image.open(stream) as image:
                image_format = (image.format or "").upper()
            closed_stat = os.fstat(stream.fileno())
        final_stat = source.stat()
    except SearchValidationError:
        raise
    except MemoryError as exc:
        raise SearchServiceError("识别查询图片时内存不足。") from exc
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise SearchValidationError("查询图片损坏或格式不受支持。") from exc
    if _stat_signature(closed_stat) != _stat_signature(
        expected_stat
    ) or _stat_signature(final_stat) != _stat_signature(expected_stat):
        raise SearchValidationError("查询图片在处理时发生变化，请重试。")
    suffix = _IMAGE_SUFFIX_BY_FORMAT.get(image_format)
    if suffix is None:
        raise SearchValidationError("查询图片格式不受支持。")
    return suffix


def _prepare_lan_image(
    source_stream: BinaryIO,
    *,
    max_decode_pixels: int,
    max_output_dimension: int,
) -> Image.Image:
    """Decode one image with a hard pixel budget and return an owned RGB copy."""

    oriented: Image.Image | None = None
    try:
        with Image.open(source_stream) as opened:
            image_format = (opened.format or "").upper()
            if image_format not in _IMAGE_SUFFIX_BY_FORMAT:
                raise SearchValidationError("查询图片格式不受支持。")
            width, height = opened.size
            if width < 1 or height < 1:
                raise SearchValidationError("查询图片尺寸无效。")

            original_pixels = width * height
            if (
                original_pixels > max_decode_pixels
                or max(width, height) > max_output_dimension
            ):
                # JPEG and a few other decoders can reduce during decode. If a
                # format cannot honour ``draft``, the post-draft pixel gate
                # rejects it before ``load`` expands unsafe pixel memory.
                opened.draft(
                    "RGB",
                    _decoder_draft_size(
                        opened.size,
                        max_dimension=max_output_dimension,
                    ),
                )
            decoded_pixels = opened.width * opened.height
            if decoded_pixels > max_decode_pixels:
                raise SearchValidationError(
                    "查询图片像素过大，无法在安全内存范围内处理。"
                )
            if max(opened.size) > max_output_dimension:
                # Resize before EXIF transpose so formats without decoder
                # subsampling do not create a second full-resolution oriented
                # copy. Rotation/reflection is applied to the reduced pixels.
                opened.thumbnail(
                    (max_output_dimension, max_output_dimension),
                    Image.Resampling.LANCZOS,
                )

            oriented = ImageOps.exif_transpose(opened)
            oriented.load()
            if oriented.width * oriented.height > max_decode_pixels:
                raise SearchValidationError("查询图片旋转后的像素数量超过安全限制。")
            return _flatten_lan_image(oriented)
    except SearchValidationError:
        raise
    except MemoryError as exc:
        raise SearchServiceError("处理查询图片时内存不足。") from exc
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise SearchValidationError("查询图片损坏或无法解码。") from exc
    finally:
        if oriented is not None:
            oriented.close()


def _flatten_lan_image(image: Image.Image) -> Image.Image:
    """Match the model encoder's transparent-image behaviour using white."""

    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        try:
            background = Image.new("RGB", rgba.size, "white")
            alpha = rgba.getchannel("A")
            try:
                background.paste(rgba, mask=alpha)
            finally:
                alpha.close()
            background.load()
            return background
        finally:
            rgba.close()
    converted = image.convert("RGB")
    converted.load()
    return converted


def _write_bounded_jpeg(
    image: Image.Image,
    target: Path,
    *,
    model_source_limit: int,
    data_uri_limit: int,
) -> None:
    """Write a JPEG that both the backend reader and data URI can accept."""

    payload_limit = min(
        model_source_limit,
        _maximum_data_uri_payload(data_uri_limit, mime_type="image/jpeg"),
    )
    current = image
    owns_current = False
    last_size = 0
    try:
        for _resize_round in range(_MAX_LAN_RESIZE_ROUNDS):
            for quality in _LAN_JPEG_QUALITIES:
                try:
                    mode = "wb" if target.exists() else "xb"
                    with target.open(mode) as stream:
                        current.save(
                            stream,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                            progressive=True,
                            subsampling="4:2:0",
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    last_size = target.stat().st_size
                except OSError as exc:
                    raise SearchServiceError(
                        "保存查询图片暂存副本失败，可能是磁盘空间不足。"
                    ) from exc
                if (
                    0 < last_size <= payload_limit
                    and _data_uri_size(last_size, mime_type="image/jpeg")
                    <= data_uri_limit
                ):
                    return

            longest = max(current.size)
            if longest <= _MIN_LAN_OUTPUT_DIMENSION:
                break
            scale = 0.75
            if last_size > 0:
                scale = min(
                    scale,
                    max(0.35, math.sqrt(payload_limit / last_size) * 0.92),
                )
            next_longest = max(
                _MIN_LAN_OUTPUT_DIMENSION,
                int(longest * scale),
            )
            if next_longest >= longest:
                break
            ratio = next_longest / longest
            next_size = (
                max(1, int(current.width * ratio)),
                max(1, int(current.height * ratio)),
            )
            resized = current.resize(next_size, Image.Resampling.LANCZOS)
            if owns_current:
                current.close()
            current = resized
            owns_current = True
    finally:
        if owns_current:
            current.close()
    raise SearchValidationError("查询图片无法在安全尺寸内压缩到模型允许的大小。")


def _maximum_data_uri_payload(limit: int, *, mime_type: str) -> int:
    prefix_size = len(f"data:{mime_type};base64,".encode("ascii"))
    if limit <= prefix_size + 4:
        raise SearchValidationError("模型图片请求预算过小。")
    payload = ((limit - prefix_size) // 4) * 3
    while payload > 0 and _data_uri_size(payload, mime_type=mime_type) > limit:
        payload -= 1
    if payload < 1:
        raise SearchValidationError("模型图片请求预算过小。")
    return payload


def _data_uri_size(payload_size: int, *, mime_type: str) -> int:
    prefix_size = len(f"data:{mime_type};base64,".encode("ascii"))
    return prefix_size + 4 * ((payload_size + 2) // 3)


def _decoder_draft_size(
    size: tuple[int, int],
    *,
    max_dimension: int,
) -> tuple[int, int]:
    width, height = size
    if max(width, height) <= max_dimension:
        return size
    if width >= height:
        return max_dimension, max(1, height * max_dimension // width)
    return max(1, width * max_dimension // height), max_dimension


def _verify_staged_jpeg(path: Path) -> None:
    try:
        with Image.open(path) as image:
            if (image.format or "").upper() != "JPEG":
                raise SearchServiceError("查询图片暂存副本格式无效。")
            image.verify()
    except SearchServiceError:
        raise
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise SearchServiceError("查询图片暂存副本校验失败。") from exc


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(getattr(value, "st_size", -1)),
        int(getattr(value, "st_mtime_ns", -1)),
    )


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
