from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)

SPLIT_SCHEMA_VERSION = 1
SPLIT_KIND = "zvec-search-quality-split"
SPLIT_ALGORITHM = "sha256-seeded-stratified-v1"
SPLIT_FINGERPRINT_ALGORITHM = "sha256-canonical-json-v1"
DEFAULT_SEED = "zvec-search-quality-holdout-v1"
DEFAULT_VALIDATION_FRACTION = 0.40
ROLES = ("calibration", "validation")
MIN_FORMAL_TOP_K = 5
MIN_FORMAL_CANDIDATE_K = 50


def _read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON from {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {source}")
    return value


def _write_json(path: str | Path, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_seed(seed: Any) -> str:
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError("split seed must be a non-empty string")
    return seed


def _validate_fraction(validation_fraction: Any) -> float:
    if (
        isinstance(validation_fraction, bool)
        or not isinstance(validation_fraction, (int, float))
        or not math.isfinite(float(validation_fraction))
        or not 0.0 < float(validation_fraction) < 1.0
    ):
        raise ValueError("validation_fraction must be finite and in (0, 1)")
    return float(validation_fraction)


def _annotation_status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(item["annotation"]["status"] == status for item in items)
        for status in sorted(evaluate.ANNOTATION_STATUSES)
    }


def _validate_human_metadata(item: dict[str, Any]) -> None:
    annotation = item["annotation"]
    annotator = annotation.get("annotator")
    annotated_at = annotation.get("annotated_at")
    if not isinstance(annotator, str) or not annotator.strip():
        raise ValueError(f"dataset item {item['id']}: human_verified needs annotator")
    if not isinstance(annotated_at, str) or not annotated_at.strip():
        raise ValueError(
            f"dataset item {item['id']}: human_verified needs annotated_at"
        )
    try:
        datetime.fromisoformat(annotated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"dataset item {item['id']}: annotated_at must be ISO-8601"
        ) from exc


def _validated_split_items(
    dataset: dict[str, Any],
    *,
    allow_synthetic: bool,
) -> list[dict[str, Any]]:
    items = evaluate.validate_dataset(dataset)
    pending = [
        item["id"] for item in items if item["annotation"]["status"] == "pending"
    ]
    if pending:
        preview = ", ".join(pending[:5])
        suffix = "..." if len(pending) > 5 else ""
        raise ValueError(
            "holdout split requires every case to be annotated; pending cases: "
            f"{preview}{suffix}"
        )
    synthetic = [
        item["id"]
        for item in items
        if item["annotation"]["status"] == "synthetic_fixture"
    ]
    if synthetic and not allow_synthetic:
        raise ValueError(
            "synthetic fixture labels require allow_synthetic=True or --allow-synthetic"
        )
    for item in items:
        if item["annotation"]["status"] == "human_verified":
            _validate_human_metadata(item)
    return items


def _stratum_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item["mode"]), str(item["query_type"])


def _required_strata() -> set[tuple[str, str]]:
    return {
        (mode, query_type)
        for mode in evaluate.MODES
        for query_type in (mode, "no-answer")
    }


def _item_rank(seed: str, item: dict[str, Any]) -> str:
    mode, query_type = _stratum_key(item)
    return _canonical_sha256(
        {
            "algorithm": SPLIT_ALGORITHM,
            "seed": seed,
            "mode": mode,
            "query_type": query_type,
            "id": item["id"],
        }
    )


def _validation_count(size: int, validation_fraction: float) -> int:
    # Round halves upward, then preserve one case on both sides of each stratum.
    target = math.floor(size * validation_fraction + 0.5)
    return min(size - 1, max(1, target))


