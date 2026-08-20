from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.search_quality import collection_fairness, evaluate, quality_gate
from tests.search_quality import split as dataset_split

SHA_A = "a" * 64
SHA_B = "b" * 64


def _item(
    relevant: list[str],
    *,
    scope_mode: str = "selected",
    scope_ids: list[str] | None = None,
) -> dict:
    return {
        "id": "cross-case",
        "query_type": "text",
        "mode": "text",
        "query": {"text": "fixture query"},
        "library_scope": {
            "mode": scope_mode,
            "library_ids": scope_ids if scope_ids is not None else ["large", "small"],
        },
        "relevant_images": [{"image_id": image_id} for image_id in relevant],
        "annotation": {"status": "synthetic_fixture", "notes": "test"},
    }


def _case(results: list[str], *, library_ids: list[str] | None = None) -> dict:
    value = {
        "id": "cross-case",
        "latency_ms": 1,
        "api_requests": 0,
        "results": [
            {"image_id": image_id, "rank": rank}
            for rank, image_id in enumerate(results, start=1)
        ],
    }
    if library_ids is not None:
        value["library_ids"] = library_ids
    return value


def _formal_gate_fixture() -> tuple[dict, dict, dict]:
    annotation = {
        "status": "human_verified",
        "annotator": "test-human",
        "annotated_at": "2026-07-13T12:00:00+00:00",
        "notes": "Deterministic gate contract.",
    }
    scope = {"mode": "selected", "library_ids": ["large", "small"]}
    dataset = {
        "schema_version": 1,
        "name": "cross-collection-gate-fixture",
        "items": [
            {
                "id": "large-answer",
                "query_type": "text",
                "mode": "text",
                "query": {"text": "large answer"},
                "library_scope": scope,
                "relevant_images": [{"image_id": "large:answer.jpg"}],
                "annotation": annotation,
            },
            {
                "id": "small-answer",
                "query_type": "text",
                "mode": "text",
                "query": {"text": "small answer"},
                "library_scope": scope,
                "relevant_images": [{"image_id": "small:answer.jpg"}],
                "annotation": annotation,
            },
            {
                "id": "absent",
                "query_type": "no-answer",
                "mode": "text",
                "query": {"text": "absent"},
                "library_scope": scope,
                "relevant_images": [],
                "annotation": annotation,
            },
        ],
    }

    def run_case(case_id: str, results: list[str]) -> dict:
        return {
            "id": case_id,
            "library_ids": ["large", "small"],
            "latency_ms": 10,
            "api_requests": 0,
            "results": [
                {"image_id": image_id, "rank": rank, "score": rank / 10}
                for rank, image_id in enumerate(results, start=1)
            ],
        }

    before = {
        "schema_version": 1,
        "name": "biased-before",
        "cases": [
            run_case(
                "large-answer",
                [*(f"small:noise-{i}.jpg" for i in range(5)), "large:answer.jpg"],
            ),
            run_case(
                "small-answer",
                [*(f"large:noise-{i}.jpg" for i in range(5)), "small:answer.jpg"],
            ),
            run_case("absent", ["large:false-return.jpg"]),
        ],
    }
    after = {
        "schema_version": 1,
        "name": "unified-after",
        "cases": [
            run_case("large-answer", ["large:answer.jpg", "small:after-noise.jpg"]),
            run_case("small-answer", ["small:answer.jpg", "large:after-noise.jpg"]),
            run_case("absent", []),
        ],
    }
    return dataset, before, after


