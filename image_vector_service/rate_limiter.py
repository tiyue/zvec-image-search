"""Thread-safe, process-wide rate limiting for DashScope model requests.

The limiter deliberately owns no database or collection state.  It only tracks
HTTP attempts, token reservations and in-flight calls, so it is safe to share
between independent Collections and worker threads.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


class RateLimitError(RuntimeError):
    """Base error for local model rate limiting."""


class RateLimitCancelled(RateLimitError):
    """Raised when a caller cancels while waiting for a request permit."""


class RateLimitCapacityError(RateLimitError):
    """Raised when one request cannot fit inside the configured token window."""


DASHSCOPE_PROVIDER_MAX_REQUESTS_PER_MINUTE = 60
DASHSCOPE_PROVIDER_MAX_TOKENS_PER_MINUTE = 100_000


@dataclass(frozen=True)
class RateLimitConfig:
    """Active safety watermarks and provider hard ceilings for one model."""

    requests_per_minute: int = 48
    tokens_per_minute: int = 80_000
    hard_requests_per_minute: int = 60
    hard_tokens_per_minute: int = 100_000
    window_seconds: float = 60.0
    initial_concurrency: int = 2
    max_concurrency: int = 6
    additive_increase_every: int = 8
    cancellation_poll_seconds: float = 0.1

    def __post_init__(self) -> None:
        integer_fields = (
            "requests_per_minute",
            "tokens_per_minute",
            "hard_requests_per_minute",
            "hard_tokens_per_minute",
            "initial_concurrency",
            "max_concurrency",
            "additive_increase_every",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.requests_per_minute > self.hard_requests_per_minute:
            raise ValueError(
                "requests_per_minute cannot exceed hard_requests_per_minute."
            )
        if self.tokens_per_minute > self.hard_tokens_per_minute:
            raise ValueError("tokens_per_minute cannot exceed hard_tokens_per_minute.")
        if self.hard_requests_per_minute > DASHSCOPE_PROVIDER_MAX_REQUESTS_PER_MINUTE:
            raise ValueError(
                "hard_requests_per_minute cannot exceed the DashScope provider "
                f"limit of {DASHSCOPE_PROVIDER_MAX_REQUESTS_PER_MINUTE}."
            )
        if self.hard_tokens_per_minute > DASHSCOPE_PROVIDER_MAX_TOKENS_PER_MINUTE:
            raise ValueError(
                "hard_tokens_per_minute cannot exceed the DashScope provider "
                f"limit of {DASHSCOPE_PROVIDER_MAX_TOKENS_PER_MINUTE}."
            )
        if self.initial_concurrency > self.max_concurrency:
            raise ValueError("initial_concurrency cannot exceed max_concurrency.")
        for name in ("window_seconds", "cancellation_poll_seconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")


@dataclass(frozen=True)
class RateLimitSnapshot:
    """A stable diagnostics view suitable for logs or a task monitor."""

    model: str
    window_requests: int
    window_tokens: int
    in_flight: int
    recommended_concurrency: int
    cooldown_seconds: float
    total_attempts: int
    total_reserved_tokens: int
    total_actual_tokens: int
    total_waits: int
    total_wait_seconds: float
    total_throttles: int
    successful_attempts: int
    failed_attempts: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _WindowEvent:
    timestamp: float
    tokens: int


Waiter = Callable[[threading.Condition, float], None]


def _condition_wait(condition: threading.Condition, timeout: float) -> None:
    condition.wait(timeout=timeout)


class RateLimitPermit:
    """One reserved HTTP attempt; complete it exactly once."""

    def __init__(self, limiter: ModelRateLimiter, event: _WindowEvent) -> None:
        self._limiter = limiter
        self._event = event
        self._finished = False
        self._lock = threading.Lock()

    def succeed(self, actual_tokens: int | None = None) -> None:
        """Release the in-flight slot and reconcile estimated token usage."""

        self._finish(success=True, actual_tokens=actual_tokens)

    def fail(self) -> None:
        """Release the slot while retaining the conservative token reservation."""

        self._finish(success=False, actual_tokens=None)

    def _finish(self, *, success: bool, actual_tokens: int | None) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("A rate-limit permit can only be completed once.")
            self._finished = True
        self._limiter._finish(self._event, success, actual_tokens)


class ModelRateLimiter:
    """Rolling RPM/TPM limiter with a small AIMD concurrency controller."""

    def __init__(
        self,
        model: str,
        config: RateLimitConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        waiter: Waiter = _condition_wait,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("model must not be empty.")
        self.model = normalized_model
        self.config = config or RateLimitConfig()
        self._clock = clock
        self._wall_clock = wall_clock
        self._waiter = waiter
        self._condition = threading.Condition(threading.Lock())
        self._events: deque[_WindowEvent] = deque()
        self._in_flight = 0
        self._recommended_concurrency = self.config.initial_concurrency
        self._successes_since_increase = 0
        self._cooldown_until = 0.0
        self._total_attempts = 0
        self._total_reserved_tokens = 0
        self._total_actual_tokens = 0
        self._total_waits = 0
        self._total_wait_seconds = 0.0
        self._total_throttles = 0
        self._successful_attempts = 0
        self._failed_attempts = 0

    def acquire(
        self,
        estimated_tokens: int,
        cancel_event: threading.Event | None = None,
    ) -> RateLimitPermit:
        """Wait for RPM, TPM, cooldown and concurrency capacity, then reserve it."""

        if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int):
            raise TypeError("estimated_tokens must be an integer.")
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens cannot be negative.")
        if estimated_tokens > self.config.tokens_per_minute:
            raise RateLimitCapacityError(
                f"One {self.model} request reserves {estimated_tokens} tokens, "
                f"above the {self.config.tokens_per_minute}-token window."
            )

        with self._condition:
            while True:
                self._raise_if_cancelled(cancel_event)
                now = self._clock()
                self._prune(now)
                wait_seconds = self._required_wait(now, estimated_tokens)
                if wait_seconds <= 0:
                    event = _WindowEvent(now, estimated_tokens)
                    self._events.append(event)
                    self._in_flight += 1
                    self._total_attempts += 1
                    self._total_reserved_tokens += estimated_tokens
                    return RateLimitPermit(self, event)

                wait_slice = max(wait_seconds, 0.001)
                if cancel_event is not None:
                    wait_slice = min(
                        wait_slice,
                        self.config.cancellation_poll_seconds,
                    )
                before = self._clock()
                self._total_waits += 1
                self._waiter(self._condition, wait_slice)
                elapsed = max(0.0, self._clock() - before)
                self._total_wait_seconds += elapsed

    def observe_429(self, retry_after: str | float | int | None = None) -> float:
        """Apply provider cooldown and multiplicatively reduce concurrency."""

        delay = retry_after_seconds(retry_after, now=self._wall_clock()) or 0.0
        with self._condition:
            now = self._clock()
            self._cooldown_until = max(self._cooldown_until, now + delay)
            self._recommended_concurrency = max(1, self._recommended_concurrency // 2)
            self._successes_since_increase = 0
            self._total_throttles += 1
            self._condition.notify_all()
        return delay

    def snapshot(self) -> RateLimitSnapshot:
        with self._condition:
            now = self._clock()
            self._prune(now)
            return RateLimitSnapshot(
                model=self.model,
                window_requests=len(self._events),
                window_tokens=sum(item.tokens for item in self._events),
                in_flight=self._in_flight,
                recommended_concurrency=self._recommended_concurrency,
                cooldown_seconds=max(0.0, self._cooldown_until - now),
                total_attempts=self._total_attempts,
                total_reserved_tokens=self._total_reserved_tokens,
                total_actual_tokens=self._total_actual_tokens,
                total_waits=self._total_waits,
                total_wait_seconds=self._total_wait_seconds,
                total_throttles=self._total_throttles,
                successful_attempts=self._successful_attempts,
                failed_attempts=self._failed_attempts,
            )

    def _required_wait(self, now: float, estimated_tokens: int) -> float:
        waits = [max(0.0, self._cooldown_until - now)]
        if self._in_flight >= self._recommended_concurrency:
            # A completion notification normally wakes this early.  The bounded
            # poll also makes cancellation observable if a request gets stuck.
            waits.append(self.config.cancellation_poll_seconds)
        if len(self._events) >= self.config.requests_per_minute:
            waits.append(
                max(
                    0.0,
                    self._events[0].timestamp + self.config.window_seconds - now,
                )
            )

        used_tokens = sum(item.tokens for item in self._events)
        excess = used_tokens + estimated_tokens - self.config.tokens_per_minute
        if excess > 0:
            released = 0
            for item in self._events:
                released += item.tokens
                if released >= excess:
                    waits.append(
                        max(
                            0.0,
                            item.timestamp + self.config.window_seconds - now,
                        )
                    )
                    break
        return max(waits)

    def _finish(
        self,
        event: _WindowEvent,
        success: bool,
        actual_tokens: int | None,
    ) -> None:
        if actual_tokens is not None and (
            isinstance(actual_tokens, bool)
            or not isinstance(actual_tokens, int)
            or actual_tokens < 0
        ):
            raise ValueError("actual_tokens must be a non-negative integer or None.")
        with self._condition:
            if self._in_flight < 1:
                raise RuntimeError("Rate limiter in-flight accounting underflow.")
            self._in_flight -= 1
            if success:
                self._successful_attempts += 1
                if actual_tokens is not None:
                    event.tokens = actual_tokens
                    self._total_actual_tokens += actual_tokens
                self._successes_since_increase += 1
                if (
                    self._successes_since_increase
                    >= self.config.additive_increase_every
                    and self._recommended_concurrency < self.config.max_concurrency
                ):
                    self._recommended_concurrency += 1
                    self._successes_since_increase = 0
            else:
                self._failed_attempts += 1
            self._prune(self._clock())
            self._condition.notify_all()

    def _prune(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        while self._events and self._events[0].timestamp <= cutoff:
            self._events.popleft()

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RateLimitCancelled("The model request was cancelled while waiting.")


_PROCESS_LIMITERS: dict[str, ModelRateLimiter] = {}
_PROCESS_LIMITERS_LOCK = threading.Lock()


def get_process_rate_limiter(
    model: str,
    config: RateLimitConfig | None = None,
) -> ModelRateLimiter:
    """Return the one process-wide limiter for a model name.

    The first client establishes the model's limits.  Later clients always reuse
    that object, preventing separate Collections from accidentally multiplying
    the provider quota.
    """

    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("model must not be empty.")
    with _PROCESS_LIMITERS_LOCK:
        limiter = _PROCESS_LIMITERS.get(normalized_model)
        if limiter is None:
            limiter = ModelRateLimiter(normalized_model, config)
            _PROCESS_LIMITERS[normalized_model] = limiter
        return limiter


def reset_process_rate_limiters() -> None:
    """Clear the registry for isolated tests before any production work starts."""

    with _PROCESS_LIMITERS_LOCK:
        if any(item.snapshot().in_flight for item in _PROCESS_LIMITERS.values()):
            raise RuntimeError(
                "Cannot reset process rate limiters with active requests."
            )
        _PROCESS_LIMITERS.clear()


def retry_after_seconds(
    value: str | float | int | None,
    *,
    now: float | None = None,
) -> float | None:
    """Parse Retry-After in either delta-seconds or RFC 7231 HTTP-date form."""

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return max(0.0, numeric) if math.isfinite(numeric) else None
    text = value.strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = math.nan
    if math.isfinite(numeric):
        return max(0.0, numeric)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - current)
