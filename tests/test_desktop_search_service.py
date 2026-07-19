from __future__ import annotations

import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zvec_desktop.search_service import (
    SearchProtocolError,
    SearchRequest,
    SearchService,
    SearchValidationError,
    SearchWaitTimeout,
    normalize_search_request,
)


class FakeSearchClient:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self.jobs: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.submit_error: Exception | None = None

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.submissions.append((command, dict(params or {})))
        if self.submit_error is not None:
            raise self.submit_error
        return {"id": "job-1", "status": "queued"}

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not self.jobs:
            return {"id": job_id, "status": "running"}
        if len(self.jobs) == 1:
            return dict(self.jobs[0])
        return dict(self.jobs.pop(0))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"id": job_id, "status": "cancelled"}


class SearchRequestValidationTest(unittest.TestCase):
    def test_text_image_and_combined_query_types(self) -> None:
        self.assertEqual(
            normalize_search_request(SearchRequest(text="  海边日落 ")).query_type,
            "text",
        )
        self.assertEqual(
            normalize_search_request(SearchRequest(image_path="query.jpg")).query_type,
            "image",
        )
        combined = normalize_search_request(
            SearchRequest(
                text="角色动作",
                image_path="query.jpg",
                image_weight=7,
                text_weight=3,
            )
        )
        self.assertEqual(combined.query_type, "combined")
        self.assertAlmostEqual(combined.image_weight, 0.7)
        self.assertAlmostEqual(combined.text_weight, 0.3)

    def test_tag_query_is_fuzzy_text_only_mode(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(text=" 神 ", search_mode="tags")
        )
        self.assertEqual(normalized.query_type, "tag")
        self.assertEqual(normalized.text, "神")
        with self.assertRaisesRegex(SearchValidationError, "不能同时提供图片"):
            normalize_search_request(
                SearchRequest(text="原", image_path="a.jpg", search_mode="tags")
            )

    def test_values_are_deduplicated_and_candidate_budget_covers_top_k(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(
                text="cosplay",
                top_k=15,
                candidate_k=50,
                library_ids=("lib-a", "lib-a", "lib-b"),
                tags=(" 原神 ", "原神", "动作"),
            )
        )
        self.assertEqual(normalized.library_ids, ("lib-a", "lib-b"))
        self.assertEqual(normalized.tags, ("原神", "动作"))
        expanded = normalize_search_request(
            SearchRequest(text="x", top_k=15, candidate_k=10)
        )
        self.assertEqual(expanded.candidate_k, 15)
        self.assertEqual(expanded.sort_mode, "confidence")

    def test_result_count_has_no_legacy_500_item_product_cap(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(text="cosplay", top_k=750, candidate_k=750)
        )
        self.assertEqual(normalized.top_k, 750)
        self.assertEqual(normalized.candidate_k, 750)

    def test_empty_and_invalid_weight_requests_are_rejected(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "请输入"):
            normalize_search_request(SearchRequest(text="  "))
        with self.assertRaisesRegex(SearchValidationError, "不能同时为零"):
            normalize_search_request(
                SearchRequest(
                    text="x",
                    image_path="a.jpg",
                    image_weight=0,
                    text_weight=0,
                )
            )

    def test_invalid_sort_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "sort_mode"):
            normalize_search_request(
                SearchRequest(text="x", sort_mode="unknown")  # type: ignore[arg-type]
            )


class SearchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.query_root = self.root / "query-staging"
        self.client = FakeSearchClient()
        self.service = SearchService(self.client, self.query_root)

    def _image(self, name: str = "查询 图片.JPG") -> Path:
        path = self.root / name
        path.write_bytes(b"not-real-image-but-copy-contract-is-valid")
        return path

    def _output(self) -> Path:
        output = self.root / "results" / "search-1"
        output.mkdir(parents=True)
        (output / "results.json").write_text(
            '{"schema_version":1,"results":[]}', encoding="utf-8"
        )
        return output

    def test_submit_stages_image_and_builds_exact_backend_payload(self) -> None:
        source = self._image()
        submission = self.service.submit(
            SearchRequest(
                text=" 雷电将军 ",
                image_path=source,
                library_ids=("lib-a", "lib-b"),
                tags=("原神",),
                top_k=15,
                candidate_k=50,
                image_weight=8,
                text_weight=2,
            )
        )

        self.assertEqual(submission.query_type, "combined")
        self.assertIsNotNone(submission.staged_image)
        assert submission.staged_image is not None
        self.assertEqual(submission.staged_image.parent, self.query_root.resolve())
        self.assertEqual(submission.staged_image.read_bytes(), source.read_bytes())
        command, params = self.client.submissions[0]
        self.assertEqual(command, "search")
        self.assertEqual(params["text"], "雷电将军")
        self.assertEqual(params["image"], str(submission.staged_image))
        self.assertEqual(params["library_ids"], ["lib-a", "lib-b"])
        self.assertEqual(params["tags"], ["原神"])
        self.assertEqual(params["top_k"], 15)
        self.assertEqual(params["candidate_k"], 50)
        self.assertEqual(params["sort_mode"], "confidence")
        self.assertAlmostEqual(params["image_weight"], 0.8)
        self.assertAlmostEqual(params["text_weight"], 0.2)

    def test_submit_failure_removes_private_staging_copy(self) -> None:
        self.client.submit_error = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.service.submit(SearchRequest(image_path=self._image()))
        self.assertEqual(list(self.query_root.glob("*")), [])

    def test_wait_returns_manifest_reports_progress_and_cleans_staging(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        output = self._output()
        self.client.jobs = [
            {"id": "job-1", "status": "running", "progress": {"done": 1}},
            {
                "id": "job-1",
                "status": "succeeded",
                "progress": {"done": 2},
                "result": {"output_dir": str(output), "result_count": 0},
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
        self.assertEqual(outcome.manifest_path, output / "results.json")
        self.assertEqual(updates, ["running", "succeeded"])
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())

    def test_wait_forwards_cancellation_and_waits_for_terminal_proof(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        event = threading.Event()
        event.set()
        self.client.jobs = [
            {"id": "job-1", "status": "running"},
            {"id": "job-1", "status": "cancelled", "error": None},
        ]

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
            cancel_event=event,
        )

        self.assertEqual(self.client.cancelled, ["job-1"])
        self.assertEqual(outcome.status, "cancelled")
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())

    def test_timeout_retains_image_for_a_still_running_backend(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        with self.assertRaises(SearchWaitTimeout):
            self.service.wait(submission, timeout=0.002, poll_interval=0.001)
        assert submission.staged_image is not None
        self.assertTrue(submission.staged_image.exists())

    def test_success_without_manifest_is_a_protocol_error_and_still_cleans(
        self,
    ) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        output = self.root / "missing-manifest"
        output.mkdir()
        self.client.jobs = [
            {
                "id": "job-1",
                "status": "succeeded",
                "result": {"output_dir": str(output)},
            }
        ]
        with self.assertRaisesRegex(SearchProtocolError, "results.json"):
            self.service.wait(submission, timeout=1, poll_interval=0.001)
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())


if __name__ == "__main__":
    unittest.main()
