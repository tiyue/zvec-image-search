"""Private, local persistence for search-learning feedback.

This database is intentionally separate from ``activity.sqlite3``.  It stores
only stable identifiers and bounded numeric ranking features: never image
bytes, absolute paths, credentials, model prompts, or query text unless the
user explicitly opts in.  Search integration must use :meth:`try_record_search`
so an unavailable or full database can never make a normal search fail.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal

JsonObject = dict[str, Any]
FeedbackAction = Literal[
    "relevant",
    "not_relevant",
    "export",
    "copy",
    "open",
    "detail",
]

SEARCH_LEARNING_DATABASE_NAME: Final = "search-learning.sqlite3"
SEARCH_LEARNING_SCHEMA_VERSION: Final = 1
EXPLICIT_ACTIONS: Final = frozenset({"relevant", "not_relevant"})
IMPLICIT_ACTIONS: Final = frozenset({"export", "copy", "open", "detail"})
FEEDBACK_WEIGHTS: Final[dict[str, float]] = {
    "relevant": 1.0,
    "not_relevant": -1.0,
    "export": 0.8,
    "copy": 0.6,
    "open": 0.3,
    "detail": 0.1,
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|token|prompt|"
    r"response|completion|image[_-]?data|base64|binary|path|directory|root)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ABSOLUTE_PATH_FRAGMENT = re.compile(
    r"(?:^|[\s\"'=({\[])(?:[A-Za-z]:[\\/]|/(?!/)[^\s\r\n]+)"
)
_KNOWN_SECRET_ENVIRONMENT_VARIABLES: Final = (
    "DASHSCOPE_API_KEY",
    "ZVEC_BACKEND_SESSION_TOKEN",
    "ZVEC_SESSION_TOKEN",
    "OPENAI_API_KEY",
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS search_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    query_type TEXT NOT NULL,
    requested_count INTEGER NOT NULL CHECK (requested_count >= 0),
    returned_count INTEGER NOT NULL CHECK (returned_count >= 0),
    library_ids_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    ranking_model_version TEXT,
    calibration_version TEXT,
    query_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_sessions_time
    ON search_sessions(created_at DESC, session_id DESC);
CREATE INDEX IF NOT EXISTS idx_search_sessions_type_time
    ON search_sessions(query_type, created_at DESC, session_id DESC);

CREATE TABLE IF NOT EXISTS search_candidates (
    session_id TEXT NOT NULL REFERENCES search_sessions(session_id)
        ON DELETE CASCADE,
    library_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    sha256 TEXT,
    original_rank INTEGER NOT NULL CHECK (original_rank >= 1),
    displayed_rank INTEGER CHECK (displayed_rank IS NULL OR displayed_rank >= 1),
    feature_schema_version INTEGER NOT NULL CHECK (feature_schema_version >= 1),
    features_json TEXT NOT NULL,
    ranking_score REAL NOT NULL,
    displayed INTEGER NOT NULL CHECK (displayed IN (0, 1)),
    PRIMARY KEY (session_id, library_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_search_candidates_doc
    ON search_candidates(library_id, doc_id, session_id);
CREATE INDEX IF NOT EXISTS idx_search_candidates_session_rank
    ON search_candidates(session_id, original_rank);

CREATE TABLE IF NOT EXISTS feedback_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES search_sessions(session_id)
        ON DELETE CASCADE,
    library_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ('relevant', 'not_relevant', 'export', 'copy', 'open', 'detail')
    ),
    feedback_weight REAL NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (session_id, library_id, doc_id)
        REFERENCES search_candidates(session_id, library_id, doc_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedback_events_time
    ON feedback_events(created_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_events_session_time
    ON feedback_events(session_id, created_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_events_candidate
    ON feedback_events(session_id, library_id, doc_id, revoked_at);

CREATE TABLE IF NOT EXISTS learning_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    learning_enabled INTEGER NOT NULL CHECK (learning_enabled IN (0, 1)),
    implicit_feedback_enabled INTEGER NOT NULL CHECK (
        implicit_feedback_enabled IN (0, 1)
    ),
    save_query_text INTEGER NOT NULL CHECK (save_query_text IN (0, 1)),
    active_model_version TEXT,
    previous_model_version TEXT,
    shadow_mode INTEGER NOT NULL CHECK (shadow_mode IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    model_version TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('training', 'candidate', 'active', 'retired', 'failed')
    ),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    parent_version TEXT,
    model_file TEXT NOT NULL,
    report_file TEXT,
    explicit_samples INTEGER NOT NULL DEFAULT 0,
    positive_samples INTEGER NOT NULL DEFAULT 0,
    negative_samples INTEGER NOT NULL DEFAULT 0,
    query_sessions INTEGER NOT NULL DEFAULT 0,
    gate_status TEXT NOT NULL CHECK (
        gate_status IN ('pending', 'passed', 'failed')
    ),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_model_versions_time
    ON model_versions(created_at DESC, model_version DESC);

CREATE TABLE IF NOT EXISTS learning_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (job_type = 'train'),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'succeeded', 'failed', 'cancelled',
            'interrupted'
        )
    ),
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    model_version TEXT,
    message TEXT,
    error_code TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_learning_jobs_time
    ON learning_jobs(submitted_at DESC, job_id DESC);

INSERT OR IGNORE INTO learning_settings (
    singleton, learning_enabled, implicit_feedback_enabled, save_query_text,
    active_model_version, previous_model_version, shadow_mode, updated_at
) VALUES (1, 1, 0, 0, NULL, NULL, 1, '1970-01-01T00:00:00.000000Z');
"""


