from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


class AutoTagCacheMigrationError(RuntimeError):
    """Raised when a legacy cache cannot be migrated without risking data loss."""


class AutoTagCacheFlightError(RuntimeError):
    """Raised when a shared-cache single-flight handle is used incorrectly."""


@dataclass
class _AutoTagCacheFlightState:
    leader_token: object
    completed: threading.Event


_SINGLE_FLIGHT_LOCK = threading.Lock()
_SINGLE_FLIGHTS: dict[tuple[int, str, str], _AutoTagCacheFlightState] = {}


class AutoTagCacheFlight:
    """One process-local claim for a shared cache key.

    The first claimant is the leader and owns the model request. Later claimants
    wait on the same completion event, then re-read SQLite through their own
    ``SharedAutoTagCache`` connection. Claims are deliberately process-local:
    SQLite remains the durable cross-Collection result store, while this small
    registry prevents duplicate HTTP calls between concurrent jobs in one app.
    """

    def __init__(
        self,
        *,
        identity: tuple[int, str, str],
        state: _AutoTagCacheFlightState,
        leader_token: object | None,
    ) -> None:
        self._identity = identity
        self._state = state
        self._leader_token = leader_token
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def cache_key(self) -> str:
        return self._identity[2]

    @property
    def is_leader(self) -> bool:
        return self._leader_token is not None

    def wait(
        self,
        *,
        cancel_check: Callable[[], None] | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        """Wait for the leader while giving callers regular cancellation points."""

        if self.is_leader:
            raise AutoTagCacheFlightError(
                "A single-flight leader cannot wait on itself."
            )
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive.")
        while not self._state.completed.is_set():
            if cancel_check is not None:
                cancel_check()
            self._state.completed.wait(timeout=poll_interval)
        if cancel_check is not None:
            cancel_check()

    def release(self) -> None:
        """Release a leader claim exactly once; follower calls are harmless."""

        if not self.is_leader:
            return
        with self._release_lock:
            if self._released:
                return
            self._released = True
        with _SINGLE_FLIGHT_LOCK:
            current = _SINGLE_FLIGHTS.get(self._identity)
            if current is self._state and current.leader_token is self._leader_token:
                del _SINGLE_FLIGHTS[self._identity]
                current.completed.set()

    def __enter__(self) -> AutoTagCacheFlight:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


@dataclass(frozen=True)
class AutoTagCacheMigrationReport:
    source_path: str
    scanned: int = 0
    imported: int = 0
    upgraded: int = 0
    rekeyed: int = 0
    unchanged: int = 0
    skipped: int = 0
    already_current: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        return self.imported + self.upgraded + self.rekeyed


@dataclass(frozen=True)
class _CacheRecord:
    cache_key: str
    sha256: str
    model: str
    prompt_version: str
    schema_version: int
    status: str
    annotation_json: str | None
    request_id: str
    usage_json: str
    cost_yuan: float | None
    attempts: int
    error: str


class SharedAutoTagCache:
    """Cross-Collection annotation cache keyed by image content and versions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._single_flight_database = _normalized_path(path.expanduser().resolve())
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS auto_tag_cache (
                cache_key TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                annotation_json TEXT,
                request_id TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}',
                cost_yuan REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_auto_tag_identity
                ON auto_tag_cache(sha256, model, prompt_version, schema_version);
            CREATE TABLE IF NOT EXISTS legacy_auto_tag_cache_migrations (
                source_path TEXT PRIMARY KEY,
                source_revision TEXT NOT NULL,
                scanned INTEGER NOT NULL,
                imported INTEGER NOT NULL,
                upgraded INTEGER NOT NULL,
                rekeyed INTEGER NOT NULL,
                skipped INTEGER NOT NULL,
                migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.connection.commit()

    def claim(self, cache_key: str) -> AutoTagCacheFlight:
        """Claim the right to populate one missing cache entry in this process."""

        normalized_key = str(cache_key).strip()
        if not normalized_key:
            raise ValueError("cache_key must not be empty.")
        # Include the PID so a forked child never inherits an unfinishable parent
        # flight from the copied process memory.
        identity = (os.getpid(), self._single_flight_database, normalized_key)
        with _SINGLE_FLIGHT_LOCK:
            state = _SINGLE_FLIGHTS.get(identity)
            if state is None:
                token = object()
                state = _AutoTagCacheFlightState(
                    leader_token=token,
                    completed=threading.Event(),
                )
                _SINGLE_FLIGHTS[identity] = state
                return AutoTagCacheFlight(
                    identity=identity,
                    state=state,
                    leader_token=token,
                )
            return AutoTagCacheFlight(
                identity=identity,
                state=state,
                leader_token=None,
            )

    def wait_for_result(
        self,
        flight: AutoTagCacheFlight,
        *,
        cancel_check: Callable[[], None] | None = None,
        poll_interval: float = 0.1,
    ) -> dict[str, Any] | None:
        """Wait for a leader and re-read its committed result from SQLite."""

        expected_identity = (
            os.getpid(),
            self._single_flight_database,
            flight.cache_key,
        )
        if flight._identity != expected_identity:
            raise AutoTagCacheFlightError(
                "The single-flight handle belongs to a different cache database."
            )
        flight.wait(cancel_check=cancel_check, poll_interval=poll_interval)
        return self.get(flight.cache_key)

    def get(self, cache_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_tag_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["annotation"] = _decode_object(item.pop("annotation_json"))
        item["usage"] = _decode_object(item.pop("usage_json"))
        return item

    def set(
        self,
        *,
        cache_key: str,
        sha256: str,
        model: str,
        prompt_version: str,
        schema_version: int,
        status: str,
        annotation: dict[str, Any] | None = None,
        request_id: str = "",
        usage: dict[str, Any] | None = None,
        cost_yuan: float | None = None,
        error: str = "",
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("Auto-tag cache status must be succeeded or failed.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO auto_tag_cache("
                "cache_key, sha256, model, prompt_version, schema_version, status, "
                "annotation_json, request_id, usage_json, cost_yuan, attempts, error"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "status=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.status "
                "ELSE excluded.status END, "
                "annotation_json=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.annotation_json "
                "ELSE excluded.annotation_json END, "
                "request_id=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.request_id "
                "ELSE excluded.request_id END, "
                "usage_json=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.usage_json "
                "ELSE excluded.usage_json END, "
                "cost_yuan=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.cost_yuan "
                "ELSE excluded.cost_yuan END, "
                "attempts=auto_tag_cache.attempts + 1, "
                "error=CASE WHEN auto_tag_cache.status = 'succeeded' "
                "AND excluded.status = 'failed' THEN auto_tag_cache.error "
                "ELSE excluded.error END, updated_at=CURRENT_TIMESTAMP",
                (
                    cache_key,
                    sha256,
                    model,
                    prompt_version,
                    schema_version,
                    status,
                    json.dumps(annotation, ensure_ascii=False)
                    if annotation is not None
                    else None,
                    request_id,
                    json.dumps(usage or {}, ensure_ascii=False),
                    cost_yuan,
                    error,
                ),
            )

    def migrate_legacy_database(self, legacy_path: Path) -> AutoTagCacheMigrationReport:
        """Copy a per-Collection cache into this shared cache exactly once per revision.

        The source is opened read-only and is never deleted or modified. Valid rows are
        applied in the same destination transaction as the revision marker, so a failed
        migration can be retried without leaving a partially acknowledged import.
        """

        source = legacy_path.expanduser().resolve()
        source_name = _normalized_path(source)
        if source_name == _normalized_path(self.path.expanduser().resolve()):
            raise AutoTagCacheMigrationError(
                "Legacy and shared auto-tag caches must use different databases."
            )
        if not source.is_file():
            return AutoTagCacheMigrationReport(source_path=str(source))

        source_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(
                f"{source.as_uri()}?mode=ro", timeout=10, uri=True
            )
            source_connection.row_factory = sqlite3.Row
            if not _table_exists(source_connection, "auto_tag_cache"):
                return AutoTagCacheMigrationReport(source_path=str(source))
            _validate_legacy_schema(source_connection)
            scanned, revision = _legacy_revision(source_connection)
            if scanned == 0:
                return AutoTagCacheMigrationReport(source_path=str(source))

            marker = self.connection.execute(
                "SELECT source_revision FROM legacy_auto_tag_cache_migrations "
                "WHERE source_path = ?",
                (source_name,),
            ).fetchone()
            if marker is not None and str(marker["source_revision"]) == revision:
                return AutoTagCacheMigrationReport(
                    source_path=str(source),
                    scanned=scanned,
                    unchanged=scanned,
                    already_current=True,
                )

            rows = source_connection.execute(
                "SELECT cache_key, sha256, model, prompt_version, schema_version, "
                "status, annotation_json, request_id, usage_json, cost_yuan, "
                "attempts, error FROM auto_tag_cache ORDER BY rowid"
            ).fetchall()
            records, warnings = _validated_legacy_records(rows)
            return self._apply_legacy_records(
                source_path=str(source),
                source_name=source_name,
                source_revision=revision,
                scanned=scanned,
                records=records,
                warnings=warnings,
            )
        except AutoTagCacheMigrationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise AutoTagCacheMigrationError(
                f"Could not migrate legacy auto-tag cache: {source}"
            ) from exc
        finally:
            if source_connection is not None:
                source_connection.close()

    def _apply_legacy_records(
        self,
        *,
        source_path: str,
        source_name: str,
        source_revision: str,
        scanned: int,
        records: list[_CacheRecord],
        warnings: list[str],
    ) -> AutoTagCacheMigrationReport:
        imported = 0
        upgraded = 0
        rekeyed = 0
        unchanged = 0

        try:
            # Acquire the destination write lock before rechecking the marker. This
            # serializes migrations from different Collection processes sharing it.
            self.connection.execute("BEGIN IMMEDIATE")
            marker = self.connection.execute(
                "SELECT source_revision FROM legacy_auto_tag_cache_migrations "
                "WHERE source_path = ?",
                (source_name,),
            ).fetchone()
            if marker is not None and str(marker["source_revision"]) == source_revision:
                self.connection.rollback()
                return AutoTagCacheMigrationReport(
                    source_path=source_path,
                    scanned=scanned,
                    unchanged=scanned,
                    already_current=True,
                )

            for record in records:
                outcome = self._merge_legacy_record(record, warnings)
                if outcome == "imported":
                    imported += 1
                elif outcome == "upgraded":
                    upgraded += 1
                elif outcome == "rekeyed":
                    rekeyed += 1
                else:
                    unchanged += 1

            skipped = scanned - len(records)
            self.connection.execute(
                "INSERT INTO legacy_auto_tag_cache_migrations("
                "source_path, source_revision, scanned, imported, upgraded, "
                "rekeyed, skipped) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_path) DO UPDATE SET "
                "source_revision=excluded.source_revision, "
                "scanned=excluded.scanned, imported=excluded.imported, "
                "upgraded=excluded.upgraded, rekeyed=excluded.rekeyed, "
                "skipped=excluded.skipped, migrated_at=CURRENT_TIMESTAMP",
                (
                    source_name,
                    source_revision,
                    scanned,
                    imported,
                    upgraded,
                    rekeyed,
                    skipped,
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise AutoTagCacheMigrationError(
                "Shared auto-tag cache migration was rolled back."
            ) from exc
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

        return AutoTagCacheMigrationReport(
            source_path=source_path,
            scanned=scanned,
            imported=imported,
            upgraded=upgraded,
            rekeyed=rekeyed,
            unchanged=unchanged,
            skipped=scanned - len(records),
            warnings=tuple(warnings),
        )

    def _merge_legacy_record(self, record: _CacheRecord, warnings: list[str]) -> str:
        existing = self.connection.execute(
            "SELECT * FROM auto_tag_cache WHERE cache_key = ? OR "
            "(sha256 = ? AND model = ? AND prompt_version = ? "
            "AND schema_version = ?) ORDER BY (cache_key = ?) DESC LIMIT 1",
            (
                record.cache_key,
                record.sha256,
                record.model,
                record.prompt_version,
                record.schema_version,
                record.cache_key,
            ),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                "INSERT INTO auto_tag_cache("
                "cache_key, sha256, model, prompt_version, schema_version, status, "
                "annotation_json, request_id, usage_json, cost_yuan, attempts, error"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _record_values(record),
            )
            return "imported"

        if not _same_identity(existing, record):
            warnings.append(
                "Skipped a legacy cache row because its deterministic key is already "
                "used by another identity."
            )
            return "unchanged"

        existing_key = str(existing["cache_key"])
        should_upgrade = record.status == "succeeded" and not _usable_success(existing)
        if should_upgrade:
            self.connection.execute(
                "UPDATE auto_tag_cache SET cache_key = ?, sha256 = ?, model = ?, "
                "prompt_version = ?, schema_version = ?, status = ?, "
                "annotation_json = ?, request_id = ?, usage_json = ?, "
                "cost_yuan = ?, attempts = MAX(attempts, ?), error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE cache_key = ?",
                (*_record_values(record), existing_key),
            )
            return "upgraded"

        if existing_key != record.cache_key:
            self.connection.execute(
                "UPDATE auto_tag_cache SET cache_key = ? WHERE cache_key = ?",
                (record.cache_key, existing_key),
            )
            return "rekeyed"
        return "unchanged"

    def close(self) -> None:
        self.connection.close()


def make_auto_tag_cache_key(
    sha256_hex: str, model: str, prompt_version: str, schema_version: int
) -> str:
    """Return the key format used by ``annotation_service._cache_key``."""

    payload = f"{sha256_hex}\0{model}\0{prompt_version}\0{schema_version}"
    return sha256(payload.encode("utf-8")).hexdigest()


def _validated_legacy_records(
    rows: list[sqlite3.Row],
) -> tuple[list[_CacheRecord], list[str]]:
    records: list[_CacheRecord] = []
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        try:
            records.append(_validated_legacy_record(row))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"Skipped invalid legacy cache row {index}: {exc}")
    return records, warnings


def _validated_legacy_record(row: sqlite3.Row) -> _CacheRecord:
    digest = str(row["sha256"] or "").strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("sha256 must contain 64 hexadecimal characters")
    model = str(row["model"] or "").strip()
    prompt_version = str(row["prompt_version"] or "").strip()
    if not model or not prompt_version:
        raise ValueError("model and prompt_version must not be empty")
    schema_version = row["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ValueError("schema_version must be a positive integer")
    status = str(row["status"] or "")
    if status not in {"succeeded", "failed"}:
        raise ValueError("status must be succeeded or failed")

    annotation = _decode_optional_object(row["annotation_json"], "annotation_json")
    if status == "succeeded" and annotation is None:
        raise ValueError("a succeeded row must contain annotation_json")
    usage = _decode_optional_object(row["usage_json"], "usage_json") or {}
    cost_yuan = row["cost_yuan"]
    if cost_yuan is not None:
        if isinstance(cost_yuan, bool) or not isinstance(cost_yuan, (int, float)):
            raise ValueError("cost_yuan must be numeric or null")
        cost_yuan = float(cost_yuan)
        if not math.isfinite(cost_yuan) or cost_yuan < 0:
            raise ValueError("cost_yuan must be finite and non-negative")
    attempts = row["attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")

    return _CacheRecord(
        cache_key=make_auto_tag_cache_key(
            digest, model, prompt_version, schema_version
        ),
        sha256=digest,
        model=model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        status=status,
        annotation_json=(
            json.dumps(annotation, ensure_ascii=False, separators=(",", ":"))
            if annotation is not None
            else None
        ),
        request_id=str(row["request_id"] or ""),
        usage_json=json.dumps(usage, ensure_ascii=False, separators=(",", ":")),
        cost_yuan=cost_yuan,
        attempts=attempts,
        error=str(row["error"] or ""),
    )


def _legacy_revision(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute(
        "SELECT COUNT(*) AS row_count, COALESCE(SUM(attempts), 0) AS attempts, "
        "COALESCE(SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0) "
        "AS succeeded, COALESCE(MAX(updated_at), '') AS newest, "
        "COALESCE(SUM(LENGTH(cache_key) + LENGTH(sha256) + LENGTH(model) + "
        "LENGTH(prompt_version) + LENGTH(status) + "
        "LENGTH(COALESCE(annotation_json, '')) + LENGTH(request_id) + "
        "LENGTH(usage_json) + LENGTH(error)), 0) AS payload_size "
        "FROM auto_tag_cache"
    ).fetchone()
    count = int(row["row_count"])
    revision_payload = json.dumps(
        [
            count,
            int(row["attempts"]),
            int(row["succeeded"]),
            str(row["newest"]),
            int(row["payload_size"]),
        ],
        separators=(",", ":"),
    )
    revision = sha256(revision_payload.encode("utf-8")).hexdigest()
    return count, revision


def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
    required = {
        "cache_key",
        "sha256",
        "model",
        "prompt_version",
        "schema_version",
        "status",
        "annotation_json",
        "request_id",
        "usage_json",
        "cost_yuan",
        "attempts",
        "error",
        "updated_at",
    }
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(auto_tag_cache)")
    }
    missing = sorted(required - columns)
    if missing:
        raise AutoTagCacheMigrationError(
            "Legacy auto-tag cache is missing required columns: " + ", ".join(missing)
        )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _record_values(record: _CacheRecord) -> tuple[Any, ...]:
    return (
        record.cache_key,
        record.sha256,
        record.model,
        record.prompt_version,
        record.schema_version,
        record.status,
        record.annotation_json,
        record.request_id,
        record.usage_json,
        record.cost_yuan,
        record.attempts,
        record.error,
    )


def _same_identity(row: sqlite3.Row, record: _CacheRecord) -> bool:
    return (
        str(row["sha256"]).lower() == record.sha256
        and str(row["model"]) == record.model
        and str(row["prompt_version"]) == record.prompt_version
        and int(row["schema_version"]) == record.schema_version
    )


def _usable_success(row: sqlite3.Row) -> bool:
    if str(row["status"]) != "succeeded":
        return False
    try:
        annotation = _decode_optional_object(row["annotation_json"], "annotation_json")
        return annotation is not None
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _decode_optional_object(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = value if isinstance(value, dict) else json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return decoded


def _decode_object(value: Any) -> dict[str, Any]:
    return _decode_optional_object(value, "Auto-tag cache JSON") or {}


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path))
