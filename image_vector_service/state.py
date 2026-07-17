from __future__ import annotations

import json
import os
import sqlite3
import uuid
from array import array
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import ConfigurationError
from .logical_paths import logical_document_id, normalize_path, resolve_under_root
from .tags import normalize_tags

ENTRY_COLUMNS = (
    "doc_id",
    "root_id",
    "relative_path",
    "file_name",
    "extension",
    "mime_type",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "width",
    "height",
    "tags_json",
    "folder_tags_json",
    "accepted_auto_tags_json",
)

_SQLITE_PARAMETER_CHUNK = 900


class IndexState:
    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(path, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            version = self._existing_schema_version()
            if version == "1":
                raise ConfigurationError(
                    "Path schema V1 requires migration. Run "
                    "'image_service.py migrate-schema --dry-run' first."
                )
            self._create_schema()
            self._validate_schema_version()
            if legacy_path is not None:
                self._migrate_legacy_json(legacy_path)
        except ConfigurationError:
            if hasattr(self, "connection"):
                self.connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise ConfigurationError(f"Invalid SQLite state database: {path}") from exc

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                doc_id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                folder_tags_json TEXT NOT NULL DEFAULT '[]',
                accepted_auto_tags_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_entries_sha256 ON entries(sha256);
            CREATE INDEX IF NOT EXISTS idx_entries_root_id ON entries(root_id);
            CREATE TABLE IF NOT EXISTS roots (
                root_id TEXT PRIMARY KEY,
                current_path TEXT NOT NULL UNIQUE,
                recursive INTEGER NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS embedding_cache (
                cache_key TEXT PRIMARY KEY,
                modality TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dimension INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_embedding_cache_modality
                ON embedding_cache(modality);
            CREATE TABLE IF NOT EXISTS entry_tag_index (
                doc_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY(doc_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_entry_tag_index_tag
                ON entry_tag_index(tag);
            CREATE TABLE IF NOT EXISTS index_runs (
                run_id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL,
                root_path TEXT NOT NULL,
                status TEXT NOT NULL,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                deferred_count INTEGER NOT NULL DEFAULT 0,
                needs_attention INTEGER NOT NULL DEFAULT 0,
                failure_manifest TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_index_runs_root_started
                ON index_runs(root_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS index_run_entries (
                run_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                change_type TEXT NOT NULL,
                PRIMARY KEY(run_id, doc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_index_run_entries_doc
                ON index_run_entries(doc_id);
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_tag_cache_identity
                ON auto_tag_cache(sha256, model, prompt_version, schema_version);
            CREATE TABLE IF NOT EXISTS document_annotations (
                doc_id TEXT PRIMARY KEY,
                source_sha256 TEXT NOT NULL,
                cache_key TEXT,
                status TEXT NOT NULL,
                proposed_tags_json TEXT NOT NULL DEFAULT '[]',
                accepted_tags_json TEXT NOT NULL DEFAULT '[]',
                rejected_tags_json TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '{}',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                structured_json TEXT NOT NULL DEFAULT '{}',
                policy_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_document_annotations_status
                ON document_annotations(status);
            CREATE TABLE IF NOT EXISTS auto_tag_review_batches (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                snapshots_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                undone_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_auto_tag_review_batches_latest
                ON auto_tag_review_batches(sequence DESC);
            """
        )
        root_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(roots)")
        }
        entry_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(entries)")
        }
        annotation_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(document_annotations)"
            )
        }
        index_run_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(index_runs)")
        }
        if "tags_json" not in entry_columns:
            self.connection.execute(
                "ALTER TABLE entries ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "folder_tags_json" not in entry_columns:
            self.connection.execute(
                "ALTER TABLE entries ADD COLUMN folder_tags_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "accepted_auto_tags_json" not in entry_columns:
            self.connection.execute(
                "ALTER TABLE entries ADD COLUMN accepted_auto_tags_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "tags_json" not in root_columns:
            self.connection.execute(
                "ALTER TABLE roots ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "structured_json" not in annotation_columns:
            self.connection.execute(
                "ALTER TABLE document_annotations ADD COLUMN structured_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "policy_json" not in annotation_columns:
            self.connection.execute(
                "ALTER TABLE document_annotations ADD COLUMN policy_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "deferred_count" not in index_run_columns:
            self.connection.execute(
                "ALTER TABLE index_runs ADD COLUMN deferred_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "needs_attention" not in index_run_columns:
            self.connection.execute(
                "ALTER TABLE index_runs ADD COLUMN needs_attention "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "failure_manifest" not in index_run_columns:
            self.connection.execute(
                "ALTER TABLE index_runs ADD COLUMN failure_manifest "
                "TEXT NOT NULL DEFAULT ''"
            )
        tag_index_count = int(
            self.connection.execute("SELECT COUNT(*) FROM entry_tag_index").fetchone()[
                0
            ]
        )
        if tag_index_count == 0:
            self._rebuild_tag_index()
        self.connection.commit()

    def _existing_schema_version(self) -> str | None:
        table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not table:
            return None
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='state_schema_version'"
        ).fetchone()
        return str(row[0]) if row else None

    def _validate_schema_version(self) -> None:
        version = self.get_metadata("state_schema_version")
        if version is None:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) "
                    "VALUES('state_schema_version', '2')"
                )
        elif version != "2":
            raise ConfigurationError(f"Unsupported state schema version: {version}")

    def _migrate_legacy_json(self, legacy_path: Path) -> None:
        if self.count() or not legacy_path.is_file():
            return
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        if data.get("entries"):
            raise ConfigurationError(
                "Legacy JSON state must be migrated before using schema V2."
            )

    def ensure_collection_uuid(
        self, collection_uuid: str, reset_if_unbound: bool = False
    ) -> bool:
        current = self.get_metadata("collection_uuid")
        if current == collection_uuid:
            return False
        had_entries = self.count() > 0
        if current is None and not reset_if_unbound:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('collection_uuid', ?)",
                    (collection_uuid,),
                )
            return False
        with self.connection:
            self.connection.execute("DELETE FROM entries")
            self.connection.execute("DELETE FROM roots")
            self.connection.execute("DELETE FROM embedding_cache")
            self.connection.execute("DELETE FROM entry_tag_index")
            self.connection.execute("DELETE FROM index_runs")
            self.connection.execute("DELETE FROM index_run_entries")
            self.connection.execute("DELETE FROM document_annotations")
            self.connection.execute("DELETE FROM auto_tag_review_batches")
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES('collection_uuid', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (collection_uuid,),
            )
        return had_entries

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def get(self, doc_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM entries WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return self._entry_from_row(row) if row else None

    def set_many(self, entries: Iterable[dict[str, Any]]) -> None:
        items = list(entries)
        values = [self._entry_values(entry) for entry in items]
        if not values:
            return
        placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in ENTRY_COLUMNS
            if column != "doc_id"
        )
        with self.connection:
            columns = ", ".join(ENTRY_COLUMNS)
            self.connection.executemany(
                f"INSERT INTO entries({columns}) VALUES({placeholders}) "
                f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
                values,
            )
            for entry in items:
                self._replace_indexed_tags(entry)

    def remove_many(self, doc_ids: Iterable[str]) -> None:
        values = [(doc_id,) for doc_id in doc_ids]
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "DELETE FROM entry_tag_index WHERE doc_id = ?", values
            )
            self.connection.executemany(
                "DELETE FROM document_annotations WHERE doc_id = ?", values
            )
            self.connection.executemany(
                "DELETE FROM index_run_entries WHERE doc_id = ?", values
            )
            self.connection.executemany("DELETE FROM entries WHERE doc_id = ?", values)

    def list_effective_tags(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT tag FROM entry_tag_index ORDER BY tag COLLATE NOCASE"
        )
        return [str(row["tag"]) for row in rows]

    def list_entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM entries ORDER BY root_id, relative_path"
        )
        return [self._entry_from_row(row) for row in rows]

    def entries_for_any_effective_tags(
        self, tags: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Return entries containing any exact effective tag.

        Fuzzy matching and alias expansion happen before this method is called.
        Queries are chunked below SQLite's parameter limit so a broad, but
        valid, tag expansion cannot fail partway through a large search.
        """

        normalized = normalize_tags(tags)
        if not normalized:
            return []

        entries: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(normalized), _SQLITE_PARAMETER_CHUNK):
            chunk = normalized[offset : offset + _SQLITE_PARAMETER_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT DISTINCT entries.* FROM entries "
                "JOIN entry_tag_index "
                "ON entry_tag_index.doc_id = entries.doc_id "
                f"WHERE entry_tag_index.tag IN ({placeholders})",
                chunk,
            )
            for row in rows:
                entry = self._entry_from_row(row)
                entries[str(entry["doc_id"])] = entry
        return sorted(
            entries.values(),
            key=lambda entry: (
                str(entry["root_id"]),
                str(entry["relative_path"]),
                str(entry["doc_id"]),
            ),
        )

    def begin_index_run(self, root_id: str, root_path: str) -> str:
        run_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO index_runs(run_id, root_id, root_path, status) "
                "VALUES(?, ?, ?, 'running')",
                (run_id, root_id, root_path),
            )
        return run_id

    def record_index_run_entries(
        self, run_id: str, doc_ids: Iterable[str], change_type: str
    ) -> None:
        if change_type not in {"inserted", "updated"}:
            raise ValueError("change_type must be 'inserted' or 'updated'.")
        values = [(run_id, doc_id, change_type) for doc_id in doc_ids]
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT INTO index_run_entries(run_id, doc_id, change_type) "
                "VALUES(?, ?, ?) ON CONFLICT(run_id, doc_id) DO UPDATE SET "
                "change_type=excluded.change_type",
                values,
            )

    def finish_index_run(
        self,
        run_id: str,
        *,
        status: str,
        inserted: int,
        updated: int,
        failed: int,
        deferred: int = 0,
        needs_attention: bool = False,
        failure_manifest: str = "",
    ) -> None:
        if status not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ValueError("Invalid index run status.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE index_runs SET status = ?, inserted_count = ?, "
                "updated_count = ?, failed_count = ?, deferred_count = ?, "
                "needs_attention = ?, failure_manifest = ?, "
                "finished_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (
                    status,
                    inserted,
                    updated,
                    failed,
                    deferred,
                    int(needs_attention),
                    failure_manifest,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ConfigurationError(f"Unknown index run: {run_id}")

    def latest_index_run_id(self, root_id: str | None = None) -> str | None:
        if root_id is None:
            row = self.connection.execute(
                "SELECT run_id FROM index_runs "
                "WHERE status IN ('succeeded', 'partial') "
                # CURRENT_TIMESTAMP has one-second precision. rowid preserves the
                # real insertion order when fast runs share a timestamp.
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT run_id FROM index_runs WHERE root_id = ? "
                "AND status IN ('succeeded', 'partial') "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (root_id,),
            ).fetchone()
        return str(row["run_id"]) if row else None

    def entries_for_index_run(
        self, run_id: str, *, change_type: str = "inserted"
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT entries.* FROM index_run_entries "
            "JOIN entries ON entries.doc_id = index_run_entries.doc_id "
            "WHERE index_run_entries.run_id = ? "
            "AND index_run_entries.change_type = ? "
            "ORDER BY entries.relative_path",
            (run_id, change_type),
        )
        return [self._entry_from_row(row) for row in rows]

    def get_auto_tag_cache(self, cache_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_tag_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["annotation"] = self._decode_json_object(
            item.pop("annotation_json"), "annotation cache"
        )
        item["usage"] = self._decode_json_object(
            item.pop("usage_json"), "annotation usage"
        )
        return item

    def set_auto_tag_cache(
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
                "status=excluded.status, annotation_json=excluded.annotation_json, "
                "request_id=excluded.request_id, usage_json=excluded.usage_json, "
                "cost_yuan=excluded.cost_yuan, attempts=auto_tag_cache.attempts + 1, "
                "error=excluded.error, updated_at=CURRENT_TIMESTAMP",
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

    def set_document_annotation(
        self,
        *,
        doc_id: str,
        source_sha256: str,
        cache_key: str | None,
        status: str,
        proposed_tags: Iterable[str] = (),
        accepted_tags: Iterable[str] = (),
        rejected_tags: Iterable[str] = (),
        description: str = "",
        entities: dict[str, Any] | None = None,
        warnings: Iterable[str] = (),
        structured: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if status not in {"pending_review", "accepted", "rejected", "failed"}:
            raise ValueError("Invalid document annotation status.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO document_annotations("
                "doc_id, source_sha256, cache_key, status, proposed_tags_json, "
                "accepted_tags_json, rejected_tags_json, description, entities_json, "
                "warnings_json, structured_json, policy_json, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "source_sha256=excluded.source_sha256, cache_key=excluded.cache_key, "
                "status=excluded.status, "
                "proposed_tags_json=excluded.proposed_tags_json, "
                "accepted_tags_json=excluded.accepted_tags_json, "
                "rejected_tags_json=excluded.rejected_tags_json, "
                "description=excluded.description, "
                "entities_json=excluded.entities_json, "
                "warnings_json=excluded.warnings_json, "
                "structured_json=excluded.structured_json, "
                "policy_json=excluded.policy_json, error=excluded.error, "
                "updated_at=CURRENT_TIMESTAMP",
                (
                    doc_id,
                    source_sha256,
                    cache_key,
                    status,
                    json.dumps(list(normalize_tags(proposed_tags)), ensure_ascii=False),
                    json.dumps(list(normalize_tags(accepted_tags)), ensure_ascii=False),
                    json.dumps(list(normalize_tags(rejected_tags)), ensure_ascii=False),
                    description,
                    json.dumps(entities or {}, ensure_ascii=False),
                    json.dumps(list(warnings), ensure_ascii=False),
                    json.dumps(structured or {}, ensure_ascii=False),
                    json.dumps(policy or {}, ensure_ascii=False),
                    error,
                ),
            )

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM document_annotations WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return self._document_annotation_from_row(row) if row else None

    def list_document_annotations(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM document_annotations ORDER BY updated_at DESC, doc_id"
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM document_annotations WHERE status = ? "
                "ORDER BY updated_at DESC, doc_id",
                (status,),
            )
        return [self._document_annotation_from_row(row) for row in rows]

    def count_document_annotations(self, status: str | None = None) -> int:
        if status is None:
            row = self.connection.execute(
                "SELECT COUNT(*) AS annotation_count FROM document_annotations"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS annotation_count FROM document_annotations "
                "WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row["annotation_count"]) if row is not None else 0

    def page_document_annotations(
        self,
        status: str | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[int, list[dict[str, Any]]]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Annotation page offset must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Annotation page limit must be a positive integer.")

        total = self.count_document_annotations(status)
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM document_annotations "
                "ORDER BY updated_at DESC, doc_id LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM document_annotations WHERE status = ? "
                "ORDER BY updated_at DESC, doc_id LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        return total, [self._document_annotation_from_row(row) for row in rows]

    def create_auto_tag_review_batch(
        self,
        *,
        batch_id: str,
        snapshots: Iterable[dict[str, Any]],
    ) -> None:
        """Persist the complete pre-operation state before touching the repository."""

        normalized_id = str(batch_id).strip()
        if not normalized_id:
            raise ValueError("Auto-tag review batch_id is required.")
        values = list(snapshots)
        if not values or not all(isinstance(item, dict) for item in values):
            raise ValueError(
                "Auto-tag review batch snapshots must be non-empty objects."
            )
        with self.connection:
            self.connection.execute(
                "INSERT INTO auto_tag_review_batches("
                "batch_id, status, snapshots_json) VALUES(?, 'applying', ?)",
                (normalized_id, json.dumps(values, ensure_ascii=False)),
            )

    def update_auto_tag_review_batch(
        self,
        batch_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if status not in {
            "applying",
            "applied",
            "rolling_back",
            "rolled_back",
            "undoing",
            "undone",
            "rollback_failed",
            "undo_failed",
        }:
            raise ValueError("Invalid auto-tag review batch status.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE auto_tag_review_batches SET status = ?, result_json = ?, "
                "error = ?, updated_at = CURRENT_TIMESTAMP, "
                "undone_at = CASE WHEN ? = 'undone' THEN CURRENT_TIMESTAMP "
                "ELSE undone_at END WHERE batch_id = ?",
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    str(error or ""),
                    status,
                    str(batch_id),
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown auto-tag review batch: {batch_id}")

    def latest_auto_tag_review_batch(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_tag_review_batches ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return self._auto_tag_review_batch_from_row(row) if row else None

    def get_auto_tag_review_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM auto_tag_review_batches WHERE batch_id = ?",
            (str(batch_id),),
        ).fetchone()
        return self._auto_tag_review_batch_from_row(row) if row else None

    def ids_for_root(self, root_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT doc_id FROM entries WHERE root_id = ?", (root_id,)
        )
        return {str(row["doc_id"]) for row in rows}

    def entries_for_root(self, root_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM entries WHERE root_id = ? ORDER BY relative_path",
            (root_id,),
        )
        return [self._entry_from_row(row) for row in rows]

    def find_doc_id_by_sha(self, sha256: str) -> str | None:
        row = self.connection.execute(
            "SELECT doc_id FROM entries WHERE sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        return str(row["doc_id"]) if row else None

    def find_entry_for_path(self, path: Path) -> dict[str, Any] | None:
        resolved = path.expanduser().resolve()
        for root in self.list_roots():
            root_path = Path(str(root["current_path"]))
            try:
                relative = resolved.relative_to(root_path)
            except ValueError:
                continue
            doc_id = logical_document_id(str(root["root_id"]), relative)
            return self.get(doc_id)
        return None

    def get_cached_vector(self, cache_key: str, dimension: int) -> list[float] | None:
        row = self.connection.execute(
            "SELECT embedding, dimension FROM embedding_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row or int(row["dimension"]) != dimension:
            return None
        values = array("f")
        values.frombytes(bytes(row["embedding"]))
        if len(values) != dimension:
            self.connection.execute(
                "DELETE FROM embedding_cache WHERE cache_key = ?", (cache_key,)
            )
            self.connection.commit()
            return None
        with self.connection:
            self.connection.execute(
                "UPDATE embedding_cache SET hit_count = hit_count + 1, "
                "last_used_at = CURRENT_TIMESTAMP WHERE cache_key = ?",
                (cache_key,),
            )
        return list(values)

    def set_cached_vector(
        self,
        cache_key: str,
        modality: str,
        vector: list[float],
    ) -> None:
        payload = array("f", vector).tobytes()
        with self.connection:
            self.connection.execute(
                "INSERT INTO embedding_cache"
                "(cache_key, modality, embedding, dimension) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "modality=excluded.modality, embedding=excluded.embedding, "
                "dimension=excluded.dimension, last_used_at=CURRENT_TIMESTAMP",
                (cache_key, modality, payload, len(vector)),
            )

    def cache_stats(self) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(LENGTH(embedding)), 0) AS bytes, "
            "COALESCE(SUM(hit_count), 0) AS hits FROM embedding_cache"
        ).fetchone()
        return {
            "entries": int(row["entries"]),
            "bytes": int(row["bytes"]),
            "hits": int(row["hits"]),
        }

    def clear_cache(self) -> int:
        count = self.cache_stats()["entries"]
        with self.connection:
            self.connection.execute("DELETE FROM embedding_cache")
        return count

    def import_cache_rows(self, rows: Iterable[tuple]) -> None:
        values = list(rows)
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT INTO embedding_cache(cache_key, modality, embedding, "
                "dimension, created_at, last_used_at, hit_count) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def checkpoint(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM entries"
        ).fetchone()
        return int(row["count"])

    def ensure_root(self, current_path: str, recursive: bool) -> str:
        existing = self.find_root_id(current_path)
        if existing is not None:
            return existing
        normalized = normalize_path(Path(current_path))
        root_id = str(uuid.uuid4())
        self.record_root(root_id, normalized, recursive)
        return root_id

    def find_root_id(self, current_path: str) -> str | None:
        normalized = normalize_path(Path(current_path))
        row = self.connection.execute(
            "SELECT root_id FROM roots WHERE current_path = ?",
            (normalized,),
        ).fetchone()
        return str(row["root_id"]) if row else None

    def record_root(self, root_id: str, current_path: str, recursive: bool) -> None:
        normalized = normalize_path(Path(current_path))
        with self.connection:
            self.connection.execute(
                "INSERT INTO roots(root_id, current_path, recursive) VALUES(?, ?, ?) "
                "ON CONFLICT(root_id) DO UPDATE SET "
                "current_path=excluded.current_path, recursive=excluded.recursive",
                (root_id, normalized, int(recursive)),
            )

    def root_for_path(self, current_path: str) -> dict[str, Any] | None:
        normalized = normalize_path(Path(current_path))
        row = self.connection.execute(
            "SELECT * FROM roots WHERE current_path = ?", (normalized,)
        ).fetchone()
        return dict(row) if row else None

    def root_path(self, root_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT current_path FROM roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        return str(row["current_path"]) if row else None

    def list_roots(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT roots.root_id, roots.current_path, roots.recursive, "
            "roots.tags_json, "
            "COUNT(entries.doc_id) AS document_count FROM roots "
            "LEFT JOIN entries ON entries.root_id = roots.root_id "
            "GROUP BY roots.root_id ORDER BY roots.current_path"
        )
        return [
            {
                "root_id": str(row["root_id"]),
                "current_path": str(row["current_path"]),
                "recursive": bool(row["recursive"]),
                "tags": self._decode_tags(str(row["tags_json"])),
                "document_count": int(row["document_count"]),
            }
            for row in rows
        ]

    def tags_for_root(self, root_id: str) -> list[str]:
        row = self.connection.execute(
            "SELECT tags_json FROM roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if not row:
            raise ConfigurationError(f"Unknown root_id: {root_id}")
        return self._decode_tags(str(row["tags_json"]))

    def set_root_tags(self, root_id: str, tags: Iterable[str]) -> None:
        values = list(normalize_tags(tags))
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE roots SET tags_json = ? WHERE root_id = ?",
                (json.dumps(values, ensure_ascii=False), root_id),
            )
        if cursor.rowcount != 1:
            raise ConfigurationError(f"Unknown root_id: {root_id}")

    @staticmethod
    def _decode_tags(value: str) -> list[str]:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("Invalid root tags in state database.") from exc
        if not isinstance(decoded, list) or not all(
            isinstance(tag, str) for tag in decoded
        ):
            raise ConfigurationError("Invalid root tags in state database.")
        return list(normalize_tags(decoded))

    @staticmethod
    def _decode_json_object(value: Any, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        try:
            decoded = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid {label} JSON.") from exc
        if not isinstance(decoded, dict):
            raise ConfigurationError(f"Invalid {label} JSON object.")
        return decoded

    @classmethod
    def _document_annotation_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["proposed_tags"] = cls._decode_tags(str(item.pop("proposed_tags_json")))
        item["accepted_tags"] = cls._decode_tags(str(item.pop("accepted_tags_json")))
        item["rejected_tags"] = cls._decode_tags(str(item.pop("rejected_tags_json")))
        item["entities"] = cls._decode_json_object(
            item.pop("entities_json"), "annotation entities"
        )
        item["structured"] = cls._decode_json_object(
            item.pop("structured_json", "{}"), "annotation structured fields"
        )
        item["policy"] = cls._decode_json_object(
            item.pop("policy_json", "{}"), "annotation policy"
        )
        try:
            warnings = json.loads(str(item.pop("warnings_json")))
        except json.JSONDecodeError as exc:
            raise ConfigurationError("Invalid annotation warnings JSON.") from exc
        if not isinstance(warnings, list) or not all(
            isinstance(value, str) for value in warnings
        ):
            raise ConfigurationError("Invalid annotation warnings JSON.")
        item["warnings"] = warnings
        return item

    @classmethod
    def _auto_tag_review_batch_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            snapshots = json.loads(str(item.pop("snapshots_json")))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "Invalid auto-tag review batch snapshots JSON."
            ) from exc
        if not isinstance(snapshots, list) or not all(
            isinstance(value, dict) for value in snapshots
        ):
            raise ConfigurationError("Invalid auto-tag review batch snapshots JSON.")
        item["snapshots"] = snapshots
        item["result"] = cls._decode_json_object(
            item.pop("result_json"), "auto-tag review batch result"
        )
        return item

    @classmethod
    def _entry_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        entry = dict(row)
        entry["tags"] = cls._decode_tags(str(entry.pop("tags_json")))
        entry["folder_tags"] = cls._decode_tags(
            str(entry.pop("folder_tags_json", "[]"))
        )
        entry["accepted_auto_tags"] = cls._decode_tags(
            str(entry.pop("accepted_auto_tags_json", "[]"))
        )
        entry["effective_tags"] = list(cls._effective_tags(entry))
        return entry

    @staticmethod
    def _entry_values(entry: dict[str, Any]) -> tuple[Any, ...]:
        encoded_tags = {
            "tags_json": json.dumps(
                list(normalize_tags(entry.get("tags", ()))), ensure_ascii=False
            ),
            "folder_tags_json": json.dumps(
                list(normalize_tags(entry.get("folder_tags", ()))),
                ensure_ascii=False,
            ),
            "accepted_auto_tags_json": json.dumps(
                list(normalize_tags(entry.get("accepted_auto_tags", ()))),
                ensure_ascii=False,
            ),
        }
        return tuple(
            encoded_tags[column] if column in encoded_tags else entry[column]
            for column in ENTRY_COLUMNS
        )

    @staticmethod
    def _effective_tags(entry: dict[str, Any]) -> tuple[str, ...]:
        return normalize_tags(
            [
                *entry.get("tags", ()),
                *entry.get("folder_tags", ()),
                *entry.get("accepted_auto_tags", ()),
            ]
        )

    def _replace_indexed_tags(self, entry: dict[str, Any]) -> None:
        doc_id = str(entry["doc_id"])
        self.connection.execute(
            "DELETE FROM entry_tag_index WHERE doc_id = ?", (doc_id,)
        )
        tags = self._effective_tags(entry)
        if tags:
            self.connection.executemany(
                "INSERT INTO entry_tag_index(doc_id, tag) VALUES(?, ?)",
                [(doc_id, tag) for tag in tags],
            )

    def _rebuild_tag_index(self) -> None:
        self.connection.execute("DELETE FROM entry_tag_index")
        rows = self.connection.execute("SELECT * FROM entries")
        for row in rows:
            self._replace_indexed_tags(self._entry_from_row(row))

    def rebind_root(self, root_id: str, new_path: str) -> dict[str, Any]:
        path = Path(new_path).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(path)
        row = self.connection.execute(
            "SELECT * FROM roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if not row:
            raise ConfigurationError(f"Unknown root_id: {root_id}")
        overlap = self.find_overlapping_root(str(path), exclude_root_id=root_id)
        if overlap:
            raise ConfigurationError(
                f"The new path overlaps registered root {overlap['root_id']}: "
                f"{overlap['current_path']}"
            )
        entries = self.connection.execute(
            "SELECT relative_path FROM entries WHERE root_id = ?", (root_id,)
        )
        for entry in entries:
            resolve_under_root(path, str(entry["relative_path"]))
        normalized = normalize_path(path)
        with self.connection:
            self.connection.execute(
                "UPDATE roots SET current_path = ? WHERE root_id = ?",
                (normalized, root_id),
            )
        return {
            "root_id": root_id,
            "previous_path": str(row["current_path"]),
            "current_path": normalized,
        }

    def find_overlapping_root(
        self, root_path: str, exclude_root_id: str | None = None
    ) -> dict[str, str] | None:
        candidate = os.path.normcase(os.path.abspath(root_path))
        rows = self.connection.execute("SELECT root_id, current_path FROM roots")
        for row in rows:
            if exclude_root_id and str(row["root_id"]) == exclude_root_id:
                continue
            existing = os.path.normcase(os.path.abspath(str(row["current_path"])))
            if existing == candidate:
                continue
            try:
                common = os.path.commonpath([existing, candidate])
            except ValueError:
                continue
            if common in {existing, candidate}:
                return {
                    "root_id": str(row["root_id"]),
                    "current_path": str(row["current_path"]),
                }
        return None

    def close(self) -> None:
        self.connection.close()
