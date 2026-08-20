from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image

from image_vector_service.search_learning_store import (
    SearchCandidateRecord,
    SearchSessionRecord,
)
from zvec_host.configuration_service import DesktopConfigurationService
from zvec_host.credentials import SessionCredentialStore
from zvec_webview.facade import FacadeError, PreviewFacade
from zvec_webview.image_registry import ImageRegistry
from zvec_webview.server import GatewayError, GatewayServer


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class _Facade:
    def __init__(self, registry: ImageRegistry) -> None:
        self.image_registry = registry
        self.search_body: dict[str, Any] | None = None
        self.folder_query: dict[str, Any] | None = None
        self.folder_image_query: dict[str, Any] | None = None
        self.folder_delete_query: dict[str, Any] | None = None
        self.folder_name_tag_query: dict[str, Any] | None = None
        self.folder_name_tag_settings_value = {
            "schema_version": 1,
            "blacklist": ["自拍", "V"],
            "revision": "a" * 64,
            "using_defaults": True,
        }
        self.job_history_query: dict[str, Any] | None = None
        self.activity_log_query: dict[str, Any] | None = None
        self.frontend_activities: list[dict[str, Any]] = []
        self.frontend_activity_failure = False
        self.history_request: dict[str, Any] | None = None
        self.activity_error: FacadeError | None = None
        self.migration_recovery_body: dict[str, Any] | None = None
        self.fixed_evaluation_body: dict[str, Any] | None = None
        self.lan_calls: list[tuple[str, object | None]] = []
        self.image_edit_calls: list[tuple[str, object]] = []

    def bootstrap(self) -> dict[str, Any]:
        return {
            "service": {"status": "ready", "backend_ready": True},
            "libraries": [],
            "models": {},
            "organize": {"proposals": [], "aliases": []},
        }

    def settings(self) -> dict[str, Any]:
        return {"credentials": {"configured": False}}

    def folder_name_tag_settings(self) -> dict[str, Any]:
        return dict(self.folder_name_tag_settings_value)

    def update_folder_name_tag_settings(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.folder_name_tag_settings_value = {
            **self.folder_name_tag_settings_value,
            "blacklist": list(payload["blacklist"]),
            "using_defaults": False,
        }
        return dict(self.folder_name_tag_settings_value)

    def _lan_status(self, message: str = "ready") -> dict[str, Any]:
        return {
            "enabled": True,
            "running": message != "stopped",
            "bind_host": "192.168.1.20",
            "port": 38522,
            "display_name": "Zvec test",
            "discovery_port": 38521,
            "address": "http://192.168.1.20:38522",
            "available_hosts": [],
            "pending_pairings": [],
            "device": None,
            "message": message,
        }

    def lan_access_status(self) -> dict[str, Any]:
        self.lan_calls.append(("status", None))
        return self._lan_status()

    def update_lan_access(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.lan_calls.append(("update", payload))
        return self._lan_status("updated")

    def start_lan_access(self) -> dict[str, Any]:
        self.lan_calls.append(("start", None))
        return self._lan_status("started")

    def stop_lan_access(self) -> dict[str, Any]:
        self.lan_calls.append(("stop", None))
        return self._lan_status("stopped")

    def approve_lan_pairing(self, pairing_id: str) -> dict[str, Any]:
        self.lan_calls.append(("approve", pairing_id))
        return self._lan_status("approved")

    def reject_lan_pairing(self, pairing_id: str) -> dict[str, Any]:
        self.lan_calls.append(("reject", pairing_id))
        return self._lan_status("rejected")

    def revoke_lan_device(self) -> dict[str, Any]:
        self.lan_calls.append(("revoke", None))
        return self._lan_status("revoked")

    def latest_results(self, *, page: int, page_size: int) -> dict[str, Any]:
        return {"id": "latest", "page": page, "page_size": page_size, "items": []}

    def search_history(self, *, limit: int) -> dict[str, Any]:
        self.history_request = {"kind": "list", "limit": limit}
        return {
            "items": [
                {
                    "id": "search-folder-1",
                    "label": "红色和服 室内",
                    "query_type": "text",
                    "created_at": "2026-07-22T17:00:00+08:00",
                    "total_items": 36,
                    "status": "succeeded",
                }
            ]
        }

    def historical_results(
        self, history_id: str, *, page: int, page_size: int
    ) -> dict[str, Any]:
        self.history_request = {
            "kind": "page",
            "history_id": history_id,
            "page": page,
            "page_size": page_size,
        }
        return {
            "id": f"history:{history_id}",
            "history_id": history_id,
            "page": page,
            "page_size": page_size,
            "items": [],
        }

    def submit_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_body = payload
        return {"id": "search-1", "status": "queued", "items": []}

    def search(self, operation_id: str, *, page: int, page_size: int) -> dict[str, Any]:
        return {
            "id": operation_id,
            "status": "succeeded",
            "page": page,
            "page_size": page_size,
            "items": [],
        }

    def cancel_search(self, operation_id: str) -> dict[str, Any]:
        return {"id": operation_id, "status": "cancelled"}

    def list_jobs(self) -> dict[str, Any]:
        return {"jobs": []}

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "job-1", "status": "queued", **payload}

    def job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "status": "running"}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "status": "cancelled"}

    @staticmethod
    def _image_edit_task(task_id: str = "a" * 32) -> dict[str, Any]:
        return {
            "id": task_id,
            "source_name": "source.png",
            "status": "queued",
            "stage": "queued",
            "prompt": "修改背景",
            "result": None,
            "error": None,
        }

    def image_edit_settings(self) -> dict[str, Any]:
        self.image_edit_calls.append(("settings", None))
        return {"configured": True, "output_directory": r"C:\Output"}

    def update_image_edit_settings(self, output_directory: str) -> dict[str, Any]:
        self.image_edit_calls.append(("update_settings", output_directory))
        return {"configured": True, "output_directory": output_directory}

    def submit_image_edit_multipart(
        self, content_type: str, body: bytes
    ) -> dict[str, Any]:
        self.image_edit_calls.append(("submit_multipart", (content_type, body)))
        return self._image_edit_task()

    def submit_registered_image_edit(
        self, image_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        self.image_edit_calls.append(("submit_registered", (image_id, metadata)))
        return self._image_edit_task()

    def list_image_edit_tasks(
        self, *, active: bool | None, limit: int
    ) -> dict[str, Any]:
        self.image_edit_calls.append(("list", (active, limit)))
        return {"tasks": [self._image_edit_task()], "count": 1, "total_count": 1}

    def get_image_edit_task(self, task_id: str) -> dict[str, Any]:
        self.image_edit_calls.append(("get", task_id))
        return self._image_edit_task(task_id)

    def cancel_image_edit_task(self, task_id: str) -> dict[str, Any]:
        self.image_edit_calls.append(("cancel", task_id))
        return {**self._image_edit_task(task_id), "status": "cancelled"}

    def abandon_image_edit_tasks(self) -> dict[str, Any]:
        self.image_edit_calls.append(("abandon", None))
        return {"abandoned_task_ids": ["a" * 32], "count": 1}

    def data_migration_recovery(self) -> dict[str, Any]:
        return {
            "recovery": {
                "pending": True,
                "operation_id": "migration-old",
                "migration_type": "schema",
                "backup_directory": "C:\\Backups\\migration-old",
                "stage": "migrate",
            }
        }

    def submit_data_migration_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.migration_recovery_body = payload
        return {
            "id": "recovery-job",
            "status": "queued",
            "stage": "recovery_queued",
        }

    def install_search_learning_evaluation(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.fixed_evaluation_body = payload
        return {
            "installed": True,
            "evaluation_set_id": "human-reviewed-v1",
            "external_api_calls": 0,
        }

    def job_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        task_type: str | None = None,
        library_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        if self.activity_error is not None:
            raise self.activity_error
        self.job_history_query = {
            "cursor": cursor,
            "limit": limit,
            "status": status,
            "task_type": task_type,
            "library_id": library_id,
            "query": query,
        }
        return {
            "items": [
                {"job_id": "job-new", "status": "running"},
                {"job_id": "job-old", "status": "succeeded"},
            ],
            "next_cursor": "next-job-cursor",
            "has_more": True,
            "total_count": 2,
        }

    def activity_logs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        level: str | None = None,
        category: str | None = None,
        library_id: str | None = None,
        job_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        if self.activity_error is not None:
            raise self.activity_error
        self.activity_log_query = {
            "cursor": cursor,
            "limit": limit,
            "level": level,
            "category": category,
            "library_id": library_id,
            "job_id": job_id,
            "query": query,
        }
        return {
            "items": [
                {
                    "sequence": 20,
                    "level": "warning",
                    "category": "image_failure",
                    "job_id": job_id,
                    "message": "图片处理失败",
                }
            ],
            "next_cursor": None,
            "has_more": False,
            "total_count": 1,
        }

    def record_frontend_activity(self, payload: dict[str, Any]) -> None:
        if self.frontend_activity_failure:
            raise RuntimeError("activity mirror unavailable")
        self.frontend_activities.append(payload)

    def assign_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def replace_model_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def save_credentials(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"configured": True}

    def delete_credentials(self) -> dict[str, Any]:
        return {"configured": False}

    def add_library(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"libraries": [payload]}

    def set_library_enabled(self, library_id: str, enabled: bool) -> dict[str, Any]:
        return {"id": library_id, "enabled": enabled}

    def set_default_library(self, library_id: str) -> dict[str, Any]:
        return {"id": library_id, "is_default": True}

    def update_library(
        self, library_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": library_id,
            "updated_fields": sorted(payload),
            "values": payload,
            "restart": {"required": True},
        }

    def tag_folders(
        self,
        library_id: str,
        *,
        query: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self.folder_query = {
            "library_id": library_id,
            "query": query,
            "offset": offset,
            "limit": limit,
        }
        return {
            "folders": [
                {
                    "folder_key": "root-1:characters/raiden",
                    "name": "raiden",
                    "image_count": 128,
                }
            ],
            "total": 1,
            "offset": offset,
            "limit": limit,
        }

    def folder_images(
        self,
        library_id: str,
        *,
        folder_key: str,
        page: int,
        page_size: int,
        include_subfolders: bool,
    ) -> dict[str, Any]:
        self.folder_image_query = {
            "library_id": library_id,
            "folder_key": folder_key,
            "page": page,
            "page_size": page_size,
            "include_subfolders": include_subfolders,
        }
        return {
            "folder_key": folder_key,
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
        }

    def preview_folder_delete(
        self,
        library_id: str,
        *,
        folder_key: str,
        include_subfolders: bool,
    ) -> dict[str, Any]:
        self.folder_delete_query = {
            "phase": "preview",
            "library_id": library_id,
            "folder_key": folder_key,
            "include_subfolders": include_subfolders,
        }
        return {
            "operation_id": "a" * 32,
            "confirmation_token": "preview-token",
            "folder_key": folder_key,
            "blocked": False,
            "api_requests": 0,
        }

    def preview_folder_name_tags(
        self,
        library_id: str,
        *,
        selection: dict[str, Any],
        mode: str = "normal",
        force: bool = True,
    ) -> dict[str, Any]:
        self.folder_name_tag_query = {
            "library_id": library_id,
            "selection": dict(selection),
            "mode": mode,
            "force": force,
        }
        return {
            "selected": 10,
            "changed": 8,
            "samples": [],
            "api_requests": 0,
        }

    def commit_folder_delete(
        self,
        library_id: str,
        *,
        operation_id: str,
        confirmation_token: str,
        confirm: bool,
    ) -> dict[str, Any]:
        self.folder_delete_query = {
            "phase": "commit",
            "library_id": library_id,
            "operation_id": operation_id,
            "confirmation_token": confirmation_token,
            "confirm": confirm,
        }
        return {"job": {"id": "job-delete", "status": "queued"}}


def _json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        return response.status, response.headers, json.loads(response.read().decode())


def _write_frontend_build(root: Path, *, script: str = "assets/app-H4sH.js") -> None:
    (root / "assets").mkdir(parents=True)
    (root / ".vite").mkdir()
    (root / script).write_text("window.ready = true;", encoding="utf-8")
    (root / "assets/app-C5sS.css").write_text("body { color: #111; }", encoding="utf-8")
    (root / "index.html").write_text(
        "<!doctype html><title>Zvec Preview</title>"
        f'<script type="module" src="./{script}"></script>'
        '<link rel="stylesheet" href="./assets/app-C5sS.css">',
        encoding="utf-8",
    )
    (root / ".vite/manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": script,
                    "src": "index.html",
                    "isEntry": True,
                    "css": ["assets/app-C5sS.css"],
                }
            }
        ),
        encoding="utf-8",
    )


