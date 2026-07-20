"""Persistent, non-secret configuration for the Android LAN gateway."""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import socket
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

LAN_SETTINGS_SCHEMA_VERSION: Final = 1
DEFAULT_LAN_PORT: Final = 38_522
DEFAULT_DISCOVERY_PORT: Final = 38_521
_MAX_SETTINGS_BYTES: Final = 64 * 1024
_RFC1918_NETWORKS: Final = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class LanSettingsError(RuntimeError):
    """The persisted LAN configuration is invalid or cannot be written."""


@dataclass(frozen=True, slots=True)
class LanHostOption:
    address: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"address": self.address, "label": self.label}


@dataclass(frozen=True, slots=True)
class LanAccessSettings:
    enabled: bool = False
    bind_host: str = ""
    port: int = DEFAULT_LAN_PORT
    display_name: str = ""

    def normalized(self) -> LanAccessSettings:
        if not isinstance(self.enabled, bool):
            raise LanSettingsError("enabled must be a boolean")
        host = _private_ipv4(self.bind_host, allow_empty=True)
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise LanSettingsError("port must be an integer")
        if not 1 <= self.port <= 65_535:
            raise LanSettingsError("port must be between 1 and 65535")
        name = str(self.display_name or "").strip()
        if not name:
            name = default_display_name()
        if len(name) > 96 or any(character in name for character in "\r\n\0"):
            raise LanSettingsError("display_name is invalid")
        if self.enabled and not host:
            raise LanSettingsError("an enabled LAN gateway requires a private IPv4")
        return LanAccessSettings(
            enabled=self.enabled,
            bind_host=host,
            port=self.port,
            display_name=name,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self.normalized())


class LanSettingsStore:
    """Store only public listener preferences; pairing secrets live elsewhere."""

    def __init__(self, config_home: str | Path) -> None:
        self.path = Path(config_home).expanduser().resolve() / "lan-access.json"

    def load(self) -> LanAccessSettings:
        if not self.path.exists():
            hosts = available_private_ipv4_hosts()
            return LanAccessSettings(
                bind_host=hosts[0].address if hosts else "",
                display_name=default_display_name(),
            )
        try:
            if self.path.stat().st_size > _MAX_SETTINGS_BYTES:
                raise LanSettingsError("lan-access.json is too large")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except LanSettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LanSettingsError(f"Unable to read LAN settings: {exc}") from exc
        if not isinstance(payload, dict):
            raise LanSettingsError("LAN settings root must be an object")
        if payload.get("schema_version") != LAN_SETTINGS_SCHEMA_VERSION:
            raise LanSettingsError("LAN settings schema version is unsupported")
        unknown = set(payload) - {
            "schema_version",
            "enabled",
            "bind_host",
            "port",
            "display_name",
        }
        if unknown:
            raise LanSettingsError(
                "LAN settings contain unsupported fields: " + ", ".join(sorted(unknown))
            )
        return LanAccessSettings(
            enabled=payload.get("enabled", False),
            bind_host=str(payload.get("bind_host") or ""),
            port=payload.get("port", DEFAULT_LAN_PORT),
            display_name=str(payload.get("display_name") or ""),
        ).normalized()

    def save(self, settings: LanAccessSettings) -> LanAccessSettings:
        normalized = settings.normalized()
        payload = {
            "schema_version": LAN_SETTINGS_SCHEMA_VERSION,
            **normalized.to_dict(),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_SETTINGS_BYTES:
            raise LanSettingsError("LAN settings payload is too large")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".lan-access-",
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
            raise LanSettingsError(f"Unable to save LAN settings: {exc}") from exc
        return normalized


def default_display_name() -> str:
    computer = platform.node().strip() or socket.gethostname().strip() or "Windows"
    safe = "".join(character for character in computer if character not in "\r\n\0")
    return f"Zvec on {safe[:64]}"


def available_private_ipv4_hosts() -> tuple[LanHostOption, ...]:
    """Return stable private IPv4 candidates without shelling out to ipconfig."""

    candidates: set[str] = set()
    names = {socket.gethostname(), platform.node()}
    for name in names:
        if not name:
            continue
        try:
            addresses = socket.getaddrinfo(name, None, socket.AF_INET)
        except OSError:
            continue
        for address in addresses:
            raw = str(address[4][0])
            try:
                normalized = _private_ipv4(raw, allow_empty=False)
            except LanSettingsError:
                continue
            candidates.add(normalized)
    return tuple(
        LanHostOption(address=value, label="专用网络")
        for value in sorted(
            candidates,
            key=lambda item: tuple(map(int, item.split("."))),
        )
    )


def _private_ipv4(value: Any, *, allow_empty: bool) -> str:
    raw = str(value or "").strip()
    if not raw and allow_empty:
        return ""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise LanSettingsError("bind_host must be an IPv4 address") from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not any(address in network for network in _RFC1918_NETWORKS)
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        raise LanSettingsError("bind_host must be a private, non-loopback IPv4")
    return str(address)


__all__ = [
    "DEFAULT_DISCOVERY_PORT",
    "DEFAULT_LAN_PORT",
    "LAN_SETTINGS_SCHEMA_VERSION",
    "LanAccessSettings",
    "LanHostOption",
    "LanSettingsError",
    "LanSettingsStore",
    "available_private_ipv4_hosts",
    "default_display_name",
]