def _assign_items(
    items: list[dict[str, Any]],
    *,
    seed: str,
    validation_fraction: float,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_stratum_key(item), []).append(item)

    missing = sorted(_required_strata() - set(grouped))
    if missing:
        descriptions = ", ".join(f"{mode}/{query_type}" for mode, query_type in missing)
        raise ValueError(
            "holdout split requires answerable and no-answer coverage for every "
            f"mode; missing strata: {descriptions}"
        )

    unexpected = sorted(set(grouped) - _required_strata())
    if unexpected:  # Defensive: evaluate currently prevents this shape.
        descriptions = ", ".join(
            f"{mode}/{query_type}" for mode, query_type in unexpected
        )
        raise ValueError(f"holdout split contains unsupported strata: {descriptions}")

    assignments: dict[str, list[str]] = {role: [] for role in ROLES}
    strata: list[dict[str, Any]] = []
    original_position = {str(item["id"]): index for index, item in enumerate(items)}
    for mode, query_type in sorted(grouped):
        members = grouped[(mode, query_type)]
        if len(members) < 2:
            raise ValueError(
                "holdout split needs at least two cases in every stratum so both "
                f"roles retain coverage; {mode}/{query_type} has {len(members)}"
            )
        ordered = sorted(
            members,
            key=lambda item: (_item_rank(seed, item), str(item["id"])),
        )
        validation_size = _validation_count(len(ordered), validation_fraction)
        validation_ids = {str(item["id"]) for item in ordered[:validation_size]}
        role_ids = {
            "calibration": sorted(
                (
                    str(item["id"])
                    for item in members
                    if str(item["id"]) not in validation_ids
                ),
                key=original_position.__getitem__,
            ),
            "validation": sorted(validation_ids, key=original_position.__getitem__),
        }
        for role in ROLES:
            assignments[role].extend(role_ids[role])
        strata.append(
            {
                "mode": mode,
                "query_type": query_type,
                "answer_state": (
                    "no_answer" if query_type == "no-answer" else "answerable"
                ),
                "total": len(members),
                "calibration": len(role_ids["calibration"]),
                "validation": len(role_ids["validation"]),
                "calibration_case_ids": role_ids["calibration"],
                "validation_case_ids": role_ids["validation"],
            }
        )

    for role in ROLES:
        assignments[role].sort(key=original_position.__getitem__)
    return assignments, strata


def _subset_name(dataset_name: str, role: str) -> str:
    return f"{dataset_name}::{role}"


def _subset_dataset(
    dataset: dict[str, Any],
    items_by_id: dict[str, dict[str, Any]],
    case_ids: list[str],
    *,
    role: str,
    source_fingerprint: str,
    manifest_fingerprint: str | None = None,
) -> dict[str, Any]:
    subset: dict[str, Any] = {
        "schema_version": evaluate.DATASET_SCHEMA_VERSION,
        "name": _subset_name(str(dataset["name"]), role),
        "description": (
            f"Deterministic {role} subset of {dataset['name']}. "
            "Relevance annotations are copied unchanged from the source dataset."
        ),
        "items": [copy.deepcopy(items_by_id[case_id]) for case_id in case_ids],
        "split": {
            "kind": SPLIT_KIND,
            "role": role,
            "source_dataset_name": dataset["name"],
            "source_dataset_fingerprint": source_fingerprint,
            "fingerprint_algorithm": evaluate.DATASET_FINGERPRINT_ALGORITHM,
        },
    }
    if manifest_fingerprint is not None:
        subset["split"]["manifest_fingerprint"] = manifest_fingerprint
        subset["split"]["manifest_fingerprint_algorithm"] = SPLIT_FINGERPRINT_ALGORITHM
    return subset


def split_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    canonical = copy.deepcopy(manifest)
    canonical.pop("manifest_fingerprint", None)
    return _canonical_sha256(canonical)


