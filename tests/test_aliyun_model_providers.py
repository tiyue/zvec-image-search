from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.annotation_service import (
    _annotation_failure_category,
    _vision_failure_kind,
)
from image_vector_service.auto_tagging_assets import ENTITY_TYPES, FIELD_SPECS
from image_vector_service.config import RuntimeCredentials, ServiceConfig
from image_vector_service.model_services.aliyun.embedding import (
    AliyunEmbeddingProvider,
    AliyunModelError,
)
from image_vector_service.model_services.aliyun.vision import (
    AliyunVisionTaggingProvider,
)
from image_vector_service.rate_limiter import RateLimitCancelled
from image_vector_service.vision_tagging_client import (
    TaggingContext,
    VisionResponseError,
    VisionTaggingConfig,
    VisionTaggingError,
    VisionTransportError,
)


class _Permit:
    def __init__(self) -> None:
        self.succeeded = 0
        self.failed = 0

    def succeed(self, _actual_tokens: int | None = None) -> None:
        self.succeeded += 1

    def fail(self) -> None:
        self.failed += 1


class _Limiter:
    def __init__(self, *, acquire_error: Exception | None = None) -> None:
        self.acquire_error = acquire_error
        self.permits: list[_Permit] = []
        self.retry_after: list[str | None] = []

    def acquire(self, *_args: Any, **_kwargs: Any) -> _Permit:
        if self.acquire_error is not None:
            raise self.acquire_error
        permit = _Permit()
        self.permits.append(permit)
        return permit

    def observe_429(self, value: str | None = None) -> None:
        self.retry_after.append(value)


class _EmbeddingHttpResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        retry_after: str | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.retry_after if name == "Retry-After" else default


