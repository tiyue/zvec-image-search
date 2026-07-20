from __future__ import annotations

import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from image_vector_service.backend_server import _normalize_job
from zvec_desktop.library_tasks import (
    ActiveLearningDecision,
    ActiveLearningQueueRequest,
    ActiveLearningReviewRequest,
    ActiveLearningReviewUndoRequest,
    AutoTagEstimateRequest,
    AutoTagPendingRequest,
    AutoTagReviewDecision,
    AutoTagReviewFilters,
    AutoTagReviewRequest,
    AutoTagRunRequest,
    AutoTagUndoRequest,
    ClusterApplyIdentityRequest,
    ClusterDetailRequest,
    ClusterImagesRequest,
    ClusterListRequest,
    ClusterMergeRequest,
    ClusterSplitRequest,
    ClusterUndoRequest,
    FolderDeleteCommitRequest,
    FolderDeletePreviewRequest,
    FolderTagBackfillRequest,
    IdentityConfirmationRequest,
    IndexAndAutoTagRequest,
    IndexRequest,
    LibrariesRequest,
    LibraryTaskProtocolError,
    LibraryTaskService,
    LibraryTaskValidationError,
    LibraryTaskWaitTimeout,
    LowRiskBatchReviewRequest,
    MetadataBackfillRequest,
    RootsRequest,
    StatsRequest,
    SyncRequest,
    TagAliasDeleteRequest,
    TagAliasListRequest,
    TagAliasUpsertRequest,
)


class FakeLibraryTaskClient:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self.jobs: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        values = dict(params or {})
        self.submissions.append((command, values))
        return {
            "id": f"job-{len(self.submissions)}",
            "command": command,
            "status": "queued",
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not self.jobs:
            return {"id": job_id, "status": "running"}
        if len(self.jobs) == 1:
            return dict(self.jobs[0])
        return dict(self.jobs.pop(0))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"id": job_id, "status": "cancelled"}


class LibraryTaskRequestContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.query_root = Path(self.temporary.name)

    def assert_backend_accepts(self, request: Any) -> None:
        command, _params = _normalize_job(
            {"command": request.command, "params": request.to_params()},
            self.query_root,
        )
        self.assertEqual(command, request.command)

    def test_every_library_request_matches_the_backend_job_contract(self) -> None:
        review = AutoTagReviewRequest(
            library_id="library-a",
            decisions=(
                AutoTagReviewDecision(
                    proposal_id="proposal-1",
                    decision="accept",
                    accepted_tags=("站姿", "刻晴"),
                    confirmed_identity_tags=("刻晴",),
                ),
            ),
        )
        requests = (
            LibrariesRequest(),
            IndexRequest("library-a", tags=("本次新增",)),
            SyncRequest("library-a", dry_run=True),
            IndexAndAutoTagRequest(
                "library-a",
                tags=("本次新增",),
                external_processing_confirmed=True,
            ),
            StatsRequest("library-a"),
            RootsRequest("library-a"),
            FolderDeletePreviewRequest("library-a", "zvec-folder-v1.preview"),
            FolderDeleteCommitRequest(
                "library-a",
                "0123456789abcdef0123456789abcdef",
                "preview-token",
            ),
            AutoTagEstimateRequest("library-a"),
            AutoTagRunRequest(
                "library-a",
                scope="latest_index_run",
                external_processing_confirmed=True,
            ),
            AutoTagPendingRequest(
                "library-a",
                filters=AutoTagReviewFilters(
                    latest_index_only=True,
                    character="刻晴",
                    review_state="identity",
                ),
            ),
            review,
            IdentityConfirmationRequest(
                "library-a",
                "proposal-2",
                accepted_tags=("Cosplay", "荧"),
                confirmed_identity_tags=("荧",),
            ),
            LowRiskBatchReviewRequest(
                "library-a",
                proposal_ids=("proposal-3",),
                accepted_tags_by_proposal={"proposal-3": ("站姿",)},
            ),
            AutoTagUndoRequest("library-a"),
            FolderTagBackfillRequest("library-a"),
            MetadataBackfillRequest("library-a", max_images=275),
            TagAliasListRequest("library-a"),
            TagAliasUpsertRequest(
                "library-a",
                canonical_name="雷电将军",
                aliases=("雷神", "影"),
            ),
            TagAliasDeleteRequest("library-a", canonical_name="雷电将军"),
            ClusterImagesRequest("library-a"),
            ClusterListRequest("library-a", offset=5, limit=50),
            ClusterDetailRequest("library-a", "semantic-001", offset=40, limit=60),
            ClusterMergeRequest("library-a", ("semantic-001", "semantic-002")),
            ClusterSplitRequest("library-a", "semantic-001", ("doc-1", "doc-2")),
            ClusterApplyIdentityRequest(
                "library-a", "semantic-001", "character", "雷电将军"
            ),
            ClusterUndoRequest("library-a"),
            ActiveLearningQueueRequest("library-a", review_budget=25),
            ActiveLearningReviewRequest(
                "library-a",
                "queue-001",
                decisions=(
                    ActiveLearningDecision("doc-1", "accept"),
                    ActiveLearningDecision("doc-2", "edit", ("刻晴",)),
                ),
            ),
            ActiveLearningReviewUndoRequest("library-a"),
        )

        for request in requests:
            with self.subTest(command=request.command):
                self.assert_backend_accepts(request)

    def test_folder_delete_commit_requires_valid_preview_and_confirmation(
        self,
    ) -> None:
        with self.assertRaises(LibraryTaskValidationError):
            FolderDeletePreviewRequest(
                "library-a", "zvec-folder-v1.preview", include_subfolders=False
            )
        with self.assertRaises(LibraryTaskValidationError):
            FolderDeleteCommitRequest("library-a", "not-an-operation", "preview-token")
        with self.assertRaises(LibraryTaskValidationError) as caught:
            FolderDeleteCommitRequest(
                "library-a",
                "a" * 32,
                "preview-token",
                confirm=False,
            )
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_index_tags_preserve_no_change_new_only_and_clear_intent(self) -> None:
        unchanged = IndexRequest("library-a", tags=None).to_params()
        add_to_new = IndexRequest(
            "library-a", tags=(" 原神 ", "原神", "写真")
        ).to_params()
        clear = IndexRequest("library-a", tags=()).to_params()

        self.assertIsNone(unchanged["tags"])
        self.assertEqual(add_to_new["tags"], ["原神", "写真"])
        self.assertEqual(clear["tags"], [])

        combined = IndexAndAutoTagRequest(
            "library-a",
            tags=("本批新增",),
            external_processing_confirmed=True,
        ).to_params()
        self.assertEqual(combined["tags"], ["本批新增"])
        self.assertNotIn("scope", combined)

    def test_execution_policy_is_not_exposed_as_fake_per_job_fields(self) -> None:
        requests = (
            IndexRequest("library-a"),
            SyncRequest("library-a"),
            IndexAndAutoTagRequest(
                "library-a",
                external_processing_confirmed=True,
            ),
            AutoTagRunRequest(
                "library-a",
                external_processing_confirmed=True,
            ),
        )

        for request in requests:
            with self.subTest(command=request.command):
                params = request.to_params()
                self.assertNotIn("concurrency", params)
                self.assertNotIn("skip_errors", params)

    def test_folder_tag_backfill_cannot_inject_manual_tags(self) -> None:
        params = FolderTagBackfillRequest(
            "library-a",
            recursive=True,
            verify_hash=True,
        ).to_params()

        self.assertNotIn("tags", params)
        self.assertEqual(
            params,
            {
                "library_id": "library-a",
                "recursive": True,
                "verify_hash": True,
            },
        )

    def test_external_model_processing_requires_explicit_confirmation(self) -> None:
        for constructor in (
            lambda: AutoTagRunRequest("library-a"),
            lambda: IndexAndAutoTagRequest("library-a"),
        ):
            with self.subTest(constructor=constructor):
                with self.assertRaises(LibraryTaskValidationError) as raised:
                    constructor()
                self.assertEqual(
                    raised.exception.code,
                    "external_processing_not_confirmed",
                )

        estimate = AutoTagEstimateRequest("library-a").to_params()
        self.assertFalse(estimate["external_processing_confirmed"])

    def test_auto_tag_limits_scope_and_model_are_validated_locally(self) -> None:
        invalid_requests = (
            lambda: AutoTagEstimateRequest("library-a", scope="recent"),  # type: ignore[arg-type]
            lambda: AutoTagEstimateRequest("library-a", max_images=0),
            lambda: AutoTagEstimateRequest("library-a", max_images=10_001),
            lambda: AutoTagEstimateRequest("library-a", max_budget_cny=float("nan")),
            lambda: AutoTagEstimateRequest("library-a", model="bad\nmodel"),
        )
        for constructor in invalid_requests:
            with (
                self.subTest(constructor=constructor),
                self.assertRaises(LibraryTaskValidationError),
            ):
                constructor()

    def test_pending_filters_and_pagination_match_the_review_workbench(self) -> None:
        request = AutoTagPendingRequest(
            " library-a ",
            offset=100,
            limit=500,
            filters=AutoTagReviewFilters(
                latest_index_only=True,
                character=" 刻晴 ",
                work="原神",
                action="站立",
                expression="微笑",
                review_state="identity",
            ),
        )
        params = request.to_params()

        self.assertEqual(params["library_id"], "library-a")
        self.assertEqual(params["offset"], 100)
        self.assertEqual(params["limit"], 500)
        self.assertEqual(
            params["filters"],
            {
                "latest_index_only": True,
                "character": "刻晴",
                "work": "原神",
                "action": "站立",
                "expression": "微笑",
                "review_state": "identity",
            },
        )
        with self.assertRaises(LibraryTaskValidationError):
            AutoTagPendingRequest("library-a", offset=-1)
        with self.assertRaises(LibraryTaskValidationError):
            AutoTagPendingRequest("library-a", limit=501)


