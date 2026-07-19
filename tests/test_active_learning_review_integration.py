from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from image_vector_service.active_learning import (
    ActiveLearningCandidate,
    ActiveLearningConfig,
    build_active_learning_queue,
    write_active_learning_queue,
)
from image_vector_service.active_learning_review_store import ActiveLearningReviewStore
from image_vector_service.config import ServiceConfig
from image_vector_service.service import ImageVectorService


class _ReviewState:
    def __init__(self, root: Path, doc_ids: tuple[str, ...]) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.annotations: dict[str, dict[str, Any]] = {}
        for index, doc_id in enumerate(doc_ids, 1):
            source = root / f"{doc_id}.png"
            source.write_bytes(b"test image placeholder")
            sha256 = f"{index:064x}"
            self.entries[doc_id] = {
                "doc_id": doc_id,
                "sha256": sha256,
                "relative_path": source.name,
                "source_path": str(source),
                "tags": [],
                "folder_tags": [],
                "accepted_auto_tags": [],
                "inherited_tags": [],
                "effective_tags": [],
            }
            self.annotations[doc_id] = {
                "source_sha256": sha256,
                "proposed_tags": ["模型标签"],
            }

    def get(self, doc_id: str) -> dict[str, Any] | None:
        value = self.entries.get(doc_id)
        return dict(value) if value is not None else None

    def get_many(self, doc_ids: Any) -> dict[str, dict[str, Any]]:
        requested = {str(value) for value in doc_ids}
        return {
            doc_id: dict(entry)
            for doc_id, entry in self.entries.items()
            if doc_id in requested
        }

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        value = self.annotations.get(doc_id)
        return dict(value) if value is not None else None


class _ReviewResolver:
    @staticmethod
    def resolve_fields(fields: dict[str, Any]) -> Path:
        return Path(str(fields["source_path"]))


class _ForbiddenModelClient:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"active-learning review called model method: {name}")


class _RepositoryIdentity:
    collection_uuid = "temporary-review-collection"


class _ReviewWriter:
    def __init__(self, doc_ids: tuple[str, ...]) -> None:
        self.tags = {doc_id: [f"原标签-{doc_id}"] for doc_id in doc_ids}
        self.failed_doc_ids: set[str] = set()
        self.apply_calls: list[str] = []
        self.restore_calls: list[str] = []
        self.finalized = 0

    def active_learning_snapshot(self, doc_id: str) -> dict[str, Any]:
        return {"doc_id": doc_id, "tags": list(self.tags[doc_id])}

    def apply_active_learning_decision(self, decision: dict[str, object]) -> str:
        doc_id = str(decision["doc_id"])
        self.apply_calls.append(doc_id)
        if doc_id in self.failed_doc_ids:
            raise RuntimeError(f"simulated write failure: {doc_id}")
        action = str(decision["action"])
        raw_tags = decision.get("tags", ())
        self.tags[doc_id] = (
            [str(value) for value in cast(Iterable[object], raw_tags)]
            if action == "edit"
            else ["模型标签"]
        )
        return "accepted"

    def restore_active_learning_snapshot(self, snapshot: dict[str, Any]) -> None:
        doc_id = str(snapshot["doc_id"])
        self.restore_calls.append(doc_id)
        self.tags[doc_id] = [str(value) for value in snapshot["tags"]]

    def active_learning_matches_snapshot(self, snapshot: dict[str, Any]) -> bool:
        doc_id = str(snapshot["doc_id"])
        return self.active_learning_snapshot(doc_id) == snapshot

    def finalize_active_learning_changes(self) -> None:
        self.finalized += 1


class ActiveLearningReviewIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_active_learning_review_"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.doc_ids = ("doc-a", "doc-b", "doc-c", "doc-d")
        state = _ReviewState(self.root, self.doc_ids)
        writer = _ReviewWriter(self.doc_ids)
        service = ImageVectorService.__new__(ImageVectorService)
        service.config = ServiceConfig(
            workspace=self.root / "workspace",
            config_home=self.root / "config",
            library_id="library-review",
        )
        service.state = state
        service.repository = _RepositoryIdentity()
        service.source_resolver = _ReviewResolver()
        service.auto_tagging = writer
        service.active_learning_reviews = ActiveLearningReviewStore(
            service.config.state_path
        )
        service.cancel_check = lambda: None
        service.progress = lambda _message: None
        service._embedding_client = _ForbiddenModelClient()
        self.service = service
        self.state = state
        self.writer = writer

    def _install_queue(
        self,
        doc_ids: tuple[str, ...],
        *,
        stale_doc_id: str | None = None,
    ) -> str:
        candidates = []
        for index, doc_id in enumerate(doc_ids, 1):
            entry = self.state.entries[doc_id]
            candidates.append(
                ActiveLearningCandidate(
                    doc_id=doc_id,
                    group_id=f"group-{index}",
                    candidate_kind="tag_review",
                    library_id="library-review",
                    source_sha256=str(entry["sha256"]),
                    tag_snapshot=(
                        ("过期标签",) if doc_id == stale_doc_id else ("模型标签",)
                    ),
                    conflict_score=0.9,
                    outlier_score=0.7,
                    relative_path=str(entry["relative_path"]),
                )
            )
        queue = build_active_learning_queue(
            candidates,
            ActiveLearningConfig(total_budget=20),
        )
        write_active_learning_queue(
            self.service.config.active_learning_queue_path, queue
        )
        return queue.queue_id

    def test_each_failure_is_persisted_and_later_items_continue_then_undo(
        self,
    ) -> None:
        queue_id = self._install_queue(self.doc_ids, stale_doc_id="doc-d")
        self.writer.failed_doc_ids.add("doc-b")

        result = self.service.review_active_learning_queue(
            queue_id=queue_id,
            decisions=(
                {"doc_id": "doc-a", "decision": "accept", "labels": []},
                {"doc_id": "doc-b", "decision": "accept", "labels": []},
                {"doc_id": "doc-c", "decision": "skip", "labels": []},
                {"doc_id": "doc-d", "decision": "accept", "labels": []},
            ),
        )

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["conflicts"], 1)
        self.assertTrue(result["undo_available"])
        self.assertEqual(result["api_requests"], 0)
        self.assertEqual(result["embedding_api_requests"], 0)
        self.assertEqual(result["qwen_api_requests"], 0)
        # doc-b failed, but doc-c and doc-d were still evaluated afterwards.
        self.assertEqual(self.writer.apply_calls, ["doc-a", "doc-b"])

        batch_id = str(result["batch_id"])
        persisted = ActiveLearningReviewStore(self.service.config.state_path)
        batch = persisted.get_batch(batch_id)
        statuses = {
            item["doc_id"]: item["status"]
            for item in persisted.list_entries(batch_id)["items"]
        }
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(
            statuses,
            {
                "doc-a": "applied",
                "doc-b": "failed",
                "doc-c": "skipped",
                "doc-d": "conflict",
            },
        )

        # Reopening the review store simulates a backend restart. The real tag
        # state and the persisted undo journal are sufficient to recover.
        self.service.active_learning_reviews = persisted
        undo = self.service.undo_latest_active_learning_review()
        self.assertTrue(undo["undone"])
        self.assertEqual(undo["restored"], 1)
        self.assertEqual(undo["failed"], 0)
        self.assertEqual(undo["conflicts"], 0)
        self.assertEqual(undo["api_requests"], 0)
        self.assertEqual(undo["embedding_api_requests"], 0)
        self.assertEqual(undo["qwen_api_requests"], 0)
        self.assertEqual(self.writer.tags["doc-a"], ["原标签-doc-a"])
        self.assertEqual(persisted.get_batch(batch_id)["status"], "undone")

    def test_newer_manual_edit_is_kept_and_undo_records_conflict(self) -> None:
        queue_id = self._install_queue(("doc-a",))
        result = self.service.review_active_learning_queue(
            queue_id=queue_id,
            decisions=({"doc_id": "doc-a", "decision": "accept", "labels": []},),
        )
        batch_id = str(result["batch_id"])
        self.writer.tags["doc-a"] = ["用户后续编辑"]

        undo = self.service.undo_latest_active_learning_review()

        self.assertFalse(undo["undone"])
        self.assertEqual(undo["restored"], 0)
        self.assertEqual(undo["failed"], 0)
        self.assertEqual(undo["conflicts"], 1)
        self.assertTrue(undo["undo_available"])
        self.assertEqual(self.writer.tags["doc-a"], ["用户后续编辑"])
        batch = self.service.active_learning_reviews.get_batch(batch_id)
        self.assertEqual(batch["status"], "undo_partial")
        entry = self.service.active_learning_reviews.get_entry(batch_id, "doc-a")
        self.assertEqual(entry["undo_status"], "conflict")
        self.assertEqual(entry["undo_error_code"], "undo_state_conflict")
        self.assertEqual(undo["embedding_api_requests"], 0)
        self.assertEqual(undo["qwen_api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
