from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from image_vector_service.backend_server import BackendJobManager, _normalize_job
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition
from image_vector_service.state import IndexState
from zvec_desktop.library_tasks import (
    AutoTagPolicyMigrateRequest,
    FolderDeleteCommitRequest,
    FolderDeletePreviewRequest,
    FolderImagesRequest,
    FolderListRequest,
    ManualTagBatchRequest,
    ManualTagSelection,
    ManualTagUndoRequest,
    SearchResultsCleanupRequest,
)


class _Service:
    def __init__(self, progress, cancel_check) -> None:
        self.progress = progress
        self.cancel_check = cancel_check
        self.stats_started = threading.Event()
        self.stats_release = threading.Event()
        self.calls: list[tuple[str, object]] = []

    def stats(self):
        self.stats_started.set()
        self.stats_release.wait(timeout=5)
        return {"tracked_files": 1}

    def manual_tag_batch(self, **kwargs):
        self.calls.append(("manual_tag_batch", kwargs))
        self.progress("Updated manual tags 2/2 images.")
        return {"updated": 1, "failed": 1, "failures": [{"doc_id": "bad"}]}

    def undo_latest_manual_tag_batch(self):
        self.calls.append(("manual_tag_undo", None))
        return {"undone": True, "restored": 1, "failed": 0}

    def cleanup_search_results(self, **kwargs):
        self.calls.append(("search_results_cleanup", kwargs))
        return {"deleted": 2, "skipped": 1, "failed": 0, "failures": []}

    def reconcile_pending_identity_tags(self, **kwargs):
        self.calls.append(("auto_tag_policy_migrate", kwargs))
        return {"migrated": 3, "api_requests": 0}

    def recover_folder_deletions(self, **kwargs):
        self.calls.append(("folder_delete_recovery", kwargs))
        return {"recovered": 0, "failed": 0, "failures": [], "api_requests": 0}

    def preview_folder_deletion(self, **kwargs):
        self.calls.append(("folder_delete_preview", kwargs))
        return {
            "operation_id": "a" * 32,
            "confirmation_token": "preview-token",
            "folder_key": kwargs["folder_key"],
            "image_count": 1,
            "file_count": 1,
            "blocked": False,
            "api_requests": 0,
        }

    def commit_folder_deletion(self, **kwargs):
        self.calls.append(("folder_delete_commit", kwargs))
        return {
            "operation_id": kwargs["operation_id"],
            "status": "committed",
            "indexed_deleted": 1,
            "failed": 0,
            "api_requests": 0,
        }

    def close(self) -> None:
        self.stats_release.set()


class _RecoveryWarningService(_Service):
    def recover_folder_deletions(self, **kwargs):
        self.calls.append(("folder_delete_recovery", kwargs))
        return {
            "recovered": 0,
            "failed": 1,
            "failures": [{"operation_id": "a" * 32, "error": "locked"}],
            "api_requests": 0,
        }


class FolderBatchBackendContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        workspace = root / "workspace"
        workspace.mkdir()
        images = root / "images"
        images.mkdir()
        state = IndexState(workspace / "image_collection.state.sqlite3")
        state.ensure_collection_uuid("collection-a")
        state.record_root("root-a", str(images), True)
        state.set_many(
            [
                {
                    "doc_id": "doc-a",
                    "root_id": "root-a",
                    "relative_path": "原神/雷电将军/a.jpg",
                    "file_name": "a.jpg",
                    "extension": "jpg",
                    "mime_type": "image/jpeg",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                    "mtime_ns": 2,
                    "width": 3,
                    "height": 4,
                    "tags": [],
                    "folder_tags": ["雷电将军"],
                    "accepted_auto_tags": [],
                    "inherited_tags": [],
                }
            ]
        )
        state.close()
        library = LibraryDefinition(
            "library-a",
            "Library A",
            images,
            workspace,
        )
        catalog = LibraryCatalog("library-a", (library,), root / "results")
        self.catalog = catalog
        self.service: _Service | None = None

        def factory(_library, progress, cancel_check):
            self.service = _Service(progress, cancel_check)
            return self.service

        self.manager = BackendJobManager(
            instance_id="test",
            config_fingerprint="f" * 64,
            config=ServiceConfig(workspace=workspace),
            library_catalog=catalog,
            library_service_factory=factory,
        )
        self.manager.start()
        self.addCleanup(self.manager.close)
        deadline = time.monotonic() + 3
        while self.service is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.service is not None

    def _wait(self, job_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.manager.get(job_id)
            if job["status"] in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                return job
            time.sleep(0.01)
        self.fail(f"job did not finish: {job_id}")

    def test_folder_reads_do_not_wait_for_the_library_worker(self) -> None:
        blocked = self.manager.submit(
            {"command": "stats", "params": {"library_id": "library-a"}}
        )
        assert self.service is not None
        self.assertTrue(self.service.stats_started.wait(timeout=2))

        folders = self.manager.submit(
            {
                "command": "folder_list",
                "params": {"library_id": "library-a"},
            }
        )
        completed = self._wait(folders["id"])
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual(completed["result"]["folders"][0]["root_id"], "root-a")
        self.assertEqual(self.manager.get(blocked["id"])["status"], "running")
        self.service.stats_release.set()
        self._wait(blocked["id"])

    def test_manual_and_cleanup_commands_are_validated_and_routed(self) -> None:
        requests = (
            FolderListRequest("library-a"),
            FolderImagesRequest("library-a", "zvec-folder-v1.fake"),
            ManualTagBatchRequest(
                "library-a",
                ManualTagSelection("selected", doc_ids=("doc-a",)),
                "add",
                ("原神", "雷电将军"),
            ),
            ManualTagUndoRequest("library-a"),
            SearchResultsCleanupRequest("library-a"),
            AutoTagPolicyMigrateRequest("library-a"),
            FolderDeletePreviewRequest("library-a", "zvec-folder-v1.fake"),
            FolderDeleteCommitRequest(
                "library-a", "a" * 32, "preview-token", confirm=True
            ),
        )
        for request in requests:
            with self.subTest(command=request.command):
                command, _params = _normalize_job(
                    {"command": request.command, "params": request.to_params()},
                    Path(self.temporary.name),
                )
                self.assertEqual(command, request.command)

        manual = self.manager.submit(
            {
                "command": "manual_tag_batch",
                "params": requests[2].to_params(),
            }
        )
        manual_job = self._wait(manual["id"])
        self.assertEqual(manual_job["status"], "partial")
        cleanup = self.manager.submit(
            {
                "command": "search_results_cleanup",
                "params": requests[4].to_params(),
            }
        )
        self.assertEqual(self._wait(cleanup["id"])["status"], "succeeded")
        policy = self.manager.submit(
            {
                "command": "auto_tag_policy_migrate",
                "params": requests[5].to_params(),
            }
        )
        policy_job = self._wait(policy["id"])
        self.assertEqual(policy_job["status"], "succeeded")
        self.assertEqual(policy_job["result"]["api_requests"], 0)
        capabilities = self.manager.version_info()["capabilities"]
        self.assertTrue(capabilities["folder_browser"])
        self.assertTrue(capabilities["manual_tag_batch"])
        self.assertTrue(capabilities["search_results_cleanup"])
        self.assertTrue(capabilities["auto_tag_policy_migrate"])
        self.assertTrue(capabilities["folder_delete_two_phase"])

        preview = self.manager.submit(
            {
                "command": "folder_delete_preview",
                "params": requests[6].to_params(),
            }
        )
        preview_job = self._wait(preview["id"])
        self.assertEqual(preview_job["status"], "succeeded")
        self.assertEqual(preview_job["result"]["api_requests"], 0)
        commit = self.manager.submit(
            {
                "command": "folder_delete_commit",
                "params": requests[7].to_params(),
            }
        )
        commit_job = self._wait(commit["id"])
        self.assertEqual(commit_job["status"], "succeeded")
        self.assertEqual(commit_job["result"]["api_requests"], 0)
        self.assertIn(
            ("folder_delete_recovery", {"library_id": "library-a"}),
            self.service.calls,
        )

    def test_recovery_warning_does_not_make_the_library_unavailable(self) -> None:
        warning_services: list[_RecoveryWarningService] = []

        def warning_factory(_library, progress, cancel):
            service = _RecoveryWarningService(progress, cancel)
            warning_services.append(service)
            return service

        warning_manager = BackendJobManager(
            instance_id="recovery-warning",
            config_fingerprint="e" * 64,
            config=ServiceConfig(workspace=Path(self.temporary.name) / "workspace"),
            library_catalog=self.catalog,
            library_service_factory=warning_factory,
        )
        warning_manager.start()
        self.addCleanup(warning_manager.close)
        deadline = time.monotonic() + 3
        health_status, health = warning_manager.health()
        while not health["service_ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
            health_status, health = warning_manager.health()

        self.assertEqual(health_status, 200)
        self.assertTrue(health["service_ready"])
        recovery = health["libraries"][0]["folder_delete_recovery"]
        self.assertEqual(recovery["failed"], 1)
        stats = warning_manager.submit(
            {"command": "stats", "params": {"library_id": "library-a"}}
        )
        self.assertTrue(warning_services[0].stats_started.wait(timeout=2))
        warning_services[0].stats_release.set()
        deadline = time.monotonic() + 3
        stats_job = warning_manager.get(stats["id"])
        while stats_job["status"] not in {"succeeded", "failed"}:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
            stats_job = warning_manager.get(stats["id"])
        self.assertEqual(stats_job["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
