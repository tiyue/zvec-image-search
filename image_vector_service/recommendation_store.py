"""Device-scoped recommendation history and shared explicit preferences."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_MAX_IDENTIFIER_LENGTH = 256
_MAX_HISTORY = 240
_MAX_PREFERENCE_QUERY = 900
_MAX_PROFILE_PREFERENCES = 256
_MAX_PROFILE_EVENTS = _MAX_PROFILE_PREFERENCES * 16


@dataclass(frozen=True, slots=True)
class RecommendationBatchItem:
    item_id: str
    position: int
    candidate_id: str
    library_id: str
    doc_id: str
    sha256: str
    slot: str


@dataclass(frozen=True, slots=True)
class RecommendationBatch:
    batch_id: str
    viewer_id: str
    request_id: str
    items: tuple[RecommendationBatchItem, ...]


@dataclass(frozen=True, slots=True)
class SharedPreference:
    sha256: str
    action: str
    library_id: str
    doc_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class RecommendationActionResult:
    recorded: bool
    preference: str | None
    preference_sequence: int | None
    preference_changed: bool


class RecommendationStore:
    """Persists opaque identifiers only; a generated batch is not an exposure."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self._connection = sqlite3.connect(
                self.path, timeout=5, check_same_thread=False
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_batch(
        self,
        viewer_id: str,
        request_id: str,
        items: Sequence[RecommendationBatchItem],
    ) -> RecommendationBatch:
        viewer = _identifier(viewer_id, "viewer_id")
        request = _identifier(request_id, "request_id")
        normalized = _items(items)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT batch_id FROM batches "
                    "WHERE viewer_id = ? AND request_id = ?",
                    (viewer, request),
                ).fetchone()
                if row is not None:
                    batch = self._batch(viewer, request, str(row[0]))
                    if batch.items != normalized:
                        raise ValueError(
                            "request_id was already created with different items"
                        )
                    connection.execute("COMMIT")
                    return batch
                batch_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO batches(batch_id, viewer_id, request_id) "
                    "VALUES (?, ?, ?)",
                    (batch_id, viewer, request),
                )
                for item in normalized:
                    connection.execute(
                        "INSERT INTO items("
                        "item_id, batch_id, position, candidate_id, library_id, "
                        "doc_id, sha256, slot"
                        ") "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.item_id,
                            batch_id,
                            item.position,
                            item.candidate_id,
                            item.library_id,
                            item.doc_id,
                            item.sha256,
                            item.slot,
                        ),
                    )
                connection.execute("COMMIT")
                return RecommendationBatch(batch_id, viewer, request, normalized)
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise ValueError("event_id was already recorded") from exc
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def mark_shown(self, viewer_id: str, batch_id: str, event_id: str) -> bool:
        """Record a successful display once and atomically increment exposure."""

        viewer = _identifier(viewer_id, "viewer_id")
        batch = _identifier(batch_id, "batch_id")
        event = _identifier(event_id, "event_id")
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT shown_event_id FROM batches "
                    "WHERE batch_id = ? AND viewer_id = ?",
                    (batch, viewer),
                ).fetchone()
                if row is None:
                    raise ValueError("batch does not belong to viewer")
                inserted = self._insert_event(
                    connection, viewer, batch, event, None, "shown"
                )
                if not inserted:
                    connection.execute("COMMIT")
                    return False
                if row[0] is None:
                    for sha256 in connection.execute(
                        "SELECT DISTINCT sha256 FROM items WHERE batch_id = ?", (batch,)
                    ):
                        connection.execute(
                            "INSERT INTO content_stats(viewer_id, sha256, shown_count) "
                            "VALUES (?, ?, 1) ON CONFLICT(viewer_id, sha256) "
                            "DO UPDATE SET shown_count = shown_count + 1",
                            (viewer, str(sha256[0])),
                        )
                    connection.execute(
                        "UPDATE batches SET shown_event_id = ? WHERE batch_id = ?",
                        (event, batch),
                    )
                    connection.execute("COMMIT")
                    return True
                connection.execute("COMMIT")
                return False
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def record_action(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
    ) -> bool:
        """Record a non-display action once for one batch item."""

        normalized_action = _identifier(action, "action")
        if normalized_action == "shown":
            raise ValueError("shown must be recorded through mark_shown")
        result = self._record_event(
            viewer_id,
            batch_id,
            event_id,
            item_id,
            normalized_action,
            include_preference=False,
        )
        return result.recorded

    def record_action_with_preference(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
    ) -> tuple[bool, str | None]:
        """Record an action and atomically return the current shared preference."""

        result = self.record_action_with_preference_details(
            viewer_id,
            batch_id,
            event_id,
            item_id,
            action,
        )
        return result.recorded, result.preference

    def record_action_with_preference_details(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
    ) -> RecommendationActionResult:
        """Record an action and return its exact shared-preference effect."""

        normalized_action = _identifier(action, "action")
        if normalized_action == "shown":
            raise ValueError("shown must be recorded through mark_shown")
        return self._record_event(
            viewer_id,
            batch_id,
            event_id,
            item_id,
            normalized_action,
            include_preference=True,
        )

    def recent_sha256(
        self, viewer_id: str, *, limit: int = _MAX_HISTORY
    ) -> tuple[str, ...]:
        viewer = _identifier(viewer_id, "viewer_id")
        _history_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                "SELECT items.sha256 FROM batches "
                "JOIN events ON events.event_id = batches.shown_event_id "
                "JOIN items ON items.batch_id = batches.batch_id "
                "WHERE batches.viewer_id = ? GROUP BY items.sha256 "
                "ORDER BY MAX(events.sequence) DESC LIMIT ?",
                (viewer, limit),
            )
            return tuple(str(row[0]) for row in rows)

    def exposure_counts(self, viewer_id: str, shas: Iterable[str]) -> dict[str, int]:
        viewer = _identifier(viewer_id, "viewer_id")
        normalized = tuple(
            dict.fromkeys(_identifier(value, "sha256") for value in shas)
        )
        if len(normalized) > 900:
            raise ValueError("at most 900 SHA values are supported")
        result = {value: 0 for value in normalized}
        if not normalized:
            return result
        placeholders = ", ".join("?" for _ in normalized)
        with self._lock:
            rows = self._connection.execute(
                "SELECT sha256, shown_count FROM content_stats WHERE viewer_id = ? "
                f"AND sha256 IN ({placeholders})",
                (viewer, *normalized),
            )
            result.update({str(row[0]): int(row[1]) for row in rows})
        return result

    def final_preferences(self, shas: Iterable[str]) -> dict[str, SharedPreference]:
        """Return the latest explicit preference per SHA across all viewers."""

        normalized = tuple(
            dict.fromkeys(_identifier(value, "sha256") for value in shas)
        )
        if len(normalized) > _MAX_PREFERENCE_QUERY:
            raise ValueError("at most 900 SHA values are supported")
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        with self._lock:
            rows = self._connection.execute(
                "WITH ranked AS ("
                "SELECT items.sha256, events.action, items.library_id, "
                "items.doc_id, events.sequence, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY items.sha256 ORDER BY events.sequence DESC"
                ") AS preference_rank "
                "FROM items JOIN events ON events.item_id = items.item_id "
                f"WHERE items.sha256 IN ({placeholders}) "
                "AND events.action IN ('like', 'dislike') "
                ") "
                "SELECT sha256, action, library_id, doc_id, sequence "
                "FROM ranked WHERE preference_rank = 1",
                normalized,
            )
            return {
                str(row[0]): SharedPreference(
                    sha256=sha256,
                    action=str(row[1]),
                    library_id=str(row[2]),
                    doc_id=str(row[3]),
                    sequence=int(row[4]),
                )
                for row in rows
                if (sha256 := str(row[0]))
            }

    def recent_final_preferences(
        self, *, limit: int = _MAX_PROFILE_PREFERENCES
    ) -> tuple[SharedPreference, ...]:
        """Load a bounded newest-first shared preference profile."""

        _preference_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                "SELECT items.sha256, events.action, items.library_id, "
                "items.doc_id, events.sequence "
                "FROM events INDEXED BY idx_events_preference_sequence "
                "JOIN items ON items.item_id = events.item_id "
                "WHERE events.action IN ('like', 'dislike') "
                "ORDER BY events.sequence DESC LIMIT ?",
                (_MAX_PROFILE_EVENTS,),
            )
            preferences: list[SharedPreference] = []
            seen_sha256: set[str] = set()
            for row in rows:
                sha256 = str(row[0])
                if not sha256 or sha256 in seen_sha256:
                    continue
                seen_sha256.add(sha256)
                preferences.append(
                    SharedPreference(
                        sha256=sha256,
                        action=str(row[1]),
                        library_id=str(row[2]),
                        doc_id=str(row[3]),
                        sequence=int(row[4]),
                    )
                )
                if len(preferences) == limit:
                    break
            return tuple(preferences)

    def batch_for_request(
        self, viewer_id: str, request_id: str
    ) -> RecommendationBatch | None:
        viewer = _identifier(viewer_id, "viewer_id")
        request = _identifier(request_id, "request_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT batch_id FROM batches WHERE viewer_id = ? AND request_id = ?",
                (viewer, request),
            ).fetchone()
            return None if row is None else self._batch(viewer, request, str(row[0]))

    def journal_mode(self) -> str:
        with self._lock:
            return str(
                self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()

    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def schema_columns(self) -> dict[str, tuple[str, ...]]:
        with self._lock:
            return {
                table: tuple(
                    str(row[1])
                    for row in self._connection.execute(f"PRAGMA table_info({table})")
                )
                for table in ("batches", "items", "events", "content_stats")
            }

    def _record_event(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        *,
        include_preference: bool,
    ) -> RecommendationActionResult:
        viewer = _identifier(viewer_id, "viewer_id")
        batch = _identifier(batch_id, "batch_id")
        event = _identifier(event_id, "event_id")
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT items.sha256 FROM items "
                    "JOIN batches ON batches.batch_id = items.batch_id "
                    "WHERE batches.viewer_id = ? AND items.batch_id = ? "
                    "AND items.item_id = ?",
                    (viewer, batch, _identifier(item_id, "item_id")),
                ).fetchone()
                if row is None:
                    raise ValueError("batch item does not belong to viewer")
                sha256 = str(row[0])
                previous_preference = (
                    self._latest_preference(connection, sha256)
                    if include_preference and action in {"like", "dislike"}
                    else None
                )
                inserted = self._insert_event(
                    connection, viewer, batch, event, item_id, action
                )
                if not include_preference:
                    connection.execute("COMMIT")
                    return RecommendationActionResult(inserted, None, None, False)
                preference = self._latest_preference(connection, sha256)
                connection.execute("COMMIT")
                return RecommendationActionResult(
                    recorded=inserted,
                    preference=None if preference is None else preference[0],
                    preference_sequence=None if preference is None else preference[1],
                    preference_changed=(
                        inserted
                        and action in {"like", "dislike"}
                        and preference is not None
                        and (
                            previous_preference is None
                            or previous_preference[0] != preference[0]
                        )
                    ),
                )
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _latest_preference(
        connection: sqlite3.Connection, sha256: str
    ) -> tuple[str, int] | None:
        row = connection.execute(
            "SELECT events.action, events.sequence FROM items "
            "JOIN events ON events.item_id = items.item_id "
            "WHERE items.sha256 = ? "
            "AND events.action IN ('like', 'dislike') "
            "ORDER BY events.sequence DESC LIMIT 1",
            (sha256,),
        ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]))

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str | None,
        action: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT viewer_id, batch_id, item_id, action FROM events "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == (viewer_id, batch_id, item_id, action):
                return False
            raise ValueError("event_id was already recorded with different content")
        connection.execute(
            "INSERT INTO events(event_id, viewer_id, batch_id, item_id, action) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, viewer_id, batch_id, item_id, action),
        )
        return True

    def _batch(
        self, viewer_id: str, request_id: str, batch_id: str
    ) -> RecommendationBatch:
        with self._lock:
            rows = self._connection.execute(
                "SELECT item_id, position, candidate_id, library_id, doc_id, "
                "sha256, slot "
                "FROM items "
                "WHERE batch_id = ? ORDER BY position",
                (batch_id,),
            )
            return RecommendationBatch(
                batch_id,
                viewer_id,
                request_id,
                tuple(
                    RecommendationBatchItem(
                        str(row[0]),
                        int(row[1]),
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[5]),
                        str(row[6]),
                    )
                    for row in rows
                ),
            )

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                viewer_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                shown_event_id TEXT,
                UNIQUE(viewer_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,
                library_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                slot TEXT NOT NULL,
                UNIQUE(batch_id, position),
                UNIQUE(batch_id, candidate_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                viewer_id TEXT NOT NULL,
                batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                item_id TEXT REFERENCES items(item_id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(
                    (action = 'shown' AND item_id IS NULL)
                    OR (action != 'shown' AND item_id IS NOT NULL)
                )
            );
            CREATE TABLE IF NOT EXISTS content_stats (
                viewer_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                shown_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(viewer_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_events_viewer_action_sequence
                ON events(viewer_id, action, sequence DESC);
            CREATE INDEX IF NOT EXISTS idx_items_sha256
                ON items(sha256);
            CREATE INDEX IF NOT EXISTS idx_events_item_action_sequence
                ON events(item_id, action, sequence DESC);
            CREATE INDEX IF NOT EXISTS idx_events_preference_sequence
                ON events(sequence DESC, item_id, action)
                WHERE action IN ('like', 'dislike');
                """
            )


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_IDENTIFIER_LENGTH
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _history_limit(limit: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_HISTORY
    ):
        raise ValueError("limit must be an integer between 1 and 240")


def _preference_limit(limit: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_PROFILE_PREFERENCES
    ):
        raise ValueError("limit must be an integer between 1 and 256")


def _items(
    values: Sequence[RecommendationBatchItem],
) -> tuple[RecommendationBatchItem, ...]:
    result: list[RecommendationBatchItem] = []
    item_ids: set[str] = set()
    candidate_ids: set[str] = set()
    for position, item in enumerate(values):
        if not isinstance(item, RecommendationBatchItem):
            raise TypeError("items must contain RecommendationBatchItem values")
        if item.position != position:
            raise ValueError("item positions must be contiguous and start at zero")
        normalized = RecommendationBatchItem(
            _identifier(item.item_id, "item_id"),
            item.position,
            _identifier(item.candidate_id, "candidate_id"),
            _identifier(item.library_id, "library_id"),
            _identifier(item.doc_id, "doc_id"),
            _identifier(item.sha256, "sha256"),
            _identifier(item.slot, "slot"),
        )
        if normalized.item_id in item_ids or normalized.candidate_id in candidate_ids:
            raise ValueError("items must not repeat item_id or candidate_id")
        item_ids.add(normalized.item_id)
        candidate_ids.add(normalized.candidate_id)
        result.append(normalized)
    return tuple(result)
