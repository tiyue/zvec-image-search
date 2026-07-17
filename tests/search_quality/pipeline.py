from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)
dataset_split = importlib.import_module(
    "tests.search_quality.split" if __package__ else "split"
)
calibrate = importlib.import_module(
    "tests.search_quality.calibrate" if __package__ else "calibrate"
)
quality_gate = importlib.import_module(
    "tests.search_quality.quality_gate" if __package__ else "quality_gate"
)

PIPELINE_SCHEMA_VERSION = 1
PIPELINE_KIND = "zvec-search-quality-release-pipeline"
STATE_KIND = "zvec-search-quality-release-pipeline-state"
EXPECTED_CASE_COUNT = 60
EXPECTED_CALIBRATION_COUNT = 36
EXPECTED_VALIDATION_COUNT = 24
DEFAULT_SEED = "search-quality-mvp-v1"
DEFAULT_VALIDATION_FRACTION = 0.40
GATE_ARTIFACT_NAMES = (
    "validation-dataset.json",
    "before-evaluation.json",
    "before-evaluation.md",
    "after-evaluation.json",
    "after-evaluation.md",
    "comparison.json",
    "comparison.md",
)


class PipelineError(ValueError):
    """A fail-closed release-pipeline validation error."""


@dataclass(frozen=True)
class PipelinePaths:
    dataset: Path
    work_dir: Path
    managed_split: bool
    manifest: Path
    calibration_dataset: Path
    validation_dataset: Path
    calibration_run: Path
    validation_before_run: Path
    validation_after_run: Path
    configuration: Path
    state: Path
    report_dir: Path

    @classmethod
    def resolve(
        cls,
        dataset: str | Path,
        work_dir: str | Path,
        *,
        manifest: str | Path | None = None,
        calibration_dataset: str | Path | None = None,
        validation_dataset: str | Path | None = None,
        calibration_run: str | Path | None = None,
        validation_before_run: str | Path | None = None,
        validation_after_run: str | Path | None = None,
    ) -> PipelinePaths:
        source = Path(dataset).expanduser().resolve(strict=False)
        destination = Path(work_dir).expanduser().resolve(strict=False)
        supplied_split = (manifest, calibration_dataset, validation_dataset)
        if any(value is not None for value in supplied_split) and not all(
            value is not None for value in supplied_split
        ):
            raise PipelineError(
                "--split-manifest, --calibration-dataset, and "
                "--validation-dataset must be supplied together"
            )
        managed_split = manifest is None
        manifest_path = (
            destination / "holdout-split.json"
            if manifest is None
            else Path(manifest).expanduser().resolve(strict=False)
        )
        calibration_path = (
            destination / "calibration-dataset.json"
            if calibration_dataset is None
            else Path(calibration_dataset).expanduser().resolve(strict=False)
        )
        validation_path = (
            destination / "validation-dataset.json"
            if validation_dataset is None
            else Path(validation_dataset).expanduser().resolve(strict=False)
        )
        calibration_run_path = (
            destination / "calibration-run.json"
            if calibration_run is None
            else Path(calibration_run).expanduser().resolve(strict=False)
        )
        validation_before_path = (
            destination / "validation-before-run.json"
            if validation_before_run is None
            else Path(validation_before_run).expanduser().resolve(strict=False)
        )
        validation_after_path = (
            destination / "validation-after-run.json"
            if validation_after_run is None
            else Path(validation_after_run).expanduser().resolve(strict=False)
        )
        return cls(
            dataset=source,
            work_dir=destination,
            managed_split=managed_split,
            manifest=manifest_path,
            calibration_dataset=calibration_path,
            validation_dataset=validation_path,
            calibration_run=calibration_run_path,
            validation_before_run=validation_before_path,
            validation_after_run=validation_after_path,
            configuration=destination / "search-quality.json",
            state=destination / "pipeline-state.json",
            report_dir=destination / "validation-report",
        )


@dataclass(frozen=True)
class SplitArtifacts:
    manifest: dict[str, Any]
    calibration_dataset: dict[str, Any]
    validation_dataset: dict[str, Any]


