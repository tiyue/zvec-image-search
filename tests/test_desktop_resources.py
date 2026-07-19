from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zvec_desktop import resources
from zvec_desktop.theme import DesktopTypography


class DesktopResourceTest(unittest.TestCase):
    def test_frozen_resource_root_has_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = root / "assets" / "test-resource.bin"
            resource.parent.mkdir()
            resource.write_bytes(b"resource")
            with (
                mock.patch.object(resources.sys, "_MEIPASS", str(root), create=True),
                mock.patch.object(resources.sys, "frozen", False, create=True),
            ):
                self.assertEqual(
                    resources.find_resource("assets/test-resource.bin"), resource
                )

    def test_missing_icon_is_safe_and_traversal_is_rejected(self) -> None:
        window = mock.Mock()
        with mock.patch.object(resources, "find_resource", return_value=None):
            self.assertIsNone(resources.apply_application_icon(window))
        window.iconbitmap.assert_not_called()

        with self.assertRaisesRegex(ValueError, "relative"):
            resources.find_resource("../outside.ico")

    def test_application_icon_is_applied_as_the_window_default(self) -> None:
        window = mock.Mock()
        icon = Path("C:/application/Zvec.AppIcon.ico")
        with mock.patch.object(resources, "find_resource", return_value=icon):
            self.assertEqual(resources.apply_application_icon(window), icon)

        window.iconbitmap.assert_called_once_with(default=str(icon))

    def test_ui_font_prefers_installed_windows_chinese_family(self) -> None:
        window = mock.Mock()
        typography = DesktopTypography(
            family="Preferred UI",
            fallback_families=("Fallback UI", "Arial"),
        )
        with mock.patch.object(
            resources.tkfont,
            "families",
            return_value=("Arial", "fallback ui"),
        ):
            self.assertEqual(
                resources.resolve_ui_font_family(window, typography),
                "fallback ui",
            )

    def test_ui_font_resolution_failure_keeps_preferred_family(self) -> None:
        typography = DesktopTypography(family="Preferred UI")
        with mock.patch.object(
            resources.tkfont,
            "families",
            side_effect=resources.tk.TclError("headless"),
        ):
            self.assertEqual(
                resources.resolve_ui_font_family(mock.Mock(), typography),
                "Preferred UI",
            )

    def test_tk_scaling_uses_reported_dpi_and_clamps_outliers(self) -> None:
        window = mock.Mock()
        window.winfo_fpixels.return_value = 144.0

        self.assertEqual(resources.configure_tk_scaling(window), 2.0)
        window.tk.call.assert_called_once_with("tk", "scaling", 2.0)

        window.reset_mock()
        window.winfo_fpixels.return_value = 1000.0
        self.assertEqual(
            resources.configure_tk_scaling(window), resources.MAXIMUM_TK_SCALING
        )

    def test_tk_scaling_failure_is_safe(self) -> None:
        window = mock.Mock()
        window.winfo_fpixels.side_effect = resources.tk.TclError("display closed")

        self.assertIsNone(resources.configure_tk_scaling(window))
        window.tk.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
