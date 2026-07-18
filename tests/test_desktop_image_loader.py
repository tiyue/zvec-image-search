from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from zvec_desktop.image_geometry import ImageFitMode
from zvec_desktop.image_loader import (
    AsyncImageLoader,
    BoundedImageLoader,
    ImageLoadErrorCode,
)


class DesktopImageLoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="zvec-desktop-image-loader-"
        )
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_image(
        self, name: str, size: tuple[int, int] = (80, 40), color: str = "purple"
    ) -> Path:
        path = self.root / name
        with Image.new("RGB", size, color) as image:
            image.save(path)
        return path

    def test_load_returns_detached_exact_size_image(self) -> None:
        path = self._write_image("preview.png")
        loader = BoundedImageLoader()

        result = loader.load(path, (120, 120), ImageFitMode.CONTAIN)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.source_size, (80, 40))
        self.assertEqual(result.oriented_size, (80, 40))
        self.assertEqual(result.image_format, "PNG")
        assert result.image is not None
        self.assertEqual(result.image.size, (120, 120))

        # Deletion proves Pillow no longer owns an open file handle on Windows.
        path.unlink()
        self.assertEqual(result.image.getpixel((60, 60))[3], 255)
        result.image.close()
        loader.close()

    def test_exif_orientation_is_applied_before_fitting(self) -> None:
        path = self.root / "rotated.jpg"
        with Image.new("RGB", (40, 20), "red") as image:
            exif = Image.Exif()
            exif[274] = 6  # Rotate 90 degrees clockwise for display.
            image.save(path, exif=exif)

        with BoundedImageLoader() as loader:
            result = loader.load(path, (20, 40), ImageFitMode.CONTAIN)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.source_size, (40, 20))
        self.assertEqual(result.oriented_size, (20, 40))
        self.assertEqual(result.exif_orientation, 6)
        assert result.image is not None
        self.assertEqual(result.image.size, (20, 40))
        result.image.close()

    def test_mirrored_exif_orientation_is_applied_to_pixels(self) -> None:
        path = self.root / "mirrored.jpg"
        with Image.new("RGB", (80, 40), "red") as image:
            for x in range(40, 80):
                for y in range(40):
                    image.putpixel((x, y), (0, 0, 255))
            exif = Image.Exif()
            exif[274] = 2  # Mirror horizontally for display.
            image.save(path, quality=100, subsampling=0, exif=exif)

        with BoundedImageLoader() as loader:
            result = loader.load(path, (80, 40), ImageFitMode.CONTAIN)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.exif_orientation, 2)
        assert result.image is not None
        try:
            left = result.image.getpixel((10, 20))
            right = result.image.getpixel((70, 20))
            self.assertGreater(left[2], left[0])
            self.assertGreater(right[0], right[2])
        finally:
            result.image.close()

    def test_bad_inputs_return_structured_failures(self) -> None:
        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"this is not an image")
        missing = self.root / "missing.jpg"
        loader = BoundedImageLoader()

        cases = (
            (loader.load(corrupt, (100, 100)), ImageLoadErrorCode.DECODE_FAILED),
            (loader.load(missing, (100, 100)), ImageLoadErrorCode.NOT_FOUND),
            (
                loader.load(corrupt, (0, 100)),
                ImageLoadErrorCode.INVALID_TARGET_SIZE,
            ),
            (
                loader.load(corrupt, (100, 100), "stretch"),
                ImageLoadErrorCode.INVALID_FIT_MODE,
            ),
        )
        for result, expected_code in cases:
            with self.subTest(code=expected_code):
                self.assertFalse(result.ok)
                self.assertIsNone(result.image)
                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.code, expected_code)
                self.assertTrue(result.error.message)

        loader.close()

    def test_pixel_and_file_limits_fail_before_unbounded_work(self) -> None:
        image_path = self._write_image("large.png", size=(20, 20))
        byte_path = self.root / "bytes.bin"
        byte_path.write_bytes(b"x" * 32)

        with BoundedImageLoader(
            max_decode_pixels=100,
            max_file_bytes=16,
        ) as loader:
            pixel_result = loader.load(image_path, (10, 10))
            byte_result = loader.load(byte_path, (10, 10))

        self.assertEqual(
            pixel_result.error and pixel_result.error.code,
            ImageLoadErrorCode.FILE_TOO_LARGE,
        )
        # Use a separate loader so the valid PNG reaches its pixel header check.
        with BoundedImageLoader(max_decode_pixels=100) as pixel_loader:
            pixel_result = pixel_loader.load(image_path, (10, 10))
        self.assertEqual(
            pixel_result.error and pixel_result.error.code,
            ImageLoadErrorCode.PIXEL_LIMIT_EXCEEDED,
        )
        self.assertEqual(
            byte_result.error and byte_result.error.code,
            ImageLoadErrorCode.FILE_TOO_LARGE,
        )

    def test_cache_is_thread_safe_and_returns_independent_images(self) -> None:
        path = self._write_image("cached.png", color="green")
        loader = BoundedImageLoader(cache_items=2, max_cache_pixels=20_000)

        first = loader.load(path, (64, 64), ImageFitMode.COVER)
        self.assertTrue(first.ok, first.error)
        assert first.image is not None
        first.image.putpixel((0, 0), (255, 0, 0, 255))

        barrier = threading.Barrier(8)

        def load_after_barrier(_index: int):
            barrier.wait(timeout=5)
            return loader.load(path, (64, 64), ImageFitMode.COVER)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(load_after_barrier, range(8)))

        try:
            self.assertTrue(all(result.ok for result in results))
            self.assertTrue(all(result.from_cache for result in results))
            for result in results:
                assert result.image is not None
                self.assertNotEqual(result.image.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertGreaterEqual(loader.cache_info.hits, 8)
            self.assertEqual(loader.cache_info.entries, 1)
        finally:
            first.image.close()
            for result in results:
                if result.image is not None:
                    result.image.close()
            loader.close()

    def test_cache_uses_lru_eviction_and_invalidates_changed_files(self) -> None:
        first_path = self._write_image("first.bmp", color="red")
        second_path = self._write_image("second.bmp", color="blue")
        loader = BoundedImageLoader(cache_items=1, max_cache_pixels=10_000)

        first = loader.load(first_path, (10, 10))
        second = loader.load(second_path, (10, 10))
        first_again = loader.load(first_path, (10, 10))
        self.assertFalse(first.from_cache)
        self.assertFalse(second.from_cache)
        self.assertFalse(first_again.from_cache)
        self.assertGreaterEqual(loader.cache_info.evictions, 2)

        old_modified = first_path.stat().st_mtime_ns
        with Image.new("RGB", (20, 20), "green") as changed:
            changed.save(first_path)
        os.utime(first_path, ns=(old_modified + 1_000_000, old_modified + 1_000_000))
        changed_result = loader.load(first_path, (10, 10))
        self.assertEqual(changed_result.source_size, (20, 20))
        self.assertFalse(changed_result.from_cache)

        for result in (first, second, first_again, changed_result):
            if result.image is not None:
                result.image.close()
        loader.close()

    def test_async_loader_runs_work_and_closes_without_ui_exceptions(self) -> None:
        path = self._write_image("async.png")
        async_loader = AsyncImageLoader(max_workers=2)
        futures = [async_loader.load(path, (50, 50), "cover") for _ in range(4)]
        results = [future.result(timeout=5) for future in futures]
        async_loader.shutdown()

        self.assertTrue(all(result.ok for result in results))
        for result in results:
            assert result.image is not None
            result.image.close()

        closed_result = async_loader.load(path, (50, 50)).result(timeout=1)
        self.assertEqual(
            closed_result.error and closed_result.error.code,
            ImageLoadErrorCode.LOADER_CLOSED,
        )

    def test_async_corrupt_image_does_not_prevent_later_valid_preview(self) -> None:
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not an image")
        valid = self._write_image("valid.png")
        async_loader = AsyncImageLoader(max_workers=2)

        broken_result = async_loader.load(corrupt, (40, 40)).result(timeout=5)
        valid_result = async_loader.load(valid, (40, 40)).result(timeout=5)
        async_loader.shutdown()

        self.assertEqual(
            broken_result.error and broken_result.error.code,
            ImageLoadErrorCode.DECODE_FAILED,
        )
        self.assertTrue(valid_result.ok, valid_result.error)
        assert valid_result.image is not None
        valid_result.image.close()


if __name__ == "__main__":
    unittest.main()
