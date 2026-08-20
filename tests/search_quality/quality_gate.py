from __future__ import annotations

import argparse
import copy
import importlib
from pathlib import Path
from typing import Any

evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)
dataset_split = importlib.import_module(
    "tests.search_quality.split" if __package__ else "split"
)

SCHEMA_VERSION = 1
MAX_NO_ANSWER_FALSE_RETURN_RATE = 0.10
MIN_PRECISION_AT_5_GAIN = 0.15
MAX_RECALL_AT_5_DROP = 0.03
MAX_P95_LATENCY_INCREASE_RATIO = 0.20
MAX_API_REQUEST_INCREASE = 0
MIN_CROSS_COLLECTION_PAIRWISE_ACCURACY = 0.80
MIN_WORST_DIRECTIONAL_PAIRWISE_ACCURACY = 0.50
EPSILON = 1e-12


def _required_metric(
    metrics: dict[str, Any], path: tuple[str, ...], label: str
) -> float:
    current: Any = metrics
    for key in path:
        if not isinstance(current, dict):
            raise ValueError(f"Cannot evaluate {label}: metric is missing")
        current = current.get(key)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise ValueError(f"Cannot evaluate {label}: metric is missing or non-numeric")
    return float(current)


def validate_gate_dataset(
    dataset: dict[str, Any], *, allow_test_fixtures: bool = False
) -> list[dict[str, Any]]:
    items = evaluate.validate_dataset(dataset)
    non_human = [
        item
        for item in items
        if item["annotation"]["status"] in {"pending", "synthetic_fixture"}
    ]
    if non_human and not allow_test_fixtures:
        counts = {
            status: sum(item["annotation"]["status"] == status for item in non_human)
            for status in ("pending", "synthetic_fixture")
        }
        details = ", ".join(
            f"{status}={count}" for status, count in counts.items() if count
        )
        raise ValueError(
            "Quality gate requires a fully human-verified dataset; "
            f"found {details}. --allow-test-fixtures is for deterministic tests only."
        )
    return items


def _check(
    *,
    label: str,
    actual: float | None,
    threshold: float,
    comparator: str,
    passed: bool,
    before: float | None = None,
    after: float | None = None,
    note: str | None = None,
    metric: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "label": label,
        "passed": passed,
        "actual": actual,
        "comparator": comparator,
        "threshold": threshold,
    }
    if before is not None:
        value["before"] = before
    if after is not None:
        value["after"] = after
    if note is not None:
        value["note"] = note
    if metric is not None:
        value["metric"] = metric
    return value


def _require_dataset_fingerprint(report: dict[str, Any], label: str) -> str:
    identity = report.get("dataset")
    if not isinstance(identity, dict):
        raise ValueError(f"Cannot evaluate gate: {label} dataset identity is missing")
    fingerprint = identity.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(
            f"Cannot evaluate gate: {label} dataset fingerprint is missing"
        )
    if identity.get("fingerprint_algorithm") != evaluate.DATASET_FINGERPRINT_ALGORITHM:
        raise ValueError(
            f"Cannot evaluate gate: {label} dataset fingerprint algorithm is invalid"
        )
    return fingerprint


