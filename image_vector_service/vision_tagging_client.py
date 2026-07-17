"""Independent qwen3-vl-flash client for proposed image annotations.

This module intentionally has no dependency on the embedding client or the
index transaction. Callers can run it after indexing and persist/review the
returned proposals separately.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from .auto_tagging_assets import (
    AUTO_TAGGING_SCHEMA_VERSION,
    ENTITY_STATES,
    ENTITY_TYPES,
    FIELD_SPECS,
    FIELD_TAG_LABELS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
)
from .image_data_uri import (
    ImageDataUriError,
    ImageSourceChangedError,
    encode_image_data_uri,
)
from .rate_limiter import (
    ModelRateLimiter,
    RateLimitCancelled,
    RateLimitConfig,
    get_process_rate_limiter,
    retry_after_seconds,
)

DEFAULT_VISION_API_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)
RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504}
EVIDENCE_SOURCES = {
    "manual_tag",
    "folder_name",
    "file_name",
    "sidecar",
    "ocr",
    "watermark",
    "visual",
    "none",
}
EXPLICIT_IDENTITY_EVIDENCE = {
    "manual_tag",
    "folder_name",
    "file_name",
    "sidecar",
    "ocr",
    "watermark",
}
IDENTITY_RESTRICTED_ENTITY_TYPES = {"real_person", "cosplayer"}
ENTITY_CONFIRMATION_MIN_CONFIDENCE = 0.80
FIELD_REVIEW_CONFIDENCE = 0.65
_SECONDARY_IMAGE_DATA_URI_TARGET = 7 * 1024 * 1024

_DELIMITERS = r"\s_\-—–|/·,，;；"
_COUNT_COMPONENT = (
    r"\d{1,7}(?:\.\d+)?\s*"
    r"(?:(?:p|pics?|pictures?|images?|photos?)(?![A-Za-z])|张|枚|幅|个|图)"
)
_SIZE_COMPONENT = r"\d+(?:\.\d+)?\s*(?:bytes?|[kmgtpe]i?b)(?![A-Za-z])"
_TECHNICAL_EXACT_RE = re.compile(
    rf"^(?:{_COUNT_COMPONENT}|{_SIZE_COMPONENT})$", re.IGNORECASE
)
_TECHNICAL_INLINE_RE = re.compile(
    rf"(?:{_COUNT_COMPONENT}|{_SIZE_COMPONENT})",
    re.IGNORECASE,
)
_EDGE_DELIMITER_RE = re.compile(rf"^[{_DELIMITERS}]+|[{_DELIMITERS}]+$")
_REPEATED_DELIMITER_RE = re.compile(r"(?:\s*[_\-—–|/·,，;；]\s*){2,}")


class VisionTaggingError(RuntimeError):
    """Base error for visual auto-tagging."""


class VisionConfigurationError(VisionTaggingError):
    """Raised for an unusable client configuration."""


class VisionInputError(VisionTaggingError, ValueError):
    """Raised before a request when the local image or context is invalid."""


class VisionSourceChangedError(VisionInputError):
    """Raised when source bytes no longer match the indexed SHA-256."""

    retryable = True


class VisionTransportError(VisionTaggingError):
    """Raised when DashScope cannot return a usable HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class VisionResponseError(VisionTaggingError):
    """Raised when a successful response violates the annotation schema."""


class TaggingBudgetExceeded(VisionTaggingError):
    """Raised before an API request that would exceed the configured budget."""


@dataclass(frozen=True)
class VisionPricing:
    """Dynamic pricing; values are yuan per one million tokens."""

    input_yuan_per_million: float
    output_yuan_per_million: float
    effective_from: str = ""

    def __post_init__(self) -> None:
        for name in ("input_yuan_per_million", "output_yuan_per_million"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number.")
            object.__setattr__(self, name, value)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_yuan_per_million
            + output_tokens * self.output_yuan_per_million
        ) / 1_000_000


@dataclass(frozen=True)
class TaggingBudget:
    max_images: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_yuan: float | None = None
    estimated_input_tokens_per_image: int = 0
    estimated_output_tokens_per_image: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_images",
            "max_input_tokens",
            "max_output_tokens",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive when configured.")
        if self.max_cost_yuan is not None and (
            not math.isfinite(self.max_cost_yuan) or self.max_cost_yuan <= 0
        ):
            raise ValueError("max_cost_yuan must be finite and positive.")
        if self.estimated_input_tokens_per_image < 0:
            raise ValueError("estimated_input_tokens_per_image cannot be negative.")
        if self.estimated_output_tokens_per_image < 0:
            raise ValueError("estimated_output_tokens_per_image cannot be negative.")


@dataclass(frozen=True)
class VisionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, value: Any) -> VisionUsage:
        raw = dict(value) if isinstance(value, Mapping) else {}
        input_tokens = _non_negative_int(
            raw.get("prompt_tokens", raw.get("input_tokens", 0)),
            "input token usage",
        )
        output_tokens = _non_negative_int(
            raw.get("completion_tokens", raw.get("output_tokens", 0)),
            "output token usage",
        )
        total_tokens = _non_negative_int(
            raw.get("total_tokens", input_tokens + output_tokens),
            "total token usage",
        )
        if total_tokens < input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens
        return cls(input_tokens, output_tokens, total_tokens, raw)


