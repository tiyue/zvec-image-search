"""Composite lifecycle and local Windows control surface for LAN access."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .discovery import DISCOVERY_PORT, DiscoveryServer
from .http_server import API_PORT, LanApiServer
from .models import LanSearchBackend, MediaResolver
from .pairing import (
    CredentialStore,
    MemoryCredentialStore,
    PairedClientView,
    PairingManager,
    PairingView,
)
from .uploads import (
    DEFAULT_MIN_FREE_BYTES,
    DEFAULT_SPACE_CHECK_INTERVAL_BYTES,
    QueryImageStore,
)


class LanGatewayError(RuntimeError):
    """The combined LAN API and discovery service could not start."""


@dataclass(frozen=True, slots=True)
class LanGatewayAddress:
    advertised_host: str
    api_port: int
    discovery_port: int

    @property
    def origin(self) -> str:
        return f"http://{self.advertised_host}:{self.api_port}"


class LanGatewayServer:
    """Start/stop the HTTP data plane and UDP discovery as one unit."""

    def __init__(
        self,
        *,
        instance_id: str,
        name: str,
        advertised_host: str,
        search_backend: LanSearchBackend,
        media_resolver: MediaResolver,
        credential_store: CredentialStore | None = None,
        upload_directory: str | Path | None = None,
        upload_min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        upload_space_check_interval_bytes: int = DEFAULT_SPACE_CHECK_INTERVAL_BYTES,
        bind_host: str = "0.0.0.0",
        api_port: int = API_PORT,
        discovery_port: int = DISCOVERY_PORT,
    ) -> None:
        credentials = (
            MemoryCredentialStore() if credential_store is None else credential_store
        )
        self._instance_id = instance_id
        self._name = name
        self._advertised_host = advertised_host
        self._discovery_port = discovery_port
        self._search_backend = search_backend
        self._pairing = PairingManager(credentials)
        self._query_images = QueryImageStore(
            upload_directory,
            min_free_bytes=upload_min_free_bytes,
            space_check_interval_bytes=upload_space_check_interval_bytes,
        )
        self._api = LanApiServer(
            instance_id=instance_id,
            name=name,
            search_backend=search_backend,
            media_resolver=media_resolver,
            pairing_manager=self._pairing,
            query_images=self._query_images,
            host=bind_host,
            port=api_port,
            allowed_hosts=(advertised_host,),
        )
        self._discovery: DiscoveryServer | None = None
        self._address: LanGatewayAddress | None = None
        self._lock = threading.RLock()

    @property
    def address(self) -> LanGatewayAddress:
        address = self._address
        if address is None:
            raise LanGatewayError("LAN gateway is not running")
        return address

    @property
    def is_running(self) -> bool:
        discovery = self._discovery
        return self._api.is_running and discovery is not None and discovery.is_running

    @property
    def pairing_manager(self) -> PairingManager:
        return self._pairing

    @property
    def api_server(self) -> LanApiServer:
        return self._api

    def start(self) -> LanGatewayAddress:
        with self._lock:
            if self.is_running:
                return self.address
            cleanup_error = self._cleanup_client_sessions(
                self._pairing.paired_clients()
            )
            self._query_images.close()
            if cleanup_error is not None:
                raise LanGatewayError(
                    "could not clean previous LAN client sessions"
                ) from cleanup_error
            discovery: DiscoveryServer | None = None
            try:
                api_address = self._api.start()
                discovery = DiscoveryServer(
                    instance_id=self._instance_id,
                    name=self._name,
                    advertised_host=self._advertised_host,
                    api_port=api_address.port,
                    port=self._discovery_port,
                )
                discovery_address = discovery.start()
            except Exception as exc:
                if discovery is not None:
                    discovery.stop()
                self._api.stop()
                self._cleanup_client_sessions(self._pairing.paired_clients())
                self._query_images.close()
                raise LanGatewayError("could not start LAN gateway") from exc
            address = LanGatewayAddress(
                advertised_host=self._advertised_host,
                api_port=api_address.port,
                discovery_port=discovery_address.port,
            )
            self._discovery = discovery
            self._address = address
            return address

    def stop(self) -> None:
        with self._lock:
            discovery = self._discovery
            errors: list[Exception] = []
            try:
                self._api.stop()
            except Exception as exc:
                errors.append(exc)
            if discovery is not None:
                try:
                    discovery.stop()
                except Exception as exc:
                    errors.append(exc)
            cleanup_error = self._cleanup_client_sessions(
                self._pairing.paired_clients()
            )
            if cleanup_error is not None:
                errors.append(cleanup_error)
            self._query_images.close()
            if self._discovery is discovery:
                self._discovery = None
                self._address = None
            if errors:
                raise LanGatewayError("could not fully stop LAN gateway") from errors[0]

    def pending_pairings(self) -> tuple[PairingView, ...]:
        return self._pairing.pending_requests()

    def approve_pairing(self, pairing_id: str) -> PairingView:
        return self._pairing.approve(pairing_id)

    def reject_pairing(self, pairing_id: str) -> PairingView:
        return self._pairing.reject(pairing_id)

    def paired_clients(self) -> tuple[PairedClientView, ...]:
        return self._pairing.paired_clients()

    def revoke_client(self, device_id: str) -> bool:
        """Revoke a locally selected device by its public device id."""

        with self._lock:
            target = next(
                (
                    client
                    for client in self._pairing.paired_clients()
                    if client.device_id == device_id
                ),
                None,
            )
            if target is None:
                return False
            revocation_error: Exception | None = None
            revoked = False
            try:
                revoked = self._pairing.revoke_client(device_id)
            except Exception as exc:
                revocation_error = exc
            cleanup_error = self._cleanup_client_sessions((target,))
            if cleanup_error is not None or revocation_error is not None:
                cause = cleanup_error or revocation_error
                raise LanGatewayError("could not fully revoke LAN client") from cause
            return revoked

    def revoke_device(self, device_id: str | None = None) -> bool:
        """Revoke one device, or every paired device when no id is supplied."""

        if device_id is not None:
            return self.revoke_client(device_id)
        with self._lock:
            clients = self._pairing.paired_clients()
            revocation_error: Exception | None = None
            revoked = False
            try:
                revoked = self._pairing.revoke_device()
            except Exception as exc:
                revocation_error = exc
            cleanup_error = self._cleanup_client_sessions(clients)
            self._query_images.close()
            if cleanup_error is not None or revocation_error is not None:
                cause = cleanup_error or revocation_error
                raise LanGatewayError("could not fully revoke LAN clients") from cause
            return revoked

    def _cleanup_client_sessions(
        self,
        clients: tuple[PairedClientView, ...],
    ) -> Exception | None:
        first_error: Exception | None = None
        for client in clients:
            try:
                self._search_backend.delete_client_session(client_id=client.session_id)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            try:
                self._query_images.delete_owner(client.session_id)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        return first_error

    def __enter__(self) -> LanGatewayServer:
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        self.stop()
