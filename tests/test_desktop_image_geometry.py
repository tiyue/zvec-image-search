from __future__ import annotations

import unittest

from PIL import Image

from zvec_desktop.image_geometry import (
    ImageFitMode,
    ImageGeometryError,
    calculate_image_layout,
    fit_image,
)


class ImageGeometryTest(unittest.TestCase):
    def test_contain_keeps_the_entire_source_and_centers_letterbox(self) -> None:
        layout = calculate_image_layout((200, 100), (100, 100), ImageFitMode.CONTAIN)

        self.assertEqual(layout.source_crop.as_box(), (0.0, 0.0, 200.0, 100.0))
        self.assertEqual(layout.destination.x, 0.0)
        self.assertEqual(layout.destination.y, 25.0)
        self.assertEqual(layout.destination.width, 100.0)
        self.assertEqual(layout.destination.height, 50.0)
        self.assertTrue(layout.is_letterboxed)
        self.assertFalse(layout.is_cropped)

    def test_cover_fills_target_with_a_centered_source_crop(self) -> None:
        layout = calculate_image_layout((200, 100), (100, 100), "cover")

        self.assertEqual(layout.source_crop.as_box(), (50.0, 0.0, 150.0, 100.0))
        self.assertEqual(layout.destination.as_box(), (0.0, 0.0, 100.0, 100.0))
        self.assertTrue(layout.is_cropped)
        self.assertFalse(layout.is_letterboxed)

    def test_contain_render_preserves_both_edges_of_the_full_image(self) -> None:
        source = Image.new("RGB", (200, 100), "red")
        for x in range(100, 200):
            for y in range(100):
                source.putpixel((x, y), (0, 0, 255))

        rendered = fit_image(source, (100, 100), ImageFitMode.CONTAIN)
        try:
            self.assertEqual(rendered.size, (100, 100))
            self.assertEqual(rendered.getpixel((0, 0))[3], 0)
            self.assertGreater(rendered.getpixel((5, 50))[0], 200)
            self.assertGreater(rendered.getpixel((94, 50))[2], 200)
        finally:
            rendered.close()
            source.close()

    def test_cover_render_has_no_geometric_letterbox(self) -> None:
        source = Image.new("RGB", (200, 100), (20, 180, 70))
        rendered = fit_image(source, (75, 120), ImageFitMode.COVER)
        try:
            self.assertEqual(rendered.size, (75, 120))
            self.assertTrue(
                all(rendered.getpixel(point)[3] == 255 for point in ((0, 0), (74, 119)))
            )
        finally:
            rendered.close()
            source.close()

    def test_fit_supports_palette_images_without_mutating_the_source(self) -> None:
        source = Image.new("P", (160, 80), 3)
        original_size = source.size
        original_mode = source.mode

        contained = fit_image(source, (80, 80), ImageFitMode.CONTAIN)
        covered = fit_image(source, (80, 80), ImageFitMode.COVER)
        try:
            self.assertEqual(contained.mode, "RGBA")
            self.assertEqual(covered.mode, "RGBA")
            self.assertEqual(contained.size, (80, 80))
            self.assertEqual(covered.size, (80, 80))
            self.assertEqual(source.size, original_size)
            self.assertEqual(source.mode, original_mode)
        finally:
            contained.close()
            covered.close()
            source.close()

    def test_invalid_or_dangerously_large_dimensions_are_rejected(self) -> None:
        for source, target in (
            ((0, 10), (100, 100)),
            ((10, 10), (0, 100)),
            ((1_000_001, 1), (100, 100)),
        ):
            with (
                self.subTest(source=source, target=target),
                self.assertRaises(ImageGeometryError),
            ):
                calculate_image_layout(source, target, ImageFitMode.CONTAIN)

        image = Image.new("RGB", (1, 1))
        try:
            with self.assertRaisesRegex(ImageGeometryError, "safe render limit"):
                fit_image(image, (32_768, 2_000), ImageFitMode.CONTAIN)
        finally:
            image.close()

    def test_invalid_mode_is_not_silently_changed(self) -> None:
        with self.assertRaisesRegex(ImageGeometryError, "unsupported"):
            calculate_image_layout((10, 10), (20, 20), "stretch")


if __name__ == "__main__":
    unittest.main()
