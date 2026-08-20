from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tests.search_quality import calibrate, evaluate, pipeline


def _query(mode: str, case_id: str) -> dict[str, str]:
    if mode == "text":
        return {"text": f"query {case_id}"}
    if mode == "image":
        return {"image": f"queries/{case_id}.png"}
    return {
        "text": f"query {case_id}",
        "image": f"queries/{case_id}.png",
    }


def _human_dataset() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    answerable_counts = {"text": 25, "image": 10, "combined": 10}
    for mode, count in answerable_counts.items():
        for index in range(count):
            case_id = f"{mode}-answer-{index + 1:02d}"
            library = "lib-a" if index % 2 == 0 else "lib-b"
            items.append(
                {
                    "id": case_id,
                    "query_type": mode,
                    "mode": mode,
                    "query": _query(mode, case_id),
                    "library_scope": {
                        "mode": "all_enabled",
                        "library_ids": [],
                    },
                    "relevant_images": [
                        {
                            "image_id": f"{library}:images/{case_id}.jpg",
                            "sha256": f"{len(items) + 1:064x}",
                        }
                    ],
                    "annotation": {
                        "status": "human_verified",
                        "annotator": "pipeline-test-reviewer",
                        "annotated_at": "2026-07-13T10:00:00+00:00",
                        "notes": "Unit-test human-label fixture.",
                    },
                }
            )
    for mode in ("text", "image", "combined"):
        for index in range(5):
            case_id = f"{mode}-no-answer-{index + 1:02d}"
            items.append(
                {
                    "id": case_id,
                    "query_type": "no-answer",
                    "mode": mode,
                    "query": _query(mode, case_id),
                    "library_scope": {
                        "mode": "all_enabled",
                        "library_ids": [],
                    },
                    "relevant_images": [],
                    "annotation": {
                        "status": "human_verified",
                        "annotator": "pipeline-test-reviewer",
                        "annotated_at": "2026-07-13T10:00:00+00:00",
                        "notes": "Unit-test human-label fixture.",
                    },
                }
            )
    assert len(items) == 60
    return {
        "schema_version": 1,
        "name": "pipeline-human-review-test",
        "description": "Formal-shape unit-test fixture with explicit annotations.",
        "items": items,
    }


def _runtime_diagnostics(configuration: dict[str, Any], mode: str) -> dict[str, Any]:
    thresholds = configuration[mode]
    diagnostics = {
        "configured": True,
        "ranking_mode": "confidence_v2",
        "thresholds": {
            "minimum": thresholds["minimum_confidence"],
            "possible": 0.55,
            "high": 0.78,
        },
        "score_gap": thresholds["score_gap"],
        "max_confidence_drop": thresholds["max_confidence_drop"],
        "fusion": copy.deepcopy(configuration["fusion"]),
    }
    collection_calibration = configuration.get("collection_calibration")
    if isinstance(collection_calibration, dict):
        libraries = collection_calibration["modes"][mode]["libraries"]
        diagnostics["collection_calibration"] = {
            "configured": True,
            "mode": collection_calibration["mode"],
            "fallback": collection_calibration["fallback"],
            "offsets": {
                library_id: libraries.get(library_id, {}).get("offset", 0.0)
                for library_id in ("lib-a", "lib-b")
            },
            "unknown_library_ids": [
                library_id
                for library_id in ("lib-a", "lib-b")
                if library_id not in libraries
            ],
        }
    return diagnostics


