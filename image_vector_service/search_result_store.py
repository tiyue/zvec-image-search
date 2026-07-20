from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

RESULT_STORE_FILENAME = "results.sqlite3"
RESULT_STORE_KIND = "sqlite"
RESULT_STORE_SCHEMA_VERSION = 2
SUPPORTED_RESULT_STORE_SCHEMA_VERSIONS = frozenset({1, 2})
RESULT_STORE_APPLICATION_ID = 0x5A565243  # "ZVRC"
RESULT_STORE_WRITE_BATCH = 1_000
MAX_RESULT_PAGE_SIZE = 10_000
MAX_SQLITE_INTEGER = (1 << 63) - 1

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SearchResultStoreError(RuntimeError):
    """Raised when an immutable paged search-result store is invalid."""


@dataclass(frozen=True, slots=True)
class ResultStoreReference:
    """Portable manifest reference to one result session in a sidecar store."""

    session_id: str
    result_count: int
    path: str = RESULT_STORE_FILENAME
    kind: str = RESULT_STORE_KIND
    schema_version: int = RESULT_STORE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "path": self.path,
            "session_id": self.session_id,
            "result_count": self.result_count,
        }


@dataclass(frozen=True, slots=True)
class StoredResult:
    rank: int
    payload: dict[str, Any]


def parse_result_store_reference(value: Any) -> ResultStoreReference:
    """Validate a manifest reference without touching the filesystem."""

    if not isinstance(value, Mapping):
        raise SearchResultStoreError("result_store must be an object")
    kind = value.get("kind")
    if kind != RESULT_STORE_KIND:
        raise SearchResultStoreError(f"unsupported result_store kind: {kind!r}")
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_RESULT_STORE_SCHEMA_VERSIONS
    ):
        raise SearchResultStoreError(
            f"unsupported result_store schema_version: {schema_version!r}"
        )
    path = value.get("path")
    # Sidecars are deliberately restricted to one direct child. This prevents
    # a crafted manifest from opening an arbitrary SQLite database.
    if path != RESULT_STORE_FILENAME:
        raise SearchResultStoreError(
            f"result_store.path must be {RESULT_STORE_FILENAME!r}"
        )
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise SearchResultStoreError("result_store.session_id is invalid")
    result_count = value.get("result_count")
    if (
        isinstance(result_count, bool)
        or not isinstance(result_count, int)
        or result_count < 0
    ):
        raise SearchResultStoreError(
            "result_store.result_count must be a non-negative integer"
        )
    return ResultStoreReference(
        session_id=session_id,
        result_count=result_count,
        path=path,
        kind=kind,
        schema_version=schema_version,
    )