class _EmbeddingConnection:
    def __init__(
        self,
        responses: list[_EmbeddingHttpResponse],
        *,
        request_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.request_error = request_error
        self.requests = 0

    def request(self, *_args: Any, **_kwargs: Any) -> None:
        self.requests += 1
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> _EmbeddingHttpResponse:
        return self.responses.pop(0)


class _VisionHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _VisionHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _embedding_success(dimension: int, *, index: int = 0) -> bytes:
    return _json_bytes(
        {
            "output": {
                "embeddings": [
                    {"index": index, "embedding": [0.0] * dimension},
                ]
            },
            "request_id": "embedding-request",
            "usage": {"total_tokens": 3},
        }
    )


def _vision_success() -> bytes:
    annotation = {
        "schema_version": 2,
        "description": "离线测试",
        "fields": {name: {"values": [], "confidence": 0.9} for name in FIELD_SPECS},
        "entities": {name: [] for name in ENTITY_TYPES},
    }
    return _json_bytes(
        {
            "id": "vision-request",
            "choices": [
                {"message": {"content": json.dumps(annotation, ensure_ascii=False)}}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
    )


def _http_error(
    status: int,
    *,
    code: str,
    message: str,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    headers = Message()
    headers["Content-Type"] = "application/json"
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://provider.invalid/vision",
        status,
        message,
        headers,
        BytesIO(_json_bytes({"code": code, "message": message})),
    )


class _NoExternalNetworkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        guard = patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError(
                "external network is disabled in provider tests"
            ),
        )
        guard.start()
        self.addCleanup(guard.stop)


class ProviderCategoryBoundaryTests(unittest.TestCase):
    def test_business_failure_policy_consumes_category_not_provider_text(self) -> None:
        cases = (
            ("authentication", "systemic", "provider_auth"),
            ("content_policy", "item", "content_policy"),
            ("invalid_response", "item", "schema_response"),
            ("input", "item", "invalid_input"),
            ("rate_limit", "retryable", "retryable_transport"),
            ("transient", "retryable", "retryable_transport"),
            ("cancelled", "retryable", "retryable_transport"),
            ("configuration", "systemic", "systemic"),
        )
        for category, expected_kind, expected_policy in cases:
            with self.subTest(category=category):
                error = VisionTaggingError(
                    "opaque-provider-message",
                    category=category,
                )
                kind = _vision_failure_kind(error)
                policy = _annotation_failure_category(
                    str(error),
                    kind,
                    error.category,
                )
                self.assertEqual(kind, expected_kind)
                self.assertEqual(policy, expected_policy)


class AliyunEmbeddingProviderTests(_NoExternalNetworkTestCase):
    def _client(
        self,
        directory: str,
        *,
        max_retries: int = 0,
        limiter: _Limiter | None = None,
    ) -> AliyunEmbeddingProvider:
        credentials = RuntimeCredentials()
        credentials.configure("offline-key", "https://provider.invalid/embedding")
        return AliyunEmbeddingProvider(
            ServiceConfig(
                workspace=Path(directory),
                runtime_credentials=credentials,
                max_retries=max_retries,
                retry_base_seconds=0,
            ),
            limiter=limiter,
        )

    def test_http_error_categories_and_attempts(self) -> None:
        cases = (
            (401, "authentication"),
            (403, "authentication"),
            (429, "rate_limit"),
            (500, "transient"),
            (503, "transient"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for status, expected in cases:
                with self.subTest(status=status):
                    retries = 1 if status in {429, 500, 503} else 0
                    limiter = _Limiter()
                    body = _json_bytes(
                        {"code": "ProviderError", "message": "offline failure"}
                    )
                    connection = _EmbeddingConnection(
                        [
                            _EmbeddingHttpResponse(status, body, retry_after="0")
                            for _attempt in range(retries + 1)
                        ]
                    )
                    client = self._client(
                        directory,
                        max_retries=retries,
                        limiter=limiter,
                    )
                    with (
                        patch.object(
                            client,
                            "_get_connection",
                            return_value=connection,
                        ),
                        self.assertRaises(AliyunModelError) as raised,
                    ):
                        client.embed_text("offline query")

                    self.assertEqual(raised.exception.category, expected)
                    self.assertEqual(raised.exception.attempts, retries + 1)
                    self.assertEqual(connection.requests, retries + 1)
                    if status == 429:
                        self.assertEqual(limiter.retry_after, ["0", "0"])

    def test_timeout_disconnect_invalid_json_and_response_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for failure in (TimeoutError("timed out"), OSError("disconnected")):
                with self.subTest(failure=failure.__class__.__name__):
                    client = self._client(directory, max_retries=1, limiter=_Limiter())
                    connection = _EmbeddingConnection([], request_error=failure)
                    with (
                        patch.object(
                            client,
                            "_get_connection",
                            return_value=connection,
                        ),
                        self.assertRaises(AliyunModelError) as raised,
                    ):
                        client.embed_text("offline query")
                    self.assertEqual(raised.exception.category, "transient")
                    self.assertEqual(raised.exception.attempts, 2)

            invalid_bodies = (
                b"not-json",
                b"{" + b"x" * (2 * 1024 * 1024 + 1),
            )
            for body in invalid_bodies:
                with self.subTest(size=len(body)):
                    client = self._client(directory, limiter=_Limiter())
                    connection = _EmbeddingConnection(
                        [_EmbeddingHttpResponse(200, body)]
                    )
                    with (
                        patch.object(
                            client,
                            "_get_connection",
                            return_value=connection,
                        ),
                        self.assertRaises(AliyunModelError) as raised,
                    ):
                        client.embed_text("offline query")
                    self.assertEqual(raised.exception.category, "invalid_response")
                    self.assertEqual(raised.exception.attempts, 1)

    def test_embedding_schema_rejects_bad_indices_vectors_and_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(directory, limiter=_Limiter())
            dimension = client.config.dimension
            cases: tuple[dict[str, Any], ...] = (
                {"output": {"embeddings": []}},
                {"output": {"embeddings": [{"index": 0, "embedding": [0.0]}]}},
                {
                    "output": {
                        "embeddings": [{"index": "0", "embedding": [0.0] * dimension}]
                    }
                },
                {
                    "output": {
                        "embeddings": [{"index": 1, "embedding": [0.0] * dimension}]
                    }
                },
                {
                    "output": {
                        "embeddings": [
                            {"index": 0, "embedding": [float("nan")] * dimension}
                        ]
                    }
                },
                {
                    "output": {
                        "embeddings": [
                            {"index": 0, "embedding": [float("inf")] * dimension}
                        ]
                    }
                },
                {
                    "output": {
                        "embeddings": [
                            {"index": 0, "embedding": [object()] * dimension}
                        ]
                    }
                },
            )
            for body in cases:
                with (
                    self.subTest(body=body),
                    patch.object(client, "_post_json", return_value=body),
                    self.assertRaises(AliyunModelError) as raised,
                ):
                    client.embed_text("offline query")
                self.assertEqual(raised.exception.category, "invalid_response")

    def test_retry_success_and_pre_request_cancellation_report_exact_attempts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            limiter = _Limiter()
            client = self._client(directory, max_retries=1, limiter=limiter)
            connection = _EmbeddingConnection(
                [
                    _EmbeddingHttpResponse(
                        429,
                        _json_bytes({"code": "RateLimit", "message": "retry"}),
                        retry_after="0",
                    ),
                    _EmbeddingHttpResponse(
                        200,
                        _embedding_success(client.config.dimension),
                    ),
                ]
            )
            with patch.object(client, "_get_connection", return_value=connection):
                response = client.embed_text("offline query")
            self.assertEqual(response.attempts, 2)

            cancelled = self._client(
                directory,
                limiter=_Limiter(acquire_error=RateLimitCancelled("cancelled")),
            )
            with self.assertRaises(AliyunModelError) as raised:
                cancelled.embed_text("offline query")
            self.assertEqual(raised.exception.category, "cancelled")
            self.assertEqual(raised.exception.attempts, 0)


class AliyunVisionProviderTests(_NoExternalNetworkTestCase):
    def _image(self, directory: str) -> Path:
        path = Path(directory) / "image.png"
        Image.new("RGB", (2, 2), (20, 40, 60)).save(path)
        return path

    def _client(
        self,
        *,
        max_retries: int = 0,
        max_response_bytes: int = 2 * 1024 * 1024,
        limiter: _Limiter | None = None,
    ) -> AliyunVisionTaggingProvider:
        return AliyunVisionTaggingProvider(
            VisionTaggingConfig(
                api_url="https://provider.invalid/vision",
                api_key="offline-key",
                max_retries=max_retries,
                retry_base_seconds=0,
                max_response_bytes=max_response_bytes,
            ),
            limiter=limiter,
        )

    def test_provider_error_matrix_uses_stable_categories(self) -> None:
        cases = (
            (401, "InvalidApiKey", "invalid API key", "authentication"),
            (403, "Forbidden", "permission denied", "authentication"),
            (
                400,
                "DataInspectionFailed",
                "input contains inappropriate content",
                "content_policy",
            ),
            (429, "RateLimit", "too many requests", "rate_limit"),
            (503, "Unavailable", "temporarily unavailable", "transient"),
        )
        with tempfile.TemporaryDirectory() as directory:
            image_path = self._image(directory)
            for status, code, message, expected in cases:
                with self.subTest(status=status, code=code):
                    client = self._client(limiter=_Limiter())
                    error = _http_error(status, code=code, message=message)
                    with (
                        patch(
                            "image_vector_service.model_services.aliyun.vision."
                            "urllib.request.urlopen",
                            side_effect=error,
                        ),
                        self.assertRaises(VisionTransportError) as raised,
                    ):
                        client.tag_image(image_path)
                    self.assertEqual(raised.exception.category, expected)
                    self.assertEqual(raised.exception.attempts, 1)

    def test_timeout_invalid_json_oversize_and_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = self._image(directory)
            retrying = self._client(max_retries=1, limiter=_Limiter())
            with patch(
                "image_vector_service.model_services.aliyun.vision."
                "urllib.request.urlopen",
                side_effect=[
                    TimeoutError("timed out"),
                    _VisionHttpResponse(_vision_success()),
                ],
            ):
                response = retrying.tag_image(image_path)
            self.assertEqual(response.attempts, 2)

            malformed = self._client(limiter=_Limiter())
            with (
                patch(
                    "image_vector_service.model_services.aliyun.vision."
                    "urllib.request.urlopen",
                    return_value=_VisionHttpResponse(b"not-json"),
                ),
                self.assertRaises(VisionResponseError) as invalid,
            ):
                malformed.tag_image(image_path)
            self.assertEqual(invalid.exception.category, "invalid_response")
            self.assertEqual(invalid.exception.attempts, 1)

            oversized = self._client(max_response_bytes=128, limiter=_Limiter())
            with (
                patch(
                    "image_vector_service.model_services.aliyun.vision."
                    "urllib.request.urlopen",
                    return_value=_VisionHttpResponse(b"x" * 129),
                ),
                self.assertRaises(VisionResponseError) as too_large,
            ):
                oversized.tag_image(image_path)
            self.assertEqual(too_large.exception.category, "invalid_response")
            self.assertEqual(too_large.exception.attempts, 1)

            cancelled = self._client(
                limiter=_Limiter(acquire_error=RateLimitCancelled("cancelled"))
            )
            with self.assertRaises(VisionTaggingError) as stopped:
                cancelled.tag_image(image_path, context=TaggingContext())
            self.assertEqual(stopped.exception.category, "cancelled")
            self.assertEqual(stopped.exception.attempts, 0)

    def test_legacy_dashscope_environment_names_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = self._image(directory)
            client = AliyunVisionTaggingProvider(
                VisionTaggingConfig(max_retries=0),
                limiter=_Limiter(),
            )
            with (
                patch.dict(
                    "os.environ",
                    {
                        "DASHSCOPE_API_KEY": "environment-key",
                        "DASHSCOPE_VISION_API_URL": (
                            "https://provider.invalid/environment-vision"
                        ),
                    },
                    clear=True,
                ),
                patch(
                    "image_vector_service.model_services.aliyun.vision."
                    "urllib.request.urlopen",
                    return_value=_VisionHttpResponse(_vision_success()),
                ) as urlopen,
            ):
                client.tag_image(image_path)

            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://provider.invalid/environment-vision",
            )
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer environment-key",
            )


if __name__ == "__main__":
    unittest.main()
