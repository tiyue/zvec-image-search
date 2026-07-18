"""Secure API-key storage shared with the former WPF desktop client.

On Windows this module uses the same Generic Credential target as the WPF
application, so a pure-Python upgrade can reuse the saved DashScope key without
placing it in JSON or environment files.  Other platforms deliberately fall
back to an in-memory session store until a native keyring integration is added.
"""

from __future__ import annotations

import ctypes
import getpass
import os
import threading
from ctypes import wintypes
from typing import Protocol

DEFAULT_DASHSCOPE_CREDENTIAL_TARGET = "Zvec.ImageSearch/DashScopeApiKey"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BYTES = 2560


class CredentialError(RuntimeError):
    """A secure credential operation failed."""


class CredentialStore(Protocol):
    """Minimal secret-store contract used by the desktop settings page."""

    @property
    def persistent(self) -> bool: ...

    def has_secret(self) -> bool: ...

    def read_secret(self) -> str | None: ...

    def save_secret(self, secret: str) -> None: ...

    def delete_secret(self) -> None: ...


class SessionCredentialStore:
    """Thread-safe, non-persistent fallback for non-Windows desktops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secret: str | None = None

    @property
    def persistent(self) -> bool:
        return False

    def has_secret(self) -> bool:
        with self._lock:
            return self._secret is not None

    def read_secret(self) -> str | None:
        with self._lock:
            return self._secret

    def save_secret(self, secret: str) -> None:
        normalized = _validated_secret(secret)
        with self._lock:
            self._secret = normalized

    def delete_secret(self) -> None:
        with self._lock:
            self._secret = None


class _NativeCredentialApi(Protocol):
    def read(self, target: str) -> bytes | None: ...

    def write(self, target: str, username: str, secret: bytes) -> None: ...

    def delete(self, target: str) -> None: ...


class WindowsCredentialStore:
    """Windows Credential Manager-backed Generic Credential store."""

    def __init__(
        self,
        target_name: str = DEFAULT_DASHSCOPE_CREDENTIAL_TARGET,
        *,
        native_api: _NativeCredentialApi | None = None,
    ) -> None:
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValueError("target_name must be non-empty")
        if "\r" in target_name or "\n" in target_name:
            raise ValueError("target_name must not contain line breaks")
        self._target_name = target_name.strip()
        if native_api is None:
            if os.name != "nt":
                raise OSError("Windows Credential Manager is only available on Windows")
            native_api = _WindowsCredentialApi()
        self._native = native_api

    @property
    def persistent(self) -> bool:
        return True

    @property
    def target_name(self) -> str:
        return self._target_name

    def has_secret(self) -> bool:
        return self._native.read(self._target_name) is not None

    def read_secret(self) -> str | None:
        raw = self._native.read(self._target_name)
        if raw is None:
            return None
        mutable = bytearray(raw)
        try:
            return mutable.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialError("Windows 凭据不是有效的 UTF-8 文本。") from exc
        finally:
            mutable[:] = b"\x00" * len(mutable)

    def save_secret(self, secret: str) -> None:
        normalized = _validated_secret(secret)
        encoded = bytearray(normalized.encode("utf-8"))
        if len(encoded) > _MAX_CREDENTIAL_BYTES:
            encoded[:] = b"\x00" * len(encoded)
            raise ValueError("安全凭据超过 Windows 凭据大小限制。")
        try:
            self._native.write(self._target_name, getpass.getuser(), bytes(encoded))
        finally:
            encoded[:] = b"\x00" * len(encoded)

    def delete_secret(self) -> None:
        self._native.delete(self._target_name)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _WindowsCredentialApi:
    """Small ctypes boundary kept injectable for tests."""

    def __init__(self) -> None:
        try:
            library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise CredentialError("无法加载 Windows 凭据管理器。") from exc
        self._library = library
        self._read = library.CredReadW
        self._read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._read.restype = wintypes.BOOL
        self._write = library.CredWriteW
        self._write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._delete = library.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = library.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def read(self, target: str) -> bytes | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"无法读取 Windows 凭据管理器（错误代码 {error}）。")
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size == 0 or not credential.CredentialBlob:
                return None
            return ctypes.string_at(credential.CredentialBlob, size)
        finally:
            self._free(pointer)

    def write(self, target: str, username: str, secret: bytes) -> None:
        buffer = ctypes.create_string_buffer(secret, len(secret))
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = username
        try:
            if not self._write(ctypes.byref(credential), 0):
                error = ctypes.get_last_error()
                raise CredentialError(
                    f"无法保存凭据到 Windows 凭据管理器（错误代码 {error}）。"
                )
        finally:
            ctypes.memset(buffer, 0, len(secret))

    def delete(self, target: str) -> None:
        if self._delete(target, _CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != _ERROR_NOT_FOUND:
            raise CredentialError(
                f"无法删除 Windows 凭据管理器中的凭据（错误代码 {error}）。"
            )


def default_credential_store() -> CredentialStore:
    """Return persistent Windows storage or a documented session-only fallback."""

    if os.name == "nt":
        return WindowsCredentialStore()
    return SessionCredentialStore()


def _validated_secret(secret: str) -> str:
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("安全凭据不能为空。")
    if "\r" in secret or "\n" in secret:
        raise ValueError("安全凭据不能包含换行符。")
    return secret.strip()


__all__ = [
    "CredentialError",
    "CredentialStore",
    "DEFAULT_DASHSCOPE_CREDENTIAL_TARGET",
    "SessionCredentialStore",
    "WindowsCredentialStore",
    "default_credential_store",
]
