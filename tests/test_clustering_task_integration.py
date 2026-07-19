from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from image_vector_service.active_learning_review_store import ActiveLearningReviewStore
from image_vector_service.cluster_operation_store import ClusterOperationStore
from image_vector_service.config import ServiceConfig
from image_vector_service.models import SearchHit
from image_vector_service.service import ImageVectorService


class _State:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    def list_entries(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.entries]

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        if doc_id not in {"doc-a", "doc-b"}:
            return None
        entry = self.get(doc_id)
        assert entry is not None
        return {
            "source_sha256": entry["sha256"],
            "proposed_tags": (["刻晴", "原神"] if doc_id == "doc-a" else ["测试标签"]),
            "structured": {
                "entities": {
                    "character": (
                        [
                            {
                                "name": "刻晴",
                                "confidence": 0.97,
                                "state": "accepted",
                            }
                        ]
                        if doc_id == "doc-a"
                        else []
                    ),
                    "work": (
                        [
                            {
                                "name": "原神",
                                "confidence": 0.99,
                                "state": "accepted",
                            }
                        ]
                        if doc_id == "doc-a"
                        else []
                    ),
                }
            },
        }

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return next(
            (dict(entry) for entry in self.entries if entry["doc_id"] == doc_id),
            None,
        )

    def get_many(self, doc_ids: Any) -> dict[str, dict[str, Any]]:
        requested = {str(value) for value in doc_ids}
        return {
            str(entry["doc_id"]): dict(entry)
            for entry in self.entries
            if str(entry["doc_id"]) in requested
        }


class _Resolver:
    def resolve_fields(self, fields: dict[str, Any]) -> Path:
        return Path(str(fields["source_path"]))


class _Repository:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.fetch_count = 0
        self.query_count = 0
        self.doc_count = len(vectors)

    def fetch_vector(self, doc_id: str) -> list[float] | None:
        self.fetch_count += 1
        value = self.vectors.get(doc_id)
        return None if value is None else list(value)

    def query(
        self,
        vector: list[float],
        top_k: int,
        _tags: tuple[str, ...],
        _tag_mode: str,
        _rank_source: str,
    ) -> list[SearchHit]:
        self.query_count += 1
        scored = []
        for doc_id, candidate in self.vectors.items():
            dot = sum(
                left * right for left, right in zip(vector, candidate, strict=True)
            )
            left_norm = math.sqrt(sum(value * value for value in vector))
            right_norm = math.sqrt(sum(value * value for value in candidate))
            similarity = dot / (left_norm * right_norm)
            scored.append((1.0 - similarity, doc_id))
        scored.sort()
        return [
            SearchHit(doc_id=doc_id, distance=distance, fields={"sha256": doc_id})
            for distance, doc_id in scored[:top_k]
        ]


class _ForbiddenModelClient:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"clustering must not access model client method {name}")


class _AutoTaggingReview:
    def __init__(self) -> None:
        self.tags: dict[str, list[str]] = {"doc-a": [], "doc-b": []}
        self.fail_docs: set[str] = set()
        self.finalized = 0

    def active_learning_snapshot(self, doc_id: str) -> dict[str, Any]:
        return {"doc_id": doc_id, "tags": list(self.tags.get(doc_id, []))}

    def apply_active_learning_decision(self, decision: dict[str, object]) -> str:
        doc_id = str(decision["doc_id"])
        if doc_id in self.fail_docs:
            raise RuntimeError(f"synthetic write failure: {doc_id}")
        self.tags[doc_id] = ["刻晴", "原神"]
        return "accepted"

    def restore_active_learning_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.tags[str(snapshot["doc_id"])] = list(snapshot["tags"])

    def active_learning_matches_snapshot(self, snapshot: dict[str, Any]) -> bool:
        return self.active_learning_snapshot(str(snapshot["doc_id"])) == snapshot

    def finalize_active_learning_changes(self) -> None:
        self.finalized += 1

    def cluster_identity_snapshot(self, doc_id: str) -> dict[str, Any]:
        return self.active_learning_snapshot(doc_id)

    def apply_cluster_identity(
        self,
        doc_id: str,
        *,
        category: str,
        value: str,
    ) -> None:
        if category not in {"real_person", "cosplayer", "character", "work"}:
            raise ValueError("unsupported identity")
        self.tags[doc_id] = sorted({*self.tags.get(doc_id, []), value})

    def restore_cluster_identity_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.restore_active_learning_snapshot(snapshot)

    def cluster_identity_matches_snapshot(self, snapshot: dict[str, Any]) -> bool:
        return self.active_learning_matches_snapshot(snapshot)


class ClusteringTaskIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.entries: list[dict[str, Any]] = []
        for index, doc_id in enumerate(("doc-a", "doc-b", "doc-c", "doc-d"), 1):
            path = root / f"{doc_id}.png"
            Image.new("RGB", (32, 24), (index * 30, 80, 140)).save(path)
            self.entries.append(
                {
                    "doc_id": doc_id,
                    "sha256": f"{index:064x}",
                    "relative_path": path.name,
                    "source_path": str(path),
                    "tags": ["刻晴"] if doc_id == "doc-a" else [],
                    "folder_tags": ["原神"] if doc_id == "doc-a" else [],
                    "accepted_auto_tags": [],
                    "inherited_tags": [],
                    "effective_tags": [],
                }
            )
        self.repository = _Repository(
            {
                "doc-a": [1.0, 0.0],
                "doc-b": [0.995, 0.005],
                "doc-c": [0.0, 1.0],
                "doc-d": [0.005, 0.995],
            }
        )
        service = ImageVectorService.__new__(ImageVectorService)
        service.config = ServiceConfig(
            workspace=root / "workspace",
            config_home=root / "config",
            library_id="lib-test",
            dimension=2,
        )
        service.state = _State(self.entries)
        service.repository = self.repository
        service.source_resolver = _Resolver()
        service.cancel_check = lambda: None
        service.progress = lambda _message: None
        service._embedding_client = _ForbiddenModelClient()
        service.auto_tagging = _AutoTaggingReview()
        service.active_learning_reviews = ActiveLearningReviewStore(
            service.config.state_path
        )
        service.cluster_operations = ClusterOperationStore(
            service.config.state_path,
            recover_interrupted=False,
        )
        self.service = service

    def test_cluster_task_reuses_vectors_without_embedding_api_requests(self) -> None:
        first = self.service.cluster_images(
            scope="new_or_changed",
            cluster_types=("exact", "perceptual", "semantic"),
        )
        first_queries = self.repository.query_count

        self.assertEqual(first["api_requests"], 0)
        self.assertEqual(first["embedding_api_requests"], 0)
        self.assertFalse(first["embedding_recomputed"])
        self.assertEqual(first["qwen_api_requests"], 0)
        self.assertNotIn("snapshot", first)
        self.assertGreater(first["cluster_count"], 0)
        self.assertGreater(first_queries, 0)
        self.assertTrue(self.service.config.cluster_snapshot_path.is_file())

        second = self.service.cluster_images(
            scope="new_or_changed",
            cluster_types=("exact", "perceptual", "semantic"),
        )
        self.assertEqual(second["new_or_changed_count"], 0)
        self.assertEqual(second["semantic_query_count"], 0)
        self.assertEqual(self.repository.query_count, first_queries)

    def test_cluster_browse_and_active_learning_decisions_are_persistent(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        listing = self.service.list_image_clusters(limit=50)
        self.assertGreaterEqual(listing["total"], 1)
        cluster_id = str(listing["items"][0]["cluster_id"])
        detail = self.service.image_cluster_detail(cluster_id)
        self.assertGreaterEqual(len(detail["items"]), 2)
        self.assertTrue(detail["items"][0]["source_path"])

        queue = self.service.build_active_learning_review_queue(review_budget=20)
        self.assertEqual(queue["api_requests"], 0)
        self.assertEqual(queue["embedding_api_requests"], 0)
        self.assertEqual(queue["qwen_api_requests"], 0)
        self.assertTrue(queue["items"])
        self.assertIn("source_path", queue["items"][0])
        first_doc = str(queue["items"][0]["doc_id"])
        updated = self.service.review_active_learning_queue(
            queue_id=str(queue["queue_id"]),
            decisions=({"doc_id": first_doc, "decision": "accept", "labels": []},),
        )
        self.assertEqual(updated["decisions"][0]["doc_id"], first_doc)
        self.assertEqual(updated["applied"], 1)
        self.assertTrue(updated["undo_available"])
        self.assertTrue(self.service.config.active_learning_queue_path.is_file())
        undone = self.service.undo_latest_active_learning_review()
        self.assertTrue(undone["undone"])
        self.assertEqual(undone["restored"], 1)
        self.assertEqual(self.service.auto_tagging.tags[first_doc], [])

    def test_active_learning_failure_does_not_stop_later_items(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        queue = self.service.build_active_learning_review_queue(review_budget=20)
        doc_ids = [str(item["doc_id"]) for item in queue["items"][:2]]
        self.assertEqual(len(doc_ids), 2)
        self.service.auto_tagging.fail_docs.add(doc_ids[0])

        result = self.service.review_active_learning_queue(
            queue_id=str(queue["queue_id"]),
            decisions=tuple(
                {"doc_id": doc_id, "decision": "accept", "labels": []}
                for doc_id in doc_ids
            ),
        )

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["embedding_api_requests"], 0)
        self.assertEqual(result["qwen_api_requests"], 0)
        self.assertEqual(self.service.auto_tagging.tags[doc_ids[1]], ["刻晴", "原神"])

    def test_manual_cluster_merge_split_and_undo_are_persistent(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        before = self.service.list_image_clusters(limit=20)
        cluster_ids = [str(item["cluster_id"]) for item in before["items"]]
        self.assertGreaterEqual(len(cluster_ids), 2)

        merged = self.service.merge_image_clusters(cluster_ids[:2])
        self.assertGreaterEqual(merged["applied"], 1)
        self.assertTrue(merged["undo_available"])
        after_merge = self.service.list_image_clusters(limit=20)
        self.assertLess(after_merge["total"], before["total"])

        merge_undo = self.service.undo_latest_cluster_operation()
        self.assertTrue(merge_undo["undone"])
        restored = self.service.list_image_clusters(limit=20)
        self.assertEqual(restored["total"], before["total"])

        multi = next(
            item for item in restored["items"] if int(item["member_count"]) > 1
        )
        detail = self.service.image_cluster_detail(str(multi["cluster_id"]))
        selected_doc = str(detail["items"][0]["doc_id"])
        split = self.service.split_image_cluster(
            str(multi["cluster_id"]),
            (selected_doc,),
        )
        self.assertGreaterEqual(split["applied"], 1)
        self.assertLess(
            self.service.list_image_clusters(limit=20)["total"],
            restored["total"],
        )
        self.assertGreaterEqual(
            self.service.list_image_clusters(
                limit=20,
                cluster_type="single",
            )["total"],
            2,
        )
        split_undo = self.service.undo_latest_cluster_operation()
        self.assertTrue(split_undo["undone"])

    def test_cluster_identity_is_identity_only_and_undoable(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        cluster = next(
            item
            for item in self.service.list_image_clusters(limit=20)["items"]
            if int(item["member_count"]) > 1
        )
        cluster_id = str(cluster["cluster_id"])
        result = self.service.apply_image_cluster_identity(
            cluster_id,
            identity_category="character",
            identity_value="雷电将军",
        )
        self.assertGreaterEqual(result["applied"], 1)
        self.assertEqual(result["api_requests"], 0)
        self.assertTrue(
            any("雷电将军" in tags for tags in self.service.auto_tagging.tags.values())
        )
        with self.assertRaises(ValueError):
            self.service.apply_image_cluster_identity(
                cluster_id,
                identity_category="action",
                identity_value="挥手",
            )
        undone = self.service.undo_latest_cluster_operation()
        self.assertTrue(undone["undone"])
        self.assertFalse(
            any("雷电将军" in tags for tags in self.service.auto_tagging.tags.values())
        )

    def test_cancelled_cluster_identity_is_recorded_and_can_be_undone(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        cluster = next(
            item
            for item in self.service.list_image_clusters(limit=20)["items"]
            if int(item["member_count"]) > 1
        )
        cluster_id = str(cluster["cluster_id"])
        detail = self.service.image_cluster_detail(cluster_id)
        member_ids = [str(item["doc_id"]) for item in detail["items"]]
        checks = 0

        def cancel_after_one_item() -> None:
            nonlocal checks
            checks += 1
            if checks > 1:
                raise RuntimeError("synthetic cluster identity cancellation")

        self.service.cancel_check = cancel_after_one_item
        with self.assertRaisesRegex(RuntimeError, "identity cancellation"):
            self.service.apply_image_cluster_identity(
                cluster_id,
                identity_category="character",
                identity_value="raiden-shogun",
            )

        operation = self.service.cluster_operations.list_operations(
            self.service._cluster_library_key(), limit=1
        )["items"][0]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["undo_status"], "needs_attention")
        self.assertEqual(operation["succeeded_count"], 1)
        self.assertEqual(
            sum(
                "raiden-shogun" in self.service.auto_tagging.tags[doc_id]
                for doc_id in member_ids
            ),
            1,
        )

        self.service.cancel_check = lambda: None
        recovered = self.service.undo_latest_cluster_operation()
        self.assertTrue(recovered["undone"])
        self.assertEqual(recovered["restored"], 1)
        self.assertFalse(
            any(
                "raiden-shogun" in self.service.auto_tagging.tags[doc_id]
                for doc_id in member_ids
            )
        )

    def test_cancelled_cluster_undo_retries_only_remaining_items(self) -> None:
        self.service.cluster_images(scope="all", cluster_types=("semantic",))
        cluster = next(
            item
            for item in self.service.list_image_clusters(limit=20)["items"]
            if int(item["member_count"]) > 1
        )
        cluster_id = str(cluster["cluster_id"])
        detail = self.service.image_cluster_detail(cluster_id)
        member_ids = [str(item["doc_id"]) for item in detail["items"]]
        applied = self.service.apply_image_cluster_identity(
            cluster_id,
            identity_category="character",
            identity_value="raiden-shogun",
        )
        self.assertEqual(applied["applied"], len(member_ids))
        checks = 0

        def cancel_after_one_restore() -> None:
            nonlocal checks
            checks += 1
            if checks > 1:
                raise RuntimeError("synthetic cluster undo cancellation")

        self.service.cancel_check = cancel_after_one_restore
        with self.assertRaisesRegex(RuntimeError, "undo cancellation"):
            self.service.undo_latest_cluster_operation()

        original = self.service.cluster_operations.operation(
            str(applied["operation_id"])
        )
        self.assertEqual(original["undo_status"], "needs_attention")
        remaining = sum(
            "raiden-shogun" in self.service.auto_tagging.tags[doc_id]
            for doc_id in member_ids
        )
        self.assertEqual(remaining, len(member_ids) - 1)

        self.service.cancel_check = lambda: None
        recovered = self.service.undo_latest_cluster_operation()
        self.assertTrue(recovered["undone"])
        self.assertEqual(recovered["restored"], len(member_ids) - 1)
        self.assertEqual(recovered["conflicts"], 0)
        self.assertFalse(
            any(
                "raiden-shogun" in self.service.auto_tagging.tags[doc_id]
                for doc_id in member_ids
            )
        )

    def test_one_unreadable_image_is_reported_and_the_batch_continues(self) -> None:
        broken = Path(str(self.entries[1]["source_path"]))
        broken.write_bytes(b"not-an-image")

        result = self.service.cluster_images(
            scope="all",
            cluster_types=("exact", "perceptual", "semantic"),
        )

        self.assertEqual(result["processed"], 4)
        self.assertGreaterEqual(result["failure_count"], 1)
        failures = result["failures"]
        self.assertTrue(
            any(item["code"] == "perceptual_hash_failed" for item in failures)
        )
        self.assertEqual(result["embedding_api_requests"], 0)
        self.assertEqual(result["qwen_api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