def build_split_artifacts(
    dataset: dict[str, Any],
    *,
    seed: str = DEFAULT_SEED,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    allow_synthetic: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    resolved_seed = _validate_seed(seed)
    resolved_fraction = _validate_fraction(validation_fraction)
    items = _validated_split_items(dataset, allow_synthetic=allow_synthetic)
    assignments, strata = _assign_items(
        items,
        seed=resolved_seed,
        validation_fraction=resolved_fraction,
    )
    source_fingerprint = evaluate.dataset_fingerprint(dataset)
    items_by_id = {str(item["id"]): item for item in items}
    provisional_subsets = {
        role: _subset_dataset(
            dataset,
            items_by_id,
            assignments[role],
            role=role,
            source_fingerprint=source_fingerprint,
        )
        for role in ROLES
    }
    manifest: dict[str, Any] = {
        "schema_version": SPLIT_SCHEMA_VERSION,
        "kind": SPLIT_KIND,
        "dataset": {
            "name": dataset["name"],
            "fingerprint": source_fingerprint,
            "fingerprint_algorithm": evaluate.DATASET_FINGERPRINT_ALGORITHM,
            "case_count": len(items),
        },
        "strategy": {
            "algorithm": SPLIT_ALGORITHM,
            "seed": resolved_seed,
            "validation_fraction": resolved_fraction,
            "stratification": ["mode", "query_type"],
            "minimum_per_role_per_stratum": 1,
        },
        "annotation_policy": {
            "allow_synthetic": allow_synthetic,
            "status_counts": _annotation_status_counts(items),
        },
        "splits": {
            role: {
                "dataset_name": provisional_subsets[role]["name"],
                "dataset_fingerprint": evaluate.dataset_fingerprint(
                    provisional_subsets[role]
                ),
                "fingerprint_algorithm": evaluate.DATASET_FINGERPRINT_ALGORITHM,
                "case_count": len(assignments[role]),
                "case_ids": assignments[role],
            }
            for role in ROLES
        },
        "strata": strata,
        "manifest_fingerprint_algorithm": SPLIT_FINGERPRINT_ALGORITHM,
    }
    manifest["manifest_fingerprint"] = split_manifest_fingerprint(manifest)
    subsets = {
        role: _subset_dataset(
            dataset,
            items_by_id,
            assignments[role],
            role=role,
            source_fingerprint=source_fingerprint,
            manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        for role in ROLES
    }
    return manifest, subsets["calibration"], subsets["validation"]


def _manifest_split(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    splits = manifest.get("splits")
    if not isinstance(splits, dict) or set(splits) != set(ROLES):
        raise ValueError("split manifest must define calibration and validation")
    value = splits.get(role)
    if not isinstance(value, dict):
        raise ValueError(f"split manifest {role} entry must be an object")
    return value


def validate_split_manifest(manifest: dict[str, Any]) -> dict[str, list[str]]:
    if (
        manifest.get("schema_version") != SPLIT_SCHEMA_VERSION
        or manifest.get("kind") != SPLIT_KIND
    ):
        raise ValueError("split manifest must be zvec-search-quality-split schema v1")
    if manifest.get("manifest_fingerprint_algorithm") != SPLIT_FINGERPRINT_ALGORITHM:
        raise ValueError("split manifest uses an unsupported fingerprint algorithm")
    fingerprint = manifest.get("manifest_fingerprint")
    if not _is_sha256(fingerprint):
        raise ValueError("split manifest fingerprint must be a lowercase SHA-256")
    if fingerprint != split_manifest_fingerprint(manifest):
        raise ValueError("split manifest fingerprint does not match its contents")

    dataset_identity = manifest.get("dataset")
    if not isinstance(dataset_identity, dict):
        raise ValueError("split manifest dataset identity must be an object")
    if (
        not isinstance(dataset_identity.get("name"), str)
        or not str(dataset_identity["name"]).strip()
        or not _is_sha256(dataset_identity.get("fingerprint"))
        or dataset_identity.get("fingerprint_algorithm")
        != evaluate.DATASET_FINGERPRINT_ALGORITHM
        or isinstance(dataset_identity.get("case_count"), bool)
        or not isinstance(dataset_identity.get("case_count"), int)
        or dataset_identity["case_count"] <= 0
    ):
        raise ValueError("split manifest has an invalid dataset identity")

    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict):
        raise ValueError("split manifest strategy must be an object")
    if strategy.get("algorithm") != SPLIT_ALGORITHM:
        raise ValueError("split manifest uses an unsupported split algorithm")
    _validate_seed(strategy.get("seed"))
    _validate_fraction(strategy.get("validation_fraction"))
    if strategy.get("stratification") != ["mode", "query_type"]:
        raise ValueError("split manifest must stratify by mode and query_type")
    if strategy.get("minimum_per_role_per_stratum") != 1:
        raise ValueError("split manifest must retain every stratum in both roles")

    role_ids: dict[str, list[str]] = {}
    for role in ROLES:
        entry = _manifest_split(manifest, role)
        case_ids = entry.get("case_ids")
        if not isinstance(case_ids, list) or any(
            not isinstance(case_id, str) or not case_id.strip() for case_id in case_ids
        ):
            raise ValueError(f"split manifest {role} case_ids must be strings")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError(f"split manifest {role} contains duplicate case IDs")
        if entry.get("case_count") != len(case_ids):
            raise ValueError(f"split manifest {role} case_count does not match")
        if (
            not isinstance(entry.get("dataset_name"), str)
            or not str(entry["dataset_name"]).strip()
            or not _is_sha256(entry.get("dataset_fingerprint"))
            or entry.get("fingerprint_algorithm")
            != evaluate.DATASET_FINGERPRINT_ALGORITHM
        ):
            raise ValueError(f"split manifest {role} dataset identity is invalid")
        role_ids[role] = list(case_ids)

    overlap = sorted(set(role_ids["calibration"]) & set(role_ids["validation"]))
    if overlap:
        raise ValueError(
            "split manifest leaks cases across calibration and validation: "
            + ", ".join(overlap[:5])
        )
    all_ids = role_ids["calibration"] + role_ids["validation"]
    if len(all_ids) != dataset_identity["case_count"]:
        raise ValueError("split manifest roles do not cover the source case count")

    strata = manifest.get("strata")
    if not isinstance(strata, list) or any(
        not isinstance(value, dict) for value in strata
    ):
        raise ValueError("split manifest strata must be an array of objects")
    seen_strata: set[tuple[str, str]] = set()
    strata_role_ids: dict[str, list[str]] = {role: [] for role in ROLES}
    for stratum in strata:
        mode = stratum.get("mode")
        query_type = stratum.get("query_type")
        if not isinstance(mode, str) or not isinstance(query_type, str):
            raise ValueError("split manifest strata need mode and query_type")
        key = (mode, query_type)
        if key not in _required_strata() or key in seen_strata:
            raise ValueError("split manifest has invalid or duplicate strata")
        seen_strata.add(key)
        expected_state = "no_answer" if key[1] == "no-answer" else "answerable"
        if stratum.get("answer_state") != expected_state:
            raise ValueError("split manifest stratum answer_state does not match")
        total = stratum.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 2:
            raise ValueError("split manifest strata must contain at least two cases")
        for role in ROLES:
            ids = stratum.get(f"{role}_case_ids")
            count = stratum.get(role)
            if (
                not isinstance(ids, list)
                or any(
                    not isinstance(case_id, str) or not case_id.strip()
                    for case_id in ids
                )
                or len(ids) != len(set(ids))
                or count != len(ids)
                or not ids
            ):
                raise ValueError(f"split manifest stratum must retain {role} coverage")
            strata_role_ids[role].extend(ids)
        if stratum["calibration"] + stratum["validation"] != total:
            raise ValueError("split manifest stratum counts do not add up")
    if seen_strata != _required_strata():
        raise ValueError("split manifest does not cover all required strata")
    for role in ROLES:
        if len(strata_role_ids[role]) != len(set(strata_role_ids[role])):
            raise ValueError(f"split manifest repeats {role} cases across strata")
        if set(strata_role_ids[role]) != set(role_ids[role]):
            raise ValueError(
                f"split manifest {role} IDs disagree with stratum assignments"
            )
    return role_ids


def _validate_source_dataset(
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    *,
    allow_synthetic: bool,
) -> list[dict[str, Any]]:
    role_ids = validate_split_manifest(manifest)
    items = _validated_split_items(dataset, allow_synthetic=allow_synthetic)
    identity = manifest["dataset"]
    if dataset.get("name") != identity["name"]:
        raise ValueError("split manifest uses a different source dataset name")
    if evaluate.dataset_fingerprint(dataset) != identity["fingerprint"]:
        raise ValueError("split manifest source dataset fingerprint does not match")
    if len(items) != identity["case_count"]:
        raise ValueError("split manifest source dataset case count does not match")
    if set(item["id"] for item in items) != set(
        role_ids["calibration"] + role_ids["validation"]
    ):
        raise ValueError("split manifest does not exhaustively cover source case IDs")
    annotation_policy = manifest.get("annotation_policy")
    if not isinstance(annotation_policy, dict):
        raise ValueError("split manifest annotation_policy must be an object")
    if annotation_policy.get("allow_synthetic") is not allow_synthetic:
        raise ValueError("split manifest synthetic-label policy does not match")
    if annotation_policy.get("status_counts") != _annotation_status_counts(items):
        raise ValueError("split manifest annotation counts do not match")

    expected_assignments, expected_strata = _assign_items(
        items,
        seed=manifest["strategy"]["seed"],
        validation_fraction=manifest["strategy"]["validation_fraction"],
    )
    if expected_assignments != role_ids or expected_strata != manifest["strata"]:
        raise ValueError("split manifest assignments are not deterministic")
    return items


def subset_dataset_from_manifest(
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    role: str,
    *,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"unknown split role: {role}")
    items = _validate_source_dataset(
        dataset,
        manifest,
        allow_synthetic=allow_synthetic,
    )
    entry = _manifest_split(manifest, role)
    items_by_id = {str(item["id"]): item for item in items}
    subset = _subset_dataset(
        dataset,
        items_by_id,
        entry["case_ids"],
        role=role,
        source_fingerprint=manifest["dataset"]["fingerprint"],
        manifest_fingerprint=manifest["manifest_fingerprint"],
    )
    if subset["name"] != entry["dataset_name"]:
        raise ValueError(f"split manifest {role} dataset name does not match")
    if evaluate.dataset_fingerprint(subset) != entry["dataset_fingerprint"]:
        raise ValueError(f"split manifest {role} dataset fingerprint does not match")
    return subset


def _validate_existing_subset(
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    role: str,
    *,
    allow_synthetic: bool,
) -> list[dict[str, Any]]:
    items = _validated_split_items(dataset, allow_synthetic=allow_synthetic)
    entry = _manifest_split(manifest, role)
    if dataset.get("name") != entry["dataset_name"]:
        raise ValueError(f"input is not the manifest's {role} dataset")
    if evaluate.dataset_fingerprint(dataset) != entry["dataset_fingerprint"]:
        raise ValueError(f"manifest {role} dataset fingerprint does not match")
    if [str(item["id"]) for item in items] != entry["case_ids"]:
        raise ValueError(f"manifest {role} dataset case order does not match")
    split_metadata = dataset.get("split")
    if not isinstance(split_metadata, dict) or split_metadata.get("role") != role:
        raise ValueError(f"input dataset is missing {role} split metadata")
    if split_metadata.get("kind") != SPLIT_KIND:
        raise ValueError(f"input dataset has invalid {role} split metadata")
    if split_metadata.get("manifest_fingerprint") != manifest["manifest_fingerprint"]:
        raise ValueError("input dataset references a different split manifest")
    if (
        split_metadata.get("manifest_fingerprint_algorithm")
        != SPLIT_FINGERPRINT_ALGORITHM
    ):
        raise ValueError("input dataset uses an invalid split fingerprint algorithm")
    return items


def _validate_formal_run_capture(
    run: dict[str, Any],
    dataset: dict[str, Any],
    *,
    role: str,
    allow_synthetic: bool,
) -> None:
    if allow_synthetic:
        return
    if run.get("draft") is not False:
        raise ValueError(f"{role} run must be a non-draft capture")
    if run.get("baseline_eligible") is not True:
        raise ValueError(f"{role} run must be baseline_eligible")

    capture = run.get("capture")
    if not isinstance(capture, dict) or capture.get("kind") != (
        "zvec-persistent-backend-capture"
    ):
        raise ValueError(f"{role} run must come from persistent backend capture")
    if capture.get("baseline_eligible") is not True:
        raise ValueError(f"{role} capture must be baseline_eligible")
    pending_case_count = capture.get("pending_case_count")
    if (
        isinstance(pending_case_count, bool)
        or not isinstance(pending_case_count, int)
        or pending_case_count != 0
    ):
        raise ValueError(f"{role} capture must contain no pending cases")
    for field, minimum in (
        ("top_k", MIN_FORMAL_TOP_K),
        ("candidate_k", MIN_FORMAL_CANDIDATE_K),
    ):
        value = capture.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{role} capture {field} must be at least {minimum}")

    items = evaluate.validate_dataset(dataset)
    identity = run.get("dataset")
    if not isinstance(identity, dict):
        raise ValueError(f"{role} run is missing query corpus identity")
    if identity.get("name") != dataset.get("name"):
        raise ValueError(f"{role} run uses a different query corpus name")
    if (
        identity.get("fingerprint_algorithm")
        != evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM
    ):
        raise ValueError(f"{role} run query corpus fingerprint algorithm is invalid")
    if identity.get("fingerprint") != evaluate.query_corpus_fingerprint(dataset):
        raise ValueError(f"{role} run query corpus fingerprint does not match")
    case_count = identity.get("case_count")
    if (
        isinstance(case_count, bool)
        or not isinstance(case_count, int)
        or case_count != len(items)
    ):
        raise ValueError(f"{role} run query corpus case_count does not match")


def prepare_calibration_inputs(
    dataset: dict[str, Any],
    run: dict[str, Any],
    manifest: dict[str, Any],
    *,
    allow_synthetic: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    role_ids = validate_split_manifest(manifest)
    dataset_fingerprint = evaluate.dataset_fingerprint(dataset)
    source_entry = manifest["dataset"]
    calibration_entry = _manifest_split(manifest, "calibration")
    validation_entry = _manifest_split(manifest, "validation")

    input_is_source = False
    if (
        dataset.get("name") == source_entry["name"]
        and dataset_fingerprint == source_entry["fingerprint"]
    ):
        input_is_source = True
        calibration_dataset = subset_dataset_from_manifest(
            dataset,
            manifest,
            "calibration",
            allow_synthetic=allow_synthetic,
        )
    elif (
        dataset.get("name") == validation_entry["dataset_name"]
        and dataset_fingerprint == validation_entry["dataset_fingerprint"]
    ):
        raise ValueError("validation holdout cannot be used for calibration")
    elif (
        dataset.get("name") == calibration_entry["dataset_name"]
        and dataset_fingerprint == calibration_entry["dataset_fingerprint"]
    ):
        _validate_existing_subset(
            dataset,
            manifest,
            "calibration",
            allow_synthetic=allow_synthetic,
        )
        calibration_dataset = copy.deepcopy(dataset)
    else:
        raise ValueError("dataset does not match the split source or calibration role")

    cases = evaluate.validate_run(run)
    source_ids = set(role_ids["calibration"] + role_ids["validation"])
    unknown = sorted(set(cases) - source_ids)
    if unknown:
        raise ValueError(
            "run contains cases outside the split source dataset: "
            + ", ".join(unknown[:5])
        )
    missing = [case_id for case_id in role_ids["calibration"] if case_id not in cases]
    if missing:
        raise ValueError("run is missing calibration cases: " + ", ".join(missing[:5]))
    calibration_ids = set(role_ids["calibration"])
    case_ids = set(cases)
    if not allow_synthetic:
        if case_ids == source_ids:
            if not input_is_source:
                raise ValueError(
                    "full-source calibration run requires the source dataset input"
                )
            capture_dataset = dataset
        elif case_ids == calibration_ids:
            capture_dataset = calibration_dataset
        else:
            raise ValueError(
                "formal calibration run must cover exactly the source or "
                "calibration query corpus"
            )
        _validate_formal_run_capture(
            run,
            capture_dataset,
            role="calibration",
            allow_synthetic=False,
        )
    calibration_run = copy.deepcopy(run)
    calibration_run["name"] = f"{run['name']}::calibration"
    calibration_run["cases"] = [
        case for case in calibration_run["cases"] if case["id"] in calibration_ids
    ]
    calibration_run["split"] = {
        "kind": SPLIT_KIND,
        "role": "calibration",
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": SPLIT_FINGERPRINT_ALGORITHM,
    }
    return calibration_dataset, calibration_run


def calibration_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_split_manifest(manifest)
    return {
        "kind": SPLIT_KIND,
        "role": "calibration",
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": SPLIT_FINGERPRINT_ALGORITHM,
        "source_dataset_fingerprint": manifest["dataset"]["fingerprint"],
        "source_dataset_fingerprint_algorithm": manifest["dataset"][
            "fingerprint_algorithm"
        ],
        "validation_dataset_fingerprint": manifest["splits"]["validation"][
            "dataset_fingerprint"
        ],
        "strategy": copy.deepcopy(manifest["strategy"]),
    }


def prepare_validation_inputs(
    dataset: dict[str, Any],
    run: dict[str, Any],
    manifest: dict[str, Any],
    *,
    allow_synthetic: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve and validate the frozen validation dataset and its exact run cases."""

    role_ids = validate_split_manifest(manifest)
    dataset_fingerprint = evaluate.dataset_fingerprint(dataset)
    source_entry = manifest["dataset"]
    calibration_entry = _manifest_split(manifest, "calibration")
    validation_entry = _manifest_split(manifest, "validation")

    if (
        dataset.get("name") == source_entry["name"]
        and dataset_fingerprint == source_entry["fingerprint"]
    ):
        validation_dataset = subset_dataset_from_manifest(
            dataset,
            manifest,
            "validation",
            allow_synthetic=allow_synthetic,
        )
    elif (
        dataset.get("name") == calibration_entry["dataset_name"]
        and dataset_fingerprint == calibration_entry["dataset_fingerprint"]
    ):
        raise ValueError("calibration role cannot be used for validation")
    elif (
        dataset.get("name") == validation_entry["dataset_name"]
        and dataset_fingerprint == validation_entry["dataset_fingerprint"]
    ):
        _validate_existing_subset(
            dataset,
            manifest,
            "validation",
            allow_synthetic=allow_synthetic,
        )
        validation_dataset = copy.deepcopy(dataset)
    else:
        raise ValueError("dataset does not match the split source or validation role")

    run_split = run.get("split")
    if run_split is None and not allow_synthetic:
        raise ValueError("formal validation run is missing validation split metadata")
    if run_split is not None:
        if not isinstance(run_split, dict):
            raise ValueError("run split metadata must be an object")
        if run_split.get("role") == "calibration":
            raise ValueError("calibration run cannot be used for validation")
        if (
            run_split.get("kind") != SPLIT_KIND
            or run_split.get("role") != "validation"
            or run_split.get("manifest_fingerprint") != manifest["manifest_fingerprint"]
            or run_split.get("manifest_fingerprint_algorithm")
            != SPLIT_FINGERPRINT_ALGORITHM
        ):
            raise ValueError("validation run split metadata does not match manifest")
    cases = evaluate.validate_run(run)
    source_ids = set(role_ids["calibration"] + role_ids["validation"])
    unknown = sorted(set(cases) - source_ids)
    if unknown:
        raise ValueError(
            "run contains cases outside the split source dataset: "
            + ", ".join(unknown[:5])
        )
    leaked = sorted(set(cases) & set(role_ids["calibration"]))
    if leaked:
        raise ValueError(
            "validation run contains calibration case leakage: " + ", ".join(leaked[:5])
        )
    missing = [case_id for case_id in role_ids["validation"] if case_id not in cases]
    if missing:
        raise ValueError("run is missing validation cases: " + ", ".join(missing[:5]))
    if len(cases) != len(role_ids["validation"]):
        raise ValueError("validation run case coverage does not match the manifest")
    _validate_formal_run_capture(
        run,
        validation_dataset,
        role="validation",
        allow_synthetic=allow_synthetic,
    )

    validation_run = copy.deepcopy(run)
    validation_run["name"] = f"{run['name']}::validation"
    validation_run["cases"] = [
        copy.deepcopy(cases[case_id]) for case_id in role_ids["validation"]
    ]
    validation_run["split"] = {
        "kind": SPLIT_KIND,
        "role": "validation",
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": SPLIT_FINGERPRINT_ALGORITHM,
    }
    return validation_dataset, validation_run


def validation_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_split_manifest(manifest)
    return {
        "kind": SPLIT_KIND,
        "role": "validation",
        "manifest_fingerprint": manifest["manifest_fingerprint"],
        "manifest_fingerprint_algorithm": SPLIT_FINGERPRINT_ALGORITHM,
        "source_dataset_name": manifest["dataset"]["name"],
        "source_dataset_fingerprint": manifest["dataset"]["fingerprint"],
        "source_dataset_fingerprint_algorithm": manifest["dataset"][
            "fingerprint_algorithm"
        ],
        "validation_dataset_name": manifest["splits"]["validation"]["dataset_name"],
        "validation_dataset_fingerprint": manifest["splits"]["validation"][
            "dataset_fingerprint"
        ],
        "validation_case_count": manifest["splits"]["validation"]["case_count"],
        "strategy": copy.deepcopy(manifest["strategy"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create deterministic, stratified calibration and validation datasets"
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--calibration-output", required=True)
    parser.add_argument("--validation-output", required=True)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=DEFAULT_VALIDATION_FRACTION,
    )
    parser.add_argument("--allow-synthetic", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = [
        Path(args.dataset).resolve(),
        Path(args.manifest).resolve(),
        Path(args.calibration_output).resolve(),
        Path(args.validation_output).resolve(),
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("dataset and split output paths must all be different")
    manifest, calibration_dataset, validation_dataset = build_split_artifacts(
        _read_json(args.dataset),
        seed=args.seed,
        validation_fraction=args.validation_fraction,
        allow_synthetic=args.allow_synthetic,
    )
    _write_json(args.manifest, manifest)
    _write_json(args.calibration_output, calibration_dataset)
    _write_json(args.validation_output, validation_dataset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
