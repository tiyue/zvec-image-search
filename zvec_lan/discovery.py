"""UDP discovery responder for the Zvec Android LAN protocol."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from dataclasses import dataclass

DISCOVERY_PORT = 38521
DISCOVERY_REQUEST_PREFIX = "ZVEC_LAN_DISCOVER/1 "
_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9._~-]{16,160}$")
_MAX_DATAGRAM_BYTES = 4096
_MAX_NONCE_BYTES = 512


class DiscoveryError(RuntimeError):
    """The discovery responder could not start or stop cleanly."""


@dataclass(frozen=True, slots=True)
class DiscoveryAddress:
    host: str
    port: int


class DiscoveryServer:
    """Reply to nonce-bearing UDP probes without granting any authority."""

    def __init__(
        self,
        *,
        instance_id: str,
        name: str,
        advertised_host: str,
        api_port: int,
        bind_host: str = "0.0.0.0",
        port: int = DISCOVERY_PORT,
    ) -> None:
        if _SAFE_INSTANCE_ID.fullmatch(instance_id) is None:
            raise ValueError("instance_id must be a stable URL-safe random id")
        normalized_name = name.strip()
        if not 1 <= len(normalized_name) <= 128 or any(
            ord(character) < 32 for character in normalized_name
        ):
            raise ValueError("name must be 1-128 printable characters")
        try:
            advertised_ip = ipaddress.ip_address(advertised_host)
            bind_ip = ipaddress.ip_address(bind_host)
        except ValueError as exc:
            raise ValueError("discovery hosts must be IPv4 addresses") from exc
        if advertised_ip.version != 4 or bind_ip.version != 4:
            raise ValueError("discovery hosts must be IPv4 addresses")
        if isinstance(api_port, bool) or not 1 <= api_port <= 65535:
            raise ValueError("api_port must be between 1 and 65535")
        if isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self._instance_id = instance_id
        self._name = normalized_name
        self._advertised_host = str(advertised_ip)
        self._api_port = api_port
        self._bind_host = str(bind_ip)
        self._configured_port = port
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._address: DiscoveryAddress | None = None
        self._lock = threading.RLock()

    @property
    def address(self) -> DiscoveryAddress:
        address = self._address
        if address is None:
            raise DiscoveryError("discovery server is not running")
        return address

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return self._socket is not None and thread is not None and thread.is_alive()

    def start(self) -> DiscoveryAddress:
        with self._lock:
            if self.is_running:
                return self.address
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                udp_socket.bind((self._bind_host, self._configured_port))
                udp_socket.settimeout(0.2)
            except OSError as exc:
                udp_socket.close()
                raise DiscoveryError("could not start LAN discovery") from exc
            host, port = udp_socket.getsockname()[:2]
            self._stop_event.clear()
            self._socket = udp_socket
            self._address = DiscoveryAddress(str(host), int(port))
            thread = threading.Thread(
                target=self._serve,
                args=(udp_socket,),
                name="zvec-lan-discovery",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._address

    def stop(self) -> None:
        with self._lock:
            udp_socket = self._socket
            thread = self._thread
            self._stop_event.set()
            if udp_socket is not None:
                udp_socket.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._socket is udp_socket:
                self._socket = None
                self._thread = None
                self._address = None

    def _serve(self, udp_socket: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                payload, peer = udp_socket.recvfrom(_MAX_DATAGRAM_BYTES)
            except TimeoutError:
                continue
            except OSError:
                break
            nonce = parse_discovery_request(payload)
            if nonce is None:
                continue
            response = json.dumps(
                {
                    "protocol": 1,
                    "nonce": nonce,
                    "instance_id": self._instance_id,
                    "name": self._name,
                    "host": self._advertised_host,
                    "port": self._api_port,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                udp_socket.sendto(response, peer)
            except OSError:
                if self._stop_event.is_set():
                    break


def parse_discovery_request(payload: bytes) -> str | None:
    """Return a valid nonce, or ``None`` for a malformed discovery datagram."""

    if not payload or len(payload) >= _MAX_DATAGRAM_BYTES:
        return None
    try:
        request = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if not request.startswith(DISCOVERY_REQUEST_PREFIX):
        return None
    nonce = request[len(DISCOVERY_REQUEST_PREFIX) :]
    encoded_nonce = nonce.encode("utf-8")
    if (
        not nonce
        or len(encoded_nonce) > _MAX_NONCE_BYTES
        or any(not character.isprintable() for character in nonce)
    ):
        return None
    return nonce