def _fairness_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fairness_scope_signature(
    report: dict[str, Any],
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    fairness = report.get("collection_fairness")
    if not isinstance(fairness, dict):
        return None
    cases = fairness.get("cases")
    if not isinstance(cases, list):
        return None
    signature: list[tuple[str, tuple[str, ...]]] = []
    for case in cases:
        if not isinstance(case, dict):
            return None
        case_id = case.get("id")
        scope_ids = case.get("scope_library_ids")
        if not isinstance(case_id, str) or not case_id.strip():
            return None
        if not isinstance(scope_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in scope_ids
        ):
            return None
        normalized = tuple(sorted(value.strip() for value in scope_ids))
        if len(normalized) < 2 or len(normalized) != len(set(normalized)):
            return None
        signature.append((case_id.strip(), normalized))
    return tuple(sorted(signature))


def _collection_fairness_checks(
    before_report: dict[str, Any], after_report: dict[str, Any]
) -> dict[str, Any]:
    fairness = after_report.get("collection_fairness")
    if not isinstance(fairness, dict):
        fairness = {}
    coverage = fairness.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    metrics = fairness.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}

    status = fairness.get("status")
    assessable_cases = _fairness_number(coverage.get("assessable_cases"))
    collection_count = _fairness_number(coverage.get("collection_count"))
    labelled_collection_count = _fairness_number(
        coverage.get("relevant_label_collection_count")
    )
    evidenced_labelled_collection_count = _fairness_number(
        coverage.get("evidenced_relevant_label_collection_count")
    )
    unevidenced_labelled_libraries = coverage.get(
        "unevidenced_relevant_label_library_ids"
    )
    evidence_complete = (
        isinstance(unevidenced_labelled_libraries, list)
        and not unevidenced_labelled_libraries
        and evidenced_labelled_collection_count is not None
        and labelled_collection_count is not None
        and evidenced_labelled_collection_count == labelled_collection_count
    )
    comparisons = _fairness_number(coverage.get("cross_collection_comparisons"))
    measured = (
        status == "measured"
        and assessable_cases is not None
        and assessable_cases >= 1
        and collection_count is not None
        and collection_count >= 2
        and labelled_collection_count is not None
        and labelled_collection_count >= 2
        and evidence_complete
    )
    collection_count_text = int(collection_count) if collection_count is not None else 0
    assessable_cases_text = int(assessable_cases) if assessable_cases is not None else 0
    labelled_collection_count_text = (
        int(labelled_collection_count) if labelled_collection_count is not None else 0
    )
    evidenced_labelled_collection_count_text = (
        int(evidenced_labelled_collection_count)
        if evidenced_labelled_collection_count is not None
        else 0
    )
    coverage_note = (
        f"status={status or 'legacy_not_measured'}, "
        f"Collections={collection_count_text}, "
        f"labelled_Collections={labelled_collection_count_text}, "
        f"evidenced_labelled_Collections={evidenced_labelled_collection_count_text}, "
        f"unevidenced={unevidenced_labelled_libraries or []}, "
        f"assessable_cases={assessable_cases_text}. "
        "Formal acceptance needs human relevance labels from at least two "
        "Collections in answerable cross-Collection cases."
    )

    pairwise_accuracy = _fairness_number(
        metrics.get("cross_collection_pairwise_accuracy")
    )
    worst_direction = _fairness_number(
        metrics.get("worst_directional_pairwise_accuracy")
    )
    worst_note = None
    if measured and worst_direction is None and comparisons == 0:
        # Filtering can leave only relevant hits from multiple Collections. There
        # is then positive cross-Collection evidence and no erroneous direction.
        worst_direction = 1.0
        worst_note = (
            "No returned non-relevant cross-Collection pair remained; relevant "
            "hits from at least two Collections provide positive evidence."
        )

    before_scope = _fairness_scope_signature(before_report)
    after_scope = _fairness_scope_signature(after_report)
    scope_consistent = (
        before_scope is not None
        and after_scope is not None
        and before_scope == after_scope
    )

    return {
        "cross_collection_scope_consistency": _check(
            label="Cross-Collection scope consistency",
            actual=1.0 if scope_consistent else 0.0,
            threshold=1.0,
            comparator="==",
            passed=scope_consistent,
            note=(
                "Before and after runs must evaluate the same Collection ids for "
                "every multi-Collection answerable case."
            ),
            metric="collection_fairness.cases.scope_library_ids",
        ),
        "cross_collection_coverage": _check(
            label="Cross-Collection human-label coverage",
            actual=assessable_cases,
            threshold=1.0,
            comparator=">=",
            passed=measured,
            note=coverage_note,
            metric="collection_fairness.coverage.assessable_cases",
        ),
        "cross_collection_pairwise_accuracy": _check(
            label="Cross-Collection pairwise accuracy",
            actual=pairwise_accuracy,
            threshold=MIN_CROSS_COLLECTION_PAIRWISE_ACCURACY,
            comparator=">=",
            passed=(
                measured
                and pairwise_accuracy is not None
                and pairwise_accuracy + EPSILON
                >= MIN_CROSS_COLLECTION_PAIRWISE_ACCURACY
            ),
            after=pairwise_accuracy,
            note=(
                "At most 20% of relevance-conditioned cross-Collection orderings "
                "may be inverted; no Collection-size prior is used."
            ),
            metric=("collection_fairness.metrics.cross_collection_pairwise_accuracy"),
        ),
        "worst_directional_pairwise_accuracy": _check(
            label="Worst directional Collection-pair accuracy",
            actual=worst_direction,
            threshold=MIN_WORST_DIRECTIONAL_PAIRWISE_ACCURACY,
            comparator=">=",
            passed=(
                measured
                and worst_direction is not None
                and worst_direction + EPSILON >= MIN_WORST_DIRECTIONAL_PAIRWISE_ACCURACY
            ),
            after=worst_direction,
            note=worst_note
            or (
                "No ordered Collection pair may rank relevant images below "
                "cross-Collection noise more often than above it."
            ),
            metric=("collection_fairness.metrics.worst_directional_pairwise_accuracy"),
        ),
    }


