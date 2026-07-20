from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from image_vector_service.cluster_operation_store import (
    ClusterOperationItemInput,
    ClusterOperationStore,
    ClusterOperationStoreUnavailable,
    ClusterOperationValidationError,
    InvalidClusterRuleCursor,
)
from image_vector_service.image_clustering import (
    ClusterEdge,
    ClusterItemState,
    ClusterSnapshot,
    IdentityAnchor,
    ImageCluster,
)


def _snapshot(*, algorithm_version: str = "cluster-test-v1") -> ClusterSnapshot:
    return ClusterSnapshot(
        algorithm_version=algorithm_version,
        embedding_version="qwen3-vl-embedding:test",
        items=(
            ClusterItemState("doc-a", "fingerprint-a", "cluster-one"),
            ClusterItemState("doc-b", "fingerprint-b", "cluster-one"),
            ClusterItemState("doc-c", "fingerprint-c", "cluster-two"),
            ClusterItemState("doc-d", "fingerprint-d", "cluster-two"),
        ),
        edges=(
            ClusterEdge("doc-a", "doc-b", "exact", 1.0),
            ClusterEdge("doc-c", "doc-d", "semantic", 0.93),
        ),
        clusters=(
            ImageCluster(
                cluster_id="cluster-one",
                member_doc_ids=("doc-a", "doc-b"),
                edge_kinds=("exact",),
                identity_anchors=(
                    IdentityAnchor(
                        category="character",
                        value="雷电将军",
                        source="manual",
                        confidence=1.0,
                        support=1,
                        conflict=False,
                    ),
                ),
            ),
            ImageCluster(
                cluster_id="cluster-two",
                member_doc_ids=("doc-c", "doc-d"),
                edge_kinds=("semantic",),
            ),
        ),
    )


class ClusterOperationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cluster-operations.sqlite3"
        self.store = ClusterOperationStore(
            self.database,
            recover_interrupted=False,
            redactions=("secret-token",),
        )
        self.snapshot = _snapshot()
        self.snapshot_record = self.store.save_snapshot(
            "lib-main",
            self.snapshot,
            snapshot_version="snapshot-v1",
            metadata={"source": "unit-test"},
        )

    def _completed_identity_operation(self, operation_id: str) -> dict[str, object]:
        self.store.create_operation(
            operation_id=operation_id,
            library_id="lib-main",
            operation_type="apply_identity",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"version": "before"},
            items=(
                ClusterOperationItemInput(
                    item_key="identity-doc-a",
                    doc_id="doc-a",
                    cluster_id="cluster-one",
                    identity_category="character",
                    identity_value="雷电将军",
                    before={"tags": []},
                ),
                ClusterOperationItemInput(
                    item_key="identity-doc-b",
                    doc_id="doc-b",
                    cluster_id="cluster-one",
                    identity_category="character",
                    identity_value="雷电将军",
                    before={"tags": []},
                ),
            ),
        )
        for index, doc_id in enumerate(("doc-a", "doc-b")):
            self.store.record_item_result(
                operation_id,
                index,
                status="applied",
                after={"doc_id": doc_id, "tags": ["雷电将军"]},
            )
        return self.store.complete_operation(
            operation_id,
            after_snapshot={"version": "after"},
            result_snapshot_version="snapshot-v1",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_normalized_snapshot_roundtrip_activation_and_paging(self) -> None:
        self.assertEqual(self.store.load_snapshot("snapshot-v1"), self.snapshot)
        self.assertEqual(self.snapshot_record.item_count, 4)
        self.assertEqual(self.snapshot_record.edge_count, 2)
        self.assertEqual(self.snapshot_record.cluster_count, 2)
        self.assertEqual(len(self.snapshot_record.snapshot_sha256), 64)
        self.assertTrue(self.snapshot_record.active)

        page = self.store.page_snapshot_items("snapshot-v1", offset=1, limit=2)
        self.assertEqual(page["total_count"], 4)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])

        second = self.store.save_snapshot(
            "lib-main",
            _snapshot(algorithm_version="cluster-test-v2"),
            snapshot_version="snapshot-v2",
        )
        self.assertTrue(second.active)
        self.assertFalse(self.store.snapshot_version("snapshot-v1").active)
        restored = self.store.activate_snapshot("snapshot-v1")
        self.assertTrue(restored.active)
        self.assertEqual(
            self.store.active_snapshot_version("lib-main").snapshot_version,  # type: ignore[union-attr]
            "snapshot-v1",
        )

    def test_cluster_pages_and_detail_use_bounded_sql_reads(self) -> None:
        base = _snapshot()
        with_single = ClusterSnapshot(
            algorithm_version=base.algorithm_version,
            embedding_version=base.embedding_version,
            items=(
                *base.items,
                ClusterItemState("doc-e", "fingerprint-e", "cluster-single"),
            ),
            edges=base.edges,
            clusters=(
                *base.clusters,
                ImageCluster(
                    cluster_id="cluster-single",
                    member_doc_ids=("doc-e",),
                    edge_kinds=(),
                ),
            ),
        )
        self.store.save_snapshot(
            "lib-main",
            with_single,
            snapshot_version="snapshot-page",
        )

        first_page = self.store.page_clusters(
            "snapshot-page", offset=0, limit=1, cluster_type="all"
        )
        exact = self.store.page_clusters(
            "snapshot-page", cluster_type="exact", limit=10
        )
        semantic = self.store.page_clusters(
            "snapshot-page", cluster_type="semantic", limit=10
        )
        near_duplicate = self.store.page_clusters(
            "snapshot-page", cluster_type="near_duplicate", limit=10
        )
        singles = self.store.page_clusters(
            "snapshot-page", cluster_type="single", limit=10
        )

        self.assertEqual(first_page["total_count"], 2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(exact["items"][0]["cluster_id"], "cluster-one")
        self.assertEqual(exact["items"][0]["representative_doc_id"], "doc-a")
        self.assertEqual(exact["items"][0]["member_count"], 2)
        self.assertEqual(
            exact["items"][0]["identity_anchors"][0]["category"], "character"
        )
        self.assertEqual(semantic["items"][0]["cluster_id"], "cluster-two")
        self.assertEqual(near_duplicate["total_count"], 1)
        self.assertEqual(singles["items"][0]["cluster_id"], "cluster-single")
        self.assertEqual(first_page["api_requests"], 0)

        detail = self.store.cluster_detail(
            "snapshot-page",
            "cluster-one",
            offset=0,
            limit=1,
            edge_offset=0,
            edge_limit=1,
        )
        self.assertEqual(detail["cluster"]["representative_doc_id"], "doc-a")
        self.assertEqual(detail["members"]["total_count"], 2)
        self.assertEqual(detail["members"]["items"][0]["doc_id"], "doc-a")
        self.assertTrue(detail["members"]["has_more"])
        self.assertEqual(detail["edges"]["total_count"], 1)
        self.assertEqual(detail["edges"]["items"][0]["kind"], "exact")
        self.assertFalse(detail["edges"]["has_more"])
        self.assertEqual(detail["api_requests"], 0)

        second_member_page = self.store.cluster_detail(
            "snapshot-page", "cluster-one", offset=1, limit=1
        )
        self.assertEqual(second_member_page["members"]["items"][0]["doc_id"], "doc-b")
        self.assertFalse(second_member_page["members"]["has_more"])

    def test_manual_rules_are_canonical_idempotent_conflict_safe_and_paginated(
        self,
    ) -> None:
        first = self.store.add_rule(
            library_id="lib-main",
            snapshot_version="snapshot-v1",
            rule_kind="must_link",
            left_doc_id="doc-b",
            right_doc_id="doc-a",
        )
        duplicate = self.store.add_rule(
            library_id="lib-main",
            snapshot_version="snapshot-v1",
            rule_kind="must-link",
            left_doc_id="doc-a",
            right_doc_id="doc-b",
        )
        self.assertEqual(
            (first["left_doc_id"], first["right_doc_id"]), ("doc-a", "doc-b")
        )
        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(first["rule_id"], duplicate["rule_id"])

        with self.assertRaisesRegex(
            ClusterOperationValidationError, "opposite manual rule"
        ):
            self.store.add_rule(
                library_id="lib-main",
                snapshot_version="snapshot-v1",
                rule_kind="must_not_link",
                left_doc_id="doc-a",
                right_doc_id="doc-b",
            )

        self.store.add_rule(
            library_id="lib-main",
            snapshot_version="snapshot-v1",
            rule_kind="must_not_link",
            left_doc_id="doc-a",
            right_doc_id="doc-c",
        )
        self.store.add_rule(
            library_id="lib-main",
            snapshot_version="snapshot-v1",
            rule_kind="must_link",
            left_doc_id="doc-c",
            right_doc_id="doc-d",
        )
        first_page = self.store.list_rules("lib-main", limit=2)
        second_page = self.store.list_rules(
            "lib-main", cursor=first_page["next_cursor"], limit=2
        )
        identifiers = {
            item["rule_id"] for item in [*first_page["items"], *second_page["items"]]
        }
        self.assertEqual(len(identifiers), 3)
        self.assertTrue(first_page["has_more"])
        self.assertFalse(second_page["has_more"])

        revoked = self.store.revoke_rule(str(first["rule_id"]))
        self.assertFalse(revoked["active"])
        active_ids = {
            item["rule_id"]
            for item in self.store.list_rules("lib-main", limit=20)["items"]
        }
        self.assertNotIn(first["rule_id"], active_ids)
        with self.assertRaises(InvalidClusterRuleCursor):
            self.store.list_rules("lib-main", cursor="not-a-cursor")

    def test_identity_operations_only_accept_the_four_identity_categories(self) -> None:
        allowed = ("real_person", "cosplayer", "character", "work")
        operation = self.store.create_operation(
            library_id="lib-main",
            operation_type="apply_identity",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"snapshot_version": "snapshot-v1"},
            items=tuple(
                ClusterOperationItemInput(
                    item_key=f"identity-{category}",
                    doc_id="doc-a",
                    cluster_id="cluster-one",
                    identity_category=category,
                    identity_value=f"value-{category}",
                    before={"tags": []},
                )
                for category in allowed
            ),
        )
        self.assertEqual(operation["total_count"], 4)
        self.assertEqual(self.store.external_api_calls, 0)
        self.assertEqual(self.store.stats()["external_api_calls"], 0)

        for blocked in ("action", "expression", "scene", "camera", "composition"):
            with (
                self.subTest(blocked=blocked),
                self.assertRaisesRegex(
                    ClusterOperationValidationError, "Identity category"
                ),
            ):
                self.store.create_operation(
                    library_id="lib-main",
                    operation_type="apply_identity",
                    source_snapshot_version="snapshot-v1",
                    before_snapshot={},
                    items=(
                        ClusterOperationItemInput(
                            item_key=f"blocked-{blocked}",
                            doc_id="doc-a",
                            identity_category=blocked,
                            identity_value="walking",
                        ),
                    ),
                )

    def test_single_item_failure_is_durable_and_does_not_erase_other_results(
        self,
    ) -> None:
        operation = self.store.create_operation(
            operation_id="merge-batch",
            library_id="lib-main",
            operation_type="merge",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"clusters": ["cluster-one", "cluster-two"]},
            metadata={"requested_by": "test"},
            items=(
                ClusterOperationItemInput(
                    item_key="pair-a-c",
                    left_doc_id="doc-a",
                    right_doc_id="doc-c",
                    before={"linked": False},
                ),
                ClusterOperationItemInput(
                    item_key="pair-b-d",
                    left_doc_id="doc-b",
                    right_doc_id="doc-d",
                    before={"linked": False},
                ),
                ClusterOperationItemInput(
                    item_key="pair-a-d",
                    left_doc_id="doc-a",
                    right_doc_id="doc-d",
                    before={"linked": False},
                ),
            ),
        )
        self.assertEqual(operation["status"], "pending")
        self.store.record_item_result(
            "merge-batch", 0, status="applied", after={"linked": True}
        )
        self.store.record_item_result(
            "merge-batch",
            1,
            status="failed",
            error_code="image_missing",
            error_message="secret-token\nmissing image",
        )
        self.store.record_item_result("merge-batch", 2, status="skipped")
        completed = self.store.complete_operation(
            "merge-batch",
            after_snapshot={"clusters": ["merged-cluster"]},
        )

        self.assertEqual(completed["status"], "partial")
        self.assertEqual(completed["succeeded_count"], 1)
        self.assertEqual(completed["failed_count"], 1)
        self.assertEqual(completed["skipped_count"], 1)
        self.assertEqual(completed["undo_status"], "available")
        self.assertEqual(completed["before_snapshot"]["clusters"][0], "cluster-one")
        self.assertEqual(completed["after_snapshot"]["clusters"], ["merged-cluster"])
        items = self.store.operation_items("merge-batch")["items"]
        self.assertEqual(
            [item["status"] for item in items], ["applied", "failed", "skipped"]
        )
        self.assertIn("[REDACTED] missing image", items[1]["error_message"])

        rule = self.store.add_rule(
            library_id="lib-main",
            snapshot_version="snapshot-v1",
            rule_kind="must_link",
            left_doc_id="doc-a",
            right_doc_id="doc-c",
            operation_id="merge-batch",
        )
        self.assertEqual(rule["created_by_operation_id"], "merge-batch")

    def test_undo_batch_preserves_restore_state_and_updates_original_status(
        self,
    ) -> None:
        original = self.store.create_operation(
            operation_id="identity-batch",
            library_id="lib-main",
            operation_type="apply_identity",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"version": "before"},
            items=(
                ClusterOperationItemInput(
                    item_key="identity-doc-a",
                    doc_id="doc-a",
                    cluster_id="cluster-one",
                    identity_category="character",
                    identity_value="雷电将军",
                    before={"tags": []},
                ),
            ),
        )
        self.assertEqual(original["status"], "pending")
        self.store.record_item_result(
            "identity-batch",
            0,
            status="applied",
            after={"tags": ["雷电将军"]},
        )
        completed = self.store.complete_operation(
            "identity-batch", after_snapshot={"version": "after"}
        )
        self.assertEqual(completed["undo_status"], "available")

        undo = self.store.begin_undo("identity-batch")
        undo_items = self.store.operation_items(str(undo["operation_id"]))["items"]
        self.assertEqual(undo_items[0]["before"]["current"]["tags"], ["雷电将军"])
        self.assertEqual(undo_items[0]["before"]["restore"]["tags"], [])
        self.store.record_item_result(
            str(undo["operation_id"]),
            0,
            status="undone",
            after={"tags": []},
        )
        completed_undo = self.store.complete_operation(
            str(undo["operation_id"]), after_snapshot={"version": "restored"}
        )
        refreshed_original = self.store.operation("identity-batch")

        self.assertEqual(completed_undo["status"], "succeeded")
        self.assertEqual(refreshed_original["undo_status"], "succeeded")
        self.assertEqual(
            refreshed_original["undone_by_operation_id"], undo["operation_id"]
        )

    def test_failed_or_cancelled_operation_with_applied_items_remains_undoable(
        self,
    ) -> None:
        self.store.create_operation(
            operation_id="cancelled-merge",
            library_id="lib-main",
            operation_type="merge",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"snapshot_version": "snapshot-v1"},
            items=(
                ClusterOperationItemInput(
                    item_key="merge-a-c",
                    left_doc_id="doc-a",
                    right_doc_id="doc-c",
                    before={"active": False},
                ),
                ClusterOperationItemInput(
                    item_key="merge-b-d",
                    left_doc_id="doc-b",
                    right_doc_id="doc-d",
                    before={"active": False},
                ),
            ),
        )
        self.store.record_item_result(
            "cancelled-merge",
            0,
            status="applied",
            after={"active": True, "rule_id": "rule-a-c"},
        )
        failed = self.store.fail_operation(
            "cancelled-merge",
            error_code="operation_cancelled",
            error_message="user cancelled the operation",
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["undo_status"], "needs_attention")
        self.assertEqual(failed["succeeded_count"], 1)
        undo = self.store.begin_undo("cancelled-merge")
        undo_id = str(undo["operation_id"])
        undo_items = self.store.operation_items(undo_id)["items"]
        self.assertEqual([item["item_key"] for item in undo_items], ["merge-a-c"])
        self.store.record_item_result(
            undo_id,
            0,
            status="undone",
            after={"active": False},
        )
        self.store.complete_operation(
            undo_id,
            after_snapshot={"snapshot_version": "snapshot-v1"},
        )
        self.assertEqual(
            self.store.operation("cancelled-merge")["undo_status"], "succeeded"
        )

    def test_failed_partial_undo_needs_attention_and_retries_only_remaining_item(
        self,
    ) -> None:
        self._completed_identity_operation("partial-undo-source")
        first_undo = self.store.begin_undo("partial-undo-source")
        first_undo_id = str(first_undo["operation_id"])
        self.store.record_item_result(
            first_undo_id,
            0,
            status="undone",
            after={"doc_id": "doc-a", "tags": []},
        )
        failed_undo = self.store.fail_operation(
            first_undo_id,
            error_code="operation_cancelled",
            error_message="undo was cancelled",
        )

        self.assertEqual(failed_undo["status"], "failed")
        first_items = self.store.operation_items(first_undo_id)["items"]
        self.assertEqual(
            [item["status"] for item in first_items], ["undone", "undo_failed"]
        )
        original = self.store.operation("partial-undo-source")
        self.assertEqual(original["undo_status"], "needs_attention")

        retry = self.store.begin_undo("partial-undo-source")
        retry_id = str(retry["operation_id"])
        retry_items = self.store.operation_items(retry_id)["items"]
        self.assertEqual([item["item_key"] for item in retry_items], ["identity-doc-b"])
        self.store.record_item_result(
            retry_id,
            0,
            status="undone",
            after={"doc_id": "doc-b", "tags": []},
        )
        self.store.complete_operation(
            retry_id,
            after_snapshot={"version": "restored"},
        )
        self.assertEqual(
            self.store.operation("partial-undo-source")["undo_status"], "succeeded"
        )

    def test_restart_marks_unfinished_batch_and_items_for_attention(self) -> None:
        self.store.create_operation(
            operation_id="crashed-batch",
            library_id="lib-main",
            operation_type="split",
            source_snapshot_version="snapshot-v1",
            before_snapshot={"cluster": "cluster-one"},
            items=(
                ClusterOperationItemInput(
                    item_key="split-a-b",
                    left_doc_id="doc-a",
                    right_doc_id="doc-b",
                    before={"blocked": False},
                ),
                ClusterOperationItemInput(
                    item_key="split-c-d",
                    left_doc_id="doc-c",
                    right_doc_id="doc-d",
                    before={"blocked": False},
                ),
            ),
        )
        self.store.record_item_result(
            "crashed-batch", 0, status="applied", after={"blocked": True}
        )

        recovered = ClusterOperationStore(self.database, recover_interrupted=True)
        batch = recovered.operation("crashed-batch")
        items = recovered.operation_items("crashed-batch")["items"]

        self.assertEqual(batch["status"], "interrupted")
        self.assertEqual(batch["undo_status"], "needs_attention")
        self.assertEqual(batch["error_code"], "process_restarted")
        self.assertEqual([item["status"] for item in items], ["applied", "interrupted"])
        self.assertEqual(batch["succeeded_count"], 1)
        self.assertEqual(batch["failed_count"], 1)

        undo = recovered.begin_undo("crashed-batch")
        undo_id = str(undo["operation_id"])
        undo_items = recovered.operation_items(undo_id)["items"]
        self.assertEqual([item["item_key"] for item in undo_items], ["split-a-b"])
        recovered.record_item_result(
            undo_id,
            0,
            status="undone",
            after={"blocked": False},
        )
        recovered.complete_operation(
            undo_id,
            after_snapshot={"cluster": "cluster-one"},
        )
        self.assertEqual(
            recovered.operation("crashed-batch")["undo_status"], "succeeded"
        )

    def test_interrupted_undo_retry_skips_items_already_restored(self) -> None:
        self._completed_identity_operation("interrupted-undo-source")
        first_undo = self.store.begin_undo("interrupted-undo-source")
        first_undo_id = str(first_undo["operation_id"])
        self.store.record_item_result(
            first_undo_id,
            0,
            status="undone",
            after={"doc_id": "doc-a", "tags": []},
        )

        recovered = ClusterOperationStore(self.database, recover_interrupted=True)
        self.assertEqual(recovered.operation(first_undo_id)["status"], "interrupted")
        self.assertEqual(
            recovered.operation("interrupted-undo-source")["undo_status"],
            "needs_attention",
        )

        retry = recovered.begin_undo("interrupted-undo-source")
        retry_id = str(retry["operation_id"])
        retry_items = recovered.operation_items(retry_id)["items"]
        self.assertEqual([item["item_key"] for item in retry_items], ["identity-doc-b"])
        recovered.record_item_result(
            retry_id,
            0,
            status="undone",
            after={"doc_id": "doc-b", "tags": []},
        )
        recovered.complete_operation(
            retry_id,
            after_snapshot={"version": "restored"},
        )
        self.assertEqual(
            recovered.operation("interrupted-undo-source")["undo_status"],
            "succeeded",
        )

    def test_store_does_not_take_over_an_existing_database_user_version(self) -> None:
        separate = self.root / "shared-library.sqlite3"
        connection = sqlite3.connect(separate)
        try:
            connection.execute("PRAGMA user_version = 77")
            connection.commit()
        finally:
            connection.close()

        ClusterOperationStore(separate, recover_interrupted=False)
        connection = sqlite3.connect(separate)
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(user_version, 77)

    def test_failed_connection_setup_closes_partial_handle(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")

        with (
            patch(
                "image_vector_service.cluster_operation_store.sqlite3.connect",
                return_value=connection,
            ),
            self.assertRaisesRegex(sqlite3.OperationalError, "pragma failed"),
        ):
            self.store._connect()

        connection.close.assert_called_once_with()

    def test_failed_write_begin_closes_connection(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("database is busy")

        with (
            patch.object(self.store, "_connect", return_value=connection),
            self.assertRaisesRegex(
                ClusterOperationStoreUnavailable,
                "Unable to open the cluster operation database for writing",
            ),
            self.store._write_connection(),
        ):
            self.fail("BEGIN IMMEDIATE failure must not yield a connection")

        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
