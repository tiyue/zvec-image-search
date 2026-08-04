"""High-level service orchestration for the ARW selection module.

Ties together the database, importer, decoder, cache, exporter and deleter
into a single service that the WebView gateway calls. Project CRUD, import
scheduling, rating, workspace state, export and permanent delete all flow
through this service.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import CancelledError, Future
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import DerivedCache
from .db import (
    VALID_COLOR_LABELS,
    VALID_FILTER_EXPORTED,
    VALID_FILTER_FORMATS,
    VALID_FILTER_ORIENTATIONS,
    VALID_FILTER_RATED,
    VALID_FILTER_STAR_MODES,
    VALID_SORT_DIRECTIONS,
    VALID_SORT_FIELDS,
    VALID_STAR_RATINGS,
    MemberRecord,
    OperationLogItem,
    OperationTarget,
    ProjectSummary,
    RawSelectionDB,
    WorkspaceState,
)
from .decoder import (
    DecodeResult,
    decode_full_base,
    decode_preview,
    decode_thumbnail,
    image_to_jpeg_bytes,
    image_to_png_bytes,
    is_rawpy_available,
    render_full_decode_with_look,
)
from .importer import (
    SUPPORTED_EXTENSIONS,
    AssetImporter,
    ImportResult,
    _is_reparse_point,
    normalize_path,
    source_file_identity,
    unsupported_arw_camera,
)
from .jobs import RawSelectionJob, RawSelectionJobRegistry
from .scheduler import (
    DecodeScheduler,
    GenerationToken,
    SchedulerQueueFull,
    StaleGeneration,
    TaskPriority,
)

_CACHE_DIR_NAME = "cache"
_DB_NAME = "projects.sqlite3"
_FULL_BASE_CACHE_CAPACITY = 2


@dataclass(frozen=True, slots=True)
class MemberListResult:
    members: list[MemberRecord]
    total: int
    filtered: int


@dataclass(frozen=True, slots=True)
class DeleteResult:
    deleted: int
    already_missing: int
    failed: int
    error_details: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExportResult:
    exported: int
    skipped: int
    failed: int
    cancelled: int
    error_details: tuple[str, ...]


@dataclass(slots=True)
class _FullBaseEntry:
    key: tuple[object, ...]
    result: DecodeResult
    users: int = 0
    evicted: bool = False


@dataclass(slots=True)
class _FullBasePending:
    future: Future[_FullBaseEntry]
    waiters: int = 1


class RawSelectionService:
    """Service facade for all ARW selection operations.

    All public methods are thread-safe via the underlying DB's RLock.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_dir / _DB_NAME
        self._cache_dir = self.data_dir / _CACHE_DIR_NAME
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._db = RawSelectionDB(self._db_path)
        self._db.reset_unavailable_creative_looks()
        self._importer = AssetImporter(self._db)
        self._derived_cache = DerivedCache(self._cache_dir)
        self._scheduler = DecodeScheduler()
        self._jobs = RawSelectionJobRegistry()
        self._lock = threading.RLock()
        self._export_lock = threading.Lock()
        self._full_base_lock = threading.RLock()
        self._full_bases: OrderedDict[tuple[object, ...], _FullBaseEntry] = (
            OrderedDict()
        )
        self._full_base_pending: dict[tuple[object, ...], _FullBasePending] = {}
        self._closed = False

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._jobs.close()
            self._importer.close()
            try:
                self._scheduler.shutdown(wait=True, cancel_queued=True)
            finally:
                self._close_full_bases()
                self._db.close()

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def create_project(self, name: str) -> dict[str, Any]:
        p = self._db.create_project(name)
        return _project_to_dict(p)

    def list_projects(self) -> list[dict[str, Any]]:
        return [self._project_with_cover(p) for p in self._db.list_projects()]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        p = self._db.get_project(project_id)
        if p is None:
            return None
        return self._project_with_cover(p)

    def _project_with_cover(self, project: ProjectSummary) -> dict[str, Any]:
        payload = _project_to_dict(project)
        payload["cover_member_ids"] = [
            member.id for member in self._db.list_members(project.id, offset=0, limit=4)
        ]
        return payload

    def rename_project(self, project_id: str, name: str) -> dict[str, Any] | None:
        p = self._db.rename_project(project_id, name)
        if p is None:
            return None
        return _project_to_dict(p)

    def delete_project(self, project_id: str) -> bool:
        if self._db.get_project(project_id) is None:
            return False
        self._jobs.cancel_project(project_id)
        source_paths = {
            member.normalized_path
            for member in self._db.list_members(project_id, offset=0, limit=100_000)
        }
        self._scheduler.bump_generation(project_id, cancel_queued=True)
        deleted = self._db.delete_project(project_id)
        self._scheduler.bump_generation(project_id, cancel_queued=True)
        errors: list[str] = []
        for normalized_path in sorted(source_paths):
            if self._db.get_members_by_asset_path(normalized_path):
                continue
            self._evict_full_base_sources({normalized_path})
            errors.extend(self._derived_cache.clear_source(normalized_path).errors)
            self._db.delete_asset_by_path(normalized_path)
        if errors:
            raise OSError(f"Derived cache clear failed: {'; '.join(errors)}")
        return deleted

    def cancel_project_work(self, project_id: str) -> None:
        """Cancel queued work and make running results stale after a project switch."""
        self._scheduler.cancel_scope(project_id)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_files(
        self,
        project_id: str,
        file_paths: Sequence[str | Path],
    ) -> dict[str, Any]:
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        log_id = self._db.create_operation_log(
            "import",
            (),
            payload_json=json.dumps(
                {"project_id": project_id, "kind": "files", "count": len(file_paths)},
                separators=(",", ":"),
            ),
        )
        self._db.set_operation_log_status(log_id, "in_progress")
        try:
            result = self._importer.import_files(project_id, file_paths)
        except Exception:
            self._db.set_operation_log_status(log_id, "failed")
            raise
        self._db.set_operation_log_status(log_id, "completed")
        payload = _import_result_to_dict(result)
        payload["log_id"] = log_id
        return payload

    def import_folder(
        self,
        project_id: str,
        folder_path: str | Path,
    ) -> dict[str, Any]:
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        log_id = self._db.create_operation_log(
            "import",
            (),
            payload_json=json.dumps(
                {"project_id": project_id, "kind": "folder", "path": str(folder_path)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self._db.set_operation_log_status(log_id, "in_progress")
        try:
            result = self._importer.import_folder(project_id, folder_path)
        except Exception:
            self._db.set_operation_log_status(log_id, "failed")
            raise
        self._db.set_operation_log_status(log_id, "completed")
        payload = _import_result_to_dict(result)
        payload["log_id"] = log_id
        return payload

    def import_folder_async(
        self,
        project_id: str,
        folder_path: str | Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ImportResult:
        """Import folder in a background-friendly manner with progress."""
        return self._importer.import_folder_incremental(
            project_id,
            folder_path,
            on_batch=on_progress,
        )

    def start_folder_import(
        self,
        project_id: str,
        folder_path: str | Path,
    ) -> dict[str, Any]:
        """Start a progressive folder import and background thumbnail pass."""

        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        requested_path = str(Path(folder_path).expanduser())
        log_id = self._db.create_operation_log(
            "import",
            (),
            payload_json=json.dumps(
                {"project_id": project_id, "path": requested_path},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        def worker(job: RawSelectionJob) -> None:
            self._db.set_operation_log_status(log_id, "in_progress")
            job.mark_running("scanning")
            try:
                result = self._importer.import_folder_incremental(
                    project_id,
                    folder_path,
                    on_batch=lambda registered, seen: job.update_progress(
                        "scanning",
                        {"registered": registered, "seen": seen},
                    ),
                    cancel_event=job.cancel_event,
                )
                payload = _import_result_to_dict(result)
                if job.cancel_event.is_set():
                    self._db.set_operation_log_status(log_id, "cancelled")
                    job.finish(payload)
                    return

                members = self._db.list_members(
                    project_id,
                    offset=0,
                    limit=100_000,
                )
                completed = 0
                failed = 0
                job.update_progress(
                    "thumbnails",
                    {
                        "registered": result.registered,
                        "thumbnails_total": len(members),
                        "thumbnails_completed": 0,
                        "thumbnails_failed": 0,
                    },
                )
                for member in members:
                    if job.cancel_event.is_set():
                        break
                    thumbnail = self.get_thumbnail_bytes(
                        member.id,
                        priority=TaskPriority.BACKGROUND,
                    )
                    if thumbnail.error is None:
                        completed += 1
                    else:
                        failed += 1
                    job.update_progress(
                        "thumbnails",
                        {
                            "thumbnails_completed": completed,
                            "thumbnails_failed": failed,
                        },
                    )
                payload["thumbnails_completed"] = completed
                payload["thumbnails_failed"] = failed
                self._db.set_operation_log_status(
                    log_id,
                    "cancelled" if job.cancel_event.is_set() else "completed",
                )
                job.finish(payload)
            except Exception:
                self._db.set_operation_log_status(log_id, "failed")
                raise

        job = self._jobs.start(
            kind="import",
            project_id=project_id,
            log_id=log_id,
            worker=worker,
        )
        return job.snapshot()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return None if job is None else job.snapshot()

    def cancel_job(self, job_id: str) -> bool | None:
        return self._jobs.cancel(job_id)

    def wait_for_metadata(self, timeout: float = 5.0) -> bool:
        """Wait for the lightweight metadata worker; intended for tests/diagnostics."""
        return self._importer.wait_for_metadata(timeout)

    # ------------------------------------------------------------------
    # Members listing & filtering
    # ------------------------------------------------------------------

    def list_members(
        self,
        project_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
        star_mode: str = "none",
        star_value: int = 0,
        color_labels: str = "",
        filename_contains: str = "",
        rated_filter: str = "all",
        exported_filter: str = "all",
        formats: str = "",
        orientations: str = "",
        sort_field: str = "filename",
        sort_direction: str = "asc",
    ) -> dict[str, Any]:
        """List project members with optional filtering and sorting.

        Filter and sort parameters come from the workspace state saved by
        the frontend.
        """
        _validate_member_query(
            offset=offset,
            limit=limit,
            star_mode=star_mode,
            star_value=star_value,
            color_labels=color_labels,
            filename_contains=filename_contains,
            rated_filter=rated_filter,
            exported_filter=exported_filter,
            formats=formats,
            orientations=orientations,
            sort_field=sort_field,
            sort_direction=sort_direction,
        )
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")

        # For now, fetch all members and apply filtering/sorting in Python.
        # This is fine for the initial implementation; the DB can be
        # optimized later with proper SQL queries.
        all_members = self._db.list_members(project_id, offset=0, limit=100_000)
        total = len(all_members)

        # Apply filtering
        filtered = _apply_filters(
            all_members,
            star_mode=star_mode,
            star_value=star_value,
            color_labels=color_labels,
            filename_contains=filename_contains,
            rated_filter=rated_filter,
            exported_filter=exported_filter,
            formats=formats,
            orientations=orientations,
        )

        # Apply sorting
        _sort_members(filtered, sort_field, sort_direction)

        # Paginate
        page = filtered[offset : offset + limit]

        return {
            "members": [_member_to_dict(m) for m in page],
            "total": total,
            "filtered": len(filtered),
        }

    def get_member(self, member_id: str) -> dict[str, Any] | None:
        m = self._db.get_member(member_id)
        if m is None:
            return None
        return _member_to_dict(m)

    def get_member_count(self, project_id: str) -> int:
        return self._db.count_members(project_id)

    # ------------------------------------------------------------------
    # Rating & creative look
    # ------------------------------------------------------------------

    def update_rating(
        self,
        member_id: str,
        star_rating: int,
        color_label: str,
    ) -> bool:
        return self._db.update_rating(member_id, star_rating, color_label)

    def update_creative_look(
        self,
        member_id: str,
        creative_look: str,
    ) -> bool:
        from .creative_look import DEFAULT_LOOK, is_valid_look

        if not isinstance(creative_look, str) or not is_valid_look(creative_look):
            raise ValueError("Invalid creative look")
        member = self._db.get_member(member_id)
        if member is None:
            return False
        if member.extension.casefold() != ".arw" and creative_look != DEFAULT_LOOK:
            raise ValueError("Creative looks only apply to ARW members")
        return self._db.update_creative_look(member_id, creative_look)

    # ------------------------------------------------------------------
    # Workspace state
    # ------------------------------------------------------------------

    def get_workspace_state(self, project_id: str) -> dict[str, Any] | None:
        ws = self._db.get_workspace_state(project_id)
        if ws is None:
            return None
        return _workspace_state_to_dict(ws)

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
        self._db.save_workspace_state(
            project_id,
            last_member_id=last_member_id,
            filter_star_mode=filter_star_mode,
            filter_star_value=filter_star_value,
            filter_color_labels=filter_color_labels,
            filter_filename=filter_filename,
            filter_rated=filter_rated,
            filter_exported=filter_exported,
            filter_formats=filter_formats,
            filter_orientations=filter_orientations,
            sort_field=sort_field,
            sort_direction=sort_direction,
            filmstrip_scroll=filmstrip_scroll,
        )

    # ------------------------------------------------------------------
    # Remove from project
    # ------------------------------------------------------------------

    def remove_members(self, member_ids: Sequence[str]) -> int:
        """Remove members from a project. Does NOT delete source files."""
        members = [
            member
            for member_id in member_ids
            if (member := self._db.get_member(member_id)) is not None
        ]
        removed = self._db.remove_members(member_ids)
        errors: list[str] = []
        for normalized_path in sorted({member.normalized_path for member in members}):
            if self._db.get_members_by_asset_path(normalized_path):
                continue
            self._evict_full_base_sources({normalized_path})
            errors.extend(self._derived_cache.clear_source(normalized_path).errors)
            self._db.delete_asset_by_path(normalized_path)
        if errors:
            raise OSError(f"Derived cache clear failed: {'; '.join(errors)}")
        return removed

    # ------------------------------------------------------------------
    # Permanent delete (two-phase)
    # ------------------------------------------------------------------

    def permanent_delete(
        self,
        member_ids: Sequence[str],
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Permanently delete source files with two-phase safety.

        Phase 1: Persist target list and confirmation state.
        Phase 2: Delete each file, then clean up references.
        """
        if not confirmed:
            raise ValueError("Permanent delete requires explicit confirmation.")

        if not member_ids:
            raise ValueError("member_ids must not be empty")
        if any(
            not isinstance(member_id, str) or not member_id for member_id in member_ids
        ):
            raise ValueError("member_ids must contain non-empty strings")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("member_ids must not contain duplicates")

        # Gather exact source-version targets from DB.
        targets: list[OperationTarget] = []
        seen_paths: set[str] = set()
        for mid in member_ids:
            member = self._db.get_member(mid)
            if member is None:
                raise ValueError(f"Member not found: {mid}")
            norm = member.normalized_path
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            identity = member.file_identity
            if identity is None:
                try:
                    current_stat = os.lstat(norm)
                except OSError:
                    current_stat = None
                if (
                    current_stat is not None
                    and int(current_stat.st_size) == member.file_size
                    and int(current_stat.st_mtime_ns) == member.mtime_ns
                ):
                    identity = source_file_identity(current_stat)
                    if identity is not None:
                        self._db.set_asset_identity_if_missing(
                            member.asset_id,
                            file_size=member.file_size,
                            mtime_ns=member.mtime_ns,
                            file_identity=identity,
                        )
            targets.append(
                OperationTarget(
                    asset_id=member.asset_id,
                    normalized_path=norm,
                    source_file_size=member.file_size,
                    source_mtime_ns=member.mtime_ns,
                    source_file_identity=identity,
                    source_extension=member.extension,
                )
            )

        if not targets:
            return {
                "deleted": 0,
                "already_missing": 0,
                "failed": 0,
                "details": [],
            }

        # Phase 1: Create operation log
        log_id = self._db.create_operation_log(
            "permanent_delete",
            targets,
            payload_json='{"confirmed": true}',
        )
        self._db.set_operation_log_status(log_id, "in_progress")
        return self._execute_delete_log(log_id)

    def _execute_delete_log(self, log_id: str) -> dict[str, Any]:
        deleted = 0
        already_missing = 0
        failed = 0
        details: list[dict[str, str]] = []
        for item in self._db.get_operation_log_items(log_id):
            if item.result in {"deleted", "already_missing"}:
                self._cleanup_deleted_source(item.normalized_path)
                result = item.result
                error_msg = None
            elif item.result == "failed":
                result = "failed"
                error_msg = item.error_message or "Delete failed"
            elif item.result == "pending":
                result, error_msg = self._execute_delete_item(item)
            else:
                result = "failed"
                error_msg = f"Unexpected delete item state: {item.result}"
                self._db.update_operation_item(item.id, "failed", error_msg)

            detail = {"path": item.normalized_path, "result": result}
            if result == "deleted":
                deleted += 1
            elif result == "already_missing":
                already_missing += 1
            else:
                failed += 1
                detail["error"] = error_msg or "Delete failed"
            details.append(detail)

        self._db.set_operation_log_status(log_id, "completed")
        return {
            "deleted": deleted,
            "already_missing": already_missing,
            "failed": failed,
            "details": details,
            "log_id": log_id,
        }

    def _execute_delete_item(self, item: OperationLogItem) -> tuple[str, str | None]:
        try:
            current_stat = os.lstat(item.normalized_path)
        except FileNotFoundError:
            self._db.update_operation_item(item.id, "already_missing")
            self._cleanup_deleted_source(item.normalized_path)
            return "already_missing", None
        except OSError as exc:
            error = str(exc)
            self._db.update_operation_item(item.id, "failed", error)
            return "failed", error

        safety_error = _delete_target_error(item, current_stat)
        if safety_error is not None:
            self._db.update_operation_item(item.id, "failed", safety_error)
            return "failed", safety_error

        try:
            final_stat = os.lstat(item.normalized_path)
            safety_error = _delete_target_error(item, final_stat)
            if safety_error is not None:
                self._db.update_operation_item(item.id, "failed", safety_error)
                return "failed", safety_error
            os.unlink(item.normalized_path)
        except FileNotFoundError:
            self._db.update_operation_item(item.id, "already_missing")
            self._cleanup_deleted_source(item.normalized_path)
            return "already_missing", None
        except OSError as exc:
            error = str(exc)
            self._db.update_operation_item(item.id, "failed", error)
            return "failed", error

        # Persist physical deletion before reference cleanup so recovery is idempotent.
        self._db.update_operation_item(item.id, "deleted")
        self._cleanup_deleted_source(item.normalized_path)
        return "deleted", None

    def _cleanup_deleted_source(self, normalized_path: str) -> None:
        members = self._db.get_members_by_asset_path(normalized_path)
        for project_id in sorted({member.project_id for member in members}):
            self._scheduler.bump_generation(project_id, cancel_queued=True)
        self._evict_full_base_sources({normalized_path})
        clear_result = self._derived_cache.clear_source(normalized_path)
        self._db.remove_members_by_asset_path(normalized_path)
        self._db.delete_asset_by_path(normalized_path)
        if clear_result.errors:
            raise OSError(
                f"Derived cache clear failed: {'; '.join(clear_result.errors)}"
            )

    def recover_pending_deletes(self) -> int:
        """Recover from interrupted permanent-delete operations.

        Called on startup to complete reference cleanup for files that
        were physically deleted but whose DB references were not yet cleaned.
        """
        recovered = 0
        pending_logs = self._db.get_pending_operation_logs()
        for log_entry in pending_logs:
            if log_entry.operation_type != "permanent_delete":
                # Import/export worker threads cannot survive a process restart.
                # Mark their durable records as interrupted instead of leaving
                # an operation indefinitely in progress or replaying file I/O.
                self._db.set_operation_log_status(log_entry.id, "failed")
                continue
            items = self._db.get_operation_log_items(log_entry.id)
            for item in items:
                if item.result == "pending":
                    result, _error = self._execute_delete_item(item)
                    if result in {"deleted", "already_missing"}:
                        recovered += 1
                elif item.result in {"deleted", "already_missing"}:
                    self._cleanup_deleted_source(item.normalized_path)
                    recovered += 1
            remaining = self._db.get_operation_log_items(log_entry.id)
            if not any(item.result == "pending" for item in remaining):
                self._db.set_operation_log_status(log_entry.id, "completed")
        return recovered

    # ------------------------------------------------------------------
    # Export (source file copy)
    # ------------------------------------------------------------------

    def export_files(
        self,
        project_id: str,
        member_ids: Sequence[str],
        destination: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[dict[str, int]], None] | None = None,
        operation_log_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy source files to a destination directory.

        ARW files are copied as raw bytes; JPG/PNG keep original format.
        No re-encoding, no metadata modification.
        """
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        if not member_ids:
            raise ValueError("member_ids must not be empty")
        if any(
            not isinstance(member_id, str) or not member_id for member_id in member_ids
        ):
            raise ValueError("member_ids must contain non-empty strings")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("member_ids must not contain duplicates")
        members: list[MemberRecord] = []
        for member_id in member_ids:
            member = self._db.get_member_for_project(project_id, member_id)
            if member is None:
                raise ValueError("Every member must belong to the export project")
            members.append(member)

        dest = Path(destination).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        if not dest.is_dir():
            raise ValueError("Export destination must be a directory")

        if operation_log_id is None:
            operation_log_id = self._db.create_operation_log(
                "export",
                tuple(
                    OperationTarget(
                        asset_id=member.asset_id,
                        normalized_path=member.normalized_path,
                        source_file_size=member.file_size,
                        source_mtime_ns=member.mtime_ns,
                        source_file_identity=member.file_identity,
                        source_extension=member.extension,
                    )
                    for member in members
                ),
                payload_json=json.dumps(
                    {"project_id": project_id, "destination": str(dest)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        log_items = {
            item.asset_id: item
            for item in self._db.get_operation_log_items(operation_log_id)
        }
        self._db.set_operation_log_status(operation_log_id, "in_progress")

        exported = 0
        skipped = 0
        failed = 0
        cancelled = 0
        details: list[dict[str, str]] = []

        def record_progress(
            member: MemberRecord,
            result: str,
            error: str | None = None,
        ) -> None:
            item = log_items.get(member.asset_id)
            if item is not None:
                log_result = (
                    result
                    if result in {"exported", "cancelled", "failed"}
                    else "failed"
                )
                self._db.update_operation_item(item.id, log_result, error)
            if on_progress is not None:
                on_progress(
                    {
                        "total": len(members),
                        "completed": exported + skipped + failed + cancelled,
                        "exported": exported,
                        "skipped": skipped,
                        "failed": failed,
                        "cancelled": cancelled,
                    }
                )

        with self._export_lock:
            for member in members:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "cancelled",
                        }
                    )
                    record_progress(member, "cancelled")
                    continue

                src = Path(member.normalized_path)
                try:
                    source_stat = os.lstat(src)
                except FileNotFoundError:
                    skipped += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "skipped",
                            "error": "Source file missing",
                        }
                    )
                    record_progress(member, "skipped", "Source file missing")
                    continue
                except OSError as exc:
                    failed += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "failed",
                            "error": str(exc),
                        }
                    )
                    record_progress(member, "failed", str(exc))
                    continue

                source_error = _member_source_error(member, source_stat)
                if source_error is not None:
                    failed += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "failed",
                            "error": source_error,
                        }
                    )
                    record_progress(member, "failed", source_error)
                    continue

                try:
                    dest_path = _copy_source_exact(
                        src,
                        dest,
                        member,
                        cancel_event=cancel_event,
                    )
                    if not self._db.mark_member_exported(
                        project_id,
                        member.id,
                        asset_id=member.asset_id,
                        file_size=member.file_size,
                        mtime_ns=member.mtime_ns,
                        file_identity=member.file_identity,
                    ):
                        with suppress(OSError):
                            dest_path.unlink()
                        raise OSError("Project member changed before export completed")
                    exported += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "dest": str(dest_path),
                            "result": "exported",
                            "member_id": member.id,
                        }
                    )
                    record_progress(member, "exported")
                except _ExportCancelled:
                    cancelled += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "cancelled",
                        }
                    )
                    record_progress(member, "cancelled")
                except OSError as exc:
                    failed += 1
                    details.append(
                        {
                            "path": member.normalized_path,
                            "result": "failed",
                            "error": str(exc),
                        }
                    )
                    record_progress(member, "failed", str(exc))

        self._db.set_operation_log_status(
            operation_log_id,
            "cancelled" if cancelled else "completed",
        )
        return {
            "exported": exported,
            "skipped": skipped,
            "failed": failed,
            "cancelled": cancelled,
            "details": details,
            "log_id": operation_log_id,
        }

    def start_export(
        self,
        project_id: str,
        member_ids: Sequence[str],
        destination: str | Path,
    ) -> dict[str, Any]:
        """Start a cancellable export job and return its initial snapshot."""

        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        if not member_ids or len(member_ids) != len(set(member_ids)):
            raise ValueError("member_ids must be non-empty and unique")
        members: list[MemberRecord] = []
        for member_id in member_ids:
            if not isinstance(member_id, str) or not member_id:
                raise ValueError("member_ids must contain non-empty strings")
            member = self._db.get_member_for_project(project_id, member_id)
            if member is None:
                raise ValueError("Every member must belong to the export project")
            members.append(member)
        dest = Path(destination).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        if not dest.is_dir():
            raise ValueError("Export destination must be a directory")
        log_id = self._db.create_operation_log(
            "export",
            tuple(
                OperationTarget(
                    asset_id=member.asset_id,
                    normalized_path=member.normalized_path,
                    source_file_size=member.file_size,
                    source_mtime_ns=member.mtime_ns,
                    source_file_identity=member.file_identity,
                    source_extension=member.extension,
                )
                for member in members
            ),
            payload_json=json.dumps(
                {"project_id": project_id, "destination": str(dest)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        def worker(job: RawSelectionJob) -> None:
            job.mark_running("copying")
            job.update_progress(
                "copying",
                {
                    "total": len(members),
                    "completed": 0,
                    "exported": 0,
                    "skipped": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
            )
            try:
                result = self.export_files(
                    project_id,
                    member_ids,
                    dest,
                    cancel_event=job.cancel_event,
                    on_progress=lambda progress: job.update_progress(
                        "copying", progress
                    ),
                    operation_log_id=log_id,
                )
                job.finish(result)
            except Exception:
                self._db.set_operation_log_status(log_id, "failed")
                raise

        job = self._jobs.start(
            kind="export",
            project_id=project_id,
            log_id=log_id,
            worker=worker,
        )
        return job.snapshot()

    # ------------------------------------------------------------------
    # Clear project cache
    # ------------------------------------------------------------------

    def clear_project_cache(self, project_id: str) -> int:
        """Delete all derived cache for a project.

        Only deletes derived caches (thumbnails, previews, full decodes).
        Preserves project references, ratings, color labels and creative
        look selections.
        """
        if self._db.get_project(project_id) is None:
            raise ValueError("Project not found")
        return self._invalidate_project_sources(project_id)

    def _invalidate_project_sources(self, project_id: str) -> int:
        members = self._db.list_members(project_id, offset=0, limit=100_000)
        self._scheduler.bump_generation(project_id, cancel_queued=True)
        source_paths = {member.normalized_path for member in members}
        self._evict_full_base_sources(source_paths)
        removed = 0
        errors: list[str] = []
        for normalized_path in sorted(source_paths):
            result = self._derived_cache.clear_source(normalized_path)
            removed += result.removed
            errors.extend(result.errors)
        if errors:
            raise OSError(f"Derived cache clear failed: {'; '.join(errors)}")
        return removed

    # ------------------------------------------------------------------
    # Source file status
    # ------------------------------------------------------------------

    def check_source_exists(self, member_id: str) -> bool:
        """Check if the source file still exists on disk."""
        member = self._db.get_member(member_id)
        if member is None:
            return False
        return os.path.isfile(member.normalized_path)

    def get_source_status(self, member_id: str) -> dict[str, Any] | None:
        member = self._db.get_member(member_id)
        if member is None:
            return None
        try:
            current_stat = os.lstat(member.normalized_path)
        except FileNotFoundError:
            return {
                "status": "error",
                "code": "source_missing",
                "message": "源文件已移动或删除。",
            }
        except OSError:
            return {
                "status": "error",
                "code": "source_inaccessible",
                "message": "源文件当前无法访问。",
            }
        error = _member_source_error(member, current_stat)
        if error in {"Source version changed", "Source identity changed"}:
            if member.extension.casefold() == ".arw":
                try:
                    unsupported = unsupported_arw_camera(member.normalized_path)
                except (OSError, UnicodeError, ValueError):
                    return {
                        "status": "error",
                        "code": "source_invalid",
                        "message": "变化后的 ARW 无法验证相机型号。",
                    }
                if unsupported is not None:
                    return {
                        "status": "error",
                        "code": "source_unsupported_camera",
                        "message": "变化后的 ARW 不是 Sony A7M4 文件。",
                    }
            refreshed = self._refresh_changed_source(member, current_stat)
            if refreshed is not None:
                return {
                    "status": "refreshed",
                    "code": "source_refreshed",
                    "message": "检测到源文件变化，已重新生成预览。",
                }
        if error is not None:
            code = (
                "source_unsafe"
                if "link" in error.casefold() or "resolved" in error.casefold()
                else "source_invalid"
            )
            return {"status": "error", "code": code, "message": error}
        return {"status": "ok", "code": "ok", "message": "源文件可用。"}

    # ------------------------------------------------------------------
    # Image generation (thumbnail / preview)
    # ------------------------------------------------------------------

    def get_thumbnail_bytes(
        self,
        member_id: str,
        *,
        priority: TaskPriority | str | int = TaskPriority.VISIBLE,
    ) -> ImageBytesResult:
        """Generate and return thumbnail bytes for HTTP response.

        Concurrent identical requests decode once via single-flight (8.3).
        """
        member, error = self._load_image_member(member_id)
        if member is None:
            return ImageBytesResult(b"", "", error=error)
        selected_priority = _service_priority(priority)
        token = self._scheduler.capture_generation(member.project_id)
        key = ("thumbnail",) + _source_cache_key(member)
        try:
            scheduled = self._scheduler.submit_single_flight(
                key,
                member.extension,
                "thumbnail",
                lambda: self._render_thumbnail(member, token),
                priority=selected_priority,
                scope=member.project_id,
                token=token,
            )
            return scheduled.result()
        except (SchedulerQueueFull, StaleGeneration, CancelledError) as exc:
            return _scheduler_image_error(exc)

    def _render_thumbnail(
        self,
        member: MemberRecord,
        token: GenerationToken,
    ) -> ImageBytesResult:
        is_png = member.extension.casefold() == ".png"
        cache_extra = "png" if is_png else ""
        content_type = "image/png" if is_png else "image/jpeg"
        cached = self._derived_cache.get_detailed(
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="thumbnails",
            file_identity=member.file_identity,
            extra=cache_extra,
        )
        if cached.status == "hit" and cached.data is not None:
            source_error = self._current_source_error(member)
            if source_error is not None:
                return ImageBytesResult(b"", "", error=source_error, retryable=True)
            return ImageBytesResult(cached.data, content_type)
        warning = _cache_read_warning(cached.status, cached.error)

        result = decode_thumbnail(member.normalized_path, member.extension)
        if result.image is None or result.error is not None:
            return ImageBytesResult(b"", "", error=result.error or "Decode failed")
        try:
            data = (
                image_to_png_bytes(result.image)
                if is_png
                else image_to_jpeg_bytes(result.image, quality=85)
            )
        finally:
            result.image.close()
        source_error = self._current_source_error(member)
        if source_error is not None:
            return ImageBytesResult(b"", "", error=source_error, retryable=True)
        write = self._derived_cache.put(
            data,
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="thumbnails",
            file_identity=member.file_identity,
            extra=cache_extra,
            epoch=token,
            epoch_guard=self._scheduler.generation_guard,
        )
        warning = _cache_write_outcome(write.status, write.error, warning)
        return ImageBytesResult(data, content_type, warning=warning)

    def get_preview_bytes(
        self,
        member_id: str,
        *,
        display_width: int = 0,
        display_height: int = 0,
        look: str = "as_shot",
        quality: str = "best",
        priority: TaskPriority | str | int = TaskPriority.CURRENT,
    ) -> ImageBytesResult:
        """Generate and return preview bytes for HTTP response.

        For ARW with ``as_shot`` (or any non-ARW file), uses the embedded
        preview if large enough, else falls back to full decode. Uncalibrated
        non-default looks are rejected by the capability registry.
        """
        from .creative_look import DEFAULT_LOOK, LOOK_CONFIG_VERSION, is_valid_look

        member, error = self._load_image_member(member_id)
        if member is None:
            return ImageBytesResult(b"", "", error=error)
        if not is_valid_look(look):
            raise ValueError("Invalid creative look")
        if quality not in {"embedded", "best"}:
            raise ValueError("Invalid preview quality")
        for name, value in (
            ("display_width", display_width),
            ("display_height", display_height),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

        is_arw = member.extension.casefold() == ".arw"
        if not is_arw and look != DEFAULT_LOOK:
            raise ValueError("Creative looks only apply to ARW members")
        use_look = is_arw and look != DEFAULT_LOOK
        selected_priority = _service_priority(priority)
        token = self._scheduler.capture_generation(member.project_id)

        # Cache key includes source version + output size + look + config ver.
        size_extra = f"{display_width}x{display_height}"
        look_extra = f"{look}|{LOOK_CONFIG_VERSION}" if use_look else "as_shot"
        cache_extra = f"{size_extra}|{look_extra}|{quality}"
        if member.extension.casefold() == ".png":
            cache_extra += "|png"
        cached = self._derived_cache.get_detailed(
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="previews",
            file_identity=member.file_identity,
            extra=cache_extra,
        )
        if cached.status == "hit" and cached.data is not None:
            source_error = self._current_source_error(member)
            if source_error is not None:
                return ImageBytesResult(b"", "", error=source_error, retryable=True)
            content_type = (
                "image/png" if member.extension.casefold() == ".png" else "image/jpeg"
            )
            return ImageBytesResult(cached.data, content_type)
        read_warning = _cache_read_warning(cached.status, cached.error)
        final_key = ("preview", cache_extra) + _source_cache_key(member)

        try:
            if use_look:
                with self._borrow_full_base(
                    member,
                    token,
                    selected_priority,
                ) as base:
                    scheduled = self._scheduler.submit_single_flight(
                        final_key,
                        member.extension,
                        "look",
                        lambda: self._render_look_preview(
                            member,
                            base,
                            look,
                            cache_extra,
                            token,
                        ),
                        priority=selected_priority,
                        scope=member.project_id,
                        token=token,
                    )
                    rendered = scheduled.result()
            else:
                scheduled = self._scheduler.submit_single_flight(
                    final_key,
                    member.extension,
                    "preview",
                    lambda: self._render_as_shot_preview(
                        member,
                        display_width,
                        display_height,
                        cache_extra,
                        token,
                        selected_priority,
                        allow_full=quality == "best",
                    ),
                    priority=selected_priority,
                    scope=member.project_id,
                    token=token,
                )
                rendered = scheduled.result()
        except (SchedulerQueueFull, StaleGeneration, CancelledError) as exc:
            return _scheduler_image_error(exc)
        except _FullDecodeUnavailable as exc:
            return ImageBytesResult(b"", "", error=str(exc))
        return _add_image_warning(rendered, read_warning)

    def _render_as_shot_preview(
        self,
        member: MemberRecord,
        display_width: int,
        display_height: int,
        cache_extra: str,
        token: GenerationToken,
        priority: TaskPriority,
        *,
        allow_full: bool,
    ) -> ImageBytesResult:
        result = decode_preview(
            member.normalized_path,
            member.extension,
            display_width=display_width,
            display_height=display_height,
            fast=not allow_full,
        )
        warning = None
        needs_full = member.extension.casefold() == ".arw" and (
            result.image is None or result.error == "preview_below_threshold"
        )
        if needs_full and allow_full:
            try:
                with self._borrow_full_base(member, token, priority) as base:
                    full_result = render_full_decode_with_look(
                        base,
                        member.extension,
                        "as_shot",
                    )
                if full_result.image is None:
                    if result.image is None:
                        return ImageBytesResult(
                            b"",
                            "",
                            error=full_result.error
                            or result.error
                            or "Full decode failed",
                        )
                    warning = "Full decode unavailable; embedded preview retained"
                else:
                    if result.image is not None:
                        result.image.close()
                    result = full_result
            except (
                _FullDecodeUnavailable,
                SchedulerQueueFull,
                StaleGeneration,
                CancelledError,
            ):
                if result.image is None:
                    raise
                warning = "Full decode unavailable; embedded preview retained"
        elif result.image is None:
            return ImageBytesResult(b"", "", error=result.error or "Decode failed")
        return self._encode_preview_result(
            member,
            result,
            cache_extra,
            token,
            warning=warning,
        )

    def _render_look_preview(
        self,
        member: MemberRecord,
        base: DecodeResult,
        look: str,
        cache_extra: str,
        token: GenerationToken,
    ) -> ImageBytesResult:
        result = render_full_decode_with_look(base, member.extension, look)
        return self._encode_preview_result(
            member,
            result,
            cache_extra,
            token,
        )

    def _encode_preview_result(
        self,
        member: MemberRecord,
        result: DecodeResult,
        cache_extra: str,
        token: GenerationToken,
        *,
        warning: str | None = None,
    ) -> ImageBytesResult:
        if result.image is None:
            return ImageBytesResult(b"", "", error=result.error or "Decode failed")
        is_png = member.extension.casefold() == ".png"
        try:
            data = (
                image_to_png_bytes(result.image)
                if is_png
                else image_to_jpeg_bytes(result.image, quality=88)
            )
        finally:
            result.image.close()
        source_error = self._current_source_error(member)
        if source_error is not None:
            return ImageBytesResult(b"", "", error=source_error, retryable=True)
        write = self._derived_cache.put(
            data,
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="previews",
            file_identity=member.file_identity,
            extra=cache_extra,
            epoch=token,
            epoch_guard=self._scheduler.generation_guard,
        )
        warning = _cache_write_outcome(write.status, write.error, warning)
        content_type = "image/png" if is_png else "image/jpeg"
        return ImageBytesResult(data, content_type, warning=warning)

    def _load_image_member(
        self, member_id: str
    ) -> tuple[MemberRecord | None, str | None]:
        member = self._db.get_member(member_id)
        if member is None:
            return None, "Member not found"
        try:
            current_stat = os.lstat(member.normalized_path)
        except FileNotFoundError:
            return None, "Source file missing"
        except OSError:
            return None, "Source file cannot be accessed"
        source_error = _member_source_error(member, current_stat)
        if source_error in {"Source version changed", "Source identity changed"}:
            refreshed = self._refresh_changed_source(member, current_stat)
            if refreshed is not None:
                return refreshed, None
        if source_error is not None:
            return None, source_error
        return member, None

    def _refresh_changed_source(
        self,
        member: MemberRecord,
        current_stat: os.stat_result,
    ) -> MemberRecord | None:
        asset = self._db.get_asset(member.asset_id)
        if asset is None:
            return None
        if member.extension.casefold() == ".arw":
            try:
                if unsupported_arw_camera(member.normalized_path) is not None:
                    return None
            except (OSError, UnicodeError, ValueError):
                return None
        project_ids = {
            item.project_id
            for item in self._db.get_members_by_asset_path(member.normalized_path)
        }
        if not self._db.refresh_asset_source_version(
            asset,
            file_size=int(current_stat.st_size),
            mtime_ns=int(current_stat.st_mtime_ns),
            file_identity=source_file_identity(current_stat),
        ):
            return self._db.get_member(member.id)
        for project_id in project_ids:
            self._scheduler.bump_generation(project_id, cancel_queued=True)
        self._evict_full_base_sources({member.normalized_path})
        self._derived_cache.clear_source(member.normalized_path)
        self._importer.schedule_metadata()
        return self._db.get_member(member.id)

    def _current_source_error(self, member: MemberRecord) -> str | None:
        try:
            current_stat = os.lstat(member.normalized_path)
        except FileNotFoundError:
            return "Source file missing"
        except OSError:
            return "Source file cannot be accessed"
        return _member_source_error(member, current_stat)

    def _full_base_key(self, member: MemberRecord) -> tuple[object, ...]:
        return ("full-base",) + _source_cache_key(member)

    def _acquire_full_base(
        self,
        member: MemberRecord,
        token: GenerationToken,
        priority: TaskPriority,
    ) -> _FullBaseEntry:
        key = self._full_base_key(member)
        with self._full_base_lock:
            existing = self._full_bases.get(key)
            if existing is not None:
                existing.users += 1
                self._full_bases.move_to_end(key)
                return existing
            pending = self._full_base_pending.get(key)
            if pending is None:
                pending = _FullBasePending(Future())
                self._full_base_pending[key] = pending
                owns_pending = True
            else:
                pending.waiters += 1
                owns_pending = False

        if not owns_pending:
            return pending.future.result()

        decoded_holder: list[DecodeResult] = []

        def decode_base() -> DecodeResult:
            decoded = decode_full_base(member.normalized_path, member.extension)
            if decoded.image is not None:
                decoded_holder.append(decoded)
            source_error = self._current_source_error(member)
            if source_error is not None:
                if decoded.image is not None:
                    decoded.image.close()
                    decoded_holder.clear()
                raise _FullDecodeUnavailable(source_error)
            return decoded

        try:
            scheduled = self._scheduler.submit_single_flight(
                key,
                member.extension,
                "full",
                decode_base,
                priority=priority,
                scope=member.project_id,
                token=token,
            )
            decoded = scheduled.result()
            if decoded.image is None:
                raise _FullDecodeUnavailable(decoded.error or "Full decode failed")

            with self._scheduler.generation_guard(token) as current:
                if not current:
                    raise StaleGeneration(
                        f"scope generation is stale: {token.generation}"
                    )
                with self._full_base_lock:
                    entry = _FullBaseEntry(
                        key=key,
                        result=decoded,
                        users=pending.waiters,
                    )
                    self._full_bases[key] = entry
                    self._full_base_pending.pop(key, None)
                    self._trim_full_bases_locked()
            pending.future.set_result(entry)
            decoded_holder.clear()
            return entry
        except BaseException as exc:
            for result in decoded_holder:
                if result.image is not None:
                    result.image.close()
            with self._full_base_lock:
                if self._full_base_pending.get(key) is pending:
                    self._full_base_pending.pop(key, None)
                if not pending.future.done():
                    pending.future.set_exception(exc)
            raise

    @contextmanager
    def _borrow_full_base(
        self,
        member: MemberRecord,
        token: GenerationToken,
        priority: TaskPriority,
    ) -> Iterator[DecodeResult]:
        entry = self._acquire_full_base(member, token, priority)
        try:
            yield entry.result
        finally:
            self._release_full_base(entry)

    def _release_full_base(self, entry: _FullBaseEntry) -> None:
        with self._full_base_lock:
            entry.users -= 1
            if entry.users == 0 and entry.evicted and entry.result.image is not None:
                entry.result.image.close()
            elif not entry.evicted:
                self._trim_full_bases_locked()

    def _trim_full_bases_locked(self) -> None:
        while len(self._full_bases) > _FULL_BASE_CACHE_CAPACITY:
            idle_key = next(
                (key for key, entry in self._full_bases.items() if entry.users == 0),
                None,
            )
            if idle_key is None:
                return
            entry = self._full_bases.pop(idle_key)
            entry.evicted = True
            if entry.result.image is not None:
                entry.result.image.close()

    def _evict_full_base_sources(self, normalized_paths: set[str]) -> None:
        with self._full_base_lock:
            for key, entry in tuple(self._full_bases.items()):
                if str(key[1]) not in normalized_paths:
                    continue
                self._full_bases.pop(key, None)
                entry.evicted = True
                if entry.users == 0 and entry.result.image is not None:
                    entry.result.image.close()

    def _close_full_bases(self) -> None:
        with self._full_base_lock:
            entries = tuple(self._full_bases.values())
            self._full_bases.clear()
            for entry in entries:
                entry.evicted = True
                if entry.users == 0 and entry.result.image is not None:
                    entry.result.image.close()

    def list_creative_looks(self) -> list[dict[str, str]]:
        """Return the A7M4 creative-look options for the UI selector."""
        from .creative_look import LOOK_META, calibration_note

        return [
            {
                "id": meta.id,
                "label": meta.label,
                "description": meta.description,
                "calibration": calibration_note(meta.id),
            }
            for meta in LOOK_META
        ]

    def rawpy_available(self) -> bool:
        """Return True if rawpy/LibRaw is available for ARW decoding."""
        return is_rawpy_available()


# ----------------------------------------------------------------------
# Filtering & sorting
# ----------------------------------------------------------------------


def _apply_filters(
    members: list[MemberRecord],
    *,
    star_mode: str,
    star_value: int,
    color_labels: str,
    filename_contains: str,
    rated_filter: str,
    exported_filter: str,
    formats: str,
    orientations: str,
) -> list[MemberRecord]:
    result = list(members)

    # Star rating filter
    if star_mode == "exact":
        result = [m for m in result if m.star_rating == star_value]
    elif star_mode == "at_least":
        result = [m for m in result if m.star_rating >= star_value]
    elif star_mode == "unrated":
        result = [m for m in result if m.star_rating == 0]

    # Color label filter (comma-separated, OR within category)
    if color_labels:
        labels = {c.strip() for c in color_labels.split(",") if c.strip()}
        if labels:
            result = [m for m in result if m.color_label in labels]

    # Filename contains filter
    if filename_contains:
        needle = filename_contains.casefold()
        result = [m for m in result if needle in m.file_name.casefold()]

    # Rated/unrated filter
    if rated_filter == "rated":
        result = [m for m in result if m.star_rating > 0]
    elif rated_filter == "unrated":
        result = [m for m in result if m.star_rating == 0]

    if exported_filter == "exported":
        result = [m for m in result if m.exported_at is not None]
    elif exported_filter == "unexported":
        result = [m for m in result if m.exported_at is None]

    format_values = _parse_csv_filter(formats, VALID_FILTER_FORMATS, "formats")
    if format_values:
        result = [m for m in result if _format_group(m.extension) in format_values]

    orientation_values = _parse_csv_filter(
        orientations,
        VALID_FILTER_ORIENTATIONS,
        "orientations",
    )
    if orientation_values:
        result = [m for m in result if _member_orientation(m) in orientation_values]

    return result


def _sort_members(
    members: list[MemberRecord],
    sort_field: str,
    sort_direction: str,
) -> None:
    """In-place stable sort of members."""
    reverse = sort_direction == "desc"
    members.sort(key=lambda member: member.import_order)

    if sort_field == "filename":
        members.sort(key=lambda m: m.file_name.casefold(), reverse=reverse)
    elif sort_field == "import_order":
        members.sort(key=lambda m: m.import_order, reverse=reverse)
    elif sort_field == "shot_time":
        known = [member for member in members if member.shot_time is not None]
        unknown = [member for member in members if member.shot_time is None]
        known.sort(key=lambda member: member.shot_time or "", reverse=reverse)
        members[:] = known + unknown
    elif sort_field == "star_rating":
        members.sort(key=lambda m: m.star_rating, reverse=reverse)
    elif sort_field == "mtime_ns":
        members.sort(key=lambda m: m.mtime_ns, reverse=reverse)
    elif sort_field == "extension":
        members.sort(key=lambda m: _format_group(m.extension), reverse=reverse)
    elif sort_field == "file_size":
        members.sort(key=lambda m: m.file_size, reverse=reverse)


def _validate_member_query(
    *,
    offset: int,
    limit: int,
    star_mode: str,
    star_value: int,
    color_labels: str,
    filename_contains: str,
    rated_filter: str,
    exported_filter: str,
    formats: str,
    orientations: str,
    sort_field: str,
    sort_direction: str,
) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 10_000
    ):
        raise ValueError("limit must be an integer between 1 and 10000")
    if star_mode not in VALID_FILTER_STAR_MODES:
        raise ValueError("Invalid star_mode")
    if (
        isinstance(star_value, bool)
        or not isinstance(star_value, int)
        or star_value not in VALID_STAR_RATINGS
    ):
        raise ValueError("Invalid star_value")
    if star_mode in {"exact", "at_least"} and star_value == 0:
        raise ValueError("exact and at_least require a 1-5 star_value")
    _parse_csv_filter(color_labels, VALID_COLOR_LABELS, "color_labels")
    if not isinstance(filename_contains, str):
        raise ValueError("filename_contains must be a string")
    if rated_filter not in VALID_FILTER_RATED:
        raise ValueError("Invalid rated_filter")
    if exported_filter not in VALID_FILTER_EXPORTED:
        raise ValueError("Invalid exported_filter")
    _parse_csv_filter(formats, VALID_FILTER_FORMATS, "formats")
    _parse_csv_filter(orientations, VALID_FILTER_ORIENTATIONS, "orientations")
    if sort_field not in VALID_SORT_FIELDS:
        raise ValueError("Invalid sort_field")
    if sort_direction not in VALID_SORT_DIRECTIONS:
        raise ValueError("Invalid sort_direction")


def _parse_csv_filter(
    value: str,
    allowed: frozenset[str],
    field: str,
) -> set[str]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    if len(tokens) != len(set(tokens)) or any(token not in allowed for token in tokens):
        raise ValueError(f"Invalid {field}")
    return set(tokens)


def _format_group(extension: str) -> str:
    return (
        "jpeg"
        if extension.casefold() in {".jpg", ".jpeg"}
        else extension.lstrip(".").casefold()
    )


def _member_orientation(member: MemberRecord) -> str | None:
    if member.width is None or member.height is None:
        return None
    if member.width == member.height:
        return "square"
    return "landscape" if member.width > member.height else "portrait"


# ----------------------------------------------------------------------
# Path safety for permanent delete
# ----------------------------------------------------------------------


def _delete_target_error(
    item: OperationLogItem, current_stat: os.stat_result
) -> str | None:
    path = item.normalized_path
    target = Path(path)
    if not target.is_absolute() or any(character in path for character in "*?[]"):
        return "Path safety check failed"
    if not stat.S_ISREG(current_stat.st_mode):
        return "Delete target is not a regular file"
    if os.path.islink(path) or _is_reparse_point(path):
        return "Delete target is a link or reparse point"
    try:
        if normalize_path(target.resolve(strict=True)) != path:
            return "Resolved delete path changed after confirmation"
    except OSError:
        return "Delete target cannot be resolved safely"
    if (
        item.source_extension is None
        or target.suffix.casefold() != item.source_extension
    ):
        return "Delete target file type changed after confirmation"
    if item.source_extension not in SUPPORTED_EXTENSIONS:
        return "Delete target file type is unsupported"
    if item.source_file_size is None or item.source_mtime_ns is None:
        return "Legacy delete log lacks a confirmed source version"
    if (
        int(current_stat.st_size) != item.source_file_size
        or int(current_stat.st_mtime_ns) != item.source_mtime_ns
    ):
        return "Delete target version changed after confirmation"
    identity = source_file_identity(current_stat)
    if (
        item.source_file_identity is not None
        and identity is not None
        and identity != item.source_file_identity
    ):
        return "Delete target identity changed after confirmation"
    return None


def _member_source_error(
    member: MemberRecord, current_stat: os.stat_result
) -> str | None:
    path = member.normalized_path
    if not stat.S_ISREG(current_stat.st_mode):
        return "Source is not a regular file"
    if os.path.islink(path) or _is_reparse_point(path):
        return "Source is a link or reparse point"
    try:
        if normalize_path(Path(path).resolve(strict=True)) != path:
            return "Resolved source path changed"
    except OSError:
        return "Source path cannot be resolved safely"
    if (
        int(current_stat.st_size) != member.file_size
        or int(current_stat.st_mtime_ns) != member.mtime_ns
    ):
        return "Source version changed"
    identity = source_file_identity(current_stat)
    if (
        member.file_identity is not None
        and identity is not None
        and identity != member.file_identity
    ):
        return "Source identity changed"
    return None


def _copy_source_exact(
    source: Path,
    destination: Path,
    member: MemberRecord,
    *,
    cancel_event: threading.Event | None,
) -> Path:
    before = os.lstat(source)
    source_error = _member_source_error(member, before)
    if source_error is not None:
        raise OSError(source_error)

    fd, temporary_name = tempfile.mkstemp(
        dir=destination,
        prefix=".zvec-export-",
        suffix=".tmp",
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_digest = hashlib.sha256()
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _ExportCancelled
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                source_digest.update(chunk)
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())

        after = os.lstat(source)
        source_error = _member_source_error(member, after)
        if source_error is not None:
            raise OSError(source_error)
        target_digest = _sha256_file(temporary)
        if target_digest != source_digest.digest():
            raise OSError("Exported bytes do not match the source")
        shutil.copystat(source, temporary, follow_symlinks=False)

        sequence = 0
        while True:
            candidate = _export_candidate(destination, source.name, sequence)
            if _publish_without_overwrite(temporary, candidate):
                return candidate
            sequence += 1
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _export_candidate(destination: Path, file_name: str, sequence: int) -> Path:
    original = Path(file_name)
    if sequence == 0:
        return destination / original.name
    return destination / f"{original.stem}_{sequence}{original.suffix}"


def _publish_without_overwrite(temporary: Path, candidate: Path) -> bool:
    try:
        os.link(temporary, candidate)
    except FileExistsError:
        return False
    except OSError:
        try:
            reservation = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            return False
        try:
            os.close(reservation)
            reservation = -1
            os.replace(temporary, candidate)
            return True
        except Exception:
            with suppress(OSError):
                candidate.unlink()
            raise
        finally:
            if reservation >= 0:
                os.close(reservation)
    else:
        temporary.unlink()
        return True


class _ExportCancelled(Exception):
    pass


def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()


# ----------------------------------------------------------------------
# Image generation
# ----------------------------------------------------------------------


class _FullDecodeUnavailable(RuntimeError):
    pass


def _source_cache_key(member: MemberRecord) -> tuple[object, ...]:
    return (
        member.normalized_path,
        member.file_size,
        member.mtime_ns,
        member.file_identity,
    )


def _service_priority(value: TaskPriority | str | int) -> TaskPriority:
    if isinstance(value, TaskPriority):
        return value
    if isinstance(value, bool):
        raise ValueError("Invalid image priority")
    if isinstance(value, int):
        try:
            return TaskPriority(value)
        except ValueError as exc:
            raise ValueError("Invalid image priority") from exc
    if not isinstance(value, str):
        raise ValueError("Invalid image priority")
    normalized = value.strip().casefold()
    priorities = {
        "current": TaskPriority.CURRENT,
        "compare": TaskPriority.COMPARE,
        "visible": TaskPriority.VISIBLE,
        "overscan": TaskPriority.OVERSCAN,
        "adjacent": TaskPriority.OVERSCAN,
        "background": TaskPriority.BACKGROUND,
    }
    try:
        return priorities[normalized]
    except KeyError as exc:
        raise ValueError("Invalid image priority") from exc


def _merge_warning(first: str | None, second: str | None) -> str | None:
    if first and second:
        return f"{first}; {second}"
    return first or second


def _cache_read_warning(status: str, error: str | None) -> str | None:
    if status != "error":
        return None
    return f"Derived cache read failed: {error or 'unknown error'}"


def _cache_write_outcome(
    status: str,
    error: str | None,
    warning: str | None,
) -> str | None:
    if status == "stale":
        raise StaleGeneration("image result became stale before cache publish")
    if status == "error":
        return _merge_warning(
            warning,
            f"Derived cache write failed: {error or 'unknown error'}",
        )
    return warning


def _scheduler_image_error(exc: BaseException) -> ImageBytesResult:
    if isinstance(exc, SchedulerQueueFull):
        message = "Image work queue is full; retry the request"
    else:
        message = "Image request became stale or was cancelled; retry if still visible"
    return ImageBytesResult(b"", "", error=message, retryable=True)


def _add_image_warning(
    result: ImageBytesResult,
    warning: str | None,
) -> ImageBytesResult:
    return ImageBytesResult(
        result.data,
        result.content_type,
        error=result.error,
        warning=_merge_warning(result.warning, warning),
        retryable=result.retryable,
    )


@dataclass(frozen=True, slots=True)
class ImageBytesResult:
    """Result of serving image bytes for HTTP response."""

    data: bytes
    content_type: str
    error: str | None = None
    warning: str | None = None
    retryable: bool = False


def _project_to_dict(p: ProjectSummary) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "member_count": p.member_count,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _member_to_dict(m: MemberRecord) -> dict[str, Any]:
    return {
        "id": m.id,
        "project_id": m.project_id,
        "asset_id": m.asset_id,
        "import_order": m.import_order,
        "star_rating": m.star_rating,
        "color_label": m.color_label,
        "creative_look": m.creative_look,
        "file_name": m.file_name,
        "normalized_path": m.normalized_path,
        "extension": m.extension,
        "file_size": m.file_size,
        "mtime_ns": m.mtime_ns,
        "file_identity": m.file_identity,
        "shot_time": m.shot_time,
        "width": m.width,
        "height": m.height,
        "metadata_status": m.metadata_status,
        "exported_at": m.exported_at,
        "is_exported": m.exported_at is not None,
    }


def _workspace_state_to_dict(ws: WorkspaceState) -> dict[str, Any]:
    return {
        "last_member_id": ws.last_member_id,
        "filter_star_mode": ws.filter_star_mode,
        "filter_star_value": ws.filter_star_value,
        "filter_color_labels": ws.filter_color_labels,
        "filter_filename": ws.filter_filename,
        "filter_rated": ws.filter_rated,
        "filter_exported": ws.filter_exported,
        "filter_formats": ws.filter_formats,
        "filter_orientations": ws.filter_orientations,
        "sort_field": ws.sort_field,
        "sort_direction": ws.sort_direction,
        "filmstrip_scroll": ws.filmstrip_scroll,
    }


def _import_result_to_dict(r: ImportResult) -> dict[str, Any]:
    return {
        "registered": r.registered,
        "skipped_unsupported": r.skipped_unsupported,
        "skipped_raw_formats": r.skipped_raw_formats,
        "skipped_unsupported_camera": r.skipped_unsupported_camera,
        "skipped_reparse": r.skipped_reparse,
        "errors": r.errors,
        "error_details": list(r.error_details),
        "unsupported_camera_details": list(r.unsupported_camera_details),
    }
