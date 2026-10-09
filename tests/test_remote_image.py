from __future__ import annotations

import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

import zvec_webview.remote_image as remote_image
from zvec_webview.image_registry import ImageRegistryError
from zvec_webview.remote_image import encode_remote_media

_THUMBNAIL_LONG_EDGE = 640
_THUMBNAIL_TARGET = 80 * 1024
_PREVIEW_LONG_EDGE = 1600
_PREVIEW_TARGET = 500 * 1024
_HARD_CAP = 1024 * 1024


def _noisy_photo(path: Path, size: tuple[int, int], *, seed: int = 7) -> Path:
    """Write a deterministic, photo-like high-entropy JPEG."""

    data = random.Random(seed).randbytes(size[0] * size[1] * 3)
    Image.frombytes("RGB", size, data).save(path, format="JPEG", quality=95)
    return path


def _decoded(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    image.load()
    return image


class RemoteImageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_noisy_photo_meets_target_without_upscaling(self) -> None:
        source = _noisy_photo(self.root / "photo.jpg", (1200, 800))

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        self.assertLessEqual(len(encoding.content), _THUMBNAIL_TARGET)
        self.assertLessEqual(len(encoding.content), _HARD_CAP)
        self.assertEqual(encoding.content_type, "image/jpeg")
        with _decoded(encoding.content) as rendered:
            self.assertLessEqual(max(rendered.size), _THUMBNAIL_LONG_EDGE)
            self.assertLessEqual(rendered.size[0], 1200)
            self.assertLessEqual(rendered.size[1], 800)

    def test_preview_budget_and_dimensions(self) -> None:
        source = _noisy_photo(self.root / "preview.jpg", (1800, 1200))

        encoding = encode_remote_media(
            source,
            long_edge=_PREVIEW_LONG_EDGE,
            target_bytes=_PREVIEW_TARGET,
        )

        self.assertLessEqual(len(encoding.content), _PREVIEW_TARGET)
        with _decoded(encoding.content) as rendered:
            self.assertLessEqual(max(rendered.size), _PREVIEW_LONG_EDGE)

    def test_transparent_png_preserves_alpha(self) -> None:
        source = self.root / "split.png"
        image = Image.new("RGBA", (2000, 1000), (0, 0, 0, 0))
        for x in range(1000, 2000):
            for y in range(1000):
                image.putpixel((x, y), (255, 0, 0, 255))
        image.save(source)

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        self.assertIn(encoding.content_type, {"image/webp", "image/png"})
        with _decoded(encoding.content) as rendered:
            rendered = rendered.convert("RGBA")
            width, height = rendered.size
            self.assertEqual(rendered.getpixel((20, 20))[3], 0)
            self.assertEqual(rendered.getpixel((width - 20, 20)), (255, 0, 0, 255))

    def test_flat_geometry_stays_lossless(self) -> None:
        source = self.root / "text.png"
        image = Image.new("RGB", (1800, 900), (255, 255, 255))
        for x in range(0, 1800, 3):
            for y in range(900):
                image.putpixel((x, y), (0, 0, 0))
        image.save(source)

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        self.assertIn(encoding.content_type, {"image/webp", "image/png"})
        self.assertNotEqual(encoding.content_type, "image/jpeg")

    def test_tiny_conforming_source_is_reused_verbatim(self) -> None:
        source = self.root / "tiny.jpg"
        Image.new("RGB", (80, 60), (10, 20, 30)).save(source, format="JPEG")
        original_bytes = source.read_bytes()

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        self.assertEqual(encoding.content, original_bytes)
        self.assertEqual(encoding.content_type, "image/jpeg")

    def test_oversized_source_is_reencoded_even_when_small_below_target(self) -> None:
        source = self.root / "big.jpg"
        Image.new("RGB", (1280, 960), (200, 200, 200)).save(source, quality=20)
        self.assertLess(source.stat().st_size, _THUMBNAIL_TARGET)

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        self.assertNotEqual(encoding.content, source.read_bytes())
        with _decoded(encoding.content) as rendered:
            self.assertLessEqual(max(rendered.size), _THUMBNAIL_LONG_EDGE)

    def test_exif_orientation_is_baked_into_output(self) -> None:
        source = self.root / "oriented.jpg"
        image = Image.new("RGB", (400, 200), (120, 60, 30))
        for x in range(0, 400, 4):
            for y in range(200):
                image.putpixel((x, y), (0, 0, 0))
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        with _decoded(encoding.content) as rendered:
            self.assertEqual(rendered.size, (200, 400))
            self.assertEqual(int(rendered.getexif().get(274, 1) or 1), 1)

    def test_never_upscales_a_small_non_conforming_source(self) -> None:
        source = self.root / "small-oriented.jpg"
        image = Image.new("RGB", (100, 60), (40, 80, 120))
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        with _decoded(encoding.content) as rendered:
            self.assertEqual(rendered.size, (60, 100))

    def test_icc_profile_is_preserved(self) -> None:
        source = self.root / "icc.jpg"
        icc = bytes(range(256)) * 4
        Image.new("RGB", (1200, 900), (30, 90, 150)).save(
            source,
            quality=90,
            icc_profile=icc,
        )

        encoding = encode_remote_media(
            source,
            long_edge=_THUMBNAIL_LONG_EDGE,
            target_bytes=_THUMBNAIL_TARGET,
        )

        with _decoded(encoding.content) as rendered:
            self.assertEqual(rendered.info.get("icc_profile"), icc)

    def test_lab_conversion_embeds_rgb_profile(self) -> None:
        source = self.root / "lab.tif"
        input_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
        Image.new("LAB", (800, 600), (128, 128, 128)).save(
            source, icc_profile=input_profile.tobytes()
        )
        encoding = encode_remote_media(source, long_edge=640, target_bytes=80 * 1024)
        with _decoded(encoding.content) as rendered:
            output_profile = ImageCms.ImageCmsProfile(
                io.BytesIO(rendered.info["icc_profile"])
            )
            self.assertEqual(rendered.mode, "RGB")
            self.assertEqual(output_profile.profile.xcolor_space.strip(), "RGB")
            self.assertLessEqual(abs(rendered.getpixel((0, 0))[0] - 119), 2)

    def test_encoding_attempts_are_bounded_and_quality_has_a_floor(self) -> None:
        source = _noisy_photo(self.root / "attempts.jpg", (1400, 1000))
        calls: list[tuple[str, int | None]] = []
        original = remote_image._encode

        def counting(image: Image.Image, fmt: str, *, quality, icc_profile):
            calls.append((fmt, quality))
            return original(image, fmt, quality=quality, icc_profile=icc_profile)

        with patch.object(remote_image, "_encode", counting):
            encode_remote_media(
                source,
                long_edge=_THUMBNAIL_LONG_EDGE,
                target_bytes=_THUMBNAIL_TARGET,
            )

        self.assertLessEqual(len(calls), 6)
        self.assertTrue(calls)
        for fmt, quality in calls:
            self.assertEqual(fmt, "JPEG")
            self.assertIsNotNone(quality)
            assert quality is not None
            self.assertGreaterEqual(quality, 74)

    def test_hard_cap_failure_raises_a_clear_error(self) -> None:
        source = _noisy_photo(self.root / "huge.jpg", (1600, 1200))

        with self.assertRaises(ImageRegistryError):
            encode_remote_media(
                source,
                long_edge=_PREVIEW_LONG_EDGE,
                target_bytes=1,
                hard_cap_bytes=4096,
            )

    def test_hard_cap_is_reachable_for_difficult_targets(self) -> None:
        source = _noisy_photo(self.root / "reachable.jpg", (1600, 1200))

        encoding = encode_remote_media(
            source,
            long_edge=_PREVIEW_LONG_EDGE,
            target_bytes=1,
            hard_cap_bytes=_HARD_CAP,
        )

        self.assertLessEqual(len(encoding.content), _HARD_CAP)

    def test_corrupt_source_raises_image_registry_error(self) -> None:
        source = self.root / "broken.jpg"
        source.write_bytes(b"definitely not an image")

        with self.assertRaises(ImageRegistryError):
            encode_remote_media(
                source,
                long_edge=_THUMBNAIL_LONG_EDGE,
                target_bytes=_THUMBNAIL_TARGET,
            )

    def test_invalid_bounds_are_rejected(self) -> None:
        source = _noisy_photo(self.root / "bounds.jpg", (200, 200))

        with self.assertRaises(ValueError):
            encode_remote_media(source, long_edge=0, target_bytes=1024)
        with self.assertRaises(ValueError):
            encode_remote_media(source, long_edge=640, target_bytes=0)


if __name__ == "__main__":
    unittest.main()
