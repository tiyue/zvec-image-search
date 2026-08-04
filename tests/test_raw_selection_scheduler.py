from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from image_vector_service.raw_selection.cache import DerivedCache
from image_vector_service.raw_selection.decoder import (
    decode_full_base,
    full_decode_runtime_facts,
    render_full_decode_with_look,
)
from image_vector_service.raw_selection.scheduler import (
    DecodeScheduler,
    SchedulerQueueFull,
    StaleGeneration,
    TaskPriority,
)

_LANES = ("arw_embed", "jpg_thumbnail", "png_thumbnail", "arw_full")


def _scheduler(*, queue_capacity: int = 4) -> DecodeScheduler:
    return DecodeScheduler(
        worker_counts={name: 1 for name in _LANES},
        queue_capacities={name: queue_capacity for name in _LANES},
    )


class BoundedPrioritySchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = _scheduler(queue_capacity=2)
        self.addCleanup(self._cleanup_scheduler)

    def _cleanup_scheduler(self) -> None:
        self.scheduler.shutdown(wait=True, cancel_queued=True, timeout=5.0)

    def test_full_queue_rejects_low_and_current_evicts_old_background(self) -> None:
        running_started = threading.Event()
        release_running = threading.Event()
        order: list[str] = []

        def blocker() -> str:
            running_started.set()
            if not release_running.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            order.append("running")
            return "running"

        running = self.scheduler.submit(
            ".jpg",
            "thumb",
            blocker,
            priority=TaskPriority.BACKGROUND,
        )
        self.assertTrue(running_started.wait(timeout=5.0))
        oldest = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: order.append("oldest") or "oldest",
            priority=TaskPriority.BACKGROUND,
        )
        newest = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: order.append("newest") or "newest",
            priority=TaskPriority.BACKGROUND,
        )

        with self.assertRaises(SchedulerQueueFull):
            self.scheduler.submit(
                ".jpg",
                "thumb",
                lambda: "rejected",
                priority=TaskPriority.BACKGROUND,
            )

        current = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: order.append("current") or "current",
            priority=TaskPriority.CURRENT,
        )
        self.assertTrue(oldest.future.cancelled())
        release_running.set()
        self.assertEqual(running.result(timeout=5.0), "running")
        self.assertEqual(current.result(timeout=5.0), "current")
        self.assertEqual(newest.result(timeout=5.0), "newest")
        self.assertTrue(self.scheduler.wait_idle(timeout=5.0))
        self.assertEqual(order, ["running", "current", "newest"])
        metrics = self.scheduler.metrics()["pools"]["jpg_thumbnail"]
        self.assertEqual(metrics["queue_capacity"], 2)
        self.assertEqual(metrics["peak_queued"], 2)
        self.assertEqual(metrics["rejected"], 1)
        self.assertEqual(metrics["evicted"], 1)

    def test_all_fixed_priorities_run_in_requirement_order(self) -> None:
        scheduler = _scheduler(queue_capacity=5)
        self.addCleanup(
            lambda: scheduler.shutdown(
                wait=True,
                cancel_queued=True,
                timeout=5.0,
            )
        )
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        order: list[str] = []

        def blocker() -> None:
            blocker_started.set()
            if not release_blocker.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")

        running = scheduler.submit(".jpg", "thumb", blocker)
        self.assertTrue(blocker_started.wait(timeout=5.0))
        scheduled = [
            scheduler.submit(
                ".jpg",
                "thumb",
                lambda label=label: order.append(label),
                priority=priority,
            )
            for label, priority in (
                ("background", TaskPriority.BACKGROUND),
                ("overscan", TaskPriority.OVERSCAN),
                ("visible", TaskPriority.VISIBLE),
                ("compare", TaskPriority.COMPARE),
                ("current", TaskPriority.CURRENT),
            )
        ]
        release_blocker.set()
        running.result(timeout=5.0)
        for task in scheduled:
            task.result(timeout=5.0)
        self.assertEqual(
            order,
            ["current", "compare", "visible", "overscan", "background"],
        )

    def test_scope_generation_cancels_only_its_queued_work(self) -> None:
        running_started = threading.Event()
        release_running = threading.Event()
        token = self.scheduler.capture_generation("project-a")

        def blocker() -> str:
            running_started.set()
            if not release_running.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            return "running-old-generation"

        running = self.scheduler.submit(
            ".jpg",
            "thumb",
            blocker,
            scope="project-a",
            token=token,
        )
        self.assertTrue(running_started.wait(timeout=5.0))
        stale_queued = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: "must-not-run",
            scope="project-a",
            token=token,
        )
        other_scope = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: "other-project",
            scope="project-b",
        )

        replacement = self.scheduler.bump_generation("project-a")
        self.assertEqual(replacement.generation, token.generation + 1)
        self.assertFalse(self.scheduler.is_current(token))
        self.assertTrue(stale_queued.future.cancelled())
        self.assertFalse(other_scope.future.cancelled())
        release_running.set()
        with self.assertRaises(StaleGeneration):
            running.result(timeout=5.0)
        self.assertEqual(other_scope.result(timeout=5.0), "other-project")
        self.assertTrue(self.scheduler.wait_idle(timeout=5.0))

    def test_direct_queued_cancel_releases_capacity(self) -> None:
        running_started = threading.Event()
        release_running = threading.Event()

        def blocker() -> str:
            running_started.set()
            if not release_running.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            return "done"

        running = self.scheduler.submit(".jpg", "thumb", blocker)
        self.assertTrue(running_started.wait(timeout=5.0))
        queued = self.scheduler.submit(".jpg", "thumb", lambda: "queued")
        self.assertTrue(queued.cancel())
        self.assertTrue(queued.future.cancelled())
        replacement = self.scheduler.submit(".jpg", "thumb", lambda: "replacement")
        release_running.set()
        self.assertEqual(running.result(timeout=5.0), "done")
        self.assertEqual(replacement.result(timeout=5.0), "replacement")

    def test_single_flight_shares_success_and_promotes_queued_priority(self) -> None:
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        order: list[str] = []
        calls = 0
        calls_lock = threading.Lock()

        def blocker() -> str:
            blocker_started.set()
            if not release_blocker.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            return "blocker"

        blocker_task = self.scheduler.submit(".jpg", "thumb", blocker)
        self.assertTrue(blocker_started.wait(timeout=5.0))

        def shared_work() -> int:
            nonlocal calls
            with calls_lock:
                calls += 1
            order.append("shared")
            return 42

        original = self.scheduler.submit_single_flight(
            ("same", 1),
            ".jpg",
            "thumb",
            shared_work,
            priority=TaskPriority.BACKGROUND,
        )
        visible = self.scheduler.submit(
            ".jpg",
            "thumb",
            lambda: order.append("visible") or "visible",
            priority=TaskPriority.VISIBLE,
        )
        joined = self.scheduler.submit_single_flight(
            ("same", 1),
            ".jpg",
            "thumb",
            shared_work,
            priority=TaskPriority.CURRENT,
        )
        self.assertTrue(joined.joined_single_flight)
        self.assertIs(original.future, joined.future)
        release_blocker.set()
        self.assertEqual(blocker_task.result(timeout=5.0), "blocker")
        self.assertEqual(original.result(timeout=5.0), 42)
        self.assertEqual(joined.result(timeout=5.0), 42)
        self.assertEqual(visible.result(timeout=5.0), "visible")
        self.assertTrue(self.scheduler.wait_idle(timeout=5.0))
        self.assertEqual(calls, 1)
        self.assertEqual(order, ["shared", "visible"])

    def test_single_flight_failure_releases_waiters_and_allows_retry(self) -> None:
        attempts = 0

        def fail() -> int:
            nonlocal attempts
            attempts += 1
            raise ValueError("decode failed")

        first = self.scheduler.submit_single_flight("failure", ".jpg", "thumb", fail)
        joined = self.scheduler.submit_single_flight("failure", ".jpg", "thumb", fail)
        self.assertIs(first.future, joined.future)
        with self.assertRaisesRegex(ValueError, "decode failed"):
            first.result(timeout=5.0)
        with self.assertRaisesRegex(ValueError, "decode failed"):
            joined.result(timeout=5.0)

        retried = self.scheduler.submit_single_flight(
            "failure",
            ".jpg",
            "thumb",
            lambda: 7,
        )
        self.assertEqual(retried.result(timeout=5.0), 7)
        self.assertEqual(attempts, 1)

    def test_rejected_single_flight_does_not_poison_retry_key(self) -> None:
        scheduler = _scheduler(queue_capacity=1)
        self.addCleanup(
            lambda: scheduler.shutdown(
                wait=True,
                cancel_queued=True,
                timeout=5.0,
            )
        )
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def blocker() -> str:
            blocker_started.set()
            if not release_blocker.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            return "blocker"

        running = scheduler.submit(".jpg", "thumb", blocker)
        self.assertTrue(blocker_started.wait(timeout=5.0))
        queued = scheduler.submit(
            ".jpg",
            "thumb",
            lambda: "current",
            priority=TaskPriority.CURRENT,
        )
        with self.assertRaises(SchedulerQueueFull):
            scheduler.submit_single_flight(
                "retry-key",
                ".jpg",
                "thumb",
                lambda: "rejected",
                priority=TaskPriority.BACKGROUND,
            )
        release_blocker.set()
        self.assertEqual(running.result(timeout=5.0), "blocker")
        self.assertEqual(queued.result(timeout=5.0), "current")
        self.assertTrue(scheduler.wait_idle(timeout=5.0))
        retry = scheduler.submit_single_flight(
            "retry-key",
            ".jpg",
            "thumb",
            lambda: "retry-succeeded",
            priority=TaskPriority.CURRENT,
        )
        self.assertEqual(retry.result(timeout=5.0), "retry-succeeded")

    def test_rejected_single_flight_releases_a_concurrent_joiner(self) -> None:
        pool = self.scheduler._pool_for(".jpg", "thumb")
        owner_submitting = threading.Event()
        allow_rejection = threading.Event()

        def reject_submit(*_args, **_kwargs):
            owner_submitting.set()
            if not allow_rejection.wait(timeout=5.0):
                raise AssertionError("rejection gate was not released")
            raise SchedulerQueueFull("forced rejection")

        with (
            patch.object(pool, "submit", side_effect=reject_submit),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            try:
                owner = executor.submit(
                    self.scheduler.submit_single_flight,
                    "rejected-with-waiter",
                    ".jpg",
                    "thumb",
                    lambda: "never",
                )
                self.assertTrue(owner_submitting.wait(timeout=5.0))
                joined = self.scheduler.submit_single_flight(
                    "rejected-with-waiter",
                    ".jpg",
                    "thumb",
                    lambda: "never",
                )
                self.assertTrue(joined.joined_single_flight)
                allow_rejection.set()
                with self.assertRaisesRegex(SchedulerQueueFull, "forced rejection"):
                    owner.result(timeout=5.0)
                with self.assertRaisesRegex(SchedulerQueueFull, "forced rejection"):
                    joined.result(timeout=5.0)
            finally:
                allow_rejection.set()

        retry = self.scheduler.submit_single_flight(
            "rejected-with-waiter",
            ".jpg",
            "thumb",
            lambda: "retry",
        )
        self.assertEqual(retry.result(timeout=5.0), "retry")

    def test_stale_token_is_rejected_before_submission(self) -> None:
        token = self.scheduler.capture_generation("project")
        self.scheduler.bump_generation("project")
        with self.assertRaises(StaleGeneration):
            self.scheduler.submit(
                ".jpg",
                "thumb",
                lambda: 1,
                scope="project",
                token=token,
            )

    def test_shutdown_cancels_queue_and_joins_after_running_work_finishes(self) -> None:
        running_started = threading.Event()
        release_running = threading.Event()
        queued_cancelled = threading.Event()
        shutdown_called = threading.Event()
        shutdown_finished = threading.Event()

        def blocker() -> str:
            running_started.set()
            if not release_running.wait(timeout=5.0):
                raise AssertionError("test blocker was not released")
            return "done"

        running = self.scheduler.submit(".jpg", "thumb", blocker)
        self.assertTrue(running_started.wait(timeout=5.0))
        queued = self.scheduler.submit(".jpg", "thumb", lambda: "queued")
        queued.future.add_done_callback(
            lambda future: queued_cancelled.set() if future.cancelled() else None
        )

        def shutdown() -> None:
            shutdown_called.set()
            self.scheduler.shutdown(
                wait=True,
                cancel_queued=True,
                timeout=5.0,
            )
            shutdown_finished.set()

        thread = threading.Thread(target=shutdown, name="test-scheduler-shutdown")
        thread.start()
        self.assertTrue(shutdown_called.wait(timeout=5.0))
        self.assertTrue(queued_cancelled.wait(timeout=5.0))
        self.assertFalse(shutdown_finished.is_set())
        release_running.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(shutdown_finished.is_set())
        with self.assertRaises(StaleGeneration):
            running.result(timeout=5.0)
        with self.assertRaises(CancelledError):
            queued.result(timeout=5.0)
        self.assertFalse(self.scheduler.has_pending_work())
        self.assertTrue(self.scheduler.metrics()["shutdown"])


class DerivedCacheConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = DerivedCache(Path(self.temporary.name) / "cache")

    def test_file_identity_is_part_of_key_and_source_clear_is_precise(self) -> None:
        first = self.cache.put(
            b"first",
            normalized_path="C:/images/a.arw",
            file_size=10,
            mtime_ns=20,
            file_identity="volume:file-a",
            kind="thumbnails",
        )
        second = self.cache.put(
            b"second",
            normalized_path="C:/images/b.arw",
            file_size=10,
            mtime_ns=20,
            file_identity="volume:file-b",
            kind="thumbnails",
        )
        self.assertTrue(first.written)
        self.assertTrue(second.written)
        self.assertEqual(
            self.cache.get(
                normalized_path="C:/images/a.arw",
                file_size=10,
                mtime_ns=20,
                file_identity="volume:file-a",
                kind="thumbnails",
            ),
            b"first",
        )
        self.assertIsNone(
            self.cache.get(
                normalized_path="C:/images/a.arw",
                file_size=10,
                mtime_ns=20,
                file_identity="replacement",
                kind="thumbnails",
            )
        )
        cleared = self.cache.clear_source(
            "C:/images/a.arw",
            file_identity="volume:file-a",
        )
        self.assertEqual(cleared.removed, 1)
        self.assertEqual(cleared.errors, ())
        self.assertIsNotNone(
            self.cache.get(
                normalized_path="C:/images/b.arw",
                file_size=10,
                mtime_ns=20,
                file_identity="volume:file-b",
                kind="thumbnails",
            )
        )

    def test_stale_epoch_never_replaces_cache_entry(self) -> None:
        scheduler = _scheduler(queue_capacity=1)
        self.addCleanup(
            lambda: scheduler.shutdown(wait=True, cancel_queued=True, timeout=5.0)
        )
        token = scheduler.capture_generation("project")
        guard_requested = threading.Event()
        continue_guard = threading.Event()
        result_holder: list[object] = []

        @contextmanager
        def delayed_guard(epoch: object):
            guard_requested.set()
            if not continue_guard.wait(timeout=5.0):
                raise AssertionError("epoch guard was not released")
            with scheduler.generation_guard(epoch) as current:
                yield current

        def write() -> None:
            result_holder.append(
                self.cache.put(
                    b"stale",
                    normalized_path="C:/images/a.arw",
                    file_size=10,
                    mtime_ns=20,
                    file_identity="id-a",
                    kind="previews",
                    epoch=token,
                    epoch_guard=delayed_guard,
                )
            )

        thread = threading.Thread(target=write, name="test-stale-cache-write")
        thread.start()
        self.assertTrue(guard_requested.wait(timeout=5.0))
        scheduler.bump_generation("project")
        continue_guard.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result_holder), 1)
        self.assertEqual(result_holder[0].status, "stale")
        self.assertIsNone(
            self.cache.get(
                normalized_path="C:/images/a.arw",
                file_size=10,
                mtime_ns=20,
                file_identity="id-a",
                kind="previews",
            )
        )
        self.assertEqual(list((self.cache.root / "previews").glob("*.tmp")), [])

    def test_disk_write_error_is_explicit_and_leaves_no_temporary_file(self) -> None:
        with patch(
            "image_vector_service.raw_selection.cache.os.replace",
            side_effect=OSError(28, "disk full"),
        ):
            result = self.cache.put(
                b"payload",
                normalized_path="C:/images/a.arw",
                file_size=10,
                mtime_ns=20,
                file_identity="id-a",
                kind="thumbnails",
            )
        self.assertEqual(result.status, "error")
        self.assertIn("disk full", result.error or "")
        self.assertEqual(list(self.cache.root.rglob("*.tmp")), [])

    def test_epoch_requires_a_guard(self) -> None:
        with self.assertRaisesRegex(ValueError, "supplied together"):
            self.cache.put(
                b"payload",
                normalized_path="C:/images/a.arw",
                file_size=10,
                mtime_ns=20,
                kind="thumbnails",
                epoch=object(),
            )


