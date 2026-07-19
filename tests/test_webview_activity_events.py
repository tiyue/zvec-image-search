from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.activity_store import ActivityStore
from zvec_desktop.configuration_service import DesktopConfigurationService
from zvec_desktop.credentials import SessionCredentialStore
from zvec_webview.facade import FacadeError, PreviewFacade


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class WebviewActivityEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_webview_activity_events_"
        )
        self.root = Path(self.temporary.name).resolve()
        self.config_path = self.root / "config-home" / "config.json"
        self.images = self.root / "images"
        self.images.mkdir(parents=True)
        snapshot = DesktopConfigurationService(self.config_path).create_initial(
            self.images,
            name="人物图库",
        )
        self.library_id = snapshot.configuration.default_library_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _facade(self, store: ActivityStore) -> PreviewFacade:
        return PreviewFacade(
            self.config_path,
            backend_host=_IdleHost(),
            credential_store=SessionCredentialStore(),
            activity_store=store,
            observer_job_history_fallback=True,
        )

    def test_settings_events_are_complete_without_secrets_or_local_paths(self) -> None:
        store = ActivityStore(self.config_path.parent)
        facade = self._facade(store)
        api_key = "opaque-new-key-that-must-never-enter-activity"
        second_root = self.root / "cosplay-images"
        second_root.mkdir()
        results_directory = self.root / "private-results"
        try:
            model_snapshot = facade._models.load(create=True)
            facade.assign_models(
                {
                    "embedding_model": model_snapshot.embedding_model,
                    "auto_tag_primary_model": (model_snapshot.auto_tag_primary_model),
                    "auto_tag_escalation_model": (
                        model_snapshot.auto_tag_escalation_model
                    ),
                }
            )
            facade.replace_model_json({"json_text": model_snapshot.json_text})
            facade.save_credentials({"api_key": api_key})
            with patch.object(facade, "start_backend_async", return_value=None):
                added = facade.add_library(
                    {
                        "name": "Cosplay",
                        "image_root": str(second_root),
                        "results_directory": str(results_directory),
                    }
                )
            second_id = next(
                item["id"]
                for item in added["libraries"]
                if item["id"] != self.library_id
            )
            facade.set_default_library(second_id)
            facade.set_library_enabled(self.library_id, False)
            facade.update_library(
                second_id,
                {
                    "name": "Cosplay 精选",
                    "results_directory": str(self.root / "new-private-results"),
                },
            )
            facade.delete_credentials()

            self.assertTrue(store.flush(timeout=2.0))
            page = store.list_operation_logs(limit=200)
            events = {item["event"] for item in page["items"]}
            self.assertTrue(
                {
                    "model_roles_updated",
                    "model_catalog_replaced",
                    "credentials_saved",
                    "credentials_deleted",
                    "library_added",
                    "default_library_changed",
                    "library_enabled_changed",
                    "library_updated",
                }.issubset(events)
            )

            serialized = repr(page)
            self.assertNotIn(api_key, serialized)
            self.assertNotIn(model_snapshot.json_text, serialized)
            for local_path in (
                second_root,
                results_directory,
                self.root / "new-private-results",
            ):
                self.assertNotIn(str(local_path), serialized)

            store.flush(timeout=2.0)
            raw = b"".join(
                candidate.read_bytes()
                for candidate in (
                    store.path,
                    store.path.with_name(f"{store.path.name}-wal"),
                )
                if candidate.is_file()
            )
            self.assertNotIn(api_key.encode(), raw)
            self.assertNotIn(str(second_root).encode(), raw)
            self.assertNotIn(str(results_directory).encode(), raw)
        finally:
            facade.close(force=True)
            store.close()

    def test_image_failure_event_keeps_only_safe_relative_path(self) -> None:
        store = ActivityStore(self.config_path.parent)
        facade = self._facade(store)
        absolute = self.root / "private" / "broken.jpg"
        try:
            facade._record_job_snapshot(
                {
                    "id": "a" * 32,
                    "command": "index",
                    "params": {"library_id": self.library_id},
                    "status": "failed",
                    "failure_count": 1,
                    "result": {
                        "failed": 1,
                        "error_images": [
                            {
                                "name": "broken.jpg",
                                "relative_path": "人物/broken.jpg",
                                "path": str(absolute),
                                "reason": "图片解码失败",
                            }
                        ],
                    },
                }
            )
            self.assertTrue(store.flush(timeout=2.0))
            page = store.list_operation_logs(
                category="image_failure",
                job_id="a" * 32,
            )
            self.assertEqual(page["total_count"], 1)
            details = page["items"][0]["details"]
            self.assertEqual(details["relative_path"], "人物/broken.jpg")
            self.assertNotIn(str(absolute), repr(page))
        finally:
            facade.close(force=True)
            store.close()

    def test_unavailable_activity_database_does_not_block_settings(self) -> None:
        blocking_file = self.root / "not-a-directory"
        blocking_file.write_text("file", encoding="utf-8")
        store = ActivityStore(blocking_file / "config-home")
        self.assertFalse(store.available)
        facade = self._facade(store)
        try:
            updated = facade.update_library(
                self.library_id,
                {"name": "仍可保存"},
            )
            self.assertEqual(updated["libraries"][0]["name"], "仍可保存")
            with self.assertRaises(FacadeError) as caught:
                facade.activity_logs(limit=10)
            self.assertEqual(caught.exception.code, "activity_store_unavailable")
            self.assertEqual(caught.exception.status, 503)
        finally:
            facade.close(force=True)
            store.close()


if __name__ == "__main__":
    unittest.main()
