from __future__ import annotations

import importlib
from collections import defaultdict
from typing import Any

image_identity = importlib.import_module(
    "tests.search_quality.image_identity" if __package__ else "image_identity"
)

SCHEMA_VERSION = 1
STATUS_MEASURED = "measured"
STATUS_INSUFFICIENT_COVERAGE = "insufficient_coverage"


def _text_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{context} must be an array of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{context} contains duplicate library ids")
    return normalized


def _scope_library_ids(
    item: dict[str, Any], case: dict[str, Any]
) -> tuple[tuple[str, ...], str]:
    item_id = str(item["id"])
    scope = item["library_scope"]
    scope_mode = str(scope["mode"])
    configured = _text_list(
        scope["library_ids"], f"dataset item {item_id} library_scope.library_ids"
    )
    raw_case_ids = case.get("library_ids")
    observed = (
        _text_list(raw_case_ids, f"run case {item_id} library_ids")
        if raw_case_ids is not None
        else ()
    )

    if scope_mode == "single":
        if observed and set(observed) != set(configured):
            raise ValueError(
                f"run case {item_id} library_ids do not match its single scope"
            )
        return configured, "dataset_single"
    if scope_mode == "selected":
        if not configured:
            raise ValueError(
                f"dataset item {item_id} selected scope has no library ids"
            )
        if observed and set(observed) != set(configured):
            raise ValueError(
                f"run case {item_id} library_ids do not match its selected scope"
            )
        return configured, "dataset_selected"
    if scope_mode != "all_enabled":
        raise ValueError(f"dataset item {item_id} has an invalid library scope")
    if observed:
        return observed, "run_all_enabled"
    return (), "unobserved_all_enabled"


def _record_library_id(
    record: dict[str, Any],
    scope_ids: tuple[str, ...],
    context: str,
) -> str:
    image_id = record.get("image_id")
    if not isinstance(image_id, str) or not image_id.strip():
        raise ValueError(f"{context} has no valid image_id")
    image_id = image_id.strip()
    explicit = record.get("library_id")
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        raise ValueError(f"{context} library_id must be a non-empty string")
    explicit_id = explicit.strip() if isinstance(explicit, str) else None

    matches = [
        library_id for library_id in scope_ids if image_id.startswith(f"{library_id}:")
    ]
    if explicit_id is not None:
        if explicit_id not in scope_ids:
            raise ValueError(f"{context} library_id is outside the evaluated scope")
        if not image_id.startswith(f"{explicit_id}:"):
            raise ValueError(f"{context} library_id disagrees with image_id")
        if len(matches) > 1:
            raise ValueError(f"{context} image_id has ambiguous library attribution")
        return explicit_id
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing"
        raise ValueError(f"{context} has {reason} library attribution")
    return matches[0]


