from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class SearchLearningToolError(ValueError):
    """Raised when an offline learning input is malformed or unsafe."""


def finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchLearningToolError(f"{name} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        raise SearchLearningToolError(f"{name} must be a finite number.")
    return result


def positive_number(value: object, name: str) -> float:
    result = finite_number(value, name)
    if result <= 0:
        raise SearchLearningToolError(f"{name} must be greater than zero.")
    return result


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load a bounded JSON array/object or JSONL file.

    Training exports are intentionally local and must never contain image
    binaries.  A 256 MiB guard prevents an accidental unbounded read.
    """

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SearchLearningToolError(f"Input file does not exist: {resolved}")
    if resolved.stat().st_size > 256 * 1024 * 1024:
        raise SearchLearningToolError("Input file exceeds the 256 MiB safety limit.")
    text = resolved.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise SearchLearningToolError("Input file is empty.")

    if resolved.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SearchLearningToolError(
                    f"Invalid JSONL at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise SearchLearningToolError(
                    f"JSONL line {line_number} must contain an object."
                )
            records.append(value)
        if not records:
            raise SearchLearningToolError("Input file has no records.")
        return records

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SearchLearningToolError(f"Invalid JSON: {exc.msg}") from exc
    if isinstance(payload, dict):
        payload = payload.get("examples", payload.get("records"))
    if not isinstance(payload, list) or not payload:
        raise SearchLearningToolError(
            "JSON input must be a non-empty array or contain examples/records."
        )
    if not all(isinstance(item, dict) for item in payload):
        raise SearchLearningToolError("Every input record must be an object.")
    return [dict(item) for item in payload]


def deterministic_validation_mask(
    keys: Iterable[str], *, ratio: float, seed: str
) -> list[bool]:
    if not 0.0 <= ratio < 1.0:
        raise SearchLearningToolError("validation_ratio must be in [0, 1).")
    threshold = int(ratio * 10_000)
    mask: list[bool] = []
    for key in keys:
        digest = hashlib.sha256(f"{seed}\0{key}".encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % 10_000
        mask.append(bucket < threshold)
    return mask


def stable_sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-min(value, 709.0))
        return 1.0 / (1.0 + factor)
    factor = math.exp(max(value, -709.0))
    return factor / (1.0 + factor)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    total = 0.0
    weight_sum = 0.0
    for value, weight in values:
        total += value * weight
        weight_sum += weight
    return total / weight_sum if weight_sum > 0 else 0.0