def assess_quality_gate(
    before_report: dict[str, Any],
    after_report: dict[str, Any],
    *,
    enforce_collection_fairness: bool = True,
) -> dict[str, Any]:
    # This also validates that both inputs are completed evaluation reports.
    evaluate.compare_reports(before_report, after_report)
    before_fingerprint = _require_dataset_fingerprint(before_report, "before")
    after_fingerprint = _require_dataset_fingerprint(after_report, "after")
    if before_fingerprint != after_fingerprint:
        raise ValueError("Cannot evaluate gate: dataset fingerprints do not match")
    before_metrics = before_report["metrics"]
    after_metrics = after_report["metrics"]

    no_answer_rate = _required_metric(
        after_metrics,
        ("no_answer_false_return_rate",),
        "no-answer false-return rate",
    )
    before_precision = _required_metric(
        before_metrics,
        ("strict_precision_at_5",),
        "before standard Precision@5",
    )
    after_precision = _required_metric(
        after_metrics,
        ("strict_precision_at_5",),
        "after standard Precision@5",
    )
    precision_gain = after_precision - before_precision
    before_recall = _required_metric(
        before_metrics, ("recall_at_5",), "before Recall@5"
    )
    after_recall = _required_metric(after_metrics, ("recall_at_5",), "after Recall@5")
    recall_drop = max(0.0, before_recall - after_recall)
    before_p95 = _required_metric(
        before_metrics, ("latency_ms", "p95"), "before P95 latency"
    )
    after_p95 = _required_metric(
        after_metrics, ("latency_ms", "p95"), "after P95 latency"
    )
    if before_p95 == 0:
        latency_ratio = 0.0 if after_p95 == 0 else None
        latency_passed = after_p95 == 0
        latency_note = (
            None
            if after_p95 == 0
            else "Increase ratio is undefined because the baseline P95 is zero."
        )
    else:
        latency_ratio = (after_p95 - before_p95) / before_p95
        latency_passed = latency_ratio <= MAX_P95_LATENCY_INCREASE_RATIO + EPSILON
        latency_note = None
    before_requests = _required_metric(
        before_metrics, ("api_requests", "total"), "before API requests"
    )
    after_requests = _required_metric(
        after_metrics, ("api_requests", "total"), "after API requests"
    )
    request_increase = after_requests - before_requests

    checks = {
        "no_answer_false_return_rate": _check(
            label="No-answer false-return rate",
            actual=no_answer_rate,
            threshold=MAX_NO_ANSWER_FALSE_RETURN_RATE,
            comparator="<",
            passed=no_answer_rate < MAX_NO_ANSWER_FALSE_RETURN_RATE,
            after=no_answer_rate,
        ),
        "precision_at_5_gain": _check(
            label="Standard Precision@5 improvement",
            actual=precision_gain,
            threshold=MIN_PRECISION_AT_5_GAIN,
            comparator=">=",
            passed=precision_gain + EPSILON >= MIN_PRECISION_AT_5_GAIN,
            before=before_precision,
            after=after_precision,
            metric="strict_precision_at_5",
        ),
        "recall_at_5_drop": _check(
            label="Recall@5 drop",
            actual=recall_drop,
            threshold=MAX_RECALL_AT_5_DROP,
            comparator="<=",
            passed=recall_drop <= MAX_RECALL_AT_5_DROP + EPSILON,
            before=before_recall,
            after=after_recall,
        ),
        "p95_latency_increase_ratio": _check(
            label="P95 latency increase",
            actual=latency_ratio,
            threshold=MAX_P95_LATENCY_INCREASE_RATIO,
            comparator="<=",
            passed=latency_passed,
            before=before_p95,
            after=after_p95,
            note=latency_note,
        ),
        "api_request_increase": _check(
            label="Additional API requests",
            actual=request_increase,
            threshold=float(MAX_API_REQUEST_INCREASE),
            comparator="<=",
            passed=request_increase <= MAX_API_REQUEST_INCREASE,
            before=before_requests,
            after=after_requests,
            note="Uses total requests over the same evaluated cases.",
        ),
    }
    if enforce_collection_fairness:
        checks.update(_collection_fairness_checks(before_report, after_report))
    passed = all(check["passed"] for check in checks.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "zvec-search-quality-acceptance-gate",
        "status": "pass" if passed else "fail",
        "passed": passed,
        "collection_fairness_enforced": enforce_collection_fairness,
        "checks": checks,
    }


