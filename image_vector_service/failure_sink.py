from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import FailureKind, FileFailure

_CAPACITY_ERRORS = {errno.ENOSPC}
if hasattr(errno, "EDQUOT"):
    _CAPACITY_ERRORS.add(errno.EDQUOT)

_PATH_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class _SourceChangedError(OSError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            getattr(errno, "ESTALE", errno.EAGAIN),
            "source changed before failed-image quarantine "
            f"(expected sha256 {expected}, copied sha256 {actual})",
        )


class _BlobIntegrityError(OSError):
    def __init__(self, path: Path, expected: str, actual: str) -> None:
        super().__init__(
            errno.EIO,
            "failed-image blob checksum mismatch "
            f"for {path} (expected sha256 {expected}, actual sha256 {actual})",
        )


@dataclass(frozen=True)
class FailureCapture:
    failure: FileFailure
    manifest_path: str = ""
    copied: bool = False
    needs_attention: bool = False


class FailureSink:
    """Persist index failures without moving or changing the source image.

    One content-addressed blob is retained per SHA-256 across jobs.  Every failure is
    also appended to a per-job JSONL manifest so duplicate source paths and copy
    errors remain auditable without returning an unbounded response to the desktop.
    """

    def __init__(self, results_directory: Path, job_id: str, root_path: Path) -> None:
        normalized_job_id = "".join(
            character
            for character in str(job_id)
            if character.isalnum() or character in "-_"
        )
        self.job_id = normalized_job_id or uuid.uuid4().hex
        self.root_path = root_path.expanduser().resolve()
        self.failure_root = results_directory.expanduser().resolve() / "failed-images"
        self.blobs_directory = self.failure_root / "blobs"
        self.jobs_directory = self.failure_root / "jobs"
        self._manifest_path = self.jobs_directory / f"{self.job_id}.jsonl"
        self._manifest_created = False
        self._known_blobs: dict[str, Path] = {}
        self._lock = threading.Lock()

    @property
    def manifest_path(self) -> str:
        return str(self._manifest_path) if self._manifest_created else ""

    def capture(
        self,
        *,
        path: str | Path,
        error: str,
        kind: FailureKind,
        stage: str,
        sha256_hex: str = "",
        quarantine: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> FailureCapture:
        source = Path(path).expanduser().resolve(strict=False)
        digest = _validated_sha256(sha256_hex)
        quarantined_path = ""
        copy_error = ""
        copied = False
        needs_attention = False

        with self._lock:
            # Only deterministic per-image failures belong in the blob store.
            # Provider throttling, authentication failures and outages describe
            # the request, not the source image, even if a caller forgets to turn
            # quarantine off explicitly.
            if quarantine and kind == "item":
                try:
                    if not source.is_file():
                        raise FileNotFoundError(source)
                    destination, copied, digest = self._copy_blob(source, digest)
                    quarantined_path = str(destination)
                except OSError as exc:
                    copy_error = str(exc) or exc.__class__.__name__
                    needs_attention = _is_capacity_error(exc) or isinstance(
                        exc, _BlobIntegrityError
                    )

            failure = FileFailure(
                path=str(source),
                error=str(error) or "Index operation failed.",
                kind=kind,
                stage=str(stage),
                sha256=digest,
                quarantined_path=quarantined_path,
                copy_error=copy_error,
            )
            manifest_error = self._append_manifest(failure, metadata or {})
            if manifest_error:
                failure.copy_error = _join_errors(failure.copy_error, manifest_error)
                needs_attention = True

        return FailureCapture(
            failure=failure,
            manifest_path=self.manifest_path,
            copied=copied,
            needs_attention=needs_attention,
        )

    def _copy_blob(self, source: Path, expected_digest: str) -> tuple[Path, bool, str]:
        """Copy one stable snapshot and publish it under its verified digest.

        ``expected_digest`` comes from the scan/index stage and can be stale by the
        time a worker reports an error.  Hashing the source and then reopening it
        for copying leaves a TOCTOU window, so the temporary copy itself is hashed
        before it is allowed into the content-addressed blob directory.
        """

        staging = self.blobs_directory / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        partial = staging / f".{uuid.uuid4().hex}.partial"
        try:
            shutil.copy2(source, partial)
            actual_digest = _file_sha256(partial)
            if expected_digest and actual_digest != expected_digest:
                raise _SourceChangedError(expected_digest, actual_digest)

            digest = actual_digest
            bucket = self.blobs_directory / digest[:2]
            destination = bucket / f"{digest}{_safe_suffix(source.suffix)}"
            lock_key = f"blob:{self.failure_root}:{digest}"
            with _shared_path_lock(lock_key):
                bucket.mkdir(parents=True, exist_ok=True)
                existing = self._existing_blob(digest, bucket)
                if existing is not None:
                    return existing, False, digest

                copied = _publish_partial(partial, destination)
                if not copied:
                    _verify_blob(destination, digest)
                self._known_blobs[digest] = destination
                return destination, copied, digest
        finally:
            partial.unlink(missing_ok=True)
            # Another concurrent capture can still own a temporary file.
            with suppress(OSError):
                staging.rmdir()

    def _existing_blob(self, digest: str, bucket: Path) -> Path | None:
        known = self._known_blobs.get(digest)
        if known is not None and known.is_file():
            _verify_blob(known, digest)
            return known

        existing = next(
            (
                candidate
                for candidate in sorted(bucket.glob(f"{digest}.*"))
                if candidate.is_file()
            ),
            None,
        )
        if existing is None:
            return None
        _verify_blob(existing, digest)
        self._known_blobs[digest] = existing
        return existing

    def _append_manifest(self, failure: FileFailure, metadata: dict[str, Any]) -> str:
        entry = {
            "schema_version": 1,
            "job_id": self.job_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "root_path": str(self.root_path),
            "source_path": failure.path,
            "stage": failure.stage,
            "kind": failure.kind,
            "error": failure.error,
            "sha256": failure.sha256,
            "blob_path": failure.quarantined_path,
            "copy_error": failure.copy_error,
            "metadata": metadata,
        }
        try:
            payload = (
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            ).encode("utf-8")
            self.jobs_directory.mkdir(parents=True, exist_ok=True)
            lock_key = f"manifest:{self._manifest_path}"
            with _shared_path_lock(lock_key):
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                flags |= getattr(os, "O_BINARY", 0)
                descriptor = os.open(self._manifest_path, flags, 0o600)
                try:
                    _write_all(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._manifest_created = True
            return ""
        except (OSError, TypeError, ValueError) as exc:
            label = (
                "storage capacity exhausted"
                if isinstance(exc, OSError) and _is_capacity_error(exc)
                else "failure manifest write failed"
            )
            return f"{label}: {exc}"


def _publish_partial(partial: Path, destination: Path) -> bool:
    """Atomically publish a completed temporary file without overwriting a peer."""

    try:
        os.link(partial, destination)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        unsupported = {
            errno.EPERM,
            errno.EXDEV,
            getattr(errno, "ENOTSUP", errno.EPERM),
            getattr(errno, "EOPNOTSUPP", errno.EPERM),
        }
        if exc.errno not in unsupported:
            raise

    # Hard links are unavailable on a few removable/network filesystems.  The
    # caller holds the process-wide digest lock, so this fallback remains safe
    # for the desktop's single backend process.
    if destination.exists():
        return False
    os.replace(partial, destination)
    return True


def _verify_blob(path: Path, expected_digest: str) -> None:
    actual_digest = _file_sha256(path)
    if actual_digest != expected_digest:
        raise _BlobIntegrityError(path, expected_digest, actual_digest)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "failure manifest write returned no data")
        view = view[written:]


@contextmanager
def _shared_path_lock(key: str) -> Iterator[None]:
    """Serialize writers sharing one blob digest or manifest in this process."""

    with _PATH_LOCKS_GUARD:
        current = _PATH_LOCKS.get(key)
        if current is None:
            lock = threading.Lock()
            references = 1
        else:
            lock, references = current
            references += 1
        _PATH_LOCKS[key] = (lock, references)

    try:
        with lock:
            yield
    finally:
        with _PATH_LOCKS_GUARD:
            current = _PATH_LOCKS.get(key)
            if current is not None:
                current_lock, references = current
                if references <= 1:
                    _PATH_LOCKS.pop(key, None)
                else:
                    _PATH_LOCKS[key] = (current_lock, references - 1)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) == 64 and all(
        character in "0123456789abcdef" for character in normalized
    ):
        return normalized
    return ""


def _safe_suffix(value: str) -> str:
    normalized = str(value or "").lower()
    if (
        normalized.startswith(".")
        and 1 < len(normalized) <= 10
        and normalized[1:].isalnum()
    ):
        return normalized
    return ".bin"


def _is_capacity_error(error: OSError) -> bool:
    return error.errno in _CAPACITY_ERRORS


def _join_errors(current: str, added: str) -> str:
    return f"{current}; {added}" if current else added


# Keep the index-specific name for existing imports while auto-tagging and future
# pipelines share the same content-addressed failure store.
IndexFailureSink = FailureSink
