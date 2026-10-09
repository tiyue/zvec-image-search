"""Validated FRPC import and owned runtime configuration for remote access.

The user imports an existing single-proxy FRPC TOML file.  Only the validated
FRP fields are copied into an owned JSON config inside the application config
home; the original file is never modified.  Public preferences persist only the
enabled flag so no credential enters the loopback status surface.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

REMOTE_SETTINGS_SCHEMA_VERSION: Final = 1
REMOTE_PREFERENCES_FILE: Final = "remote-access.json"
REMOTE_OWNED_CONFIG_FILE: Final = "frpc-owned.json"
_MAX_IMPORT_BYTES: Final = 256 * 1024
_MAX_SETTINGS_BYTES: Final = 64 * 1024

_PROXY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_ALLOWED_CLIENT_KEYS: Final = frozenset(
    {
        "serverAddr",
        "serverPort",
        "auth",
        "loginFailExit",
        "log",
        "transport",
        "user",
        "dnsServer",
        "proxies",
    }
)
_ALLOWED_AUTH_KEYS: Final = frozenset({"method", "token"})
_ALLOWED_PROXY_KEYS: Final = frozenset(
    {
        "name",
        "type",
        "localIP",
        "localPort",
        "remotePort",
        "useEncryption",
        "useCompression",
    }
)
_PLUGIN_PROXY_KEYS: Final = ("plugin", "pluginParams", "pluginLocalPath")
_ALLOWED_TRANSPORT_KEYS: Final = frozenset(
    {
        "protocol",
        "dialServerTimeout",
        "dialServerKeepalive",
        "poolCount",
        "tcpMux",
        "heartbeatInterval",
        "heartbeatTimeout",
        "tls",
    }
)
_TRANSPORT_SCALAR_KEYS: Final = (
    "protocol",
    "dialServerTimeout",
    "dialServerKeepalive",
    "poolCount",
    "tcpMux",
    "heartbeatInterval",
    "heartbeatTimeout",
)
_ALLOWED_TLS_KEYS: Final = frozenset({"enable", "serverName", "insecureSkipVerify"})
_CERTIFICATE_KEYS: Final = ("certFile", "keyFile", "trustedCaFile")


class RemoteSettingsError(RuntimeError):
    """The imported or owned remote-access configuration is unusable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RemoteFrpConfig:
    """The validated, non-secret plus credential FRP fields we own."""

    server_addr: str
    server_port: int
    token: str
    proxy_name: str
    remote_port: int
    local_ip: str
    local_port: int
    use_encryption: bool = False
    use_compression: bool = False
    transport: dict[str, Any] | None = None
    user: str | None = None
    dns_server: str | None = None

    def public_origin(self) -> str:
        return f"http://{self.server_addr}:{self.remote_port}"

    def to_owned_payload(self, *, local_host: str, local_port: int) -> dict[str, Any]:
        proxy: dict[str, Any] = {
            "name": self.proxy_name,
            "type": "tcp",
            "localIP": local_host,
            "localPort": local_port,
            "remotePort": self.remote_port,
        }
        if self.use_encryption:
            proxy["useEncryption"] = True
        if self.use_compression:
            proxy["useCompression"] = True
        payload: dict[str, Any] = {
            "serverAddr": self.server_addr,
            "serverPort": self.server_port,
            "auth": {"method": "token", "token": self.token},
            "loginFailExit": False,
            "log": {"to": "console", "level": "info"},
            "proxies": [proxy],
        }
        if self.transport:
            payload["transport"] = dict(self.transport)
        if self.user:
            payload["user"] = self.user
        if self.dns_server:
            payload["dnsServer"] = self.dns_server
        return payload


@dataclass(frozen=True, slots=True)
class RemotePreferences:
    enabled: bool = False


