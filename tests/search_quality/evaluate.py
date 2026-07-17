from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

image_identity = importlib.import_module(
    "tests.search_quality.image_identity" if __package__ else "image_identity"
)
collection_fairness = importlib.import_module(
    "tests.search_quality.collection_fairness" if __package__ else "collection_fairness"
)

DATASET_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
QUERY_TYPES = {"text", "image", "combined", "no-answer"}
MODES = {"text", "image", "combined"}
ANNOTATION_STATUSES = {"pending", "human_verified", "synthetic_fixture"}
LIBRARY_SCOPE_MODES = {"all_enabled", "single", "selected"}
TOP_K = 5
DATASET_FINGERPRINT_ALGORITHM = "sha256-canonical-json-v1"
QUERY_CORPUS_FINGERPRINT_ALGORITHM = "sha256-canonical-query-corpus-v1"
RUN_FINGERPRINT_ALGORITHM = "sha256-canonical-run-json-v1"
SOURCE_FILE_SHA256_ALGORITHM = "sha256-file-bytes-v1"


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


def _write_text(path: str | Path, value: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value.rstrip() + "\n", encoding="utf-8")


def _pending_query(query_type: str, mode: str, index: int) -> dict[str, str]:
    marker = f"TODO-{index:03d}"
    if mode == "text":
        return {"text": f"{marker}: replace with a real human-authored query"}
    if mode == "image":
        return {"image": f"LOCAL_ONLY/{marker}-query-image.jpg"}
    return {
        "text": f"{marker}: replace with a real human-authored query",
        "image": f"LOCAL_ONLY/{marker}-query-image.jpg",
    }


def _pending_item(
    query_type: str,
    mode: str,
    index: int,
    *,
    id_prefix: str | None = None,
    library_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{id_prefix or query_type}-{index:03d}",
        "query_type": query_type,
        "mode": mode,
        "query": _pending_query(query_type, mode, index),
        "library_scope": library_scope or {"mode": "all_enabled", "library_ids": []},
        "relevant_images": [],
        "annotation": {
            "status": "pending",
            "annotator": None,
            "annotated_at": None,
            "notes": "Replace locally; do not commit private paths or labels.",
        },
    }


def generate_pending_dataset(count: int = 60) -> dict[str, Any]:
    if count < 1:
        raise ValueError("count must be positive")
    weights = {
        "text": 25,
        "image": 10,
        "combined": 10,
        "no-answer": 15,
    }
    quotas = {name: count * weight / 60 for name, weight in weights.items()}
    counts = {name: math.floor(value) for name, value in quotas.items()}
    remaining = count - sum(counts.values())
    allocation_order = sorted(
        weights,
        key=lambda name: (quotas[name] - counts[name], weights[name]),
        reverse=True,
    )
    for name in allocation_order[:remaining]:
        counts[name] += 1
    no_answer_modes = ("text", "image", "combined")
    items = []
    no_answer_index = 0
    index = 1
    for query_type in ("text", "image", "combined", "no-answer"):
        for _offset in range(counts[query_type]):
            if query_type == "no-answer":
                mode = no_answer_modes[no_answer_index % len(no_answer_modes)]
                no_answer_index += 1
            else:
                mode = query_type
            items.append(_pending_item(query_type, mode, index))
            index += 1
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "name": "zvec-search-quality-pending-template",
        "description": (
            "Pending-only template. It contains no human relevance labels and must "
            "not be used as a baseline until annotated locally."
        ),
        "items": items,
    }


def _expand_pending_template(
    template: dict[str, Any], start_index: int
) -> list[dict[str, Any]]:
    query_type = template.get("query_type")
    if query_type not in QUERY_TYPES:
        raise ValueError(f"Invalid pending template query_type: {query_type!r}")
    count = template.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("pending template count must be a positive integer")
    mode = template.get("mode")
    modes = template.get("modes")
    if query_type == "no-answer" and modes is not None:
        if (
            not isinstance(modes, list)
            or not modes
            or any(value not in MODES for value in modes)
        ):
            raise ValueError("no-answer pending template modes must list valid modes")
        template_modes = list(modes)
    else:
        resolved_mode = mode or query_type
        if resolved_mode not in MODES:
            raise ValueError(f"Invalid pending template mode: {resolved_mode!r}")
        template_modes = [resolved_mode]
    scope = template.get("library_scope")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("pending template library_scope must be an object")
    return [
        _pending_item(
            query_type,
            template_modes[offset % len(template_modes)],
            start_index + offset,
            id_prefix=str(template.get("id_prefix") or query_type),
            library_scope=copy.deepcopy(scope),
        )
        for offset in range(count)
    ]


