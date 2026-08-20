from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .config import ConfigurationError
from .models import ImageRecord

SCAN_STAGING_SCHEMA_VERSION = 1
SCAN_STAGING_APPLICATION_ID = 0x5A565353  # "ZVSS"
SCAN_STAGING_PREFIX = "scan-staging-v1-"
SCAN_STAGING_SUFFIX = ".sqlite3"
DEFAULT_SCAN_WRITE_BATCH_SIZE = 256
MIN_SCAN_WRITE_BATCH_SIZE = 100
MAX_SCAN_WRITE_BATCH_SIZE = 500
DEFAULT_ABANDONED_AFTER_SECONDS = 24 * 60 * 60

ScanStagingStatus = Literal[
    "scanning",
    "ready",
    "consumed",
    "failed",
    "cancelled",
]

_RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_STAGING_NAME_PATTERN = re.compile(
    rf"^{re.escape(SCAN_STAGING_PREFIX)}([a-f0-9]{{32}})"
    rf"{re.escape(SCAN_STAGING_SUFFIX)}$"
)
_VALID_STATUSES = {"scanning", "ready", "consumed", "failed", "cancelled"}
_ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "scanning": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset({"consumed", "failed", "cancelled"}),
    "consumed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_REQUIRED_SCHEMA_COLUMNS: dict[str, frozenset[str]] = {
    "scan_metadata": frozenset(
        {
            "singleton",
            "schema_version",
            "run_id",
            "root_id",
            "status",
            "owner_pid",
            "created_at",
            "updated_at",
            "scanned",
            "supported",
            "skipped",
            "peak_in_flight",
            "complete",
            "failure_count",
            "record_count",
            "seen_count",
            "fast_unchanged_count",
            "write_batch_count",
            "max_write_batch",
        }
    ),
    "seen_documents": frozenset(
        {"doc_id", "root_id", "relative_path", "fast_unchanged"}
    ),
    "scan_records": frozenset(
        {
            "root_id",
            "relative_path",
            "doc_id",
            "sequence",
            "file_name",
            "extension",
            "mime_type",
            "sha256",
            "size_bytes",
            "mtime_ns",
            "width",
            "height",
            "fast_unchanged",
        }
    ),
    "content_groups": frozenset(
        {
            "sha256",
            "representative_root_id",
            "representative_relative_path",
            "representative_doc_id",
            "member_count",
            "fast_unchanged_count",
        }
    ),
}


class ScanStagingError(RuntimeError):
    """Raised when a scan staging database is invalid or cannot be used."""


@dataclass(frozen=True, slots=True, order=True)
class ScanPathKey:
    root_id: str
    relative_path: str
    doc_id: str


@dataclass(frozen=True, slots=True, order=True)
class ScanSha256Key:
    sha256: str
    root_id: str
    relative_path: str
    doc_id: str


@dataclass(frozen=True, slots=True)
class SeenScanDocument:
    doc_id: str
    root_id: str
    relative_path: str
    fast_unchanged: bool = False


@dataclass(frozen=True, slots=True)
class StagedImageRecord:
    """Portable scan record.

    Absolute source paths are deliberately excluded from the SQLite schema.
    A caller reconstructs a path only while consuming a bounded batch and must
    supply the currently registered root.
    """

    sequence: int
    doc_id: str
    root_id: str
    relative_path: str
    file_name: str
    extension: str
    mime_type: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    width: int
    height: int
    fast_unchanged: bool = False

    @classmethod
    def from_image_record(
        cls,
        sequence: int,
        record: ImageRecord,
        *,
        fast_unchanged: bool = False,
    ) -> StagedImageRecord:
        return cls(
            sequence=sequence,
            doc_id=record.doc_id,
            root_id=record.root_id,
            relative_path=_portable_relative_path(record.relative_path),
            file_name=record.file_name,
            extension=record.extension,
            mime_type=record.mime_type,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            mtime_ns=record.mtime_ns,
            width=record.width,
            height=record.height,
            fast_unchanged=fast_unchanged,
        )

    @property
    def path_key(self) -> ScanPathKey:
        return ScanPathKey(self.root_id, self.relative_path, self.doc_id)

    @property
    def sha256_key(self) -> ScanSha256Key:
        return ScanSha256Key(
            self.sha256,
            self.root_id,
            self.relative_path,
            self.doc_id,
        )

    def to_image_record(self, root_path: str | Path) -> ImageRecord:
        root = Path(root_path).expanduser().resolve()
        relative = PurePosixPath(_portable_relative_path(self.relative_path))
        absolute_path = root.joinpath(*relative.parts)
        return ImageRecord(
            doc_id=self.doc_id,
            root_id=self.root_id,
            relative_path=self.relative_path,
            absolute_path=str(absolute_path),
            file_name=self.file_name,
            extension=self.extension,
            mime_type=self.mime_type,
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            mtime_ns=self.mtime_ns,
            width=self.width,
            height=self.height,
        )


@dataclass(frozen=True, slots=True)
class ScanContentGroup:
    sha256: str
    representative: StagedImageRecord
    member_count: int
    fast_unchanged_count: int

    @property
    def representative_root_id(self) -> str:
        return self.representative.root_id

    @property
    def representative_relative_path(self) -> str:
        return self.representative.relative_path

    @property
    def representative_doc_id(self) -> str:
        return self.representative.doc_id


