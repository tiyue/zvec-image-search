"""Desktop lifecycle and least-privilege adapters for Android LAN access."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from zvec_lan import (
    DISCOVERY_PORT,
    JsonCredentialStore,
    LanBackendError,
    LanGatewayServer,
    LibraryInfo,
    MediaSource,
    SearchPage,
    SearchPending,
    SearchRequest,
    SearchResultItem,
)

from .image_registry import ImageRegistry, ImageRegistryError
from .lan_settings import (
    LanAccessSettings,
    LanSettingsError,
    LanSettingsStore,
    available_private_ipv4_hosts,
)

_INSTANCE_SCHEMA_VERSION = 1
_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,160}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_INSTANCE_FILE_BYTES = 16 * 1024
_DIGEST_VERIFICATION_CACHE_ENTRIES = 4_096
_DEFAULT_SEARCH_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_SEARCH_CLEANUP_INTERVAL_SECONDS = 5 * 60
_SUCCESS_SEARCH_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})
_PENDING_SEARCH_STATUSES = frozenset({"queued", "running", "starting"})


class LanAccessError(RuntimeError):
    """A safe local-control error suitable for the loopback JSON gateway."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class PreviewLanFacade(Protocol):
    """Only the Preview operations required by the read-only LAN adapter."""

    @property
    def image_registry(self) -> ImageRegistry: ...

    def bootstrap(self) -> Mapping[str, Any]: ...

    def submit_lan_search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def search(
        self,
        operation_id: str,
        *,
        page: int = 1,
        page_size: int = 15,
    ) -> Mapping[str, Any]: ...

    def cancel_search(self, operation_id: str) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class _MediaBinding:
    source_image_id: str
    source_path: Path
    source_version: tuple[Path, int, int]
    owner: str
    search_id: str
    sha256: str
    content_type: str | None = None
    digest_verified: bool = False


_DigestVerificationKey = tuple[Path, int, int, str]


@dataclass(slots=True)
class _DigestVerificationFlight:
    """Share one full-file digest read between concurrent media requests."""

    event: threading.Event
    verified: bool = False


