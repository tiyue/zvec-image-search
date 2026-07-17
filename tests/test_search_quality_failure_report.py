from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.search_quality import failure_report

FIXTURE_DIR = Path(__file__).parent / "search_quality"


class SearchQualityFailureReportTest(unittest.TestCase):
    def setUp(self) -> None:
        source_dataset = json.loads(
            (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
        )
        self.dataset = copy.deepcopy(source_dataset)
        self.dataset["name"] = "pending-failure-analysis-fixture"
        for item in self.dataset["items"]:
            item["suggested_relevant_images"] = item["relevant_images"]
            item["relevant_images"] = []
            item["annotation"] = {
                "status": "pending",
                "annotator": None,
                "annotated_at": None,
                "notes": "Unverified test suggestion.",
            }

        self.run_data = json.loads(
            (FIXTURE_DIR / "deterministic_run.json").read_text("utf-8")
        )
        self.run_data["score_fields"] = {
            "text": "raw_score",
            "image": "raw_score",
            "combined": "confidence",
        }
        modes = {item["id"]: item["mode"] for item in self.dataset["items"]}
        for case in self.run_data["cases"]:
            mode = modes[case["id"]]
            for result in case["results"]:
                result["raw_score"] = result["score"]
                result["normalized_score"] = (
                    result["score"]
                    if mode == "combined"
                    else 1.0 - result["score"] / 2.0
                )
                result["confidence"] = result["normalized_score"]

    def test_reports_per_query_hits_misses_scores_and_gaps(self) -> None:
        report = failure_report.build_failure_report(
            self.dataset, self.run_data, worst_limit=3
        )

        self.assertTrue(report["draft"])
        self.assertFalse(report["baseline_eligible"])
        self.assertEqual(report["label_source"], "ai_assisted_suggestions")
        self.assertIn("suggested_relevant_images", report["warning"])
        text_queries = {
            value["id"]: value for value in report["by_query_type"]["text"]["queries"]
        }
        shapes = text_queries["text-shapes"]
        self.assertFalse(shapes["top5"][0]["relevant"])
        self.assertTrue(shapes["top5"][1]["relevant"])
        self.assertEqual(shapes["top5_hit_count"], 2)
        self.assertEqual(shapes["first_relevant_rank"], 2)
        self.assertEqual(shapes["missed_relevant_images"], [])
        self.assertEqual(shapes["false_positive_images_top5"], ["fixture:x"])
        self.assertEqual(shapes["ranking_score"]["field"], "raw_score")
        self.assertAlmostEqual(shapes["ranking_score"]["top1_top2_gap"]["gap"], 0.05)
        self.assertEqual(shapes["ranking_score"]["maximum_gap"]["after_rank"], 2)
        self.assertAlmostEqual(
            shapes["score_distributions"]["confidence"]["mean"],
            (0.925 + 0.9 + 0.85) / 3,
        )

    def test_highlights_no_answer_and_combined_confidence_failures(self) -> None:
        report = failure_report.build_failure_report(self.dataset, self.run_data)
        no_answer_queries = {
            value["id"]: value
            for value in report["by_query_type"]["no-answer"]["queries"]
        }

        self.assertAlmostEqual(
            no_answer_queries["text-no-answer"]["no_answer_highest_confidence"],
            0.825,
        )
        self.assertEqual(
            no_answer_queries["combined-no-answer"]["failure_categories"],
            ["false_return_on_no_answer"],
        )
        combined = report["by_query_type"]["combined"]["queries"][0]
        self.assertEqual(combined["ranking_score"]["field"], "confidence")
        self.assertAlmostEqual(combined["ranking_score"]["top1_top2_gap"]["gap"], 0.4)
        self.assertEqual(
            report["pattern_differences"]["summaries"]["no-answer"][
                "no_answer_false_return_rate"
            ],
            1.0,
        )
        self.assertGreaterEqual(len(report["algorithm_recommendations"]), 3)
        self.assertTrue(
            any(
                value["algorithm"] == "mode_specific_calibration_and_abstention"
                for value in report["algorithm_recommendations"]
            )
        )

    def test_human_verified_dataset_is_baseline_eligible(self) -> None:
        verified = copy.deepcopy(self.dataset)
        for item in verified["items"]:
            item["relevant_images"] = item["suggested_relevant_images"]
            item["annotation"]["status"] = "human_verified"

        report = failure_report.build_failure_report(verified, self.run_data)

        self.assertFalse(report["draft"])
        self.assertTrue(report["baseline_eligible"])
        self.assertEqual(report["label_source"], "human_verified")
        self.assertIsNone(report["warning"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.json"
            run_path = root / "run.json"
            output_path = root / "failure.json"
            markdown_path = root / "failure.md"
            dataset_path.write_text(
                json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8"
            )
            run_path.write_text(
                json.dumps(self.run_data, ensure_ascii=False), encoding="utf-8"
            )

            exit_code = failure_report.main(
                [
                    "--dataset",
                    str(dataset_path),
                    "--run",
                    str(run_path),
                    "--output",
                    str(output_path),
                    "--markdown",
                    str(markdown_path),
                    "--worst-limit",
                    "2",
                ]
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output_path.read_text("utf-8"))
            self.assertEqual(payload["kind"], "zvec-search-quality-failure-analysis")
            self.assertEqual(len(payload["worst_queries"]["answerable"]), 2)
            markdown = markdown_path.read_text("utf-8")
            self.assertIn("DRAFT ONLY", markdown)
            self.assertIn("combined queries", markdown)
            self.assertIn("Evidence-backed algorithm recommendations", markdown)


if __name__ == "__main__":
    unittest.main()
