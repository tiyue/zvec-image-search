from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...auto_tagging_assets import SYSTEM_PROMPT
from ...image_data_uri import (
    ImageDataUriError,
    ImageSourceChangedError,
    encode_image_data_uri,
)
from ...rate_limiter import (
    ModelRateLimiter,
    RateLimitCancelled,
    RateLimitCapacityError,
    RateLimitConfig,
    RateLimitTimeout,
    get_process_rate_limiter,
    retry_after_seconds,
)
from ..contracts import ProviderErrorCategory
from ..tagging import (
    TaggingBudgetTracker,
    TaggingContext,
    VisionConfigurationError,
    VisionInputError,
    VisionResponseError,
    VisionSourceChangedError,
    VisionTaggingConfig,
    VisionTaggingError,
    VisionTaggingResponse,
    VisionTransportError,
    VisionUsage,
    parse_model_annotation_json,
)

DEFAULT_VISION_API_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)
RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_SECONDARY_IMAGE_DATA_URI_TARGET = 7 * 1024 * 1024


class AliyunVisionTaggingProvider:
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
        self._call_local = threading.local()

    def diagnostic_snapshot(self) -> dict[str, object]:
        return dict(self.limiter.snapshot().as_dict())

    def _resolved_api_url(self) -> str:
        return (
            (self.config.api_url or "").strip()
            or os.getenv("DASHSCOPE_VISION_API_URL", "").strip()
            or DEFAULT_VISION_API_URL
        )

    def _require_api_key(self) -> str:
        api_key = (self.config.api_key or "").strip() or os.getenv(
            "DASHSCOPE_API_KEY", ""
        ).strip()
        if not api_key:
            raise VisionConfigurationError(
                "DASHSCOPE_API_KEY is required for visual auto-tagging."
            )
        return api_key

    def tag_image(
        self,
        image_path: Path,
        *,
        context: TaggingContext | None = None,
    ) -> VisionTaggingResponse:
        self._call_local.attempts = 0
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
                self._resolved_api_url(),
                data=request_data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._require_api_key()}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(request_data)),
                    "User-Agent": "zvec-visual-auto-tagger/1.0",
                },
            )
            try:
                permit = self.limiter.acquire(
                    estimated_tokens,
                    self.cancel_event,
                    max_wait_seconds=self.config.timeout_seconds,
                )
            except RateLimitCancelled as exc:
                raise VisionTaggingError(
                    str(exc),
                    category="cancelled",
                    attempts=max(0, int(getattr(self._call_local, "attempts", 0))),
                ) from exc
            except RateLimitTimeout as exc:
                raise VisionTransportError(
                    str(exc),
                    category="transient",
                    attempts=max(0, int(getattr(self._call_local, "attempts", 0))),
                ) from exc
            except RateLimitCapacityError as exc:
                raise VisionConfigurationError(str(exc)) from exc
            call_attempts = int(getattr(self._call_local, "attempts", 0)) + 1
            self._call_local.attempts = call_attempts
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
                    category=_aliyun_error_category(status_code, code, message),
                    attempts=call_attempts,
                )
                if status_code not in RETRYABLE_HTTP_STATUSES:
                    raise last_error from exc
                if status_code == 429:
                    self.limiter.observe_429(retry_after)
                    retry_after = None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                permit.fail()
                last_error = VisionTransportError(
                    f"DashScope visual tagging request failed: {exc}",
                    category="transient",
                    attempts=call_attempts,
                )
            except VisionResponseError as exc:
                permit.fail()
                exc.attempts = call_attempts
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
                    exc.attempts = call_attempts
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
                        attempts=call_attempts,
                    )

            if attempt < self.config.max_retries:
                delay = self.config.retry_base_seconds * (2**attempt)
                provider_delay = retry_after_seconds(retry_after)
                if provider_delay is not None:
                    delay = max(delay, provider_delay)
                try:
                    self._sleep_before_retry(delay + random.uniform(0, delay * 0.2))
                except RateLimitCancelled as exc:
                    raise VisionTaggingError(
                        str(exc),
                        category="cancelled",
                        attempts=max(
                            0,
                            int(getattr(self._call_local, "attempts", 0)),
                        ),
                    ) from exc

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


def _aliyun_error_category(
    status_code: int | None,
    code: str = "",
    message: str = "",
) -> ProviderErrorCategory:
    text = f"{code} {message}".casefold()
    if any(
        marker in text
        for marker in (
            "data_inspection_failed",
            "datainspectionfailed",
            "inappropriate content",
            "content policy",
        )
    ):
        return "content_policy"
    if status_code in {401, 403} or any(
        marker in text
        for marker in (
            "invalid api key",
            "invalidapikey",
            "unauthorized",
            "authentication",
            "permission denied",
        )
    ):
        return "authentication"
    if status_code == 429:
        return "rate_limit"
    if status_code in RETRYABLE_HTTP_STATUSES or status_code is None:
        return "transient"
    if status_code in {400, 413, 422}:
        return "input"
    return "unknown"


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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


__all__ = ["AliyunVisionTaggingProvider"]
