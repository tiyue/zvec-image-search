from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.data_migration import (
    DataMigrationExecutionError,
    DataMigrationRequest,
)
from image_vector_service.search_learning_service import SearchLearningServiceError
from image_vector_service.state import IndexState
from zvec_desktop.configuration_service import DesktopConfigurationService
from zvec_desktop.credentials import SessionCredentialStore
from zvec_desktop.library_tasks import LibraryTaskService
from zvec_desktop.result_catalog import ResultCatalog, SearchResult
from zvec_webview import app
from zvec_webview.facade import (
    FacadeError,
    PreviewFacade,
    _library_request,
    _search_request,
)


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class _CompletedFolderDeleteClient:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self.jobs: dict[str, tuple[str, dict[str, Any]]] = {}

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        values = dict(params or {})
        job_id = f"{len(self.submissions) + 1:032x}"
        self.submissions.append((command, values))
        self.jobs[job_id] = (command, values)
        return {"id": job_id, "command": command, "status": "queued"}

    def get_job(self, job_id: str) -> dict[str, Any]:
        command, params = self.jobs[job_id]
        if command == "folder_delete_preview":
            result = {
                "operation_id": "a" * 32,
                "confirmation_token": "preview-token",
                "folder_key": params["folder_key"],
                "folder_name": "characters",
                "image_count": 12,
                "file_count": 13,
                "blocked": False,
                "api_requests": 0,
            }
        else:
            result = {
                "operation_id": params["operation_id"],
                "status": "committed",
                "indexed_deleted": 12,
                "failed": 0,
                "api_requests": 0,
            }
        return {
            "id": job_id,
            "command": command,
            "params": params,
            "status": "succeeded",
            "result": result,
            "failure_count": 0,
        }

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        command, params = self.jobs[job_id]
        return {
            "id": job_id,
            "command": command,
            "params": params,
            "status": "cancelled",
        }


class _ReadyHost:
    def __init__(self, client: _CompletedFolderDeleteClient) -> None:
        self.client = client
        self.is_running = True

    def stop(self, *, force: bool = False) -> None:
        del force
        self.is_running = False


class _SuccessfulMigrationCoordinator:
    def execute(self, request: object, **kwargs: object) -> dict[str, object]:
        del request
        on_progress = kwargs.get("on_progress")
        if callable(on_progress):
            on_progress(
                {
                    "stage": "backup_complete",
                    "progress": 25,
                    "message": "backup ready",
                    "backup": {
                        "status": "created",
                        "destination": "C:\\Backups\\migration-test",
                    },
                }
            )
            on_progress({"stage": "complete", "progress": 100, "message": "done"})
        return {
            "status": "succeeded",
            "backup": {"destination": "C:\\Backups\\migration-test"},
            "verification": {"status": "verified"},
            "api_requests": 0,
        }


class _BlockingMigrationCoordinator:
    def __init__(self) -> None:
        self.backup_ready = threading.Event()
        self.release = threading.Event()

    def execute(self, request: object, **kwargs: object) -> dict[str, object]:
        del request
        on_progress = kwargs.get("on_progress")
        if callable(on_progress):
            on_progress(
                {
                    "stage": "backup_complete",
                    "progress": 25,
                    "message": "backup ready",
                    "backup": {
                        "status": "created",
                        "destination": "C:\\Backups\\migration-test",
                    },
                }
            )
        self.backup_ready.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test migration release timed out")
        return {
            "status": "succeeded",
            "backup": {"destination": "C:\\Backups\\migration-test"},
            "verification": {"status": "verified"},
            "api_requests": 0,
        }


