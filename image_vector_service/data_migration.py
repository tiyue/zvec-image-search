from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Final, Literal, Protocol

from .config import ServiceConfig
from .path_migration import migrate_schema
from .process_lock import ProcessLock
from .workspace_backup import (
    COLLECTION_DIRECTORY_NAME,
    MANIFEST_FILE_NAME,
    METADATA_FILE_NAME,
    STATE_FILE_NAME,
    WorkspaceBackupError,
    WorkspaceBackupSource,
    create_migration_backup,
    new_backup_destination,
    plan_migration_backup,
    resolve_backup_library_directory,
    validate_backup_library_id,
)

MigrationType = Literal["schema", "root", "legacy_config", "docker_workspace"]
MigrationProgressCallback = Callable[[dict[str, object]], None]

SUPPORTED_MIGRATION_TYPES: Final = frozenset(
    {"schema", "root", "legacy_config", "docker_workspace"}
)
MIGRATION_CONFIRMATION_PHRASE: Final = "MIGRATE"
PREVIEW_TTL_SECONDS: Final = 15 * 60
MAX_PREVIEW_CACHE_SIZE: Final = 128
SAFE_LIBRARY_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DataMigrationError(RuntimeError):
    """Base class for safe desktop data-migration failures."""


class DataMigrationValidationError(DataMigrationError, ValueError):
    """A migration request is invalid or unsafe."""


class DataMigrationCancelled(DataMigrationError):
    """Cancellation was accepted before a mutating phase began."""


