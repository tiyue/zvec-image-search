from __future__ import annotations

import inspect
import queue
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from zvec_desktop.widgets import (
    AsyncImageCanvas,
    FullscreenImageViewer,
    ImageTaskDispatcher,
    ResponsiveGallery,
)


class _FakeRoot:
    def __init__(self) -> None:
        self._next_handle = 0
        self.cancelled: list[str] = []

    def after(self, _delay: int, _callback: object) -> str:
        self._next_handle += 1
        return f"after-{self._next_handle}"

    def after_cancel(self, handle: str) -> None:
        self.cancelled.append(handle)


class DesktopWidgetContractTest(unittest.TestCase):
    def test_preview_defaults_to_contain_and_rejects_unknown_modes(self) -> None:
        mode = inspect.signature(AsyncImageCanvas.__init__).parameters["mode"]

        self.assertEqual(mode.default, "contain")
        AsyncImageCanvas._validate_mode("contain")
        AsyncImageCanvas._validate_mode("cover")
        with self.assertRaisesRegex(ValueError, "contain.*cover"):
            AsyncImageCanvas._validate_mode("stretch")

    def test_fullscreen_defaults_to_cover_and_space_toggles_both_modes(self) -> None:
        viewer = object.__new__(FullscreenImageViewer)
        viewer._mode = "cover"
        viewer._surface = mock.Mock()
        viewer._mode_button = mock.Mock()

        result = viewer._on_toggle_mode(mock.Mock())
        self.assertEqual(result, "break")
        self.assertEqual(viewer._mode, "contain")
        viewer._surface.set_mode.assert_called_once_with("contain")
        viewer._mode_button.configure.assert_called_once_with(text="铺满屏幕")

        viewer.toggle_mode()
        self.assertEqual(viewer._mode, "cover")
        viewer._surface.set_mode.assert_called_with("cover")
        viewer._mode_button.configure.assert_called_with(text="完整显示")

    def test_destroyed_canvas_cancels_queued_decode_and_releases_photo(self) -> None:
        canvas = object.__new__(AsyncImageCanvas)
        pending = mock.Mock()
        canvas._render_token = 7
        canvas._resize_handle = None
        canvas._pending_future = pending
        canvas._destroyed = False
        canvas._photo = mock.Mock()
        event = mock.Mock(widget=canvas)

        canvas._on_destroy(event)

        self.assertTrue(canvas._destroyed)
        self.assertEqual(canvas._render_token, 8)
        self.assertIsNone(canvas._pending_future)
        self.assertIsNone(canvas._photo)
        pending.cancel.assert_called_once_with()

    def test_gallery_clears_columns_left_by_a_wider_window(self) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._configured_columns = 5
        gallery._content = mock.Mock()

        gallery._configure_grid_columns(2)

        self.assertEqual(gallery._configured_columns, 2)
        calls = gallery._content.grid_columnconfigure.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0], mock.call(0, weight=1, uniform="gallery"))
        self.assertEqual(calls[1], mock.call(1, weight=1, uniform="gallery"))
        self.assertEqual(calls[2], mock.call(2, weight=0, uniform=""))
        self.assertEqual(calls[4], mock.call(4, weight=0, uniform=""))

    def test_callback_failure_does_not_freeze_later_image_completions(self) -> None:
        root = _FakeRoot()
        images = [mock.Mock(spec=Image.Image), mock.Mock(spec=Image.Image)]

        def loader(path: Path, _size: tuple[int, int], _mode: str) -> Image.Image:
            return images[int(path.stem) - 1]

        first_completion = mock.Mock(side_effect=RuntimeError("render failed"))

        def finish_second(image: Image.Image | None, error: str | None) -> None:
            self.assertIsNone(error)
            self.assertIs(image, images[1])
            assert image is not None
            image.close()

        second_completion = mock.Mock(side_effect=finish_second)
        dispatcher = ImageTaskDispatcher(root, loader, workers=2)  # type: ignore[arg-type]
        futures = [
            dispatcher.request(Path("1"), (40, 40), "contain", first_completion),
            dispatcher.request(Path("2"), (40, 40), "contain", second_completion),
        ]
        for future in futures:
            assert future is not None
            future.result(timeout=5)

        deadline = time.monotonic() + 5
        while dispatcher._completed.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        if dispatcher._completed.qsize() < 2:
            raise queue.Empty("worker completions were not collected")

        dispatcher._drain()
        dispatcher.close()

        first_completion.assert_called_once()
        second_completion.assert_called_once()
        images[0].close.assert_called_once_with()
        images[1].close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
