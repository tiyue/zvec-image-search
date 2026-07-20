from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zvec_lan import (
    DISCOVERY_REQUEST_PREFIX,
    LanApiServer,
    LanBackendError,
    LanGatewayServer,
    LibraryInfo,
    MediaSource,
    PairingManager,
    QueryImageStore,
    SearchPage,
    SearchPending,
    SearchRequest,
    SearchResultItem,
)
from zvec_lan.http_server import _validated_host_header


def _secret(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


@dataclass(frozen=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


def _read_raw_http_response(connection: socket.socket) -> bytes:
    response = b""
    while b"\r\n\r\n" not in response:
        block = connection.recv(65536)
        if not block:
            return response
        response += block
    head, body = response.split(b"\r\n\r\n", 1)
    content_length = 0
    for line in head.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            content_length = int(value.strip())
            break
    while len(body) < content_length:
        block = connection.recv(65536)
        if not block:
            break
        body += block
    return head + b"\r\n\r\n" + body[:content_length]


class _FakeBackend:
    def __init__(self) -> None:
        self.created: list[tuple[SearchRequest, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.deleted_sessions: list[str] = []
        self.session_cleanup_error: Exception | None = None
        self.create_started: threading.Event | None = None
        self.create_release: threading.Event | None = None
        self.page_result: SearchPage | SearchPending = SearchPending(
            search_id="search-1",
            status="running",
            retry_after_seconds=1,
        )
        self.library_error: LanBackendError | None = None

    def list_libraries(self, *, client_id: str) -> tuple[LibraryInfo, ...]:
        if self.library_error is not None:
            raise self.library_error
        self.last_library_client = client_id
        return (
            LibraryInfo(
                id="library-1",
                name=r"C:\Private\Family Photos",
                count=42,
            ),
        )

    def create_search(self, request: SearchRequest, *, client_id: str) -> str:
        if self.create_started is not None:
            self.create_started.set()
        if self.create_release is not None:
            self.create_release.wait(timeout=5)
        self.created.append((request, client_id))
        return "search-1"

    def get_search_page(
        self,
        search_id: str,
        *,
        page: int,
        page_size: int,
        client_id: str,
    ) -> SearchPage | SearchPending:
        self.page_call = (search_id, page, page_size, client_id)
        return self.page_result

    def delete_search(self, search_id: str, *, client_id: str) -> None:
        self.deleted.append((search_id, client_id))

    def delete_client_session(self, *, client_id: str) -> None:
        self.deleted_sessions.append(client_id)
        if self.session_cleanup_error is not None:
            raise self.session_cleanup_error


class _FakeResolver:
    def __init__(self, sources: dict[str, MediaSource]) -> None:
        self.sources = sources
        self.calls: list[tuple[str, str]] = []

    def resolve_original(
        self,
        media_id: str,
        *,
        client_id: str,
    ) -> MediaSource | None:
        self.calls.append((media_id, client_id))
        return self.sources.get(media_id)


class LanHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media_body = bytes(range(256)) * 4096
        self.media_path = self.root / "private-original.bin"
        self.media_path.write_bytes(self.media_body)
        self.media_sha256 = hashlib.sha256(self.media_body).hexdigest()
        self.backend = _FakeBackend()
        self.resolver = _FakeResolver(
            {
                "media-1": MediaSource(
                    path=self.media_path,
                    sha256=self.media_sha256,
                    content_type="application/octet-stream",
                )
            }
        )
        self.pairing = PairingManager()
        pairing = self.pairing.create_request(
            device_id="android-install-main",
            device_name="Pixel Tablet",
            client_secret=_secret(1),
        )
        self.pairing.approve(pairing.pairing_id)
        token = self.pairing.poll(
            pairing.pairing_id,
            client_secret=_secret(1),
        ).token
        assert token is not None
        self.token = token
        authenticated = self.pairing.authenticate(token)
        assert authenticated is not None
        self.client_id = authenticated.session_id
        self.uploads = QueryImageStore(self.root / "uploads")
        self.server = LanApiServer(
            instance_id="stable-instance-id-1234567890",
            name="Zvec on TEST-PC",
            search_backend=self.backend,
            media_resolver=self.resolver,
            pairing_manager=self.pairing,
            query_images=self.uploads,
            host="127.0.0.1",
            port=0,
            stream_chunk_bytes=32 * 1024,
            allowed_hosts=("127.0.0.1",),
        )
        self.address = self.server.start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self.uploads.close)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        encode_chunked: bool = False,
        port: int | None = None,
    ) -> _Response:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.address.port if port is None else port,
            timeout=5,
        )
        try:
            connection.request(
                method,
                path,
                body=body,
                headers=request_headers,
                encode_chunked=encode_chunked,
            )
            response = connection.getresponse()
            response_body = response.read()
            return _Response(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response_body,
            )
        finally:
            connection.close()

    def start_extra_server(
        self,
        *,
        connection_idle_timeout: float = 60.0,
        max_connections: int = 64,
        query_images: QueryImageStore | None = None,
    ) -> tuple[LanApiServer, Any, QueryImageStore]:
        uploads = query_images or QueryImageStore(
            self.root / f"extra-uploads-{time.time_ns()}"
        )
        server = LanApiServer(
            instance_id="stable-instance-id-1234567890",
            name="Zvec on TEST-PC",
            search_backend=self.backend,
            media_resolver=self.resolver,
            pairing_manager=self.pairing,
            query_images=uploads,
            host="127.0.0.1",
            port=0,
            allowed_hosts=("127.0.0.1",),
            connection_idle_timeout=connection_idle_timeout,
            max_connections=max_connections,
        )
        address = server.start()
        self.addCleanup(uploads.close)
        self.addCleanup(server.stop)
        return server, address, uploads

    def json_request(
        self,
        method: str,
        path: str,
        payload: object,
        *,
        authenticated: bool = True,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        return self.request(
            method,
            path,
            body=body,
            headers=request_headers,
            authenticated=authenticated,
        )

    def test_http_pairing_flow_returns_token_once(self) -> None:
        secret = _secret(2)
        started = self.json_request(
            "POST",
            "/api/v1/pair-requests",
            {
                "device_id": "android-install-second",
                "device_name": "Android Phone",
                "client_secret": secret,
            },
            authenticated=False,
        )

        self.assertEqual(started.status, 201)
        started_json = started.json()
        self.assertEqual(started_json["status"], "pending")
        self.assertEqual(started_json["expires_in_seconds"], 300)
        self.assertRegex(started_json["comparison_code"], r"^\d{6}$")
        pairing_id = started_json["pairing_id"]
        self.pairing.approve(pairing_id)

        approved = self.json_request(
            "POST",
            f"/api/v1/pair-requests/{pairing_id}/poll",
            {"client_secret": secret},
            authenticated=False,
        )
        repeated = self.json_request(
            "POST",
            f"/api/v1/pair-requests/{pairing_id}/poll",
            {"client_secret": secret},
            authenticated=False,
        )

        self.assertEqual(approved.status, 200)
        self.assertEqual(approved.json()["status"], "approved")
        self.assertIn("token", approved.json())
        self.assertEqual(repeated.json(), {"status": "approved"})

    def test_host_and_browser_headers_are_rejected_before_pairing(self) -> None:
        payload = {
            "device_id": "android-install-second",
            "device_name": "Android Phone",
            "client_secret": _secret(2),
        }
        evil_host = self.json_request(
            "POST",
            "/api/v1/pair-requests",
            payload,
            authenticated=False,
            headers={"Host": f"evil.example:{self.address.port}"},
        )
        origin = self.json_request(
            "POST",
            "/api/v1/pair-requests",
            payload,
            authenticated=False,
            headers={"Origin": "https://evil.example"},
        )
        sec_fetch = self.json_request(
            "POST",
            "/api/v1/pair-requests",
            payload,
            authenticated=False,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        unknown_browser_method = self.request(
            "PROPFIND",
            "/api/v1/pair-requests",
            headers={"Origin": "https://evil.example"},
            authenticated=False,
        )

        self.assertEqual(evil_host.status, 421)
        self.assertEqual(evil_host.json()["error"]["code"], "invalid_host")
        self.assertEqual(origin.status, 403)
        self.assertEqual(sec_fetch.status, 403)
        self.assertEqual(unknown_browser_method.status, 403)
        self.assertEqual(self.pairing.pending_requests(), ())

    def test_default_http_port_may_omit_host_port_only_for_port_80(self) -> None:
        self.assertEqual(_validated_host_header("192.168.1.20", 80), "192.168.1.20")
        with self.assertRaises(Exception) as caught:
            _validated_host_header("192.168.1.20", 38522)
        self.assertEqual(getattr(caught.exception, "status", None), 421)

    def test_stop_invalidates_keep_alive_connection_across_restart(self) -> None:
        old_port = self.address.port
        connection = socket.create_connection(("127.0.0.1", old_port), 2)
        connection.settimeout(2)
        self.addCleanup(connection.close)
        request = (
            "GET /api/v1/status HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{old_port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode()
        connection.sendall(request)
        first = _read_raw_http_response(connection)
        self.assertIn(b"HTTP/1.1 200", first)

        self.server.stop()
        self.address = self.server.start()

        received = b""
        try:
            connection.sendall(request)
            while True:
                block = connection.recv(65536)
                if not block:
                    break
                received += block
        except (OSError, TimeoutError):
            pass
        self.assertNotIn(b"HTTP/1.1 200", received)
        self.assertEqual(self.request("GET", "/api/v1/status").status, 200)

    def test_stop_interrupts_slow_upload_without_waiting_for_client(self) -> None:
        old_port = self.address.port
        connection = socket.create_connection(("127.0.0.1", old_port), 2)
        connection.settimeout(1)
        self.addCleanup(connection.close)
        headers = (
            "POST /api/v1/query-images?name=slow.raw HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{old_port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: 1048576\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode()
        connection.sendall(headers + b"partial-upload")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not list(
            self.uploads.directory.glob("*.part")
        ):
            time.sleep(0.01)
        self.assertTrue(list(self.uploads.directory.glob("*.part")))

        started = time.monotonic()
        self.server.stop()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and list(
            self.uploads.directory.glob("*.part")
        ):
            time.sleep(0.01)
        self.assertEqual(list(self.uploads.directory.glob("*.part")), [])

        self.address = self.server.start()
        try:
            connection.sendall(
                (
                    "GET /api/v1/status HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{old_port}\r\n"
                    f"Authorization: Bearer {self.token}\r\n\r\n"
                ).encode()
            )
            old_response = connection.recv(65536)
        except (OSError, TimeoutError):
            old_response = b""
        self.assertNotIn(b"HTTP/1.1 200", old_response)
        self.assertEqual(self.request("GET", "/api/v1/status").status, 200)

    def test_body_idle_timeout_cleans_partial_upload(self) -> None:
        _server, address, uploads = self.start_extra_server(connection_idle_timeout=0.2)
        connection = socket.create_connection(("127.0.0.1", address.port), 2)
        connection.settimeout(2)
        self.addCleanup(connection.close)
        connection.sendall(
            (
                "POST /api/v1/query-images?name=idle.raw HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{address.port}\r\n"
                f"Authorization: Bearer {self.token}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Content-Length: 100\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + b"partial"
        )

        response = b""
        while True:
            block = connection.recv(65536)
            if not block:
                break
            response += block

        self.assertIn(b"HTTP/1.1 408", response)
        self.assertIn(b"request_timeout", response)
        self.assertEqual(list(uploads.directory.glob("*.part")), [])

    def test_header_idle_timeout_closes_slowloris_connection(self) -> None:
        _server, address, _uploads = self.start_extra_server(
            connection_idle_timeout=0.2
        )
        connection = socket.create_connection(("127.0.0.1", address.port), 2)
        connection.settimeout(1)
        self.addCleanup(connection.close)
        connection.sendall(b"GET /api/v1/status HTTP/1.1\r\nHo")
        time.sleep(0.35)

        try:
            connection.sendall(
                (
                    f"st: 127.0.0.1:{address.port}\r\n"
                    f"Authorization: Bearer {self.token}\r\n\r\n"
                ).encode()
            )
            response = connection.recv(65536)
        except (OSError, TimeoutError):
            response = b""

        self.assertNotIn(b"HTTP/1.1 200", response)

    def test_continuously_progressing_upload_can_exceed_idle_interval(self) -> None:
        _server, address, uploads = self.start_extra_server(connection_idle_timeout=0.3)
        connection = socket.create_connection(("127.0.0.1", address.port), 2)
        connection.settimeout(2)
        self.addCleanup(connection.close)
        content = b"abcdef"
        connection.sendall(
            (
                "POST /api/v1/query-images?name=progress.raw HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{address.port}\r\n"
                f"Authorization: Bearer {self.token}\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {len(content)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        started = time.monotonic()
        for byte in content:
            connection.sendall(bytes((byte,)))
            time.sleep(0.12)
        response = _read_raw_http_response(connection)

        self.assertGreater(time.monotonic() - started, 0.3)
        self.assertIn(b"HTTP/1.1 201", response)
        self.assertEqual(len(list(uploads.directory.glob("*.bin"))), 1)

    def test_active_connection_cap_rejects_excess_without_spawning_handlers(
        self,
    ) -> None:
        _server, address, _uploads = self.start_extra_server(
            connection_idle_timeout=2,
            max_connections=2,
        )

        def open_keep_alive() -> socket.socket:
            connection = socket.create_connection(("127.0.0.1", address.port), 2)
            connection.settimeout(1)
            connection.sendall(
                (
                    "GET /api/v1/status HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{address.port}\r\n"
                    f"Authorization: Bearer {self.token}\r\n"
                    "Connection: keep-alive\r\n\r\n"
                ).encode()
            )
            self.assertIn(b"HTTP/1.1 200", _read_raw_http_response(connection))
            return connection

        first = open_keep_alive()
        second = open_keep_alive()
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        excess = socket.create_connection(("127.0.0.1", address.port), 2)
        excess.settimeout(1)
        self.addCleanup(excess.close)
        try:
            excess.sendall(
                (
                    "GET /api/v1/status HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{address.port}\r\n"
                    f"Authorization: Bearer {self.token}\r\n\r\n"
                ).encode()
            )
            rejected = excess.recv(65536)
        except (OSError, TimeoutError):
            rejected = b""
        self.assertNotIn(b"HTTP/1.1 200", rejected)

        first.close()
        deadline = time.monotonic() + 2
        replacement: socket.socket | None = None
        while time.monotonic() < deadline and replacement is None:
            candidate = socket.create_connection(("127.0.0.1", address.port), 2)
            candidate.settimeout(0.5)
            try:
                candidate.sendall(
                    (
                        "GET /api/v1/status HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{address.port}\r\n"
                        f"Authorization: Bearer {self.token}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                )
                if b"HTTP/1.1 200" in _read_raw_http_response(candidate):
                    replacement = candidate
                    break
            except (OSError, TimeoutError):
                pass
            candidate.close()
            time.sleep(0.02)
        self.assertIsNotNone(replacement)
        if replacement is not None:
            replacement.close()

    def test_all_protected_routes_require_bearer_including_head(self) -> None:
        cases = (
            ("GET", "/api/v1/status", None),
            ("GET", "/api/v1/libraries", None),
            ("POST", "/api/v1/query-images?name=x", b"image"),
            ("DELETE", "/api/v1/query-images/query-id-123456", None),
            ("POST", "/api/v1/searches", b"{}"),
            ("GET", "/api/v1/searches/search-1", None),
            ("DELETE", "/api/v1/searches/search-1", None),
            ("GET", "/api/v1/media/media-1/original", None),
            ("HEAD", "/api/v1/media/media-1/original", None),
            ("DELETE", "/api/v1/session", None),
        )

        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                response = self.request(
                    method,
                    path,
                    body=body,
                    authenticated=False,
                )
                self.assertEqual(response.status, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                if method == "HEAD":
                    self.assertEqual(response.body, b"")
                else:
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "unauthorized",
                    )

    def test_route_allow_list_does_not_expose_desktop_management(self) -> None:
        for method, path in (
            ("GET", "/api/v1/settings"),
            ("GET", "/api/v1/credentials"),
            ("POST", "/api/v1/indexing"),
            ("POST", "/api/v1/migrations"),
            ("DELETE", "/api/v1/library-files/media-1"),
        ):
            with self.subTest(method=method, path=path):
                response = self.request(method, path)
                self.assertEqual(response.status, 404)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "route_not_found",
                )

    def test_status_and_libraries_are_path_free(self) -> None:
        status = self.request("GET", "/api/v1/status")
        libraries = self.request("GET", "/api/v1/libraries")

        self.assertEqual(
            status.json(),
            {
                "protocol": 1,
                "instance_id": "stable-instance-id-1234567890",
                "name": "Zvec on TEST-PC",
            },
        )
        self.assertEqual(
            libraries.json(),
            {"libraries": [{"id": "library-1", "name": "Family Photos", "count": 42}]},
        )
        self.assertNotIn(b"C:\\Private", libraries.body)

    def test_backend_error_message_cannot_leak_an_absolute_path(self) -> None:
        self.backend.library_error = LanBackendError(
            "libraries_failed",
            r"Could not read C:\Users\Alice\private-library",
            status=503,
        )

        response = self.request("GET", "/api/v1/libraries")

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json()["error"]["code"], "libraries_failed")
        self.assertNotIn(b"Alice", response.body)
        self.assertNotIn(b"private-library", response.body)

    def test_content_length_and_chunked_query_uploads_stream_to_disk(self) -> None:
        content = b"query-data-" * 300_000
        fixed = self.request(
            "POST",
            "/api/v1/query-images?name=fixed.jpg",
            body=content,
            headers={"Content-Type": "image/jpeg"},
        )
        chunked = self.request(
            "POST",
            "/api/v1/query-images?name=chunked.png",
            body=[b"chunk-1", b"chunk-2", b"chunk-3"],
            headers={"Content-Type": "image/png"},
            encode_chunked=True,
        )

        self.assertEqual(fixed.status, 201)
        fixed_image = self.uploads.get(
            fixed.json()["query_image_id"],
            owner_id=self.client_id,
        )
        self.assertEqual(fixed_image.path.stat().st_size, len(content))
        self.assertEqual(chunked.status, 201)
        chunked_image = self.uploads.get(
            chunked.json()["query_image_id"],
            owner_id=self.client_id,
        )
        self.assertEqual(chunked_image.path.read_bytes(), b"chunk-1chunk-2chunk-3")

    def test_impossible_declared_size_returns_507_without_a_product_413(
        self,
    ) -> None:
        connection = socket.create_connection(("127.0.0.1", self.address.port), 2)
        self.addCleanup(connection.close)
        request = (
            "POST /api/v1/query-images?name=huge.raw HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.address.port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: 99999999999999999999\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + b"partial"
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            block = connection.recv(65536)
            if not block:
                break
            response += block

        self.assertIn(b" 507 ", response)
        self.assertNotIn(b" 413 ", response)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and list(
            self.uploads.directory.glob("*.part")
        ):
            time.sleep(0.01)
        self.assertEqual(list(self.uploads.directory.glob("*.part")), [])

    def test_storage_507_cleans_partial_and_does_not_break_later_uploads(
        self,
    ) -> None:
        free_bytes = [15]
        uploads = QueryImageStore(
            self.root / "limited-fixed-uploads",
            min_free_bytes=10,
            free_space_provider=lambda _path: free_bytes[0],
        )
        _server, address, _uploads = self.start_extra_server(query_images=uploads)

        insufficient = self.request(
            "POST",
            "/api/v1/query-images?name=too-large.jpg",
            body=b"123456",
            headers={"Content-Type": "image/jpeg"},
            port=address.port,
        )
        free_bytes[0] = 20
        recovered = self.request(
            "POST",
            "/api/v1/query-images?name=recovered.jpg",
            body=b"123456",
            headers={"Content-Type": "image/jpeg"},
            port=address.port,
        )

        self.assertEqual(insufficient.status, 507)
        self.assertEqual(
            insufficient.json()["error"]["code"],
            "upload_storage_error",
        )
        self.assertEqual(recovered.status, 201)
        self.assertEqual(list(uploads.directory.glob("*.part")), [])

    def test_chunked_upload_returns_507_when_rolling_space_runs_out(self) -> None:
        free_values = iter((20, 11))
        uploads = QueryImageStore(
            self.root / "limited-chunked-uploads",
            write_chunk_bytes=2,
            min_free_bytes=10,
            space_check_interval_bytes=4,
            free_space_provider=lambda _path: next(free_values),
        )
        _server, address, _uploads = self.start_extra_server(query_images=uploads)

        response = self.request(
            "POST",
            "/api/v1/query-images?name=chunked.jpg",
            body=[b"ab", b"cd", b"ef"],
            headers={"Content-Type": "image/jpeg"},
            encode_chunked=True,
            port=address.port,
        )

        self.assertEqual(response.status, 507)
        self.assertEqual(list(uploads.directory.glob("zvec-query-*")), [])

    def test_malicious_numeric_lengths_and_deep_json_return_400(self) -> None:
        too_long_length = self.request(
            "POST",
            "/api/v1/query-images?name=x",
            body=b"",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "1" * 21,
            },
        )
        deep_json = b"[" * 1500 + b"0" + b"]" * 1500
        recursive = self.request(
            "POST",
            "/api/v1/pair-requests",
            body=deep_json,
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        malformed_query = self.request(
            "GET",
            "/api/v1/searches/search-1?page",
        )

        self.assertEqual(too_long_length.status, 400)
        self.assertEqual(recursive.status, 400)
        self.assertEqual(recursive.json()["error"]["code"], "invalid_json")
        self.assertEqual(malformed_query.status, 400)

    def test_oversized_chunk_header_returns_400_without_a_traceback(self) -> None:
        connection = socket.create_connection(("127.0.0.1", self.address.port), 2)
        self.addCleanup(connection.close)
        request = (
            "POST /api/v1/query-images?name=x HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.address.port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n"
            "10000000000000000\r\n"
        ).encode()
        connection.sendall(request)
        response = b""
        while True:
            block = connection.recv(65536)
            if not block:
                break
            response += block

        self.assertIn(b" 400 ", response)
        self.assertIn(b"invalid_chunked_body", response)
        self.assertNotIn(b"Traceback", response)

    def test_search_upload_async_poll_page_and_cleanup_contract(self) -> None:
        uploaded = self.request(
            "POST",
            "/api/v1/query-images?name=query.jpg",
            body=b"original-query-image",
            headers={"Content-Type": "image/jpeg"},
        )
        query_image_id = uploaded.json()["query_image_id"]
        created = self.json_request(
            "POST",
            "/api/v1/searches",
            {
                "mode": "image",
                "query_image_id": query_image_id,
                "library_ids": ["library-1"],
                "top_k": 10**18,
            },
        )

        self.assertEqual(created.status, 202)
        self.assertEqual(created.json(), {"search_id": "search-1"})
        request, client_id = self.backend.created[-1]
        self.assertEqual(request.top_k, 10**18)
        self.assertEqual(request.query_image_path.read_bytes(), b"original-query-image")
        self.assertEqual(client_id, self.client_id)

        pending = self.request(
            "GET",
            "/api/v1/searches/search-1?page=1&page_size=30",
        )
        self.assertEqual(pending.status, 202)
        self.assertEqual(pending.headers["retry-after"], "1")
        self.assertEqual(pending.json()["status"], "running")

        self.backend.page_result = SearchPage(
            search_id="search-1",
            page=1,
            page_size=30,
            total=1,
            items=(
                SearchResultItem(
                    media_id="media-1",
                    score=0.99,
                    name=r"D:\Private\photo.jpg",
                    width=100,
                    height=200,
                    tags=("cat",),
                    library_id="library-1",
                    library_name="Family Photos",
                    content_type="image/jpeg",
                    size_bytes=len(self.media_body),
                ),
            ),
        )
        complete = self.request(
            "GET",
            "/api/v1/searches/search-1?page=1&page_size=30",
        )
        self.assertEqual(complete.status, 200)
        self.assertEqual(complete.json()["items"][0]["name"], "photo.jpg")
        self.assertNotIn(b"D:\\Private", complete.body)

        deleted_search = self.request("DELETE", "/api/v1/searches/search-1")
        deleted_image = self.request(
            "DELETE",
            f"/api/v1/query-images/{query_image_id}",
        )
        self.assertEqual(deleted_search.status, 204)
        self.assertEqual(deleted_image.status, 204)
        self.assertEqual(
            self.backend.deleted,
            [("search-1", self.client_id)],
        )

    def test_full_head_range_cache_and_unsatisfied_media_responses(self) -> None:
        etag = f'"{self.media_sha256}"'
        full = self.request("GET", "/api/v1/media/media-1/original")
        head = self.request("HEAD", "/api/v1/media/media-1/original")
        partial = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=10-19"},
        )
        suffix = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=-12"},
        )
        not_modified = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"If-None-Match": etag},
        )
        unsatisfied = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": f"bytes={len(self.media_body)}-"},
        )
        multi_range = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=0-1,4-5"},
        )

        self.assertEqual(full.status, 200)
        self.assertEqual(full.body, self.media_body)
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["etag"], etag)
        self.assertEqual(int(full.headers["content-length"]), len(self.media_body))
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(int(head.headers["content-length"]), len(self.media_body))
        self.assertEqual(partial.status, 206)
        self.assertEqual(partial.body, self.media_body[10:20])
        self.assertEqual(
            partial.headers["content-range"],
            f"bytes 10-19/{len(self.media_body)}",
        )
        self.assertEqual(suffix.body, self.media_body[-12:])
        self.assertEqual(not_modified.status, 304)
        self.assertEqual(not_modified.body, b"")
        for response in (unsatisfied, multi_range):
            self.assertEqual(response.status, 416)
            self.assertEqual(
                response.headers["content-range"],
                f"bytes */{len(self.media_body)}",
            )
            self.assertEqual(response.headers["etag"], etag)

    def test_if_range_and_head_range_follow_single_range_semantics(self) -> None:
        etag = f'"{self.media_sha256}"'
        stale = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=0-3", "If-Range": '"old"'},
        )
        matching = self.request(
            "GET",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=0-3", "If-Range": etag},
        )
        head = self.request(
            "HEAD",
            "/api/v1/media/media-1/original",
            headers={"Range": "bytes=5-9"},
        )

        self.assertEqual(stale.status, 200)
        self.assertEqual(stale.body, self.media_body)
        self.assertEqual(matching.status, 206)
        self.assertEqual(matching.body, self.media_body[:4])
        self.assertEqual(head.status, 206)
        self.assertEqual(head.body, b"")
        self.assertEqual(head.headers["content-length"], "5")

    def test_multiple_originals_transfer_concurrently_without_global_serialization(
        self,
    ) -> None:
        def download(_index: int) -> bytes:
            return self.request("GET", "/api/v1/media/media-1/original").body

        with ThreadPoolExecutor(max_workers=12) as executor:
            bodies = list(executor.map(download, range(24)))

        self.assertEqual(bodies, [self.media_body] * 24)
        self.assertGreaterEqual(len(self.resolver.calls), 24)

    def test_missing_media_error_never_contains_the_internal_path(self) -> None:
        missing_path = self.root / "Users" / "Alice" / "secret.jpg"
        self.resolver.sources["media-missing"] = MediaSource(
            path=missing_path,
            sha256="a" * 64,
            content_type="image/jpeg",
        )

        response = self.request(
            "GET",
            "/api/v1/media/media-missing/original",
        )

        self.assertEqual(response.status, 404)
        self.assertNotIn(str(missing_path).encode(), response.body)
        self.assertNotIn(b"Alice", response.body)

    def test_delete_session_revokes_current_bearer(self) -> None:
        uploaded = self.request(
            "POST",
            "/api/v1/query-images?name=session.jpg",
            body=b"session-query",
            headers={"Content-Type": "image/jpeg"},
        )
        query_image_id = uploaded.json()["query_image_id"]
        image_path = self.uploads.get(
            query_image_id,
            owner_id=self.client_id,
        ).path
        deleted = self.request("DELETE", "/api/v1/session")
        after = self.request("GET", "/api/v1/status")

        self.assertEqual(deleted.status, 204)
        self.assertEqual(after.status, 401)
        self.assertEqual(self.backend.deleted_sessions, [self.client_id])
        self.assertFalse(image_path.exists())

    def test_delete_session_cancels_concurrent_upload_before_publish(self) -> None:
        connection = socket.create_connection(("127.0.0.1", self.address.port), 2)
        connection.settimeout(1)
        self.addCleanup(connection.close)
        connection.sendall(
            (
                "POST /api/v1/query-images?name=racing.raw HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.address.port}\r\n"
                f"Authorization: Bearer {self.token}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Content-Length: 1000000\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + b"partial"
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not list(
            self.uploads.directory.glob("*.part")
        ):
            time.sleep(0.01)
        self.assertTrue(list(self.uploads.directory.glob("*.part")))

        deleted = self.request("DELETE", "/api/v1/session")

        self.assertEqual(deleted.status, 204)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and list(
            self.uploads.directory.glob("*.part")
        ):
            time.sleep(0.01)
        self.assertEqual(list(self.uploads.directory.glob("*.part")), [])
        self.assertEqual(list(self.uploads.directory.glob("*.bin")), [])
        self.assertIsNone(self.pairing.authenticate(self.token))

    def test_concurrent_search_create_rolls_back_after_local_revoke(self) -> None:
        self.backend.create_started = threading.Event()
        self.backend.create_release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.json_request,
                "POST",
                "/api/v1/searches",
                {"mode": "text", "text": "cat", "top_k": 100},
            )
            self.assertTrue(self.backend.create_started.wait(timeout=2))
            self.assertTrue(self.pairing.revoke_client("android-install-main"))
            self.backend.create_release.set()
            response = future.result(timeout=5)

        self.assertEqual(response.status, 401)
        self.assertEqual(response.json()["error"]["code"], "session_revoked")
        self.assertEqual(
            self.backend.deleted,
            [("search-1", self.client_id)],
        )
        self.assertIn(self.client_id, self.backend.deleted_sessions)

    def test_repairing_same_device_cleans_old_session_before_new_token(self) -> None:
        uploaded = self.request(
            "POST",
            "/api/v1/query-images?name=old-session.jpg",
            body=b"old-query",
            headers={"Content-Type": "image/jpeg"},
        )
        old_image = self.uploads.get(
            uploaded.json()["query_image_id"],
            owner_id=self.client_id,
        )
        new_secret = _secret(44)
        started = self.json_request(
            "POST",
            "/api/v1/pair-requests",
            {
                "device_id": "android-install-main",
                "device_name": "Pixel Tablet",
                "client_secret": new_secret,
            },
            authenticated=False,
        )
        pairing_id = started.json()["pairing_id"]
        self.pairing.approve(pairing_id)

        approved = self.json_request(
            "POST",
            f"/api/v1/pair-requests/{pairing_id}/poll",
            {"client_secret": new_secret},
            authenticated=False,
        )

        self.assertEqual(approved.status, 200)
        new_token = approved.json()["token"]
        self.assertIn(self.client_id, self.backend.deleted_sessions)
        self.assertFalse(old_image.path.exists())
        self.assertEqual(self.request("GET", "/api/v1/status").status, 401)
        self.assertEqual(
            self.request(
                "GET",
                "/api/v1/status",
                headers={"Authorization": f"Bearer {new_token}"},
                authenticated=False,
            ).status,
            200,
        )


class LanGatewayLifecycleTests(unittest.TestCase):
    @staticmethod
    def pair_device(
        gateway: LanGatewayServer,
        *,
        device_id: str,
        secret_byte: int,
    ) -> tuple[str, str]:
        client_secret = _secret(secret_byte)
        pairing = gateway.pairing_manager.create_request(
            device_id=device_id,
            device_name=device_id,
            client_secret=client_secret,
        )
        gateway.approve_pairing(pairing.pairing_id)
        token = gateway.pairing_manager.poll(
            pairing.pairing_id,
            client_secret=client_secret,
        ).token
        assert token is not None
        client = gateway.pairing_manager.authenticate(token)
        assert client is not None
        return token, client.session_id

    def gateway(
        self,
        directory: str,
        backend: _FakeBackend,
    ) -> LanGatewayServer:
        gateway = LanGatewayServer(
            instance_id="stable-instance-id-1234567890",
            name="Zvec on TEST-PC",
            advertised_host="127.0.0.1",
            search_backend=backend,
            media_resolver=_FakeResolver({}),
            upload_directory=Path(directory) / "uploads",
            bind_host="127.0.0.1",
            api_port=0,
            discovery_port=0,
        )
        self.addCleanup(gateway.stop)
        return gateway

    def test_composite_service_starts_discovery_and_http_and_stops_both(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = _FakeBackend()
            gateway = self.gateway(directory, backend)

            address = gateway.start()
            self.assertEqual(gateway.start(), address)
            self.assertTrue(gateway.is_running)
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(1)
            try:
                client.sendto(
                    f"{DISCOVERY_REQUEST_PREFIX}lifecycle-nonce".encode(),
                    ("127.0.0.1", address.discovery_port),
                )
                response, _peer = client.recvfrom(4096)
            finally:
                client.close()
            self.assertEqual(json.loads(response)["port"], address.api_port)
            self.assertEqual(address.origin, f"http://127.0.0.1:{address.api_port}")

            gateway.stop()
            gateway.stop()
            self.assertFalse(gateway.is_running)

    def test_stop_cleans_sessions_and_query_images_without_revoking_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = _FakeBackend()
            gateway = self.gateway(directory, backend)
            gateway.start()
            token, session_id = self.pair_device(
                gateway,
                device_id="android-stop-device",
                secret_byte=31,
            )
            image = gateway.api_server.query_image_store.store(
                [b"query"],
                owner_id=session_id,
                display_name="query.jpg",
                content_type="image/jpeg",
            )

            gateway.stop()

            self.assertIn(session_id, backend.deleted_sessions)
            self.assertFalse(image.path.exists())
            self.assertIsNotNone(gateway.pairing_manager.authenticate(token))
            self.assertFalse(gateway.is_running)

            gateway.start()
            self.assertTrue(gateway.is_running)

    def test_running_revoke_client_cleans_resources_then_revokes_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = _FakeBackend()
            gateway = self.gateway(directory, backend)
            gateway.start()
            token, session_id = self.pair_device(
                gateway,
                device_id="android-revoke-device",
                secret_byte=32,
            )
            image = gateway.api_server.query_image_store.store(
                [b"query"],
                owner_id=session_id,
                display_name="query.jpg",
                content_type="image/jpeg",
            )

            self.assertTrue(gateway.revoke_client("android-revoke-device"))

            self.assertEqual(backend.deleted_sessions, [session_id])
            self.assertFalse(image.path.exists())
            self.assertIsNone(gateway.pairing_manager.authenticate(token))

    def test_revoke_all_cleans_each_session_and_every_query_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = _FakeBackend()
            gateway = self.gateway(directory, backend)
            gateway.start()
            first_token, first_session = self.pair_device(
                gateway,
                device_id="android-one",
                secret_byte=33,
            )
            second_token, second_session = self.pair_device(
                gateway,
                device_id="android-two",
                secret_byte=34,
            )
            store = gateway.api_server.query_image_store
            first_image = store.store(
                [b"one"],
                owner_id=first_session,
                display_name="one.jpg",
                content_type="image/jpeg",
            )
            second_image = store.store(
                [b"two"],
                owner_id=second_session,
                display_name="two.jpg",
                content_type="image/jpeg",
            )

            self.assertTrue(gateway.revoke_device())

            self.assertEqual(
                set(backend.deleted_sessions),
                {first_session, second_session},
            )
            self.assertFalse(first_image.path.exists())
            self.assertFalse(second_image.path.exists())
            self.assertIsNone(gateway.pairing_manager.authenticate(first_token))
            self.assertIsNone(gateway.pairing_manager.authenticate(second_token))

    def test_cleanup_failure_still_revokes_old_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = _FakeBackend()
            gateway = self.gateway(directory, backend)
            gateway.start()
            token, session_id = self.pair_device(
                gateway,
                device_id="android-failing-device",
                secret_byte=35,
            )
            image = gateway.api_server.query_image_store.store(
                [b"query"],
                owner_id=session_id,
                display_name="query.jpg",
                content_type="image/jpeg",
            )
            backend.session_cleanup_error = RuntimeError("backend cleanup failed")

            with self.assertRaisesRegex(Exception, "fully revoke"):
                gateway.revoke_client("android-failing-device")

            self.assertFalse(image.path.exists())
            self.assertIsNone(gateway.pairing_manager.authenticate(token))
            backend.session_cleanup_error = None


if __name__ == "__main__":
    unittest.main()
