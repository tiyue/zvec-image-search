from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from zvec_desktop.app import DesktopLaunchOptions, parse_options
from zvec_desktop.gallery_layout import calculate_gallery_layout


class DesktopOptionTests(unittest.TestCase):
    def test_default_gallery_page_contains_fifteen_images(self) -> None:
        options = parse_options([])
        self.assertEqual(options.page_size, 15)

    def test_rejects_unbounded_page_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            parse_options(["--page-size", "101"])

    def test_normal_gallery_fills_five_by_three_and_small_window_scrolls(self) -> None:
        normal = calculate_gallery_layout(633, 438, 15)
        self.assertEqual((normal.columns, normal.rows), (5, 3))
        self.assertFalse(normal.scroll_required)

        compact = calculate_gallery_layout(340, 300, 15)
        self.assertGreater(compact.rows, 3)
        self.assertTrue(compact.scroll_required)

    def test_missing_api_key_directs_the_user_to_settings(self) -> None:
        from zvec_desktop.ui import ZvecDesktopWindow

        window = object.__new__(ZvecDesktopWindow)
        window._api_key_var = mock.Mock()
        window._api_key_var.get.return_value = ""
        window.root = mock.Mock()

        with mock.patch("zvec_desktop.ui.messagebox.showwarning") as warning:
            window.configure_api_key()

        warning.assert_called_once()
        self.assertIn("设置 → 模型与密钥", warning.call_args.args[1])


@unittest.skipUnless(os.name == "nt", "tkinter widget tests are Windows-only")
class DesktopAsyncWidgetTests(unittest.TestCase):
    class _FakeRoot:
        def __init__(self) -> None:
            self._next_handle = 0
            self.cancelled: list[str] = []

        def after(self, _delay: int, _callback: object) -> str:
            self._next_handle += 1
            return f"after-{self._next_handle}"

        def after_cancel(self, handle: str) -> None:
            self.cancelled.append(handle)

    def test_reverse_image_callbacks_cannot_replace_new_path_mode_or_size(self) -> None:
        import zvec_desktop.widgets as widgets

        requests: list[tuple[Path, tuple[int, int], str, object]] = []
        worker_futures: list[mock.Mock] = []
        dispatcher = mock.Mock()

        def request(path: Path, size: tuple[int, int], mode: str, completion: object):
            requests.append((path, size, mode, completion))
            future = mock.Mock()
            worker_futures.append(future)
            return future

        dispatcher.request.side_effect = request
        canvas = object.__new__(widgets.AsyncImageCanvas)
        canvas._dispatcher = dispatcher
        canvas._path = Path("first.jpg")
        canvas._mode = "contain"
        canvas._render_token = 1
        canvas._resize_handle = None
        canvas._pending_future = None
        canvas._destroyed = False
        canvas._photo = None
        dimensions = [100, 80]
        draw_placeholder = mock.Mock()
        delete = mock.Mock()
        create_image = mock.Mock()
        first_image = mock.Mock(spec=Image.Image)
        second_image = mock.Mock(spec=Image.Image)
        with (
            mock.patch.object(canvas, "winfo_width", side_effect=lambda: dimensions[0]),
            mock.patch.object(
                canvas, "winfo_height", side_effect=lambda: dimensions[1]
            ),
            mock.patch.object(canvas, "winfo_exists", return_value=True),
            mock.patch.object(canvas, "_draw_placeholder", draw_placeholder),
            mock.patch.object(canvas, "delete", delete),
            mock.patch.object(canvas, "create_image", create_image),
            mock.patch.object(
                widgets.ImageTk,
                "PhotoImage",
                side_effect=lambda image: (
                    "second-photo" if image is second_image else "first-photo"
                ),
            ),
        ):
            canvas._request_render(1)
            canvas._path = Path("second.jpg")
            canvas._mode = "cover"
            dimensions[:] = [200, 160]
            second_token = canvas._invalidate_render()
            canvas._request_render(second_token)

            second_completion = requests[1][3]
            assert callable(second_completion)
            second_completion(second_image, None)
            first_completion = requests[0][3]
            assert callable(first_completion)
            first_completion(first_image, None)

        self.assertEqual(canvas._photo, "second-photo")
        first_image.close.assert_called_once_with()
        second_image.close.assert_called_once_with()
        worker_futures[0].cancel.assert_called_once_with()
        create_image.assert_called_once_with(
            100, 80, image="second-photo", anchor="center"
        )
        self.assertEqual(
            [(path, size, mode) for path, size, mode, _callback in requests],
            [
                (Path("first.jpg"), (100, 80), "contain"),
                (Path("second.jpg"), (200, 160), "cover"),
            ],
        )

    def test_dispatcher_closes_image_that_finishes_after_shutdown(self) -> None:
        from zvec_desktop.widgets import ImageTaskDispatcher

        root = self._FakeRoot()
        started = threading.Event()
        release = threading.Event()
        image_closed = threading.Event()
        image = mock.Mock(spec=Image.Image)
        image.close.side_effect = image_closed.set

        def loader(_path: Path, _size: tuple[int, int], _mode: str) -> Image.Image:
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return image

        completion = mock.Mock()
        dispatcher = ImageTaskDispatcher(root, loader, workers=1)  # type: ignore[arg-type]
        dispatcher.request(Path("late.jpg"), (40, 40), "contain", completion)
        self.assertTrue(started.wait(timeout=5))

        dispatcher.close()
        release.set()

        self.assertTrue(image_closed.wait(timeout=5))
        image.close.assert_called_once_with()
        completion.assert_not_called()


