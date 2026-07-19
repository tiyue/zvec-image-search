# ruff: noqa: F821 - PyInstaller injects the spec globals at build time.
"""Independent PyInstaller onedir spec for Windows x64 Webview Preview."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
RELEASE_DIR = ROOT / "release" / "webview_preview"
ICON = ROOT / "assets" / "Zvec.AppIcon.ico"
WEB_ASSETS = ROOT / "zvec_webview" / "frontend_dist"

# All executables intentionally share one Analysis/PYZ/COLLECT dependency graph.
analysis = Analysis(
    [str(RELEASE_DIR / "frozen_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "model-catalog.default.json"), "."),
        (str(ICON), "assets"),
        (str(WEB_ASSETS), "zvec_webview/frontend_dist"),
    ],
    hiddenimports=[
        "image_service",
        "image_vector_service.activity_store",
        "image_vector_service.folder_deletion",
        "image_vector_service.library_browser",
        "zvec_launcher",
        "zvec_webview.app",
        "zvec_webview.facade",
        "zvec_webview.frontend_assets",
        "zvec_webview.image_registry",
        "zvec_webview.native_bridge",
        "zvec_webview.runtime",
        "zvec_webview.server",
        "bottle",
        "clr",
        "clr_loader",
        "proxy_tools",
        "pythonnet",
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.win32",
        "webview.platforms.winforms",
        "PIL.Image",
        "PIL.ImageOps",
        "zvec._zvec",
    ],
    hookspath=[
        str(RELEASE_DIR / "hooks"),
        str(ROOT / "release" / "python_preview" / "hooks"),
    ],
    hooksconfig={},
    runtime_hooks=[str(RELEASE_DIR / "runtime_hook.py")],
    excludes=[
        "cefpython3",
        "openai",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "pystray",
        "sentence_transformers",
        "tkinter",
        "torch",
        "transformers",
        "webview.platforms.android",
        "webview.platforms.cef",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "zvec_desktop.app",
        "zvec_desktop.organize_panel",
        "zvec_desktop.settings_panel",
        "zvec_desktop.theme",
        "zvec_desktop.tray",
        "zvec_desktop.ui",
        "zvec_desktop.widgets",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

preview_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Zvec.WebviewPreview",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON),
)

cli_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="zvec",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON),
)

backend_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="zvec-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON),
)

preview = COLLECT(
    preview_exe,
    cli_exe,
    backend_exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Zvec-Webview-Preview",
)