class _RawSelectionRouteStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.thumbnail_error: str | None = None
        self.thumbnail_retryable = False

    def close(self) -> None:
        return None

    def get_project(self, project_id: str) -> dict[str, str] | None:
        return {"id": project_id} if project_id == "project-1" else None

    def list_members(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_members", (project_id,), kwargs))
        return {"members": [], "total": 0, "filtered": 0}

    def get_thumbnail_bytes(self, member_id: str, **kwargs: Any) -> Any:
        self.calls.append(("thumbnail", (member_id,), kwargs))
        return type(
            "ImageResult",
            (),
            {
                "data": b"thumbnail",
                "content_type": "image/jpeg",
                "error": self.thumbnail_error,
                "retryable": self.thumbnail_retryable,
                "warning": None,
            },
        )()

    def get_preview_bytes(self, member_id: str, **kwargs: Any) -> Any:
        self.calls.append(("preview", (member_id,), kwargs))
        return type(
            "ImageResult",
            (),
            {
                "data": b"preview",
                "content_type": "image/jpeg",
                "error": None,
                "retryable": False,
                "warning": None,
            },
        )()

    def start_folder_import(self, project_id: str, path: str) -> dict[str, Any]:
        self.calls.append(("start_import", (project_id, path), {}))
        return {
            "id": "import-job-1",
            "kind": "import",
            "project_id": project_id,
            "status": "queued",
        }

    def start_export(
        self,
        project_id: str,
        member_ids: list[str],
        destination: str,
    ) -> dict[str, Any]:
        self.calls.append(
            ("start_export", (project_id, tuple(member_ids), destination), {})
        )
        return {
            "id": "export-job-1",
            "kind": "export",
            "project_id": project_id,
            "status": "queued",
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.calls.append(("get_job", (job_id,), {}))
        if job_id == "missing":
            return None
        return {
            "id": job_id,
            "kind": "import",
            "project_id": "project-1",
            "status": "running",
            "phase": "scanning",
            "progress": {"registered": 2, "seen": 3},
        }

    def cancel_job(self, job_id: str) -> bool | None:
        self.calls.append(("cancel_job", (job_id,), {}))
        if job_id == "missing":
            return None
        return job_id != "completed"

    def get_source_status(self, member_id: str) -> dict[str, str] | None:
        self.calls.append(("source_status", (member_id,), {}))
        if member_id == "missing":
            return None
        return {
            "status": "refreshed",
            "code": "source_refreshed",
            "message": "refreshed",
        }

    def list_creative_looks(self) -> list[dict[str, str]]:
        return [{"id": "as_shot"}]

    def update_creative_look(self, member_id: str, look: str) -> bool:
        self.calls.append(("creative_look", (member_id, look), {}))
        return member_id != "missing"

    def cancel_project_work(self, project_id: str) -> None:
        self.calls.append(("cancel", (project_id,), {}))


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        assets = self.root / "assets"
        assets.mkdir()
        _write_frontend_build(assets)
        self.registry = ImageRegistry()
        self.facade = _Facade(self.registry)
        self.server = GatewayServer(
            self.facade,  # type: ignore[arg-type]
            assets_directory=assets,
            token="a" * 32,
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    def test_serves_same_origin_assets_and_bootstrap(self) -> None:
        with urlopen(self.server.url, timeout=5) as response:
            content = response.read().decode("utf-8")
            self.assertIn("Zvec Preview", content)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn(
                "default-src 'self'", response.headers["Content-Security-Policy"]
            )

        status, _headers, payload = _json(self.server.url + "api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(payload["service"]["backend_ready"])

        with urlopen(self.server.url + "assets/app-H4sH.js", timeout=5) as response:
            self.assertIn(b"window.ready", response.read())

    def test_default_gateway_serves_the_manifest_built_vue_application(self) -> None:
        server = GatewayServer(
            self.facade,  # type: ignore[arg-type]
            token="d" * 32,
        )
        try:
            server.start()
            with urlopen(server.url, timeout=5) as response:
                index = response.read().decode("utf-8")
            self.assertIn('<div id="app"></div>', index)
            self.assertRegex(index, r"\./assets/index-[A-Za-z0-9_-]+\.js")
        finally:
            server.stop()

    def test_rejects_remote_or_incomplete_vite_assets(self) -> None:
        for mode, expected in (("remote", "remote/CDN"), ("missing", "missing")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                assets = Path(temporary)
                _write_frontend_build(assets)
                if mode == "remote":
                    (assets / "index.html").write_text(
                        '<script src="https://cdn.example/app.js"></script>',
                        encoding="utf-8",
                    )
                else:
                    (assets / "assets/app-H4sH.js").unlink()
                with self.assertRaisesRegex(GatewayError, expected):
                    GatewayServer(
                        self.facade,  # type: ignore[arg-type]
                        assets_directory=assets,
                        token="c" * 32,
                    )

    def test_posts_length_delimited_json_and_scopes_token(self) -> None:
        status, _headers, payload = _json(
            self.server.url + "api/search",
            method="POST",
            body={"text": "原神", "top_k": 15},
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["id"], "search-1")
        self.assertEqual(self.facade.search_body, {"text": "原神", "top_k": 15})

        wrong = self.server.address.origin + "/wrong/api/bootstrap"
        with self.assertRaises(HTTPError) as caught:
            urlopen(wrong, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_raw_selection_routes_forward_filters_priorities_and_async_jobs(
        self,
    ) -> None:
        raw = _RawSelectionRouteStub()
        self.server._raw_selection = raw  # type: ignore[assignment]

        status, _headers, payload = _json(
            self.server.url
            + "api/raw-selection/projects/project-1/members?"
            + "star_mode=exact&star_value=3&color_labels=red%2Cblue&"
            + "exported=unexported&formats=arw%2Cjpeg&"
            + "orientations=landscape%2Csquare&sort=mtime_ns&dir=desc"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["filtered"], 0)
        self.assertEqual(
            raw.calls[-1],
            (
                "list_members",
                ("project-1",),
                {
                    "offset": 0,
                    "limit": 1000,
                    "star_mode": "exact",
                    "star_value": 3,
                    "color_labels": "red,blue",
                    "filename_contains": "",
                    "rated_filter": "all",
                    "exported_filter": "unexported",
                    "formats": "arw,jpeg",
                    "orientations": "landscape,square",
                    "sort_field": "mtime_ns",
                    "sort_direction": "desc",
                },
            ),
        )

        with urlopen(
            self.server.url
            + "api/raw-selection/members/member-1/thumbnail?priority=overscan&v=1",
            timeout=5,
        ) as response:
            self.assertEqual(response.read(), b"thumbnail")
        self.assertEqual(
            raw.calls[-1],
            ("thumbnail", ("member-1",), {"priority": "overscan"}),
        )

        with urlopen(
            self.server.url
            + "api/raw-selection/members/member-2/preview?"
            + "dw=1280&dh=720&look=as_shot&priority=compare&v=2",
            timeout=5,
        ) as response:
            self.assertEqual(response.read(), b"preview")
        self.assertEqual(
            raw.calls[-1],
            (
                "preview",
                ("member-2",),
                {
                    "display_width": 1280,
                    "display_height": 720,
                    "look": "as_shot",
                    "quality": "best",
                    "priority": "compare",
                },
            ),
        )

        import_folder = str(Path(tempfile.gettempdir()).resolve() / "camera-roll")
        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/projects/project-1/import-folder",
            method="POST",
            body={"path": import_folder},
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["id"], "import-job-1")
        self.assertEqual(
            raw.calls[-1],
            ("start_import", ("project-1", import_folder), {}),
        )

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/jobs/import-job-1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["progress"], {"registered": 2, "seen": 3})
        self.assertEqual(raw.calls[-1], ("get_job", ("import-job-1",), {}))

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/jobs/import-job-1/cancel",
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["cancel_requested"])
        self.assertEqual(raw.calls[-1], ("cancel_job", ("import-job-1",), {}))

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/members/member-2/source-status"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["code"], "source_refreshed")
        self.assertEqual(raw.calls[-1], ("source_status", ("member-2",), {}))

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/projects/project-1/export",
            method="POST",
            body={
                "member_ids": ["member-1", "member-2"],
                "destination": "D:\\temporary-export",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["id"], "export-job-1")
        self.assertEqual(
            raw.calls[-1],
            (
                "start_export",
                (
                    "project-1",
                    ("member-1", "member-2"),
                    "D:\\temporary-export",
                ),
                {},
            ),
        )

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/projects/project-1/cancel-work",
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(raw.calls[-1], ("cancel", ("project-1",), {}))

    def test_raw_selection_routes_reject_invalid_or_retryable_requests(self) -> None:
        raw = _RawSelectionRouteStub()
        self.server._raw_selection = raw  # type: ignore[assignment]

        status, _headers, payload = _json(self.server.url + "api/raw-selection/looks")
        self.assertEqual(status, 200)
        self.assertEqual(payload["looks"], [{"id": "as_shot"}])

        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + "api/raw-selection/members/member-1/creative-look",
                method="PATCH",
                body={"creative_look": "VV"},
            )
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

        status, _headers, payload = _json(
            self.server.url + "api/raw-selection/members/member-1/creative-look",
            method="PATCH",
            body={"creative_look": "as_shot"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            raw.calls[-1],
            ("creative_look", ("member-1", "as_shot"), {}),
        )

        invalid_urls = (
            "api/raw-selection/projects/project-1/members?unknown=1",
            "api/raw-selection/members/member-1/thumbnail?priority=urgent",
            "api/raw-selection/members/member-1/thumbnail?priority=current&priority=visible",
        )
        for relative in invalid_urls:
            with (
                self.subTest(relative=relative),
                self.assertRaises(HTTPError) as caught,
            ):
                urlopen(self.server.url + relative, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            payload = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "invalid_query")
            caught.exception.close()

        for relative, body in (
            (
                "api/raw-selection/members/member-1/rating",
                {"star_rating": True, "color_label": "red"},
            ),
            (
                "api/raw-selection/members/delete-permanent",
                {"member_ids": ["member-1"], "confirmed": 1},
            ),
        ):
            with (
                self.subTest(relative=relative),
                self.assertRaises(HTTPError) as caught,
            ):
                _json(
                    self.server.url + relative,
                    method="POST" if "delete" in relative else "PATCH",
                    body=body,
                )
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

        raw.thumbnail_error = "Image work queue is full; retry the request"
        raw.thumbnail_retryable = True
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                self.server.url
                + "api/raw-selection/members/member-1/thumbnail?priority=visible",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 503)
        caught.exception.close()

        for relative in (
            "api/raw-selection/jobs/missing",
            "api/raw-selection/members/missing/source-status",
        ):
            with (
                self.subTest(relative=relative),
                self.assertRaises(HTTPError) as caught,
            ):
                urlopen(self.server.url + relative, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_recommendation_routes_inject_viewer_and_hide_paths(self) -> None:
        source = self.root / "raiden.png"
        Image.new("RGB", (80, 120), (80, 50, 160)).save(source)

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def create_recommendations(
                self, viewer_id: str, request_id: str
            ) -> dict[str, Any]:
                self.calls.append(("create", viewer_id, request_id))
                return {
                    "request_id": request_id,
                    "batch_id": "batch-1",
                    "count": 1,
                    "items": [
                        {
                            "item_id": "item-1",
                            "source_path": str(source),
                            "vector": [0.2, 0.4],
                            "bucket": "quality",
                        }
                    ],
                }

            def mark_recommendations_shown(
                self, viewer_id: str, batch_id: str, event_id: str
            ) -> dict[str, Any]:
                self.calls.append(("shown", viewer_id, batch_id, event_id))
                return {"recorded": True}

            def record_recommendation_action(
                self,
                viewer_id: str,
                batch_id: str,
                event_id: str,
                item_id: str,
                action: str,
                metadata: dict[str, str] | None = None,
            ) -> dict[str, Any]:
                self.calls.append(
                    ("action", viewer_id, batch_id, event_id, item_id, action, metadata)
                )
                return {"recorded": False, "preference": "dislike"}

        client = Client()
        self.facade.config_home = self.root
        self.facade._ready_client = lambda: client  # type: ignore[attr-defined]
        status, _headers, created = _json(
            self.server.url + "api/recommendations",
            method="POST",
            body={"request_id": "request-1"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("source_path", str(created))
        self.assertNotIn("vector", created["items"][0])
        self.assertIn("media_id", created["items"][0])
        self.assertEqual(
            _json(
                self.server.url + "api/recommendations/batch-1/shown",
                method="POST",
                body={"event_id": "shown-1"},
            )[0],
            200,
        )
        action_status, _headers, action_response = _json(
            self.server.url + "api/recommendations/batch-1/actions",
            method="POST",
            body={
                "event_id": "action-1",
                "item_id": "item-1",
                "action": "like",
                "metadata": {"surface": "grid"},
            },
        )
        self.assertEqual(action_status, 200)
        self.assertEqual(
            action_response,
            {
                "ok": True,
                "event_id": "action-1",
                "recorded": False,
                "preference": "dislike",
            },
        )
        self.assertTrue((self.root / "desktop-recommendation-viewer.json").exists())
        viewer = client.calls[0][1]
        self.assertEqual(client.calls[1][1], viewer)
        self.assertEqual(client.calls[2][1], viewer)
        self.assertEqual(client.calls[2][-1], {"surface": "grid"})

        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + "api/recommendations",
                method="POST",
                body={"request_id": "request-2", "viewer_id": "browser-value"},
            )
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_lan_access_control_routes_remain_loopback_only(self) -> None:
        status, _headers, payload = _json(self.server.url + "api/lan-access")
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])

        body = {
            "enabled": True,
            "bind_host": "192.168.1.20",
            "port": 38522,
            "display_name": "Zvec test",
        }
        self.assertEqual(
            _json(
                self.server.url + "api/lan-access",
                method="PUT",
                body=body,
            )[0],
            200,
        )
        for route in (
            "api/lan-access/start",
            "api/lan-access/stop",
            "api/lan-access/pairings/pair-1/approve",
            "api/lan-access/pairings/pair-2/reject",
        ):
            self.assertEqual(_json(self.server.url + route, method="POST")[0], 200)
        self.assertEqual(
            _json(self.server.url + "api/lan-access/device", method="DELETE")[0],
            200,
        )
        self.assertEqual(
            self.facade.lan_calls,
            [
                ("status", None),
                ("update", body),
                ("start", None),
                ("stop", None),
                ("approve", "pair-1"),
                ("reject", "pair-2"),
                ("revoke", None),
            ],
        )

    def test_fixed_evaluation_import_route_forwards_the_selected_source(self) -> None:
        source_path = r"C:\Evaluation\fixed-evaluation.json"

        status, _headers, payload = _json(
            self.server.url + "api/search-learning/fixed-evaluation",
            method="POST",
            body={"source_path": source_path},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["installed"])
        self.assertEqual(payload["external_api_calls"], 0)
        self.assertEqual(
            self.facade.fixed_evaluation_body,
            {"source_path": source_path},
        )

    def test_serves_registered_images_without_path_disclosure(self) -> None:
        source = self.root / "person.jpg"
        Image.new("RGB", (80, 120), (90, 40, 120)).save(source)
        metadata = self.registry.register(source)

        with urlopen(
            self.server.url + f"api/image/{metadata.image_id}?variant=thumbnail",
            timeout=5,
        ) as response:
            content = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertTrue(response.headers["ETag"])
            self.assertNotIn(str(source).encode(), content)

    def test_accepts_dropped_query_image_as_bounded_binary_upload(self) -> None:
        output = io.BytesIO()
        Image.new("RGB", (96, 128), (80, 30, 160)).save(output, format="PNG")
        request = Request(
            self.server.url + "api/query-image?name=" + quote("雷电 将军.png"),
            data=output.getvalue(),
            headers={"Content-Type": "image/png"},
            method="POST",
        )

        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 201)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "雷电 将军.png")
        self.assertNotIn(str(self.root), repr(payload))
        resolved = self.registry.resolve(payload["id"])
        self.assertTrue(resolved.is_file())
        self.assertEqual(resolved.suffix, ".png")

    def test_image_edit_json_routes_forward_registry_ids_and_explicit_abandon(
        self,
    ) -> None:
        status, _headers, settings = _json(self.server.url + "api/image-edit/settings")
        self.assertEqual(status, 200)
        self.assertTrue(settings["configured"])
        self.assertEqual(
            _json(
                self.server.url + "api/image-edit/settings",
                method="PUT",
                body={"output_directory": r"D:\Generated"},
            )[0],
            200,
        )

        submission = {
            "source_image_id": "opaque-image-id",
            "model": "qwen-image-edit-plus",
            "prompt": "修改背景",
            "negative_prompt": "",
            "prompt_extend": True,
        }
        submitted = _json(
            self.server.url + "api/image-edit/tasks",
            method="POST",
            body=submission,
        )
        self.assertEqual(submitted[0], 202)
        self.assertEqual(submitted[2]["task"]["status"], "queued")
        self.assertIn(
            (
                "submit_registered",
                (
                    "opaque-image-id",
                    {
                        "model": "qwen-image-edit-plus",
                        "prompt": "修改背景",
                        "negative_prompt": "",
                        "prompt_extend": True,
                    },
                ),
            ),
            self.facade.image_edit_calls,
        )

        listed = _json(self.server.url + "api/image-edit/tasks?active=true&limit=2")
        self.assertEqual(listed[0], 200)
        self.assertEqual(self.facade.image_edit_calls[-1], ("list", (True, 2)))

        task_id = "b" * 32
        self.assertEqual(
            _json(self.server.url + f"api/image-edit/tasks/{task_id}")[0], 200
        )
        self.assertEqual(
            _json(
                self.server.url + f"api/image-edit/tasks/{task_id}",
                method="DELETE",
            )[2]["task"]["status"],
            "cancelled",
        )
        abandoned = _json(
            self.server.url + "api/image-edit/tasks/abandon",
            method="POST",
            body={"confirm": True},
        )
        self.assertEqual(abandoned[2]["count"], 1)

        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + "api/image-edit/tasks/abandon",
                method="POST",
                body={"confirm": False},
            )
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_image_edit_multipart_is_forwarded_without_browser_path_disclosure(
        self,
    ) -> None:
        boundary = "----gateway-image-edit-test"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="metadata"\r\n\r\n{}\r\n',
                f"--{boundary}\r\n".encode(),
                (
                    b'Content-Disposition: form-data; name="file"; '
                    b'filename="source.png"\r\n\r\n'
                ),
                b"image-bytes\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        content_type = f"multipart/form-data; boundary={boundary}"
        request = Request(
            self.server.url + "api/image-edit/tasks",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )

        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 202)
        self.assertEqual(payload["task"]["status"], "queued")
        self.assertEqual(
            self.facade.image_edit_calls[-1],
            ("submit_multipart", (content_type, body)),
        )
        self.assertNotIn(str(self.root), repr(payload))

    def test_search_history_routes_list_and_reopen_persisted_results(self) -> None:
        status, _headers, history = _json(
            self.server.url + "api/results/history?limit=8"
        )
        self.assertEqual(status, 200)
        self.assertEqual(history["items"][0]["label"], "红色和服 室内")
        self.assertEqual(self.facade.history_request, {"kind": "list", "limit": 8})

        status, _headers, page = _json(
            self.server.url
            + "api/results/history/"
            + quote("search-folder-1")
            + "?page=2&page_size=15"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["history_id"], "search-folder-1")
        self.assertEqual(
            self.facade.history_request,
            {
                "kind": "page",
                "history_id": "search-folder-1",
                "page": 2,
                "page_size": 15,
            },
        )

    def test_rejects_invalid_query_image_upload(self) -> None:
        request = Request(
            self.server.url + "api/query-image?name=broken.png",
            data=b"not an image",
            headers={"Content-Type": "image/png"},
            method="POST",
        )

        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()
        self.assertEqual(payload["error"]["code"], "invalid_query_image")

    def test_rejects_non_json_request_body(self) -> None:
        request = Request(
            self.server.url + "api/search",
            data=b"text=hello",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 415)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")
        caught.exception.close()

    def test_job_detail_and_submission_use_frontend_wrapper(self) -> None:
        status, _headers, submitted = _json(
            self.server.url + "api/jobs",
            method="POST",
            body={"task_type": "stats", "library_id": "lib-test"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(submitted["job"]["id"], "job-1")

        status, _headers, loaded = _json(self.server.url + "api/jobs/job-1")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["job"]["status"], "running")

    def test_activity_routes_forward_cursor_and_bounded_filters(self) -> None:
        status, _headers, history = _json(
            self.server.url
            + "api/job-history?cursor=opaque-job&limit=200&status=running"
            + "&task_type=auto_tag&library_id=lib-test&query=failed"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["job_id"] for item in history["items"]],
            ["job-new", "job-old"],
        )
        self.assertEqual(history["next_cursor"], "next-job-cursor")
        self.assertEqual(
            self.facade.job_history_query,
            {
                "cursor": "opaque-job",
                "limit": 200,
                "status": "running",
                "task_type": "auto_tag",
                "library_id": "lib-test",
                "query": "failed",
            },
        )

        status, _headers, logs = _json(
            self.server.url
            + "api/activity-logs?cursor=opaque-log&limit=100&level=warning"
            + "&category=image_failure&library_id=lib-test&job_id=job-new"
            + "&query="
            + quote("无法解码")
        )
        self.assertEqual(status, 200)
        self.assertEqual(logs["items"][0]["sequence"], 20)
        self.assertEqual(
            self.facade.activity_log_query,
            {
                "cursor": "opaque-log",
                "limit": 100,
                "level": "warning",
                "category": "image_failure",
                "library_id": "lib-test",
                "job_id": "job-new",
                "query": "无法解码",
            },
        )

    def test_activity_routes_reject_invalid_or_duplicate_query_parameters(self) -> None:
        cases = (
            "api/job-history?limit=0",
            "api/job-history?limit=201",
            "api/job-history?limit=10&limit=20",
            "api/job-history?cursor=first&cursor=second",
            "api/job-history?unknown=value",
            "api/activity-logs?level=warning&level=error",
            "api/activity-logs?job_id=" + "x" * 129,
            "api/activity-logs?query=" + "x" * 257,
        )
        for path in cases:
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                _json(self.server.url + path)
            self.assertEqual(caught.exception.code, 400)
            payload = json.loads(caught.exception.read().decode("utf-8"))
            caught.exception.close()
            self.assertEqual(payload["error"]["code"], "invalid_query")

        self.assertIsNone(self.facade.job_history_query)
        self.assertIsNone(self.facade.activity_log_query)

    def test_activity_routes_are_exact_and_preserve_facade_error_status(self) -> None:
        with self.assertRaises(HTTPError) as extra_path:
            _json(self.server.url + "api/job-history/duplicate")
        self.assertEqual(extra_path.exception.code, 404)
        extra_path.exception.close()

        wrong_token = self.server.address.origin + "/wrong/api/activity-logs"
        with self.assertRaises(HTTPError) as unauthorized:
            _json(wrong_token)
        self.assertEqual(unauthorized.exception.code, 404)
        unauthorized.exception.close()

        self.facade.activity_error = FacadeError(
            "activity_store_unavailable",
            "活动数据库暂不可用。",
            status=503,
        )
        with self.assertRaises(HTTPError) as unavailable:
            _json(self.server.url + "api/activity-logs?limit=50")
        self.assertEqual(unavailable.exception.code, 503)
        payload = json.loads(unavailable.exception.read().decode("utf-8"))
        unavailable.exception.close()
        self.assertEqual(payload["error"]["code"], "activity_store_unavailable")

    def test_frontend_diagnostic_activity_mirror_is_best_effort(self) -> None:
        secret = "sk-private-value-123"
        body = {
            "event": "search_completed",
            "level": "info",
            "message": (
                "done\r\nwithout injection "
                f"Bearer abcdefghijklmnop {self.server.address.token} "
                f"api_key={secret} C:\\Users\\person\\private.jpg"
            ),
            "details": {"result_count": 15, "api_key": secret},
        }
        status, _headers, accepted = _json(
            self.server.url + "api/diagnostics/frontend",
            method="POST",
            body=body,
        )
        self.assertEqual(status, 202)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(self.facade.frontend_activities), 1)
        mirrored = self.facade.frontend_activities[0]
        serialized = json.dumps(mirrored, ensure_ascii=False)
        self.assertIn("done\\nwithout injection", mirrored["message"])
        self.assertNotIn(self.server.address.token, serialized)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("C:\\Users\\person", serialized)
        self.assertEqual(mirrored["details"]["api_key"], "<redacted>")

        self.facade.frontend_activity_failure = True
        status, _headers, accepted = _json(
            self.server.url + "api/diagnostics/frontend",
            method="POST",
            body={
                "event": "window_error",
                "level": "error",
                "message": "still acknowledged",
            },
        )
        self.assertEqual(status, 202)
        self.assertTrue(accepted["accepted"])

    def test_library_update_route_keeps_legacy_routes_compatible(self) -> None:
        status, _headers, updated = _json(
            self.server.url + "api/libraries/lib-test",
            method="PUT",
            body={
                "name": "Cosplay",
                "results_directory": str(self.root / "results"),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["id"], "lib-test")
        self.assertEqual(updated["updated_fields"], ["name", "results_directory"])

        status, _headers, enabled = _json(
            self.server.url + "api/libraries/lib-test/enabled",
            method="PUT",
            body={"enabled": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(enabled["enabled"])

        status, _headers, selected = _json(
            self.server.url + "api/libraries/lib-test/default",
            method="PUT",
            body={},
        )
        self.assertEqual(status, 200)
        self.assertTrue(selected["is_default"])

    def test_folder_browser_routes_preserve_server_side_selection_scope(self) -> None:
        status, _headers, folders = _json(
            self.server.url
            + "api/libraries/lib-test/folders?query=raiden&offset=5&limit=50"
        )
        self.assertEqual(status, 200)
        self.assertEqual(folders["folders"][0]["image_count"], 128)
        self.assertEqual(
            self.facade.folder_query,
            {
                "library_id": "lib-test",
                "query": "raiden",
                "offset": 5,
                "limit": 50,
            },
        )

        folder_key = quote("root-1:characters/raiden", safe="")
        status, _headers, images = _json(
            self.server.url
            + "api/libraries/lib-test/folder-images"
            + f"?folder_key={folder_key}&page=2&page_size=100"
            + "&include_subfolders=false"
        )
        self.assertEqual(status, 200)
        self.assertEqual(images["folder_key"], "root-1:characters/raiden")
        self.assertEqual(
            self.facade.folder_image_query,
            {
                "library_id": "lib-test",
                "folder_key": "root-1:characters/raiden",
                "page": 2,
                "page_size": 100,
                "include_subfolders": False,
            },
        )

    def test_folder_delete_routes_keep_preview_and_commit_distinct(self) -> None:
        status, _headers, preview = _json(
            self.server.url + "api/libraries/lib-test/folder-delete/preview",
            method="POST",
            body={
                "folder_key": "zvec-folder-v1.preview",
                "include_subfolders": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["operation_id"], "a" * 32)
        self.assertEqual(preview["api_requests"], 0)
        self.assertEqual(
            self.facade.folder_delete_query,
            {
                "phase": "preview",
                "library_id": "lib-test",
                "folder_key": "zvec-folder-v1.preview",
                "include_subfolders": True,
            },
        )

        status, _headers, commit = _json(
            self.server.url + "api/libraries/lib-test/folder-delete/commit",
            method="POST",
            body={
                "operation_id": "a" * 32,
                "confirmation_token": "preview-token",
                "confirm": True,
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(commit["job"]["id"], "job-delete")
        self.assertEqual(
            self.facade.folder_delete_query,
            {
                "phase": "commit",
                "library_id": "lib-test",
                "operation_id": "a" * 32,
                "confirmation_token": "preview-token",
                "confirm": True,
            },
        )

    def test_folder_name_tag_preview_route_keeps_the_scope_object(self) -> None:
        status, _headers, preview = _json(
            self.server.url + "api/libraries/lib-test/folder-name-tags/preview",
            method="POST",
            body={
                "selection": {
                    "mode": "folder",
                    "folder_key": "zvec-folder-v1.preview",
                    "include_subfolders": True,
                }
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["changed"], 8)
        self.assertEqual(
            self.facade.folder_name_tag_query,
            {
                "library_id": "lib-test",
                "selection": {
                    "mode": "folder",
                    "folder_key": "zvec-folder-v1.preview",
                    "include_subfolders": True,
                },
                "mode": "normal",
                "force": True,
            },
        )

    def test_folder_name_tag_settings_routes_keep_the_blacklist_array(self) -> None:
        status, _headers, initial = _json(
            self.server.url + "api/folder-name-tag-settings"
        )
        self.assertEqual(status, 200)
        self.assertEqual(initial["blacklist"], ["自拍", "V"])

        status, _headers, saved = _json(
            self.server.url + "api/folder-name-tag-settings",
            method="PUT",
            body={"blacklist": ["图包", "V"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["blacklist"], ["图包", "V"])
        self.assertFalse(saved["using_defaults"])

    def test_data_migration_recovery_routes_are_separate_from_job_lookup(self) -> None:
        status, _headers, payload = _json(
            self.server.url + "api/data-migrations/recovery"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["recovery"]["operation_id"], "migration-old")

        status, _headers, submitted = _json(
            self.server.url + "api/data-migrations/recovery/restore",
            method="POST",
            body={
                "operation_id": "migration-old",
                "confirmation_phrase": "RESTORE",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(submitted["migration"]["id"], "recovery-job")
        self.assertEqual(
            self.facade.migration_recovery_body,
            {
                "operation_id": "migration-old",
                "confirmation_phrase": "RESTORE",
            },
        )


class PersistentLibraryGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        assets = self.root / "assets"
        assets.mkdir()
        _write_frontend_build(assets)
        self.images = self.root / "images"
        self.images.mkdir()
        self.config_path = self.root / "config.json"
        self.configuration = DesktopConfigurationService(self.config_path)
        snapshot = self.configuration.create_initial(self.images, name="Original")
        self.library_id = snapshot.configuration.default_library_id
        self.facade = PreviewFacade(
            self.config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        self.server = GatewayServer(
            self.facade, assets_directory=assets, token="b" * 32
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.facade.close(force=True)
        self.temporary.cleanup()

    def test_data_migration_api_rejects_escaping_library_id(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + "api/data-migrations/precheck",
                method="POST",
                body={
                    "dry_run": True,
                    "request": {
                        "migration_type": "schema",
                        "source": str(self.root),
                        "target": str(self.root),
                        "library_id": "../../escaped",
                        "automatic_backup": True,
                    },
                },
            )

        self.assertEqual(caught.exception.code, 400)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()
        self.assertEqual(payload["error"]["code"], "invalid_data_migration")
        self.assertIn("safe single-segment", payload["error"]["message"])

    def test_put_persists_library_paths_and_global_results_directory(self) -> None:
        replacement_images = self.root / "replacement-images"
        replacement_images.mkdir()
        workspace = self.root / "replacement-workspace"
        results = self.root / "replacement-results"

        status, _headers, payload = _json(
            self.server.url + f"api/libraries/{self.library_id}",
            method="PUT",
            body={
                "name": "Cosplay",
                "image_root": str(replacement_images),
                "workspace_directory": str(workspace),
                "results_directory": str(results),
                "enabled": True,
                "is_default": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["restart_required"])
        self.assertEqual(payload["restart"]["backend_status"], "idle")
        persisted = DesktopConfigurationService(self.config_path).load()
        assert persisted is not None
        library = persisted.configuration.by_id[self.library_id]
        self.assertEqual(library.name, "Cosplay")
        self.assertEqual(library.image_root, replacement_images.resolve())
        self.assertEqual(library.workspace_directory, workspace.resolve())
        self.assertEqual(persisted.configuration.results_directory, results.resolve())
        self.assertEqual(persisted.configuration.default_library_id, self.library_id)

        status, _headers, settings = _json(self.server.url + "api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings["results_directory"], str(results.resolve()))

    def test_invalid_put_returns_structured_error_and_preserves_config(self) -> None:
        before = self.config_path.read_bytes()
        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + f"api/libraries/{self.library_id}",
                method="PUT",
                body={"workspace_directory": "relative-workspace"},
            )
        self.assertEqual(caught.exception.code, 409)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()
        self.assertEqual(payload["error"]["code"], "library_save_failed")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIsNotNone(DesktopConfigurationService(self.config_path).load())

    def test_activity_routes_use_the_real_persistent_facade(self) -> None:
        self.assertTrue(
            self.facade.record_frontend_activity(
                {
                    "event": "window_error",
                    "level": "error",
                    "message": "bounded frontend failure",
                    "details": {"recoverable": True},
                }
            )
        )

        status, _headers, logs = _json(
            self.server.url + "api/activity-logs?limit=1&level=error&category=frontend"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(logs["items"]), 1)
        self.assertEqual(logs["items"][0]["event"], "window_error")
        self.assertEqual(logs["items"][0]["category"], "frontend")
        self.assertIn("next_cursor", logs)

        status, _headers, history = _json(self.server.url + "api/job-history?limit=1")
        self.assertEqual(status, 200)
        self.assertIsInstance(history["items"], list)
        self.assertIn("next_cursor", history)

        with self.assertRaises(HTTPError) as invalid_cursor:
            _json(self.server.url + "api/job-history?cursor=not-a-valid-cursor")
        self.assertEqual(invalid_cursor.exception.code, 400)
        payload = json.loads(invalid_cursor.exception.read().decode("utf-8"))
        invalid_cursor.exception.close()
        self.assertEqual(payload["error"]["code"], "invalid_activity_cursor")

    def test_search_learning_routes_are_strict_durable_and_separate(self) -> None:
        self.facade._search_learning.store.record_search(  # noqa: SLF001
            SearchSessionRecord(
                session_id="search-feedback-route",
                query_type="text",
                requested_count=15,
                returned_count=1,
                library_ids=(self.library_id,),
                latency_ms=12,
                query_text="must not persist by default",
                candidates=(
                    SearchCandidateRecord(
                        library_id=self.library_id,
                        doc_id="doc-feedback-route",
                        original_rank=1,
                        displayed_rank=1,
                        displayed=True,
                        ranking_score=0.8,
                        features={"vector_confidence": 0.8},
                    ),
                ),
            )
        )
        status, _headers, feedback = _json(
            self.server.url + "api/search-feedback",
            method="POST",
            body={
                "session_id": "search-feedback-route",
                "library_id": self.library_id,
                "doc_id": "doc-feedback-route",
                "action": "relevant",
                "source": "context_menu",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(feedback["feedback_weight"], 1.0)

        status, _headers, learning = _json(
            self.server.url + "api/search-learning/status"
        )
        self.assertEqual(status, 200)
        self.assertEqual(learning["database"], "search-learning.sqlite3")
        self.assertEqual(learning["training_counts"]["explicit_samples"], 1)
        self.assertFalse(learning["settings"]["save_query_text"])
        self.assertTrue((self.root / "search-learning.sqlite3").is_file())
        self.assertTrue((self.root / "activity.sqlite3").is_file())

        status, _headers, page = _json(
            self.server.url
            + "api/search-feedback?session_id=search-feedback-route&limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["event_id"], feedback["event_id"])

        status, _headers, revoked = _json(
            self.server.url + f"api/search-feedback/{feedback['event_id']}",
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertFalse(revoked["active"])

        with self.assertRaises(HTTPError) as caught:
            _json(
                self.server.url + "api/search-feedback",
                method="POST",
                body={
                    "session_id": "search-feedback-route",
                    "library_id": self.library_id,
                    "doc_id": "doc-feedback-route",
                    "action": "relevant",
                    "feedback_weight": 99,
                },
            )
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()
        self.assertEqual(error["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