def expand_dataset_items(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = dataset.get("items", [])
    if not isinstance(raw_items, list) or any(
        not isinstance(item, dict) for item in raw_items
    ):
        raise ValueError("dataset items must be an array of objects")
    items = [copy.deepcopy(item) for item in raw_items]
    raw_templates = dataset.get("pending_templates", [])
    if not isinstance(raw_templates, list) or any(
        not isinstance(item, dict) for item in raw_templates
    ):
        raise ValueError("pending_templates must be an array of objects")
    next_index = len(items) + 1
    for template in raw_templates:
        expanded = _expand_pending_template(template, next_index)
        items.extend(expanded)
        next_index += len(expanded)
    return items


def _validate_query(item: dict[str, Any]) -> None:
    item_id = item["id"]
    query_type = item["query_type"]
    mode = item["mode"]
    query = item.get("query")
    if not isinstance(query, dict):
        raise ValueError(f"dataset item {item_id}: query must be an object")
    text = query.get("text")
    image = query.get("image")
    if mode in {"text", "combined"} and (not isinstance(text, str) or not text.strip()):
        raise ValueError(f"dataset item {item_id}: {mode} query requires text")
    if mode in {"image", "combined"} and (
        not isinstance(image, str) or not image.strip()
    ):
        raise ValueError(f"dataset item {item_id}: {mode} query requires image")
    if query_type != "no-answer" and query_type != mode:
        raise ValueError(
            f"dataset item {item_id}: mode must match query_type for answerable cases"
        )


def validate_dataset(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    if dataset.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("dataset schema_version must be 1")
    name = dataset.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("dataset name must be a non-empty string")
    items = expand_dataset_items(dataset)
    identifiers: set[str] = set()
    identity_index = image_identity.ImageIdentityIndex()
    relevant_groups: list[tuple[str, list[Any]]] = []
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("every dataset item needs a non-empty id")
        if item_id in identifiers:
            raise ValueError(f"duplicate dataset item id: {item_id}")
        identifiers.add(item_id)
        query_type = item.get("query_type")
        mode = item.get("mode")
        if query_type not in QUERY_TYPES:
            raise ValueError(f"dataset item {item_id}: invalid query_type")
        if mode not in MODES:
            raise ValueError(f"dataset item {item_id}: invalid mode")
        _validate_query(item)

        scope = item.get("library_scope")
        if not isinstance(scope, dict) or scope.get("mode") not in LIBRARY_SCOPE_MODES:
            raise ValueError(f"dataset item {item_id}: invalid library_scope")
        library_ids = scope.get("library_ids")
        if not isinstance(library_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in library_ids
        ):
            raise ValueError(f"dataset item {item_id}: invalid library_ids")
        if scope["mode"] == "single" and len(library_ids) != 1:
            raise ValueError(f"dataset item {item_id}: single scope needs one library")

        relevant = item.get("relevant_images")
        if not isinstance(relevant, list) or any(
            not isinstance(value, dict) for value in relevant
        ):
            raise ValueError(f"dataset item {item_id}: relevant_images must be objects")
        relevant_references: list[Any] = []
        try:
            for index, value in enumerate(relevant, start=1):
                relevant_references.append(
                    identity_index.add(
                        value,
                        f"dataset item {item_id}: relevant_images[{index}]",
                    )
                )
        except image_identity.ImageIdentityError as exc:
            raise ValueError(str(exc)) from exc
        relevant_groups.append(
            (f"dataset item {item_id}: relevant_images", relevant_references)
        )

        annotation = item.get("annotation")
        if not isinstance(annotation, dict):
            raise ValueError(f"dataset item {item_id}: annotation must be an object")
        status = annotation.get("status")
        if status not in ANNOTATION_STATUSES:
            raise ValueError(f"dataset item {item_id}: invalid annotation status")
        if status == "pending" and relevant:
            raise ValueError(
                f"dataset item {item_id}: pending cases cannot contain relevance labels"
            )
        if query_type == "no-answer" and relevant:
            raise ValueError(
                f"dataset item {item_id}: no-answer cases cannot have relevant images"
            )
        if status != "pending" and query_type != "no-answer" and not relevant:
            raise ValueError(
                f"dataset item {item_id}: annotated answerable case needs "
                "relevance labels"
            )
    try:
        for context, references in relevant_groups:
            identity_index.require_unique(references, context)
    except image_identity.ImageIdentityError as exc:
        raise ValueError(str(exc)) from exc
    return items


def dataset_fingerprint(dataset: dict[str, Any]) -> str:
    """Fingerprint the expanded evaluation corpus, including labels and scopes."""
    items = validate_dataset(dataset)
    canonical = json.dumps(
        {
            "schema_version": dataset["schema_version"],
            "name": dataset["name"],
            "items": items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def query_corpus_fingerprint(dataset: dict[str, Any]) -> str:
    """Fingerprint only executable query identity, excluding all human labels."""
    items = validate_dataset(dataset)
    canonical = json.dumps(
        [
            {
                "id": item["id"],
                "query_type": item["query_type"],
                "mode": item["mode"],
                "query": item["query"],
                "library_scope": item["library_scope"],
            }
            for item in items
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_fingerprint(run: dict[str, Any]) -> str:
    """Bind an evaluation report to the complete parsed run artifact."""
    validate_run(run)
    canonical = json.dumps(
        run,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def source_file_sha256(path: str | Path) -> str:
    """Hash the exact artifact bytes for non-Python release verification."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_run(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if run.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ValueError("run schema_version must be 1")
    name = run.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("run name must be a non-empty string")
    semantics = run.get("score_semantics", {})
    if not isinstance(semantics, dict):
        raise ValueError("score_semantics must be an object")
    for mode, value in semantics.items():
        if mode not in MODES or value not in {"lower_is_better", "higher_is_better"}:
            raise ValueError("score_semantics contains an invalid mode or value")
    score_fields = run.get("score_fields")
    if score_fields is not None:
        if not isinstance(score_fields, dict):
            raise ValueError("score_fields must be an object when supplied")
        if set(score_fields) != MODES or any(
            value not in {"score", "raw_score", "normalized_score", "confidence"}
            for value in score_fields.values()
        ):
            raise ValueError(
                "score_fields must map every query mode to a supported score field"
            )
    raw_cases = run.get("cases")
    if not isinstance(raw_cases, list) or any(
        not isinstance(value, dict) for value in raw_cases
    ):
        raise ValueError("run cases must be an array of objects")
    cases: dict[str, dict[str, Any]] = {}
    identity_index = image_identity.ImageIdentityIndex()
    result_groups: list[tuple[str, list[Any]]] = []
    for case in raw_cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("every run case needs a non-empty id")
        if case_id in cases:
            raise ValueError(f"duplicate run case id: {case_id}")
        latency = case.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            raise ValueError(f"run case {case_id}: latency_ms must be numeric")
        if not math.isfinite(float(latency)) or latency < 0:
            raise ValueError(f"run case {case_id}: latency_ms must be non-negative")
        requests = case.get("api_requests")
        if isinstance(requests, bool) or not isinstance(requests, int) or requests < 0:
            raise ValueError(
                f"run case {case_id}: api_requests must be a non-negative integer"
            )
        results = case.get("results")
        if not isinstance(results, list) or any(
            not isinstance(value, dict) for value in results
        ):
            raise ValueError(f"run case {case_id}: results must be objects")
        result_references: list[Any] = []
        for position, result in enumerate(results, start=1):
            try:
                result_references.append(
                    identity_index.add(
                        result,
                        f"run case {case_id}: results[{position}]",
                    )
                )
            except image_identity.ImageIdentityError as exc:
                raise ValueError(str(exc)) from exc
            rank = result.get("rank")
            if rank is not None and (
                isinstance(rank, bool) or not isinstance(rank, int) or rank != position
            ):
                raise ValueError(
                    f"run case {case_id}: result rank must match displayed order"
                )
        result_groups.append((f"run case {case_id}: results", result_references))
        cases[case_id] = case
    try:
        for context, references in result_groups:
            identity_index.require_unique(references, context)
    except image_identity.ImageIdentityError as exc:
        raise ValueError(str(exc)) from exc
    return cases


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _metric_block(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    precisions: list[float] = []
    strict_precisions: list[float] = []
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    no_answer_correct: list[bool] = []
    result_counts: list[int] = []
    latencies: list[float] = []
    request_counts: list[int] = []
    identity_index = image_identity.ImageIdentityIndex()
    identity_pairs: list[
        tuple[
            dict[str, Any],
            dict[str, Any],
            list[Any],
            list[Any],
        ]
    ] = []
    try:
        for item, case in pairs:
            item_id = str(item["id"])
            relevant_references = [
                identity_index.add(
                    value,
                    f"dataset item {item_id}: relevant_images[{index}]",
                )
                for index, value in enumerate(item["relevant_images"], start=1)
            ]
            result_references = [
                identity_index.add(
                    value,
                    f"run case {item_id}: results[{index}]",
                )
                for index, value in enumerate(case["results"], start=1)
            ]
            identity_pairs.append((item, case, relevant_references, result_references))
    except image_identity.ImageIdentityError as exc:
        raise ValueError(str(exc)) from exc

    for item, case, relevant_references, result_references in identity_pairs:
        results = case["results"]
        result_counts.append(len(results))
        latencies.append(float(case["latency_ms"]))
        request_counts.append(int(case["api_requests"]))
        if item["query_type"] == "no-answer":
            no_answer_correct.append(not results)
            continue
        relevant = {identity_index.key(value) for value in relevant_references}
        top = [identity_index.key(value) for value in result_references[:TOP_K]]
        matched = sum(identity in relevant for identity in top)
        precisions.append(matched / len(top) if top else 0.0)
        strict_precisions.append(matched / TOP_K)
        recalls.append(matched / len(relevant))
        reciprocal_ranks.append(
            next(
                (
                    1.0 / rank
                    for rank, identity in enumerate(top, start=1)
                    if identity in relevant
                ),
                0.0,
            )
        )
    no_answer_accuracy = (
        sum(no_answer_correct) / len(no_answer_correct) if no_answer_correct else None
    )
    return {
        "answerable_cases": len(precisions),
        "no_answer_cases": len(no_answer_correct),
        "precision_at_5": fmean(precisions) if precisions else None,
        "strict_precision_at_5": (
            fmean(strict_precisions) if strict_precisions else None
        ),
        "recall_at_5": fmean(recalls) if recalls else None,
        "mrr": fmean(reciprocal_ranks) if reciprocal_ranks else None,
        "no_answer_accuracy": no_answer_accuracy,
        "no_answer_false_return_rate": (
            1.0 - no_answer_accuracy if no_answer_accuracy is not None else None
        ),
        "average_result_count": fmean(result_counts) if result_counts else None,
        "latency_ms": {
            "average": fmean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "api_requests": {
            "total": sum(request_counts),
            "average": fmean(request_counts) if request_counts else None,
        },
    }


def _selected_pairs(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    allow_synthetic: bool,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    items = validate_dataset(dataset)
    cases = validate_run(run)
    synthetic = [
        item for item in items if item["annotation"]["status"] == "synthetic_fixture"
    ]
    if synthetic and not allow_synthetic:
        raise ValueError(
            "Synthetic fixture labels require allow_synthetic=True or "
            "--allow-synthetic."
        )
    selected = [
        item
        for item in items
        if item["annotation"]["status"] == "human_verified"
        or (allow_synthetic and item["annotation"]["status"] == "synthetic_fixture")
    ]
    if not selected:
        raise ValueError("Dataset has no eligible annotated cases to evaluate.")
    missing = [item["id"] for item in selected if item["id"] not in cases]
    if missing:
        raise ValueError(f"Run is missing evaluated cases: {', '.join(missing)}")
    known_ids = {item["id"] for item in items}
    unknown = sorted(set(cases) - known_ids)
    if unknown:
        raise ValueError(f"Run contains unknown cases: {', '.join(unknown)}")
    coverage = {
        "total": len(items),
        "evaluated": len(selected),
        "pending": sum(item["annotation"]["status"] == "pending" for item in items),
        "human_verified": sum(
            item["annotation"]["status"] == "human_verified" for item in items
        ),
        "synthetic": len(synthetic),
    }
    return [(item, cases[item["id"]]) for item in selected], coverage


def evaluate_dataset(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    allow_synthetic: bool = False,
    dataset_source: str | None = None,
    run_source: str | None = None,
    run_source_sha256: str | None = None,
) -> dict[str, Any]:
    pairs, coverage = _selected_pairs(dataset, run, allow_synthetic=allow_synthetic)
    by_mode = {
        mode: _metric_block(
            [(item, case) for item, case in pairs if item["mode"] == mode]
        )
        for mode in sorted(MODES)
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "zvec-search-quality-evaluation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "name": dataset["name"],
            "source": dataset_source,
            "fingerprint": dataset_fingerprint(dataset),
            "fingerprint_algorithm": DATASET_FINGERPRINT_ALGORITHM,
        },
        "run": {
            "name": run["name"],
            "source": run_source,
            "fingerprint": run_fingerprint(run),
            "fingerprint_algorithm": RUN_FINGERPRINT_ALGORITHM,
            "source_sha256": run_source_sha256,
            "source_sha256_algorithm": (
                SOURCE_FILE_SHA256_ALGORITHM if run_source_sha256 is not None else None
            ),
        },
        "coverage": coverage,
        "metric_notes": {
            "precision_at_5": (
                "Legacy selective precision: relevant returned items divided by the "
                "number actually returned in the first five; answerable cases only. "
                "Use strict_precision_at_5 for standard fixed-denominator P@5."
            ),
            "strict_precision_at_5": (
                "Standard Precision@5: relevant items in the first five divided by "
                "five; missing result slots count as non-relevant. Answerable cases "
                "only."
            ),
            "recall_at_5": (
                "Relevant items found in the first five; answerable cases only."
            ),
            "mrr": (
                "MRR@5: reciprocal rank of the first relevant item in the first "
                "five; answerable cases only."
            ),
            "no_answer": "Any returned item is a false return for a no-answer case.",
            "image_identity": (
                "SHA-256 identifies content globally across Collections when "
                "available; legacy records without a digest fall back to image_id."
            ),
        },
        "metrics": _metric_block(pairs),
        "by_mode": by_mode,
        "collection_fairness": collection_fairness.evaluate_collection_fairness(pairs),
    }


def evaluate_files(
    dataset_path: str | Path,
    run_path: str | Path,
    *,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    resolved_run_path = Path(run_path)
    return evaluate_dataset(
        _read_json(dataset_path),
        _read_json(resolved_run_path),
        allow_synthetic=allow_synthetic,
        dataset_source=str(Path(dataset_path)),
        run_source=str(resolved_run_path),
        run_source_sha256=source_file_sha256(resolved_run_path),
    )


COMPARISON_METRICS = {
    "precision_at_5": (
        "Returned precision (legacy precision_at_5)",
        ("precision_at_5",),
    ),
    "strict_precision_at_5": (
        "Strict Precision@5",
        ("strict_precision_at_5",),
    ),
    "recall_at_5": ("Recall@5", ("recall_at_5",)),
    "mrr": ("MRR@5", ("mrr",)),
    "no_answer_accuracy": ("No-answer accuracy", ("no_answer_accuracy",)),
    "no_answer_false_return_rate": (
        "No-answer false-return rate",
        ("no_answer_false_return_rate",),
    ),
    "average_result_count": ("Average result count", ("average_result_count",)),
    "average_latency_ms": ("Average latency (ms)", ("latency_ms", "average")),
    "p95_latency_ms": ("P95 latency (ms)", ("latency_ms", "p95")),
    "total_api_requests": ("Total API requests", ("api_requests", "total")),
    "average_api_requests": (
        "Average API requests",
        ("api_requests", "average"),
    ),
}


def _nested_metric(value: dict[str, Any], path: tuple[str, ...]) -> float | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise ValueError(f"Metric {'.'.join(path)} must be numeric or null")
    return float(current)


def _comparison_block(
    before_metrics: dict[str, Any], after_metrics: dict[str, Any]
) -> dict[str, Any]:
    values = {}
    for key, (label, path) in COMPARISON_METRICS.items():
        before = _nested_metric(before_metrics, path)
        after = _nested_metric(after_metrics, path)
        values[key] = {
            "label": label,
            "before": before,
            "after": after,
            "delta": after - before
            if before is not None and after is not None
            else None,
        }
    return values


def _collection_fairness_comparison(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    before_block = before.get("collection_fairness")
    after_block = after.get("collection_fairness")
    before_metrics = (
        before_block.get("metrics", {}) if isinstance(before_block, dict) else {}
    )
    after_metrics = (
        after_block.get("metrics", {}) if isinstance(after_block, dict) else {}
    )
    metric_paths = {
        "cross_collection_pairwise_accuracy": "Cross-Collection pairwise accuracy",
        "cross_collection_inversion_rate": "Cross-Collection inversion rate",
        "worst_directional_pairwise_accuracy": ("Worst directional pairwise accuracy"),
    }
    metrics: dict[str, Any] = {}
    for key, label in metric_paths.items():
        before_value = _nested_metric(before_metrics, (key,))
        after_value = _nested_metric(after_metrics, (key,))
        metrics[key] = {
            "label": label,
            "before": before_value,
            "after": after_value,
            "delta": (
                after_value - before_value
                if before_value is not None and after_value is not None
                else None
            ),
        }
    return {
        "before_status": (
            before_block.get("status")
            if isinstance(before_block, dict)
            else "legacy_not_measured"
        ),
        "after_status": (
            after_block.get("status")
            if isinstance(after_block, dict)
            else "legacy_not_measured"
        ),
        "metrics": metrics,
    }


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    for label, report in (("before", before), ("after", after)):
        if (
            report.get("schema_version") != REPORT_SCHEMA_VERSION
            or report.get("kind") != "zvec-search-quality-evaluation"
        ):
            raise ValueError(f"{label} is not a search-quality evaluation report")
        if not isinstance(report.get("metrics"), dict):
            raise ValueError(f"{label} report has no metrics")
    before_dataset = before.get("dataset")
    after_dataset = after.get("dataset")
    if not isinstance(before_dataset, dict) or not isinstance(after_dataset, dict):
        raise ValueError("evaluation reports must identify their dataset")
    if before_dataset.get("name") != after_dataset.get("name"):
        raise ValueError("before and after reports use different datasets")
    before_fingerprint = before_dataset.get("fingerprint")
    after_fingerprint = after_dataset.get("fingerprint")
    if (before_fingerprint is None) != (after_fingerprint is None):
        raise ValueError("only one evaluation report has a dataset fingerprint")
    if before_fingerprint is not None and before_fingerprint != after_fingerprint:
        raise ValueError("before and after reports have different dataset fingerprints")
    before_coverage = before.get("coverage")
    after_coverage = after.get("coverage")
    if not isinstance(before_coverage, dict) or before_coverage != after_coverage:
        raise ValueError("before and after reports have different evaluation coverage")
    before_modes = before.get("by_mode", {})
    after_modes = after.get("by_mode", {})
    modes = sorted(set(before_modes) & set(after_modes))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "zvec-search-quality-comparison",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": copy.deepcopy(before_dataset),
        "before": before["run"],
        "after": after["run"],
        "metrics": _comparison_block(before["metrics"], after["metrics"]),
        "by_mode": {
            mode: _comparison_block(before_modes[mode], after_modes[mode])
            for mode in modes
        },
        "collection_fairness": _collection_fairness_comparison(before, after),
    }


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_evaluation_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    rows = [
        ("Strict Precision@5", metrics["strict_precision_at_5"]),
        ("Returned precision (legacy precision_at_5)", metrics["precision_at_5"]),
        ("Recall@5", metrics["recall_at_5"]),
        ("MRR@5", metrics["mrr"]),
        ("No-answer accuracy", metrics["no_answer_accuracy"]),
        ("No-answer false-return rate", metrics["no_answer_false_return_rate"]),
        ("Average result count", metrics["average_result_count"]),
        ("Average latency (ms)", metrics["latency_ms"]["average"]),
        ("P95 latency (ms)", metrics["latency_ms"]["p95"]),
        ("Total API requests", metrics["api_requests"]["total"]),
    ]
    lines = [
        "# Search quality evaluation",
        "",
        f"- Dataset: `{report['dataset']['name']}`",
        f"- Run: `{report['run']['name']}`",
        f"- Evaluated cases: {report['coverage']['evaluated']}",
        f"- Pending cases: {report['coverage']['pending']}",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines.extend(f"| {label} | {_format_metric(value)} |" for label, value in rows)
    fairness = report.get("collection_fairness")
    if isinstance(fairness, dict):
        fairness_metrics = fairness.get("metrics", {})
        fairness_coverage = fairness.get("coverage", {})
        pairwise_accuracy = _format_metric(
            fairness_metrics.get("cross_collection_pairwise_accuracy")
        )
        inversion_rate = _format_metric(
            fairness_metrics.get("cross_collection_inversion_rate")
        )
        worst_direction = _format_metric(
            fairness_metrics.get("worst_directional_pairwise_accuracy")
        )
        lines.extend(
            [
                "",
                "## Cross-Collection fairness",
                "",
                f"- Status: `{fairness.get('status', 'not_measured')}`",
                "- Assessable multi-Collection cases: "
                f"{fairness_coverage.get('assessable_cases', 0)}",
                "- Collections with human relevance labels: "
                f"{fairness_coverage.get('relevant_label_collection_count', 0)}",
                "- Cross-Collection comparisons: "
                f"{fairness_coverage.get('cross_collection_comparisons', 0)}",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Pairwise accuracy | {pairwise_accuracy} |",
                f"| Inversion rate | {inversion_rate} |",
                f"| Worst directional accuracy | {worst_direction} |",
            ]
        )
    return "\n".join(lines)


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "# Search quality comparison",
        "",
        f"- Before: `{comparison['before']['name']}`",
        f"- After: `{comparison['after']['name']}`",
        "",
        "| Metric | Before | After | Delta |",
        "|---|---:|---:|---:|",
    ]
    for value in comparison["metrics"].values():
        lines.append(
            f"| {value['label']} | {_format_metric(value['before'])} | "
            f"{_format_metric(value['after'])} | {_format_metric(value['delta'])} |"
        )
    fairness = comparison.get("collection_fairness")
    if isinstance(fairness, dict):
        lines.extend(
            [
                "",
                "## Cross-Collection fairness",
                "",
                f"- Before status: `{fairness.get('before_status')}`",
                f"- After status: `{fairness.get('after_status')}`",
                "",
                "| Metric | Before | After | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for value in fairness.get("metrics", {}).values():
            lines.append(
                f"| {value['label']} | {_format_metric(value['before'])} | "
                f"{_format_metric(value['after'])} | "
                f"{_format_metric(value['delta'])} |"
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline search-quality evaluator")
    commands = parser.add_subparsers(dest="command", required=True)

    evaluate_command = commands.add_parser("evaluate", help="Evaluate one run")
    evaluate_command.add_argument("--dataset", required=True)
    evaluate_command.add_argument("--run", required=True)
    evaluate_command.add_argument("--output", required=True)
    evaluate_command.add_argument("--markdown")
    evaluate_command.add_argument("--allow-synthetic", action="store_true")

    compare_command = commands.add_parser("compare", help="Compare two reports")
    compare_command.add_argument("--before", required=True)
    compare_command.add_argument("--after", required=True)
    compare_command.add_argument("--output", required=True)
    compare_command.add_argument("--markdown")

    template_command = commands.add_parser(
        "template", help="Generate an explicit pending-only dataset template"
    )
    template_command.add_argument("--count", type=int, default=60)
    template_command.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "template":
        _write_json(args.output, generate_pending_dataset(args.count))
        return 0
    if args.command == "evaluate":
        report = evaluate_files(
            args.dataset,
            args.run,
            allow_synthetic=args.allow_synthetic,
        )
        _write_json(args.output, report)
        if args.markdown:
            _write_text(args.markdown, render_evaluation_markdown(report))
        return 0
    before = _read_json(args.before)
    after = _read_json(args.after)
    comparison = compare_reports(before, after)
    _write_json(args.output, comparison)
    if args.markdown:
        _write_text(args.markdown, render_comparison_markdown(comparison))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