class TaggingBudgetTracker:
    """Thread-safe hard-limit tracker shared by a tagging job."""

    def __init__(
        self,
        budget: TaggingBudget,
        pricing: VisionPricing | None = None,
    ) -> None:
        if budget.max_cost_yuan is not None and pricing is None:
            raise ValueError("Pricing is required when max_cost_yuan is configured.")
        self.budget = budget
        self.pricing = pricing
        self._lock = threading.Lock()
        self._reserved_images = 0
        self._completed_images = 0
        self._pending_images = 0
        self._input_tokens = 0
        self._output_tokens = 0

    def authorize_next_image(self) -> None:
        with self._lock:
            next_images = self._reserved_images + 1
            next_input = (
                self._input_tokens
                + (self._pending_images + 1)
                * self.budget.estimated_input_tokens_per_image
            )
            next_output = (
                self._output_tokens
                + (self._pending_images + 1)
                * self.budget.estimated_output_tokens_per_image
            )
            if (
                self.budget.max_images is not None
                and next_images > self.budget.max_images
            ):
                raise TaggingBudgetExceeded("The image budget is exhausted.")
            if (
                self.budget.max_input_tokens is not None
                and next_input > self.budget.max_input_tokens
            ):
                raise TaggingBudgetExceeded("The input-token budget is exhausted.")
            if (
                self.budget.max_output_tokens is not None
                and next_output > self.budget.max_output_tokens
            ):
                raise TaggingBudgetExceeded("The output-token budget is exhausted.")
            if self.budget.max_cost_yuan is not None:
                assert self.pricing is not None
                projected_cost = self.pricing.cost(next_input, next_output)
                if projected_cost > self.budget.max_cost_yuan:
                    raise TaggingBudgetExceeded("The monetary budget is exhausted.")
            self._reserved_images = next_images
            self._pending_images += 1

    def record(self, usage: VisionUsage) -> None:
        with self._lock:
            if self._pending_images < 1:
                raise RuntimeError("No authorized tagging request is pending.")
            self._pending_images -= 1
            self._completed_images += 1
            self._input_tokens += usage.input_tokens
            self._output_tokens += usage.output_tokens

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            cost = (
                self.pricing.cost(self._input_tokens, self._output_tokens)
                if self.pricing is not None
                else 0.0
            )
            return {
                "reserved_images": self._reserved_images,
                "completed_images": self._completed_images,
                "pending_images": self._pending_images,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cost_yuan": cost,
            }


@dataclass(frozen=True)
class VisionTaggingConfig:
    model: str = "qwen3-vl-flash"
    api_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    max_image_bytes: int = 10 * 1024 * 1024 - 64 * 1024
    max_source_image_bytes: int = 256 * 1024 * 1024
    max_request_bytes: int = 30 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_context_chars: int = 8_000
    max_output_tokens: int = 1_000
    temperature: float = 0.0
    use_json_response_format: bool = True
    pricing: VisionPricing | None = None
    concurrency: int = 2
    rate_limit_requests_per_minute: int = 48
    rate_limit_tokens_per_minute: int = 80_000
    rate_limit_hard_requests_per_minute: int = 60
    rate_limit_hard_tokens_per_minute: int = 100_000
    rate_limit_max_concurrency: int = 6
    rate_limit_estimated_input_tokens: int = 1_000
    rate_limit_estimated_output_tokens: int = 250
    expected_source_sha256: str | None = None

    @property
    def resolved_api_url(self) -> str:
        return (
            (self.api_url or "").strip()
            or os.getenv("DASHSCOPE_VISION_API_URL", "").strip()
            or DEFAULT_VISION_API_URL
        )

    def require_api_key(self) -> str:
        api_key = (self.api_key or "").strip() or os.getenv(
            "DASHSCOPE_API_KEY", ""
        ).strip()
        if not api_key:
            raise VisionConfigurationError(
                "DASHSCOPE_API_KEY is required for visual auto-tagging."
            )
        return api_key

    def validate(self) -> None:
        if not self.model.strip():
            raise VisionConfigurationError("The visual model cannot be empty.")
        if not self.resolved_api_url.lower().startswith(("http://", "https://")):
            raise VisionConfigurationError("The visual API URL must use HTTP(S).")
        if self.timeout_seconds <= 0:
            raise VisionConfigurationError("timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise VisionConfigurationError("max_retries cannot be negative.")
        if self.retry_base_seconds < 0:
            raise VisionConfigurationError("retry_base_seconds cannot be negative.")
        for name in (
            "max_image_bytes",
            "max_source_image_bytes",
            "max_request_bytes",
            "max_response_bytes",
            "max_context_chars",
            "max_output_tokens",
        ):
            if getattr(self, name) < 1:
                raise VisionConfigurationError(f"{name} must be positive.")
        if self.max_request_bytes < self.max_image_bytes:
            raise VisionConfigurationError(
                "max_request_bytes must be at least max_image_bytes."
            )
        if not 0 <= self.temperature <= 2:
            raise VisionConfigurationError("temperature must be between 0 and 2.")
        rate_limit_fields = (
            "concurrency",
            "rate_limit_requests_per_minute",
            "rate_limit_tokens_per_minute",
            "rate_limit_hard_requests_per_minute",
            "rate_limit_hard_tokens_per_minute",
            "rate_limit_max_concurrency",
            "rate_limit_estimated_input_tokens",
            "rate_limit_estimated_output_tokens",
        )
        for name in rate_limit_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise VisionConfigurationError(f"{name} must be a positive integer.")
        if (
            self.rate_limit_requests_per_minute
            > self.rate_limit_hard_requests_per_minute
        ):
            raise VisionConfigurationError(
                "rate_limit_requests_per_minute cannot exceed its hard limit."
            )
        if self.rate_limit_tokens_per_minute > self.rate_limit_hard_tokens_per_minute:
            raise VisionConfigurationError(
                "rate_limit_tokens_per_minute cannot exceed its hard limit."
            )
        if self.concurrency > self.rate_limit_max_concurrency:
            raise VisionConfigurationError(
                "concurrency cannot exceed rate_limit_max_concurrency."
            )
        if self.expected_source_sha256 is not None:
            expected_hash = self.expected_source_sha256.strip().casefold()
            if len(expected_hash) != 64 or any(
                character not in "0123456789abcdef" for character in expected_hash
            ):
                raise VisionConfigurationError(
                    "expected_source_sha256 must be a 64-character hex digest."
                )


@dataclass(frozen=True)
class TaggingContext:
    folder_name: str = ""
    file_name: str = ""
    manual_tags: tuple[str, ...] = ()
    sidecar_metadata: Mapping[str, Any] = field(default_factory=dict)
    ocr_text: str = ""
    watermark_text: str = ""

    def with_file_name(self, file_name: str) -> TaggingContext:
        if self.file_name:
            return self
        return TaggingContext(
            folder_name=self.folder_name,
            file_name=file_name,
            manual_tags=self.manual_tags,
            sidecar_metadata=self.sidecar_metadata,
            ocr_text=self.ocr_text,
            watermark_text=self.watermark_text,
        )

    def to_prompt_dict(self, max_chars: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "manual_tags": [_clean_context_text(tag, 200) for tag in self.manual_tags],
            "folder_name": _clean_context_text(self.folder_name, 500),
            "file_name": _clean_context_text(self.file_name, 500),
            "sidecar": _json_safe_mapping(self.sidecar_metadata),
            "ocr_text": _clean_context_text(self.ocr_text, max_chars),
            "watermark_text": _clean_context_text(self.watermark_text, 2_000),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return payload
        # Preserve high-priority explicit metadata and trim the two long fields.
        overflow = len(encoded) - max_chars
        for key in ("ocr_text", "sidecar", "watermark_text"):
            if overflow <= 0:
                break
            value = payload[key]
            serialized = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, default=str)
            )
            trim_by = min(len(serialized), overflow + 32)
            payload[key] = serialized[: max(0, len(serialized) - trim_by)]
            overflow = (
                len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                - max_chars
            )
        return payload