@dataclass(frozen=True)
class RunBinding:
    path: Path
    run: dict[str, Any]
    source_sha256: str
    fingerprint: str
    captured_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source_sha256": self.source_sha256,
            "source_sha256_algorithm": evaluate.SOURCE_FILE_SHA256_ALGORITHM,
            "fingerprint": self.fingerprint,
            "fingerprint_algorithm": evaluate.RUN_FINGERPRINT_ALGORITHM,
            "captured_at": self.captured_at.isoformat(),
        }


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{label} is not readable JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} JSON root must be an object: {path}")
    return value


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_json_atomic(path: Path, value: dict[str, Any], *, replace: bool) -> None:
    """Write one validated artifact without exposing a partially-written JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            temporary_name = destination.name
            destination.write(_json_bytes(value))
            destination.flush()
            os.fsync(destination.fileno())
        temporary = Path(temporary_name)
        if replace:
            os.replace(temporary, path)
        else:
            # os.rename fails if the destination already exists on Windows. The
            # pre-check keeps the error portable and preserves a frozen artifact.
            if path.exists():
                raise PipelineError(f"refusing to overwrite frozen artifact: {path}")
            os.rename(temporary, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PipelineError(f"{label} must include a timezone offset")
    return parsed


def _dataset_report(dataset: dict[str, Any], path: Path) -> dict[str, Any]:
    items = evaluate.validate_dataset(dataset)
    counts = Counter(str(item["annotation"]["status"]) for item in items)
    exact_count = len(items) == EXPECTED_CASE_COUNT
    fully_human = counts == {"human_verified": EXPECTED_CASE_COUNT}
    return {
        "path": str(path),
        "case_count": len(items),
        "expected_case_count": EXPECTED_CASE_COUNT,
        "annotation_statuses": dict(sorted(counts.items())),
        "human_verified": counts.get("human_verified", 0),
        "pending": counts.get("pending", 0),
        "synthetic": counts.get("synthetic_fixture", 0),
        "dataset_fingerprint": evaluate.dataset_fingerprint(dataset),
        "dataset_fingerprint_algorithm": evaluate.DATASET_FINGERPRINT_ALGORITHM,
        "query_corpus_fingerprint": evaluate.query_corpus_fingerprint(dataset),
        "query_corpus_fingerprint_algorithm": (
            evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM
        ),
        "release_ready": exact_count and fully_human,
    }


def _require_release_dataset(report: dict[str, Any]) -> None:
    if report["case_count"] != EXPECTED_CASE_COUNT:
        raise PipelineError(
            "formal search-quality release requires exactly "
            f"{EXPECTED_CASE_COUNT} cases; found {report['case_count']}"
        )
    if report["synthetic"]:
        raise PipelineError("synthetic_fixture labels are forbidden in a formal run")
    if report["human_verified"] != EXPECTED_CASE_COUNT or report["pending"]:
        raise PipelineError(
            "formal search-quality release requires 60/60 human_verified cases; "
            f"found human_verified={report['human_verified']}, "
            f"pending={report['pending']}"
        )


def _planned_split(
    dataset: dict[str, Any],
    *,
    seed: str,
    validation_fraction: float,
) -> SplitArtifacts:
    manifest, calibration_dataset, validation_dataset = (
        dataset_split.build_split_artifacts(
            dataset,
            seed=seed,
            validation_fraction=validation_fraction,
            allow_synthetic=False,
        )
    )
    calibration_count = manifest["splits"]["calibration"]["case_count"]
    validation_count = manifest["splits"]["validation"]["case_count"]
    if (
        calibration_count != EXPECTED_CALIBRATION_COUNT
        or validation_count != EXPECTED_VALIDATION_COUNT
    ):
        raise PipelineError(
            "the formal 60-case corpus must produce a 36/24 holdout; "
            f"got {calibration_count}/{validation_count}"
        )
    return SplitArtifacts(manifest, calibration_dataset, validation_dataset)


def _split_existence(paths: PipelinePaths) -> tuple[bool, bool, bool]:
    return (
        paths.manifest.is_file(),
        paths.calibration_dataset.is_file(),
        paths.validation_dataset.is_file(),
    )


def _validate_formal_split_shape(manifest: dict[str, Any]) -> None:
    source_count = manifest.get("dataset", {}).get("case_count")
    calibration_count = (
        manifest.get("splits", {}).get("calibration", {}).get("case_count")
    )
    validation_count = (
        manifest.get("splits", {}).get("validation", {}).get("case_count")
    )
    if source_count != EXPECTED_CASE_COUNT:
        raise PipelineError(
            "frozen split source must contain exactly "
            f"{EXPECTED_CASE_COUNT} cases; found {source_count}"
        )
    if (
        calibration_count != EXPECTED_CALIBRATION_COUNT
        or validation_count != EXPECTED_VALIDATION_COUNT
    ):
        raise PipelineError(
            "frozen split must contain exactly 36 calibration and 24 validation "
            f"cases; found {calibration_count}/{validation_count}"
        )
    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("validation_fraction") != (
        DEFAULT_VALIDATION_FRACTION
    ):
        raise PipelineError("formal frozen split must use a 40% validation holdout")
    annotation_policy = manifest.get("annotation_policy")
    status_counts = (
        annotation_policy.get("status_counts")
        if isinstance(annotation_policy, dict)
        else None
    )
    if (
        not isinstance(annotation_policy, dict)
        or annotation_policy.get("allow_synthetic") is not False
        or not isinstance(status_counts, dict)
        or status_counts.get("human_verified") != EXPECTED_CASE_COUNT
        or status_counts.get("pending", 0) != 0
        or status_counts.get("synthetic_fixture", 0) != 0
        or any(
            name not in {"human_verified", "pending", "synthetic_fixture"}
            for name in status_counts
        )
    ):
        raise PipelineError(
            "formal frozen split must bind exactly 60 human_verified annotations"
        )


def _read_and_validate_split(paths: PipelinePaths) -> SplitArtifacts:
    existence = _split_existence(paths)
    if not all(existence):
        qualifier = "adopted" if not paths.managed_split else "managed"
        missing = [
            str(path)
            for path, exists in zip(
                (
                    paths.manifest,
                    paths.calibration_dataset,
                    paths.validation_dataset,
                ),
                existence,
                strict=True,
            )
            if not exists
        ]
        raise PipelineError(
            f"{qualifier} split is incomplete; missing: " + ", ".join(missing)
        )
    actual = SplitArtifacts(
        _read_json_object(paths.manifest, "split manifest"),
        _read_json_object(paths.calibration_dataset, "calibration dataset"),
        _read_json_object(paths.validation_dataset, "validation dataset"),
    )
    try:
        dataset_split.validate_split_manifest(actual.manifest)
    except ValueError as exc:
        raise PipelineError(f"invalid frozen split manifest: {exc}") from exc
    _validate_formal_split_shape(actual.manifest)
    return actual


def _load_frozen_split(
    paths: PipelinePaths,
    planned: SplitArtifacts,
) -> SplitArtifacts | None:
    existence = _split_existence(paths)
    if not any(existence):
        return None
    actual = _read_and_validate_split(paths)
    if actual.manifest != planned.manifest:
        raise PipelineError(
            "frozen split manifest does not match the current 60-case dataset, "
            "seed, or validation fraction"
        )
    if actual.calibration_dataset != planned.calibration_dataset:
        raise PipelineError("frozen calibration dataset does not match its manifest")
    if actual.validation_dataset != planned.validation_dataset:
        raise PipelineError("frozen validation dataset does not match its manifest")
    return actual


def _load_adopted_split(
    paths: PipelinePaths,
    source_dataset: dict[str, Any],
) -> SplitArtifacts:
    actual = _read_and_validate_split(paths)
    try:
        expected_calibration = dataset_split.subset_dataset_from_manifest(
            source_dataset,
            actual.manifest,
            "calibration",
            allow_synthetic=False,
        )
        expected_validation = dataset_split.subset_dataset_from_manifest(
            source_dataset,
            actual.manifest,
            "validation",
            allow_synthetic=False,
        )
    except ValueError as exc:
        raise PipelineError(
            f"adopted split does not match the current source dataset: {exc}"
        ) from exc
    if actual.calibration_dataset != expected_calibration:
        raise PipelineError(
            "adopted calibration dataset does not exactly match its frozen manifest"
        )
    if actual.validation_dataset != expected_validation:
        raise PipelineError(
            "adopted validation dataset does not exactly match its frozen manifest"
        )
    return actual


def prepare_split(paths: PipelinePaths, planned: SplitArtifacts) -> None:
    if not paths.managed_split:
        raise PipelineError("an adopted split must be validated, not regenerated")
    existing = _load_frozen_split(paths, planned)
    if existing is not None:
        return
    if paths.work_dir.exists() and not paths.work_dir.is_dir():
        raise PipelineError(f"work directory is not a directory: {paths.work_dir}")
    _write_json_atomic(paths.manifest, planned.manifest, replace=False)
    _write_json_atomic(
        paths.calibration_dataset,
        planned.calibration_dataset,
        replace=False,
    )
    _write_json_atomic(
        paths.validation_dataset,
        planned.validation_dataset,
        replace=False,
    )


def _require_run_split(
    run: dict[str, Any],
    manifest: dict[str, Any],
    role: str,
) -> None:
    binding = run.get("split")
    if not isinstance(binding, dict):
        raise PipelineError(f"{role} run is not bound to its frozen split dataset")
    expected = {
        "kind": dataset_split.SPLIT_KIND,
        "role": role,
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": dataset_split.SPLIT_FINGERPRINT_ALGORITHM,
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise PipelineError(f"{role} run split {field} does not match the manifest")


def _load_formal_run(
    path: Path,
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    role: str,
) -> RunBinding:
    run = _read_json_object(path, f"{role} capture")
    _require_run_split(run, manifest, role)
    try:
        if role == "calibration":
            dataset_split.prepare_calibration_inputs(
                dataset,
                run,
                manifest,
                allow_synthetic=False,
            )
        elif role == "validation":
            dataset_split.prepare_validation_inputs(
                dataset,
                run,
                manifest,
                allow_synthetic=False,
            )
        else:
            raise PipelineError(f"unsupported pipeline role: {role}")
    except ValueError as exc:
        raise PipelineError(f"invalid formal {role} capture: {exc}") from exc
    capture = run.get("capture")
    if not isinstance(capture, dict):
        raise PipelineError(f"{role} capture metadata is missing")
    captured_at = _parse_timestamp(capture.get("generated_at"), f"{role} captured_at")
    return RunBinding(
        path=path,
        run=run,
        source_sha256=evaluate.source_file_sha256(path),
        fingerprint=evaluate.run_fingerprint(run),
        captured_at=captured_at,
    )


def _file_binding(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "source_sha256": evaluate.source_file_sha256(path),
        "source_sha256_algorithm": evaluate.SOURCE_FILE_SHA256_ALGORITHM,
    }


def _gate_artifact_bindings(report_dir: Path) -> dict[str, dict[str, Any]]:
    return {name: _file_binding(report_dir / name) for name in GATE_ARTIFACT_NAMES}


def _split_binding(paths: PipelinePaths, split: SplitArtifacts) -> dict[str, Any]:
    return {
        "manifest_fingerprint": split.manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": dataset_split.SPLIT_FINGERPRINT_ALGORITHM,
        "manifest": _file_binding(paths.manifest),
        "calibration_dataset": _file_binding(paths.calibration_dataset),
        "validation_dataset": _file_binding(paths.validation_dataset),
        "calibration_case_count": EXPECTED_CALIBRATION_COUNT,
        "validation_case_count": EXPECTED_VALIDATION_COUNT,
    }


def _configuration_binding(path: Path, configuration: dict[str, Any]) -> dict[str, Any]:
    generated_at = _parse_timestamp(
        configuration.get("generated_at"), "configuration generated_at"
    )
    return {
        **_file_binding(path),
        "generated_at": generated_at.isoformat(),
    }


def _captured_confidence_v2_options(run: dict[str, Any]) -> dict[str, float]:
    """Require calibration evidence from the production unified ranking path."""

    expected: dict[str, Any] | None = None
    for case in run["cases"]:
        case_id = str(case["id"])
        if case.get("ranking_mode") != calibrate.FUSION_V2:
            raise PipelineError(
                f"calibration case {case_id} did not use confidence_v2 ranking"
            )
        diagnostics = case.get("search_quality")
        fusion = diagnostics.get("fusion") if isinstance(diagnostics, dict) else None
        if not isinstance(fusion, dict) or fusion.get("mode") != calibrate.FUSION_V2:
            raise PipelineError(
                f"calibration case {case_id} has no confidence_v2 runtime proof"
            )
        if expected is None:
            expected = copy.deepcopy(fusion)
        elif fusion != expected:
            raise PipelineError(
                "calibration capture used inconsistent fusion settings across cases"
            )
    if expected is None:
        raise PipelineError("calibration capture contains no cases")
    required_fields = set(calibrate.FUSION_V2_DEFAULTS)
    if set(expected) != {"mode", *required_fields}:
        raise PipelineError(
            "calibration confidence_v2 diagnostics must contain every fusion option"
        )
    options: dict[str, float] = {}
    for name in sorted(required_fields):
        value = expected[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PipelineError(f"calibration fusion option {name} must be numeric")
        options[name] = float(value)
    return options


def _new_calibrated_state(
    paths: PipelinePaths,
    dataset_report: dict[str, Any],
    split: SplitArtifacts,
    calibration_run: RunBinding,
    validation_before: RunBinding,
    configuration: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "stage": "calibrated",
        "source_dataset": {
            "path": str(paths.dataset),
            "fingerprint": dataset_report["dataset_fingerprint"],
            "fingerprint_algorithm": evaluate.DATASET_FINGERPRINT_ALGORITHM,
            "query_corpus_fingerprint": dataset_report["query_corpus_fingerprint"],
            "query_corpus_fingerprint_algorithm": (
                evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM
            ),
            "case_count": EXPECTED_CASE_COUNT,
            "source_sha256": evaluate.source_file_sha256(paths.dataset),
            "source_sha256_algorithm": evaluate.SOURCE_FILE_SHA256_ALGORITHM,
        },
        "split": _split_binding(paths, split),
        "calibration": {
            "run": calibration_run.to_dict(),
            "configuration": _configuration_binding(paths.configuration, configuration),
        },
        "validation": {"before_run": validation_before.to_dict()},
    }


def _require_exact(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise PipelineError(f"pipeline state {label} no longer matches its artifact")


def _validate_calibrated_state(
    state: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    if state.get("schema_version") != PIPELINE_SCHEMA_VERSION:
        raise PipelineError("pipeline state schema_version is invalid")
    if state.get("kind") != STATE_KIND:
        raise PipelineError("pipeline state kind is invalid")
    if state.get("stage") not in {"calibrated", "passed", "failed"}:
        raise PipelineError("pipeline state stage is invalid")
    for section in ("source_dataset", "split", "calibration"):
        _require_exact(section, state.get(section), expected[section])
    validation = state.get("validation")
    expected_validation = expected["validation"]
    if not isinstance(validation, dict):
        raise PipelineError("pipeline state validation section is missing")
    _require_exact(
        "validation.before_run",
        validation.get("before_run"),
        expected_validation["before_run"],
    )


def _configuration_matches_after_capture(
    run: dict[str, Any], configuration: dict[str, Any]
) -> None:
    """Prove that the post-change backend loaded the generated configuration."""

    expected_fusion = configuration.get("fusion")
    expected_collection_calibration = configuration.get("collection_calibration")
    for case in run["cases"]:
        case_id = str(case["id"])
        mode = str(case.get("mode", ""))
        if mode not in evaluate.MODES:
            raise PipelineError(f"after capture case {case_id} has no valid mode")
        diagnostics = case.get("search_quality")
        if (
            not isinstance(diagnostics, dict)
            or diagnostics.get("configured") is not True
        ):
            raise PipelineError(
                f"after capture case {case_id} does not prove search-quality config "
                "was loaded by the persistent backend"
            )
        if diagnostics.get("fusion") != expected_fusion:
            raise PipelineError(
                f"after capture case {case_id} used a different fusion configuration"
            )
        if expected_collection_calibration is not None:
            if not isinstance(expected_collection_calibration, dict):
                raise PipelineError("configuration collection_calibration is invalid")
            modes = expected_collection_calibration.get("modes")
            mode_config = modes.get(mode) if isinstance(modes, dict) else None
            libraries = (
                mode_config.get("libraries") if isinstance(mode_config, dict) else None
            )
            if not isinstance(libraries, dict):
                raise PipelineError(
                    f"configuration collection_calibration has no {mode} libraries"
                )
            library_ids = case.get("library_ids")
            if not isinstance(library_ids, list) or any(
                not isinstance(library_id, str) or not library_id.strip()
                for library_id in library_ids
            ):
                raise PipelineError(
                    f"after capture case {case_id} has no Collection ids for "
                    "calibration proof"
                )
            expected_offsets = {}
            unknown_library_ids = []
            for library_id in library_ids:
                library = libraries.get(library_id)
                if not isinstance(library, dict):
                    expected_offsets[library_id] = 0.0
                    unknown_library_ids.append(library_id)
                    continue
                offset = library.get("offset")
                if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                    raise PipelineError(
                        "configuration collection_calibration offset is invalid"
                    )
                expected_offsets[library_id] = float(offset)
            runtime_collection_calibration = diagnostics.get("collection_calibration")
            expected_runtime = {
                "configured": True,
                "mode": expected_collection_calibration.get("mode"),
                "fallback": expected_collection_calibration.get("fallback"),
                "offsets": expected_offsets,
                "unknown_library_ids": unknown_library_ids,
            }
            if runtime_collection_calibration != expected_runtime:
                raise PipelineError(
                    f"after capture case {case_id} used a different Collection "
                    "calibration configuration"
                )
        expected_threshold = configuration.get(mode)
        if not isinstance(expected_threshold, dict):
            raise PipelineError(f"configuration has no {mode} threshold section")
        if isinstance(diagnostics.get("thresholds"), dict):
            thresholds = diagnostics["thresholds"]
            checks = {
                "thresholds.minimum": expected_threshold.get("minimum_confidence"),
                "score_gap": expected_threshold.get("score_gap"),
                "max_confidence_drop": expected_threshold.get("max_confidence_drop"),
            }
            actual = {
                "thresholds.minimum": thresholds.get("minimum"),
                "score_gap": diagnostics.get("score_gap"),
                "max_confidence_drop": diagnostics.get("max_confidence_drop"),
            }
        else:
            runtime_threshold = diagnostics.get(mode)
            if not isinstance(runtime_threshold, dict):
                raise PipelineError(
                    f"after capture case {case_id} has no {mode} runtime thresholds"
                )
            checks = {
                field: expected_threshold.get(field)
                for field in (
                    "minimum_score",
                    "minimum_confidence",
                    "score_gap",
                    "max_confidence_drop",
                )
            }
            actual = {field: runtime_threshold.get(field) for field in checks}
        for field, expected_value in checks.items():
            if actual.get(field) != expected_value:
                raise PipelineError(
                    f"after capture case {case_id} runtime {field} does not match "
                    "the calibrated configuration"
                )


def _capture_command(paths: PipelinePaths, role: str, label: str) -> str:
    dataset = (
        paths.calibration_dataset if role == "calibration" else paths.validation_dataset
    )
    output = {
        "calibration": paths.calibration_run,
        "validation-before": paths.validation_before_run,
        "validation-after": paths.validation_after_run,
    }[label]
    return (
        "python tests/search_quality/capture.py "
        f'--dataset "{dataset}" --output "{output}" '
        "--base-url <persistent-backend-url> --token-env ZVEC_BACKEND_TOKEN "
        "--query-staging-root <host-query-staging-root> "
        "--backend-query-root <backend-query-root> "
        "--query-source-root <query-source-root-1> "
        "--query-source-root <query-source-root-2-if-needed> "
        "--top-k 10 --candidate-k 50 "
        f"--name {label}"
    )


def _status_actions(stage: str, paths: PipelinePaths) -> list[str]:
    if stage == "review_required":
        return [
            "Complete explicit human review until status reports 60/60 human_verified.",
            f'python tests/search_quality/review.py --dataset "{paths.dataset}" status',
        ]
    if stage == "split_required":
        return ["Run this pipeline with the prepare command to freeze the 36/24 split."]
    if stage == "captures_required":
        actions = []
        if not paths.validation_before_run.is_file():
            actions.append(_capture_command(paths, "validation", "validation-before"))
        if not paths.calibration_run.is_file():
            actions.append(_capture_command(paths, "calibration", "calibration"))
        return actions
    if stage == "ready_to_calibrate":
        return [
            "Run this pipeline with the run command to calibrate and bind artifacts."
        ]
    if stage == "after_capture_required":
        return [
            f'Deploy "{paths.configuration}" to the persistent backend workspace '
            "and restart/reload that backend.",
            _capture_command(paths, "validation", "validation-after"),
        ]
    if stage == "ready_to_gate":
        return [
            "Run this pipeline with the run command to execute the validation gate."
        ]
    if stage == "failed":
        return [
            "Keep this failed attempt immutable; optimize the implementation and use "
            "a fresh work directory for the next before/calibration/after capture set."
        ]
    return []


def inspect_pipeline(
    paths: PipelinePaths,
    *,
    seed: str = DEFAULT_SEED,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
) -> dict[str, Any]:
    dataset = _read_json_object(paths.dataset, "source dataset")
    dataset_report = _dataset_report(dataset, paths.dataset)
    base: dict[str, Any] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "kind": PIPELINE_KIND,
        "work_dir": str(paths.work_dir),
        "dataset": dataset_report,
    }
    if not dataset_report["release_ready"]:
        stage = "review_required"
        base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
        return base

    _require_release_dataset(dataset_report)
    if paths.managed_split:
        planned = _planned_split(
            dataset,
            seed=seed,
            validation_fraction=validation_fraction,
        )
        split = _load_frozen_split(paths, planned)
        if split is None:
            stage = "split_required"
            base["planned_split"] = {
                "manifest_fingerprint": planned.manifest["manifest_fingerprint"],
                "calibration_case_count": EXPECTED_CALIBRATION_COUNT,
                "validation_case_count": EXPECTED_VALIDATION_COUNT,
                "seed": seed,
                "validation_fraction": validation_fraction,
            }
            base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
            return base
    else:
        split = _load_adopted_split(paths, dataset)

    base["split"] = _split_binding(paths, split)
    base["split"]["ownership"] = "managed" if paths.managed_split else "adopted"
    base["split"]["seed"] = split.manifest["strategy"]["seed"]
    if paths.validation_after_run.exists() and (
        not paths.configuration.is_file() or not paths.state.is_file()
    ):
        raise PipelineError(
            "validation-after capture predates a bound calibration state; use a "
            "fresh work directory and capture it only after deploying the config"
        )

    calibration_run = (
        _load_formal_run(
            paths.calibration_run,
            split.calibration_dataset,
            split.manifest,
            "calibration",
        )
        if paths.calibration_run.is_file()
        else None
    )
    validation_before = (
        _load_formal_run(
            paths.validation_before_run,
            split.validation_dataset,
            split.manifest,
            "validation",
        )
        if paths.validation_before_run.is_file()
        else None
    )
    missing_initial = []
    if validation_before is None:
        missing_initial.append(paths.validation_before_run)
    if calibration_run is None:
        missing_initial.append(paths.calibration_run)
    if missing_initial:
        stage = "captures_required"
        base["missing_artifacts"] = [str(path) for path in missing_initial]
        available_captures = {}
        if calibration_run is not None:
            available_captures["calibration"] = calibration_run.to_dict()
        if validation_before is not None:
            available_captures["validation_before"] = validation_before.to_dict()
        if available_captures:
            base["captures"] = available_captures
        base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
        return base

    assert calibration_run is not None
    assert validation_before is not None
    _captured_confidence_v2_options(calibration_run.run)
    base["captures"] = {
        "calibration": calibration_run.to_dict(),
        "validation_before": validation_before.to_dict(),
    }

    config_exists = paths.configuration.is_file()
    state_exists = paths.state.is_file()
    if config_exists != state_exists:
        raise PipelineError(
            "configuration and pipeline-state must either both exist or both be absent"
        )
    if not config_exists:
        stage = "ready_to_calibrate"
        base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
        return base

    configuration = _read_json_object(paths.configuration, "calibrated configuration")
    state = _read_json_object(paths.state, "pipeline state")
    expected_state = _new_calibrated_state(
        paths,
        dataset_report,
        split,
        calibration_run,
        validation_before,
        configuration,
    )
    _validate_calibrated_state(state, expected_state)
    calibrated_at = _parse_timestamp(
        configuration.get("generated_at"), "configuration generated_at"
    )
    base["configuration"] = expected_state["calibration"]["configuration"]

    if not paths.validation_after_run.is_file():
        stage = "after_capture_required"
        base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
        return base

    validation_after = _load_formal_run(
        paths.validation_after_run,
        split.validation_dataset,
        split.manifest,
        "validation",
    )
    if validation_after.captured_at <= calibrated_at:
        raise PipelineError(
            "validation-after capture must be created after calibration; recapture "
            "from the backend after deploying the generated configuration"
        )
    if (
        validation_after.path == validation_before.path
        or validation_after.source_sha256 == validation_before.source_sha256
        or validation_after.fingerprint == validation_before.fingerprint
    ):
        raise PipelineError(
            "validation before/after captures must be distinct artifacts"
        )
    _configuration_matches_after_capture(validation_after.run, configuration)
    base["captures"]["validation_after"] = validation_after.to_dict()

    state_stage = state["stage"]
    report_path = paths.report_dir / "comparison.json"
    if state_stage == "calibrated":
        if paths.report_dir.exists():
            raise PipelineError(
                "unbound validation report directory exists; use a fresh work "
                "directory instead of trusting partial gate output"
            )
        stage = "ready_to_gate"
        base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
        return base

    validation_state = state.get("validation")
    if not isinstance(validation_state, dict):
        raise PipelineError("completed pipeline state has no validation section")
    _require_exact(
        "validation.after_run",
        validation_state.get("after_run"),
        validation_after.to_dict(),
    )
    gate_state = state.get("gate")
    if not isinstance(gate_state, dict):
        raise PipelineError("completed pipeline state has no gate binding")
    _require_exact(
        "gate.artifacts",
        gate_state.get("artifacts"),
        _gate_artifact_bindings(paths.report_dir),
    )
    comparison = _read_json_object(report_path, "quality-gate comparison")
    gate = comparison.get("quality_gate")
    if not isinstance(gate, dict) or not isinstance(gate.get("passed"), bool):
        raise PipelineError("quality-gate comparison has no boolean pass result")
    expected_stage = "passed" if gate["passed"] else "failed"
    _require_exact("gate.passed", gate_state.get("passed"), gate["passed"])
    _require_exact("stage", state_stage, expected_stage)
    stage = expected_stage
    base["gate"] = {
        "passed": gate["passed"],
        "status": gate.get("status"),
        "report": str(report_path),
    }
    base.update({"stage": stage, "next_actions": _status_actions(stage, paths)})
    return base


def _calibrate_stage(
    paths: PipelinePaths,
    dataset_report: dict[str, Any],
    split: SplitArtifacts,
    calibration_run: RunBinding,
    validation_before: RunBinding,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    fusion_options = _captured_confidence_v2_options(calibration_run.run)
    configuration = calibrate.calibrate_files(
        paths.calibration_dataset,
        paths.calibration_run,
        split_manifest_path=paths.manifest,
        allow_synthetic=False,
        fusion_mode=calibrate.FUSION_V2,
        fusion_options=fusion_options,
        fusion_source="formal_release_pipeline_captured_runtime",
    )
    if dry_run:
        return configuration
    _write_json_atomic(paths.configuration, configuration, replace=False)
    state = _new_calibrated_state(
        paths,
        dataset_report,
        split,
        calibration_run,
        validation_before,
        configuration,
    )
    _write_json_atomic(paths.state, state, replace=False)
    return configuration


def _gate_stage(paths: PipelinePaths, *, dry_run: bool) -> bool:
    if dry_run:
        source_dataset = _read_json_object(paths.dataset, "source dataset")
        manifest = _read_json_object(paths.manifest, "split manifest")
        before = _read_json_object(paths.validation_before_run, "validation before")
        after_run = _read_json_object(paths.validation_after_run, "validation after")
        dataset, before = dataset_split.prepare_validation_inputs(
            source_dataset, before, manifest, allow_synthetic=False
        )
        after_dataset, after_run = dataset_split.prepare_validation_inputs(
            source_dataset,
            after_run,
            manifest,
            allow_synthetic=False,
        )
        if evaluate.dataset_fingerprint(dataset) != evaluate.dataset_fingerprint(
            after_dataset
        ):
            raise PipelineError("validation before/after datasets do not match")
        before_report = evaluate.evaluate_dataset(dataset, before)
        after_report = evaluate.evaluate_dataset(dataset, after_run)
        return bool(
            quality_gate.assess_quality_gate(
                before_report,
                after_report,
                enforce_collection_fairness=True,
            )["passed"]
        )

    comparison = quality_gate.run_quality_gate(
        paths.dataset,
        paths.validation_before_run,
        paths.validation_after_run,
        paths.report_dir,
        split_manifest_path=paths.manifest,
        allow_test_fixtures=False,
    )
    passed = bool(comparison["quality_gate"]["passed"])
    state = _read_json_object(paths.state, "pipeline state")
    state["stage"] = "passed" if passed else "failed"
    validation = state.get("validation")
    if not isinstance(validation, dict):
        raise PipelineError("pipeline state validation section is missing")
    split = _read_json_object(paths.manifest, "split manifest")
    validation_dataset = _read_json_object(
        paths.validation_dataset, "validation dataset"
    )
    after_binding = _load_formal_run(
        paths.validation_after_run,
        validation_dataset,
        split,
        "validation",
    )
    validation["after_run"] = after_binding.to_dict()
    state["gate"] = {
        "passed": passed,
        "artifacts": _gate_artifact_bindings(paths.report_dir),
    }
    _write_json_atomic(paths.state, state, replace=True)
    return passed


def run_pipeline(
    paths: PipelinePaths,
    *,
    seed: str = DEFAULT_SEED,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    status = inspect_pipeline(
        paths,
        seed=seed,
        validation_fraction=validation_fraction,
    )
    stage = status["stage"]
    if stage == "review_required":
        return status, 2
    dataset = _read_json_object(paths.dataset, "source dataset")
    dataset_report = _dataset_report(dataset, paths.dataset)
    _require_release_dataset(dataset_report)
    planned = (
        _planned_split(
            dataset,
            seed=seed,
            validation_fraction=validation_fraction,
        )
        if paths.managed_split
        else None
    )
    if stage == "split_required":
        if planned is None:
            raise PipelineError("an adopted split cannot enter split_required")
        if dry_run:
            return status, 0
        prepare_split(paths, planned)
        status = inspect_pipeline(
            paths,
            seed=seed,
            validation_fraction=validation_fraction,
        )
        stage = status["stage"]
    if stage == "captures_required":
        return status, 2
    if paths.managed_split:
        if planned is None:
            raise PipelineError("managed split plan is unexpectedly missing")
        split = _load_frozen_split(paths, planned)
        if split is None:
            raise PipelineError("split unexpectedly disappeared during pipeline run")
    else:
        split = _load_adopted_split(paths, dataset)
    if stage == "ready_to_calibrate":
        calibration_run = _load_formal_run(
            paths.calibration_run,
            split.calibration_dataset,
            split.manifest,
            "calibration",
        )
        validation_before = _load_formal_run(
            paths.validation_before_run,
            split.validation_dataset,
            split.manifest,
            "validation",
        )
        configuration = _calibrate_stage(
            paths,
            dataset_report,
            split,
            calibration_run,
            validation_before,
            dry_run=dry_run,
        )
        if dry_run:
            status = copy.deepcopy(status)
            status["dry_run"] = {
                "would_generate": [str(paths.configuration), str(paths.state)],
                "configuration_preview": {
                    "schema_version": configuration["schema_version"],
                    "kind": configuration["kind"],
                    "text": configuration["text"],
                    "image": configuration["image"],
                    "combined": configuration["combined"],
                    "fusion": configuration["fusion"],
                    "holdout_split": configuration.get("holdout_split"),
                },
            }
            return status, 0
        status = inspect_pipeline(
            paths,
            seed=seed,
            validation_fraction=validation_fraction,
        )
        return status, 2
    if stage == "after_capture_required":
        return status, 2
    if stage == "ready_to_gate":
        passed = _gate_stage(paths, dry_run=dry_run)
        if dry_run:
            status = copy.deepcopy(status)
            status["dry_run"] = {
                "predicted_gate_pass": passed,
                "would_generate": [str(paths.report_dir)],
            }
            return status, 0
        status = inspect_pipeline(
            paths,
            seed=seed,
            validation_fraction=validation_fraction,
        )
        return status, 0 if passed else 1
    if stage == "passed":
        return status, 0
    if stage == "failed":
        return status, 1
    raise PipelineError(f"unsupported pipeline stage: {stage}")


def _print_status(status: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    dataset = status["dataset"]
    print(f"Search-quality pipeline: {str(status['stage']).upper()}")
    print(
        "Dataset: "
        f"{dataset['human_verified']}/{dataset['expected_case_count']} human_verified, "
        f"pending={dataset['pending']}, synthetic={dataset['synthetic']}"
    )
    print(f"Dataset fingerprint: {dataset['dataset_fingerprint']}")
    split = status.get("split") or status.get("planned_split")
    if isinstance(split, dict):
        print(f"Split manifest: {split['manifest_fingerprint']}")
        print(
            "Holdout: "
            f"{split['calibration_case_count']} calibration / "
            f"{split['validation_case_count']} validation"
        )
    gate = status.get("gate")
    if isinstance(gate, dict):
        print(f"Validation gate: {str(gate['status']).upper()}")
        print(f"Report: {gate['report']}")
    actions = status.get("next_actions", [])
    if actions:
        print("Next actions:")
        for action in actions:
            print(f"- {action}")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument(
        "--split-manifest",
        help="adopt an existing frozen split manifest instead of generating one",
    )
    parser.add_argument(
        "--calibration-dataset",
        help="existing calibration subset; requires --split-manifest",
    )
    parser.add_argument(
        "--validation-dataset",
        help="existing validation subset; requires --split-manifest",
    )
    parser.add_argument(
        "--calibration-run",
        help="existing formal confidence_v2 calibration capture",
    )
    parser.add_argument(
        "--validation-before-run",
        help="existing formal validation baseline capture",
    )
    parser.add_argument(
        "--validation-after-run",
        help="existing formal post-configuration validation capture",
    )
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=DEFAULT_VALIDATION_FRACTION,
    )
    parser.add_argument("--json", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed orchestration for the formal 60-case search-quality release"
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="read-only release readiness audit")
    _add_common_arguments(status)
    prepare = commands.add_parser(
        "prepare", help="freeze or validate/adopt the deterministic 36/24 split"
    )
    _add_common_arguments(prepare)
    run = commands.add_parser(
        "run",
        help="resume calibration and validation-gate stages from frozen artifacts",
    )
    _add_common_arguments(run)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="validate every available artifact without writing pipeline outputs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths = PipelinePaths.resolve(
            args.dataset,
            args.work_dir,
            manifest=args.split_manifest,
            calibration_dataset=args.calibration_dataset,
            validation_dataset=args.validation_dataset,
            calibration_run=args.calibration_run,
            validation_before_run=args.validation_before_run,
            validation_after_run=args.validation_after_run,
        )
        if args.command == "status":
            status = inspect_pipeline(
                paths,
                seed=args.seed,
                validation_fraction=args.validation_fraction,
            )
            exit_code = 0
        elif args.command == "prepare":
            dataset = _read_json_object(paths.dataset, "source dataset")
            report = _dataset_report(dataset, paths.dataset)
            _require_release_dataset(report)
            if paths.managed_split:
                planned = _planned_split(
                    dataset,
                    seed=args.seed,
                    validation_fraction=args.validation_fraction,
                )
                prepare_split(paths, planned)
            else:
                _load_adopted_split(paths, dataset)
            status = inspect_pipeline(
                paths,
                seed=args.seed,
                validation_fraction=args.validation_fraction,
            )
            exit_code = 0
        elif args.command == "run":
            status, exit_code = run_pipeline(
                paths,
                seed=args.seed,
                validation_fraction=args.validation_fraction,
                dry_run=args.dry_run,
            )
        else:
            raise PipelineError(f"unsupported command: {args.command}")
    except (OSError, PipelineError, ValueError) as exc:
        print(f"search-quality pipeline failed closed: {exc}", file=sys.stderr)
        return 2
    _print_status(status, as_json=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
