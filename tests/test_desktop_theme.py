from __future__ import annotations

import unittest
from dataclasses import replace

from zvec_desktop.theme import DEFAULT_THEME, DesktopTheme, DesktopTypography


class DesktopThemeContractTest(unittest.TestCase):
    def test_typography_has_a_clear_information_hierarchy(self) -> None:
        typography = DEFAULT_THEME.typography

        self.assertGreater(typography.display, typography.title)
        self.assertGreater(typography.title, typography.page_title)
        self.assertGreater(typography.page_title, typography.preview_title)
        self.assertGreater(typography.preview_title, typography.section)
        self.assertGreater(typography.section, typography.body)
        self.assertGreaterEqual(typography.body, typography.supporting)
        self.assertGreaterEqual(typography.control, typography.supporting)

    def test_typography_resolves_chinese_font_fallback_case_insensitively(self) -> None:
        typography = DesktopTypography()

        self.assertEqual(
            typography.resolve_family(("Arial", "microsoft yahei", "Segoe UI")),
            "microsoft yahei",
        )
        self.assertEqual(typography.resolve_family(()), typography.family)

    def test_spacing_uses_one_monotonic_scale(self) -> None:
        spacing = DEFAULT_THEME.spacing

        self.assertEqual(tuple(sorted(spacing.scale)), spacing.scale)
        self.assertEqual(len(set(spacing.scale)), len(spacing.scale))
        self.assertTrue(all(value % 2 == 0 for value in spacing.scale))

    def test_navigation_and_content_have_distinct_visual_roles(self) -> None:
        theme = DEFAULT_THEME

        self.assertNotEqual(theme.navigation, theme.window)
        self.assertNotEqual(theme.navigation, theme.surface)
        self.assertNotEqual(theme.navigation_selected, theme.navigation)
        self.assertNotEqual(theme.primary_soft, theme.primary)

    def test_settings_editor_and_focus_tokens_remain_distinct(self) -> None:
        theme = DEFAULT_THEME

        self.assertNotEqual(theme.code_surface, theme.surface)
        self.assertNotEqual(theme.code_text, theme.code_surface)
        self.assertNotEqual(theme.focus, theme.primary_soft)

    def test_component_metrics_have_one_consistent_density(self) -> None:
        metrics = DEFAULT_THEME.metrics

        self.assertLess(metrics.compact_control_height, metrics.control_height)
        self.assertLess(metrics.input_padding_y * 2, metrics.control_height)
        self.assertGreaterEqual(metrics.tree_row_height, metrics.compact_control_height)
        self.assertEqual(metrics.card_gap, DEFAULT_THEME.spacing.md)
        self.assertEqual(metrics.card_padding, DEFAULT_THEME.spacing.lg)

    def test_semantic_aliases_follow_custom_theme_overrides(self) -> None:
        theme = replace(
            DesktopTheme(),
            surface="#ABCDEF",
            border_strong="#123456",
        )

        self.assertEqual(theme.card_background, "#ABCDEF")
        self.assertEqual(theme.control_background, "#ABCDEF")
        self.assertEqual(theme.control_border, "#123456")

    def test_core_text_and_status_colours_meet_accessible_contrast(self) -> None:
        theme = DEFAULT_THEME

        for foreground, background in (
            (theme.text, theme.surface),
            (theme.text_muted, theme.surface),
            (theme.text_faint, theme.surface),
            (theme.on_primary, theme.primary),
            (theme.on_navigation, theme.navigation),
            (theme.success, theme.surface),
            (theme.warning, theme.surface),
            (theme.danger, theme.surface),
            (theme.info, theme.surface),
        ):
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(_contrast_ratio(foreground, background), 4.5)


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(colour: str) -> float:
    channels = tuple(int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5))
    linear = tuple(
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


if __name__ == "__main__":
    unittest.main()