class DecoderReuseTests(unittest.TestCase):
    def test_jpeg_base_can_render_multiple_owned_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.jpg"
            Image.new("RGB", (48, 32), (20, 80, 140)).save(path)
            base = decode_full_base(str(path), ".jpg")
            self.assertIsNotNone(base.image)
            original_pixel = base.image.getpixel((0, 0))
            first = render_full_decode_with_look(base, ".jpg", "st")
            second = render_full_decode_with_look(base, ".jpg", "vv")
            try:
                self.assertIsNotNone(first.image)
                self.assertIsNotNone(second.image)
                self.assertIsNot(first.image, base.image)
                self.assertIsNot(second.image, base.image)
                self.assertEqual(base.image.getpixel((0, 0)), original_pixel)
            finally:
                if first.image is not None:
                    first.image.close()
                if second.image is not None:
                    second.image.close()
                if base.image is not None:
                    base.image.close()

    def test_runtime_facts_do_not_claim_process_or_libraw_thread_isolation(
        self,
    ) -> None:
        facts = full_decode_runtime_facts()
        self.assertEqual(facts.execution_model, "in_process")
        self.assertFalse(facts.process_isolated)
        self.assertIsNone(facts.rawpy_internal_thread_limit)
        self.assertTrue(facts.reusable_base_result)


if __name__ == "__main__":
    unittest.main()