def _holdout_gate_fixture() -> tuple[dict, dict, dict, dict, dict]:
    annotation = {
        "status": "human_verified",
        "annotator": "holdout-test-human",
        "annotated_at": "2026-07-13T12:00:00+00:00",
        "notes": "Deterministic holdout gate contract.",
    }
    scope = {"mode": "selected", "library_ids": ["large", "small"]}
    items = []
    for mode in sorted(evaluate.MODES):
        for index in range(2):
            answer_id = f"{mode}-answer-{index}"
            empty_id = f"{mode}-empty-{index}"
            query = (
                {"text": answer_id}
                if mode == "text"
                else {"image": f"fixtures/{answer_id}.png"}
            )
            empty_query = (
                {"text": empty_id}
                if mode == "text"
                else {"image": f"fixtures/{empty_id}.png"}
            )
            if mode == "combined":
                query = {
                    "text": answer_id,
                    "image": f"fixtures/{answer_id}.png",
                }
                empty_query = {
                    "text": empty_id,
                    "image": f"fixtures/{empty_id}.png",
                }
            items.extend(
                [
                    {
                        "id": answer_id,
                        "query_type": mode,
                        "mode": mode,
                        "query": query,
                        "library_scope": scope,
                        "relevant_images": [
                            {"image_id": f"large:{answer_id}:large.jpg"},
                            {"image_id": f"small:{answer_id}:small.jpg"},
                        ],
                        "annotation": annotation,
                    },
                    {
                        "id": empty_id,
                        "query_type": "no-answer",
                        "mode": mode,
                        "query": empty_query,
                        "library_scope": scope,
                        "relevant_images": [],
                        "annotation": annotation,
                    },
                ]
            )
    dataset = {
        "schema_version": 1,
        "name": "formal-holdout-gate-fixture",
        "items": items,
    }
    manifest, _, validation_dataset = dataset_split.build_split_artifacts(
        dataset,
        seed="formal-holdout-gate-v1",
    )

    def build_run(name: str, *, improved: bool) -> dict:
        cases = []
        for item in validation_dataset["items"]:
            case_id = item["id"]
            if item["query_type"] == "no-answer":
                results = (
                    []
                    if improved
                    else [{"image_id": f"large:{case_id}:false.jpg", "rank": 1}]
                )
            elif improved:
                results = [
                    {"image_id": item["relevant_images"][0]["image_id"], "rank": 1},
                    {"image_id": item["relevant_images"][1]["image_id"], "rank": 2},
                    {"image_id": f"large:{case_id}:noise.jpg", "rank": 3},
                    {"image_id": f"small:{case_id}:noise.jpg", "rank": 4},
                ]
            else:
                results = [
                    {
                        "image_id": f"small:{case_id}:before-noise-{rank}.jpg",
                        "rank": rank,
                    }
                    for rank in range(1, 6)
                ] + [
                    {"image_id": item["relevant_images"][0]["image_id"], "rank": 6},
                    {"image_id": item["relevant_images"][1]["image_id"], "rank": 7},
                ]
            cases.append(
                {
                    "id": case_id,
                    "library_ids": ["large", "small"],
                    "latency_ms": 10,
                    "api_requests": 0,
                    "results": results,
                }
            )
        return {
            "schema_version": 1,
            "name": name,
            "dataset": {
                "name": validation_dataset["name"],
                "fingerprint": evaluate.query_corpus_fingerprint(validation_dataset),
                "fingerprint_algorithm": (evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM),
                "case_count": len(validation_dataset["items"]),
            },
            "draft": False,
            "baseline_eligible": True,
            "capture": {
                "kind": "zvec-persistent-backend-capture",
                "pending_case_count": 0,
                "baseline_eligible": True,
                "top_k": 10,
                "candidate_k": 50,
            },
            "split": {
                "kind": validation_dataset["split"]["kind"],
                "role": validation_dataset["split"]["role"],
                "manifest_fingerprint": validation_dataset["split"][
                    "manifest_fingerprint"
                ],
                "manifest_fingerprint_algorithm": validation_dataset["split"][
                    "manifest_fingerprint_algorithm"
                ],
            },
            "cases": cases,
        }

    return (
        dataset,
        validation_dataset,
        manifest,
        build_run("holdout-before", improved=False),
        build_run("holdout-after", improved=True),
    )


