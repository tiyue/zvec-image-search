"""Small, dependency-free visual theme shared by the tkinter desktop UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DesktopTheme:
    window: str = "#F3F6FB"
    surface: str = "#FFFFFF"
    surface_muted: str = "#E9EFF7"
    border: str = "#D5DEEA"
    text: str = "#172033"
    text_muted: str = "#617087"
    primary: str = "#4F5FE7"
    primary_hover: str = "#3E4ED0"
    success: str = "#0F8A74"
    warning: str = "#B46A18"
    danger: str = "#C44747"
    fullscreen: str = "#05070B"
    selection: str = "#DDE4FF"


DEFAULT_THEME = DesktopTheme()
