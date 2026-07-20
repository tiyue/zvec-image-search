"""Windows x64 entry point for the independent pywebview Preview."""

from __future__ import annotations

import argparse
import os
import platform
import struct
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from zvec_desktop.backend_host import BackendBusyError

from .native_bridge import NativeBridge
from .runtime import PreviewRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zvec-webview-preview",
        description="Zvec Windows x64 pywebview Preview",
    )
    parser.add_argument("--config", type=Path, help="使用指定的 config.json。")
    parser.add_argument("--width", type=int, default=1440, help="初始窗口宽度。")
    parser.add_argument("--height", type=int, default=900, help="初始窗口高度。")
    parser.add_argument(
        "--debug-webview",
        action="store_true",
        help="打开 Webview 调试工具，仅用于开发。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        _validate_windows_x64()
        width = _window_size(options.width, "width", 980, 7680)
        height = _window_size(options.height, "height", 680, 4320)
        import webview
    except (ImportError, RuntimeError, ValueError) as exc:
        _report_startup_error(str(exc) or exc.__class__.__name__)
        return 2

    runtime = PreviewRuntime(options.config)
    try:
        started = runtime.start()
    except Exception as exc:
        _report_startup_error(str(exc) or exc.__class__.__name__)
        return 1

    window_holder: dict[str, Any] = {}
    allow_close = threading.Event()
    shutdown_started = threading.Event()

    def begin_shutdown() -> dict[str, Any]:
        if shutdown_started.is_set():
            return {"ok": True, "closing": True}
        if runtime.facade.has_active_jobs():
            return {
                "ok": False,
                "error": "仍有任务运行，请先等待完成或取消任务。",
                "busy": True,
            }
        shutdown_started.set()

        def shutdown_worker() -> None:
            try:
                runtime.close(force=False)
            except BackendBusyError:
                shutdown_started.clear()
                window = window_holder.get("window")
                if window is not None:
                    _notify_close_blocked(window)
                return
            except Exception:
                shutdown_started.clear()
                window = window_holder.get("window")
                if window is not None:
                    _notify_close_failed(window)
                return
            allow_close.set()
            window = window_holder.get("window")
            if window is not None:
                window.destroy()

        threading.Thread(
            target=shutdown_worker,
            name="zvec-webview-shutdown",
            daemon=True,
        ).start()
        return {"ok": True, "closing": True}

    def request_exit() -> dict[str, Any]:
        return begin_shutdown()

    bridge = NativeBridge(runtime.facade.image_registry, request_exit=request_exit)
    try:
        window = webview.create_window(
            "Zvec 图片库 Preview",
            started.address.url,
            js_api=bridge,
            width=width,
            height=height,
            min_size=(980, 680),
            background_color="#F8FAFC",
            text_select=True,
        )
        window_holder["window"] = window
        bridge.attach_window(window)

        def on_closing(*_args: Any) -> bool | None:
            if allow_close.is_set():
                return None
            result = begin_shutdown()
            if result.get("busy") is True:
                _notify_close_blocked(window)
            return False

        window.events.closing += on_closing
        webview.start(
            gui="edgechromium",
            debug=bool(options.debug_webview),
            private_mode=False,
            storage_path=str(runtime.facade.config_home / "webview"),
        )
    except Exception as exc:
        _report_startup_error(f"WebView2 窗口启动失败：{exc}")
        with suppress(BackendBusyError):
            runtime.close(force=False)
        return 1
    finally:
        # The close guard normally proves idleness. Never force-kill an accepted
        # indexing or annotation task merely because the Preview window failed.
        with suppress(BackendBusyError):
            runtime.close(force=False)
    return 0


def _validate_windows_x64() -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise RuntimeError("此 Preview 仅提供 Windows x64 版本。")
    if platform.python_implementation() != "CPython":
        raise RuntimeError("此 Preview 需要 CPython。")
    if struct.calcsize("P") != 8 or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        raise RuntimeError("此 Preview 仅支持 64 位 Windows（x64）。")


def _window_size(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数。")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def _notify_close_blocked(window: Any) -> None:
    try:
        window.evaluate_js(
            "window.dispatchEvent(new CustomEvent('zvec-close-blocked'))"
        )
    except Exception:
        return


def _notify_close_failed(window: Any) -> None:
    try:
        window.evaluate_js("window.dispatchEvent(new CustomEvent('zvec-close-failed'))")
    except Exception:
        return


def _report_startup_error(message: str) -> None:
    try:
        import ctypes

        loader = getattr(ctypes, "windll", None)
        if loader is None:
            raise OSError("Windows user32 is unavailable")
        loader.user32.MessageBoxW(
            None,
            message,
            "Zvec Preview",
            0x10,
        )
    except Exception:
        print(f"Zvec Preview: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
