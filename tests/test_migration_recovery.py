from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.data_migration import DataMigrationRequest
from image_vector_service.migration_recovery import (
    RECOVERY_FILE_NAME,
    MigrationRecoveryError,
    MigrationRecoveryStore,
)


class MigrationRecoveryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MigrationRecoveryStore(self.root)
        self.request = DataMigrationRequest(
            migration_type="schema",
            source="C:\\Zvec\\workspace",
            target="C:\\Zvec\\workspace",
            library_id="lib-1",
            library_name="人物图库",
        )
        self.backup = {
            "status": "created",
            "destination": "C:\\Zvec\\backups\\migration-1",
            "libraries": [{"library_id": "lib-1"}],
            "api_requests": 0,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_arm_load_update_and_clear_round_trip(self) -> None:
        armed = self.store.arm("operation-1", self.request, self.backup)

        self.assertEqual(armed.stage, "backup_complete")
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.request, self.request)
        self.assertEqual(loaded.backup, self.backup)
        self.assertEqual(
            loaded.public_view()["backup_directory"],
            "C:\\Zvec\\backups\\migration-1",
        )

        updated = self.store.update_stage("operation-1", "migrate")
        self.assertEqual(updated.stage, "migrate")
        self.assertTrue(self.store.clear("operation-1"))
        self.assertIsNone(self.store.load())

    def test_existing_different_operation_fails_closed(self) -> None:
        self.store.arm("operation-1", self.request, self.backup)

        with self.assertRaisesRegex(MigrationRecoveryError, "unfinished"):
            self.store.arm("operation-2", self.request, self.backup)
        with self.assertRaisesRegex(MigrationRecoveryError, "different"):
            self.store.clear("operation-2")

    def test_tampered_request_or_integrity_is_rejected(self) -> None:
        self.store.arm("operation-1", self.request, self.backup)
        payload = json.loads(
            (self.root / RECOVERY_FILE_NAME).read_text(encoding="utf-8")
        )
        payload["request"]["target"] = "D:\\escaped"
        (self.root / RECOVERY_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

        with self.assertRaisesRegex(MigrationRecoveryError, "integrity"):
            self.store.load()

    def test_invalid_backup_destination_is_rejected_before_write(self) -> None:
        with self.assertRaisesRegex(MigrationRecoveryError, "destination"):
            self.store.arm(
                "operation-1",
                self.request,
                {"destination": "relative\\backup"},
            )
        self.assertFalse((self.root / RECOVERY_FILE_NAME).exists())

    def test_atomic_write_failure_keeps_previous_marker(self) -> None:
        self.store.arm("operation-1", self.request, self.backup)
        original = (self.root / RECOVERY_FILE_NAME).read_bytes()

        with (
            patch("image_vector_service.migration_recovery.os.replace") as replace,
            self.assertRaisesRegex(MigrationRecoveryError, "durably persist"),
        ):
            replace.side_effect = OSError("disk full")
            self.store.update_stage("operation-1", "verify")

        self.assertEqual((self.root / RECOVERY_FILE_NAME).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
