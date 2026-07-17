from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.search_quality import calibrate, evaluate, quality_gate
from tests.search_quality import split as dataset_split


def _query(mode: str, label: str) -> dict[str, str]:
    if mode == "text":
        return {"text": label}
    if mode == "image":
        return {"image": f"fixtures/{label}.png"}
    return {"text": label, "image": f"fixtures/{label}.png"}


def _dataset(
    per_stratum: int = 4,
    *,
    annotation_status: str = "synthetic_fixture",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for mode in sorted(evaluate.MODES):
        for index in range(per_stratum):
            answer_id = f"{mode}-answer-{index}"
            empty_id = f"{mode}-empty-{index}"
            if annotation_status == "human_verified":
                annotation = {
                    "status": "human_verified",
                    "annotator": "search-quality-test-fixture",
                    "annotated_at": "2026-07-13T12:00:00+00:00",
                    "notes": "In-memory unit-test fixture, not a release label.",
                }
            else:
                annotation = {
                    "status": annotation_status,
                    "notes": "Synthetic split-tool test fixture.",
                }
            items.extend(
                [
                    {
                        "id": answer_id,
                        "query_type": mode,
                        "mode": mode,
                        "query": _query(mode, answer_id),
                        "library_scope": {
                            "mode": "selected",
                            "library_ids": ["fixture"],
                        },
                        "relevant_images": [
                            {"image_id": f"fixture:{answer_id}:relevant"}
                        ],
                        "annotation": copy.deepcopy(annotation),
                    },
                    {
                        "id": empty_id,
                        "query_type": "no-answer",
                        "mode": mode,
                        "query": _query(mode, empty_id),
                        "library_scope": {
                            "mode": "selected",
                            "library_ids": ["fixture"],
                        },
                        "relevant_images": [],
                        "annotation": copy.deepcopy(annotation),
                    },
                ]
            )
    return {
        "schema_version": 1,
        "name": "deterministic-holdout-test-fixture",
        "description": "Synthetic unit-test fixture only.",
        "items": items,
    }


def _run(dataset: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for item in evaluate.validate_dataset(dataset):
        mode = item["mode"]
        lower_is_better = mode != "combined"
        if item["query_type"] == "no-answer":
            results = [
                {
                    "image_id": f"fixture:{item['id']}:false",
                    "score": 0.9 if lower_is_better else 0.1,
                }
            ]
        else:
            results = [
                {
                    "image_id": item["relevant_images"][0]["image_id"],
                    "score": 0.1 if lower_is_better else 0.9,
                },
                {
                    "image_id": f"fixture:{item['id']}:noise",
                    "score": 0.2 if lower_is_better else 0.8,
                },
            ]
        cases.append(
            {
                "id": item["id"],
                "latency_ms": 1,
                "api_requests": 0,
                "results": results,
            }
        )
    return {
        "schema_version": 1,
        "name": "deterministic-holdout-run",
        "score_semantics": {
            "text": "lower_is_better",
            "image": "lower_is_better",
            "combined": "higher_is_better",
        },
        "cases": cases,
    }


def _formal_capture_run(dataset: dict[str, Any]) -> dict[str, Any]:
    run = _run(dataset)
    items = evaluate.validate_dataset(dataset)
    run.update(
        {
            "dataset": {
                "name": dataset["name"],
                "fingerprint": evaluate.query_corpus_fingerprint(dataset),
                "fingerprint_algorithm": (evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM),
                "case_count": len(items),
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
        }
    )
    if isinstance(dataset.get("split"), dict):
        run["split"] = {
            key: dataset["split"][key]
            for key in (
                "kind",
                "role",
                "manifest_fingerprint",
                "manifest_fingerprint_algorithm",
            )
        }
    return run


class SearchQualitySplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = _dataset()
        self.run_data = _run(self.dataset)

    def _build(
        self,
        *,
        seed: str = "release-0.5",
        fraction: float = 0.4,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return dataset_split.build_split_artifacts(
            self.dataset,
            seed=seed,
            validation_fraction=fraction,
            allow_synthetic=True,
        )

    def test_split_is_reproducible_disjoint_exhaustive_and_stratified(self):
        first = self._build()
        second = self._build()

        self.assertEqual(first, second)
        manifest, calibration_dataset, validation_dataset = first
        role_ids = dataset_split.validate_split_manifest(manifest)
        calibration_ids = set(role_ids["calibration"])
        validation_ids = set(role_ids["validation"])
        source_ids = {item["id"] for item in self.dataset["items"]}
        self.assertFalse(calibration_ids & validation_ids)
        self.assertEqual(calibration_ids | validation_ids, source_ids)
        self.assertEqual(len(manifest["strata"]), 6)
        self.assertTrue(
            all(
                stratum["calibration"] >= 1 and stratum["validation"] >= 1
                for stratum in manifest["strata"]
            )
        )
        self.assertEqual(
            evaluate.dataset_fingerprint(calibration_dataset),
            manifest["splits"]["calibration"]["dataset_fingerprint"],
        )
        self.assertEqual(
            evaluate.dataset_fingerprint(validation_dataset),
            manifest["splits"]["validation"]["dataset_fingerprint"],
        )
        self.assertEqual(calibration_dataset["split"]["role"], "calibration")
        self.assertEqual(validation_dataset["split"]["role"], "validation")

    def test_seed_and_source_fingerprint_are_part_of_the_identity(self):
        dataset = _dataset(per_stratum=5)
        first, _, _ = dataset_split.build_split_artifacts(
            dataset,
            seed="release-a",
            allow_synthetic=True,
        )
        second, _, _ = dataset_split.build_split_artifacts(
            dataset,
            seed="release-b",
            allow_synthetic=True,
        )
        self.assertNotEqual(
            first["manifest_fingerprint"], second["manifest_fingerprint"]
        )
        self.assertNotEqual(
            first["splits"]["validation"]["case_ids"],
            second["splits"]["validation"]["case_ids"],
        )

        changed = copy.deepcopy(dataset)
        changed["items"][0]["query"]["text"] += " changed"
        changed_manifest, _, _ = dataset_split.build_split_artifacts(
            changed,
            seed="release-a",
            allow_synthetic=True,
        )
        self.assertNotEqual(
            first["dataset"]["fingerprint"],
            changed_manifest["dataset"]["fingerprint"],
        )

    def test_split_fails_closed_for_pending_or_unattributed_human_labels(self):
        pending = copy.deepcopy(self.dataset)
        pending["items"][0]["annotation"] = {
            "status": "pending",
            "notes": "not reviewed",
        }
        pending["items"][0]["relevant_images"] = []
        with self.assertRaisesRegex(ValueError, "pending cases"):
            dataset_split.build_split_artifacts(
                pending,
                allow_synthetic=True,
            )

        human = _dataset(annotation_status="human_verified")
        human["items"][0]["annotation"].pop("annotator")
        with self.assertRaisesRegex(ValueError, "needs annotator"):
            dataset_split.build_split_artifacts(human)

        with self.assertRaisesRegex(ValueError, "allow_synthetic"):
            dataset_split.build_split_artifacts(self.dataset)

    def test_every_required_stratum_needs_two_cases(self):
        with self.assertRaisesRegex(ValueError, "at least two cases"):
            dataset_split.build_split_artifacts(
                _dataset(per_stratum=1),
                allow_synthetic=True,
            )

        missing = copy.deepcopy(self.dataset)
        missing["items"] = [
            item
            for item in missing["items"]
            if not (item["mode"] == "combined" and item["query_type"] == "no-answer")
        ]
        with self.assertRaisesRegex(ValueError, "missing strata: combined/no-answer"):
            dataset_split.build_split_artifacts(
                missing,
                allow_synthetic=True,
            )

    def test_boundary_fractions_still_keep_both_roles_in_every_stratum(self):
        dataset = _dataset(per_stratum=2)
        for fraction in (0.000001, 0.999999):
            manifest, _, _ = dataset_split.build_split_artifacts(
                dataset,
                validation_fraction=fraction,
                allow_synthetic=True,
            )
            self.assertTrue(
                all(
                    stratum["calibration"] == 1 and stratum["validation"] == 1
                    for stratum in manifest["strata"]
                )
            )
        for invalid in (0.0, 1.0, float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "validation_fraction"):
                dataset_split.build_split_artifacts(
                    dataset,
                    validation_fraction=invalid,
                    allow_synthetic=True,
                )

    def test_manifest_validation_detects_cross_role_leakage(self):
        manifest, _, _ = self._build()
        tampered = copy.deepcopy(manifest)
        leaked = tampered["splits"]["validation"]["case_ids"][0]
        tampered["splits"]["calibration"]["case_ids"].append(leaked)
        tampered["splits"]["calibration"]["case_count"] += 1
        tampered["manifest_fingerprint"] = dataset_split.split_manifest_fingerprint(
            tampered
        )

        with self.assertRaisesRegex(ValueError, "leaks cases"):
            dataset_split.validate_split_manifest(tampered)

    def test_calibration_preparation_filters_holdout_and_rejects_validation(self):
        manifest, calibration_dataset, validation_dataset = self._build()
        prepared_dataset, prepared_run = dataset_split.prepare_calibration_inputs(
            self.dataset,
            self.run_data,
            manifest,
            allow_synthetic=True,
        )
        calibration_ids = manifest["splits"]["calibration"]["case_ids"]
        validation_ids = set(manifest["splits"]["validation"]["case_ids"])
        self.assertEqual(
            [item["id"] for item in prepared_dataset["items"]], calibration_ids
        )
        self.assertEqual(
            [case["id"] for case in prepared_run["cases"]], calibration_ids
        )
        self.assertFalse(
            {case["id"] for case in prepared_run["cases"]} & validation_ids
        )

        subset_dataset, subset_run = dataset_split.prepare_calibration_inputs(
            calibration_dataset,
            self.run_data,
            manifest,
            allow_synthetic=True,
        )
        self.assertEqual(subset_dataset, calibration_dataset)
        self.assertEqual([case["id"] for case in subset_run["cases"]], calibration_ids)
        with self.assertRaisesRegex(ValueError, "validation holdout"):
            dataset_split.prepare_calibration_inputs(
                validation_dataset,
                self.run_data,
                manifest,
                allow_synthetic=True,
            )

    def test_validation_preparation_accepts_source_or_frozen_subset(self):
        manifest, calibration_dataset, validation_dataset = self._build()
        validation_ids = manifest["splits"]["validation"]["case_ids"]
        validation_id_set = set(validation_ids)
        validation_run = copy.deepcopy(self.run_data)
        validation_run["cases"] = [
            case for case in validation_run["cases"] if case["id"] in validation_id_set
        ]

        source_dataset, source_run = dataset_split.prepare_validation_inputs(
            self.dataset,
            validation_run,
            manifest,
            allow_synthetic=True,
        )
        self.assertEqual(source_dataset, validation_dataset)
        self.assertEqual([case["id"] for case in source_run["cases"]], validation_ids)
        self.assertEqual(source_run["split"]["role"], "validation")

        subset_dataset, subset_run = dataset_split.prepare_validation_inputs(
            validation_dataset,
            validation_run,
            manifest,
            allow_synthetic=True,
        )
        self.assertEqual(subset_dataset, validation_dataset)
        self.assertEqual([case["id"] for case in subset_run["cases"]], validation_ids)
        with self.assertRaisesRegex(ValueError, "calibration role"):
            dataset_split.prepare_validation_inputs(
                calibration_dataset,
                validation_run,
                manifest,
                allow_synthetic=True,
            )

        provenance = dataset_split.validation_provenance(manifest)
        self.assertEqual(provenance["role"], "validation")
        self.assertEqual(
            provenance["validation_dataset_fingerprint"],
            manifest["splits"]["validation"]["dataset_fingerprint"],
        )
        self.assertEqual(
            provenance["source_dataset_fingerprint"],
            manifest["dataset"]["fingerprint"],
        )

    def test_validation_preparation_rejects_leakage_unknown_and_missing_cases(self):
        manifest, _, validation_dataset = self._build()
        validation_ids = manifest["splits"]["validation"]["case_ids"]
        validation_id_set = set(validation_ids)
        validation_run = copy.deepcopy(self.run_data)
        validation_run["cases"] = [
            case for case in validation_run["cases"] if case["id"] in validation_id_set
        ]

        with self.assertRaisesRegex(ValueError, "calibration case leakage"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                self.run_data,
                manifest,
                allow_synthetic=True,
            )

        unknown = copy.deepcopy(validation_run)
        extra = copy.deepcopy(unknown["cases"][0])
        extra["id"] = "outside-split"
        unknown["cases"].append(extra)
        with self.assertRaisesRegex(ValueError, "outside the split source"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                unknown,
                manifest,
                allow_synthetic=True,
            )

        missing = copy.deepcopy(validation_run)
        missing["cases"].pop()
        with self.assertRaisesRegex(ValueError, "missing validation cases"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                missing,
                manifest,
                allow_synthetic=True,
            )

        calibration_role = copy.deepcopy(validation_run)
        calibration_role["split"] = {"role": "calibration"}
        with self.assertRaisesRegex(ValueError, "calibration run"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                calibration_role,
                manifest,
                allow_synthetic=True,
            )

        stale_validation = copy.deepcopy(validation_run)
        stale_validation["split"] = {
            "kind": dataset_split.SPLIT_KIND,
            "role": "validation",
            "manifest_fingerprint": "0" * 64,
            "manifest_fingerprint_algorithm": (
                dataset_split.SPLIT_FINGERPRINT_ALGORITHM
            ),
        }
        with self.assertRaisesRegex(ValueError, "does not match manifest"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                stale_validation,
                manifest,
                allow_synthetic=True,
            )

    def test_formal_validation_rejects_stale_or_ineligible_capture(self):
        source = _dataset(annotation_status="human_verified")
        manifest, _, validation_dataset = dataset_split.build_split_artifacts(source)
        run = _formal_capture_run(validation_dataset)
        prepared_dataset, prepared_run = dataset_split.prepare_validation_inputs(
            validation_dataset,
            run,
            manifest,
        )
        self.assertEqual(prepared_dataset, validation_dataset)
        self.assertEqual(len(prepared_run["cases"]), len(validation_dataset["items"]))

        changed_source = copy.deepcopy(source)
        for item in changed_source["items"]:
            if item["mode"] in {"text", "combined"}:
                item["query"]["text"] += " changed"
        changed_manifest, _, changed_validation = dataset_split.build_split_artifacts(
            changed_source,
            seed=manifest["strategy"]["seed"],
        )
        with self.assertRaisesRegex(ValueError, "split metadata does not match"):
            dataset_split.prepare_validation_inputs(
                changed_validation,
                run,
                changed_manifest,
            )

        missing_split = copy.deepcopy(run)
        missing_split.pop("split")
        with self.assertRaisesRegex(ValueError, "missing validation split metadata"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                missing_split,
                manifest,
            )

        calibration_split = copy.deepcopy(run)
        calibration_split["split"]["role"] = "calibration"
        with self.assertRaisesRegex(ValueError, "calibration run cannot be used"):
            dataset_split.prepare_validation_inputs(
                validation_dataset,
                calibration_split,
                manifest,
            )

        invalid_runs = {
            "draft": ("draft", True, "non-draft"),
            "baseline": ("baseline_eligible", False, "baseline_eligible"),
        }
        for label, (field, value, message) in invalid_runs.items():
            invalid = copy.deepcopy(run)
            invalid[field] = value
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                dataset_split.prepare_validation_inputs(
                    validation_dataset,
                    invalid,
                    manifest,
                )

        invalid_capture_values = {
            "kind": ("kind", "other-capture", "persistent backend"),
            "capture_baseline": (
                "baseline_eligible",
                False,
                "capture must be baseline_eligible",
            ),
            "pending_bool": (
                "pending_case_count",
                False,
                "contain no pending cases",
            ),
            "top_k": ("top_k", 4, "top_k must be at least 5"),
            "candidate_k": (
                "candidate_k",
                49,
                "candidate_k must be at least 50",
            ),
        }
        for label, (field, value, message) in invalid_capture_values.items():
            invalid = copy.deepcopy(run)
            invalid["capture"][field] = value
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                dataset_split.prepare_validation_inputs(
                    validation_dataset,
                    invalid,
                    manifest,
                )

    def test_formal_calibration_requires_capture_bound_to_used_queries(self):
        source = _dataset(annotation_status="human_verified")
        manifest, calibration_dataset, _ = dataset_split.build_split_artifacts(source)

        full_run = _formal_capture_run(source)
        prepared_dataset, prepared_run = dataset_split.prepare_calibration_inputs(
            source,
            full_run,
            manifest,
        )
        self.assertEqual(prepared_dataset, calibration_dataset)
        self.assertEqual(
            [case["id"] for case in prepared_run["cases"]],
            manifest["splits"]["calibration"]["case_ids"],
        )

        subset_run = _formal_capture_run(calibration_dataset)
        dataset_split.prepare_calibration_inputs(
            calibration_dataset,
            subset_run,
            manifest,
        )

        stale = copy.deepcopy(subset_run)
        stale["dataset"]["fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
            dataset_split.prepare_calibration_inputs(
                calibration_dataset,
                stale,
                manifest,
            )

        legacy = _run(calibration_dataset)
        with self.assertRaisesRegex(ValueError, "non-draft capture"):
            dataset_split.prepare_calibration_inputs(
                calibration_dataset,
                legacy,
                manifest,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "calibration.json"
            run_path = root / "legacy-run.json"
            dataset_path.write_text(json.dumps(calibration_dataset), encoding="utf-8")
            run_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-draft capture"):
                calibrate.calibrate_files(dataset_path, run_path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "calibration.json"
            run_path = root / "stale-run.json"
            manifest_path = root / "split.json"
            dataset_path.write_text(json.dumps(calibration_dataset), encoding="utf-8")
            run_path.write_text(json.dumps(stale), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
                calibrate.calibrate_files(
                    dataset_path,
                    run_path,
                    split_manifest_path=manifest_path,
                )

    def test_changed_source_and_unknown_run_cases_fail_closed(self):
        manifest, _, _ = self._build()
        changed = copy.deepcopy(self.dataset)
        changed["items"][0]["query"]["text"] += " changed"
        with self.assertRaisesRegex(ValueError, "does not match"):
            dataset_split.prepare_calibration_inputs(
                changed,
                self.run_data,
                manifest,
                allow_synthetic=True,
            )

        run = copy.deepcopy(self.run_data)
        extra = copy.deepcopy(run["cases"][0])
        extra["id"] = "outside-split"
        run["cases"].append(extra)
        with self.assertRaisesRegex(ValueError, "outside the split source"):
            dataset_split.prepare_calibration_inputs(
                self.dataset,
                run,
                manifest,
                allow_synthetic=True,
            )

    def test_cli_writes_three_deterministic_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.json"
            manifest_path = root / "split.json"
            calibration_path = root / "calibration.json"
            validation_path = root / "validation.json"
            dataset_path.write_text(json.dumps(self.dataset), encoding="utf-8")
            arguments = [
                "--dataset",
                str(dataset_path),
                "--manifest",
                str(manifest_path),
                "--calibration-output",
                str(calibration_path),
                "--validation-output",
                str(validation_path),
                "--seed",
                "release-0.5",
                "--allow-synthetic",
            ]
            self.assertEqual(dataset_split.main(arguments), 0)
            first_payloads = [
                path.read_bytes()
                for path in (manifest_path, calibration_path, validation_path)
            ]
            self.assertEqual(dataset_split.main(arguments), 0)
            self.assertEqual(
                first_payloads,
                [
                    path.read_bytes()
                    for path in (manifest_path, calibration_path, validation_path)
                ],
            )

    def test_calibrate_cli_records_manifest_and_never_uses_validation_cases(self):
        manifest, _, _ = self._build()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.json"
            run_path = root / "run.json"
            manifest_path = root / "split.json"
            output_path = root / "search-quality.json"
            dataset_path.write_text(json.dumps(self.dataset), encoding="utf-8")
            run_path.write_text(json.dumps(self.run_data), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertEqual(
                calibrate.main(
                    [
                        "--dataset",
                        str(dataset_path),
                        "--run",
                        str(run_path),
                        "--split-manifest",
                        str(manifest_path),
                        "--output",
                        str(output_path),
                        "--allow-synthetic",
                    ]
                ),
                0,
            )
            configuration = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                configuration["holdout_split"]["manifest_fingerprint"],
                manifest["manifest_fingerprint"],
            )
            self.assertEqual(
                configuration["coverage"]["evaluated"],
                manifest["splits"]["calibration"]["case_count"],
            )
            self.assertEqual(
                configuration["holdout_split"]["validation_dataset_fingerprint"],
                manifest["splits"]["validation"]["dataset_fingerprint"],
            )

    def test_full_dataset_baseline_cannot_be_reused_after_split(self):
        manifest, _, _ = self._build()
        full_baseline = evaluate.evaluate_dataset(
            self.dataset,
            self.run_data,
            allow_synthetic=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "dataset": root / "dataset.json",
                "run": root / "run.json",
                "manifest": root / "split.json",
                "baseline": root / "baseline.json",
            }
            payloads = {
                "dataset": self.dataset,
                "run": self.run_data,
                "manifest": manifest,
                "baseline": full_baseline,
            }
            for key, path in paths.items():
                path.write_text(json.dumps(payloads[key]), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "different dataset|fingerprint does not match",
            ):
                calibrate.calibrate_files(
                    paths["dataset"],
                    paths["run"],
                    split_manifest_path=paths["manifest"],
                    baseline_path=paths["baseline"],
                    allow_synthetic=True,
                )

    def test_full_formal_gate_rejects_ineligible_validation_capture(self):
        source = _dataset(annotation_status="human_verified")
        for item in source["items"]:
            item["library_scope"] = {
                "mode": "selected",
                "library_ids": ["large", "small"],
            }
            if item["query_type"] != "no-answer":
                item["relevant_images"] = [
                    {"image_id": f"large:{item['id']}:large.jpg"},
                    {"image_id": f"small:{item['id']}:small.jpg"},
                ]
        manifest, _, validation_dataset = dataset_split.build_split_artifacts(source)

        def gate_run(name: str, improved: bool) -> dict[str, Any]:
            run = _formal_capture_run(validation_dataset)
            run["name"] = name
            for item, case in zip(
                validation_dataset["items"], run["cases"], strict=True
            ):
                case["library_ids"] = ["large", "small"]
                if item["query_type"] == "no-answer":
                    case["results"] = (
                        []
                        if improved
                        else [{"image_id": f"large:{item['id']}:false.jpg"}]
                    )
                elif improved:
                    case["results"] = [
                        {"image_id": value["image_id"]}
                        for value in item["relevant_images"]
                    ]
                else:
                    case["results"] = [
                        {"image_id": f"small:{item['id']}:noise-{index}.jpg"}
                        for index in range(5)
                    ] + [
                        {"image_id": value["image_id"]}
                        for value in item["relevant_images"]
                    ]
                for rank, result in enumerate(case["results"], start=1):
                    result["rank"] = rank
            return run

        before = gate_run("formal-before", False)
        after = gate_run("formal-after", True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "dataset": root / "source.json",
                "manifest": root / "split.json",
                "before": root / "before.json",
                "after": root / "after.json",
            }
            for name, payload in (
                ("dataset", source),
                ("manifest", manifest),
                ("before", before),
                ("after", after),
            ):
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            comparison = quality_gate.run_quality_gate(
                paths["dataset"],
                paths["before"],
                paths["after"],
                root / "valid-report",
                split_manifest_path=paths["manifest"],
            )
            self.assertTrue(comparison["quality_gate"]["passed"])

            after["draft"] = True
            after["baseline_eligible"] = False
            after["capture"]["candidate_k"] = 1
            paths["after"].write_text(json.dumps(after), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-draft capture"):
                quality_gate.run_quality_gate(
                    paths["dataset"],
                    paths["before"],
                    paths["after"],
                    root / "invalid-report",
                    split_manifest_path=paths["manifest"],
                )


if __name__ == "__main__":
    unittest.main()
