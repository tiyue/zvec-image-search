"""Locate desktop assets and normalize platform visual resources.

All helpers are best-effort: a missing icon, unavailable font enumeration or
an unusual display server must never prevent the desktop application from
starting.  Keeping these operations here also makes source and PyInstaller
runtimes follow the same visual setup.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from typing import Any

from .theme import DEFAULT_THEME, DesktopTypography

APPLICATION_ICON = Path("assets") / "Zvec.AppIcon.ico"
MINIMUM_TK_SCALING = 1.0
MAXIMUM_TK_SCALING = 4.0


def resource_roots() -> tuple[Path, ...]:
    """Return ordered resource roots without assuming a frozen executable."""

    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        roots.append(Path(frozen_root))
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        roots.extend((executable_root / "_internal", executable_root))
    roots.append(Path(__file__).resolve().parents[1])

    # Preserve precedence while avoiding repeated filesystem probes.
    return tuple(dict.fromkeys(root.resolve() for root in roots))


def find_resource(relative_path: str | Path) -> Path | None:
    """Find a bundled resource, returning ``None`` when it is unavailable."""

    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("resource path must remain relative to its resource root")
    for root in resource_roots():
        candidate = root / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # A disconnected installation volume must not prevent app startup.
            continue
    return None


def apply_application_icon(window: Any) -> Path | None:
    """Apply the Windows icon when present and never fail window creation."""

    icon = find_resource(APPLICATION_ICON)
    if icon is None:
        return None
    try:
        # ``default=`` also applies the icon to later Toplevel preview windows.
        window.iconbitmap(default=str(icon))
    except (OSError, RuntimeError, tk.TclError):
        return None
    return icon


def resolve_ui_font_family(
    window: Any,
    typography: DesktopTypography = DEFAULT_THEME.typography,
) -> str:
    """Resolve the preferred Chinese UI font installed on the current system."""

    try:
        available = tkfont.families(root=window)
    except (OSError, RuntimeError, tk.TclError):
        return typography.family
    return typography.resolve_family(available)


def configure_tk_scaling(window: Any) -> float | None:
    """Align Tk point scaling with the active display's reported DPI.

    Tk expects pixels per typographic point, hence ``dpi / 72``.  Clamping
    protects layout from broken remote-desktop drivers that occasionally
    report zero or implausibly large DPI values.  The function is safe to call
    immediately after creating ``Tk`` and before configuring named fonts.
    """

    try:
        dpi = float(window.winfo_fpixels("1i"))
        if dpi <= 0:
            return None
        scaling = min(MAXIMUM_TK_SCALING, max(MINIMUM_TK_SCALING, dpi / 72.0))
        window.tk.call("tk", "scaling", scaling)
    except (OSError, RuntimeError, TypeError, ValueError, tk.TclError):
        return None
    return scaling
