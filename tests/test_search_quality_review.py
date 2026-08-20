from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from tests.search_quality import review

FIXTURE_DIR = Path(__file__).parent / "search_quality"


def _pending_annotation() -> dict[str, Any]:
    return {
        "status": "pending",
        "annotator": None,
        "annotated_at": None,
        "notes": "Unverified suggestion.",
    }


def _dataset() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "review-test",
        "items": [
            {
                "id": "text-001",
                "query_type": "text",
                "mode": "text",
                "query": {"text": "red sunset"},
                "library_scope": {"mode": "all_enabled", "library_ids": []},
                "relevant_images": [],
                "suggested_relevant_images": [
                    {"image_id": "library-a:photos/sunset-01.jpg"},
                    {"image_id": "library-a:photos/sunset-02.jpg"},
                ],
                "annotation": _pending_annotation(),
            },
            {
                "id": "no-answer-001",
                "query_type": "no-answer",
                "mode": "text",
                "query": {"text": "a subject absent from the library"},
                "library_scope": {"mode": "all_enabled", "library_ids": []},
                "relevant_images": [],
                "suggested_relevant_images": [],
                "annotation": _pending_annotation(),
            },
        ],
    }


class LocalDataset:
    def __init__(self, value: dict[str, Any] | None = None):
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".local.json",
            prefix="review-test-",
            dir=FIXTURE_DIR,
            delete=False,
        ) as handle:
            self.path = Path(handle.name)
            json.dump(
                value if value is not None else _dataset(),
                handle,
                ensure_ascii=False,
            )

    def read(self) -> dict[str, Any]:
        value = json.loads(self.path.read_text("utf-8"))
        assert isinstance(value, dict)
        return value

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        for leftover in FIXTURE_DIR.glob(f".{self.path.name}.*.tmp"):
            leftover.unlink(missing_ok=True)


class SearchQualityReviewTest(unittest.TestCase):
    def setUp(self):
        self.local = LocalDataset()

    def tearDown(self):
        self.local.close()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = review.main(["--dataset", str(self.local.path), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_list_and_show_expose_pending_query_and_unverified_suggestions(self):
        code, output, error = self.run_cli("list")
        self.assertEqual(code, 0, error)
        self.assertIn("text-001", output)
        self.assertIn("no-answer-001", output)

        code, output, error = self.run_cli("show", "text-001")
        self.assertEqual(code, 0, error)
        self.assertIn("red sunset", output)
        self.assertIn("Suggested relevant images (2; unverified)", output)
        self.assertIn("library-a:photos/sunset-01.jpg", output)

    def test_accept_stages_suggestions_but_does_not_verify_or_add_labels(self):
        code, output, error = self.run_cli("accept", "text-001")
        self.assertEqual(code, 0, error)
        self.assertIn("status remains pending", output)
        item = self.local.read()["items"][0]
        self.assertEqual(item["annotation"]["status"], "pending")
        self.assertEqual(item["relevant_images"], [])
        self.assertEqual(
            len(item["review_draft"]["relevant_images"]),
            2,
        )

    def test_replace_then_explicit_confirm_verifies_exactly_one_item(self):
        replacement = "library-a:photos/selected.jpg"
        code, _, error = self.run_cli("replace", "text-001", "--image-id", replacement)
        self.assertEqual(code, 0, error)
        code, output, error = self.run_cli(
            "confirm",
            "text-001",
            "--annotator",
            "reviewer@example",
            "--notes",
            "Inspected full-size image.",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("exactly one item", output)

        items = self.local.read()["items"]
        answerable = items[0]
        no_answer = items[1]
        self.assertEqual(answerable["annotation"]["status"], "human_verified")
        self.assertEqual(answerable["annotation"]["annotator"], "reviewer@example")
        self.assertTrue(str(answerable["annotation"]["annotated_at"]).endswith("Z"))
        self.assertEqual(
            answerable["annotation"]["notes"], "Inspected full-size image."
        )
        self.assertEqual(answerable["relevant_images"], [{"image_id": replacement}])
        self.assertNotIn("review_draft", answerable)
        self.assertEqual(no_answer["annotation"]["status"], "pending")

    def test_no_answer_requires_explicit_flag_and_preserves_empty_relevance(self):
        code, _, error = self.run_cli(
            "confirm", "no-answer-001", "--annotator", "reviewer"
        )
        self.assertEqual(code, 2)
        self.assertIn("explicit --no-answer", error)

        code, _, error = self.run_cli(
            "confirm",
            "no-answer-001",
            "--annotator",
            "reviewer",
            "--no-answer",
        )
        self.assertEqual(code, 0, error)
        item = self.local.read()["items"][1]
        self.assertEqual(item["annotation"]["status"], "human_verified")
        self.assertEqual(item["relevant_images"], [])

    def test_answerable_confirmation_rejects_empty_relevance(self):
        before = self.local.path.read_bytes()
        code, _, error = self.run_cli("confirm", "text-001", "--annotator", "reviewer")
        self.assertEqual(code, 2)
        self.assertIn("non-empty relevance", error)
        self.assertEqual(self.local.path.read_bytes(), before)

    def test_summary_status_and_validate_report_progress(self):
        code, output, error = self.run_cli("summary")
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(summary["human_verified"], 0)

        code, output, error = self.run_cli("status", "text-001")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "pending")

        code, output, error = self.run_cli("validate")
        self.assertEqual(code, 0, error)
        self.assertIn("Valid review dataset", output)

    def test_synthetic_fixture_is_rejected(self):
        value = _dataset()
        item = value["items"][0]
        item["annotation"] = {
            "status": "synthetic_fixture",
            "annotator": None,
            "annotated_at": None,
            "notes": "test fixture",
        }
        self.local.close()
        self.local = LocalDataset(value)
        code, _, error = self.run_cli("validate")
        self.assertEqual(code, 2)
        self.assertIn("synthetic_fixture cannot be human-reviewed", error)

    def test_path_traversal_is_rejected_for_dataset_query_and_image_id(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".local.json") as outside,
            self.assertRaisesRegex(review.ReviewError, "outside review area"),
        ):
            review._resolve_dataset_path(outside.name)

        value = _dataset()
        image_item = value["items"][0]
        image_item["query_type"] = "image"
        image_item["mode"] = "image"
        image_item["query"] = {"image": "../private.jpg"}
        self.local.close()
        self.local = LocalDataset(value)
        code, _, error = self.run_cli("validate")
        self.assertEqual(code, 2)
        self.assertIn("path traversal", error)

        self.local.close()
        self.local = LocalDataset()
        code, _, error = self.run_cli(
            "replace", "text-001", "--image-id", "library-a:../private.jpg"
        )
        self.assertEqual(code, 2)
        self.assertIn("path traversal", error)

    def test_verified_item_with_stale_draft_is_rejected(self):
        value = _dataset()
        item = value["items"][0]
        item["relevant_images"] = [{"image_id": "library-a:photos/sunset-01.jpg"}]
        item["annotation"] = {
            "status": "human_verified",
            "annotator": "reviewer",
            "annotated_at": "2026-07-13T12:00:00Z",
            "notes": "checked",
        }
        item["review_draft"] = {
            "relevant_images": [{"image_id": "library-a:photos/sunset-02.jpg"}]
        }
        self.local.close()
        self.local = LocalDataset(copy.deepcopy(value))
        code, _, error = self.run_cli("validate")
        self.assertEqual(code, 2)
        self.assertIn("cannot retain review_draft", error)


if __name__ == "__main__":
    unittest.main()
