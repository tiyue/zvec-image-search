from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from image_vector_service.config import RuntimeCredentials, ServiceConfig
from image_vector_service.dashscope_client import DashScopeEmbeddingClient
from image_vector_service.rate_limiter import (
    ModelRateLimiter,
    RateLimitCancelled,
    RateLimitConfig,
    get_process_rate_limiter,
    reset_process_rate_limiters,
    retry_after_seconds,
)
from image_vector_service.vision_tagging_client import (
    DashScopeVisionTaggingClient,
    VisionTaggingConfig,
    VisionTransportError,
)


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.value

    def wait(self, _condition: threading.Condition, timeout: float) -> None:
        self.waits.append(timeout)
        self.value += timeout


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return default


class FakeHttpErrorResponse:
    def __init__(
        self,
        status: int,
        payload: dict[str, object],
        retry_after: str | None = None,
    ) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self._retry_after = retry_after

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        if name == "Retry-After":
            return self._retry_after
        return default


class FakePooledConnection:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self._call_index = 0

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
    ) -> None:
        pass

    def getresponse(self):
        response = self._responses[self._call_index]
        self._call_index += 1
        return response

    def close(self) -> None:
        pass


def _http_error(
    status: int,
    *,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    headers = {"Content-Type": "application/json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    payload = json.dumps(
        {"code": "RateLimit" if status == 429 else "Rejected", "message": "retry"}
    ).encode("utf-8")
    return urllib.error.HTTPError(
        "https://example.invalid/model",
        status,
        "request failed",
        headers,
        BytesIO(payload),
    )


class ModelRateLimiterTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_process_rate_limiters()

    @staticmethod
    def _config(**overrides: int | float) -> RateLimitConfig:
        values: dict[str, int | float] = {
            "requests_per_minute": 48,
            "tokens_per_minute": 80_000,
            "hard_requests_per_minute": 60,
            "hard_tokens_per_minute": 100_000,
            "initial_concurrency": 60,
            "max_concurrency": 60,
        }
        values.update(overrides)
        return RateLimitConfig(**values)  # type: ignore[arg-type]

    def test_49th_request_waits_for_rolling_window_with_fake_clock(self) -> None:
        clock = FakeClock()
        limiter = ModelRateLimiter(
            "qwen3-vl-embedding",
            self._config(),
            clock=clock,
            wall_clock=clock,
            waiter=clock.wait,
        )

        for _ in range(48):
            limiter.acquire(1).succeed(1)
        limiter.acquire(1).succeed(1)

        snapshot = limiter.snapshot()
        self.assertEqual(clock.value, 60.0)
        self.assertEqual(snapshot.total_attempts, 49)
        self.assertEqual(snapshot.window_requests, 1)
        self.assertEqual(snapshot.total_wait_seconds, 60.0)

    def test_tpm_reservation_is_corrected_by_actual_usage(self) -> None:
        clock = FakeClock()
        limiter = ModelRateLimiter(
            "qwen3-vl-flash",
            self._config(
                requests_per_minute=60,
                tokens_per_minute=1_000,
                hard_tokens_per_minute=1_000,
            ),
            clock=clock,
            wall_clock=clock,
            waiter=clock.wait,
        )

        limiter.acquire(600).succeed(400)
        limiter.acquire(600).succeed(600)
        self.assertEqual(limiter.snapshot().window_tokens, 1_000)

        limiter.acquire(1).succeed(1)
        snapshot = limiter.snapshot()
        self.assertEqual(clock.value, 60.0)
        self.assertEqual(snapshot.window_tokens, 1)
        self.assertEqual(snapshot.total_actual_tokens, 1_001)

    def test_retry_after_http_date_cools_down_and_aimd_halves_concurrency(self) -> None:
        monotonic = FakeClock(10.0)
        wall_now = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
        limiter = ModelRateLimiter(
            "qwen3-vl-flash",
            self._config(initial_concurrency=8, max_concurrency=8),
            clock=monotonic,
            wall_clock=lambda: wall_now.timestamp(),
            waiter=monotonic.wait,
        )
        retry_at = format_datetime(wall_now + timedelta(seconds=30), usegmt=True)

        self.assertEqual(limiter.observe_429(retry_at), 30.0)
        throttled = limiter.snapshot()
        self.assertEqual(throttled.recommended_concurrency, 4)
        self.assertEqual(throttled.total_throttles, 1)
        self.assertEqual(throttled.cooldown_seconds, 30.0)

        limiter.acquire(1).succeed(1)
        self.assertEqual(monotonic.value, 40.0)
        self.assertEqual(limiter.snapshot().cooldown_seconds, 0.0)

    def test_wait_can_be_cancelled_without_consuming_a_request(self) -> None:
        clock = FakeClock()
        cancel_event = threading.Event()

        def wait_then_cancel(
            _condition: threading.Condition,
            timeout: float,
        ) -> None:
            clock.value += timeout
            cancel_event.set()

        limiter = ModelRateLimiter(
            "qwen3-vl-embedding",
            self._config(requests_per_minute=1),
            clock=clock,
            wall_clock=clock,
            waiter=wait_then_cancel,
        )
        limiter.acquire(1).succeed(1)

        with self.assertRaises(RateLimitCancelled):
            limiter.acquire(1, cancel_event)
        self.assertEqual(limiter.snapshot().total_attempts, 1)

    def test_concurrent_acquire_never_exceeds_recommended_concurrency(self) -> None:
        worker_count = 12
        limiter = ModelRateLimiter(
            "qwen3-vl-flash",
            self._config(
                requests_per_minute=60,
                initial_concurrency=3,
                max_concurrency=3,
            ),
        )
        start = threading.Barrier(worker_count + 1)
        release_first_wave = threading.Event()
        active_condition = threading.Condition()
        active = 0
        maximum_active = 0

        def worker() -> None:
            nonlocal active, maximum_active
            start.wait()
            permit = limiter.acquire(1)
            with active_condition:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 3:
                    active_condition.notify_all()
            release_first_wave.wait()
            with active_condition:
                active -= 1
            permit.succeed(1)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker) for _ in range(worker_count)]
            start.wait()
            with active_condition:
                self.assertTrue(
                    active_condition.wait_for(lambda: active == 3, timeout=2)
                )
            release_first_wave.set()
            for future in futures:
                future.result(timeout=2)

        self.assertEqual(maximum_active, 3)
        self.assertEqual(limiter.snapshot().total_attempts, worker_count)

    def test_process_registry_is_shared_per_model(self) -> None:
        first = get_process_rate_limiter("qwen3-vl-embedding", self._config())
        second = get_process_rate_limiter("qwen3-vl-embedding")
        flash = get_process_rate_limiter("qwen3-vl-flash", self._config())

        self.assertIs(first, second)
        self.assertIsNot(first, flash)

    def test_safety_watermarks_cannot_exceed_provider_hard_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "hard_requests_per_minute"):
            self._config(
                requests_per_minute=61,
                hard_requests_per_minute=60,
            )
        with self.assertRaisesRegex(ValueError, "hard_tokens_per_minute"):
            self._config(
                tokens_per_minute=100_001,
                hard_tokens_per_minute=100_000,
            )
        with self.assertRaisesRegex(ValueError, "provider limit of 60"):
            self._config(
                requests_per_minute=61,
                hard_requests_per_minute=61,
            )
        with self.assertRaisesRegex(ValueError, "provider limit of 100000"):
            self._config(
                tokens_per_minute=100_001,
                hard_tokens_per_minute=100_001,
            )

    def test_retry_after_parser_accepts_seconds_and_http_date(self) -> None:
        now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        http_date = format_datetime(now + timedelta(seconds=12), usegmt=True)
        self.assertEqual(retry_after_seconds("2.5", now=now.timestamp()), 2.5)
        self.assertEqual(retry_after_seconds(http_date, now=now.timestamp()), 12.0)
        self.assertIsNone(retry_after_seconds("not-a-date", now=now.timestamp()))


