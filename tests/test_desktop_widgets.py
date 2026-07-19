from __future__ import annotations

import inspect
import queue
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import zvec_desktop.widgets as desktop_widgets
from zvec_desktop.theme import DEFAULT_THEME
from zvec_desktop.widgets import (
    AsyncImageCanvas,
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

    def test_removed_immersive_viewer_is_not_exposed(self) -> None:
        self.assertFalse(hasattr(desktop_widgets, "FullscreenImageViewer"))

        parameters = inspect.signature(ResponsiveGallery.set_items).parameters
        self.assertNotIn("on_open", parameters)

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

    def test_gallery_waits_for_initial_geometry_without_starving_tk(self) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._resize_handle = "old-handle"
        gallery._layout_signature = (5, 120, 140, False)
        gallery._canvas = mock.Mock()
        gallery._canvas.winfo_width.return_value = 1
        gallery._canvas.winfo_height.return_value = 1
        gallery.after = mock.Mock(return_value="retry-handle")

        gallery._rebuild()

        self.assertIsNone(gallery._layout_signature)
        self.assertEqual(gallery._resize_handle, "retry-handle")
        gallery.after.assert_called_once_with(
            ResponsiveGallery._LAYOUT_RETRY_MS,
            gallery._rebuild,
        )

    def test_gallery_ignores_stale_content_height_for_scrollbar_visibility(
        self,
    ) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._desired_scrollbar_visible = False
        gallery._scrollbar_visible = True
        gallery._canvas = mock.Mock()
        gallery._canvas.bbox.return_value = (0, 0, 600, 900)
        gallery._scrollbar = mock.Mock()

        gallery._update_scroll_region(mock.Mock())

        self.assertFalse(gallery._scrollbar_visible)
        gallery._canvas.configure.assert_called_once_with(scrollregion=(0, 0, 600, 900))
        gallery._scrollbar.grid_remove.assert_called_once_with()
        gallery._canvas.yview_moveto.assert_called_once_with(0.0)

    def test_gallery_portrait_thumbnails_use_contain_and_compact_captions(
        self,
    ) -> None:
        self.assertEqual(ResponsiveGallery._THUMBNAIL_MODE, "contain")
        self.assertEqual(ResponsiveGallery._caption_height(112), 40)
        self.assertEqual(ResponsiveGallery._caption_height(139), 42)
        self.assertEqual(ResponsiveGallery._caption_height(400), 44)

    def test_gallery_selection_style_always_takes_precedence_over_hover(self) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._theme = DEFAULT_THEME
        gallery._selected_index = 2
        gallery._hovered_index = 2

        self.assertEqual(
            gallery._card_visual_style(2),
            (DEFAULT_THEME.selection, DEFAULT_THEME.primary, 2),
        )

        gallery._selected_index = None
        self.assertEqual(
            gallery._card_visual_style(2),
            (DEFAULT_THEME.surface_subtle, DEFAULT_THEME.focus, 1),
        )
        self.assertEqual(
            gallery._card_visual_style(1),
            (DEFAULT_THEME.surface, DEFAULT_THEME.border_strong, 1),
        )

    def test_gallery_hover_updates_chrome_without_clearing_another_card(self) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._hovered_index = None
        gallery._update_selection_styles = mock.Mock()

        gallery._set_hovered_index(4)
        self.assertEqual(gallery._hovered_index, 4)
        gallery._update_selection_styles.assert_called_once_with()

        gallery._clear_hovered_index(3)
        self.assertEqual(gallery._hovered_index, 4)
        gallery._update_selection_styles.assert_called_once_with()

        gallery._clear_hovered_index(4)
        self.assertIsNone(gallery._hovered_index)
        self.assertEqual(gallery._update_selection_styles.call_count, 2)

    def test_gallery_selected_card_repaints_caption_and_title_consistently(
        self,
    ) -> None:
        gallery = object.__new__(ResponsiveGallery)
        gallery._theme = DEFAULT_THEME
        gallery._selected_index = 0
        gallery._hovered_index = 0
        card = mock.Mock()
        caption = mock.Mock()
        title = mock.Mock()
        subtitle = mock.Mock()
        gallery._card_chrome = {0: (card, caption, title, subtitle)}

        gallery._update_selection_styles()

        card.configure.assert_called_once_with(
            background=DEFAULT_THEME.selection,
            highlightbackground=DEFAULT_THEME.primary,
            highlightthickness=2,
        )
        caption.configure.assert_called_once_with(background=DEFAULT_THEME.selection)
        title.configure.assert_called_once_with(
            background=DEFAULT_THEME.selection,
            foreground=DEFAULT_THEME.primary,
        )
        subtitle.configure.assert_called_once_with(background=DEFAULT_THEME.selection)

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
