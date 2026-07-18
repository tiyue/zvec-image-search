from __future__ import annotations

import unittest
from collections.abc import Callable

from PIL import Image

from zvec_desktop.tray import (
    TrayEventKind,
    TrayIconError,
    TrayIconService,
    create_tray_image,
)


class FakeTrayIcon:
    def __init__(self) -> None:
        self.run_count = 0
        self.stop_count = 0
        self.notifications: list[tuple[str, str | None]] = []

    def run_detached(self) -> None:
        self.run_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((message, title))


class TrayHarness:
    def __init__(self) -> None:
        self.icon = FakeTrayIcon()
        self.image: Image.Image | None = None
        self.on_show: Callable[[], None] | None = None
        self.on_exit: Callable[[], None] | None = None

    def factory(
        self,
        _title: str,
        image: Image.Image,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> FakeTrayIcon:
        self.image = image
        self.on_show = on_show
        self.on_exit = on_exit
        return self.icon


class TrayIconServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = TrayHarness()
        self.service = TrayIconService(icon_factory=self.harness.factory)
        self.addCleanup(self._stop_if_running)

    def _stop_if_running(self) -> None:
        if self.service.running:
            self.service.stop()

    def test_start_is_idempotent_and_creates_rgba_icon(self) -> None:
        self.service.start()
        self.service.start()

        self.assertTrue(self.service.running)
        self.assertEqual(self.harness.icon.run_count, 1)
        assert self.harness.image is not None
        self.assertEqual(self.harness.image.mode, "RGBA")
        self.assertEqual(self.harness.image.size, (64, 64))

    def test_callbacks_only_enqueue_until_main_thread_drains(self) -> None:
        self.service.start()
        assert self.harness.on_show is not None
        assert self.harness.on_exit is not None
        self.harness.on_show()
        self.harness.on_exit()
        delivered: list[TrayEventKind] = []

        events = self.service.drain_events(lambda event: delivered.append(event.kind))

        self.assertEqual(
            [event.kind for event in events],
            [TrayEventKind.SHOW, TrayEventKind.EXIT],
        )
        self.assertEqual(delivered, [TrayEventKind.SHOW, TrayEventKind.EXIT])

    def test_stop_removes_icon_and_ignores_late_callbacks(self) -> None:
        self.service.start()
        assert self.harness.on_show is not None
        self.service.stop()
        self.harness.on_show()

        self.assertFalse(self.service.running)
        self.assertEqual(self.harness.icon.stop_count, 1)
        self.assertEqual(self.service.drain_events(), ())

    def test_notify_is_best_effort_when_not_started(self) -> None:
        self.service.notify("后台任务仍在运行")
        self.assertEqual(self.harness.icon.notifications, [])
        self.service.start()
        self.service.notify("处理完成", title="Zvec")
        self.assertEqual(
            self.harness.icon.notifications,
            [("处理完成", "Zvec")],
        )

    def test_factory_failure_is_structured(self) -> None:
        service = TrayIconService(
            icon_factory=lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        with self.assertRaisesRegex(TrayIconError, "boom"):
            service.start()
        self.assertFalse(service.running)

    def test_generated_icon_validates_size_and_has_visible_pixels(self) -> None:
        image = create_tray_image(32)
        self.addCleanup(image.close)
        self.assertIsNotNone(image.getbbox())
        for invalid in (0, 15, 257, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                create_tray_image(invalid)


if __name__ == "__main__":
    unittest.main()
