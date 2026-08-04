"""Independent SQLite persistence for the ARW selection module.

This database stores projects, source assets, project-member relationships,
ratings, color labels, creative-look selections, workspace state and
two-phase operation logs. It never touches the existing gallery state DB,
recommendation DB or zvec collection.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_VERSION = 2
_MAX_NAME_LENGTH = 40

VALID_STAR_RATINGS = frozenset({0, 1, 2, 3, 4, 5})
VALID_COLOR_LABELS = frozenset({"none", "red", "yellow", "green", "blue", "purple"})
VALID_SORT_FIELDS = frozenset(
    {
        "filename",
        "import_order",
        "shot_time",
        "star_rating",
        "mtime_ns",
        "extension",
        "file_size",
    }
)
VALID_SORT_DIRECTIONS = frozenset({"asc", "desc"})
VALID_FILTER_STAR_MODES = frozenset({"exact", "at_least", "unrated", "none"})
VALID_FILTER_RATED = frozenset({"all", "rated", "unrated"})
VALID_FILTER_EXPORTED = frozenset({"all", "exported", "unexported"})
VALID_FILTER_FORMATS = frozenset({"arw", "jpeg", "png"})
VALID_FILTER_ORIENTATIONS = frozenset({"landscape", "portrait", "square"})
VALID_METADATA_STATUSES = frozenset({"pending", "processing", "ready", "failed"})
VALID_OPERATION_TYPES = frozenset({"import", "permanent_delete", "export"})
VALID_OPERATION_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled"}
)
VALID_OP_ITEM_RESULTS = frozenset(
    {
        "pending",
        "deleted",
        "already_missing",
        "exported",
        "cancelled",
        "failed",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    id: str
    name: str
    member_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AssetRecord:
    id: str
    normalized_path: str
    file_name: str
    extension: str
    file_size: int
    mtime_ns: int
    file_identity: str | None
    shot_time: str | None
    width: int | None
    height: int | None
    metadata_status: str


@dataclass(frozen=True, slots=True)
class AssetRegistration:
    normalized_path: str
    file_name: str
    extension: str
    file_size: int
    mtime_ns: int
    file_identity: str | None


@dataclass(frozen=True, slots=True)
class MemberRecord:
    id: str
    project_id: str
    asset_id: str
    import_order: int
    star_rating: int
    color_label: str
    creative_look: str
    file_name: str
    normalized_path: str
    extension: str
    file_size: int
    mtime_ns: int
    file_identity: str | None
    shot_time: str | None
    width: int | None
    height: int | None
    metadata_status: str
    exported_at: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    last_member_id: str | None
    filter_star_mode: str
    filter_star_value: int
    filter_color_labels: str
    filter_filename: str
    filter_rated: str
    filter_exported: str
    filter_formats: str
    filter_orientations: str
    sort_field: str
    sort_direction: str
    filmstrip_scroll: float


@dataclass(frozen=True, slots=True)
class OperationLogEntry:
    id: str
    operation_type: str
    status: str
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class OperationLogItem:
    id: str
    log_id: str
    asset_id: str
    normalized_path: str
    source_file_size: int | None
    source_mtime_ns: int | None
    source_file_identity: str | None
    source_extension: str | None
    result: str
    error_message: str | None


@dataclass(frozen=True, slots=True)
class OperationTarget:
    asset_id: str
    normalized_path: str
    source_file_size: int
    source_mtime_ns: int
    source_file_identity: str | None
    source_extension: str


def _generate_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class RawSelectionDB:
    """Thread-safe independent SQLite store for ARW selection projects."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self._conn = sqlite3.connect(
                str(self.path), timeout=5, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            try:
                self._create_schema()
            except Exception:
                self._conn.close()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _create_schema(self) -> None:
        conn = self._conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()

        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        existing_tables = {
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if row is None:
            if {"projects", "assets", "project_members"} & existing_tables:
                version = 1
            else:
                self._create_latest_schema_objects()
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
                conn.commit()
                return
        else:
            try:
                version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid RAW selection schema version.") from exc

        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                "RAW selection database schema is newer than this application: "
                f"{version} > {_SCHEMA_VERSION}."
            )
        if version < 1:
            raise RuntimeError(f"Unsupported RAW selection schema version: {version}.")
        if version == 1:
            self._migrate_v1_to_v2()
        self._create_latest_schema_objects()
        self._validate_latest_schema()
        conn.commit()

    def _create_latest_schema_objects(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS projects ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS assets ("
            "id TEXT PRIMARY KEY, normalized_path TEXT NOT NULL UNIQUE, "
            "file_name TEXT NOT NULL, extension TEXT NOT NULL, "
            "file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, "
            "file_identity TEXT, shot_time TEXT, width INTEGER, height INTEGER, "
            "metadata_status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS project_members ("
            "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_id TEXT NOT NULL, "
            "import_order INTEGER NOT NULL, star_rating INTEGER NOT NULL DEFAULT 0, "
            "color_label TEXT NOT NULL DEFAULT 'none', "
            "creative_look TEXT NOT NULL DEFAULT 'as_shot', exported_at TEXT, "
            "created_at TEXT NOT NULL, UNIQUE(project_id, asset_id), "
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE, "
            "FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE RESTRICT)",
            "CREATE INDEX IF NOT EXISTS idx_members_project "
            "ON project_members(project_id, import_order)",
            "CREATE TABLE IF NOT EXISTS workspace_state ("
            "project_id TEXT PRIMARY KEY, last_member_id TEXT, "
            "filter_star_mode TEXT NOT NULL DEFAULT 'none', "
            "filter_star_value INTEGER NOT NULL DEFAULT 0, "
            "filter_color_labels TEXT NOT NULL DEFAULT '', "
            "filter_filename TEXT NOT NULL DEFAULT '', "
            "filter_rated TEXT NOT NULL DEFAULT 'all', "
            "filter_exported TEXT NOT NULL DEFAULT 'all', "
            "filter_formats TEXT NOT NULL DEFAULT '', "
            "filter_orientations TEXT NOT NULL DEFAULT '', "
            "sort_field TEXT NOT NULL DEFAULT 'filename', "
            "sort_direction TEXT NOT NULL DEFAULT 'asc', "
            "filmstrip_scroll REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS operation_log ("
            "id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TEXT NOT NULL, completed_at TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_oplog_status "
            "ON operation_log(status, created_at)",
            "CREATE TABLE IF NOT EXISTS operation_log_items ("
            "id TEXT PRIMARY KEY, log_id TEXT NOT NULL, asset_id TEXT NOT NULL, "
            "normalized_path TEXT NOT NULL, source_file_size INTEGER, "
            "source_mtime_ns INTEGER, source_file_identity TEXT, "
            "source_extension TEXT, result TEXT NOT NULL DEFAULT 'pending', "
            "error_message TEXT, seq INTEGER NOT NULL, "
            "FOREIGN KEY (log_id) REFERENCES operation_log(id) ON DELETE CASCADE)",
            "CREATE INDEX IF NOT EXISTS idx_oplog_items_log "
            "ON operation_log_items(log_id, seq)",
        )
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_v1_to_v2(self) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            additions = {
                "assets": (
                    ("width", "INTEGER"),
                    ("height", "INTEGER"),
                    ("metadata_status", "TEXT NOT NULL DEFAULT 'pending'"),
                ),
                "project_members": (("exported_at", "TEXT"),),
                "workspace_state": (
                    ("filter_exported", "TEXT NOT NULL DEFAULT 'all'"),
                    ("filter_formats", "TEXT NOT NULL DEFAULT ''"),
                    ("filter_orientations", "TEXT NOT NULL DEFAULT ''"),
                ),
                "operation_log_items": (
                    ("source_file_size", "INTEGER"),
                    ("source_mtime_ns", "INTEGER"),
                    ("source_file_identity", "TEXT"),
                    ("source_extension", "TEXT"),
                ),
            }
            for table, columns in additions.items():
                existing = self._column_names(table)
                for name, declaration in columns:
                    if name not in existing:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(_SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _column_names(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _validate_latest_schema(self) -> None:
        required = {
            "assets": {"width", "height", "metadata_status"},
            "project_members": {"exported_at"},
            "workspace_state": {
                "filter_exported",
                "filter_formats",
                "filter_orientations",
            },
            "operation_log_items": {
                "source_file_size",
                "source_mtime_ns",
                "source_file_identity",
                "source_extension",
            },
        }
        for table, columns in required.items():
            missing = columns - self._column_names(table)
            if missing:
                raise RuntimeError(
                    f"RAW selection schema is missing {table} columns: "
                    f"{', '.join(sorted(missing))}."
                )

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def create_project(self, name: str) -> ProjectSummary:
        name = name.strip()
        if not name or len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"Project name must be 1-{_MAX_NAME_LENGTH} characters after trimming."
            )
        pid = _generate_id()
        now = _now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO projects(id, name, created_at, "
                    "updated_at) VALUES(?,?,?,?)",
                    (pid, name, now, now),
                )
                self._conn.execute(
                    "INSERT INTO workspace_state(project_id, updated_at) VALUES(?,?)",
                    (pid, now),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return ProjectSummary(
            id=pid, name=name, member_count=0, created_at=now, updated_at=now
        )

    def list_projects(self) -> list[ProjectSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.id, p.name, p.created_at, p.updated_at, "
                "COUNT(m.id) AS member_count "
                "FROM projects p "
                "LEFT JOIN project_members m ON m.project_id = p.id "
                "GROUP BY p.id ORDER BY p.updated_at DESC"
            ).fetchall()
        return [
            ProjectSummary(
                id=r["id"],
                name=r["name"],
                member_count=r["member_count"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_project(self, project_id: str) -> ProjectSummary | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT p.id, p.name, p.created_at, p.updated_at, "
                "COUNT(m.id) AS member_count "
                "FROM projects p "
                "LEFT JOIN project_members m ON m.project_id = p.id "
                "WHERE p.id = ? GROUP BY p.id",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectSummary(
            id=row["id"],
            name=row["name"],
            member_count=row["member_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def rename_project(self, project_id: str, name: str) -> ProjectSummary | None:
        name = name.strip()
        if not name or len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"Project name must be 1-{_MAX_NAME_LENGTH} characters after trimming."
            )
        now = _now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE projects SET name=?, updated_at=? WHERE id=?",
                    (name, now, project_id),
                )
                if cur.rowcount == 0:
                    self._conn.execute("ROLLBACK")
                    return None
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "DELETE FROM projects WHERE id=?", (project_id,)
                )
                if cur.rowcount == 0:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return True

    def touch_project(self, project_id: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id)
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Asset registration
    # ------------------------------------------------------------------

    def upsert_asset(
        self,
        *,
        normalized_path: str,
        file_name: str,
        extension: str,
        file_size: int,
        mtime_ns: int,
        file_identity: str | None = None,
        shot_time: str | None = None,
        width: int | None = None,
        height: int | None = None,
        metadata_status: str = "pending",
    ) -> str:
        """Insert or update an asset record. Returns the asset id."""
        if metadata_status not in VALID_METADATA_STATUSES:
            raise ValueError("Invalid metadata_status")
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM assets WHERE normalized_path=?",
                (normalized_path,),
            ).fetchone()
            if existing is not None:
                changed = _source_version_changed(
                    existing,
                    file_size=file_size,
                    mtime_ns=mtime_ns,
                    file_identity=file_identity,
                )
                if changed:
                    self._conn.execute(
                        "UPDATE assets SET file_name=?, extension=?, file_size=?, "
                        "mtime_ns=?, file_identity=?, shot_time=?, width=?, height=?, "
                        "metadata_status=? WHERE id=?",
                        (
                            file_name,
                            extension,
                            file_size,
                            mtime_ns,
                            file_identity,
                            shot_time,
                            width,
                            height,
                            metadata_status,
                            existing["id"],
                        ),
                    )
                    self._conn.execute(
                        "UPDATE project_members SET exported_at=NULL WHERE asset_id=?",
                        (existing["id"],),
                    )
                else:
                    self._conn.execute(
                        "UPDATE assets SET file_name=?, extension=?, "
                        "file_identity=COALESCE(file_identity, ?) WHERE id=?",
                        (file_name, extension, file_identity, existing["id"]),
                    )
                self._conn.commit()
                return existing["id"]
            aid = _generate_id()
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO assets(id, normalized_path, file_name, extension, "
                "file_size, mtime_ns, file_identity, shot_time, width, height, "
                "metadata_status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    normalized_path,
                    file_name,
                    extension,
                    file_size,
                    mtime_ns,
                    file_identity,
                    shot_time,
                    width,
                    height,
                    metadata_status,
                    now,
                ),
            )
            self._conn.commit()
            return aid

    def register_assets_batch(
        self,
        project_id: str,
        registrations: Sequence[AssetRegistration],
    ) -> tuple[int, list[AssetRecord]]:
        """Register source versions and project members in one bounded transaction."""
        if not registrations:
            return 0, []
        now = _now_iso()
        added = 0
        records: list[AssetRecord] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                project = self._conn.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if project is None:
                    raise ValueError("Project not found")
                max_order_row = self._conn.execute(
                    "SELECT COALESCE(MAX(import_order), 0) "
                    "FROM project_members WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                next_order = int(max_order_row[0]) + 1

                for registration in registrations:
                    row = self._conn.execute(
                        "SELECT * FROM assets WHERE normalized_path=?",
                        (registration.normalized_path,),
                    ).fetchone()
                    if row is None:
                        asset_id = _generate_id()
                        self._conn.execute(
                            "INSERT INTO assets(id, normalized_path, file_name, "
                            "extension, file_size, mtime_ns, file_identity, "
                            "shot_time, width, height, metadata_status, created_at) "
                            "VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,'pending',?)",
                            (
                                asset_id,
                                registration.normalized_path,
                                registration.file_name,
                                registration.extension,
                                registration.file_size,
                                registration.mtime_ns,
                                registration.file_identity,
                                now,
                            ),
                        )
                    else:
                        asset_id = str(row["id"])
                        if _source_version_changed(
                            row,
                            file_size=registration.file_size,
                            mtime_ns=registration.mtime_ns,
                            file_identity=registration.file_identity,
                        ):
                            self._conn.execute(
                                "UPDATE assets SET file_name=?, extension=?, "
                                "file_size=?, mtime_ns=?, file_identity=?, "
                                "shot_time=NULL, width=NULL, height=NULL, "
                                "metadata_status='pending' WHERE id=?",
                                (
                                    registration.file_name,
                                    registration.extension,
                                    registration.file_size,
                                    registration.mtime_ns,
                                    registration.file_identity,
                                    asset_id,
                                ),
                            )
                            self._conn.execute(
                                "UPDATE project_members SET exported_at=NULL "
                                "WHERE asset_id=?",
                                (asset_id,),
                            )
                        else:
                            self._conn.execute(
                                "UPDATE assets SET file_name=?, extension=?, "
                                "file_identity=COALESCE(file_identity, ?) WHERE id=?",
                                (
                                    registration.file_name,
                                    registration.extension,
                                    registration.file_identity,
                                    asset_id,
                                ),
                            )

                    member_id = _generate_id()
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO project_members"
                        "(id, project_id, asset_id, import_order, star_rating, "
                        "color_label, creative_look, exported_at, created_at) "
                        "VALUES(?,?,?,?,0,'none','as_shot',NULL,?)",
                        (member_id, project_id, asset_id, next_order, now),
                    )
                    if cur.rowcount > 0:
                        next_order += 1
                        added += 1
                    current = self._conn.execute(
                        "SELECT * FROM assets WHERE id=?", (asset_id,)
                    ).fetchone()
                    records.append(_row_to_asset(current))

                if added:
                    self._conn.execute(
                        "UPDATE projects SET updated_at=? WHERE id=?",
                        (now, project_id),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return added, records

    def list_assets_needing_metadata(self, *, limit: int = 32) -> list[AssetRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE metadata_status='pending' "
                "ORDER BY created_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_asset(row) for row in rows]

    def claim_asset_metadata(self, asset: AssetRecord) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assets SET metadata_status='processing' "
                "WHERE id=? AND file_size=? AND mtime_ns=? "
                "AND ((file_identity IS NULL AND ? IS NULL) OR file_identity=?) "
                "AND metadata_status='pending'",
                (
                    asset.id,
                    asset.file_size,
                    asset.mtime_ns,
                    asset.file_identity,
                    asset.file_identity,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_asset_metadata(
        self,
        asset: AssetRecord,
        *,
        shot_time: str | None,
        width: int | None,
        height: int | None,
        metadata_status: str,
    ) -> bool:
        if metadata_status not in {"ready", "failed"}:
            raise ValueError("metadata_status must be ready or failed")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assets SET shot_time=?, width=?, height=?, metadata_status=? "
                "WHERE id=? AND file_size=? AND mtime_ns=? "
                "AND ((file_identity IS NULL AND ? IS NULL) OR file_identity=?) "
                "AND metadata_status='processing'",
                (
                    shot_time,
                    width,
                    height,
                    metadata_status,
                    asset.id,
                    asset.file_size,
                    asset.mtime_ns,
                    asset.file_identity,
                    asset.file_identity,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def refresh_asset_source_version(
        self,
        asset: AssetRecord,
        *,
        file_size: int,
        mtime_ns: int,
        file_identity: str | None,
    ) -> bool:
        """Reset metadata only if *asset* is still the currently stored version."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assets SET file_size=?, mtime_ns=?, file_identity=?, "
                "shot_time=NULL, width=NULL, height=NULL, metadata_status='pending' "
                "WHERE id=? AND file_size=? AND mtime_ns=? "
                "AND ((file_identity IS NULL AND ? IS NULL) OR file_identity=?)",
                (
                    file_size,
                    mtime_ns,
                    file_identity,
                    asset.id,
                    asset.file_size,
                    asset.mtime_ns,
                    asset.file_identity,
                    asset.file_identity,
                ),
            )
            if cur.rowcount > 0:
                self._conn.execute(
                    "UPDATE project_members SET exported_at=NULL WHERE asset_id=?",
                    (asset.id,),
                )
            self._conn.commit()
            return cur.rowcount > 0

    def reset_processing_metadata(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assets SET metadata_status='pending' "
                "WHERE metadata_status='processing'"
            )
            self._conn.commit()
            return cur.rowcount

    def ensure_arw_metadata_version(self, version: str) -> int:
        """Reset stored ARW dimensions once when extraction semantics change."""

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='arw_metadata_version'"
            ).fetchone()
            if row is not None and row["value"] == version:
                return 0
            cur = self._conn.execute(
                "UPDATE assets SET shot_time=NULL, width=NULL, height=NULL, "
                "metadata_status='pending' WHERE extension='.arw'"
            )
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('arw_metadata_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (version,),
            )
            self._conn.commit()
            return cur.rowcount

    def count_unfinished_metadata(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM assets "
                "WHERE metadata_status IN ('pending', 'processing')"
            ).fetchone()
        return int(row[0])

    def get_asset_by_path(self, normalized_path: str) -> AssetRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE normalized_path=?",
                (normalized_path,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_asset(row)

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_asset(row)

    # ------------------------------------------------------------------
    # Project membership
    # ------------------------------------------------------------------

    def add_members_batch(self, project_id: str, assets: Sequence[AssetRecord]) -> int:
        """Add assets to a project, idempotently skipping existing members.

        Returns the number of newly added members.
        """
        if not assets:
            return 0
        added = 0
        now = _now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                max_order_row = self._conn.execute(
                    "SELECT COALESCE(MAX(import_order), 0) "
                    "FROM project_members WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                next_order = int(max_order_row[0]) + 1

                for asset in assets:
                    existing = self._conn.execute(
                        "SELECT id FROM project_members "
                        "WHERE project_id=? AND asset_id=?",
                        (project_id, asset.id),
                    ).fetchone()
                    if existing is not None:
                        continue
                    mid = _generate_id()
                    self._conn.execute(
                        "INSERT INTO project_members"
                        "(id, project_id, asset_id, import_order, "
                        "star_rating, color_label, creative_look, created_at) "
                        "VALUES(?,?,?,?,0,'none','as_shot',?)",
                        (mid, project_id, asset.id, next_order, now),
                    )
                    next_order += 1
                    added += 1

                self._conn.execute(
                    "UPDATE projects SET updated_at=? WHERE id=?",
                    (now, project_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return added

    def get_member(self, member_id: str) -> MemberRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT m.*, a.normalized_path, a.file_name, a.extension, "
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time, "
                "a.width, a.height, a.metadata_status "
                "FROM project_members m "
                "JOIN assets a ON a.id = m.asset_id "
                "WHERE m.id = ?",
                (member_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_member(row)

    def get_member_for_project(
        self, project_id: str, member_id: str
    ) -> MemberRecord | None:
        member = self.get_member(member_id)
        if member is None or member.project_id != project_id:
            return None
        return member

    def list_members(
        self,
        project_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[MemberRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.*, a.normalized_path, a.file_name, a.extension, "
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time, "
                "a.width, a.height, a.metadata_status "
                "FROM project_members m "
                "JOIN assets a ON a.id = m.asset_id "
                "WHERE m.project_id = ? "
                "ORDER BY m.import_order "
                "LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
        return [_row_to_member(r) for r in rows]

    def count_members(self, project_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM project_members WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return int(row[0])

    def update_rating(
        self,
        member_id: str,
        star_rating: int,
        color_label: str,
    ) -> bool:
        if (
            isinstance(star_rating, bool)
            or not isinstance(star_rating, int)
            or star_rating not in VALID_STAR_RATINGS
        ):
            raise ValueError(f"star_rating must be one of {sorted(VALID_STAR_RATINGS)}")
        if not isinstance(color_label, str) or color_label not in VALID_COLOR_LABELS:
            raise ValueError(f"color_label must be one of {sorted(VALID_COLOR_LABELS)}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE project_members SET star_rating=?, color_label=? WHERE id=?",
                (star_rating, color_label, member_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_creative_look(self, member_id: str, creative_look: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE project_members SET creative_look=? WHERE id=?",
                (creative_look, member_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def reset_unavailable_creative_looks(self) -> int:
        """Reset historical uncalibrated presets to the camera-rendered look."""

        with self._lock:
            cur = self._conn.execute(
                "UPDATE project_members SET creative_look='as_shot' "
                "WHERE creative_look<>'as_shot'"
            )
            self._conn.commit()
            return cur.rowcount

    def mark_member_exported(
        self,
        project_id: str,
        member_id: str,
        *,
        asset_id: str,
        file_size: int,
        mtime_ns: int,
        file_identity: str | None,
    ) -> bool:
        exported_at = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE project_members SET exported_at=? "
                "WHERE id=? AND project_id=? AND asset_id=? "
                "AND EXISTS (SELECT 1 FROM assets a "
                "WHERE a.id=project_members.asset_id "
                "AND a.file_size=? AND a.mtime_ns=? "
                "AND (? IS NULL OR a.file_identity IS NULL "
                "OR a.file_identity=?))",
                (
                    exported_at,
                    member_id,
                    project_id,
                    asset_id,
                    file_size,
                    mtime_ns,
                    file_identity,
                    file_identity,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_asset_identity_if_missing(
        self,
        asset_id: str,
        *,
        file_size: int,
        mtime_ns: int,
        file_identity: str,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE assets SET file_identity=? WHERE id=? AND file_size=? "
                "AND mtime_ns=? AND file_identity IS NULL",
                (file_identity, asset_id, file_size, mtime_ns),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_members(self, member_ids: Sequence[str]) -> int:
        if not member_ids:
            return 0
        placeholders = ",".join("?" for _ in member_ids)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM project_members WHERE id IN ({placeholders})",
                member_ids,
            )
            self._conn.commit()
            return cur.rowcount

    def remove_members_by_asset_path(self, normalized_path: str) -> list[str]:
        """Remove all project members referencing the given asset path.

        Returns the list of affected project IDs.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT m.project_id "
                    "FROM project_members m "
                    "JOIN assets a ON a.id = m.asset_id "
                    "WHERE a.normalized_path = ?",
                    (normalized_path,),
                ).fetchall()
                project_ids = [r[0] for r in rows]
                self._conn.execute(
                    "DELETE FROM project_members "
                    "WHERE asset_id IN (SELECT id FROM assets WHERE normalized_path=?)",
                    (normalized_path,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return project_ids

    def delete_asset_by_path(self, normalized_path: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM assets WHERE normalized_path=?", (normalized_path,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_members_by_asset_path(self, normalized_path: str) -> list[MemberRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.*, a.normalized_path, a.file_name, a.extension, "
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time, "
                "a.width, a.height, a.metadata_status "
                "FROM project_members m "
                "JOIN assets a ON a.id = m.asset_id "
                "WHERE a.normalized_path = ? "
                "ORDER BY m.import_order",
                (normalized_path,),
            ).fetchall()
        return [_row_to_member(r) for r in rows]

    # ------------------------------------------------------------------
    # Workspace state
    # ------------------------------------------------------------------

    def get_workspace_state(self, project_id: str) -> WorkspaceState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspace_state WHERE project_id=?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceState(
            last_member_id=row["last_member_id"],
            filter_star_mode=row["filter_star_mode"],
            filter_star_value=row["filter_star_value"],
            filter_color_labels=row["filter_color_labels"],
            filter_filename=row["filter_filename"],
            filter_rated=row["filter_rated"],
            filter_exported=row["filter_exported"],
            filter_formats=row["filter_formats"],
            filter_orientations=row["filter_orientations"],
            sort_field=row["sort_field"],
            sort_direction=row["sort_direction"],
            filmstrip_scroll=row["filmstrip_scroll"],
        )

    def save_workspace_state(
        self,
        project_id: str,
        *,
        last_member_id: str | None,
        filter_star_mode: str = "none",
        filter_star_value: int = 0,
        filter_color_labels: str = "",
        filter_filename: str = "",
        filter_rated: str = "all",
        filter_exported: str = "all",
        filter_formats: str = "",
        filter_orientations: str = "",
        sort_field: str = "filename",
        sort_direction: str = "asc",
        filmstrip_scroll: float = 0.0,
    ) -> None:
        if filter_star_mode not in VALID_FILTER_STAR_MODES:
            raise ValueError("Invalid filter_star_mode")
        if (
            isinstance(filter_star_value, bool)
            or not isinstance(filter_star_value, int)
            or filter_star_value not in VALID_STAR_RATINGS
        ):
            raise ValueError("Invalid filter_star_value")
        if filter_rated not in VALID_FILTER_RATED:
            raise ValueError("Invalid filter_rated")
        if filter_exported not in VALID_FILTER_EXPORTED:
            raise ValueError("Invalid filter_exported")
        _validate_csv_values(
            filter_color_labels,
            VALID_COLOR_LABELS,
            "filter_color_labels",
        )
        if not isinstance(filter_filename, str):
            raise ValueError("filter_filename must be a string")
        _validate_csv_values(filter_formats, VALID_FILTER_FORMATS, "filter_formats")
        _validate_csv_values(
            filter_orientations,
            VALID_FILTER_ORIENTATIONS,
            "filter_orientations",
        )
        if sort_field not in VALID_SORT_FIELDS:
            raise ValueError("Invalid sort_field")
        if sort_direction not in VALID_SORT_DIRECTIONS:
            raise ValueError("Invalid sort_direction")
        if (
            isinstance(filmstrip_scroll, bool)
            or not isinstance(filmstrip_scroll, (int, float))
            or not math.isfinite(float(filmstrip_scroll))
            or float(filmstrip_scroll) < 0
        ):
            raise ValueError("Invalid filmstrip_scroll")
        if filter_star_mode in {"exact", "at_least"} and filter_star_value == 0:
            raise ValueError("exact and at_least require a 1-5 filter_star_value")
        now = _now_iso()
        with self._lock:
            project = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise ValueError("Project not found")
            if last_member_id is not None:
                member = self._conn.execute(
                    "SELECT 1 FROM project_members WHERE id=? AND project_id=?",
                    (last_member_id, project_id),
                ).fetchone()
                if member is None:
                    raise ValueError("last_member_id does not belong to project")
            self._conn.execute(
                "INSERT INTO workspace_state"
                "(project_id, last_member_id, filter_star_mode, "
                "filter_star_value, filter_color_labels, filter_filename, "
                "filter_rated, filter_exported, filter_formats, "
                "filter_orientations, sort_field, sort_direction, "
                "filmstrip_scroll, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "last_member_id=excluded.last_member_id, "
                "filter_star_mode=excluded.filter_star_mode, "
                "filter_star_value=excluded.filter_star_value, "
                "filter_color_labels=excluded.filter_color_labels, "
                "filter_filename=excluded.filter_filename, "
                "filter_rated=excluded.filter_rated, "
                "filter_exported=excluded.filter_exported, "
                "filter_formats=excluded.filter_formats, "
                "filter_orientations=excluded.filter_orientations, "
                "sort_field=excluded.sort_field, "
                "sort_direction=excluded.sort_direction, "
                "filmstrip_scroll=excluded.filmstrip_scroll, "
                "updated_at=excluded.updated_at",
                (
                    project_id,
                    last_member_id,
                    filter_star_mode,
                    filter_star_value,
                    filter_color_labels,
                    filter_filename,
                    filter_rated,
                    filter_exported,
                    filter_formats,
                    filter_orientations,
                    sort_field,
                    sort_direction,
                    filmstrip_scroll,
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (now, project_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Operation log (two-phase delete / export)
    # ------------------------------------------------------------------

    def create_operation_log(
        self,
        operation_type: str,
        items: Sequence[OperationTarget],
        *,
        payload_json: str = "",
    ) -> str:
        """Create an operation log with item entries.

        Args:
            operation_type: 'import', 'permanent_delete' or 'export'.
            items: exact source-version snapshots confirmed by the user.
            payload_json: serialized confirmation metadata.
        """
        if operation_type not in VALID_OPERATION_TYPES:
            raise ValueError("Invalid operation_type")
        log_id = _generate_id()
        now = _now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO operation_log"
                    "(id, operation_type, payload_json, status, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (log_id, operation_type, payload_json, "pending", now),
                )
                for seq, item in enumerate(items):
                    self._conn.execute(
                        "INSERT INTO operation_log_items"
                        "(id, log_id, asset_id, normalized_path, "
                        "source_file_size, source_mtime_ns, "
                        "source_file_identity, source_extension, result, seq) "
                        "VALUES(?,?,?,?,?,?,?,?,'pending',?)",
                        (
                            _generate_id(),
                            log_id,
                            item.asset_id,
                            item.normalized_path,
                            item.source_file_size,
                            item.source_mtime_ns,
                            item.source_file_identity,
                            item.source_extension,
                            seq,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return log_id

    def set_operation_log_status(self, log_id: str, status: str) -> None:
        if status not in VALID_OPERATION_STATUSES:
            raise ValueError("Invalid operation status")
        now = _now_iso() if status in ("completed", "failed", "cancelled") else None
        with self._lock:
            self._conn.execute(
                "UPDATE operation_log SET status=?, completed_at=? WHERE id=?",
                (status, now, log_id),
            )
            self._conn.commit()

    def update_operation_item(
        self,
        item_id: str,
        result: str,
        error_message: str | None = None,
    ) -> None:
        if result not in VALID_OP_ITEM_RESULTS:
            raise ValueError("Invalid operation item result")
        with self._lock:
            self._conn.execute(
                "UPDATE operation_log_items SET result=?, error_message=? WHERE id=?",
                (result, error_message, item_id),
            )
            self._conn.commit()

    def get_pending_operation_logs(self) -> list[OperationLogEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM operation_log "
                "WHERE status IN ('pending', 'in_progress') "
                "ORDER BY created_at"
            ).fetchall()
        return [_row_to_oplog(r) for r in rows]

    def get_operation_log_items(self, log_id: str) -> list[OperationLogItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM operation_log_items WHERE log_id=? ORDER BY seq",
                (log_id,),
            ).fetchall()
        return [_row_to_opitem(r) for r in rows]

    def get_operation_log_entry(self, log_id: str) -> OperationLogEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operation_log WHERE id=?", (log_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_oplog(row)


# ----------------------------------------------------------------------
# Row mappers
# ----------------------------------------------------------------------


def _row_to_asset(row: sqlite3.Row) -> AssetRecord:
    return AssetRecord(
        id=row["id"],
        normalized_path=row["normalized_path"],
        file_name=row["file_name"],
        extension=row["extension"],
        file_size=row["file_size"],
        mtime_ns=row["mtime_ns"],
        file_identity=row["file_identity"],
        shot_time=row["shot_time"],
        width=row["width"],
        height=row["height"],
        metadata_status=row["metadata_status"],
    )


def _row_to_member(row: sqlite3.Row) -> MemberRecord:
    return MemberRecord(
        id=row["id"],
        project_id=row["project_id"],
        asset_id=row["asset_id"],
        import_order=row["import_order"],
        star_rating=row["star_rating"],
        color_label=row["color_label"],
        creative_look=row["creative_look"],
        file_name=row["file_name"],
        normalized_path=row["normalized_path"],
        extension=row["extension"],
        file_size=row["file_size"],
        mtime_ns=row["mtime_ns"],
        file_identity=row["file_identity"],
        shot_time=row["shot_time"],
        width=row["width"],
        height=row["height"],
        metadata_status=row["metadata_status"],
        exported_at=row["exported_at"],
    )


def _row_to_oplog(row: sqlite3.Row) -> OperationLogEntry:
    return OperationLogEntry(
        id=row["id"],
        operation_type=row["operation_type"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _row_to_opitem(row: sqlite3.Row) -> OperationLogItem:
    return OperationLogItem(
        id=row["id"],
        log_id=row["log_id"],
        asset_id=row["asset_id"],
        normalized_path=row["normalized_path"],
        source_file_size=row["source_file_size"],
        source_mtime_ns=row["source_mtime_ns"],
        source_file_identity=row["source_file_identity"],
        source_extension=row["source_extension"],
        result=row["result"],
        error_message=row["error_message"],
    )


def _source_version_changed(
    row: sqlite3.Row,
    *,
    file_size: int,
    mtime_ns: int,
    file_identity: str | None,
) -> bool:
    if int(row["file_size"]) != file_size or int(row["mtime_ns"]) != mtime_ns:
        return True
    existing_identity = row["file_identity"]
    return (
        existing_identity is not None
        and file_identity is not None
        and str(existing_identity) != file_identity
    )


def _validate_csv_values(value: str, allowed: frozenset[str], field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    if len(tokens) != len(set(tokens)) or any(token not in allowed for token in tokens):
        raise ValueError(f"Invalid {field}")
