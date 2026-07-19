"""Design tokens shared by the dependency-free tkinter desktop UI.

The desktop client intentionally keeps its visual language in one small,
dependency-free module.  Pages should consume semantic tokens from here rather
than inventing one-off colours, font sizes, padding or component dimensions.

Tk uses point-sized fonts, so the typography scale follows Windows display
scaling.  Pixel-based layout values remain deliberately compact enough for the
five-column gallery while retaining a consistent four-pixel rhythm.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DesktopTypography:
    """Point-sized type hierarchy with Windows Chinese font fallbacks.

    Existing fields retain their original order so callers that construct a
    custom theme positionally remain compatible.  ``resolve_family`` is useful
    for Linux development and future portable builds where Microsoft YaHei UI
    may be unavailable.
    """

    family: str = "Microsoft YaHei UI"
    body: int = 10
    supporting: int = 9
    control: int = 10
    section: int = 12
    preview_title: int = 13
    page_title: int = 17
    title: int = 20
    display: int = 24
    monospace_family: str = "Cascadia Mono"
    fallback_families: tuple[str, ...] = (
        "Microsoft YaHei",
        "Segoe UI",
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "PingFang SC",
        "Arial",
    )

    def resolve_family(self, available_families: Iterable[str]) -> str:
        """Return the first installed family while preserving its real casing."""

        available = {
            candidate.strip().casefold(): candidate.strip()
            for candidate in available_families
            if candidate.strip()
        }
        for preferred in (self.family, *self.fallback_families):
            installed = available.get(preferred.casefold())
            if installed is not None:
                return installed
        # Let Tk perform its platform fallback when font enumeration is not
        # available (for example during a headless packaging smoke test).
        return self.family


@dataclass(frozen=True, slots=True)
class DesktopSpacing:
    """A small spacing scale used instead of one-off padding values."""

    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxs: int = 2
    xxl: int = 32

    @property
    def scale(self) -> tuple[int, ...]:
        """Return the complete spacing scale from smallest to largest."""

        return (self.xxs, self.xs, self.sm, self.md, self.lg, self.xl, self.xxl)


@dataclass(frozen=True, slots=True)
class DesktopRadii:
    """Corner-radius intent for canvas-backed and future native components."""

    small: int = 4
    medium: int = 8
    large: int = 12
    pill: int = 999


@dataclass(frozen=True, slots=True)
class DesktopMetrics:
    """Shared component dimensions that keep dense pages visually consistent."""

    border_width: int = 1
    focus_width: int = 2
    compact_control_height: int = 30
    control_height: int = 36
    button_padding_x: int = 14
    button_padding_y: int = 8
    input_padding_x: int = 10
    input_padding_y: int = 8
    tab_padding_x: int = 16
    tab_padding_y: int = 10
    tree_row_height: int = 34
    card_padding: int = 16
    card_gap: int = 12


@dataclass(frozen=True, slots=True)
class DesktopTheme:
    """Modern light palette with a restrained indigo brand accent.

    The first group of fields is kept in its historical order for API
    compatibility.  New semantic tokens are appended after the original theme
    objects so old keyword- and positional-based custom themes keep working.
    """

    window: str = "#F5F6FA"
    surface: str = "#FFFFFF"
    surface_subtle: str = "#FAFAFC"
    surface_muted: str = "#F0F1F5"
    surface_strong: str = "#E7E9EF"
    preview: str = "#10131F"
    navigation: str = "#181B2A"
    navigation_hover: str = "#24283B"
    navigation_selected: str = "#383A75"
    navigation_text: str = "#E6E8F0"
    navigation_muted: str = "#AEB4C4"
    border: str = "#E0E3EA"
    border_strong: str = "#C9CDD8"
    text: str = "#172033"
    text_muted: str = "#556074"
    text_faint: str = "#667085"
    primary: str = "#5B5BD6"
    primary_hover: str = "#4C49C4"
    primary_pressed: str = "#3F3CA8"
    primary_soft: str = "#F0EFFF"
    focus: str = "#7774E7"
    success: str = "#0B7866"
    success_soft: str = "#E7F5F0"
    warning: str = "#9A5B13"
    warning_soft: str = "#FFF4E2"
    danger: str = "#B23A48"
    danger_soft: str = "#FCEDEF"
    code_surface: str = "#F7F7FA"
    code_text: str = "#293248"
    fullscreen: str = "#050609"
    selection: str = "#E9E9FF"
    typography: DesktopTypography = field(default_factory=DesktopTypography)
    spacing: DesktopSpacing = field(default_factory=DesktopSpacing)
    on_primary: str = "#FFFFFF"
    on_navigation: str = "#FFFFFF"
    info: str = "#3767B7"
    info_soft: str = "#EAF2FF"
    disabled_surface: str = "#F2F3F6"
    disabled_text: str = "#7A8392"
    overlay: str = "#0D1321"
    shadow: str = "#D7DBE5"
    radii: DesktopRadii = field(default_factory=DesktopRadii)
    metrics: DesktopMetrics = field(default_factory=DesktopMetrics)

    @property
    def card_background(self) -> str:
        """Semantic alias used by raised content groups."""

        return self.surface

    @property
    def control_background(self) -> str:
        """Semantic alias used by buttons, fields and selectors."""

        return self.surface

    @property
    def control_border(self) -> str:
        """Stronger border reserved for interactive controls."""

        return self.border_strong


DEFAULT_THEME = DesktopTheme()
