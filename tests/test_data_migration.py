from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from image_vector_service.data_migration import (
    MIGRATION_CONFIRMATION_PHRASE,
    DataMigrationCancelled,
    DataMigrationCoordinator,
    DataMigrationExecutionError,
    DataMigrationRequest,
    DataMigrationValidationError,
    NativeMigrationPrecheck,
    WindowsNativeMigrationOperations,
    _inspect_workspace,
    restore_full_migration_backup,
)
from image_vector_service.workspace_backup import (
    WorkspaceBackupSource,
    create_migration_backup,
    new_backup_destination,
)


class _FakeOperations:
    def __init__(self) -> None:
        self.fingerprint = "stable-fingerprint"
        self.blockers: tuple[str, ...] = ()
        self.apply_report: dict[str, object] = {"status": "migrated", "api_requests": 0}
        self.verification: dict[str, object] = {
            "status": "verified",
            "collection_documents": 12,
            "sqlite_entries": 12,
            "sqlite_integrity": "ok",
            "search_probe": "passed",
            "api_requests": 0,
        }
        self.restore_report: dict[str, object] = {
            "status": "restored",
            "api_requests": 0,
        }
        self.backup_calls = 0
        self.apply_calls = 0
        self.verify_calls = 0
        self.restore_calls = 0
        self.raise_apply: Exception | None = None
        self.raise_restore: Exception | None = None
        self.cancel_during_apply = False
        self.fingerprint_after_backup: str | None = None

    def precheck(self, request: DataMigrationRequest) -> NativeMigrationPrecheck:
        return NativeMigrationPrecheck(
            status="blocked" if self.blockers else "ready",
            source_display=request.source,
            target_display=request.target,
            collection_documents=12,
            sqlite_entries=12,
            collection_status="ready",
            sqlite_status="ok",
            fingerprint=self.fingerprint,
            backup_plan={
                "status": "ready",
                "destination": "C:\\Backups\\migration-test",
                "estimated_payload_bytes": 4096,
                "blockers": [],
                "warnings": [],
                "api_requests": 0,
            },
            blockers=self.blockers,
            api_requests=0,
        )

    def create_backup(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]:
        del request, precheck
        self.backup_calls += 1
        if self.fingerprint_after_backup is not None:
            self.fingerprint = self.fingerprint_after_backup
        return {
            "status": "created",
            "destination": "C:\\Backups\\migration-test",
            "api_requests": 0,
        }

    def apply(
        self,
        request: DataMigrationRequest,
        *,
        cancel_event: threading.Event,
        on_progress,
    ) -> Mapping[str, object]:
        del request
        self.apply_calls += 1
        on_progress({"stage": "migrate", "progress": 60, "message": "working"})
        if self.cancel_during_apply:
            cancel_event.set()
        if self.raise_apply is not None:
            raise self.raise_apply
        return self.apply_report

    def verify(
        self,
        request: DataMigrationRequest,
        precheck: NativeMigrationPrecheck,
    ) -> Mapping[str, object]:
        del request, precheck
        self.verify_calls += 1
        return self.verification

    def restore(
        self,
        request: DataMigrationRequest,
        backup: Mapping[str, object],
    ) -> Mapping[str, object]:
        del request, backup
        self.restore_calls += 1
        if self.raise_restore is not None:
            raise self.raise_restore
        return self.restore_report


class DataMigrationCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operations = _FakeOperations()
        self.coordinator = DataMigrationCoordinator(
            self.operations,
            platform_name="Windows",
        )
        self.request = DataMigrationRequest(
            migration_type="schema",
            source="C:\\Zvec\\workspace",
            target="C:\\Zvec\\workspace",
            library_id="lib-1",
            library_name="人物图库",
        )

    def test_preview_is_dry_run_and_reports_local_only_invariants(self) -> None:
        preview = self.coordinator.preview(self.request)

        payload = preview.to_dict()
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["vectors_recomputed"], 0)
        self.assertEqual(payload["model_api_requests"], 0)
        self.assertEqual(preview.precheck.collection_documents, 12)
        self.assertEqual(preview.precheck.sqlite_entries, 12)
        self.assertTrue(preview.confirmation_token)
        self.assertEqual(self.operations.backup_calls, 0)
        self.assertEqual(self.operations.apply_calls, 0)

    def test_windows_only_contract_fails_before_touching_operations(self) -> None:
        coordinator = DataMigrationCoordinator(
            self.operations,
            platform_name="Linux",
        )
        with self.assertRaisesRegex(DataMigrationValidationError, "only on Windows"):
            coordinator.preview(self.request)
        self.assertEqual(self.operations.backup_calls, 0)

    def test_requires_exact_confirmation_and_one_time_unchanged_preview(self) -> None:
        preview = self.coordinator.preview(self.request)
        with self.assertRaisesRegex(
            DataMigrationValidationError, "confirmation_phrase"
        ):
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase="yes",
            )

        result = self.coordinator.execute(
            self.request,
            confirmation_token=preview.confirmation_token,
            confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
        )
        self.assertEqual(result["status"], "succeeded")
        with self.assertRaisesRegex(DataMigrationValidationError, "already used"):
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

    def test_source_change_after_preview_requires_a_new_precheck(self) -> None:
        preview = self.coordinator.preview(self.request)
        self.operations.fingerprint = "changed"

        with self.assertRaisesRegex(DataMigrationValidationError, "changed"):
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )
        self.assertEqual(self.operations.backup_calls, 0)

    def test_source_change_during_backup_prevents_mutation(self) -> None:
        preview = self.coordinator.preview(self.request)
        self.operations.fingerprint_after_backup = "changed-during-backup"

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

        self.assertEqual(self.operations.backup_calls, 1)
        self.assertEqual(self.operations.apply_calls, 0)
        self.assertEqual(raised.exception.report["status"], "failed")
        self.assertIn("changed", str(raised.exception.report["error"]))

    def test_blocked_preview_cannot_be_executed(self) -> None:
        self.operations.blockers = ("SQLite state is unreadable",)
        preview = self.coordinator.preview(self.request)

        self.assertEqual(preview.precheck.status, "blocked")
        with self.assertRaisesRegex(DataMigrationValidationError, "SQLite"):
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )
        self.assertEqual(self.operations.backup_calls, 0)

    def test_cancellation_before_backup_never_starts_mutation(self) -> None:
        preview = self.coordinator.preview(self.request)
        cancellation = threading.Event()
        cancellation.set()

        with self.assertRaises(DataMigrationCancelled):
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
                cancel_event=cancellation,
            )
        self.assertEqual(self.operations.backup_calls, 0)
        self.assertEqual(self.operations.apply_calls, 0)

    def test_success_has_backup_progress_and_post_migration_verification(self) -> None:
        preview = self.coordinator.preview(self.request)
        progress: list[dict[str, object]] = []

        result = self.coordinator.execute(
            self.request,
            confirmation_token=preview.confirmation_token,
            confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            on_progress=progress.append,
        )

        self.assertEqual(self.operations.backup_calls, 1)
        self.assertEqual(self.operations.apply_calls, 1)
        self.assertEqual(self.operations.verify_calls, 1)
        self.assertEqual(self.operations.restore_calls, 0)
        self.assertEqual(result["vectors_recomputed"], 0)
        self.assertEqual(result["model_api_requests"], 0)
        self.assertEqual(
            [item["stage"] for item in progress],
            [
                "precheck",
                "backup",
                "backup_complete",
                "migrate",
                "migrate",
                "verify",
                "complete",
            ],
        )
        backup_progress = next(
            item for item in progress if item["stage"] == "backup_complete"
        )
        self.assertEqual(
            backup_progress["backup"],
            {
                "status": "created",
                "destination": "C:\\Backups\\migration-test",
                "api_requests": 0,
            },
        )

    def test_cancel_during_atomic_mutation_is_deferred_until_safe_verification(
        self,
    ) -> None:
        self.operations.cancel_during_apply = True
        preview = self.coordinator.preview(self.request)

        result = self.coordinator.execute(
            self.request,
            confirmation_token=preview.confirmation_token,
            confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["cancellation_requested"])
        self.assertTrue(result["cancellation_deferred"])
        self.assertEqual(self.operations.verify_calls, 1)

    def test_cancel_after_partial_mutation_restores_the_complete_backup(self) -> None:
        self.operations.raise_apply = DataMigrationCancelled(
            "cancelled after the first library was written"
        )
        preview = self.coordinator.preview(self.request)

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

        self.assertEqual(self.operations.restore_calls, 1)
        self.assertEqual(raised.exception.report["status"], "failed_recovered")
        self.assertIn("cancelled", raised.exception.report["error"])

    def test_explicit_crash_recovery_restores_without_model_requests(self) -> None:
        progress: list[dict[str, object]] = []

        report = self.coordinator.recover(
            self.request,
            {
                "status": "created",
                "destination": "C:\\Backups\\migration-test",
                "api_requests": 0,
            },
            on_progress=progress.append,
        )

        self.assertEqual(self.operations.restore_calls, 1)
        self.assertEqual(report["status"], "restored")
        self.assertEqual(report["model_api_requests"], 0)
        self.assertEqual(
            [item["stage"] for item in progress],
            ["restore", "recovery_complete"],
        )

    def test_failed_crash_recovery_retains_needs_attention_status(self) -> None:
        self.operations.raise_restore = RuntimeError("backup disk unavailable")

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.recover(
                self.request,
                {
                    "status": "created",
                    "destination": "C:\\Backups\\migration-test",
                    "api_requests": 0,
                },
            )

        self.assertEqual(raised.exception.report["status"], "needs_attention")
        self.assertIn("backup disk unavailable", raised.exception.report["error"])

    def test_count_mismatch_restores_backup_and_returns_structured_failure(
        self,
    ) -> None:
        self.operations.verification["collection_documents"] = 11
        preview = self.coordinator.preview(self.request)

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

        self.assertEqual(self.operations.restore_calls, 1)
        self.assertEqual(raised.exception.report["status"], "failed_recovered")
        self.assertEqual(
            raised.exception.report["recovery"],
            {"status": "restored", "api_requests": 0},
        )

    def test_external_api_evidence_aborts_and_restores(self) -> None:
        self.operations.apply_report["api_requests"] = 1
        preview = self.coordinator.preview(self.request)

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

        self.assertEqual(self.operations.restore_calls, 1)
        self.assertIn("external API", str(raised.exception.__cause__))

    def test_restore_failure_is_reported_as_needs_attention(self) -> None:
        self.operations.raise_apply = RuntimeError("injected failure")
        self.operations.raise_restore = RuntimeError("disk full")
        preview = self.coordinator.preview(self.request)

        with self.assertRaises(DataMigrationExecutionError) as raised:
            self.coordinator.execute(
                self.request,
                confirmation_token=preview.confirmation_token,
                confirmation_phrase=MIGRATION_CONFIRMATION_PHRASE,
            )

        self.assertEqual(raised.exception.report["status"], "needs_attention")
        self.assertEqual(
            raised.exception.report["recovery"],
            {"status": "failed", "error": "disk full"},
        )


