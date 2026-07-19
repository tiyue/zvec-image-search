from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .config import ConfigurationError, ServiceConfig
from .library_browser import LibraryBrowser

_PREVIEW_TTL_SECONDS = 15 * 60
_DELETE_CHUNK_SIZE = 128
_OWNER_FILE = ".zvec-folder-trash-owner.json"
_MAX_FAILURE_DETAILS = 100
_RECOVERABLE_STATUSES = ("staging", "staged", "committing", "finalizing")
_COMPLETED_RETENTION_DAYS = 30
_PRUNE_BATCH_SIZE = 250


class FolderDeletionError(ConfigurationError):
    """A destructive folder operation cannot be performed safely."""


class _DeletionState(Protocol):
    def root_path(self, root_id: str) -> str | None: ...

    def iter_folder_entries(
        self,
        root_id: str,
        relative_folder: str,
        *,
        include_subfolders: bool = False,
        chunk_size: int = 256,
    ) -> Iterator[list[dict[str, Any]]]: ...

    def remove_many(self, doc_ids: Iterable[str]) -> None: ...

    def get_many(self, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]: ...


class _DeletionRepository(Protocol):
    collection_uuid: str

    def delete(self, doc_ids: Iterable[str]) -> tuple[list[str], dict[str, str]]: ...

    def contains(self, doc_id: str) -> bool: ...


@dataclass(frozen=True)
class _PhysicalEntry:
    relative_path: str
    size_bytes: int
    mtime_ns: int
    is_directory: bool


@dataclass(frozen=True)
class _TreeSnapshot:
    entries: tuple[_PhysicalEntry, ...]
    file_count: int
    directory_count: int
    size_bytes: int
    reparse_points: tuple[str, ...]


class FolderDeletionJournal:
    """Durable saga log kept separately from the image state database."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL,
                collection_uuid TEXT NOT NULL,
                folder_key TEXT NOT NULL,
                root_id TEXT NOT NULL,
                relative_folder TEXT NOT NULL,
                root_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                stage_path TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_folder_delete_operations_status
                ON operations(status, updated_at);
            CREATE TABLE IF NOT EXISTS operation_items (
                operation_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(operation_id, doc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_folder_delete_items_status
                ON operation_items(operation_id, status, doc_id);
            """
        )
        self.connection.commit()

    def create(
        self,
        operation: Mapping[str, Any],
        entries: Iterable[Mapping[str, Any]],
    ) -> None:
        values = [
            (
                str(operation["operation_id"]),
                str(entry["doc_id"]),
                str(entry["relative_path"]),
                str(entry.get("sha256") or ""),
                int(entry.get("size_bytes") or 0),
                int(entry.get("mtime_ns") or 0),
            )
            for entry in entries
        ]
        with self.connection:
            self.connection.execute(
                "INSERT INTO operations("
                "operation_id, library_id, collection_uuid, folder_key, root_id, "
                "relative_folder, root_path, target_path, stage_path, token_hash, "
                "snapshot_digest, expires_at, status, preview_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)",
                (
                    str(operation["operation_id"]),
                    str(operation["library_id"]),
                    str(operation["collection_uuid"]),
                    str(operation["folder_key"]),
                    str(operation["root_id"]),
                    str(operation["relative_folder"]),
                    str(operation["root_path"]),
                    str(operation["target_path"]),
                    str(operation["stage_path"]),
                    str(operation["token_hash"]),
                    str(operation["snapshot_digest"]),
                    float(operation["expires_at"]),
                    json.dumps(operation["preview"], ensure_ascii=False),
                ),
            )
            if values:
                self.connection.executemany(
                    "INSERT INTO operation_items("
                    "operation_id, doc_id, relative_path, sha256, size_bytes, mtime_ns"
                    ") VALUES(?, ?, ?, ?, ?, ?)",
                    values,
                )

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["preview"] = _json_object(result.pop("preview_json"), "preview")
        result["result"] = _json_object(result.pop("result_json"), "result")
        return result

    def operations_with_status(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"SELECT operation_id FROM operations WHERE status IN ({placeholders}) "
            "ORDER BY created_at",
            tuple(statuses),
        )
        return [
            operation
            for row in rows
            if (operation := self.operation(str(row["operation_id"]))) is not None
        ]

    def items(
        self,
        operation_id: str,
        *,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        values: list[Any] = [operation_id]
        where = "operation_id = ?"
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where += f" AND status IN ({placeholders})"
            values.extend(statuses)
        suffix = " ORDER BY doc_id"
        if limit is not None:
            suffix += " LIMIT ?"
            values.append(limit)
        rows = self.connection.execute(
            f"SELECT * FROM operation_items WHERE {where}{suffix}", values
        )
        return [dict(row) for row in rows]

    def update_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE operations SET status = ?, result_json = ?, error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (
                    status,
                    json.dumps(dict(result or {}), ensure_ascii=False),
                    str(error),
                    operation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise FolderDeletionError(f"Unknown folder deletion: {operation_id}")

    def prune(
        self,
        *,
        now: float | None = None,
        completed_retention_days: int = _COMPLETED_RETENTION_DAYS,
        limit: int = _PRUNE_BATCH_SIZE,
    ) -> dict[str, int]:
        """Bound journal growth without deleting recoverable safety evidence."""

        current = time.time() if now is None else float(now)
        retention = max(1, int(completed_retention_days))
        batch_limit = max(1, min(int(limit), 1_000))
        rows = self.connection.execute(
            "SELECT operation_id FROM operations "
            "WHERE (status = 'prepared' AND expires_at < ?) "
            "OR (status IN ('committed', 'partial') "
            "AND updated_at < datetime('now', ?)) "
            "ORDER BY updated_at LIMIT ?",
            (current, f"-{retention} days", batch_limit),
        ).fetchall()
        operation_ids = [str(row["operation_id"]) for row in rows]
        if not operation_ids:
            return {"operations": 0, "items": 0}
        placeholders = ", ".join("?" for _ in operation_ids)
        item_count = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM operation_items "
                f"WHERE operation_id IN ({placeholders})",
                operation_ids,
            ).fetchone()[0]
        )
        with self.connection:
            self.connection.execute(
                f"DELETE FROM operation_items WHERE operation_id IN ({placeholders})",
                operation_ids,
            )
            self.connection.execute(
                f"DELETE FROM operations WHERE operation_id IN ({placeholders})",
                operation_ids,
            )
        return {"operations": len(operation_ids), "items": item_count}

    def mark_items(
        self,
        operation_id: str,
        doc_ids: Iterable[str],
        *,
        status: str,
        error: str = "",
    ) -> None:
        values = [(status, str(error), operation_id, str(doc_id)) for doc_id in doc_ids]
        if not values:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE operation_items SET status = ?, error = ?, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE operation_id = ? AND doc_id = ?",
                values,
            )

    def close(self) -> None:
        self.connection.close()


