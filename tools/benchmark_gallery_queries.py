"""Synthetic benchmark for the folder-browsing SQLite queries.

Round 1 task A only.  The benchmark builds a temporary state database from
synthetic metadata (no image files, no Collection, no model calls), then
measures the gallery read paths used by ``LibraryBrowser``:

* one folder page (first, middle and deepest page) for direct images
* one recursive folder page
* a folder-list page, its deep page, keyword filters and a root filter

Every case runs twice: once through the current implementation and once
through a frozen copy of the round-1 baseline SQL.  Results must be identical;
the script exits non-zero when they are not.  Read timings are reported for a
database with and without the ``NOCASE`` folder-page index so the gain can be
attributed, and the index build, space and write costs are reported separately.

Run the regression sizes with::

    python -B tools/benchmark_gallery_queries.py --scenario small
    python -B tools/benchmark_gallery_queries.py --scenario large
    python -B tools/benchmark_gallery_queries.py --scenario multi

The measurements describe synthetic metadata on the local machine only.  They
are not an end-to-end claim about real photo libraries.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from image_vector_service.state import (  # noqa: E402
    IndexState,
    IndexStateReader,
    _count_folder_entries,
    _escape_like,
    _normalized_relative_folder,
    _validate_page,
)

# Name of the round-1 index whose presence/absence is compared.
FOLDER_PAGE_INDEX = "idx_entries_folder_page_nocase"

DEEP_PAGE_LIMIT = 60
FOLDER_LIST_LIMIT = 200
WRITE_BATCH_SIZE = 500
INSERT_BATCH_SIZE = 2_000


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    roots: int
    folders: int
    images: int
    dense_images: int
    repeats: int


SCENARIOS: dict[str, Scenario] = {
    "small": Scenario("small", 1, 100, 10_000, 2_000, 15),
    "large": Scenario("large", 1, 1_000, 100_000, 20_000, 9),
    "multi": Scenario("multi", 2, 2_000, 200_000, 20_000, 7),
}


def _album_count(folders: int) -> int:
    return max(1, min(folders, folders // 10))


def _folder_name(index: int, folders: int) -> str:
    album = index * _album_count(folders) // folders
    # Basenames repeat across albums to exercise same-named folders.
    return f"album-{album:03d}/set-{index % 50:04d}"


def _dense_folder(folders: int) -> str:
    return _folder_name(0, folders)


def _images_per_folder(scenario: Scenario) -> int:
    return (scenario.images - scenario.dense_images) // scenario.folders


def _dense_folder_images(scenario: Scenario) -> int:
    return _images_per_folder(scenario) + scenario.dense_images


def _folder_images(scenario: Scenario, folder_index: int) -> int:
    count = _images_per_folder(scenario)
    return count + (scenario.dense_images if folder_index == 0 else 0)


def _entry(
    doc_id: str,
    root_id: str,
    relative_path: str,
    sequence: int,
) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "root_id": root_id,
        "relative_path": relative_path,
        "file_name": relative_path.rsplit("/", 1)[-1],
        "extension": "jpg",
        "mime_type": "image/jpeg",
        "sha256": f"{sequence:064x}",
        "size_bytes": 1024,
        "mtime_ns": 1_600_000_000_000_000_000 + sequence,
        "width": 800,
        "height": 1200,
        "tags": ["manual-tag"] if sequence % 3 else [],
        "folder_tags": ["folder-tag"] if sequence % 4 else [],
        "accepted_auto_tags": ["model-tag"] if sequence % 5 else [],
        "inherited_tags": ["inherited-tag"] if sequence % 6 else [],
    }


def _entry_groups(
    scenario: Scenario,
) -> Iterator[tuple[str, str, list[dict[str, Any]]]]:
    """Yield (root_id, relative_folder, entries) groups of the library."""

    sequence = 0
    for root_index in range(scenario.roots):
        for folder_index in range(scenario.folders):
            folder = _folder_name(folder_index, scenario.folders)
            entries = []
            for image_index in range(_folder_images(scenario, folder_index)):
                sequence += 1
                entries.append(
                    _entry(
                        f"doc-{root_index}-{folder_index:05d}-{image_index:06d}",
                        f"root-{root_index}",
                        f"{folder}/{image_index:06d}.jpg",
                        sequence,
                    )
                )
            yield f"root-{root_index}", folder, entries


def _failed_annotation_rows(scenario: Scenario) -> list[tuple[str, str, str, str]]:
    """Mark every 17th direct image of every folder as a failed annotation."""

    rows: list[tuple[str, str, str, str]] = []
    sequence = 0
    for root_index in range(scenario.roots):
        for folder_index in range(scenario.folders):
            for image_index in range(_folder_images(scenario, folder_index)):
                sequence += 1
                if image_index % 17 == 0:
                    rows.append(
                        (
                            f"doc-{root_index}-{folder_index:05d}-{image_index:06d}",
                            f"{sequence:064x}",
                            "failed",
                            "synthetic failure",
                        )
                    )
    return rows


def build_library(state: IndexState, scenario: Scenario) -> dict[str, float]:
    """Insert the synthetic library and report bulk insertion cost."""

    started = time.perf_counter()
    inserted = 0
    batch: list[dict[str, Any]] = []
    for _root_id, _folder, entries in _entry_groups(scenario):
        batch.extend(entries)
        if len(batch) >= INSERT_BATCH_SIZE:
            state.set_many(batch)
            inserted += len(batch)
            batch = []
    if batch:
        state.set_many(batch)
        inserted += len(batch)
    failed_rows = _failed_annotation_rows(scenario)
    if failed_rows:
        with state.connection:
            state.connection.executemany(
                "INSERT INTO document_annotations("
                "doc_id, source_sha256, status, description) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET status=excluded.status",
                failed_rows,
            )
    return {
        "inserted": inserted,
        "failed_annotations": len(failed_rows),
        "insert_seconds": time.perf_counter() - started,
    }


def _legacy_where(root_id: str | None, query: str) -> tuple[str, list[Any]]:
    """Frozen copy of the round-1 baseline folder-list filter."""

    where: list[str] = []
    values: list[Any] = []
    if root_id is not None:
        where.append("folder_index.root_id = ?")
        values.append(str(root_id).strip())
    if query:
        pattern = f"%{_escape_like(query)}%"
        where.append(
            "(folder_index.relative_folder LIKE ? ESCAPE '\\' "
            "OR roots.current_path LIKE ? ESCAPE '\\')"
        )
        values.extend((pattern, pattern))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, values


def _legacy_grouped_from(where_sql: str) -> str:
    return (
        "FROM entry_folder_index AS folder_index "
        "JOIN entries ON entries.doc_id = folder_index.doc_id "
        "JOIN roots ON roots.root_id = folder_index.root_id "
        "JOIN folder_catalog ON "
        "folder_catalog.root_id = folder_index.root_id AND "
        "folder_catalog.relative_folder = folder_index.relative_folder "
        "LEFT JOIN document_annotations "
        "ON document_annotations.doc_id = entries.doc_id "
        f"{where_sql} GROUP BY folder_index.root_id, "
        "folder_index.relative_folder HAVING SUM(folder_index.is_direct) > 0"
    )


def _legacy_list_folders_sql(
    *,
    root_id: str | None = None,
    query: str = "",
    offset: int = 0,
    limit: int = FOLDER_LIST_LIMIT,
) -> tuple[str, list[Any]]:
    """Frozen copy of the round-1 baseline folder-list page statement."""

    where_sql, values = _legacy_where(root_id, str(query).strip())
    grouped_from = _legacy_grouped_from(where_sql)
    sql = (
        "SELECT folder_index.root_id, folder_index.relative_folder, "
        "roots.current_path, SUM(folder_index.is_direct) AS image_count, "
        "SUM(folder_index.is_direct) AS direct_image_count, "
        "COUNT(*) AS descendant_image_count, "
        "folder_catalog.first_indexed_at, folder_catalog.last_indexed_at, "
        "folder_catalog.timestamp_source, "
        "SUM(CASE WHEN folder_index.is_direct = 1 AND "
        "LENGTH(TRIM(entries.tags_json)) > 2 THEN 1 ELSE 0 END) "
        "AS manual_tagged_count, "
        "SUM(CASE WHEN folder_index.is_direct = 1 AND "
        "LENGTH(TRIM(entries.accepted_auto_tags_json)) > 2 "
        "THEN 1 ELSE 0 END) AS model_tagged_count, "
        "SUM(CASE WHEN folder_index.is_direct = 1 AND "
        "LENGTH(TRIM(entries.inherited_tags_json)) > 2 "
        "THEN 1 ELSE 0 END) AS inherited_tagged_count, "
        "SUM(CASE WHEN folder_index.is_direct = 1 AND "
        "document_annotations.status = 'failed' THEN 1 ELSE 0 END) "
        f"AS failed_count {grouped_from} "
        "ORDER BY folder_catalog.first_indexed_at DESC, "
        "roots.current_path COLLATE NOCASE, roots.current_path, "
        "folder_index.relative_folder COLLATE NOCASE, "
        "folder_index.relative_folder, folder_index.root_id "
        "LIMIT ? OFFSET ?"
    )
    return sql, [*values, limit, offset]


def legacy_list_folders(
    connection: sqlite3.Connection,
    *,
    root_id: str | None = None,
    query: str = "",
    offset: int = 0,
    limit: int = FOLDER_LIST_LIMIT,
) -> tuple[int, list[dict[str, Any]]]:
    """Frozen copy of the round-1 baseline folder-list query."""

    _validate_page(offset, limit, maximum=1_000)
    where_sql, values = _legacy_where(root_id, str(query).strip())
    count_row = connection.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 {_legacy_grouped_from(where_sql)})",
        values,
    ).fetchone()
    total = int(count_row[0]) if count_row else 0
    sql, parameters = _legacy_list_folders_sql(
        root_id=root_id, query=query, offset=offset, limit=limit
    )
    rows = connection.execute(sql, parameters)
    folders: list[dict[str, Any]] = []
    for row in rows:
        relative_folder = str(row["relative_folder"])
        root_name = Path(str(row["current_path"])).name or str(row["current_path"])
        folders.append(
            {
                "root_id": str(row["root_id"]),
                "root_name": root_name,
                "relative_folder": relative_folder,
                "name": (
                    relative_folder.rsplit("/", 1)[-1] if relative_folder else root_name
                ),
                "image_count": int(row["image_count"]),
                "direct_image_count": int(row["direct_image_count"] or 0),
                "descendant_image_count": int(row["descendant_image_count"] or 0),
                "first_indexed_at": str(row["first_indexed_at"]),
                "last_indexed_at": str(row["last_indexed_at"]),
                "timestamp_source": str(row["timestamp_source"]),
                "manual_tagged_count": int(row["manual_tagged_count"] or 0),
                "model_tagged_count": int(row["model_tagged_count"] or 0),
                "inherited_tagged_count": int(row["inherited_tagged_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
            }
        )
    return total, folders


def _legacy_folder_predicate(
    root_id: str,
    relative_folder: str,
    *,
    include_subfolders: bool,
) -> tuple[str, tuple[Any, ...]]:
    """Frozen copy of the round-1 baseline folder-entry predicate."""

    normalized_root = str(root_id).strip()
    normalized_folder = _normalized_relative_folder(relative_folder)
    if not include_subfolders:
        return (
            "entries.root_id = ? AND entries.parent_directory = ?",
            (normalized_root, normalized_folder),
        )
    if not normalized_folder:
        return "entries.root_id = ?", (normalized_root,)
    descendant_pattern = f"{_escape_like(normalized_folder)}/%"
    return (
        "entries.root_id = ? AND (entries.parent_directory = ? OR "
        "entries.parent_directory LIKE ? ESCAPE '\\')",
        (normalized_root, normalized_folder, descendant_pattern),
    )


def _legacy_page_sql(predicate: str) -> str:
    return (
        "SELECT entries.*, document_annotations.status AS annotation_status, "
        "document_annotations.policy_json AS annotation_policy_json "
        "FROM entries LEFT JOIN document_annotations "
        "ON document_annotations.doc_id = entries.doc_id "
        f"WHERE {predicate} "
        "ORDER BY entries.relative_path COLLATE NOCASE, entries.doc_id "
        "LIMIT ? OFFSET ?"
    )


def legacy_page_folder_entries(
    connection: sqlite3.Connection,
    root_id: str,
    relative_folder: str,
    *,
    include_subfolders: bool = False,
    offset: int = 0,
    limit: int = DEEP_PAGE_LIMIT,
) -> tuple[int, list[dict[str, Any]]]:
    """Frozen copy of the round-1 baseline folder-entry page query."""

    predicate, values = _legacy_folder_predicate(
        root_id, relative_folder, include_subfolders=include_subfolders
    )
    total = _count_folder_entries(
        connection,
        root_id,
        relative_folder,
        include_subfolders=include_subfolders,
    )
    rows = connection.execute(
        _legacy_page_sql(predicate), (*values, limit, offset)
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = IndexState._entry_from_row(row)
        entry["annotation_status"] = str(entry.get("annotation_status") or "")
        entry["annotation_policy"] = IndexState._decode_json_object(
            entry.pop("annotation_policy_json", "{}"), "annotation policy"
        )
        entries.append(entry)
    return total, entries


@dataclass(slots=True)
class CaseSpec:
    name: str
    call: Callable[[IndexStateReader], Any]
    legacy: Callable[[sqlite3.Connection], Any]


def _case_specs(scenario: Scenario, *, root_id: str, query_root: str) -> list[CaseSpec]:
    dense = _dense_folder(scenario.folders)
    dense_images = _dense_folder_images(scenario)
    middle = max(0, dense_images // 2)
    deepest = max(0, dense_images - DEEP_PAGE_LIMIT)
    list_deepest = max(0, scenario.folders - FOLDER_LIST_LIMIT)

    def direct_page(reader: IndexStateReader, offset: int) -> Any:
        return reader.page_folder_entries(
            root_id, dense, offset=offset, limit=DEEP_PAGE_LIMIT
        )

    def legacy_direct_page(connection: sqlite3.Connection, offset: int) -> Any:
        return legacy_page_folder_entries(
            connection, root_id, dense, offset=offset, limit=DEEP_PAGE_LIMIT
        )

    def recursive_page(reader: IndexStateReader) -> Any:
        return reader.page_folder_entries(
            root_id,
            "album-000",
            include_subfolders=True,
            offset=0,
            limit=DEEP_PAGE_LIMIT,
        )

    def legacy_recursive_page(connection: sqlite3.Connection) -> Any:
        return legacy_page_folder_entries(
            connection,
            root_id,
            "album-000",
            include_subfolders=True,
            offset=0,
            limit=DEEP_PAGE_LIMIT,
        )

    return [
        CaseSpec(
            "folder_page_first",
            lambda reader: direct_page(reader, 0),
            lambda connection: legacy_direct_page(connection, 0),
        ),
        CaseSpec(
            "folder_page_middle",
            lambda reader: direct_page(reader, middle),
            lambda connection: legacy_direct_page(connection, middle),
        ),
        CaseSpec(
            "folder_page_deepest",
            lambda reader: direct_page(reader, deepest),
            lambda connection: legacy_direct_page(connection, deepest),
        ),
        CaseSpec(
            "folder_page_beyond_end",
            lambda reader: direct_page(reader, dense_images + 10),
            lambda connection: legacy_direct_page(connection, dense_images + 10),
        ),
        CaseSpec(
            "folder_page_recursive",
            recursive_page,
            legacy_recursive_page,
        ),
        CaseSpec(
            "folder_page_missing",
            lambda reader: reader.page_folder_entries(
                root_id, "album-999/missing", offset=0, limit=DEEP_PAGE_LIMIT
            ),
            lambda connection: legacy_page_folder_entries(
                connection,
                root_id,
                "album-999/missing",
                offset=0,
                limit=DEEP_PAGE_LIMIT,
            ),
        ),
        CaseSpec(
            "folder_list_first",
            lambda reader: reader.list_folders(offset=0, limit=FOLDER_LIST_LIMIT),
            lambda connection: legacy_list_folders(
                connection, offset=0, limit=FOLDER_LIST_LIMIT
            ),
        ),
        CaseSpec(
            "folder_list_deep",
            lambda reader: reader.list_folders(
                offset=list_deepest, limit=FOLDER_LIST_LIMIT
            ),
            lambda connection: legacy_list_folders(
                connection, offset=list_deepest, limit=FOLDER_LIST_LIMIT
            ),
        ),
        CaseSpec(
            "folder_list_basename_query",
            lambda reader: reader.list_folders(query="set-0007", limit=10),
            lambda connection: legacy_list_folders(
                connection, query="set-0007", limit=10
            ),
        ),
        CaseSpec(
            "folder_list_root_query",
            lambda reader: reader.list_folders(query=query_root, limit=10),
            lambda connection: legacy_list_folders(
                connection, query=query_root, limit=10
            ),
        ),
        CaseSpec(
            "folder_list_no_match",
            lambda reader: reader.list_folders(query="zzz-none", limit=10),
            lambda connection: legacy_list_folders(
                connection, query="zzz-none", limit=10
            ),
        ),
        CaseSpec(
            "folder_list_root_filter",
            lambda reader: reader.list_folders(
                root_id=root_id, offset=0, limit=FOLDER_LIST_LIMIT
            ),
            lambda connection: legacy_list_folders(
                connection, root_id=root_id, offset=0, limit=FOLDER_LIST_LIMIT
            ),
        ),
    ]


def _normalize(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(_normalize(item) for item in value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _new_reader_call(state_path: Path, call: Callable[[IndexStateReader], Any]) -> Any:
    reader = IndexStateReader(state_path)
    try:
        return call(reader)
    finally:
        reader.close()


def _read_call(
    spec: CaseSpec, state_path: Path, repeats: int, *, legacy: bool
) -> dict[str, Any]:
    if legacy:
        # ``LibraryBrowser`` opens a short-lived reader per request, so the
        # baseline must pay the same connection setup to stay comparable.  It
        # reuses ``IndexStateReader`` so both paths prepare the connection
        # identically; only the SQL differs.
        def call() -> Any:
            reader = IndexStateReader(state_path)
            try:
                return spec.legacy(reader.connection)
            finally:
                reader.close()

        output = _normalize(call())
        timing = _measure(call, repeats)
        return {"output": output, "timing": timing}
    output = _normalize(_new_reader_call(state_path, spec.call))
    timing = _measure(lambda: _new_reader_call(state_path, spec.call), repeats)
    return {"output": output, "timing": timing}


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _measure(action: Callable[[], Any], repeats: int) -> dict[str, float]:
    """Time ``action`` ``repeats`` times, one sample per call.

    Every sample opens its own connection, and ``action`` already ran once to
    produce the case output that was compared for equivalence.  The reported
    ``first_sample_ms`` is therefore the first sample after that warm-up read:
    the OS page cache and the database pages are already hot, so it is *not* a
    cold-cache measurement.  Only ``p50``/``p95``/``min``/``max`` describe the
    steady state.
    """

    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started) * 1_000)
    return {
        "first_sample_ms": samples[0],
        "p50_ms": _percentile(samples, 0.5),
        "p95_ms": _percentile(samples, 0.95),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def _query_plan(connection: sqlite3.Connection, sql: str, values: Sequence[Any]) -> str:
    return " | ".join(
        str(row["detail"])
        for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", values)
    )


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _open_readonly(state_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _executed_plans(spec: CaseSpec, state_path: Path) -> list[dict[str, str]]:
    """Explain every SELECT the current implementation runs for one case."""

    reader = IndexStateReader(state_path)
    statements: list[str] = []
    reader.connection.set_trace_callback(statements.append)
    try:
        spec.call(reader)
    finally:
        reader.connection.set_trace_callback(None)
        reader.close()
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    plans: list[dict[str, str]] = []
    connection = _open_readonly(state_path)
    try:
        for statement in selects:
            parameters = tuple("x" for _ in range(statement.count("?")))
            plans.append(
                {
                    "sql": _compact(statement),
                    "plan": _query_plan(connection, statement, parameters),
                }
            )
    finally:
        connection.close()
    return plans


def _baseline_plans(
    scenario: Scenario, state_path: Path, *, root_id: str
) -> dict[str, str]:
    dense = _dense_folder(scenario.folders)
    predicate, page_values = _legacy_folder_predicate(
        root_id, dense, include_subfolders=False
    )
    list_sql, list_values = _legacy_list_folders_sql(offset=0, limit=FOLDER_LIST_LIMIT)
    plans: dict[str, str] = {}
    connection = _open_readonly(state_path)
    try:
        plans["page_baseline"] = _query_plan(
            connection,
            _legacy_page_sql(predicate),
            (*page_values, DEEP_PAGE_LIMIT, 0),
        )
        plans["list_page_baseline"] = _query_plan(connection, list_sql, list_values)
    finally:
        connection.close()
    return plans


def _database_bytes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def _vacuum(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _copy_database(source: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(source)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()


def _has_index(path: Path, name: str) -> bool:
    connection = _open_readonly(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def _connection_has_index(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _write_cost(
    state_path: Path, scenario: Scenario, *, with_index: bool
) -> dict[str, Any]:
    """Measure one representative metadata batch on an existing database.

    ``with_index=False`` drops ``idx_entries_folder_page_nocase`` *after* the
    state database is open: opening an :class:`IndexState` recreates a missing
    index, so dropping the file beforehand would be undone and the write would
    silently run with the index.  The batch is timed on that same open state,
    and the index presence is recorded before and after the batch so the
    reported number really reflects the requested index state.
    """

    state = IndexState(state_path)
    try:
        if not with_index:
            state.connection.execute(f"DROP INDEX IF EXISTS {FOLDER_PAGE_INDEX}")
            state.connection.commit()
        index_present_before = _connection_has_index(
            state.connection, FOLDER_PAGE_INDEX
        )
        entries = [
            _entry(
                f"doc-write-{index:06d}",
                "root-0",
                f"{_dense_folder(scenario.folders)}/write-{index:06d}.jpg",
                9_000_000 + index,
            )
            for index in range(WRITE_BATCH_SIZE)
        ]
        started = time.perf_counter()
        state.set_many(entries)
        elapsed = (time.perf_counter() - started) * 1_000
        index_present_after = _connection_has_index(state.connection, FOLDER_PAGE_INDEX)
    finally:
        state.close()
    return {
        "batch_rows": WRITE_BATCH_SIZE,
        "batch_ms": elapsed,
        "index_present_before": index_present_before,
        "index_present_after": index_present_after,
    }


def _time_reopen(state_path: Path) -> float:
    started = time.perf_counter()
    state = IndexState(state_path)
    state.close()
    return time.perf_counter() - started


def _round_timing(timing: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 3) for key, value in timing.items()}


def _row_count(output: Any) -> int | None:
    if isinstance(output, tuple) and len(output) == 2 and isinstance(output[1], list):
        return len(output[1])
    return None


def _total(output: Any) -> int | None:
    if isinstance(output, tuple) and output and isinstance(output[0], int):
        return output[0]
    return None


def run_scenario(scenario: Scenario, work_root: Path, *, keep: bool) -> dict[str, Any]:
    # Every run works inside its own fresh temporary subdirectory of the given
    # root so cleanup can never delete files the caller already had there.
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = work_root / f"{scenario.name}-{uuid.uuid4().hex[:12]}"
    run_dir.mkdir()
    try:
        full_path = run_dir / f"{scenario.name}-full.sqlite3"
        noindex_path = run_dir / f"{scenario.name}-noindex.sqlite3"

        state = IndexState(full_path)
        try:
            state.ensure_collection_uuid("benchmark-collection")
            for root_index in range(scenario.roots):
                state.record_root(
                    f"root-{root_index}",
                    str(run_dir / "photos" / f"root-{root_index}"),
                    True,
                )
            build = build_library(state, scenario)
            index_present = _has_index(full_path, FOLDER_PAGE_INDEX)
        finally:
            state.close()

        _vacuum(full_path)
        size_full = _database_bytes(full_path)
        _copy_database(full_path, noindex_path)
        connection = sqlite3.connect(noindex_path)
        try:
            connection.execute(f"DROP INDEX IF EXISTS {FOLDER_PAGE_INDEX}")
            connection.commit()
        finally:
            connection.close()
        _vacuum(noindex_path)
        size_noindex = _database_bytes(noindex_path)

        reader = IndexStateReader(full_path)
        try:
            catalog_folders = int(
                reader.connection.execute(
                    "SELECT COUNT(*) FROM folder_catalog"
                ).fetchone()[0]
            )
            index_rows = int(
                reader.connection.execute(
                    "SELECT COUNT(*) FROM entry_folder_index"
                ).fetchone()[0]
            )
            rows = int(
                reader.connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            )
        finally:
            reader.close()

        root_id = "root-0"
        query_root = f"root-{scenario.roots - 1}"
        specs = _case_specs(scenario, root_id=root_id, query_root=query_root)
        by_name = {spec.name: spec for spec in specs}

        report: dict[str, Any] = {
            "scenario": asdict(scenario),
            "database": {
                "root_ids": [f"root-{index}" for index in range(scenario.roots)],
                "entries": rows,
                "catalog_folders": catalog_folders,
                "entry_folder_index_rows": index_rows,
                "folder_page_index_present": index_present,
                "bytes_with_index": size_full,
                "bytes_without_index": size_noindex,
                "index_bytes": size_full - size_noindex,
                "bulk_insert": build,
            },
            "cases": {},
            "equivalence": {},
        }

        for spec in specs:
            after = _read_call(spec, full_path, scenario.repeats, legacy=False)
            before = _read_call(spec, noindex_path, scenario.repeats, legacy=True)
            after_noindex = _read_call(
                spec, noindex_path, scenario.repeats, legacy=False
            )
            before_index = _read_call(spec, full_path, scenario.repeats, legacy=True)
            reference = before["output"]
            outputs = {
                "new_sql_with_index": after["output"],
                "new_sql_without_index": after_noindex["output"],
                "baseline_sql_with_index": before_index["output"],
            }
            mismatch = sorted(
                name for name, value in outputs.items() if value != reference
            )
            report["equivalence"][spec.name] = mismatch if mismatch else "identical"
            report["cases"][spec.name] = {
                "rows_returned": _row_count(after["output"]),
                "total": _total(after["output"]),
                "new_sql_with_index": _round_timing(after["timing"]),
                "new_sql_without_index": _round_timing(after_noindex["timing"]),
                "baseline_sql_without_index": _round_timing(before["timing"]),
                "baseline_sql_with_index": _round_timing(before_index["timing"]),
            }

        report["plans"] = _baseline_plans(scenario, noindex_path, root_id=root_id)
        for name, key in (
            ("folder_page_first", "page_current"),
            ("folder_list_first", "list_page_current"),
        ):
            report["plans"][f"{key}_without_index"] = _executed_plans(
                by_name[name], noindex_path
            )
            report["plans"][f"{key}_with_index"] = _executed_plans(
                by_name[name], full_path
            )

        report["index_build"] = {
            "reopen_seconds_without_index": _time_reopen(noindex_path),
            "reopen_seconds_with_index": _time_reopen(full_path),
        }
        report["index_build"]["index_build_seconds"] = (
            report["index_build"]["reopen_seconds_without_index"]
            - report["index_build"]["reopen_seconds_with_index"]
        )
        report["write_cost"] = {
            "with_index": _write_cost(full_path, scenario, with_index=True),
            "without_index": _write_cost(noindex_path, scenario, with_index=False),
        }
        report["work_dir"] = str(run_dir)
        report["work_dir_kept"] = keep
        return report
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="small",
        help="synthetic library size preset",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("build") / "benchmark_gallery_queries",
        help=(
            "scratch root; each run creates its own temporary subdirectory "
            "there so existing files are never touched"
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep this run's temporary directory (its path is reported)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional JSON report path (stdout is always written)",
    )
    arguments = parser.parse_args(argv)

    scenario = SCENARIOS[arguments.scenario]
    report = run_scenario(scenario, arguments.work_dir, keep=arguments.keep)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    mismatches = {
        name: value
        for name, value in report["equivalence"].items()
        if value != "identical"
    }
    if mismatches:
        print(f"output mismatch: {json.dumps(mismatches)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
