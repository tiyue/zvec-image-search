from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from zvec_webview.image_registry import ImageRegistry
from zvec_webview.native_bridge import NativeBridge

_TERMINAL_EXPORT_STATES = {"succeeded", "partial", "failed"}


class _FolderWindow:
    def __init__(self, selected: Path | None) -> None:
        self.selected = selected
        self.dialog_type: object | None = None
        self.allow_multiple: bool | None = None

    def create_file_dialog(self, dialog_type: object, **kwargs: object) -> object:
        self.dialog_type = dialog_type
        self.allow_multiple = bool(kwargs.get("allow_multiple"))
        return () if self.selected is None else (str(self.selected),)


def _fake_webview() -> SimpleNamespace:
    return SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder", OPEN="open"))


class NativeExportTests(unittest.TestCase):
    def _registry(self) -> ImageRegistry:
        registry = ImageRegistry()
        self.addCleanup(registry.close)
        return registry

    def _register_image(
        self,
        registry: ImageRegistry,
        path: Path,
        color: tuple[int, int, int],
    ) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48, 64), color).save(path)
        return registry.register(path).image_id

    def _wait_for_export(
        self,
        bridge: NativeBridge,
        job_id: str,
        *,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        latest: dict[str, object] | None = None
        while time.monotonic() < deadline:
            response = bridge.export_status(job_id)
            self.assertTrue(response["ok"], response)
            latest = response["job"]
            if latest.get("status") in _TERMINAL_EXPORT_STATES:
                return latest
            time.sleep(0.01)
        self.fail(f"export did not finish before timeout; latest={latest!r}")

    def test_export_chooses_folder_runs_in_background_and_renames_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "chosen-export"
            destination.mkdir()
            registry = self._registry()
            first_id = self._register_image(
                registry,
                root / "source-a" / "portrait.jpg",
                (180, 40, 70),
            )
            second_id = self._register_image(
                registry,
                root / "source-b" / "portrait.jpg",
                (30, 100, 190),
            )
            window = _FolderWindow(destination)
            bridge = NativeBridge(registry)
            bridge.attach_window(window)
            copy_started = threading.Event()
            release_copy = threading.Event()
            original_copy = shutil.copy2

            def delayed_copy(source: Path, target: Path) -> str:
                copy_started.set()
                if not release_copy.wait(timeout=1.0):
                    raise TimeoutError("test did not release background export")
                return str(original_copy(source, target))

            with (
                patch.dict(sys.modules, {"webview": _fake_webview()}),
                patch(
                    "zvec_webview.native_bridge.shutil.copy2",
                    side_effect=delayed_copy,
                ),
            ):
                response = bridge.export_images([first_id, second_id])
                self.assertTrue(response["ok"], response)
                self.assertFalse(response["cancelled"])
                self.assertTrue(copy_started.wait(timeout=0.5))
                job = response["job"]
                job_id = str(job["id"])
                in_progress = bridge.export_status(job_id)
                self.assertIn(in_progress["job"]["status"], {"queued", "running"})
                release_copy.set()
                completed = self._wait_for_export(bridge, job_id, timeout=1.5)

            self.assertEqual(window.dialog_type, "folder")
            self.assertFalse(window.allow_multiple)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["processed"], 2)
            self.assertEqual(completed["exported"], 2)
            self.assertEqual(completed["skipped"], 0)
            self.assertTrue((destination / "portrait.jpg").is_file())
            self.assertTrue((destination / "portrait (2).jpg").is_file())

            public_state = repr((response, in_progress, completed))
            self.assertNotIn(str(destination.resolve()), public_state)
            self.assertNotIn(str((root / "source-a").resolve()), public_state)
            self.assertNotIn(str((root / "source-b").resolve()), public_state)
            self.assertEqual(completed["destination_name"], destination.name)

    def test_one_failed_image_does_not_stop_export_and_writes_safe_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "partial-export"
            destination.mkdir()
            registry = self._registry()
            missing_path = root / "missing" / "gone.jpg"
            missing_id = self._register_image(
                registry,
                missing_path,
                (140, 70, 30),
            )
            good_path = root / "available" / "good.jpg"
            good_id = self._register_image(
                registry,
                good_path,
                (20, 150, 90),
            )
            missing_path.unlink()
            window = _FolderWindow(destination)
            bridge = NativeBridge(registry)
            bridge.attach_window(window)

            with patch.dict(sys.modules, {"webview": _fake_webview()}):
                response = bridge.export_images([missing_id, good_id])
            self.assertTrue(response["ok"], response)
            job_id = str(response["job"]["id"])
            completed = self._wait_for_export(bridge, job_id)

            self.assertEqual(completed["status"], "partial")
            self.assertEqual(completed["processed"], 2)
            self.assertEqual(completed["exported"], 1)
            self.assertEqual(completed["skipped"], 1)
            self.assertTrue((destination / good_path.name).is_file())
            manifests = list(destination.glob("yaolens-export-errors-*.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["job_id"], job_id)
            self.assertEqual(len(manifest["errors"]), 1)

            public_state = repr((response, completed, manifest))
            self.assertNotIn(str(root.resolve()), public_state)
            self.assertNotIn(str(missing_path.resolve()), public_state)
            self.assertNotIn(str(good_path.resolve()), public_state)

    def test_cancelling_folder_selection_creates_no_export_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = self._registry()
            image_id = self._register_image(
                registry,
                root / "source" / "image.jpg",
                (80, 60, 170),
            )
            window = _FolderWindow(None)
            bridge = NativeBridge(registry)
            bridge.attach_window(window)

            with patch.dict(sys.modules, {"webview": _fake_webview()}):
                response = bridge.export_images([image_id])

            self.assertEqual(
                response,
                {"ok": True, "action": "export", "cancelled": True},
            )
            self.assertEqual(window.dialog_type, "folder")
            self.assertFalse(window.allow_multiple)


if __name__ == "__main__":
    unittest.main()
