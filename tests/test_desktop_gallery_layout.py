from __future__ import annotations

import unittest

from zvec_desktop.gallery_layout import (
    DEFAULT_GALLERY_COLUMNS,
    DEFAULT_GALLERY_PAGE_SIZE,
    DEFAULT_GALLERY_ROWS,
    calculate_gallery_layout,
)


class DesktopGalleryLayoutTest(unittest.TestCase):
    def test_default_page_fills_a_normal_five_by_three_viewport(self) -> None:
        # 633x438 is the measured gallery viewport inside a 1280x850 main window.
        layout = calculate_gallery_layout(633, 438, DEFAULT_GALLERY_PAGE_SIZE)

        self.assertEqual(DEFAULT_GALLERY_PAGE_SIZE, 15)
        self.assertEqual(
            (layout.columns, layout.rows),
            (DEFAULT_GALLERY_COLUMNS, DEFAULT_GALLERY_ROWS),
        )
        self.assertFalse(layout.scroll_required)
        self.assertLessEqual(layout.content_height, 438)

    def test_narrow_window_reflows_without_horizontal_overflow(self) -> None:
        layout = calculate_gallery_layout(340, 300, DEFAULT_GALLERY_PAGE_SIZE)

        self.assertEqual(layout.columns, 2)
        self.assertEqual(layout.rows, 8)
        self.assertTrue(layout.scroll_required)
        self.assertLessEqual(layout.card_width, 340)

        extremely_narrow = calculate_gallery_layout(
            72,
            300,
            DEFAULT_GALLERY_PAGE_SIZE,
        )
        self.assertEqual(extremely_narrow.columns, 1)
        self.assertLessEqual(extremely_narrow.card_width, 72)
        self.assertTrue(extremely_narrow.scroll_required)

    def test_scaled_normal_window_still_fits_five_by_three(self) -> None:
        layout = calculate_gallery_layout(600, 380, DEFAULT_GALLERY_PAGE_SIZE)

        self.assertEqual((layout.columns, layout.rows), (5, 3))
        self.assertGreaterEqual(layout.card_height, 112)
        self.assertFalse(layout.scroll_required)

    def test_short_window_keeps_readable_cards_and_enables_scroll(self) -> None:
        layout = calculate_gallery_layout(600, 320, DEFAULT_GALLERY_PAGE_SIZE)

        self.assertEqual((layout.columns, layout.rows), (5, 3))
        self.assertGreaterEqual(layout.card_height, 154)
        self.assertTrue(layout.scroll_required)

    def test_empty_gallery_has_no_synthetic_row_or_scrollbar(self) -> None:
        layout = calculate_gallery_layout(600, 590, 0)

        self.assertEqual(layout.rows, 0)
        self.assertEqual(layout.card_height, 0)
        self.assertEqual(layout.content_height, 0)
        self.assertFalse(layout.scroll_required)

    def test_invalid_values_are_rejected_instead_of_guessing(self) -> None:
        invalid_calls: tuple[tuple[int, int, int, dict[str, int]], ...] = (
            (0, 590, 15, {}),
            (600, 590, -1, {}),
            (600, 590, 15, {"target_rows": 0}),
            (600, 590, 15, {"max_columns": True}),
        )
        for width, height, count, options in invalid_calls:
            with self.subTest(options=options), self.assertRaises(ValueError):
                calculate_gallery_layout(width, height, count, **options)


if __name__ == "__main__":
    unittest.main()