class CollectionFairnessTest(unittest.TestCase):
    def test_single_collection_is_explicitly_insufficient(self) -> None:
        item = _item(["only:relevant.jpg"], scope_mode="single", scope_ids=["only"])
        report = collection_fairness.evaluate_collection_fairness(
            [(item, _case(["only:relevant.jpg"]))]
        )

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertEqual(report["coverage"]["assessable_cases"], 0)
        self.assertEqual(
            report["coverage"]["excluded_cases"]["single_collection_scope"], 1
        )
        self.assertIsNone(report["metrics"]["cross_collection_pairwise_accuracy"])

    def test_relevant_candidates_ahead_of_other_collection_noise_pass(self) -> None:
        item = _item(["large:relevant.jpg", "small:relevant.jpg"])
        case = _case(
            [
                "large:relevant.jpg",
                "small:relevant.jpg",
                "large:noise.jpg",
                "small:noise.jpg",
            ],
            library_ids=["large", "small"],
        )

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["coverage"]["cross_collection_comparisons"], 2)
        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["worst_directional_pairwise_accuracy"], 1.0)

    def test_small_collection_noise_ahead_of_large_relevance_is_detected(self) -> None:
        item = _item(["large:relevant.jpg", "small:relevant.jpg"])
        case = _case(
            [
                "small:noise.jpg",
                "small:relevant.jpg",
                "large:relevant.jpg",
                "large:noise.jpg",
            ],
            library_ids=["large", "small"],
        )

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 0.5)
        self.assertEqual(report["metrics"]["worst_directional_pairwise_accuracy"], 0.0)
        self.assertEqual(report["by_direction"]["large->small"]["inversions"], 1)
        self.assertEqual(report["by_direction"]["small->large"]["inversions"], 0)

    def test_ranking_among_relevant_images_does_not_invent_a_size_prior(self) -> None:
        item = _item(
            [
                "large:relevant-1.jpg",
                "large:relevant-2.jpg",
                "large:relevant-3.jpg",
                "small:relevant.jpg",
            ]
        )
        case = _case(
            [
                "small:relevant.jpg",
                "large:relevant-1.jpg",
                "large:relevant-2.jpg",
                "large:relevant-3.jpg",
            ],
            library_ids=["large", "small"],
        )

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["coverage"]["cross_collection_comparisons"], 0)
        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 1.0)
        self.assertTrue(report["cases"][0]["positive_cross_collection_evidence"])

    def test_absent_relevant_label_is_left_to_recall_and_fails_coverage(
        self,
    ) -> None:
        item = _item(["large:missing-relevant.jpg"])
        case = _case(
            ["small:noise-1.jpg", "small:noise-2.jpg"],
            library_ids=["large", "small"],
        )

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertEqual(report["coverage"]["cross_collection_comparisons"], 0)
        self.assertIsNone(report["metrics"]["cross_collection_pairwise_accuracy"])
        self.assertEqual(
            report["coverage"]["unevidenced_relevant_label_library_ids"],
            ["large"],
        )

    def test_unreturned_extra_label_does_not_duplicate_pairwise_penalty(self) -> None:
        item = _item(["large:returned.jpg", "large:not-returned.jpg"])
        case = _case(
            ["large:returned.jpg", "small:noise.jpg"],
            library_ids=["large", "small"],
        )

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["coverage"]["cross_collection_comparisons"], 1)
        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 1.0)

    def test_separate_cases_can_evidence_each_labelled_collection(self) -> None:
        large = _item(["large:relevant.jpg"])
        large["id"] = "large-only-result"
        small = _item(["small:relevant.jpg"])
        small["id"] = "small-only-result"
        report = collection_fairness.evaluate_collection_fairness(
            [
                (
                    large,
                    {
                        **_case(
                            ["large:relevant.jpg"],
                            library_ids=["large", "small"],
                        ),
                        "id": large["id"],
                    },
                ),
                (
                    small,
                    {
                        **_case(
                            ["small:relevant.jpg"],
                            library_ids=["large", "small"],
                        ),
                        "id": small["id"],
                    },
                ),
            ]
        )

        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["coverage"]["assessable_cases"], 2)
        self.assertEqual(
            report["coverage"]["evidenced_relevant_label_library_ids"],
            ["large", "small"],
        )
        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 1.0)

    def test_same_sha_copy_in_another_collection_is_still_relevant(self) -> None:
        item = _item(["large:original.jpg"])
        item["relevant_images"][0]["sha256"] = SHA_A
        case = _case([], library_ids=["large", "small"])
        case["results"] = [
            {
                "image_id": "small:content-copy.jpg",
                "sha256": SHA_A,
                "rank": 1,
            },
            {"image_id": "large:noise.jpg", "sha256": SHA_B, "rank": 2},
        ]

        report = collection_fairness.evaluate_collection_fairness([(item, case)])

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertEqual(report["metrics"]["cross_collection_pairwise_accuracy"], 1.0)
        self.assertEqual(report["cases"][0]["relevant_hits_by_library"], {"small": 1})
        self.assertEqual(report["by_direction"]["small->large"]["inversions"], 0)

    def test_duplicate_relevance_labels_by_sha_fail_closed(self) -> None:
        item = _item(["large:one.jpg", "small:copy.jpg"])
        for relevant in item["relevant_images"]:
            relevant["sha256"] = SHA_A

        with self.assertRaisesRegex(ValueError, "duplicate global image identity"):
            collection_fairness.evaluate_collection_fairness(
                [
                    (
                        item,
                        _case(["large:one.jpg"], library_ids=["large", "small"]),
                    )
                ]
            )

    def test_labelled_collection_without_evidence_does_not_satisfy_gate(self) -> None:
        large = _item(["large:relevant.jpg"])
        large["id"] = "large-evidence"
        small = _item(["small:missing.jpg"])
        small["id"] = "small-no-evidence"
        report = collection_fairness.evaluate_collection_fairness(
            [
                (
                    large,
                    {
                        **_case(
                            ["large:relevant.jpg", "small:noise.jpg"],
                            library_ids=["large", "small"],
                        ),
                        "id": large["id"],
                    },
                ),
                (
                    small,
                    {
                        **_case([], library_ids=["large", "small"]),
                        "id": small["id"],
                    },
                ),
            ]
        )

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertEqual(
            report["coverage"]["evidenced_relevant_label_library_ids"],
            ["large"],
        )
        self.assertEqual(
            report["coverage"]["unevidenced_relevant_label_library_ids"],
            ["small"],
        )
        checks = quality_gate._collection_fairness_checks(
            {"collection_fairness": report},
            {"collection_fairness": report},
        )
        self.assertFalse(checks["cross_collection_coverage"]["passed"])

    def test_all_enabled_without_library_ids_fails_coverage_closed(self) -> None:
        item = _item(["large:relevant.jpg"], scope_mode="all_enabled", scope_ids=[])

        report = collection_fairness.evaluate_collection_fairness(
            [(item, _case(["large:relevant.jpg"]))]
        )

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertEqual(
            report["coverage"]["excluded_cases"]["unobserved_all_enabled_scope"],
            1,
        )

    def test_scope_and_result_attribution_mismatches_fail_closed(self) -> None:
        item = _item(["large:relevant.jpg"])
        with self.assertRaisesRegex(ValueError, "do not match its selected scope"):
            collection_fairness.evaluate_collection_fairness(
                [
                    (
                        item,
                        _case(
                            ["large:relevant.jpg"],
                            library_ids=["large", "unexpected"],
                        ),
                    )
                ]
            )

        with self.assertRaisesRegex(ValueError, "outside the evaluated scope"):
            collection_fairness.evaluate_collection_fairness(
                [
                    (
                        item,
                        {
                            **_case(
                                ["large:relevant.jpg"],
                                library_ids=["large", "small"],
                            ),
                            "results": [
                                {
                                    "image_id": "large:relevant.jpg",
                                    "library_id": "unexpected",
                                }
                            ],
                        },
                    )
                ]
            )


