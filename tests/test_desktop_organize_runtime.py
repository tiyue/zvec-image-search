from __future__ import annotations

import unittest
from collections.abc import Mapping
from concurrent.futures import Future
from typing import Any

from zvec_desktop.library_tasks import (
    AutoTagPendingRequest,
    AutoTagReviewDecision,
    AutoTagReviewRequest,
    TagAliasListRequest,
)
from zvec_desktop.organize_runtime import (
    OrganizeJobError,
    OrganizeRuntimeError,
    RuntimeOrganizeOperations,
)


class FakeAsyncJobController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.next_future: Future[dict[str, Any]] = Future()

    def submit_job(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
        *,
        wait_for_completion: bool = True,
        poll_interval: float | None = None,
    ) -> Future[dict[str, Any]]:
        del poll_interval
        self.calls.append((command, dict(params or {}), wait_for_completion))
        return self.next_future


class RuntimeOrganizeOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FakeAsyncJobController()
        self.operations = RuntimeOrganizeOperations(self.controller)

    def test_pending_request_maps_terminal_job_to_result_payload(self) -> None:
        result_future = self.operations.load_pending(
            AutoTagPendingRequest("library-a", offset=100, limit=100)
        )
        self.controller.next_future.set_result(
            {
                "id": "job-1",
                "command": "auto_tag_pending",
                "status": "succeeded",
                "result": {"proposals": [], "pending_count": 0},
            }
        )

        self.assertEqual(
            result_future.result(),
            {"proposals": [], "pending_count": 0},
        )
        command, params, waits = self.controller.calls[0]
        self.assertEqual(command, "auto_tag_pending")
        self.assertEqual(params["library_id"], "library-a")
        self.assertEqual(params["offset"], 100)
        self.assertTrue(waits)

    def test_review_and_alias_methods_submit_their_validated_contracts(self) -> None:
        review = AutoTagReviewRequest(
            "library-a",
            (AutoTagReviewDecision("proposal-1", "accept", ("动作",)),),
        )
        self.operations.submit_review(review)
        self.assertEqual(self.controller.calls[-1][0], "auto_tag_review")

        self.controller.next_future = Future()
        self.operations.list_aliases(TagAliasListRequest("library-a"))
        self.assertEqual(self.controller.calls[-1][0], "tag_alias_list")

    def test_failed_job_surfaces_status_id_and_backend_error(self) -> None:
        result_future = self.operations.load_pending(AutoTagPendingRequest("library-a"))
        self.controller.next_future.set_result(
            {
                "id": "job-failed",
                "command": "auto_tag_pending",
                "status": "failed",
                "result": None,
                "error": {"code": "bad_data", "message": "proposal damaged"},
            }
        )

        with self.assertRaises(OrganizeJobError) as raised:
            result_future.result()

        self.assertEqual(raised.exception.job_id, "job-failed")
        self.assertEqual(raised.exception.status, "failed")
        self.assertIn("proposal damaged", str(raised.exception))

    def test_success_without_result_is_a_protocol_error(self) -> None:
        result_future = self.operations.load_pending(AutoTagPendingRequest("library-a"))
        self.controller.next_future.set_result(
            {
                "id": "job-1",
                "command": "auto_tag_pending",
                "status": "succeeded",
                "result": None,
            }
        )
        with self.assertRaisesRegex(OrganizeRuntimeError, "没有返回结果对象"):
            result_future.result()

    def test_controller_failure_is_forwarded_to_panel_future(self) -> None:
        result_future = self.operations.load_pending(AutoTagPendingRequest("library-a"))
        self.controller.next_future.set_exception(RuntimeError("backend offline"))
        with self.assertRaisesRegex(RuntimeError, "backend offline"):
            result_future.result()


if __name__ == "__main__":
    unittest.main()
