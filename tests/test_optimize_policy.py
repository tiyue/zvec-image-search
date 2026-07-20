from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from image_vector_service.optimize_policy import (
    OptimizePolicyConfig,
    OptimizePolicyStore,
    evaluate_optimize_gate,
)

UTC = timezone.utc
START = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)


def _config(**overrides: object) -> OptimizePolicyConfig:
    values: dict[str, object] = {
        "change_threshold": 100_000,
        "delete_count_threshold": 10_000,
        "delete_ratio_threshold": 0.05,
        "delete_ratio_minimum_count": 100,
        "max_interval": timedelta(hours=12),
        "failure_backoff_initial": timedelta(minutes=1),
        "failure_backoff_maximum": timedelta(minutes=2),
        "sqlite_timeout_seconds": 0.05,
    }
    values.update(overrides)
    return OptimizePolicyConfig(**values)  # type: ignore[arg-type]


class _RuntimeAdapter:
    def __init__(
        self,
        *,
        cancelled: bool = False,
        idle: bool = True,
        failure: Exception | None = None,
    ) -> None:
        self.cancelled = cancelled
        self.idle = idle
        self.failure = failure
        self.idle_checks = 0

    def cancellation_requested(self) -> bool:
        if self.failure is not None:
            raise self.failure
        return self.cancelled

    def queue_is_idle(self) -> bool:
        self.idle_checks += 1
        return self.idle


class OptimizePolicyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "optimize-policy.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_one_hundred_thousand_accumulated_changes_trigger_once(self) -> None:
        store = OptimizePolicyStore(self.path, _config())

        first = store.mark_changes("index:one", changes=60_000, now=START)
        duplicate = store.mark_changes("index:one", changes=60_000, now=START)
        before = store.should_optimize(2_000_000, now=START)
        second = store.mark_changes(
            "index:two",
            changes=40_000,
            now=START + timedelta(minutes=1),
        )
        at_boundary = store.should_optimize(
            2_000_000,
            now=START + timedelta(minutes=1),
        )

        self.assertTrue(first.ok)
        self.assertTrue(duplicate.ok)
        self.assertTrue(duplicate.duplicate)
        self.assertFalse(before.should_optimize)
        self.assertEqual(before.reason, "below_threshold")
        self.assertTrue(second.ok)
        self.assertTrue(at_boundary.should_optimize)
        self.assertEqual(at_boundary.reason, "change_threshold")
        self.assertEqual(at_boundary.pending_changes, 100_000)
        self.assertEqual(store.status().generation, 2)

    def test_state_survives_restart_without_raw_operation_ids(self) -> None:
        first = OptimizePolicyStore(self.path, _config())
        first.mark_changes(
            "index:C:/private/library/person.jpg",
            changes=123,
            deletes=7,
            now=START,
        )

        reopened = OptimizePolicyStore(self.path, _config())
        status = reopened.status()
        with closing(sqlite3.connect(self.path)) as connection:
            persisted = connection.execute(
                "SELECT operation_digest FROM optimize_change_operations"
            ).fetchone()

        self.assertTrue(status.available)
        self.assertEqual(status.pending_changes, 123)
        self.assertEqual(status.pending_deletes, 7)
        self.assertEqual(status.pending_since, START)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertNotIn("private", str(persisted[0]))
        self.assertEqual(len(str(persisted[0])), 64)

    def test_delete_absolute_and_ratio_thresholds_are_independent(self) -> None:
        absolute_store = OptimizePolicyStore(
            self.path,
            _config(delete_count_threshold=500),
        )
        absolute_store.mark_changes("delete:absolute", deletes=500, now=START)
        absolute = absolute_store.should_optimize(1_000_000, now=START)

        ratio_path = Path(self.temporary.name) / "ratio.sqlite3"
        ratio_store = OptimizePolicyStore(
            ratio_path,
            _config(delete_count_threshold=10_000),
        )
        ratio_store.mark_changes("delete:ratio", deletes=100, now=START)
        below_ratio = ratio_store.should_optimize(1_901, now=START)
        exact_ratio = ratio_store.should_optimize(1_900, now=START)

        self.assertEqual(absolute.reason, "delete_count_threshold")
        self.assertTrue(absolute.should_optimize)
        self.assertEqual(below_ratio.reason, "below_threshold")
        self.assertFalse(below_ratio.should_optimize)
        self.assertEqual(exact_ratio.reason, "delete_ratio_threshold")
        self.assertTrue(exact_ratio.should_optimize)
        self.assertAlmostEqual(exact_ratio.delete_ratio, 0.05)

    def test_small_batch_waits_until_exact_maximum_interval(self) -> None:
        store = OptimizePolicyStore(self.path, _config())
        store.mark_changes("small:index", changes=5, now=START)

        just_before = store.should_optimize(
            100_000,
            now=START + timedelta(hours=12) - timedelta(microseconds=1),
        )
        at_boundary = store.should_optimize(
            100_000,
            now=START + timedelta(hours=12),
        )

        self.assertFalse(just_before.should_optimize)
        self.assertEqual(just_before.reason, "below_threshold")
        self.assertTrue(at_boundary.should_optimize)
        self.assertEqual(at_boundary.reason, "maximum_interval")

    def test_success_is_idempotent_and_preserves_concurrent_changes(self) -> None:
        store = OptimizePolicyStore(
            self.path,
            _config(change_threshold=100),
        )
        store.mark_changes("batch:one", changes=100, now=START)
        decision = store.should_optimize(10_000, now=START)
        store.mark_attempt(decision, "attempt:one", now=START)
        store.mark_changes(
            "batch:arrived-during-optimize",
            changes=20,
            deletes=2,
            now=START + timedelta(seconds=1),
        )

        first = store.mark_success(
            decision,
            attempt_id="attempt:one",
            now=START + timedelta(seconds=2),
        )
        duplicate = store.mark_success(
            decision,
            attempt_id="attempt:one",
            now=START + timedelta(seconds=3),
        )
        status = store.status()

        self.assertTrue(first.applied)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(status.pending_changes, 20)
        self.assertEqual(status.pending_deletes, 2)
        self.assertEqual(
            status.pending_since,
            START + timedelta(seconds=1),
        )
        self.assertEqual(status.consecutive_failures, 0)

    def test_out_of_order_decision_callbacks_keep_newer_generation_pending(
        self,
    ) -> None:
        store = OptimizePolicyStore(
            self.path,
            _config(change_threshold=1),
        )
        store.mark_changes("batch:one", changes=80, now=START)
        first_decision = store.should_optimize(10_000, now=START)
        store.mark_changes(
            "batch:two",
            changes=20,
            now=START + timedelta(seconds=1),
        )
        second_decision = store.should_optimize(
            10_000,
            now=START + timedelta(seconds=1),
        )
        store.mark_changes(
            "batch:three",
            changes=10,
            now=START + timedelta(seconds=2),
        )

        store.mark_success(first_decision, now=START + timedelta(seconds=3))
        store.mark_success(second_decision, now=START + timedelta(seconds=4))

        status = store.status()
        self.assertEqual(status.pending_changes, 10)
        self.assertEqual(status.generation, 3)
        self.assertEqual(
            status.pending_since,
            START + timedelta(seconds=2),
        )

    def test_failure_is_sanitized_idempotent_and_exponentially_backed_off(
        self,
    ) -> None:
        store = OptimizePolicyStore(
            self.path,
            _config(change_threshold=1),
        )
        store.mark_changes("large:index", changes=1, now=START)
        decision = store.should_optimize(100, now=START)
        store.mark_attempt(decision, "attempt:one", now=START)
        secret_error = RuntimeError(
            "Authorization=super-secret\nfailed at C:/private/library/index.bin"
        )

        first_failure = store.mark_failure(
            decision,
            secret_error,
            attempt_id="attempt:one",
            now=START,
        )
        duplicate = store.mark_failure(
            decision,
            secret_error,
            attempt_id="attempt:one",
            now=START,
        )
        before_first_retry = store.should_optimize(
            100,
            now=START + timedelta(seconds=59),
        )
        at_first_retry = store.should_optimize(
            100,
            now=START + timedelta(seconds=60),
        )
        second_failure = store.mark_failure(
            decision,
            "second failure",
            attempt_id="attempt:two",
            now=START + timedelta(seconds=60),
        )
        before_capped_retry = store.should_optimize(
            100,
            now=START + timedelta(seconds=179),
        )
        at_capped_retry = store.should_optimize(
            100,
            now=START + timedelta(seconds=180),
        )
        status = store.status()

        self.assertTrue(first_failure.applied)
        self.assertTrue(duplicate.duplicate)
        self.assertTrue(second_failure.applied)
        self.assertEqual(status.consecutive_failures, 2)
        self.assertNotIn("super-secret", status.last_attempt_error)
        self.assertNotIn("C:/private", status.last_attempt_error)
        self.assertNotIn("\n", status.last_attempt_error)
        self.assertEqual(before_first_retry.reason, "failure_backoff")
        self.assertAlmostEqual(before_first_retry.retry_after_seconds or 0, 1.0)
        self.assertTrue(at_first_retry.should_optimize)
        self.assertEqual(before_capped_retry.reason, "failure_backoff")
        self.assertAlmostEqual(before_capped_retry.retry_after_seconds or 0, 1.0)
        self.assertTrue(at_capped_retry.should_optimize)

    def test_reused_operation_id_with_other_counts_is_rejected(self) -> None:
        store = OptimizePolicyStore(self.path, _config())
        store.mark_changes("same-operation", changes=10, now=START)

        conflict = store.mark_changes("same-operation", changes=11, now=START)

        self.assertFalse(conflict.ok)
        self.assertIn("reused", conflict.error)
        self.assertEqual(store.status().pending_changes, 10)

    def test_unavailable_storage_never_raises_or_requests_optimize(self) -> None:
        blocker = Path(self.temporary.name) / "not-a-directory"
        blocker.write_text("blocked", encoding="utf-8")
        store = OptimizePolicyStore(blocker / "policy.sqlite3", _config())

        write = store.mark_changes("index:one", changes=100_000, now=START)
        decision = store.should_optimize(100_000, now=START)

        self.assertFalse(write.ok)
        self.assertFalse(decision.should_optimize)
        self.assertEqual(decision.reason, "policy_unavailable")
        self.assertTrue(decision.policy_error)

    def test_locked_storage_fails_quickly_without_losing_main_operation(self) -> None:
        store = OptimizePolicyStore(self.path, _config())
        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            result = store.mark_changes("blocked:index", changes=1, now=START)
            blocker.rollback()

        recovered = store.mark_changes("recovered:index", changes=1, now=START)

        self.assertFalse(result.ok)
        self.assertTrue(recovered.ok)
        self.assertEqual(store.status().pending_changes, 1)


