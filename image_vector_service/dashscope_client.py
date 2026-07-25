from __future__ import annotations

import contextlib
import http.client
import json
import math
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ServiceConfig
from .image_data_uri import ImageDataUriError, encode_image_data_uri
from .image_scanner import SUPPORTED_EXTENSIONS
from .rate_limiter import (
    ModelRateLimiter,
    RateLimitCancelled,
    RateLimitConfig,
    get_process_rate_limiter,
    retry_after_seconds,
)


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    request_id: str
    usage: dict[str, Any]


class DashScopeError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "",
        splittable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.splittable = splittable


class ImageInputError(ValueError):
    splittable = True


class DashScopeEmbeddingClient:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.config.validate()
        limiter_config = RateLimitConfig(
            requests_per_minute=config.rate_limit_requests_per_minute,
            tokens_per_minute=config.rate_limit_tokens_per_minute,
            hard_requests_per_minute=config.rate_limit_hard_requests_per_minute,
            hard_tokens_per_minute=config.rate_limit_hard_tokens_per_minute,
            initial_concurrency=config.embedding_concurrency,
            max_concurrency=config.rate_limit_max_concurrency,
        )
        self.limiter = limiter or get_process_rate_limiter(config.model, limiter_config)
        self.cancel_event = cancel_event
        self.request_count = 0
        self._request_count_lock = threading.Lock()
        self._conn_local = threading.local()

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        if not image_paths:
            return EmbeddingResponse([], "", {})
        if len(image_paths) > self.config.batch_size:
            raise ValueError(
                f"A batch can contain at most {self.config.batch_size} images."
            )

        contents = self._encode_image_contents(image_paths)
        try:
            return self._embed(contents)
        except DashScopeError as exc:
            if not self._is_image_size_error(exc):
                raise
            # Provider-side limits can change independently of the client. Make
            # one deliberately smaller second representation before surfacing
            # the item failure. The source file and its SHA identity stay intact.
            retry_limit = min(self.config.max_image_bytes, 7 * 1024 * 1024)
            contents = self._encode_image_contents(
                image_paths,
                max_data_uri_bytes=retry_limit,
                max_output_dimension=3072,
                force_transcode=True,
            )
            return self._embed(contents)

    def _encode_image_contents(
        self,
        image_paths: list[Path],
        *,
        max_data_uri_bytes: int | None = None,
        max_output_dimension: int = 4096,
        force_transcode: bool = False,
    ) -> list[dict[str, str]]:
        contents: list[dict[str, str]] = []
        for path in image_paths:
            extension = path.suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise ImageInputError(f"Unsupported image extension: {extension}")
            try:
                encoded = encode_image_data_uri(
                    path,
                    max_source_bytes=self.config.max_source_image_bytes,
                    max_data_uri_bytes=(
                        self.config.max_image_bytes
                        if max_data_uri_bytes is None
                        else max_data_uri_bytes
                    ),
                    max_output_dimension=max_output_dimension,
                    force_transcode=force_transcode,
                )
            except ImageDataUriError as exc:
                raise ImageInputError(str(exc)) from exc
            contents.append({"image": encoded.data_uri})
        return contents

    def embed_text(self, text: str) -> EmbeddingResponse:
        text = text.strip()
        if not text:
            raise ValueError("Search text cannot be empty.")
        return self._embed([{"text": text}])

    def _embed(self, contents: list[dict[str, str]]) -> EmbeddingResponse:
        payload = {
            "model": self.config.model,
            "input": {"contents": contents},
            "parameters": {
                "output_type": "dense",
                "dimension": self.config.dimension,
                "enable_fusion": False,
            },
        }
        body = self._post_json(
            payload,
            estimated_tokens=self._estimate_tokens(contents),
        )
        output = body.get("output") or {}
        embeddings = output.get("embeddings") or []
        if len(embeddings) != len(contents):
            raise DashScopeError(
                f"Expected {len(contents)} embeddings, received {len(embeddings)}."
            )

        embeddings = sorted(embeddings, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in embeddings:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.config.dimension:
                actual = len(vector) if isinstance(vector, list) else "unknown"
                raise DashScopeError(
                    f"Expected vector dimension {self.config.dimension}, "
                    f"received {actual}."
                )
            converted = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in converted):
                raise DashScopeError("Embedding contains NaN or infinite values.")
            vectors.append(converted)

        return EmbeddingResponse(
            vectors=vectors,
            request_id=str(body.get("request_id") or output.get("request_id") or ""),
            usage=dict(body.get("usage") or {}),
        )

    def _get_connection(
        self, scheme: str, host: str, port: int
    ) -> http.client.HTTPConnection:
        """Return a thread-local pooled HTTP(S) connection."""
        conn = getattr(self._conn_local, "conn", None)
        conn_key = getattr(self._conn_local, "key", None)
        key = (scheme, host, port)
        if conn is None or conn_key != key:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, port, timeout=self.config.timeout_seconds
                )
            else:
                conn = http.client.HTTPConnection(
                    host, port, timeout=self.config.timeout_seconds
                )
            self._conn_local.conn = conn
            self._conn_local.key = key
        return conn

    def _post_json(
        self,
        payload: dict[str, Any],
        *,
        estimated_tokens: int,
    ) -> dict[str, Any]:
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(request_data) > self.config.max_request_bytes:
            raise ImageInputError(
                f"Request payload exceeds {self.config.max_request_bytes} bytes."
            )
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            api_key = self.config.require_api_key()
            retry_after: str | None = None
            parsed = urlsplit(self.config.api_url)
            scheme = parsed.scheme or "https"
            host = parsed.hostname or "dashscope.aliyuncs.com"
            port = parsed.port or (443 if scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "zvec-local-image-service/1.0",
            }
            permit = self.limiter.acquire(estimated_tokens, self.cancel_event)
            with self._request_count_lock:
                self.request_count += 1
            try:
                conn = self._get_connection(scheme, host, port)
                conn.request("POST", path, body=request_data, headers=headers)
                response = conn.getresponse()
                response_data = response.read()
                status = response.status
                if status >= 400:
                    permit.fail()
                    retry_after = response.getheader("Retry-After")
                    code, message = self._parse_error_body(response_data)
                    splittable = self._is_splittable_input_error(status, code, message)
                    last_error = DashScopeError(
                        f"DashScope {code or 'HTTPError'}: {message}",
                        status_code=status,
                        code=code,
                        splittable=splittable,
                    )
                    if status not in {408, 409, 429, 500, 502, 503, 504}:
                        raise last_error
                    if status == 429:
                        self.limiter.observe_429(retry_after)
                        retry_after = None
                else:
                    try:
                        decoded = json.loads(response_data.decode("utf-8"))
                        if not isinstance(decoded, dict):
                            raise ValueError("API response must be a JSON object.")
                        body = dict(decoded)
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as exc:
                        permit.fail()
                        raise DashScopeError(
                            f"DashScope returned an invalid JSON response: {exc}"
                        ) from exc
                    permit.succeed(_total_tokens_from_usage(body.get("usage")))
                    return body
            except DashScopeError:
                raise
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                permit.fail()
                # Connection may be stale; force reconnect on next attempt.
                self._conn_local.conn = None
                last_error = DashScopeError(f"DashScope request failed: {exc}")
                retry_after = None
            except Exception:
                permit.fail()
                raise

            if attempt < self.config.max_retries:
                delay = self.config.retry_base_seconds * (2**attempt)
                provider_delay = retry_after_seconds(retry_after)
                if provider_delay is not None:
                    delay = max(delay, provider_delay)
                self._sleep_before_retry(delay + random.uniform(0, delay * 0.2))

        assert last_error is not None
        raise last_error

    def _estimate_tokens(self, contents: list[dict[str, str]]) -> int:
        estimate = 0
        for content in contents:
            if "image" in content:
                estimate += self.config.embedding_estimated_tokens_per_image
                continue
            text = content.get("text", "")
            character_estimate = max(1, math.ceil(len(text) / 2))
            estimate += max(
                self.config.embedding_estimated_tokens_per_text,
                character_estimate,
            )
        return max(1, estimate)

    def _sleep_before_retry(self, delay: float) -> None:
        if delay <= 0:
            return
        if self.cancel_event is not None:
            if self.cancel_event.wait(delay):
                raise RateLimitCancelled(
                    "The embedding request was cancelled before retrying."
                )
            return
        time.sleep(delay)

    @staticmethod
    def _parse_error_body(data: bytes) -> tuple[str, str]:
        try:
            body = json.loads(data.decode("utf-8"))
            code = str(body.get("code") or "")
            message = str(body.get("message") or "")
            return code, message
        except Exception:
            return "", "request failed"

    @staticmethod
    def _is_splittable_input_error(status: int, code: str, message: str) -> bool:
        if status not in {400, 413, 422}:
            return False
        text = f"{code} {message}".lower()
        return any(
            token in text
            for token in ("image", "media", "file", "content", "base64", "format")
        )

    @staticmethod
    def _is_image_size_error(error: DashScopeError) -> bool:
        if error.status_code not in {400, 413, 422}:
            return False
        text = f"{error.code} {error}".casefold()
        size_tokens = (
            "image size should be",
            "file size is too large",
            "multimodal file size",
            "max bytes per data-uri",
            "data uri item",
            "data-uri item",
            "10240kb",
            "10485760",
        )
        return any(token in text for token in size_tokens)


def _total_tokens_from_usage(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for name in ("total_tokens", "total_token"):
        total = _non_negative_int_or_none(value.get(name))
        if total is not None:
            return total
    input_tokens = _first_usage_int(
        value,
        ("input_tokens", "prompt_tokens", "input_token"),
    )
    output_tokens = _first_usage_int(
        value,
        ("output_tokens", "completion_tokens", "output_token"),
    )
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def _first_usage_int(value: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        parsed = _non_negative_int_or_none(value.get(name))
        if parsed is not None:
            return parsed
    return None


def _non_negative_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None
