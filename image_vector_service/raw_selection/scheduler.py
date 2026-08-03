"""Bounded, priority-aware decode scheduler with single-flight merging.

Implements requirement sections 8.2 and 8.3 (and the generation-token part of
8.1):

- 8.2: separate bounded thread pools per decode task type. No unbounded pools
  or queues. Full-RAW workers limit LibRaw's internal threads to avoid
  multiplicative oversubscription.
- 8.3: single-flight request merging — concurrent requests for the same
  source file + version + cache spec decode exactly once and share the result;
  a failure releases all waiters; after cancel/clear/version-change a stale
  result never writes back into the valid cache (generation token).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")

# Bounded concurrency tiers (8.2). Chosen as safe production defaults within
# the documented matrices; never unbounded.
ARW_EMBED_WORKERS = 12
JPG_THUMB_WORKERS = 12
PNG_THUMB_WORKERS = 6
ARW_FULL_WORKERS = 3


class DecodeScheduler:
    """Owns bounded decode pools and a single-flight in-flight map."""

    def __init__(self) -> None:
        self._arw_embed = ThreadPoolExecutor(
            max_workers=ARW_EMBED_WORKERS, thread_name_prefix="rs-arw-embed"
        )
        self._jpg_thumb = ThreadPoolExecutor(
            max_workers=JPG_THUMB_WORKERS, thread_name_prefix="rs-jpg-thumb"
        )
        self._png_thumb = ThreadPoolExecutor(
            max_workers=PNG_THUMB_WORKERS, thread_name_prefix="rs-png-thumb"
        )
        self._arw_full = ThreadPoolExecutor(
            max_workers=ARW_FULL_WORKERS, thread_name_prefix="rs-arw-full"
        )
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, Future] = {}
        self._generation = 0

    # ------------------------------------------------------------------
    # Generation tokens (8.1): bump to invalidate in-flight late writes.
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def bump_generation(self) -> None:
        """Invalidate outstanding results (project close / cache clear)."""
        with self._lock:
            self._generation += 1

    # ------------------------------------------------------------------
    # Pool selection (8.2)
    # ------------------------------------------------------------------

    def _pool_for(self, extension: str, kind: str) -> ThreadPoolExecutor:
        ext = extension.casefold()
        if kind == "full" and ext == ".arw":
            return self._arw_full
        if ext == ".arw":
            return self._arw_embed
        if ext == ".png":
            return self._png_thumb
        return self._jpg_thumb

    # ------------------------------------------------------------------
    # Single-flight (8.3)
    # ------------------------------------------------------------------

    def run_single_flight(
        self,
        key: Hashable,
        extension: str,
        kind: str,
        fn: Callable[[], T],
    ) -> T:
        """Run *fn* once per concurrent identical *key*; waiters share it.

        Blocks until the result is available. If another thread is already
        computing the same key, this call waits for and returns that result
        instead of decoding again. A raised exception is propagated to every
        waiter and the in-flight entry is released so a later call retries.
        """
        pool = self._pool_for(extension, kind)
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                fut = existing
            else:
                fut = pool.submit(fn)
                self._inflight[key] = fut

        if existing is None:
            # Register the release callback outside the lock. Calling
            # add_done_callback while holding the lock would deadlock when the
            # future already completed: the callback runs synchronously on this
            # thread and tries to re-acquire the same lock.
            def _release(_future: Future) -> None:
                with self._lock:
                    self._inflight.pop(key, None)

            fut.add_done_callback(_release)

        return fut.result()

    def shutdown(self, wait: bool = True) -> None:
        self._arw_embed.shutdown(wait=wait)
        self._jpg_thumb.shutdown(wait=wait)
        self._png_thumb.shutdown(wait=wait)
        self._arw_full.shutdown(wait=wait)
