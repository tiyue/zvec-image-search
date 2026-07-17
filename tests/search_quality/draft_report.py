from __future__ import annotations

import argparse
import copy
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import evaluate
except ImportError:  # pragma: no cover - direct script execution
    import evaluate  # type: ignore[no-redef]

DRAFT_REPORT_SCHEMA_VERSION = 1
LABEL_SOURCE = "ai_assisted_suggestions"
DEFAULT_MAX_FALSE_RETURN_RATE = 0.10
DEFAULT_MAX_RECALL_DROP = 0.03
DEFAULT_MAX_REPORTED_CANDIDATES = 10
SCORE_FIELDS = ("confidence", "raw_score")


def _suggested_images(item: dict[str, Any]) -> list[dict[str, str]]:
    item_id = str(item["id"])
    values = item.get("suggested_relevant_images")
    if not isinstance(values, list) or any(
        not isinstance(value, dict) for value in values
    ):
        raise ValueError(
            f"dataset item {item_id}: suggested_relevant_images must be an array "
            "of objects"
        )
    suggestions: list[dict[str, str]] = []
    for value in values:
        image_id = value.get("image_id")
        if not isinstance(image_id, str) or not image_id.strip():
            raise ValueError(
                f"dataset item {item_id}: every suggested image needs image_id"
            )
        suggestions.append({"image_id": image_id})
    image_ids = [value["image_id"] for value in suggestions]
    if len(image_ids) != len(set(image_ids)):
        raise ValueError(f"dataset item {item_id}: duplicate suggested image")
    if item["query_type"] == "no-answer":
        if suggestions:
            raise ValueError(
                f"dataset item {item_id}: no-answer cases cannot have suggested "
                "relevant images"
            )
    elif not suggestions:
        raise ValueError(
            f"dataset item {item_id}: answerable draft needs at least one suggested "
            "relevant image"
        )
    return suggestions


def _draft_pairs(
    dataset: dict[str, Any], run: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    items = evaluate.validate_dataset(dataset)
    cases = evaluate.validate_run(run)
    non_pending = [
        str(item["id"]) for item in items if item["annotation"]["status"] != "pending"
    ]
    if non_pending:
        raise ValueError(
            "AI-assisted draft reporting accepts pending annotations only; "
            f"found non-pending cases: {', '.join(non_pending)}"
        )
    missing = [str(item["id"]) for item in items if item["id"] not in cases]
    if missing:
        raise ValueError(f"Run is missing draft cases: {', '.join(missing)}")
    known_ids = {str(item["id"]) for item in items}
    unknown = sorted(set(cases) - known_ids)
    if unknown:
        raise ValueError(f"Run contains unknown cases: {', '.join(unknown)}")

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    answerable = 0
    no_answer = 0
    suggested_labels = 0
    for item in items:
        suggestions = _suggested_images(item)
        draft_item = copy.deepcopy(item)
        draft_item["relevant_images"] = suggestions
        pairs.append((draft_item, cases[str(item["id"])]))
        suggested_labels += len(suggestions)
        if item["query_type"] == "no-answer":
            no_answer += 1
        else:
            answerable += 1
    return pairs, {
        "total": len(items),
        "evaluated": len(items),
        "pending": len(items),
        "answerable": answerable,
        "no_answer": no_answer,
        "suggested_relevance_labels": suggested_labels,
        "human_verified": 0,
    }


def _numeric_result_field(result: dict[str, Any], field: str, case_id: str) -> float:
    value = result.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"run case {case_id}: every result needs numeric {field} for "
            "threshold exploration"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"run case {case_id}: {field} must be finite")
    return number


def _field_semantics(run: dict[str, Any], mode: str, field: str) -> str:
    if field == "confidence":
        return "higher_is_better"
    semantics = run.get("score_semantics")
    if not isinstance(semantics, dict) or semantics.get(mode) not in {
        "lower_is_better",
        "higher_is_better",
    }:
        raise ValueError(
            f"run score_semantics must define raw_score behavior for {mode}"
        )
    return str(semantics[mode])


def _candidate_thresholds(values: set[float], semantics: str) -> list[float]:
    if not values:
        raise ValueError("threshold exploration requires candidate results")
    ordered = sorted(values)
    if semantics == "lower_is_better":
        reject_all = math.nextafter(ordered[0], -math.inf)
    else:
        reject_all = math.nextafter(ordered[-1], math.inf)
    return [reject_all, *ordered]


def _filter_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    field: str,
    threshold: float,
    semantics: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    filtered = []
    for item, case in pairs:
        next_case = dict(case)
        next_case["results"] = [
            result
            for result in case["results"]
            if (
                _numeric_result_field(result, field, str(case["id"])) <= threshold
                if semantics == "lower_is_better"
                else _numeric_result_field(result, field, str(case["id"])) >= threshold
            )
        ]
        filtered.append((item, next_case))
    return filtered