class AutoTagReviewRequestTest(unittest.TestCase):
    def test_single_identity_confirmation_uses_one_review_decision(self) -> None:
        request = IdentityConfirmationRequest(
            "library-a",
            proposal_id="proposal-1",
            accepted_tags=("站姿", "雷电将军"),
            confirmed_identity_tags=("雷电将军",),
        )

        params = request.to_params()
        self.assertEqual(params["library_id"], "library-a")
        self.assertEqual(len(params["decisions"]), 1)
        decision = params["decisions"][0]
        self.assertEqual(decision["proposal_id"], "proposal-1")
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["accepted_tags"], ["站姿", "雷电将军"])
        self.assertEqual(decision["confirmed_identity_tags"], ["雷电将军"])

    def test_identity_confirmation_must_be_explicit_and_selected(self) -> None:
        with self.assertRaises(LibraryTaskValidationError) as missing:
            IdentityConfirmationRequest(
                "library-a",
                "proposal-1",
                accepted_tags=("站姿",),
                confirmed_identity_tags=(),
            )
        self.assertEqual(missing.exception.code, "identity_confirmation_required")

        with self.assertRaises(LibraryTaskValidationError) as mismatch:
            IdentityConfirmationRequest(
                "library-a",
                "proposal-1",
                accepted_tags=("站姿",),
                confirmed_identity_tags=("雷电将军",),
            )
        self.assertEqual(mismatch.exception.code, "identity_confirmation_mismatch")

    def test_reject_and_manual_review_cannot_confirm_identity(self) -> None:
        with self.assertRaises(LibraryTaskValidationError):
            AutoTagReviewDecision(
                "proposal-1",
                "reject",
                confirmed_identity_tags=("刻晴",),
            )
        with self.assertRaises(LibraryTaskValidationError):
            AutoTagReviewDecision("proposal-1", "manual")

    def test_low_risk_batch_always_excludes_identity_tags(self) -> None:
        request = LowRiskBatchReviewRequest(
            "library-a",
            proposal_ids=("proposal-1", "proposal-2"),
            accepted_tags_by_proposal={
                "proposal-1": ("站姿", "微笑"),
                "proposal-2": ("全身",),
            },
        )

        params = request.to_params()
        self.assertTrue(params["exclude_identity_tags"])
        self.assertEqual(params["proposal_ids"], ["proposal-1", "proposal-2"])
        self.assertEqual(
            params["accepted_tags_by_proposal"],
            {
                "proposal-1": ["站姿", "微笑"],
                "proposal-2": ["全身"],
            },
        )

        with self.assertRaises(LibraryTaskValidationError) as mismatch:
            LowRiskBatchReviewRequest(
                "library-a",
                proposal_ids=("proposal-1", "proposal-2"),
                accepted_tags_by_proposal={"proposal-1": ("站姿",)},
            )
        self.assertEqual(mismatch.exception.code, "batch_proposal_mismatch")

    def test_recommended_batch_requires_one_batch_confirmation(self) -> None:
        with self.assertRaises(LibraryTaskValidationError) as raised:
            LowRiskBatchReviewRequest(
                "library-a",
                proposal_ids=("proposal-1",),
                accepted_tags_by_proposal={
                    "proposal-1": ("站姿", "刻晴"),
                },
                acceptance_mode="recommended",
            )
        self.assertEqual(raised.exception.code, "batch_confirmation_required")

        request = LowRiskBatchReviewRequest(
            "library-a",
            proposal_ids=("proposal-1",),
            accepted_tags_by_proposal={
                "proposal-1": ("站姿", "刻晴"),
            },
            acceptance_mode="recommended",
            batch_confirmation=True,
        )
        params = request.to_params()
        self.assertEqual(params["acceptance_mode"], "recommended")
        self.assertTrue(params["batch_confirmation"])
        self.assertFalse(params["exclude_identity_tags"])

    def test_general_review_rejects_duplicate_proposals(self) -> None:
        decision = AutoTagReviewDecision(
            "proposal-1", "accept", accepted_tags=("站姿",)
        )
        with self.assertRaises(LibraryTaskValidationError) as raised:
            AutoTagReviewRequest("library-a", decisions=(decision, decision))
        self.assertEqual(raised.exception.code, "duplicate_proposal")