class CollectionFairnessGateTest(unittest.TestCase):
    def test_formal_quality_gate_requires_validation_manifest(self) -> None:
        dataset, _, _, before, after = _holdout_gate_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "dataset": root / "dataset.json",
                "before": root / "before.json",
                "after": root / "after.json",
            }
            for name, payload in (
                ("dataset", dataset),
                ("before", before),
                ("after", after),
            ):
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires --split-manifest"):
                quality_gate.run_quality_gate(
                    paths["dataset"],
                    paths["before"],
                    paths["after"],
                    root / "report",
                )

    def test_formal_cli_uses_only_validation_and_records_provenance(self) -> None:
        dataset, validation_dataset, manifest, before, after = _holdout_gate_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "dataset": root / "dataset.json",
                "validation": root / "validation.json",
                "manifest": root / "split.json",
                "before": root / "before.json",
                "after": root / "after.json",
            }
            for name, payload in (
                ("dataset", dataset),
                ("validation", validation_dataset),
                ("manifest", manifest),
                ("before", before),
                ("after", after),
            ):
                paths[name].write_text(json.dumps(payload), encoding="utf-8")

            output = root / "full-source-report"
            exit_code = quality_gate.main(
                [
                    "--dataset",
                    str(paths["dataset"]),
                    "--before-run",
                    str(paths["before"]),
                    "--after-run",
                    str(paths["after"]),
                    "--split-manifest",
                    str(paths["manifest"]),
                    "--output-dir",
                    str(output),
                ]
            )

            self.assertEqual(exit_code, 0)
            comparison = json.loads((output / "comparison.json").read_text("utf-8"))
            holdout = comparison["holdout_split"]
            self.assertEqual(holdout["role"], "validation")
            self.assertEqual(holdout["manifest_source"], str(paths["manifest"]))
            self.assertEqual(
                holdout["source_dataset_source"],
                str(paths["dataset"]),
            )
            validation_artifact = output / "validation-dataset.json"
            self.assertEqual(
                holdout["validation_dataset_source"],
                str(validation_artifact),
            )
            self.assertEqual(
                json.loads(validation_artifact.read_text("utf-8")),
                validation_dataset,
            )
            self.assertEqual(
                holdout["validation_dataset_fingerprint"],
                manifest["splits"]["validation"]["dataset_fingerprint"],
            )
            self.assertEqual(
                comparison["dataset"]["fingerprint"],
                manifest["splits"]["validation"]["dataset_fingerprint"],
            )
            before_report = json.loads(
                (output / "before-evaluation.json").read_text("utf-8")
            )
            self.assertEqual(
                before_report["coverage"]["evaluated"],
                manifest["splits"]["validation"]["case_count"],
            )
            markdown = (output / "comparison.md").read_text("utf-8")
            self.assertIn("## Validation holdout", markdown)
            self.assertIn(manifest["manifest_fingerprint"], markdown)

            with self.assertRaisesRegex(ValueError, "complete source dataset"):
                quality_gate.run_quality_gate(
                    paths["validation"],
                    paths["before"],
                    paths["after"],
                    root / "subset-report",
                    split_manifest_path=paths["manifest"],
                )

    def test_formal_holdout_cli_returns_one_for_collection_bias(self) -> None:
        dataset, _, manifest, before, after = _holdout_gate_fixture()
        dataset_by_id = {item["id"]: item for item in dataset["items"]}
        for case in after["cases"]:
            item = dataset_by_id[case["id"]]
            if item["query_type"] == "no-answer":
                continue
            case["results"] = [
                {"image_id": f"small:{case['id']}:noise.jpg", "rank": 1},
                {"image_id": item["relevant_images"][0]["image_id"], "rank": 2},
                {"image_id": item["relevant_images"][1]["image_id"], "rank": 3},
                {"image_id": f"large:{case['id']}:noise.jpg", "rank": 4},
            ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "dataset": root / "dataset.json",
                "manifest": root / "split.json",
                "before": root / "before.json",
                "after": root / "after.json",
            }
            for name, payload in (
                ("dataset", dataset),
                ("manifest", manifest),
                ("before", before),
                ("after", after),
            ):
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            output = root / "report"

            exit_code = quality_gate.main(
                [
                    "--dataset",
                    str(paths["dataset"]),
                    "--before-run",
                    str(paths["before"]),
                    "--after-run",
                    str(paths["after"]),
                    "--split-manifest",
                    str(paths["manifest"]),
                    "--output-dir",
                    str(output),
                ]
            )

            self.assertEqual(exit_code, 1)
            comparison = json.loads((output / "comparison.json").read_text("utf-8"))
            checks = comparison["quality_gate"]["checks"]
            self.assertTrue(checks["cross_collection_coverage"]["passed"])
            self.assertFalse(checks["cross_collection_pairwise_accuracy"]["passed"])

    def test_formal_gate_passes_with_cross_collection_human_evidence(self) -> None:
        dataset, before, after = _formal_gate_fixture()
        before_report = evaluate.evaluate_dataset(dataset, before)
        after_report = evaluate.evaluate_dataset(dataset, after)

        gate = quality_gate.assess_quality_gate(before_report, after_report)

        self.assertTrue(gate["passed"])
        self.assertTrue(gate["collection_fairness_enforced"])
        self.assertTrue(gate["checks"]["cross_collection_coverage"]["passed"])
        self.assertEqual(
            gate["checks"]["cross_collection_pairwise_accuracy"]["actual"],
            1.0,
        )

    def test_formal_gate_rejects_directional_collection_bias(self) -> None:
        dataset, before, after = _formal_gate_fixture()
        for case in after["cases"]:
            if case["id"] == "large-answer":
                case["results"] = [
                    {"image_id": "small:after-noise.jpg", "rank": 1},
                    {"image_id": "large:answer.jpg", "rank": 2},
                ]
            elif case["id"] == "small-answer":
                case["results"] = [
                    {"image_id": "large:after-noise.jpg", "rank": 1},
                    {"image_id": "small:answer.jpg", "rank": 2},
                ]

        gate = quality_gate.assess_quality_gate(
            evaluate.evaluate_dataset(dataset, before),
            evaluate.evaluate_dataset(dataset, after),
        )

        self.assertFalse(gate["passed"])
        checks = gate["checks"]
        self.assertTrue(checks["cross_collection_coverage"]["passed"])
        self.assertFalse(checks["cross_collection_pairwise_accuracy"]["passed"])
        self.assertFalse(checks["worst_directional_pairwise_accuracy"]["passed"])

    def test_formal_gate_does_not_treat_single_collection_as_passing(self) -> None:
        dataset, before, after = _formal_gate_fixture()
        for item in dataset["items"]:
            item["library_scope"] = {
                "mode": "single",
                "library_ids": ["large"],
            }
        dataset["items"][1]["relevant_images"] = [
            {"image_id": "large:small-case-answer.jpg"}
        ]
        for case in (*before["cases"], *after["cases"]):
            case["library_ids"] = ["large"]
        before["cases"][0]["results"] = [
            *(
                {"image_id": f"large:before-a-noise-{index}.jpg", "rank": index + 1}
                for index in range(5)
            ),
            {"image_id": "large:answer.jpg", "rank": 6},
        ]
        before["cases"][1]["results"] = [
            *(
                {"image_id": f"large:before-b-noise-{index}.jpg", "rank": index + 1}
                for index in range(5)
            ),
            {"image_id": "large:small-case-answer.jpg", "rank": 6},
        ]
        before["cases"][2]["results"] = [
            {"image_id": "large:false-return.jpg", "rank": 1}
        ]
        after["cases"][0]["results"] = [{"image_id": "large:answer.jpg", "rank": 1}]
        after["cases"][1]["results"] = [
            {"image_id": "large:small-case-answer.jpg", "rank": 1}
        ]
        after["cases"][2]["results"] = []

        gate = quality_gate.assess_quality_gate(
            evaluate.evaluate_dataset(dataset, before),
            evaluate.evaluate_dataset(dataset, after),
        )

        self.assertFalse(gate["passed"])
        coverage = gate["checks"]["cross_collection_coverage"]
        self.assertFalse(coverage["passed"])
        self.assertEqual(coverage["actual"], 0.0)
        for name in (
            "no_answer_false_return_rate",
            "precision_at_5_gain",
            "recall_at_5_drop",
            "p95_latency_increase_ratio",
            "api_request_increase",
        ):
            self.assertTrue(gate["checks"][name]["passed"], name)

    def test_formal_gate_fails_when_all_enabled_scope_changes(self) -> None:
        dataset, before, after = _formal_gate_fixture()
        for item in dataset["items"]:
            item["library_scope"] = {"mode": "all_enabled", "library_ids": []}
        for case in after["cases"]:
            case["library_ids"] = ["large", "small", "new-library"]

        gate = quality_gate.assess_quality_gate(
            evaluate.evaluate_dataset(dataset, before),
            evaluate.evaluate_dataset(dataset, after),
        )

        self.assertFalse(gate["passed"])
        self.assertFalse(gate["checks"]["cross_collection_scope_consistency"]["passed"])


if __name__ == "__main__":
    unittest.main()