def _explore_field(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    field: str,
    semantics: str,
    max_false_return_rate: float,
    max_recall_drop: float,
    max_reported_candidates: int,
) -> dict[str, Any]:
    baseline = evaluate._metric_block(pairs)
    baseline_recall = baseline["recall_at_5"]
    if not isinstance(baseline_recall, (int, float)):
        raise ValueError("threshold exploration needs answerable cases")
    if baseline["no_answer_false_return_rate"] is None:
        raise ValueError("threshold exploration needs no-answer cases")
    values = {
        _numeric_result_field(result, field, str(case["id"]))
        for _item, case in pairs
        for result in case["results"]
    }
    thresholds = _candidate_thresholds(values, semantics)
    eligible: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    for threshold in thresholds:
        metrics = evaluate._metric_block(
            _filter_pairs(
                pairs,
                field=field,
                threshold=threshold,
                semantics=semantics,
            )
        )
        precision = metrics["precision_at_5"]
        recall = metrics["recall_at_5"]
        false_return_rate = metrics["no_answer_false_return_rate"]
        result_count = metrics["average_result_count"]
        if not all(
            isinstance(value, (int, float))
            for value in (precision, recall, false_return_rate, result_count)
        ):
            continue
        recall_drop = max(0.0, float(baseline_recall) - float(recall))
        if float(false_return_rate) >= max_false_return_rate:
            continue
        if recall_drop > max_recall_drop + 1e-12:
            continue
        less_aggressive = threshold if semantics == "lower_is_better" else -threshold
        candidate = {
            "value": threshold,
            "operator": "<=" if semantics == "lower_is_better" else ">=",
            "metrics": metrics,
            "recall_at_5_drop": recall_drop,
        }
        objective = (
            float(precision),
            float(recall),
            -float(false_return_rate),
            -float(result_count),
            less_aggressive,
        )
        eligible.append((objective, candidate))
    eligible.sort(key=lambda value: value[0], reverse=True)
    reported = [value for _objective, value in eligible[:max_reported_candidates]]
    return {
        "score_semantics": semantics,
        "evaluated_thresholds": len(thresholds),
        "eligible_candidate_count": len(eligible),
        "reported_candidate_limit": max_reported_candidates,
        "best_candidate": reported[0] if reported else None,
        "eligible_candidates": reported,
    }


def _threshold_exploration(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    run: dict[str, Any],
    *,
    max_false_return_rate: float,
    max_recall_drop: float,
    max_reported_candidates: int,
) -> dict[str, Any]:
    modes: dict[str, Any] = {}
    for mode in sorted(evaluate.MODES):
        mode_pairs = [(item, case) for item, case in pairs if item["mode"] == mode]
        if not mode_pairs:
            continue
        fields = {}
        for field in SCORE_FIELDS:
            fields[field] = _explore_field(
                mode_pairs,
                field=field,
                semantics=_field_semantics(run, mode, field),
                max_false_return_rate=max_false_return_rate,
                max_recall_drop=max_recall_drop,
                max_reported_candidates=max_reported_candidates,
            )
        modes[mode] = {"fields": fields}
    return {
        "draft_only": True,
        "production_config_generated": False,
        "constraints": {
            "no_answer_false_return_rate": {
                "operator": "<",
                "value": max_false_return_rate,
            },
            "recall_at_5_drop": {
                "operator": "<=",
                "value": max_recall_drop,
            },
            "objective": "maximize_legacy_returned_precision_at_5",
        },
        "modes": modes,
    }


def build_draft_report(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    dataset_source: str | None = None,
    run_source: str | None = None,
    max_false_return_rate: float = DEFAULT_MAX_FALSE_RETURN_RATE,
    max_recall_drop: float = DEFAULT_MAX_RECALL_DROP,
    max_reported_candidates: int = DEFAULT_MAX_REPORTED_CANDIDATES,
) -> dict[str, Any]:
    if not 0 < max_false_return_rate <= 1:
        raise ValueError("max_false_return_rate must be in (0, 1]")
    if not 0 <= max_recall_drop <= 1:
        raise ValueError("max_recall_drop must be in [0, 1]")
    if max_reported_candidates < 1:
        raise ValueError("max_reported_candidates must be positive")
    pairs, coverage = _draft_pairs(dataset, run)
    by_mode = {
        mode: evaluate._metric_block(
            [(item, case) for item, case in pairs if item["mode"] == mode]
        )
        for mode in sorted(evaluate.MODES)
    }
    return {
        "schema_version": DRAFT_REPORT_SCHEMA_VERSION,
        "kind": "zvec-search-quality-ai-assisted-draft",
        "draft": True,
        "baseline_eligible": False,
        "label_source": LABEL_SOURCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warning": (
            "Exploratory metrics use unverified AI-assisted relevance suggestions. "
            "They are not a human baseline, calibration input, or release gate."
        ),
        "dataset": {"name": dataset["name"], "source": dataset_source},
        "run": {"name": run["name"], "source": run_source},
        "coverage": coverage,
        "metrics": evaluate._metric_block(pairs),
        "by_mode": by_mode,
        "threshold_exploration": _threshold_exploration(
            pairs,
            run,
            max_false_return_rate=max_false_return_rate,
            max_recall_drop=max_recall_drop,
            max_reported_candidates=max_reported_candidates,
        ),
    }


