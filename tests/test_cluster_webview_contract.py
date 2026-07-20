from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from zvec_desktop.configuration_service import DesktopConfigurationService
from zvec_desktop.credentials import SessionCredentialStore
from zvec_desktop.library_tasks import (
    ActiveLearningReviewRequest,
    ActiveLearningReviewUndoRequest,
    ClusterApplyIdentityRequest,
    ClusterDetailRequest,
    ClusterImagesRequest,
    ClusterListRequest,
    ClusterMergeRequest,
    ClusterSplitRequest,
    ClusterUndoRequest,
)
from zvec_webview.facade import PreviewFacade, _library_request


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class ClusterWebViewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_cluster_webview_contract_"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.image_root = self.root / "images"
        self.image_root.mkdir()
        self.source = self.image_root / "角色" / "portrait.png"
        self.source.parent.mkdir()
        Image.new("RGB", (96, 128), (80, 40, 160)).save(self.source)
        self.config_path = self.root / "config.json"
        snapshot = DesktopConfigurationService(self.config_path).create_initial(
            self.image_root,
            name="Cosplay",
        )
        self.library_id = snapshot.configuration.default_library_id
        self.facade = PreviewFacade(
            self.config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        self.addCleanup(self.facade.close)

    def test_frontend_task_payloads_map_to_validated_requests(self) -> None:
        task_type, request, library_id = _library_request(
            {
                "task_type": "cluster_images",
                "library_id": self.library_id,
                "scope": "new_or_changed",
            }
        )
        self.assertEqual(task_type, "cluster_images")
        self.assertEqual(library_id, self.library_id)
        self.assertIsInstance(request, ClusterImagesRequest)
        self.assertEqual(
            request.to_params()["cluster_types"],
            ["exact", "perceptual"],
        )

        task_type, request, library_id = _library_request(
            {
                "task_type": "cluster_images",
                "library_id": self.library_id,
                "scope": "new_or_changed",
                "cluster_types": ["exact", "perceptual", "semantic"],
            }
        )
        self.assertEqual(task_type, "cluster_images")
        self.assertEqual(library_id, self.library_id)
        self.assertIsInstance(request, ClusterImagesRequest)
        self.assertEqual(
            request.to_params()["cluster_types"],
            ["exact", "perceptual", "semantic"],
        )

        task_type, request, _library_id = _library_request(
            {
                "task_type": "cluster_list",
                "library_id": self.library_id,
                "offset": 12,
                "limit": 12,
                "cluster_type": "all",
            }
        )
        self.assertEqual(task_type, "cluster_list")
        self.assertIsInstance(request, ClusterListRequest)
        self.assertEqual(request.to_params()["cluster_type"], "all")

        task_type, request, _library_id = _library_request(
            {
                "task_type": "cluster_detail",
                "library_id": self.library_id,
                "cluster_id": "cluster-1",
                "offset": 120,
                "limit": 60,
            }
        )
        self.assertEqual(task_type, "cluster_detail")
        self.assertIsInstance(request, ClusterDetailRequest)
        self.assertEqual(request.to_params()["offset"], 120)
        self.assertEqual(request.to_params()["limit"], 60)

        manual_requests = (
            (
                {
                    "task_type": "cluster_merge",
                    "library_id": self.library_id,
                    "cluster_ids": ["cluster-1", "cluster-2"],
                },
                ClusterMergeRequest,
            ),
            (
                {
                    "task_type": "cluster_split",
                    "library_id": self.library_id,
                    "cluster_id": "cluster-1",
                    "doc_ids": ["doc-1", "doc-2"],
                },
                ClusterSplitRequest,
            ),
            (
                {
                    "task_type": "cluster_apply_identity",
                    "library_id": self.library_id,
                    "cluster_id": "cluster-1",
                    "identity_category": "cosplayer",
                    "identity_value": "示例名称",
                },
                ClusterApplyIdentityRequest,
            ),
            (
                {
                    "task_type": "cluster_undo",
                    "library_id": self.library_id,
                },
                ClusterUndoRequest,
            ),
        )
        for payload, expected_type in manual_requests:
            with self.subTest(task_type=payload["task_type"]):
                task_type, request, _library_id = _library_request(payload)
                self.assertEqual(task_type, payload["task_type"])
                self.assertIsInstance(request, expected_type)

        for category in ("action", "expression"):
            with (
                self.subTest(category=category),
                self.assertRaisesRegex(Exception, "identity_category"),
            ):
                _library_request(
                    {
                        "task_type": "cluster_apply_identity",
                        "library_id": self.library_id,
                        "cluster_id": "cluster-1",
                        "identity_category": category,
                        "identity_value": "不允许的标签",
                    }
                )

        task_type, request, _library_id = _library_request(
            {
                "task_type": "active_learning_review",
                "library_id": self.library_id,
                "queue_id": "queue-1",
                "decisions": [
                    {"doc_id": "doc-1", "decision": "accept"},
                    {
                        "doc_id": "doc-2",
                        "decision": "edit",
                        "labels": ["雷电将军", "原神"],
                    },
                ],
            }
        )
        self.assertEqual(task_type, "active_learning_review")
        self.assertIsInstance(request, ActiveLearningReviewRequest)
        self.assertEqual(len(request.decisions), 2)

        task_type, request, _library_id = _library_request(
            {
                "task_type": "active_learning_review_undo",
                "library_id": self.library_id,
            }
        )
        self.assertEqual(task_type, "active_learning_review_undo")
        self.assertIsInstance(request, ActiveLearningReviewUndoRequest)
        self.assertEqual(request.to_params(), {"library_id": self.library_id})

    def test_cluster_list_and_detail_register_safe_image_urls(self) -> None:
        representative = {
            "doc_id": "doc-1",
            "relative_path": "角色/portrait.png",
            "source_path": str(self.source),
        }
        list_view = self.facade._job_view(
            {
                "id": "1" * 32,
                "command": "cluster_list",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "total": 1,
                    "offset": 0,
                    "limit": 12,
                    "items": [
                        {
                            "cluster_id": "cluster-1",
                            "cluster_type": "semantic",
                            "member_count": 2,
                            "edge_kinds": ["semantic"],
                            "representative": representative,
                        }
                    ],
                    "api_requests": 0,
                },
            }
        )
        result = list_view["result"]
        cluster = result["items"][0]
        image = cluster["representative"]
        self.assertTrue(image["image_available"])
        self.assertTrue(image["image_id"])
        self.assertIn("variant=thumbnail", image["thumbnail_url"])
        self.assertEqual(image["relative_path"], "角色/portrait.png")
        self.assertNotIn(str(self.root), repr(list_view))
        self.assertNotIn("source_path", repr(list_view))

        detail_view = self.facade._job_view(
            {
                "id": "2" * 32,
                "command": "cluster_detail",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "cluster": {
                        "cluster_id": "cluster-1",
                        "member_count": 1,
                        "edge_kinds": ["semantic"],
                        "representative": representative,
                    },
                    "member_count": 1,
                    "offset": 20,
                    "limit": 20,
                    "has_more": True,
                    "edge_count": 40,
                    "edge_offset": 10,
                    "edge_limit": 20,
                    "edge_has_more": True,
                    "items": [{**representative, "tags": ["原神"]}],
                    "api_requests": 0,
                },
            }
        )
        detail = detail_view["result"]
        self.assertEqual(detail["member_count"], 1)
        self.assertEqual(detail["offset"], 20)
        self.assertEqual(detail["limit"], 20)
        self.assertTrue(detail["has_more"])
        self.assertEqual(detail["edge_count"], 40)
        self.assertEqual(detail["edge_offset"], 10)
        self.assertEqual(detail["edge_limit"], 20)
        self.assertTrue(detail["edge_has_more"])
        self.assertEqual(detail["members"][0]["tags"], ["原神"])
        self.assertTrue(detail["members"][0]["image_available"])
        self.assertNotIn(str(self.root), repr(detail_view))

    def test_cluster_operation_summary_is_public_but_snapshots_remain_private(
        self,
    ) -> None:
        private_snapshot = {
            "source_path": str(self.source),
            "items": [{"doc_id": "doc-1", "tags": ["内部标签"]}],
        }
        for command in (
            "cluster_merge",
            "cluster_split",
            "cluster_apply_identity",
            "cluster_undo",
        ):
            with self.subTest(command=command):
                view = self.facade._job_view(
                    {
                        "id": command.replace("_", "")[:32].ljust(32, "1"),
                        "command": command,
                        "params": {"library_id": self.library_id},
                        "status": "succeeded",
                        "result": {
                            "operation_id": "cluster-operation-1",
                            "operation": command.removeprefix("cluster_"),
                            "status": "succeeded",
                            "applied": 12,
                            "failed": 1,
                            "skipped": 2,
                            "conflicts": 1,
                            "restored": 4,
                            "undone": command == "cluster_undo",
                            "undo_available": command != "cluster_undo",
                            "before_snapshot": private_snapshot,
                            "after_snapshot": private_snapshot,
                            "rules": [{"source_path": str(self.source)}],
                            "api_requests": 0,
                            "embedding_api_requests": 0,
                            "qwen_api_requests": 0,
                        },
                    }
                )
                result = view["result"]
                self.assertEqual(result["operation_id"], "cluster-operation-1")
                self.assertEqual(result["applied"], 12)
                self.assertEqual(result["failed"], 1)
                self.assertEqual(result["skipped"], 2)
                self.assertEqual(result["conflicts"], 1)
                self.assertEqual(result["restored"], 4)
                serialized = repr(view)
                self.assertNotIn("snapshot", serialized)
                self.assertNotIn("rules", serialized)
                self.assertNotIn(str(self.root), serialized)

    def test_active_learning_queue_preserves_review_fields_and_media(self) -> None:
        view = self.facade._job_view(
            {
                "id": "3" * 32,
                "command": "active_learning_queue",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "schema_version": 1,
                    "queue_id": "queue-1",
                    "selection_version": "active-learning-v1",
                    "candidate_count": 8,
                    "selected_count": 1,
                    "items": [
                        {
                            "rank": 1,
                            "doc_id": "doc-1",
                            "group_id": "cluster-1",
                            "query_id": "query-1",
                            "relative_path": "角色/portrait.png",
                            "source_path": str(self.source),
                            "uncertainty_score": 0.82,
                            "reasons": ["identity_conflict"],
                            "suggested_tags": ["雷电将军", "原神"],
                        }
                    ],
                    "api_requests": 0,
                    "embedding_api_requests": 0,
                    "qwen_api_requests": 0,
                },
            }
        )
        result = view["result"]
        self.assertEqual(result["queue_id"], "queue-1")
        self.assertEqual(result["candidate_count"], 8)
        sample = result["items"][0]
        self.assertEqual(sample["reasons"], ["identity_conflict"])
        self.assertEqual(sample["suggested_tags"], ["雷电将军", "原神"])
        self.assertTrue(sample["image_available"])
        self.assertEqual(result["embedding_api_requests"], 0)
        self.assertEqual(result["qwen_api_requests"], 0)
        self.assertNotIn(str(self.root), repr(view))

    def test_cluster_run_summary_does_not_expose_persisted_snapshot(self) -> None:
        view = self.facade._job_view(
            {
                "id": "4" * 32,
                "command": "cluster_images",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "processed": 20,
                    "total": 20,
                    "cluster_count": 6,
                    "api_requests": 0,
                    "embedding_api_requests": 0,
                    "qwen_api_requests": 0,
                    "embedding_recomputed": False,
                    "snapshot": {"items": [{"source_path": str(self.source)}]},
                },
            }
        )
        result = view["result"]
        self.assertEqual(result["cluster_count"], 6)
        self.assertFalse(result["embedding_recomputed"])
        self.assertNotIn("snapshot", result)
        self.assertNotIn(str(self.root), repr(view))

    def test_active_learning_review_exposes_counts_but_not_undo_snapshots(
        self,
    ) -> None:
        private_snapshot = {
            "doc_id": "doc-1",
            "entry": {
                "source_path": str(self.source),
                "accepted_auto_tags": ["旧标签"],
            },
            "annotation": {"private_model_response": "must-not-leak"},
        }
        view = self.facade._job_view(
            {
                "id": "5" * 32,
                "command": "active_learning_review",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "batch_id": "review-batch-1",
                    "status": "partial",
                    "applied": 7,
                    "failed": 1,
                    "skipped": 2,
                    "conflicts": 1,
                    "undo_available": True,
                    "before_snapshot": private_snapshot,
                    "after_snapshot": private_snapshot,
                    "entries": [
                        {
                            "doc_id": "doc-1",
                            "before_snapshot": private_snapshot,
                            "after_snapshot": private_snapshot,
                        }
                    ],
                    "failures": [{"doc_id": "doc-2", "error": "state conflict"}],
                    "api_requests": 0,
                    "embedding_api_requests": 0,
                    "qwen_api_requests": 0,
                },
            }
        )

        result = view["result"]
        self.assertEqual(result["applied"], 7)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["conflicts"], 1)
        self.assertTrue(result["undo_available"])
        self.assertEqual(result["batch_id"], "review-batch-1")
        serialized = repr(view)
        self.assertNotIn("before_snapshot", serialized)
        self.assertNotIn("after_snapshot", serialized)
        self.assertNotIn("private_model_response", serialized)
        self.assertNotIn(str(self.root), serialized)

        undo_view = self.facade._job_view(
            {
                "id": "6" * 32,
                "command": "active_learning_review_undo",
                "params": {"library_id": self.library_id},
                "status": "succeeded",
                "result": {
                    "batch_id": "review-batch-1",
                    "restored": 7,
                    "failed": 0,
                    "conflicts": 0,
                    "undo_available": False,
                    "before_snapshot": private_snapshot,
                    "api_requests": 0,
                },
            }
        )
        self.assertEqual(undo_view["result"]["restored"], 7)
        self.assertFalse(undo_view["result"]["undo_available"])
        self.assertNotIn("snapshot", repr(undo_view))


if __name__ == "__main__":
    unittest.main()
