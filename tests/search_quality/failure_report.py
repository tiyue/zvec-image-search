from __future__ import annotations

import argparse
import copy
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

try:
    from . import evaluate
except ImportError:  # pragma: no cover - direct script execution
    import evaluate  # type: ignore[no-redef]

FAILURE_REPORT_SCHEMA_VERSION = 1
TOP_K = 5
QUERY_TYPE_ORDER = ("text", "image", "combined", "no-answer")
SUPPORTED_SCORE_FIELDS = ("score", "raw_score", "normalized_score", "confidence")


def _numeric(value: Any, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _optional_numeric(value: Any, *, context: str) -> float | None:
    if value is None:
        return None
    return _numeric(value, context=context)


def _percentile(values: list[float], percentile: float) -> float | None:
    return evaluate._percentile(values, percentile)


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "maximum": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "standard_deviation": None,
        }
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": fmean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "standard_deviation": pstdev(values),
    }


def _suggested_images(item: dict[str, Any]) -> list[dict[str, str]]:
    item_id = str(item["id"])
    raw = item.get("suggested_relevant_images")
    if not isinstance(raw, list) or any(not isinstance(value, dict) for value in raw):
        raise ValueError(
            f"dataset item {item_id}: suggested_relevant_images must be an array "
            "of objects"
        )
    values: list[dict[str, str]] = []
    for value in raw:
        image_id = value.get("image_id")
        if not isinstance(image_id, str) or not image_id.strip():
            raise ValueError(f"dataset item {item_id}: suggested image needs image_id")
        values.append({"image_id": image_id})
    identifiers = [value["image_id"] for value in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"dataset item {item_id}: duplicate suggested image")
    if item["query_type"] == "no-answer" and values:
        raise ValueError(
            f"dataset item {item_id}: no-answer cases cannot have suggestions"
        )
    if item["query_type"] != "no-answer" and not values:
        raise ValueError(
            f"dataset item {item_id}: pending answerable case needs suggestions"
        )
    return values