def render_gate_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        evaluate.render_comparison_markdown(comparison),
        "",
    ]
    holdout = comparison.get("holdout_split")
    if isinstance(holdout, dict):
        lines.extend(_holdout_markdown(holdout))
        lines.append("")
    lines.extend(["## Acceptance gate", ""])
    gate = comparison["quality_gate"]
    lines.extend(
        [
            f"**Overall: {str(gate['status']).upper()}**",
            "",
            "| Check | Result | Actual | Requirement |",
            "|---|---|---:|---:|",
        ]
    )
    for check in gate["checks"].values():
        result = "PASS" if check["passed"] else "FAIL"
        actual = evaluate._format_metric(check["actual"])
        requirement = f"{check['comparator']} {check['threshold']}"
        lines.append(f"| {check['label']} | {result} | {actual} | {requirement} |")
    return "\n".join(lines)


def _holdout_markdown(holdout: dict[str, Any]) -> list[str]:
    return [
        "## Validation holdout",
        "",
        f"- Role: `{holdout.get('role')}`",
        f"- Manifest: `{holdout.get('manifest_source')}`",
        f"- Source dataset: `{holdout.get('source_dataset_source')}`",
        f"- Validation dataset: `{holdout.get('validation_dataset_source')}`",
        f"- Manifest fingerprint: `{holdout.get('manifest_fingerprint')}`",
        f"- Source dataset fingerprint: `{holdout.get('source_dataset_fingerprint')}`",
        "- Validation dataset fingerprint: "
        f"`{holdout.get('validation_dataset_fingerprint')}`",
        f"- Validation cases: {holdout.get('validation_case_count')}",
    ]


def _render_evaluation_with_holdout(report: dict[str, Any]) -> str:
    rendered = evaluate.render_evaluation_markdown(report)
    holdout = report.get("holdout_split")
    if not isinstance(holdout, dict):
        return rendered
    return rendered + "\n\n" + "\n".join(_holdout_markdown(holdout))


def _validation_provenance(
    manifest: dict[str, Any],
    *,
    manifest_source: str | Path,
    source_dataset_source: str | Path,
    validation_dataset_source: str | Path,
    before_run_source: str | Path,
    after_run_source: str | Path,
) -> dict[str, Any]:
    provenance = dataset_split.validation_provenance(manifest)
    provenance.update(
        {
            "manifest_source": str(Path(manifest_source)),
            "input_dataset_source": str(Path(source_dataset_source)),
            "source_dataset_source": str(Path(source_dataset_source)),
            "source_dataset_case_count": manifest["dataset"]["case_count"],
            "validation_dataset_source": str(Path(validation_dataset_source)),
            "before_run_source": str(Path(before_run_source)),
            "after_run_source": str(Path(after_run_source)),
        }
    )
    return provenance