class FolderDeletionManager:
    """Preview, commit and recover one library-bound folder deletion saga."""

    def __init__(
        self,
        *,
        config: ServiceConfig,
        state: _DeletionState,
        repository: _DeletionRepository,
        library_id: str,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.repository = repository
        self.library_id = str(library_id).strip()
        if not self.library_id:
            raise ValueError("library_id must not be empty.")
        self.progress = progress or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: None)
        self.journal = FolderDeletionJournal(
            config.workspace / "folder_deletions.sqlite3"
        )
        self.journal.prune()

    def close(self) -> None:
        self.journal.close()

    def preview(
        self,
        *,
        folder_key: str,
        include_subfolders: bool = True,
    ) -> dict[str, Any]:
        self.cancel_check()
        if include_subfolders is not True:
            raise FolderDeletionError(
                "Folder clearing always includes every descendant folder."
            )
        with LibraryBrowser(
            library_id=self.library_id,
            state_path=self.config.state_path,
        ) as browser:
            root_id, relative_folder = browser.decode_folder_key(folder_key)
        root_path_value = self.state.root_path(root_id)
        if not root_path_value:
            raise FolderDeletionError("The selected folder root no longer exists.")
        root_path = Path(root_path_value).expanduser().resolve()
        target_path = _folder_target(root_path, relative_folder)
        operation_id = uuid.uuid4().hex
        stage_path = _stage_path(root_path, root_id, operation_id)
        protected = self._protected_overlaps(target_path)
        root_blocked = target_path == target_path.parent

        entries = self._scope_entries(root_id, relative_folder)
        tree = _scan_tree(target_path)
        physical_files = {
            _path_key(target_path / Path(item.relative_path))
            for item in tree.entries
            if not item.is_directory
        }
        indexed_paths = {
            _path_key(root_path / Path(str(entry["relative_path"])))
            for entry in entries
        }
        missing = sum(1 for path in indexed_paths if path not in physical_files)
        changed = 0
        by_relative = {
            str(item.relative_path).replace("\\", "/"): item
            for item in tree.entries
            if not item.is_directory
        }
        for entry in entries:
            relative_to_target = _relative_inside_target(
                str(entry["relative_path"]), relative_folder
            )
            physical = by_relative.get(relative_to_target)
            if physical is None:
                continue
            if physical.size_bytes != int(entry.get("size_bytes") or 0) or (
                physical.mtime_ns != int(entry.get("mtime_ns") or 0)
            ):
                changed += 1
        other_files = max(0, tree.file_count - (len(entries) - missing))

        warnings: list[str] = []
        blocked_reasons: list[str] = []
        if protected:
            blocked_reasons.append("protected_path_overlap")
            warnings.append(
                "The selected folder overlaps application data, results, cache, "
                "or failure storage and cannot be cleared safely."
            )
        if tree.reparse_points:
            blocked_reasons.append("reparse_point")
            warnings.append(
                "The selected folder contains a symbolic link, junction, or other "
                "reparse point. Remove it before clearing the folder."
            )
        if changed:
            blocked_reasons.append("indexed_file_changed")
            warnings.append(
                f"{changed} indexed files changed after indexing; re-index or move "
                "them before clearing this folder."
            )
        if root_blocked:
            blocked_reasons.append("filesystem_root")
            warnings.append("A filesystem root cannot be cleared by this operation.")
        if other_files:
            warnings.append(
                f"The physical folder contains {other_files} files that are not "
                "indexed; confirmation will delete them too."
            )
        if relative_folder == "":
            warnings.append(
                "The registered library root and its database binding will be kept."
            )

        token = secrets.token_urlsafe(32)
        expires_at = time.time() + _PREVIEW_TTL_SECONDS
        digest = _snapshot_digest(
            collection_uuid=str(self.repository.collection_uuid),
            root_id=root_id,
            relative_folder=relative_folder,
            entries=entries,
            tree=tree,
        )
        folder_name = (
            PurePosixPath(relative_folder).name
            if relative_folder
            else root_path.name or str(root_path)
        )
        preview = {
            "operation_id": operation_id,
            "confirmation_token": token,
            "folder_key": folder_key,
            "name": folder_name,
            "folder_name": folder_name,
            "root_name": root_path.name or str(root_path),
            "relative_folder": relative_folder,
            "include_subfolders": True,
            "is_library_root": relative_folder == "",
            "image_count": len(entries),
            "file_count": tree.file_count,
            "other_file_count": other_files,
            "unindexed_file_count": other_files,
            "directory_count": tree.directory_count,
            "folder_count": tree.directory_count,
            "size_bytes": tree.size_bytes,
            "missing_indexed_count": missing,
            "changed_indexed_count": changed,
            "missing_count": missing,
            "changed_count": changed,
            "protected_count": len(blocked_reasons),
            "confirmation_phrase": folder_name,
            "warnings": warnings,
            "blocked": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "api_requests": 0,
        }
        self.cancel_check()
        self.journal.create(
            {
                "operation_id": operation_id,
                "library_id": self.library_id,
                "collection_uuid": str(self.repository.collection_uuid),
                "folder_key": folder_key,
                "root_id": root_id,
                "relative_folder": relative_folder,
                "root_path": str(root_path),
                "target_path": str(target_path),
                "stage_path": str(stage_path),
                "token_hash": _token_hash(token),
                "snapshot_digest": digest,
                "expires_at": expires_at,
                "preview": preview,
            },
            entries,
        )
        return preview

    def commit(
        self,
        *,
        operation_id: str,
        confirmation_token: str,
        confirm: bool,
    ) -> dict[str, Any]:
        # Cancellation is safe until physical staging begins.  Once the source
        # tree has moved into the owned trash directory, the saga must reach a
        # consistent database commit or rollback instead of abandoning it.
        self.cancel_check()
        if confirm is not True:
            raise FolderDeletionError(
                "Explicit folder deletion confirmation is required."
            )
        operation = self._operation(operation_id)
        if operation["library_id"] != self.library_id:
            raise FolderDeletionError("The deletion belongs to a different library.")
        if operation["collection_uuid"] != str(self.repository.collection_uuid):
            raise FolderDeletionError("The Collection changed after the preview.")
        if not hmac.compare_digest(
            str(operation["token_hash"]), _token_hash(confirmation_token)
        ):
            raise FolderDeletionError(
                "The folder deletion confirmation token is invalid."
            )
        if float(operation["expires_at"]) < time.time():
            raise FolderDeletionError("The folder deletion preview expired.")
        if operation["status"] in {"committed", "partial", "needs_attention"}:
            result = dict(operation.get("result") or {})
            result["already_finished"] = True
            return result
        if operation["status"] != "prepared":
            raise FolderDeletionError(
                f"The folder deletion is currently {operation['status']}."
            )
        preview = dict(operation["preview"])
        if preview.get("blocked"):
            raise FolderDeletionError(
                "The preview contains safety blockers and cannot be committed."
            )
        self._validate_snapshot(operation)
        return self._execute(operation)

    def recover_incomplete(self) -> dict[str, Any]:
        """Resume only operations that crossed the physical staging boundary."""

        recovered = 0
        failed = 0
        failures: list[dict[str, str]] = []
        for operation in self.journal.operations_with_status(_RECOVERABLE_STATUSES):
            if str(operation.get("library_id") or "") != self.library_id:
                continue
            try:
                if str(operation.get("collection_uuid") or "") != str(
                    self.repository.collection_uuid
                ):
                    raise FolderDeletionError(
                        "The interrupted deletion belongs to a replaced Collection."
                    )
                result = self._execute(operation, recovering=True)
                if result.get("needs_attention"):
                    failed += 1
                    if len(failures) < _MAX_FAILURE_DETAILS:
                        failures.append(
                            {
                                "operation_id": str(operation["operation_id"]),
                                "error": (
                                    "Recovery completed with an inconsistent item."
                                ),
                            }
                        )
                else:
                    recovered += 1
            except Exception as exc:
                failed += 1
                if len(failures) < _MAX_FAILURE_DETAILS:
                    failures.append(
                        {
                            "operation_id": str(operation["operation_id"]),
                            "error": str(exc) or exc.__class__.__name__,
                        }
                    )
        return {
            "recovered": recovered,
            "failed": failed,
            "failures": failures,
            "api_requests": 0,
        }

    def _execute(
        self, operation: Mapping[str, Any], *, recovering: bool = False
    ) -> dict[str, Any]:
        if not recovering:
            self.cancel_check()
        operation_id = str(operation["operation_id"])
        self.journal.update_operation(operation_id, status="staging")
        self.progress("Safely staging the selected folder...")
        self._stage(operation, recovering=recovering)
        prepared_items = self.journal.items(
            operation_id,
            statuses=("prepared",),
        )
        self.journal.mark_items(
            operation_id,
            (item["doc_id"] for item in prepared_items),
            status="staged",
        )
        self.journal.update_operation(operation_id, status="committing")

        deleted = 0
        missing = 0
        failures: list[dict[str, str]] = []
        needs_attention = False
        while True:
            items = self.journal.items(
                operation_id,
                statuses=("staged",),
                limit=_DELETE_CHUNK_SIZE,
            )
            if not items:
                break
            # Once physical staging completed, each bounded batch must reach a
            # consistent commit or rollback. Cancellation is deliberately not
            # checked inside this critical section.
            ids = [str(item["doc_id"]) for item in items]
            snapshots, snapshot_failures = self._snapshot_documents(ids)
            if snapshot_failures:
                for doc_id, error in snapshot_failures.items():
                    self._record_item_failure(operation_id, doc_id, error, failures)
                ids = [doc_id for doc_id in ids if doc_id not in snapshot_failures]
            if not ids:
                continue

            succeeded, delete_failures = self._delete_documents(ids)
            succeeded_set = set(succeeded)
            for doc_id in ids:
                if doc_id in succeeded_set or doc_id in delete_failures:
                    continue
                try:
                    still_present = self.repository.contains(doc_id)
                except Exception as exc:
                    delete_failures[doc_id] = (
                        "Collection delete returned no status and presence could "
                        f"not be verified: {str(exc) or exc.__class__.__name__}"
                    )
                else:
                    if still_present:
                        delete_failures[doc_id] = (
                            "Collection delete returned no status for an existing "
                            "document."
                        )
                    else:
                        # A crash may occur after the Collection commit but before
                        # SQLite or the journal is updated.  Treat an exactly
                        # verified absence as the already-completed first half of
                        # the same saga and finish the local state transaction.
                        succeeded.append(doc_id)
                        succeeded_set.add(doc_id)
            for doc_id, error in delete_failures.items():
                self._record_item_failure(operation_id, doc_id, error, failures)
            if not succeeded:
                continue
            absent_before = [doc_id for doc_id in succeeded if doc_id not in snapshots]
            missing += len(absent_before)
            try:
                self._remove_state_documents(succeeded)
                remaining = self.state.get_many(succeeded)
                if remaining:
                    raise FolderDeletionError(
                        "SQLite retained deleted document rows: "
                        + ", ".join(sorted(remaining)[:10])
                    )
            except Exception as exc:
                restored, restore_failures = self._restore_documents(
                    snapshots[doc_id] for doc_id in succeeded if doc_id in snapshots
                )
                restored_set = set(restored)
                for doc_id in succeeded:
                    if doc_id not in snapshots:
                        needs_attention = True
                        error = (
                            "SQLite delete failed after the Collection document was "
                            "already absent; rollback was unavailable."
                        )
                    elif doc_id not in restored_set:
                        needs_attention = True
                        error = restore_failures.get(
                            doc_id, "Collection rollback did not restore the document."
                        )
                    else:
                        error = f"SQLite delete failed and was rolled back: {exc}"
                    self._record_item_failure(operation_id, doc_id, error, failures)
                break

            self.journal.mark_items(operation_id, succeeded, status="deleted")
            for doc_id in succeeded:
                item = next(item for item in items if item["doc_id"] == doc_id)
                staged = self._staged_file(operation, str(item["relative_path"]))
                try:
                    _unlink_staged_file(staged)
                except OSError as exc:
                    needs_attention = True
                    if len(failures) < _MAX_FAILURE_DETAILS:
                        failures.append(
                            {
                                "doc_id": doc_id,
                                "error": (
                                    "Database deleted; staged file cleanup failed: "
                                    f"{exc}"
                                ),
                            }
                        )
                deleted += 1
            selected_count = len(self.journal.items(operation_id))
            self.progress(f"Removed {deleted}/{selected_count} indexed images.")

        failed_items = self.journal.items(operation_id, statuses=("failed", "staged"))
        if failed_items:
            restore_errors = self._restore_payload(operation)
            if restore_errors:
                needs_attention = True
                failures.extend(
                    restore_errors[: max(0, _MAX_FAILURE_DETAILS - len(failures))]
                )
        else:
            self.journal.update_operation(operation_id, status="finalizing")
            purge_errors = self._purge_stage(operation)
            if purge_errors:
                needs_attention = True
                failures.extend(
                    purge_errors[: max(0, _MAX_FAILURE_DETAILS - len(failures))]
                )

        failed_count = len(failed_items)
        result = {
            "operation_id": operation_id,
            "status": (
                "needs_attention"
                if needs_attention
                else "partial"
                if failed_count
                else "committed"
            ),
            "selected": len(self.journal.items(operation_id)),
            "indexed_deleted": deleted,
            "physical_deleted": deleted,
            "missing": missing,
            "failed": failed_count + int(needs_attention and failed_count == 0),
            "failures": failures[:_MAX_FAILURE_DETAILS],
            "failures_truncated": len(failures) > _MAX_FAILURE_DETAILS,
            "needs_attention": needs_attention,
            "is_library_root": not bool(operation["relative_folder"]),
            "root_preserved": not bool(operation["relative_folder"]),
            "api_requests": 0,
        }
        final_status = str(result["status"])
        self.journal.update_operation(
            operation_id,
            status=final_status,
            result=result,
            error="; ".join(item["error"] for item in failures[:3]),
        )
        return result

    def _validate_snapshot(self, operation: Mapping[str, Any]) -> None:
        target = Path(str(operation["target_path"]))
        if self._protected_overlaps(target):
            raise FolderDeletionError(
                "The selected folder now overlaps protected data."
            )
        entries = self._scope_entries(
            str(operation["root_id"]), str(operation["relative_folder"])
        )
        tree = _scan_tree(target)
        if tree.reparse_points:
            raise FolderDeletionError(
                "The selected folder now contains a reparse point. Preview it again."
            )
        digest = _snapshot_digest(
            collection_uuid=str(self.repository.collection_uuid),
            root_id=str(operation["root_id"]),
            relative_folder=str(operation["relative_folder"]),
            entries=entries,
            tree=tree,
        )
        if digest != operation["snapshot_digest"]:
            raise FolderDeletionError(
                "The folder or its index changed after preview. Create a new preview."
            )

    def _stage(self, operation: Mapping[str, Any], *, recovering: bool) -> None:
        root = Path(str(operation["root_path"]))
        target = Path(str(operation["target_path"]))
        stage = Path(str(operation["stage_path"]))
        payload = stage / "payload"
        _ensure_owned_stage(stage, operation)
        if payload.exists():
            if not target.exists() and not operation["relative_folder"]:
                target.mkdir(parents=True, exist_ok=True)
            return
        if not target.exists():
            payload.mkdir(parents=True, exist_ok=True)
            return
        if _is_reparse(target):
            raise FolderDeletionError("The selected folder is a reparse point.")
        if target == root and target == target.parent:
            raise FolderDeletionError("A filesystem root cannot be staged.")
        try:
            target.rename(payload)
            if not operation["relative_folder"]:
                target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if recovering and payload.exists():
                if not target.exists() and not operation["relative_folder"]:
                    target.mkdir(parents=True, exist_ok=True)
                return
            raise FolderDeletionError(
                f"The selected folder could not be staged safely: {exc}"
            ) from exc

    def _restore_payload(self, operation: Mapping[str, Any]) -> list[dict[str, str]]:
        payload = Path(str(operation["stage_path"])) / "payload"
        target = Path(str(operation["target_path"]))
        if not payload.exists():
            return []
        target.mkdir(parents=True, exist_ok=True)
        failures: list[dict[str, str]] = []
        try:
            _merge_tree(payload, target)
        except OSError as exc:
            failures.append({"doc_id": "", "error": f"Restore failed: {exc}"})
        return failures

    def _purge_stage(self, operation: Mapping[str, Any]) -> list[dict[str, str]]:
        stage = Path(str(operation["stage_path"]))
        if not stage.exists():
            return []
        try:
            _delete_owned_stage(stage, operation)
        except OSError as exc:
            return [{"doc_id": "", "error": f"Staged folder cleanup failed: {exc}"}]
        return []

    def _staged_file(self, operation: Mapping[str, Any], relative_path: str) -> Path:
        inside = _relative_inside_target(
            relative_path, str(operation["relative_folder"])
        )
        return Path(str(operation["stage_path"])) / "payload" / Path(inside)

    def _scope_entries(
        self, root_id: str, relative_folder: str
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for chunk in self.state.iter_folder_entries(
            root_id,
            relative_folder,
            include_subfolders=True,
            chunk_size=500,
        ):
            entries.extend(dict(entry) for entry in chunk)
        return entries

    def _protected_overlaps(self, target: Path) -> list[str]:
        protected = {
            self.config.workspace.expanduser().resolve(),
            self.config.results_path.expanduser().resolve(),
            self.config.log_dir.expanduser().resolve(),
            _default_config_home(),
            self.journal.path,
        }
        overlaps = []
        for path in protected:
            if _paths_overlap(target, path):
                overlaps.append(str(path))
        return sorted(overlaps)

    def _operation(self, operation_id: str) -> dict[str, Any]:
        normalized = str(operation_id).strip().lower()
        if len(normalized) != 32 or any(
            c not in "0123456789abcdef" for c in normalized
        ):
            raise FolderDeletionError("The folder deletion operation id is invalid.")
        operation = self.journal.operation(normalized)
        if operation is None:
            raise FolderDeletionError("The folder deletion preview does not exist.")
        return operation

    def _snapshot_documents(
        self, ids: Sequence[str]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        method = getattr(self.repository, "snapshot_documents", None)
        if not callable(method):
            return {}, {}
        snapshots, failures = method(ids)
        return dict(snapshots), dict(failures)

    def _delete_documents(self, ids: Sequence[str]) -> tuple[list[str], dict[str, str]]:
        method = getattr(self.repository, "delete_resilient", None)
        if callable(method):
            succeeded, failures = method(ids)
            return list(succeeded), dict(failures)
        try:
            succeeded, failures = self.repository.delete(ids)
            return list(succeeded), dict(failures)
        except Exception:
            succeeded = []
            failures = {}
            for doc_id in ids:
                try:
                    deleted, failed = self.repository.delete([doc_id])
                    succeeded.extend(deleted)
                    failures.update(failed)
                except Exception as exc:
                    failures[doc_id] = str(exc) or exc.__class__.__name__
            return succeeded, failures

    def _restore_documents(
        self, documents: Iterable[Any]
    ) -> tuple[list[str], dict[str, str]]:
        items = list(documents)
        if not items:
            return [], {}
        method = getattr(self.repository, "restore_documents", None)
        if not callable(method):
            return [], {
                str(getattr(document, "id", "")): "Collection rollback is unavailable."
                for document in items
            }
        succeeded, failures = method(items)
        return list(succeeded), dict(failures)

    def _remove_state_documents(self, ids: Sequence[str]) -> None:
        method = getattr(self.state, "remove_many_for_folder_delete", None)
        if callable(method):
            method(ids)
        else:
            self.state.remove_many(ids)

    def _record_item_failure(
        self,
        operation_id: str,
        doc_id: str,
        error: str,
        failures: list[dict[str, str]],
    ) -> None:
        self.journal.mark_items(operation_id, [doc_id], status="failed", error=error)
        if len(failures) < _MAX_FAILURE_DETAILS:
            failures.append({"doc_id": doc_id, "error": str(error)})


def _folder_target(root: Path, relative_folder: str) -> Path:
    normalized = str(relative_folder).replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts if normalized else ()
    if any(part in {"", ".", ".."} for part in parts):
        raise FolderDeletionError("The selected folder path is unsafe.")
    candidate = Path(os.path.abspath(root.joinpath(*parts)))
    try:
        if os.path.commonpath([_path_key(root), _path_key(candidate)]) != _path_key(
            root
        ):
            raise FolderDeletionError(
                "The selected folder escapes its registered root."
            )
    except ValueError as exc:
        raise FolderDeletionError(
            "The selected folder is on another filesystem."
        ) from exc
    return candidate


def _stage_path(root: Path, root_id: str, operation_id: str) -> Path:
    root_digest = hashlib.sha256(str(root_id).encode("utf-8")).hexdigest()[:12]
    return root.parent / f".zvec-folder-trash-{root_digest}" / operation_id


def _scan_tree(target: Path) -> _TreeSnapshot:
    if not target.exists():
        return _TreeSnapshot((), 0, 0, 0, ())
    if _is_reparse(target):
        return _TreeSnapshot((), 0, 0, 0, (".",))
    entries: list[_PhysicalEntry] = []
    reparse: list[str] = []
    stack: list[tuple[Path, str]] = [(target, "")]
    while stack:
        directory, relative_directory = stack.pop()
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: item.name.casefold()
            )
        except OSError as exc:
            raise FolderDeletionError(f"Cannot enumerate {directory}: {exc}") from exc
        for child in children:
            relative = (
                f"{relative_directory}/{child.name}"
                if relative_directory
                else child.name
            ).replace("\\", "/")
            path = Path(child.path)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise FolderDeletionError(f"Cannot inspect {path}: {exc}") from exc
            if _stat_is_reparse(path, metadata):
                reparse.append(relative)
                continue
            is_directory = stat.S_ISDIR(metadata.st_mode)
            entries.append(
                _PhysicalEntry(
                    relative_path=relative,
                    size_bytes=0 if is_directory else int(metadata.st_size),
                    mtime_ns=int(metadata.st_mtime_ns),
                    is_directory=is_directory,
                )
            )
            if is_directory:
                stack.append((path, relative))
    return _TreeSnapshot(
        entries=tuple(sorted(entries, key=lambda item: item.relative_path.casefold())),
        file_count=sum(not item.is_directory for item in entries),
        directory_count=sum(item.is_directory for item in entries),
        size_bytes=sum(item.size_bytes for item in entries),
        reparse_points=tuple(sorted(reparse)),
    )


def _snapshot_digest(
    *,
    collection_uuid: str,
    root_id: str,
    relative_folder: str,
    entries: Sequence[Mapping[str, Any]],
    tree: _TreeSnapshot,
) -> str:
    digest = hashlib.sha256()
    for value in (collection_uuid, root_id, relative_folder):
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    for entry in sorted(entries, key=lambda item: str(item["doc_id"])):
        for key in ("doc_id", "relative_path", "sha256", "size_bytes", "mtime_ns"):
            digest.update(str(entry.get(key, "")).encode("utf-8"))
            digest.update(b"\0")
    for physical_entry in tree.entries:
        digest.update(physical_entry.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(physical_entry.size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(physical_entry.mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(b"d" if physical_entry.is_directory else b"f")
    for path in tree.reparse_points:
        digest.update(b"r")
        digest.update(path.encode("utf-8"))
    return digest.hexdigest()


def _relative_inside_target(relative_path: str, relative_folder: str) -> str:
    path_parts = PurePosixPath(str(relative_path).replace("\\", "/")).parts
    folder_parts = (
        PurePosixPath(str(relative_folder).replace("\\", "/")).parts
        if relative_folder
        else ()
    )
    if path_parts[: len(folder_parts)] != folder_parts:
        raise FolderDeletionError("An indexed image is outside the selected folder.")
    remainder = path_parts[len(folder_parts) :]
    if not remainder or any(part in {"", ".", ".."} for part in remainder):
        raise FolderDeletionError("An indexed image has an unsafe relative path.")
    return "/".join(remainder)


def _ensure_owned_stage(stage: Path, operation: Mapping[str, Any]) -> None:
    container = stage.parent
    container.mkdir(parents=True, exist_ok=True)
    owner_path = container / _OWNER_FILE
    expected = {
        "schema_version": 1,
        "collection_uuid": str(operation["collection_uuid"]),
        "root_id": str(operation["root_id"]),
    }
    if owner_path.exists():
        try:
            current = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FolderDeletionError(
                "The folder trash ownership marker is invalid."
            ) from exc
        if current != expected:
            raise FolderDeletionError("The folder trash belongs to another Collection.")
    else:
        unknown = [path for path in container.iterdir() if path.name != _OWNER_FILE]
        if unknown:
            raise FolderDeletionError("The folder trash contains unowned content.")
        temporary = owner_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(expected, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(owner_path)
    stage.mkdir(parents=False, exist_ok=True)
    operation_marker = stage / _OWNER_FILE
    marker = {**expected, "operation_id": str(operation["operation_id"])}
    if operation_marker.exists():
        current = json.loads(operation_marker.read_text(encoding="utf-8"))
        if current != marker:
            raise FolderDeletionError("The staged operation marker is invalid.")
    else:
        operation_marker.write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )


def _delete_owned_stage(stage: Path, operation: Mapping[str, Any]) -> None:
    marker = stage / _OWNER_FILE
    expected_id = str(operation["operation_id"])
    if not marker.is_file():
        raise FolderDeletionError("Refusing to delete an unowned staged directory.")
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data.get("operation_id") != expected_id:
        raise FolderDeletionError("The staged directory belongs to another operation.")
    _delete_tree_no_follow(stage)


def _delete_tree_no_follow(root: Path) -> None:
    if _is_reparse(root):
        root.unlink()
        return
    directories: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        directories.append(directory)
        for child in os.scandir(directory):
            path = Path(child.path)
            metadata = child.stat(follow_symlinks=False)
            if _stat_is_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
                path.unlink()
            else:
                stack.append(path)
    for directory in reversed(directories):
        directory.rmdir()


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        target = destination / child.name
        if _is_reparse(child):
            raise OSError(f"Refusing to restore a reparse point: {child}")
        if child.is_dir():
            if target.exists() and not target.is_dir():
                raise OSError(f"Restore target conflicts with a file: {target}")
            _merge_tree(child, target)
            child.rmdir()
        else:
            if target.exists():
                raise OSError(f"Restore target already exists: {target}")
            child.rename(target)
    source.rmdir()


def _unlink_staged_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _stat_is_reparse(path, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"Staged image is not a regular file: {path}")
    path.unlink()
    parent = path.parent
    while parent.name != "payload":
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_key = _path_key(left)
        right_key = _path_key(right)
        common = os.path.commonpath([left_key, right_key])
    except ValueError:
        return False
    return common in {left_key, right_key}


def _default_config_home() -> Path:
    """Resolve the complete desktop config/cache root without desktop imports."""

    configured = os.getenv("ZVEC_CONFIG_HOME") or os.getenv("ZVEC_DOCKER_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"], "zvec-image-search").resolve()
    return Path.home().joinpath(".zvec-image-search").resolve()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_reparse(path: Path) -> bool:
    try:
        return _stat_is_reparse(path, path.lstat())
    except FileNotFoundError:
        return False


def _stat_is_reparse(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if attributes & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise FolderDeletionError(f"Invalid folder deletion {label} JSON.") from exc
    if not isinstance(decoded, dict):
        raise FolderDeletionError(f"Invalid folder deletion {label} JSON.")
    return decoded
