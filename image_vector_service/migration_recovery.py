"""Durable crash-recovery marker for desktop data migrations.

The marker is written only after a complete backup exists and before the first
mutating migration step starts.  It is intentionally stored outside every
Collection so a damaged Workspace cannot hide the recovery instructions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Final

from .data_migration import DataMigrationRequest, DataMigrationValidationError

RECOVERY_FILE_NAME: Final = "data-migration-recovery.json"
RECOVERY_SCHEMA_VERSION: Final = 1
MAX_RECOVERY_FILE_BYTES: Final = 256 * 1024
_SAFE_OPERATION_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class MigrationRecoveryError(RuntimeError):
    """The durable migration-recovery marker is missing or invalid."""


@dataclass(frozen=True, slots=True)
class MigrationRecoveryRecord:
    """Validated private recovery data plus a bounded public representation."""

    operation_id: str
    request: DataMigrationRequest
    backup: dict[str, object]
    stage: str
    created_at_epoch: float
    updated_at_epoch: float

    def public_view(self) -> dict[str, object]:
        destination = self.backup.get("destination")
        return {
            "pending": True,
            "operation_id": self.operation_id,
            "migration_type": self.request.migration_type,
            "library_id": self.request.library_id,
            "library_name": self.request.library_name,
            "backup_directory": (
                str(destination) if isinstance(destination, str) else ""
            ),
            "stage": self.stage,
            "created_at_epoch": self.created_at_epoch,
            "updated_at_epoch": self.updated_at_epoch,
        }


class MigrationRecoveryStore:
    """Atomically persist the one recovery marker allowed by migration locking."""

    def __init__(self, config_home: str | Path) -> None:
        self.config_home = Path(config_home).expanduser().resolve()
        self.path = self.config_home / RECOVERY_FILE_NAME

    def load(self) -> MigrationRecoveryRecord | None:
        if not self.path.is_file():
            return None
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise MigrationRecoveryError(
                "Could not inspect the migration recovery marker."
            ) from exc
        if size <= 0 or size > MAX_RECOVERY_FILE_BYTES:
            raise MigrationRecoveryError(
                "The migration recovery marker has an invalid size."
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationRecoveryError(
                "The migration recovery marker is unreadable."
            ) from exc
        return _record_from_payload(payload)

    def arm(
        self,
        operation_id: str,
        request: DataMigrationRequest,
        backup: Mapping[str, object],
        *,
        stage: str = "backup_complete",
    ) -> MigrationRecoveryRecord:
        existing = self.load()
        normalized_id = _operation_id(operation_id)
        if existing is not None and existing.operation_id != normalized_id:
            raise MigrationRecoveryError(
                "Another unfinished migration already requires recovery."
            )
        now = time.time()
        record = MigrationRecoveryRecord(
            operation_id=normalized_id,
            request=request,
            backup=_backup_receipt(backup),
            stage=_stage(stage),
            created_at_epoch=(existing.created_at_epoch if existing else now),
            updated_at_epoch=now,
        )
        self._write(record)
        return record

    def update_stage(
        self,
        operation_id: str,
        stage: str,
    ) -> MigrationRecoveryRecord:
        record = self.require(operation_id)
        updated = MigrationRecoveryRecord(
            operation_id=record.operation_id,
            request=record.request,
            backup=record.backup,
            stage=_stage(stage),
            created_at_epoch=record.created_at_epoch,
            updated_at_epoch=time.time(),
        )
        self._write(updated)
        return updated

    def require(self, operation_id: str | None = None) -> MigrationRecoveryRecord:
        record = self.load()
        if record is None:
            raise MigrationRecoveryError(
                "No unfinished data migration requires recovery."
            )
        if operation_id is not None and record.operation_id != _operation_id(
            operation_id
        ):
            raise MigrationRecoveryError(
                "The recovery marker belongs to a different migration."
            )
        return record

    def clear(self, operation_id: str) -> bool:
        record = self.load()
        if record is None:
            return False
        if record.operation_id != _operation_id(operation_id):
            raise MigrationRecoveryError(
                "The recovery marker belongs to a different migration."
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MigrationRecoveryError(
                "Could not clear the migration recovery marker."
            ) from exc
        return True

    def _write(self, record: MigrationRecoveryRecord) -> None:
        self.config_home.mkdir(parents=True, exist_ok=True)
        payload = _payload_without_integrity(record)
        payload["integrity_sha256"] = _integrity(payload)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RECOVERY_FILE_BYTES:
            raise MigrationRecoveryError(
                "The migration recovery marker is too large to persist safely."
            )
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise MigrationRecoveryError(
                "Could not durably persist the migration recovery marker."
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _record_from_payload(value: object) -> MigrationRecoveryRecord:
    if not isinstance(value, Mapping):
        raise MigrationRecoveryError("The migration recovery marker is invalid.")
    payload = dict(value)
    supplied_integrity = payload.pop("integrity_sha256", None)
    if not isinstance(supplied_integrity, str) or not hmac.compare_digest(
        supplied_integrity, _integrity(payload)
    ):
        raise MigrationRecoveryError(
            "The migration recovery marker failed its integrity check."
        )
    if payload.get("schema_version") != RECOVERY_SCHEMA_VERSION:
        raise MigrationRecoveryError(
            "The migration recovery marker uses an unsupported schema."
        )
    try:
        request_payload = payload["request"]
        if not isinstance(request_payload, Mapping):
            raise TypeError("request")
        request = DataMigrationRequest.from_mapping(request_payload)
    except (KeyError, TypeError, DataMigrationValidationError) as exc:
        raise MigrationRecoveryError(
            "The migration recovery request is invalid."
        ) from exc
    backup_payload = payload.get("backup")
    if not isinstance(backup_payload, Mapping):
        raise MigrationRecoveryError(
            "The migration recovery backup receipt is invalid."
        )
    return MigrationRecoveryRecord(
        operation_id=_operation_id(payload.get("operation_id")),
        request=request,
        backup=_backup_receipt(backup_payload),
        stage=_stage(payload.get("stage")),
        created_at_epoch=_timestamp(payload.get("created_at_epoch")),
        updated_at_epoch=_timestamp(payload.get("updated_at_epoch")),
    )


def _payload_without_integrity(
    record: MigrationRecoveryRecord,
) -> dict[str, object]:
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "operation_id": record.operation_id,
        "request": record.request.to_dict(),
        "backup": record.backup,
        "stage": record.stage,
        "created_at_epoch": record.created_at_epoch,
        "updated_at_epoch": record.updated_at_epoch,
    }


def _integrity(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _operation_id(value: object) -> str:
    normalized = str(value or "").strip()
    if _SAFE_OPERATION_ID.fullmatch(normalized) is None or ".." in normalized:
        raise MigrationRecoveryError("The migration operation id is invalid.")
    return normalized


def _stage(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if (
        not normalized
        or len(normalized) > 64
        or re.fullmatch(r"[a-z][a-z0-9_-]*", normalized) is None
    ):
        raise MigrationRecoveryError("The migration recovery stage is invalid.")
    return normalized


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MigrationRecoveryError("The migration recovery timestamp is invalid.")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise MigrationRecoveryError("The migration recovery timestamp is invalid.")
    return timestamp


def _backup_receipt(value: Mapping[str, object]) -> dict[str, object]:
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise MigrationRecoveryError(
            "The migration recovery backup receipt is not JSON serializable."
        ) from exc
    if not isinstance(copied, dict):
        raise MigrationRecoveryError(
            "The migration recovery backup receipt is invalid."
        )
    destination = copied.get("destination")
    if (
        not isinstance(destination, str)
        or not destination.strip()
        or not PureWindowsPath(destination).is_absolute()
        or any(character in destination for character in "\r\n\x00")
    ):
        raise MigrationRecoveryError(
            "The migration recovery backup destination is invalid."
        )
    return copied
