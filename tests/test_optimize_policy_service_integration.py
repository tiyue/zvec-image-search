from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from image_vector_service.optimize_policy import (
    OptimizePolicyConfig,
    OptimizePolicyStore,
)
from image_vector_service.service import ImageVectorService


class _Repository:
    def __init__(self) -> None:
        self.doc_count = 10_000
        self.optimize_count = 0
        self.failure: Exception | None = None

    def optimize(self) -> None:
        self.optimize_count += 1
        if self.failure is not None:
            raise self.failure


class _RuntimeAdapter:
    def __init__(self, *, idle: bool = True, cancelled: bool = False) -> None:
        self.idle = idle
        self.cancelled = cancelled

    def cancellation_requested(self) -> bool:
        return self.cancelled

    def queue_is_idle(self) -> bool:
        return self.idle


def _config(*, threshold: int = 1) -> OptimizePolicyConfig:
    return OptimizePolicyConfig(
        change_threshold=threshold,
        delete_count_threshold=500,
        delete_ratio_threshold=0.05,
        delete_ratio_minimum_count=100,
        max_interval=timedelta(hours=12),
        failure_backoff_initial=timedelta(minutes=1),
        failure_backoff_maximum=timedelta(hours=1),
        sqlite_timeout_seconds=0.05,
    )


class OptimizePolicyServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec_optimize_service_")
        self.addCleanup(self.temporary.cleanup)

    def _service(
        self,
        *,
        threshold: int = 1,
        externally_managed: bool = True,
    ) -> ImageVectorService:
        service = ImageVectorService.__new__(ImageVectorService)
        service._closed = False
        service.repository = _Repository()
        service.cancel_check = lambda: None
        service.optimize_policy = OptimizePolicyStore(
            Path(self.temporary.name) / f"policy-{id(service)}.sqlite3",
            _config(threshold=threshold),
        )
        service._optimize_runtime_adapter = _RuntimeAdapter()
        service._optimize_external_idle_runner = externally_managed
        service._optimize_retry_not_before = 0.0
        service._set_optimize_pending_status(service.optimize_policy.status())
        self.addCleanup(service.close)
        return service

    def test_due_changes_optimize_once_and_clear_the_pending_hint(self) -> None:
        service = self._service()
        service._record_collection_mutation(
            "index:one",
            changes=1,
            deletes=0,
        )

        result = service.run_idle_maintenance()
        second = service.run_idle_maintenance()

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(second["status"], "not_due")
        self.assertEqual(service.repository.optimize_count, 1)
        self.assertFalse(service.has_pending_optimize_maintenance())
        self.assertEqual(service.optimize_policy.status().total_pending, 0)

    def test_busy_queue_defers_without_calling_repository_optimize(self) -> None:
        service = self._service()
        service._record_collection_mutation(
            "index:busy",
            changes=1,
            deletes=0,
        )
        service.configure_optimize_runtime(_RuntimeAdapter(idle=False))

        result = service.run_idle_maintenance()

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "queue_busy")
        self.assertEqual(service.repository.optimize_count, 0)

    def test_failure_is_backed_off_and_does_not_repeat_on_next_read(self) -> None:
        service = self._service()
        service.repository.failure = OSError(28, "synthetic disk full")
        service._record_collection_mutation(
            "index:failing",
            changes=1,
            deletes=0,
        )

        first = service.run_idle_maintenance()
        second = service.run_idle_maintenance()

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "not_due")
        self.assertEqual(service.repository.optimize_count, 1)
        self.assertEqual(
            service.optimize_policy.status().consecutive_failures,
            1,
        )

    def test_standalone_close_runs_due_maintenance_but_not_small_batches(
        self,
    ) -> None:
        due = self._service(externally_managed=False)
        due._record_collection_mutation("index:due", changes=1, deletes=0)
        due.close()

        small = self._service(threshold=2, externally_managed=False)
        small._record_collection_mutation("index:small", changes=1, deletes=0)
        small.close()

        self.assertEqual(due.repository.optimize_count, 1)
        self.assertEqual(small.repository.optimize_count, 0)

    def test_repeated_read_only_maintenance_checks_do_not_read_sqlite(self) -> None:
        service = self._service()
        with patch.object(
            service.optimize_policy,
            "should_optimize",
            wraps=service.optimize_policy.should_optimize,
        ) as should_optimize:
            for _index in range(20):
                result = service.run_idle_maintenance()
                self.assertEqual(result["status"], "not_due")

        should_optimize.assert_not_called()

    def test_folder_delete_uses_stable_id_for_duplicate_commit_callbacks(self) -> None:
        service = self._service(threshold=1_000)

        service._record_folder_deletion_mutation("operation-a", 100)
        service._record_folder_deletion_mutation("operation-a", 100)

        status = service.optimize_policy.status()
        self.assertEqual(status.pending_deletes, 100)
        self.assertEqual(status.generation, 1)


if __name__ == "__main__":
    unittest.main()
