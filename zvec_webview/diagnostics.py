"""Bounded, redacted JSONL diagnostics for the embedded frontend."""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

JsonObject = dict[str, Any]

_ALLOWED_EVENTS: Final = frozenset(
    {
        "window_error",
        "unhandled_rejection",
        "search_submit_started",
        "search_submit_accepted",
        "search_poll_started",
        "search_completed",
        "search_failed",
        "search_cancelled",
        "search_watchdog_timeout",
    }
)
_ALLOWED_FIELDS: Final = frozenset(
    {
        "event",
        "level",
        "message",
        "details",
        "timestamp",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|session|token)",
    re.IGNORECASE,
)
_INLINE_SECRET_PATTERNS: Final = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)
_LOCAL_PATH_PATTERNS: Final = (
    re.compile(r"(?i)\bfile:///[^ \t\r\n\"']+"),
    re.compile(r"(?i)\b[a-z]:[\\/][^ \t\r\n\"'<>|]+"),
    re.compile(r"\\\\[^\\\s]+\\[^ \t\r\n\"'<>|]+"),
)
_CONTEXT_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class DiagnosticValidationError(ValueError):
    """A frontend diagnostic did not match the narrow public contract."""


class FrontendDiagnosticLog:
    """Append one sanitized event per line with small bounded rotations."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 1 * 1024 * 1024,
        backup_count: int = 3,
        redactions: Sequence[str] = (),
    ) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        if not 1 <= backup_count <= 20:
            raise ValueError("backup_count must be between 1 and 20")
        self._path = Path(path).expanduser().resolve()
        self._max_bytes = int(max_bytes)
        self._backup_count = int(backup_count)
        self._redactions = tuple(
            value for value in redactions if isinstance(value, str) and value
        )
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, payload: Mapping[str, Any]) -> JsonObject:
        event = validate_frontend_diagnostic(payload, redactions=self._redactions)
        event["server_time"] = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(encoded) > self._max_bytes:
            # Individual events are already bounded, but keep the writer safe
            # when limits are changed independently in the future.
            raise DiagnosticValidationError("诊断事件超过单文件大小限制。")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(encoded))
            with self._path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
        return event

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self._max_bytes:
            return
        oldest = self._backup_path(self._backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self._path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")


def validate_frontend_diagnostic(
    payload: Mapping[str, Any],
    *,
    redactions: Sequence[str] = (),
) -> JsonObject:
    """Validate a small schema and return a log-injection-safe copy."""

    unknown = sorted(set(payload) - _ALLOWED_FIELDS)
    if unknown:
        raise DiagnosticValidationError(
            f"诊断事件包含不支持的字段：{', '.join(unknown)}"
        )
    event = payload.get("event")
    if not isinstance(event, str) or event not in _ALLOWED_EVENTS:
        raise DiagnosticValidationError("诊断事件类型无效。")
    level = payload.get("level")
    if level not in {"error", "warning", "info"}:
        raise DiagnosticValidationError("level 必须是 error、warning 或 info。")
    result: JsonObject = {"event": event, "level": level}
    if "message" in payload:
        result["message"] = _required_text(
            payload["message"], "message", 2_000, redactions
        )
    if "details" in payload:
        result["details"] = _details(payload["details"], redactions)
    if "timestamp" in payload:
        result["timestamp"] = _required_text(
            payload["timestamp"], "timestamp", 64, redactions
        )
    if "message" not in result and not result.get("details"):
        raise DiagnosticValidationError("message 与 details 至少需要提供一项。")
    return result


def _details(value: Any, redactions: Sequence[str]) -> JsonObject:
    if not isinstance(value, Mapping):
        raise DiagnosticValidationError("details 必须是对象。")
    if len(value) > 20:
        raise DiagnosticValidationError("details 字段过多。")
    result: JsonObject = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not _CONTEXT_KEY.fullmatch(raw_key):
            raise DiagnosticValidationError("details 字段名称无效。")
        if _SENSITIVE_KEY.search(raw_key):
            result[raw_key] = "<redacted>"
            continue
        if raw_value is None or isinstance(raw_value, bool | int):
            result[raw_key] = raw_value
            continue
        if isinstance(raw_value, float):
            if not math.isfinite(raw_value):
                raise DiagnosticValidationError(f"details.{raw_key} 必须是有限数字。")
            result[raw_key] = raw_value
            continue
        if isinstance(raw_value, str):
            result[raw_key] = _optional_text(
                raw_value, f"details.{raw_key}", 500, redactions
            )
            continue
        raise DiagnosticValidationError(
            f"details.{raw_key} 只允许字符串、数字、布尔值或 null。"
        )
    return result


def _required_text(
    value: Any,
    name: str,
    maximum: int,
    redactions: Sequence[str],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosticValidationError(f"{name} 必须是非空字符串。")
    return _sanitize_text(value, name, maximum, redactions)


def _optional_text(
    value: Any,
    name: str,
    maximum: int,
    redactions: Sequence[str],
) -> str:
    if not isinstance(value, str):
        raise DiagnosticValidationError(f"{name} 必须是字符串。")
    return _sanitize_text(value, name, maximum, redactions)


def _sanitize_text(
    value: str,
    name: str,
    maximum: int,
    redactions: Sequence[str],
) -> str:
    if len(value) > maximum:
        raise DiagnosticValidationError(f"{name} 过长。")
    # Keep a one-record-per-line JSONL format even if browser errors contain
    # multiline stacks or attacker-controlled newline characters.
    sanitized = value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    sanitized = "".join(
        character if ord(character) >= 32 else " " for character in sanitized
    ).strip()
    for secret in redactions:
        sanitized = sanitized.replace(secret, "<redacted>")
    for pattern in _INLINE_SECRET_PATTERNS:
        sanitized = pattern.sub(_redact_match, sanitized)
    for pattern in _LOCAL_PATH_PATTERNS:
        sanitized = pattern.sub("<local-path>", sanitized)
    return sanitized


def _redact_match(match: re.Match[str]) -> str:
    label = match.group(1) if match.lastindex else "secret"
    return f"{label}=<redacted>"


__all__ = [
    "DiagnosticValidationError",
    "FrontendDiagnosticLog",
    "validate_frontend_diagnostic",
]