def build_draft_report_files(
    dataset_path: str | Path,
    run_path: str | Path,
    **options: Any,
) -> dict[str, Any]:
    return build_draft_report(
        evaluate._read_json(dataset_path),
        evaluate._read_json(run_path),
        dataset_source=str(Path(dataset_path)),
        run_source=str(Path(run_path)),
        **options,
    )


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    rows = [
        ("Strict Precision@5", metrics["strict_precision_at_5"]),
        ("Returned precision (legacy precision_at_5)", metrics["precision_at_5"]),
        ("Recall@5", metrics["recall_at_5"]),
        ("MRR", metrics["mrr"]),
        ("No-answer accuracy", metrics["no_answer_accuracy"]),
        ("No-answer false-return rate", metrics["no_answer_false_return_rate"]),
        ("Average result count", metrics["average_result_count"]),
        ("Average latency (ms)", metrics["latency_ms"]["average"]),
        ("P95 latency (ms)", metrics["latency_ms"]["p95"]),
        ("Total API requests", metrics["api_requests"]["total"]),
    ]
    lines = [
        "# AI-assisted search-quality draft",
        "",
        "**DRAFT ONLY — NOT BASELINE ELIGIBLE.**",
        "",
        (
            "All relevance labels in this report come from unverified AI-assisted "
            "suggestions. Human review is required before formal evaluation, "
            "calibration, quality-gate decisions, or release claims."
        ),
        "",
        f"- Dataset: `{report['dataset']['name']}`",
        f"- Run: `{report['run']['name']}`",
        f"- Label source: `{report['label_source']}`",
        f"- Draft: `{str(report['draft']).lower()}`",
        f"- Baseline eligible: `{str(report['baseline_eligible']).lower()}`",
        f"- Evaluated cases: {report['coverage']['evaluated']}",
        "",
        "| Exploratory metric | Value |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {label} | {evaluate._format_metric(value)} |" for label, value in rows
    )
    lines.extend(
        [
            "",
            "## Draft threshold exploration",
            "",
            (
                "These candidates are diagnostics only. This tool never writes a "
                "production-loadable `search-quality.json`."
            ),
            "",
            (
                "| Mode | Field | Eligible | Best draft threshold | Returned "
                "precision | R@5 | No-answer false return |"
            ),
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    modes = report["threshold_exploration"]["modes"]
    for mode, value in modes.items():
        for field, exploration in value["fields"].items():
            best = exploration["best_candidate"]
            if best is None:
                threshold = "n/a"
                precision = "n/a"
                recall = "n/a"
                false_return = "n/a"
            else:
                threshold = (
                    f"{best['operator']} {evaluate._format_metric(best['value'])}"
                )
                precision = evaluate._format_metric(best["metrics"]["precision_at_5"])
                recall = evaluate._format_metric(best["metrics"]["recall_at_5"])
                false_return = evaluate._format_metric(
                    best["metrics"]["no_answer_false_return_rate"]
                )
            lines.append(
                f"| {mode} | {field} | "
                f"{exploration['eligible_candidate_count']} | {threshold} | "
                f"{precision} | {recall} | {false_return} |"
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explore a pending dataset with AI-assisted relevance suggestions; "
            "never produces a formal baseline or production threshold config"
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown")
    parser.add_argument(
        "--max-false-return-rate",
        type=float,
        default=DEFAULT_MAX_FALSE_RETURN_RATE,
    )
    parser.add_argument(
        "--max-recall-drop",
        type=float,
        default=DEFAULT_MAX_RECALL_DROP,
    )
    parser.add_argument(
        "--max-reported-candidates",
        type=int,
        default=DEFAULT_MAX_REPORTED_CANDIDATES,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_draft_report_files(
        args.dataset,
        args.run,
        max_false_return_rate=args.max_false_return_rate,
        max_recall_drop=args.max_recall_drop,
        max_reported_candidates=args.max_reported_candidates,
    )
    evaluate._write_json(args.output, report)
    if args.markdown:
        evaluate._write_text(args.markdown, render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
