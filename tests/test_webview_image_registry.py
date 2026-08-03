from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import zvec_webview.image_registry as image_registry_module
from zvec_webview.image_registry import ImageRegistry, ImageRegistryError


def _image(path: Path, *, size: tuple[int, int] = (1200, 800)) -> Path:
    Image.new("RGB", size, (64, 96, 180)).save(path, format="JPEG")
    return path


def _decoded_size(content: bytes) -> tuple[int, int]:
    """Decode a generated payload so tests verify what the browser receives."""

    with Image.open(io.BytesIO(content)) as image:
        image.load()
        return image.size


class ImageRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_returns_opaque_id_and_bounded_variants(self) -> None:
        source = _image(self.root / "portrait.jpg")
        registry = ImageRegistry(max_entries=10, max_cache_bytes=4 * 1024 * 1024)

        metadata = registry.register(source)

        self.assertEqual(metadata.name, "portrait.jpg")
        self.assertNotIn(str(source), metadata.image_id)
        self.assertNotIn(source.name, metadata.image_id)
        self.assertEqual((metadata.width, metadata.height), (1200, 800))
        self.assertEqual(registry.resolve(metadata.image_id), source.resolve())

        thumbnail = registry.payload(metadata.image_id, "thumbnail")
        preview = registry.payload(metadata.image_id, "preview")
        self.assertEqual(thumbnail.content_type, "image/jpeg")
        self.assertEqual(preview.content_type, "image/jpeg")
        self.assertTrue(thumbnail.content.startswith(b"\xff\xd8"))
        self.assertTrue(preview.content.startswith(b"\xff\xd8"))
        self.assertIs(registry.payload(metadata.image_id, "thumbnail"), thumbnail)

    def test_landscape_portrait_and_square_variants_preserve_aspect_ratio(self) -> None:
        """Thumbnail generation must scale to fit and never crop to a fixed box."""

        registry = ImageRegistry(max_entries=10, max_cache_bytes=32 * 1024 * 1024)
        cases = {
            "landscape": {
                "source": (2400, 1200),
                "thumbnail": (640, 320),
                "preview": (1920, 960),
            },
            "portrait": {
                "source": (1200, 2400),
                "thumbnail": (320, 640),
                "preview": (960, 1920),
            },
            "square": {
                "source": (2200, 2200),
                "thumbnail": (640, 640),
                "preview": (1920, 1920),
            },
        }

        for name, dimensions in cases.items():
            source_size = dimensions["source"]
            source = _image(self.root / f"{name}.jpg", size=source_size)
            image_id = registry.register(source).image_id

            for variant in ("thumbnail", "preview"):
                with self.subTest(shape=name, variant=variant):
                    actual_size = _decoded_size(
                        registry.payload(image_id, variant).content
                    )
                    self.assertEqual(actual_size, dimensions[variant])
                    self.assertAlmostEqual(
                        actual_size[0] / actual_size[1],
                        source_size[0] / source_size[1],
                        places=6,
                    )

    def test_registry_rejects_missing_or_expired_images(self) -> None:
        registry = ImageRegistry()
        with self.assertRaises(ImageRegistryError):
            registry.register(self.root / "missing.jpg")

        source = _image(self.root / "gone.jpg", size=(32, 32))
        image_id = registry.register(source).image_id
        source.unlink()
        with self.assertRaises(ImageRegistryError):
            registry.resolve(image_id)

    def test_registry_eviction_invalidates_oldest_identifier(self) -> None:
        registry = ImageRegistry(max_entries=1)
        first = registry.register(_image(self.root / "first.jpg", size=(20, 20)))
        second = registry.register(_image(self.root / "second.jpg", size=(20, 20)))

        with self.assertRaises(ImageRegistryError):
            registry.resolve(first.image_id)
        self.assertEqual(registry.resolve(second.image_id).name, "second.jpg")

    def test_unchanged_registration_reuses_dimensions_and_changed_file_refreshes(
        self,
    ) -> None:
        source = _image(self.root / "metadata.jpg", size=(320, 480))
        registry = ImageRegistry()

        with patch.object(
            image_registry_module,
            "_image_dimensions",
            wraps=image_registry_module._image_dimensions,
        ) as dimensions:
            first = registry.register(source)
            second = registry.register(source)
            self.assertEqual(dimensions.call_count, 1)
            self.assertEqual(first.image_id, second.image_id)

            time.sleep(0.01)
            _image(source, size=(640, 360))
            source.touch()
            refreshed = registry.register(source)

        self.assertEqual(refreshed.image_id, first.image_id)
        self.assertEqual((refreshed.width, refreshed.height), (640, 360))
        self.assertEqual(dimensions.call_count, 2)

    def test_persistent_thumbnail_cache_survives_restart_and_recovers_corruption(
        self,
    ) -> None:
        source = _image(self.root / "persistent.jpg", size=(1600, 900))
        cache = self.root / "preview-cache"
        first_registry = ImageRegistry(cache_directory=cache)
        first_id = first_registry.register(source).image_id
        first = first_registry.payload(first_id, "thumbnail")
        cache_files = tuple(cache.rglob("*.cache"))
        self.assertEqual(len(cache_files), 1)
        self.assertNotIn(source.name, str(cache_files[0]))

        second_registry = ImageRegistry(cache_directory=cache)
        second_id = second_registry.register(source).image_id
        with patch.object(
            image_registry_module,
            "_render_image",
            side_effect=AssertionError("disk cache should avoid rendering"),
        ):
            second = second_registry.payload(second_id, "thumbnail")
        self.assertEqual(second.content, first.content)

        cache_files[0].write_bytes(b"corrupt")
        third_registry = ImageRegistry(cache_directory=cache)
        third_id = third_registry.register(source).image_id
        with patch.object(
            image_registry_module,
            "_render_image",
            wraps=image_registry_module._render_image,
        ) as render:
            recovered = third_registry.payload(third_id, "thumbnail")
        self.assertEqual(render.call_count, 1)
        self.assertTrue(recovered.content.startswith(b"\xff\xd8"))

    def test_same_thumbnail_request_is_coalesced_across_threads(self) -> None:
        source = _image(self.root / "coalesced.jpg", size=(1200, 800))
        registry = ImageRegistry(max_render_workers=3)
        image_id = registry.register(source).image_id
        render_count = 0
        count_lock = threading.Lock()
        callers_ready = threading.Barrier(9)
        render_started = threading.Event()
        release_render = threading.Event()
        real_render = image_registry_module._render_image

        def blocked_render(path, variant, mtime_ns):
            nonlocal render_count
            with count_lock:
                render_count += 1
            render_started.set()
            self.assertTrue(release_render.wait(timeout=2))
            return real_render(path, variant, mtime_ns)

        def request_payload(_index):
            callers_ready.wait(timeout=2)
            return registry.payload(image_id, "thumbnail")

        with (
            patch.object(image_registry_module, "_render_image", blocked_render),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            futures = tuple(
                executor.submit(request_payload, index) for index in range(8)
            )
            callers_ready.wait(timeout=2)
            self.assertTrue(render_started.wait(timeout=2))
            release_render.set()
            payloads = tuple(future.result(timeout=2) for future in futures)

        self.assertEqual(render_count, 1)
        self.assertEqual(len({payload.content for payload in payloads}), 1)

    def test_render_concurrency_and_disk_cache_lru_are_bounded(self) -> None:
        cache = self.root / "bounded-cache"
        registry = ImageRegistry(
            cache_directory=cache,
            max_render_workers=2,
            max_disk_cache_entries=2,
        )
        image_ids = [
            registry.register(
                _image(self.root / f"bounded-{index}.jpg", size=(900, 600))
            ).image_id
            for index in range(4)
        ]
        active = 0
        maximum_active = 0
        active_lock = threading.Lock()
        two_renders_started = threading.Event()
        release_renders = threading.Event()
        real_render = image_registry_module._render_image

        def tracked_render(path, variant, mtime_ns):
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_renders_started.set()
            try:
                self.assertTrue(release_renders.wait(timeout=2))
                return real_render(path, variant, mtime_ns)
            finally:
                with active_lock:
                    active -= 1

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            ThreadPoolExecutor(max_workers=4) as executor,
        ):
            futures = tuple(
                executor.submit(registry.payload, image_id, "thumbnail")
                for image_id in image_ids
            )
            self.assertTrue(two_renders_started.wait(timeout=2))
            with active_lock:
                self.assertEqual(active, 2)
                self.assertEqual(maximum_active, 2)
            release_renders.set()
            tuple(future.result(timeout=2) for future in futures)

        self.assertEqual(maximum_active, 2)
        self.assertEqual(len(tuple(cache.rglob("*.cache"))), 4)
        registry.prune_disk_cache()
        self.assertLessEqual(len(tuple(cache.rglob("*.cache"))), 2)

    def test_render_failure_releases_waiters_and_next_request_can_retry(self) -> None:
        source = _image(self.root / "retry.jpg", size=(800, 600))
        registry = ImageRegistry(max_render_workers=2)
        image_id = registry.register(source).image_id
        callers_ready = threading.Barrier(7)
        render_started = threading.Event()
        release_render = threading.Event()

        def fail_render(_path, _variant, _mtime_ns):
            render_started.set()
            self.assertTrue(release_render.wait(timeout=2))
            raise ImageRegistryError("temporary render failure")

        def request_payload():
            callers_ready.wait(timeout=2)
            return registry.payload(image_id, "thumbnail")

        with (
            patch.object(image_registry_module, "_render_image", fail_render),
            ThreadPoolExecutor(max_workers=6) as executor,
        ):
            futures = [executor.submit(request_payload) for _index in range(6)]
            callers_ready.wait(timeout=2)
            self.assertTrue(render_started.wait(timeout=2))
            release_render.set()
            for future in futures:
                with self.assertRaisesRegex(
                    ImageRegistryError, "temporary render failure"
                ):
                    future.result(timeout=2)

        recovered = registry.payload(image_id, "thumbnail")
        self.assertTrue(recovered.content.startswith(b"\xff\xd8"))

    def test_forget_during_render_prevents_memory_cache_resurrection(self) -> None:
        source = _image(self.root / "forgotten.jpg", size=(800, 600))
        registry = ImageRegistry()
        image_id = registry.register(source).image_id
        started = threading.Event()
        release = threading.Event()
        real_render = image_registry_module._render_image

        def blocked_render(path, variant, mtime_ns):
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return real_render(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", blocked_render),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(registry.payload, image_id, "thumbnail")
            self.assertTrue(started.wait(timeout=2))
            registry.forget(image_id)
            release.set()
            with self.assertRaisesRegex(ImageRegistryError, "引用已失效"):
                future.result(timeout=2)

        self.assertEqual(len(registry._cache), 0)
        self.assertEqual(registry._cache_bytes, 0)

    def test_source_change_during_render_is_retried_without_stale_disk_entry(
        self,
    ) -> None:
        source = _image(self.root / "changing.jpg", size=(800, 600))
        cache = self.root / "changing-cache"
        registry = ImageRegistry(cache_directory=cache)
        image_id = registry.register(source).image_id
        real_render = image_registry_module._render_image
        render_count = 0

        def changing_render(path, variant, mtime_ns):
            nonlocal render_count
            render_count += 1
            payload = real_render(path, variant, mtime_ns)
            if render_count == 1:
                time.sleep(0.01)
                _image(source, size=(640, 960))
                source.touch()
            return payload

        with patch.object(image_registry_module, "_render_image", changing_render):
            payload = registry.payload(image_id, "thumbnail")

        self.assertEqual(render_count, 2)
        self.assertTrue(payload.content)
        self.assertEqual(len(tuple(cache.rglob("*.cache"))), 1)

    def test_exif_dimensions_do_not_decode_full_image(self) -> None:
        source = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (320, 640), (10, 20, 30)).save(source, exif=exif)
        registry = ImageRegistry()

        with patch(
            "PIL.JpegImagePlugin.JpegImageFile.load",
            side_effect=AssertionError("dimension read must not decode pixels"),
        ):
            metadata = registry.register(source)

        self.assertEqual((metadata.width, metadata.height), (640, 320))

    def test_normalized_image_id_reuses_the_same_memory_payload(self) -> None:
        source = _image(self.root / "normalized-id.jpg", size=(640, 480))
        registry = ImageRegistry()
        image_id = registry.register(source).image_id

        first = registry.payload(image_id, "thumbnail")
        second = registry.payload(f"  {image_id}  ", "thumbnail")

        self.assertIs(second, first)
        self.assertEqual(len(registry._cache), 1)

    def test_atomic_cache_write_cleans_temporary_file_after_replace_failure(
        self,
    ) -> None:
        target = self.root / "atomic" / "aa" / "payload.cache"
        with patch.object(
            image_registry_module.os,
            "replace",
            side_effect=OSError("replace failed"),
        ):
            written = image_registry_module._write_disk_payload(
                target, b"replace-safe-cache"
            )

        self.assertFalse(written)
        self.assertFalse(target.exists())
        self.assertEqual(tuple(target.parent.glob("*.tmp")), ())

    def test_two_registries_can_atomically_publish_the_same_thumbnail(self) -> None:
        source = _image(self.root / "shared-cache.jpg", size=(1400, 900))
        cache = self.root / "shared-cache-root"
        first = ImageRegistry(cache_directory=cache)
        second = ImageRegistry(cache_directory=cache)
        first_id = first.register(source).image_id
        second_id = second.register(source).image_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            payloads = tuple(
                executor.map(
                    lambda request: request[0].payload(request[1], "thumbnail"),
                    ((first, first_id), (second, second_id)),
                )
            )

        self.assertEqual(payloads[0].content, payloads[1].content)
        self.assertEqual(len(tuple(cache.rglob("*.cache"))), 1)
        restarted = ImageRegistry(cache_directory=cache)
        restarted_id = restarted.register(source).image_id
        with patch.object(
            image_registry_module,
            "_render_image",
            side_effect=AssertionError("published cache should be reusable"),
        ):
            self.assertTrue(restarted.payload(restarted_id, "thumbnail").content)
        restarted.close()


if __name__ == "__main__":
    unittest.main()