def write_result_store(
    path: Path,
    *,
    session_id: str,
    created_at: str,
    results: Iterable[Mapping[str, Any]],
) -> ResultStoreReference:
    """Write one immutable result session with bounded Python memory usage.

    The database is created beside its final name and atomically published only
    after the transaction commits. Readers therefore see either no sidecar or a
    complete sidecar, never a half-written search result.
    """

    reference = parse_result_store_reference(
        {
            "kind": RESULT_STORE_KIND,
            "schema_version": RESULT_STORE_SCHEMA_VERSION,
            "path": RESULT_STORE_FILENAME,
            "session_id": session_id,
            "result_count": 0,
        }
    )
    if path.name != RESULT_STORE_FILENAME:
        raise SearchResultStoreError(
            f"result store filename must be {RESULT_STORE_FILENAME!r}"
        )
    if not isinstance(created_at, str) or not created_at.strip():
        raise SearchResultStoreError("created_at must be a non-empty string")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SearchResultStoreError(
            f"could not create result store directory: {exc}"
        ) from exc
    temporary_path = path.with_name(f"{path.name}.tmp")
    _remove_temporary_store(temporary_path)

    connection: sqlite3.Connection | None = None
    result_count = 0
    try:
        connection = sqlite3.connect(str(temporary_path), timeout=30.0)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA application_id = {RESULT_STORE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {RESULT_STORE_SCHEMA_VERSION}")
        connection.executescript(
            """
            CREATE TABLE search_sessions (
                session_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                result_count INTEGER NOT NULL CHECK (result_count >= 0)
            ) WITHOUT ROWID;

            CREATE TABLE result_items (
                session_id TEXT NOT NULL,
                rank INTEGER NOT NULL CHECK (rank >= 1),
                copied_file TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (session_id, rank),
                FOREIGN KEY (session_id) REFERENCES search_sessions(session_id)
                    ON DELETE CASCADE
            ) WITHOUT ROWID;

            """
        )
        connection.execute(
            "INSERT INTO search_sessions(session_id, created_at, result_count) "
            "VALUES (?, ?, 0)",
            (reference.session_id, created_at),
        )

        batch: list[tuple[str, int, str | None, str]] = []
        expected_rank = 1
        for raw_result in results:
            payload = _validated_result_payload(raw_result, expected_rank)
            copied_value = payload.get("copied_file")
            copied_file = str(copied_value) if copied_value is not None else None
            batch.append(
                (
                    reference.session_id,
                    expected_rank,
                    copied_file,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
            result_count += 1
            expected_rank += 1
            if len(batch) >= RESULT_STORE_WRITE_BATCH:
                _insert_batch(connection, batch)
                batch.clear()
        if batch:
            _insert_batch(connection, batch)

        connection.execute(
            "UPDATE search_sessions SET result_count = ? WHERE session_id = ?",
            (result_count, reference.session_id),
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary_path, path)
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        SearchResultStoreError,
    ) as exc:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.rollback()
            connection.close()
        _remove_temporary_store(temporary_path)
        if isinstance(exc, SearchResultStoreError):
            raise
        raise SearchResultStoreError(f"could not write result store: {exc}") from exc

    return ResultStoreReference(
        session_id=reference.session_id,
        result_count=result_count,
    )


def result_count(path: Path, session_id: str) -> int:
    """Read the O(1) session count recorded during publication."""

    try:
        with closing(_read_connection(path)) as connection:
            _validate_store_header(connection)
            row = connection.execute(
                "SELECT result_count FROM search_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise SearchResultStoreError(f"could not read result store: {exc}") from exc
    if row is None:
        raise SearchResultStoreError(f"result session does not exist: {session_id}")
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SearchResultStoreError("result session count is invalid")
    return value


def read_result_range(
    path: Path,
    *,
    session_id: str,
    start_rank: int,
    limit: int,
) -> list[StoredResult]:
    """Read a rank range without OFFSET or loading preceding rows."""

    _validate_range(start_rank=start_rank, limit=limit)
    try:
        with closing(_read_connection(path)) as connection:
            _validate_store_header(connection)
            rows = connection.execute(
                "SELECT rank, payload_json FROM result_items "
                "WHERE session_id = ? AND rank >= ? "
                "ORDER BY rank ASC LIMIT ?",
                (session_id, start_rank, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        raise SearchResultStoreError(f"could not read result store: {exc}") from exc
    return [_decode_result_row(row, session_id=session_id) for row in rows]


def read_results_after(
    path: Path,
    *,
    session_id: str,
    after_rank: int,
    limit: int,
) -> list[StoredResult]:
    """Cursor-style page read for API consumers that do not use page numbers."""

    if (
        isinstance(after_rank, bool)
        or not isinstance(after_rank, int)
        or after_rank < 0
    ):
        raise SearchResultStoreError("after_rank must be a non-negative integer")
    return read_result_range(
        path,
        session_id=session_id,
        start_rank=after_rank + 1,
        limit=limit,
    )


def iter_copied_files(
    path: Path,
    *,
    session_id: str,
    batch_size: int = RESULT_STORE_WRITE_BATCH,
) -> Iterator[str]:
    """Stream copied filenames for conservative ownership validation/cleanup."""

    for copied_file in iter_result_file_references(
        path, session_id=session_id, batch_size=batch_size
    ):
        if copied_file is not None:
            yield copied_file


def iter_result_file_references(
    path: Path,
    *,
    session_id: str,
    batch_size: int = RESULT_STORE_WRITE_BATCH,
) -> Iterator[str | None]:
    """Stream every row, using ``None`` for source-only results."""

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_RESULT_PAGE_SIZE
    ):
        raise SearchResultStoreError(
            f"batch_size must be between 1 and {MAX_RESULT_PAGE_SIZE}"
        )
    connection = _read_connection(path)
    try:
        _validate_store_header(connection)
        cursor = connection.execute(
            "SELECT copied_file FROM result_items "
            "WHERE session_id = ? ORDER BY rank ASC",
            (session_id,),
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                copied_file = row[0]
                if copied_file is not None and not isinstance(copied_file, str):
                    raise SearchResultStoreError(
                        "result store contains an invalid copied filename"
                    )
                yield copied_file
    except sqlite3.Error as exc:
        raise SearchResultStoreError(f"could not read result store: {exc}") from exc
    finally:
        connection.close()


def _insert_batch(
    connection: sqlite3.Connection,
    rows: list[tuple[str, int, str | None, str]],
) -> None:
    connection.executemany(
        "INSERT INTO result_items(session_id, rank, copied_file, payload_json) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def _validated_result_payload(
    raw_result: Mapping[str, Any], expected_rank: int
) -> dict[str, Any]:
    if not isinstance(raw_result, Mapping):
        raise SearchResultStoreError("each search result must be an object")
    payload = dict(raw_result)
    rank = payload.get("rank")
    if rank != expected_rank:
        raise SearchResultStoreError(
            f"result rank must be contiguous; expected {expected_rank}, got {rank!r}"
        )
    copied_file = payload.get("copied_file")
    if copied_file is None:
        _validate_source_only_payload(payload, expected_rank)
    elif (
        not isinstance(copied_file, str)
        or not copied_file.strip()
        or copied_file != Path(copied_file).name
        or "/" in copied_file
        or "\\" in copied_file
    ):
        raise SearchResultStoreError(
            f"result {expected_rank} copied_file must be a direct filename"
        )
    return payload


def _validate_source_only_payload(payload: Mapping[str, Any], rank: int) -> None:
    """Require enough logical identity to resolve a result without a copy."""

    for field_name in ("library_id", "root_id"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise SearchResultStoreError(
                f"result {rank} source-only {field_name} must be a non-empty string"
            )
    relative_path = payload.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise SearchResultStoreError(
            f"result {rank} source-only relative_path must be a non-empty string"
        )
    windows_path = PureWindowsPath(relative_path)
    posix_path = PurePosixPath(relative_path.replace("\\", "/"))
    if (
        windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise SearchResultStoreError(
            f"result {rank} source-only relative_path must stay below its image root"
        )


def _validate_range(*, start_rank: int, limit: int) -> None:
    if (
        isinstance(start_rank, bool)
        or not isinstance(start_rank, int)
        or start_rank < 1
        or start_rank > MAX_SQLITE_INTEGER
    ):
        raise SearchResultStoreError("start_rank must be a positive integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_RESULT_PAGE_SIZE
    ):
        raise SearchResultStoreError(
            f"limit must be between 1 and {MAX_RESULT_PAGE_SIZE}"
        )


def _decode_result_row(row: tuple[object, object], *, session_id: str) -> StoredResult:
    rank, payload_json = row
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise SearchResultStoreError(
            f"result session {session_id} contains an invalid rank"
        )
    if not isinstance(payload_json, str):
        raise SearchResultStoreError(
            f"result session {session_id} contains an invalid payload"
        )
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise SearchResultStoreError(
            f"result session {session_id} contains invalid JSON at rank {rank}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("rank") != rank:
        raise SearchResultStoreError(
            f"result session {session_id} payload rank mismatch at rank {rank}"
        )
    return StoredResult(rank=rank, payload=payload)


def _read_connection(path: Path) -> sqlite3.Connection:
    if _is_link_like(path):
        raise SearchResultStoreError(f"result store must not be a link: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SearchResultStoreError(f"result store does not exist: {path}") from exc
    if not resolved.is_file() or _is_link_like(resolved):
        raise SearchResultStoreError(
            f"result store must be a regular non-link file: {resolved}"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise SearchResultStoreError(f"could not open result store: {exc}") from exc


def _validate_store_header(connection: sqlite3.Connection) -> None:
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError, IndexError) as exc:
        raise SearchResultStoreError("result store header is invalid") from exc
    if application_id != RESULT_STORE_APPLICATION_ID:
        raise SearchResultStoreError("result store application id is invalid")
    if schema_version not in SUPPORTED_RESULT_STORE_SCHEMA_VERSIONS:
        raise SearchResultStoreError(
            f"unsupported result store schema version: {schema_version}"
        )


def _remove_temporary_store(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-journal"),
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        # Cleanup is best effort. The original write error remains authoritative;
        # any stale temporary file is undeclared, so ownership cleanup fails closed.
        with suppress(OSError):
            candidate.unlink(missing_ok=True)


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))