class RemoteSettingsStore:
    """Import one bounded FRPC TOML file and own its ported configuration."""

    def __init__(self, config_home: str | Path) -> None:
        home = Path(config_home).expanduser().resolve()
        self.preferences_path = home / REMOTE_PREFERENCES_FILE
        self.config_path = home / REMOTE_OWNED_CONFIG_FILE

    def load_preferences(self) -> RemotePreferences:
        if not self.preferences_path.exists():
            return RemotePreferences()
        payload = _read_json_object(self.preferences_path, "远程访问偏好无法读取。")
        if payload.get("schema_version") != REMOTE_SETTINGS_SCHEMA_VERSION:
            raise RemoteSettingsError(
                "remote_settings_invalid", "远程访问偏好版本不受支持。"
            )
        unknown = set(payload) - {"schema_version", "enabled"}
        if unknown:
            raise RemoteSettingsError(
                "remote_settings_invalid", "远程访问偏好包含未知字段。"
            )
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RemoteSettingsError(
                "remote_settings_invalid", "远程访问偏好 enabled 必须为布尔值。"
            )
        return RemotePreferences(enabled=enabled)

    def save_preferences(self, enabled: bool) -> RemotePreferences:
        if not isinstance(enabled, bool):
            raise RemoteSettingsError(
                "remote_settings_invalid", "远程访问偏好 enabled 必须为布尔值。"
            )
        preferences = RemotePreferences(enabled=enabled)
        payload = {
            "schema_version": REMOTE_SETTINGS_SCHEMA_VERSION,
            "enabled": preferences.enabled,
        }
        _atomic_write_json(self.preferences_path, payload, "远程访问偏好无法保存。")
        return preferences

    def load_config(self) -> RemoteFrpConfig | None:
        if not self.config_path.exists():
            return None
        payload = _read_json_object(self.config_path, "已保存的 FRPC 配置无法读取。")
        return _config_from_owned(payload)

    def import_toml(self, source_path: str | Path) -> RemoteFrpConfig:
        """Validate one TOML import and write the owned config copy."""

        if not isinstance(source_path, (str, Path)):
            raise RemoteSettingsError(
                "remote_config_invalid", "请选择要导入的 FRPC 配置文件。"
            )
        path = Path(source_path).expanduser()
        try:
            if not path.is_file():
                raise RemoteSettingsError(
                    "remote_config_invalid", "无法读取所选的 FRPC 配置文件。"
                )
            if path.stat().st_size > _MAX_IMPORT_BYTES:
                raise RemoteSettingsError(
                    "remote_config_invalid", "FRPC 配置文件过大。"
                )
            raw = path.read_bytes()
        except RemoteSettingsError:
            raise
        except OSError as exc:
            raise RemoteSettingsError(
                "remote_config_invalid", "无法读取所选的 FRPC 配置文件。"
            ) from exc
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteSettingsError(
                "remote_config_invalid", "FRPC 配置必须是 UTF-8 编码的 TOML。"
            ) from exc
        try:
            parsed = tomllib.loads(decoded)
        except tomllib.TOMLDecodeError as exc:
            raise RemoteSettingsError(
                "remote_config_invalid", "FRPC 配置不是有效的 TOML。"
            ) from exc
        config = _parse_import(parsed)
        self.write_config(
            config, local_host=config.local_ip, local_port=config.local_port
        )
        return config

    def write_config(
        self,
        config: RemoteFrpConfig,
        *,
        local_host: str,
        local_port: int,
    ) -> Path:
        payload = config.to_owned_payload(local_host=local_host, local_port=local_port)
        _atomic_write_json(self.config_path, payload, "FRPC 运行配置无法写入。")
        return self.config_path


def _parse_import(data: Any) -> RemoteFrpConfig:
    if not isinstance(data, dict):
        raise _unsupported("FRPC 配置根必须是表。")
    unknown = set(data) - _ALLOWED_CLIENT_KEYS
    if "includes" in unknown:
        raise _unsupported("不支持 includes 引入的外部配置文件。")
    if "visitors" in unknown:
        raise _unsupported("不支持 visitors 代理访问者。")
    if unknown:
        raise _unsupported(
            "FRPC 配置包含不受支持的顶层字段：" + ", ".join(sorted(unknown))
        )

    server_addr = _ipv4_literal(data.get("serverAddr"), "serverAddr")
    server_port = _port(data.get("serverPort"), "serverPort")

    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise _unsupported("缺少 auth.token 认证信息。")
    unknown_auth = set(auth) - _ALLOWED_AUTH_KEYS
    if unknown_auth:
        raise _unsupported(
            "auth 包含不受支持的字段：" + ", ".join(sorted(unknown_auth))
        )
    method = auth.get("method", "token")
    if str(method).casefold() != "token":
        raise _unsupported("仅支持 token 认证方式。")
    token = auth.get("token")
    if not isinstance(token, str) or not token:
        raise _unsupported("auth.token 必须是非空字符串。")

    transport = _validate_transport(data.get("transport"))
    user = _optional_text(data.get("user"), "user")
    dns_server = _optional_text(data.get("dnsServer"), "dnsServer")

    proxies = data.get("proxies")
    if not isinstance(proxies, list) or len(proxies) != 1:
        raise _unsupported("仅支持恰好一个 TCP 代理。")
    proxy = proxies[0]
    if not isinstance(proxy, dict):
        raise _unsupported("代理定义必须是表。")
    for plugin_key in _PLUGIN_PROXY_KEYS:
        if plugin_key in proxy:
            raise _unsupported("不支持代理插件。")
    unknown_proxy = set(proxy) - _ALLOWED_PROXY_KEYS
    if unknown_proxy:
        raise _unsupported(
            "代理包含不受支持的字段：" + ", ".join(sorted(unknown_proxy))
        )
    if str(proxy.get("type", "")).casefold() != "tcp":
        raise _unsupported("仅支持 TCP 代理类型。")
    name = proxy.get("name")
    if not isinstance(name, str) or not _PROXY_NAME_PATTERN.match(name):
        raise _unsupported("代理名称无效。")
    remote_port = _port(proxy.get("remotePort"), "remotePort")
    local_port = proxy.get("localPort")
    if local_port is not None:
        local_port = _port(local_port, "localPort")
    else:
        local_port = remote_port
    local_ip = proxy.get("localIP")
    if local_ip is not None:
        local_ip = _ipv4_literal(local_ip, "localIP")
    else:
        local_ip = "127.0.0.1"
    use_encryption = _optional_bool(proxy.get("useEncryption"))
    use_compression = _optional_bool(proxy.get("useCompression"))

    return RemoteFrpConfig(
        server_addr=server_addr,
        server_port=server_port,
        token=token,
        proxy_name=name,
        remote_port=remote_port,
        local_ip=local_ip,
        local_port=local_port,
        use_encryption=use_encryption,
        use_compression=use_compression,
        transport=transport,
        user=user,
        dns_server=dns_server,
    )