def _analysis_pairs(
    dataset: dict[str, Any], run: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    items = evaluate.validate_dataset(dataset)
    cases = evaluate.validate_run(run)
    missing = [str(item["id"]) for item in items if item["id"] not in cases]
    if missing:
        raise ValueError(f"Run is missing failure-analysis cases: {', '.join(missing)}")
    known_ids = {str(item["id"]) for item in items}
    unknown = sorted(set(cases) - known_ids)
    if unknown:
        raise ValueError(f"Run contains unknown cases: {', '.join(unknown)}")

    statuses = {status: 0 for status in evaluate.ANNOTATION_STATUSES}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_suggestions = False
    for item in items:
        status = str(item["annotation"]["status"])
        statuses[status] += 1
        analysis_item = copy.deepcopy(item)
        if status == "pending":
            analysis_item["relevant_images"] = _suggested_images(item)
            used_suggestions = True
        pairs.append((analysis_item, cases[str(item["id"])]))

    baseline_eligible = bool(items) and statuses["human_verified"] == len(items)
    if used_suggestions:
        label_source = "ai_assisted_suggestions"
    elif statuses["synthetic_fixture"]:
        label_source = "synthetic_fixture"
    else:
        label_source = "human_verified"
    return pairs, {
        "total": len(items),
        "annotation_statuses": statuses,
        "used_suggested_relevant_images": used_suggestions,
        "label_source": label_source,
        "draft": not baseline_eligible,
        "baseline_eligible": baseline_eligible,
    }


def _score_field(run: dict[str, Any], mode: str) -> str:
    fields = run.get("score_fields")
    if isinstance(fields, dict):
        field = fields.get(mode)
        if field in SUPPORTED_SCORE_FIELDS:
            return str(field)
    return "score"


def _score_semantics(run: dict[str, Any], mode: str, field: str) -> str:
    if field in {"confidence", "normalized_score"}:
        return "higher_is_better"
    semantics = run.get("score_semantics")
    if isinstance(semantics, dict) and semantics.get(mode) in {
        "lower_is_better",
        "higher_is_better",
    }:
        return str(semantics[mode])
    raise ValueError(f"run score_semantics must define {mode}")


def _quality_gap(left: float, right: float, semantics: str) -> float:
    if semantics == "lower_is_better":
        return right - left
    return left - right


def _gap_details(
    results: list[dict[str, Any]],
    *,
    field: str,
    semantics: str,
    case_id: str,
    limit: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    selected = results if limit is None else results[:limit]
    if len(selected) < 2:
        return None, None
    gaps: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(
        zip(selected, selected[1:], strict=False), start=1
    ):
        left_score = _numeric(
            left.get(field), context=f"run case {case_id} rank {index} {field}"
        )
        right_score = _numeric(
            right.get(field), context=f"run case {case_id} rank {index + 1} {field}"
        )
        gaps.append(
            {
                "after_rank": index,
                "before_score": left_score,
                "after_score": right_score,
                "gap": _quality_gap(left_score, right_score, semantics),
            }
        )
    return gaps[0], max(gaps, key=lambda value: float(value["gap"]))


def _result_view(
    result: dict[str, Any],
    *,
    rank: int,
    relevant: set[str],
    case_id: str,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "rank": rank,
        "image_id": str(result["image_id"]),
        "relevant": str(result["image_id"]) in relevant,
    }
    for field in SUPPORTED_SCORE_FIELDS:
        if field in result:
            view[field] = _numeric(
                result[field], context=f"run case {case_id} rank {rank} {field}"
            )
    for field in ("match_state", "rank_source"):
        if field in result:
            view[field] = result[field]
    return view


def _case_analysis(
    item: dict[str, Any], case: dict[str, Any], run: dict[str, Any]
) -> dict[str, Any]:
    case_id = str(item["id"])
    mode = str(item["mode"])
    results = case["results"]
    relevant = {str(value["image_id"]) for value in item["relevant_images"]}
    result_ids = [str(value["image_id"]) for value in results]
    top_results = results[:TOP_K]
    top_ids = result_ids[:TOP_K]
    hits = [image_id for image_id in top_ids if image_id in relevant]
    false_positives = [image_id for image_id in top_ids if image_id not in relevant]
    first_relevant_rank = next(
        (
            rank
            for rank, image_id in enumerate(result_ids, start=1)
            if image_id in relevant
        ),
        None,
    )
    field = _score_field(run, mode)
    semantics = _score_semantics(run, mode, field)
    top1_top2_gap, _top5_max_gap = _gap_details(
        results,
        field=field,
        semantics=semantics,
        case_id=case_id,
        limit=TOP_K,
    )
    _all_top1_top2, maximum_gap = _gap_details(
        results,
        field=field,
        semantics=semantics,
        case_id=case_id,
    )

    score_values: dict[str, list[float]] = {}
    for score_field in SUPPORTED_SCORE_FIELDS:
        values = [
            _numeric(
                result[score_field],
                context=f"run case {case_id} {score_field}",
            )
            for result in results
            if score_field in result
        ]
        if values:
            score_values[score_field] = values

    failure_categories: list[str] = []
    if item["query_type"] == "no-answer":
        if results:
            failure_categories.append("false_return_on_no_answer")
    else:
        if first_relevant_rank is None:
            failure_categories.append("no_relevant_in_returned_candidates")
        elif first_relevant_rank > TOP_K:
            failure_categories.append("first_relevant_below_top5")
        if top_ids and top_ids[0] not in relevant:
            failure_categories.append("irrelevant_top1")
        if false_positives:
            failure_categories.append("top5_false_positives")
        if len(hits) < len(relevant):
            failure_categories.append("incomplete_top5_recall")

    confidence_values = score_values.get("confidence", [])
    highest_confidence = max(confidence_values) if confidence_values else None
    return {
        "id": case_id,
        "query_type": item["query_type"],
        "mode": mode,
        "query": copy.deepcopy(item["query"]),
        "status": case.get("status"),
        "result_count": len(results),
        "candidate_count": case.get("candidate_count"),
        "latency_ms": _numeric(
            case["latency_ms"], context=f"run case {case_id} latency_ms"
        ),
        "ranking_score": {
            "field": field,
            "semantics": semantics,
            "distribution": _distribution(score_values.get(field, [])),
            "top1_top2_gap": top1_top2_gap,
            "maximum_gap": maximum_gap,
        },
        "score_distributions": {
            name: _distribution(values) for name, values in score_values.items()
        },
        "top5": [
            _result_view(
                result,
                rank=rank,
                relevant=relevant,
                case_id=case_id,
            )
            for rank, result in enumerate(top_results, start=1)
        ],
        "top5_hit_count": len(hits),
        "top5_relevant_images": hits,
        "first_relevant_rank": first_relevant_rank,
        "missed_relevant_images": sorted(relevant - set(top_ids)),
        "unretrieved_relevant_images": sorted(relevant - set(result_ids)),
        "false_positive_images_top5": false_positives,
        "no_answer_highest_confidence": (
            highest_confidence if item["query_type"] == "no-answer" else None
        ),
        "failure_categories": failure_categories,
    }


def _mean_optional(values: list[float | int | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return fmean(numbers) if numbers else None


def _segment_summary(queries: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [value for value in queries if value["query_type"] != "no-answer"]
    no_answer = [value for value in queries if value["query_type"] == "no-answer"]
    returned = sum(int(value["result_count"]) for value in queries)
    top5_returned = sum(len(value["top5"]) for value in queries)
    top5_hits = sum(int(value["top5_hit_count"]) for value in answerable)
    top1_hits = sum(
        bool(value["top5"] and value["top5"][0]["relevant"]) for value in answerable
    )
    no_answer_false_returns = sum(bool(value["result_count"]) for value in no_answer)
    first_ranks = [
        value["first_relevant_rank"]
        for value in answerable
        if value["first_relevant_rank"] is not None
    ]
    no_answer_confidences = [
        float(value["no_answer_highest_confidence"])
        for value in no_answer
        if value["no_answer_highest_confidence"] is not None
    ]
    return {
        "case_count": len(queries),
        "answerable_count": len(answerable),
        "no_answer_count": len(no_answer),
        "average_result_count": returned / len(queries) if queries else None,
        "top5_precision": top5_hits / top5_returned if top5_returned else None,
        "top1_accuracy": top1_hits / len(answerable) if answerable else None,
        "query_success_at_5": (
            sum(bool(value["top5_hit_count"]) for value in answerable) / len(answerable)
            if answerable
            else None
        ),
        "no_relevant_in_returned_rate": (
            sum(value["first_relevant_rank"] is None for value in answerable)
            / len(answerable)
            if answerable
            else None
        ),
        "average_first_relevant_rank_when_found": (
            fmean(first_ranks) if first_ranks else None
        ),
        "average_top1_top2_gap": _mean_optional(
            [
                value["ranking_score"]["top1_top2_gap"]["gap"]
                if value["ranking_score"]["top1_top2_gap"] is not None
                else None
                for value in queries
            ]
        ),
        "average_maximum_gap": _mean_optional(
            [
                value["ranking_score"]["maximum_gap"]["gap"]
                if value["ranking_score"]["maximum_gap"] is not None
                else None
                for value in queries
            ]
        ),
        "no_answer_false_return_rate": (
            no_answer_false_returns / len(no_answer) if no_answer else None
        ),
        "no_answer_highest_confidence": _distribution(no_answer_confidences),
    }


def _worst_queries(queries: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    answerable = [value for value in queries if value["query_type"] != "no-answer"]
    no_answer = [value for value in queries if value["query_type"] == "no-answer"]
    answerable.sort(
        key=lambda value: (
            value["first_relevant_rank"] is not None,
            int(value["top5_hit_count"]),
            -(int(value["first_relevant_rank"] or 10**9)),
            -len(value["false_positive_images_top5"]),
            str(value["id"]),
        )
    )
    no_answer.sort(
        key=lambda value: (
            -float(value["no_answer_highest_confidence"] or -math.inf),
            -int(value["result_count"]),
            str(value["id"]),
        )
    )

    def compact(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "query_type": value["query_type"],
            "mode": value["mode"],
            "first_relevant_rank": value["first_relevant_rank"],
            "top5_hit_count": value["top5_hit_count"],
            "false_positive_count_top5": len(value["false_positive_images_top5"]),
            "no_answer_highest_confidence": value["no_answer_highest_confidence"],
            "failure_categories": value["failure_categories"],
        }

    return {
        "answerable": [compact(value) for value in answerable[:limit]],
        "no_answer": [compact(value) for value in no_answer[:limit]],
    }


def _confidence_evidence(queries: list[dict[str, Any]]) -> dict[str, Any]:
    relevant_confidences: list[float] = []
    false_confidences: list[float] = []
    no_answer_top: list[float] = []
    by_mode: dict[str, dict[str, list[float]]] = {
        mode: {"relevant": [], "false": [], "no_answer_top": []}
        for mode in sorted(evaluate.MODES)
    }
    for query in queries:
        if query["query_type"] == "no-answer":
            confidence = query["no_answer_highest_confidence"]
            if confidence is not None:
                no_answer_top.append(float(confidence))
                by_mode[str(query["mode"])]["no_answer_top"].append(float(confidence))
        for result in query["top5"]:
            confidence = _optional_numeric(
                result.get("confidence"),
                context=f"query {query['id']} top5 confidence",
            )
            if confidence is None:
                continue
            bucket = "relevant" if result["relevant"] else "false"
            if bucket == "relevant":
                relevant_confidences.append(confidence)
            else:
                false_confidences.append(confidence)
            by_mode[str(query["mode"])][bucket].append(confidence)
    return {
        "top5_relevant": _distribution(relevant_confidences),
        "top5_false_positive": _distribution(false_confidences),
        "no_answer_top1": _distribution(no_answer_top),
        "by_mode": {
            mode: {name: _distribution(values) for name, values in buckets.items()}
            for mode, buckets in by_mode.items()
        },
    }


def _pattern_observations(
    summaries: dict[str, dict[str, Any]], confidence: dict[str, Any]
) -> list[str]:
    observations: list[str] = []
    answerable = [(name, summaries[name]) for name in ("text", "image", "combined")]
    successful = [
        (name, float(value["query_success_at_5"]))
        for name, value in answerable
        if value["query_success_at_5"] is not None
    ]
    if successful:
        best = max(successful, key=lambda value: value[1])
        worst = min(successful, key=lambda value: value[1])
        observations.append(
            f"Top-5 query success ranges from {worst[1]:.3f} ({worst[0]}) to "
            f"{best[1]:.3f} ({best[0]}), so one shared cutoff/ranking policy is "
            "not supported by this run."
        )
    no_answer = summaries["no-answer"]
    false_rate = no_answer["no_answer_false_return_rate"]
    if false_rate is not None:
        observations.append(
            f"No-answer false-return rate is {float(false_rate):.3f}; its highest-"
            "confidence distribution is reported separately from answerable cases."
        )
    relevant_min = confidence["top5_relevant"]["minimum"]
    no_answer_max = confidence["no_answer_top1"]["maximum"]
    if relevant_min is not None and no_answer_max is not None:
        relation = (
            "overlaps" if float(no_answer_max) >= float(relevant_min) else "separates"
        )
        observations.append(
            f"No-answer top confidence max {float(no_answer_max):.4f} {relation} "
            f"the observed relevant-confidence floor {float(relevant_min):.4f}."
        )
    return observations


def _recommendations(
    queries: list[dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
    confidence: dict[str, Any],
) -> list[dict[str, Any]]:
    answerable = [value for value in queries if value["query_type"] != "no-answer"]
    absent = sum(value["first_relevant_rank"] is None for value in answerable)
    below_top5 = sum(
        value["first_relevant_rank"] is not None
        and int(value["first_relevant_rank"]) > TOP_K
        for value in answerable
    )
    false_rate = summaries["no-answer"]["no_answer_false_return_rate"]
    relevant_floor = confidence["top5_relevant"]["minimum"]
    no_answer_ceiling = confidence["no_answer_top1"]["maximum"]
    mode_confidence = confidence["by_mode"]
    no_answer_with_results = sum(
        bool(value["result_count"])
        for value in queries
        if value["query_type"] == "no-answer"
    )
    combined_success = evaluate._format_metric(
        summaries["combined"]["query_success_at_5"]
    )
    combined_top1 = evaluate._format_metric(summaries["combined"]["top1_accuracy"])
    combined_no_answer = mode_confidence["combined"]["no_answer_top"]
    text_confidence = mode_confidence["text"]
    image_confidence = mode_confidence["image"]
    text_no_answer_max = evaluate._format_metric(
        text_confidence["no_answer_top"]["maximum"]
    )
    text_relevant_min = evaluate._format_metric(text_confidence["relevant"]["minimum"])
    image_no_answer_max = evaluate._format_metric(
        image_confidence["no_answer_top"]["maximum"]
    )
    image_relevant_min = evaluate._format_metric(
        image_confidence["relevant"]["minimum"]
    )
    recommendations: list[dict[str, Any]] = [
        {
            "priority": 1,
            "algorithm": "combined_absolute_score_fusion",
            "proposal": (
                "Build combined confidence from calibrated absolute text and image "
                "similarities, then apply an agreement or minimum-channel rule. Do "
                "not turn fused rank position into confidence. This needs no new model "
                "call."
            ),
            "evidence": [
                f"Combined query success@5 is {combined_success} and Top-1 "
                f"accuracy is {combined_top1}.",
                "Combined no-answer top confidence spans "
                f"{evaluate._format_metric(combined_no_answer['minimum'])}"
                " to "
                f"{evaluate._format_metric(combined_no_answer['maximum'])}, "
                "overlapping combined relevant results.",
            ],
        },
        {
            "priority": 2,
            "algorithm": "mode_specific_calibration_and_abstention",
            "proposal": (
                "Calibrate text, image, and combined independently and return an empty "
                "result only from calibrated confidence, not from raw rank or a shared "
                "threshold."
            ),
            "evidence": [
                (
                    "No-answer false-return rate is "
                    f"{evaluate._format_metric(false_rate)}."
                ),
                "Top-5 query success by type: "
                + ", ".join(
                    f"{name}={evaluate._format_metric(summaries[name]['query_success_at_5'])}"
                    for name in ("text", "image", "combined")
                )
                + ".",
                f"Text no-answer max confidence is {text_no_answer_max} versus "
                f"relevant minimum {text_relevant_min}; image is "
                f"{image_no_answer_max} versus {image_relevant_min}.",
            ],
        },
        {
            "priority": 3,
            "algorithm": "recall_before_thresholding",
            "proposal": (
                "For cases with no relevant item in the captured candidate list, "
                "retain a larger per-channel candidate union and normalize text/image "
                "scores before fusion. This reuses existing embeddings and model calls."
            ),
            "evidence": [
                f"{absent}/{len(answerable)} answerable queries have no labelled "
                "relevant result in the captured list.",
                f"{below_top5}/{len(answerable)} first find a relevant result below "
                "rank 5.",
            ],
        },
    ]
    if relevant_floor is not None and no_answer_ceiling is not None:
        recommendations[1]["evidence"].extend(
            [
                f"Overall relevant Top-5 confidence minimum is "
                f"{float(relevant_floor):.4f}.",
                f"Overall no-answer top-confidence maximum is "
                f"{float(no_answer_ceiling):.4f}.",
            ]
        )
    recommendations.append(
        {
            "priority": len(recommendations) + 1,
            "algorithm": "gap_as_secondary_dynamic_top_k",
            "proposal": (
                "Use the largest adjacent score gap only to shorten an already trusted "
                "result list. A gap cannot establish that the first result is "
                "relevant, so it must not replace the absolute no-result threshold."
            ),
            "evidence": [
                "Average maximum ranking-score gap by type: "
                + ", ".join(
                    f"{name}={evaluate._format_metric(summaries[name]['average_maximum_gap'])}"
                    for name in QUERY_TYPE_ORDER
                )
                + ".",
                (
                    f"{no_answer_with_results} "
                    "no-answer cases still have a rank-1 result, which a gap-only "
                    "rule cannot remove."
                ),
            ],
        }
    )
    return recommendations[:5]


def build_failure_report(
    dataset: dict[str, Any],
    run: dict[str, Any],
    *,
    dataset_source: str | None = None,
    run_source: str | None = None,
    worst_limit: int = 10,
) -> dict[str, Any]:
    if worst_limit < 1:
        raise ValueError("worst_limit must be positive")
    pairs, label_state = _analysis_pairs(dataset, run)
    queries = [_case_analysis(item, case, run) for item, case in pairs]
    by_query_type: dict[str, Any] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for query_type in QUERY_TYPE_ORDER:
        selected = [value for value in queries if value["query_type"] == query_type]
        summary = _segment_summary(selected)
        summaries[query_type] = summary
        by_query_type[query_type] = {"summary": summary, "queries": selected}
    confidence = _confidence_evidence(queries)
    warning = None
    if label_state["used_suggested_relevant_images"]:
        warning = (
            "Failure labels use unverified suggested_relevant_images. This is a draft "
            "diagnostic, not a baseline, calibration input, or release gate."
        )
    return {
        "schema_version": FAILURE_REPORT_SCHEMA_VERSION,
        "kind": "zvec-search-quality-failure-analysis",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "draft": label_state["draft"],
        "baseline_eligible": label_state["baseline_eligible"],
        "label_source": label_state["label_source"],
        "warning": warning,
        "dataset": {"name": dataset["name"], "source": dataset_source},
        "run": {"name": run["name"], "source": run_source},
        "coverage": label_state,
        "by_query_type": by_query_type,
        "confidence_separation": confidence,
        "pattern_differences": {
            "summaries": summaries,
            "observations": _pattern_observations(summaries, confidence),
        },
        "worst_queries": _worst_queries(queries, worst_limit),
        "algorithm_recommendations": _recommendations(queries, summaries, confidence),
    }


def build_failure_report_files(
    dataset_path: str | Path,
    run_path: str | Path,
    *,
    worst_limit: int = 10,
) -> dict[str, Any]:
    return build_failure_report(
        evaluate._read_json(dataset_path),
        evaluate._read_json(run_path),
        dataset_source=str(Path(dataset_path)),
        run_source=str(Path(run_path)),
        worst_limit=worst_limit,
    )


def _markdown_query_row(query: dict[str, Any]) -> str:
    first_rank = query["first_relevant_rank"]
    max_gap = query["ranking_score"]["maximum_gap"]
    confidence = query["no_answer_highest_confidence"]
    return (
        f"| `{query['id']}` | {query['mode']} | {query['top5_hit_count']} | "
        f"{first_rank if first_rank is not None else 'none'} | "
        f"{len(query['missed_relevant_images'])} | "
        f"{len(query['false_positive_images_top5'])} | "
        f"{evaluate._format_metric(max_gap['gap'] if max_gap else None)} | "
        f"{evaluate._format_metric(confidence)} |"
    )


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Search-quality failure analysis",
        "",
        f"- Draft: `{str(report['draft']).lower()}`",
        f"- Baseline eligible: `{str(report['baseline_eligible']).lower()}`",
        f"- Label source: `{report['label_source']}`",
        f"- Dataset: `{report['dataset']['name']}`",
        f"- Run: `{report['run']['name']}`",
    ]
    if report["warning"]:
        lines.extend(["", f"**DRAFT ONLY:** {report['warning']}"])
    lines.extend(
        [
            "",
            "## Query-type differences",
            "",
            (
                "| Type | Cases | Top-5 precision | Top-1 accuracy | Query success@5 "
                "| No relevant returned | No-answer false return | Avg max gap |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    summaries = report["pattern_differences"]["summaries"]
    for name in QUERY_TYPE_ORDER:
        value = summaries[name]
        lines.append(
            f"| {name} | {value['case_count']} | "
            f"{evaluate._format_metric(value['top5_precision'])} | "
            f"{evaluate._format_metric(value['top1_accuracy'])} | "
            f"{evaluate._format_metric(value['query_success_at_5'])} | "
            f"{evaluate._format_metric(value['no_relevant_in_returned_rate'])} | "
            f"{evaluate._format_metric(value['no_answer_false_return_rate'])} | "
            f"{evaluate._format_metric(value['average_maximum_gap'])} |"
        )
    lines.extend(["", "### Observations", ""])
    lines.extend(
        f"- {value}" for value in report["pattern_differences"]["observations"]
    )
    for query_type in QUERY_TYPE_ORDER:
        lines.extend(
            [
                "",
                f"## {query_type} queries",
                "",
                (
                    "| Query | Mode | Top-5 hits | First relevant rank | Missed "
                    "relevant | False positives | Max gap | No-answer max confidence |"
                ),
                "|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        lines.extend(
            _markdown_query_row(query)
            for query in report["by_query_type"][query_type]["queries"]
        )
    lines.extend(["", "## Worst answerable queries", ""])
    for value in report["worst_queries"]["answerable"]:
        lines.append(
            f"- `{value['id']}`: first relevant="
            f"{value['first_relevant_rank'] or 'none'}, Top-5 hits="
            f"{value['top5_hit_count']}, failures="
            f"{', '.join(value['failure_categories']) or 'none'}"
        )
    lines.extend(["", "## Worst no-answer queries", ""])
    for value in report["worst_queries"]["no_answer"]:
        lines.append(
            f"- `{value['id']}`: highest confidence="
            f"{evaluate._format_metric(value['no_answer_highest_confidence'])}, "
            f"false positives={value['false_positive_count_top5']}"
        )
    lines.extend(["", "## Evidence-backed algorithm recommendations", ""])
    for recommendation in report["algorithm_recommendations"]:
        lines.append(
            f"{recommendation['priority']}. **{recommendation['algorithm']}** — "
            f"{recommendation['proposal']}"
        )
        lines.extend(f"   - Evidence: {value}" for value in recommendation["evidence"])
    lines.extend(
        [
            "",
            "The JSON companion contains every Top-5 result, relevance flag, raw and "
            "normalized scores, confidence, score distributions, and adjacent-gap "
            "details.",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose retrieval and ranking failures by query type without changing "
            "production search behavior"
        )
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown")
    parser.add_argument("--worst-limit", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_failure_report_files(
        args.dataset, args.run, worst_limit=args.worst_limit
    )
    evaluate._write_json(args.output, report)
    if args.markdown:
        evaluate._write_text(args.markdown, render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
