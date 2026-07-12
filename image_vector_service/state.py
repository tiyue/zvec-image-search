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
)


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
                    "'image_service.py migrate-path-schema --dry-run' first."
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
                height INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entries_sha256 ON entries(sha256);
            CREATE INDEX IF NOT EXISTS idx_entries_root_id ON entries(root_id);
            CREATE TABLE IF NOT EXISTS roots (
                root_id TEXT PRIMARY KEY,
                current_path TEXT NOT NULL UNIQUE,
                recursive INTEGER NOT NULL
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
            """
        )
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
        return dict(row) if row else None

    def set_many(self, entries: Iterable[dict[str, Any]]) -> None:
        values = [tuple(entry[column] for column in ENTRY_COLUMNS) for entry in entries]
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

    def remove_many(self, doc_ids: Iterable[str]) -> None:
        values = [(doc_id,) for doc_id in doc_ids]
        if not values:
            return
        with self.connection:
            self.connection.executemany("DELETE FROM entries WHERE doc_id = ?", values)

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
        return [dict(row) for row in rows]

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
        normalized = normalize_path(Path(current_path))
        row = self.connection.execute(
            "SELECT root_id FROM roots WHERE current_path = ?", (normalized,)
        ).fetchone()
        if row:
            return str(row["root_id"])
        root_id = str(uuid.uuid4())
        self.record_root(root_id, normalized, recursive)
        return root_id

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
            "COUNT(entries.doc_id) AS document_count FROM roots "
            "LEFT JOIN entries ON entries.root_id = roots.root_id "
            "GROUP BY roots.root_id ORDER BY roots.current_path"
        )
        return [
            {
                "root_id": str(row["root_id"]),
                "current_path": str(row["current_path"]),
                "recursive": bool(row["recursive"]),
                "document_count": int(row["document_count"]),
            }
            for row in rows
        ]

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
