from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from zvec_webview.image_registry import ImageRegistry
from zvec_webview.native_bridge import NativeBridge


class _Window:
    def __init__(self, selected: Path) -> None:
        self.selected = selected
        self.dialog_type = None

    def create_file_dialog(self, dialog_type, **_kwargs):
        self.dialog_type = dialog_type
        return (str(self.selected),)


class NativeBridgeTests(unittest.TestCase):
    def _registered_image(self, directory: str) -> tuple[ImageRegistry, str, Path]:
        path = Path(directory) / "native action.jpg"
        Image.new("RGB", (48, 64), (30, 90, 150)).save(path)
        registry = ImageRegistry()
        metadata = registry.register(path)
        return registry, metadata.image_id, path.resolve()

    def test_query_image_selection_does_not_return_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "query.jpg"
            Image.new("RGB", (48, 64), (30, 90, 150)).save(path)
            registry = ImageRegistry()
            bridge = NativeBridge(registry)
            window = _Window(path)
            bridge.attach_window(window)
            fake_webview = SimpleNamespace(
                FileDialog=SimpleNamespace(OPEN="open", FOLDER="folder")
            )

            with patch.dict(sys.modules, {"webview": fake_webview}):
                response = bridge.select_query_image()

            self.assertTrue(response["ok"])
            image = response["image"]
            self.assertEqual(response["id"], image["id"])
            self.assertEqual(response["name"], "query.jpg")
            self.assertEqual(image["name"], "query.jpg")
            self.assertNotIn(str(path), repr(response))
            self.assertEqual(registry.resolve(image["id"]), path.resolve())
            self.assertEqual(window.dialog_type, "open")

    def test_open_image_uses_default_program_and_suppresses_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, image_id, path = self._registered_image(temporary)
            bridge = NativeBridge(registry, native_action_cooldown=60)
            startfile = Mock()

            with (
                patch("zvec_webview.native_bridge.os.name", "nt"),
                patch(
                    "zvec_webview.native_bridge.os.startfile", startfile, create=True
                ),
            ):
                first = bridge.open_image(image_id)
                duplicate = bridge.open_image(image_id)

            self.assertEqual(first, {"ok": True, "action": "open"})
            self.assertFalse(duplicate["ok"])
            self.assertEqual(duplicate["code"], "duplicate_action")
            startfile.assert_called_once_with(path)

    def test_reveal_image_selects_file_in_explorer_and_suppresses_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, image_id, path = self._registered_image(temporary)
            bridge = NativeBridge(registry, native_action_cooldown=60)

            with (
                patch("zvec_webview.native_bridge.os.name", "nt"),
                patch(
                    "zvec_webview.native_bridge._select_file_with_shell",
                    return_value=True,
                ) as select_file,
            ):
                first = bridge.reveal_image(image_id)
                duplicate = bridge.reveal_image(image_id)

            self.assertEqual(
                first,
                {"ok": True, "action": "reveal", "selected": True},
            )
            self.assertFalse(duplicate["ok"])
            self.assertEqual(duplicate["code"], "duplicate_action")
            select_file.assert_called_once_with(path)

    def test_reveal_falls_back_to_parent_when_shell_selection_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, image_id, path = self._registered_image(temporary)
            bridge = NativeBridge(registry, native_action_cooldown=0)
            startfile = Mock()

            with (
                patch("zvec_webview.native_bridge.os.name", "nt"),
                patch(
                    "zvec_webview.native_bridge._select_file_with_shell",
                    return_value=False,
                ),
                patch(
                    "zvec_webview.native_bridge.os.startfile", startfile, create=True
                ),
            ):
                response = bridge.reveal_image(image_id)

            self.assertTrue(response["ok"])
            self.assertFalse(response["selected"])
            self.assertIn("已打开所在文件夹", response["message"])
            startfile.assert_called_once_with(path.parent)

    def test_batch_clipboard_actions_resolve_opaque_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, image_id, path = self._registered_image(temporary)
            bridge = NativeBridge(registry)

            with (
                patch("zvec_webview.native_bridge._copy_windows_image") as copy_image,
                patch("zvec_webview.native_bridge._copy_windows_files") as copy_files,
                patch(
                    "zvec_webview.native_bridge._copy_windows_clipboard"
                ) as copy_text,
            ):
                image_result = bridge.copy_image(image_id)
                files_result = bridge.copy_files([image_id, image_id])
                paths_result = bridge.copy_image_paths([image_id])

            self.assertTrue(image_result["ok"])
            self.assertEqual(files_result["count"], 1)
            self.assertEqual(paths_result["count"], 1)
            copy_image.assert_called_once_with(path)
            copy_files.assert_called_once_with((path,))
            copy_text.assert_called_once_with(str(path))

    def test_failed_native_action_returns_error_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, image_id, _path = self._registered_image(temporary)
            bridge = NativeBridge(registry, native_action_cooldown=60)
            startfile = Mock(side_effect=OSError("default program failed"))

            with (
                patch("zvec_webview.native_bridge.os.name", "nt"),
                patch(
                    "zvec_webview.native_bridge.os.startfile", startfile, create=True
                ),
            ):
                first = bridge.open_image(image_id)
                second = bridge.open_image(image_id)

            self.assertFalse(first["ok"])
            self.assertEqual(first["code"], "native_action_failed")
            self.assertIn("default program failed", first["error"])
            self.assertFalse(second["ok"])
            self.assertEqual(startfile.call_count, 2)


if __name__ == "__main__":
    unittest.main()