class SearchLearningStoreUnavailable(RuntimeError):
    """The optional learning database cannot currently be read or written."""


class InvalidSearchLearningCursor(ValueError):
    """A pagination cursor is malformed or belongs to another endpoint."""


class SearchLearningValidationError(ValueError):
    """A learning record violates the privacy or data contract."""


@dataclass(frozen=True, slots=True)
class SearchCandidateRecord:
    library_id: str
    doc_id: str
    original_rank: int
    displayed_rank: int | None = None
    sha256: str | None = None
    feature_schema_version: int = 1
    features: Mapping[str, Any] = field(default_factory=dict)
    ranking_score: float = 0.0
    displayed: bool = False


@dataclass(frozen=True, slots=True)
class SearchSessionRecord:
    session_id: str
    query_type: str
    requested_count: int
    returned_count: int
    library_ids: Sequence[str]
    latency_ms: int
    candidates: Sequence[SearchCandidateRecord]
    created_at: datetime | str | None = None
    ranking_model_version: str | None = None
    calibration_version: str | None = None
    query_text: str | None = None


class SearchLearningStore:
    """Thread-safe, failure-isolated store for local search feedback."""

    def __init__(
        self,
        config_home: str | Path,
        *,
        retention_days: int = 180,
        max_sessions: int = 50_000,
        max_feedback_events: int = 100_000,
        max_candidates_per_session: int = 1_000,
        sqlite_timeout_seconds: float = 1.0,
        redactions: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if max_sessions < 100:
            raise ValueError("max_sessions must be at least 100")
        if max_feedback_events < 300:
            raise ValueError("max_feedback_events must be at least 300")
        if not 1 <= max_candidates_per_session <= 10_000:
            raise ValueError("max_candidates_per_session must be between 1 and 10000")
        if sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be positive")
        self._config_home = Path(config_home).expanduser().resolve()
        self._path = self._config_home / SEARCH_LEARNING_DATABASE_NAME
        self._retention_days = retention_days
        self._max_sessions = max_sessions
        self._max_feedback_events = max_feedback_events
        self._max_candidates_per_session = max_candidates_per_session
        self._sqlite_timeout_seconds = sqlite_timeout_seconds
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        environment_redactions = tuple(
            value
            for name in _KNOWN_SECRET_ENVIRONMENT_VARIABLES
            if (value := os.getenv(name, "").strip())
        )
        self._redactions = tuple(
            dict.fromkeys(
                value
                for value in (*redactions, *environment_redactions)
                if isinstance(value, str) and value
            )
        )
        self._available = False
        self._last_error: str | None = None
        try:
            self._config_home.mkdir(parents=True, exist_ok=True)
            connection = self._open()
            try:
                connection.executescript(_SCHEMA)
                connection.execute(
                    f"PRAGMA user_version={SEARCH_LEARNING_SCHEMA_VERSION}"
                )
                self._recover_interrupted_jobs(connection)
                self._prune(connection)
                connection.commit()
            finally:
                connection.close()
            self._available = True
        except (OSError, sqlite3.Error) as exc:
            self._remember_error(exc)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def try_record_search(self, record: SearchSessionRecord) -> bool:
        """Best-effort recording entry point used by the search path."""

        try:
            self.record_search(record)
        except (SearchLearningStoreUnavailable, SearchLearningValidationError):
            return False
        return True

    def record_search(self, record: SearchSessionRecord) -> None:
        session, candidates = self._normalize_session(record)
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO search_sessions (
                    session_id, created_at, query_type, requested_count,
                    returned_count, library_ids_json, latency_ms,
                    ranking_model_version, calibration_version, query_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                session,
            )
            connection.executemany(
                """
                INSERT INTO search_candidates (
                    session_id, library_id, doc_id, sha256, original_rank,
                    displayed_rank, feature_schema_version, features_json,
                    ranking_score, displayed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, library_id, doc_id) DO UPDATE SET
                    displayed_rank = COALESCE(
                        excluded.displayed_rank, search_candidates.displayed_rank
                    ),
                    displayed = MAX(search_candidates.displayed, excluded.displayed)
                """,
                candidates,
            )
            self._prune(connection)

    def ensure_candidate(
        self,
        *,
        session_id: str,
        library_id: str,
        doc_id: str,
        original_rank: int,
        ranking_score: float = 0.0,
        sha256: str | None = None,
        feature_schema_version: int = 1,
        features: Mapping[str, Any] | None = None,
    ) -> None:
        """Add a safely bounded candidate when an uncaptured later page is used."""

        session_id = _identifier(session_id, "session_id")
        candidate = self._normalize_candidate(
            session_id,
            SearchCandidateRecord(
                library_id=library_id,
                doc_id=doc_id,
                original_rank=original_rank,
                displayed_rank=original_rank,
                sha256=sha256,
                feature_schema_version=feature_schema_version,
                features=features or {},
                ranking_score=ranking_score,
                displayed=True,
            ),
        )
        with self._write_connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM search_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if exists is None:
                raise SearchLearningValidationError("Unknown search session")
            connection.execute(
                """
                INSERT INTO search_candidates (
                    session_id, library_id, doc_id, sha256, original_rank,
                    displayed_rank, feature_schema_version, features_json,
                    ranking_score, displayed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, library_id, doc_id) DO UPDATE SET
                    displayed_rank = excluded.displayed_rank,
                    displayed = 1
                """,
                candidate,
            )

    def record_feedback(
        self,
        *,
        session_id: str,
        library_id: str,
        doc_id: str,
        action: str,
        source: str,
    ) -> JsonObject:
        session_id = _identifier(session_id, "session_id")
        library_id = _identifier(library_id, "library_id")
        doc_id = _identifier(doc_id, "doc_id")
        action = action.strip().lower() if isinstance(action, str) else ""
        if action not in FEEDBACK_WEIGHTS:
            raise SearchLearningValidationError("Unsupported feedback action")
        source = _identifier(source, "source")
        settings = self.settings()
        if not settings["learning_enabled"]:
            raise SearchLearningValidationError("Search learning is disabled")
        if action in IMPLICIT_ACTIONS and not settings["implicit_feedback_enabled"]:
            return {
                "accepted": False,
                "reason": "implicit_feedback_disabled",
                "action": action,
            }
        now = self._now()
        event_id = f"feedback-{uuid.uuid4().hex}"
        with self._write_connection() as connection:
            candidate = connection.execute(
                """
                SELECT 1 FROM search_candidates
                WHERE session_id = ? AND library_id = ? AND doc_id = ?
                """,
                (session_id, library_id, doc_id),
            ).fetchone()
            if candidate is None:
                raise SearchLearningValidationError("Unknown search candidate")
            existing = connection.execute(
                """
                SELECT * FROM feedback_events
                WHERE session_id = ? AND library_id = ? AND doc_id = ?
                  AND action = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, event_id DESC LIMIT 1
                """,
                (session_id, library_id, doc_id, action),
            ).fetchone()
            if existing is not None:
                result = self._feedback_row(existing)
                result["created"] = False
                return result
            if action in EXPLICIT_ACTIONS:
                connection.execute(
                    """
                    UPDATE feedback_events SET revoked_at = ?
                    WHERE session_id = ? AND library_id = ? AND doc_id = ?
                      AND action IN ('relevant', 'not_relevant')
                      AND revoked_at IS NULL
                    """,
                    (now, session_id, library_id, doc_id),
                )
            connection.execute(
                """
                INSERT INTO feedback_events (
                    event_id, session_id, library_id, doc_id, action,
                    feedback_weight, created_at, source, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    event_id,
                    session_id,
                    library_id,
                    doc_id,
                    action,
                    FEEDBACK_WEIGHTS[action],
                    now,
                    source,
                ),
            )
            self._prune(connection)
        return {
            "event_id": event_id,
            "session_id": session_id,
            "library_id": library_id,
            "doc_id": doc_id,
            "action": action,
            "feedback_weight": FEEDBACK_WEIGHTS[action],
            "created_at": now,
            "source": source,
            "active": True,
            "created": True,
        }

    def revoke_feedback(self, event_id: str) -> JsonObject:
        event_id = _identifier(event_id, "event_id")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM feedback_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise SearchLearningValidationError("Feedback event was not found")
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE feedback_events SET revoked_at = ? WHERE event_id = ?",
                    (self._now(), event_id),
                )
                row = connection.execute(
                    "SELECT * FROM feedback_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                assert row is not None
        return self._feedback_row(row)

    def list_feedback(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
        action: str | None = None,
        active_only: bool = True,
    ) -> JsonObject:
        limit = _limit(limit)
        filters: list[str] = []
        parameters: list[Any] = []
        if session_id:
            filters.append("session_id = ?")
            parameters.append(_identifier(session_id, "session_id"))
        if action:
            normalized_action = action.strip().lower()
            if normalized_action not in FEEDBACK_WEIGHTS:
                raise SearchLearningValidationError("Unsupported feedback action")
            filters.append("action = ?")
            parameters.append(normalized_action)
        if active_only:
            filters.append("revoked_at IS NULL")
        if cursor:
            created_at, event_id = _decode_cursor(cursor, "feedback", 2)
            if not isinstance(created_at, str) or not isinstance(event_id, str):
                raise InvalidSearchLearningCursor("Invalid feedback cursor")
            filters.append("(created_at < ? OR (created_at = ? AND event_id < ?))")
            parameters.extend((created_at, created_at, event_id))
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM feedback_events{where}
                ORDER BY created_at DESC, event_id DESC LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        visible = rows[:limit]
        return {
            "items": [self._feedback_row(row) for row in visible],
            "has_more": len(rows) > limit,
            "next_cursor": (
                _encode_cursor(
                    "feedback", [visible[-1]["created_at"], visible[-1]["event_id"]]
                )
                if len(rows) > limit and visible
                else None
            ),
        }

    def list_sessions(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        query_type: str | None = None,
    ) -> JsonObject:
        limit = _limit(limit)
        filters: list[str] = []
        parameters: list[Any] = []
        if query_type:
            filters.append("s.query_type = ?")
            parameters.append(_identifier(query_type, "query_type"))
        if cursor:
            created_at, session_id = _decode_cursor(cursor, "sessions", 2)
            if not isinstance(created_at, str) or not isinstance(session_id, str):
                raise InvalidSearchLearningCursor("Invalid session cursor")
            filters.append(
                "(s.created_at < ? OR (s.created_at = ? AND s.session_id < ?))"
            )
            parameters.extend((created_at, created_at, session_id))
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*,
                    COUNT(DISTINCT c.doc_id) AS candidate_count,
                    COUNT(DISTINCT CASE WHEN f.revoked_at IS NULL THEN f.event_id END)
                        AS feedback_count
                FROM search_sessions AS s
                LEFT JOIN search_candidates AS c ON c.session_id = s.session_id
                LEFT JOIN feedback_events AS f ON f.session_id = s.session_id
                {where}
                GROUP BY s.session_id
                ORDER BY s.created_at DESC, s.session_id DESC LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        visible = rows[:limit]
        return {
            "items": [self._session_row(row) for row in visible],
            "has_more": len(rows) > limit,
            "next_cursor": (
                _encode_cursor(
                    "sessions", [visible[-1]["created_at"], visible[-1]["session_id"]]
                )
                if len(rows) > limit and visible
                else None
            ),
        }

    def settings(self) -> JsonObject:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise SearchLearningStoreUnavailable("Learning settings are unavailable")
        return {
            "learning_enabled": bool(row["learning_enabled"]),
            "implicit_feedback_enabled": bool(row["implicit_feedback_enabled"]),
            "save_query_text": bool(row["save_query_text"]),
            "active_model_version": row["active_model_version"],
            "previous_model_version": row["previous_model_version"],
            "shadow_mode": bool(row["shadow_mode"]),
            "updated_at": row["updated_at"],
        }

    def update_settings(
        self,
        *,
        learning_enabled: bool | None = None,
        implicit_feedback_enabled: bool | None = None,
        save_query_text: bool | None = None,
    ) -> JsonObject:
        updates = {
            "learning_enabled": learning_enabled,
            "implicit_feedback_enabled": implicit_feedback_enabled,
            "save_query_text": save_query_text,
        }
        if not any(value is not None for value in updates.values()):
            return self.settings()
        if any(
            value is not None and not isinstance(value, bool)
            for value in updates.values()
        ):
            raise SearchLearningValidationError("Learning settings must be boolean")
        current = self.settings()
        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE learning_settings SET
                    learning_enabled = ?, implicit_feedback_enabled = ?,
                    save_query_text = ?, updated_at = ? WHERE singleton = 1
                """,
                (
                    int(
                        current["learning_enabled"]
                        if learning_enabled is None
                        else learning_enabled
                    ),
                    int(
                        current["implicit_feedback_enabled"]
                        if implicit_feedback_enabled is None
                        else implicit_feedback_enabled
                    ),
                    int(
                        current["save_query_text"]
                        if save_query_text is None
                        else save_query_text
                    ),
                    self._now(),
                ),
            )
        return self.settings()

    def training_counts(self) -> JsonObject:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT f.session_id) AS query_sessions,
                    COUNT(*) AS explicit_samples,
                    SUM(CASE WHEN f.action = 'relevant' THEN 1 ELSE 0 END)
                        AS positive_samples,
                    SUM(CASE WHEN f.action = 'not_relevant' THEN 1 ELSE 0 END)
                        AS negative_samples
                FROM feedback_events AS f
                WHERE f.revoked_at IS NULL
                  AND f.action IN ('relevant', 'not_relevant')
                """
            ).fetchone()
        assert row is not None
        return {
            key: int(row[key] or 0)
            for key in (
                "query_sessions",
                "explicit_samples",
                "positive_samples",
                "negative_samples",
            )
        }

    def training_examples(self) -> list[JsonObject]:
        """Return one strongest label per candidate, preferring explicit feedback."""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                WITH ranked_feedback AS (
                    SELECT f.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY f.session_id, f.library_id, f.doc_id
                            ORDER BY
                                CASE WHEN f.action IN (
                                    'relevant', 'not_relevant'
                                ) THEN 0 ELSE 1 END ASC,
                                CASE WHEN f.action IN (
                                    'relevant', 'not_relevant'
                                ) THEN f.created_at END DESC,
                                ABS(f.feedback_weight) DESC,
                                f.created_at DESC,
                                f.event_id DESC
                        ) AS preference
                    FROM feedback_events AS f
                    WHERE f.revoked_at IS NULL
                )
                SELECT f.session_id, f.action, f.feedback_weight,
                       c.feature_schema_version, c.features_json
                FROM ranked_feedback AS f
                JOIN search_candidates AS c
                  ON c.session_id = f.session_id
                 AND c.library_id = f.library_id
                 AND c.doc_id = f.doc_id
                WHERE f.preference = 1
                ORDER BY f.created_at ASC, f.event_id ASC
                """
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "label": 0 if row["action"] == "not_relevant" else 1,
                "explicit": row["action"] in EXPLICIT_ACTIONS,
                "feedback_weight": float(row["feedback_weight"]),
                "feature_schema_version": int(row["feature_schema_version"]),
                "features": json.loads(row["features_json"]),
            }
            for row in rows
        ]

    def register_model_version(self, payload: Mapping[str, Any]) -> None:
        model_version = _identifier(payload.get("model_version"), "model_version")
        status = str(payload.get("status") or "candidate")
        if status not in {"training", "candidate", "active", "retired", "failed"}:
            raise SearchLearningValidationError("Invalid model version status")
        gate_status = str(payload.get("gate_status") or "pending")
        if gate_status not in {"pending", "passed", "failed"}:
            raise SearchLearningValidationError("Invalid evaluation gate status")
        model_file = _relative_artifact(payload.get("model_file"), "model_file")
        report_value = payload.get("report_file")
        report_file = (
            _relative_artifact(report_value, "report_file") if report_value else None
        )
        metrics = _safe_json(payload.get("metrics") or {}, maximum=32 * 1024)
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO model_versions (
                    model_version, status, created_at, activated_at,
                    parent_version, model_file, report_file, explicit_samples,
                    positive_samples, negative_samples, query_sessions,
                    gate_status, metrics_json, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_version) DO UPDATE SET
                    status = excluded.status,
                    report_file = excluded.report_file,
                    explicit_samples = excluded.explicit_samples,
                    positive_samples = excluded.positive_samples,
                    negative_samples = excluded.negative_samples,
                    query_sessions = excluded.query_sessions,
                    gate_status = excluded.gate_status,
                    metrics_json = excluded.metrics_json,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message
                """,
                (
                    model_version,
                    status,
                    str(payload.get("created_at") or self._now()),
                    payload.get("activated_at"),
                    _optional_identifier(payload.get("parent_version")),
                    model_file,
                    report_file,
                    _non_negative_int(payload.get("explicit_samples", 0)),
                    _non_negative_int(payload.get("positive_samples", 0)),
                    _non_negative_int(payload.get("negative_samples", 0)),
                    _non_negative_int(payload.get("query_sessions", 0)),
                    gate_status,
                    metrics,
                    _optional_identifier(payload.get("error_code")),
                    _safe_text(payload.get("error_message"), 512),
                ),
            )

    def model_version(self, model_version: str) -> JsonObject:
        model_version = _identifier(model_version, "model_version")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_versions WHERE model_version = ?",
                (model_version,),
            ).fetchone()
        if row is None:
            raise SearchLearningValidationError("Model version was not found")
        return self._model_row(row)

    def list_model_versions(self, *, limit: int = 20) -> list[JsonObject]:
        limit = _limit(limit, maximum=100)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM model_versions
                ORDER BY created_at DESC, model_version DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._model_row(row) for row in rows]

    def activate_model(self, model_version: str, *, shadow_mode: bool) -> JsonObject:
        model = self.model_version(model_version)
        if model["gate_status"] != "passed":
            raise SearchLearningValidationError(
                "Model cannot be activated before the fixed evaluation gate passes"
            )
        now = self._now()
        settings = self.settings()
        current = settings.get("active_model_version")
        # Promoting the same candidate from shadow mode to formal activation
        # must retain the last different model as the rollback target.
        previous = (
            settings.get("previous_model_version")
            if current == model_version
            else current
        )
        with self._write_connection() as connection:
            if current and current != model_version:
                connection.execute(
                    """
                    UPDATE model_versions SET status = 'retired'
                    WHERE model_version = ?
                    """,
                    (current,),
                )
            connection.execute(
                """
                UPDATE model_versions
                SET status = 'active', activated_at = ? WHERE model_version = ?
                """,
                (now, model_version),
            )
            connection.execute(
                """
                UPDATE learning_settings SET active_model_version = ?,
                    previous_model_version = ?, shadow_mode = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (model_version, previous, int(shadow_mode), now),
            )
        return self.settings()

    def rollback_model(self) -> JsonObject:
        settings = self.settings()
        previous = settings.get("previous_model_version")
        current = settings.get("active_model_version")
        if not previous:
            raise SearchLearningValidationError(
                "No previous model version is available"
            )
        previous_model = self.model_version(str(previous))
        if previous_model["gate_status"] != "passed":
            raise SearchLearningValidationError("Previous model is not gate-approved")
        now = self._now()
        with self._write_connection() as connection:
            if current:
                connection.execute(
                    """
                    UPDATE model_versions SET status = 'retired'
                    WHERE model_version = ?
                    """,
                    (current,),
                )
            connection.execute(
                """
                UPDATE model_versions SET status = 'active', activated_at = ?
                WHERE model_version = ?
                """,
                (now, previous),
            )
            connection.execute(
                """
                UPDATE learning_settings SET active_model_version = ?,
                    previous_model_version = ?, shadow_mode = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (previous, current, now),
            )
        return self.settings()

    def record_learning_job(self, payload: Mapping[str, Any]) -> None:
        job_id = _identifier(payload.get("job_id"), "job_id")
        status = str(payload.get("status") or "queued")
        if status not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise SearchLearningValidationError("Invalid learning job status")
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO learning_jobs (
                    job_id, job_type, status, submitted_at, started_at,
                    finished_at, model_version, message, error_code, error_message
                ) VALUES (?, 'train', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    started_at = COALESCE(
                        excluded.started_at, learning_jobs.started_at
                    ),
                    finished_at = excluded.finished_at,
                    model_version = COALESCE(
                        excluded.model_version, learning_jobs.model_version
                    ),
                    message = excluded.message,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message
                """,
                (
                    job_id,
                    status,
                    str(payload.get("submitted_at") or self._now()),
                    payload.get("started_at"),
                    payload.get("finished_at"),
                    _optional_identifier(payload.get("model_version")),
                    _safe_text(payload.get("message"), 512),
                    _optional_identifier(payload.get("error_code")),
                    _safe_text(payload.get("error_message"), 512),
                ),
            )

    def learning_job(self, job_id: str) -> JsonObject:
        job_id = _identifier(job_id, "job_id")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise SearchLearningValidationError("Learning job was not found")
        return dict(row)

    def latest_learning_job(self) -> JsonObject | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM learning_jobs
                ORDER BY submitted_at DESC, job_id DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def clear_learning_data(self) -> JsonObject:
        """Clear only the dedicated learning database; never touch user data."""

        with self._write_connection() as connection:
            counts = {
                "sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM search_sessions"
                    ).fetchone()[0]
                ),
                "feedback_events": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM feedback_events"
                    ).fetchone()[0]
                ),
                "model_versions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM model_versions"
                    ).fetchone()[0]
                ),
            }
            connection.execute("DELETE FROM feedback_events")
            connection.execute("DELETE FROM search_candidates")
            connection.execute("DELETE FROM search_sessions")
            connection.execute("DELETE FROM learning_jobs")
            connection.execute("DELETE FROM model_versions")
            connection.execute(
                """
                UPDATE learning_settings SET active_model_version = NULL,
                    previous_model_version = NULL, shadow_mode = 1, updated_at = ?
                WHERE singleton = 1
                """,
                (self._now(),),
            )
        return {"cleared": True, **counts}

    def anonymous_export(self) -> JsonObject:
        """Return bounded training examples without stable library/document IDs."""

        examples = self.training_examples()
        exported = []
        for item in examples:
            digest = hashlib.sha256(
                str(item["session_id"]).encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            exported.append(
                {
                    "anonymous_session": digest,
                    "label": item["label"],
                    "feedback_weight": item["feedback_weight"],
                    "feature_schema_version": item["feature_schema_version"],
                    "features": item["features"],
                }
            )
        return {
            "schema_version": 1,
            "generated_at": self._now(),
            "examples": exported,
        }

    def status(self) -> JsonObject:
        counts = (
            self.training_counts()
            if self._available
            else {
                "query_sessions": 0,
                "explicit_samples": 0,
                "positive_samples": 0,
                "negative_samples": 0,
            }
        )
        settings = self.settings() if self._available else {}
        return {
            "available": self._available,
            "database": SEARCH_LEARNING_DATABASE_NAME,
            "last_error": self._last_error,
            "settings": settings,
            "training_counts": counts,
            "minimum_requirements": {
                "query_sessions": 100,
                "explicit_samples": 300,
                "positive_and_negative_required": True,
            },
            "latest_job": self.latest_learning_job() if self._available else None,
        }

    def _normalize_session(
        self, record: SearchSessionRecord
    ) -> tuple[tuple[Any, ...], list[tuple[Any, ...]]]:
        if not isinstance(record, SearchSessionRecord):
            raise SearchLearningValidationError(
                "Search session must use the typed record"
            )
        session_id = _identifier(record.session_id, "session_id")
        query_type = _identifier(record.query_type, "query_type")
        requested_count = _non_negative_int(record.requested_count)
        returned_count = _non_negative_int(record.returned_count)
        latency_ms = _non_negative_int(record.latency_ms)
        library_ids = sorted(
            {_identifier(item, "library_id") for item in record.library_ids}
        )
        settings = self.settings()
        query_text = None
        if settings["save_query_text"] and record.query_text:
            query_text = self._sanitize_query_text(record.query_text)
        created_at = _timestamp(record.created_at or self._clock())
        session = (
            session_id,
            created_at,
            query_type,
            requested_count,
            returned_count,
            json.dumps(library_ids, separators=(",", ":"), ensure_ascii=False),
            latency_ms,
            _optional_identifier(record.ranking_model_version),
            _optional_identifier(record.calibration_version),
            query_text,
        )
        candidates = [
            self._normalize_candidate(session_id, candidate)
            for candidate in record.candidates[: self._max_candidates_per_session]
        ]
        return session, candidates

    def _normalize_candidate(
        self, session_id: str, candidate: SearchCandidateRecord
    ) -> tuple[Any, ...]:
        if not isinstance(candidate, SearchCandidateRecord):
            raise SearchLearningValidationError("Candidate must use the typed record")
        library_id = _identifier(candidate.library_id, "library_id")
        doc_id = _identifier(candidate.doc_id, "doc_id")
        original_rank = _positive_int(candidate.original_rank)
        displayed_rank = (
            _positive_int(candidate.displayed_rank)
            if candidate.displayed_rank is not None
            else None
        )
        sha256 = None
        if candidate.sha256:
            if not _SHA256.fullmatch(candidate.sha256):
                raise SearchLearningValidationError(
                    "sha256 must be a hexadecimal digest"
                )
            sha256 = candidate.sha256.lower()
        feature_schema_version = _positive_int(candidate.feature_schema_version)
        features = _safe_features(candidate.features, redactions=self._redactions)
        score = float(candidate.ranking_score)
        if not math.isfinite(score):
            raise SearchLearningValidationError("ranking_score must be finite")
        return (
            session_id,
            library_id,
            doc_id,
            sha256,
            original_rank,
            displayed_rank,
            feature_schema_version,
            features,
            score,
            int(bool(candidate.displayed)),
        )

    def _sanitize_query_text(self, value: str) -> str:
        text = _safe_text(value, 1_024) or ""
        text = _BEARER.sub("[REDACTED]", text)
        for secret in self._redactions:
            text = text.replace(secret, "[REDACTED]")
        if _looks_absolute_path(text):
            return "[REDACTED_PATH]"
        return text

    def _feedback_row(self, row: sqlite3.Row) -> JsonObject:
        return {
            "event_id": row["event_id"],
            "session_id": row["session_id"],
            "library_id": row["library_id"],
            "doc_id": row["doc_id"],
            "action": row["action"],
            "feedback_weight": float(row["feedback_weight"]),
            "created_at": row["created_at"],
            "source": row["source"],
            "active": row["revoked_at"] is None,
            "revoked_at": row["revoked_at"],
        }

    def _session_row(self, row: sqlite3.Row) -> JsonObject:
        # Query text is intentionally omitted from every regular API response.
        return {
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "query_type": row["query_type"],
            "requested_count": int(row["requested_count"]),
            "returned_count": int(row["returned_count"]),
            "library_ids": json.loads(row["library_ids_json"]),
            "latency_ms": int(row["latency_ms"]),
            "ranking_model_version": row["ranking_model_version"],
            "calibration_version": row["calibration_version"],
            "candidate_count": int(row["candidate_count"]),
            "feedback_count": int(row["feedback_count"]),
        }

    def _model_row(self, row: sqlite3.Row) -> JsonObject:
        return {
            **dict(row),
            "metrics": json.loads(row["metrics_json"]),
        }

    def _recover_interrupted_jobs(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        connection.execute(
            """
            UPDATE learning_jobs SET status = 'interrupted', finished_at = ?,
                error_code = 'process_restarted',
                error_message =
                    'The previous process stopped before training completed.'
            WHERE status IN ('queued', 'running')
            """,
            (now,),
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = _timestamp(self._clock() - timedelta(days=self._retention_days))
        connection.execute(
            "DELETE FROM search_sessions WHERE created_at < ?", (cutoff,)
        )
        session_count = int(
            connection.execute("SELECT COUNT(*) FROM search_sessions").fetchone()[0]
        )
        overage = max(0, session_count - self._max_sessions)
        if overage:
            connection.execute(
                """
                DELETE FROM search_sessions WHERE session_id IN (
                    SELECT session_id FROM search_sessions
                    ORDER BY created_at ASC, session_id ASC LIMIT ?
                )
                """,
                (overage,),
            )
        feedback_count = int(
            connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
        )
        feedback_overage = max(0, feedback_count - self._max_feedback_events)
        if feedback_overage:
            connection.execute(
                """
                DELETE FROM feedback_events WHERE event_id IN (
                    SELECT event_id FROM feedback_events
                    ORDER BY created_at ASC, event_id ASC LIMIT ?
                )
                """,
                (feedback_overage,),
            )

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._sqlite_timeout_seconds,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={int(self._sqlite_timeout_seconds * 1000)}"
        )
        return connection

    def _read_connection(self):
        return _ConnectionContext(self, write=False)

    def _write_connection(self):
        return _ConnectionContext(self, write=True)

    def _remember_error(self, exc: BaseException) -> None:
        message = _safe_text(str(exc), 240) or "unavailable"
        for secret in self._redactions:
            message = message.replace(secret, "[REDACTED]")
        self._last_error = f"{exc.__class__.__name__}: {message}"

    def _now(self) -> str:
        return _timestamp(self._clock())


class _ConnectionContext:
    def __init__(self, store: SearchLearningStore, *, write: bool) -> None:
        self._store = store
        self._write = write
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        if not self._store.available:
            raise SearchLearningStoreUnavailable("Search learning is unavailable")
        self._store._lock.acquire()
        try:
            self._connection = self._store._open()
            if self._write:
                self._connection.execute("BEGIN IMMEDIATE")
            return self._connection
        except (OSError, sqlite3.Error) as exc:
            self._store._remember_error(exc)
            self._store._lock.release()
            raise SearchLearningStoreUnavailable(
                "Search learning is unavailable"
            ) from exc

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        connection = self._connection
        try:
            if connection is not None:
                if self._write:
                    if exc is None:
                        connection.commit()
                    else:
                        connection.rollback()
                connection.close()
        except sqlite3.Error as close_error:
            self._store._remember_error(close_error)
            if exc is None:
                raise SearchLearningStoreUnavailable(
                    "Unable to commit search learning data"
                ) from close_error
        finally:
            self._store._lock.release()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise SearchLearningValidationError(f"{name} must be text")
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise SearchLearningValidationError(f"{name} is invalid")
    return normalized


def _optional_identifier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _identifier(value, "identifier")


def _relative_artifact(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchLearningValidationError(f"{name} is required")
    normalized = value.strip().replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
        raise SearchLearningValidationError(f"{name} must be a local artifact name")
    return pure.as_posix()


def _safe_features(value: Mapping[str, Any], *, redactions: Sequence[str] = ()) -> str:
    if not isinstance(value, Mapping):
        raise SearchLearningValidationError("features must be an object")
    cleaned: JsonObject = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not _IDENTIFIER.fullmatch(raw_key):
            raise SearchLearningValidationError("Feature name is invalid")
        if _SENSITIVE_KEY.search(raw_key):
            continue
        if isinstance(raw_value, (bool, int)) or (
            isinstance(raw_value, float) and math.isfinite(raw_value)
        ):
            cleaned[raw_key] = raw_value
        elif isinstance(raw_value, str) and len(raw_value) <= 80:
            if (
                _looks_absolute_path(raw_value)
                or _BEARER.search(raw_value)
                or any(secret and secret in raw_value for secret in redactions)
            ):
                continue
            cleaned[raw_key] = raw_value
    return _safe_json(cleaned, maximum=16 * 1024)


def _safe_json(value: Any, *, maximum: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SearchLearningValidationError("Value is not safe JSON") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise SearchLearningValidationError("JSON value is too large")
    return encoded


def _safe_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    if len(text) > maximum:
        text = text[:maximum]
    if _BEARER.search(text) or _looks_absolute_path(text):
        return "[REDACTED]"
    return text


def _looks_absolute_path(value: str) -> bool:
    return (
        PureWindowsPath(value).is_absolute()
        or PurePosixPath(value).is_absolute()
        or _ABSOLUTE_PATH_FRAGMENT.search(value) is not None
    )


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise SearchLearningValidationError("Value must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SearchLearningValidationError("Value must be a positive integer") from exc
    if result < 1:
        raise SearchLearningValidationError("Value must be a positive integer")
    return result


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise SearchLearningValidationError("Value must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SearchLearningValidationError(
            "Value must be a non-negative integer"
        ) from exc
    if result < 0:
        raise SearchLearningValidationError("Value must be a non-negative integer")
    return result


def _limit(value: int, *, maximum: int = 200) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise SearchLearningValidationError(f"limit must be between 1 and {maximum}")
    return value


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SearchLearningValidationError("Timestamp is invalid") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    else:
        raise SearchLearningValidationError("Timestamp is invalid")
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _encode_cursor(kind: str, values: Sequence[Any]) -> str:
    raw = json.dumps([kind, *values], separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, kind: str, arity: int) -> list[Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1_024:
        raise InvalidSearchLearningCursor("Invalid search-learning cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding, altchars=b"-_", validate=True
        ).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSearchLearningCursor("Invalid search-learning cursor") from exc
    if not isinstance(payload, list) or len(payload) != arity + 1 or payload[0] != kind:
        raise InvalidSearchLearningCursor("Invalid search-learning cursor")
    return payload[1:]


__all__ = [
    "EXPLICIT_ACTIONS",
    "FEEDBACK_WEIGHTS",
    "IMPLICIT_ACTIONS",
    "InvalidSearchLearningCursor",
    "SEARCH_LEARNING_DATABASE_NAME",
    "SearchCandidateRecord",
    "SearchLearningStore",
    "SearchLearningStoreUnavailable",
    "SearchLearningValidationError",
    "SearchSessionRecord",
]
