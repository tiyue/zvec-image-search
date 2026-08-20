from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from image_vector_service.image_data_uri import (
    ImageDataUriError,
    ImageSourceChangedError,
    encode_image_data_uri,
)


class ImageDataUriTest(unittest.TestCase):
    def test_small_image_keeps_original_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "small.png"
            Image.new("RGB", (32, 24), (30, 80, 160)).save(path)
            original = path.read_bytes()

            result = encode_image_data_uri(
                path,
                max_source_bytes=1024 * 1024,
            )

        self.assertFalse(result.transformed)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(_decode_data_uri(result.data_uri), original)
        self.assertEqual(result.data_uri_bytes, len(result.data_uri.encode("ascii")))

    def test_base64_overflow_is_transcoded_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "large.png"
            noise = Image.effect_noise((1200, 900), 100).convert("RGB")
            noise.save(path, format="PNG")
            before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            source_size = path.stat().st_size
            limit = 350_000
            self.assertLess(source_size, 5 * 1024 * 1024)
            self.assertGreater(4 * ((source_size + 2) // 3), limit)

            result = encode_image_data_uri(
                path,
                max_source_bytes=5 * 1024 * 1024,
                max_data_uri_bytes=limit,
                max_output_dimension=1024,
            )

            after_hash = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertTrue(result.transformed)
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertLessEqual(result.data_uri_bytes, limit)
        self.assertEqual(before_hash, after_hash)
        with Image.open(BytesIO(_decode_data_uri(result.data_uri))) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertLessEqual(max(decoded.size), 1024)

    def test_transparent_image_is_composited_on_white(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "transparent.png"
            noise = Image.effect_noise((800, 800), 100).convert("RGB")
            alpha = Image.new("L", noise.size, 0)
            draw = ImageDraw.Draw(alpha)
            draw.rectangle((200, 200, 600, 600), fill=255)
            image = Image.merge("RGBA", (*noise.split(), alpha))
            image.save(path)
            result = encode_image_data_uri(
                path,
                max_source_bytes=4 * 1024 * 1024,
                max_data_uri_bytes=100_000,
            )

        self.assertTrue(result.transformed)
        with Image.open(BytesIO(_decode_data_uri(result.data_uri))) as decoded:
            corner = decoded.convert("RGB").getpixel((0, 0))
        self.assertGreater(min(corner), 240)

    def test_force_transcode_recompresses_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "force.png"
            Image.effect_noise((640, 480), 60).convert("RGB").save(path)
            before = path.read_bytes()
            before_stat = path.stat()

            result = encode_image_data_uri(
                path,
                max_source_bytes=4 * 1024 * 1024,
                max_data_uri_bytes=800_000,
                force_transcode=True,
            )

            after = path.read_bytes()
            after_stat = path.stat()

        self.assertTrue(result.transformed)
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertLessEqual(result.data_uri_bytes, 800_000)
        self.assertEqual(
            hashlib.sha256(before).digest(),
            hashlib.sha256(after).digest(),
        )
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_large_jpeg_uses_decoder_draft_before_pixel_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "high-resolution.jpg"
            Image.effect_noise((1024, 1024), 70).convert("RGB").save(
                path,
                format="JPEG",
                quality=95,
            )
            with patch(
                "image_vector_service.image_data_uri._MAX_DECODE_PIXELS",
                300_000,
            ):
                result = encode_image_data_uri(
                    path,
                    max_source_bytes=4 * 1024 * 1024,
                    max_data_uri_bytes=500_000,
                    max_output_dimension=512,
                    force_transcode=True,
                )

        self.assertTrue(result.transformed)
        self.assertLessEqual(result.width * result.height, 300_000)
        self.assertLessEqual(max(result.width, result.height), 512)

    def test_wide_and_tall_jpegs_use_aspect_ratio_draft_target(self) -> None:
        for width, height in ((1532, 1021), (1021, 1532)):
            with self.subTest(size=(width, height)):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "high-resolution.jpg"
                    Image.effect_noise((width, height), 70).convert("RGB").save(
                        path,
                        format="JPEG",
                        quality=95,
                    )
                    with patch(
                        "image_vector_service.image_data_uri._MAX_DECODE_PIXELS",
                        400_000,
                    ):
                        result = encode_image_data_uri(
                            path,
                            max_source_bytes=8 * 1024 * 1024,
                            max_data_uri_bytes=500_000,
                            max_output_dimension=512,
                            force_transcode=True,
                        )

                self.assertTrue(result.transformed)
                self.assertLessEqual(result.width * result.height, 400_000)
                self.assertLessEqual(max(result.width, result.height), 512)

    def test_request_overhead_reduces_available_image_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "request.png"
            Image.effect_noise((900, 700), 80).convert("RGB").save(path)
            result = encode_image_data_uri(
                path,
                max_source_bytes=4 * 1024 * 1024,
                max_data_uri_bytes=2 * 1024 * 1024,
                request_byte_budget=500_000,
                request_overhead_bytes=100_000,
            )

        self.assertLessEqual(result.data_uri_bytes, 400_000)

    def test_source_limit_and_impossible_request_budget_fail_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.png"
            Image.new("RGB", (64, 64), "navy").save(path)
            with self.assertRaises(ImageDataUriError):
                encode_image_data_uri(path, max_source_bytes=1)
            with self.assertRaises(ImageDataUriError):
                encode_image_data_uri(
                    path,
                    max_source_bytes=1024 * 1024,
                    request_byte_budget=100,
                    request_overhead_bytes=100,
                )

    def test_expected_sha_detects_same_size_replacement_with_preserved_mtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "stable-size.bmp"
            Image.new("RGB", (32, 24), (10, 20, 30)).save(path)
            original_payload = path.read_bytes()
            original_hash = hashlib.sha256(original_payload).hexdigest()
            original_stat = path.stat()

            Image.new("RGB", (32, 24), (220, 210, 200)).save(path)
            os.utime(
                path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            self.assertEqual(path.stat().st_size, len(original_payload))
            self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)

            with self.assertRaises(ImageSourceChangedError):
                encode_image_data_uri(
                    path,
                    max_source_bytes=1024 * 1024,
                    expected_source_sha256=original_hash,
                )

    def test_expected_sha_accepts_and_encodes_the_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "verified.png"
            Image.new("RGB", (24, 24), (30, 60, 90)).save(path)
            payload = path.read_bytes()

            result = encode_image_data_uri(
                path,
                max_source_bytes=1024 * 1024,
                expected_source_sha256=hashlib.sha256(payload).hexdigest(),
            )

        self.assertEqual(_decode_data_uri(result.data_uri), payload)


def _decode_data_uri(value: str) -> bytes:
    _prefix, encoded = value.split(",", 1)
    return base64.b64decode(encoded, validate=True)


if __name__ == "__main__":
    unittest.main()
