from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import ConfigurationError

ENTRY_COLUMNS = (
    "doc_id",
    "root_path",
    "relative_path",
    "absolute_path",
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
            self._create_schema()
            self._validate_schema_version()
            if legacy_path is not None:
                self._migrate_legacy_json(legacy_path)
        except sqlite3.DatabaseError as exc:
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
                root_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                absolute_path TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_entries_root_path ON entries(root_path);
            CREATE TABLE IF NOT EXISTS roots (
                root_path TEXT PRIMARY KEY,
                recursive INTEGER NOT NULL
            );
            """
        )
        self.connection.commit()

    def _validate_schema_version(self) -> None:
        version = self.get_metadata("state_schema_version")
        if version is None:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) "
                    "VALUES('state_schema_version', '1')"
                )
        elif version != "1":
            raise ConfigurationError(f"Unsupported state schema version: {version}")

    def _migrate_legacy_json(self, legacy_path: Path) -> None:
        if self.count() or not legacy_path.is_file():
            return
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        entries = list((data.get("entries") or {}).values())
        if entries:
            self.set_many(entries)
            for entry in entries:
                self.record_root(str(entry["root_path"]), recursive=True)
        migrated = legacy_path.with_suffix(legacy_path.suffix + ".migrated")
        legacy_path.replace(migrated)

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

    def ids_for_root(self, root_path: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT doc_id FROM entries WHERE root_path = ?", (root_path,)
        )
        return {str(row["doc_id"]) for row in rows}

    def find_doc_id_by_sha(self, sha256: str) -> str | None:
        row = self.connection.execute(
            "SELECT doc_id FROM entries WHERE sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        return str(row["doc_id"]) if row else None

    def count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM entries"
        ).fetchone()
        return int(row["count"])

    def record_root(self, root_path: str, recursive: bool) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO roots(root_path, recursive) VALUES(?, ?) "
                "ON CONFLICT(root_path) DO UPDATE SET recursive=excluded.recursive",
                (root_path, int(recursive)),
            )

    def root_scope(self, root_path: str) -> bool | None:
        row = self.connection.execute(
            "SELECT recursive FROM roots WHERE root_path = ?", (root_path,)
        ).fetchone()
        return bool(row["recursive"]) if row else None

    def find_overlapping_root(self, root_path: str) -> str | None:
        candidate = os.path.normcase(os.path.abspath(root_path))
        rows = self.connection.execute("SELECT root_path FROM roots")
        for row in rows:
            existing = os.path.normcase(os.path.abspath(str(row["root_path"])))
            if existing == candidate:
                continue
            try:
                common = os.path.commonpath([existing, candidate])
            except ValueError:
                continue
            if common in {existing, candidate}:
                return str(row["root_path"])
        return None

    def close(self) -> None:
        self.connection.close()