class TagAliasRequestTest(unittest.TestCase):
    def test_alias_upsert_normalizes_and_deduplicates_terms(self) -> None:
        request = TagAliasUpsertRequest(
            "library-a",
            canonical_name=" 雷电将军 ",
            aliases=("雷神", " 影 ", "雷神", "雷电将军"),
        )

        self.assertEqual(
            request.to_params(),
            {
                "library_id": "library-a",
                "canonical_name": "雷电将军",
                "aliases": ["雷神", "影"],
            },
        )

    def test_alias_requests_reject_empty_and_oversized_values(self) -> None:
        with self.assertRaises(LibraryTaskValidationError):
            TagAliasUpsertRequest("library-a", "雷电将军", ())
        with self.assertRaises(LibraryTaskValidationError):
            TagAliasUpsertRequest("library-a", "雷电将军", ("雷电将军",))
        with self.assertRaises(LibraryTaskValidationError):
            TagAliasDeleteRequest("library-a", "x" * 257)

    def test_clustering_and_active_learning_validate_safe_bounds(self) -> None:
        self.assertEqual(
            ClusterImagesRequest("library-a").to_params()["cluster_types"],
            ["exact", "perceptual"],
        )
        with self.assertRaises(LibraryTaskValidationError):
            ClusterImagesRequest("library-a", cluster_types=())
        with self.assertRaises(LibraryTaskValidationError):
            ClusterListRequest("library-a", limit=501)
        with self.assertRaises(LibraryTaskValidationError):
            ActiveLearningQueueRequest("library-a", review_budget=31)
        with self.assertRaises(LibraryTaskValidationError):
            ActiveLearningDecision("doc-1", "edit")
        with self.assertRaises(LibraryTaskValidationError):
            ActiveLearningReviewRequest(
                "library-a",
                "queue-1",
                decisions=(
                    ActiveLearningDecision("doc-1", "accept"),
                    ActiveLearningDecision("doc-1", "reject"),
                ),
            )

    def test_cluster_manual_operations_validate_identity_uniqueness_and_pages(
        self,
    ) -> None:
        detail = ClusterDetailRequest(
            "library-a", "cluster-a", offset=2_000, limit=2_000
        )
        self.assertEqual(detail.to_params()["offset"], 2_000)
        self.assertEqual(detail.to_params()["limit"], 2_000)

        for category in ("real_person", "cosplayer", "character", "work"):
            with self.subTest(category=category):
                request = ClusterApplyIdentityRequest(
                    "library-a", "cluster-a", category, "身份值"
                )
                self.assertEqual(request.to_params()["identity_category"], category)

        invalid_requests = (
            lambda: ClusterDetailRequest("library-a", "cluster-a", limit=2_001),
            lambda: ClusterMergeRequest("library-a", ("a", "a")),
            lambda: ClusterMergeRequest(
                "library-a", tuple(f"cluster-{index}" for index in range(101))
            ),
            lambda: ClusterSplitRequest("library-a", "a", ("doc-1", "doc-1")),
            lambda: ClusterSplitRequest(
                "library-a", "a", tuple(f"doc-{index}" for index in range(10_001))
            ),
            lambda: ClusterApplyIdentityRequest(
                "library-a",
                "a",
                "action",
                "站立",  # type: ignore[arg-type]
            ),
            lambda: ClusterApplyIdentityRequest(
                "library-a",
                "a",
                "expression",
                "微笑",  # type: ignore[arg-type]
            ),
        )
        for constructor in invalid_requests:
            with (
                self.subTest(constructor=constructor),
                self.assertRaises(LibraryTaskValidationError),
            ):
                constructor()


class LibraryTaskServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeLibraryTaskClient()
        self.service = LibraryTaskService(self.client)

    def test_convenience_methods_submit_every_command_without_side_effects(
        self,
    ) -> None:
        methods = (
            lambda: self.service.list_libraries(),
            lambda: self.service.submit_index(IndexRequest("library-a")),
            lambda: self.service.submit_sync(SyncRequest("library-a")),
            lambda: self.service.submit_index_and_auto_tag(
                IndexAndAutoTagRequest("library-a", external_processing_confirmed=True)
            ),
            lambda: self.service.submit_stats(StatsRequest("library-a")),
            lambda: self.service.submit_roots(RootsRequest("library-a")),
            lambda: self.service.estimate_auto_tags(
                AutoTagEstimateRequest("library-a")
            ),
            lambda: self.service.submit_auto_tag(
                AutoTagRunRequest("library-a", external_processing_confirmed=True)
            ),
            lambda: self.service.list_pending_auto_tags(
                AutoTagPendingRequest("library-a")
            ),
            lambda: self.service.submit_auto_tag_review(
                AutoTagReviewRequest(
                    "library-a",
                    decisions=(
                        AutoTagReviewDecision(
                            "proposal-1", "accept", accepted_tags=("站姿",)
                        ),
                    ),
                )
            ),
            lambda: self.service.confirm_identity(
                IdentityConfirmationRequest(
                    "library-a",
                    "proposal-2",
                    accepted_tags=("刻晴",),
                    confirmed_identity_tags=("刻晴",),
                )
            ),
            lambda: self.service.accept_low_risk_batch(
                LowRiskBatchReviewRequest(
                    "library-a",
                    proposal_ids=("proposal-3",),
                    accepted_tags_by_proposal={"proposal-3": ("站姿",)},
                )
            ),
            lambda: self.service.undo_latest_auto_tag_batch(
                AutoTagUndoRequest("library-a")
            ),
            lambda: self.service.backfill_folder_tags(
                FolderTagBackfillRequest("library-a")
            ),
            lambda: self.service.backfill_metadata(
                MetadataBackfillRequest("library-a")
            ),
            lambda: self.service.list_tag_aliases(TagAliasListRequest("library-a")),
            lambda: self.service.upsert_tag_alias(
                TagAliasUpsertRequest("library-a", "雷电将军", ("影",))
            ),
            lambda: self.service.delete_tag_alias(
                TagAliasDeleteRequest("library-a", "雷电将军")
            ),
            lambda: self.service.cluster_images(ClusterImagesRequest("library-a")),
            lambda: self.service.list_clusters(ClusterListRequest("library-a")),
            lambda: self.service.cluster_detail(
                ClusterDetailRequest("library-a", "semantic-001")
            ),
            lambda: self.service.merge_clusters(
                ClusterMergeRequest("library-a", ("semantic-001", "semantic-002"))
            ),
            lambda: self.service.split_cluster(
                ClusterSplitRequest("library-a", "semantic-001", ("doc-1",))
            ),
            lambda: self.service.apply_cluster_identity(
                ClusterApplyIdentityRequest("library-a", "semantic-001", "work", "原神")
            ),
            lambda: self.service.undo_latest_cluster_operation(
                ClusterUndoRequest("library-a")
            ),
            lambda: self.service.active_learning_queue(
                ActiveLearningQueueRequest("library-a")
            ),
            lambda: self.service.review_active_learning(
                ActiveLearningReviewRequest(
                    "library-a",
                    "queue-001",
                    decisions=(ActiveLearningDecision("doc-1", "accept"),),
                )
            ),
            lambda: self.service.undo_latest_active_learning_review(
                ActiveLearningReviewUndoRequest("library-a")
            ),
        )

        submissions = [method() for method in methods]
        self.assertEqual(
            [command for command, _params in self.client.submissions],
            [
                "libraries",
                "index",
                "sync",
                "index_and_auto_tag",
                "stats",
                "roots",
                "auto_tag_estimate",
                "auto_tag",
                "auto_tag_pending",
                "auto_tag_review",
                "auto_tag_review",
                "auto_tag_review_batch",
                "auto_tag_review_undo",
                "folder_tag_backfill",
                "metadata_backfill",
                "tag_alias_list",
                "tag_alias_upsert",
                "tag_alias_delete",
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
            ],
        )
        self.assertEqual(
            [submission.job_id for submission in submissions],
            [f"job-{index}" for index in range(1, len(methods) + 1)],
        )

    def test_wait_returns_structured_success_and_progress(self) -> None:
        submission = self.service.submit_stats(StatsRequest("library-a"))
        self.client.jobs = [
            {
                "id": submission.job_id,
                "command": "stats",
                "status": "running",
                "progress": {"message": "Reading"},
            },
            {
                "id": submission.job_id,
                "command": "stats",
                "status": "succeeded",
                "result": {"documents": 42},
                "error": None,
            },
        ]
        updates: list[str] = []

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
            on_progress=lambda job: updates.append(str(job["status"])),
        )

        self.assertTrue(outcome.successful)
        self.assertEqual(outcome.result, {"documents": 42})
        self.assertEqual(updates, ["running", "succeeded"])

    def test_wait_forwards_cancel_and_accepts_cancelled_terminal_state(self) -> None:
        submission = self.service.submit_sync(SyncRequest("library-a"))
        event = threading.Event()
        event.set()
        self.client.jobs = [
            {"id": submission.job_id, "status": "running"},
            {"id": submission.job_id, "status": "cancelled"},
        ]

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
            cancel_event=event,
        )

        self.assertEqual(self.client.cancelled, [submission.job_id])
        self.assertEqual(outcome.status, "cancelled")

    def test_timeout_and_protocol_errors_are_structured(self) -> None:
        submission = self.service.submit_stats(StatsRequest("library-a"))
        with self.assertRaises(LibraryTaskWaitTimeout) as timed_out:
            self.service.wait(submission, timeout=0.002, poll_interval=0.001)
        self.assertEqual(timed_out.exception.to_dict()["code"], "library_task_timeout")

        self.client.jobs = [
            {
                "id": submission.job_id,
                "status": "succeeded",
                "result": None,
            }
        ]
        with self.assertRaises(LibraryTaskProtocolError) as invalid:
            self.service.wait(submission, timeout=1, poll_interval=0.001)
        self.assertEqual(invalid.exception.to_dict()["code"], "invalid_backend_job")

    def test_failed_job_requires_and_preserves_structured_error(self) -> None:
        submission = self.service.submit_stats(StatsRequest("library-a"))
        self.client.jobs = [
            {
                "id": submission.job_id,
                "status": "failed",
                "error": {
                    "code": "job_failed",
                    "message": "Collection unavailable",
                    "details": {"type": "ConfigurationError"},
                },
            }
        ]

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
        )

        self.assertFalse(outcome.successful)
        assert outcome.error is not None
        self.assertEqual(outcome.error["code"], "job_failed")


if __name__ == "__main__":
    unittest.main()