def _case_metrics(
    item: dict[str, Any],
    case: dict[str, Any],
    scope_ids: tuple[str, ...],
) -> dict[str, Any]:
    item_id = str(item["id"])
    identity_index = image_identity.ImageIdentityIndex()
    relevant_records: list[tuple[dict[str, Any], str, Any]] = []
    result_records: list[tuple[dict[str, Any], str, Any, int]] = []
    relevant_counts: defaultdict[str, int] = defaultdict(int)
    try:
        for index, record in enumerate(item["relevant_images"], start=1):
            context = f"dataset item {item_id} relevant_images[{index}]"
            library_id = _record_library_id(record, scope_ids, context)
            reference = identity_index.add(record, context)
            relevant_records.append((record, library_id, reference))
            relevant_counts[library_id] += 1
        for position, result in enumerate(case["results"], start=1):
            context = f"run case {item_id} results[{position}]"
            library_id = _record_library_id(result, scope_ids, context)
            reference = identity_index.add(result, context)
            result_records.append((result, library_id, reference, position))
        identity_index.require_unique(
            [record[2] for record in relevant_records],
            f"dataset item {item_id} relevant_images",
        )
        identity_index.require_unique(
            [record[2] for record in result_records],
            f"run case {item_id} results",
        )
    except image_identity.ImageIdentityError as exc:
        raise ValueError(str(exc)) from exc

    relevant_library_by_key = {
        identity_index.key(reference): library_id
        for _, library_id, reference in relevant_records
    }
    ranked: list[tuple[tuple[str, str], str, bool, int]] = []
    relevant_hits: defaultdict[str, int] = defaultdict(int)
    returned_counts: defaultdict[str, int] = defaultdict(int)
    for _, library_id, reference, position in result_records:
        key = identity_index.key(reference)
        is_relevant = key in relevant_library_by_key
        ranked.append((key, library_id, is_relevant, position))
        returned_counts[library_id] += 1
        if is_relevant:
            relevant_hits[library_id] += 1

    direction_counts: defaultdict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    # Recall already measures whether labelled images were retrieved.  Fairness
    # therefore compares only observable returned orderings, instead of counting
    # every unreturned label as another inversion against the same displayed
    # cross-Collection candidate.  This avoids making exhaustive annotation sets
    # (or a smaller requested Top-K) mechanically look more biased.
    for _, relevant_library_id, is_relevant, relevant_rank in ranked:
        if not is_relevant:
            continue
        for _, candidate_library_id, is_relevant, candidate_rank in ranked:
            if is_relevant or candidate_library_id == relevant_library_id:
                continue
            counts = direction_counts[(relevant_library_id, candidate_library_id)]
            counts[0] += 1
            if relevant_rank > candidate_rank:
                counts[1] += 1

    comparisons = sum(value[0] for value in direction_counts.values())
    inversions = sum(value[1] for value in direction_counts.values())
    relevant_hit_libraries = {
        library_id for library_id, count in relevant_hits.items() if count
    }
    # A returned list containing relevant hits from two Collections is direct
    # positive evidence even when filtering removed every non-relevant candidate.
    positive_cross_collection_evidence = len(relevant_hit_libraries) >= 2
    # A labelled Collection is evidenced only when a relevant result from that
    # Collection was actually returned.  Cross-Collection noise alone is not
    # evidence for a missing relevant Collection.
    evidenced_relevant_libraries = set(relevant_hit_libraries)
    assessable = bool(relevant_hit_libraries)
    if comparisons:
        accuracy = 1.0 - inversions / comparisons
    elif assessable:
        accuracy = 1.0
    else:
        accuracy = None

    return {
        "id": item_id,
        "scope_library_ids": list(scope_ids),
        "relevant_labels_by_library": dict(sorted(relevant_counts.items())),
        "returned_candidates_by_library": dict(sorted(returned_counts.items())),
        "relevant_hits_by_library": dict(sorted(relevant_hits.items())),
        "cross_collection_comparisons": comparisons,
        "cross_collection_inversions": inversions,
        "pairwise_accuracy": accuracy,
        "assessable": assessable,
        "positive_cross_collection_evidence": positive_cross_collection_evidence,
        "evidenced_relevant_libraries": sorted(evidenced_relevant_libraries),
        "directions": {
            f"{source}->{target}": {
                "relevant_library_id": source,
                "candidate_library_id": target,
                "comparisons": counts[0],
                "inversions": counts[1],
                "pairwise_accuracy": 1.0 - counts[1] / counts[0],
            }
            for (source, target), counts in sorted(direction_counts.items())
        },
    }