class _RecoveryMigrationCoordinator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[object, dict[str, object]]] = []

    def recover(
        self,
        request: object,
        backup: Mapping[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append((request, dict(backup)))
        on_progress = kwargs.get("on_progress")
        if callable(on_progress):
            on_progress({"stage": "restore", "progress": 20, "message": "restoring"})
        if self.fail:
            raise DataMigrationExecutionError(
                "restore failed",
                {
                    "status": "needs_attention",
                    "error": "restore failed",
                    "backup": dict(backup),
                    "api_requests": 0,
                },
            )
        if callable(on_progress):
            on_progress(
                {
                    "stage": "recovery_complete",
                    "progress": 100,
                    "message": "restored",
                }
            )
        return {
            "status": "restored",
            "backup": dict(backup),
            "api_requests": 0,
            "model_api_requests": 0,
        }


class _FixedEvaluationService:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.failure: SearchLearningServiceError | None = None

    def install_fixed_evaluation(self, source_path: str) -> dict[str, object]:
        self.sources.append(source_path)
        if self.failure is not None:
            raise self.failure
        return {
            "installed": True,
            "evaluation_set_id": "human-reviewed-v1",
            "external_api_calls": 0,
        }


class FacadeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bootstrap_without_config_reports_first_use_state(self) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            self.assertIsNone(facade.start_backend_async())
            payload = facade.bootstrap()
            self.assertEqual(payload["product"]["platform"], "Windows x64")
            self.assertEqual(payload["service"]["status"], "needs_setup")
            self.assertFalse(payload["service"]["backend_ready"])
            self.assertEqual(payload["libraries"], [])
            self.assertFalse(payload["service"]["credentials_configured"])
        finally:
            facade.close()

    def test_invalid_config_keeps_preview_available_in_degraded_state(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            self.assertIsNone(facade.start_backend_async())
            payload = facade.bootstrap()
            self.assertEqual(payload["service"]["status"], "degraded")
            self.assertFalse(payload["service"]["backend_ready"])
            self.assertTrue(payload["service"]["error"])
        finally:
            facade.close()

    def test_fixed_evaluation_import_validates_and_forwards_only_source_path(
        self,
    ) -> None:
        service = _FixedEvaluationService()
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            search_learning_service=service,  # type: ignore[arg-type]
        )
        try:
            result = facade.install_search_learning_evaluation(
                {"source_path": "  C:\\Evaluation\\fixed-evaluation.json  "}
            )

            self.assertTrue(result["installed"])
            self.assertEqual(result["external_api_calls"], 0)
            self.assertEqual(
                service.sources,
                ["C:\\Evaluation\\fixed-evaluation.json"],
            )

            invalid_payloads = (
                {},
                {"source_path": "   "},
                {"source_path": 42},
                {"source_path": "C:\\valid.json", "unexpected": True},
            )
            for payload in invalid_payloads:
                with (
                    self.subTest(payload=payload),
                    self.assertRaises(FacadeError) as caught,
                ):
                    facade.install_search_learning_evaluation(payload)
                self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(len(service.sources), 1)
        finally:
            facade.close()

    def test_fixed_evaluation_pack_error_is_returned_as_a_safe_facade_error(
        self,
    ) -> None:
        service = _FixedEvaluationService()
        service.failure = SearchLearningServiceError(
            "fixed_evaluation_pack_invalid",
            "The selected evaluation pack is invalid.",
        )
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            search_learning_service=service,  # type: ignore[arg-type]
        )
        try:
            with self.assertRaises(FacadeError) as caught:
                facade.install_search_learning_evaluation(
                    {"source_path": "C:\\Evaluation\\invalid.json"}
                )

            self.assertEqual(caught.exception.code, "fixed_evaluation_pack_invalid")
            self.assertEqual(caught.exception.status, 400)
        finally:
            facade.close()

    def test_migration_success_with_backend_restart_failure_needs_attention(
        self,
    ) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            data_migration_coordinator=_SuccessfulMigrationCoordinator(),  # type: ignore[arg-type]
        )
        try:
            with patch.object(
                facade,
                "_restart_backend_after_migration",
                return_value={
                    "status": "degraded",
                    "restart_required": True,
                    "error": {
                        "code": "backend_restart_failed",
                        "message": "backend unavailable",
                    },
                },
            ):
                submitted = facade.submit_data_migration(
                    {
                        "request": {
                            "migration_type": "schema",
                            "source": str(self.root),
                            "target": str(self.root),
                            "library_id": "lib-test",
                        },
                        "confirmation_token": "preview-token",
                        "confirmation_phrase": "MIGRATE",
                    }
                )
                operation = facade._migration_operations[submitted["id"]]
                assert operation.future is not None
                operation.future.result(timeout=5)
                result = facade.data_migration(submitted["id"])

            self.assertEqual(result["status"], "needs_attention")
            self.assertEqual(result["stage"], "backend_restart")
            self.assertEqual(result["error"]["code"], "backend_restart_failed")
        finally:
            facade.close()

    def test_migration_gate_stops_backend_and_persists_backup_receipt(self) -> None:
        coordinator = _BlockingMigrationCoordinator()
        host = _ReadyHost(_CompletedFolderDeleteClient())
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=host,  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            data_migration_coordinator=coordinator,  # type: ignore[arg-type]
        )
        try:
            with patch.object(
                facade,
                "_restart_backend_after_migration",
                return_value={"status": "ready", "restart_required": False},
            ):
                submitted = facade.submit_data_migration(
                    {
                        "request": {
                            "migration_type": "schema",
                            "source": str(self.root),
                            "target": str(self.root),
                            "library_id": "lib-test",
                        },
                        "confirmation_token": "preview-token",
                        "confirmation_phrase": "MIGRATE",
                    }
                )
                self.assertTrue(coordinator.backup_ready.wait(timeout=5))
                self.assertFalse(host.is_running)
                recovery_marker = facade._migration_recovery.load()
                assert recovery_marker is not None
                self.assertEqual(recovery_marker.operation_id, submitted["id"])
                self.assertEqual(recovery_marker.stage, "backup_complete")
                self.assertEqual(
                    facade.data_migration_recovery(),
                    {"recovery": None, "migration_active": True},
                )

                for action in (
                    lambda: facade.submit_search({}),
                    lambda: facade.submit_job({}),
                    lambda: facade.update_library("lib-test", {}),
                ):
                    with self.assertRaises(FacadeError) as blocked:
                        action()
                    self.assertEqual(blocked.exception.code, "data_migration_busy")

                running = facade.data_migration(submitted["id"])
                self.assertEqual(
                    running["backup"]["destination"],
                    "C:\\Backups\\migration-test",
                )
                history = facade.job_history(query=submitted["id"])
                self.assertEqual(len(history["items"]), 1)
                self.assertIn("backup", history["items"][0]["result_summary"])

                coordinator.release.set()
                operation = facade._migration_operations[submitted["id"]]
                assert operation.future is not None
                operation.future.result(timeout=5)
                self.assertEqual(
                    facade.data_migration(submitted["id"])["status"],
                    "succeeded",
                )
        finally:
            coordinator.release.set()
            facade.close()

    def test_pending_crash_marker_blocks_new_migrations_until_restored(self) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            data_migration_coordinator=_SuccessfulMigrationCoordinator(),  # type: ignore[arg-type]
        )
        request = DataMigrationRequest(
            migration_type="schema",
            source="C:\\Zvec\\workspace",
            target="C:\\Zvec\\workspace",
            library_id="lib-test",
        )
        try:
            facade._migration_recovery.arm(
                "old-migration",
                request,
                {"destination": "C:\\Backups\\old-migration"},
                stage="migrate",
            )

            with self.assertRaises(FacadeError) as blocked:
                facade.submit_data_migration(
                    {
                        "request": request.to_dict(),
                        "confirmation_token": "preview-token",
                        "confirmation_phrase": "MIGRATE",
                    }
                )

            self.assertEqual(blocked.exception.code, "migration_recovery_required")
            self.assertFalse(facade._migration_gate)
        finally:
            facade.close()

    def test_crash_recovery_runs_in_background_and_clears_marker(self) -> None:
        coordinator = _RecoveryMigrationCoordinator()
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            data_migration_coordinator=coordinator,  # type: ignore[arg-type]
        )
        request = DataMigrationRequest(
            migration_type="schema",
            source="C:\\Zvec\\workspace",
            target="C:\\Zvec\\workspace",
            library_id="lib-test",
        )
        try:
            facade._migration_recovery.arm(
                "old-migration",
                request,
                {"destination": "C:\\Backups\\old-migration"},
                stage="migrate",
            )
            with self.assertRaisesRegex(FacadeError, "RESTORE"):
                facade.submit_data_migration_recovery(
                    {
                        "operation_id": "old-migration",
                        "confirmation_phrase": "restore",
                    }
                )

            submitted = facade.submit_data_migration_recovery(
                {
                    "operation_id": "old-migration",
                    "confirmation_phrase": "RESTORE",
                }
            )
            operation = facade._migration_operations[submitted["id"]]
            assert operation.future is not None
            operation.future.result(timeout=5)

            result = facade.data_migration(submitted["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["stage"], "recovery_complete")
            self.assertIsNone(facade.data_migration_recovery()["recovery"])
            self.assertEqual(len(coordinator.calls), 1)
        finally:
            facade.close()

    def test_failed_crash_recovery_keeps_marker_for_retry(self) -> None:
        coordinator = _RecoveryMigrationCoordinator(fail=True)
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            data_migration_coordinator=coordinator,  # type: ignore[arg-type]
        )
        request = DataMigrationRequest(
            migration_type="root",
            source="C:\\Pictures",
            target="D:\\Pictures",
            workspace_directory="C:\\Zvec\\workspace",
            library_id="lib-test",
        )
        try:
            facade._migration_recovery.arm(
                "old-migration",
                request,
                {"destination": "C:\\Backups\\old-migration"},
                stage="migrate",
            )
            submitted = facade.submit_data_migration_recovery(
                {
                    "operation_id": "old-migration",
                    "confirmation_phrase": "RESTORE",
                }
            )
            operation = facade._migration_operations[submitted["id"]]
            assert operation.future is not None
            operation.future.result(timeout=5)

            result = facade.data_migration(submitted["id"])
            self.assertEqual(result["status"], "needs_attention")
            self.assertEqual(
                facade.data_migration_recovery()["recovery"]["operation_id"],
                "old-migration",
            )
        finally:
            facade.close()

    def test_completed_recovery_marker_is_removed_without_restoring(self) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        request = DataMigrationRequest(
            migration_type="schema",
            source="C:\\Zvec\\workspace",
            target="C:\\Zvec\\workspace",
        )
        try:
            facade._migration_recovery.arm(
                "old-migration",
                request,
                {"destination": "C:\\Backups\\old-migration"},
                stage="complete",
            )

            self.assertIsNone(facade.data_migration_recovery()["recovery"])
            self.assertIsNone(facade._migration_recovery.load())
        finally:
            facade.close()

    def test_data_migration_process_lock_blocks_a_second_facade(self) -> None:
        first = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        second = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        process_lock = first._acquire_data_migration_process_lock()
        try:
            with self.assertRaises(FacadeError) as blocked:
                second._acquire_data_migration_process_lock()
            self.assertEqual(blocked.exception.code, "data_migration_busy")
        finally:
            process_lock.release()
            first.close()
            second.close()

    def test_library_task_contract_supports_confirmed_identity_batches(self) -> None:
        with self.assertRaisesRegex(Exception, "batch confirmation"):
            _library_request(
                {
                    "task_type": "organize_batch_accept",
                    "library_id": "lib-test",
                    "proposal_ids": ["p1"],
                    "accepted_tags_by_proposal": {"p1": ["室内", "刻晴"]},
                    "acceptance_mode": "recommended",
                    "exclude_identity_tags": False,
                }
            )

        task_type, request, library_id = _library_request(
            {
                "task_type": "organize_batch_accept",
                "library_id": "lib-test",
                "proposal_ids": ["p1"],
                "accepted_tags_by_proposal": {"p1": ["室内", "站立"]},
                "exclude_identity_tags": True,
            }
        )
        self.assertEqual(task_type, "organize_batch_accept")
        self.assertEqual(library_id, "lib-test")
        self.assertTrue(request.to_params()["exclude_identity_tags"])
        self.assertNotIn("acceptance_mode", request.to_params())

        _task_type, identity_request, _library_id = _library_request(
            {
                "task_type": "organize_batch_accept",
                "library_id": "lib-test",
                "proposal_ids": ["p1"],
                "accepted_tags_by_proposal": {"p1": ["室内", "刻晴"]},
                "acceptance_mode": "recommended",
                "batch_confirmation": True,
                "exclude_identity_tags": False,
            }
        )
        identity_params = identity_request.to_params()
        self.assertEqual(identity_params["acceptance_mode"], "recommended")
        self.assertTrue(identity_params["batch_confirmation"])
        self.assertFalse(identity_params["exclude_identity_tags"])

    def test_folder_delete_contract_keeps_preview_and_commit_separate(self) -> None:
        task_type, preview, library_id = _library_request(
            {
                "task_type": "folder_delete_preview",
                "library_id": "lib-test",
                "folder_key": "zvec-folder-v1.preview",
                "include_subfolders": True,
            }
        )
        self.assertEqual(task_type, "folder_delete_preview")
        self.assertEqual(library_id, "lib-test")
        self.assertEqual(preview.command, "folder_delete_preview")

        task_type, commit, library_id = _library_request(
            {
                "task_type": "folder_delete_commit",
                "library_id": "lib-test",
                "operation_id": "a" * 32,
                "confirmation_token": "preview-token",
                "confirm": True,
            }
        )
        self.assertEqual(task_type, "folder_delete_commit")
        self.assertEqual(library_id, "lib-test")
        self.assertEqual(commit.to_params()["confirm"], True)

        with self.assertRaisesRegex(Exception, "confirmation"):
            _library_request(
                {
                    "task_type": "folder_delete_commit",
                    "library_id": "lib-test",
                    "operation_id": "a" * 32,
                    "confirmation_token": "preview-token",
                    "confirm": False,
                }
            )

    def test_folder_delete_facade_uses_the_real_validated_task_service(self) -> None:
        client = _CompletedFolderDeleteClient()
        host = _ReadyHost(client)
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=host,  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        facade._backend_state = "ready"
        facade._task_service = LibraryTaskService(client)
        try:
            preview = facade.preview_folder_delete(
                "library-a",
                folder_key="zvec-folder-v1.preview",
                include_subfolders=True,
            )
            committed = facade.commit_folder_delete(
                "library-a",
                operation_id=str(preview["operation_id"]),
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )
        finally:
            facade.close()

        self.assertEqual(preview["api_requests"], 0)
        self.assertEqual(committed["job"]["status"], "succeeded")
        self.assertEqual(
            [command for command, _params in client.submissions],
            ["folder_delete_preview", "folder_delete_commit"],
        )
        self.assertEqual(client.submissions[0][1]["include_subfolders"], True)
        self.assertEqual(client.submissions[1][1]["confirm"], True)
        self.assertNotIn("model", repr(client.submissions))

    def test_library_task_contract_supports_manual_folder_batches_and_undo(
        self,
    ) -> None:
        task_type, request, library_id = _library_request(
            {
                "task_type": "manual_tag_batch",
                "library_id": "lib-test",
                "selection": {
                    "mode": "folder",
                    "folder_key": "zvec-folder-v1.example",
                    "include_subfolders": False,
                    "excluded_doc_ids": ["doc-2"],
                },
                "operation": "add",
                "tags": ["Raiden", "Genshin"],
            }
        )
        self.assertEqual(task_type, "manual_tag_batch")
        self.assertEqual(library_id, "lib-test")
        self.assertEqual(
            request.to_params(),
            {
                "library_id": "lib-test",
                "selection": {
                    "mode": "folder",
                    "folder_key": "zvec-folder-v1.example",
                    "include_subfolders": False,
                    "excluded_doc_ids": ["doc-2"],
                },
                "operation": "add",
                "tags": ["Raiden", "Genshin"],
            },
        )

        undo_type, undo, _library_id = _library_request(
            {"task_type": "manual_tag_undo", "library_id": "lib-test"}
        )
        self.assertEqual(undo_type, "manual_tag_undo")
        self.assertEqual(undo.to_params(), {"library_id": "lib-test"})

        cleanup_type, cleanup, _library_id = _library_request(
            {
                "task_type": "search_results_cleanup",
                "library_id": "lib-test",
                "keep_latest": 3,
            }
        )
        self.assertEqual(cleanup_type, "search_results_cleanup")
        self.assertEqual(
            cleanup.to_params(),
            {"library_id": "lib-test", "keep_latest": 3, "dry_run": False},
        )

        migrate_type, migrate, _library_id = _library_request(
            {"task_type": "auto_tag_policy_migrate", "library_id": "lib-test"}
        )
        self.assertEqual(migrate_type, "auto_tag_policy_migrate")
        self.assertEqual(
            migrate.to_params(),
            {"library_id": "lib-test", "dry_run": False},
        )

    def test_library_task_contract_rejects_unknown_enum_values(self) -> None:
        cases = (
            {
                "task_type": "auto_tag",
                "library_id": "lib-test",
                "scope": "everything",
                "external_processing_confirmed": True,
            },
            {
                "task_type": "organize_identity_confirm",
                "library_id": "lib-test",
                "proposal_id": "p1",
                "accepted_tags": ["角色:刻晴"],
                "confirmed_identity_tags": ["角色:刻晴"],
                "decision": "reject",
            },
            {
                "task_type": "organize_refresh",
                "library_id": "lib-test",
                "review_state": "pending",
            },
            {
                "task_type": "organize_review",
                "library_id": "lib-test",
                "proposal_id": "p1",
                "decision": "approve",
                "accepted_tags": ["室内"],
            },
        )
        for payload in cases:
            with (
                self.subTest(payload=payload),
                self.assertRaises(FacadeError) as caught,
            ):
                _library_request(payload)
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertIn("supported", caught.exception.details)

    def test_proposal_view_replaces_source_path_with_opaque_image_url(self) -> None:
        source = self.root / "person.jpg"
        Image.new("RGB", (32, 48), (40, 80, 120)).save(source)
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            view = facade._proposal_view(
                {
                    "proposal_id": "p1",
                    "relative_path": "人物/person.jpg",
                    "source_path": str(source),
                    "low_risk_tags": ["站立"],
                }
            )
            self.assertNotIn("source_path", view)
            self.assertNotIn(str(source), repr(view))
            self.assertTrue(str(view["image_url"]).startswith("api/image/"))
        finally:
            facade.close()

    def test_folder_image_view_keeps_tag_sources_but_hides_absolute_path(self) -> None:
        source = self.root / "folder" / "portrait.jpg"
        source.parent.mkdir()
        Image.new("RGB", (120, 180), (40, 80, 120)).save(source)
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            view = facade._folder_image_view(
                {
                    "doc_id": "doc-1",
                    "relative_path": "folder/portrait.jpg",
                    "source_path": str(source),
                    "manual_tags": ["Raiden"],
                    "folder_tags": ["Genshin"],
                    "accepted_auto_tags": ["Cosplay"],
                }
            )
        finally:
            facade.close()

        self.assertEqual(view["doc_id"], "doc-1")
        self.assertEqual(view["manual_tags"], ["Raiden"])
        self.assertEqual(view["folder_tags"], ["Genshin"])
        self.assertEqual(view["accepted_auto_tags"], ["Cosplay"])
        self.assertTrue(view["image_available"])
        self.assertTrue(str(view["thumbnail_url"]).startswith("api/image/"))
        self.assertNotIn("source_path", view)
        self.assertNotIn(str(source), repr(view))

    def test_folder_catalog_reads_during_backend_idle_and_returns_opaque_images(
        self,
    ) -> None:
        images = self.root / "images"
        source = images / "Genshin" / "Raiden" / "portrait.jpg"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (120, 180), (40, 80, 120)).save(source)
        config_path = self.root / "config.json"
        snapshot = DesktopConfigurationService(config_path).create_initial(
            images, name="Characters"
        )
        library = snapshot.configuration.libraries[0]
        state = IndexState(
            ServiceConfig(workspace=library.workspace_directory).state_path
        )
        try:
            state.ensure_collection_uuid("collection-test")
            root_id = state.ensure_root(str(images), True)
            state.set_many(
                [
                    {
                        "doc_id": "doc-1",
                        "root_id": root_id,
                        "relative_path": "Genshin/Raiden/portrait.jpg",
                        "file_name": source.name,
                        "extension": ".jpg",
                        "mime_type": "image/jpeg",
                        "sha256": "a" * 64,
                        "size_bytes": source.stat().st_size,
                        "mtime_ns": source.stat().st_mtime_ns,
                        "width": 120,
                        "height": 180,
                        "tags": ["Raiden"],
                        "folder_tags": ["Genshin"],
                        "accepted_auto_tags": ["Cosplay"],
                        "inherited_tags": [],
                    }
                ]
            )
        finally:
            state.close()

        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            folders = facade.tag_folders(library.library_id, query="Raiden")
            self.assertEqual(folders["total"], 1)
            folder_key = folders["folders"][0]["folder_key"]
            self.assertNotIn(str(images), repr(folders))

            page = facade.folder_images(
                library.library_id,
                folder_key=str(folder_key),
                page=1,
                page_size=100,
            )
        finally:
            facade.close()

        self.assertEqual(page["total_items"], 1)
        self.assertEqual(page["items"][0]["manual_tags"], ["Raiden"])
        self.assertTrue(page["items"][0]["image_available"])
        self.assertNotIn(str(images), repr(page))

    def test_job_view_exposes_batch_counts_without_cleanup_paths(self) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            view = facade._job_view(
                {
                    "id": "job-1",
                    "command": "search_results_cleanup",
                    "status": "succeeded",
                    "params": {},
                    "result": {
                        "keep_latest": 3,
                        "deleted": 8,
                        "skipped": 2,
                        "failed": 1,
                        "deleted_paths": [str(self.root / "results" / "old")],
                        "failures": [
                            {
                                "path": str(self.root / "results" / "locked"),
                                "error": "locked",
                            }
                        ],
                    },
                }
            )
        finally:
            facade.close()

        self.assertEqual(view["result"]["keep_latest"], 3)
        self.assertEqual(view["result"]["deleted"], 8)
        self.assertEqual(view["failure_count"], 1)
        self.assertNotIn(str(self.root), repr(view))

    def test_policy_migration_is_queued_for_each_enabled_library_without_model_calls(
        self,
    ) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        config_path = self.root / "config.json"
        configuration = DesktopConfigurationService(config_path)
        snapshot = configuration.create_initial(first, name="First")
        first_id = snapshot.configuration.default_library_id
        snapshot = configuration.add_library(second, name="Second", enabled=False)
        second_id = snapshot.configuration.libraries[-1].library_id
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            with patch.object(facade, "submit_job", return_value={}) as submit:
                facade._schedule_policy_migrations()
        finally:
            facade.close()

        submit.assert_called_once_with(
            {
                "task_type": "auto_tag_policy_migrate",
                "library_id": first_id,
                "dry_run": False,
            }
        )
        self.assertNotIn(second_id, repr(submit.call_args_list))

    def test_search_result_view_exposes_real_dimensions_scores_and_tags(self) -> None:
        source = self.root / "portrait.jpg"
        Image.new("RGB", (320, 640), (40, 80, 120)).save(source)
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            view = facade._search_result_view(
                SearchResult(
                    display_path=source,
                    copied_path=None,
                    original_path=source,
                    name=source.name,
                    rank=1,
                    relative_path="人物/portrait.jpg",
                    library_name="人物图库",
                    tags=("人物", "站姿"),
                    matched_tags=("站姿",),
                    raw_score=0.18,
                    normalized_score=0.91,
                    confidence=0.9,
                    match_state="high",
                    rank_source="text",
                )
            )
        finally:
            facade.close()

        self.assertEqual((view["width"], view["height"]), (320, 640))
        self.assertEqual(view["raw_score"], 0.18)
        self.assertEqual(view["normalized_score"], 0.91)
        self.assertEqual(view["confidence"], 0.9)
        self.assertEqual(view["matched_tags"], ["站姿"])
        self.assertEqual(view["tags"], ["人物", "站姿"])
        self.assertGreater(view["size_bytes"], 0)

    def test_index_and_auto_tag_requires_explicit_external_consent(self) -> None:
        with self.assertRaisesRegex(Exception, "explicitly confirmed"):
            _library_request(
                {
                    "task_type": "index_and_auto_tag",
                    "library_id": "lib-test",
                    "external_processing_confirmed": False,
                }
            )

    def test_per_job_concurrency_and_skip_error_controls_are_rejected(self) -> None:
        for task_type in ("index", "sync", "index_and_auto_tag", "auto_tag"):
            for field, value in (("concurrency", 4), ("skip_errors", True)):
                with (
                    self.subTest(task_type=task_type, field=field),
                    self.assertRaises(FacadeError) as caught,
                ):
                    _library_request(
                        {
                            "task_type": task_type,
                            "library_id": "lib-test",
                            field: value,
                        }
                    )
                self.assertEqual(caught.exception.code, "unsupported_task_options")
                assert caught.exception.details is not None
                self.assertEqual(caught.exception.details["task_type"], task_type)
                self.assertEqual(caught.exception.details["fields"], [field])

        with self.assertRaises(FacadeError) as caught:
            _library_request(
                {
                    "task_type": "index",
                    "library_id": "lib-test",
                    "concurrency": 4,
                    "skip_errors": False,
                }
            )
        assert caught.exception.details is not None
        self.assertEqual(
            caught.exception.details["fields"], ["concurrency", "skip_errors"]
        )

    def test_independent_auto_tag_and_estimate_contracts(self) -> None:
        task_type, request, library_id = _library_request(
            {
                "task_type": "auto_tag",
                "library_id": "lib-test",
                "scope": "untagged",
                "max_images": 10_000,
                "max_budget_cny": 2.5,
                "external_processing_confirmed": True,
            }
        )
        self.assertEqual((task_type, library_id), ("auto_tag", "lib-test"))
        self.assertEqual(
            request.to_params(),
            {
                "library_id": "lib-test",
                "scope": "untagged",
                "model": None,
                "max_images": 10_000,
                "max_budget_cny": 2.5,
                "external_processing_confirmed": True,
            },
        )

        estimate_type, estimate, _library_id = _library_request(
            {
                "task_type": "auto_tag_estimate",
                "library_id": "lib-test",
                "scope": "failed_all",
                "max_images": 10_000,
            }
        )
        self.assertEqual(estimate_type, "auto_tag_estimate")
        self.assertEqual(estimate.to_params()["external_processing_confirmed"], False)
        self.assertEqual(estimate.to_params()["scope"], "failed_all")

        with self.assertRaises(FacadeError):
            _library_request(
                {
                    "task_type": "auto_tag",
                    "library_id": "lib-test",
                    "scope": "all",
                    "external_processing_confirmed": True,
                }
            )
        all_type, all_request, _library_id = _library_request(
            {
                "task_type": "auto_tag",
                "library_id": "lib-test",
                "scope": "all",
                "all_scope_confirmed": True,
                "external_processing_confirmed": True,
            }
        )
        self.assertEqual(all_type, "auto_tag")
        self.assertEqual(all_request.to_params()["scope"], "all")

        for task in ("auto_tag", "auto_tag_estimate", "index_and_auto_tag"):
            with self.subTest(task=task), self.assertRaises(FacadeError):
                _library_request(
                    {
                        "task_type": task,
                        "library_id": "lib-test",
                        "max_images": 10_001,
                        "external_processing_confirmed": True,
                    }
                )

    def test_configuration_paths_are_windows_host_paths(self) -> None:
        images = self.root / "images"
        images.mkdir()
        service = DesktopConfigurationService(self.root / "config.json")
        snapshot = service.create_initial(images, name="人物图库")

        self.assertEqual(
            snapshot.configuration.libraries[0].image_root, images.resolve()
        )
        self.assertNotIn("image_name", snapshot.configuration.to_dict())
        self.assertNotIn("workspace_type", snapshot.configuration.to_dict())

    def test_facade_reuses_catalog_and_invalidates_on_configuration_change(
        self,
    ) -> None:
        images = self.root / "images"
        images.mkdir()
        config_path = self.root / "config.json"
        service = DesktopConfigurationService(config_path)
        snapshot = service.create_initial(images, name="Original")
        library_id = snapshot.configuration.default_library_id
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            with patch.object(
                ResultCatalog,
                "from_config",
                wraps=ResultCatalog.from_config,
            ) as factory:
                first = facade._catalog()
                self.assertIs(facade._catalog(), first)
                self.assertEqual(factory.call_count, 1)

                facade.update_library(library_id, {"name": "Via facade"})
                second = facade._catalog()
                self.assertIsNot(second, first)
                self.assertEqual(factory.call_count, 2)

                service.update_library(library_id, name="External update")
                third = facade._catalog()
                self.assertIsNot(third, second)
                self.assertEqual(factory.call_count, 3)
        finally:
            facade.close(force=True)

    def test_settings_update_library_and_global_results_contract(self) -> None:
        images = self.root / "images"
        images.mkdir()
        config_path = self.root / "config.json"
        snapshot = DesktopConfigurationService(config_path).create_initial(
            images, name="Portraits"
        )
        library_id = snapshot.configuration.default_library_id
        new_results = self.root / "new-results"
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            self.assertEqual(
                facade.settings()["results_directory"],
                str(snapshot.configuration.results_directory),
            )
            facade._backend_state = "ready"
            with patch.object(facade, "has_active_jobs", return_value=True):
                result = facade.update_library(
                    library_id,
                    {
                        "name": "Cosplay",
                        "results_directory": str(new_results),
                        "is_default": True,
                    },
                )

            self.assertEqual(result["results_directory"], str(new_results.resolve()))
            self.assertEqual(result["libraries"][0]["id"], library_id)
            self.assertEqual(result["libraries"][0]["name"], "Cosplay")
            self.assertTrue(result["restart_required"])
            self.assertEqual(
                result["restart"],
                {
                    "required": True,
                    "backend_status": "ready",
                    "active_jobs": True,
                    "can_restart_now": False,
                    "reason": "active_jobs",
                },
            )
            persisted = DesktopConfigurationService(config_path).load()
            assert persisted is not None
            self.assertEqual(persisted.configuration.by_id[library_id].name, "Cosplay")
            self.assertEqual(
                persisted.configuration.results_directory, new_results.resolve()
            )
        finally:
            facade.close(force=True)

    def test_library_update_restart_state_covers_idle_and_degraded_backend(
        self,
    ) -> None:
        images = self.root / "images"
        images.mkdir()
        config_path = self.root / "config.json"
        snapshot = DesktopConfigurationService(config_path).create_initial(images)
        library_id = snapshot.configuration.default_library_id
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            idle = facade.update_library(library_id, {"name": "Idle update"})
            self.assertFalse(idle["restart_required"])
            self.assertEqual(
                idle["restart"],
                {
                    "required": False,
                    "backend_status": "idle",
                    "active_jobs": False,
                    "can_restart_now": True,
                    "reason": None,
                },
            )

            facade._backend_state = "degraded"
            degraded = facade.update_library(library_id, {"name": "Recovered config"})
            self.assertTrue(degraded["restart_required"])
            self.assertEqual(degraded["restart"]["reason"], "backend_recovery")
            self.assertTrue(degraded["restart"]["can_restart_now"])
        finally:
            facade.close(force=True)

    def test_library_update_rejects_unknown_fields(self) -> None:
        facade = PreviewFacade(
            self.root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        try:
            with self.assertRaises(FacadeError) as caught:
                facade.update_library("lib-test", {"workspace_type": "volume"})
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(
                caught.exception.details, {"unknown_fields": ["workspace_type"]}
            )
        finally:
            facade.close()

    def test_search_request_accepts_more_than_500_results(self) -> None:
        request, _query, page_size = _search_request(
            {"text": "portrait", "top_k": 750, "candidate_k": 750}
        )
        self.assertEqual(request.top_k, 750)
        self.assertEqual(request.candidate_k, 750)
        self.assertEqual(page_size, 15)

        dynamic, query, _page_size = _search_request({"text": "portrait", "top_k": 75})
        self.assertEqual(dynamic.candidate_k, 112)
        self.assertEqual(dynamic.sort_mode, "confidence")
        self.assertEqual(query["sort_mode"], "confidence")

        with self.assertRaisesRegex(FacadeError, "candidate_k"):
            _search_request({"text": "portrait", "top_k": 750, "candidate_k": 749})
        with self.assertRaisesRegex(FacadeError, "sort_mode"):
            _search_request({"text": "portrait", "sort_mode": "score"})

    def test_app_rejects_non_windows_platform(self) -> None:
        with (
            patch.object(app.os, "name", "posix"),
            patch.object(app.sys, "platform", "linux"),
            self.assertRaisesRegex(RuntimeError, "Windows x64"),
        ):
            app._validate_windows_x64()


if __name__ == "__main__":
    unittest.main()