EntityState = Literal[
    "confirmed",
    "suggested",
    "conflict",
    "unable_to_confirm",
    "not_applicable",
]


@dataclass(frozen=True)
class EntityAnnotation:
    name: str | None
    state: EntityState
    evidence: tuple[str, ...]
    evidence_text: str = ""
    confidence: float = 0.0
    rejection_reason: str = ""


@dataclass(frozen=True)
class StableFieldAnnotation:
    values: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class VisionAnnotation:
    schema_version: int
    prompt_version: str
    description: str
    categories: dict[str, tuple[str, ...]]
    entities: dict[str, tuple[EntityAnnotation, ...]]
    tags: tuple[str, ...]
    suggested_tags: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    stable_fields: dict[str, StableFieldAnnotation] = field(default_factory=dict)
    controlled_tags: tuple[str, ...] = ()
    requires_review: bool = False
    review_reasons: tuple[str, ...] = ()
    plus_recommended: bool = False


@dataclass(frozen=True)
class VisionTaggingResponse:
    annotation: VisionAnnotation
    request_id: str
    usage: VisionUsage
    cost_yuan: float | None


class DashScopeVisionTaggingClient:
    def __init__(
        self,
        config: VisionTaggingConfig | None = None,
        *,
        budget_tracker: TaggingBudgetTracker | None = None,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config or VisionTaggingConfig()
        self.config.validate()
        self.budget_tracker = budget_tracker
        limiter_config = RateLimitConfig(
            requests_per_minute=self.config.rate_limit_requests_per_minute,
            tokens_per_minute=self.config.rate_limit_tokens_per_minute,
            hard_requests_per_minute=(self.config.rate_limit_hard_requests_per_minute),
            hard_tokens_per_minute=self.config.rate_limit_hard_tokens_per_minute,
            initial_concurrency=self.config.concurrency,
            max_concurrency=self.config.rate_limit_max_concurrency,
        )
        self.limiter = limiter or get_process_rate_limiter(
            self.config.model, limiter_config
        )
        self.cancel_event = cancel_event
        self.request_count = 0
        self._request_count_lock = threading.Lock()

    def tag_image(
        self,
        image_path: Path,
        *,
        context: TaggingContext | None = None,
    ) -> VisionTaggingResponse:
        path = image_path.expanduser().resolve()
        effective_context = (context or TaggingContext()).with_file_name(path.name)
        image_data_url = self._encode_image(path, effective_context)
        request_data = self._build_request_data(image_data_url, effective_context)
        if self.budget_tracker is not None:
            self.budget_tracker.authorize_next_image()

        try:
            response = self._post_json(request_data, effective_context)
        except VisionTransportError as exc:
            if not _is_image_size_error(exc):
                raise
            # The provider rejected a representation that was already within
            # the documented limit. Retry exactly once with a smaller, forced
            # JPEG representation while preserving the original file identity.
            image_data_url = self._encode_image(
                path,
                effective_context,
                max_data_uri_bytes=min(
                    self.config.max_image_bytes,
                    _SECONDARY_IMAGE_DATA_URI_TARGET,
                ),
                max_output_dimension=3072,
                force_transcode=True,
            )
            request_data = self._build_request_data(
                image_data_url,
                effective_context,
            )
            response = self._post_json(request_data, effective_context)
        if self.budget_tracker is not None:
            self.budget_tracker.record(response.usage)
        return response

    def _build_request_data(
        self,
        image_data_url: str,
        context: TaggingContext,
    ) -> bytes:
        payload = self._build_payload(image_data_url, context)
        request_data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request_data) > self.config.max_request_bytes:
            raise VisionInputError(
                f"Visual tagging request exceeds {self.config.max_request_bytes} bytes."
            )
        return request_data

    def _encode_image(
        self,
        path: Path,
        context: TaggingContext,
        *,
        max_data_uri_bytes: int | None = None,
        max_output_dimension: int = 4096,
        force_transcode: bool = False,
    ) -> str:
        # The compatible API counts the complete data URI, not the source file.
        # Compute the exact JSON overhead first so the shared encoder can satisfy
        # both DashScope's item limit and this client's full-request budget.
        empty_payload = self._build_payload("", context)
        request_overhead = len(
            json.dumps(
                empty_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        try:
            encoded = encode_image_data_uri(
                path,
                max_source_bytes=self.config.max_source_image_bytes,
                max_data_uri_bytes=(
                    self.config.max_image_bytes
                    if max_data_uri_bytes is None
                    else max_data_uri_bytes
                ),
                request_byte_budget=self.config.max_request_bytes,
                request_overhead_bytes=request_overhead,
                max_output_dimension=max_output_dimension,
                expected_source_sha256=self.config.expected_source_sha256,
                force_transcode=force_transcode,
            )
        except ImageSourceChangedError as exc:
            raise VisionSourceChangedError(str(exc)) from exc
        except ImageDataUriError as exc:
            raise VisionInputError(str(exc)) from exc
        return encoded.data_uri

    def _build_payload(
        self,
        image_data_url: str,
        context: TaggingContext,
    ) -> dict[str, Any]:
        context_json = json.dumps(
            context.to_prompt_dict(self.config.max_context_chars),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {
                            "type": "text",
                            "text": (
                                "分析图片并结合以下显式元数据输出规定 JSON。"
                                f"元数据：{context_json}"
                            ),
                        },
                    ],
                },
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "enable_thinking": False,
        }
        if self.config.use_json_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post_json(
        self,
        request_data: bytes,
        context: TaggingContext,
    ) -> VisionTaggingResponse:
        last_error: VisionTaggingError | None = None
        estimated_tokens = self.config.rate_limit_estimated_input_tokens + min(
            self.config.rate_limit_estimated_output_tokens,
            self.config.max_output_tokens,
        )
        for attempt in range(self.config.max_retries + 1):
            retry_after: str | None = None
            request = urllib.request.Request(
                self.config.resolved_api_url,
                data=request_data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.config.require_api_key()}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(request_data)),
                    "User-Agent": "zvec-visual-auto-tagger/1.0",
                },
            )
            permit = self.limiter.acquire(estimated_tokens, self.cancel_event)
            with self._request_count_lock:
                self.request_count += 1
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as http_response:
                    response_body = _read_limited(
                        http_response,
                        self.config.max_response_bytes,
                    )
            except urllib.error.HTTPError as exc:
                permit.fail()
                status_code = exc.code
                retry_after = (
                    exc.headers.get("Retry-After") if exc.headers is not None else None
                )
                code, message = _safe_http_error_details(exc)
                exc.close()
                last_error = VisionTransportError(
                    f"DashScope {code or 'HTTPError'}: {message}",
                    status_code=status_code,
                    code=code,
                )
                if status_code not in RETRYABLE_HTTP_STATUSES:
                    raise last_error from exc
                if status_code == 429:
                    self.limiter.observe_429(retry_after)
                    retry_after = None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                permit.fail()
                last_error = VisionTransportError(
                    f"DashScope visual tagging request failed: {exc}"
                )
            except VisionResponseError as exc:
                permit.fail()
                last_error = exc
            except Exception:
                permit.fail()
                raise
            else:
                try:
                    envelope = _parse_json_object(response_body, "API response")
                    usage = VisionUsage.from_api(envelope.get("usage"))
                    model_content = _extract_model_content(envelope)
                    annotation = parse_model_annotation_json(
                        model_content,
                        context=context,
                    )
                except VisionResponseError as exc:
                    # Invalid model JSON is transient and may succeed on a
                    # bounded retry. Keep the conservative token reservation
                    # because a malformed envelope may omit reliable usage.
                    permit.fail()
                    last_error = exc
                else:
                    permit.succeed(usage.total_tokens if usage.raw else None)
                    cost = (
                        self.config.pricing.cost(
                            usage.input_tokens,
                            usage.output_tokens,
                        )
                        if self.config.pricing is not None
                        else None
                    )
                    return VisionTaggingResponse(
                        annotation=annotation,
                        request_id=str(
                            envelope.get("id") or envelope.get("request_id") or ""
                        ),
                        usage=usage,
                        cost_yuan=cost,
                    )

            if attempt < self.config.max_retries:
                delay = self.config.retry_base_seconds * (2**attempt)
                provider_delay = retry_after_seconds(retry_after)
                if provider_delay is not None:
                    delay = max(delay, provider_delay)
                self._sleep_before_retry(delay + random.uniform(0, delay * 0.2))

        assert last_error is not None
        raise last_error

    def _sleep_before_retry(self, delay: float) -> None:
        if delay <= 0:
            return
        if self.cancel_event is not None:
            if self.cancel_event.wait(delay):
                raise RateLimitCancelled(
                    "The visual tagging request was cancelled before retrying."
                )
            return
        time.sleep(delay)


