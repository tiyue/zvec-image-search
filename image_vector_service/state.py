from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from array import array
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .collection_write_outbox import CollectionWriteOutbox, CollectionWriteOutboxError
from .config import ConfigurationError
from .logical_paths import logical_document_id, normalize_path, resolve_under_root
from .tags import normalize_tags

ENTRY_COLUMNS = (
    "doc_id",
    "root_id",
    "relative_path",
    "parent_directory",
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
    "inherited_tags_json",
)

DOCUMENT_ANNOTATION_COLUMNS = (
    "doc_id",
    "source_sha256",
    "cache_key",
    "status",
    "retry_eligible",
    "proposed_tags_json",
    "accepted_tags_json",
    "rejected_tags_json",
    "description",
    "entities_json",
    "warnings_json",
    "structured_json",
    "policy_json",
    "error",
    "updated_at",
)

_SQLITE_PARAMETER_CHUNK = 900
DEFAULT_STATE_READ_PAGE_SIZE = 256
MAX_STATE_READ_PAGE_SIZE = 1_000
_FS_CHANGE_PENDING = 0
_FS_CHANGE_PROCESSED = 1
_FS_CHANGE_CLAIMED = 2


class IndexState:
    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.create_function(
                "zvec_annotation_retryable",
                2,
                _annotation_retry_eligible,
                deterministic=True,
            )
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self._read_local = threading.local()
            version = self._existing_schema_version()
            if version == "1":
                raise ConfigurationError(
                    "Path schema V1 requires migration. Run "
                    "'image_service.py migrate-schema --dry-run' first."
                )
            self._create_schema()
            self._validate_schema_version()
            # Persist exact, already-generated vector writes before applying
            # them to Zvec. Interrupted items are made replayable here without
            # scanning entries or calling an embedding model again.
            self.write_outbox = CollectionWriteOutbox(self.connection)
            self._cache_hit_counter = 0
            self._roots_light_cache: list[tuple[str, str]] | None = None
            if legacy_path is not None:
                self._migrate_legacy_json(legacy_path)
        except ConfigurationError:
            self._close_all_connections()
            raise
        except CollectionWriteOutboxError as exc:
            self._close_all_connections()
            raise ConfigurationError(
                f"Invalid collection write recovery state: {path}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            self._close_all_connections()
            raise ConfigurationError(f"Invalid SQLite state database: {path}") from exc

    def _read_connection(self) -> sqlite3.Connection:
        """Return a thread-local read-only connection for concurrent reads.

        WAL mode allows multiple connections to read the same database file
        simultaneously.  Each thread gets its own Connection object so the
        per-connection C-level mutex does not serialize reads across threads.
        """

        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            self._read_local.conn = conn
        return conn

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
                parent_directory TEXT NOT NULL DEFAULT '',
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
                accepted_auto_tags_json TEXT NOT NULL DEFAULT '[]',
                inherited_tags_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_entries_sha256
                ON entries(sha256, doc_id);
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
                ON entry_tag_index(tag, doc_id);
            CREATE TABLE IF NOT EXISTS entry_folder_index (
                doc_id TEXT NOT NULL,
                root_id TEXT NOT NULL,
                relative_folder TEXT NOT NULL,
                is_direct INTEGER NOT NULL,
                PRIMARY KEY(doc_id, relative_folder)
            );
            CREATE INDEX IF NOT EXISTS idx_entry_folder_index_page
                ON entry_folder_index(root_id, relative_folder, doc_id);
            CREATE TABLE IF NOT EXISTS folder_catalog (
                root_id TEXT NOT NULL,
                relative_folder TEXT NOT NULL,
                first_indexed_at TEXT NOT NULL,
                last_indexed_at TEXT NOT NULL,
                timestamp_source TEXT NOT NULL,
                PRIMARY KEY(root_id, relative_folder)
            );
            CREATE INDEX IF NOT EXISTS idx_folder_catalog_first_indexed
                ON folder_catalog(first_indexed_at DESC, root_id, relative_folder);
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
                retry_eligible INTEGER NOT NULL DEFAULT 1 CHECK (
                    retry_eligible IN (0, 1)
                ),
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
            CREATE TABLE IF NOT EXISTS manual_tag_batches (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                operation TEXT NOT NULL,
                selection_json TEXT NOT NULL DEFAULT '{}',
                tags_json TEXT NOT NULL DEFAULT '[]',
                total_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                undone_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_manual_tag_batches_latest
                ON manual_tag_batches(sequence DESC);
            CREATE TABLE IF NOT EXISTS manual_tag_batch_entries (
                batch_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                before_tags_json TEXT NOT NULL,
                after_tags_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(batch_id, doc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_manual_tag_batch_entries_status
                ON manual_tag_batch_entries(batch_id, status, doc_id);
            CREATE TABLE IF NOT EXISTS fs_change_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                event_type TEXT NOT NULL,
                queued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                processed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(root_id, relative_path)
            );
            CREATE INDEX IF NOT EXISTS idx_fcq_pending
                ON fs_change_queue(processed, queued_at);
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
        folder_catalog_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(folder_catalog)")
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
        if "inherited_tags_json" not in entry_columns:
            self.connection.execute(
                "ALTER TABLE entries ADD COLUMN inherited_tags_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "parent_directory" not in entry_columns:
            self.connection.execute(
                "ALTER TABLE entries ADD COLUMN parent_directory "
                "TEXT NOT NULL DEFAULT ''"
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
        if "retry_eligible" not in annotation_columns:
            self.connection.execute(
                "ALTER TABLE document_annotations ADD COLUMN retry_eligible "
                "INTEGER NOT NULL DEFAULT 1 CHECK (retry_eligible IN (0, 1))"
            )
            self.connection.execute(
                "UPDATE document_annotations SET retry_eligible = "
                "zvec_annotation_retryable(policy_json, error)"
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
        if "last_indexed_at" not in folder_catalog_columns:
            self.connection.execute(
                "ALTER TABLE folder_catalog ADD COLUMN last_indexed_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        self._backfill_parent_directories()
        self._backfill_folder_index()
        self._backfill_folder_catalog()
        self._backfill_folder_catalog_last_indexed()
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_folder_page "
            "ON entries(root_id, parent_directory, relative_path, doc_id)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_auto_tag_page "
            "ON entries(root_id, relative_path, doc_id)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_annotations_status_page "
            "ON document_annotations("
            "status, updated_at DESC, doc_id, retry_eligible)"
        )
        sha_index_columns = tuple(
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA index_info('idx_entries_sha256')"
            )
        )
        if sha_index_columns != ("sha256", "doc_id"):
            # Older state databases indexed only the hash.  Including doc_id
            # makes bulk content-dedup lookups covering and gives MIN(doc_id)
            # deterministic results without visiting every matching row.
            self.connection.execute("DROP INDEX IF EXISTS idx_entries_sha256")
            self.connection.execute(
                "CREATE INDEX idx_entries_sha256 ON entries(sha256, doc_id)"
            )
        tag_index_columns = tuple(
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA index_info('idx_entry_tag_index_tag')"
            )
        )
        if tag_index_columns != ("tag", "doc_id"):
            # The original tag-only index forced a table lookup for every
            # matching document id. Rebuild it once as a covering index so a
            # broad fuzzy tag search can aggregate directly from the B-tree.
            self.connection.execute("DROP INDEX IF EXISTS idx_entry_tag_index_tag")
            self.connection.execute(
                "CREATE INDEX idx_entry_tag_index_tag ON entry_tag_index(tag, doc_id)"
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

    def _backfill_parent_directories(self) -> None:
        """Populate the additive folder index once for pre-feature databases."""

        marker = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'parent_directory_backfill_v1'"
        ).fetchone()
        if marker is not None and str(marker[0]) == "1":
            return
        cursor = self.connection.execute(
            "SELECT doc_id, relative_path FROM entries ORDER BY doc_id"
        )
        with self.connection:
            while True:
                rows = cursor.fetchmany(1_000)
                if not rows:
                    break
                self.connection.executemany(
                    "UPDATE entries SET parent_directory = ? WHERE doc_id = ?",
                    [
                        (
                            _relative_parent_directory(str(row["relative_path"])),
                            str(row["doc_id"]),
                        )
                        for row in rows
                    ],
                )
            self.connection.execute(
                "INSERT INTO metadata(key, value) "
                "VALUES('parent_directory_backfill_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def _backfill_folder_index(self) -> None:
        marker = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'entry_folder_index_v1'"
        ).fetchone()
        if marker is not None and str(marker[0]) == "1":
            return
        cursor = self.connection.execute(
            "SELECT doc_id, root_id, relative_path FROM entries ORDER BY doc_id"
        )
        with self.connection:
            self.connection.execute("DELETE FROM entry_folder_index")
            while True:
                rows = cursor.fetchmany(1_000)
                if not rows:
                    break
                for row in rows:
                    self._replace_folder_index(dict(row))
            self.connection.execute(
                "INSERT INTO metadata(key, value) "
                "VALUES('entry_folder_index_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def _backfill_folder_catalog(self) -> None:
        """Recover stable folder timestamps without consulting image files.

        Existing schema-v2 databases did not store first-indexed timestamps on
        entries or folders.  Successful ``inserted`` index-run membership is
        the only trustworthy historical source.  Folders without complete run
        coverage receive one deterministic migration timestamp and an explicit
        source marker instead of pretending that file mtimes are import times.
        """

        marker = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'folder_catalog_v1'"
        ).fetchone()
        if marker is not None and str(marker[0]) == "1":
            return
        migration_row = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()
        migration_time = str(migration_row[0])
        rows = self.connection.execute(
            "WITH first_insert AS ("
            "SELECT index_run_entries.doc_id, MIN(index_runs.started_at) "
            "AS started_at FROM index_run_entries JOIN index_runs "
            "ON index_runs.run_id = index_run_entries.run_id "
            "WHERE index_run_entries.change_type = 'inserted' "
            "AND index_runs.status IN ('succeeded', 'partial') "
            "GROUP BY index_run_entries.doc_id) "
            "SELECT entries.root_id, entries.parent_directory, "
            "COUNT(*) AS image_count, COUNT(first_insert.started_at) "
            "AS timestamped_count, "
            "MIN(strftime('%Y-%m-%dT%H:%M:%fZ', "
            "first_insert.started_at)) AS first_indexed_at "
            "FROM entries LEFT JOIN first_insert "
            "ON first_insert.doc_id = entries.doc_id "
            "GROUP BY entries.root_id, entries.parent_directory "
            "ORDER BY entries.root_id, entries.parent_directory"
        ).fetchall()
        values: list[tuple[str, str, str, str, str]] = []
        for row in rows:
            image_count = int(row["image_count"])
            timestamped_count = int(row["timestamped_count"])
            if timestamped_count == image_count:
                source = "index_run"
            elif timestamped_count:
                source = "partial_index_run"
            else:
                source = "migration"
            values.append(
                (
                    str(row["root_id"]),
                    str(row["parent_directory"]),
                    str(row["first_indexed_at"] or migration_time),
                    str(row["first_indexed_at"] or migration_time),
                    source,
                )
            )
        with self.connection:
            self.connection.execute("DELETE FROM folder_catalog")
            if values:
                self.connection.executemany(
                    "INSERT INTO folder_catalog("
                    "root_id, relative_folder, first_indexed_at, "
                    "last_indexed_at, timestamp_source) VALUES(?, ?, ?, ?, ?)",
                    values,
                )
            self.connection.execute(
                "INSERT INTO metadata(key, value) "
                "VALUES('folder_catalog_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def _backfill_folder_catalog_last_indexed(self) -> None:
        """Add stable last-indexed timestamps to existing folder catalogs."""

        marker = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'folder_catalog_v2'"
        ).fetchone()
        if marker is not None and str(marker[0]) == "1":
            return
        rows = self.connection.execute(
            "SELECT folder_catalog.root_id, folder_catalog.relative_folder, "
            "folder_catalog.first_indexed_at, "
            "MAX(strftime('%Y-%m-%dT%H:%M:%fZ', index_runs.started_at)) "
            "AS last_indexed_at FROM folder_catalog "
            "LEFT JOIN entries ON entries.root_id = folder_catalog.root_id "
            "AND entries.parent_directory = folder_catalog.relative_folder "
            "LEFT JOIN index_run_entries ON "
            "index_run_entries.doc_id = entries.doc_id "
            "AND index_run_entries.change_type IN ('inserted', 'updated') "
            "LEFT JOIN index_runs ON index_runs.run_id = index_run_entries.run_id "
            "AND index_runs.status IN ('succeeded', 'partial') "
            "GROUP BY folder_catalog.root_id, folder_catalog.relative_folder"
        ).fetchall()
        with self.connection:
            self.connection.executemany(
                "UPDATE folder_catalog SET last_indexed_at = ? "
                "WHERE root_id = ? AND relative_folder = ?",
                [
                    (
                        str(row["last_indexed_at"] or row["first_indexed_at"]),
                        str(row["root_id"]),
                        str(row["relative_folder"]),
                    )
                    for row in rows
                ],
            )
            self.connection.execute(
                "INSERT INTO metadata(key, value) "
                "VALUES('folder_catalog_v2', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
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
            self.write_outbox.discard_all_for_collection_rebind()
            self.connection.execute("DELETE FROM entries")
            self.connection.execute("DELETE FROM roots")
            self.connection.execute("DELETE FROM embedding_cache")
            self.connection.execute("DELETE FROM entry_tag_index")
            self.connection.execute("DELETE FROM entry_folder_index")
            self.connection.execute("DELETE FROM folder_catalog")
            self.connection.execute("DELETE FROM index_runs")
            self.connection.execute("DELETE FROM index_run_entries")
            self.connection.execute("DELETE FROM document_annotations")
            self.connection.execute("DELETE FROM auto_tag_review_batches")
            self.connection.execute("DELETE FROM manual_tag_batch_entries")
            self.connection.execute("DELETE FROM manual_tag_batches")
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES('collection_uuid', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (collection_uuid,),
            )
        return had_entries

    def get_metadata(self, key: str) -> str | None:
        row = (
            self._read_connection()
            .execute("SELECT value FROM metadata WHERE key = ?", (key,))
            .fetchone()
        )
        return str(row["value"]) if row else None

    def get(self, doc_id: str) -> dict[str, Any] | None:
        row = (
            self._read_connection()
            .execute("SELECT * FROM entries WHERE doc_id = ?", (doc_id,))
            .fetchone()
        )
        return self._entry_from_row(row) if row else None

    def set_many(self, entries: Iterable[dict[str, Any]]) -> None:
        items = [dict(entry) for entry in entries]
        missing_inherited = [
            str(entry["doc_id"]) for entry in items if "inherited_tags" not in entry
        ]
        inherited_by_id: dict[str, list[str]] = {}
        for offset in range(0, len(missing_inherited), _SQLITE_PARAMETER_CHUNK):
            chunk = missing_inherited[offset : offset + _SQLITE_PARAMETER_CHUNK]
            if not chunk:
                continue
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT doc_id, inherited_tags_json FROM entries "
                f"WHERE doc_id IN ({placeholders})",
                chunk,
            )
            inherited_by_id.update(
                {
                    str(row["doc_id"]): self._decode_tags(
                        str(row["inherited_tags_json"])
                    )
                    for row in rows
                }
            )
        for entry in items:
            if "inherited_tags" not in entry:
                entry["inherited_tags"] = inherited_by_id.get(str(entry["doc_id"]), [])
        values = [self._entry_values(entry) for entry in items]
        if not values:
            return
        placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in ENTRY_COLUMNS
            if column != "doc_id"
        )
        observed_row = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()
        observed_at = str(observed_row[0])
        with self.connection:
            columns = ", ".join(ENTRY_COLUMNS)
            self.connection.executemany(
                f"INSERT INTO entries({columns}) VALUES({placeholders}) "
                f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
                values,
            )
            doc_ids = [str(entry["doc_id"]) for entry in items]
            for offset in range(0, len(doc_ids), _SQLITE_PARAMETER_CHUNK):
                chunk = doc_ids[offset : offset + _SQLITE_PARAMETER_CHUNK]
                ph = ", ".join("?" for _ in chunk)
                self.connection.execute(
                    f"DELETE FROM entry_tag_index WHERE doc_id IN ({ph})",
                    chunk,
                )
                self.connection.execute(
                    f"DELETE FROM entry_folder_index WHERE doc_id IN ({ph})",
                    chunk,
                )
            tag_rows: list[tuple[str, str]] = []
            folder_rows: list[tuple[str, str, str, int]] = []
            catalog_rows: list[tuple[str, str, str, str]] = []
            for entry in items:
                doc_id = str(entry["doc_id"])
                root_id = str(entry["root_id"])
                for tag in self._effective_tags(entry):
                    tag_rows.append((doc_id, tag))
                direct = _relative_parent_directory(str(entry["relative_path"]))
                for folder in _folder_ancestors(direct):
                    folder_rows.append((doc_id, root_id, folder, int(folder == direct)))
                catalog_rows.append((root_id, direct, observed_at, observed_at))
            if tag_rows:
                self.connection.executemany(
                    "INSERT INTO entry_tag_index(doc_id, tag) VALUES(?, ?)",
                    tag_rows,
                )
            if folder_rows:
                self.connection.executemany(
                    "INSERT INTO entry_folder_index("
                    "doc_id, root_id, relative_folder, is_direct) "
                    "VALUES(?, ?, ?, ?)",
                    folder_rows,
                )
            if catalog_rows:
                self.connection.executemany(
                    "INSERT INTO folder_catalog("
                    "root_id, relative_folder, first_indexed_at, "
                    "last_indexed_at, timestamp_source) "
                    "VALUES(?, ?, ?, ?, 'observed') "
                    "ON CONFLICT(root_id, relative_folder) DO NOTHING",
                    catalog_rows,
                )

    def remove_many(self, doc_ids: Iterable[str]) -> None:
        self._remove_many(doc_ids, mark_source_deleted=False)

    def remove_many_for_folder_delete(self, doc_ids: Iterable[str]) -> int:
        """Delete folder-owned state and retire undo records atomically.

        A folder deletion removes the source documents themselves, so an old
        manual-tag undo must not attempt to write those documents back later.
        The history row remains available for audit, but leaves the retryable
        status set used by ``manual_tag_undo_available``.
        """

        return self._remove_many(doc_ids, mark_source_deleted=True)

    def _remove_many(self, doc_ids: Iterable[str], *, mark_source_deleted: bool) -> int:
        normalized_ids = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not normalized_ids:
            return 0
        with self.connection:
            existing_ids: list[str] = []
            for offset in range(0, len(normalized_ids), _SQLITE_PARAMETER_CHUNK):
                chunk = normalized_ids[offset : offset + _SQLITE_PARAMETER_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                rows = self.connection.execute(
                    f"SELECT doc_id FROM entries WHERE doc_id IN ({placeholders})",
                    chunk,
                )
                existing_ids.extend(str(row["doc_id"]) for row in rows)
            if not existing_ids:
                return 0
            values = [(doc_id,) for doc_id in existing_ids]
            if mark_source_deleted:
                self.connection.executemany(
                    "UPDATE manual_tag_batch_entries SET "
                    "status = 'source_deleted', "
                    "error = CASE WHEN error = '' THEN "
                    "'Source document deleted with its folder.' ELSE error END, "
                    "updated_at = CURRENT_TIMESTAMP WHERE doc_id = ? AND "
                    "status IN ('planned', 'applied', 'undo_failed', 'conflict')",
                    values,
                )
                self._invalidate_latest_auto_tag_review_batch(set(existing_ids))
            self.connection.executemany(
                "DELETE FROM entry_tag_index WHERE doc_id = ?", values
            )
            self.connection.executemany(
                "DELETE FROM entry_folder_index WHERE doc_id = ?", values
            )
            self.connection.executemany(
                "DELETE FROM document_annotations WHERE doc_id = ?", values
            )
            self.connection.executemany(
                "DELETE FROM index_run_entries WHERE doc_id = ?", values
            )
            self.connection.executemany("DELETE FROM entries WHERE doc_id = ?", values)
            self.connection.execute(
                "DELETE FROM folder_catalog WHERE NOT EXISTS ("
                "SELECT 1 FROM entry_folder_index WHERE "
                "entry_folder_index.root_id = folder_catalog.root_id AND "
                "entry_folder_index.relative_folder = "
                "folder_catalog.relative_folder AND "
                "entry_folder_index.is_direct = 1)"
            )
        return len(existing_ids)

    def _invalidate_latest_auto_tag_review_batch(
        self, deleted_doc_ids: set[str]
    ) -> None:
        row = self.connection.execute(
            "SELECT batch_id, status, snapshots_json, result_json "
            "FROM auto_tag_review_batches ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None or str(row["status"]) in {
            "invalidated",
            "undone",
            "rolled_back",
        }:
            return
        try:
            snapshots = json.loads(str(row["snapshots_json"]))
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "Invalid auto-tag review batch JSON during source deletion."
            ) from exc
        if not isinstance(snapshots, list) or not isinstance(result, dict):
            raise ConfigurationError(
                "Invalid auto-tag review batch state during source deletion."
            )
        after_snapshots = result.get("after_snapshots")
        referenced = {
            str(snapshot.get("doc_id") or "")
            for group in (
                snapshots,
                after_snapshots if isinstance(after_snapshots, list) else [],
            )
            for snapshot in group
            if isinstance(snapshot, dict)
        }
        invalidated = sorted(deleted_doc_ids & referenced)
        if not invalidated:
            return
        result.update(
            {
                "undo_available": False,
                "invalidated_reason": "source_deleted",
                "invalidated_doc_ids": invalidated,
            }
        )
        self.connection.execute(
            "UPDATE auto_tag_review_batches SET status = 'invalidated', "
            "result_json = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (json.dumps(result, ensure_ascii=False), str(row["batch_id"])),
        )

    def list_effective_tags(self) -> list[str]:
        rows = self._read_connection().execute(
            "SELECT DISTINCT tag FROM entry_tag_index ORDER BY tag COLLATE NOCASE"
        )
        return [str(row["tag"]) for row in rows]

    def list_entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM entries ORDER BY root_id, relative_path"
        )
        return [self._entry_from_row(row) for row in rows]

    def iter_entries(
        self,
        *,
        chunk_size: int = DEFAULT_STATE_READ_PAGE_SIZE,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield the library in stable keyset pages instead of one full list."""

        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("Entry chunk_size must be an integer.")
        if not 1 <= chunk_size <= MAX_STATE_READ_PAGE_SIZE:
            raise ValueError("Entry chunk_size must be between 1 and 1000.")
        after_root = ""
        after_path = ""
        after_doc_id = ""
        first = True
        while True:
            if first:
                rows = self.connection.execute(
                    "SELECT * FROM entries "
                    "ORDER BY root_id, relative_path, doc_id LIMIT ?",
                    (chunk_size,),
                ).fetchall()
                first = False
            else:
                rows = self.connection.execute(
                    "SELECT * FROM entries WHERE "
                    "root_id > ? OR (root_id = ? AND relative_path > ?) OR "
                    "(root_id = ? AND relative_path = ? AND doc_id > ?) "
                    "ORDER BY root_id, relative_path, doc_id LIMIT ?",
                    (
                        after_root,
                        after_root,
                        after_path,
                        after_root,
                        after_path,
                        after_doc_id,
                        chunk_size,
                    ),
                ).fetchall()
            if not rows:
                return
            yield [self._entry_from_row(row) for row in rows]
            last = rows[-1]
            after_root = str(last["root_id"])
            after_path = str(last["relative_path"])
            after_doc_id = str(last["doc_id"])

    def iter_entries_with_annotations(
        self,
        *,
        chunk_size: int = DEFAULT_STATE_READ_PAGE_SIZE,
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
        """Yield entries and their optional annotations in bounded JOIN pages.

        Metadata discovery needs both records for every image.  Keeping the
        join and keyset cursor in SQLite avoids a full ``list_entries`` copy and
        one annotation query per document while retaining deterministic order.
        """

        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("Joined entry chunk_size must be an integer.")
        if not 1 <= chunk_size <= MAX_STATE_READ_PAGE_SIZE:
            raise ValueError("Joined entry chunk_size must be between 1 and 1000.")
        after_root = ""
        after_path = ""
        after_doc_id = ""
        first = True
        while True:
            if first:
                rows = self.connection.execute(
                    _joined_annotation_select()
                    + " FROM entries LEFT JOIN document_annotations AS annotations "
                    "ON annotations.doc_id = entries.doc_id "
                    "ORDER BY entries.root_id, entries.relative_path, entries.doc_id "
                    "LIMIT ?",
                    (chunk_size,),
                ).fetchall()
                first = False
            else:
                rows = self.connection.execute(
                    _joined_annotation_select()
                    + " FROM entries LEFT JOIN document_annotations AS annotations "
                    "ON annotations.doc_id = entries.doc_id "
                    "WHERE (entries.root_id, entries.relative_path, entries.doc_id) "
                    "> (?, ?, ?) "
                    "ORDER BY entries.root_id, entries.relative_path, entries.doc_id "
                    "LIMIT ?",
                    (
                        after_root,
                        after_path,
                        after_doc_id,
                        chunk_size,
                    ),
                ).fetchall()
            if not rows:
                return
            yield [
                _decode_joined_annotation_row(row, annotation_optional=True)
                for row in rows
            ]
            last = rows[-1]
            after_root = str(last["entry_root_id"])
            after_path = str(last["entry_relative_path"])
            after_doc_id = str(last["entry_doc_id"])

    def get_document_annotations(
        self,
        doc_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch annotations in bounded SQLite parameter batches."""

        normalized = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        result: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(normalized), _SQLITE_PARAMETER_CHUNK):
            chunk = normalized[offset : offset + _SQLITE_PARAMETER_CHUNK]
            if not chunk:
                continue
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM document_annotations WHERE doc_id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                annotation = self._document_annotation_from_row(row)
                result[str(annotation["doc_id"])] = annotation
        return result

    def list_auto_tag_candidates(
        self,
        scope: str,
        *,
        limit: int,
        root_id: str | None = None,
        run_id: str | None = None,
        after: tuple[str, str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return one stable, bounded auto-tag candidate page in a single query.

        Annotation freshness is evaluated inside SQLite by comparing the stored
        source hash with the current entry hash.  This avoids loading the whole
        library and issuing one annotation lookup per image merely to select a
        small model batch.
        """

        normalized_scope = str(scope).strip().lower()
        supported = {"untagged", "failed", "failed_all", "all", "latest_index_run"}
        if normalized_scope not in supported:
            raise ValueError(f"Unsupported auto-tag scope: {scope}")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("Auto-tag candidate limit must be between 1 and 10000.")
        normalized_root = str(root_id).strip() if root_id is not None else None
        if normalized_root == "":
            raise ValueError("root_id must not be empty when supplied.")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 3
            or not all(isinstance(value, str) for value in after)
        ):
            raise ValueError("Auto-tag candidate cursor must contain three strings.")

        joins = [
            "LEFT JOIN document_annotations AS annotations ",
            "ON annotations.doc_id = entries.doc_id ",
        ]
        filters: list[str] = []
        parameters: list[Any] = []
        if normalized_scope == "latest_index_run":
            normalized_run = str(run_id or "").strip()
            if not normalized_run:
                return []
            joins.append(
                "JOIN index_run_entries AS run_entries "
                "ON run_entries.doc_id = entries.doc_id "
            )
            filters.extend(
                ["run_entries.run_id = ?", "run_entries.change_type = 'inserted'"]
            )
            parameters.append(normalized_run)
        elif normalized_scope == "untagged":
            filters.append(
                "(annotations.doc_id IS NULL "
                "OR annotations.source_sha256 != entries.sha256)"
            )
        elif normalized_scope in {"failed", "failed_all"}:
            filters.extend(
                [
                    "annotations.source_sha256 = entries.sha256",
                    "annotations.status = 'failed'",
                ]
            )
            if normalized_scope == "failed":
                filters.append("annotations.retry_eligible = 1")

        if normalized_root is not None:
            filters.append("entries.root_id = ?")
            parameters.append(normalized_root)
        if after is not None:
            after_root, after_path, after_doc_id = after
            filters.append(
                "(entries.root_id > ? "
                "OR (entries.root_id = ? AND entries.relative_path > ?) "
                "OR (entries.root_id = ? AND entries.relative_path = ? "
                "AND entries.doc_id > ?))"
            )
            parameters.extend(
                [
                    after_root,
                    after_root,
                    after_path,
                    after_root,
                    after_path,
                    after_doc_id,
                ]
            )

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self.connection.execute(
            "SELECT entries.* FROM entries " + "".join(joins) + f"{where} "
            "ORDER BY entries.root_id, entries.relative_path, entries.doc_id "
            "LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def page_document_annotations_with_entries(
        self,
        status: str,
        *,
        root_id: str | None = None,
        run_id: str | None = None,
        after: tuple[str, str] | None = None,
        limit: int = 256,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Read a bounded annotation page together with its entry in one query."""

        normalized_status = str(status).strip()
        if not normalized_status:
            raise ValueError("Annotation status must not be empty.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1_000
        ):
            raise ValueError("Annotation page limit must be between 1 and 1000.")
        normalized_root = str(root_id).strip() if root_id is not None else None
        if normalized_root == "":
            raise ValueError("root_id must not be empty when supplied.")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 2
            or not all(isinstance(value, str) for value in after)
        ):
            raise ValueError("Annotation cursor must contain two strings.")

        filters = ["annotations.status = ?"]
        parameters: list[Any] = [normalized_status]
        if normalized_root is not None:
            filters.append("entries.root_id = ?")
            parameters.append(normalized_root)
        if run_id is not None:
            normalized_run = str(run_id).strip()
            if not normalized_run:
                return []
            filters.append(
                "EXISTS (SELECT 1 FROM index_run_entries AS run_entries "
                "WHERE run_entries.run_id = ? "
                "AND run_entries.doc_id = entries.doc_id "
                "AND run_entries.change_type = 'inserted')"
            )
            parameters.append(normalized_run)
        if after is not None:
            after_updated_at, after_doc_id = after
            filters.append(
                "(annotations.updated_at < ? "
                "OR (annotations.updated_at = ? AND annotations.doc_id > ?))"
            )
            parameters.extend([after_updated_at, after_updated_at, after_doc_id])

        rows = self.connection.execute(
            _joined_annotation_select() + " FROM document_annotations AS annotations "
            "JOIN entries ON entries.doc_id = annotations.doc_id "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY annotations.updated_at DESC, annotations.doc_id "
            "LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        decoded = [_decode_joined_annotation_row(row) for row in rows]
        return [
            (annotation, entry)
            for entry, annotation in decoded
            if annotation is not None
        ]

    def iter_entries_for_folders_with_annotations(
        self,
        folders: Iterable[tuple[str, str]],
        *,
        folder_batch_size: int = 100,
        row_batch_size: int = 500,
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
        """Yield only entries in requested direct folders with joined annotations."""

        normalized = list(
            dict.fromkeys(
                (str(root_id), str(relative_folder))
                for root_id, relative_folder in folders
            )
        )
        if not normalized:
            return
        if not 1 <= folder_batch_size <= 200:
            raise ValueError("folder_batch_size must be between 1 and 200.")
        if not 1 <= row_batch_size <= 2_000:
            raise ValueError("row_batch_size must be between 1 and 2000.")

        for offset in range(0, len(normalized), folder_batch_size):
            chunk = normalized[offset : offset + folder_batch_size]
            predicates = " OR ".join(
                "(entries.root_id = ? AND entries.parent_directory = ?)" for _ in chunk
            )
            parameters = tuple(value for folder in chunk for value in folder)
            cursor = self.connection.execute(
                _joined_annotation_select() + " FROM entries "
                "LEFT JOIN document_annotations AS annotations "
                "ON annotations.doc_id = entries.doc_id "
                f"WHERE {predicates} "
                "ORDER BY entries.root_id, entries.relative_path, entries.doc_id",
                parameters,
            )
            batch: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            for row in cursor:
                batch.append(
                    _decode_joined_annotation_row(row, annotation_optional=True)
                )
                if len(batch) >= row_batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch

    def list_folders(
        self,
        *,
        root_id: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> tuple[int, list[dict[str, Any]]]:
        return _list_folders(
            self.connection,
            root_id=root_id,
            query=query,
            offset=offset,
            limit=limit,
        )

    def list_folder_roots(self) -> list[dict[str, Any]]:
        return _list_folder_roots(self.connection)

    def count_folder_entries(
        self,
        root_id: str,
        relative_folder: str,
        *,
        include_subfolders: bool = False,
    ) -> int:
        return _count_folder_entries(
            self.connection,
            root_id,
            relative_folder,
            include_subfolders=include_subfolders,
        )

    def page_folder_entries(
        self,
        root_id: str,
        relative_folder: str,
        *,
        include_subfolders: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[int, list[dict[str, Any]]]:
        return _page_folder_entries(
            self.connection,
            root_id,
            relative_folder,
            include_subfolders=include_subfolders,
            offset=offset,
            limit=limit,
        )

    def iter_folder_entries(
        self,
        root_id: str,
        relative_folder: str,
        *,
        include_subfolders: bool = False,
        chunk_size: int = DEFAULT_STATE_READ_PAGE_SIZE,
    ) -> Iterator[list[dict[str, Any]]]:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("Folder chunk_size must be an integer.")
        if not 1 <= chunk_size <= MAX_STATE_READ_PAGE_SIZE:
            raise ValueError("Folder chunk_size must be between 1 and 1000.")
        offset = 0
        while True:
            _total, entries = self.page_folder_entries(
                root_id,
                relative_folder,
                include_subfolders=include_subfolders,
                offset=offset,
                limit=chunk_size,
            )
            if not entries:
                return
            yield entries
            offset += len(entries)

    def get_many(self, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        normalized = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        result: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(normalized), _SQLITE_PARAMETER_CHUNK):
            chunk = normalized[offset : offset + _SQLITE_PARAMETER_CHUNK]
            if not chunk:
                continue
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._read_connection().execute(
                f"SELECT * FROM entries WHERE doc_id IN ({placeholders})", chunk
            )
            for row in rows:
                entry = self._entry_from_row(row)
                result[str(entry["doc_id"])] = entry
        return result

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
            self._record_folder_index_times(
                run_id,
                [doc_id for _run_id, doc_id, _change_type in values],
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
        policy_json = json.dumps(policy or {}, ensure_ascii=False)
        retry_eligible = (
            _annotation_retry_eligible(policy_json, error) if status == "failed" else 1
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO document_annotations("
                "doc_id, source_sha256, cache_key, status, retry_eligible, "
                "proposed_tags_json, "
                "accepted_tags_json, rejected_tags_json, description, entities_json, "
                "warnings_json, structured_json, policy_json, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "source_sha256=excluded.source_sha256, cache_key=excluded.cache_key, "
                "status=excluded.status, retry_eligible=excluded.retry_eligible, "
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
                    retry_eligible,
                    json.dumps(list(normalize_tags(proposed_tags)), ensure_ascii=False),
                    json.dumps(list(normalize_tags(accepted_tags)), ensure_ascii=False),
                    json.dumps(list(normalize_tags(rejected_tags)), ensure_ascii=False),
                    description,
                    json.dumps(entities or {}, ensure_ascii=False),
                    json.dumps(list(warnings), ensure_ascii=False),
                    json.dumps(structured or {}, ensure_ascii=False),
                    policy_json,
                    error,
                ),
            )

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM document_annotations WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return self._document_annotation_from_row(row) if row else None

    def delete_document_annotation(self, doc_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM document_annotations WHERE doc_id = ?",
                (str(doc_id),),
            )

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

    def count_document_annotations(
        self,
        status: str | None = None,
        *,
        root_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        filters: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            filters.append("document_annotations.status = ?")
            parameters.append(status)
        if root_id is not None:
            normalized_root = str(root_id).strip()
            if not normalized_root:
                return 0
            filters.append("entries.root_id = ?")
            parameters.append(normalized_root)
        if run_id is not None:
            normalized_run = str(run_id).strip()
            if not normalized_run:
                return 0
            filters.append(
                "EXISTS (SELECT 1 FROM index_run_entries AS run_entries "
                "WHERE run_entries.run_id = ? "
                "AND run_entries.doc_id = entries.doc_id "
                "AND run_entries.change_type = 'inserted')"
            )
            parameters.append(normalized_run)
        requires_entry_join = root_id is not None or run_id is not None
        from_sql = "document_annotations"
        if requires_entry_join:
            from_sql += " JOIN entries ON entries.doc_id = document_annotations.doc_id"
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        row = self.connection.execute(
            f"SELECT COUNT(*) AS annotation_count FROM {from_sql}{where}",
            parameters,
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
            "invalidated",
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

    def create_manual_tag_batch(
        self,
        *,
        batch_id: str,
        operation: str,
        selection: dict[str, Any],
        tags: Iterable[str],
        total_count: int,
    ) -> None:
        normalized_id = str(batch_id).strip()
        if not normalized_id:
            raise ValueError("Manual tag batch_id is required.")
        if operation not in {"add", "remove", "replace_manual"}:
            raise ValueError("Invalid manual tag batch operation.")
        if isinstance(total_count, bool) or total_count < 0:
            raise ValueError("Manual tag batch total_count must be non-negative.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO manual_tag_batches("
                "batch_id, status, operation, selection_json, tags_json, total_count"
                ") VALUES(?, 'applying', ?, ?, ?, ?)",
                (
                    normalized_id,
                    operation,
                    json.dumps(selection, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        list(normalize_tags(tags)),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    total_count,
                ),
            )

    def record_manual_tag_batch_entries(
        self,
        batch_id: str,
        snapshots: Iterable[tuple[str, Iterable[str], Iterable[str]]],
    ) -> None:
        values = [
            (
                str(batch_id),
                str(doc_id),
                json.dumps(
                    list(normalize_tags(before)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    list(normalize_tags(after)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "planned",
            )
            for doc_id, before, after in snapshots
        ]
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT INTO manual_tag_batch_entries("
                "batch_id, doc_id, before_tags_json, after_tags_json, status"
                ") VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_id, doc_id) DO NOTHING",
                values,
            )

    def update_manual_tag_batch_entries(
        self,
        batch_id: str,
        outcomes: Iterable[tuple[str, str, str]],
    ) -> None:
        allowed = {
            "planned",
            "applied",
            "failed",
            "undone",
            "undo_failed",
            "conflict",
            "source_deleted",
        }
        values: list[tuple[str, str, str, str]] = []
        for doc_id, status, error in outcomes:
            if status not in allowed:
                raise ValueError("Invalid manual tag batch entry status.")
            values.append((status, str(error or ""), str(batch_id), str(doc_id)))
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE manual_tag_batch_entries SET status = ?, error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND doc_id = ?",
                values,
            )

    def finish_manual_tag_batch(
        self,
        batch_id: str,
        *,
        status: str,
        processed: int,
        updated: int,
        unchanged: int,
        failed: int,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        allowed = {
            "applying",
            "applied",
            "partial",
            "failed",
            "cancelled",
            "undoing",
            "undone",
            "undo_partial",
            "undo_failed",
            "no_changes",
        }
        if status not in allowed:
            raise ValueError("Invalid manual tag batch status.")
        counts = (processed, updated, unchanged, failed)
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("Manual tag batch counters must be non-negative.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE manual_tag_batches SET status = ?, processed_count = ?, "
                "updated_count = ?, unchanged_count = ?, failed_count = ?, "
                "result_json = ?, error = ?, updated_at = CURRENT_TIMESTAMP, "
                "undone_at = CASE WHEN ? = 'undone' THEN CURRENT_TIMESTAMP "
                "ELSE undone_at END WHERE batch_id = ?",
                (
                    status,
                    processed,
                    updated,
                    unchanged,
                    failed,
                    json.dumps(result or {}, ensure_ascii=False),
                    str(error or ""),
                    status,
                    str(batch_id),
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown manual tag batch: {batch_id}")

    def update_manual_tag_batch_progress(
        self,
        batch_id: str,
        *,
        processed: int,
        updated: int,
        unchanged: int,
        failed: int,
    ) -> None:
        counts = (processed, updated, unchanged, failed)
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("Manual tag batch counters must be non-negative.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE manual_tag_batches SET processed_count = ?, "
                "updated_count = ?, unchanged_count = ?, failed_count = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (processed, updated, unchanged, failed, str(batch_id)),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown manual tag batch: {batch_id}")

    def finish_manual_tag_undo(
        self,
        batch_id: str,
        *,
        status: str,
        result: dict[str, Any],
        error: str = "",
    ) -> None:
        if status not in {"undoing", "undone", "undo_partial", "undo_failed"}:
            raise ValueError("Invalid manual tag undo status.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE manual_tag_batches SET status = ?, result_json = ?, "
                "error = ?, updated_at = CURRENT_TIMESTAMP, "
                "undone_at = CASE WHEN ? = 'undone' THEN CURRENT_TIMESTAMP "
                "ELSE undone_at END WHERE batch_id = ?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    str(error or ""),
                    status,
                    str(batch_id),
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown manual tag batch: {batch_id}")

    def latest_manual_tag_batch(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM manual_tag_batches "
            "WHERE updated_count > 0 ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return self._manual_tag_batch_from_row(row) if row else None

    def manual_tag_batch_entries(
        self,
        batch_id: str,
        *,
        statuses: Sequence[str] = ("applied",),
        offset: int = 0,
        limit: int = 256,
        after_doc_id: str = "",
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Manual tag history offset must be non-negative.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Manual tag history limit must be between 1 and 1000.")
        placeholders = ", ".join("?" for _ in statuses)
        after = str(after_doc_id)
        rows = self.connection.execute(
            "SELECT * FROM manual_tag_batch_entries WHERE batch_id = ? "
            f"AND status IN ({placeholders}) AND doc_id > ? "
            "ORDER BY doc_id LIMIT ? OFFSET ?",
            (str(batch_id), *statuses, after, limit, offset),
        )
        return [self._manual_tag_batch_entry_from_row(row) for row in rows]

    def count_manual_tag_batch_entries(
        self,
        batch_id: str,
        *,
        statuses: Sequence[str],
    ) -> int:
        if not statuses:
            return 0
        placeholders = ", ".join("?" for _ in statuses)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM manual_tag_batch_entries WHERE batch_id = ? "
            f"AND status IN ({placeholders})",
            (str(batch_id), *statuses),
        ).fetchone()
        return int(row[0]) if row else 0

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

    def iter_entries_for_root(
        self,
        root_id: str,
        *,
        chunk_size: int = DEFAULT_STATE_READ_PAGE_SIZE,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield one root in bounded stable pages for rebind validation."""

        normalized_root = str(root_id).strip()
        if not normalized_root:
            raise ValueError("root_id must not be empty.")
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("Root entry chunk_size must be an integer.")
        if not 1 <= chunk_size <= MAX_STATE_READ_PAGE_SIZE:
            raise ValueError("Root entry chunk_size must be between 1 and 1000.")
        after_path = ""
        after_doc_id = ""
        first = True
        while True:
            if first:
                rows = self.connection.execute(
                    "SELECT * FROM entries WHERE root_id = ? "
                    "ORDER BY relative_path, doc_id LIMIT ?",
                    (normalized_root, chunk_size),
                ).fetchall()
                first = False
            else:
                rows = self.connection.execute(
                    "SELECT * FROM entries WHERE root_id = ? AND "
                    "(relative_path, doc_id) > (?, ?) "
                    "ORDER BY relative_path, doc_id LIMIT ?",
                    (
                        normalized_root,
                        after_path,
                        after_doc_id,
                        chunk_size,
                    ),
                ).fetchall()
            if not rows:
                return
            yield [self._entry_from_row(row) for row in rows]
            last = rows[-1]
            after_path = str(last["relative_path"])
            after_doc_id = str(last["doc_id"])

    def find_doc_id_by_sha(self, sha256: str) -> str | None:
        row = (
            self._read_connection()
            .execute(
                "SELECT doc_id FROM entries WHERE sha256 = ? ORDER BY doc_id LIMIT 1",
                (sha256,),
            )
            .fetchone()
        )
        return str(row["doc_id"]) if row else None

    def find_doc_ids_by_sha_many(self, shas: Iterable[str]) -> dict[str, str]:
        """Return one deterministic existing document id for each known hash.

        ``json_each`` keeps this to one SQLite statement without depending on
        the connection's parameter limit.  The covering ``(sha256, doc_id)``
        index lets SQLite select the lexicographically first id for each hash
        without loading all matching entries into Python.
        """

        normalized = list(dict.fromkeys(str(sha256) for sha256 in shas))
        if not normalized:
            return {}
        rows = self._read_connection().execute(
            "WITH requested(sha256) AS ("
            "SELECT DISTINCT CAST(value AS TEXT) FROM json_each(?)"
            ") "
            "SELECT requested.sha256, MIN(entries.doc_id) AS doc_id "
            "FROM requested JOIN entries "
            "ON entries.sha256 = requested.sha256 "
            "GROUP BY requested.sha256",
            (json.dumps(normalized, ensure_ascii=True),),
        )
        return {str(row["sha256"]): str(row["doc_id"]) for row in rows}

    def find_entry_for_path(self, path: Path) -> dict[str, Any] | None:
        resolved = path.expanduser().resolve()
        for root_id, current_path in self._list_roots_light():
            root_path = Path(current_path)
            try:
                relative = resolved.relative_to(root_path)
            except ValueError:
                continue
            doc_id = logical_document_id(root_id, relative)
            return self.get(doc_id)
        return None

    def _list_roots_light(self) -> list[tuple[str, str]]:
        if self._roots_light_cache is None:
            rows = (
                self._read_connection()
                .execute(
                    "SELECT root_id, current_path FROM roots ORDER BY current_path"
                )
                .fetchall()
            )
            self._roots_light_cache = [
                (str(row["root_id"]), str(row["current_path"])) for row in rows
            ]
        return self._roots_light_cache

    def get_cached_vector(self, cache_key: str, dimension: int) -> list[float] | None:
        row = (
            self._read_connection()
            .execute(
                "SELECT embedding, dimension FROM embedding_cache WHERE cache_key = ?",
                (cache_key,),
            )
            .fetchone()
        )
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
        self._cache_hit_counter += 1
        if self._cache_hit_counter % 32 == 0:
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
        row = (
            self._read_connection()
            .execute("SELECT COUNT(*) AS count FROM entries")
            .fetchone()
        )
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
        self._roots_light_cache = None

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

    def manual_tag_undo_available(self) -> bool:
        row = self.connection.execute(
            "SELECT batch_id, status FROM manual_tag_batches "
            "WHERE updated_count > 0 ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None or str(row["status"]) == "undone":
            return False
        pending = self.connection.execute(
            "SELECT 1 FROM manual_tag_batch_entries WHERE batch_id = ? "
            "AND status IN ('applied', 'undo_failed', 'conflict') LIMIT 1",
            (str(row["batch_id"]),),
        ).fetchone()
        return pending is not None

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
        self._roots_light_cache = None
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
    def _document_annotation_from_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
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
    def _manual_tag_batch_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["selection"] = cls._decode_json_object(
            item.pop("selection_json"), "manual tag batch selection"
        )
        item["tags"] = cls._decode_tags(str(item.pop("tags_json")))
        item["result"] = cls._decode_json_object(
            item.pop("result_json"), "manual tag batch result"
        )
        return item

    @classmethod
    def _manual_tag_batch_entry_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["before_tags"] = cls._decode_tags(str(item.pop("before_tags_json")))
        item["after_tags"] = cls._decode_tags(str(item.pop("after_tags_json")))
        return item

    @classmethod
    def _entry_from_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        entry = dict(row)
        entry["tags"] = cls._decode_tags(str(entry.pop("tags_json")))
        entry["folder_tags"] = cls._decode_tags(
            str(entry.pop("folder_tags_json", "[]"))
        )
        entry["accepted_auto_tags"] = cls._decode_tags(
            str(entry.pop("accepted_auto_tags_json", "[]"))
        )
        entry["inherited_tags"] = cls._decode_tags(
            str(entry.pop("inherited_tags_json", "[]"))
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
            "inherited_tags_json": json.dumps(
                list(normalize_tags(entry.get("inherited_tags", ()))),
                ensure_ascii=False,
            ),
        }
        return tuple(
            encoded_tags[column]
            if column in encoded_tags
            else _relative_parent_directory(str(entry["relative_path"]))
            if column == "parent_directory"
            else entry[column]
            for column in ENTRY_COLUMNS
        )

    @staticmethod
    def _effective_tags(entry: dict[str, Any]) -> tuple[str, ...]:
        return normalize_tags(
            [
                *entry.get("tags", ()),
                *entry.get("folder_tags", ()),
                *entry.get("accepted_auto_tags", ()),
                *entry.get("inherited_tags", ()),
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

    def _replace_folder_index(self, entry: dict[str, Any]) -> None:
        doc_id = str(entry["doc_id"])
        root_id = str(entry["root_id"])
        direct = _relative_parent_directory(str(entry["relative_path"]))
        self.connection.execute(
            "DELETE FROM entry_folder_index WHERE doc_id = ?", (doc_id,)
        )
        self.connection.executemany(
            "INSERT INTO entry_folder_index("
            "doc_id, root_id, relative_folder, is_direct) VALUES(?, ?, ?, ?)",
            [
                (doc_id, root_id, folder, int(folder == direct))
                for folder in _folder_ancestors(direct)
            ],
        )

    def _ensure_folder_catalog(
        self, entry: dict[str, Any], first_indexed_at: str
    ) -> None:
        """Record only the image's actionable, direct parent directory."""

        self.connection.execute(
            "INSERT INTO folder_catalog("
            "root_id, relative_folder, first_indexed_at, last_indexed_at, "
            "timestamp_source) VALUES(?, ?, ?, ?, 'observed') "
            "ON CONFLICT(root_id, relative_folder) DO NOTHING",
            (
                str(entry["root_id"]),
                _relative_parent_directory(str(entry["relative_path"])),
                first_indexed_at,
                first_indexed_at,
            ),
        )

    def _record_folder_index_times(
        self,
        run_id: str,
        doc_ids: Iterable[str],
    ) -> None:
        """Update folder chronology only for explicit index-run membership."""

        normalized_ids = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not normalized_ids:
            return
        run = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', started_at) "
            "FROM index_runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if run is None:
            raise ConfigurationError(f"Unknown index run: {run_id}")
        indexed_at = str(run[0])
        folders: set[tuple[str, str]] = set()
        for offset in range(0, len(normalized_ids), _SQLITE_PARAMETER_CHUNK):
            chunk = normalized_ids[offset : offset + _SQLITE_PARAMETER_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT root_id, parent_directory FROM entries "
                f"WHERE doc_id IN ({placeholders})",
                chunk,
            )
            folders.update(
                (str(row["root_id"]), str(row["parent_directory"])) for row in rows
            )
        self.connection.executemany(
            "INSERT INTO folder_catalog("
            "root_id, relative_folder, first_indexed_at, last_indexed_at, "
            "timestamp_source) VALUES(?, ?, ?, ?, 'index_run') "
            "ON CONFLICT(root_id, relative_folder) DO UPDATE SET "
            "first_indexed_at = MIN(folder_catalog.first_indexed_at, "
            "excluded.first_indexed_at), "
            "last_indexed_at = MAX(folder_catalog.last_indexed_at, "
            "excluded.last_indexed_at), "
            "timestamp_source = CASE WHEN folder_catalog.timestamp_source = "
            "'observed' THEN 'index_run' ELSE folder_catalog.timestamp_source END",
            [
                (root_id, relative_folder, indexed_at, indexed_at)
                for root_id, relative_folder in sorted(folders)
            ],
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
        self._roots_light_cache = None
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

    def _close_all_connections(self) -> None:
        read_conn = getattr(self._read_local, "conn", None)
        if read_conn is not None:
            read_conn.close()
            self._read_local.conn = None
        if hasattr(self, "connection"):
            self.connection.close()

    # ---- File-system change queue (watchdog incremental index) ----

    def enqueue_change(self, root_id: str, relative_path: str, event_type: str) -> None:
        """Record or refresh a pending file change for *root_id*.

        Uses ``ON CONFLICT`` so repeated events for the same file refresh the
        timestamp and event type without creating duplicates.
        """
        with self.connection:
            self.connection.execute(
                "INSERT INTO fs_change_queue(root_id, relative_path, event_type) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(root_id, relative_path) DO UPDATE SET "
                "event_type=excluded.event_type, "
                "queued_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "processed=0",
                (root_id, relative_path, event_type),
            )

    def claim_pending_changes(
        self,
        root_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically claim pending changes without marking them complete."""

        if limit is not None and limit < 1:
            raise ValueError("limit must be positive.")
        limit_clause = " LIMIT ?" if limit is not None else ""
        parameters: list[object] = [
            _FS_CHANGE_CLAIMED,
            root_id,
            _FS_CHANGE_PENDING,
        ]
        if limit is not None:
            parameters.append(limit)
        parameters.append(_FS_CHANGE_PENDING)
        with self.connection:
            rows = self.connection.execute(
                "UPDATE fs_change_queue SET processed = ? WHERE id IN ("
                "SELECT id FROM fs_change_queue "
                "WHERE root_id = ? AND processed = ? "
                f"ORDER BY queued_at, id{limit_clause}"
                ") AND processed = ? "
                "RETURNING id, root_id, relative_path, event_type, queued_at",
                parameters,
            ).fetchall()
        rows = sorted(rows, key=lambda row: (str(row["queued_at"]), int(row["id"])))
        return [
            {
                "id": int(row["id"]),
                "root_id": str(row["root_id"]),
                "relative_path": str(row["relative_path"]),
                "event_type": str(row["event_type"]),
                "queued_at": str(row["queued_at"]),
            }
            for row in rows
        ]

    def acknowledge_claimed_changes(self, change_ids: Iterable[int]) -> int:
        """Mark successfully handled claims as processed."""

        return self._update_claimed_changes(
            change_ids,
            target_state=_FS_CHANGE_PROCESSED,
        )

    def release_claimed_changes(self, change_ids: Iterable[int]) -> int:
        """Return interrupted claims to the pending queue."""

        return self._update_claimed_changes(
            change_ids,
            target_state=_FS_CHANGE_PENDING,
        )

    def _update_claimed_changes(
        self,
        change_ids: Iterable[int],
        *,
        target_state: int,
    ) -> int:
        ids = list(dict.fromkeys(int(change_id) for change_id in change_ids))
        if not ids:
            return 0
        updated = 0
        with self.connection:
            for offset in range(0, len(ids), _SQLITE_PARAMETER_CHUNK):
                chunk = ids[offset : offset + _SQLITE_PARAMETER_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = self.connection.execute(
                    f"UPDATE fs_change_queue SET processed = ? "
                    f"WHERE processed = ? AND id IN ({placeholders})",
                    (target_state, _FS_CHANGE_CLAIMED, *chunk),
                )
                updated += cursor.rowcount
        return updated

    def recover_interrupted_changes(self, root_id: str) -> dict[str, int]:
        """Restore interrupted claims and close stale index runs at startup."""

        with self.connection:
            changes = self.connection.execute(
                "UPDATE fs_change_queue SET processed = ? "
                "WHERE root_id = ? AND processed = ?",
                (_FS_CHANGE_PENDING, root_id, _FS_CHANGE_CLAIMED),
            )
            index_runs = self.connection.execute(
                "UPDATE index_runs SET status = 'failed', "
                "finished_at = CURRENT_TIMESTAMP, needs_attention = 1 "
                "WHERE root_id = ? AND status = 'running'",
                (root_id,),
            )
        return {
            "changes": changes.rowcount,
            "index_runs": index_runs.rowcount,
        }

    def drain_pending_changes(self, root_id: str) -> list[dict[str, Any]]:
        """Compatibility helper that immediately acknowledges claimed changes."""

        changes = self.claim_pending_changes(root_id)
        self.acknowledge_claimed_changes(
            int(change["id"]) for change in changes
        )
        return changes

    def count_pending_changes(self, root_id: str) -> int:
        row = (
            self._read_connection()
            .execute(
                "SELECT COUNT(*) FROM fs_change_queue "
                "WHERE root_id = ? AND processed = 0",
                (root_id,),
            )
            .fetchone()
        )
        return int(row[0]) if row else 0

    def clear_processed_changes(self, *, older_than_days: int = 7) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM fs_change_queue WHERE processed = 1 "
                "AND queued_at < datetime('now', ?)",
                (f"-{older_than_days} days",),
            )
            return cursor.rowcount

    def close(self) -> None:
        self._close_all_connections()


class IndexStateReader:
    """Independent WAL reader used by responsive gallery endpoints.

    The persistent service owns the writable :class:`IndexState` connection on
    its Collection worker.  Opening a short-lived query-only connection lets the
    desktop browse folders while a long index or annotation task is running.
    """

    def __init__(self, path: Path):
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ConfigurationError(f"State database is missing: {resolved}")
        try:
            self.connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro",
                uri=True,
                timeout=2,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA busy_timeout=2000")
        except sqlite3.DatabaseError as exc:
            raise ConfigurationError(
                f"Cannot open the state database for browsing: {resolved}"
            ) from exc

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def root_path(self, root_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT current_path FROM roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        return str(row["current_path"]) if row else None

    def manual_tag_undo_available(self) -> bool:
        row = self.connection.execute(
            "SELECT batch_id, status FROM manual_tag_batches "
            "WHERE updated_count > 0 ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None or str(row["status"]) == "undone":
            return False
        pending = self.connection.execute(
            "SELECT 1 FROM manual_tag_batch_entries WHERE batch_id = ? "
            "AND status IN ('applied', 'undo_failed', 'conflict') LIMIT 1",
            (str(row["batch_id"]),),
        ).fetchone()
        return pending is not None

    def list_folders(
        self,
        *,
        root_id: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> tuple[int, list[dict[str, Any]]]:
        return _list_folders(
            self.connection,
            root_id=root_id,
            query=query,
            offset=offset,
            limit=limit,
        )

    def list_folder_roots(self) -> list[dict[str, Any]]:
        return _list_folder_roots(self.connection)

    def page_folder_entries(
        self,
        root_id: str,
        relative_folder: str,
        *,
        include_subfolders: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[int, list[dict[str, Any]]]:
        return _page_folder_entries(
            self.connection,
            root_id,
            relative_folder,
            include_subfolders=include_subfolders,
            offset=offset,
            limit=limit,
        )

    def resolve_document_path(self, doc_id: str) -> Path:
        row = self.connection.execute(
            "SELECT entries.root_id, entries.relative_path, roots.current_path "
            "FROM entries JOIN roots ON roots.root_id = entries.root_id "
            "WHERE entries.doc_id = ?",
            (str(doc_id),),
        ).fetchone()
        if row is None:
            raise ConfigurationError(f"Unknown document: {doc_id}")
        return resolve_under_root(str(row["current_path"]), str(row["relative_path"]))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> IndexStateReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _relative_parent_directory(relative_path: str) -> str:
    normalized = str(relative_path).replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    return "/".join(parts[:-1]) if len(parts) > 1 else ""


def _folder_ancestors(relative_folder: str) -> tuple[str, ...]:
    normalized = _normalized_relative_folder(relative_folder)
    if not normalized:
        return ("",)
    parts = PurePosixPath(normalized).parts
    return ("", *("/".join(parts[:index]) for index in range(1, len(parts) + 1)))


def _normalized_relative_folder(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_folder must stay inside its registered root.")
    return "/".join(parts)


def _validate_page(offset: int, limit: int, *, maximum: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Folder page offset must be a non-negative integer.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum
    ):
        raise ValueError(f"Folder page limit must be between 1 and {maximum}.")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_folders(
    connection: sqlite3.Connection,
    *,
    root_id: str | None,
    query: str,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    _validate_page(offset, limit, maximum=1_000)
    normalized_query = str(query).strip()
    where: list[str] = []
    values: list[Any] = []
    if root_id is not None:
        normalized_root = str(root_id).strip()
        if not normalized_root:
            raise ValueError("root_id must not be empty when supplied.")
        where.append("folder_index.root_id = ?")
        values.append(normalized_root)
    if normalized_query:
        pattern = f"%{_escape_like(normalized_query)}%"
        where.append(
            "(folder_index.relative_folder LIKE ? ESCAPE '\\' "
            "OR roots.current_path LIKE ? ESCAPE '\\')"
        )
        values.extend((pattern, pattern))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    grouped_from = (
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
    count_row = connection.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 {grouped_from})", values
    ).fetchone()
    total = int(count_row[0]) if count_row else 0
    rows = connection.execute(
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
        "LIMIT ? OFFSET ?",
        (*values, limit, offset),
    )
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
                    PurePosixPath(relative_folder).name
                    if relative_folder
                    else root_name
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


def _list_folder_roots(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every registered root, including a clear zero-image state."""

    rows = connection.execute(
        "SELECT roots.root_id, roots.current_path, "
        "COUNT(DISTINCT entries.parent_directory) AS folder_count, "
        "COUNT(entries.doc_id) AS image_count, "
        "MIN(folder_catalog.first_indexed_at) AS first_indexed_at, "
        "MAX(folder_catalog.last_indexed_at) AS last_indexed_at "
        "FROM roots LEFT JOIN entries ON entries.root_id = roots.root_id "
        "LEFT JOIN folder_catalog ON folder_catalog.root_id = entries.root_id "
        "AND folder_catalog.relative_folder = entries.parent_directory "
        "GROUP BY roots.root_id "
        "ORDER BY CASE WHEN COUNT(entries.doc_id) = 0 THEN 1 ELSE 0 END, "
        "MIN(folder_catalog.first_indexed_at) DESC, "
        "roots.current_path COLLATE NOCASE, roots.current_path, roots.root_id"
    )
    summaries: list[dict[str, Any]] = []
    for row in rows:
        current_path = str(row["current_path"])
        summaries.append(
            {
                "root_id": str(row["root_id"]),
                "root_name": Path(current_path).name or current_path,
                "folder_count": int(row["folder_count"]),
                "image_count": int(row["image_count"]),
                "is_empty": int(row["image_count"]) == 0,
                "first_indexed_at": str(row["first_indexed_at"] or ""),
                "last_indexed_at": str(row["last_indexed_at"] or ""),
            }
        )
    return summaries


def _folder_predicate(
    root_id: str,
    relative_folder: str,
    *,
    include_subfolders: bool,
) -> tuple[str, tuple[Any, ...]]:
    normalized_root = str(root_id).strip()
    if not normalized_root:
        raise ValueError("root_id must be a non-empty string.")
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


def _count_folder_entries(
    connection: sqlite3.Connection,
    root_id: str,
    relative_folder: str,
    *,
    include_subfolders: bool,
) -> int:
    predicate, values = _folder_predicate(
        root_id,
        relative_folder,
        include_subfolders=include_subfolders,
    )
    row = connection.execute(
        f"SELECT COUNT(*) FROM entries WHERE {predicate.replace('entries.', '')}",
        values,
    ).fetchone()
    return int(row[0]) if row else 0


def _page_folder_entries(
    connection: sqlite3.Connection,
    root_id: str,
    relative_folder: str,
    *,
    include_subfolders: bool,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    _validate_page(offset, limit, maximum=1_000)
    predicate, values = _folder_predicate(
        root_id,
        relative_folder,
        include_subfolders=include_subfolders,
    )
    count_row = connection.execute(
        f"SELECT COUNT(*) FROM entries WHERE {predicate.replace('entries.', '')}",
        values,
    ).fetchone()
    total = int(count_row[0]) if count_row else 0
    rows = connection.execute(
        "SELECT entries.*, document_annotations.status AS annotation_status, "
        "document_annotations.policy_json AS annotation_policy_json "
        "FROM entries LEFT JOIN document_annotations "
        "ON document_annotations.doc_id = entries.doc_id "
        f"WHERE {predicate} "
        "ORDER BY entries.relative_path COLLATE NOCASE, entries.doc_id "
        "LIMIT ? OFFSET ?",
        (*values, limit, offset),
    )
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = IndexState._entry_from_row(row)
        entry["annotation_status"] = str(entry.get("annotation_status") or "")
        entry["annotation_policy"] = IndexState._decode_json_object(
            entry.pop("annotation_policy_json", "{}"), "annotation policy"
        )
        entries.append(entry)
    return total, entries


def _joined_annotation_select() -> str:
    entry_columns = ", ".join(
        f"entries.{column} AS entry_{column}" for column in ENTRY_COLUMNS
    )
    annotation_columns = ", ".join(
        f"annotations.{column} AS annotation_{column}"
        for column in DOCUMENT_ANNOTATION_COLUMNS
    )
    return f"SELECT {entry_columns}, {annotation_columns}"


def _decode_joined_annotation_row(
    row: Mapping[str, Any],
    *,
    annotation_optional: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    entry = IndexState._entry_from_row(
        {column: row[f"entry_{column}"] for column in ENTRY_COLUMNS}
    )
    if row["annotation_doc_id"] is None:
        if annotation_optional:
            return entry, None
        raise ConfigurationError("Joined annotation row is missing its annotation.")
    annotation = IndexState._document_annotation_from_row(
        {column: row[f"annotation_{column}"] for column in DOCUMENT_ANNOTATION_COLUMNS}
    )
    return entry, annotation


def _annotation_retry_eligible(policy_value: Any, error: Any) -> int:
    """Return the persisted retry decision used by failed auto-tag queries."""

    policy: Mapping[str, Any]
    if isinstance(policy_value, Mapping):
        policy = policy_value
    else:
        try:
            decoded = json.loads(str(policy_value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        policy = decoded if isinstance(decoded, Mapping) else {}

    explicit = policy.get("retry_eligible")
    if isinstance(explicit, bool):
        return int(explicit)
    category = str(policy.get("failure_category") or "").strip().casefold()
    if category:
        return int(category not in {"content_policy", "provider_auth", "systemic"})

    text = str(error or "").casefold()
    blocked_markers = (
        "data_inspection_failed",
        "datainspectionfailed",
        "inappropriate content",
        "content policy",
        "invalid api key",
        "invalidapikey",
        "unauthorized",
        "authentication",
        "permission denied",
    )
    return int(not any(marker in text for marker in blocked_markers))
