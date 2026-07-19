# ruff: noqa: F821 - PyInstaller injects the spec globals at build time.
"""PyInstaller onedir spec for the pure-Python Windows x64 desktop."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
RELEASE_DIR = ROOT / "release" / "python_preview"
ICON = ROOT / "assets" / "Zvec.AppIcon.ico"

# One Analysis/PYZ and one COLLECT are intentional: all three executables share
# Tcl/Tk, Pillow, the CPython runtime, and the zvec native dependency closure.
analysis = Analysis(
    [str(RELEASE_DIR / "frozen_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "model-catalog.default.json"), "."),
        (str(ICON), "assets"),
    ],
    hiddenimports=[
        "image_service",
        "zvec_launcher",
        "zvec_desktop.app",
        "zvec_desktop.settings_panel",
        "zvec_desktop.single_instance",
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageTk",
        "zvec._zvec",
    ],
    hookspath=[str(RELEASE_DIR / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(RELEASE_DIR / "runtime_hook.py")],
    excludes=[
        "openai",
        "sentence_transformers",
        "torch",
        "transformers",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

desktop_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Zvec.Desktop",
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

desktop = COLLECT(
    desktop_exe,
    cli_exe,
    backend_exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Zvec-Desktop",
)