class PreviewLanAdapter:
    """Translate typed LAN calls into the existing Preview search workflow.

    The adapter intentionally exposes no settings, task, migration, indexing,
    deletion, or native-shell operation. Media identifiers remain opaque and
    are additionally scoped to the paired client that received them.
    """

    def __init__(
        self,
        facade: PreviewLanFacade,
        *,
        search_ttl_seconds: float = _DEFAULT_SEARCH_TTL_SECONDS,
        cleanup_interval_seconds: float = _DEFAULT_SEARCH_CLEANUP_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(search_ttl_seconds, bool)
            or not isinstance(search_ttl_seconds, (int, float))
            or not math.isfinite(search_ttl_seconds)
            or search_ttl_seconds <= 0
        ):
            raise ValueError("search_ttl_seconds must be positive and finite")
        if (
            isinstance(cleanup_interval_seconds, bool)
            or not isinstance(cleanup_interval_seconds, (int, float))
            or not math.isfinite(cleanup_interval_seconds)
            or cleanup_interval_seconds < 0
        ):
            raise ValueError("cleanup_interval_seconds must be finite and non-negative")
        self._facade = facade
        self._search_owners: dict[str, str] = {}
        self._search_media: dict[str, set[str]] = {}
        self._search_last_access: dict[str, float] = {}
        self._client_generations: dict[str, int] = {}
        self._search_ttl_seconds = float(search_ttl_seconds)
        self._cleanup_interval_seconds = float(cleanup_interval_seconds)
        self._clock = clock
        self._last_cleanup_at = float("-inf")
        # Capabilities live exactly as long as their owning search. There is no
        # result-count cap: DELETE search/session releases all associated rows.
        self._media: dict[str, _MediaBinding] = {}
        self._media_keys: dict[tuple[str, str, str, str], str] = {}
        # Indexed SHA-256 values still need one content check before an original
        # is exposed. Cache that proof by the exact file version so repeated
        # searches do not read the same large source twice. In-flight checks are
        # also shared, while the bounded LRU prevents large libraries from
        # turning verification metadata into unbounded process state.
        self._verified_digests: OrderedDict[_DigestVerificationKey, None] = (
            OrderedDict()
        )
        self._digest_verification_flights: dict[
            _DigestVerificationKey, _DigestVerificationFlight
        ] = {}
        self._lock = threading.RLock()

    def list_libraries(self, *, client_id: str) -> Sequence[LibraryInfo]:
        _client_id(client_id)
        self._cleanup_expired_searches()
        try:
            payload = self._facade.bootstrap()
        except Exception as exc:
            raise _adapter_failure(exc, "libraries_unavailable", 503) from exc
        raw_libraries = payload.get("libraries")
        libraries: list[LibraryInfo] = []
        for raw in raw_libraries if isinstance(raw_libraries, list) else []:
            if not isinstance(raw, Mapping) or raw.get("enabled", True) is not True:
                continue
            library_id = _safe_text(raw.get("id"), maximum=128)
            name = _safe_text(raw.get("name"), maximum=256)
            if library_id and name:
                libraries.append(LibraryInfo(id=library_id, name=name))
        return tuple(libraries)

    def create_search(self, request: SearchRequest, *, client_id: str) -> str:
        owner = _client_id(client_id)
        if not isinstance(request, SearchRequest):
            raise LanBackendError("invalid_search", "搜索请求无效。", status=400)
        payload: dict[str, Any] = {
            "mode": "tag" if request.mode == "tag" else "semantic",
            "library_ids": list(request.library_ids),
            "top_k": request.top_k,
            "page_size": min(100, request.top_k),
            "sort_mode": "confidence",
        }
        if request.text:
            payload["text"] = request.text
            if request.mode == "tag":
                payload["tags"] = [request.text]
                payload["tag_mode"] = "any"
        if request.mode in {"image", "combined"}:
            path = request.query_image_path
            if path is None:
                raise LanBackendError(
                    "query_image_not_found",
                    "查询图片不存在或已过期。",
                    status=404,
                )
            payload["image_path"] = str(path)
        self._cleanup_expired_searches()
        generation, displaced = self._begin_client_search(owner)
        for displaced_id in displaced:
            self._cancel_search_best_effort(displaced_id)
        try:
            submitted = self._facade.submit_lan_search(payload)
        except Exception as exc:
            raise _adapter_failure(exc, "search_submit_failed", 503) from exc
        search_id = _safe_text(submitted.get("id"), maximum=160)
        if not search_id:
            raise LanBackendError(
                "search_submit_failed",
                "电脑未能创建搜索任务。",
                status=502,
            )
        with self._lock:
            if self._client_generations.get(owner) != generation:
                superseded = True
            else:
                superseded = False
                self._search_owners[search_id] = owner
                self._search_media.setdefault(search_id, set())
                self._search_last_access[search_id] = self._clock()
        if superseded:
            self._cancel_search_best_effort(search_id)
            raise LanBackendError(
                "search_superseded",
                "A newer search replaced this request.",
                status=409,
            )
        return search_id

    def get_search_page(
        self,
        search_id: str,
        *,
        page: int,
        page_size: int,
        client_id: str,
    ) -> SearchPage | SearchPending:
        owner = _client_id(client_id)
        self._cleanup_expired_searches()
        normalized_id = self._owned_search(search_id, owner, touch=True)
        try:
            payload = self._facade.search(
                normalized_id,
                page=page,
                page_size=page_size,
            )
        except Exception as exc:
            raise _adapter_failure(exc, "search_read_failed", 503) from exc
        status = _safe_text(payload.get("status"), maximum=64).casefold()
        if status in _PENDING_SEARCH_STATUSES:
            progress: Literal["queued", "running"] = (
                "queued" if status == "queued" else "running"
            )
            return SearchPending(
                search_id=normalized_id,
                status=progress,
                retry_after_seconds=1,
            )
        if status not in _SUCCESS_SEARCH_STATUSES:
            code = "search_cancelled" if status == "cancelled" else "search_failed"
            response_status = 409 if status == "cancelled" else 500
            raise LanBackendError(
                code,
                "搜索已取消。" if status == "cancelled" else "搜索执行失败。",
                status=response_status,
            )

        raw_items = payload.get("items")
        items: list[SearchResultItem] = []
        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, Mapping):
                continue
            source_image_id = _safe_text(raw.get("id"), maximum=160)
            if not source_image_id:
                continue
            digest = _normalized_sha256(raw.get("sha256"))
            content_type = _content_type(raw.get("name"))
            media_id = self._remember_media(
                source_image_id,
                owner=owner,
                search_id=normalized_id,
                sha256=digest,
                content_type=content_type,
            )
            if media_id is None:
                continue
            items.append(
                SearchResultItem(
                    media_id=media_id,
                    score=_result_score(raw),
                    name=_optional_text(raw.get("name"), maximum=1_024),
                    width=_optional_positive_int(raw.get("width")),
                    height=_optional_positive_int(raw.get("height")),
                    tags=_string_tuple(raw.get("tags"), maximum_items=100),
                    library_id=_optional_text(raw.get("library_id"), maximum=128),
                    library_name=_optional_text(raw.get("library_name"), maximum=256),
                    content_type=content_type,
                    size_bytes=_optional_positive_int(raw.get("size_bytes")),
                )
            )
        return SearchPage(
            search_id=normalized_id,
            page=_positive_int(payload.get("page"), page),
            page_size=_positive_int(payload.get("page_size"), page_size),
            total=_non_negative_int(payload.get("total_items")),
            items=tuple(items),
        )

    def delete_search(self, search_id: str, *, client_id: str) -> None:
        owner = _client_id(client_id)
        normalized_id = self._owned_search(search_id, owner)
        with self._lock:
            self._release_search_locked(normalized_id, owner)
        self._cancel_search_best_effort(normalized_id)

    def delete_client_session(self, *, client_id: str) -> None:
        """Revoke every capability and best-effort cancel this client's work."""

        owner = _client_id(client_id)
        with self._lock:
            self._client_generations[owner] = self._client_generations.get(owner, 0) + 1
            search_ids = self._release_client_searches_locked(owner)
        for search_id in search_ids:
            self._cancel_search_best_effort(search_id)

    def resolve_original(
        self,
        media_id: str,
        *,
        client_id: str,
    ) -> MediaSource | None:
        owner = _client_id(client_id)
        self._cleanup_expired_searches()
        normalized_id = _safe_text(media_id, maximum=160)
        with self._lock:
            binding = self._media.get(normalized_id)
            if (
                binding is None
                or binding.owner != owner
                or self._search_owners.get(binding.search_id) != owner
            ):
                return None
            self._search_last_access[binding.search_id] = self._clock()
            digest = binding.sha256
            content_type = binding.content_type
            source_path = binding.source_path
            source_version = binding.source_version
            digest_verified = binding.digest_verified
        try:
            resolved = source_path.resolve(strict=True)
            status = resolved.stat()
        except OSError:
            return None
        if (
            resolved != source_path
            or resolved.is_symlink()
            or not resolved.is_file()
            or (resolved, status.st_mtime_ns, status.st_size) != source_version
        ):
            return None
        if not digest_verified:
            verification_key = (
                resolved,
                status.st_mtime_ns,
                status.st_size,
                digest,
            )
            if not self._verify_indexed_digest(
                verification_key,
                source_version=source_version,
            ):
                return None
        with self._lock:
            current = self._media.get(normalized_id)
            if (
                current is not binding
                or current.owner != owner
                or self._search_owners.get(current.search_id) != owner
            ):
                return None
            if not digest_verified:
                current.digest_verified = True
        return MediaSource(path=resolved, sha256=digest, content_type=content_type)

    def _verify_indexed_digest(
        self,
        key: _DigestVerificationKey,
        *,
        source_version: tuple[Path, int, int],
    ) -> bool:
        """Verify one indexed digest with a bounded cross-search single-flight."""

        with self._lock:
            if key in self._verified_digests:
                self._verified_digests.move_to_end(key)
                return True
            flight = self._digest_verification_flights.get(key)
            owner = flight is None
            if flight is None:
                flight = _DigestVerificationFlight(threading.Event())
                self._digest_verification_flights[key] = flight

        if not owner:
            flight.event.wait()
            return flight.verified

        verified = False
        path, _mtime_ns, _size, expected_digest = key
        try:
            actual_digest = _sha256_file(path)
            after = path.stat()
            verified = (
                actual_digest == expected_digest
                and (path, after.st_mtime_ns, after.st_size) == source_version
            )
            return verified
        except OSError:
            return False
        finally:
            with self._lock:
                current_flight = self._digest_verification_flights.pop(key, None)
                if verified:
                    self._verified_digests[key] = None
                    self._verified_digests.move_to_end(key)
                    while (
                        len(self._verified_digests) > _DIGEST_VERIFICATION_CACHE_ENTRIES
                    ):
                        self._verified_digests.popitem(last=False)
                completed = current_flight or flight
                completed.verified = verified
                completed.event.set()

    def _owned_search(self, search_id: str, owner: str, *, touch: bool = False) -> str:
        normalized_id = _safe_text(search_id, maximum=160)
        with self._lock:
            if not normalized_id or self._search_owners.get(normalized_id) != owner:
                raise LanBackendError(
                    "search_not_found",
                    "搜索任务不存在或已过期。",
                    status=404,
                )
            if touch:
                self._search_last_access[normalized_id] = self._clock()
        return normalized_id

    def _remember_media(
        self,
        source_image_id: str,
        *,
        owner: str,
        search_id: str,
        sha256: str | None,
        content_type: str | None,
    ) -> str | None:
        try:
            source_path = self._facade.image_registry.resolve(source_image_id)
            status = source_path.stat()
        except (ImageRegistryError, OSError):
            return None
        source_version = (source_path, status.st_mtime_ns, status.st_size)
        digest = sha256
        if digest is None:
            try:
                digest = _sha256_file(source_path)
                after = source_path.stat()
            except OSError:
                return None
            if (after.st_mtime_ns, after.st_size) != (
                status.st_mtime_ns,
                status.st_size,
            ):
                return None
        key = (owner, search_id, source_image_id, digest)
        with self._lock:
            if self._search_owners.get(search_id) != owner:
                return None
            self._search_last_access[search_id] = self._clock()
            existing = self._media_keys.get(key)
            if existing is not None and existing in self._media:
                binding = self._media[existing]
                binding.content_type = content_type or binding.content_type
                return existing
            media_id = secrets.token_urlsafe(24)
            while media_id in self._media:
                media_id = secrets.token_urlsafe(24)
            binding = _MediaBinding(
                source_image_id=source_image_id,
                source_path=source_path,
                source_version=source_version,
                owner=owner,
                search_id=search_id,
                sha256=digest,
                content_type=content_type,
                digest_verified=sha256 is None,
            )
            self._media[media_id] = binding
            self._media_keys[key] = media_id
            self._search_media.setdefault(search_id, set()).add(media_id)
            return media_id

    def _release_search_locked(self, search_id: str, owner: str) -> None:
        if self._search_owners.get(search_id) != owner:
            return
        self._search_owners.pop(search_id, None)
        self._search_last_access.pop(search_id, None)
        for media_id in self._search_media.pop(search_id, set()):
            binding = self._media.pop(media_id, None)
            if binding is None:
                continue
            self._media_keys.pop(
                (
                    binding.owner,
                    binding.search_id,
                    binding.source_image_id,
                    binding.sha256,
                ),
                None,
            )

    def _begin_client_search(self, owner: str) -> tuple[int, tuple[str, ...]]:
        """Atomically supersede prior work before submitting a new search."""

        with self._lock:
            generation = self._client_generations.get(owner, 0) + 1
            self._client_generations[owner] = generation
            displaced = self._release_client_searches_locked(owner)
            return generation, displaced

    def _release_client_searches_locked(self, owner: str) -> tuple[str, ...]:
        search_ids = tuple(
            search_id
            for search_id, search_owner in self._search_owners.items()
            if search_owner == owner
        )
        for search_id in search_ids:
            self._release_search_locked(search_id, owner)
        return search_ids

    def _cancel_search_best_effort(self, search_id: str) -> None:
        try:
            current = self._facade.search(search_id, page=1, page_size=1)
            status = _safe_text(current.get("status"), maximum=64).casefold()
            if status in _PENDING_SEARCH_STATUSES:
                self._facade.cancel_search(search_id)
        except Exception:
            # Capabilities are already gone. A terminal or trimmed operation
            # must not prevent a restarted Android App from recovering.
            return

    def _cleanup_expired_searches(self) -> None:
        """Lazily expire inactive searches while normal browsing refreshes TTL."""

        now = self._clock()
        with self._lock:
            if now - self._last_cleanup_at < self._cleanup_interval_seconds:
                return
            self._last_cleanup_at = now
            expired = tuple(
                (search_id, owner)
                for search_id, owner in self._search_owners.items()
                if now - self._search_last_access.get(search_id, now)
                >= self._search_ttl_seconds
            )
            for search_id, owner in expired:
                # Recheck while holding the lock so a concurrent page/media
                # access that refreshed the timestamp wins over cleanup.
                if (
                    now - self._search_last_access.get(search_id, now)
                    >= self._search_ttl_seconds
                ):
                    self._release_search_locked(search_id, owner)
        for search_id, _owner in expired:
            with self._lock:
                still_owned = search_id in self._search_owners
            if not still_owned:
                self._cancel_search_best_effort(search_id)


