"""High-level service orchestration for the ARW selection module.

Ties together the database, importer, decoder, cache, exporter and deleter
into a single service that the WebView gateway calls. Project CRUD, import
scheduling, rating, workspace state, export and permanent delete all flow
through this service.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import DerivedCache
from .db import (
    MemberRecord,
    ProjectSummary,
    RawSelectionDB,
    WorkspaceState,
)
from .decoder import (
    decode_full,
    decode_full_with_look,
    decode_preview,
    decode_thumbnail,
    image_to_jpeg_bytes,
    is_rawpy_available,
)
from .importer import AssetImporter, ImportResult

_CACHE_DIR_NAME = "cache"
_DB_NAME = "projects.sqlite3"


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
    error_details: tuple[str, ...]


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
        self._importer = AssetImporter(self._db)
        self._derived_cache = DerivedCache(self._cache_dir)
        self._lock = threading.RLock()

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def create_project(self, name: str) -> dict[str, Any]:
        p = self._db.create_project(name)
        return _project_to_dict(p)

    def list_projects(self) -> list[dict[str, Any]]:
        return [_project_to_dict(p) for p in self._db.list_projects()]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        p = self._db.get_project(project_id)
        if p is None:
            return None
        return _project_to_dict(p)

    def rename_project(self, project_id: str, name: str) -> dict[str, Any] | None:
        p = self._db.rename_project(project_id, name)
        if p is None:
            return None
        return _project_to_dict(p)

    def delete_project(self, project_id: str) -> bool:
        return self._db.delete_project(project_id)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_files(
        self,
        project_id: str,
        file_paths: Sequence[str | Path],
    ) -> dict[str, Any]:
        result = self._importer.import_files(project_id, file_paths)
        return _import_result_to_dict(result)

    def import_folder(
        self,
        project_id: str,
        folder_path: str | Path,
    ) -> dict[str, Any]:
        result = self._importer.import_folder(project_id, folder_path)
        return _import_result_to_dict(result)

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
        sort_field: str = "filename",
        sort_direction: str = "asc",
    ) -> dict[str, Any]:
        """List project members with optional filtering and sorting.

        Filter and sort parameters come from the workspace state saved by
        the frontend.
        """
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
            sort_field=sort_field,
            sort_direction=sort_direction,
            filmstrip_scroll=filmstrip_scroll,
        )

    # ------------------------------------------------------------------
    # Remove from project
    # ------------------------------------------------------------------

    def remove_members(self, member_ids: Sequence[str]) -> int:
        """Remove members from a project. Does NOT delete source files."""
        return self._db.remove_members(member_ids)

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

        # Gather targets from DB
        targets: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for mid in member_ids:
            member = self._db.get_member(mid)
            if member is None:
                continue
            norm = member.normalized_path
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            targets.append((member.asset_id, norm))

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

        # Phase 2: Execute deletes
        deleted = 0
        already_missing = 0
        failed = 0
        details: list[dict[str, str]] = []
        log_items = self._db.get_operation_log_items(log_id)

        for item in log_items:
            error_msg = None
            try:
                if not os.path.isfile(item.normalized_path):
                    # Already missing — clean up references
                    self._db.remove_members_by_asset_path(item.normalized_path)
                    self._db.delete_asset_by_path(item.normalized_path)
                    self._db.update_operation_item(item.id, "already_missing")
                    already_missing += 1
                    details.append({
                        "path": item.normalized_path,
                        "result": "already_missing",
                    })
                    continue

                # Verify path safety: must be an absolute file path
                # (not directory, not wildcard, not symlink/reparse)
                if not _is_safe_delete_target(item.normalized_path):
                    self._db.update_operation_item(
                        item.id, "failed", "Path safety check failed"
                    )
                    failed += 1
                    details.append({
                        "path": item.normalized_path,
                        "result": "failed",
                        "error": "Path safety check failed",
                    })
                    continue

                # Delete the physical file (no recycle bin)
                os.unlink(item.normalized_path)
                self._db.update_operation_item(item.id, "deleted")
                # Clean up references now that the file is gone.
                self._db.remove_members_by_asset_path(item.normalized_path)
                self._db.delete_asset_by_path(item.normalized_path)
                deleted += 1
                details.append({
                    "path": item.normalized_path,
                    "result": "deleted",
                })
            except OSError as exc:
                error_msg = str(exc)
                self._db.update_operation_item(item.id, "failed", error_msg)
                failed += 1
                details.append({
                    "path": item.normalized_path,
                    "result": "failed",
                    "error": error_msg,
                })

        self._db.set_operation_log_status(log_id, "completed")

        return {
            "deleted": deleted,
            "already_missing": already_missing,
            "failed": failed,
            "details": details,
            "log_id": log_id,
        }

    def recover_pending_deletes(self) -> int:
        """Recover from interrupted permanent-delete operations.

        Called on startup to complete reference cleanup for files that
        were physically deleted but whose DB references were not yet cleaned.
        """
        recovered = 0
        pending_logs = self._db.get_pending_operation_logs()
        for log_entry in pending_logs:
            if log_entry.operation_type != "permanent_delete":
                continue
            items = self._db.get_operation_log_items(log_entry.id)
            for item in items:
                if item.result == "pending":
                    # File was not yet processed — check if it exists
                    if not os.path.isfile(item.normalized_path):
                        # File is gone — clean up references
                        self._db.remove_members_by_asset_path(
                            item.normalized_path
                        )
                        self._db.delete_asset_by_path(item.normalized_path)
                        self._db.update_operation_item(
                            item.id, "already_missing"
                        )
                        recovered += 1
                    else:
                        # File still exists — leave as pending for manual retry
                        pass
                elif item.result == "deleted":
                    # File was deleted but references not yet cleaned
                    self._db.remove_members_by_asset_path(
                        item.normalized_path
                    )
                    self._db.delete_asset_by_path(item.normalized_path)
                    recovered += 1
            self._db.set_operation_log_status(log_entry.id, "completed")
        return recovered

    # ------------------------------------------------------------------
    # Export (source file copy)
    # ------------------------------------------------------------------

    def export_files(
        self,
        member_ids: Sequence[str],
        destination: str | Path,
    ) -> dict[str, Any]:
        """Copy source files to a destination directory.

        ARW files are copied as raw bytes; JPG/PNG keep original format.
        No re-encoding, no metadata modification.
        """
        import shutil

        dest = Path(destination)
        dest.mkdir(parents=True, exist_ok=True)

        exported = 0
        skipped = 0
        failed = 0
        details: list[dict[str, str]] = []

        for mid in member_ids:
            member = self._db.get_member(mid)
            if member is None:
                failed += 1
                details.append({
                    "member_id": mid,
                    "result": "failed",
                    "error": "Member not found",
                })
                continue

            src = Path(member.normalized_path)
            if not src.is_file():
                skipped += 1
                details.append({
                    "path": member.normalized_path,
                    "result": "skipped",
                    "error": "Source file missing",
                })
                continue

            # Determine destination filename with conflict resolution
            dest_name = src.name
            dest_path = dest / dest_name
            if dest_path.exists():
                # Add sequence suffix
                stem = src.stem
                ext = src.suffix
                seq = 1
                while True:
                    candidate = dest / f"{stem}_{seq}{ext}"
                    if not candidate.exists():
                        dest_path = candidate
                        break
                    seq += 1

            try:
                shutil.copy2(src, dest_path)
                exported += 1
                details.append({
                    "path": member.normalized_path,
                    "dest": str(dest_path),
                    "result": "exported",
                })
            except OSError as exc:
                failed += 1
                details.append({
                    "path": member.normalized_path,
                    "result": "failed",
                    "error": str(exc),
                })

        return {
            "exported": exported,
            "skipped": skipped,
            "failed": failed,
            "details": details,
        }

    # ------------------------------------------------------------------
    # Clear project cache
    # ------------------------------------------------------------------

    def clear_project_cache(self, project_id: str) -> int:
        """Delete all derived cache for a project.

        Only deletes derived caches (thumbnails, previews, full decodes).
        Preserves project references, ratings, color labels and creative
        look selections.
        """
        removed = self._derived_cache.clear()
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

    # ------------------------------------------------------------------
    # Image generation (thumbnail / preview)
    # ------------------------------------------------------------------

    def get_thumbnail_bytes(self, member_id: str) -> ImageBytesResult:
        """Generate and return thumbnail JPEG bytes for HTTP response."""
        member = self._db.get_member(member_id)
        if member is None:
            return ImageBytesResult(b"", "", error="Member not found")
        if not os.path.isfile(member.normalized_path):
            return ImageBytesResult(b"", "", error="Source file missing")

        # Check persistent derived cache first (8.4 source-version keyed).
        cached = self._derived_cache.get(
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="thumbnails",
        )
        if cached is not None:
            return ImageBytesResult(cached, "image/jpeg")

        result = decode_thumbnail(member.normalized_path, member.extension)
        if result.image is None or result.error is not None:
            return ImageBytesResult(
                b"", "", error=result.error or "Decode failed"
            )
        data = image_to_jpeg_bytes(result.image, quality=85)
        self._derived_cache.put(
            data,
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="thumbnails",
        )
        return ImageBytesResult(data, "image/jpeg")

    def get_preview_bytes(
        self,
        member_id: str,
        *,
        display_width: int = 0,
        display_height: int = 0,
        look: str = "as_shot",
    ) -> ImageBytesResult:
        """Generate and return preview JPEG bytes for HTTP response.

        For ARW with ``as_shot`` (or any non-ARW file), uses the embedded
        preview if large enough, else falls back to full decode. For ARW
        with a non-default creative look, performs a full decode and applies
        the calibrated look transform. The cache key includes the look ID and
        look-config version (requirement 7.4.3).
        """
        from .creative_look import DEFAULT_LOOK, LOOK_CONFIG_VERSION, is_valid_look

        member = self._db.get_member(member_id)
        if member is None:
            return ImageBytesResult(b"", "", error="Member not found")
        if not os.path.isfile(member.normalized_path):
            return ImageBytesResult(b"", "", error="Source file missing")

        is_arw = member.extension.casefold() == ".arw"
        use_look = is_arw and look != DEFAULT_LOOK and is_valid_look(look)

        # Cache key includes source version + output size + look + config ver.
        size_extra = f"{display_width}x{display_height}"
        look_extra = f"{look}|{LOOK_CONFIG_VERSION}" if use_look else "as_shot"
        cached = self._derived_cache.get(
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="previews",
            extra=f"{size_extra}|{look_extra}",
        )
        if cached is not None:
            return ImageBytesResult(cached, "image/jpeg")

        if use_look:
            result = decode_full_with_look(
                member.normalized_path, member.extension, look
            )
        else:
            # Try embedded preview first
            result = decode_preview(
                member.normalized_path,
                member.extension,
                display_width=display_width,
                display_height=display_height,
            )
            if (
                result.error == "preview_below_threshold"
                and result.image is not None
            ):
                if is_arw:
                    full_result = decode_full(
                        member.normalized_path, member.extension
                    )
                    if full_result.image is not None:
                        result = full_result
            elif result.image is None:
                return ImageBytesResult(
                    b"", "", error=result.error or "Decode failed"
                )

        if result.image is None:
            return ImageBytesResult(
                b"", "", error=result.error or "Decode failed"
            )

        data = image_to_jpeg_bytes(result.image, quality=88)
        self._derived_cache.put(
            data,
            normalized_path=member.normalized_path,
            file_size=member.file_size,
            mtime_ns=member.mtime_ns,
            kind="previews",
            extra=f"{size_extra}|{look_extra}",
        )
        return ImageBytesResult(data, "image/jpeg")

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
) -> list[MemberRecord]:
    result = list(members)

    # Star rating filter
    if star_mode == "exact" and star_value > 0:
        result = [m for m in result if m.star_rating == star_value]
    elif star_mode == "at_least" and star_value > 0:
        result = [m for m in result if m.star_rating >= star_value]

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

    return result


def _sort_members(
    members: list[MemberRecord],
    sort_field: str,
    sort_direction: str,
) -> None:
    """In-place stable sort of members."""
    reverse = sort_direction == "desc"

    if sort_field == "filename":
        members.sort(key=lambda m: m.file_name.casefold(), reverse=reverse)
    elif sort_field == "import_order":
        members.sort(key=lambda m: m.import_order, reverse=reverse)
    elif sort_field == "shot_time":
        members.sort(
            key=lambda m: (m.shot_time or ""), reverse=reverse
        )
    elif sort_field == "star_rating":
        members.sort(key=lambda m: m.star_rating, reverse=reverse)
    else:
        members.sort(key=lambda m: m.import_order, reverse=reverse)


# ----------------------------------------------------------------------
# Path safety for permanent delete
# ----------------------------------------------------------------------


def _is_safe_delete_target(path: str) -> bool:
    """Verify a path is safe for permanent deletion.

    Must be:
    - An absolute file path (not directory)
    - Not a symlink or reparse point
    - Not a wildcard or pattern
    """
    from .importer import _is_reparse_point

    p = Path(path)
    if not p.is_absolute():
        return False
    if not os.path.isfile(path):
        return False
    if os.path.islink(path):
        return False
    if _is_reparse_point(path):
        return False
    # Reject paths with wildcards
    return not any(c in path for c in "*?[]")


# ----------------------------------------------------------------------
# Image generation
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageBytesResult:
    """Result of serving image bytes for HTTP response."""

    data: bytes
    content_type: str
    error: str | None = None


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
    }


def _workspace_state_to_dict(ws: WorkspaceState) -> dict[str, Any]:
    return {
        "last_member_id": ws.last_member_id,
        "filter_star_mode": ws.filter_star_mode,
        "filter_star_value": ws.filter_star_value,
        "filter_color_labels": ws.filter_color_labels,
        "filter_filename": ws.filter_filename,
        "filter_rated": ws.filter_rated,
        "sort_field": ws.sort_field,
        "sort_direction": ws.sort_direction,
        "filmstrip_scroll": ws.filmstrip_scroll,
    }


def _import_result_to_dict(r: ImportResult) -> dict[str, Any]:
    return {
        "registered": r.registered,
        "skipped_unsupported": r.skipped_unsupported,
        "skipped_raw_formats": r.skipped_raw_formats,
        "skipped_reparse": r.skipped_reparse,
        "errors": r.errors,
        "error_details": list(r.error_details),
    }
