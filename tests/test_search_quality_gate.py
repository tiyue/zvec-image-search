from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.search_quality import evaluate, quality_gate

FIXTURE_DIR = Path(__file__).parent / "search_quality"


class SearchQualityGateTest(unittest.TestCase):
    def test_requires_fully_human_verified_dataset_by_default(self):
        synthetic = evaluate._read_json(FIXTURE_DIR / "deterministic_dataset.json")
        with self.assertRaisesRegex(ValueError, "fully human-verified"):
            quality_gate.validate_gate_dataset(synthetic)

        pending = evaluate._read_json(FIXTURE_DIR / "dataset.json")
        with self.assertRaisesRegex(ValueError, "pending=60"):
            quality_gate.validate_gate_dataset(pending)

    def test_one_shot_gate_generates_all_reports_and_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "quality-report"
            before_run = evaluate._read_json(FIXTURE_DIR / "deterministic_run.json")
            hidden_relevant_cases = {"text-red", "image-similar", "combined-match"}
            for case in before_run["cases"]:
                if case["id"] in hidden_relevant_cases:
                    case["results"] = [
                        {
                            "image_id": f"fixture:{case['id']}-noise-{index}",
                            "score": float(index),
                        }
                        for index in range(1, 6)
                    ] + case["results"]
            before_path = Path(temporary) / "before-run.json"
            before_path.write_text(json.dumps(before_run), encoding="utf-8")
            exit_code = quality_gate.main(
                [
                    "--dataset",
                    str(FIXTURE_DIR / "deterministic_dataset.json"),
                    "--before-run",
                    str(before_path),
                    "--after-run",
                    str(FIXTURE_DIR / "deterministic_after_run.json"),
                    "--output-dir",
                    str(output_dir),
                    "--allow-test-fixtures",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {
                    "before-evaluation.json",
                    "before-evaluation.md",
                    "after-evaluation.json",
                    "after-evaluation.md",
                    "comparison.json",
                    "comparison.md",
                },
            )
            comparison = json.loads((output_dir / "comparison.json").read_text("utf-8"))
            self.assertEqual(
                comparison["before"]["source_sha256"],
                evaluate.source_file_sha256(before_path),
            )
            self.assertEqual(
                comparison["after"]["source_sha256"],
                evaluate.source_file_sha256(
                    FIXTURE_DIR / "deterministic_after_run.json"
                ),
            )
            for identity in (comparison["before"], comparison["after"]):
                self.assertEqual(
                    identity["fingerprint_algorithm"],
                    evaluate.RUN_FINGERPRINT_ALGORITHM,
                )
                self.assertEqual(
                    identity["source_sha256_algorithm"],
                    evaluate.SOURCE_FILE_SHA256_ALGORITHM,
                )
            gate = comparison["quality_gate"]
            self.assertEqual(gate["status"], "pass")
            self.assertTrue(all(check["passed"] for check in gate["checks"].values()))
            self.assertAlmostEqual(
                gate["checks"]["precision_at_5_gain"]["actual"],
                0.15,
            )
            self.assertEqual(
                gate["checks"]["precision_at_5_gain"]["metric"],
                "strict_precision_at_5",
            )
            self.assertEqual(gate["checks"]["api_request_increase"]["actual"], 0)
            markdown = (output_dir / "comparison.md").read_text("utf-8")
            self.assertIn("Overall: PASS", markdown)
            self.assertIn("P95 latency increase", markdown)

    def test_gate_rejects_selective_precision_gain_without_standard_p_at_5_gain(
        self,
    ):
        dataset = evaluate._read_json(FIXTURE_DIR / "deterministic_dataset.json")
        before = evaluate.evaluate_dataset(
            dataset,
            evaluate._read_json(FIXTURE_DIR / "deterministic_run.json"),
            allow_synthetic=True,
        )
        after = evaluate.evaluate_dataset(
            dataset,
            evaluate._read_json(FIXTURE_DIR / "deterministic_after_run.json"),
            allow_synthetic=True,
        )

        self.assertGreater(
            after["metrics"]["precision_at_5"] - before["metrics"]["precision_at_5"],
            0.15,
        )
        self.assertEqual(
            after["metrics"]["strict_precision_at_5"],
            before["metrics"]["strict_precision_at_5"],
        )
        gate = quality_gate.assess_quality_gate(before, after)
        self.assertFalse(gate["checks"]["precision_at_5_gain"]["passed"])
        self.assertEqual(
            gate["checks"]["precision_at_5_gain"]["actual"],
            0.0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                quality_gate.main(
                    [
                        "--dataset",
                        str(FIXTURE_DIR / "deterministic_dataset.json"),
                        "--before-run",
                        str(FIXTURE_DIR / "deterministic_run.json"),
                        "--after-run",
                        str(FIXTURE_DIR / "deterministic_after_run.json"),
                        "--output-dir",
                        temporary,
                        "--allow-test-fixtures",
                    ]
                ),
                1,
            )

    def test_gate_fails_closed_without_matching_dataset_fingerprints(self):
        dataset = evaluate._read_json(FIXTURE_DIR / "deterministic_dataset.json")
        report = evaluate.evaluate_dataset(
            dataset,
            evaluate._read_json(FIXTURE_DIR / "deterministic_run.json"),
            allow_synthetic=True,
        )
        before = copy.deepcopy(report)
        after = copy.deepcopy(report)
        before["dataset"].pop("fingerprint")
        after["dataset"].pop("fingerprint")
        with self.assertRaisesRegex(ValueError, "fingerprint is missing"):
            quality_gate.assess_quality_gate(before, after)

        after = copy.deepcopy(report)
        after["dataset"]["fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "different dataset fingerprints"):
            quality_gate.assess_quality_gate(report, after)

    def test_failed_gate_returns_one_but_still_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            exit_code = quality_gate.main(
                [
                    "--dataset",
                    str(FIXTURE_DIR / "deterministic_dataset.json"),
                    "--before-run",
                    str(FIXTURE_DIR / "deterministic_run.json"),
                    "--after-run",
                    str(FIXTURE_DIR / "deterministic_run.json"),
                    "--output-dir",
                    str(output_dir),
                    "--allow-test-fixtures",
                ]
            )

            self.assertEqual(exit_code, 1)
            comparison = json.loads((output_dir / "comparison.json").read_text("utf-8"))
            self.assertEqual(comparison["quality_gate"]["status"], "fail")
            self.assertFalse(
                comparison["quality_gate"]["checks"]["precision_at_5_gain"]["passed"]
            )
            self.assertTrue((output_dir / "comparison.md").is_file())


if __name__ == "__main__":
    unittest.main()