class _InstanceIdStore:
    """Persist the public discovery identity independently of listener settings."""

    def __init__(self, config_home: str | Path) -> None:
        self.path = Path(config_home).expanduser().resolve() / "lan-instance.json"

    def load_or_create(self) -> str:
        if self.path.exists():
            try:
                if self.path.stat().st_size > _MAX_INSTANCE_FILE_BYTES:
                    raise ValueError("instance file is too large")
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                value = (
                    payload.get("instance_id") if isinstance(payload, dict) else None
                )
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema_version") != _INSTANCE_SCHEMA_VERSION
                    or set(payload) != {"schema_version", "instance_id"}
                    or not isinstance(value, str)
                    or _INSTANCE_ID_PATTERN.fullmatch(value) is None
                ):
                    raise ValueError("instance file is invalid")
                return value
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise LanAccessError(
                    "lan_identity_unavailable",
                    "局域网设备标识无法读取。",
                    status=500,
                ) from exc
        value = secrets.token_urlsafe(24)
        encoded = (
            json.dumps(
                {
                    "schema_version": _INSTANCE_SCHEMA_VERSION,
                    "instance_id": value,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".lan-instance-",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise LanAccessError(
                "lan_identity_unavailable",
                "局域网设备标识无法保存。",
                status=500,
            ) from exc
        return value


class LanAccessController:
    """Own persisted LAN preferences and the independent network gateway."""

    def __init__(
        self,
        facade: PreviewLanFacade,
        config_home: str | Path,
        *,
        settings_store: LanSettingsStore | None = None,
        gateway_factory: Callable[..., Any] = LanGatewayServer,
        credential_store: Any | None = None,
    ) -> None:
        self._config_home = Path(config_home).expanduser().resolve()
        self._settings = settings_store or LanSettingsStore(self._config_home)
        self._identity = _InstanceIdStore(self._config_home)
        self._adapter = PreviewLanAdapter(facade)
        self._gateway_factory = gateway_factory
        self._credentials = credential_store
        self._credential_error: Exception | None = None
        if self._credentials is None:
            try:
                self._credentials = JsonCredentialStore(
                    self._config_home / "lan-devices.json"
                )
            except Exception as exc:
                self._credential_error = exc
        self._server: Any | None = None
        self._last_error = ""
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        try:
            settings = self._settings.load()
        except LanSettingsError as exc:
            raise LanAccessError(
                "lan_settings_unavailable",
                "局域网设置无法读取。",
                status=500,
            ) from exc
        with self._lock:
            server = self._server
            running_server = (
                server if server is not None and server.is_running else None
            )
            running = running_server is not None
            last_error = self._last_error
        pairings = (
            running_server.pending_pairings() if running_server is not None else ()
        )
        clients = self._paired_clients(running_server)
        address = running_server.address.origin if running_server is not None else ""
        message = last_error
        if not message:
            if running:
                message = "局域网访问已开启，等待 Android 设备连接。"
            elif settings.enabled:
                message = "局域网访问已允许，但服务当前未运行。"
            else:
                message = "局域网访问未开启。"
        device = clients[-1] if clients else None
        return {
            "enabled": settings.enabled,
            "running": running,
            "bind_host": settings.bind_host,
            "port": settings.port,
            "display_name": settings.display_name,
            "discovery_port": (
                running_server.address.discovery_port
                if running_server is not None
                else DISCOVERY_PORT
            ),
            "address": address,
            "available_hosts": [
                option.to_dict() for option in available_private_ipv4_hosts()
            ],
            "pending_pairings": [
                {
                    "id": pairing.pairing_id,
                    "device_name": pairing.device_name,
                    "code": pairing.comparison_code,
                    "expires_at": _iso_timestamp(pairing.expires_at),
                    "status": pairing.status,
                }
                for pairing in pairings
            ],
            "device": (
                {
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "paired_at": _iso_timestamp(device.paired_at),
                    "last_seen_at": _iso_timestamp(device.paired_at),
                }
                if device is not None
                else None
            ),
            "message": message,
        }

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"enabled", "bind_host", "port", "display_name"}
        if set(payload) != required:
            raise LanAccessError(
                "invalid_lan_settings",
                "局域网设置字段不完整或包含未知字段。",
                status=400,
            )
        enabled = payload.get("enabled")
        port = payload.get("port")
        if (
            not isinstance(enabled, bool)
            or isinstance(port, bool)
            or not isinstance(port, int)
        ):
            raise LanAccessError(
                "invalid_lan_settings",
                "enabled 必须是布尔值，port 必须是整数。",
                status=400,
            )
        try:
            candidate = LanAccessSettings(
                enabled=enabled,
                bind_host=str(payload.get("bind_host") or ""),
                port=port,
                display_name=str(payload.get("display_name") or ""),
            ).normalized()
            previous = self._settings.load()
            saved = self._settings.save(candidate)
        except LanSettingsError as exc:
            raise LanAccessError(
                "invalid_lan_settings",
                str(exc),
                status=400,
            ) from exc
        with self._lock:
            running = bool(self._server is not None and self._server.is_running)
        if running and not saved.enabled:
            self.stop()
        elif running and _listener_changed(previous, saved):
            self.stop()
            try:
                self.start()
            except LanAccessError:
                # Restore the last known listener so a typo does not strand a
                # previously working paired device after a failed restart.
                with suppress(Exception):
                    self._settings.save(previous)
                    if previous.enabled:
                        self.start()
                raise
        return self.status()

    def start(self) -> dict[str, Any]:
        try:
            settings = self._settings.load()
        except LanSettingsError as exc:
            raise LanAccessError(
                "lan_settings_unavailable",
                "局域网设置无法读取。",
                status=500,
            ) from exc
        if not settings.enabled:
            raise LanAccessError(
                "lan_access_disabled",
                "请先勾选允许 Android 局域网访问并保存设置。",
                status=409,
            )
        if self._credential_error is not None or self._credentials is None:
            raise LanAccessError(
                "lan_credentials_unavailable",
                "已配对设备凭据无法读取，请检查配置目录权限。",
                status=500,
            )
        with self._lock:
            current = self._server
            if current is not None and current.is_running:
                return self.status()
        server: Any | None = None
        try:
            server = self._gateway_factory(
                instance_id=self._identity.load_or_create(),
                name=settings.display_name,
                advertised_host=settings.bind_host,
                search_backend=self._adapter,
                media_resolver=self._adapter,
                credential_store=self._credentials,
                upload_directory=self._config_home / "lan-query-images",
                bind_host=settings.bind_host,
                api_port=settings.port,
                discovery_port=DISCOVERY_PORT,
            )
            server.start()
        except Exception as exc:
            if server is not None:
                with suppress(Exception):
                    server.stop()
            with self._lock:
                self._last_error = "局域网服务启动失败，请检查端口和防火墙。"
            raise LanAccessError(
                "lan_start_failed",
                "局域网服务启动失败，请检查端口和防火墙。",
                status=503,
            ) from exc
        with self._lock:
            self._server = server
            self._last_error = ""
        return self.status()

    def start_if_enabled(self) -> None:
        """Best-effort application startup; LAN failure never blocks desktop UI."""

        try:
            if self._settings.load().enabled:
                self.start()
        except Exception:
            with self._lock:
                if not self._last_error:
                    self._last_error = "局域网服务自动启动失败。"

    def stop(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
        if server is not None:
            try:
                server.stop()
            except Exception as exc:
                with self._lock:
                    self._last_error = "局域网服务停止失败。"
                raise LanAccessError(
                    "lan_stop_failed",
                    "局域网服务停止失败。",
                    status=500,
                ) from exc
        with self._lock:
            if self._server is server:
                self._server = None
            self._last_error = ""
        return self.status()

    def approve(self, pairing_id: str) -> dict[str, Any]:
        server = self._running_server()
        try:
            server.approve_pairing(pairing_id)
        except Exception as exc:
            raise LanAccessError(
                "pairing_approval_failed",
                "配对请求不存在、已过期或已处理。",
                status=409,
            ) from exc
        return self.status()

    def reject(self, pairing_id: str) -> dict[str, Any]:
        server = self._running_server()
        try:
            server.reject_pairing(pairing_id)
        except Exception as exc:
            raise LanAccessError(
                "pairing_rejection_failed",
                "配对请求不存在、已过期或已处理。",
                status=409,
            ) from exc
        return self.status()

    def revoke_device(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
        try:
            if server is not None:
                server.revoke_device()
            elif self._credentials is not None:
                self._credentials.delete_all()
        except Exception as exc:
            raise LanAccessError(
                "device_revoke_failed",
                "无法撤销 Android 设备访问。",
                status=500,
            ) from exc
        return self.status()

    def close(self) -> None:
        with suppress(LanAccessError):
            self.stop()

    def _running_server(self) -> Any:
        with self._lock:
            server = self._server
            if server is None or not server.is_running:
                raise LanAccessError(
                    "lan_not_running",
                    "局域网服务尚未运行。",
                    status=409,
                )
            return server

    def _paired_clients(self, server: Any | None) -> tuple[Any, ...]:
        try:
            if server is not None:
                return tuple(server.paired_clients())
            if self._credentials is None:
                return ()
            values = self._credentials.list_clients()
            return tuple(
                _ClientView(value.device_id, value.device_name, value.paired_at)
                for value in values
            )
        except Exception:
            return ()


@dataclass(frozen=True, slots=True)
class _ClientView:
    device_id: str
    device_name: str
    paired_at: float


def _listener_changed(before: LanAccessSettings, after: LanAccessSettings) -> bool:
    return (
        before.bind_host,
        before.port,
        before.display_name,
    ) != (
        after.bind_host,
        after.port,
        after.display_name,
    )


def _adapter_failure(
    exc: Exception,
    fallback_code: str,
    status: int,
) -> LanBackendError:
    code = _safe_text(getattr(exc, "code", None), maximum=128) or fallback_code
    supplied_status = getattr(exc, "status", status)
    safe_status = supplied_status if isinstance(supplied_status, int) else status
    if code == "backend_not_ready":
        message = "电脑端图库服务尚未就绪，请稍后重试。"
    elif code in {"invalid_search", "invalid_request"}:
        message = "搜索参数无效。"
        safe_status = 400
    elif code == "query_image_not_found":
        message = "查询图片不存在或已过期。"
        safe_status = 404
    else:
        message = "电脑端暂时无法完成此操作。"
    return LanBackendError(code, message, status=max(400, min(599, safe_status)))


def _client_id(value: object) -> str:
    normalized = _safe_text(value, maximum=160)
    if not normalized:
        raise LanBackendError("invalid_client", "设备会话无效。", status=401)
    return normalized


def _safe_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character in normalized for character in "\r\n\0")
    ):
        return ""
    return normalized


def _optional_text(value: object, *, maximum: int) -> str | None:
    return _safe_text(value, maximum=maximum) or None


def _optional_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _positive_int(value: object, fallback: int) -> int:
    parsed = _optional_positive_int(value)
    return parsed if parsed is not None else fallback


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _string_tuple(value: object, *, maximum_items: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    output: list[str] = []
    for raw in value[:maximum_items]:
        normalized = _safe_text(raw, maximum=256)
        if normalized and normalized not in output:
            output.append(normalized)
    return tuple(output)


def _result_score(value: Mapping[str, Any]) -> float | None:
    for key in ("ranking_confidence", "confidence", "raw_score"):
        raw = value.get(key)
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and math.isfinite(float(raw))
        ):
            return float(raw)
    return None


def _normalized_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if _SHA256_PATTERN.fullmatch(normalized) else None


def _content_type(value: object) -> str | None:
    name = _safe_text(value, maximum=1_024)
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed if guessed and guessed.startswith("image/") else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


__all__ = [
    "LanAccessController",
    "LanAccessError",
    "PreviewLanAdapter",
    "PreviewLanFacade",
]
