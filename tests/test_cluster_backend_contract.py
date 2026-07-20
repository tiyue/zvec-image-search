from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from image_vector_service.activity_store import ActivityStore
from image_vector_service.backend_server import (
    BackendJobManager,
    BackendRequestError,
    _normalize_job,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition

_TERMINAL = {"succeeded", "partial", "needs_attention", "failed", "cancelled"}


class _ClusterContractService:
    def __init__(self, progress: Any, cancel_check: Any) -> None:
        self.progress = progress
        self.cancel_check = cancel_check
        self.started = {
            operation: threading.Event()
            for operation in (
                "cluster_images",
                "cluster_list",
                "cluster_detail",
                "cluster_merge",
                "cluster_split",
                "cluster_apply_identity",
                "cluster_undo",
                "active_learning_queue",
                "active_learning_review",
                "active_learning_review_undo",
            )
        }
        self.blocked_operations: set[str] = set()

    def _checkpoint(self, operation: str) -> None:
        self.started[operation].set()
        while operation in self.blocked_operations:
            self.cancel_check()
            time.sleep(0.005)
        self.cancel_check()

    def cluster_images(self, **params: Any) -> dict[str, Any]:
        self._checkpoint("cluster_images")
        self.progress("Preparing clustering inputs 3/3")
        return {
            "processed": 3,
            "total": 3,
            "cluster_count": 1,
            "failure_count": 0,
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
            "embedding_recomputed": False,
            "received": params,
        }

    def list_image_clusters(self, **params: Any) -> dict[str, Any]:
        self._checkpoint("cluster_list")
        return {
            "total": 1,
            "offset": params["offset"],
            "limit": params["limit"],
            "items": [
                {
                    "cluster_id": "cluster-1",
                    "cluster_type": "semantic",
                    "member_count": 2,
                    "edge_kinds": ["semantic"],
                    "representative": {
                        "doc_id": "doc-1",
                        "relative_path": "角色/portrait.png",
                    },
                }
            ],
            "api_requests": 0,
        }

    def image_cluster_detail(
        self, cluster_id: str, *, offset: int, limit: int
    ) -> dict[str, Any]:
        self._checkpoint("cluster_detail")
        return {
            "cluster": {
                "cluster_id": cluster_id,
                "member_count": 2,
                "edge_kinds": ["semantic"],
            },
            "member_count": 2,
            "offset": offset,
            "limit": limit,
            "has_more": offset + 2 < 5,
            "items": [
                {"doc_id": "doc-1", "relative_path": "角色/portrait.png"},
                {"doc_id": "doc-2", "relative_path": "角色/portrait-2.png"},
            ],
            "edge_count": 4,
            "edge_offset": 0,
            "edge_limit": 1_000,
            "edge_has_more": False,
            "api_requests": 0,
        }

    def merge_image_clusters(self, cluster_ids: list[str]) -> dict[str, Any]:
        self._checkpoint("cluster_merge")
        return self._operation_result("merge", received={"cluster_ids": cluster_ids})

    def split_image_cluster(
        self, cluster_id: str, doc_ids: list[str]
    ) -> dict[str, Any]:
        self._checkpoint("cluster_split")
        return self._operation_result(
            "split", received={"cluster_id": cluster_id, "doc_ids": doc_ids}
        )

    def apply_image_cluster_identity(
        self,
        cluster_id: str,
        *,
        identity_category: str,
        identity_value: str,
    ) -> dict[str, Any]:
        self._checkpoint("cluster_apply_identity")
        return self._operation_result(
            "apply_identity",
            received={
                "cluster_id": cluster_id,
                "identity_category": identity_category,
                "identity_value": identity_value,
            },
        )

    def undo_latest_cluster_operation(self) -> dict[str, Any]:
        self._checkpoint("cluster_undo")
        return {
            **self._operation_result("undo"),
            "undone": True,
            "restored": 2,
            "conflicts": 0,
            "undo_available": False,
        }

    @staticmethod
    def _operation_result(
        operation: str, *, received: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "operation_id": f"operation-{operation}",
            "operation": operation,
            "status": "succeeded",
            "applied": 2,
            "failed": 0,
            "skipped": 0,
            "undo_available": operation != "undo",
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
            "embedding_recomputed": False,
            "received": received or {},
        }

    def build_active_learning_review_queue(
        self, *, review_budget: int
    ) -> dict[str, Any]:
        self._checkpoint("active_learning_queue")
        return {
            "schema_version": 1,
            "queue_id": "queue-1",
            "selection_version": "active-learning-v1",
            "candidate_count": 2,
            "selected_count": 1,
            "items": [
                {
                    "rank": 1,
                    "doc_id": "doc-1",
                    "group_id": "cluster-1",
                    "uncertainty_score": 0.8,
                    "reasons": ["cluster_outlier"],
                }
            ],
            "review_budget": review_budget,
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def review_active_learning_queue(
        self, *, queue_id: str, decisions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self._checkpoint("active_learning_review")
        return {
            "schema_version": 1,
            "queue_id": queue_id,
            "selection_version": "active-learning-v1",
            "candidate_count": 2,
            "selected_count": 1,
            "items": [],
            "decisions": decisions,
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def undo_latest_active_learning_review(self) -> dict[str, Any]:
        self._checkpoint("active_learning_review_undo")
        return {
            "undone": True,
            "batch_id": "review-batch-1",
            "restored": 1,
            "failed": 0,
            "conflicts": 0,
            "undo_available": False,
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def close(self) -> None:
        return


class ClusterBackendContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_cluster_backend_contract_"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name in ("images", "workspace", "results", "query", "config"):
            (self.root / name).mkdir()
        library = LibraryDefinition(
            library_id="library-a",
            name="Library A",
            image_root=self.root / "images",
            workspace=self.root / "workspace",
        )
        self.catalog = LibraryCatalog(
            default_library_id=library.library_id,
            libraries=(library,),
            federated_results_directory=self.root / "results",
        )
        self.activity = ActivityStore(self.root / "config")
        self.addCleanup(self.activity.close)
        self.service: _ClusterContractService | None = None

        def factory(
            _library: LibraryDefinition, progress: Any, cancel_check: Any
        ) -> _ClusterContractService:
            self.service = _ClusterContractService(progress, cancel_check)
            return self.service

        self.manager = BackendJobManager(
            instance_id="cluster-contract",
            config_fingerprint="c" * 64,
            config=ServiceConfig(
                workspace=self.root / "control",
                results_directory=self.root / "results",
            ),
            query_root=self.root / "query",
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=self.activity,
        )
        self.manager.start()
        self.addCleanup(self.manager.close)

    def _wait(self, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = self.manager.get(job_id)
            if latest["status"] in _TERMINAL:
                return latest
            time.sleep(0.01)
        self.fail(f"job did not finish: {latest}")

    def test_cluster_images_defaults_to_hash_based_sources(self) -> None:
        command, params = _normalize_job(
            {
                "command": "cluster_images",
                "params": {"library_id": "library-a"},
            },
            self.root / "query",
        )

        self.assertEqual(command, "cluster_images")
        self.assertEqual(params["cluster_types"], ["exact", "perceptual"])

    def test_all_intelligence_tasks_submit_poll_and_persist_history(self) -> None:
        requests = (
            {
                "command": "cluster_images",
                "params": {
                    "library_id": "library-a",
                    "scope": "new_or_changed",
                    "cluster_types": ["exact", "perceptual", "semantic"],
                },
            },
            {
                "command": "cluster_list",
                "params": {
                    "library_id": "library-a",
                    "offset": 0,
                    "limit": 12,
                    "cluster_type": "all",
                },
            },
            {
                "command": "cluster_detail",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "offset": 40,
                    "limit": 60,
                },
            },
            {
                "command": "cluster_merge",
                "params": {
                    "library_id": "library-a",
                    "cluster_ids": ["cluster-1", "cluster-2"],
                },
            },
            {
                "command": "cluster_split",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "doc_ids": ["doc-1", "doc-2"],
                },
            },
            {
                "command": "cluster_apply_identity",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "identity_category": "character",
                    "identity_value": "雷电将军",
                },
            },
            {
                "command": "cluster_undo",
                "params": {"library_id": "library-a"},
            },
            {
                "command": "active_learning_queue",
                "params": {"library_id": "library-a", "review_budget": 25},
            },
            {
                "command": "active_learning_review",
                "params": {
                    "library_id": "library-a",
                    "queue_id": "queue-1",
                    "decisions": [
                        {"doc_id": "doc-1", "decision": "accept", "labels": []}
                    ],
                },
            },
            {
                "command": "active_learning_review_undo",
                "params": {"library_id": "library-a"},
            },
        )
        completed: list[dict[str, Any]] = []
        for payload in requests:
            submitted = self.manager.submit(payload)
            job = self._wait(submitted["id"])
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["result"]["api_requests"], 0)
            completed.append(job)

        cluster = completed[0]
        self.assertEqual(
            cluster["result"]["received"]["cluster_types"],
            ["exact", "perceptual", "semantic"],
        )
        self.assertFalse(cluster["result"]["embedding_recomputed"])

        self.assertTrue(self.activity.flush(timeout=2.0))
        history = self.activity.list_job_history(limit=50)
        by_id = {item["job_id"]: item for item in history["items"]}
        for job in completed:
            self.assertIn(job["id"], by_id)
            self.assertEqual(by_id[job["id"]]["status"], "succeeded")
        self.assertEqual(
            {by_id[job["id"]]["task_type"] for job in completed},
            {
                "cluster_images",
                "cluster_list",
                "cluster_detail",
                "cluster_merge",
                "cluster_split",
                "cluster_apply_identity",
                "cluster_undo",
                "active_learning_queue",
                "active_learning_review",
                "active_learning_review_undo",
            },
        )

    def test_each_intelligence_task_can_be_cancelled_without_stopping_worker(
        self,
    ) -> None:
        assert self.service is not None
        requests = (
            (
                "cluster_images",
                {
                    "library_id": "library-a",
                    "scope": "all",
                    "cluster_types": ["semantic"],
                },
            ),
            (
                "cluster_list",
                {
                    "library_id": "library-a",
                    "offset": 0,
                    "limit": 10,
                    "cluster_type": "all",
                },
            ),
            (
                "cluster_detail",
                {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "offset": 0,
                    "limit": 20,
                },
            ),
            (
                "cluster_merge",
                {
                    "library_id": "library-a",
                    "cluster_ids": ["cluster-1", "cluster-2"],
                },
            ),
            (
                "cluster_split",
                {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "doc_ids": ["doc-1"],
                },
            ),
            (
                "cluster_apply_identity",
                {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "identity_category": "work",
                    "identity_value": "原神",
                },
            ),
            ("cluster_undo", {"library_id": "library-a"}),
            (
                "active_learning_queue",
                {"library_id": "library-a", "review_budget": 25},
            ),
            (
                "active_learning_review",
                {
                    "library_id": "library-a",
                    "queue_id": "queue-1",
                    "decisions": [
                        {"doc_id": "doc-1", "decision": "accept", "labels": []}
                    ],
                },
            ),
            (
                "active_learning_review_undo",
                {"library_id": "library-a"},
            ),
        )
        for command, params in requests:
            with self.subTest(command=command):
                self.service.started[command].clear()
                self.service.blocked_operations.add(command)
                submitted = self.manager.submit({"command": command, "params": params})
                self.assertTrue(
                    self.service.started[command].wait(timeout=2.0), command
                )
                cancelled = self.manager.cancel(submitted["id"])
                self.assertIn(cancelled["status"], {"cancelling", "cancelled"})
                terminal = self._wait(submitted["id"])
                self.assertEqual(terminal["status"], "cancelled")
                self.service.blocked_operations.discard(command)

        follow_up = self._wait(
            self.manager.submit(
                {
                    "command": "cluster_list",
                    "params": {
                        "library_id": "library-a",
                        "offset": 0,
                        "limit": 10,
                        "cluster_type": "semantic",
                    },
                }
            )["id"]
        )
        self.assertEqual(follow_up["status"], "succeeded")

    def test_manual_cluster_normalization_rejects_duplicates_limits_and_traits(
        self,
    ) -> None:
        invalid_payloads = (
            {
                "command": "cluster_detail",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "limit": 2_001,
                },
            },
            {
                "command": "cluster_merge",
                "params": {
                    "library_id": "library-a",
                    "cluster_ids": ["cluster-1", "cluster-2", "cluster-2"],
                },
            },
            {
                "command": "cluster_merge",
                "params": {
                    "library_id": "library-a",
                    "cluster_ids": [f"cluster-{index}" for index in range(101)],
                },
            },
            {
                "command": "cluster_split",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "doc_ids": ["doc-1", "doc-1"],
                },
            },
            {
                "command": "cluster_split",
                "params": {
                    "library_id": "library-a",
                    "cluster_id": "cluster-1",
                    "doc_ids": [f"doc-{index}" for index in range(10_001)],
                },
            },
            *(
                {
                    "command": "cluster_apply_identity",
                    "params": {
                        "library_id": "library-a",
                        "cluster_id": "cluster-1",
                        "identity_category": category,
                        "identity_value": "不允许的标签",
                    },
                }
                for category in ("action", "expression")
            ),
        )
        for payload in invalid_payloads:
            with (
                self.subTest(command=payload["command"], params=payload["params"]),
                self.assertRaises(BackendRequestError),
            ):
                _normalize_job(payload, self.root / "query")


if __name__ == "__main__":
    unittest.main()