def _is_image_size_error(error: VisionTransportError) -> bool:
    if error.status_code not in {400, 413, 422}:
        return False
    text = f"{error.code} {error}".casefold()
    return any(
        token in text
        for token in (
            "image size should be",
            "file size is too large",
            "multimodal file size",
            "max bytes per data-uri",
            "data uri item",
            "data-uri item",
            "10240kb",
            "10485760",
        )
    )


_CONTROLLED_CODE_FIELDS = {
    code: field_name for field_name, spec in FIELD_SPECS.items() for code in spec.values
}

# Only deterministic equivalents are mapped. Ambiguous model inventions are
# dropped while the remaining valid fields continue through strict validation.
_REPAIR_CODE_ALIASES: dict[str, tuple[str, str]] = {
    "pose_laying": ("pose", "pose_lying"),
    "pose_posing": ("action", "action_posing"),
    "pose_holding_object": ("action", "action_holding_object"),
    "pose_bust": ("shot_type", "shot_bust"),
    "count_three_people": ("people_count", "count_small_group"),
    "hair_golden": ("hair_color", "hair_blonde"),
    "hair_blond": ("hair_color", "hair_blonde"),
    "clothing_tshirt": ("clothing", "clothing_shirt_or_blouse"),
    "clothing_cardigan": ("clothing", "clothing_jacket"),
    "clothing_blazer": ("clothing", "clothing_jacket"),
    "clothing_bikini": ("clothing", "clothing_swimsuit"),
    "clothing_tights": ("legwear", "legwear_tights"),
    "clothing_pantyhose": ("legwear", "legwear_pantyhose"),
    "clothing_gloves": ("accessory", "accessory_gloves"),
    "clothing_choker": ("accessory", "accessory_choker"),
    "accessory_idol_outfit": ("clothing", "clothing_idol_outfit"),
    "prop_bag": ("accessory", "accessory_bag"),
    "prop_tail": ("accessory", "accessory_tail"),
    "prop_wings": ("accessory", "accessory_wings"),
    "prop_sunglasses": ("accessory", "accessory_sunglasses"),
    "prop_headphones": ("accessory", "accessory_headphones"),
    "prop_gloves": ("accessory", "accessory_gloves"),
    "prop_animal_ears": ("accessory", "accessory_animal_ears"),
    "prop_scarf": ("accessory", "accessory_scarf"),
    "prop_piano": ("prop", "prop_musical_instrument"),
    "prop_wine_glass": ("prop", "prop_food_or_drink"),
    "prop_wine_bottle": ("prop", "prop_food_or_drink"),
    "prop_bottle": ("prop", "prop_food_or_drink"),
    "prop_glass": ("prop", "prop_food_or_drink"),
    "prop_cup": ("prop", "prop_food_or_drink"),
    "prop_cupcake": ("prop", "prop_food_or_drink"),
    "prop_dessert": ("prop", "prop_food_or_drink"),
    "prop_fruit": ("prop", "prop_food_or_drink"),
    "prop_sweet_treat": ("prop", "prop_food_or_drink"),
    "scene_pool": ("scene", "scene_outdoor"),
    "scene_poolside": ("scene", "scene_outdoor"),
    "scene_water": ("scene", "scene_nature"),
    "scene_river": ("scene", "scene_nature"),
    "scene_stream": ("scene", "scene_nature"),
    "scene_bar": ("scene", "scene_indoor"),
    "scene_cafe": ("scene", "scene_indoor"),
    "scene_office": ("scene", "scene_indoor"),
    "scene_bathroom": ("scene", "scene_indoor"),
    "action_writing": ("action", "action_holding_object"),
    "legwear_white_stockings": ("legwear", "legwear_thigh_high_stockings"),
}

