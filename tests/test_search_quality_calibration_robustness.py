from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.search_quality import load_search_quality
from tests.search_quality import calibrate, evaluate

FIXTURE_DIR = Path(__file__).parent / "search_quality"


def _robust_fixture() -> tuple[dict, dict]:
    source_dataset = json.loads(
        (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
    )
    source_run = json.loads((FIXTURE_DIR / "deterministic_run.json").read_text("utf-8"))
    source_cases = {case["id"]: case for case in source_run["cases"]}
    items = []
    cases = []
    for mode in sorted(evaluate.MODES):
        answer_template = next(
            item
            for item in source_dataset["items"]
            if item["mode"] == mode and item["query_type"] != "no-answer"
        )
        no_answer_template = next(
            item
            for item in source_dataset["items"]
            if item["mode"] == mode and item["query_type"] == "no-answer"
        )
        for index in range(2):
            answer = copy.deepcopy(answer_template)
            answer_id = f"{mode}-robust-answer-{index + 1}"
            relevant_id = f"fixture:{answer_id}:relevant"
            answer["id"] = answer_id
            answer["relevant_images"] = [{"image_id": relevant_id}]
            answer["annotation"] = {
                "status": "synthetic_fixture",
                "notes": "robust calibration fixture",
            }
            answer_case = copy.deepcopy(source_cases[answer_template["id"]])
            answer_case["id"] = answer_id
            if mode == "combined":
                relevant_score = 0.900 - index * 0.005
                noise_score = 0.600
            else:
                relevant_score = 0.100 + index * 0.005
                noise_score = 0.400
            answer_case["results"] = [
                {"image_id": relevant_id, "score": relevant_score},
                {
                    "image_id": f"fixture:{answer_id}:noise",
                    "score": noise_score,
                },
            ]
            items.append(answer)
            cases.append(answer_case)

            no_answer = copy.deepcopy(no_answer_template)
            no_answer_id = f"{mode}-robust-no-answer-{index + 1}"
            no_answer["id"] = no_answer_id
            no_answer["relevant_images"] = []
            no_answer["annotation"] = {
                "status": "synthetic_fixture",
                "notes": "robust calibration fixture",
            }
            no_answer_case = copy.deepcopy(source_cases[no_answer_template["id"]])
            no_answer_case["id"] = no_answer_id
            no_answer_score = (
                0.200 - index * 0.010 if mode == "combined" else 0.800 + index * 0.010
            )
            no_answer_case["results"] = [
                {
                    "image_id": f"fixture:{no_answer_id}:false",
                    "score": no_answer_score,
                }
            ]
            items.append(no_answer)
            cases.append(no_answer_case)

    dataset = copy.deepcopy(source_dataset)
    dataset["name"] = "robust-calibration-fixture"
    dataset["items"] = items
    run = copy.deepcopy(source_run)
    run["name"] = "robust-calibration-run"
    run["cases"] = cases
    return dataset, run


class SearchQualityCalibrationRobustnessTest(unittest.TestCase):
    def test_robust_candidate_is_deterministic_and_passes_inner_cv(self):
        dataset, run = _robust_fixture()
        configuration = calibrate.calibrate_dataset(
            dataset,
            run,
            allow_synthetic=True,
            selection_strategy=calibrate.SELECTION_ROBUST_INNER_CV,
        )

        selection = configuration["diagnostics"]["selection"]
        self.assertEqual(
            selection["strategy"],
            calibrate.SELECTION_ROBUST_INNER_CV,
        )
        self.assertTrue(selection["candidate_only"])
        self.assertEqual(selection["api_requests_added"], 0)
        for mode in sorted(evaluate.MODES):
            threshold = configuration["thresholds"][mode]
            self.assertEqual(threshold["score_gap"], calibrate.ROBUST_SCORE_GAP)
            self.assertEqual(
                threshold["max_confidence_drop"],
                calibrate.ROBUST_MAX_CONFIDENCE_DROP,
            )
            margin = threshold["threshold_margin"]
            self.assertEqual(
                margin["fraction_toward_no_answer_boundary"],
                calibrate.ROBUST_THRESHOLD_MARGIN_FRACTION,
            )
            self.assertAlmostEqual(margin["reserved_rejection_fraction"], 0.95)
            self.assertNotEqual(threshold["value"], margin["source_value"])
            cross_validation = selection["inner_cross_validation"][mode]
            self.assertTrue(cross_validation["constraints_passed"])
            self.assertEqual(cross_validation["fold_count"], 2)
            self.assertEqual(cross_validation["api_requests_added"], 0)
            self.assertEqual(
                cross_validation["out_of_fold_metrics"]["no_answer_false_return_rate"],
                0.0,
            )
            self.assertLessEqual(
                cross_validation["out_of_fold_recall_at_5_drop"],
                calibrate.DEFAULT_MAX_RECALL_DROP,
            )

        reversed_dataset = copy.deepcopy(dataset)
        reversed_dataset["items"].reverse()
        reversed_run = copy.deepcopy(run)
        reversed_run["cases"].reverse()
        repeated = calibrate.calibrate_dataset(
            reversed_dataset,
            reversed_run,
            allow_synthetic=True,
            selection_strategy=calibrate.SELECTION_ROBUST_INNER_CV,
        )
        self.assertEqual(configuration["thresholds"], repeated["thresholds"])
        self.assertEqual(
            selection["inner_cross_validation"],
            repeated["diagnostics"]["selection"]["inner_cross_validation"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "search-quality.json"
            destination.write_text(json.dumps(configuration), encoding="utf-8")
            loaded = load_search_quality(Path(temporary))
            self.assertTrue(loaded.configured)
            self.assertEqual(
                loaded.text.minimum,
                configuration["text"]["minimum_confidence"],
            )

    def test_robust_candidate_fails_closed_without_two_cases_per_stratum(self):
        dataset = json.loads(
            (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
        )
        run = json.loads((FIXTURE_DIR / "deterministic_run.json").read_text("utf-8"))
        with self.assertRaisesRegex(
            ValueError,
            "at least two answerable and two no-answer cases",
        ):
            calibrate.calibrate_dataset(
                dataset,
                run,
                allow_synthetic=True,
                selection_strategy=calibrate.SELECTION_ROBUST_INNER_CV,
            )


if __name__ == "__main__":
    unittest.main()
