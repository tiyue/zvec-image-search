from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from tools.search_learning.calibration import (
    CalibrationExample,
    apply_calibrator,
    build_calibration_registry,
    calibration_metrics,
)
from tools.search_learning.common import (
    SearchLearningToolError,
    atomic_write_json,
    load_records,
    stable_sigmoid,
)
from tools.search_learning.evaluate_calibration import evaluate_registry
from tools.search_learning.ranker import RankerExample, train_ranker


class SearchLearningRankerToolTests(unittest.TestCase):
    def _examples(self) -> list[RankerExample]:
        examples = []
        for index in range(40):
            label = int(index >= 20)
            confidence = 0.1 + (0.8 * label) + (index % 5) * 0.005
            examples.append(
                RankerExample(
                    label=label,
                    features={
                        "vector_confidence": confidence,
                        "manual_tag_matches": float(label),
                    },
                    group_id=f"query-{index}",
                )
            )
        return examples

    def test_ranker_training_is_deterministic_and_emits_runtime_schema(self) -> None:
        first = train_ranker(
            self._examples(),
            model_version="ranker-test",
            validation_ratio=0.25,
            seed="fixed",
        )
        second = train_ranker(
            self._examples(),
            model_version="ranker-test",
            validation_ratio=0.25,
            seed="fixed",
        )

        self.assertEqual(first[0], second[0])
        model, report = first
        self.assertEqual(model["schema_version"], 1)
        self.assertEqual(model["feature_schema_version"], 1)
        self.assertEqual(model["kind"], "logistic")
        self.assertGreater(model["weights"]["vector_confidence"], 0)
        negative = stable_sigmoid(
            model["intercept"] + model["weights"]["vector_confidence"] * 0.1
        )
        positive = stable_sigmoid(
            model["intercept"]
            + model["weights"]["vector_confidence"] * 0.9
            + model["weights"]["manual_tag_matches"]
        )
        self.assertGreater(positive, negative)
        self.assertFalse(report["activation_allowed"])

    def test_ranker_rejects_single_class_training_data(self) -> None:
        examples = [
            RankerExample(1, {"vector_confidence": index / 10}, group_id=str(index))
            for index in range(4)
        ]
        with self.assertRaisesRegex(SearchLearningToolError, "positive and negative"):
            train_ranker(examples, model_version="invalid")


class SearchLearningCalibrationToolTests(unittest.TestCase):
    def _examples(self) -> list[CalibrationExample]:
        examples = []
        for index in range(80):
            label = int(index >= 40)
            score = (0.12 + index * 0.002) if not label else (0.68 + index * 0.002)
            examples.append(
                CalibrationExample(
                    score=score,
                    label=label,
                    query_type="identity" if index % 2 else "text",
                    collection_id="lib-a" if index % 3 else "lib-b",
                    group_id=f"session-{index // 4}",
                    rank=(index % 20) + 1,
                    has_answer=index >= 20,
                )
            )
        return examples

    def test_registry_is_versioned_layered_and_never_uses_floor_below_twenty_percent(
        self,
    ) -> None:
        registry, report = build_calibration_registry(
            self._examples(),
            calibration_version="calibration-test",
            minimum_query_samples=4,
            minimum_collection_samples=4,
            seed="fixed",
        )

        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["calibration_version"], "calibration-test")
        self.assertIn("text", registry["query_types"])
        self.assertIn("identity", registry["query_types"])
        self.assertIn("lib-a", registry["collections"])
        calibrators = [registry["global"], *registry["query_types"].values()]
        calibrators.extend(
            calibrator
            for modes in registry["collections"].values()
            for calibrator in modes.values()
        )
        self.assertTrue(all(item["minimum_confidence"] >= 0.20 for item in calibrators))
        self.assertFalse(report["activation_allowed"])

    def test_isotonic_application_is_monotonic_and_bounded(self) -> None:
        calibrator = {
            "method": "isotonic",
            "points": [[0.0, 0.1], [0.5, 0.4], [1.0, 0.9]],
            "minimum_confidence": 0.3,
        }
        values = [apply_calibrator(calibrator, value / 20) for value in range(21)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.0 <= value <= 1.0 for value in values))
        self.assertAlmostEqual(apply_calibrator(calibrator, 0.25), 0.25)

    def test_metrics_and_registry_evaluation_report_reliability(self) -> None:
        examples = self._examples()
        calibrator = {
            "method": "platt",
            "a": 8.0,
            "b": -4.0,
            "minimum_confidence": 0.3,
        }
        metrics = calibration_metrics(examples, calibrator, bins=8)
        self.assertEqual(metrics["sample_count"], len(examples))
        self.assertTrue(math.isfinite(metrics["brier_score"]))
        self.assertTrue(metrics["reliability"])
        self.assertIsNotNone(metrics["no_answer_false_return_rate"])
        report = evaluate_registry(
            examples,
            {
                "schema_version": 1,
                "calibration_version": "test",
                "global": calibrator,
            },
        )
        self.assertEqual(report["calibration_version"], "test")
        self.assertTrue(report["groups"])


class SearchLearningInputSafetyTests(unittest.TestCase):
    def test_json_and_jsonl_inputs_and_atomic_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path = root / "examples.json"
            json_path.write_text(
                json.dumps({"examples": [{"label": 1}]}), encoding="utf-8"
            )
            self.assertEqual(load_records(json_path), [{"label": 1}])
            jsonl_path = root / "examples.jsonl"
            jsonl_path.write_text('{"label": 0}\n{"label": 1}\n', encoding="utf-8")
            self.assertEqual(len(load_records(jsonl_path)), 2)

            output = root / "nested" / "model.json"
            atomic_write_json(output, {"schema_version": 1})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"schema_version": 1},
            )
            self.assertFalse(list(output.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
