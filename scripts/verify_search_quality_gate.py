from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


class VerificationError(ValueError):
    """Raised when a claimed release gate cannot be independently reproduced."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise VerificationError(f"JSON object contains duplicate key {key!r}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise VerificationError(f"JSON contains non-finite number {value}")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = path.read_text(encoding="utf-8-sig")
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(
            f"Could not read {label} JSON at {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} JSON root must be an object: {path}")
    return value


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} must be a non-empty string")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(root)))
        return os.path.normcase(common) == os.path.normcase(str(root))
    except ValueError:
        return False


def _resolve_reference(
    source: Any,
    *,
    label: str,
    repository_root: Path,
    report_directory: Path,
) -> Path:
    source_text = _required_text(source, f"{label} source")
    source_path = Path(source_text)
    candidates = (
        [source_path]
        if source_path.is_absolute()
        else [repository_root / source_path, report_directory / source_path]
    )
    matches: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved not in matches:
            matches.append(resolved)
    if not matches:
        raise VerificationError(f"{label} source file was not found: {source_text}")
    if len(matches) != 1:
        rendered = ", ".join(str(path) for path in matches)
        raise VerificationError(f"{label} source path is ambiguous: {rendered}")
    resolved = matches[0]
    if not _is_within(resolved, repository_root):
        raise VerificationError(
            f"{label} source must stay inside the repository: {resolved}"
        )
    return resolved


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return first == second


def _assert_same_reference(first: Path, second: Path, label: str) -> None:
    if not _same_file(first, second):
        raise VerificationError(f"{label} source references do not resolve to one file")


def _assert_validation_split(
    value: Any,
    *,
    label: str,
    manifest_fingerprint: str,
    split_kind: str,
    split_fingerprint_algorithm: str,
) -> None:
    split = _required_object(value, f"{label} split")
    expected = {
        "kind": split_kind,
        "role": "validation",
        "manifest_fingerprint": manifest_fingerprint,
        "manifest_fingerprint_algorithm": split_fingerprint_algorithm,
    }
    for key, expected_value in expected.items():
        if split.get(key) != expected_value:
            raise VerificationError(
                f"{label} split {key} does not match the validation manifest"
            )


def _validate_generated_at(value: Any) -> None:
    text = _required_text(value, "gate generated_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError("gate generated_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise VerificationError("gate generated_at must include a timezone")


def _normalize_comparison(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    normalized.pop("generated_at", None)
    dataset = _required_object(normalized.get("dataset"), "gate dataset")
    before = _required_object(normalized.get("before"), "gate before binding")
    after = _required_object(normalized.get("after"), "gate after binding")
    holdout = _required_object(
        normalized.get("holdout_split"), "gate validation holdout"
    )
    dataset["source"] = "<validated-dataset-source>"
    before["source"] = "<validated-before-run-source>"
    after["source"] = "<validated-after-run-source>"
    holdout["manifest_source"] = "<validated-manifest-source>"
    holdout["input_dataset_source"] = "<validated-source-dataset-source>"
    holdout["source_dataset_source"] = "<validated-source-dataset-source>"
    holdout["validation_dataset_source"] = "<validated-dataset-source>"
    holdout["before_run_source"] = "<validated-before-run-source>"
    holdout["after_run_source"] = "<validated-after-run-source>"
    return normalized


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verify_release_gate(report_path: str | Path, repository_root: str | Path) -> None:
    root = Path(repository_root).resolve(strict=True)
    if not root.is_dir():
        raise VerificationError(f"Repository root is not a directory: {root}")
    report_file = Path(report_path).resolve(strict=True)
    if not report_file.is_file():
        raise VerificationError(f"Gate report is not a file: {report_file}")

    # Import the canonical implementation from the checked-out repository only.
    sys.path.insert(0, str(root))
    try:
        from tests.search_quality import evaluate, quality_gate
        from tests.search_quality import split as dataset_split
    except ImportError as exc:
        raise VerificationError(
            "Could not import the repository search-quality implementation"
        ) from exc

    report = _read_json_object(report_file, "gate report")
    _validate_generated_at(report.get("generated_at"))
    dataset_binding = _required_object(report.get("dataset"), "gate dataset")
    before_binding = _required_object(report.get("before"), "gate before binding")
    after_binding = _required_object(report.get("after"), "gate after binding")
    holdout = _required_object(report.get("holdout_split"), "gate holdout_split")

    report_directory = report_file.parent
    dataset_path = _resolve_reference(
        dataset_binding.get("source"),
        label="validation dataset",
        repository_root=root,
        report_directory=report_directory,
    )
    holdout_dataset_path = _resolve_reference(
        holdout.get("validation_dataset_source"),
        label="holdout validation dataset",
        repository_root=root,
        report_directory=report_directory,
    )
    _assert_same_reference(dataset_path, holdout_dataset_path, "validation dataset")
    source_dataset_path = _resolve_reference(
        holdout.get("source_dataset_source"),
        label="source dataset",
        repository_root=root,
        report_directory=report_directory,
    )
    input_dataset_path = _resolve_reference(
        holdout.get("input_dataset_source"),
        label="holdout input dataset",
        repository_root=root,
        report_directory=report_directory,
    )
    _assert_same_reference(
        source_dataset_path,
        input_dataset_path,
        "source dataset",
    )
    manifest_path = _resolve_reference(
        holdout.get("manifest_source"),
        label="split manifest",
        repository_root=root,
        report_directory=report_directory,
    )
    before_path = _resolve_reference(
        before_binding.get("source"),
        label="before run",
        repository_root=root,
        report_directory=report_directory,
    )
    holdout_before_path = _resolve_reference(
        holdout.get("before_run_source"),
        label="holdout before run",
        repository_root=root,
        report_directory=report_directory,
    )
    _assert_same_reference(before_path, holdout_before_path, "before run")
    after_path = _resolve_reference(
        after_binding.get("source"),
        label="after run",
        repository_root=root,
        report_directory=report_directory,
    )
    holdout_after_path = _resolve_reference(
        holdout.get("after_run_source"),
        label="holdout after run",
        repository_root=root,
        report_directory=report_directory,
    )
    _assert_same_reference(after_path, holdout_after_path, "after run")
    if _same_file(before_path, after_path):
        raise VerificationError("Before and after runs must be distinct files")
    if (
        len(
            {
                source_dataset_path,
                dataset_path,
                manifest_path,
                before_path,
                after_path,
            }
        )
        != 5
    ):
        raise VerificationError("Gate inputs must be five distinct artifacts")

    source_dataset = _read_json_object(source_dataset_path, "source dataset")
    dataset = _read_json_object(dataset_path, "validation dataset")
    manifest = _read_json_object(manifest_path, "split manifest")
    before_run = _read_json_object(before_path, "before run")
    after_run = _read_json_object(after_path, "after run")

    dataset_split.validate_split_manifest(manifest)
    source_items = quality_gate.validate_gate_dataset(source_dataset)
    if len(source_items) != 60:
        raise VerificationError(
            "Formal release source dataset must contain exactly 60 human-verified cases"
        )
    manifest_fingerprint = _required_text(
        manifest.get("manifest_fingerprint"), "manifest fingerprint"
    )
    if holdout.get("manifest_fingerprint") != manifest_fingerprint:
        raise VerificationError("Gate holdout manifest fingerprint does not match")
    source_entry = _required_object(manifest.get("dataset"), "manifest source dataset")
    source_fingerprint = evaluate.dataset_fingerprint(source_dataset)
    if (
        source_dataset.get("name") != source_entry.get("name")
        or source_fingerprint != source_entry.get("fingerprint")
        or source_entry.get("case_count") != 60
    ):
        raise VerificationError("Source dataset does not match the split manifest")
    if (
        holdout.get("source_dataset_fingerprint") != source_fingerprint
        or holdout.get("source_dataset_case_count") != 60
    ):
        raise VerificationError("Gate holdout source dataset identity does not match")
    splits = _required_object(manifest.get("splits"), "manifest splits")
    calibration_entry = _required_object(
        splits.get("calibration"), "manifest calibration split"
    )
    validation_entry = _required_object(
        splits.get("validation"),
        "manifest validation split",
    )
    if (
        calibration_entry.get("case_count") != 36
        or validation_entry.get("case_count") != 24
    ):
        raise VerificationError(
            "Formal release split must contain 36 calibration and 24 validation cases"
        )
    dataset_split.subset_dataset_from_manifest(
        source_dataset,
        manifest,
        "calibration",
    )
    expected_validation = dataset_split.subset_dataset_from_manifest(
        source_dataset,
        manifest,
        "validation",
    )
    if _canonical_json(dataset) != _canonical_json(expected_validation):
        raise VerificationError(
            "Validation dataset does not exactly reproduce the manifest assignment"
        )
    dataset_fingerprint = evaluate.dataset_fingerprint(dataset)
    if dataset.get("name") != validation_entry.get("dataset_name"):
        raise VerificationError("Input dataset is not the manifest validation dataset")
    if dataset_fingerprint != validation_entry.get("dataset_fingerprint"):
        raise VerificationError(
            "Validation dataset fingerprint does not match manifest"
        )
    if dataset_binding.get("fingerprint") != dataset_fingerprint:
        raise VerificationError("Gate dataset canonical fingerprint does not match")
    _assert_validation_split(
        dataset.get("split"),
        label="validation dataset",
        manifest_fingerprint=manifest_fingerprint,
        split_kind=dataset_split.SPLIT_KIND,
        split_fingerprint_algorithm=dataset_split.SPLIT_FINGERPRINT_ALGORITHM,
    )
    for label, run in (("before run", before_run), ("after run", after_run)):
        _assert_validation_split(
            run.get("split"),
            label=label,
            manifest_fingerprint=manifest_fingerprint,
            split_kind=dataset_split.SPLIT_KIND,
            split_fingerprint_algorithm=dataset_split.SPLIT_FINGERPRINT_ALGORITHM,
        )

    before_sha256 = evaluate.source_file_sha256(before_path)
    after_sha256 = evaluate.source_file_sha256(after_path)
    if before_sha256 == after_sha256:
        raise VerificationError("Before and after run bytes must be distinct")
    for label, binding, digest in (
        ("before", before_binding, before_sha256),
        ("after", after_binding, after_sha256),
    ):
        if binding.get("source_sha256") != digest:
            raise VerificationError(f"{label} run source SHA-256 does not match")
        if (
            binding.get("source_sha256_algorithm")
            != evaluate.SOURCE_FILE_SHA256_ALGORITHM
        ):
            raise VerificationError(f"{label} run source hash algorithm is invalid")

    with tempfile.TemporaryDirectory(prefix="zvec-release-quality-replay-") as temp:
        replay = quality_gate.run_quality_gate(
            source_dataset_path,
            before_path,
            after_path,
            temp,
            split_manifest_path=manifest_path,
        )
    replay_gate = _required_object(replay.get("quality_gate"), "replayed quality gate")
    if replay_gate.get("passed") is not True or replay_gate.get("status") != "pass":
        raise VerificationError("Replayed search-quality acceptance gate did not pass")

    for label, binding, replay_binding in (
        ("before", before_binding, replay.get("before")),
        ("after", after_binding, replay.get("after")),
    ):
        replay_identity = _required_object(replay_binding, f"replayed {label} binding")
        if binding.get("fingerprint") != replay_identity.get("fingerprint"):
            raise VerificationError(
                f"{label} canonical run fingerprint does not match replay"
            )
        if binding.get("fingerprint_algorithm") != evaluate.RUN_FINGERPRINT_ALGORITHM:
            raise VerificationError(f"{label} canonical run algorithm is invalid")

    claimed = _canonical_json(_normalize_comparison(report))
    reproduced = _canonical_json(_normalize_comparison(replay))
    if claimed != reproduced:
        raise VerificationError(
            "Gate report does not exactly match the independently replayed "
            "dataset, metrics, Collection fairness, or acceptance checks"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Independently replay and verify a formal release search-quality gate"
        )
    )
    parser.add_argument("--report", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_release_gate(args.report, args.repository_root)
    except (OSError, VerificationError, ValueError) as exc:
        print(f"Search-quality gate replay verification failed: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"Search-quality gate replay verified: {Path(args.report).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
