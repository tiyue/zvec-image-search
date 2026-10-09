from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import zvec_webview.image_registry as image_registry_module
from zvec_webview.image_registry import (
    ImageQueueFull,
    ImageRegistry,
    ImageRegistryError,
)
from zvec_webview.image_scheduler import PriorityGate


def _image(path: Path, *, size: tuple[int, int] = (1200, 800)) -> Path:
    Image.new("RGB", size, (64, 96, 180)).save(path, format="JPEG")
    return path


def _decoded_size(content: bytes) -> tuple[int, int]:
    """Decode a generated payload so tests verify what the browser receives."""

    with Image.open(io.BytesIO(content)) as image:
        image.load()
        return image.size


def _wait_until(predicate, *, timeout: float = 2.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"{message} was not reached in time")


def _flight_ticket_priority(
    registry: ImageRegistry,
    image_id: str,
    variant: str,
) -> str | None:
    with registry._lock:
        for (
            flight_id,
            flight_variant,
            _mtime,
            _size,
        ), flight in registry._flights.items():
            if (
                flight_id == image_id
                and flight_variant == variant
                and flight.ticket is not None
            ):
                return flight.ticket.priority
    return None


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

    def test_payload_rejects_unknown_priority_and_bad_variant(self) -> None:
        registry = ImageRegistry()
        image_id = registry.register(_image(self.root / "priority.jpg")).image_id

        for priority in ("urgent", "", True, 1, [], {}):
            with (
                self.subTest(priority=priority),
                self.assertRaisesRegex(ImageRegistryError, "优先级"),
            ):
                registry.payload(image_id, "thumbnail", priority=priority)
        with self.assertRaisesRegex(ImageRegistryError, "尺寸"):
            registry.payload(image_id, "poster")

    def test_higher_priority_waiter_renders_before_earlier_lower_priority(
        self,
    ) -> None:
        registry = ImageRegistry(max_render_workers=1)
        ids = {
            name: registry.register(
                _image(self.root / f"{name}.jpg", size=(80, 60))
            ).image_id
            for name in ("A", "B", "C")
        }
        order: list[str] = []
        order_lock = threading.Lock()
        first_started = threading.Event()
        release_all = threading.Event()
        real_render = image_registry_module._render_image

        def tracked_render(path, variant, mtime_ns):
            with order_lock:
                order.append(path.name)
                first_started.set()
            self.assertTrue(release_all.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            blocker = executor.submit(registry.payload, ids["A"], "preview")
            self.assertTrue(first_started.wait(timeout=2))
            low = executor.submit(registry.payload, ids["B"], "preview", "background")
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="B queued",
            )
            high = executor.submit(registry.payload, ids["C"], "preview", "current")
            _wait_until(
                lambda: registry._render_gate.pending == 2,
                message="C queued",
            )
            release_all.set()
            for future in (blocker, low, high):
                future.result(timeout=5)

        self.assertEqual(order, ["A.jpg", "C.jpg", "B.jpg"])

    def test_equal_priority_waiters_keep_arrival_order(self) -> None:
        registry = ImageRegistry(max_render_workers=1)
        ids = {
            name: registry.register(
                _image(self.root / f"fifo-{name}.jpg", size=(60, 40))
            ).image_id
            for name in ("A", "B", "C")
        }
        order: list[str] = []
        order_lock = threading.Lock()
        first_started = threading.Event()
        release_all = threading.Event()
        real_render = image_registry_module._render_image

        def tracked_render(path, variant, mtime_ns):
            with order_lock:
                order.append(path.name)
                first_started.set()
            self.assertTrue(release_all.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            blocker = executor.submit(registry.payload, ids["A"], "preview")
            self.assertTrue(first_started.wait(timeout=2))
            first = executor.submit(registry.payload, ids["B"], "preview", "visible")
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="B queued",
            )
            second = executor.submit(registry.payload, ids["C"], "preview", "visible")
            _wait_until(
                lambda: registry._render_gate.pending == 2,
                message="C queued",
            )
            release_all.set()
            for future in (blocker, first, second):
                future.result(timeout=5)

        self.assertEqual(order, ["fifo-A.jpg", "fifo-B.jpg", "fifo-C.jpg"])

    def test_current_request_promotes_a_queued_same_key_prefetch(self) -> None:
        registry = ImageRegistry(max_render_workers=1)
        ids = {
            name: registry.register(
                _image(self.root / f"promote-{name}.jpg", size=(60, 40))
            ).image_id
            for name in ("A", "C", "D")
        }
        order: list[str] = []
        order_lock = threading.Lock()
        first_started = threading.Event()
        release_all = threading.Event()
        real_render = image_registry_module._render_image

        def tracked_render(path, variant, mtime_ns):
            with order_lock:
                order.append(path.name)
                first_started.set()
            self.assertTrue(release_all.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            ThreadPoolExecutor(max_workers=4) as executor,
        ):
            blocker = executor.submit(registry.payload, ids["A"], "preview")
            self.assertTrue(first_started.wait(timeout=2))
            prefetch = executor.submit(
                registry.payload, ids["C"], "preview", "prefetch"
            )
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="prefetch queued",
            )
            owner = executor.submit(registry.payload, ids["D"], "preview", "background")
            _wait_until(
                lambda: registry._render_gate.pending == 2,
                message="background queued",
            )
            joiner = executor.submit(registry.payload, ids["D"], "preview", "current")
            _wait_until(
                lambda: (
                    _flight_ticket_priority(registry, ids["D"], "preview") == "current"
                ),
                message="D promoted",
            )
            release_all.set()
            for future in (blocker, prefetch, owner, joiner):
                future.result(timeout=5)

        self.assertEqual(order, ["promote-A.jpg", "promote-D.jpg", "promote-C.jpg"])

    def test_current_request_promotes_during_the_disk_check(self) -> None:
        registry = ImageRegistry(
            max_render_workers=1,
            cache_directory=self.root / "promote-cache",
        )
        ids = {
            name: registry.register(
                _image(self.root / f"disk-{name}.jpg", size=(60, 40))
            ).image_id
            for name in ("A", "C", "D")
        }
        order: list[str] = []
        order_lock = threading.Lock()
        first_started = threading.Event()
        release_all = threading.Event()
        disk_armed = threading.Event()
        disk_entered = threading.Event()
        disk_release = threading.Event()
        real_render = image_registry_module._render_image
        real_read = image_registry_module._read_disk_payload

        def tracked_render(path, variant, mtime_ns):
            with order_lock:
                order.append(path.name)
                first_started.set()
            self.assertTrue(release_all.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        def controlled_read(path, variant, mtime_ns):
            if disk_armed.is_set():
                disk_entered.set()
                self.assertTrue(disk_release.wait(timeout=5))
            return real_read(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            patch.object(image_registry_module, "_read_disk_payload", controlled_read),
            ThreadPoolExecutor(max_workers=4) as executor,
        ):
            blocker = executor.submit(registry.payload, ids["A"], "thumbnail")
            self.assertTrue(first_started.wait(timeout=2))
            prefetch = executor.submit(
                registry.payload, ids["C"], "thumbnail", "prefetch"
            )
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="prefetch queued",
            )
            disk_armed.set()
            owner = executor.submit(
                registry.payload, ids["D"], "thumbnail", "background"
            )
            self.assertTrue(disk_entered.wait(timeout=2))
            joiner = executor.submit(registry.payload, ids["D"], "thumbnail", "current")
            _wait_until(
                lambda: (
                    _flight_ticket_priority(registry, ids["D"], "thumbnail")
                    == "current"
                ),
                message="D promoted",
            )
            # The owner has not queued its ticket yet: the promotion must act on
            # the ticket that already exists, not on a ticket created afterwards.
            self.assertEqual(registry._render_gate.pending, 1)
            disk_release.set()
            _wait_until(
                lambda: registry._render_gate.pending == 2,
                message="D queued",
            )
            release_all.set()
            for future in (blocker, prefetch, owner, joiner):
                future.result(timeout=5)

        self.assertEqual(order, ["disk-A.jpg", "disk-D.jpg", "disk-C.jpg"])

    def test_full_queue_reports_busy_to_owner_and_same_key_waiter(self) -> None:
        registry = ImageRegistry(
            max_render_workers=1,
            cache_directory=self.root / "full-cache",
        )
        registry._render_gate = PriorityGate(1, 1, fairness_limit=8)
        ids = {
            name: registry.register(
                _image(self.root / f"full-{name}.jpg", size=(60, 40))
            ).image_id
            for name in ("A", "C", "D")
        }
        render_started = threading.Event()
        release_render = threading.Event()
        disk_armed = threading.Event()
        disk_entered = threading.Event()
        disk_release = threading.Event()
        real_render = image_registry_module._render_image
        real_read = image_registry_module._read_disk_payload

        def tracked_render(path, variant, mtime_ns):
            render_started.set()
            self.assertTrue(release_render.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        def controlled_read(path, variant, mtime_ns):
            if disk_armed.is_set():
                disk_entered.set()
                self.assertTrue(disk_release.wait(timeout=5))
            return real_read(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", tracked_render),
            patch.object(image_registry_module, "_read_disk_payload", controlled_read),
            ThreadPoolExecutor(max_workers=4) as executor,
        ):
            blocker = executor.submit(registry.payload, ids["A"], "thumbnail")
            self.assertTrue(render_started.wait(timeout=2))
            prefetch = executor.submit(
                registry.payload, ids["C"], "thumbnail", "prefetch"
            )
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="queue saturated",
            )
            disk_armed.set()
            owner = executor.submit(
                registry.payload, ids["D"], "thumbnail", "background"
            )
            self.assertTrue(disk_entered.wait(timeout=2))
            joiner = executor.submit(registry.payload, ids["D"], "thumbnail", "current")
            _wait_until(
                lambda: (
                    _flight_ticket_priority(registry, ids["D"], "thumbnail")
                    == "current"
                ),
                message="D joined",
            )
            disk_release.set()

            with self.assertRaises(ImageQueueFull) as owner_error:
                owner.result(timeout=5)
            with self.assertRaises(ImageQueueFull) as joiner_error:
                joiner.result(timeout=5)
            self.assertEqual(
                str(owner_error.exception),
                str(joiner_error.exception),
            )
            self.assertTrue(
                all(
                    "队列已满" in str(error.exception)
                    for error in (owner_error, joiner_error)
                )
            )

            release_render.set()
            blocker.result(timeout=5)
            prefetch.result(timeout=5)

        self.assertTrue(issubclass(ImageQueueFull, ImageRegistryError))

    def test_cache_hits_do_not_wait_for_a_render_slot(self) -> None:
        registry = ImageRegistry(
            max_render_workers=1,
            cache_directory=self.root / "hit-cache",
        )
        cached_id = registry.register(
            _image(self.root / "hit-cached.jpg", size=(200, 150))
        ).image_id
        primer = registry.payload(cached_id, "thumbnail")
        busy_id = registry.register(
            _image(self.root / "hit-busy.jpg", size=(200, 150))
        ).image_id
        render_started = threading.Event()
        release_render = threading.Event()
        real_render = image_registry_module._render_image

        def blocked_render(path, variant, mtime_ns):
            render_started.set()
            self.assertTrue(release_render.wait(timeout=5))
            return real_render(path, variant, mtime_ns)

        with (
            patch.object(image_registry_module, "_render_image", blocked_render),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            busy = executor.submit(registry.payload, busy_id, "thumbnail")
            self.assertTrue(render_started.wait(timeout=2))
            memory_hit = executor.submit(registry.payload, cached_id, "thumbnail")
            self.assertIs(memory_hit.result(timeout=2), primer)
            registry._cache.clear()
            registry._cache_bytes = 0
            disk_hit = executor.submit(registry.payload, cached_id, "thumbnail")
            self.assertEqual(disk_hit.result(timeout=2).content, primer.content)
            self.assertFalse(release_render.is_set())
            self.assertEqual(registry._render_gate.pending, 0)
            release_render.set()
            busy.result(timeout=5)

    def test_close_wakes_queued_requests_without_releasing_active_renders(
        self,
    ) -> None:
        registry = ImageRegistry(max_render_workers=2)
        ids = {
            name: registry.register(
                _image(self.root / f"close-{name}.jpg", size=(60, 40))
            ).image_id
            for name in ("A", "B", "C")
        }
        active = 0
        active_lock = threading.Lock()
        two_started = threading.Event()
        release_render = threading.Event()
        real_render = image_registry_module._render_image

        def blocked_render(path, variant, mtime_ns):
            nonlocal active
            with active_lock:
                active += 1
                if active == 2:
                    two_started.set()
            try:
                self.assertTrue(release_render.wait(timeout=5))
                return real_render(path, variant, mtime_ns)
            finally:
                with active_lock:
                    active -= 1

        with (
            patch.object(image_registry_module, "_render_image", blocked_render),
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            first = executor.submit(registry.payload, ids["A"], "thumbnail")
            second = executor.submit(registry.payload, ids["B"], "thumbnail")
            self.assertTrue(two_started.wait(timeout=2))
            queued = executor.submit(registry.payload, ids["C"], "thumbnail")
            _wait_until(
                lambda: registry._render_gate.pending == 1,
                message="C queued",
            )

            registry.close()

            with self.assertRaisesRegex(ImageRegistryError, "关闭"):
                queued.result(timeout=5)
            self.assertEqual(registry._render_gate.pending, 0)
            # The queued request left the queue without needing the active
            # renders to be released first.
            self.assertFalse(release_render.is_set())

            release_render.set()
            for future in (first, second):
                with self.assertRaises(ImageRegistryError):
                    future.result(timeout=5)

    def test_source_change_retry_keeps_request_priority(self) -> None:
        source = _image(self.root / "retry-priority.jpg", size=(400, 300))
        registry = ImageRegistry()
        image_id = registry.register(source).image_id
        real_render = image_registry_module._render_image
        render_count = 0

        def changing_render(path, variant, mtime_ns):
            nonlocal render_count
            render_count += 1
            payload = real_render(path, variant, mtime_ns)
            if render_count == 1:
                time.sleep(0.01)
                _image(source, size=(200, 600))
                source.touch()
            return payload

        gate = registry._render_gate
        with (
            patch.object(gate, "enqueue", wraps=gate.enqueue) as enqueue_spy,
            patch.object(image_registry_module, "_render_image", changing_render),
        ):
            registry.payload(image_id, "thumbnail", "prefetch")

        self.assertEqual(render_count, 2)
        self.assertEqual(
            [call.args[0].priority for call in enqueue_spy.call_args_list],
            ["prefetch", "prefetch"],
        )

    def test_close_wakes_same_key_followers_before_owner_finishes(self) -> None:
        for phase in ("render", "disk"):
            with self.subTest(phase=phase):
                registry = ImageRegistry(
                    cache_directory=self.root / f"close-{phase}-cache"
                )
                image_id = registry.register(
                    _image(self.root / f"{phase}.jpg")
                ).image_id
                entered = threading.Event()
                release = threading.Event()
                original = (
                    image_registry_module._render_image
                    if phase == "render"
                    else image_registry_module._read_disk_payload
                )

                def blocked(*args, entered=entered, release=release, original=original):
                    entered.set()
                    self.assertTrue(release.wait(timeout=3))
                    return original(*args)

                method = "_render_image" if phase == "render" else "_read_disk_payload"
                with (
                    patch.object(image_registry_module, method, blocked),
                    ThreadPoolExecutor(2) as pool,
                ):
                    owner = pool.submit(
                        registry.payload, image_id, "thumbnail", "prefetch"
                    )
                    self.assertTrue(entered.wait(timeout=2))
                    follower = pool.submit(
                        registry.payload, image_id, "thumbnail", "current"
                    )
                    try:
                        _wait_until(
                            lambda registry=registry, image_id=image_id: (
                                _flight_ticket_priority(registry, image_id, "thumbnail")
                                == "current"
                            )
                        )
                        registry.close()
                        with self.assertRaisesRegex(ImageRegistryError, "关闭"):
                            follower.result(timeout=0.5)
                        self.assertFalse(release.is_set())
                    finally:
                        release.set()
                    with self.assertRaises(ImageRegistryError):
                        owner.result(timeout=2)

    def test_source_change_retry_keeps_a_followers_promoted_priority(self) -> None:
        source = _image(self.root / "promoted-retry.jpg")
        registry = ImageRegistry(max_render_workers=1)
        image_id = registry.register(source).image_id
        entered = threading.Event()
        release = threading.Event()
        renders = 0
        original = image_registry_module._render_image

        def changing_render(path, variant, mtime):
            nonlocal renders
            renders += 1
            if renders == 1:
                entered.set()
                self.assertTrue(release.wait(timeout=3))
                _image(source, size=(200, 600))
            return original(path, variant, mtime)

        with (
            patch.object(image_registry_module, "_render_image", changing_render),
            patch.object(
                registry._render_gate,
                "new_ticket",
                wraps=registry._render_gate.new_ticket,
            ) as create,
            ThreadPoolExecutor(2) as pool,
        ):
            owner = pool.submit(registry.payload, image_id, "preview", "prefetch")
            self.assertTrue(entered.wait(timeout=2))
            follower = pool.submit(registry.payload, image_id, "preview", "current")
            try:
                _wait_until(
                    lambda: (
                        _flight_ticket_priority(registry, image_id, "preview")
                        == "current"
                    )
                )
            finally:
                release.set()
            self.assertIs(owner.result(timeout=3), follower.result(timeout=3))
        self.assertEqual(renders, 2)
        self.assertEqual(
            [call.args[0] for call in create.call_args_list], ["prefetch", "current"]
        )

    def test_defaults_and_priorities_share_the_same_cached_payload(self) -> None:
        registry = ImageRegistry()
        image_id = registry.register(_image(self.root / "defaults.jpg")).image_id
        with patch.object(
            registry._render_gate,
            "new_ticket",
            wraps=registry._render_gate.new_ticket,
        ) as create:
            thumbnail = registry.payload(image_id, "thumbnail")
            preview = registry.payload(image_id, "preview")
            self.assertIs(
                registry.payload(image_id, "thumbnail", "background"), thumbnail
            )
            self.assertIs(registry.payload(image_id, "preview", "prefetch"), preview)
        self.assertEqual(
            [call.args[0] for call in create.call_args_list], ["visible", "current"]
        )

    def test_remote_variants_are_bounded_and_separate_from_thumbnail(self) -> None:
        source = _image(self.root / "remote.jpg", size=(2400, 1600))
        registry = ImageRegistry(max_entries=10, max_cache_bytes=8 * 1024 * 1024)
        self.addCleanup(registry.close)
        image_id = registry.register(source).image_id

        thumbnail = registry.payload(image_id, "thumbnail")
        remote_thumbnail = registry.payload(image_id, "remote_thumbnail")
        remote_preview = registry.payload(image_id, "remote_preview")

        self.assertEqual(thumbnail.content_type, "image/jpeg")
        allowed = {"image/jpeg", "image/png", "image/webp"}
        self.assertIn(remote_thumbnail.content_type, allowed)
        self.assertIn(remote_preview.content_type, allowed)
        self.assertLessEqual(len(remote_thumbnail.content), 1024 * 1024)
        self.assertLessEqual(len(remote_preview.content), 1024 * 1024)
        self.assertNotEqual(remote_thumbnail.etag, thumbnail.etag)
        self.assertLessEqual(max(_decoded_size(remote_thumbnail.content)), 640)
        self.assertLessEqual(max(_decoded_size(remote_preview.content)), 1600)
        self.assertIs(
            registry.payload(image_id, "remote_thumbnail"),
            remote_thumbnail,
        )

    def test_remote_variant_disk_cache_survives_restart(self) -> None:
        source = _image(self.root / "persist.jpg", size=(2400, 1600))
        cache = self.root / "cache"
        first = ImageRegistry(cache_directory=cache)
        image_id = first.register(source).image_id
        payload = first.payload(image_id, "remote_thumbnail")
        first.close()

        second = ImageRegistry(cache_directory=cache)
        self.addCleanup(second.close)
        cached = second.payload(
            second.register(source).image_id,
            "remote_thumbnail",
        )

        self.assertEqual(cached.content, payload.content)
        self.assertEqual(cached.content_type, payload.content_type)
        files = list((cache / "thumbnails-v1").rglob("*.cache"))
        self.assertTrue(files)
        with Image.open(files[0]) as stored:
            stored.verify()

    def test_remote_variant_source_change_invalidates_the_cached_payload(self) -> None:
        source = _image(self.root / "change.jpg", size=(2400, 1600))
        registry = ImageRegistry(cache_directory=self.root / "cache")
        self.addCleanup(registry.close)
        image_id = registry.register(source).image_id
        first = registry.payload(image_id, "remote_thumbnail")

        Image.new("RGB", (2400, 1600), (200, 10, 10)).save(source, format="JPEG")
        stat_result = os.stat(source)
        os.utime(source, ns=(stat_result.st_atime_ns + 5_000_000_000,) * 2)
        registry.register(source)
        second = registry.payload(image_id, "remote_thumbnail")

        self.assertNotEqual(first.etag, second.etag)

    def test_remote_variants_are_persisted_under_a_profile_namespace(self) -> None:
        source = _image(self.root / "namespace.jpg", size=(1200, 800))
        registry = ImageRegistry(cache_directory=self.root / "cache")
        self.addCleanup(registry.close)
        registry.register(source)
        stat_result = source.stat()
        thumbnail_path = registry._disk_path(source.resolve(), "thumbnail", stat_result)
        remote_path = registry._disk_path(
            source.resolve(),
            "remote_thumbnail",
            stat_result,
        )

        self.assertIsNotNone(thumbnail_path)
        self.assertIsNotNone(remote_path)
        self.assertNotEqual(thumbnail_path, remote_path)

    def test_remote_variants_leave_the_source_and_thumbnail_untouched(self) -> None:
        source = _image(self.root / "stable.jpg", size=(2400, 1600))
        original_bytes = source.read_bytes()
        registry = ImageRegistry(cache_directory=self.root / "cache")
        self.addCleanup(registry.close)
        image_id = registry.register(source).image_id
        thumbnail = registry.payload(image_id, "thumbnail")

        registry.payload(image_id, "remote_thumbnail")
        registry.payload(image_id, "remote_preview")

        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertTrue(thumbnail.content.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
