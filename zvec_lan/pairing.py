"""Approval-gated Android pairing and bearer credential storage."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

PAIRING_TTL_SECONDS = 5 * 60
MAX_PENDING_PAIRINGS = 32
MAX_PAIRING_RECORDS = 128
_SAFE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,160}$")


class PairingError(RuntimeError):
    """Base class for safe pairing failures."""

    code = "pairing_error"


class InvalidPairingRequest(PairingError):
    code = "invalid_pairing_request"


class PairingNotFound(PairingError):
    code = "pairing_not_found"


class PairingExpired(PairingError):
    code = "pairing_expired"


class PairingSecretRejected(PairingError):
    code = "invalid_client_secret"


class PairingTransitionRejected(PairingError):
    code = "invalid_pairing_state"


class PairingCapacityExceeded(PairingError):
    code = "pairing_capacity_exceeded"


class CredentialStoreError(PairingError):
    """Credential hashes could not be read or persisted."""


@dataclass(frozen=True, slots=True)
class AuthorizedClient:
    """Persisted paired-device record containing hashes only."""

    device_id: str
    device_name: str
    token_hash: str
    client_secret_hash: str
    paired_at: float


@dataclass(frozen=True, slots=True)
class PairedClientView:
    """Hash-free paired-device metadata safe for the local Windows UI."""

    device_id: str
    device_name: str
    paired_at: float
    session_id: str


class CredentialStore(Protocol):
    """Storage contract for hashed paired-device credentials."""

    def put(self, client: AuthorizedClient) -> None:
        """Insert or replace the credentials for a device."""

    def find_by_token_hash(self, token_hash: str) -> AuthorizedClient | None:
        """Look up a paired device by bearer-token hash."""

    def delete_by_token_hash(self, token_hash: str) -> bool:
        """Revoke a bearer token and return whether it existed."""

    def delete_by_device_id(self, device_id: str) -> bool:
        """Revoke a device without its plaintext bearer token."""

    def delete_all(self) -> bool:
        """Revoke every paired device."""

    def list_clients(self) -> Sequence[AuthorizedClient]:
        """Return paired-device records for local Windows UI display."""


class MemoryCredentialStore:
    """Thread-safe in-memory hash store, primarily useful for tests."""

    def __init__(self) -> None:
        self._by_token: dict[str, AuthorizedClient] = {}
        self._token_by_device: dict[str, str] = {}
        self._lock = threading.RLock()

    def put(self, client: AuthorizedClient) -> None:
        with self._lock:
            previous = self._token_by_device.get(client.device_id)
            if previous is not None:
                self._by_token.pop(previous, None)
            self._by_token[client.token_hash] = client
            self._token_by_device[client.device_id] = client.token_hash

    def find_by_token_hash(self, token_hash: str) -> AuthorizedClient | None:
        with self._lock:
            return self._by_token.get(token_hash)

    def delete_by_token_hash(self, token_hash: str) -> bool:
        with self._lock:
            client = self._by_token.pop(token_hash, None)
            if client is None:
                return False
            if self._token_by_device.get(client.device_id) == token_hash:
                self._token_by_device.pop(client.device_id, None)
            return True

    def delete_by_device_id(self, device_id: str) -> bool:
        with self._lock:
            token_hash = self._token_by_device.get(device_id)
            if token_hash is None:
                return False
            return self.delete_by_token_hash(token_hash)

    def delete_all(self) -> bool:
        with self._lock:
            if not self._by_token:
                return False
            self._by_token.clear()
            self._token_by_device.clear()
            return True

    def list_clients(self) -> tuple[AuthorizedClient, ...]:
        with self._lock:
            return tuple(self._by_token.values())


class JsonCredentialStore(MemoryCredentialStore):
    """Atomic JSON persistence containing token and client-secret hashes only."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._revocation_path = self._path.with_name(f"{self._path.name}.revoked")
        self._revoked_token_hashes: set[str] = set()
        super().__init__()
        self._load()
        self._load_revocations()
        self._reconcile_revocations_best_effort()

    def find_by_token_hash(self, token_hash: str) -> AuthorizedClient | None:
        with self._lock:
            if token_hash in self._revoked_token_hashes:
                return None
            return super().find_by_token_hash(token_hash)

    def list_clients(self) -> tuple[AuthorizedClient, ...]:
        with self._lock:
            return tuple(
                client
                for client in super().list_clients()
                if client.token_hash not in self._revoked_token_hashes
            )

    def put(self, client: AuthorizedClient) -> None:
        with self._lock:
            previous_by_token = dict(self._by_token)
            previous_by_device = dict(self._token_by_device)
            previous_token_hash = self._token_by_device.get(client.device_id)
            if (
                previous_token_hash is not None
                and previous_token_hash != client.token_hash
            ):
                self._stage_revocations_locked((previous_token_hash,))
            try:
                super().put(client)
                self._persist_locked()
            except CredentialStoreError:
                self._by_token = previous_by_token
                self._token_by_device = previous_by_device
                raise
            if previous_token_hash is not None:
                self._clear_revocations_best_effort_locked((previous_token_hash,))

    def delete_by_token_hash(self, token_hash: str) -> bool:
        with self._lock:
            if token_hash not in self._by_token:
                return False
            # Persist the deny decision first. If rewriting the credential file
            # then fails, a restarted process still rejects this bearer.
            self._stage_revocations_locked((token_hash,))
            previous_by_token = dict(self._by_token)
            previous_by_device = dict(self._token_by_device)
            deleted = super().delete_by_token_hash(token_hash)
            assert deleted
            try:
                self._persist_locked()
            except CredentialStoreError:
                self._by_token = previous_by_token
                self._token_by_device = previous_by_device
                raise
            self._clear_revocations_best_effort_locked((token_hash,))
            return True

    def delete_by_device_id(self, device_id: str) -> bool:
        with self._lock:
            token_hash = self._token_by_device.get(device_id)
            if token_hash is None:
                return False
            return self.delete_by_token_hash(token_hash)

    def delete_all(self) -> bool:
        with self._lock:
            if not self._by_token:
                return False
            revoked_hashes = tuple(self._by_token)
            self._stage_revocations_locked(revoked_hashes)
            previous_by_token = dict(self._by_token)
            previous_by_device = dict(self._token_by_device)
            self._by_token.clear()
            self._token_by_device.clear()
            try:
                self._persist_locked()
            except CredentialStoreError:
                self._by_token = previous_by_token
                self._token_by_device = previous_by_device
                raise
            self._clear_revocations_best_effort_locked(revoked_hashes)
            return True

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            raw_clients = payload.get("clients") if isinstance(payload, dict) else None
            if not isinstance(raw_clients, list):
                raise ValueError("missing clients")
            clients = tuple(_client_from_json(value) for value in raw_clients)
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise CredentialStoreError(
                "paired-device credentials are unavailable"
            ) from exc
        for client in clients:
            super().put(client)

    def _load_revocations(self) -> None:
        if not self._revocation_path.exists():
            return
        try:
            payload = json.loads(self._revocation_path.read_text(encoding="utf-8"))
            values = (
                payload.get("revoked_token_hashes")
                if isinstance(payload, dict)
                else None
            )
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or set(payload) != {"version", "revoked_token_hashes"}
                or not isinstance(values, list)
            ):
                raise ValueError("invalid revocation journal")
            self._revoked_token_hashes = {_validated_hash(value) for value in values}
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            # A corrupt deny journal must never be ignored because doing so can
            # resurrect a bearer that the user already revoked.
            raise CredentialStoreError(
                "paired-device revocations are unavailable"
            ) from exc

    def _stage_revocations_locked(self, token_hashes: Sequence[str]) -> None:
        candidate = self._revoked_token_hashes | set(token_hashes)
        if candidate == self._revoked_token_hashes:
            return
        self._persist_revocations(candidate)
        self._revoked_token_hashes = candidate

    def _clear_revocations_best_effort_locked(
        self,
        token_hashes: Sequence[str],
    ) -> None:
        candidate = self._revoked_token_hashes - set(token_hashes)
        try:
            self._persist_revocations(candidate)
        except CredentialStoreError:
            # A stale deny entry is safe. Startup reconciliation removes it once
            # the corresponding credential is durably absent.
            return
        self._revoked_token_hashes = candidate

    def _reconcile_revocations_best_effort(self) -> None:
        with self._lock:
            candidate = self._revoked_token_hashes & set(self._by_token)
            if candidate == self._revoked_token_hashes:
                return
            try:
                self._persist_revocations(candidate)
            except CredentialStoreError:
                return
            self._revoked_token_hashes = candidate

    def _persist_revocations(self, values: set[str]) -> None:
        payload = {
            "version": 1,
            "revoked_token_hashes": sorted(values),
        }
        temporary = self._revocation_path.with_name(
            f".{self._revocation_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            self._revocation_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(OSError):
                os.chmod(temporary, 0o600)
            os.replace(temporary, self._revocation_path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink()
            raise CredentialStoreError(
                "paired-device revocations could not be saved"
            ) from exc

    def _persist_locked(self) -> None:
        payload = {
            "version": 1,
            "clients": [
                {
                    "device_id": client.device_id,
                    "device_name": client.device_name,
                    "token_hash": client.token_hash,
                    "client_secret_hash": client.client_secret_hash,
                    "paired_at": client.paired_at,
                }
                for client in self._by_token.values()
            ],
        }
        temporary = self._path.with_name(
            f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            with suppress(OSError):
                os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink()
            raise CredentialStoreError(
                "paired-device credentials could not be saved"
            ) from exc


PairingStatus = Literal["pending", "approved", "rejected", "expired"]


@dataclass(frozen=True, slots=True)
class PairingView:
    """Safe pairing data displayed by Android and the local Windows UI."""

    pairing_id: str
    device_id: str
    device_name: str
    comparison_code: str
    status: PairingStatus
    created_at: float
    expires_at: float

    def expires_in_seconds(self, now: float) -> int:
        return max(0, int(self.expires_at - now + 0.999999))


@dataclass(frozen=True, slots=True)
class PairPollResult:
    status: PairingStatus
    token: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedClient:
    device_id: str
    device_name: str
    session_id: str


@dataclass(frozen=True, slots=True)
class _PairingRecord:
    view: PairingView
    client_secret_hash: str
    token_issued: bool = False


class PairingManager:
    """Five-minute, user-approved pairing state machine.

    The manager never retains a plaintext client secret or bearer token.  The
    bearer token is generated inside the first approved poll and returned while
    the same lock atomically marks it issued.
    """

    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
        pairing_id_factory: Callable[[], str] | None = None,
        code_factory: Callable[[], str] | None = None,
        session_cleanup_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._credentials = (
            MemoryCredentialStore() if credential_store is None else credential_store
        )
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._pairing_id_factory = pairing_id_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self._code_factory = code_factory or (
            lambda: f"{secrets.randbelow(1_000_000):06d}"
        )
        self._requests: dict[str, _PairingRecord] = {}
        self._lock = threading.RLock()
        self._session_cleanup_callback = session_cleanup_callback
        self._pending_session_cleanup: dict[str, set[str]] = {}
        self._active_sessions = {
            _session_scope(client.token_hash): client.device_id
            for client in self._credentials.list_clients()
        }

    def set_session_cleanup_callback(
        self,
        callback: Callable[[str], None] | None,
    ) -> None:
        """Register cleanup invoked before a replacement token is returned."""

        with self._lock:
            self._session_cleanup_callback = callback

    def create_request(
        self,
        *,
        device_id: str,
        device_name: str,
        client_secret: str,
    ) -> PairingView:
        normalized_id = _validated_id(device_id, "device_id")
        normalized_name = _validated_name(device_name)
        secret_hash = _hash_secret(_decode_256_bit_secret(client_secret))
        now = self._clock()
        with self._lock:
            self._prune_terminal_requests(now)
            for pairing_id, existing in tuple(self._requests.items()):
                if (
                    existing.view.device_id == normalized_id
                    and self._effective_status(existing, now) == "pending"
                ):
                    self._requests.pop(pairing_id, None)
            pending_count = sum(
                self._effective_status(record, now) == "pending"
                for record in self._requests.values()
            )
            if (
                pending_count >= MAX_PENDING_PAIRINGS
                or len(self._requests) >= MAX_PAIRING_RECORDS
            ):
                raise PairingCapacityExceeded("too many pairing requests")
            pairing_id = self._new_unique_pairing_id()
            code = self._new_comparison_code()
            view = PairingView(
                pairing_id=pairing_id,
                device_id=normalized_id,
                device_name=normalized_name,
                comparison_code=code,
                status="pending",
                created_at=now,
                expires_at=now + PAIRING_TTL_SECONDS,
            )
            self._requests[pairing_id] = _PairingRecord(
                view=view,
                client_secret_hash=secret_hash,
            )
            return view

    def poll(self, pairing_id: str, *, client_secret: str) -> PairPollResult:
        normalized_id = _validated_id(pairing_id, "pairing_id")
        supplied_hash = _hash_secret(_decode_256_bit_secret(client_secret))
        with self._lock:
            record = self._requests.get(normalized_id)
            if record is None:
                raise PairingNotFound("pairing request was not found")
            if not hmac.compare_digest(record.client_secret_hash, supplied_hash):
                raise PairingSecretRejected("client secret was rejected")
            status = self._effective_status(record, self._clock())
            if status == "expired":
                raise PairingExpired("pairing request expired")
            if status == "rejected":
                return PairPollResult(status="rejected")
            if status == "pending":
                return PairPollResult(status="pending")
            if record.token_issued:
                return PairPollResult(status="approved")
            token = self._validated_generated_token(self._token_factory())
            token_hash = _hash_token(token)
            session_id = _session_scope(token_hash)
            previous = next(
                (
                    client
                    for client in self._credentials.list_clients()
                    if client.device_id == record.view.device_id
                ),
                None,
            )
            previous_session_id = (
                None if previous is None else _session_scope(previous.token_hash)
            )
            if previous_session_id is not None:
                self._active_sessions.pop(previous_session_id, None)
                self._pending_session_cleanup.setdefault(
                    record.view.device_id,
                    set(),
                ).add(previous_session_id)
            self._credentials.put(
                AuthorizedClient(
                    device_id=record.view.device_id,
                    device_name=record.view.device_name,
                    token_hash=token_hash,
                    client_secret_hash=record.client_secret_hash,
                    paired_at=self._clock(),
                )
            )
            self._active_sessions[session_id] = record.view.device_id
            callback = self._session_cleanup_callback
            pending_cleanup = tuple(
                self._pending_session_cleanup.get(record.view.device_id, ())
            )
            if pending_cleanup and callback is not None:
                try:
                    for stale_session_id in pending_cleanup:
                        callback(stale_session_id)
                except Exception as exc:
                    self._active_sessions.pop(session_id, None)
                    with suppress(Exception):
                        self._credentials.delete_by_token_hash(token_hash)
                    raise PairingError(
                        "previous device session could not be cleaned"
                    ) from exc
                self._pending_session_cleanup.pop(record.view.device_id, None)
            self._requests[normalized_id] = replace(record, token_issued=True)
            return PairPollResult(status="approved", token=token)

    def approve(self, pairing_id: str) -> PairingView:
        return self._transition(pairing_id, target="approved")

    def reject(self, pairing_id: str) -> PairingView:
        return self._transition(pairing_id, target="rejected")

    def pending_requests(self) -> tuple[PairingView, ...]:
        now = self._clock()
        with self._lock:
            values: list[PairingView] = []
            for pairing_id, record in tuple(self._requests.items()):
                status = self._effective_status(record, now)
                if status == "pending":
                    values.append(record.view)
                elif status == "expired" and record.view.status != "expired":
                    self._requests[pairing_id] = replace(
                        record,
                        view=replace(record.view, status="expired"),
                    )
            return tuple(values)

    def authenticate(self, token: str) -> AuthenticatedClient | None:
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            return None
        token_hash = _hash_token(token)
        with self._lock:
            client = self._credentials.find_by_token_hash(token_hash)
            if client is None:
                return None
            session_id = _session_scope(client.token_hash)
            if self._active_sessions.get(session_id) != client.device_id:
                return None
            return AuthenticatedClient(
                client.device_id,
                client.device_name,
                session_id,
            )

    def is_session_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active_sessions

    def revoke(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        token_hash = _hash_token(token)
        session_id = _session_scope(token_hash)
        with self._lock:
            self._active_sessions.pop(session_id, None)
            return self._credentials.delete_by_token_hash(token_hash)

    def revoke_client(self, device_id: str) -> bool:
        normalized_id = _validated_id(device_id, "device_id")
        with self._lock:
            clients = tuple(
                client
                for client in self._credentials.list_clients()
                if client.device_id == normalized_id
            )
            for client in clients:
                self._active_sessions.pop(
                    _session_scope(client.token_hash),
                    None,
                )
            return self._credentials.delete_by_device_id(normalized_id)

    def revoke_device(self, device_id: str | None = None) -> bool:
        """Compatibility alias; ``None`` revokes all paired devices."""

        if device_id is None:
            with self._lock:
                self._active_sessions.clear()
                return self._credentials.delete_all()
        return self.revoke_client(device_id)

    def paired_clients(self) -> tuple[PairedClientView, ...]:
        with self._lock:
            return tuple(
                PairedClientView(
                    device_id=client.device_id,
                    device_name=client.device_name,
                    paired_at=client.paired_at,
                    session_id=_session_scope(client.token_hash),
                )
                for client in self._credentials.list_clients()
                if _session_scope(client.token_hash) in self._active_sessions
            )

    def _transition(
        self,
        pairing_id: str,
        *,
        target: Literal["approved", "rejected"],
    ) -> PairingView:
        normalized_id = _validated_id(pairing_id, "pairing_id")
        with self._lock:
            record = self._requests.get(normalized_id)
            if record is None:
                raise PairingNotFound("pairing request was not found")
            status = self._effective_status(record, self._clock())
            if status == "expired":
                self._requests[normalized_id] = replace(
                    record,
                    view=replace(record.view, status="expired"),
                )
                raise PairingExpired("pairing request expired")
            if status != "pending":
                raise PairingTransitionRejected("pairing request is no longer pending")
            updated = replace(record.view, status=target)
            self._requests[normalized_id] = replace(record, view=updated)
            return updated

    def _effective_status(
        self,
        record: _PairingRecord,
        now: float,
    ) -> PairingStatus:
        if record.view.status == "pending" and now >= record.view.expires_at:
            return "expired"
        return record.view.status

    def _prune_terminal_requests(self, now: float) -> None:
        for pairing_id, record in tuple(self._requests.items()):
            status = self._effective_status(record, now)
            if status in {"expired", "rejected"} or record.token_issued:
                self._requests.pop(pairing_id, None)

    def _new_unique_pairing_id(self) -> str:
        for _attempt in range(32):
            candidate = _validated_id(self._pairing_id_factory(), "pairing_id")
            if candidate not in self._requests:
                return candidate
        raise PairingError("could not allocate a pairing request")

    def _new_comparison_code(self) -> str:
        for _attempt in range(32):
            code = self._code_factory()
            if (
                isinstance(code, str)
                and len(code) == 6
                and code.isascii()
                and code.isdigit()
                and all(
                    record.view.comparison_code != code
                    or record.view.status != "pending"
                    for record in self._requests.values()
                )
            ):
                return code
        raise PairingError("could not allocate a comparison code")

    @staticmethod
    def _validated_generated_token(token: str) -> str:
        try:
            decoded = _decode_base64url(token)
        except InvalidPairingRequest as exc:
            raise PairingError("token generator returned invalid data") from exc
        if len(decoded) != 32:
            raise PairingError("token generator returned invalid data")
        return token


def pairing_payload(
    view: PairingView,
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Return the agreed Android pairing-start JSON shape."""

    current = time.time() if now is None else now
    return {
        "pairing_id": view.pairing_id,
        "comparison_code": view.comparison_code,
        "expires_in_seconds": view.expires_in_seconds(current),
        "expires_at": _iso_timestamp(view.expires_at),
        "status": view.status,
    }


def _client_from_json(value: object) -> AuthorizedClient:
    if not isinstance(value, dict):
        raise ValueError("invalid client")
    device_id = _validated_id(value.get("device_id"), "device_id")
    device_name = _validated_name(value.get("device_name"))
    token_hash = _validated_hash(value.get("token_hash"))
    client_secret_hash = _validated_hash(value.get("client_secret_hash"))
    paired_at = value.get("paired_at")
    if isinstance(paired_at, bool) or not isinstance(paired_at, (int, float)):
        raise ValueError("invalid paired_at")
    return AuthorizedClient(
        device_id=device_id,
        device_name=device_name,
        token_hash=token_hash,
        client_secret_hash=client_secret_hash,
        paired_at=float(paired_at),
    )


def _validated_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise InvalidPairingRequest(f"{field} is invalid")
    return value


def _validated_name(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidPairingRequest("device_name is invalid")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 128 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise InvalidPairingRequest("device_name is invalid")
    return normalized


def _validated_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("invalid credential hash")
    return value


def _decode_256_bit_secret(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise InvalidPairingRequest("client_secret must encode 256 bits")
    try:
        decoded = bytes.fromhex(value) if len(value) == 64 else _decode_base64url(value)
    except (ValueError, InvalidPairingRequest) as exc:
        raise InvalidPairingRequest("client_secret must encode 256 bits") from exc
    if len(decoded) != 32:
        raise InvalidPairingRequest("client_secret must encode 256 bits")
    return decoded


def _decode_base64url(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise InvalidPairingRequest("invalid base64url data")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidPairingRequest("invalid base64url data") from exc


def _hash_secret(secret: bytes) -> str:
    return hashlib.sha256(b"zvec-lan/v1/client-secret\0" + secret).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(
        b"zvec-lan/v1/bearer-token\0" + token.encode("utf-8")
    ).hexdigest()


def _session_scope(token_hash: str) -> str:
    digest = hashlib.sha256(
        b"zvec-lan/v1/session-scope\0" + bytes.fromhex(token_hash)
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
