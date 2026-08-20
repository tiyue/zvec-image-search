"""Prepare a safe per-user runtime for the frozen Windows webview payload."""

from __future__ import annotations

import os
import platform
import struct
import sys
from pathlib import Path

_PYWEBVIEW_WINDOWS_RUNTIMES = frozenset({"win-arm64", "win-x64", "win-x86"})


def _require_windows_x64() -> None:
    """Fail clearly if a payload is copied to an unsupported runtime."""

    if sys.platform != "win32" or os.name != "nt":
        raise RuntimeError("YaoLens supports Windows only.")
    if struct.calcsize("P") != 8 or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        raise RuntimeError("YaoLens requires Windows x64.")


def _configure_frozen_environment() -> None:
    application_directory = Path(sys.executable).resolve().parent
    os.environ.setdefault("ZVEC_FROZEN_APP_DIR", str(application_directory))
    os.environ.setdefault("ZVEC_WEBVIEW_PREVIEW", "1")


def _x64_pywebview_runtime_name(value: str) -> str:
    """Map pywebview's eager architecture probes to the only shipped loader."""

    return "win-x64" if value in _PYWEBVIEW_WINDOWS_RUNTIMES else value


def _configure_x64_pywebview_loader() -> None:
    """Install the pywebview 6.2.1 x64-only interop path compatibility shim."""

    from webview import util as webview_util

    original = webview_util.interop_dll_path
    if bool(getattr(original, "__zvec_x64_only__", False)):
        return

    def x64_interop_dll_path(value: str) -> str:
        return original(_x64_pywebview_runtime_name(value))

    x64_interop_dll_path.__zvec_x64_only__ = True  # type: ignore[attr-defined]
    webview_util.interop_dll_path = x64_interop_dll_path


def _is_preview_executable() -> bool:
    return Path(sys.executable).stem.casefold() in {
        "yaolens",
        "zvec.webviewpreview",
    }


if getattr(sys, "frozen", False):
    _require_windows_x64()
    _configure_frozen_environment()
    # pywebview 6.2.1 eagerly probes ARM64/x64/x86 directories when importing
    # EdgeChromium. Only the graphical Preview needs this compatibility shim;
    # CLI/backend startup remains independent from pywebview/pythonnet imports.
    if _is_preview_executable():
        _configure_x64_pywebview_loader()
