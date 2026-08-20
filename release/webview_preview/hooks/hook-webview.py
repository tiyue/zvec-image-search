"""Minimal Windows x64 PyInstaller hook for pywebview 6.2.1.

The upstream hook collects WebView2 loaders for x86, x64, and ARM64.  This
Preview intentionally ships only x64 and keeps the JavaScript bridge files that
pywebview loads from disk at runtime.
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import get_package_paths  # type: ignore[import-not-found]

_, package_path = get_package_paths("webview")
package_root = Path(package_path)


def _required(relative: str) -> Path:
    path = package_root / relative
    if not path.is_file():
        raise RuntimeError(f"pywebview 6.2.1 runtime file is missing: {relative}")
    return path


datas = [
    (str(path), f"webview/{path.parent.relative_to(package_root).as_posix()}")
    for path in sorted((package_root / "js").rglob("*.js"))
]

binaries = [
    (str(_required("lib/Microsoft.Web.WebView2.Core.dll")), "webview/lib"),
    (str(_required("lib/Microsoft.Web.WebView2.WinForms.dll")), "webview/lib"),
    (str(_required("lib/WebBrowserInterop.x64.dll")), "webview/lib"),
    (
        str(_required("lib/runtimes/win-x64/native/WebView2Loader.dll")),
        "webview/lib/runtimes/win-x64/native",
    ),
]

hiddenimports = [
    "clr",
    "webview.platforms.edgechromium",
    "webview.platforms.win32",
    "webview.platforms.winforms",
]