class DataMigrationRequestTests(unittest.TestCase):
    def test_root_request_requires_workspace_but_can_auto_resolve_root_id(self) -> None:
        with self.assertRaisesRegex(DataMigrationValidationError, "workspace"):
            DataMigrationRequest(
                migration_type="root",
                source="C:\\Old",
                target="D:\\New",
                root_id="root-1",
            )
        request = DataMigrationRequest(
            migration_type="root",
            source="C:\\Old",
            target="D:\\New",
            workspace_directory="C:\\Workspace",
        )
        self.assertEqual(request.root_id, "")

    def test_unknown_fields_and_backup_opt_out_fail_closed(self) -> None:
        with self.assertRaisesRegex(DataMigrationValidationError, "Unknown"):
            DataMigrationRequest.from_mapping(
                {
                    "migration_type": "schema",
                    "source": "C:\\Workspace",
                    "target": "C:\\Workspace",
                    "delete_source": True,
                }
            )
        with self.assertRaisesRegex(DataMigrationValidationError, "full backup"):
            DataMigrationRequest(
                migration_type="schema",
                source="C:\\Workspace",
                target="C:\\Workspace",
                automatic_backup=False,
            )

    def test_library_id_must_be_a_safe_single_path_segment(self) -> None:
        for library_id in (
            "../../escaped",
            "lib/child",
            "lib\\child",
            "C:escaped",
            "lib..escaped",
            ".",
            "..",
        ):
            with (
                self.subTest(library_id=library_id),
                self.assertRaisesRegex(
                    DataMigrationValidationError, "safe single-segment"
                ),
            ):
                DataMigrationRequest(
                    migration_type="schema",
                    source="C:\\Workspace",
                    target="C:\\Workspace",
                    library_id=library_id,
                )

    def test_library_id_rejects_windows_device_names_and_extensions(self) -> None:
        for library_id in ("CON", "con.txt", "PrN.backup", "nul.json", "COM1"):
            with (
                self.subTest(library_id=library_id),
                self.assertRaisesRegex(
                    DataMigrationValidationError, "reserved Windows device"
                ),
            ):
                DataMigrationRequest(
                    migration_type="schema",
                    source="C:\\Workspace",
                    target="C:\\Workspace",
                    library_id=library_id,
                )