def _formal_run(
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    *,
    name: str,
    captured_at: datetime,
    quality: str,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases = []
    for position, item in enumerate(evaluate.validate_dataset(dataset), start=1):
        mode = str(item["mode"])
        relevant = item["relevant_images"]
        other_library = "lib-b"
        if relevant and str(relevant[0]["image_id"]).startswith("lib-b:"):
            other_library = "lib-a"
        noise = {
            "rank": 1,
            "image_id": f"{other_library}:noise/{item['id']}.jpg",
            "library_id": other_library,
            "sha256": f"{position + 1000:064x}",
            "confidence": 0.10 if quality == "candidate" else 0.70,
        }
        if not relevant:
            results = [noise] if quality in {"candidate", "before"} else []
        elif quality == "before":
            results = [noise]
        else:
            relevant_result = {
                "rank": 1,
                "image_id": relevant[0]["image_id"],
                "library_id": str(relevant[0]["image_id"]).split(":", 1)[0],
                "sha256": relevant[0]["sha256"],
                "confidence": 0.90,
            }
            noise["rank"] = 2
            noise["confidence"] = 0.60
            results = [relevant_result, noise]
        case: dict[str, Any] = {
            "id": item["id"],
            "mode": mode,
            "status": "ok" if results else "no_reliable_match",
            "candidate_count": 50,
            "filtered_count": max(0, 50 - len(results)),
            "latency_ms": 10.0,
            "api_requests": 0,
            "backend_requests": 2,
            "library_ids": ["lib-a", "lib-b"],
            "ranking_mode": "confidence_v2",
            "results": results,
        }
        if configuration is not None:
            case["search_quality"] = _runtime_diagnostics(configuration, mode)
        elif quality == "candidate":
            case["search_quality"] = {
                "configured": True,
                "fusion": {
                    "mode": "confidence_v2",
                    **calibrate.FUSION_V2_DEFAULTS,
                },
            }
        cases.append(case)
    return {
        "schema_version": 1,
        "name": name,
        "score_semantics": {
            "text": "higher_is_better",
            "image": "higher_is_better",
            "combined": "higher_is_better",
        },
        "score_fields": {
            "text": "confidence",
            "image": "confidence",
            "combined": "confidence",
        },
        "search_quality": {
            "fusion": {
                "mode": "confidence_v2",
                **calibrate.FUSION_V2_DEFAULTS,
            }
        },
        "dataset": {
            "name": dataset["name"],
            "fingerprint": evaluate.query_corpus_fingerprint(dataset),
            "fingerprint_algorithm": evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM,
            "case_count": len(dataset["items"]),
        },
        "baseline_eligible": True,
        "draft": False,
        "capture": {
            "kind": "zvec-persistent-backend-capture",
            "generated_at": captured_at.isoformat(),
            "top_k": 10,
            "candidate_k": 50,
            "annotation_statuses": {"human_verified": len(dataset["items"])},
            "pending_case_count": 0,
            "baseline_eligible": True,
            "backend_protocol_version": 2,
        },
        "split": {
            "kind": "zvec-search-quality-split",
            "role": dataset["split"]["role"],
            "manifest_fingerprint": manifest["manifest_fingerprint"],
            "manifest_fingerprint_algorithm": "sha256-canonical-json-v1",
        },
        "cases": cases,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class SearchQualityPipelineTest(unittest.TestCase):
    def _workspace(
        self, temporary: str
    ) -> tuple[pipeline.PipelinePaths, dict[str, Any]]:
        dataset = _human_dataset()
        root = Path(temporary)
        dataset_path = root / "reviewed-dataset.json"
        _write_json(dataset_path, dataset)
        return pipeline.PipelinePaths.resolve(dataset_path, root / "attempt"), dataset

    def test_status_is_read_only_and_reports_pending_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, dataset = self._workspace(temporary)
            for item in dataset["items"][:18]:
                item["annotation"] = {
                    "status": "pending",
                    "annotator": None,
                    "annotated_at": None,
                    "notes": "Pending test label.",
                }
                item["relevant_images"] = []
            _write_json(paths.dataset, dataset)

            status = pipeline.inspect_pipeline(paths)

            self.assertEqual(status["stage"], "review_required")
            self.assertEqual(status["dataset"]["human_verified"], 42)
            self.assertEqual(status["dataset"]["pending"], 18)
            self.assertFalse(paths.work_dir.exists())

    def test_prepare_freezes_exact_36_24_split_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)

            self.assertEqual(
                pipeline.main(
                    [
                        "prepare",
                        "--dataset",
                        str(paths.dataset),
                        "--work-dir",
                        str(paths.work_dir),
                    ]
                ),
                0,
            )
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["splits"]["calibration"]["case_count"], 36)
            self.assertEqual(manifest["splits"]["validation"]["case_count"], 24)
            manifest["strategy"]["seed"] = "tampered"
            _write_json(paths.manifest, manifest)

            with self.assertRaisesRegex(pipeline.PipelineError, "does not match"):
                pipeline.inspect_pipeline(paths)

    def test_dry_run_does_not_create_split_or_other_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)

            status, exit_code = pipeline.run_pipeline(paths, dry_run=True)

            self.assertEqual(exit_code, 0)
            self.assertEqual(status["stage"], "split_required")
            self.assertFalse(paths.work_dir.exists())

    def test_existing_frozen_split_is_adopted_without_regeneration(self):
        with tempfile.TemporaryDirectory() as temporary:
            default_paths, dataset = self._workspace(temporary)
            root = Path(temporary)
            manifest, calibration_dataset, validation_dataset = (
                pipeline.dataset_split.build_split_artifacts(
                    dataset,
                    seed="already-frozen-release-seed",
                    validation_fraction=0.40,
                    allow_synthetic=False,
                )
            )
            manifest_path = root / "existing-manifest.json"
            calibration_path = root / "existing-calibration.json"
            validation_path = root / "existing-validation.json"
            _write_json(manifest_path, manifest)
            _write_json(calibration_path, calibration_dataset)
            _write_json(validation_path, validation_dataset)
            paths = pipeline.PipelinePaths.resolve(
                default_paths.dataset,
                root / "new-attempt",
                manifest=manifest_path,
                calibration_dataset=calibration_path,
                validation_dataset=validation_path,
            )

            status = pipeline.inspect_pipeline(paths)

            self.assertEqual(status["stage"], "captures_required")
            self.assertEqual(status["split"]["ownership"], "adopted")
            self.assertEqual(
                status["split"]["manifest_fingerprint"],
                manifest["manifest_fingerprint"],
            )
            self.assertEqual(status["split"]["seed"], "already-frozen-release-seed")
            self.assertFalse(paths.work_dir.exists())

            changed = copy.deepcopy(validation_dataset)
            changed["description"] += " tampered"
            _write_json(validation_path, changed)
            with self.assertRaisesRegex(pipeline.PipelineError, "does not exactly"):
                pipeline.inspect_pipeline(paths)

    def test_adopted_split_arguments_are_all_or_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)
            with self.assertRaisesRegex(pipeline.PipelineError, "supplied together"):
                pipeline.PipelinePaths.resolve(
                    paths.dataset,
                    paths.work_dir,
                    manifest=Path(temporary) / "manifest.json",
                )

    def test_formal_capture_must_be_non_draft_and_split_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)
            self.assertEqual(
                pipeline.main(
                    [
                        "prepare",
                        "--dataset",
                        str(paths.dataset),
                        "--work-dir",
                        str(paths.work_dir),
                    ]
                ),
                0,
            )
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            calibration_dataset = json.loads(
                paths.calibration_dataset.read_text(encoding="utf-8")
            )
            validation_dataset = json.loads(
                paths.validation_dataset.read_text(encoding="utf-8")
            )
            captured_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
            calibration_run = _formal_run(
                calibration_dataset,
                manifest,
                name="draft-calibration",
                captured_at=captured_at,
                quality="candidate",
            )
            calibration_run["draft"] = True
            calibration_run["baseline_eligible"] = False
            calibration_run["capture"]["baseline_eligible"] = False
            validation_before = _formal_run(
                validation_dataset,
                manifest,
                name="validation-before",
                captured_at=captured_at,
                quality="before",
            )
            _write_json(paths.calibration_run, calibration_run)
            _write_json(paths.validation_before_run, validation_before)

            with self.assertRaisesRegex(pipeline.PipelineError, "non-draft"):
                pipeline.inspect_pipeline(paths)

            calibration_run = _formal_run(
                calibration_dataset,
                manifest,
                name="calibration",
                captured_at=captured_at,
                quality="candidate",
            )
            calibration_run.pop("split")
            _write_json(paths.calibration_run, calibration_run)
            with self.assertRaisesRegex(pipeline.PipelineError, "frozen split"):
                pipeline.inspect_pipeline(paths)

            calibration_run = _formal_run(
                calibration_dataset,
                manifest,
                name="legacy-calibration",
                captured_at=captured_at,
                quality="candidate",
            )
            calibration_run["cases"][0]["ranking_mode"] = "weighted_rrf"
            _write_json(paths.calibration_run, calibration_run)
            with self.assertRaisesRegex(pipeline.PipelineError, "confidence_v2"):
                pipeline.inspect_pipeline(paths)

    def test_run_calibrates_then_requires_a_new_configured_after_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)
            self.assertEqual(
                pipeline.main(
                    [
                        "prepare",
                        "--dataset",
                        str(paths.dataset),
                        "--work-dir",
                        str(paths.work_dir),
                    ]
                ),
                0,
            )
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            calibration_dataset = json.loads(
                paths.calibration_dataset.read_text(encoding="utf-8")
            )
            validation_dataset = json.loads(
                paths.validation_dataset.read_text(encoding="utf-8")
            )
            captured_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            _write_json(
                paths.calibration_run,
                _formal_run(
                    calibration_dataset,
                    manifest,
                    name="calibration-candidate",
                    captured_at=captured_at,
                    quality="candidate",
                ),
            )
            _write_json(
                paths.validation_before_run,
                _formal_run(
                    validation_dataset,
                    manifest,
                    name="validation-before",
                    captured_at=captured_at,
                    quality="before",
                ),
            )

            dry_status, dry_exit = pipeline.run_pipeline(paths, dry_run=True)
            self.assertEqual(dry_exit, 0)
            self.assertEqual(dry_status["stage"], "ready_to_calibrate")
            self.assertEqual(
                dry_status["dry_run"]["configuration_preview"]["fusion"]["mode"],
                "confidence_v2",
            )
            self.assertFalse(paths.configuration.exists())
            self.assertFalse(paths.state.exists())

            status, exit_code = pipeline.run_pipeline(paths)

            self.assertEqual(exit_code, 2)
            self.assertEqual(status["stage"], "after_capture_required")
            self.assertTrue(paths.configuration.is_file())
            self.assertTrue(paths.state.is_file())
            configuration = json.loads(paths.configuration.read_text(encoding="utf-8"))
            generated_at = datetime.fromisoformat(configuration["generated_at"])

            stale_after = _formal_run(
                validation_dataset,
                manifest,
                name="validation-after-stale",
                captured_at=generated_at - timedelta(seconds=1),
                quality="after",
                configuration=configuration,
            )
            _write_json(paths.validation_after_run, stale_after)
            with self.assertRaisesRegex(pipeline.PipelineError, "after calibration"):
                pipeline.inspect_pipeline(paths)

            after = _formal_run(
                validation_dataset,
                manifest,
                name="validation-after",
                captured_at=generated_at + timedelta(seconds=1),
                quality="after",
                configuration=configuration,
            )
            _write_json(paths.validation_after_run, after)

            status, exit_code = pipeline.run_pipeline(paths)

            self.assertEqual(exit_code, 0)
            self.assertEqual(status["stage"], "passed")
            self.assertTrue((paths.report_dir / "comparison.json").is_file())
            comparison = json.loads(
                (paths.report_dir / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                comparison["holdout_split"]["source_dataset_source"],
                str(paths.dataset),
            )
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(state["stage"], "passed")
            self.assertEqual(
                state["validation"]["after_run"]["source_sha256"],
                evaluate.source_file_sha256(paths.validation_after_run),
            )
            self.assertIn("validation-dataset.json", state["gate"]["artifacts"])
            derived_validation = paths.report_dir / "validation-dataset.json"
            derived_validation.write_text(
                derived_validation.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(pipeline.PipelineError, "gate.artifacts"):
                pipeline.inspect_pipeline(paths)

    def test_after_capture_must_prove_generated_config_is_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _dataset = self._workspace(temporary)
            dataset = json.loads(paths.dataset.read_text(encoding="utf-8"))
            report = pipeline._dataset_report(dataset, paths.dataset)
            planned = pipeline._planned_split(
                dataset,
                seed=pipeline.DEFAULT_SEED,
                validation_fraction=pipeline.DEFAULT_VALIDATION_FRACTION,
            )
            pipeline.prepare_split(paths, planned)
            captured_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            calibration_run = _formal_run(
                planned.calibration_dataset,
                planned.manifest,
                name="calibration",
                captured_at=captured_at,
                quality="candidate",
            )
            before = _formal_run(
                planned.validation_dataset,
                planned.manifest,
                name="before",
                captured_at=captured_at,
                quality="before",
            )
            _write_json(paths.calibration_run, calibration_run)
            _write_json(paths.validation_before_run, before)
            calibration_binding = pipeline._load_formal_run(
                paths.calibration_run,
                planned.calibration_dataset,
                planned.manifest,
                "calibration",
            )
            before_binding = pipeline._load_formal_run(
                paths.validation_before_run,
                planned.validation_dataset,
                planned.manifest,
                "validation",
            )
            configuration = pipeline._calibrate_stage(
                paths,
                report,
                planned,
                calibration_binding,
                before_binding,
                dry_run=False,
            )
            generated_at = datetime.fromisoformat(configuration["generated_at"])
            after = _formal_run(
                planned.validation_dataset,
                planned.manifest,
                name="after-wrong-config",
                captured_at=generated_at + timedelta(seconds=1),
                quality="after",
                configuration=configuration,
            )
            after["cases"][0]["search_quality"]["configured"] = False
            _write_json(paths.validation_after_run, after)

            with self.assertRaisesRegex(pipeline.PipelineError, "does not prove"):
                pipeline.inspect_pipeline(paths)

    def test_after_capture_binds_collection_calibration_offsets(self):
        configuration = {
            "fusion": {"mode": "confidence_v2"},
            "text": {
                "minimum_confidence": 0.5,
                "score_gap": 1.0,
                "max_confidence_drop": 1.0,
            },
            "collection_calibration": {
                "mode": "null_mean_offset_v1",
                "fallback": "identity",
                "modes": {
                    "text": {
                        "libraries": {
                            "lib-a": {"offset": -0.02},
                            "lib-b": {"offset": 0.02},
                        }
                    }
                },
            },
        }
        diagnostics = _runtime_diagnostics(configuration, "text")
        run = {
            "cases": [
                {
                    "id": "text-case",
                    "mode": "text",
                    "library_ids": ["lib-a", "lib-b"],
                    "search_quality": diagnostics,
                }
            ]
        }

        pipeline._configuration_matches_after_capture(run, configuration)

        wrong = copy.deepcopy(run)
        wrong["cases"][0]["search_quality"]["collection_calibration"]["offsets"][
            "lib-a"
        ] = 0.0
        with self.assertRaisesRegex(
            pipeline.PipelineError,
            "different Collection calibration",
        ):
            pipeline._configuration_matches_after_capture(wrong, configuration)

        missing = copy.deepcopy(run)
        missing["cases"][0]["search_quality"].pop("collection_calibration")
        with self.assertRaisesRegex(
            pipeline.PipelineError,
            "different Collection calibration",
        ):
            pipeline._configuration_matches_after_capture(missing, configuration)


if __name__ == "__main__":
    unittest.main()