def evaluate_collection_fairness(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Measure relevance-conditioned ranking errors across Collection boundaries.

    The metric deliberately does not assume that equal-size result shares are fair,
    or that Collection size predicts relevance. It only compares a human-labelled
    relevant image with returned non-relevant candidates from other Collections.
    """

    case_reports: list[dict[str, Any]] = []
    excluded = {
        "no_answer": 0,
        "single_collection_scope": 0,
        "unobserved_all_enabled_scope": 0,
        "no_comparable_evidence": 0,
    }
    scoped_libraries: set[str] = set()
    relevant_label_libraries: set[str] = set()
    evidenced_relevant_libraries: set[str] = set()
    multi_collection_cases = 0
    for item, case in pairs:
        if item["query_type"] == "no-answer":
            excluded["no_answer"] += 1
            continue
        scope_ids, scope_source = _scope_library_ids(item, case)
        if not scope_ids:
            excluded["unobserved_all_enabled_scope"] += 1
            continue
        if len(scope_ids) < 2:
            excluded["single_collection_scope"] += 1
            continue
        multi_collection_cases += 1
        scoped_libraries.update(scope_ids)
        case_report = _case_metrics(item, case, scope_ids)
        case_report["scope_source"] = scope_source
        case_reports.append(case_report)
        relevant_label_libraries.update(case_report["relevant_labels_by_library"])
        evidenced_relevant_libraries.update(case_report["evidenced_relevant_libraries"])
        if not case_report["assessable"]:
            excluded["no_comparable_evidence"] += 1

    assessable_cases = [case for case in case_reports if case["assessable"]]
    comparisons = sum(case["cross_collection_comparisons"] for case in case_reports)
    inversions = sum(case["cross_collection_inversions"] for case in case_reports)

    direction_totals: defaultdict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    for case in case_reports:
        for direction in case["directions"].values():
            key = (
                direction["relevant_library_id"],
                direction["candidate_library_id"],
            )
            direction_totals[key][0] += direction["comparisons"]
            direction_totals[key][1] += direction["inversions"]
    by_direction: dict[str, dict[str, Any]] = {
        f"{source}->{target}": {
            "relevant_library_id": source,
            "candidate_library_id": target,
            "comparisons": counts[0],
            "inversions": counts[1],
            "pairwise_accuracy": 1.0 - counts[1] / counts[0],
        }
        for (source, target), counts in sorted(direction_totals.items())
    }
    directional_accuracies = [
        1.0 - counts[1] / counts[0] for counts in direction_totals.values()
    ]

    if comparisons:
        pairwise_accuracy: float | None = 1.0 - inversions / comparisons
    elif assessable_cases:
        pairwise_accuracy = 1.0
    else:
        pairwise_accuracy = None
    evidenced_label_libraries = relevant_label_libraries & evidenced_relevant_libraries
    unevidenced_label_libraries = (
        relevant_label_libraries - evidenced_relevant_libraries
    )
    measured = (
        bool(assessable_cases)
        and len(scoped_libraries) >= 2
        and not unevidenced_label_libraries
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "zvec-cross-collection-fairness",
        "status": STATUS_MEASURED if measured else STATUS_INSUFFICIENT_COVERAGE,
        "metric_notes": {
            "pairwise_accuracy": (
                "Probability that a returned human-labelled relevant image ranks "
                "ahead of a returned non-relevant candidate from another "
                "Collection. Recall measures human-labelled images that were not "
                "returned."
            ),
            "size_independence": (
                "No Collection-size prior or equal-result-share assumption is used; "
                "ranking is judged only against human relevance labels."
            ),
            "coverage": (
                "Measured requires at least one answerable case with two scoped "
                "Collections and a returned relevant hit. Every Collection "
                "represented by a relevance label must itself have returned "
                "relevant evidence."
            ),
        },
        "coverage": {
            "evaluated_cases": len(pairs),
            "multi_collection_answerable_cases": multi_collection_cases,
            "assessable_cases": len(assessable_cases),
            "collection_count": len(scoped_libraries),
            "library_ids": sorted(scoped_libraries),
            "relevant_label_collection_count": len(relevant_label_libraries),
            "relevant_label_library_ids": sorted(relevant_label_libraries),
            "evidenced_relevant_label_collection_count": len(evidenced_label_libraries),
            "evidenced_relevant_label_library_ids": sorted(evidenced_label_libraries),
            "unevidenced_relevant_label_library_ids": sorted(
                unevidenced_label_libraries
            ),
            "cross_collection_comparisons": comparisons,
            "direction_count": len(by_direction),
            "excluded_cases": excluded,
        },
        "metrics": {
            "cross_collection_pairwise_accuracy": pairwise_accuracy,
            "cross_collection_inversion_rate": (
                1.0 - pairwise_accuracy if pairwise_accuracy is not None else None
            ),
            "worst_directional_pairwise_accuracy": (
                min(directional_accuracies) if directional_accuracies else None
            ),
            "worst_directional_inversion_rate": (
                1.0 - min(directional_accuracies) if directional_accuracies else None
            ),
        },
        "by_direction": by_direction,
        "cases": case_reports,
    }
