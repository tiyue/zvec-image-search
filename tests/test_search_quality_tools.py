from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.search_quality import load_search_quality
from tests.search_quality import calibrate, evaluate

FIXTURE_DIR = Path(__file__).parent / "search_quality"


class SearchQualityEvaluateToolTest(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(
            (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
        )
        self.run_data = json.loads(
            (FIXTURE_DIR / "deterministic_run.json").read_text("utf-8")
        )

    def test_pending_template_has_sixty_unlabelled_cases(self):
        dataset = json.loads((FIXTURE_DIR / "dataset.json").read_text("utf-8"))
        items = evaluate.expand_dataset_items(dataset)
        self.assertEqual(len(items), 60)
        self.assertEqual(
            {
                query_type: sum(item["query_type"] == query_type for item in items)
                for query_type in ("text", "image", "combined", "no-answer")
            },
            {"text": 25, "image": 10, "combined": 10, "no-answer": 15},
        )
        self.assertTrue(
            all(item["annotation"]["status"] == "pending" for item in items)
        )
        self.assertTrue(all(item["relevant_images"] == [] for item in items))
        generated = evaluate.generate_pending_dataset(60)
        self.assertEqual(len(evaluate.expand_dataset_items(generated)), 60)

    def test_deterministic_fixture_metrics(self):
        report = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        metrics = report["metrics"]
        self.assertAlmostEqual(metrics["precision_at_5"], 13 / 24)
        self.assertEqual(metrics["strict_precision_at_5"], 0.25)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.875)
        self.assertEqual(metrics["no_answer_accuracy"], 0.0)
        self.assertEqual(metrics["no_answer_false_return_rate"], 1.0)
        self.assertAlmostEqual(metrics["average_result_count"], 12 / 7)
        self.assertAlmostEqual(metrics["latency_ms"]["average"], 75 / 7)
        self.assertEqual(metrics["api_requests"]["total"], 9)
        self.assertEqual(report["coverage"]["evaluated"], 7)
        self.assertEqual(report["coverage"]["synthetic"], 7)

    def test_synthetic_fixture_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow_synthetic"):
            evaluate.evaluate_dataset(self.dataset, self.run_data)

    def test_dataset_fingerprint_tracks_evaluation_content(self):
        report = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        self.assertEqual(
            report["dataset"]["fingerprint"],
            evaluate.dataset_fingerprint(self.dataset),
        )
        changed = copy.deepcopy(self.dataset)
        changed["items"][0]["query"]["text"] += " changed"
        self.assertNotEqual(
            evaluate.dataset_fingerprint(changed),
            report["dataset"]["fingerprint"],
        )

    def test_query_corpus_fingerprint_excludes_labels_but_tracks_execution(self):
        fingerprint = evaluate.query_corpus_fingerprint(self.dataset)
        relabelled = copy.deepcopy(self.dataset)
        relabelled["items"][0]["relevant_images"] = [
            {"image_id": "fixture:different-label"}
        ]
        relabelled["items"][0]["annotation"]["notes"] = "changed review notes"
        self.assertEqual(
            evaluate.query_corpus_fingerprint(relabelled),
            fingerprint,
        )

        changed_query = copy.deepcopy(self.dataset)
        changed_query["items"][0]["query"]["text"] += " changed"
        self.assertNotEqual(
            evaluate.query_corpus_fingerprint(changed_query),
            fingerprint,
        )
        changed_scope = copy.deepcopy(self.dataset)
        changed_scope["items"][0]["library_scope"]["library_ids"] = ["other"]
        self.assertNotEqual(
            evaluate.query_corpus_fingerprint(changed_scope),
            fingerprint,
        )

    def test_run_fingerprint_binds_reports_to_complete_run_content(self):
        report = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
            run_source="before-run.json",
        )
        self.assertEqual(
            report["run"]["fingerprint"],
            evaluate.run_fingerprint(self.run_data),
        )
        self.assertEqual(
            report["run"]["fingerprint_algorithm"],
            evaluate.RUN_FINGERPRINT_ALGORITHM,
        )

        reordered = json.loads(
            json.dumps(self.run_data, sort_keys=True, ensure_ascii=False)
        )
        self.assertEqual(
            evaluate.run_fingerprint(reordered),
            report["run"]["fingerprint"],
        )
        changed = copy.deepcopy(self.run_data)
        changed["cases"][0]["latency_ms"] += 1
        self.assertNotEqual(
            evaluate.run_fingerprint(changed),
            report["run"]["fingerprint"],
        )

    def test_run_rank_must_match_displayed_order(self):
        run = copy.deepcopy(self.run_data)
        run["cases"][0]["results"][0]["rank"] = 2
        with self.assertRaisesRegex(ValueError, "rank must match displayed order"):
            evaluate.evaluate_dataset(self.dataset, run, allow_synthetic=True)

    def test_before_after_json_and_markdown_comparison(self):
        before = evaluate.evaluate_dataset(
            self.dataset, self.run_data, allow_synthetic=True
        )
        after = copy.deepcopy(before)
        after["run"]["name"] = "improved-run"
        after["metrics"]["precision_at_5"] = 0.9
        comparison = evaluate.compare_reports(before, after)
        self.assertEqual(
            comparison["before"]["fingerprint"],
            evaluate.run_fingerprint(self.run_data),
        )
        self.assertEqual(
            comparison["before"]["fingerprint_algorithm"],
            evaluate.RUN_FINGERPRINT_ALGORITHM,
        )
        self.assertAlmostEqual(
            comparison["metrics"]["precision_at_5"]["delta"],
            0.9 - 13 / 24,
        )
        self.assertIn("Precision@5", evaluate.render_comparison_markdown(comparison))

        with tempfile.TemporaryDirectory() as temporary:
            before_path = Path(temporary) / "before.json"
            after_path = Path(temporary) / "after.json"
            output_path = Path(temporary) / "comparison.json"
            markdown_path = Path(temporary) / "comparison.md"
            before_path.write_text(json.dumps(before), encoding="utf-8")
            after_path.write_text(json.dumps(after), encoding="utf-8")
            self.assertEqual(
                evaluate.main(
                    [
                        "compare",
                        "--before",
                        str(before_path),
                        "--after",
                        str(after_path),
                        "--output",
                        str(output_path),
                        "--markdown",
                        str(markdown_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output_path.read_text("utf-8"))["kind"],
                "zvec-search-quality-comparison",
            )
            self.assertIn("Precision@5", markdown_path.read_text("utf-8"))

    def test_legacy_schema_v1_reports_remain_comparable(self):
        before = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        after = copy.deepcopy(before)
        for report in (before, after):
            report["dataset"].pop("fingerprint")
            report["dataset"].pop("fingerprint_algorithm")
        comparison = evaluate.compare_reports(before, after)
        self.assertEqual(comparison["schema_version"], 1)

    def test_evaluate_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evaluation.json"
            markdown = Path(temporary) / "evaluation.md"
            exit_code = evaluate.main(
                [
                    "evaluate",
                    "--dataset",
                    str(FIXTURE_DIR / "deterministic_dataset.json"),
                    "--run",
                    str(FIXTURE_DIR / "deterministic_run.json"),
                    "--output",
                    str(output),
                    "--markdown",
                    str(markdown),
                    "--allow-synthetic",
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.read_text("utf-8"))["schema_version"], 1)
            self.assertIn("Search quality evaluation", markdown.read_text("utf-8"))


class SearchQualityCalibrateToolTest(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(
            (FIXTURE_DIR / "deterministic_dataset.json").read_text("utf-8")
        )
        self.run_data = json.loads(
            (FIXTURE_DIR / "deterministic_run.json").read_text("utf-8")
        )

    def _captured_score_run(self):
        captured = copy.deepcopy(self.run_data)
        captured["score_fields"] = {
            "text": "raw_score",
            "image": "raw_score",
            "combined": "confidence",
        }
        modes = {item["id"]: item["mode"] for item in self.dataset["items"]}
        for case in captured["cases"]:
            mode = modes[case["id"]]
            for result in case["results"]:
                original_score = float(result["score"])
                result["raw_score"] = original_score
                result["normalized_score"] = (
                    original_score
                    if mode == "combined"
                    else max(0.0, 1.0 - original_score / 2.0)
                )
                result["confidence"] = result["normalized_score"]
                result["score"] = -999.0
        return captured

    def test_result_score_reads_the_declared_field(self):
        result = {
            "score": 9.0,
            "raw_score": 0.4,
            "normalized_score": 0.8,
            "confidence": 0.7,
        }
        self.assertEqual(calibrate._result_score(result, "case", "score"), 9.0)
        self.assertEqual(
            calibrate._result_score(result, "case", "raw_score"),
            0.4,
        )
        self.assertEqual(
            calibrate._result_score(result, "case", "normalized_score"),
            0.8,
        )
        self.assertEqual(
            calibrate._result_score(result, "case", "confidence"),
            0.7,
        )

    def test_calibration_satisfies_constraints_and_is_deterministic(self):
        configuration = calibrate.calibrate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        self.assertEqual(configuration["schema_version"], 2)
        self.assertEqual(configuration["thresholds"]["text"]["value"], 0.3)
        self.assertEqual(configuration["thresholds"]["image"]["value"], 0.1)
        self.assertEqual(configuration["thresholds"]["combined"]["value"], 0.9)
        self.assertEqual(configuration["text"]["minimum_score"], 0.3)
        self.assertEqual(configuration["text"]["minimum_confidence"], 0.85)
        self.assertEqual(configuration["image"]["minimum_confidence"], 0.95)
        self.assertEqual(configuration["combined"]["minimum_confidence"], 1.0)
        self.assertEqual(configuration["text"]["score_gap"], 1.0)
        self.assertEqual(configuration["image"]["score_gap"], 1.0)
        self.assertEqual(configuration["combined"]["score_gap"], 1.0)
        self.assertEqual(configuration["text"]["max_confidence_drop"], 1.0)
        self.assertEqual(configuration["image"]["max_confidence_drop"], 1.0)
        self.assertEqual(configuration["combined"]["max_confidence_drop"], 1.0)
        self.assertEqual(configuration["fusion"], {"mode": "confidence_v1"})
        self.assertEqual(
            configuration["diagnostics"]["fusion"]["source"],
            "default_confidence_v1",
        )
        for value in configuration["thresholds"].values():
            self.assertLess(value["metrics"]["no_answer_false_return_rate"], 0.1)
            self.assertLessEqual(value["recall_at_5_drop"], 0.03)
            self.assertGreater(value["evaluated_score_gaps"], 0)
            self.assertGreater(value["evaluated_max_confidence_drops"], 0)
            self.assertGreater(value["evaluated_parameter_pairs"], 0)
            self.assertEqual(
                value["evaluated_parameter_combinations"],
                value["evaluated_thresholds"]
                * value["evaluated_score_gaps"]
                * value["evaluated_max_confidence_drops"],
            )
            self.assertLessEqual(
                value["evaluated_parameter_combinations"],
                calibrate.MAX_PARAMETER_COMBINATIONS,
            )
        self.assertEqual(
            configuration["diagnostics"]["thresholds"]["text"][
                "evaluated_max_confidence_drops"
            ],
            configuration["thresholds"]["text"]["evaluated_max_confidence_drops"],
        )

        filtered_run = calibrate.apply_thresholds(
            self.dataset, self.run_data, configuration
        )
        report = evaluate.evaluate_dataset(
            self.dataset,
            filtered_run,
            allow_synthetic=True,
        )
        self.assertEqual(report["metrics"]["no_answer_false_return_rate"], 0.0)
        self.assertEqual(report["metrics"]["recall_at_5"], 1.0)
        self.assertGreater(report["metrics"]["precision_at_5"], 0.9)

    def test_calibration_optimizes_standard_precision_at_5(self):
        annotation = {"status": "synthetic_fixture", "notes": "test"}
        scope = {"mode": "selected", "library_ids": ["fixture"]}
        dataset = {
            "schema_version": 1,
            "name": "standard-precision-objective",
            "items": [
                {
                    "id": "answerable",
                    "query_type": "text",
                    "mode": "text",
                    "query": {"text": "three matches"},
                    "library_scope": scope,
                    "relevant_images": [
                        {"image_id": "fixture:r1"},
                        {"image_id": "fixture:r2"},
                        {"image_id": "fixture:r3"},
                    ],
                    "annotation": annotation,
                },
                {
                    "id": "empty",
                    "query_type": "no-answer",
                    "mode": "text",
                    "query": {"text": "nothing"},
                    "library_scope": scope,
                    "relevant_images": [],
                    "annotation": annotation,
                },
            ],
        }
        run = {
            "schema_version": 1,
            "name": "standard-precision-run",
            "score_semantics": {
                "text": "lower_is_better",
                "image": "lower_is_better",
                "combined": "higher_is_better",
            },
            "cases": [
                {
                    "id": "answerable",
                    "latency_ms": 1,
                    "api_requests": 0,
                    "results": [
                        {"image_id": "fixture:r1", "score": 0.1},
                        {"image_id": "fixture:noise", "score": 0.2},
                        {"image_id": "fixture:r2", "score": 0.3},
                        {"image_id": "fixture:r3", "score": 0.4},
                        {"image_id": "fixture:tail", "score": 0.5},
                    ],
                },
                {
                    "id": "empty",
                    "latency_ms": 1,
                    "api_requests": 0,
                    "results": [{"image_id": "fixture:false", "score": 0.9}],
                },
            ],
        }
        pairs, _coverage = evaluate._selected_pairs(
            dataset,
            run,
            allow_synthetic=True,
        )
        selected = calibrate._select_threshold(
            "text",
            pairs,
            "lower_is_better",
            "score",
            evaluate._metric_block(pairs),
            max_false_positive_rate=0.1,
            max_recall_drop=0.7,
        )
        self.assertEqual(selected["value"], 0.4)
        self.assertEqual(selected["metrics"]["strict_precision_at_5"], 0.6)
        self.assertEqual(selected["metrics"]["precision_at_5"], 0.75)

    def test_calibration_includes_reject_all_boundary_but_will_not_select_it(self):
        annotation = {"status": "synthetic_fixture", "notes": "test"}
        scope = {"mode": "selected", "library_ids": ["fixture"]}
        answerable = {
            "id": "answerable",
            "query_type": "text",
            "mode": "text",
            "query": {"text": "missing"},
            "library_scope": scope,
            "relevant_images": [{"image_id": "fixture:missing"}],
            "annotation": annotation,
        }
        empty = {
            "id": "empty",
            "query_type": "no-answer",
            "mode": "text",
            "query": {"text": "empty"},
            "library_scope": scope,
            "relevant_images": [],
            "annotation": annotation,
        }
        pairs = [
            (
                answerable,
                {
                    "id": "answerable",
                    "latency_ms": 1,
                    "api_requests": 0,
                    "results": [{"image_id": "fixture:noise", "score": 0.2}],
                },
            ),
            (
                empty,
                {
                    "id": "empty",
                    "latency_ms": 1,
                    "api_requests": 0,
                    "results": [{"image_id": "fixture:false", "score": 0.1}],
                },
            ),
        ]
        thresholds = calibrate._candidate_thresholds(
            pairs,
            "score",
            "lower_is_better",
        )
        self.assertLess(thresholds[0], 0.1)
        with self.assertRaisesRegex(ValueError, "No text threshold"):
            calibrate._select_threshold(
                "text",
                pairs,
                "lower_is_better",
                "score",
                evaluate._metric_block(pairs),
                max_false_positive_rate=0.1,
                max_recall_drop=0.03,
            )

    def test_calibration_rejects_mismatched_baseline_identity(self):
        baseline = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        changed = copy.deepcopy(self.dataset)
        changed["items"][0]["query"]["text"] += " changed"
        with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
            calibrate.calibrate_dataset(
                changed,
                self.run_data,
                baseline_report=baseline,
                allow_synthetic=True,
            )

        baseline = copy.deepcopy(baseline)
        baseline["coverage"]["evaluated"] -= 1
        with self.assertRaisesRegex(ValueError, "coverage does not match"):
            calibrate.calibrate_dataset(
                self.dataset,
                self.run_data,
                baseline_report=baseline,
                allow_synthetic=True,
            )

    def test_calibration_preserves_captured_confidence_scale(self):
        captured = self._captured_score_run()
        configuration = calibrate.calibrate_dataset(
            self.dataset,
            captured,
            allow_synthetic=True,
        )
        self.assertEqual(configuration["combined"]["minimum_score"], 0.0)
        self.assertEqual(configuration["combined"]["minimum_confidence"], 0.9)
        self.assertEqual(
            configuration["thresholds"]["combined"]["score_field"],
            "confidence",
        )

    def test_calibration_preserves_captured_fusion_configuration(self):
        captured = self._captured_score_run()
        captured["search_quality"] = {
            "fusion": {
                "mode": "confidence_v2",
                "agreement_reward": 0.11,
                "rank_decay": 9.0,
            }
        }
        configuration = calibrate.calibrate_dataset(
            self.dataset,
            captured,
            allow_synthetic=True,
        )
        self.assertEqual(configuration["fusion"]["mode"], "confidence_v2")
        self.assertEqual(configuration["fusion"]["agreement_reward"], 0.11)
        self.assertEqual(configuration["fusion"]["weak_channel_floor"], 0.45)
        self.assertEqual(
            configuration["diagnostics"]["fusion"]["source"],
            "captured_run.search_quality.fusion",
        )

    def test_calibration_jointly_selects_a_useful_score_gap(self):
        dataset = {
            "schema_version": 1,
            "name": "gap-fixture",
            "items": [
                {
                    "id": "strong",
                    "query_type": "text",
                    "mode": "text",
                    "query": {"text": "strong"},
                    "library_scope": {
                        "mode": "selected",
                        "library_ids": ["fixture"],
                    },
                    "relevant_images": [{"image_id": "fixture:strong"}],
                    "annotation": {"status": "synthetic_fixture", "notes": "test"},
                },
                {
                    "id": "weak",
                    "query_type": "text",
                    "mode": "text",
                    "query": {"text": "weak"},
                    "library_scope": {
                        "mode": "selected",
                        "library_ids": ["fixture"],
                    },
                    "relevant_images": [{"image_id": "fixture:weak"}],
                    "annotation": {"status": "synthetic_fixture", "notes": "test"},
                },
                {
                    "id": "empty",
                    "query_type": "no-answer",
                    "mode": "text",
                    "query": {"text": "empty"},
                    "library_scope": {
                        "mode": "selected",
                        "library_ids": ["fixture"],
                    },
                    "relevant_images": [],
                    "annotation": {"status": "synthetic_fixture", "notes": "test"},
                },
            ],
        }
        run = {
            "schema_version": 1,
            "name": "gap-run",
            "score_semantics": {
                "text": "lower_is_better",
                "image": "lower_is_better",
                "combined": "higher_is_better",
            },
            "cases": [
                {
                    "id": "strong",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [
                        {"image_id": "fixture:strong", "score": 0.1},
                        {"image_id": "fixture:noise", "score": 0.5},
                    ],
                },
                {
                    "id": "weak",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [{"image_id": "fixture:weak", "score": 0.7}],
                },
                {
                    "id": "empty",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [{"image_id": "fixture:false", "score": 0.9}],
                },
            ],
        }
        pairs, _coverage = evaluate._selected_pairs(
            dataset,
            run,
            allow_synthetic=True,
        )
        baseline = evaluate._metric_block(pairs)
        selected = calibrate._select_threshold(
            "text",
            pairs,
            "lower_is_better",
            "score",
            baseline,
            max_false_positive_rate=0.1,
            max_recall_drop=0.03,
        )
        self.assertEqual(selected["value"], 0.7)
        self.assertAlmostEqual(selected["score_gap"], 0.2)
        self.assertEqual(selected["metrics"]["precision_at_5"], 1.0)

    @staticmethod
    def _cumulative_tail_fixture():
        library_scope = {
            "mode": "selected",
            "library_ids": ["fixture"],
        }
        annotation = {"status": "synthetic_fixture", "notes": "test"}
        dataset = {
            "schema_version": 1,
            "name": "confidence-band-fixture",
            "items": [
                {
                    "id": "tail",
                    "query_type": "text",
                    "mode": "text",
                    "query": {"text": "tail"},
                    "library_scope": library_scope,
                    "relevant_images": [
                        {"image_id": "fixture:first"},
                        {"image_id": "fixture:third"},
                    ],
                    "annotation": annotation,
                },
                {
                    "id": "weak",
                    "query_type": "text",
                    "mode": "text",
                    "query": {"text": "weak"},
                    "library_scope": library_scope,
                    "relevant_images": [{"image_id": "fixture:weak"}],
                    "annotation": annotation,
                },
                {
                    "id": "empty",
                    "query_type": "no-answer",
                    "mode": "text",
                    "query": {"text": "empty"},
                    "library_scope": library_scope,
                    "relevant_images": [],
                    "annotation": annotation,
                },
            ],
        }
        run = {
            "schema_version": 1,
            "name": "confidence-band-run",
            "score_semantics": {
                "text": "lower_is_better",
                "image": "lower_is_better",
                "combined": "higher_is_better",
            },
            "cases": [
                {
                    "id": "tail",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [
                        {"image_id": "fixture:first", "score": 0.10},
                        {"image_id": "fixture:noise-1", "score": 0.12},
                        {"image_id": "fixture:third", "score": 0.14},
                        {"image_id": "fixture:noise-2", "score": 0.16},
                        {"image_id": "fixture:noise-3", "score": 0.18},
                        {"image_id": "fixture:noise-4", "score": 0.20},
                    ],
                },
                {
                    "id": "weak",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [{"image_id": "fixture:weak", "score": 0.20}],
                },
                {
                    "id": "empty",
                    "latency_ms": 1,
                    "api_requests": 1,
                    "results": [{"image_id": "fixture:false", "score": 0.30}],
                },
            ],
        }
        return dataset, run

    def test_calibration_prunes_a_tail_of_small_adjacent_drops(self):
        dataset, run = self._cumulative_tail_fixture()
        pairs, _coverage = evaluate._selected_pairs(
            dataset,
            run,
            allow_synthetic=True,
        )
        selected = calibrate._select_threshold(
            "text",
            pairs,
            "lower_is_better",
            "score",
            evaluate._metric_block(pairs),
            max_false_positive_rate=0.1,
            max_recall_drop=0.03,
        )

        self.assertEqual(selected["value"], 0.20)
        self.assertEqual(selected["score_gap"], 1.0)
        self.assertAlmostEqual(selected["max_confidence_drop"], 0.02)
        self.assertEqual(selected["metrics"]["recall_at_5"], 1.0)
        self.assertGreater(selected["metrics"]["precision_at_5"], 0.8)
        production = calibrate._production_threshold("text", selected, "score")
        self.assertAlmostEqual(production["max_confidence_drop"], 0.02)

    def test_apply_thresholds_defaults_legacy_confidence_band_to_one(self):
        dataset, run = self._cumulative_tail_fixture()
        legacy_configuration = {
            "schema_version": 2,
            "kind": "zvec-search-quality-thresholds",
            "thresholds": {
                "text": {
                    "value": 0.20,
                    "score_gap": 1.0,
                    "operator": "<=",
                    "score_semantics": "lower_is_better",
                    "score_field": "score",
                }
            },
        }

        filtered = calibrate.apply_thresholds(
            dataset,
            run,
            legacy_configuration,
        )

        tail = next(case for case in filtered["cases"] if case["id"] == "tail")
        self.assertEqual(len(tail["results"]), 6)

    def test_calibrate_cli_writes_loadable_schema_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "search-quality.json"
            exit_code = calibrate.main(
                [
                    "--dataset",
                    str(FIXTURE_DIR / "deterministic_dataset.json"),
                    "--run",
                    str(FIXTURE_DIR / "deterministic_run.json"),
                    "--output",
                    str(output),
                    "--allow-synthetic",
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["kind"], "zvec-search-quality-thresholds")
            loaded = load_search_quality(Path(temporary))
            self.assertTrue(loaded.configured)
            self.assertEqual(
                loaded.text.minimum,
                payload["text"]["minimum_confidence"],
            )

    def test_calibrate_cli_records_explicit_fusion_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "search-quality.json"
            run_path = Path(temporary) / "run.json"
            run = self._captured_score_run()
            run_path.write_text(json.dumps(run), encoding="utf-8")
            exit_code = calibrate.main(
                [
                    "--dataset",
                    str(FIXTURE_DIR / "deterministic_dataset.json"),
                    "--run",
                    str(run_path),
                    "--output",
                    str(output),
                    "--allow-synthetic",
                    "--fusion-mode",
                    "confidence_v2",
                    "--agreement-reward",
                    "0.1",
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["fusion"]["mode"], "confidence_v2")
            self.assertEqual(payload["fusion"]["agreement_reward"], 0.1)
            self.assertEqual(payload["fusion"]["rank_decay"], 10.0)
            self.assertEqual(
                payload["diagnostics"]["fusion"]["source"],
                "explicit_cli",
            )
            loaded = load_search_quality(Path(temporary))
            self.assertEqual(loaded.fusion_mode, "confidence_v2")

    def test_fusion_v2_rejects_legacy_rrf_score_scale(self):
        with self.assertRaisesRegex(
            ValueError,
            "confidence_v2 calibration requires combined score_field",
        ):
            calibrate.calibrate_dataset(
                self.dataset,
                self.run_data,
                allow_synthetic=True,
                fusion_mode="confidence_v2",
            )


if __name__ == "__main__":
    unittest.main()
