from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigurationError
from .process_lock import ProcessLock

BACKUP_SCHEMA_VERSION = 1
MANIFEST_FILE_NAME = "migration-backup-manifest.json"
STATE_FILE_NAME = "image_collection.state.sqlite3"
METADATA_FILE_NAME = "image_collection.meta.json"
COLLECTION_DIRECTORY_NAME = "image_collection"
SMALL_STATE_FILES = (
    METADATA_FILE_NAME,
    "image_collection.state.json",
    "search-quality.json",
)
MINIMUM_FREE_SPACE_RESERVE = 16 * 1024 * 1024


class WorkspaceBackupError(RuntimeError):
    """Raised when a migration backup cannot be created safely."""


@dataclass(frozen=True)
class WorkspaceBackupSource:
    library_id: str
    name: str
    workspace: Path


def new_backup_destination(
    config_home: Path, base_directory: Path | None = None
) -> Path:
    root = (
        base_directory.expanduser().resolve()
        if base_directory is not None
        else config_home.expanduser().resolve() / "migration-backups"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / f"migration-{timestamp}-{uuid.uuid4().hex[:12]}"


def plan_migration_backup(
    *,
    config_path: Path,
    sources: list[WorkspaceBackupSource],
    destination: Path,
    full_backup: bool,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    destination = destination.expanduser().resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    libraries: list[dict[str, Any]] = []
    estimated_payload_bytes = 0

    if not config_path.is_file():
        blockers.append(f"Launcher config is missing: {config_path}")
    else:
        estimated_payload_bytes += config_path.stat().st_size

    if destination.exists():
        if not destination.is_dir():
            blockers.append(f"Backup destination is not a directory: {destination}")
        elif any(destination.iterdir()):
            blockers.append(f"Backup destination is not empty: {destination}")

    for source in sources:
        workspace = source.workspace.expanduser().resolve()
        library_blockers: list[str] = []
        library_warnings: list[str] = []
        included_bytes = 0
        collection_bytes: int | None = None
        collection_files: int | None = None
        has_collection = (workspace / COLLECTION_DIRECTORY_NAME).is_dir()
        has_metadata = (workspace / METADATA_FILE_NAME).is_file()
        has_state = (workspace / STATE_FILE_NAME).is_file()

        if _paths_overlap(destination, workspace):
            library_blockers.append(
                "Backup destination cannot be inside or contain the Workspace."
            )
        if not workspace.is_dir():
            library_warnings.append(
                "Workspace does not exist; only config will be backed up."
            )
        elif any((has_collection, has_metadata, has_state)) and not all(
            (has_collection, has_metadata, has_state)
        ):
            library_blockers.append(
                "Workspace is incomplete; Collection, metadata and SQLite state "
                "must either all exist or all be absent."
            )

        if workspace.is_dir():
            state_path = workspace / STATE_FILE_NAME
            if state_path.is_file():
                included_bytes += state_path.stat().st_size
                wal_path = state_path.with_name(state_path.name + "-wal")
                if wal_path.is_file():
                    included_bytes += wal_path.stat().st_size
                try:
                    sqlite_entries = _sqlite_entry_count(state_path)
                except (OSError, sqlite3.DatabaseError) as exc:
                    sqlite_entries = None
                    library_blockers.append(f"SQLite state cannot be read: {exc}")
            else:
                sqlite_entries = None
            for name in SMALL_STATE_FILES:
                path = workspace / name
                if path.is_file():
                    included_bytes += path.stat().st_size
            if has_collection and full_backup:
                try:
                    collection_bytes, collection_files = _directory_size(
                        workspace / COLLECTION_DIRECTORY_NAME
                    )
                except OSError as exc:
                    library_blockers.append(f"Collection cannot be measured: {exc}")
            if full_backup and collection_bytes is not None:
                included_bytes += collection_bytes
            elif has_collection:
                library_warnings.append(
                    "Vector Collection is excluded from the metadata backup; "
                    "the original Workspace remains the vector source."
                )
        else:
            sqlite_entries = None

        estimated_payload_bytes += included_bytes
        blockers.extend(f"{source.name}: {item}" for item in library_blockers)
        warnings.extend(f"{source.name}: {item}" for item in library_warnings)
        libraries.append(
            {
                "library_id": source.library_id,
                "name": source.name,
                "workspace": str(workspace),
                "has_index": all((has_collection, has_metadata, has_state)),
                "sqlite_entries": sqlite_entries,
                "included_bytes": included_bytes,
                "collection_bytes": collection_bytes,
                "collection_files": collection_files,
                "collection_included": bool(full_backup and has_collection),
                "blockers": library_blockers,
                "warnings": library_warnings,
            }
        )

    reserve = max(MINIMUM_FREE_SPACE_RESERVE, estimated_payload_bytes // 10)
    required_free_bytes = estimated_payload_bytes + reserve
    probe_root = _existing_ancestor(destination.parent)
    try:
        available_free_bytes = shutil.disk_usage(probe_root).free
    except OSError as exc:
        available_free_bytes = None
        blockers.append(f"Backup destination free space cannot be checked: {exc}")
    if available_free_bytes is not None and available_free_bytes < required_free_bytes:
        blockers.append(
            "Backup destination has insufficient free space: "
            f"requires {required_free_bytes}, available {available_free_bytes}."
        )
    writable = _can_write_directory(probe_root)
    if not writable:
        blockers.append(f"Backup destination is not writable: {probe_root}")

    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "status": "blocked" if blockers else "ready",
        "backup_mode": "full" if full_backup else "metadata",
        "destination": str(destination),
        "config_path": str(config_path),
        "estimated_payload_bytes": estimated_payload_bytes,
        "required_free_bytes": required_free_bytes,
        "available_free_bytes": available_free_bytes,
        "destination_writable": writable,
        "libraries": libraries,
        "blockers": blockers,
        "warnings": warnings,
        "api_requests": 0,
    }


def create_migration_backup(
    *,
    config_path: Path,
    sources: list[WorkspaceBackupSource],
    destination: Path,
    full_backup: bool,
) -> dict[str, Any]:
    plan = plan_migration_backup(
        config_path=config_path,
        sources=sources,
        destination=destination,
        full_backup=full_backup,
    )
    if plan["blockers"]:
        raise WorkspaceBackupError("; ".join(str(item) for item in plan["blockers"]))

    config_path = config_path.expanduser().resolve()
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _verify_directory_writable(destination.parent)
    config_digest_before = _sha256_file(config_path)
    staging = Path(
        tempfile.mkdtemp(prefix=".zvec-migration-backup-", dir=destination.parent)
    ).resolve()
    files: list[dict[str, Any]] = []
    library_reports: list[dict[str, Any]] = []
    locks: list[ProcessLock] = []
    try:
        for source in sources:
            workspace = source.workspace.expanduser().resolve()
            if not workspace.is_dir():
                continue
            lock = ProcessLock(workspace / ".image_collection.lock")
            lock.acquire()
            locks.append(lock)

        # Re-run the precheck while every existing Workspace is locked. In full
        # mode this also makes the potentially large size estimate consistent with
        # the Collection that will actually be copied.
        plan = plan_migration_backup(
            config_path=config_path,
            sources=sources,
            destination=destination,
            full_backup=full_backup,
        )
        if plan["blockers"]:
            raise WorkspaceBackupError(
                "; ".join(str(item) for item in plan["blockers"])
            )

        config_target = staging / "config" / "config.json"
        config_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, config_target)
        files.append(_file_record(staging, config_target, "launcher_config"))

        for source in sources:
            workspace = source.workspace.expanduser().resolve()
            target = staging / "libraries" / source.library_id
            target.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            if workspace.is_dir():
                for name in SMALL_STATE_FILES:
                    source_path = workspace / name
                    if source_path.is_file():
                        target_path = target / name
                        shutil.copy2(source_path, target_path)
                        files.append(
                            _file_record(staging, target_path, "workspace_state")
                        )
                        copied.append(name)
                state_path = workspace / STATE_FILE_NAME
                if state_path.is_file():
                    state_target = target / STATE_FILE_NAME
                    _backup_sqlite(state_path, state_target)
                    files.append(_file_record(staging, state_target, "sqlite_snapshot"))
                    copied.append(STATE_FILE_NAME)
                    sqlite_entries = _sqlite_entry_count(state_target)
                else:
                    sqlite_entries = None
                collection_source = workspace / COLLECTION_DIRECTORY_NAME
                if full_backup and collection_source.is_dir():
                    collection_target = target / COLLECTION_DIRECTORY_NAME
                    shutil.copytree(collection_source, collection_target)
                    for path in sorted(collection_target.rglob("*")):
                        if path.is_file():
                            files.append(
                                _file_record(staging, path, "vector_collection")
                            )
                    collection_included = True
                else:
                    collection_included = False
            else:
                sqlite_entries = None
                collection_included = False
            library_reports.append(
                {
                    "library_id": source.library_id,
                    "name": source.name,
                    "workspace": str(workspace),
                    "copied_state_files": copied,
                    "sqlite_entries": sqlite_entries,
                    "collection_included": collection_included,
                }
            )

        if _sha256_file(config_path) != config_digest_before:
            raise WorkspaceBackupError(
                "Launcher config changed while the migration backup was created."
            )
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "kind": "zvec_workspace_migration_backup",
            "status": "complete",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "backup_mode": "full" if full_backup else "metadata",
            "source_config": str(config_path),
            "source_config_sha256": config_digest_before,
            "files": files,
            "libraries": library_reports,
            "excluded": (
                []
                if full_backup
                else [
                    {
                        "library_id": source.library_id,
                        "path": COLLECTION_DIRECTORY_NAME,
                        "reason": "large_vector_collection_requires_full_backup",
                    }
                    for source in sources
                    if (source.workspace / COLLECTION_DIRECTORY_NAME).is_dir()
                ]
            ),
            "precheck": plan,
            "api_requests": 0,
        }
        manifest_path = staging / MANIFEST_FILE_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            destination.rmdir()
        staging.replace(destination)
        return {
            "status": "created",
            "backup_mode": manifest["backup_mode"],
            "destination": str(destination),
            "manifest": str(destination / MANIFEST_FILE_NAME),
            "file_count": len(files),
            "libraries": library_reports,
            "api_requests": 0,
        }
    except (OSError, sqlite3.DatabaseError, ConfigurationError) as exc:
        raise WorkspaceBackupError(f"Migration backup failed: {exc}") from exc
    finally:
        for lock in reversed(locks):
            lock.release()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=5)
    destination_connection = sqlite3.connect(destination, timeout=5)
    try:
        source_connection.backup(destination_connection)
        row = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise WorkspaceBackupError(
                f"SQLite backup integrity check failed: {destination}"
            )
    finally:
        destination_connection.close()
        source_connection.close()


def _sqlite_entry_count(path: Path) -> int:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    try:
        row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def _file_record(root: Path, path: Path, role: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "role": role,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(path: Path) -> tuple[int, int]:
    size = 0
    files = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise OSError(f"Symbolic links are not supported in backups: {item}")
        if item.is_file():
            size += item.stat().st_size
            files += 1
    return size, files


def _existing_ancestor(path: Path) -> Path:
    current = path.expanduser().resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _verify_directory_writable(path: Path) -> None:
    probe = path / f".zvec-backup-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
    except OSError as exc:
        raise WorkspaceBackupError(
            f"Backup destination is not writable: {path}"
        ) from exc
    finally:
        probe.unlink(missing_ok=True)


def _can_write_directory(path: Path) -> bool:
    if not os.access(path, os.W_OK):
        return False
    probe = path / f".zvec-backup-precheck-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
        return True
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = os.path.commonpath((str(left), str(right)))
    except ValueError:
        return False
    normalized = os.path.normcase(os.path.abspath(common))
    return normalized in {
        os.path.normcase(os.path.abspath(str(left))),
        os.path.normcase(os.path.abspath(str(right))),
    }
