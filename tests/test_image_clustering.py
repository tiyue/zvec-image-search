from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from image_vector_service.image_clustering import (
    ClusteringCancelled,
    ImageClusteringConfig,
    ImageClusterInput,
    ImageClusterService,
    apply_manual_cluster_rules,
    build_identity_propagation_plan,
    cluster_detail,
    list_clusters,
    load_cluster_snapshot,
    read_cluster_snapshot,
    write_cluster_snapshot,
)


def _sha(value: str) -> str:
    return (value.encode("utf-8").hex() * 64)[:64]


def _item(
    doc_id: str,
    *,
    sha256: str | None = None,
    embedding: list[float] | None = None,
    perceptual_hash: str | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "sha256": sha256 or _sha(doc_id),
        "embedding": embedding if embedding is not None else [1.0, 0.0],
        "perceptual_hash": perceptual_hash,
        "identity_evidence": evidence or [],
    }


class ImageClusteringTest(unittest.TestCase):
    def config(self, **overrides: object) -> ImageClusteringConfig:
        values: dict[str, object] = {
            "algorithm_version": "cluster-test-v1",
            "embedding_version": "qwen3-vl-embedding:test",
            "embedding_dimension": 2,
            "semantic_similarity_threshold": 0.9,
            "perceptual_hash_distance": 2,
        }
        values.update(overrides)
        return ImageClusteringConfig(**values)  # type: ignore[arg-type]

    def test_exact_and_semantic_edges_form_transitive_components(self) -> None:
        duplicate_sha = _sha("duplicate")
        items = [
            _item("a", sha256=duplicate_sha, embedding=[1.0, 0.0]),
            _item("b", sha256=duplicate_sha, embedding=[0.99, 0.01]),
            _item("c", embedding=[0.98, 0.02]),
            _item("d", embedding=[0.0, 1.0]),
        ]

        result = ImageClusterService(self.config()).run(items)

        groups = {
            frozenset(cluster.member_doc_ids): cluster
            for cluster in result.snapshot.clusters
        }
        self.assertIn(frozenset({"a", "b", "c"}), groups)
        self.assertIn(frozenset({"d"}), groups)
        self.assertEqual(
            groups[frozenset({"a", "b", "c"})].edge_kinds, ("exact", "semantic")
        )
        self.assertEqual(result.api_requests, 0)
        self.assertEqual(result.semantic_query_count, 4)

    def test_perceptual_hash_uses_exact_hamming_radius(self) -> None:
        items = [
            _item("near-a", embedding=[1.0, 0.0], perceptual_hash="0000000000000000"),
            _item("near-b", embedding=[0.0, 1.0], perceptual_hash="0000000000000003"),
            _item("far", embedding=[-1.0, 0.0], perceptual_hash="ffffffffffffffff"),
        ]

        result = ImageClusterService(self.config()).run(items)

        near = next(
            cluster
            for cluster in result.snapshot.clusters
            if "near-a" in cluster.member_doc_ids
        )
        self.assertEqual(near.member_doc_ids, ("near-a", "near-b"))
        self.assertIn("perceptual", near.edge_kinds)

    def test_incremental_run_reuses_old_edges_and_queries_only_new_or_changed(
        self,
    ) -> None:
        calls: list[str] = []

        def neighbors(item: ImageClusterInput, _top_k: int) -> list[tuple[str, float]]:
            doc_id = item.doc_id
            calls.append(doc_id)
            return {
                "a": [("b", 0.99)],
                "b": [("a", 0.99)],
                "c": [("b", 0.98)],
            }.get(doc_id, [])

        service = ImageClusterService(self.config(), neighbors)
        first = service.run([_item("a"), _item("b")])
        first_cluster_id = first.snapshot.items[0].cluster_id
        calls.clear()

        second = service.run(
            [_item("a"), _item("b"), _item("c")],
            previous_snapshot=first.snapshot,
        )

        self.assertEqual(calls, ["c"])
        self.assertEqual(second.new_or_changed_count, 1)
        self.assertGreaterEqual(second.reused_edge_count, 1)
        self.assertEqual(second.semantic_query_count, 1)
        self.assertEqual(
            {item.cluster_id for item in second.snapshot.items}, {first_cluster_id}
        )

    def test_changed_item_drops_its_old_edge_and_is_requeried(self) -> None:
        calls: list[str] = []

        def neighbors(item: ImageClusterInput, _top_k: int) -> list[tuple[str, float]]:
            doc_id = item.doc_id
            calls.append(doc_id)
            return [("a", 0.99)] if doc_id == "b" else [("b", 0.99)]

        service = ImageClusterService(self.config(), neighbors)
        first = service.run([_item("a"), _item("b")])
        calls.clear()
        second = service.run(
            [_item("a"), _item("b", embedding=[0.95, 0.05])],
            previous_snapshot=first.snapshot,
        )

        self.assertEqual(calls, ["b"])
        self.assertEqual(second.new_or_changed_count, 1)
        self.assertEqual(second.reused_edge_count, 0)
        self.assertEqual(len(second.snapshot.clusters), 1)

    def test_legacy_v1_snapshots_remain_listable_and_detailed(self) -> None:
        legacy = {
            "schema_version": 1,
            "algorithm_version": "legacy",
            "embedding_version": "old-model",
            "clusters": {"old-cluster": ["a", "b"]},
            "items": {"a": {"sha256": _sha("a")}, "b": {"sha256": _sha("b")}},
        }

        snapshot = load_cluster_snapshot(legacy)
        total, clusters = list_clusters(snapshot)
        detail = cluster_detail(snapshot, "old-cluster")

        self.assertEqual(total, 1)
        self.assertEqual(clusters[0].member_doc_ids, ("a", "b"))
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.edges[0].kind, "legacy")

    def test_snapshot_round_trip_is_atomic_and_current_results_are_readable(
        self,
    ) -> None:
        result = ImageClusterService(self.config()).run([_item("a"), _item("b")])
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "clusters.json"
            written = write_cluster_snapshot(path, result.snapshot)
            loaded = read_cluster_snapshot(path)

        self.assertEqual(written, path.resolve())
        self.assertEqual(loaded.to_dict(), result.snapshot.to_dict())

    def test_cluster_id_is_stable_across_input_order_and_incremental_addition(
        self,
    ) -> None:
        duplicate_sha = _sha("same")
        service = ImageClusterService(self.config())
        first = service.run(
            [
                _item("b", sha256=duplicate_sha),
                _item("a", sha256=duplicate_sha),
            ]
        )
        reordered = service.run(
            [
                _item("a", sha256=duplicate_sha),
                _item("b", sha256=duplicate_sha),
            ]
        )
        added = service.run(
            [
                _item("a", sha256=duplicate_sha),
                _item("b", sha256=duplicate_sha),
                _item("c", sha256=duplicate_sha),
            ],
            previous_snapshot=first.snapshot,
        )

        first_id = first.snapshot.clusters[0].cluster_id
        self.assertEqual(reordered.snapshot.clusters[0].cluster_id, first_id)
        self.assertEqual(added.snapshot.clusters[0].cluster_id, first_id)

    def test_bad_item_and_neighbor_failure_do_not_abort_other_images(self) -> None:
        def neighbors(item: ImageClusterInput, _top_k: int) -> list[tuple[str, float]]:
            if item.doc_id == "broken-neighbor":
                raise RuntimeError("synthetic neighbor failure")
            return []

        items = [
            _item("valid"),
            _item("broken-neighbor"),
            {"doc_id": "bad-sha", "sha256": "bad", "embedding": [1.0, 0.0]},
            {"doc_id": "missing-vector", "sha256": _sha("missing"), "embedding": None},
        ]

        result = ImageClusterService(self.config(), neighbors).run(items)

        self.assertEqual(result.clustered_count, 3)
        codes = {(failure.doc_id, failure.code) for failure in result.failures}
        self.assertIn(("bad-sha", "invalid_cluster_input"), codes)
        self.assertIn(("missing-vector", "missing_embedding"), codes)
        self.assertIn(("broken-neighbor", "semantic_neighbor_failed"), codes)

    def test_cancellation_is_not_converted_to_an_item_failure(self) -> None:
        checks = 0

        def cancel() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        with self.assertRaises(ClusteringCancelled):
            ImageClusterService(self.config()).run(
                [_item("a"), _item("b"), _item("c")], cancel_check=cancel
            )

    def test_manual_identity_anchor_wins_and_only_identity_fields_propagate(
        self,
    ) -> None:
        duplicate_sha = _sha("identity-cluster")
        items = [
            _item(
                "anchor",
                sha256=duplicate_sha,
                evidence=[
                    {
                        "category": "character",
                        "value": "雷电将军",
                        "source": "manual",
                        "confidence": 1.0,
                    }
                ],
            ),
            _item(
                "model-support",
                sha256=duplicate_sha,
                evidence=[
                    {
                        "category": "character",
                        "value": "雷电将军",
                        "source": "model",
                        "confidence": 0.92,
                    }
                ],
            ),
            _item("target", sha256=duplicate_sha),
            _item(
                "action-only",
                sha256=duplicate_sha,
                evidence=[
                    {
                        "category": "action",
                        "value": "挥手",
                        "source": "model",
                        "confidence": 0.99,
                    }
                ],
            ),
        ]

        result = ImageClusterService(self.config()).run(items)
        cluster = result.snapshot.clusters[0]
        anchor = cluster.identity_anchors[0]
        plan = build_identity_propagation_plan(result.snapshot, items)

        self.assertEqual(
            (anchor.category, anchor.value, anchor.source),
            ("character", "雷电将军", "manual"),
        )
        self.assertFalse(anchor.conflict)
        self.assertEqual(
            {(value.doc_id, value.category, value.value) for value in plan.suggestions},
            {
                ("target", "character", "雷电将军"),
                ("action-only", "character", "雷电将军"),
            },
        )
        self.assertTrue(
            {value.category for value in plan.suggestions}.isdisjoint(
                {"action", "expression", "scene", "camera", "composition"}
            )
        )
        self.assertIn("action", plan.blocked_categories)

    def test_conflicting_identity_values_block_propagation(self) -> None:
        duplicate_sha = _sha("conflict")
        items = [
            _item(
                "manual",
                sha256=duplicate_sha,
                evidence=[
                    {"category": "cosplayer", "value": "Alice", "source": "manual"}
                ],
            ),
            _item(
                "folder",
                sha256=duplicate_sha,
                evidence=[
                    {"category": "cosplayer", "value": "Bob", "source": "folder"}
                ],
            ),
            _item("target", sha256=duplicate_sha),
        ]

        result = ImageClusterService(self.config()).run(items)
        cluster = result.snapshot.clusters[0]
        plan = build_identity_propagation_plan(result.snapshot, items)

        self.assertEqual(cluster.identity_anchors[0].value, "Alice")
        self.assertTrue(cluster.identity_anchors[0].conflict)
        self.assertEqual(plan.suggestions, ())

    def test_manual_rules_merge_groups_and_split_indirect_connections(self) -> None:
        def neighbors(item: ImageClusterInput, _top_k: int) -> list[tuple[str, float]]:
            return {
                "a": [("b", 0.99)],
                "b": [("a", 0.99), ("c", 0.99)],
                "c": [("b", 0.99)],
                "d": [],
            }[item.doc_id]

        snapshot = (
            ImageClusterService(self.config(), neighbors)
            .run([_item("a"), _item("b"), _item("c"), _item("d")])
            .snapshot
        )
        merged = apply_manual_cluster_rules(
            snapshot,
            [
                {
                    "rule_kind": "must_link",
                    "left_doc_id": "c",
                    "right_doc_id": "d",
                    "active": True,
                }
            ],
        )
        self.assertEqual(len(merged.clusters), 1)

        split = apply_manual_cluster_rules(
            merged,
            [
                {
                    "rule_kind": "must_not_link",
                    "left_doc_id": "b",
                    "right_doc_id": other,
                    "active": True,
                }
                for other in ("a", "c", "d")
            ],
        )
        groups = {frozenset(cluster.member_doc_ids) for cluster in split.clusters}
        self.assertIn(frozenset({"b"}), groups)
        self.assertIn(frozenset({"a"}), groups)
        self.assertIn(frozenset({"c", "d"}), groups)

    def test_manual_rule_conflicts_fail_closed(self) -> None:
        snapshot = (
            ImageClusterService(self.config()).run([_item("a"), _item("b")]).snapshot
        )
        rules = [
            {
                "rule_kind": kind,
                "left_doc_id": "a",
                "right_doc_id": "b",
            }
            for kind in ("must_link", "must_not_link")
        ]
        with self.assertRaisesRegex(Exception, "both must-link"):
            apply_manual_cluster_rules(snapshot, rules)


if __name__ == "__main__":
    unittest.main()