class WorkspaceInspectionTests(unittest.TestCase):
    def test_non_ok_sqlite_quick_check_is_a_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "image_collection").mkdir()
            (workspace / "image_collection.meta.json").write_text(
                '{"schema_version":2}', encoding="utf-8"
            )
            (workspace / "image_collection.state.sqlite3").write_bytes(b"state")
            opened = SimpleNamespace(stats=SimpleNamespace(doc_count=1))
            with (
                patch(
                    "image_vector_service.data_migration._sqlite_snapshot",
                    return_value=(1, "database disk image is malformed"),
                ),
                patch("zvec.open", return_value=opened),
            ):
                inspection = _inspect_workspace(workspace)

        self.assertEqual(
            inspection.sqlite_integrity, "database disk image is malformed"
        )
        self.assertTrue(
            any("quick_check failed" in item for item in inspection.blockers)
        )


class WindowsNativeMigrationOperationsTests(unittest.TestCase):
    def test_native_config_docker_target_must_match_selected_workspace(self) -> None:
        import zvec_launcher

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_root = root / "images"
            configured_workspace = root / "configured-workspace"
            target_workspace = root / "different-target"
            results = root / "results"
            image_root.mkdir()
            configured_workspace.mkdir()
            config_path = root / "config.json"
            library = zvec_launcher.NativeLibrary(
                library_id="lib-1",
                name="Library",
                image_root=image_root,
                workspace_directory=configured_workspace,
                enabled=True,
            )
            native = zvec_launcher.NativeConfig(
                default_library_id="lib-1",
                results_directory=results,
                libraries=(library,),
            )
            config_path.write_text(json.dumps(native.to_dict()), encoding="utf-8")
            operations = WindowsNativeMigrationOperations(
                config_home=root,
                config_path=config_path,
                results_directory=results,
            )
            request = DataMigrationRequest(
                migration_type="docker_workspace",
                source="zvec-data",
                target=str(target_workspace),
                library_id="lib-1",
            )

            with patch(
                "image_vector_service.data_migration.shutil.which",
                return_value="docker.exe",
            ):
                precheck = operations.precheck(request)

        self.assertEqual(precheck.status, "blocked")
        self.assertTrue(any("must match" in item for item in precheck.blockers))