def run_quality_gate(
    dataset_path: str | Path,
    before_run_path: str | Path,
    after_run_path: str | Path,
    output_dir: str | Path,
    *,
    allow_test_fixtures: bool = False,
    split_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    dataset = evaluate._read_json(dataset_path)
    before_run = evaluate._read_json(before_run_path)
    after_run = evaluate._read_json(after_run_path)
    destination = Path(output_dir)
    validation_dataset_path = destination / "validation-dataset.json"
    holdout: dict[str, Any] | None = None
    if split_manifest_path is None:
        if not allow_test_fixtures:
            raise ValueError(
                "Formal quality gate requires --split-manifest and evaluates only "
                "the frozen validation role."
            )
    else:
        manifest = evaluate._read_json(split_manifest_path)
        if not allow_test_fixtures:
            source_identity = manifest.get("dataset")
            if not isinstance(source_identity, dict):
                raise ValueError("split manifest source dataset identity is missing")
            if dataset.get("name") != source_identity.get(
                "name"
            ) or evaluate.dataset_fingerprint(dataset) != source_identity.get(
                "fingerprint"
            ):
                raise ValueError(
                    "Formal quality gate requires the complete source dataset "
                    "named by the split manifest."
                )
        before_dataset, before_run = dataset_split.prepare_validation_inputs(
            dataset,
            before_run,
            manifest,
            allow_synthetic=allow_test_fixtures,
        )
        after_dataset, after_run = dataset_split.prepare_validation_inputs(
            dataset,
            after_run,
            manifest,
            allow_synthetic=allow_test_fixtures,
        )
        if evaluate.dataset_fingerprint(before_dataset) != evaluate.dataset_fingerprint(
            after_dataset
        ):
            raise ValueError("before and after validation datasets do not match")
        dataset = before_dataset
        holdout = _validation_provenance(
            manifest,
            manifest_source=split_manifest_path,
            source_dataset_source=dataset_path,
            validation_dataset_source=validation_dataset_path,
            before_run_source=before_run_path,
            after_run_source=after_run_path,
        )
        evaluated_fingerprint = evaluate.dataset_fingerprint(dataset)
        if evaluated_fingerprint != holdout["validation_dataset_fingerprint"]:
            raise ValueError("evaluated validation dataset fingerprint does not match")
        holdout["evaluated_dataset_fingerprint"] = evaluated_fingerprint

    validate_gate_dataset(dataset, allow_test_fixtures=allow_test_fixtures)
    if holdout is not None:
        evaluate._write_json(validation_dataset_path, dataset)
    evaluation_dataset_source = (
        str(validation_dataset_path) if holdout is not None else str(Path(dataset_path))
    )
    before_report = evaluate.evaluate_dataset(
        dataset,
        before_run,
        allow_synthetic=allow_test_fixtures,
        dataset_source=evaluation_dataset_source,
        run_source=str(Path(before_run_path)),
        run_source_sha256=evaluate.source_file_sha256(before_run_path),
    )
    after_report = evaluate.evaluate_dataset(
        dataset,
        after_run,
        allow_synthetic=allow_test_fixtures,
        dataset_source=evaluation_dataset_source,
        run_source=str(Path(after_run_path)),
        run_source_sha256=evaluate.source_file_sha256(after_run_path),
    )
    if holdout is not None:
        before_report["holdout_split"] = copy.deepcopy(holdout)
        after_report["holdout_split"] = copy.deepcopy(holdout)
    comparison = evaluate.compare_reports(before_report, after_report)
    if holdout is not None:
        comparison["holdout_split"] = copy.deepcopy(holdout)
    comparison["quality_gate"] = assess_quality_gate(
        before_report,
        after_report,
        enforce_collection_fairness=not allow_test_fixtures,
    )

    evaluate._write_json(destination / "before-evaluation.json", before_report)
    evaluate._write_text(
        destination / "before-evaluation.md",
        _render_evaluation_with_holdout(before_report),
    )
    evaluate._write_json(destination / "after-evaluation.json", after_report)
    evaluate._write_text(
        destination / "after-evaluation.md",
        _render_evaluation_with_holdout(after_report),
    )
    evaluate._write_json(destination / "comparison.json", comparison)
    evaluate._write_text(
        destination / "comparison.md", render_gate_markdown(comparison)
    )
    return comparison


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate before/after search-quality reports and enforce the MVP gate"
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--before-run", required=True)
    parser.add_argument("--after-run", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--split-manifest")
    parser.add_argument(
        "--allow-test-fixtures",
        action="store_true",
        help="TEST ONLY: allow pending or synthetic_fixture annotations",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    comparison = run_quality_gate(
        args.dataset,
        args.before_run,
        args.after_run,
        args.output_dir,
        allow_test_fixtures=args.allow_test_fixtures,
        split_manifest_path=args.split_manifest,
    )
    gate = comparison["quality_gate"]
    print(f"Search quality gate: {str(gate['status']).upper()}")
    print(f"Report: {Path(args.output_dir) / 'comparison.md'}")
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
