"""Independent SQLite persistence for the ARW selection module.

This database stores projects, source assets, project-member relationships,
ratings, color labels, creative-look selections, workspace state and
two-phase operation logs. It never touches the existing gallery state DB,
recommendation DB or zvec collection.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_VERSION = 1
_MAX_NAME_LENGTH = 40

VALID_STAR_RATINGS = frozenset({0, 1, 2, 3, 4, 5})
VALID_COLOR_LABELS = frozenset({"none", "red", "yellow", "green", "blue", "purple"})
VALID_SORT_FIELDS = frozenset({"filename", "import_order", "shot_time", "star_rating"})
VALID_SORT_DIRECTIONS = frozenset({"asc", "desc"})
VALID_FILTER_STAR_MODES = frozenset({"exact", "at_least", "none"})
VALID_FILTER_RATED = frozenset({"all", "rated", "unrated"})
VALID_OPERATION_TYPES = frozenset({"permanent_delete", "export"})
VALID_OPERATION_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})
VALID_OP_ITEM_RESULTS = frozenset({"pending", "deleted", "already_missing", "failed"})


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


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    last_member_id: str | None
    filter_star_mode: str
    filter_star_value: int
    filter_color_labels: str
    filter_filename: str
    filter_rated: str
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
    result: str
    error_message: str | None


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
            self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _create_schema(self) -> None:
        conn = self._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                normalized_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                file_identity TEXT,
                shot_time TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_members (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                import_order INTEGER NOT NULL,
                star_rating INTEGER NOT NULL DEFAULT 0,
                color_label TEXT NOT NULL DEFAULT 'none',
                creative_look TEXT NOT NULL DEFAULT 'as_shot',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, asset_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_members_project
                ON project_members(project_id, import_order);

            CREATE TABLE IF NOT EXISTS workspace_state (
                project_id TEXT PRIMARY KEY,
                last_member_id TEXT,
                filter_star_mode TEXT NOT NULL DEFAULT 'none',
                filter_star_value INTEGER NOT NULL DEFAULT 0,
                filter_color_labels TEXT NOT NULL DEFAULT '',
                filter_filename TEXT NOT NULL DEFAULT '',
                filter_rated TEXT NOT NULL DEFAULT 'all',
                sort_field TEXT NOT NULL DEFAULT 'filename',
                sort_direction TEXT NOT NULL DEFAULT 'asc',
                filmstrip_scroll REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS operation_log (
                id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_oplog_status
                ON operation_log(status, created_at);

            CREATE TABLE IF NOT EXISTS operation_log_items (
                id TEXT PRIMARY KEY,
                log_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                seq INTEGER NOT NULL,
                FOREIGN KEY (log_id) REFERENCES operation_log(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_oplog_items_log
                ON operation_log_items(log_id, seq);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(_SCHEMA_VERSION)),
        )
        conn.commit()

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
                    "INSERT INTO workspace_state"
                    "(project_id, updated_at) VALUES(?,?)",
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
    ) -> str:
        """Insert or update an asset record. Returns the asset id."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM assets WHERE normalized_path=?",
                (normalized_path,),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE assets SET file_name=?, extension=?, file_size=?, "
                    "mtime_ns=?, file_identity=?, shot_time=? WHERE id=?",
                    (
                        file_name,
                        extension,
                        file_size,
                        mtime_ns,
                        file_identity,
                        shot_time,
                        existing["id"],
                    ),
                )
                self._conn.commit()
                return existing["id"]
            aid = _generate_id()
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO assets(id, normalized_path, file_name, extension, "
                "file_size, mtime_ns, file_identity, shot_time, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    normalized_path,
                    file_name,
                    extension,
                    file_size,
                    mtime_ns,
                    file_identity,
                    shot_time,
                    now,
                ),
            )
            self._conn.commit()
            return aid

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

    def add_members_batch(
        self, project_id: str, assets: Sequence[AssetRecord]
    ) -> int:
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
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time "
                "FROM project_members m "
                "JOIN assets a ON a.id = m.asset_id "
                "WHERE m.id = ?",
                (member_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_member(row)

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
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time "
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
        if star_rating not in VALID_STAR_RATINGS:
            raise ValueError(f"star_rating must be one of {sorted(VALID_STAR_RATINGS)}")
        if color_label not in VALID_COLOR_LABELS:
            raise ValueError(
                f"color_label must be one of {sorted(VALID_COLOR_LABELS)}"
            )
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

    def remove_members_by_asset_path(
        self, normalized_path: str
    ) -> list[str]:
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

    def get_members_by_asset_path(
        self, normalized_path: str
    ) -> list[MemberRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.*, a.normalized_path, a.file_name, a.extension, "
                "a.file_size, a.mtime_ns, a.file_identity, a.shot_time "
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
        sort_field: str = "filename",
        sort_direction: str = "asc",
        filmstrip_scroll: float = 0.0,
    ) -> None:
        if filter_star_mode not in VALID_FILTER_STAR_MODES:
            raise ValueError("Invalid filter_star_mode")
        if filter_rated not in VALID_FILTER_RATED:
            raise ValueError("Invalid filter_rated")
        if sort_field not in VALID_SORT_FIELDS:
            raise ValueError("Invalid sort_field")
        if sort_direction not in VALID_SORT_DIRECTIONS:
            raise ValueError("Invalid sort_direction")
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO workspace_state"
                "(project_id, last_member_id, filter_star_mode, "
                "filter_star_value, filter_color_labels, filter_filename, "
                "filter_rated, sort_field, sort_direction, filmstrip_scroll, "
                "updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "last_member_id=excluded.last_member_id, "
                "filter_star_mode=excluded.filter_star_mode, "
                "filter_star_value=excluded.filter_star_value, "
                "filter_color_labels=excluded.filter_color_labels, "
                "filter_filename=excluded.filter_filename, "
                "filter_rated=excluded.filter_rated, "
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
        items: Sequence[tuple[str, str]],
        *,
        payload_json: str = "",
    ) -> str:
        """Create an operation log with item entries.

        Args:
            operation_type: 'permanent_delete' or 'export'.
            items: list of (asset_id, normalized_path) tuples.
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
                for seq, (asset_id, path) in enumerate(items):
                    self._conn.execute(
                        "INSERT INTO operation_log_items"
                        "(id, log_id, asset_id, normalized_path, result, seq) "
                        "VALUES(?,?,?,?,'pending',?)",
                        (_generate_id(), log_id, asset_id, path, seq),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return log_id

    def set_operation_log_status(self, log_id: str, status: str) -> None:
        if status not in VALID_OPERATION_STATUSES:
            raise ValueError("Invalid operation status")
        now = _now_iso() if status in ("completed", "failed") else None
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
                "SELECT * FROM operation_log_items "
                "WHERE log_id=? ORDER BY seq",
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
        result=row["result"],
        error_message=row["error_message"],
    )