_REPAIR_EVIDENCE_ALIASES = {
    "manual": "manual_tag",
    "manual_tags": "manual_tag",
    "folder": "folder_name",
    "filename": "file_name",
    "file": "file_name",
    "metadata": "sidecar",
    "sidecar_metadata": "sidecar",
    "ocr_text": "ocr",
    "watermark_text": "watermark",
    "appearance": "visual",
    "image": "visual",
}


def parse_model_annotation_json(
    content: str,
    *,
    context: TaggingContext | None = None,
) -> VisionAnnotation:
    """Repair bounded model mistakes, then pass the result through strict parsing.

    This wrapper is intentionally used only for fresh provider output.
    ``parse_annotation_json`` remains the strict trust boundary for tests,
    cached data, and any other caller.
    """

    if not isinstance(content, str):
        raise VisionResponseError("Model content must be a JSON string.")
    normalized_content = _strip_model_json_fence(content)
    root = _parse_json_object(
        normalized_content.encode("utf-8"),
        "model annotation",
    )
    repaired, repair_warnings = _repair_annotation_object(root)
    annotation = parse_annotation_json(
        json.dumps(repaired, ensure_ascii=False, separators=(",", ":")),
        context=context,
    )
    return replace(
        annotation,
        warnings=_deduplicate([*annotation.warnings, *repair_warnings]),
    )


def _strip_model_json_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if match is None:
        raise VisionResponseError("Model content contains an invalid Markdown fence.")
    return match.group(1).strip()