class OptimizeRuntimeGateTest(unittest.TestCase):
    def test_cancelled_work_does_not_even_check_queue(self) -> None:
        adapter = _RuntimeAdapter(cancelled=True)

        result = evaluate_optimize_gate(adapter)

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(adapter.idle_checks, 0)

    def test_busy_queue_defers_optional_maintenance(self) -> None:
        result = evaluate_optimize_gate(_RuntimeAdapter(idle=False))

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "queue_busy")

    def test_idle_queue_allows_maintenance(self) -> None:
        result = evaluate_optimize_gate(_RuntimeAdapter())

        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "ready")

    def test_adapter_error_is_sanitized_and_fails_closed(self) -> None:
        result = evaluate_optimize_gate(
            _RuntimeAdapter(
                failure=RuntimeError(
                    "Bearer top-secret-token failed at C:/private/queue.db"
                )
            )
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "adapter_error")
        self.assertNotIn("top-secret-token", result.error)
        self.assertNotIn("C:/private", result.error)


class OptimizePolicyConfigTest(unittest.TestCase):
    def test_rejects_invalid_thresholds(self) -> None:
        invalid = (
            {"change_threshold": 0},
            {"delete_count_threshold": 0},
            {"delete_ratio_threshold": 0.0},
            {"delete_ratio_threshold": 1.1},
            {"delete_ratio_minimum_count": 0},
            {"max_interval": timedelta(0)},
            {"failure_backoff_initial": timedelta(0)},
            {
                "failure_backoff_initial": timedelta(minutes=2),
                "failure_backoff_maximum": timedelta(minutes=1),
            },
            {"sqlite_timeout_seconds": float("inf")},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                OptimizePolicyConfig(**override)