def _validate_transport(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _unsupported("transport 必须是表。")
    unknown = set(value) - _ALLOWED_TRANSPORT_KEYS
    if unknown:
        raise _unsupported(
            "transport 包含不受支持的字段：" + ", ".join(sorted(unknown))
        )
    protocol = value.get("protocol")
    if protocol is not None and str(protocol).casefold() != "tcp":
        raise _unsupported("仅支持 TCP 传输协议。")
    preserved: dict[str, Any] = {}
    for key in _TRANSPORT_SCALAR_KEYS:
        if key in value and _is_scalar(value[key]):
            preserved[key] = value[key]
    tls = value.get("tls")
    if tls is not None:
        if not isinstance(tls, dict):
            raise _unsupported("transport.tls 必须是表。")
        for certificate_key in _CERTIFICATE_KEYS:
            candidate = tls.get(certificate_key)
            if isinstance(candidate, str) and candidate.strip():
                raise _unsupported("不支持自定义 TLS 证书文件。")
        unknown_tls = set(tls) - _ALLOWED_TLS_KEYS - set(_CERTIFICATE_KEYS)
        if unknown_tls:
            raise _unsupported(
                "transport.tls 包含不受支持的字段：" + ", ".join(sorted(unknown_tls))
            )
        tls_subset = {
            key: tls[key]
            for key in ("enable", "serverName", "insecureSkipVerify")
            if key in tls and _is_scalar(tls[key])
        }
        if tls_subset:
            preserved["tls"] = tls_subset
    return preserved or None


def _config_from_owned(payload: dict[str, Any]) -> RemoteFrpConfig:
    try:
        server_addr = _ipv4_literal(payload["serverAddr"], "serverAddr")
        server_port = _port(payload["serverPort"], "serverPort")
        auth = payload["auth"]
        token = auth["token"]
        proxy = payload["proxies"][0]
        proxy_name = proxy["name"]
        remote_port = _port(proxy["remotePort"], "remotePort")
        local_ip = _ipv4_literal(proxy.get("localIP", "127.0.0.1"), "localIP")
        local_port = _port(proxy.get("localPort", remote_port), "localPort")
        use_encryption = proxy.get("useEncryption") is True
        use_compression = proxy.get("useCompression") is True
    except (KeyError, IndexError, TypeError) as exc:
        raise RemoteSettingsError(
            "remote_settings_invalid", "已保存的 FRPC 配置无效。"
        ) from exc
    if not isinstance(token, str) or not token or not isinstance(proxy_name, str):
        raise RemoteSettingsError("remote_settings_invalid", "已保存的 FRPC 配置无效。")
    return RemoteFrpConfig(
        server_addr=server_addr,
        server_port=server_port,
        token=token,
        proxy_name=proxy_name,
        remote_port=remote_port,
        local_ip=local_ip,
        local_port=local_port,
        use_encryption=use_encryption,
        use_compression=use_compression,
        transport=_validate_transport(payload.get("transport")),
        user=_optional_text(payload.get("user"), "user"),
        dns_server=_optional_text(payload.get("dnsServer"), "dnsServer"),
    )


def _read_json_object(path: Path, message: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            raise RemoteSettingsError("remote_settings_invalid", message)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RemoteSettingsError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteSettingsError("remote_settings_invalid", message) from exc
    if not isinstance(payload, dict):
        raise RemoteSettingsError("remote_settings_invalid", message)
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any], message: str) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_SETTINGS_BYTES:
        raise RemoteSettingsError("remote_settings_invalid", message)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise RemoteSettingsError("remote_settings_invalid", message) from exc


def _unsupported(message: str) -> RemoteSettingsError:
    return RemoteSettingsError("remote_config_unsupported", message)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)


def _ipv4_literal(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise _unsupported(f"{field_name} 必须是 IPv4 地址。") from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or address.is_unspecified
        or address.is_multicast
    ):
        raise _unsupported(f"{field_name} 必须是有效的 IPv4 地址。")
    return str(address)


def _port(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unsupported(f"{field_name} 必须是端口号。")
    if not 1 <= value <= 65_535:
        raise _unsupported(f"{field_name} 必须在 1 到 65535 之间。")
    return value


def _optional_bool(value: Any) -> bool:
    return value is True


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _unsupported(f"{field_name} 必须是字符串。")
    text = value.strip()
    return text or None


__all__ = [
    "REMOTE_OWNED_CONFIG_FILE",
    "REMOTE_PREFERENCES_FILE",
    "REMOTE_SETTINGS_SCHEMA_VERSION",
    "RemoteFrpConfig",
    "RemotePreferences",
    "RemoteSettingsError",
    "RemoteSettingsStore",
]
