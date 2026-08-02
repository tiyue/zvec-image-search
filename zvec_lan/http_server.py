"""Narrow authenticated HTTP data plane for the Android LAN viewer."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import select
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, NoReturn, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, unquote, urlsplit

from .models import (
    LanBackendError,
    LanSearchBackend,
    LibraryInfo,
    MediaResolver,
    MediaSource,
    RecommendationAction,
    RecommendationBackend,
    SearchMode,
    SearchPage,
    SearchPending,
    SearchRequest,
    libraries_payload,
    search_page_payload,
)
from .pairing import (
    AuthenticatedClient,
    CredentialStoreError,
    InvalidPairingRequest,
    PairingCapacityExceeded,
    PairingError,
    PairingExpired,
    PairingManager,
    PairingNotFound,
    PairingSecretRejected,
)
from .uploads import (
    EmptyUpload,
    InvalidUpload,
    QueryImageNotFound,
    QueryImageStore,
    UploadCancelled,
    UploadStorageError,
)

API_PORT = 38522
PROTOCOL_VERSION = 1
DEFAULT_STREAM_CHUNK_BYTES = 256 * 1024
DEFAULT_CONNECTION_IDLE_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_CONNECTIONS = 64
_MAX_JSON_BYTES = 64 * 1024
_MAX_CHUNK_LINE_BYTES = 8192
_MAX_TRAILER_BYTES = 64 * 1024
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n]*")
_LOGGER = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class LanHttpError(RuntimeError):
    """The LAN HTTP server could not start or provide its address."""


@dataclass(frozen=True, slots=True)
class LanApiAddress:
    host: str
    port: int

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class _ApiProblem(Exception):
    status: int
    code: str
    message: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class LanApiServer:
    """Serve only the versioned, read-only Android LAN route allow-list."""

    def __init__(
        self,
        *,
        instance_id: str,
        name: str,
        search_backend: LanSearchBackend,
        media_resolver: MediaResolver,
        pairing_manager: PairingManager,
        query_images: QueryImageStore,
        recommendation_backend: RecommendationBackend | None = None,
        host: str = "0.0.0.0",
        port: int = API_PORT,
        stream_chunk_bytes: int = DEFAULT_STREAM_CHUNK_BYTES,
        allowed_hosts: Sequence[str] | None = None,
        connection_idle_timeout: float = DEFAULT_CONNECTION_IDLE_TIMEOUT_SECONDS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        if not isinstance(instance_id, str) or not 1 <= len(instance_id) <= 160:
            raise ValueError("instance_id is invalid")
        normalized_name = name.strip()
        if not 1 <= len(normalized_name) <= 128:
            raise ValueError("name is invalid")
        if isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if isinstance(stream_chunk_bytes, bool) or stream_chunk_bytes < 1:
            raise ValueError("stream_chunk_bytes must be positive")
        if (
            isinstance(connection_idle_timeout, bool)
            or not isinstance(connection_idle_timeout, (int, float))
            or not math.isfinite(connection_idle_timeout)
            or connection_idle_timeout <= 0
        ):
            raise ValueError("connection_idle_timeout must be positive and finite")
        if isinstance(max_connections, bool) or max_connections < 1:
            raise ValueError("max_connections must be positive")
        self._instance_id = instance_id
        self._name = normalized_name
        self._search_backend = search_backend
        self._recommendation_backend = recommendation_backend
        self._media_resolver = media_resolver
        self._pairing = pairing_manager
        self._query_images = query_images
        self._pairing.set_session_cleanup_callback(self._cleanup_session_resources)
        self._host = host
        supplied_hosts = (host,) if allowed_hosts is None else tuple(allowed_hosts)
        normalized_hosts: set[str] = set()
        for supplied_host in supplied_hosts:
            try:
                address = ipaddress.ip_address(supplied_host)
            except ValueError as exc:
                raise ValueError(
                    "allowed_hosts must contain only IPv4 addresses"
                ) from exc
            if address.version != 4:
                raise ValueError("allowed_hosts must contain only IPv4 addresses")
            normalized_hosts.add(str(address))
        if not normalized_hosts:
            raise ValueError("allowed_hosts must not be empty")
        if host != "0.0.0.0":
            try:
                bound_address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError("host must be an IPv4 address") from exc
            if bound_address.version != 4:
                raise ValueError("host must be an IPv4 address")
            normalized_hosts.add(str(bound_address))
        self._allowed_hosts = frozenset(normalized_hosts)
        self._configured_port = port
        self._stream_chunk_bytes = stream_chunk_bytes
        self._connection_idle_timeout = float(connection_idle_timeout)
        self._max_connections = max_connections
        self._httpd: _LanThreadingHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._address: LanApiAddress | None = None
        self._generation = 0
        self._lock = threading.RLock()

    @property
    def address(self) -> LanApiAddress:
        address = self._address
        if address is None:
            raise LanHttpError("LAN API server is not running")
        return address

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return self._httpd is not None and thread is not None and thread.is_alive()

    @property
    def pairing_manager(self) -> PairingManager:
        return self._pairing

    @property
    def query_image_store(self) -> QueryImageStore:
        return self._query_images

    def start(self) -> LanApiAddress:
        with self._lock:
            if self.is_running:
                return self.address
            generation = self._generation + 1
            try:
                httpd = _LanThreadingHttpServer(
                    (self._host, self._configured_port),
                    _LanRequestHandler,
                    gateway=self,
                    generation=generation,
                    connection_idle_timeout=self._connection_idle_timeout,
                    max_connections=self._max_connections,
                )
            except OSError as exc:
                raise LanHttpError("could not start LAN API server") from exc
            host, port = httpd.server_address[:2]
            self._generation = generation
            self._httpd = httpd
            self._address = LanApiAddress(str(host), int(port))
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="zvec-lan-http",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._address

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            if httpd is None:
                return
            # Invalidate the generation before waiting for serve_forever. Old
            # keep-alive handlers therefore fail closed even if a platform
            # cannot immediately interrupt their socket read.
            httpd.deactivate_connections()
            if self._httpd is httpd:
                self._httpd = None
                self._thread = None
                self._address = None
            httpd.shutdown()
            httpd.server_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)

    def _is_current_generation(self, httpd: _LanThreadingHttpServer) -> bool:
        with self._lock:
            return (
                self._httpd is httpd
                and self._generation == httpd.generation
                and httpd.is_accepting_connections
            )

    def _cleanup_session_resources(self, session_id: str) -> None:
        first_error: Exception | None = None
        try:
            self._search_backend.delete_client_session(client_id=session_id)
        except Exception as exc:
            first_error = exc
        try:
            self._query_images.delete_owner(session_id)
        except Exception as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


class _LanThreadingHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        gateway: LanApiServer,
        generation: int,
        connection_idle_timeout: float,
        max_connections: int,
    ) -> None:
        self.gateway = gateway
        self.generation = generation
        self.connection_idle_timeout = connection_idle_timeout
        self.max_connections = max_connections
        self._connection_lock = threading.RLock()
        self._connections: set[socket.socket] = set()
        self._accepting_connections = True
        super().__init__(server_address, handler)

    @property
    def is_accepting_connections(self) -> bool:
        with self._connection_lock:
            return self._accepting_connections

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        with self._connection_lock:
            if (
                not self._accepting_connections
                or len(self._connections) >= self.max_connections
            ):
                request.close()
                raise OSError("LAN server generation is inactive")
            request.settimeout(self.connection_idle_timeout)
            self._connections.add(request)
        return request, address

    def shutdown_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
    ) -> None:
        try:
            super().shutdown_request(request)
        finally:
            if isinstance(request, socket.socket):
                with self._connection_lock:
                    self._connections.discard(request)

    def close_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
    ) -> None:
        try:
            super().close_request(request)
        finally:
            if isinstance(request, socket.socket):
                with self._connection_lock:
                    self._connections.discard(request)

    def deactivate_connections(self) -> None:
        """Invalidate this generation and interrupt every accepted socket."""

        with self._connection_lock:
            self._accepting_connections = False
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: object,
    ) -> None:
        exception = sys.exc_info()[1]
        if not self.is_accepting_connections or isinstance(
            exception,
            (ConnectionError, TimeoutError, OSError),
        ):
            return
        super().handle_error(request, client_address)


class _LanRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZvecLAN/1"
    sys_version = ""
    # Raw SocketIO prevents buffered reads from hiding body bytes from the
    # generation-aware cancellable receiver below. Headers remain tiny and
    # retain the socket-level idle timeout installed at accept time.
    rbufsize = 0

    @property
    def gateway(self) -> LanApiServer:
        return cast(_LanThreadingHttpServer, self.server).gateway

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def do_TRACE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._dispatch()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        if hasattr(self, "headers") and self.headers:
            try:
                self._validate_network_boundary()
            except _ApiProblem as problem:
                self._send_problem(problem)
                return
        self._send_problem(
            _ApiProblem(
                status=code,
                code="http_error",
                message="The HTTP request could not be completed.",
            )
        )

    def log_message(self, _format: str, *args: object) -> None:
        # Request targets can be attacker-controlled.  In particular, never
        # risk logging a client secret that a broken client placed in a URL.
        return

    def _dispatch(self) -> None:
        try:
            self._authenticated_session_id: str | None = None
            self._require_current_generation()
            self._validate_network_boundary()
            target = urlsplit(self.path)
            path = target.path
            if path == "/api/v1/pair-requests":
                self._require_method("POST")
                self._require_no_query(target.query)
                self._create_pair_request()
                return

            pairing_match = re.fullmatch(
                r"/api/v1/pair-requests/([^/]+)/poll",
                path,
            )
            if pairing_match is not None:
                self._require_method("POST")
                self._require_no_query(target.query)
                self._poll_pair_request(_opaque_segment(pairing_match.group(1)))
                return

            if path == "/api/v1/status":
                self._require_method("GET")
                self._require_no_query(target.query)
                self._authenticate()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "instance_id": self.gateway._instance_id,
                        "name": self.gateway._name,
                    },
                )
                return

            if path == "/api/v1/libraries":
                self._require_method("GET")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                libraries = self._backend_call(
                    self.gateway._search_backend.list_libraries,
                    client_id=client.session_id,
                )
                self._send_json(
                    HTTPStatus.OK,
                    libraries_payload(_validated_libraries(libraries)),
                )
                return

            if path == "/api/v1/query-images":
                self._require_method("POST")
                client, _token = self._authenticate()
                self._upload_query_image(client, target.query)
                return

            query_image_match = re.fullmatch(
                r"/api/v1/query-images/([^/]+)",
                path,
            )
            if query_image_match is not None:
                self._require_method("DELETE")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                query_image_id = _opaque_segment(query_image_match.group(1))
                self._delete_query_image(query_image_id, client)
                return

            if path == "/api/v1/searches":
                self._require_method("POST")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                self._create_search(client)
                return

            search_match = re.fullmatch(r"/api/v1/searches/([^/]+)", path)
            if search_match is not None:
                search_id = _opaque_segment(search_match.group(1))
                if self.command == "GET":
                    client, _token = self._authenticate()
                    self._search_page(search_id, client, target.query)
                    return
                if self.command == "DELETE":
                    self._require_no_query(target.query)
                    client, _token = self._authenticate()
                    self._delete_search(search_id, client)
                    return
                self._method_not_allowed(("GET", "DELETE"))

            if path == "/api/v1/recommendations":
                self._require_method("POST")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                self._create_recommendations(client)
                return

            recommendation_match = re.fullmatch(
                r"/api/v1/recommendations/([^/]+)/(shown|actions)",
                path,
            )
            if recommendation_match is not None:
                self._require_method("POST")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                batch_id = _opaque_segment(recommendation_match.group(1))
                if recommendation_match.group(2) == "shown":
                    self._mark_recommendations_shown(batch_id, client)
                else:
                    self._record_recommendation_action(batch_id, client)
                return

            media_match = re.fullmatch(
                r"/api/v1/media/([^/]+)/original",
                path,
            )
            if media_match is not None:
                self._require_method("GET", "HEAD")
                self._require_no_query(target.query)
                client, _token = self._authenticate()
                self._serve_original(_opaque_segment(media_match.group(1)), client)
                return

            if path == "/api/v1/session":
                self._require_method("DELETE")
                self._require_no_query(target.query)
                client, token = self._authenticate()
                self._delete_client_session(client, token)
                return

            raise _ApiProblem(404, "route_not_found", "The route was not found.")
        except _ApiProblem as exc:
            self._send_problem(exc)
        except PairingError as exc:
            self._send_problem(_pairing_problem(exc))
        except QueryImageNotFound:
            self._send_problem(
                _ApiProblem(404, "query_image_not_found", "Query image was not found.")
            )
        except EmptyUpload:
            self._send_problem(
                _ApiProblem(400, "empty_upload", "Query image is empty.")
            )
        except InvalidUpload:
            self._send_problem(
                _ApiProblem(400, "invalid_upload", "Query-image upload is invalid.")
            )
        except UploadCancelled:
            self.close_connection = True
            self._send_problem(
                _ApiProblem(400, "upload_cancelled", "Query-image upload stopped.")
            )
        except UploadStorageError:
            # A failed streaming upload can leave unread body bytes on this
            # connection.  Closing it prevents request-smuggling/desync on the
            # next keep-alive request while the store removes its partial file.
            self.close_connection = True
            self._send_problem(
                _ApiProblem(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    "upload_storage_error",
                    "Query-image storage is unavailable.",
                )
            )
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception:
            _LOGGER.exception("Unhandled LAN API request failure")
            self._send_problem(
                _ApiProblem(
                    500,
                    "internal_error",
                    "The request could not be completed.",
                )
            )

    def _validate_network_boundary(self) -> None:
        """Reject browser-originated and DNS-rebound requests before routing."""

        browser_headers = {
            name.lower()
            for name in self.headers
            if name.lower() in {"origin", "referer"}
            or name.lower().startswith("sec-fetch-")
        }
        if browser_headers:
            raise _ApiProblem(
                HTTPStatus.FORBIDDEN,
                "browser_request_rejected",
                "Browser-originated requests are not accepted.",
            )
        host_values = self.headers.get_all("Host", failobj=[])
        if len(host_values) != 1:
            raise _ApiProblem(
                HTTPStatus.MISDIRECTED_REQUEST,
                "invalid_host",
                "The request host is invalid.",
            )
        raw_host = host_values[0].strip()
        expected_port = cast(_LanThreadingHttpServer, self.server).server_port
        # _validated_host_header already rejects DNS names and non-IPv4 hosts,
        # preventing DNS-rebinding.  Any valid IPv4 is accepted so that tunneled
        # connections (e.g. frp with a public IP) work alongside direct LAN use.
        _validated_host_header(raw_host, expected_port)

    def _require_current_generation(self) -> None:
        httpd = cast(_LanThreadingHttpServer, self.server)
        if not self.gateway._is_current_generation(httpd):
            self.close_connection = True
            raise _ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "server_stopped",
                "The LAN server is no longer active.",
            )

    def _create_pair_request(self) -> None:
        payload = self._read_json_object()
        _require_exact_fields(
            payload,
            required={"device_id", "device_name", "client_secret"},
        )
        view = self.gateway._pairing.create_request(
            device_id=_required_json_string(payload, "device_id"),
            device_name=_required_json_string(payload, "device_name"),
            client_secret=_required_json_string(payload, "client_secret"),
        )
        self._send_json(
            HTTPStatus.CREATED,
            self.gateway._pairing.start_payload(view),
        )

    def _poll_pair_request(self, pairing_id: str) -> None:
        payload = self._read_json_object()
        _require_exact_fields(payload, required={"client_secret"})
        result = self.gateway._pairing.poll(
            pairing_id,
            client_secret=_required_json_string(payload, "client_secret"),
        )
        response: dict[str, object] = {"status": result.status}
        if result.token is not None:
            response["token"] = result.token
        self._send_json(HTTPStatus.OK, response)

    def _upload_query_image(
        self,
        client: AuthenticatedClient,
        raw_query: str,
    ) -> None:
        self._require_active_session(client)
        values = _parse_query_string(raw_query)
        if set(values) - {"name"} or len(values.get("name", [])) > 1:
            raise _ApiProblem(400, "invalid_query", "Upload query is invalid.")
        display_name = values.get("name", ["query-image"])[0]
        if len(display_name) > 1024:
            raise _ApiProblem(400, "invalid_query", "Upload query is invalid.")
        expected_size_bytes = self._upload_content_length()
        image = self.gateway._query_images.store(
            self._iter_request_body(expected_size_bytes),
            owner_id=client.session_id,
            display_name=display_name,
            content_type=self.headers.get("Content-Type"),
            expected_size_bytes=expected_size_bytes,
        )
        try:
            self._require_active_session(client)
        except _ApiProblem:
            with suppress(Exception):
                self.gateway._query_images.delete(
                    image.query_image_id,
                    owner_id=client.session_id,
                )
            raise
        self._send_json(
            HTTPStatus.CREATED,
            {"query_image_id": image.query_image_id},
        )

    def _delete_query_image(
        self,
        query_image_id: str,
        client: AuthenticatedClient,
    ) -> None:
        self.gateway._query_images.delete(
            query_image_id,
            owner_id=client.session_id,
        )
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _create_search(self, client: AuthenticatedClient) -> None:
        self._require_active_session(client)
        payload = self._read_json_object()
        request = self._validated_search_request(payload, client)
        search_id = self._backend_call(
            self.gateway._search_backend.create_search,
            request,
            client_id=client.session_id,
        )
        search_id = _validated_opaque_id(search_id, code="invalid_search_response")
        try:
            self._require_active_session(client)
        except _ApiProblem:
            with suppress(Exception):
                self.gateway._search_backend.delete_search(
                    search_id,
                    client_id=client.session_id,
                )
            with suppress(Exception):
                self._cleanup_session_resources(client.session_id)
            raise
        self._send_json(HTTPStatus.ACCEPTED, {"search_id": search_id})

    def _create_recommendations(self, client: AuthenticatedClient) -> None:
        self._require_active_session(client)
        payload = self._read_json_object()
        _require_exact_fields(payload, required={"request_id"})
        request_id = _validated_opaque_id(
            _required_json_string(payload, "request_id"),
            code="invalid_request",
        )
        backend = self._recommendation_backend_or_problem()
        response = self._backend_call(
            backend.create_recommendations,
            request_id,
            client_id=client.session_id,
            device_id=client.device_id,
        )
        if not isinstance(response, Mapping):
            raise _ApiProblem(
                500,
                "invalid_recommendation_response",
                "Recommendations are unavailable.",
            )
        self._require_active_session(client)
        self._send_json(HTTPStatus.OK, response)

    def _mark_recommendations_shown(
        self,
        batch_id: str,
        client: AuthenticatedClient,
    ) -> None:
        self._require_active_session(client)
        payload = self._read_json_object()
        _require_exact_fields(payload, required={"event_id"})
        event_id = _validated_opaque_id(
            _required_json_string(payload, "event_id"),
            code="invalid_request",
        )
        backend = self._recommendation_backend_or_problem()
        self._backend_call(
            backend.mark_recommendations_shown,
            batch_id,
            event_id,
            client_id=client.session_id,
            device_id=client.device_id,
        )
        self._require_active_session(client)
        self._send_json(HTTPStatus.OK, {"ok": True, "event_id": event_id})

    def _record_recommendation_action(
        self,
        batch_id: str,
        client: AuthenticatedClient,
    ) -> None:
        self._require_active_session(client)
        payload = self._read_json_object()
        _require_exact_fields(
            payload,
            required={"event_id", "item_id", "action"},
            allowed={"event_id", "item_id", "action", "metadata"},
        )
        event_id = _validated_opaque_id(
            _required_json_string(payload, "event_id"),
            code="invalid_request",
        )
        item_id = _validated_opaque_id(
            _required_json_string(payload, "item_id"),
            code="invalid_request",
        )
        action = _validated_recommendation_action(payload.get("action"))
        metadata = _validated_recommendation_metadata(payload.get("metadata"))
        backend = self._recommendation_backend_or_problem()
        action_status = self._backend_call(
            backend.record_recommendation_action,
            batch_id,
            event_id,
            item_id,
            action,
            metadata,
            client_id=client.session_id,
            device_id=client.device_id,
        )
        self._require_active_session(client)
        preference = action_status.get("preference")
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "event_id": event_id,
                "recorded": action_status.get("recorded") is True,
                "preference": (
                    preference if preference in {"like", "dislike"} else None
                ),
            },
        )

    def _recommendation_backend_or_problem(self) -> RecommendationBackend:
        backend = self.gateway._recommendation_backend
        if backend is None:
            raise _ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "recommendations_unavailable",
                "Recommendations are unavailable.",
            )
        return backend

    def _validated_search_request(
        self,
        payload: Mapping[str, object],
        client: AuthenticatedClient,
    ) -> SearchRequest:
        allowed = {"mode", "text", "query_image_id", "library_ids", "top_k"}
        _require_exact_fields(payload, required={"mode"}, allowed=allowed)
        mode = payload.get("mode")
        if mode not in {"text", "tag", "image", "combined"}:
            raise _ApiProblem(400, "invalid_search", "Search mode is invalid.")
        raw_text = payload.get("text")
        if raw_text is None:
            text = None
        elif isinstance(raw_text, str) and len(raw_text) <= 4096:
            text = raw_text.strip() or None
        else:
            raise _ApiProblem(400, "invalid_search", "Search text is invalid.")
        raw_query_image_id = payload.get("query_image_id")
        query_image_id = (
            None
            if raw_query_image_id is None
            else _validated_opaque_id(raw_query_image_id, code="invalid_search")
        )
        raw_libraries = payload.get("library_ids", [])
        if not isinstance(raw_libraries, list):
            raise _ApiProblem(400, "invalid_search", "Library ids are invalid.")
        library_ids = tuple(
            _validated_opaque_id(value, code="invalid_search")
            for value in raw_libraries
        )
        if len(set(library_ids)) != len(library_ids):
            raise _ApiProblem(400, "invalid_search", "Library ids are invalid.")
        top_k = payload.get("top_k", 100)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise _ApiProblem(400, "invalid_search", "top_k must be positive.")

        if mode in {"text", "tag"}:
            if text is None or query_image_id is not None:
                raise _ApiProblem(400, "invalid_search", "Search input is invalid.")
        elif mode == "image":
            if query_image_id is None or text is not None:
                raise _ApiProblem(400, "invalid_search", "Search input is invalid.")
        elif text is None or query_image_id is None:
            raise _ApiProblem(400, "invalid_search", "Search input is invalid.")

        query_image_path: Path | None = None
        if query_image_id is not None:
            query_image_path = self.gateway._query_images.get(
                query_image_id,
                owner_id=client.session_id,
            ).path
        return SearchRequest(
            mode=cast(SearchMode, mode),
            text=text,
            query_image_id=query_image_id,
            query_image_path=query_image_path,
            library_ids=library_ids,
            top_k=top_k,
        )

    def _search_page(
        self,
        search_id: str,
        client: AuthenticatedClient,
        raw_query: str,
    ) -> None:
        self._require_active_session(client)
        values = _parse_query_string(raw_query)
        if set(values) - {"page", "page_size"}:
            raise _ApiProblem(400, "invalid_pagination", "Pagination is invalid.")
        page = _single_positive_query_integer(values, "page", default=1)
        page_size = _single_positive_query_integer(
            values,
            "page_size",
            default=30,
        )
        if page_size > 100:
            raise _ApiProblem(
                400,
                "invalid_pagination",
                "page_size must be between 1 and 100.",
            )
        result = self._backend_call(
            self.gateway._search_backend.get_search_page,
            search_id,
            page=page,
            page_size=page_size,
            client_id=client.session_id,
        )
        try:
            self._require_active_session(client)
        except _ApiProblem:
            with suppress(Exception):
                self._cleanup_session_resources(client.session_id)
            raise
        if isinstance(result, SearchPending):
            pending = _validated_search_pending(result, search_id=search_id)
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "search_id": pending.search_id,
                    "status": pending.status,
                    "retry_after_seconds": pending.retry_after_seconds,
                },
                headers=(("Retry-After", str(pending.retry_after_seconds)),),
            )
            return
        normalized = _validated_search_page(
            result,
            search_id=search_id,
            page=page,
            page_size=page_size,
        )
        self._send_json(HTTPStatus.OK, search_page_payload(normalized))

    def _delete_search(
        self,
        search_id: str,
        client: AuthenticatedClient,
    ) -> None:
        self._backend_call(
            self.gateway._search_backend.delete_search,
            search_id,
            client_id=client.session_id,
        )
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _delete_client_session(
        self,
        client: AuthenticatedClient,
        token: str,
    ) -> None:
        problem: _ApiProblem | None = None
        try:
            self.gateway._pairing.revoke(token)
        except PairingError as exc:
            problem = _pairing_problem(exc)
        try:
            self._cleanup_session_resources(client.session_id)
        except LanBackendError as exc:
            if problem is None:
                problem = _backend_problem(exc)
        except Exception:
            _LOGGER.exception("LAN client-session cleanup failed")
            if problem is None:
                problem = _ApiProblem(
                    500,
                    "session_cleanup_failed",
                    "Device session cleanup could not be completed.",
                )
        if problem is not None:
            raise problem
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _cleanup_session_resources(self, session_id: str) -> None:
        self.gateway._cleanup_session_resources(session_id)

    def _serve_original(
        self,
        media_id: str,
        client: AuthenticatedClient,
    ) -> None:
        self._require_active_session(client)
        source = self._backend_call(
            self.gateway._media_resolver.resolve_original,
            media_id,
            client_id=client.session_id,
        )
        if source is None:
            raise _ApiProblem(404, "media_not_found", "Original media was not found.")
        self._require_active_session(client)
        if (
            not isinstance(source, MediaSource)
            or _SHA256.fullmatch(source.sha256) is None
        ):
            raise _ApiProblem(
                500,
                "invalid_media_source",
                "Original media is unavailable.",
            )
        try:
            stream = source.path.open("rb", buffering=0)
        except (FileNotFoundError, NotADirectoryError):
            raise _ApiProblem(
                404,
                "media_not_found",
                "Original media was not found.",
            ) from None
        except OSError:
            raise _ApiProblem(
                503,
                "media_unavailable",
                "Original media is unavailable.",
            ) from None
        with stream:
            try:
                file_status = os.fstat(stream.fileno())
            except OSError:
                raise _ApiProblem(
                    503,
                    "media_unavailable",
                    "Original media is unavailable.",
                ) from None
            if not stat.S_ISREG(file_status.st_mode):
                raise _ApiProblem(
                    404,
                    "media_not_found",
                    "Original media was not found.",
                )
            size = file_status.st_size
            etag = f'"{source.sha256.lower()}"'
            common_headers = (
                ("Accept-Ranges", "bytes"),
                ("ETag", etag),
                ("Cache-Control", "private, no-cache"),
            )
            if _if_none_match(self.headers.get("If-None-Match"), etag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self._send_headers(common_headers)
                self.end_headers()
                return
            requested_range = self.headers.get_all("Range", failobj=[])
            byte_range: _ByteRange | None = None
            if requested_range:
                if len(requested_range) != 1:
                    self._range_not_satisfiable(size, etag)
                if_range = self.headers.get("If-Range")
                if if_range is None or hmac_etag_equal(if_range, etag):
                    byte_range = _parse_byte_range(requested_range[0], size, etag)
            content_type = _safe_media_type(source)
            if byte_range is None:
                self.send_response(HTTPStatus.OK)
                self._send_headers(common_headers)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self._stream_file(stream, size, session_id=client.session_id)
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self._send_headers(common_headers)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(byte_range.length))
            self.send_header(
                "Content-Range",
                f"bytes {byte_range.start}-{byte_range.end}/{size}",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                stream.seek(byte_range.start)
                self._stream_file(
                    stream,
                    byte_range.length,
                    session_id=client.session_id,
                )

    def _range_not_satisfiable(self, size: int, etag: str) -> NoReturn:
        raise _ApiProblem(
            HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
            "range_not_satisfiable",
            "The requested byte range is not available.",
            headers=(
                ("Accept-Ranges", "bytes"),
                ("Content-Range", f"bytes */{size}"),
                ("ETag", etag),
            ),
        )

    def _stream_file(
        self,
        stream: BinaryIO,
        count: int,
        *,
        session_id: str,
    ) -> None:
        remaining = count
        try:
            while remaining:
                if not self.gateway._pairing.is_session_active(session_id):
                    self.close_connection = True
                    return
                block = stream.read(min(self.gateway._stream_chunk_bytes, remaining))
                if not block:
                    self.close_connection = True
                    return
                self.wfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _authenticate(self) -> tuple[AuthenticatedClient, str]:
        self._require_current_generation()
        values = self.headers.get_all("Authorization", failobj=[])
        if len(values) != 1:
            raise _unauthorized_problem()
        scheme, separator, token = values[0].partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not token
            or any(character.isspace() for character in token)
        ):
            raise _unauthorized_problem()
        client = self.gateway._pairing.authenticate(token)
        if client is None:
            raise _unauthorized_problem()
        self._require_current_generation()
        self._require_active_session(client)
        self._authenticated_session_id = client.session_id
        return client, token

    def _require_active_session(self, client: AuthenticatedClient) -> None:
        if not self.gateway._pairing.is_session_active(client.session_id):
            self.close_connection = True
            raise _ApiProblem(
                HTTPStatus.UNAUTHORIZED,
                "session_revoked",
                "The device session is no longer active.",
                headers=(("WWW-Authenticate", "Bearer"),),
            )

    def _read_json_object(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise _ApiProblem(415, "unsupported_media_type", "JSON is required.")
        lengths = self.headers.get_all("Content-Length", failobj=[])
        transfer_encodings = self.headers.get_all("Transfer-Encoding", failobj=[])
        if transfer_encodings or len(lengths) != 1:
            raise _ApiProblem(
                411,
                "content_length_required",
                "Content-Length is required.",
            )
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 20:
            raise _ApiProblem(
                400,
                "invalid_content_length",
                "Content-Length is invalid.",
            )
        length = int(raw_length)
        if length > _MAX_JSON_BYTES:
            raise _ApiProblem(413, "json_too_large", "JSON request is too large.")
        body = self._read_exact_body_bytes(length)
        if len(body) != length:
            self.close_connection = True
            raise _ApiProblem(400, "incomplete_request", "Request body is incomplete.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _ApiProblem(400, "invalid_json", "JSON request is invalid.") from None
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) for key in payload
        ):
            raise _ApiProblem(400, "invalid_json", "A JSON object is required.")
        return cast(dict[str, object], payload)

    def _upload_content_length(self) -> int | None:
        lengths = self.headers.get_all("Content-Length", failobj=[])
        encodings = self.headers.get_all("Transfer-Encoding", failobj=[])
        if lengths and encodings:
            raise _ApiProblem(
                400,
                "ambiguous_request_body",
                "Request framing is invalid.",
            )
        if encodings:
            if len(encodings) != 1 or encodings[0].strip().lower() != "chunked":
                raise _ApiProblem(
                    400,
                    "unsupported_transfer_encoding",
                    "Chunked transfer encoding is required.",
                )
            return None
        if len(lengths) != 1:
            raise _ApiProblem(411, "length_required", "Upload length is required.")
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 20:
            raise _ApiProblem(
                400,
                "invalid_content_length",
                "Content-Length is invalid.",
            )
        return int(raw_length)

    def _iter_request_body(self, expected_size_bytes: int | None) -> Iterator[bytes]:
        if expected_size_bytes is None:
            yield from self._iter_chunked_body()
            return
        remaining = expected_size_bytes
        while remaining:
            block = self._read_body_bytes(
                min(self.gateway._stream_chunk_bytes, remaining)
            )
            if not block:
                self.close_connection = True
                raise UploadCancelled("query-image upload was interrupted")
            remaining -= len(block)
            yield block

    def _iter_chunked_body(self) -> Iterator[bytes]:
        while True:
            line = self._read_body_line(_MAX_CHUNK_LINE_BYTES)
            if not line:
                self.close_connection = True
                raise UploadCancelled("query-image upload was interrupted")
            if len(line) > _MAX_CHUNK_LINE_BYTES or not line.endswith(b"\r\n"):
                raise _invalid_chunked_body()
            size_text = line[:-2].split(b";", 1)[0].strip()
            if (
                not size_text
                or any(
                    character not in b"0123456789abcdefABCDEF"
                    for character in size_text
                )
                or len(size_text) > 16
            ):
                raise _invalid_chunked_body()
            size = int(size_text, 16)
            if size == 0:
                self._consume_trailers()
                return
            remaining = size
            while remaining:
                block = self._read_body_bytes(
                    min(self.gateway._stream_chunk_bytes, remaining)
                )
                if not block:
                    self.close_connection = True
                    raise UploadCancelled("query-image upload was interrupted")
                remaining -= len(block)
                yield block
            if self._read_exact_body_bytes(2) != b"\r\n":
                raise _invalid_chunked_body()

    def _consume_trailers(self) -> None:
        consumed = 0
        while True:
            line = self._read_body_line(_MAX_CHUNK_LINE_BYTES)
            if not line:
                self.close_connection = True
                raise UploadCancelled("query-image upload was interrupted")
            consumed += len(line)
            if (
                len(line) > _MAX_CHUNK_LINE_BYTES
                or consumed > _MAX_TRAILER_BYTES
                or not line.endswith(b"\r\n")
            ):
                raise _invalid_chunked_body()
            if line == b"\r\n":
                return

    def _read_exact_body_bytes(self, count: int) -> bytes:
        output = bytearray()
        while len(output) < count:
            block = self._read_body_bytes(count - len(output))
            if not block:
                break
            output.extend(block)
        return bytes(output)

    def _read_body_line(self, limit: int) -> bytes:
        output = bytearray()
        while len(output) <= limit:
            block = self._read_body_bytes(1)
            if not block:
                return bytes(output)
            output.extend(block)
            if block == b"\n":
                return bytes(output)
        return bytes(output)

    def _read_body_bytes(self, maximum: int) -> bytes:
        deadline = time.monotonic() + self.gateway._connection_idle_timeout
        while True:
            self._require_current_generation()
            session_id = self._authenticated_session_id
            if session_id is not None and not self.gateway._pairing.is_session_active(
                session_id
            ):
                self.close_connection = True
                raise _ApiProblem(
                    HTTPStatus.UNAUTHORIZED,
                    "session_revoked",
                    "The device session is no longer active.",
                    headers=(("WWW-Authenticate", "Bearer"),),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close_connection = True
                raise _ApiProblem(
                    HTTPStatus.REQUEST_TIMEOUT,
                    "request_timeout",
                    "The request body was idle for too long.",
                )
            try:
                readable, _writable, _errored = select.select(
                    (self.connection,),
                    (),
                    (),
                    min(0.1, remaining),
                )
            except (OSError, ValueError):
                self._require_current_generation()
                return b""
            if not readable:
                continue
            try:
                block = self.connection.recv(maximum)
            except (BlockingIOError, InterruptedError, TimeoutError):
                continue
            except OSError:
                self._require_current_generation()
                return b""
            self._require_current_generation()
            return block

    def _backend_call(
        self,
        callback: Callable[_P, _R],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        try:
            return callback(*args, **kwargs)
        except LanBackendError as exc:
            raise _backend_problem(exc) from exc

    def _require_method(self, *allowed: str) -> None:
        if self.command not in allowed:
            self._method_not_allowed(allowed)

    def _method_not_allowed(self, allowed: tuple[str, ...]) -> NoReturn:
        raise _ApiProblem(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "The HTTP method is not allowed for this route.",
            headers=(("Allow", ", ".join(allowed)),),
        )

    @staticmethod
    def _require_no_query(raw_query: str) -> None:
        if raw_query:
            raise _ApiProblem(400, "invalid_query", "Query parameters are invalid.")

    def _send_empty(self, status: int | HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(
        self,
        status: int | HTTPStatus,
        payload: Mapping[str, object],
        *,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._send_headers(headers)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _send_problem(self, problem: _ApiProblem) -> None:
        code = problem.code if _ERROR_CODE.fullmatch(problem.code) else "request_failed"
        message = _safe_public_message(problem.message)
        self.close_connection = True
        self._send_json(
            problem.status,
            {"error": {"code": code, "message": message, "details": {}}},
            headers=(*problem.headers, ("Connection", "close")),
        )

    def _send_headers(self, headers: tuple[tuple[str, str], ...]) -> None:
        for name, value in headers:
            self.send_header(name, value)


def _pairing_problem(exc: PairingError) -> _ApiProblem:
    if isinstance(exc, InvalidPairingRequest):
        return _ApiProblem(400, exc.code, "Pairing request is invalid.")
    if isinstance(exc, PairingNotFound):
        return _ApiProblem(404, exc.code, "Pairing request was not found.")
    if isinstance(exc, PairingExpired):
        return _ApiProblem(410, exc.code, "Pairing request expired.")
    if isinstance(exc, PairingSecretRejected):
        return _ApiProblem(401, exc.code, "Pairing secret was rejected.")
    if isinstance(exc, PairingCapacityExceeded):
        return _ApiProblem(
            429,
            exc.code,
            "Too many pairing requests are pending.",
            headers=(("Retry-After", "30"),),
        )
    if isinstance(exc, CredentialStoreError):
        return _ApiProblem(503, "pairing_unavailable", "Pairing is unavailable.")
    return _ApiProblem(409, exc.code, "Pairing request could not be completed.")


def _backend_problem(exc: LanBackendError) -> _ApiProblem:
    status = exc.status if 400 <= exc.status <= 599 else 500
    code = exc.code if _ERROR_CODE.fullmatch(exc.code) else "backend_error"
    return _ApiProblem(status, code, _safe_public_message(exc.message))


def _unauthorized_problem() -> _ApiProblem:
    return _ApiProblem(
        HTTPStatus.UNAUTHORIZED,
        "unauthorized",
        "A valid device token is required.",
        headers=(("WWW-Authenticate", "Bearer"),),
    )


def _safe_public_message(value: str) -> str:
    if not isinstance(value, str):
        return "The request could not be completed."
    normalized = " ".join(value.split())[:256]
    if not normalized or _WINDOWS_PATH.search(normalized):
        return "The request could not be completed."
    return normalized


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    allowed: set[str] | None = None,
) -> None:
    accepted = required if allowed is None else allowed
    if not required.issubset(payload) or set(payload) - accepted:
        raise _ApiProblem(400, "invalid_request", "Request fields are invalid.")


def _required_json_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise _ApiProblem(400, "invalid_request", "Request fields are invalid.")
    return value


def _validated_recommendation_action(value: object) -> RecommendationAction:
    if value not in {"open", "like", "export", "dislike"}:
        raise _ApiProblem(400, "invalid_request", "Request fields are invalid.")
    return cast(RecommendationAction, value)


def _validated_recommendation_metadata(
    value: object,
) -> Mapping[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise _ApiProblem(400, "invalid_request", "Request fields are invalid.")
    return cast(dict[str, str], value)


def _validated_host_header(value: str, expected_port: int) -> str:
    if ":" in value:
        host_text, separator, port_text = value.rpartition(":")
        if (
            separator != ":"
            or not port_text.isascii()
            or not port_text.isdigit()
            or len(port_text) > 5
            or int(port_text) != expected_port
        ):
            raise _invalid_host_problem()
    else:
        if expected_port != 80:
            raise _invalid_host_problem()
        host_text = value
    try:
        address = ipaddress.ip_address(host_text)
    except ValueError:
        raise _invalid_host_problem() from None
    if address.version != 4:
        raise _invalid_host_problem()
    return str(address)


def _invalid_host_problem() -> _ApiProblem:
    return _ApiProblem(
        HTTPStatus.MISDIRECTED_REQUEST,
        "invalid_host",
        "The request host is invalid.",
    )


def _parse_query_string(value: str) -> dict[str, list[str]]:
    try:
        return parse_qs(value, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _ApiProblem(
            400, "invalid_query", "Query parameters are invalid."
        ) from None


def _opaque_segment(raw_value: str) -> str:
    try:
        value = unquote(raw_value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _ApiProblem(404, "route_not_found", "The route was not found.") from None
    return _validated_opaque_id(value, code="route_not_found", status=404)


def _validated_opaque_id(
    value: object,
    *,
    code: str,
    status: int = 400,
) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise _ApiProblem(status, code, "Opaque identifier is invalid.")
    return value


def _single_positive_query_integer(
    values: Mapping[str, list[str]],
    name: str,
    *,
    default: int,
) -> int:
    supplied = values.get(name)
    if supplied is None:
        return default
    if (
        len(supplied) != 1
        or not supplied[0].isascii()
        or not supplied[0].isdigit()
        or len(supplied[0]) > 20
    ):
        raise _ApiProblem(400, "invalid_pagination", "Pagination is invalid.")
    result = int(supplied[0])
    if result < 1:
        raise _ApiProblem(400, "invalid_pagination", "Pagination is invalid.")
    return result


def _validated_search_page(
    value: object,
    *,
    search_id: str,
    page: int,
    page_size: int,
) -> SearchPage:
    if not isinstance(value, SearchPage):
        raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
    if (
        value.search_id != search_id
        or value.page != page
        or value.page_size != page_size
        or isinstance(value.total, bool)
        or value.total < 0
        or len(value.items) > page_size
    ):
        raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
    for item in value.items:
        _validated_opaque_id(item.media_id, code="invalid_search_response", status=500)
        if item.score is not None and (
            isinstance(item.score, bool)
            or not isinstance(item.score, (int, float))
            or not math.isfinite(item.score)
        ):
            raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
        if item.name is not None and not isinstance(item.name, str):
            raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
        if item.library_id is not None:
            _validated_opaque_id(
                item.library_id,
                code="invalid_search_response",
                status=500,
            )
        if item.library_name is not None and not isinstance(item.library_name, str):
            raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
        if item.content_type is not None and (
            not isinstance(item.content_type, str)
            or "\r" in item.content_type
            or "\n" in item.content_type
        ):
            raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
        if not isinstance(item.tags, tuple) or any(
            not isinstance(tag, str) for tag in item.tags
        ):
            raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
        for dimension in (item.width, item.height, item.size_bytes):
            if dimension is not None and (isinstance(dimension, bool) or dimension < 0):
                raise _ApiProblem(
                    500,
                    "invalid_search_response",
                    "Search is unavailable.",
                )
    return value


def _validated_libraries(value: object) -> tuple[LibraryInfo, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _ApiProblem(500, "invalid_library_response", "Libraries are unavailable.")
    libraries: list[LibraryInfo] = []
    for library in value:
        if not isinstance(library, LibraryInfo):
            raise _ApiProblem(
                500,
                "invalid_library_response",
                "Libraries are unavailable.",
            )
        _validated_opaque_id(library.id, code="invalid_library_response", status=500)
        if (
            not isinstance(library.name, str)
            or not library.name
            or library.count is not None
            and (isinstance(library.count, bool) or library.count < 0)
        ):
            raise _ApiProblem(
                500,
                "invalid_library_response",
                "Libraries are unavailable.",
            )
        libraries.append(library)
    return tuple(libraries)


def _validated_search_pending(
    value: SearchPending,
    *,
    search_id: str,
) -> SearchPending:
    if (
        value.search_id != search_id
        or value.status not in {"queued", "running"}
        or isinstance(value.retry_after_seconds, bool)
        or not 1 <= value.retry_after_seconds <= 60
    ):
        raise _ApiProblem(500, "invalid_search_response", "Search is unavailable.")
    return value


def _parse_byte_range(value: str, size: int, etag: str) -> _ByteRange:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise _ApiProblem(
            416,
            "range_not_satisfiable",
            "The requested byte range is not available.",
            headers=(
                ("Accept-Ranges", "bytes"),
                ("Content-Range", f"bytes */{size}"),
                ("ETag", etag),
            ),
        )
    start_text, end_text = match.groups()
    if len(start_text) > 20 or len(end_text) > 20:
        raise _range_problem(size, etag)
    if start_text:
        start = int(start_text)
        if start >= size:
            raise _range_problem(size, etag)
        end = size - 1 if not end_text else min(int(end_text), size - 1)
        if end < start:
            raise _range_problem(size, etag)
        return _ByteRange(start, end)
    suffix_length = int(end_text)
    if suffix_length <= 0 or size == 0:
        raise _range_problem(size, etag)
    start = max(0, size - suffix_length)
    return _ByteRange(start, size - 1)


def _range_problem(size: int, etag: str) -> _ApiProblem:
    return _ApiProblem(
        416,
        "range_not_satisfiable",
        "The requested byte range is not available.",
        headers=(
            ("Accept-Ranges", "bytes"),
            ("Content-Range", f"bytes */{size}"),
            ("ETag", etag),
        ),
    )


def _invalid_chunked_body() -> _ApiProblem:
    return _ApiProblem(400, "invalid_chunked_body", "Chunked body is invalid.")


def _if_none_match(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    for candidate in value.split(","):
        normalized = candidate.strip()
        if normalized == "*":
            return True
        if normalized.startswith("W/"):
            normalized = normalized[2:].strip()
        if hmac_etag_equal(normalized, etag):
            return True
    return False


def hmac_etag_equal(left: str, right: str) -> bool:
    """Compare small public ETag strings with consistent semantics."""

    import hmac

    return hmac.compare_digest(left.strip(), right)


def _safe_media_type(source: MediaSource) -> str:
    supplied = source.content_type
    if (
        isinstance(supplied, str)
        and 1 <= len(supplied) <= 128
        and "\r" not in supplied
        and "\n" not in supplied
    ):
        return supplied
    guessed, _encoding = mimetypes.guess_type(source.path.name)
    return guessed or "application/octet-stream"