@dataclass(frozen=True, slots=True)
class ScanContentChunk:
    """One bounded slice of a globally unique SHA-256 content group."""

    sha256: str
    records: tuple[StagedImageRecord, ...]
    is_first: bool
    is_last: bool


@dataclass(slots=True)
class ScanIterationStats:
    select_count: int = 0
    yielded_rows: int = 0
    max_materialized_rows: int = 0

    def record_page(self, row_count: int) -> None:
        self.select_count += 1
        self.yielded_rows += row_count
        self.max_materialized_rows = max(self.max_materialized_rows, row_count)


@dataclass(frozen=True, slots=True)
class ScanStagingMetadata:
    run_id: str
    root_id: str
    status: str
    owner_pid: int
    created_at: float
    updated_at: float
    scanned: int
    supported: int
    skipped: int
    peak_in_flight: int
    complete: bool
    failure_count: int
    record_count: int
    seen_count: int
    fast_unchanged_count: int
    write_batch_count: int
    max_write_batch: int


@dataclass(frozen=True, slots=True)
class ScanStagingArtifact:
    path: Path
    run_id: str
    root_id: str
    status: str
    owner_pid: int
    created_at: float
    updated_at: float
    record_count: int
    abandoned: bool
    error: str = ""


class ScanStaging:
    """Crash-identifiable, bounded-memory SQLite storage for one root scan.

    The database contains content hashes and relative paths, so its directory
    still belongs in the application's private workspace. It never persists a
    registered root path or an absolute image path.
    """

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = connection

    @classmethod
    def create(
        cls,
        directory: str | Path,
        *,
        root_id: str,
        run_id: str | None = None,
    ) -> ScanStaging:
        normalized_root_id = _validate_root_id(root_id)
        normalized_run_id = _validate_run_id(run_id or uuid.uuid4().hex)
        parent = Path(directory).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / _staging_filename(normalized_run_id)
        if path.exists():
            raise ScanStagingError(f"scan staging already exists: {path.name}")

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, timeout=30.0, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute(f"PRAGMA application_id={SCAN_STAGING_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCAN_STAGING_SCHEMA_VERSION}")
            _create_schema(connection)
            now = time.time()
            connection.execute(
                "INSERT INTO scan_metadata("
                "singleton, schema_version, run_id, root_id, status, owner_pid, "
                "created_at, updated_at"
                ") VALUES (1, ?, ?, ?, 'scanning', ?, ?, ?)",
                (
                    SCAN_STAGING_SCHEMA_VERSION,
                    normalized_run_id,
                    normalized_root_id,
                    os.getpid(),
                    now,
                    now,
                ),
            )
            connection.commit()
            with suppress(OSError):
                path.chmod(0o600)
            return cls(path, connection)
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.close()
            _remove_staging_files(path)
            raise ScanStagingError(f"could not create scan staging: {exc}") from exc

    @classmethod
    def open(cls, path: str | Path) -> ScanStaging:
        resolved = _validated_staging_path(path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(resolved, timeout=30.0, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            _validate_database(connection)
            _validate_filename_identity(connection, resolved)
            return cls(resolved, connection)
        except (sqlite3.Error, ScanStagingError, ValueError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, ScanStagingError):
                raise
            raise ScanStagingError(f"could not open scan staging: {exc}") from exc

    def __enter__(self) -> ScanStaging:
        self._require_connection()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        with suppress(sqlite3.Error):
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        self._connection = None

    def discard(self) -> None:
        self.close()
        _remove_staging_files(self.path)

    def metadata(self) -> ScanStagingMetadata:
        row = (
            self._require_connection()
            .execute("SELECT * FROM scan_metadata WHERE singleton=1")
            .fetchone()
        )
        if row is None:
            raise ScanStagingError("scan staging metadata is missing")
        return _metadata_from_row(row)

    def append_batch(
        self,
        *,
        seen_documents: Sequence[SeenScanDocument] = (),
        records: Sequence[StagedImageRecord] = (),
        scanned: int | None = None,
        supported: int | None = None,
        skipped: int | None = None,
        peak_in_flight: int | None = None,
        complete: bool | None = None,
        failure_count: int | None = None,
    ) -> None:
        """Commit at most 500 seen/valid records as one durable scan batch."""

        seen_batch = tuple(seen_documents)
        record_batch = tuple(records)
        largest_batch = max(len(seen_batch), len(record_batch))
        if largest_batch == 0 and all(
            value is None
            for value in (
                scanned,
                supported,
                skipped,
                peak_in_flight,
                complete,
                failure_count,
            )
        ):
            return
        if largest_batch > MAX_SCAN_WRITE_BATCH_SIZE:
            raise ValueError(
                f"scan staging batches cannot exceed {MAX_SCAN_WRITE_BATCH_SIZE} rows"
            )

        metadata = self.metadata()
        if metadata.status != "scanning":
            raise ScanStagingError(
                f"cannot append to scan staging with status {metadata.status!r}"
            )
        _validate_batch(metadata.root_id, seen_batch, record_batch)
        connection = self._require_connection()
        try:
            with connection:
                if seen_batch:
                    connection.executemany(
                        "INSERT INTO seen_documents("
                        "doc_id, root_id, relative_path, fast_unchanged"
                        ") VALUES (?, ?, ?, ?)",
                        (
                            (
                                item.doc_id,
                                item.root_id,
                                _portable_relative_path(item.relative_path),
                                int(item.fast_unchanged),
                            )
                            for item in seen_batch
                        ),
                    )
                if record_batch:
                    connection.executemany(
                        "INSERT INTO scan_records("
                        "root_id, relative_path, doc_id, sequence, file_name, "
                        "extension, mime_type, sha256, size_bytes, mtime_ns, "
                        "width, height, fast_unchanged"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_record_values(item) for item in record_batch),
                    )
                updates = {
                    "scanned": scanned,
                    "supported": supported,
                    "skipped": skipped,
                    "peak_in_flight": peak_in_flight,
                    "complete": None if complete is None else int(complete),
                    "failure_count": failure_count,
                }
                assignments: list[str] = ["updated_at=?", "owner_pid=?"]
                parameters: list[object] = [time.time(), os.getpid()]
                for name, value in updates.items():
                    if value is not None:
                        assignments.append(f"{name}=?")
                        parameters.append(value)
                if seen_batch:
                    assignments.append("seen_count=seen_count+?")
                    assignments.append("fast_unchanged_count=fast_unchanged_count+?")
                    parameters.extend(
                        [
                            len(seen_batch),
                            sum(int(item.fast_unchanged) for item in seen_batch),
                        ]
                    )
                if record_batch:
                    assignments.append("record_count=record_count+?")
                    parameters.append(len(record_batch))
                if largest_batch:
                    assignments.extend(
                        [
                            "write_batch_count=write_batch_count+1",
                            "max_write_batch=MAX(max_write_batch, ?)",
                        ]
                    )
                    parameters.append(largest_batch)
                parameters.append(1)
                connection.execute(
                    f"UPDATE scan_metadata SET {', '.join(assignments)} "
                    "WHERE singleton=?",
                    parameters,
                )
        except sqlite3.IntegrityError as exc:
            raise ScanStagingError(
                f"invalid or duplicate staged scan record: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise ScanStagingError(
                f"could not append scan staging batch: {exc}"
            ) from exc

    def mark_ready(
        self,
        *,
        scanned: int,
        supported: int,
        skipped: int,
        peak_in_flight: int,
        complete: bool,
        failure_count: int,
    ) -> None:
        connection = self._require_connection()
        try:
            with connection:
                # One indexed pass avoids one content-group UPSERT per image.
                # Window aggregation preserves a deterministic representative
                # while collapsing duplicates that arrived in different batches.
                connection.execute("DELETE FROM content_groups")
                connection.execute(
                    "INSERT INTO content_groups("
                    "sha256, representative_root_id, "
                    "representative_relative_path, representative_doc_id, "
                    "member_count, fast_unchanged_count"
                    ") SELECT sha256, root_id, relative_path, doc_id, member_count, "
                    "fast_unchanged_count "
                    "FROM ("
                    "SELECT sha256, root_id, relative_path, doc_id, "
                    "COUNT(*) OVER (PARTITION BY sha256) AS member_count, "
                    "SUM(fast_unchanged) OVER (PARTITION BY sha256) "
                    "AS fast_unchanged_count, "
                    "ROW_NUMBER() OVER ("
                    "PARTITION BY sha256 "
                    "ORDER BY root_id, relative_path, doc_id"
                    ") AS group_rank FROM scan_records"
                    ") WHERE group_rank=1"
                )
        except sqlite3.Error as exc:
            raise ScanStagingError(
                f"could not build staged content groups: {exc}"
            ) from exc
        self._set_status(
            "ready",
            scanned=scanned,
            supported=supported,
            skipped=skipped,
            peak_in_flight=peak_in_flight,
            complete=int(complete),
            failure_count=failure_count,
        )

    def mark_consumed(self) -> None:
        self._set_status("consumed")

    def mark_failed(self, *, cancelled: bool = False) -> None:
        self._set_status("cancelled" if cancelled else "failed")

    def record_count(self) -> int:
        return self.metadata().record_count

    def content_group_count(self) -> int:
        row = (
            self._require_connection()
            .execute("SELECT COUNT(*) FROM content_groups")
            .fetchone()
        )
        return int(row[0]) if row is not None else 0

    def iter_records_by_path(
        self,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = after or ScanPathKey("", "", "")
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "SELECT * FROM scan_records WHERE "
                    "(root_id, relative_path, doc_id) > (?, ?, ?) "
                    "ORDER BY root_id, relative_path, doc_id LIMIT ?",
                    (
                        cursor.root_id,
                        cursor.relative_path,
                        cursor.doc_id,
                        normalized_size,
                    ),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [_staged_record_from_row(row) for row in rows]
                yield batch
                cursor = batch[-1].path_key
        finally:
            connection.close()

    def iter_records_by_sha256(
        self,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = after or ScanSha256Key("", "", "", "")
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "SELECT * FROM scan_records WHERE "
                    "(sha256, root_id, relative_path, doc_id) > (?, ?, ?, ?) "
                    "ORDER BY sha256, root_id, relative_path, doc_id LIMIT ?",
                    (
                        cursor.sha256,
                        cursor.root_id,
                        cursor.relative_path,
                        cursor.doc_id,
                        normalized_size,
                    ),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [_staged_record_from_row(row) for row in rows]
                yield batch
                cursor = batch[-1].sha256_key
        finally:
            connection.close()

    def iter_content_groups(
        self,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after_sha256: str = "",
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[ScanContentGroup]]:
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = _validate_optional_sha256(after_sha256)
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "SELECT records.*, groups.member_count, "
                    "groups.fast_unchanged_count "
                    "FROM content_groups AS groups "
                    "JOIN scan_records AS records "
                    "ON records.root_id=groups.representative_root_id "
                    "AND records.relative_path="
                    "groups.representative_relative_path "
                    "AND records.doc_id=groups.representative_doc_id "
                    "WHERE groups.sha256>? "
                    "ORDER BY groups.sha256 LIMIT ?",
                    (cursor, normalized_size),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [_content_group_from_row(row) for row in rows]
                yield batch
                cursor = batch[-1].sha256
        finally:
            connection.close()

    def iter_records_for_sha256(
        self,
        sha256: str,
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanPathKey | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        """Re-read one content group's members in bounded keyset pages.

        This lets the index pipeline enqueue representatives from several
        ``content_groups`` pages concurrently, then apply each returned vector
        to duplicate members without retaining pending member descriptors.
        """

        normalized_sha256 = _validate_sha256(sha256)
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = after or ScanPathKey("", "", "")
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "SELECT * FROM scan_records WHERE sha256=? AND "
                    "(root_id, relative_path, doc_id) > (?, ?, ?) "
                    "ORDER BY root_id, relative_path, doc_id LIMIT ?",
                    (
                        normalized_sha256,
                        cursor.root_id,
                        cursor.relative_path,
                        cursor.doc_id,
                        normalized_size,
                    ),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [_staged_record_from_row(row) for row in rows]
                yield batch
                cursor = batch[-1].path_key
        finally:
            connection.close()

    def iter_records_for_sha256s(
        self,
        sha256_values: Sequence[str],
        *,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[StagedImageRecord]]:
        """Read up to 500 requested content groups with one keyset query/page."""

        normalized_hashes = tuple(
            dict.fromkeys(_validate_sha256(value) for value in sha256_values)
        )
        if not normalized_hashes:
            return
        if len(normalized_hashes) > MAX_SCAN_WRITE_BATCH_SIZE:
            raise ValueError(
                "sha256_values cannot contain more than "
                f"{MAX_SCAN_WRITE_BATCH_SIZE} hashes"
            )
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = after or ScanSha256Key("", "", "", "")
        requested_json = json.dumps(normalized_hashes, separators=(",", ":"))
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "WITH requested(sha256) AS ("
                    "SELECT value FROM json_each(?)"
                    ") SELECT records.* FROM scan_records AS records "
                    "JOIN requested ON requested.sha256=records.sha256 "
                    "WHERE (records.sha256, records.root_id, "
                    "records.relative_path, records.doc_id) > (?, ?, ?, ?) "
                    "ORDER BY records.sha256, records.root_id, "
                    "records.relative_path, records.doc_id LIMIT ?",
                    (
                        requested_json,
                        cursor.sha256,
                        cursor.root_id,
                        cursor.relative_path,
                        cursor.doc_id,
                        normalized_size,
                    ),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [_staged_record_from_row(row) for row in rows]
                yield batch
                cursor = batch[-1].sha256_key
        finally:
            connection.close()

    def iter_content_chunks(
        self,
        *,
        chunk_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after: ScanSha256Key | None = None,
        stats: ScanIterationStats | None = None,
    ) -> Iterator[ScanContentChunk]:
        """Yield SHA groups without materializing a very large duplicate set.

        A group larger than ``chunk_size`` is returned as consecutive chunks.
        Callers embed only the first chunk's representative and may commit all
        later chunks with that same vector until ``is_last`` becomes true.
        """

        normalized_size = _validate_read_batch_size(chunk_size)
        current_sha256 = ""
        current_records: list[StagedImageRecord] = []
        current_is_first = True
        for page in self.iter_records_by_sha256(
            batch_size=normalized_size,
            after=after,
            stats=stats,
        ):
            for record in page:
                if not current_sha256:
                    current_sha256 = record.sha256
                    current_is_first = after is None or record.sha256 != after.sha256
                elif record.sha256 != current_sha256:
                    yield ScanContentChunk(
                        sha256=current_sha256,
                        records=tuple(current_records),
                        is_first=current_is_first,
                        is_last=True,
                    )
                    current_sha256 = record.sha256
                    current_records = []
                    current_is_first = True
                elif len(current_records) >= normalized_size:
                    yield ScanContentChunk(
                        sha256=current_sha256,
                        records=tuple(current_records),
                        is_first=current_is_first,
                        is_last=False,
                    )
                    current_records = []
                    current_is_first = False
                current_records.append(record)
        if current_records:
            yield ScanContentChunk(
                sha256=current_sha256,
                records=tuple(current_records),
                is_first=current_is_first,
                is_last=True,
            )

    def iter_seen_doc_ids(
        self,
        *,
        root_id: str,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after_doc_id: str = "",
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[str]]:
        normalized_root_id = _validate_root_id(root_id)
        normalized_size = _validate_read_batch_size(batch_size)
        cursor = str(after_doc_id)
        connection = self._open_read_connection()
        try:
            while True:
                rows = connection.execute(
                    "SELECT doc_id FROM seen_documents "
                    "WHERE root_id=? AND doc_id>? ORDER BY doc_id LIMIT ?",
                    (normalized_root_id, cursor, normalized_size),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [str(row["doc_id"]) for row in rows]
                yield batch
                cursor = batch[-1]
        finally:
            connection.close()

    def iter_stale_doc_ids(
        self,
        state_database: str | Path,
        *,
        root_id: str,
        batch_size: int = DEFAULT_SCAN_WRITE_BATCH_SIZE,
        after_doc_id: str = "",
        stats: ScanIterationStats | None = None,
    ) -> Iterator[list[str]]:
        """Anti-join persisted entries against the staged seen-id relation."""

        normalized_root_id = _validate_root_id(root_id)
        normalized_size = _validate_read_batch_size(batch_size)
        state_path = Path(state_database).expanduser().resolve()
        if not state_path.is_file():
            raise FileNotFoundError(state_path)
        state_uri = f"{state_path.as_uri()}?mode=ro"
        cursor = str(after_doc_id)
        connection = self._open_read_connection()
        attached = False
        try:
            connection.execute("ATTACH DATABASE ? AS index_state", (state_uri,))
            attached = True
            _validate_attached_entries_table(connection)
            while True:
                rows = connection.execute(
                    "SELECT entries.doc_id FROM index_state.entries AS entries "
                    "LEFT JOIN main.seen_documents AS seen "
                    "ON seen.doc_id=entries.doc_id "
                    "AND seen.root_id=entries.root_id "
                    "WHERE entries.root_id=? AND entries.doc_id>? "
                    "AND seen.doc_id IS NULL "
                    "ORDER BY entries.doc_id LIMIT ?",
                    (normalized_root_id, cursor, normalized_size),
                ).fetchall()
                if stats is not None:
                    stats.record_page(len(rows))
                if not rows:
                    return
                batch = [str(row["doc_id"]) for row in rows]
                yield batch
                cursor = batch[-1]
        except sqlite3.Error as exc:
            raise ScanStagingError(
                f"could not compute stale document ids: {exc}"
            ) from exc
        finally:
            if attached:
                with suppress(sqlite3.Error):
                    connection.execute("DETACH DATABASE index_state")
            connection.close()

    def count_stale_doc_ids(
        self,
        state_database: str | Path,
        *,
        root_id: str,
        stats: ScanIterationStats | None = None,
    ) -> int:
        """Count stale state entries with one anti-join aggregate query."""

        normalized_root_id = _validate_root_id(root_id)
        state_path = Path(state_database).expanduser().resolve()
        if not state_path.is_file():
            raise FileNotFoundError(state_path)
        state_uri = f"{state_path.as_uri()}?mode=ro"
        connection = self._open_read_connection()
        attached = False
        try:
            connection.execute("ATTACH DATABASE ? AS index_state", (state_uri,))
            attached = True
            _validate_attached_entries_table(connection)
            row = connection.execute(
                "SELECT COUNT(*) FROM index_state.entries AS entries "
                "LEFT JOIN main.seen_documents AS seen "
                "ON seen.doc_id=entries.doc_id "
                "AND seen.root_id=entries.root_id "
                "WHERE entries.root_id=? AND seen.doc_id IS NULL",
                (normalized_root_id,),
            ).fetchone()
            if stats is not None:
                stats.record_page(1 if row is not None else 0)
            return int(row[0]) if row is not None else 0
        except sqlite3.Error as exc:
            raise ScanStagingError(
                f"could not count stale document ids: {exc}"
            ) from exc
        finally:
            if attached:
                with suppress(sqlite3.Error):
                    connection.execute("DETACH DATABASE index_state")
            connection.close()

    def _set_status(self, status: ScanStagingStatus, **updates: object) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid scan staging status: {status}")
        connection = self._require_connection()
        metadata = self.metadata()
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(metadata.status, frozenset())
        if status not in allowed:
            raise ScanStagingError(
                f"cannot change scan staging from {metadata.status!r} to {status!r}"
            )
        assignments = ["status=?", "updated_at=?", "owner_pid=?"]
        parameters: list[object] = [status, time.time(), os.getpid()]
        for name, value in updates.items():
            if name not in {
                "scanned",
                "supported",
                "skipped",
                "peak_in_flight",
                "complete",
                "failure_count",
            }:
                raise ValueError(f"unsupported scan metadata field: {name}")
            assignments.append(f"{name}=?")
            parameters.append(value)
        parameters.append(1)
        try:
            with connection:
                connection.execute(
                    f"UPDATE scan_metadata SET {', '.join(assignments)} "
                    "WHERE singleton=?",
                    parameters,
                )
        except sqlite3.Error as exc:
            raise ScanStagingError(
                f"could not update scan staging status: {exc}"
            ) from exc

    def _open_read_connection(self) -> sqlite3.Connection:
        self._require_connection()
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, timeout=30.0, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            _validate_database(connection)
            _validate_filename_identity(connection, self.path)
            return connection
        except (sqlite3.Error, ScanStagingError, ValueError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, ScanStagingError):
                raise
            raise ScanStagingError(f"could not read scan staging: {exc}") from exc

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ScanStagingError("scan staging is closed")
        return self._connection


def discover_scan_staging(
    directory: str | Path,
    *,
    abandoned_after_seconds: float = DEFAULT_ABANDONED_AFTER_SECONDS,
    now: float | None = None,
) -> list[ScanStagingArtifact]:
    parent = Path(directory).expanduser().resolve()
    if not parent.is_dir():
        return []
    if abandoned_after_seconds < 0:
        raise ValueError("abandoned_after_seconds cannot be negative")
    current_time = time.time() if now is None else float(now)
    artifacts: list[ScanStagingArtifact] = []
    for path in sorted(parent.glob(f"{SCAN_STAGING_PREFIX}*{SCAN_STAGING_SUFFIX}")):
        match = _STAGING_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        try:
            safe_path = _validated_local_staging_file(path, parent)
            uri = f"{safe_path.as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, timeout=2.0, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                _validate_database(connection)
                _validate_filename_identity(connection, safe_path)
                row = connection.execute(
                    "SELECT * FROM scan_metadata WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise ScanStagingError("metadata is missing")
                metadata = _metadata_from_row(row)
            abandoned = (
                metadata.status == "scanning"
                and current_time - metadata.updated_at >= abandoned_after_seconds
            )
            artifacts.append(
                ScanStagingArtifact(
                    path=path,
                    run_id=metadata.run_id,
                    root_id=metadata.root_id,
                    status=metadata.status,
                    owner_pid=metadata.owner_pid,
                    created_at=metadata.created_at,
                    updated_at=metadata.updated_at,
                    record_count=metadata.record_count,
                    abandoned=abandoned,
                )
            )
        except (
            OSError,
            LookupError,
            TypeError,
            sqlite3.Error,
            ScanStagingError,
            ValueError,
        ) as exc:
            artifacts.append(
                ScanStagingArtifact(
                    path=path,
                    run_id=match.group(1),
                    root_id="",
                    status="invalid",
                    owner_pid=0,
                    created_at=0.0,
                    updated_at=0.0,
                    record_count=0,
                    abandoned=False,
                    error=str(exc) or exc.__class__.__name__,
                )
            )
    return artifacts


def cleanup_scan_staging(
    directory: str | Path,
    *,
    abandoned_after_seconds: float = DEFAULT_ABANDONED_AFTER_SECONDS,
    include_ready: bool = True,
    include_scanning: bool = True,
    now: float | None = None,
    warning_handler: Callable[[str], None] | None = None,
) -> list[Path]:
    """Remove only validated terminal or abandoned staging artifacts.

    The low-level reader can inspect a ready database, but production indexing
    cannot safely bind an old scan to today's root and parameters. Ready and
    scanning artifacts are therefore retired by default; the flags exist only
    for low-level inspection tools. Invalid, redirected, and unknown files are
    never deleted. Cleanup errors are reported per artifact and do not stop
    later artifacts or the caller's main operation.
    """

    parent = Path(directory).expanduser().resolve()
    removed: list[Path] = []
    for artifact in discover_scan_staging(
        parent,
        abandoned_after_seconds=abandoned_after_seconds,
        now=now,
    ):
        if artifact.status == "invalid":
            _emit_cleanup_warning(
                warning_handler,
                "scan staging was not retired because validation failed: "
                f"{artifact.path.name}",
            )
            continue
        removable = artifact.abandoned or artifact.status in {
            "consumed",
            "failed",
            "cancelled",
        }
        if include_ready and artifact.status == "ready":
            removable = True
        if include_scanning and artifact.status == "scanning":
            removable = True
        if removable:
            try:
                _remove_staging_files(artifact.path, expected_directory=parent)
            except (OSError, ScanStagingError) as exc:
                _emit_cleanup_warning(
                    warning_handler,
                    "scan staging cleanup failed: "
                    f"{artifact.path.name} ({exc.__class__.__name__})",
                )
            else:
                removed.append(artifact.path)
    return removed


def retire_orphaned_scan_staging(
    directory: str | Path,
    *,
    warning_handler: Callable[[str], None] | None = None,
) -> list[Path]:
    """Safely discard all validated artifacts left by an earlier scan.

    Scan staging is transient scratch space, not a recovery journal. Vector
    writes are recovered by the Collection Outbox; an interrupted scan is
    rebuilt against the current root and current options instead.
    """

    return cleanup_scan_staging(
        directory,
        include_ready=True,
        include_scanning=True,
        warning_handler=warning_handler,
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE scan_metadata (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            root_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('scanning', 'ready', 'consumed', 'failed', 'cancelled')
            ),
            owner_pid INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            scanned INTEGER NOT NULL DEFAULT 0 CHECK(scanned >= 0),
            supported INTEGER NOT NULL DEFAULT 0 CHECK(supported >= 0),
            skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped >= 0),
            peak_in_flight INTEGER NOT NULL DEFAULT 0 CHECK(peak_in_flight >= 0),
            complete INTEGER NOT NULL DEFAULT 1 CHECK(complete IN (0, 1)),
            failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
            record_count INTEGER NOT NULL DEFAULT 0 CHECK(record_count >= 0),
            seen_count INTEGER NOT NULL DEFAULT 0 CHECK(seen_count >= 0),
            fast_unchanged_count INTEGER NOT NULL DEFAULT 0
                CHECK(fast_unchanged_count >= 0),
            write_batch_count INTEGER NOT NULL DEFAULT 0
                CHECK(write_batch_count >= 0),
            max_write_batch INTEGER NOT NULL DEFAULT 0 CHECK(max_write_batch >= 0)
        );

        CREATE TABLE seen_documents (
            doc_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            fast_unchanged INTEGER NOT NULL DEFAULT 0
                CHECK(fast_unchanged IN (0, 1))
        ) WITHOUT ROWID;
        CREATE INDEX idx_seen_documents_root_doc
            ON seen_documents(root_id, doc_id);

        CREATE TABLE scan_records (
            root_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            doc_id TEXT NOT NULL UNIQUE,
            sequence INTEGER NOT NULL UNIQUE CHECK(sequence >= 0),
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256)=64),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            mtime_ns INTEGER NOT NULL CHECK(mtime_ns >= 0),
            width INTEGER NOT NULL CHECK(width > 0),
            height INTEGER NOT NULL CHECK(height > 0),
            fast_unchanged INTEGER NOT NULL DEFAULT 0
                CHECK(fast_unchanged IN (0, 1)),
            PRIMARY KEY(root_id, relative_path, doc_id)
        ) WITHOUT ROWID;
        CREATE INDEX idx_scan_records_sha_key
            ON scan_records(sha256, root_id, relative_path, doc_id);

        CREATE TABLE content_groups (
            sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
            representative_root_id TEXT NOT NULL,
            representative_relative_path TEXT NOT NULL,
            representative_doc_id TEXT NOT NULL,
            member_count INTEGER NOT NULL CHECK(member_count > 0),
            fast_unchanged_count INTEGER NOT NULL
                CHECK(fast_unchanged_count >= 0)
        ) WITHOUT ROWID;
        """
    )


def _validate_database(connection: sqlite3.Connection) -> None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != SCAN_STAGING_APPLICATION_ID:
        raise ScanStagingError("file is not a Zvec scan staging database")
    if user_version != SCAN_STAGING_SCHEMA_VERSION:
        raise ScanStagingError(
            f"unsupported scan staging schema version: {user_version}"
        )
    for table_name, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
        actual_columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if not required_columns.issubset(actual_columns):
            raise ScanStagingError(
                f"scan staging schema is missing required table/columns: {table_name}"
            )


def _validate_attached_entries_table(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA index_state.table_info(entries)"
        ).fetchall()
    }
    if not {"doc_id", "root_id"}.issubset(columns):
        raise ScanStagingError("state database entries table is missing doc_id/root_id")


def _validate_filename_identity(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    match = _STAGING_NAME_PATTERN.fullmatch(path.name)
    if match is None:
        raise ScanStagingError("invalid scan staging filename")
    row = connection.execute(
        "SELECT run_id FROM scan_metadata WHERE singleton=1"
    ).fetchone()
    if row is None or str(row[0]) != match.group(1):
        raise ScanStagingError("scan staging filename does not match its run id")


def _metadata_from_row(row: sqlite3.Row) -> ScanStagingMetadata:
    if int(row["schema_version"]) != SCAN_STAGING_SCHEMA_VERSION:
        raise ScanStagingError("scan staging metadata schema version is invalid")
    status = str(row["status"])
    if status not in _VALID_STATUSES:
        raise ScanStagingError(f"invalid scan staging status: {status!r}")
    return ScanStagingMetadata(
        run_id=_validate_run_id(str(row["run_id"])),
        root_id=_validate_root_id(str(row["root_id"])),
        status=status,
        owner_pid=int(row["owner_pid"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        scanned=int(row["scanned"]),
        supported=int(row["supported"]),
        skipped=int(row["skipped"]),
        peak_in_flight=int(row["peak_in_flight"]),
        complete=bool(row["complete"]),
        failure_count=int(row["failure_count"]),
        record_count=int(row["record_count"]),
        seen_count=int(row["seen_count"]),
        fast_unchanged_count=int(row["fast_unchanged_count"]),
        write_batch_count=int(row["write_batch_count"]),
        max_write_batch=int(row["max_write_batch"]),
    )


def _staged_record_from_row(row: sqlite3.Row) -> StagedImageRecord:
    return StagedImageRecord(
        sequence=int(row["sequence"]),
        doc_id=str(row["doc_id"]),
        root_id=str(row["root_id"]),
        relative_path=str(row["relative_path"]),
        file_name=str(row["file_name"]),
        extension=str(row["extension"]),
        mime_type=str(row["mime_type"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"]),
        width=int(row["width"]),
        height=int(row["height"]),
        fast_unchanged=bool(row["fast_unchanged"]),
    )


def _content_group_from_row(row: sqlite3.Row) -> ScanContentGroup:
    return ScanContentGroup(
        sha256=str(row["sha256"]),
        representative=_staged_record_from_row(row),
        member_count=int(row["member_count"]),
        fast_unchanged_count=int(row["fast_unchanged_count"]),
    )


def _record_values(record: StagedImageRecord) -> tuple[object, ...]:
    return (
        record.root_id,
        _portable_relative_path(record.relative_path),
        record.doc_id,
        record.sequence,
        record.file_name,
        record.extension,
        record.mime_type,
        _validate_sha256(record.sha256),
        record.size_bytes,
        record.mtime_ns,
        record.width,
        record.height,
        int(record.fast_unchanged),
    )


def _validate_batch(
    root_id: str,
    seen_documents: Sequence[SeenScanDocument],
    records: Sequence[StagedImageRecord],
) -> None:
    seen_ids: set[str] = set()
    for seen_document in seen_documents:
        if seen_document.root_id != root_id:
            raise ValueError("seen document root_id does not match scan staging")
        _portable_relative_path(seen_document.relative_path)
        if not seen_document.doc_id or seen_document.doc_id in seen_ids:
            raise ValueError("seen document batch contains an empty/duplicate doc_id")
        seen_ids.add(seen_document.doc_id)
    record_ids: set[str] = set()
    sequences: set[int] = set()
    for record in records:
        if record.root_id != root_id:
            raise ValueError("staged record root_id does not match scan staging")
        _portable_relative_path(record.relative_path)
        _validate_sha256(record.sha256)
        if not record.doc_id or record.doc_id in record_ids:
            raise ValueError("record batch contains an empty/duplicate doc_id")
        if record.sequence < 0 or record.sequence in sequences:
            raise ValueError("record batch contains an invalid/duplicate sequence")
        if record.size_bytes < 0 or record.mtime_ns < 0:
            raise ValueError("record file metadata cannot be negative")
        if record.width <= 0 or record.height <= 0:
            raise ValueError("record image dimensions must be positive")
        record_ids.add(record.doc_id)
        sequences.add(record.sequence)


def _portable_relative_path(value: str | Path) -> str:
    raw_value = str(value).replace("\\", "/")
    if raw_value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw_value):
        raise ConfigurationError("scan relative path must remain below its root")
    normalized = raw_value.strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigurationError("scan relative path must remain below its root")
    return path.as_posix()


def _validate_root_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 512 or "\x00" in normalized:
        raise ValueError("root_id must be a non-empty value up to 512 characters")
    return normalized


def _validate_run_id(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _RUN_ID_PATTERN.fullmatch(normalized):
        raise ValueError("scan staging run_id must be 32 lowercase hex characters")
    return normalized


def _validate_sha256(value: str) -> str:
    normalized = str(value).strip().lower()
    if len(normalized) != 64:
        raise ValueError("sha256 must be a 64-character hex digest")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise ValueError("sha256 must be a 64-character hex digest") from exc
    return normalized


def _validate_optional_sha256(value: str) -> str:
    return "" if not value else _validate_sha256(value)


def _validate_read_batch_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("scan read batch_size must be an integer")
    if not 1 <= value <= MAX_SCAN_WRITE_BATCH_SIZE:
        raise ValueError(
            f"scan read batch_size must be between 1 and {MAX_SCAN_WRITE_BATCH_SIZE}"
        )
    return value


def _staging_filename(run_id: str) -> str:
    return f"{SCAN_STAGING_PREFIX}{_validate_run_id(run_id)}{SCAN_STAGING_SUFFIX}"


def _validated_staging_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    parent = candidate.parent.resolve()
    return _validated_local_staging_file(parent / candidate.name, parent)


def _remove_staging_files(
    path: Path,
    *,
    expected_directory: Path | None = None,
) -> None:
    if _STAGING_NAME_PATTERN.fullmatch(path.name) is None:
        raise ScanStagingError("refusing to delete an unrecognized staging path")
    parent = (
        path.parent.resolve()
        if expected_directory is None
        else Path(expected_directory).expanduser().resolve()
    )
    if path.parent.resolve() != parent:
        raise ScanStagingError("refusing to delete scan staging outside its directory")
    candidates = (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )
    # Preflight every known SQLite artifact before deleting any of them. This
    # prevents a junction/symlink or a directory with a crafted suffix from
    # turning cleanup into an operation outside the private staging folder.
    for candidate in candidates:
        _validate_removal_candidate(candidate, parent)
    for candidate in candidates:
        try:
            file_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse_or_symlink(file_stat) or not stat.S_ISREG(file_stat.st_mode):
            raise ScanStagingError("refusing to delete redirected scan staging")
        candidate.unlink()


def _validated_local_staging_file(path: Path, parent: Path) -> Path:
    if _STAGING_NAME_PATTERN.fullmatch(path.name) is None:
        raise ScanStagingError("invalid scan staging filename")
    if path.parent.resolve() != parent.resolve():
        raise ScanStagingError("scan staging escaped its configured directory")
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        raise
    if _is_reparse_or_symlink(file_stat):
        raise ScanStagingError("redirected scan staging is not trusted")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ScanStagingError("scan staging is not a regular file")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent.resolve():
        raise ScanStagingError("scan staging escaped its configured directory")
    return resolved


def _validate_removal_candidate(path: Path, parent: Path) -> None:
    if path.parent.resolve() != parent:
        raise ScanStagingError("refusing to delete scan staging outside its directory")
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if _is_reparse_or_symlink(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ScanStagingError("refusing to delete redirected scan staging")


def _is_reparse_or_symlink(file_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(file_stat, "st_file_attributes", 0) & reparse_flag)


def _emit_cleanup_warning(
    warning_handler: Callable[[str], None] | None,
    message: str,
) -> None:
    if warning_handler is None:
        return
    with suppress(Exception):
        warning_handler(message)