def _repair_annotation_object(
    root: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    warnings: list[str] = []
    description = root.get("description", "")
    if not isinstance(description, str):
        description = "" if description is None else str(description)
        warnings.append("repair:description_coerced_to_string")
    description = unicodedata.normalize("NFKC", description).strip()
    if len(description) > 20:
        description = description[:20]
        warnings.append("repair:description_truncated")

    fields = _repair_fields(root.get("fields"), warnings)
    entities = _repair_entities(root.get("entities"), warnings)
    ignored_root_fields = set(root) - {
        "schema_version",
        "description",
        "fields",
        "entities",
    }
    if ignored_root_fields:
        warnings.append(
            "repair:ignored_root_fields:" + ",".join(sorted(ignored_root_fields))
        )
    if root.get("schema_version") != AUTO_TAGGING_SCHEMA_VERSION:
        warnings.append("repair:schema_version_normalized")
    return (
        {
            "schema_version": AUTO_TAGGING_SCHEMA_VERSION,
            "description": description,
            "fields": fields,
            "entities": entities,
        },
        _deduplicate(warnings),
    )


def _repair_fields(value: Any, warnings: list[str]) -> dict[str, dict[str, Any]]:
    if isinstance(value, Mapping):
        raw_fields = value
    else:
        raw_fields = {}
        if value is not None:
            warnings.append("repair:fields_replaced_with_empty_object")

    values_by_field: dict[str, list[str]] = {name: [] for name in FIELD_SPECS}
    confidence_by_field: dict[str, list[float]] = {name: [] for name in FIELD_SPECS}
    for source_field in FIELD_SPECS:
        raw_field = raw_fields.get(source_field)
        if isinstance(raw_field, Mapping):
            raw_values = raw_field.get("values", raw_field.get("labels", ()))
            confidence = _repair_confidence(
                raw_field.get("confidence"),
                default=0.80,
            )
            ignored_members = set(raw_field) - {"values", "labels", "confidence"}
            if ignored_members:
                warnings.append(
                    f"repair:fields.{source_field}:ignored_members:"
                    + ",".join(sorted(str(item) for item in ignored_members))
                )
        else:
            raw_values = raw_field
            confidence = 0.80
            if raw_field is not None:
                warnings.append(f"repair:fields.{source_field}:coerced_to_object")

        for raw_code in _repair_string_values(raw_values):
            resolved = _repair_controlled_code(source_field, raw_code)
            if resolved is None:
                warnings.append(
                    f"repair:fields.{source_field}:dropped_unknown_code:{raw_code}"
                )
                continue
            target_field, code = resolved
            if target_field != source_field:
                warnings.append(
                    f"repair:fields.{source_field}:moved:{code}->{target_field}"
                )
            if code not in values_by_field[target_field]:
                values_by_field[target_field].append(code)
                confidence_by_field[target_field].append(confidence)

    ignored_fields = set(raw_fields) - set(FIELD_SPECS)
    if ignored_fields:
        warnings.append(
            "repair:ignored_fields:"
            + ",".join(sorted(str(item) for item in ignored_fields))
        )

    repaired: dict[str, dict[str, Any]] = {}
    for field_name, spec in FIELD_SPECS.items():
        raw_values = values_by_field[field_name]
        if len(raw_values) > spec.max_items:
            warnings.append(f"repair:fields.{field_name}:truncated_to_{spec.max_items}")
        selected = raw_values[: spec.max_items]
        confidences = confidence_by_field[field_name][: len(selected)]
        repaired[field_name] = {
            "values": selected,
            "confidence": max(confidences, default=0.0),
        }
    return repaired


def _repair_controlled_code(
    source_field: str,
    value: str,
) -> tuple[str, str] | None:
    normalized = (
        unicodedata.normalize("NFKC", value)
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if not normalized:
        return None
    direct_field = _CONTROLLED_CODE_FIELDS.get(normalized)
    if direct_field is not None:
        return direct_field, normalized
    return _REPAIR_CODE_ALIASES.get(normalized)


def _repair_entities(
    value: Any,
    warnings: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if isinstance(value, Mapping):
        raw_entities = value
    else:
        raw_entities = {}
        if value is not None:
            warnings.append("repair:entities_replaced_with_empty_object")

    result: dict[str, list[dict[str, Any]]] = {}
    for entity_type in ENTITY_TYPES:
        raw_items = raw_entities.get(entity_type, ())
        if isinstance(raw_items, (Mapping, str)):
            items: Sequence[Any] = [raw_items]
            warnings.append(f"repair:entities.{entity_type}:wrapped_as_array")
        elif isinstance(raw_items, Sequence) and not isinstance(
            raw_items, (bytes, bytearray)
        ):
            items = raw_items
        else:
            items = ()
            if raw_items is not None:
                warnings.append(f"repair:entities.{entity_type}:replaced_with_array")

        repaired_items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(items):
            repaired = _repair_entity_item(
                entity_type,
                raw_item,
                warnings,
                index=index,
            )
            if repaired is not None:
                repaired_items.append(repaired)
        result[entity_type] = repaired_items

    ignored_types = set(raw_entities) - set(ENTITY_TYPES)
    if ignored_types:
        warnings.append(
            "repair:ignored_entity_types:"
            + ",".join(sorted(str(item) for item in ignored_types))
        )
    return result


def _repair_entity_item(
    entity_type: str,
    value: Any,
    warnings: list[str],
    *,
    index: int,
) -> dict[str, Any] | None:
    path = f"entities.{entity_type}[{index}]"
    if isinstance(value, str):
        candidate = unicodedata.normalize("NFKC", value).strip()
        if not candidate:
            return None
        if entity_type in IDENTITY_RESTRICTED_ENTITY_TYPES:
            warnings.append(f"repair:{path}:dropped_unverified_name")
            return None
        warnings.append(f"repair:{path}:string_coerced_to_suggestion")
        return {
            "name": candidate,
            "state": "suggested",
            "evidence": ["visual"],
            "evidence_text": "",
            "confidence": 0.50,
        }
    if not isinstance(value, Mapping):
        warnings.append(f"repair:{path}:dropped_non_object")
        return None

    raw_name = value.get("name")
    name = (
        unicodedata.normalize("NFKC", raw_name).strip()
        if isinstance(raw_name, str)
        else None
    )
    if name == "":
        name = None
    raw_state = str(value.get("state") or "").strip().casefold()
    state_aliases = {
        "confirmed": "confirmed",
        "suggested": "suggested",
        "conflict": "conflict",
        "unable": "unable_to_confirm",
        "unknown": "unable_to_confirm",
        "unable_to_confirm": "unable_to_confirm",
        "not_applicable": "not_applicable",
        "n/a": "not_applicable",
    }
    state = state_aliases.get(raw_state)
    if state is None:
        state = (
            "suggested"
            if name and entity_type not in IDENTITY_RESTRICTED_ENTITY_TYPES
            else "unable_to_confirm"
        )
        warnings.append(f"repair:{path}:state_normalized")

    evidence: list[str] = []
    for raw_source in _repair_string_values(value.get("evidence")):
        source = unicodedata.normalize("NFKC", raw_source).strip().casefold()
        source = _REPAIR_EVIDENCE_ALIASES.get(source, source)
        if source in EVIDENCE_SOURCES and source not in evidence:
            evidence.append(source)
        elif source:
            warnings.append(f"repair:{path}:dropped_evidence:{raw_source}")
    if not evidence:
        evidence = ["none"]
        warnings.append(f"repair:{path}:evidence_defaulted")

    evidence_text = value.get("evidence_text", "")
    if not isinstance(evidence_text, str):
        evidence_text = "" if evidence_text is None else str(evidence_text)
        warnings.append(f"repair:{path}:evidence_text_coerced")
    evidence_text = unicodedata.normalize("NFKC", evidence_text).strip()[:500]
    confidence = _repair_confidence(
        value.get("confidence"),
        default=0.80 if state == "confirmed" else 0.50,
    )

    if state in {"unable_to_confirm", "not_applicable"}:
        if name is not None:
            warnings.append(f"repair:{path}:name_cleared_for_{state}")
        name = None
    elif not name:
        state = "unable_to_confirm"
        name = None
        warnings.append(f"repair:{path}:missing_name_downgraded")
    elif (
        entity_type in IDENTITY_RESTRICTED_ENTITY_TYPES
        and not EXPLICIT_IDENTITY_EVIDENCE.intersection(evidence)
    ):
        name = None
        state = "unable_to_confirm"
        warnings.append(f"repair:{path}:identity_evidence_downgraded")

    return {
        "name": name,
        "state": state,
        "evidence": evidence,
        "evidence_text": evidence_text,
        "confidence": confidence,
    }


def _repair_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [item for item in value if isinstance(item, str)]
    return []


def _repair_confidence(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(confidence):
        return default
    return min(1.0, max(0.0, confidence))


def parse_annotation_json(
    content: str,
    *,
    context: TaggingContext | None = None,
) -> VisionAnnotation:
    """Strictly parse, validate and sanitize one model-produced JSON object."""

    if not isinstance(content, str):
        raise VisionResponseError("Model content must be a JSON string.")
    if "```" in content:
        raise VisionResponseError("Model content must not contain Markdown fences.")
    root = _parse_json_object(content.encode("utf-8"), "model annotation")
    required_keys = {"schema_version", "description", "fields", "entities"}
    missing = required_keys - root.keys()
    unknown = root.keys() - required_keys
    if missing:
        raise VisionResponseError(
            f"Model annotation is missing fields: {', '.join(sorted(missing))}."
        )
    if unknown:
        raise VisionResponseError(
            f"Model annotation has unknown fields: {', '.join(sorted(unknown))}."
        )
    if (
        isinstance(root["schema_version"], bool)
        or not isinstance(root["schema_version"], int)
        or root["schema_version"] != AUTO_TAGGING_SCHEMA_VERSION
    ):
        raise VisionResponseError(
            f"Expected schema_version {AUTO_TAGGING_SCHEMA_VERSION}."
        )
    description = _required_string(root["description"], "description")
    if len(description) > 20:
        raise VisionResponseError("description must not exceed 20 characters.")

    effective_context = context or TaggingContext()
    stable_fields, controlled_tags, field_review_reasons, field_plus = _parse_fields(
        root["fields"]
    )
    entities, entity_warnings, entity_review_reasons, entity_plus = _parse_entities(
        root["entities"],
        effective_context,
    )
    categories = {
        field_name: tuple(FIELD_TAG_LABELS[code] for code in stable_field.values)
        for field_name, stable_field in stable_fields.items()
    }
    review_reasons = _deduplicate([*field_review_reasons, *entity_review_reasons])

    return VisionAnnotation(
        schema_version=AUTO_TAGGING_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        description=description,
        categories=categories,
        entities=entities,
        tags=controlled_tags,
        suggested_tags=(),
        warnings=tuple(entity_warnings),
        stable_fields=stable_fields,
        controlled_tags=controlled_tags,
        requires_review=bool(review_reasons),
        review_reasons=review_reasons,
        plus_recommended=field_plus or entity_plus,
    )


def sanitize_generated_tag(value: str) -> str | None:
    """Remove count/size metadata and return a normalized tag, if meaningful."""

    if not isinstance(value, str):
        raise VisionResponseError("Tags must be strings.")
    tag = unicodedata.normalize("NFKC", value).strip()
    if not tag:
        return None
    if any(ord(character) < 32 for character in tag):
        raise VisionResponseError("Tags cannot contain control characters.")
    if _TECHNICAL_EXACT_RE.fullmatch(_strip_wrappers(tag)):
        return None
    previous = None
    while previous != tag:
        previous = tag
        tag = _TECHNICAL_INLINE_RE.sub("", tag)
    tag = _REPEATED_DELIMITER_RE.sub("-", tag)
    tag = _EDGE_DELIMITER_RE.sub("", tag).strip()
    if not tag or _TECHNICAL_EXACT_RE.fullmatch(_strip_wrappers(tag)):
        return None
    if len(tag) > 80:
        raise VisionResponseError("A generated tag cannot exceed 80 characters.")
    return tag


def _parse_fields(
    value: Any,
) -> tuple[
    dict[str, StableFieldAnnotation],
    tuple[str, ...],
    list[str],
    bool,
]:
    if not isinstance(value, Mapping):
        raise VisionResponseError("fields must be an object.")
    expected = set(FIELD_SPECS)
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown {', '.join(sorted(unknown))}")
        raise VisionResponseError("Invalid fields: " + "; ".join(details))

    parsed: dict[str, StableFieldAnnotation] = {}
    controlled_tags: list[str] = []
    review_reasons: list[str] = []
    plus_recommended = False
    for field_name, spec in FIELD_SPECS.items():
        field_path = f"fields.{field_name}"
        raw_field = value[field_name]
        if not isinstance(raw_field, Mapping):
            raise VisionResponseError(f"{field_path} must be an object.")
        required = {"values", "confidence"}
        if set(raw_field) != required:
            raise VisionResponseError(
                f"{field_path} must contain exactly values and confidence."
            )

        raw_values = _string_list(raw_field["values"], f"{field_path}.values")
        if not spec.multiple and len(raw_values) > 1:
            raise VisionResponseError(f"{field_path} accepts at most one value.")
        if len(raw_values) > spec.max_items:
            raise VisionResponseError(
                f"{field_path} accepts at most {spec.max_items} values."
            )
        if len(set(raw_values)) != len(raw_values):
            raise VisionResponseError(f"{field_path}.values contains duplicates.")
        unknown_values = [code for code in raw_values if code not in spec.values]
        if unknown_values:
            raise VisionResponseError(
                f"{field_path}.values contains unknown controlled codes: "
                + ", ".join(unknown_values)
            )

        confidence = _required_confidence(
            raw_field["confidence"], f"{field_path}.confidence"
        )
        parsed[field_name] = StableFieldAnnotation(raw_values, confidence)
        controlled_tags.extend(FIELD_TAG_LABELS[code] for code in raw_values)
        # Confidence is actionable only when the model selected a value. Empty
        # optional fields are not predictions and must not force review/Plus.
        if raw_values and confidence < FIELD_REVIEW_CONFIDENCE:
            review_reasons.append(f"field:{field_name}:low_confidence")
            if spec.critical:
                plus_recommended = True

    return (
        parsed,
        _deduplicate(controlled_tags),
        review_reasons,
        plus_recommended,
    )


def _parse_entities(
    value: Any,
    context: TaggingContext,
) -> tuple[
    dict[str, tuple[EntityAnnotation, ...]],
    list[str],
    list[str],
    bool,
]:
    if not isinstance(value, Mapping):
        raise VisionResponseError("entities must be an object.")
    expected = set(ENTITY_TYPES)
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        raise VisionResponseError(
            "entities must contain exactly: " + ", ".join(ENTITY_TYPES)
        )

    parsed: dict[str, tuple[EntityAnnotation, ...]] = {}
    warnings: list[str] = []
    review_reasons: list[str] = []
    plus_recommended = False
    for entity_type in ENTITY_TYPES:
        raw_items = value[entity_type]
        if not isinstance(raw_items, list):
            raise VisionResponseError(f"entities.{entity_type} must be an array.")
        items: list[EntityAnnotation] = []
        for index, raw_item in enumerate(raw_items):
            field_name = f"entities.{entity_type}[{index}]"
            item = _parse_entity(raw_item, field_name, context)
            if item.rejection_reason and item.name is None:
                warnings.append(f"{field_name}: {item.rejection_reason}")
            if item.state == "conflict":
                review_reasons.append(f"entity:{entity_type}:conflict")
                plus_recommended = True
            elif item.state == "suggested":
                review_reasons.append(f"entity:{entity_type}:suggested")
                if (
                    entity_type in {"character", "work"}
                    and item.confidence < ENTITY_CONFIRMATION_MIN_CONFIDENCE
                ):
                    plus_recommended = True
            elif item.state == "unable_to_confirm":
                review_reasons.append(f"entity:{entity_type}:unable_to_confirm")
            elif (
                item.state == "confirmed"
                and item.confidence < ENTITY_CONFIRMATION_MIN_CONFIDENCE
            ):
                review_reasons.append(f"entity:{entity_type}:low_confidence")
            items.append(item)
        parsed[entity_type] = tuple(items)
    return parsed, warnings, review_reasons, plus_recommended


def _parse_entity(
    value: Any,
    field_name: str,
    context: TaggingContext,
) -> EntityAnnotation:
    if not isinstance(value, Mapping):
        raise VisionResponseError(f"{field_name} must be an object.")
    required = {"name", "state", "evidence", "evidence_text", "confidence"}
    if set(value) != required:
        raise VisionResponseError(f"{field_name} has an invalid entity structure.")

    state = value["state"]
    if not isinstance(state, str) or state not in ENTITY_STATES:
        raise VisionResponseError(f"{field_name}.state is invalid.")
    parsed_state = cast(EntityState, state)
    raw_name = value["name"]
    if raw_name is not None and not isinstance(raw_name, str):
        raise VisionResponseError(f"{field_name}.name must be a string or null.")
    name = sanitize_generated_tag(raw_name) if isinstance(raw_name, str) else None
    evidence = _string_list(value["evidence"], f"{field_name}.evidence")
    if not evidence:
        raise VisionResponseError(f"{field_name}.evidence must not be empty.")
    if len(set(evidence)) != len(evidence):
        raise VisionResponseError(f"{field_name}.evidence contains duplicates.")
    if any(source not in EVIDENCE_SOURCES for source in evidence):
        raise VisionResponseError(f"{field_name}.evidence has an unknown source.")
    evidence_text = _optional_string(
        value["evidence_text"],
        f"{field_name}.evidence_text",
        max_length=500,
    )
    confidence = _required_confidence(value["confidence"], f"{field_name}.confidence")

    entity_type = field_name.split(".", 1)[1].split("[", 1)[0]
    usable_evidence = tuple(
        source
        for source in evidence
        if _evidence_is_available(
            source,
            name=name,
            evidence_text=evidence_text,
            context=context,
        )
    )
    rejection_reason = ""
    if parsed_state in {"unable_to_confirm", "not_applicable"}:
        if name is not None:
            raise VisionResponseError(
                f"{field_name}.name must be null when state is {parsed_state}."
            )
    elif not name:
        raise VisionResponseError(
            f"{field_name}.name is required when state is {parsed_state}."
        )
    elif (
        entity_type in IDENTITY_RESTRICTED_ENTITY_TYPES
        and not EXPLICIT_IDENTITY_EVIDENCE.intersection(usable_evidence)
    ):
        name = None
        parsed_state = "unable_to_confirm"
        rejection_reason = (
            "real-person/Cosplayer identity requires explicit text or metadata "
            "evidence; visual appearance alone cannot confirm identity"
        )
    elif (
        entity_type in {"character", "work"}
        and parsed_state == "confirmed"
        and confidence < ENTITY_CONFIRMATION_MIN_CONFIDENCE
    ):
        parsed_state = "suggested"
        rejection_reason = (
            f"confirmed confidence below {ENTITY_CONFIRMATION_MIN_CONFIDENCE:.2f}; "
            "downgraded to suggested"
        )
    return EntityAnnotation(
        name=name,
        state=parsed_state,
        evidence=usable_evidence or ("none",),
        evidence_text=evidence_text,
        confidence=confidence,
        rejection_reason=rejection_reason,
    )


def _evidence_is_available(
    source: str,
    *,
    name: str | None,
    evidence_text: str,
    context: TaggingContext,
) -> bool:
    if source in {"visual", "none"}:
        return True
    source_values: dict[str, str] = {
        "manual_tag": " ".join(context.manual_tags),
        "folder_name": context.folder_name,
        "file_name": context.file_name,
        "sidecar": json.dumps(
            _json_safe_mapping(context.sidecar_metadata),
            ensure_ascii=False,
            default=str,
        ),
        # OCR and watermark evidence must come from text that the application
        # actually supplied.  A model-provided quote alone is not evidence.
        "ocr": context.ocr_text,
        "watermark": context.watermark_text,
    }
    haystack = source_values.get(source, "")
    if not haystack:
        return False
    normalized_haystack = _comparison_key(haystack)
    if evidence_text and _comparison_key(evidence_text) in normalized_haystack:
        return True
    return bool(name and _comparison_key(name) in normalized_haystack)


def _extract_model_content(envelope: Mapping[str, Any]) -> str:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise VisionResponseError("API response contains no choices.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise VisionResponseError("API response choice must be an object.")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise VisionResponseError("API response contains no message.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        if text_parts:
            return "".join(text_parts)
    raise VisionResponseError("API response message content is not JSON text.")


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise VisionResponseError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise VisionResponseError(f"{label} must be a JSON object.")
    return value


def _read_limited(response: Any, max_bytes: int) -> bytes:
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise VisionResponseError(f"API response exceeds {max_bytes} bytes.")
    return body


def _safe_http_error_details(error: urllib.error.HTTPError) -> tuple[str, str]:
    try:
        body = json.loads(error.read(64 * 1024).decode("utf-8"))
        nested = body.get("error") if isinstance(body, dict) else None
        details = nested if isinstance(nested, dict) else body
        if not isinstance(details, dict):
            return "", str(error.reason)
        return (
            str(details.get("code") or ""),
            str(details.get("message") or error.reason),
        )
    except Exception:
        return "", str(error.reason)


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise VisionResponseError(f"{field_name} must be an array.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise VisionResponseError(f"{field_name} items must be strings.")
        result.append(item)
    return tuple(result)


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise VisionResponseError(f"{field_name} must be a string.")
    text = unicodedata.normalize("NFKC", value).strip()
    if any(ord(character) < 32 for character in text):
        raise VisionResponseError(f"{field_name} has control characters.")
    return text


def _optional_string(value: Any, field_name: str, max_length: int) -> str:
    text = _required_string(value, field_name)
    if len(text) > max_length:
        raise VisionResponseError(f"{field_name} exceeds {max_length} characters.")
    return text


def _required_confidence(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisionResponseError(f"{field_name} must be numeric.")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise VisionResponseError(f"{field_name} must be between 0 and 1.")
    return confidence


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _comparison_key(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return tuple(result)


def _strip_wrappers(value: str) -> str:
    return value.strip().strip("()[]{}（）【】<>《》").strip()


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise VisionResponseError(f"{field_name} must be a non-negative integer.")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise VisionResponseError(
            f"{field_name} must be a non-negative integer."
        ) from exc
    if converted < 0:
        raise VisionResponseError(f"{field_name} cannot be negative.")
    return converted


def _clean_context_text(value: Any, max_length: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(character for character in text if ord(character) >= 32)
    return text[:max_length]


def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VisionInputError("sidecar_metadata must be a mapping.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise VisionInputError(f"Cannot serialize sidecar metadata: {exc}") from exc
    if not isinstance(decoded, dict):
        raise VisionInputError("sidecar_metadata must serialize to an object.")
    return decoded
