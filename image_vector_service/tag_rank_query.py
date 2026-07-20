from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .tag_search import TagSearchPlan, tag_match_score


@dataclass(frozen=True)
class RankedTagCandidate:
    """One bounded tag-search candidate returned by SQLite."""

    doc_id: str
    confidence: float
    matched_tag_count: int


@dataclass(frozen=True)
class RankedTagCandidatePage:
    """A Top-N page plus the exact number of eligible documents."""

    total: int
    items: tuple[RankedTagCandidate, ...]


def ranked_tag_candidates(
    connection: sqlite3.Connection,
    query_plan: TagSearchPlan,
    filter_plan: TagSearchPlan,
    *,
    limit: int,
) -> RankedTagCandidatePage:
    """Rank matching document ids without materializing every matching entry.

    Resolved tag spellings are passed through SQLite's built-in JSON table
    function.  This avoids both the SQLite parameter limit and a temporary
    write transaction when a fuzzy fragment expands to many stored tags.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")
    if query_plan.matches_nothing or filter_plan.matches_nothing:
        return RankedTagCandidatePage(0, ())
    if not query_plan.expansions:
        return RankedTagCandidatePage(0, ())

    query_terms = _term_payload(query_plan, include_scores=True)
    filter_terms = _term_payload(filter_plan, include_scores=False)
    query_required = len(query_plan.expansions) if query_plan.mode == "all" else 1
    filter_required = (
        len(filter_plan.expansions)
        if filter_plan.expansions and filter_plan.mode == "all"
        else 1
        if filter_plan.expansions
        else 0
    )
    rows = connection.execute(
        """
        WITH query_terms AS (
            SELECT
                CAST(json_extract(value, '$.group_id') AS INTEGER) AS group_id,
                CAST(json_extract(value, '$.tag') AS TEXT) AS tag,
                CAST(json_extract(value, '$.score') AS REAL) AS score
            FROM json_each(?)
        ),
        filter_terms AS (
            SELECT
                CAST(json_extract(value, '$.group_id') AS INTEGER) AS group_id,
                CAST(json_extract(value, '$.tag') AS TEXT) AS tag
            FROM json_each(?)
        ),
        query_group_scores AS (
            SELECT
                entry_tag_index.doc_id AS doc_id,
                query_terms.group_id AS group_id,
                MAX(query_terms.score) AS score
            FROM query_terms
            JOIN entry_tag_index ON entry_tag_index.tag = query_terms.tag
            GROUP BY entry_tag_index.doc_id, query_terms.group_id
        ),
        query_docs AS (
            SELECT
                doc_id,
                AVG(score) AS confidence,
                COUNT(*) AS matched_groups
            FROM query_group_scores
            GROUP BY doc_id
        ),
        filter_group_matches AS (
            SELECT
                entry_tag_index.doc_id AS doc_id,
                filter_terms.group_id AS group_id
            FROM filter_terms
            JOIN entry_tag_index ON entry_tag_index.tag = filter_terms.tag
            GROUP BY entry_tag_index.doc_id, filter_terms.group_id
        ),
        filter_docs AS (
            SELECT doc_id, COUNT(*) AS matched_groups
            FROM filter_group_matches
            GROUP BY doc_id
        ),
        all_terms AS (
            SELECT tag FROM query_terms
            UNION
            SELECT tag FROM filter_terms
        ),
        matched_tag_counts AS (
            SELECT
                entry_tag_index.doc_id AS doc_id,
                COUNT(DISTINCT entry_tag_index.tag) AS matched_tag_count
            FROM all_terms
            JOIN entry_tag_index ON entry_tag_index.tag = all_terms.tag
            GROUP BY entry_tag_index.doc_id
        ),
        eligible AS (
            SELECT
                entries.doc_id AS doc_id,
                query_docs.confidence AS confidence,
                matched_tag_counts.matched_tag_count AS matched_tag_count,
                entries.root_id AS root_id,
                entries.relative_path AS relative_path
            FROM query_docs
            JOIN entries ON entries.doc_id = query_docs.doc_id
            JOIN matched_tag_counts
                ON matched_tag_counts.doc_id = query_docs.doc_id
            LEFT JOIN filter_docs ON filter_docs.doc_id = query_docs.doc_id
            WHERE query_docs.matched_groups >= ?
              AND (? = 0 OR COALESCE(filter_docs.matched_groups, 0) >= ?)
        ),
        ranked AS (
            SELECT *, COUNT(*) OVER () AS total_count
            FROM eligible
        )
        SELECT doc_id, confidence, matched_tag_count, total_count
        FROM ranked
        ORDER BY
            confidence DESC,
            matched_tag_count DESC,
            root_id,
            relative_path,
            doc_id
        LIMIT ?
        """,
        (
            json.dumps(query_terms, ensure_ascii=False, separators=(",", ":")),
            json.dumps(filter_terms, ensure_ascii=False, separators=(",", ":")),
            query_required,
            filter_required,
            filter_required,
            limit,
        ),
    ).fetchall()
    items = tuple(
        RankedTagCandidate(
            doc_id=str(row["doc_id"]),
            confidence=min(1.0, max(0.0, float(row["confidence"]))),
            matched_tag_count=int(row["matched_tag_count"]),
        )
        for row in rows
    )
    total = int(rows[0]["total_count"]) if rows else 0
    return RankedTagCandidatePage(total=total, items=items)


def _term_payload(
    plan: TagSearchPlan,
    *,
    include_scores: bool,
) -> list[dict[str, object]]:
    return [
        {
            "group_id": group_id,
            "tag": tag,
            "score": tag_match_score(expansion, tag) if include_scores else 1.0,
        }
        for group_id, expansion in enumerate(plan.expansions)
        for tag in expansion.matches
    ]