@unittest.skipUnless(os.name == "nt", "tkinter desktop smoke is Windows-only")
class DesktopWindowSmokeTests(unittest.TestCase):
    class _FakeTray:
        def __init__(self) -> None:
            self.running = False
            self.start_calls = 0
            self.stop_calls = 0
            self.notifications: list[str] = []

        def start(self) -> None:
            self.start_calls += 1
            self.running = True

        def stop(self) -> None:
            self.stop_calls += 1
            self.running = False

        def notify(self, message: str, *, title: str | None = None) -> None:
            del title
            self.notifications.append(message)

        def drain_events(self, callback: object) -> tuple[object, ...]:
            del callback
            return ()

    def test_preview_contains_full_image_without_immersive_viewer(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter is unavailable: {exc}")
        try:
            from zvec_desktop.result_catalog import ResultCatalogError
            from zvec_desktop.ui import ZvecDesktopWindow
        except ImportError as exc:
            self.skipTest(f"desktop UI dependencies are unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._create_result_fixture(root)
            window: ZvecDesktopWindow | None = None
            try:
                window = ZvecDesktopWindow(
                    DesktopLaunchOptions(config_path=config_path, page_size=15),
                    autostart_backend=False,
                )
                window.root.geometry("1280x850+20+20")
                deadline = time.monotonic() + 8
                while window._page is None and time.monotonic() < deadline:
                    window.root.update()
                    time.sleep(0.02)

                self.assertIsNotNone(window._page)
                self.assertEqual(len(window._items), 15)
                self.assertEqual(window._preview.mode, "contain")
                self.assertEqual(window._selected_index, 0)
                self.assertFalse(hasattr(window, "_fullscreen_button"))
                self.assertFalse(hasattr(window, "open_fullscreen"))
                self.assertEqual(str(window._open_button["state"]), "normal")
                self.assertEqual(
                    str(window._folder_open_button["state"]),
                    "normal",
                )
                self.assertEqual(
                    str(window._backend_status_label["style"]),
                    "StatusMuted.TLabel",
                )
                self.assertEqual(
                    str(window._backend_status_pill["style"]),
                    "StatusPill.TFrame",
                )
                window._set_backend_status("正在启动…", "starting")
                self.assertEqual(
                    str(window._backend_status_label["style"]),
                    "StatusStarting.TLabel",
                )
                window._set_backend_status("等待启动", "muted")
                for _ in range(12):
                    window.root.update()
                    time.sleep(0.02)
                self.assertFalse(window._gallery._scrollbar_visible)
                self.assertEqual(len(window._nav_buttons), 4)
                self.assertTrue(str(window._nav_buttons[0]["text"]).startswith("⌕"))
                self.assertEqual(
                    str(window._nav_buttons[0]["style"]), "NavSelected.TButton"
                )
                self.assertTrue(
                    all(
                        window._notebook.bbox(index) == (0, 0, 0, 0)
                        for index in range(4)
                    )
                )
                self.assertEqual(str(window._query_entry["style"]), "Search.TEntry")
                self.assertEqual(str(window._search_button["style"]), "Primary.TButton")
                self.assertEqual(
                    str(window._task_summary_label["style"]),
                    "Badge.TLabel",
                )
                self.assertEqual(window._task_empty_state.winfo_manager(), "place")
                self.assertFalse(window._search_preview_collapsed)
                self.assertEqual(window._page_title_text.get(), "图片搜索")
                self.assertTrue(window._header_search_actions.winfo_ismapped())
                self.assertFalse(window._retry_backend_button.winfo_ismapped())
                self.assertTrue(window._navigation_logo.winfo_exists())
                window._set_backend_status("不可用", "error")
                window.root.update_idletasks()
                self.assertTrue(window._retry_backend_button.winfo_ismapped())
                window._set_backend_status("等待启动", "muted")
                window.root.update_idletasks()
                self.assertFalse(window._retry_backend_button.winfo_ismapped())

                window._select_main_page(1)
                window.root.update()
                self.assertEqual(window._page_title_text.get(), "图库任务")
                self.assertFalse(window._header_search_actions.winfo_ismapped())
                window._select_main_page(0)
                window.root.update()
                self.assertEqual(window._page_title_text.get(), "图片搜索")
                self.assertTrue(window._header_search_actions.winfo_ismapped())
                first_card = window._gallery._content.winfo_children()[0]
                caption = first_card.winfo_children()[1]
                self.assertGreaterEqual(
                    caption.winfo_height(), caption.winfo_reqheight()
                )

                window.root.geometry("760x600+20+20")
                for _ in range(15):
                    window.root.update()
                    time.sleep(0.02)
                self.assertTrue(window._navigation_compact)
                self.assertTrue(window._search_controls_compact)
                self.assertTrue(window._search_preview_collapsed)
                self.assertTrue(window._gallery._scrollbar_visible)
                self.assertNotIn("\n", str(window._nav_buttons[0]["text"]))
                self.assertEqual(str(window._nav_buttons[0]["text"]), "⌕ 搜索")

                window._previous_button.configure(state="normal")
                window._next_button.configure(state="normal")
                window._show_load_error(ResultCatalogError("测试载入失败"))
                self.assertIsNone(window._page)
                self.assertEqual(window._items, ())
                self.assertEqual(str(window._previous_button["state"]), "disabled")
                self.assertEqual(str(window._next_button["state"]), "disabled")

                active_cancel = window._load_cancel
                window.close()
                self.assertTrue(active_cancel.is_set())
                window.close()
                window = None
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if window is not None:
                    window.close()

    def test_title_close_hides_to_tray_and_explicit_exit_releases_it_once(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter is unavailable: {exc}")
        try:
            from zvec_desktop.ui import ZvecDesktopWindow
        except ImportError as exc:
            self.skipTest(f"desktop UI dependencies are unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temporary:
            config_path = self._create_result_fixture(Path(temporary))
            tray = self._FakeTray()
            window: ZvecDesktopWindow | None = None
            try:
                window = ZvecDesktopWindow(
                    DesktopLaunchOptions(config_path=config_path),
                    tray_service=tray,
                    autostart_backend=False,
                )
                deadline = time.monotonic() + 3
                while not tray.running and time.monotonic() < deadline:
                    window.root.update()
                    time.sleep(0.01)
                self.assertTrue(tray.running)

                window._handle_window_close()
                window.root.update()
                self.assertEqual(window.root.state(), "withdrawn")
                self.assertFalse(window._closed)
                self.assertEqual(len(tray.notifications), 1)

                window.show_window()
                window.root.update()
                self.assertNotEqual(window.root.state(), "withdrawn")
                window.close()
                self.assertTrue(window._closed)
                self.assertEqual(tray.stop_calls, 1)
                window = None
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if window is not None:
                    window.close()

    @staticmethod
    def _create_result_fixture(root: Path) -> Path:
        images = root / "图库"
        results = root / "结果"
        workspace = root / "工作区"
        output = results / "最近搜索"
        for directory in (images, results, workspace, output):
            directory.mkdir(parents=True, exist_ok=True)

        manifest_results: list[dict[str, object]] = []
        for index in range(1, 16):
            width, height = (900, 420) if index % 2 else (420, 900)
            name = f"角色动作_{index:02d}.png"
            path = images / name
            Image.new(
                "RGB",
                (width, height),
                (30 + index * 5, 80, 140),
            ).save(path)
            copied = output / name
            copied.write_bytes(path.read_bytes())
            manifest_results.append(
                {
                    "rank": index,
                    "copied_file": name,
                    "relative_path": name,
                    "library_id": "library-main",
                    "library_name": "人物图库",
                    "tags": ["人物", "动作"],
                    "matched_tags": ["动作"],
                    "confidence": 0.9,
                    "match_state": "high",
                    "rank_source": "text",
                }
            )

        (output / "results.json").write_text(
            json.dumps(
                {
                    "created_at": "2026-07-18T12:00:00+08:00",
                    "query_type": "text",
                    "status": "ok",
                    "library_ids": ["library-main"],
                    "results": manifest_results,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "results_directory": str(results),
                    "default_library_id": "library-main",
                    "libraries": [
                        {
                            "id": "library-main",
                            "name": "人物图库",
                            "image_root": str(images),
                            "workspace_directory": str(workspace),
                            "enabled": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return config_path


if __name__ == "__main__":
    unittest.main()