class DataMigrationExecutionError(DataMigrationError):
    """Migration failed; ``report`` describes automatic recovery."""

    def __init__(self, message: str, report: Mapping[str, object]) -> None:
        self.report = dict(report)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DataMigrationRequest:
    migration_type: MigrationType
    source: str
    target: str
    library_id: str = ""
    library_name: str = ""
    workspace_directory: str = ""
    image_root: str = ""
    root_id: str = ""
    docker_image: str = "zvec-image-search:local"
    backup_directory: str = ""
    automatic_backup: bool = True

    def __post_init__(self) -> None:
        migration_type = str(self.migration_type).strip().lower()
        if migration_type not in SUPPORTED_MIGRATION_TYPES:
            raise DataMigrationValidationError(
                "migration_type must be schema, root, legacy_config, or "
                "docker_workspace."
            )
        object.__setattr__(self, "migration_type", migration_type)
        for name in (
            "source",
            "target",
            "library_id",
            "library_name",
            "workspace_directory",
            "image_root",
            "root_id",
            "docker_image",
            "backup_directory",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise DataMigrationValidationError(f"{name} must be a string.")
            normalized = value.strip()
            if any(character in normalized for character in "\r\n\x00"):
                raise DataMigrationValidationError(
                    f"{name} contains unsafe control characters."
                )
            if len(normalized) > 8_192:
                raise DataMigrationValidationError(f"{name} is too long.")
            object.__setattr__(self, name, normalized)
        if not self.source:
            raise DataMigrationValidationError("source must not be empty.")
        if not self.target:
            raise DataMigrationValidationError("target must not be empty.")
        if self.library_id and (
            SAFE_LIBRARY_ID.fullmatch(self.library_id) is None
            or ".." in self.library_id
            or self.library_id.endswith((".", " "))
        ):
            raise DataMigrationValidationError(
                "library_id must be a safe single-segment identifier."
            )
        if self.library_id:
            try:
                validate_backup_library_id(self.library_id)
            except WorkspaceBackupError as exc:
                raise DataMigrationValidationError(str(exc)) from exc
        if not isinstance(self.automatic_backup, bool):
            raise DataMigrationValidationError("automatic_backup must be a boolean.")
        if not self.automatic_backup:
            raise DataMigrationValidationError(
                "Data migration requires an automatic full backup."
            )
        if self.migration_type == "root" and not self.workspace_directory:
            raise DataMigrationValidationError(
                "root migration requires workspace_directory."
            )
        if self.migration_type == "docker_workspace" and not self.docker_image:
            raise DataMigrationValidationError(
                "docker_workspace migration requires docker_image."
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> DataMigrationRequest:
        allowed = {
            "migration_type",
            "source",
            "target",
            "library_id",
            "library_name",
            "workspace_directory",
            "image_root",
            "root_id",
            "docker_image",
            "backup_directory",
            "automatic_backup",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise DataMigrationValidationError(
                "Unknown migration request fields: " + ", ".join(sorted(unknown))
            )
        missing = {"migration_type", "source", "target"} - set(payload)
        if missing:
            raise DataMigrationValidationError(
                "Missing migration request fields: " + ", ".join(sorted(missing))
            )
        return cls(
            migration_type=str(payload["migration_type"]),  # type: ignore[arg-type]
            source=str(payload["source"]),
            target=str(payload["target"]),
            library_id=str(payload.get("library_id") or ""),
            library_name=str(payload.get("library_name") or ""),
            workspace_directory=str(payload.get("workspace_directory") or ""),
            image_root=str(payload.get("image_root") or ""),
            root_id=str(payload.get("root_id") or ""),
            docker_image=str(payload.get("docker_image") or "zvec-image-search:local"),
            backup_directory=str(payload.get("backup_directory") or ""),
            automatic_backup=_strict_boolean(
                payload.get("automatic_backup", True), "automatic_backup"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    workspace: str
    status: str
    schema_version: int | None
    collection_documents: int | None
    sqlite_entries: int | None
    sqlite_integrity: str
    collection_readable: bool
    fingerprint: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NativeMigrationPrecheck:
    status: str
    source_display: str
    target_display: str
    collection_documents: int | None
    sqlite_entries: int | None
    collection_status: str
    sqlite_status: str
    fingerprint: str
    backup_plan: Mapping[str, object]
    details: Mapping[str, object] = field(default_factory=dict)
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    api_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source": self.source_display,
            "target": self.target_display,
            "collection_documents": self.collection_documents,
            "sqlite_entries": self.sqlite_entries,
            "collection_status": self.collection_status,
            "sqlite_status": self.sqlite_status,
            "fingerprint": self.fingerprint,
            "backup_plan": dict(self.backup_plan),
            "details": dict(self.details),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "api_requests": self.api_requests,
        }


@dataclass(frozen=True, slots=True)
class DataMigrationPreview:
    preview_id: str
    confirmation_token: str
    expires_at_epoch: float
    request: DataMigrationRequest
    precheck: NativeMigrationPrecheck
    dry_run: bool = True
    vectors_recomputed: int = 0
    model_api_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "preview_id": self.preview_id,
            "confirmation_token": self.confirmation_token,
            "expires_at_epoch": self.expires_at_epoch,
            "request": self.request.to_dict(),
            "precheck": self.precheck.to_dict(),
            "dry_run": self.dry_run,
            "vectors_recomputed": self.vectors_recomputed,
            "model_api_requests": self.model_api_requests,
        }


class MigrationOperations(Protocol):
    def precheck(self, request: DataMigrationRequest) -> NativeMigrationPrecheck: ...

    def create_backup(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]: ...

    def apply(
        self,
        request: DataMigrationRequest,
        *,
        cancel_event: threading.Event,
        on_progress: MigrationProgressCallback,
    ) -> Mapping[str, object]: ...

    def verify(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]: ...

    def restore(
        self,
        request: DataMigrationRequest,
        backup: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class DataMigrationCoordinator:
    """Coordinate preview, confirmation, backup, execution, and recovery.

    The coordinator is intentionally independent of HTTP and job-manager code.
    A desktop facade can retain one instance and run ``execute`` in its existing
    background executor while forwarding the progress callback to a job record.
    """

    def __init__(
        self,
        operations: MigrationOperations,
        *,
        platform_name: str | None = None,
        preview_ttl_seconds: int = PREVIEW_TTL_SECONDS,
    ) -> None:
        self._operations = operations
        self._platform_name = platform_name or platform.system()
        self._preview_ttl_seconds = max(60, int(preview_ttl_seconds))
        self._lock = threading.RLock()
        self._previews: dict[str, DataMigrationPreview] = {}

    def preview(self, request: DataMigrationRequest) -> DataMigrationPreview:
        self._require_windows()
        precheck = self._operations.precheck(request)
        _assert_zero_api_requests(precheck.to_dict())
        now = time.time()
        preview = DataMigrationPreview(
            preview_id=f"migration-preview-{uuid.uuid4().hex}",
            confirmation_token=secrets.token_urlsafe(32),
            expires_at_epoch=now + self._preview_ttl_seconds,
            request=request,
            precheck=precheck,
        )
        with self._lock:
            self._prune_previews(now)
            if len(self._previews) >= MAX_PREVIEW_CACHE_SIZE:
                oldest = min(
                    self._previews,
                    key=lambda token: self._previews[token].expires_at_epoch,
                )
                self._previews.pop(oldest, None)
            self._previews[preview.confirmation_token] = preview
        return preview

    def execute(
        self,
        request: DataMigrationRequest,
        *,
        confirmation_token: str,
        confirmation_phrase: str,
        cancel_event: threading.Event | None = None,
        on_progress: MigrationProgressCallback | None = None,
    ) -> dict[str, object]:
        self._require_windows()
        if confirmation_phrase != MIGRATION_CONFIRMATION_PHRASE:
            raise DataMigrationValidationError(
                f"confirmation_phrase must be {MIGRATION_CONFIRMATION_PHRASE!r}."
            )
        preview = self._consume_preview(confirmation_token)
        if preview.request != request:
            raise DataMigrationValidationError(
                "Migration request changed after preview; run precheck again."
            )
        if preview.precheck.blockers:
            raise DataMigrationValidationError(
                "Migration is blocked: " + "; ".join(preview.precheck.blockers)
            )
        cancellation = cancel_event or threading.Event()
        progress = on_progress or (lambda _payload: None)
        backup: Mapping[str, object] | None = None
        mutation_started = False
        _emit_progress(progress, "precheck", 5, "正在重新检查迁移来源。")
        current = self._operations.precheck(request)
        if current.fingerprint != preview.precheck.fingerprint:
            raise DataMigrationValidationError(
                "Migration source changed after preview; run precheck again."
            )
        if current.blockers:
            raise DataMigrationValidationError(
                "Migration is now blocked: " + "; ".join(current.blockers)
            )
        _raise_if_cancelled(cancellation)
        try:
            _emit_progress(progress, "backup", 15, "正在创建完整恢复备份。")
            backup = self._operations.create_backup(request, current)
            _assert_zero_api_requests(backup)
            _emit_progress(
                progress,
                "backup_complete",
                25,
                "完整恢复备份已创建。",
                details={"backup": dict(backup)},
            )
            _raise_if_cancelled(cancellation)

            # Large backups can take minutes. Verify the source again after
            # the copy completes so no concurrent writer can change migration
            # input between the last dry-run and the first mutation.
            after_backup = self._operations.precheck(request)
            if after_backup.fingerprint != current.fingerprint:
                raise DataMigrationValidationError(
                    "Migration source changed while the backup was created; "
                    "run precheck again."
                )
            if after_backup.blockers:
                raise DataMigrationValidationError(
                    "Migration became blocked while the backup was created: "
                    + "; ".join(after_backup.blockers)
                )

            mutation_started = True
            _emit_progress(progress, "migrate", 35, "正在执行本地数据迁移。")
            applied = self._operations.apply(
                request,
                cancel_event=cancellation,
                on_progress=progress,
            )
            _assert_zero_api_requests(applied)

            # An atomic migration may finish after cancellation was requested.
            # Always verify that completed mutation instead of leaving an
            # unknown on-disk state or pretending it was rolled back.
            cancellation_deferred = cancellation.is_set()
            _emit_progress(progress, "verify", 85, "正在校验文档数与本地搜索。")
            verification = self._operations.verify(request, current)
            _assert_zero_api_requests(verification)
            _validate_verification(current, verification)
            _emit_progress(progress, "complete", 100, "迁移与校验已完成。")
            return {
                "status": "succeeded",
                "migration_type": request.migration_type,
                "source": current.source_display,
                "target": current.target_display,
                "backup": dict(backup),
                "migration": dict(applied),
                "verification": dict(verification),
                "cancellation_requested": cancellation_deferred,
                "cancellation_deferred": cancellation_deferred,
                "vectors_recomputed": 0,
                "model_api_requests": 0,
                "api_requests": 0,
            }
        except Exception as exc:
            # Cancellation is only a clean terminal state while no mutation has
            # begun.  Some multi-step migrations can observe cancellation after
            # writing one library or exporting a Docker Workspace; those cases
            # must follow the same verified-backup recovery path as any other
            # execution failure.
            if isinstance(exc, DataMigrationCancelled) and not mutation_started:
                raise
            recovery: dict[str, object] = {
                "status": "not_required" if not mutation_started else "unavailable"
            }
            if mutation_started and backup is not None:
                try:
                    _emit_progress(
                        progress,
                        "restore",
                        90,
                        "迁移失败，正在从完整备份安全恢复。",
                    )
                    recovery = dict(self._operations.restore(request, backup))
                    _assert_zero_api_requests(recovery)
                    if recovery.get("status") in {"restored", "source_untouched"}:
                        _emit_progress(
                            progress,
                            "restore_complete",
                            100,
                            "迁移失败，但完整备份已安全恢复。",
                        )
                except Exception as restore_error:
                    recovery = {
                        "status": "failed",
                        "error": _bounded_error(restore_error),
                    }
            report = {
                "status": (
                    "failed_recovered"
                    if recovery.get("status") in {"restored", "source_untouched"}
                    else "needs_attention"
                    if mutation_started
                    else "failed"
                ),
                "migration_type": request.migration_type,
                "error": _bounded_error(exc),
                "backup": dict(backup or {}),
                "recovery": recovery,
                "vectors_recomputed": 0,
                "model_api_requests": 0,
                "api_requests": 0,
            }
            raise DataMigrationExecutionError(
                (
                    "Data migration was cancelled after mutation began; "
                    "recovery details are available."
                    if isinstance(exc, DataMigrationCancelled)
                    else "Data migration failed; recovery details are available."
                ),
                report,
            ) from exc

    def recover(
        self,
        request: DataMigrationRequest,
        backup: Mapping[str, object],
        *,
        on_progress: MigrationProgressCallback | None = None,
    ) -> dict[str, object]:
        """Restore one durably recorded full backup without invoking a model."""

        self._require_windows()
        progress = on_progress or (lambda _payload: None)
        try:
            _emit_progress(
                progress,
                "restore",
                20,
                "正在校验完整备份并恢复迁移前数据。",
            )
            restored = self._operations.restore(request, backup)
            _assert_zero_api_requests(restored)
            _emit_progress(
                progress,
                "recovery_complete",
                100,
                "完整备份已恢复。",
            )
            return {
                "status": "restored",
                "migration_type": request.migration_type,
                "backup": dict(backup),
                "recovery": dict(restored),
                "vectors_recomputed": 0,
                "model_api_requests": 0,
                "api_requests": 0,
            }
        except Exception as exc:
            report = {
                "status": "needs_attention",
                "migration_type": request.migration_type,
                "error": _bounded_error(exc),
                "backup": dict(backup),
                "recovery": {"status": "failed"},
                "vectors_recomputed": 0,
                "model_api_requests": 0,
                "api_requests": 0,
            }
            raise DataMigrationExecutionError(
                "Migration recovery failed; the backup marker was retained.",
                report,
            ) from exc

    def _consume_preview(self, token: str) -> DataMigrationPreview:
        normalized = str(token).strip()
        if not normalized:
            raise DataMigrationValidationError("confirmation_token is required.")
        now = time.time()
        with self._lock:
            self._prune_previews(now)
            preview = self._previews.pop(normalized, None)
        if preview is None:
            raise DataMigrationValidationError(
                "Migration preview is missing, expired, or already used."
            )
        return preview

    def _prune_previews(self, now: float) -> None:
        expired = [
            token
            for token, preview in self._previews.items()
            if preview.expires_at_epoch <= now
        ]
        for token in expired:
            self._previews.pop(token, None)

    def _require_windows(self) -> None:
        if self._platform_name.casefold() != "windows":
            raise DataMigrationValidationError(
                "Desktop data migration is supported only on Windows."
            )


class WindowsNativeMigrationOperations:
    """Use existing local migration primitives without invoking any AI model."""

    def __init__(
        self,
        *,
        config_home: Path,
        config_path: Path,
        results_directory: Path,
    ) -> None:
        self.config_home = config_home.expanduser().resolve()
        self.config_path = config_path.expanduser().resolve()
        self.results_directory = results_directory.expanduser().resolve()

    def precheck(self, request: DataMigrationRequest) -> NativeMigrationPrecheck:
        source_display = request.source
        target_display = request.target
        blockers: list[str] = []
        warnings: list[str] = []
        details: dict[str, object] = {"migration_type": request.migration_type}
        inspections: list[WorkspaceInspection] = []

        try:
            if request.migration_type == "schema":
                workspace = _windows_path(request.source, "source")
                target = _windows_path(request.target, "target")
                if not _same_path(workspace, target):
                    blockers.append("Schema migration must target the same Workspace.")
                inspections.append(_inspect_workspace(workspace))
            elif request.migration_type == "root":
                workspace = _windows_path(
                    request.workspace_directory, "workspace_directory"
                )
                old_root = _windows_path(request.source, "source")
                new_root = _windows_path(request.target, "target")
                if not new_root.is_dir():
                    blockers.append(f"New image root is not accessible: {new_root}")
                if not old_root.exists():
                    warnings.append(
                        "The old image root is unavailable; root id will be used."
                    )
                inspections.append(_inspect_workspace(workspace))
                root_options = _read_root_options(workspace)
                details["root_options"] = root_options
                try:
                    details["root_id"] = _select_root_id(
                        root_options,
                        requested_root_id=request.root_id,
                        source_path=old_root,
                    )
                except DataMigrationValidationError as exc:
                    blockers.append(str(exc))
            elif request.migration_type == "legacy_config":
                source_config = _windows_path(request.source, "source")
                target_config = _windows_path(request.target, "target")
                source_display = str(source_config)
                target_display = str(target_config)
                native = _native_config_from_legacy(source_config)
                details["library_count"] = len(native.libraries)
                details["config_schema_target"] = 3
                for library in native.libraries:
                    inspections.append(_inspect_workspace(library.workspace_directory))
            else:
                target_workspace = _windows_path(request.target, "target")
                if shutil.which("docker") is None:
                    blockers.append(
                        "Docker CLI is required only for this one-time named-volume "
                        "export."
                    )
                # Validate how the exported Workspace will be attached before
                # Docker copies a single byte.  With a native schema-v3 config,
                # an arbitrary target would otherwise become an orphan directory
                # while the desktop kept opening the library's old Workspace.
                self._docker_library(request, target_workspace)
                if target_workspace.exists() and any(target_workspace.iterdir()):
                    try:
                        import zvec_launcher

                        zvec_launcher._validate_reusable_export(  # noqa: SLF001
                            target_workspace, request.source
                        )
                    except Exception as exc:
                        blockers.append(_bounded_error(exc))
                inspections.append(_inspect_workspace(target_workspace))
                details["docker_volume"] = request.source
                details["docker_image"] = request.docker_image
        except Exception as exc:
            blockers.append(_bounded_error(exc))

        for inspection in inspections:
            blockers.extend(inspection.blockers)
            warnings.extend(inspection.warnings)
        collection_documents = _sum_optional(
            inspection.collection_documents for inspection in inspections
        )
        sqlite_entries = _sum_optional(
            inspection.sqlite_entries for inspection in inspections
        )
        collection_status = _combined_status(
            inspection.status for inspection in inspections
        )
        sqlite_status = _combined_status(
            inspection.sqlite_integrity for inspection in inspections
        )
        fingerprint = _fingerprint_precheck(
            request,
            inspections,
            extra={"details": details, "blockers": blockers},
        )
        backup_plan = self._backup_plan(request, inspections)
        blockers.extend(
            str(value) for value in _iter_values(backup_plan.get("blockers"))
        )
        warnings.extend(
            str(value) for value in _iter_values(backup_plan.get("warnings"))
        )
        unique_blockers = tuple(dict.fromkeys(blockers))
        unique_warnings = tuple(dict.fromkeys(warnings))
        return NativeMigrationPrecheck(
            status="blocked" if unique_blockers else "ready",
            source_display=source_display,
            target_display=target_display,
            collection_documents=collection_documents,
            sqlite_entries=sqlite_entries,
            collection_status=collection_status,
            sqlite_status=sqlite_status,
            fingerprint=fingerprint,
            backup_plan=backup_plan,
            details={
                **details,
                "workspaces": [inspection.to_dict() for inspection in inspections],
                "dry_run": True,
                "vectors_recomputed": 0,
                "model_api_requests": 0,
            },
            blockers=unique_blockers,
            warnings=unique_warnings,
            api_requests=0,
        )

    def create_backup(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]:
        plan = precheck.backup_plan
        destination = Path(str(plan["destination"]))
        sources = self._backup_sources(request)
        config_path = self._source_config_path(request)
        return create_migration_backup(
            config_path=config_path,
            sources=sources,
            destination=destination,
            full_backup=True,
        )

    def apply(
        self,
        request: DataMigrationRequest,
        *,
        cancel_event: threading.Event,
        on_progress: MigrationProgressCallback,
    ) -> Mapping[str, object]:
        _raise_if_cancelled(cancel_event)
        if request.migration_type == "schema":
            workspace = _windows_path(request.source, "source")
            report = migrate_schema(self._service_config(workspace), dry_run=False)
            return {"schema_migration": report, "api_requests": 0}
        if request.migration_type == "root":
            workspace = _windows_path(
                request.workspace_directory, "workspace_directory"
            )
            from . import ImageVectorService

            service = ImageVectorService(config=self._service_config(workspace))
            try:
                root_id, _options = _resolve_root_id(
                    workspace,
                    requested_root_id=request.root_id,
                    source_path=_windows_path(request.source, "source"),
                )
                report = service.rebind_root(root_id, request.target)
            finally:
                service.close()
            return {"root_rebind": report, "api_requests": 0}
        if request.migration_type == "legacy_config":
            native = _native_config_from_legacy(_windows_path(request.source, "source"))
            reports = []
            for index, library in enumerate(native.libraries, start=1):
                _raise_if_cancelled(cancel_event)
                _emit_progress(
                    on_progress,
                    "migrate",
                    35 + int(index / max(1, len(native.libraries)) * 35),
                    f"正在迁移图库 {library.name}。",
                )
                reports.append(
                    _repair_native_library(library, native.results_directory)
                )
            _atomic_json(
                _windows_path(request.target, "target"),
                native.to_dict(),
            )
            return {
                "config_schema": 3,
                "libraries": reports,
                "api_requests": 0,
            }

        import zvec_launcher

        target = _windows_path(request.target, "target")
        export = zvec_launcher.export_docker_volume(
            request.source,
            request.docker_image,
            target,
        )
        _raise_if_cancelled(cancel_event)
        library, native = self._docker_library(request, target)
        repaired = _repair_native_library(library, native.results_directory)
        if self._source_config_path(request).is_file():
            _atomic_json(self.config_path, native.to_dict())
        return {
            "docker_export": export,
            "workspace_migration": repaired,
            "config_schema": 3,
            "api_requests": 0,
        }

    def verify(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]:
        if request.migration_type == "schema":
            reports = [
                _verify_workspace(
                    _windows_path(request.source, "source"),
                    self._service_config(_windows_path(request.source, "source")),
                )
            ]
        elif request.migration_type == "root":
            workspace = _windows_path(
                request.workspace_directory, "workspace_directory"
            )
            root_id, _options = _resolve_root_id(
                workspace,
                requested_root_id=request.root_id,
                source_path=_windows_path(request.source, "source"),
            )
            reports = [
                _verify_workspace(
                    workspace,
                    self._service_config(workspace),
                    expected_root_id=root_id,
                    expected_root_path=_windows_path(request.target, "target"),
                )
            ]
        elif request.migration_type == "legacy_config":
            native = _load_native_config(_windows_path(request.target, "target"))
            reports = [
                _verify_workspace(
                    library.workspace_directory,
                    self._service_config(library.workspace_directory),
                    expected_root_path=library.image_root,
                )
                for library in native.libraries
            ]
        else:
            target = _windows_path(request.target, "target")
            library, _native = self._docker_library(request, target)
            reports = [
                _verify_workspace(
                    target,
                    self._service_config(target),
                    expected_root_path=library.image_root,
                )
            ]
        return _aggregate_verification(reports)

    def restore(
        self,
        request: DataMigrationRequest,
        backup: Mapping[str, object],
    ) -> Mapping[str, object]:
        destination = backup.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            raise DataMigrationError("Backup receipt has no destination.")
        expected_sources = self._backup_sources(request)
        report = restore_full_migration_backup(
            Path(destination),
            expected_config_path=self._source_config_path(request),
            expected_workspaces={
                source.library_id: source.workspace for source in expected_sources
            },
        )
        if request.migration_type == "docker_workspace":
            target = _windows_path(request.target, "target")
            backed_target = any(
                _same_path(Path(str(item.get("workspace") or "")), target)
                and bool(item.get("collection_included"))
                for item in _iter_values(backup.get("libraries"))
                if isinstance(item, Mapping)
            )
            if not backed_target and target.exists():
                quarantine = target.with_name(
                    f"{target.name}.failed-migration-{uuid.uuid4().hex[:8]}"
                )
                target.replace(quarantine)
                report["failed_export_quarantine"] = str(quarantine)
                report["source_volume_untouched"] = True
        return report

    def _backup_plan(
        self,
        request: DataMigrationRequest,
        inspections: Sequence[WorkspaceInspection],
    ) -> Mapping[str, object]:
        destination = (
            _windows_path(request.backup_directory, "backup_directory")
            if request.backup_directory
            else new_backup_destination(self.config_home)
        )
        sources = self._backup_sources(request)
        try:
            return plan_migration_backup(
                config_path=self._source_config_path(request),
                sources=sources,
                destination=destination,
                full_backup=True,
            )
        except Exception as exc:
            return {
                "status": "blocked",
                "destination": str(destination),
                "blockers": [_bounded_error(exc)],
                "warnings": [],
                "libraries": [inspection.to_dict() for inspection in inspections],
                "api_requests": 0,
            }

    def _backup_sources(
        self, request: DataMigrationRequest
    ) -> list[WorkspaceBackupSource]:
        if request.migration_type == "schema":
            return [
                WorkspaceBackupSource(
                    request.library_id or "migration-library",
                    request.library_name or "图库",
                    _windows_path(request.source, "source"),
                )
            ]
        if request.migration_type == "root":
            return [
                WorkspaceBackupSource(
                    request.library_id or "migration-library",
                    request.library_name or "图库",
                    _windows_path(request.workspace_directory, "workspace_directory"),
                )
            ]
        if request.migration_type == "legacy_config":
            native = _native_config_from_legacy(_windows_path(request.source, "source"))
            return [
                WorkspaceBackupSource(
                    library.library_id,
                    library.name,
                    library.workspace_directory,
                )
                for library in native.libraries
            ]
        return [
            WorkspaceBackupSource(
                request.library_id or "docker-workspace",
                request.library_name or "Docker 图库",
                _windows_path(request.target, "target"),
            )
        ]

    def _source_config_path(self, request: DataMigrationRequest) -> Path:
        del request
        return self.config_path

    def _service_config(self, workspace: Path) -> ServiceConfig:
        return ServiceConfig(
            workspace=workspace,
            config_home=self.config_home,
            results_directory=self.results_directory,
        )

    def _docker_library(self, request: DataMigrationRequest, target: Path):
        import zvec_launcher

        if self.config_path.is_file():
            payload = _read_json_object(self.config_path)
            if payload.get("schema_version") in {1, 2}:
                raw_libraries, _default = zvec_launcher._legacy_libraries(payload)  # noqa: SLF001
                selected = next(
                    (
                        raw
                        for raw in raw_libraries
                        if str(raw.get("workspace_source") or "") == request.source
                        or str(raw.get("id") or "") == request.library_id
                    ),
                    None,
                )
                if selected is None:
                    raise DataMigrationValidationError(
                        "Docker volume is not present in the legacy config."
                    )
                native = zvec_launcher._native_from_legacy(  # noqa: SLF001
                    payload,
                    {
                        str(selected.get("id") or ""): target,
                        request.source: target,
                    },
                )
                return native.select(str(selected.get("id") or "")), native
            native = zvec_launcher.validate_native_config(payload)
            library = native.select(request.library_id or None)
            if not _same_path(library.workspace_directory, target):
                raise DataMigrationValidationError(
                    "Docker Workspace target must match the selected library's "
                    "configured Workspace directory."
                )
            return library, native
        if not request.image_root:
            raise DataMigrationValidationError(
                "Docker migration requires image_root when no config exists."
            )
        library_id = request.library_id or "docker-migrated-library"
        library = zvec_launcher.NativeLibrary(
            library_id=library_id,
            name=request.library_name or "Docker 图库",
            image_root=_windows_path(request.image_root, "image_root"),
            workspace_directory=target,
            enabled=True,
        )
        native = zvec_launcher.NativeConfig(
            default_library_id=library.library_id,
            results_directory=self.results_directory,
            libraries=(library,),
        )
        return library, native


def restore_full_migration_backup(
    backup_directory: Path,
    *,
    expected_config_path: Path | None = None,
    expected_workspaces: Mapping[str, Path] | None = None,
) -> dict[str, object]:
    """Restore a verified full backup using staged, rollback-capable swaps."""

    root = backup_directory.expanduser().resolve()
    if expected_config_path is None or expected_workspaces is None:
        raise DataMigrationValidationError(
            "Recovery requires trusted config and Workspace targets."
        )
    trusted_config = _trusted_restore_path(expected_config_path, "expected_config_path")
    trusted_workspaces = _trusted_restore_workspaces(expected_workspaces)
    manifest_path = root / MANIFEST_FILE_NAME
    manifest = _read_json_object(manifest_path)
    if (
        manifest.get("kind") != "zvec_workspace_migration_backup"
        or manifest.get("status") != "complete"
        or manifest.get("backup_mode") != "full"
    ):
        raise DataMigrationValidationError(
            "Recovery requires a complete full migration backup."
        )
    _verify_backup_files(root, manifest)
    raw_libraries = manifest.get("libraries")
    if not isinstance(raw_libraries, list):
        raise DataMigrationValidationError("Backup libraries manifest is invalid.")
    manifest_library_ids: set[str] = set()
    restore_plan: list[tuple[str, Path, Path]] = []
    for raw in raw_libraries:
        if not isinstance(raw, Mapping):
            raise DataMigrationValidationError("Backup library entry is invalid.")
        library_id = str(raw.get("library_id") or "").strip()
        try:
            library_id = validate_backup_library_id(library_id)
        except WorkspaceBackupError as exc:
            raise DataMigrationValidationError(
                "Backup library id is not a safe path segment."
            ) from exc
        if library_id in manifest_library_ids:
            raise DataMigrationValidationError(
                f"Backup library id is duplicated: {library_id}"
            )
        manifest_library_ids.add(library_id)
        trusted_workspace = trusted_workspaces.get(library_id)
        if trusted_workspace is None:
            raise DataMigrationValidationError(
                f"Backup contains an unexpected library target: {library_id}"
            )
        raw_workspace = raw.get("workspace")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            raise DataMigrationValidationError("Backup library path is invalid.")
        manifest_workspace = _trusted_restore_path(
            Path(raw_workspace), f"manifest workspace for {library_id}"
        )
        if not _same_path(manifest_workspace, trusted_workspace):
            raise DataMigrationValidationError(
                "Backup Workspace target does not match the trusted target: "
                f"{library_id}"
            )
        try:
            source = resolve_backup_library_directory(root, library_id)
        except WorkspaceBackupError as exc:
            raise DataMigrationValidationError(
                "Backup library id is not a safe path segment."
            ) from exc
        restore_plan.append((library_id, source, trusted_workspace))

    missing = set(trusted_workspaces) - manifest_library_ids
    if missing:
        raise DataMigrationValidationError(
            "Backup is missing trusted library targets: " + ", ".join(sorted(missing))
        )

    config_source = root / "config" / "config.json"
    source_config = manifest.get("source_config")
    if not isinstance(source_config, str) or not source_config.strip():
        raise DataMigrationValidationError("Backup source_config is invalid.")
    manifest_config = _trusted_restore_path(
        Path(source_config), "manifest source_config"
    )
    if not _same_path(manifest_config, trusted_config):
        raise DataMigrationValidationError(
            "Backup config target does not match the trusted config path."
        )

    # Finish validating every manifest-controlled source and destination before
    # replacing a single live artifact.  A tampered later entry must not leave
    # an earlier Workspace partially restored.
    restored = [
        _restore_workspace_artifacts(source, workspace, library_id)
        for library_id, source, workspace in restore_plan
    ]
    _atomic_replace_from_backup(config_source, trusted_config)
    return {
        "status": "restored",
        "backup": str(root),
        "config": str(trusted_config),
        "libraries": restored,
        "api_requests": 0,
    }


def _trusted_restore_workspaces(
    values: Mapping[str, Path],
) -> dict[str, Path]:
    if not isinstance(values, Mapping) or not values:
        raise DataMigrationValidationError(
            "Recovery requires at least one trusted Workspace target."
        )
    trusted: dict[str, Path] = {}
    for raw_library_id, raw_workspace in values.items():
        try:
            library_id = validate_backup_library_id(raw_library_id)
        except WorkspaceBackupError as exc:
            raise DataMigrationValidationError(
                "Trusted library id is not a safe path segment."
            ) from exc
        if library_id in trusted:
            raise DataMigrationValidationError(
                f"Trusted library id is duplicated: {library_id}"
            )
        trusted[library_id] = _trusted_restore_path(
            raw_workspace, f"trusted Workspace for {library_id}"
        )
    return trusted


def _trusted_restore_path(value: Path, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DataMigrationValidationError(f"{name} must be an absolute path.")
    return path.resolve()


def _inspect_workspace(workspace: Path) -> WorkspaceInspection:
    workspace = workspace.expanduser().resolve()
    collection = workspace / COLLECTION_DIRECTORY_NAME
    metadata = workspace / METADATA_FILE_NAME
    state = workspace / STATE_FILE_NAME
    present = (collection.is_dir(), metadata.is_file(), state.is_file())
    blockers: list[str] = []
    warnings: list[str] = []
    schema_version: int | None = None
    collection_documents: int | None = None
    sqlite_entries: int | None = None
    sqlite_integrity = "missing"
    collection_readable = False
    if not workspace.exists():
        status = "missing"
        warnings.append(f"Workspace does not exist yet: {workspace}")
    elif not any(present):
        status = "empty"
        warnings.append("Workspace has no existing index.")
    elif not all(present):
        status = "incomplete"
        blockers.append(
            "Workspace is incomplete; Collection, metadata, and SQLite must all exist."
        )
    else:
        status = "ready"
        try:
            payload = _read_json_object(metadata)
            raw_version = payload.get("schema_version")
            schema_version = int(raw_version) if raw_version is not None else None
        except Exception as exc:
            blockers.append(f"Collection metadata is unreadable: {_bounded_error(exc)}")
        try:
            sqlite_entries, sqlite_integrity = _sqlite_snapshot(state)
            if sqlite_integrity != "ok":
                blockers.append("SQLite quick_check failed: " + sqlite_integrity)
        except Exception as exc:
            sqlite_integrity = "unreadable"
            blockers.append(f"SQLite state is unreadable: {_bounded_error(exc)}")
        try:
            import zvec

            opened = zvec.open(str(collection))
            try:
                collection_documents = int(opened.stats.doc_count)
            finally:
                del opened
            collection_readable = True
        except Exception as exc:
            blockers.append(f"Vector Collection is unreadable: {_bounded_error(exc)}")
        if (
            collection_documents is not None
            and sqlite_entries is not None
            and collection_documents != sqlite_entries
        ):
            blockers.append(
                "Collection/SQLite document counts do not match: "
                f"{collection_documents} != {sqlite_entries}."
            )
    fingerprint = _workspace_fingerprint(workspace, present)
    return WorkspaceInspection(
        workspace=str(workspace),
        status=status,
        schema_version=schema_version,
        collection_documents=collection_documents,
        sqlite_entries=sqlite_entries,
        sqlite_integrity=sqlite_integrity,
        collection_readable=collection_readable,
        fingerprint=fingerprint,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def _resolve_root_id(
    workspace: Path,
    *,
    requested_root_id: str,
    source_path: Path,
) -> tuple[str, list[dict[str, str]]]:
    options = _read_root_options(workspace)
    return (
        _select_root_id(
            options,
            requested_root_id=requested_root_id,
            source_path=source_path,
        ),
        options,
    )


def _read_root_options(workspace: Path) -> list[dict[str, str]]:
    state_path = workspace / STATE_FILE_NAME
    if not state_path.is_file():
        raise DataMigrationValidationError(
            "Workspace SQLite state is missing; Root ID cannot be resolved."
        )
    connection = sqlite3.connect(
        f"{state_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    try:
        rows = connection.execute(
            "SELECT root_id, current_path FROM roots ORDER BY root_id"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise DataMigrationValidationError(
            "Workspace roots cannot be read before migration."
        ) from exc
    finally:
        connection.close()
    return [{"root_id": str(row[0]), "current_path": str(row[1])} for row in rows]


def _select_root_id(
    options: Sequence[Mapping[str, str]],
    *,
    requested_root_id: str,
    source_path: Path,
) -> str:
    requested = requested_root_id.strip()
    if requested:
        if any(option["root_id"] == requested for option in options):
            return requested
        raise DataMigrationValidationError(
            f"Requested Root ID does not exist in this Workspace: {requested}"
        )
    source_key = str(source_path).replace("\\", "/").rstrip("/").casefold()
    matching = [
        option
        for option in options
        if option["current_path"].replace("\\", "/").rstrip("/").casefold()
        == source_key
    ]
    if len(matching) == 1:
        return matching[0]["root_id"]
    if len(options) == 1:
        return options[0]["root_id"]
    if not options:
        raise DataMigrationValidationError("Workspace has no indexed roots to rebind.")
    raise DataMigrationValidationError(
        "Workspace contains multiple roots. Select a Root ID from precheck details."
    )


def _verify_workspace(
    workspace: Path,
    config: ServiceConfig,
    *,
    expected_root_id: str = "",
    expected_root_path: Path | None = None,
) -> dict[str, object]:
    from . import ImageVectorService

    service = ImageVectorService(config=config)
    try:
        stats = service.stats()
        collection_documents = int(stats["collection_stats"]["doc_count"])
        sqlite_entries = int(stats["tracked_files"])
        if collection_documents != sqlite_entries:
            raise DataMigrationError(
                "Collection/SQLite count mismatch after migration: "
                f"{collection_documents} != {sqlite_entries}."
            )
        roots = service.list_roots()
        if expected_root_id:
            matching = [
                root for root in roots if str(root.get("root_id")) == expected_root_id
            ]
            if len(matching) != 1:
                raise DataMigrationError(
                    "Expected root id was not found after migration: "
                    f"{expected_root_id}"
                )
            if expected_root_path is not None and not _same_path(
                Path(str(matching[0]["current_path"])), expected_root_path
            ):
                raise DataMigrationError("Root path verification failed.")
        elif expected_root_path is not None and roots:
            if not any(
                _same_path(
                    Path(str(root.get("current_path") or "")),
                    expected_root_path,
                )
                for root in roots
            ):
                raise DataMigrationError("Image root verification failed.")
        search_probe = "not_applicable_empty_collection"
        if sqlite_entries:
            entry = service.state.list_entries()[0]
            vector = service.repository.fetch_vector(str(entry["doc_id"]))
            if vector is None:
                raise DataMigrationError("Stored vector is missing after migration.")
            hits = service.repository.query(vector, top_k=1)
            if not hits:
                raise DataMigrationError(
                    "Stored-vector search probe returned no result."
                )
            search_probe = "passed"
        return {
            "workspace": str(workspace),
            "collection_documents": collection_documents,
            "sqlite_entries": sqlite_entries,
            "sqlite_integrity": "ok",
            "search_probe": search_probe,
            "root_count": len(roots),
            "api_requests": 0,
        }
    finally:
        service.close()


def _aggregate_verification(
    reports: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "status": "verified",
        "collection_documents": sum(
            _optional_int(report.get("collection_documents")) or 0 for report in reports
        ),
        "sqlite_entries": sum(
            _optional_int(report.get("sqlite_entries")) or 0 for report in reports
        ),
        "sqlite_integrity": (
            "ok"
            if all(report.get("sqlite_integrity") == "ok" for report in reports)
            else "failed"
        ),
        "search_probe": (
            "passed"
            if all(
                report.get("search_probe")
                in {"passed", "not_applicable_empty_collection"}
                for report in reports
            )
            else "failed"
        ),
        "workspaces": [dict(report) for report in reports],
        "api_requests": 0,
    }


def _validate_verification(
    precheck: NativeMigrationPrecheck,
    verification: Mapping[str, object],
) -> None:
    before_collection = precheck.collection_documents
    before_sqlite = precheck.sqlite_entries
    after_collection = _optional_int(verification.get("collection_documents"))
    after_sqlite = _optional_int(verification.get("sqlite_entries"))
    if before_collection is not None and after_collection != before_collection:
        raise DataMigrationError(
            "Collection document count changed during migration: "
            f"{before_collection} != {after_collection}."
        )
    if before_sqlite is not None and after_sqlite != before_sqlite:
        raise DataMigrationError(
            "SQLite entry count changed during migration: "
            f"{before_sqlite} != {after_sqlite}."
        )
    if verification.get("sqlite_integrity") != "ok":
        raise DataMigrationError("SQLite integrity verification failed.")
    if verification.get("search_probe") not in {
        "passed",
        "not_applicable_empty_collection",
    }:
        raise DataMigrationError("Local stored-vector search verification failed.")


def _native_config_from_legacy(path: Path):
    import zvec_launcher

    payload = _read_json_object(path)
    if payload.get("schema_version") == zvec_launcher.CONFIG_SCHEMA_VERSION:
        raise DataMigrationValidationError("Config is already native schema v3.")
    try:
        return zvec_launcher._native_from_legacy(payload)  # noqa: SLF001
    except zvec_launcher.LegacyDockerVolumeRequired as exc:
        raise DataMigrationValidationError(
            "Legacy config contains Docker named volumes; export them with the "
            "Docker workspace migration first."
        ) from exc


def _load_native_config(path: Path):
    import zvec_launcher

    return zvec_launcher.validate_native_config(_read_json_object(path))


def _repair_native_library(library: Any, results_directory: Path):
    import zvec_launcher

    report = zvec_launcher.repair_and_verify_workspace(
        library,
        results_directory,
        dry_run=False,
    )
    _assert_zero_api_requests(report)
    return report


def _restore_workspace_artifacts(
    source: Path,
    workspace: Path,
    library_id: str,
) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(workspace / ".image_collection.lock")
    lock.acquire()
    swaps: list[tuple[Path, Path, Path | None]] = []
    try:
        for name in (COLLECTION_DIRECTORY_NAME, METADATA_FILE_NAME, STATE_FILE_NAME):
            backup_item = source / name
            if not backup_item.exists():
                continue
            target = workspace / name
            staged = workspace / f".{name}.restore-{uuid.uuid4().hex}"
            failed = (
                workspace / f".{name}.failed-{uuid.uuid4().hex}"
                if target.exists()
                else None
            )
            if backup_item.is_dir():
                shutil.copytree(backup_item, staged)
            else:
                shutil.copy2(backup_item, staged)
            if failed is not None:
                target.replace(failed)
            try:
                staged.replace(target)
            except Exception:
                if failed is not None and failed.exists() and not target.exists():
                    failed.replace(target)
                raise
            swaps.append((target, staged, failed))
        for suffix in ("-wal", "-shm"):
            stale = workspace / f"{STATE_FILE_NAME}{suffix}"
            if stale.exists():
                stale.unlink()
    except Exception:
        for target, _staged, failed in reversed(swaps):
            if target.exists():
                _remove_known_artifact(target, workspace)
            if failed is not None and failed.exists():
                failed.replace(target)
        raise
    finally:
        lock.release()
    for _target, _staged, failed in swaps:
        if failed is not None and failed.exists():
            _remove_known_artifact(failed, workspace)
    return {
        "library_id": library_id,
        "workspace": str(workspace),
        "restored_artifacts": len(swaps),
    }


def _atomic_replace_from_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise DataMigrationValidationError(f"Backup file is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    shutil.copy2(source, temporary)
    temporary.replace(target)


def _verify_backup_files(root: Path, manifest: Mapping[str, object]) -> None:
    records = manifest.get("files")
    if not isinstance(records, list):
        raise DataMigrationValidationError("Backup file manifest is invalid.")
    for raw in records:
        if not isinstance(raw, Mapping):
            raise DataMigrationValidationError("Backup file record is invalid.")
        relative = raw.get("path")
        digest = raw.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise DataMigrationValidationError("Backup file record is incomplete.")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise DataMigrationValidationError(
                "Backup file path escapes its backup directory."
            ) from exc
        if not path.is_file() or _sha256_file(path) != digest:
            raise DataMigrationValidationError(
                f"Backup file verification failed: {relative}"
            )


def _remove_known_artifact(path: Path, workspace: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise DataMigrationValidationError(
            "Refusing to remove an artifact outside the Workspace."
        ) from exc
    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink(missing_ok=True)


def _workspace_fingerprint(workspace: Path, present: Sequence[bool]) -> str:
    digest = hashlib.sha256()
    digest.update(os.path.normcase(str(workspace)).encode("utf-8"))
    digest.update(bytes(present))
    for name in (METADATA_FILE_NAME, STATE_FILE_NAME):
        path = workspace / name
        if path.is_file():
            metadata = path.stat()
            digest.update(name.encode("utf-8"))
            digest.update(str(metadata.st_size).encode("ascii"))
            digest.update(str(metadata.st_mtime_ns).encode("ascii"))
    collection = workspace / COLLECTION_DIRECTORY_NAME
    if collection.is_dir():
        file_count = 0
        total_size = 0
        latest_mtime = 0
        for item in collection.rglob("*"):
            if not item.is_file():
                continue
            metadata = item.stat()
            file_count += 1
            total_size += metadata.st_size
            latest_mtime = max(latest_mtime, metadata.st_mtime_ns)
        digest.update(f"{file_count}:{total_size}:{latest_mtime}".encode("ascii"))
    return digest.hexdigest()


def _fingerprint_precheck(
    request: DataMigrationRequest,
    inspections: Sequence[WorkspaceInspection],
    *,
    extra: Mapping[str, object],
) -> str:
    payload = {
        "request": request.to_dict(),
        "workspaces": [inspection.fingerprint for inspection in inspections],
        "extra": extra,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _sqlite_snapshot(path: Path) -> tuple[int, str]:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        status = str(integrity[0]).lower() if integrity else "unknown"
        row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
        return (int(row[0]) if row else 0), status
    finally:
        connection.close()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataMigrationValidationError(f"Invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise DataMigrationValidationError(f"JSON file must contain an object: {path}")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _windows_path(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DataMigrationValidationError(f"{name} must be a Windows path.")
    pure = PureWindowsPath(value.strip())
    if not pure.is_absolute():
        raise DataMigrationValidationError(f"{name} must be an absolute Windows path.")
    return Path(value).expanduser().resolve()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _sum_optional(values: Iterable[int | None]) -> int | None:
    resolved = list(values)
    return (
        sum(value for value in resolved if value is not None)
        if any(value is not None for value in resolved)
        else None
    )


def _combined_status(values: Iterable[str]) -> str:
    resolved = list(values)
    if not resolved:
        return "not_applicable"
    if all(value in {"ready", "ok", "empty"} for value in resolved):
        return "ready" if "ready" in resolved else "ok"
    return ",".join(sorted(set(resolved)))


def _assert_zero_api_requests(value: Mapping[str, object]) -> None:
    for key, nested in _walk_mapping(value):
        if key == "api_requests" and _optional_int(nested) not in {None, 0}:
            raise DataMigrationError(
                "Migration attempted an external API request; operation aborted."
            )


def _walk_mapping(value: object):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk_mapping(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_mapping(nested)


def _emit_progress(
    callback: MigrationProgressCallback,
    stage: str,
    progress: int,
    message: str,
    *,
    details: Mapping[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "stage": stage,
        "progress": max(0, min(100, int(progress))),
        "message": message,
    }
    if details:
        payload.update(details)
    callback(payload)


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise DataMigrationCancelled("Data migration was cancelled before mutation.")


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _strict_boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise DataMigrationValidationError(f"{name} must be a boolean.")
    return value


def _iter_values(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _bounded_error(error: object) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    return text[:1_000] or error.__class__.__name__


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