class RateLimitedClientTests(unittest.TestCase):
    def _limiter(self, model: str) -> ModelRateLimiter:
        return ModelRateLimiter(
            model,
            RateLimitConfig(
                requests_per_minute=10,
                tokens_per_minute=10_000,
                hard_requests_per_minute=10,
                hard_tokens_per_minute=10_000,
                initial_concurrency=2,
                max_concurrency=2,
            ),
        )

    def test_embedding_retry_reserves_a_permit_for_every_http_attempt(self) -> None:
        credentials = RuntimeCredentials()
        credentials.configure("test-key", "https://example.invalid/embed")
        limiter = self._limiter("qwen3-vl-embedding")
        client = DashScopeEmbeddingClient(
            ServiceConfig(
                workspace=Path(tempfile.gettempdir()),
                runtime_credentials=credentials,
                max_retries=1,
                retry_base_seconds=0,
            ),
            limiter=limiter,
        )
        success = FakeHttpResponse(
            {
                "output": {
                    "embeddings": [
                        {"index": 0, "embedding": [0.0] * client.config.dimension}
                    ]
                },
                "request_id": "after-retry",
                "usage": {"total_tokens": 12},
            }
        )
        error_response = FakeHttpErrorResponse(
            429,
            {"code": "RateLimit", "message": "retry"},
            retry_after="0",
        )
        fake_conn = FakePooledConnection([error_response, success])

        with patch.object(client, "_get_connection", return_value=fake_conn):
            response = client.embed_text("test")

        snapshot = limiter.snapshot()
        self.assertEqual(response.request_id, "after-retry")
        self.assertEqual(client.request_count, 2)
        self.assertEqual(snapshot.total_attempts, 2)
        self.assertEqual(snapshot.total_throttles, 1)
        self.assertEqual(snapshot.failed_attempts, 1)
        self.assertEqual(snapshot.successful_attempts, 1)

    def test_vision_retry_reserves_a_permit_for_every_http_attempt(self) -> None:
        limiter = self._limiter("qwen3-vl-flash")
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "image.png"
            Image.new("RGB", (2, 2), (128, 64, 192)).save(image_path)
            client = DashScopeVisionTaggingClient(
                VisionTaggingConfig(
                    api_url="https://example.invalid/chat",
                    api_key="test-key",
                    max_retries=1,
                    retry_base_seconds=0,
                ),
                limiter=limiter,
            )
            with (
                patch(
                    "image_vector_service.vision_tagging_client.urllib.request.urlopen",
                    side_effect=[
                        _http_error(429, retry_after="0"),
                        _http_error(400),
                    ],
                ),
                self.assertRaises(VisionTransportError),
            ):
                client.tag_image(image_path)

        snapshot = limiter.snapshot()
        self.assertEqual(client.request_count, 2)
        self.assertEqual(snapshot.total_attempts, 2)
        self.assertEqual(snapshot.total_throttles, 1)
        self.assertEqual(snapshot.failed_attempts, 2)


class ProcessingConfigurationTests(unittest.TestCase):
    def test_parallel_processing_defaults_and_validation(self) -> None:
        config = ServiceConfig(workspace=Path(tempfile.gettempdir()))
        config.validate()
        self.assertEqual(config.embedding_concurrency, 2)
        self.assertEqual(config.auto_tag_concurrency, 2)
        self.assertEqual(config.max_inflight_request_bytes, 96 * 1024 * 1024)

        with self.assertRaisesRegex(Exception, "embedding_concurrency"):
            ServiceConfig(
                workspace=Path(tempfile.gettempdir()),
                embedding_concurrency=0,
            ).validate()
        with self.assertRaisesRegex(Exception, "max_inflight_request_bytes"):
            ServiceConfig(
                workspace=Path(tempfile.gettempdir()),
                max_inflight_request_bytes=1,
            ).validate()


if __name__ == "__main__":
    unittest.main()