class FullBackupRestoreTests(unittest.TestCase):
    def test_complete_full_backup_restores_config_sqlite_metadata_and_collection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            workspace = root / "workspace"
            workspace.mkdir()
            config.write_text('{"version":"before"}', encoding="utf-8")
            metadata = workspace / "image_collection.meta.json"
            metadata.write_text('{"schema_version":1}', encoding="utf-8")
            state = workspace / "image_collection.state.sqlite3"
            connection = sqlite3.connect(state)
            connection.execute("CREATE TABLE entries (doc_id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO entries VALUES ('doc-1')")
            connection.commit()
            connection.close()
            collection = workspace / "image_collection"
            collection.mkdir()
            (collection / "segment.bin").write_bytes(b"original-vector-bytes")
            destination = new_backup_destination(root, root / "backups")
            receipt = create_migration_backup(
                config_path=config,
                sources=[WorkspaceBackupSource("lib-1", "图库", workspace)],
                destination=destination,
                full_backup=True,
            )

            config.write_text('{"version":"after"}', encoding="utf-8")
            metadata.write_text('{"schema_version":4}', encoding="utf-8")
            connection = sqlite3.connect(state)
            connection.execute("INSERT INTO entries VALUES ('doc-2')")
            connection.commit()
            connection.close()
            (collection / "segment.bin").write_bytes(b"changed")

            report = restore_full_migration_backup(
                Path(str(receipt["destination"])),
                expected_config_path=config,
                expected_workspaces={"lib-1": workspace},
            )

            self.assertEqual(report["status"], "restored")
            self.assertEqual(config.read_text(encoding="utf-8"), '{"version":"before"}')
            self.assertEqual(
                metadata.read_text(encoding="utf-8"), '{"schema_version":1}'
            )
            self.assertEqual(
                (collection / "segment.bin").read_bytes(), b"original-vector-bytes"
            )
            connection = sqlite3.connect(state)
            try:
                count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            finally:
                connection.close()
            self.assertEqual(count, (1,))

    def test_tampered_backup_is_rejected_before_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            workspace = root / "workspace"
            workspace.mkdir()
            config.write_text("{}", encoding="utf-8")
            (workspace / "image_collection.meta.json").write_text(
                "{}", encoding="utf-8"
            )
            connection = sqlite3.connect(workspace / "image_collection.state.sqlite3")
            connection.execute("CREATE TABLE entries (doc_id TEXT)")
            connection.commit()
            connection.close()
            (workspace / "image_collection").mkdir()
            receipt = create_migration_backup(
                config_path=config,
                sources=[WorkspaceBackupSource("lib-1", "图库", workspace)],
                destination=new_backup_destination(root, root / "backups"),
                full_backup=True,
            )
            backup = Path(str(receipt["destination"]))
            manifest = json.loads(
                (backup / "migration-backup-manifest.json").read_text(encoding="utf-8")
            )
            first_path = backup / manifest["files"][0]["path"]
            first_path.write_bytes(b"tampered")

            with self.assertRaisesRegex(
                DataMigrationValidationError, "verification failed"
            ):
                restore_full_migration_backup(
                    backup,
                    expected_config_path=config,
                    expected_workspaces={"lib-1": workspace},
                )

    def test_manifest_library_id_cannot_escape_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            workspace = root / "workspace"
            workspace.mkdir()
            config.write_text("{}", encoding="utf-8")
            (workspace / "image_collection.meta.json").write_text(
                "{}", encoding="utf-8"
            )
            connection = sqlite3.connect(workspace / "image_collection.state.sqlite3")
            connection.execute("CREATE TABLE entries (doc_id TEXT)")
            connection.commit()
            connection.close()
            (workspace / "image_collection").mkdir()
            receipt = create_migration_backup(
                config_path=config,
                sources=[WorkspaceBackupSource("lib-1", "Library", workspace)],
                destination=new_backup_destination(root, root / "backups"),
                full_backup=True,
            )
            backup = Path(str(receipt["destination"]))
            manifest_path = backup / "migration-backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["libraries"][0]["library_id"] = "../../escaped"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            escaped = backup.parent / "escaped"

            with self.assertRaisesRegex(
                DataMigrationValidationError, "safe path segment"
            ):
                restore_full_migration_backup(
                    backup,
                    expected_config_path=config,
                    expected_workspaces={"lib-1": workspace},
                )

            self.assertFalse(escaped.exists())

    def test_manifest_restore_targets_must_match_trusted_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            workspace = root / "workspace"
            workspace.mkdir()
            config.write_text('{"version":"trusted-live"}', encoding="utf-8")
            metadata = workspace / "image_collection.meta.json"
            metadata.write_text('{"schema_version":1}', encoding="utf-8")
            connection = sqlite3.connect(workspace / "image_collection.state.sqlite3")
            connection.execute("CREATE TABLE entries (doc_id TEXT)")
            connection.commit()
            connection.close()
            (workspace / "image_collection").mkdir()
            receipt = create_migration_backup(
                config_path=config,
                sources=[WorkspaceBackupSource("lib-1", "Library", workspace)],
                destination=new_backup_destination(root, root / "backups"),
                full_backup=True,
            )
            backup = Path(str(receipt["destination"]))
            manifest_path = backup / "migration-backup-manifest.json"
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config.write_text('{"version":"current-after-backup"}', encoding="utf-8")
            metadata.write_text('{"schema_version":4}', encoding="utf-8")

            escaped_workspace = root / "escaped-workspace"
            tampered_workspace = dict(original_manifest)
            tampered_workspace["libraries"] = [
                {
                    **original_manifest["libraries"][0],
                    "workspace": str(escaped_workspace),
                }
            ]
            manifest_path.write_text(json.dumps(tampered_workspace), encoding="utf-8")
            with self.assertRaisesRegex(
                DataMigrationValidationError, "does not match the trusted target"
            ):
                restore_full_migration_backup(
                    backup,
                    expected_config_path=config,
                    expected_workspaces={"lib-1": workspace},
                )
            self.assertFalse(escaped_workspace.exists())
            self.assertEqual(
                metadata.read_text(encoding="utf-8"), '{"schema_version":4}'
            )

            escaped_config = root / "escaped-config.json"
            tampered_config = dict(original_manifest)
            tampered_config["source_config"] = str(escaped_config)
            manifest_path.write_text(json.dumps(tampered_config), encoding="utf-8")
            with self.assertRaisesRegex(
                DataMigrationValidationError, "does not match the trusted config"
            ):
                restore_full_migration_backup(
                    backup,
                    expected_config_path=config,
                    expected_workspaces={"lib-1": workspace},
                )
            self.assertFalse(escaped_config.exists())
            self.assertEqual(
                config.read_text(encoding="utf-8"),
                '{"version":"current-after-backup"}',
            )
            self.assertEqual(
                metadata.read_text(encoding="utf-8"), '{"schema_version":4}'
            )

    def test_restore_requires_explicit_trusted_targets(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(
                DataMigrationValidationError, "trusted config and Workspace"
            ),
        ):
            restore_full_migration_backup(Path(temporary))


if __name__ == "__main__":
    unittest.main()
