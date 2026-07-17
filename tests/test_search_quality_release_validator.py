from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts import verify_search_quality_gate
from tests.search_quality import evaluate
from tests.search_quality import split as dataset_split
from tests.search_quality.release_gate_fixture import build_release_gate_fixture

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected object fixture at {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _referenced(report: dict[str, Any], key: str) -> Path:
    if key == "manifest":
        source = report["holdout_split"]["manifest_source"]
    elif key == "source":
        source = report["holdout_split"]["source_dataset_source"]
    elif key == "dataset":
        source = report["dataset"]["source"]
    else:
        source = report[key]["source"]
    return (REPOSITORY_ROOT / source).resolve()


class SearchQualityReleaseValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".release-gate-replay-test-",
            dir=REPOSITORY_ROOT,
        )
        self.root = Path(self.temporary.name)
        self.report_path = build_release_gate_fixture(
            self.root / "fixture",
            REPOSITORY_ROOT,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_formal_gate_replays_exactly(self):
        verify_search_quality_gate.verify_release_gate(
            self.report_path,
            REPOSITORY_ROOT,
        )

    def test_rejects_report_metric_or_check_tampering(self):
        report = _read(self.report_path)
        report["quality_gate"]["checks"]["precision_at_5_gain"]["actual"] = 999.0
        _write(self.report_path, report)

        with self.assertRaisesRegex(
            verify_search_quality_gate.VerificationError,
            "does not exactly match",
        ):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

    def test_rejects_stale_canonical_run_fingerprint_even_with_new_byte_hash(self):
        report = _read(self.report_path)
        after_path = _referenced(report, "after")
        after = _read(after_path)
        after["cases"][0]["latency_ms"] = 11.0
        _write(after_path, after)
        report["after"]["source_sha256"] = evaluate.source_file_sha256(after_path)
        _write(self.report_path, report)

        with self.assertRaisesRegex(
            verify_search_quality_gate.VerificationError,
            "canonical run fingerprint does not match replay",
        ):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

    def test_rejects_missing_or_calibration_run_split(self):
        original_report = _read(self.report_path)
        after_path = _referenced(original_report, "after")
        original_after = _read(after_path)
        for mutation, message in (
            ("missing", "after run split must be an object"),
            ("calibration", "after run split role does not match"),
        ):
            with self.subTest(mutation=mutation):
                report = copy.deepcopy(original_report)
                after = copy.deepcopy(original_after)
                if mutation == "missing":
                    after.pop("split")
                else:
                    after["split"]["role"] = "calibration"
                _write(after_path, after)
                report["after"]["source_sha256"] = evaluate.source_file_sha256(
                    after_path
                )
                _write(self.report_path, report)
                with self.assertRaisesRegex(
                    verify_search_quality_gate.VerificationError,
                    message,
                ):
                    verify_search_quality_gate.verify_release_gate(
                        self.report_path,
                        REPOSITORY_ROOT,
                    )

    def test_rejects_manifest_and_validation_dataset_tampering(self):
        original_report = _read(self.report_path)
        manifest_path = _referenced(original_report, "manifest")
        original_manifest = _read(manifest_path)
        dataset_path = _referenced(original_report, "dataset")
        original_dataset = _read(dataset_path)

        tampered_manifest = copy.deepcopy(original_manifest)
        tampered_manifest["strategy"]["seed"] += "-tampered"
        _write(manifest_path, tampered_manifest)
        with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

        _write(manifest_path, original_manifest)
        tampered_dataset = copy.deepcopy(original_dataset)
        tampered_dataset["items"][0]["query"]["text"] = "changed query"
        _write(dataset_path, tampered_dataset)
        with self.assertRaisesRegex(ValueError, "does not exactly reproduce"):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

    def test_rejects_source_drift_and_recomputed_manifest_assignment_tampering(self):
        report = _read(self.report_path)
        source_path = _referenced(report, "source")
        original_source = _read(source_path)
        manifest_path = _referenced(report, "manifest")
        original_manifest = _read(manifest_path)

        changed_source = copy.deepcopy(original_source)
        changed_source["items"][0]["query"]["text"] = "changed source query"
        _write(source_path, changed_source)
        with self.assertRaisesRegex(ValueError, "Source dataset does not match"):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

        _write(source_path, original_source)
        changed_manifest = copy.deepcopy(original_manifest)
        changed_manifest["strategy"]["seed"] += "-changed"
        changed_manifest["manifest_fingerprint"] = (
            dataset_split.split_manifest_fingerprint(changed_manifest)
        )
        _write(manifest_path, changed_manifest)
        report["holdout_split"]["manifest_fingerprint"] = changed_manifest[
            "manifest_fingerprint"
        ]
        _write(self.report_path, report)
        with self.assertRaisesRegex(ValueError, "assignments are not deterministic"):
            verify_search_quality_gate.verify_release_gate(
                self.report_path,
                REPOSITORY_ROOT,
            )

    def test_rejects_sources_outside_repository(self):
        report = _read(self.report_path)
        with tempfile.NamedTemporaryFile(suffix=".json") as outside:
            report["before"]["source"] = outside.name
            report["holdout_split"]["before_run_source"] = outside.name
            _write(self.report_path, report)
            with self.assertRaisesRegex(
                verify_search_quality_gate.VerificationError,
                "must stay inside the repository",
            ):
                verify_search_quality_gate.verify_release_gate(
                    self.report_path,
                    REPOSITORY_ROOT,
                )


if __name__ == "__main__":
    unittest.main()
