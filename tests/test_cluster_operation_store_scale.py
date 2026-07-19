from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.benchmark_cluster_operation_store import (
    MIB,
    ClusterStoreBenchmarkResult,
    run_cluster_store_benchmark,
)

_RUN_100K = os.environ.get("ZVEC_RUN_CLUSTER_SCALE_100K", "").strip().casefold()
_RUN_100K_ENABLED = _RUN_100K in {"1", "true", "yes", "on"}


class ClusterOperationStoreScaleTests(unittest.TestCase):
    """Guard SQLite paging against accidental full-library materialization."""

    def test_10k_snapshot_persists_and_pages_with_bounded_resources(self) -> None:
        result = self._benchmark(10_000)

        self._assert_snapshot_contract(result, expected_items=10_000)
        # These ceilings deliberately allow slower CI hosts while still catching
        # full-copy regressions and runaway object materialization.
        self.assertLess(result.snapshot_total_seconds, 45.0)
        self.assertLess(result.snapshot_peak_memory_bytes, 128 * MIB)
        self.assertLess(result.page_seconds, 5.0)
        self.assertLess(result.page_peak_memory_bytes, 8 * MIB)

    @unittest.skipUnless(
        _RUN_100K_ENABLED,
        "set ZVEC_RUN_CLUSTER_SCALE_100K=1 to run the explicit 100k slow test",
    )
    def test_100k_snapshot_persists_and_pages_with_bounded_resources(self) -> None:
        result = self._benchmark(100_000)

        self._assert_snapshot_contract(result, expected_items=100_000)
        self.assertLess(result.snapshot_total_seconds, 180.0)
        self.assertLess(result.snapshot_peak_memory_bytes, 384 * MIB)
        self.assertLess(result.page_seconds, 10.0)
        self.assertLess(result.page_peak_memory_bytes, 16 * MIB)

    def _benchmark(self, item_count: int) -> ClusterStoreBenchmarkResult:
        with tempfile.TemporaryDirectory(prefix="zvec-cluster-scale-test-") as root:
            return run_cluster_store_benchmark(
                Path(root) / "cluster-operations.sqlite3",
                item_count=item_count,
                cluster_size=25,
                page_limit=257,
            )

    def _assert_snapshot_contract(
        self,
        result: ClusterStoreBenchmarkResult,
        *,
        expected_items: int,
    ) -> None:
        self.assertEqual(result.item_count, expected_items)
        self.assertEqual(result.page_total_count, expected_items)
        self.assertEqual(result.page_returned, 257)
        self.assertLess(result.page_returned, expected_items)
        self.assertTrue(result.page_has_more)
        self.assertEqual(
            result.page_first_doc_id,
            f"doc-{result.page_offset:06d}",
        )
        self.assertEqual(
            result.page_last_doc_id,
            f"doc-{result.page_offset + result.page_returned - 1:06d}",
        )
        self.assertLess(result.page_payload_bytes, 256 * 1024)
        self.assertGreater(result.database_bytes, 0)
        self.assertEqual(result.external_api_calls, 0)


if __name__ == "__main__":
    unittest.main()
