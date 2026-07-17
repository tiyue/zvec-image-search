from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.config import ConfigurationError
from image_vector_service.search_quality import load_search_quality
from tests.search_quality import draft_report, evaluate, quality_gate

FIXTURE_DIR = Path(__file__).parent / "search_quality"


class SearchQualityDraftReportTest(unittest.TestCase):
    def setUp(self) -> None:
        source_dataset = json.loads(
            (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
        )
        self.dataset = copy.deepcopy(source_dataset)
        self.dataset["name"] = "ai-assisted-pending-fixture"
        for item in self.dataset["items"]:
            item["suggested_relevant_images"] = item["relevant_images"]
            item["relevant_images"] = []
            item["annotation"] = {
                "status": "pending",
                "annotator": None,
                "annotated_at": None,
                "notes": "AI-assisted suggestion; not human verified.",
            }

        self.run_data = json.loads(
            (FIXTURE_DIR / "deterministic_run.json").read_text("utf-8")
        )
        modes = {item["id"]: item["mode"] for item in self.dataset["items"]}
        for case in self.run_data["cases"]:
            mode = modes[case["id"]]
            for result in case["results"]:
                result["raw_score"] = result["score"]
                result["confidence"] = (
                    result["score"]
                    if mode == "combined"
                    else 1.0 - result["score"] / 2.0
                )

    def test_draft_metrics_match_formal_evaluator_math(self) -> None:
        report = draft_report.build_draft_report(self.dataset, self.run_data)

        self.assertTrue(report["draft"])
        self.assertFalse(report["baseline_eligible"])
        self.assertEqual(report["label_source"], "ai_assisted_suggestions")
        self.assertEqual(report["kind"], "zvec-search-quality-ai-assisted-draft")
        metrics = report["metrics"]
        self.assertAlmostEqual(metrics["precision_at_5"], 13 / 24)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.875)
        self.assertEqual(metrics["no_answer_false_return_rate"], 1.0)
        self.assertAlmostEqual(metrics["average_result_count"], 12 / 7)
        self.assertAlmostEqual(metrics["latency_ms"]["average"], 75 / 7)
        self.assertEqual(metrics["api_requests"]["total"], 9)

    def test_thresholds_are_diagnostics_not_production_configuration(self) -> None:
        report = draft_report.build_draft_report(self.dataset, self.run_data)
        exploration = report["threshold_exploration"]

        self.assertTrue(exploration["draft_only"])
        self.assertFalse(exploration["production_config_generated"])
        for mode in ("text", "image", "combined"):
            self.assertEqual(
                set(exploration["modes"][mode]["fields"]),
                {"confidence", "raw_score"},
            )
            for field in ("confidence", "raw_score"):
                value = exploration["modes"][mode]["fields"][field]
                self.assertGreater(value["evaluated_thresholds"], 0)
                self.assertGreater(value["eligible_candidate_count"], 0)
                self.assertIsNotNone(value["best_candidate"])

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search-quality.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_search_quality(Path(temporary))

    def test_formal_evaluator_and_quality_gate_reject_draft(self) -> None:
        report = draft_report.build_draft_report(self.dataset, self.run_data)

        with self.assertRaisesRegex(ValueError, "no eligible annotated cases"):
            evaluate.evaluate_dataset(self.dataset, self.run_data)
        with self.assertRaisesRegex(ValueError, "not a search-quality evaluation"):
            quality_gate.assess_quality_gate(report, report)

    def test_pending_suggestion_contract_is_strict(self) -> None:
        missing = copy.deepcopy(self.dataset)
        missing["items"][0]["suggested_relevant_images"] = []
        with self.assertRaisesRegex(ValueError, "at least one suggested"):
            draft_report.build_draft_report(missing, self.run_data)

        verified = copy.deepcopy(self.dataset)
        verified["items"][0]["annotation"]["status"] = "human_verified"
        verified["items"][0]["relevant_images"] = verified["items"][0][
            "suggested_relevant_images"
        ]
        with self.assertRaisesRegex(ValueError, "pending annotations only"):
            draft_report.build_draft_report(verified, self.run_data)

    def test_cli_writes_explicit_draft_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.local.json"
            run_path = root / "run.local.json"
            output_path = root / "draft-report.local.json"
            markdown_path = root / "draft-report.local.md"
            dataset_path.write_text(
                json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8"
            )
            run_path.write_text(
                json.dumps(self.run_data, ensure_ascii=False), encoding="utf-8"
            )

            exit_code = draft_report.main(
                [
                    "--dataset",
                    str(dataset_path),
                    "--run",
                    str(run_path),
                    "--output",
                    str(output_path),
                    "--markdown",
                    str(markdown_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output_path.read_text("utf-8"))
            self.assertTrue(payload["draft"])
            self.assertFalse(payload["baseline_eligible"])
            markdown = markdown_path.read_text("utf-8")
            self.assertIn("DRAFT ONLY", markdown)
            self.assertIn("never writes", markdown)


if __name__ == "__main__":
    unittest.main()
