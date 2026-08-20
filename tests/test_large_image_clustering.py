from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from image_vector_service import large_image_clustering
from image_vector_service.cluster_operation_store import ClusterOperationStore
from image_vector_service.image_clustering import (
    ClusteringCancelled,
    IdentityEvidence,
    ImageClusteringConfig,
    ImageClusterInput,
    SemanticNeighbor,
)
from image_vector_service.large_image_clustering import LargeImageClusterEngine


def _sha(value: int) -> str:
    return f"{value:064x}"


class LargeImageClusteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zvec_large_cluster_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ClusterOperationStore(self.root / "clusters.sqlite3")

    def test_exact_and_existing_vector_semantic_edges_stream_to_snapshot(self) -> None:
        provider_calls: list[str] = []

        def neighbors(item: ImageClusterInput, _top_k: int):
            provider_calls.append(item.doc_id)
            if item.doc_id == "doc-b":
                return [SemanticNeighbor("doc-c", 0.95)]
            return []

        config = ImageClusteringConfig(
            embedding_version="existing-zvec-v1",
            enable_exact_hash=True,
            enable_perceptual_hash=False,
            enable_semantic=True,
            external_embedding_lookup=True,
        )
        engine = LargeImageClusterEngine(self.store, config, neighbors, batch_size=2)
        result = engine.run(
            "library-a",
            iter(
                (
                    ImageClusterInput(
                        "doc-a",
                        _sha(1),
                        identity_evidence=(
                            IdentityEvidence("character", "雷电将军", "folder"),
                        ),
                    ),
                    ImageClusterInput("doc-b", _sha(1)),
                    ImageClusterInput("doc-c", _sha(3)),
                )
            ),
        )

        self.assertEqual(result.clustered_count, 3)
        self.assertEqual(result.cluster_count, 1)
        self.assertEqual(result.edge_count, 2)
        self.assertEqual(result.semantic_query_count, 3)
        self.assertEqual(result.api_requests, 0)
        self.assertLessEqual(result.max_materialized_batch, 2)
        self.assertCountEqual(provider_calls, ["doc-a", "doc-b", "doc-c"])

        snapshot = self.store.load_snapshot(result.snapshot_version)
        self.assertEqual({edge.kind for edge in snapshot.edges}, {"exact", "semantic"})
        self.assertEqual(snapshot.clusters[0].identity_anchors[0].value, "雷电将军")
        self.assertEqual(self._staging_count(), 0)

    def test_bad_items_and_neighbor_failures_are_persisted_without_stopping(
        self,
    ) -> None:
        def neighbors(item: ImageClusterInput, _top_k: int):
            if item.doc_id == "provider-fails":
                raise RuntimeError("local vector lookup failed")
            return []

        engine = LargeImageClusterEngine(
            self.store,
            ImageClusteringConfig(
                embedding_version="existing-zvec-v1",
                enable_exact_hash=False,
                enable_perceptual_hash=False,
                enable_semantic=True,
                external_embedding_lookup=True,
            ),
            neighbors,
            batch_size=2,
        )
        result = engine.run(
            "library-a",
            (
                ImageClusterInput("good", _sha(1)),
                {"doc_id": "bad", "sha256": "not-a-sha"},
                ImageClusterInput("provider-fails", _sha(2)),
            ),
        )

        self.assertEqual(result.input_count, 3)
        self.assertEqual(result.clustered_count, 2)
        self.assertEqual(result.failure_count, 2)
        failures = self.store.page_snapshot_failures(result.snapshot_version)
        self.assertEqual(failures["total_count"], 2)
        self.assertEqual(
            {item["code"] for item in failures["items"]},
            {"invalid_cluster_input", "semantic_neighbor_failed"},
        )
        self.assertEqual(self._staging_count(), 0)

    def test_incremental_run_reuses_edges_and_skips_unchanged_semantic_queries(
        self,
    ) -> None:
        provider_calls: list[str] = []

        def neighbors(item: ImageClusterInput, _top_k: int):
            provider_calls.append(item.doc_id)
            return []

        config = ImageClusteringConfig(
            embedding_version="existing-zvec-v1",
            enable_exact_hash=True,
            enable_perceptual_hash=False,
            enable_semantic=True,
            external_embedding_lookup=True,
        )
        engine = LargeImageClusterEngine(self.store, config, neighbors, batch_size=2)
        inputs = (
            ImageClusterInput("doc-a", _sha(7)),
            ImageClusterInput("doc-b", _sha(7)),
        )
        first = engine.run("library-a", inputs)
        self.assertEqual(provider_calls, ["doc-a", "doc-b"])
        provider_calls.clear()

        second = engine.run(
            "library-a",
            inputs,
            previous_snapshot_version=first.snapshot_version,
        )

        self.assertEqual(second.new_or_changed_count, 0)
        self.assertEqual(second.removed_count, 0)
        self.assertEqual(second.reused_edge_count, 1)
        self.assertEqual(second.semantic_query_count, 0)
        self.assertEqual(provider_calls, [])
        self.assertEqual(second.edge_count, 1)

    def test_cancellation_removes_staging_and_keeps_active_snapshot_unchanged(
        self,
    ) -> None:
        baseline_engine = LargeImageClusterEngine(
            self.store,
            ImageClusteringConfig(
                embedding_version="existing-zvec-v1",
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
            batch_size=2,
        )
        baseline = baseline_engine.run(
            "library-a", [ImageClusterInput("baseline", _sha(1))]
        )
        checks = 0

        def cancel() -> bool:
            nonlocal checks
            checks += 1
            return checks > 5

        with self.assertRaises(ClusteringCancelled):
            baseline_engine.run(
                "library-a",
                (
                    ImageClusterInput(f"doc-{index}", _sha(index + 2))
                    for index in range(50)
                ),
                cancel_check=cancel,
            )

        active = self.store.active_snapshot_version("library-a")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.snapshot_version, baseline.snapshot_version)
        self.assertEqual(self._staging_count(), 0)

    def test_store_startup_cleans_crash_leftovers(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO cluster_stream_runs (
                    run_id, library_id, created_at, status
                ) VALUES ('crashed', 'library-a', '2026-01-01T00:00:00Z', 'building')
                """
            )
            connection.execute(
                """
                INSERT INTO cluster_stream_items (
                    run_id, ordinal, doc_id, sha256, fingerprint
                ) VALUES ('crashed', 0, 'doc-a', ?, 'fingerprint')
                """,
                (_sha(1),),
            )
        ClusterOperationStore(self.store.path)
        self.assertEqual(self._staging_count(), 0)

    def test_failed_sqlite_setup_closes_partial_connection(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")

        with (
            patch.object(
                large_image_clustering.sqlite3,
                "connect",
                return_value=connection,
            ),
            self.assertRaisesRegex(sqlite3.OperationalError, "pragma failed"),
        ):
            large_image_clustering._connect(self.store.path)

        connection.close.assert_called_once_with()

    def test_perceptual_tree_caps_dense_neighbor_materialization(self) -> None:
        engine = LargeImageClusterEngine(
            self.store,
            ImageClusteringConfig(
                embedding_version="existing-zvec-v1",
                enable_exact_hash=False,
                enable_perceptual_hash=True,
                enable_semantic=False,
                perceptual_hash_distance=2,
            ),
            batch_size=7,
            max_perceptual_neighbors=4,
        )
        result = engine.run(
            "library-a",
            (
                ImageClusterInput(
                    f"doc-{index:03d}",
                    _sha(index + 1),
                    perceptual_hash="0000000000000000",
                )
                for index in range(100)
            ),
        )

        self.assertEqual(result.cluster_count, 1)
        self.assertGreater(result.perceptual_truncated_queries, 0)
        self.assertLessEqual(result.max_materialized_batch, 7)
        self.assertLessEqual(result.edge_count, 99 * 4)

    def _staging_count(self) -> int:
        with closing(sqlite3.connect(self.store.path)) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_stream_runs"
                ).fetchone()[0]
            )


class LargeImageClusteringScaleTests(unittest.TestCase):
    def test_one_hundred_thousand_inputs_stay_bounded_and_do_not_build_snapshot(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="zvec_large_cluster_scale_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = ClusterOperationStore(root / "clusters.sqlite3")
        engine = LargeImageClusterEngine(
            store,
            ImageClusteringConfig(
                embedding_version="existing-zvec-v1",
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
            batch_size=257,
        )

        def inputs():
            for index in range(100_000):
                # Groups of 100 exercise SQL-streamed exact star edges while
                # the generator proves the engine accepts a one-pass iterable.
                yield ImageClusterInput(f"doc-{index:06d}", _sha(index // 100))

        result = engine.run("library-scale", inputs())

        self.assertEqual(result.input_count, 100_000)
        self.assertEqual(result.clustered_count, 100_000)
        self.assertEqual(result.cluster_count, 1_000)
        self.assertEqual(result.edge_count, 99_000)
        self.assertLessEqual(result.max_materialized_batch, 257)
        self.assertEqual(result.api_requests, 0)
        record = store.snapshot_version(result.snapshot_version)
        self.assertEqual(record.item_count, 100_000)
        self.assertEqual(record.cluster_count, 1_000)
        self.assertEqual(record.edge_count, 99_000)
        tail = store.page_snapshot_items(
            result.snapshot_version, offset=99_985, limit=15
        )
        self.assertEqual(len(tail["items"]), 15)
        self.assertEqual(tail["items"][0]["doc_id"], "doc-099985")
        self.assertFalse(tail["has_more"])
        with closing(sqlite3.connect(store.path)) as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT doc_id, fingerprint, cluster_id
                FROM cluster_snapshot_items
                WHERE snapshot_version = ? AND snapshot_order >= ?
                ORDER BY snapshot_order LIMIT ?
                """,
                (result.snapshot_version, 99_985, 15),
            ).fetchall()
        self.assertTrue(
            any("idx_cluster_snapshot_items_order" in str(row) for row in plan),
            plan,
        )


if __name__ == "__main__":
    unittest.main()
