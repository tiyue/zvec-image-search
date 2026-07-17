from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.process_lock import ProcessLock
from image_vector_service.workspace_backup import (
    MANIFEST_FILE_NAME,
    WorkspaceBackupError,
    WorkspaceBackupSource,
    create_migration_backup,
    plan_migration_backup,
)


class WorkspaceBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_workspace_backup_test_"))
        self.config = self.root / "config.json"
        self.workspace = self.root / "workspace"
        self.destination = self.root / "backups" / "migration-1"
        self.workspace.mkdir()
        self.config.write_text('{"schema_version":2}\n', encoding="utf-8")
        (self.workspace / "image_collection").mkdir()
        (self.workspace / "image_collection" / "vectors.bin").write_bytes(b"v" * 4096)
        (self.workspace / "image_collection.meta.json").write_text(
            '{"schema_version":2}\n', encoding="utf-8"
        )
        connection = sqlite3.connect(self.workspace / "image_collection.state.sqlite3")
        try:
            connection.execute("CREATE TABLE entries(doc_id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO entries(doc_id) VALUES(?)",
                [("doc-1",), ("doc-2",)],
            )
            connection.commit()
        finally:
            connection.close()
        (self.workspace / "search-quality.json").write_text(
            '{"schema_version":1}\n', encoding="utf-8"
        )
        self.sources = [WorkspaceBackupSource("library-main", "Main", self.workspace)]

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_metadata_backup_has_manifest_and_consistent_sqlite_snapshot(self) -> None:
        with patch(
            "image_vector_service.workspace_backup._directory_size"
        ) as directory_size:
            plan = plan_migration_backup(
                config_path=self.config,
                sources=self.sources,
                destination=self.destination,
                full_backup=False,
            )
        directory_size.assert_not_called()
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["libraries"][0]["collection_included"])
        self.assertIsNone(plan["libraries"][0]["collection_bytes"])
        self.assertEqual(plan["libraries"][0]["sqlite_entries"], 2)

        report = create_migration_backup(
            config_path=self.config,
            sources=self.sources,
            destination=self.destination,
            full_backup=False,
        )
        self.assertEqual(report["status"], "created")
        manifest = json.loads(
            (self.destination / MANIFEST_FILE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["backup_mode"], "metadata")
        self.assertEqual(manifest["api_requests"], 0)
        self.assertTrue(manifest["excluded"])
        library = self.destination / "libraries" / "library-main"
        self.assertTrue((library / "image_collection.meta.json").is_file())
        self.assertTrue((library / "image_collection.state.sqlite3").is_file())
        self.assertFalse((library / "image_collection").exists())
        snapshot = sqlite3.connect(library / "image_collection.state.sqlite3")
        try:
            count = snapshot.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        finally:
            snapshot.close()
        self.assertEqual(count, 2)

    def test_full_backup_is_explicit_and_includes_vector_collection(self) -> None:
        report = create_migration_backup(
            config_path=self.config,
            sources=self.sources,
            destination=self.destination,
            full_backup=True,
        )
        self.assertEqual(report["backup_mode"], "full")
        vector = (
            self.destination
            / "libraries"
            / "library-main"
            / "image_collection"
            / "vectors.bin"
        )
        self.assertEqual(vector.read_bytes(), b"v" * 4096)

    def test_precheck_blocks_overlap_insufficient_space_and_nonempty_target(
        self,
    ) -> None:
        overlap = plan_migration_backup(
            config_path=self.config,
            sources=self.sources,
            destination=self.workspace / "backup",
            full_backup=False,
        )
        self.assertEqual(overlap["status"], "blocked")
        self.assertTrue(any("inside" in item for item in overlap["blockers"]))

        self.destination.mkdir(parents=True)
        (self.destination / "unexpected.txt").write_text("occupied")
        occupied = plan_migration_backup(
            config_path=self.config,
            sources=self.sources,
            destination=self.destination,
            full_backup=False,
        )
        self.assertTrue(any("not empty" in item for item in occupied["blockers"]))

        shutil.rmtree(self.destination)
        disk_usage = shutil.disk_usage(self.root)
        with patch(
            "image_vector_service.workspace_backup.shutil.disk_usage",
            return_value=disk_usage._replace(free=1),
        ):
            insufficient = plan_migration_backup(
                config_path=self.config,
                sources=self.sources,
                destination=self.destination,
                full_backup=True,
            )
        self.assertTrue(
            any("insufficient free space" in item for item in insufficient["blockers"])
        )

        with patch(
            "image_vector_service.workspace_backup._can_write_directory",
            return_value=False,
        ):
            denied = plan_migration_backup(
                config_path=self.config,
                sources=self.sources,
                destination=self.destination,
                full_backup=False,
            )
        self.assertFalse(denied["destination_writable"])
        self.assertTrue(any("not writable" in item for item in denied["blockers"]))

    def test_blocked_plan_never_creates_partial_backup(self) -> None:
        destination = self.workspace / "nested-backup"
        with self.assertRaises(WorkspaceBackupError):
            create_migration_backup(
                config_path=self.config,
                sources=self.sources,
                destination=destination,
                full_backup=False,
            )
        self.assertFalse(destination.exists())

    def test_active_workspace_writer_blocks_consistent_snapshot(self) -> None:
        lock = ProcessLock(self.workspace / ".image_collection.lock")
        lock.acquire()
        try:
            with self.assertRaisesRegex(WorkspaceBackupError, "already using"):
                create_migration_backup(
                    config_path=self.config,
                    sources=self.sources,
                    destination=self.destination,
                    full_backup=False,
                )
        finally:
            lock.release()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
