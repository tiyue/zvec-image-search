from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .calibration import CalibrationExample, calibration_metrics
from .common import SearchLearningToolError, atomic_write_json, load_records


def _calibrator_for(
    registry: Mapping[str, Any], query_type: str, collection_id: str
) -> Mapping[str, Any]:
    collections = registry.get("collections")
    if collection_id and isinstance(collections, Mapping):
        collection = collections.get(collection_id)
        if isinstance(collection, Mapping):
            calibrator = collection.get(query_type)
            if isinstance(calibrator, Mapping):
                return calibrator
    query_types = registry.get("query_types")
    if isinstance(query_types, Mapping):
        calibrator = query_types.get(query_type)
        if isinstance(calibrator, Mapping):
            return calibrator
    calibrator = registry.get(query_type)
    if isinstance(calibrator, Mapping):
        return calibrator
    calibrator = registry.get("global")
    if isinstance(calibrator, Mapping):
        return calibrator
    raise SearchLearningToolError(
        f"No calibrator is available for query type {query_type!r}."
    )


def evaluate_registry(
    examples: Sequence[CalibrationExample], registry: Mapping[str, Any]
) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[CalibrationExample]] = {}
    for item in examples:
        groups.setdefault((item.collection_id, item.query_type), []).append(item)
    results: dict[str, Any] = {}
    for (collection_id, query_type), group in sorted(groups.items()):
        calibrator = _calibrator_for(registry, query_type, collection_id)
        key = f"{collection_id or '*'}::{query_type}"
        results[key] = calibration_metrics(group, calibrator)
    return {
        "schema_version": 1,
        "calibration_version": registry.get("calibration_version", "unknown"),
        "groups": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a Zvec calibration registry."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        examples = [
            CalibrationExample.from_mapping(record, index)
            for index, record in enumerate(load_records(args.input))
        ]
        payload = json.loads(args.calibration.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise SearchLearningToolError("Calibration file must contain an object.")
        atomic_write_json(args.output, evaluate_registry(examples, payload))
    except (OSError, json.JSONDecodeError, SearchLearningToolError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
