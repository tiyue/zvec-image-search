from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.activity_store import ActivityStoreUnavailable
from zvec_host.configuration_service import DesktopConfigurationService
from zvec_host.credentials import SessionCredentialStore
from zvec_webview.facade import FacadeError, PreviewFacade


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class _JobClient:
    def __init__(self, job: Mapping[str, Any]) -> None:
        self._job = copy.deepcopy(dict(job))

    def get_job(self, job_id: str) -> dict[str, Any]:
        if job_id != self._job["id"]:
            raise KeyError(job_id)
        return copy.deepcopy(self._job)

    def list_jobs(
        self,
        *,
        active: bool | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        del limit
        if active and self._job.get("status") not in {
            "queued",
            "running",
            "cancelling",
            "cancel_requested",
        }:
            return {"jobs": []}
        return {"jobs": [copy.deepcopy(self._job)]}


class _ReadyHost:
    def __init__(self, client: _JobClient) -> None:
        self.client = client
        self.is_running = True

    def stop(self, *, force: bool = False) -> None:
        del force
        self.is_running = False


class WebviewActivityFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_home = self.root / "config-home"
        self.config_path = self.config_home / "config.json"
        self.image_root = self.root / "library-images"
        self.workspace = self.root / "library-workspace"
        self.results = self.root / "search-results"
        for directory in (self.image_root, self.workspace, self.results):
            directory.mkdir(parents=True)
        snapshot = DesktopConfigurationService(self.config_path).create_initial(
            self.image_root,
            name="Characters",
            workspace_directory=self.workspace,
            results_directory=self.results,
        )
        self.library_id = snapshot.configuration.default_library_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _facade(
        self,
        *,
        host: _IdleHost | _ReadyHost | None = None,
        credentials: SessionCredentialStore | None = None,
    ) -> PreviewFacade:
        return PreviewFacade(
            self.config_path,
            backend_host=host or _IdleHost(),  # type: ignore[arg-type]
            credential_store=credentials or SessionCredentialStore(),
            # The fake backend has no backend.lock-owning ActivityStore writer.
            observer_job_history_fallback=True,
        )

    def _activity_database_bytes(self) -> bytes:
        payload = bytearray()
        for path in sorted(self.config_home.glob("activity.sqlite3*")):
            if path.is_file():
                payload.extend(path.read_bytes())
        return bytes(payload)

    def test_model_concurrency_round_trips_through_the_facade(self) -> None:
        facade = self._facade()
        try:
            response = facade.assign_models(
                {
                    "embedding_model": "qwen3-vl-embedding",
                    "auto_tag_primary_model": "qwen3-vl-flash",
                    "auto_tag_escalation_model": "qwen3-vl-plus",
                    "embedding_concurrency": 6,
                    "auto_tag_concurrency": 1,
                }
            )

            self.assertEqual(response["embedding_concurrency"], 6)
            self.assertEqual(response["auto_tag_concurrency"], 1)
            self.assertFalse(response["restart_required"])
            settings = facade.settings()["models"]
            self.assertEqual(settings["embedding_concurrency"], 6)
            self.assertEqual(settings["auto_tag_concurrency"], 1)

            with self.assertRaises(FacadeError) as raised:
                facade.assign_models(
                    {
                        "embedding_model": "qwen3-vl-embedding",
                        "auto_tag_primary_model": "qwen3-vl-flash",
                        "auto_tag_escalation_model": "qwen3-vl-plus",
                        "embedding_concurrency": 3,
                        "auto_tag_concurrency": 4,
                    }
                )
            self.assertEqual(raised.exception.code, "invalid_request")
        finally:
            facade.close(force=True)

    def test_successful_settings_operations_are_queryable_without_secret_payloads(
        self,
    ) -> None:
        credentials = SessionCredentialStore()
        facade = self._facade(credentials=credentials)
        api_key = "sk-private-activity-test-9d2f6a0c"
        catalog_marker = "PRIVATE-CATALOG-DISPLAY-NAME-7FBC42"

        replacement_images = self.root / "replacement-images-private"
        replacement_workspace = self.root / "replacement-workspace-private"
        replacement_results = self.root / "replacement-results-private"
        for directory in (
            replacement_images,
            replacement_workspace,
            replacement_results,
        ):
            directory.mkdir()

        models_path = self.config_home / "models.json"
        model_payload = json.loads(models_path.read_text(encoding="utf-8"))
        model_payload["models"][1]["display_name"] = catalog_marker
        # The facade accepts the editor payload as one bounded gateway field;
        # use compact JSON so the transport's control-character guard applies.
        model_json = json.dumps(
            model_payload, ensure_ascii=False, separators=(",", ":")
        )

        try:
            facade.assign_models(
                {
                    "embedding_model": "qwen3-vl-embedding",
                    "auto_tag_primary_model": "qwen3-vl-flash",
                    "auto_tag_escalation_model": "qwen3-vl-plus",
                }
            )
            facade.replace_model_json({"json_text": model_json})
            facade.save_credentials({"api_key": api_key})
            facade.delete_credentials()
            facade.update_library(
                self.library_id,
                {
                    "name": "Updated Characters",
                    "image_root": str(replacement_images),
                    "workspace_directory": str(replacement_workspace),
                    "results_directory": str(replacement_results),
                },
            )

            page = facade.activity_logs(limit=100)
            events = {str(item["event"]) for item in page["items"]}
            self.assertTrue(
                {
                    "model_roles_updated",
                    "model_catalog_replaced",
                    "credentials_saved",
                    "credentials_deleted",
                    "library_updated",
                }.issubset(events)
            )
            settings_page = facade.activity_logs(category="settings", limit=100)
            self.assertEqual(
                {str(item["category"]) for item in settings_page["items"]},
                {
                    "model_configuration",
                    "credentials",
                    "library_configuration",
                },
            )
        finally:
            facade.close(force=True)

        database = self._activity_database_bytes()
        # Scan the SQLite database and any remaining WAL/SHM files.  Checking
        # only the API response would miss a secret retained in durable pages.
        for secret in (
            api_key,
            catalog_marker,
            model_json,
            str(replacement_images),
            str(replacement_workspace),
            str(replacement_results),
        ):
            with self.subTest(secret=secret[:48]):
                self.assertNotIn(secret.encode("utf-8"), database)

    def test_activity_query_failure_maps_to_503_and_logging_stays_best_effort(
        self,
    ) -> None:
        facade = self._facade()
        try:
            with patch.object(
                facade._activity,
                "list_operation_logs",
                side_effect=ActivityStoreUnavailable("locked"),
            ):
                with self.assertRaises(FacadeError) as caught:
                    facade.activity_logs()
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(caught.exception.code, "activity_store_unavailable")

            with patch.object(
                facade._activity,
                "list_job_history",
                side_effect=ActivityStoreUnavailable("locked"),
            ):
                with self.assertRaises(FacadeError) as caught:
                    facade.job_history()
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(caught.exception.code, "activity_store_unavailable")

            # A read-only ConfigHome, antivirus lock, or full disk may break the
            # audit sink.  The user's actual setting change must still commit.
            with patch.object(
                facade._activity,
                "log",
                side_effect=OSError("activity disk unavailable"),
            ):
                result = facade.update_library(
                    self.library_id, {"name": "Saved without activity log"}
                )
            self.assertEqual(result["updated_fields"], ["name"])
            snapshot = DesktopConfigurationService(self.config_path).load()
            assert snapshot is not None
            self.assertEqual(
                snapshot.configuration.by_id[self.library_id].name,
                "Saved without activity log",
            )
        finally:
            facade.close(force=True)

    def test_terminal_job_status_survives_facade_restart(self) -> None:
        job_id = "1234567890abcdef1234567890abcdef"
        job = {
            "id": job_id,
            "command": "auto_tag",
            "params": {"library_id": self.library_id},
            "status": "succeeded",
            "submitted_at": "2026-07-19T08:00:00+00:00",
            "started_at": "2026-07-19T08:00:01+00:00",
            "finished_at": "2026-07-19T08:00:03+00:00",
            "progress": {"processed": 12, "total": 12, "percent": 100},
            "failure_count": 0,
            "result": {"candidate_count": 12, "processed": 12, "failed": 0},
        }
        facade = self._facade(host=_ReadyHost(_JobClient(job)))
        facade._backend_state = "ready"
        try:
            view = facade.job(job_id)
            self.assertEqual(view["status"], "succeeded")
            first = facade.job_history(limit=10)
            self.assertEqual(first["items"][0]["job_id"], job_id)
            self.assertEqual(first["items"][0]["processed"], 12)
        finally:
            facade.close(force=True)

        reopened = self._facade()
        try:
            page = reopened.job_history(limit=10)
            persisted = next(item for item in page["items"] if item["job_id"] == job_id)
            self.assertEqual(persisted["status"], "succeeded")
            self.assertEqual(persisted["task_type"], "auto_tag")
            self.assertEqual(persisted["processed"], 12)
            self.assertEqual(persisted["total"], 12)
        finally:
            reopened.close(force=True)

    def test_image_failure_activity_keeps_relative_path_only(self) -> None:
        source = self.image_root / "Genshin" / "Raiden" / "failed.jpg"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (48, 72), (25, 50, 75)).save(source)
        relative_path = "Genshin/Raiden/failed.jpg"
        job_id = "fedcba0987654321fedcba0987654321"
        job = {
            "id": job_id,
            "command": "auto_tag",
            "params": {"library_id": self.library_id},
            "status": "partial",
            "submitted_at": "2026-07-19T09:00:00+00:00",
            "started_at": "2026-07-19T09:00:01+00:00",
            "finished_at": "2026-07-19T09:00:04+00:00",
            "progress": {"processed": 1, "total": 2, "percent": 50},
            "failure_count": 1,
            "result": {
                "candidate_count": 2,
                "processed": 1,
                "failed": 1,
                "error_images": [
                    {
                        "name": source.name,
                        "relative_path": relative_path,
                        "source_path": str(source),
                        "reason": "mock model refusal",
                    }
                ],
            },
        }
        facade = self._facade(host=_ReadyHost(_JobClient(job)))
        facade._backend_state = "ready"
        try:
            facade.job(job_id)
            logs = facade.activity_logs(
                category="image_failure", job_id=job_id, limit=10
            )
            self.assertEqual(logs["total_count"], 1)
            details = logs["items"][0]["details"]
            self.assertEqual(details["relative_path"], relative_path)
            self.assertNotIn("source_path", details)
            self.assertNotIn(str(source), repr(logs))
        finally:
            facade.close(force=True)

        database = self._activity_database_bytes()
        self.assertIn(relative_path.encode("utf-8"), database)
        self.assertNotIn(str(source).encode("utf-8"), database)


if __name__ == "__main__":
    unittest.main()
