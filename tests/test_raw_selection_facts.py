"""Persistence, metadata, export and delete facts for RAW selection."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import image_vector_service.raw_selection.importer as raw_selection_importer
import image_vector_service.raw_selection.service as raw_selection_service
from image_vector_service.raw_selection.db import (
    AssetRegistration,
    OperationTarget,
    RawSelectionDB,
)
from image_vector_service.raw_selection.decoder import (
    DecodeResult,
    decode_full_base,
    decode_preview,
    decode_thumbnail,
)
from image_vector_service.raw_selection.scheduler import (
    SchedulerQueueFull,
    StaleGeneration,
    TaskPriority,
)
from image_vector_service.raw_selection.service import RawSelectionService


def _write_jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (120, 80),
    shot_time: str | None = None,
    orientation: int = 1,
    color: tuple[int, int, int] = (20, 40, 60),
) -> None:
    exif = Image.Exif()
    exif[274] = orientation
    if shot_time is not None:
        exif[36867] = shot_time
    Image.new("RGB", size, color=color).save(path, "JPEG", exif=exif)


def _write_minimal_arw(
    path: Path,
    *,
    make: str = "SONY",
    model: str = "ILCE-7M4",
) -> None:
    """Write the smallest TIFF container needed by the ARW identity guard."""

    make_bytes = make.encode("ascii") + b"\0"
    model_bytes = model.encode("ascii") + b"\0"
    value_start = 8 + 2 + 2 * 12 + 4
    entries = (
        struct.pack("<HHII", 0x010F, 2, len(make_bytes), value_start),
        struct.pack(
            "<HHII",
            0x0110,
            2,
            len(model_bytes),
            value_start + len(make_bytes),
        ),
    )
    path.write_bytes(
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", len(entries))
        + b"".join(entries)
        + struct.pack("<I", 0)
        + make_bytes
        + model_bytes
    )


def _write_embedded_arw(path: Path, *, orientation: int = 6) -> None:
    """Write an A7M4-like TIFF with a JPEG that has no own orientation tag."""

    source = Image.new("RGB", (120, 80), (0, 0, 0))
    for x in range(120):
        color = (240, 10, 10) if x < 60 else (10, 10, 240)
        for y in range(80):
            source.putpixel((x, y), color)
    encoded = io.BytesIO()
    source.save(encoded, "JPEG", quality=100, subsampling=0)
    jpeg = encoded.getvalue()

    make = b"SONY\0"
    model = b"ILCE-7M4\0"
    value_start = 8 + 2 + 5 * 12 + 4
    jpeg_offset = value_start + len(make) + len(model)
    entries = (
        struct.pack("<HHII", 0x010F, 2, len(make), value_start),
        struct.pack("<HHII", 0x0110, 2, len(model), value_start + len(make)),
        struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<H", orientation) + b"\0\0",
        struct.pack("<HHII", 0x0201, 4, 1, jpeg_offset),
        struct.pack("<HHII", 0x0202, 4, 1, len(jpeg)),
    )
    path.write_bytes(
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", len(entries))
        + b"".join(entries)
        + struct.pack("<I", 0)
        + make
        + model
        + jpeg
    )


def _wait_for_job(
    service: RawSelectionService,
    job_id: str,
    *,
    timeout: float = 5.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get_job(job_id)
        if snapshot is not None and snapshot["status"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"RAW job {job_id} did not finish within {timeout}s")


def _create_v1_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE assets (
                id TEXT PRIMARY KEY, normalized_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL, extension TEXT NOT NULL,
                file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                file_identity TEXT, shot_time TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE project_members (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                import_order INTEGER NOT NULL, star_rating INTEGER NOT NULL DEFAULT 0,
                color_label TEXT NOT NULL DEFAULT 'none',
                creative_look TEXT NOT NULL DEFAULT 'as_shot', created_at TEXT NOT NULL,
                UNIQUE(project_id, asset_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE RESTRICT
            );
            CREATE TABLE workspace_state (
                project_id TEXT PRIMARY KEY, last_member_id TEXT,
                filter_star_mode TEXT NOT NULL DEFAULT 'none',
                filter_star_value INTEGER NOT NULL DEFAULT 0,
                filter_color_labels TEXT NOT NULL DEFAULT '',
                filter_filename TEXT NOT NULL DEFAULT '',
                filter_rated TEXT NOT NULL DEFAULT 'all',
                sort_field TEXT NOT NULL DEFAULT 'filename',
                sort_direction TEXT NOT NULL DEFAULT 'asc',
                filmstrip_scroll REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE operation_log (
                id TEXT PRIMARY KEY, operation_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE operation_log_items (
                id TEXT PRIMARY KEY, log_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                normalized_path TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT, seq INTEGER NOT NULL,
                FOREIGN KEY (log_id) REFERENCES operation_log(id) ON DELETE CASCADE
            );
            INSERT INTO projects VALUES ('p', 'Legacy', 't', 't');
            INSERT INTO assets VALUES (
                'a', 'c:/legacy.jpg', 'legacy.jpg', '.jpg', 10, 20, NULL,
                '2024-01-01T01:02:03', 't'
            );
            INSERT INTO project_members VALUES (
                'm', 'p', 'a', 1, 4, 'red', 'as_shot', 't'
            );
            INSERT INTO workspace_state VALUES (
                'p', 'm', 'at_least', 3, 'red', 'legacy', 'rated',
                'shot_time', 'desc', 88.0, 't'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


class RawSelectionMigrationFactsTest(unittest.TestCase):
    def test_v1_to_v2_is_idempotent_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "projects.sqlite3"
            _create_v1_database(db_path)

            db = RawSelectionDB(db_path)
            member = db.get_member("m")
            workspace = db.get_workspace_state("p")
            self.assertIsNotNone(member)
            self.assertEqual(member.star_rating, 4)
            self.assertEqual(member.shot_time, "2024-01-01T01:02:03")
            self.assertIsNone(member.width)
            self.assertIsNone(member.exported_at)
            self.assertEqual(workspace.filter_exported, "all")
            self.assertEqual(workspace.filter_formats, "")
            db.save_workspace_state(
                "p",
                last_member_id="m",
                filter_exported="exported",
                filter_formats="arw,jpeg",
                filter_orientations="landscape,portrait",
                sort_field="mtime_ns",
                sort_direction="desc",
                filmstrip_scroll=64.0,
            )
            db.close()

            reopened = RawSelectionDB(db_path)
            self.assertEqual(reopened.get_member("m").color_label, "red")
            persisted = reopened.get_workspace_state("p")
            self.assertEqual(persisted.filter_exported, "exported")
            self.assertEqual(persisted.filter_formats, "arw,jpeg")
            self.assertEqual(persisted.sort_field, "mtime_ns")
            reopened.close()

            connection = sqlite3.connect(db_path)
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(version, "2")

    def test_future_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "projects.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_meta VALUES ('schema_version', '99')"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "newer"):
                RawSelectionDB(db_path)

    def test_failed_v1_migration_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "projects.sqlite3"
            _create_v1_database(db_path)
            connection = sqlite3.connect(db_path)
            connection.execute("DROP TABLE operation_log_items")
            connection.commit()
            connection.close()

            with self.assertRaises(sqlite3.OperationalError):
                RawSelectionDB(db_path)

            connection = sqlite3.connect(db_path)
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(assets)").fetchall()
            }
            connection.close()
            self.assertEqual(version, "1")
            self.assertNotIn("width", columns)

    def test_late_metadata_cannot_overwrite_a_new_source_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = RawSelectionDB(Path(temporary) / "projects.sqlite3")
            try:
                asset_id = db.upsert_asset(
                    normalized_path="c:/asset.jpg",
                    file_name="asset.jpg",
                    extension=".jpg",
                    file_size=10,
                    mtime_ns=20,
                    file_identity="old",
                )
                old = db.get_asset(asset_id)
                self.assertTrue(db.claim_asset_metadata(old))
                self.assertTrue(
                    db.refresh_asset_source_version(
                        old,
                        file_size=11,
                        mtime_ns=21,
                        file_identity="new",
                    )
                )
                self.assertFalse(
                    db.update_asset_metadata(
                        old,
                        shot_time="2020-01-01T00:00:00",
                        width=10,
                        height=10,
                        metadata_status="ready",
                    )
                )
                current = db.get_asset(asset_id)
                self.assertEqual(current.metadata_status, "pending")
                self.assertIsNone(current.shot_time)
            finally:
                db.close()


class RawSelectionMetadataAndQueryFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.service = RawSelectionService(self.root / "state")
        self.addCleanup(self.service.close)
        self.project = self.service.create_project("Facts")

    def test_arw_identity_guard_accepts_only_a7m4_tiff_metadata(self) -> None:
        supported = self.root / "supported.arw"
        unsupported = self.root / "other-camera.arw"
        malformed = self.root / "malformed.arw"
        _write_minimal_arw(supported)
        _write_minimal_arw(unsupported, make="SONY", model="ILCE-7RM5")
        malformed.write_bytes(b"not a TIFF container")

        self.assertEqual(
            raw_selection_importer.read_arw_camera_identity(supported),
            ("SONY", "ILCE-7M4"),
        )
        self.assertIsNone(raw_selection_importer.unsupported_arw_camera(supported))
        self.assertIn(
            "Unsupported ARW camera",
            raw_selection_importer.unsupported_arw_camera(unsupported) or "",
        )
        with self.assertRaisesRegex(ValueError, "invalid TIFF header"):
            raw_selection_importer.read_arw_camera_identity(malformed)

        accepted = self.service.import_files(self.project["id"], [supported])
        rejected = self.service.import_files(
            self.project["id"], [unsupported, malformed]
        )
        self.assertEqual(accepted["registered"], 1)
        self.assertEqual(rejected["registered"], 0)
        self.assertEqual(rejected["skipped_unsupported_camera"], 1)
        self.assertEqual(rejected["errors"], 1)
        self.assertEqual(self.service.get_member_count(self.project["id"]), 1)

    def test_lightweight_metadata_persists_true_time_and_oriented_size(self) -> None:
        image = self.root / "portrait.jpg"
        _write_jpeg(
            image,
            size=(120, 80),
            shot_time="2025:06:07 08:09:10",
            orientation=6,
        )
        self.service.import_files(self.project["id"], [image])
        self.assertTrue(self.service.wait_for_metadata())

        member = self.service.list_members(self.project["id"])["members"][0]
        self.assertEqual(member["metadata_status"], "ready")
        self.assertEqual(member["shot_time"], "2025-06-07T08:09:10")
        self.assertEqual((member["width"], member["height"]), (80, 120))
        self.assertIsNotNone(member["file_identity"])

    def test_source_status_refreshes_changed_version_and_invalidates_preview(
        self,
    ) -> None:
        image = self.root / "externally-changed.jpg"
        _write_jpeg(image, size=(120, 80), color=(200, 20, 20))
        self.service.import_files(self.project["id"], [image])
        self.assertTrue(self.service.wait_for_metadata())
        member = self.service.list_members(self.project["id"])["members"][0]
        before = self.service.get_thumbnail_bytes(member["id"])
        self.assertIsNone(before.error)

        _write_jpeg(image, size=(321, 123), color=(20, 20, 200))
        status = self.service.get_source_status(member["id"])

        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "refreshed")
        self.assertEqual(status["code"], "source_refreshed")
        self.assertTrue(self.service.wait_for_metadata())
        refreshed = self.service.get_member(member["id"])
        self.assertEqual((refreshed["width"], refreshed["height"]), (321, 123))
        self.assertNotEqual(refreshed["file_size"], member["file_size"])
        after = self.service.get_thumbnail_bytes(member["id"])
        self.assertIsNone(after.error)
        self.assertNotEqual(after.data, before.data)

    def test_filters_sort_unknown_last_and_invalid_parameters(self) -> None:
        first = self.root / "first.jpg"
        second = self.root / "second.jpg"
        corrupt = self.root / "unknown.jpg"
        _write_jpeg(first, size=(160, 80), shot_time="2025:01:02 01:00:00")
        _write_jpeg(second, size=(80, 160), shot_time="2025:01:01 01:00:00")
        corrupt.write_bytes(b"not an image")
        self.service.import_files(self.project["id"], [first, second, corrupt])
        self.assertTrue(self.service.wait_for_metadata())

        landscape = self.service.list_members(
            self.project["id"], formats="jpeg", orientations="landscape"
        )
        self.assertEqual(
            [item["file_name"] for item in landscape["members"]], ["first.jpg"]
        )

        ascending = self.service.list_members(
            self.project["id"], sort_field="shot_time", sort_direction="asc"
        )["members"]
        descending = self.service.list_members(
            self.project["id"], sort_field="shot_time", sort_direction="desc"
        )["members"]
        self.assertEqual(ascending[-1]["file_name"], "unknown.jpg")
        self.assertEqual(descending[-1]["file_name"], "unknown.jpg")
        self.assertEqual(ascending[0]["file_name"], "second.jpg")
        self.assertEqual(descending[0]["file_name"], "first.jpg")

        invalid_calls = (
            {"sort_field": "nope"},
            {"sort_direction": "sideways"},
            {"star_mode": "exact", "star_value": 0},
            {"star_value": True},
            {"color_labels": "orange"},
            {"formats": "jpg"},
            {"orientations": "diagonal"},
            {"exported_filter": "maybe"},
        )
        for arguments in invalid_calls:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.service.list_members(self.project["id"], **arguments)

    def test_folder_import_order_is_stable_when_enumeration_is_not(self) -> None:
        folder = self.root / "ordered"
        folder.mkdir()
        for name in ("z-last.jpg", "a-first.jpg", "m-middle.jpg"):
            _write_jpeg(folder / name)
        entries = {entry.name: entry for entry in os.scandir(folder)}
        forced_order = [
            entries["z-last.jpg"],
            entries["m-middle.jpg"],
            entries["a-first.jpg"],
        ]
        original_scandir = os.scandir

        def forced_scandir(path):
            if Path(path) == folder.resolve():
                return iter(forced_order)
            return original_scandir(path)

        with patch(
            "image_vector_service.raw_selection.importer.os.scandir",
            side_effect=forced_scandir,
        ):
            self.service.import_folder(self.project["id"], folder)

        imported = self.service.list_members(
            self.project["id"], sort_field="import_order"
        )["members"]
        self.assertEqual(
            [member["file_name"] for member in imported],
            ["a-first.jpg", "m-middle.jpg", "z-last.jpg"],
        )

    def test_progressive_import_job_reports_progress_cancels_and_logs(self) -> None:
        started = threading.Event()

        def blocked_import(
            project_id,
            folder_path,
            *,
            on_batch,
            cancel_event,
        ):
            self.assertEqual(project_id, self.project["id"])
            self.assertEqual(Path(folder_path), self.root / "import-job")
            on_batch(2, 3)
            started.set()
            self.assertTrue(cancel_event.wait(5))
            return raw_selection_importer.ImportResult(registered=2)

        with patch.object(
            self.service._importer,
            "import_folder_incremental",
            side_effect=blocked_import,
        ):
            initial = self.service.start_folder_import(
                self.project["id"], self.root / "import-job"
            )
            self.assertEqual(initial["kind"], "import")
            self.assertIsNotNone(initial["log_id"])
            self.assertTrue(started.wait(5))
            running = self.service.get_job(initial["id"])
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["phase"], "scanning")
            self.assertEqual(running["progress"]["registered"], 2)
            self.assertEqual(running["progress"]["seen"], 3)
            self.assertTrue(self.service.cancel_job(initial["id"]))
            final = _wait_for_job(self.service, initial["id"])

        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["result"]["registered"], 2)
        self.assertFalse(self.service.cancel_job(initial["id"]))
        log = self.service._db.get_operation_log_entry(initial["log_id"])
        self.assertIsNotNone(log)
        self.assertEqual(log.operation_type, "import")
        self.assertEqual(log.status, "cancelled")

    def test_mutation_boundaries_reject_invalid_persisted_state(self) -> None:
        image = self.root / "state.jpg"
        _write_jpeg(image)
        self.service.import_files(self.project["id"], [image])
        member = self.service.list_members(self.project["id"])["members"][0]

        with self.assertRaises(ValueError):
            self.service.update_rating(member["id"], 1.0, "red")
        with self.assertRaises(ValueError):
            self.service.update_rating(member["id"], 1, [])
        with self.assertRaises(ValueError):
            self.service.update_creative_look(member["id"], "not-a-look")
        with self.assertRaises(ValueError):
            self.service.update_creative_look(member["id"], "VV")
        self.assertTrue(self.service.update_creative_look(member["id"], "as_shot"))
        with self.assertRaises(ValueError):
            self.service.save_workspace_state(
                self.project["id"],
                last_member_id=member["id"],
                filter_star_mode="exact",
                filter_star_value=0,
            )
        with self.assertRaises(ValueError):
            self.service.save_workspace_state("missing", last_member_id=None)

    def test_png_service_payload_preserves_alpha_and_cache_content_type(self) -> None:
        image = self.root / "alpha.png"
        Image.new("RGBA", (80, 60), (10, 20, 30, 96)).save(image, "PNG")
        self.service.import_files(self.project["id"], [image])
        member = self.service.list_members(self.project["id"])["members"][0]

        thumbnail = self.service.get_thumbnail_bytes(member["id"])
        preview = self.service.get_preview_bytes(member["id"])
        cached_thumbnail = self.service.get_thumbnail_bytes(member["id"])

        for result in (thumbnail, preview, cached_thumbnail):
            self.assertIsNone(result.error)
            self.assertEqual(result.content_type, "image/png")
            with Image.open(io.BytesIO(result.data)) as decoded:
                self.assertEqual(decoded.mode, "RGBA")
                self.assertEqual(decoded.getpixel((0, 0))[3], 96)

    def test_palette_png_service_payload_preserves_transparency(self) -> None:
        image = self.root / "palette-alpha.png"
        palette = Image.new("P", (20, 20), 0)
        palette.putpalette([10, 20, 30] + [0, 0, 0] * 255)
        palette.info["transparency"] = 0
        palette.save(image, "PNG")
        self.service.import_files(self.project["id"], [image])
        member = self.service.list_members(self.project["id"])["members"][0]

        preview = self.service.get_preview_bytes(member["id"])

        self.assertIsNone(preview.error)
        self.assertEqual(preview.content_type, "image/png")
        with Image.open(io.BytesIO(preview.data)) as decoded:
            self.assertEqual(decoded.mode, "RGBA")
            self.assertEqual(decoded.getpixel((0, 0))[3], 0)

    def test_processing_metadata_is_retried_after_restart(self) -> None:
        image = self.root / "restart.jpg"
        _write_jpeg(image, shot_time="2025:07:08 09:10:11")
        state = self.root / "restart-state"
        db = RawSelectionDB(state / "projects.sqlite3")
        try:
            project = db.create_project("Restart")
            file_stat = image.stat()
            _added, assets = db.register_assets_batch(
                project.id,
                [
                    AssetRegistration(
                        normalized_path=os.path.normcase(str(image.resolve())),
                        file_name=image.name,
                        extension=".jpg",
                        file_size=file_stat.st_size,
                        mtime_ns=file_stat.st_mtime_ns,
                        file_identity=None,
                    )
                ],
            )
            self.assertTrue(db.claim_asset_metadata(assets[0]))
        finally:
            db.close()

        reopened = RawSelectionService(state)
        try:
            self.assertTrue(reopened.wait_for_metadata())
            member = reopened.list_members(project.id)["members"][0]
            self.assertEqual(member["metadata_status"], "ready")
            self.assertEqual(member["shot_time"], "2025-07-08T09:10:11")
        finally:
            reopened.close()

    def test_exif_orientation_matches_metadata_thumbnail_preview_and_full(
        self,
    ) -> None:
        image = self.root / "oriented.jpg"
        source = Image.new("RGB", (120, 80), (0, 0, 0))
        for x in range(120):
            color = (240, 10, 10) if x < 60 else (10, 10, 240)
            for y in range(80):
                source.putpixel((x, y), color)
        exif = Image.Exif()
        exif[274] = 6
        source.save(image, "JPEG", quality=100, subsampling=0, exif=exif)
        self.service.import_files(self.project["id"], [image])
        self.assertTrue(self.service.wait_for_metadata())
        member = self.service.list_members(self.project["id"])["members"][0]

        self.assertEqual((member["width"], member["height"]), (80, 120))
        for decode in (decode_thumbnail, decode_preview, decode_full_base):
            with self.subTest(decode=decode.__name__):
                result = decode(str(image), ".jpg")
                self.assertIsNone(result.error)
                self.assertIsNotNone(result.image)
                assert result.image is not None
                try:
                    self.assertEqual(result.image.size, (80, 120))
                    top = result.image.getpixel((40, 10))
                    bottom = result.image.getpixel((40, 110))
                    self.assertGreater(top[0], top[2])
                    self.assertGreater(bottom[2], bottom[0])
                finally:
                    result.image.close()

    def test_arw_container_orientation_matches_metadata_thumbnail_and_preview(
        self,
    ) -> None:
        image = self.root / "oriented.arw"
        _write_embedded_arw(image, orientation=6)

        imported = self.service.import_files(self.project["id"], [image])
        self.assertEqual(imported["registered"], 1)
        self.assertTrue(self.service.wait_for_metadata())
        member = self.service.list_members(self.project["id"])["members"][0]
        self.assertEqual((member["width"], member["height"]), (80, 120))

        for decode in (decode_thumbnail, decode_preview):
            with self.subTest(decode=decode.__name__):
                result = decode(str(image), ".arw")
                self.assertIsNone(result.error)
                self.assertIsNotNone(result.image)
                assert result.image is not None
                try:
                    self.assertEqual(result.image.size, (80, 120))
                    top = result.image.getpixel((40, 10))
                    bottom = result.image.getpixel((40, 110))
                    self.assertGreater(top[0], top[2])
                    self.assertGreater(bottom[2], bottom[0])
                finally:
                    result.image.close()

        fast_preview = decode_preview(
            str(image),
            ".arw",
            display_width=48,
            display_height=48,
            fast=True,
        )
        self.assertIsNone(fast_preview.error)
        self.assertIsNotNone(fast_preview.image)
        assert fast_preview.image is not None
        try:
            self.assertEqual(fast_preview.image.size, (40, 60))
            top = fast_preview.image.getpixel((20, 5))
            bottom = fast_preview.image.getpixel((20, 55))
            self.assertGreater(top[0], top[2])
            self.assertGreater(bottom[2], bottom[0])
        finally:
            fast_preview.image.close()

    def test_arw_full_decode_uses_camera_orientation(self) -> None:
        import numpy as np

        postprocess_arguments = {}

        class _FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def postprocess(self, **kwargs):
                postprocess_arguments.update(kwargs)
                if kwargs.get("user_flip") is None:
                    return np.zeros((80, 40, 3), dtype=np.uint8)
                return np.zeros((40, 80, 3), dtype=np.uint8)

        fake_rawpy = types.SimpleNamespace(imread=lambda _path: _FakeRaw())
        with (
            patch(
                "image_vector_service.raw_selection.decoder.is_rawpy_available",
                return_value=True,
            ),
            patch.dict(sys.modules, {"rawpy": fake_rawpy}),
        ):
            result = decode_full_base("ignored.arw", ".arw")

        self.assertIsNone(result.error)
        self.assertIsNotNone(result.image)
        assert result.image is not None
        try:
            self.assertEqual(result.image.size, (40, 80))
            self.assertIsNone(postprocess_arguments.get("user_flip"))
        finally:
            result.image.close()

    def test_async_metadata_and_filters_preserve_deterministic_order(self) -> None:
        inputs = (
            ("z-portrait.jpg", (80, 160), "2025:01:02 01:00:00"),
            ("a-landscape.jpg", (160, 80), "2025:01:01 01:00:00"),
            ("m-landscape.jpg", (160, 80), "2025:01:01 01:00:00"),
        )
        paths = []
        for name, size, shot_time in inputs:
            path = self.root / name
            _write_jpeg(path, size=size, shot_time=shot_time)
            paths.append(path)
        unknown = self.root / "u-unknown.jpg"
        unknown.write_bytes(b"not an image")
        paths.append(unknown)

        metadata_started = threading.Event()
        allow_metadata = threading.Event()
        original_extract = raw_selection_importer.extract_lightweight_metadata

        def gated_metadata(asset):
            metadata_started.set()
            self.assertTrue(allow_metadata.wait(5))
            return original_extract(asset)

        with patch(
            "image_vector_service.raw_selection.importer.extract_lightweight_metadata",
            side_effect=gated_metadata,
        ):
            try:
                self.service.import_files(self.project["id"], paths)
                self.assertTrue(metadata_started.wait(5))
                default_before = self.service.list_members(self.project["id"])[
                    "members"
                ]
                import_before = self.service.list_members(
                    self.project["id"], sort_field="import_order"
                )["members"]
                allow_metadata.set()
                self.assertTrue(self.service.wait_for_metadata())
            finally:
                allow_metadata.set()

        default_after = self.service.list_members(self.project["id"])["members"]
        import_after = self.service.list_members(
            self.project["id"], sort_field="import_order"
        )["members"]
        self.assertEqual(
            [member["id"] for member in default_before],
            [member["id"] for member in default_after],
        )
        self.assertEqual(
            [member["id"] for member in import_before],
            [member["id"] for member in import_after],
        )

        landscape = self.service.list_members(
            self.project["id"],
            orientations="landscape",
            sort_field="import_order",
        )["members"]
        expected_landscape = [
            member["id"]
            for member in import_after
            if member["width"] is not None and member["width"] > member["height"]
        ]
        self.assertEqual([member["id"] for member in landscape], expected_landscape)

        members_by_name = {member["file_name"]: member for member in import_after}
        ratings = {
            "z-portrait.jpg": 2,
            "a-landscape.jpg": 1,
            "m-landscape.jpg": 2,
            "u-unknown.jpg": 2,
        }
        for name, rating in ratings.items():
            self.service.update_rating(members_by_name[name]["id"], rating, "none")

        for sort_field in (
            "filename",
            "import_order",
            "shot_time",
            "star_rating",
            "mtime_ns",
            "extension",
            "file_size",
        ):
            for direction in ("asc", "desc"):
                with self.subTest(sort_field=sort_field, direction=direction):
                    first = self.service.list_members(
                        self.project["id"],
                        sort_field=sort_field,
                        sort_direction=direction,
                    )["members"]
                    second = self.service.list_members(
                        self.project["id"],
                        sort_field=sort_field,
                        sort_direction=direction,
                    )["members"]
                    self.assertEqual(
                        [member["id"] for member in first],
                        [member["id"] for member in second],
                    )
                    filtered = self.service.list_members(
                        self.project["id"],
                        orientations="landscape",
                        sort_field=sort_field,
                        sort_direction=direction,
                    )["members"]
                    expected = [
                        member["id"]
                        for member in first
                        if member["width"] is not None
                        and member["width"] > member["height"]
                    ]
                    self.assertEqual([member["id"] for member in filtered], expected)

        star_ascending = self.service.list_members(
            self.project["id"], sort_field="star_rating", sort_direction="asc"
        )["members"]
        star_descending = self.service.list_members(
            self.project["id"], sort_field="star_rating", sort_direction="desc"
        )["members"]
        self.assertEqual(
            [member["file_name"] for member in star_ascending],
            [
                "a-landscape.jpg",
                "z-portrait.jpg",
                "m-landscape.jpg",
                "u-unknown.jpg",
            ],
        )
        self.assertEqual(
            [member["file_name"] for member in star_descending],
            [
                "z-portrait.jpg",
                "m-landscape.jpg",
                "u-unknown.jpg",
                "a-landscape.jpg",
            ],
        )
        for direction in ("asc", "desc"):
            shot_time = self.service.list_members(
                self.project["id"],
                sort_field="shot_time",
                sort_direction=direction,
            )["members"]
            self.assertEqual(shot_time[-1]["file_name"], "u-unknown.jpg")

    def test_uncalibrated_arw_look_is_rejected_and_legacy_value_is_reset(self) -> None:
        state = self.root / "look-state"
        source = self.root / "look.arw"
        _write_minimal_arw(source)
        service = RawSelectionService(state)
        try:
            project = service.create_project("Looks")
            service.import_files(project["id"], [source])
            member = service.list_members(project["id"])["members"][0]
            self.assertEqual(
                [item["id"] for item in service.list_creative_looks()], ["as_shot"]
            )
            with self.assertRaisesRegex(ValueError, "Invalid creative look"):
                service.update_creative_look(member["id"], "VV")
            with self.assertRaisesRegex(ValueError, "Invalid creative look"):
                service.get_preview_bytes(member["id"], look="BW")

            # Simulate a value persisted by an older uncalibrated build.
            self.assertTrue(service._db.update_creative_look(member["id"], "VV"))
        finally:
            service.close()

        reopened = RawSelectionService(state)
        try:
            self.assertEqual(
                reopened.get_member(member["id"])["creative_look"], "as_shot"
            )
        finally:
            reopened.close()


class RawSelectionExportFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state = self.root / "state"
        self.service = RawSelectionService(self.state)
        self.addCleanup(self.service.close)

    def test_export_is_project_scoped_no_overwrite_and_persistent(self) -> None:
        source = self.root / "same.jpg"
        _write_jpeg(source)
        first_project = self.service.create_project("First")
        second_project = self.service.create_project("Second")
        self.service.import_files(first_project["id"], [source])
        self.service.import_files(second_project["id"], [source])
        self.assertTrue(self.service.wait_for_metadata())
        first_member = self.service.list_members(first_project["id"])["members"][0]
        second_member = self.service.list_members(second_project["id"])["members"][0]

        destination = self.root / "export"
        destination.mkdir()
        existing = destination / source.name
        existing.write_bytes(b"keep")
        result = self.service.export_files(
            first_project["id"], [first_member["id"]], destination
        )
        self.assertEqual(result["exported"], 1)
        log = self.service._db.get_operation_log_entry(result["log_id"])
        self.assertIsNotNone(log)
        self.assertEqual(log.operation_type, "export")
        self.assertEqual(log.status, "completed")
        log_items = self.service._db.get_operation_log_items(result["log_id"])
        self.assertEqual([item.result for item in log_items], ["exported"])
        self.assertEqual(existing.read_bytes(), b"keep")
        exported = Path(result["details"][0]["dest"])
        self.assertEqual(exported.name, "same_1.jpg")
        self.assertEqual(
            hashlib.sha256(exported.read_bytes()).digest(),
            hashlib.sha256(source.read_bytes()).digest(),
        )
        self.assertTrue(self.service.get_member(first_member["id"])["is_exported"])
        self.assertFalse(self.service.get_member(second_member["id"])["is_exported"])
        self.assertEqual(
            self.service.list_members(first_project["id"], exported_filter="exported")[
                "filtered"
            ],
            1,
        )
        self.assertEqual(
            self.service.list_members(
                second_project["id"], exported_filter="unexported"
            )["filtered"],
            1,
        )
        with self.assertRaises(ValueError):
            self.service.export_files(
                second_project["id"], [first_member["id"]], destination
            )

        reopened = RawSelectionService(self.state)
        try:
            self.assertTrue(reopened.get_member(first_member["id"])["is_exported"])
        finally:
            reopened.close()

    def test_export_job_reports_progress_cancels_and_logs_every_item(self) -> None:
        sources = [self.root / "first.jpg", self.root / "second.jpg"]
        for source in sources:
            _write_jpeg(source)
        project = self.service.create_project("Export job")
        self.service.import_files(project["id"], sources)
        members = self.service.list_members(project["id"])["members"]
        started = threading.Event()
        release = threading.Event()
        original_copy = raw_selection_service._copy_source_exact

        def blocked_copy(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return original_copy(*args, **kwargs)

        with patch(
            "image_vector_service.raw_selection.service._copy_source_exact",
            side_effect=blocked_copy,
        ):
            initial = self.service.start_export(
                project["id"],
                [member["id"] for member in members],
                self.root / "job-output",
            )
            try:
                self.assertEqual(initial["kind"], "export")
                self.assertIsNotNone(initial["log_id"])
                self.assertTrue(started.wait(5))
                running = self.service.get_job(initial["id"])
                self.assertEqual(running["status"], "running")
                self.assertEqual(running["phase"], "copying")
                self.assertEqual(running["progress"]["total"], 2)
                self.assertTrue(self.service.cancel_job(initial["id"]))
            finally:
                release.set()
            final = _wait_for_job(self.service, initial["id"])

        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["result"]["cancelled"], 2)
        self.assertEqual(final["progress"]["completed"], 2)
        log = self.service._db.get_operation_log_entry(initial["log_id"])
        self.assertIsNotNone(log)
        self.assertEqual(log.operation_type, "export")
        self.assertEqual(log.status, "cancelled")
        log_items = self.service._db.get_operation_log_items(initial["log_id"])
        self.assertEqual(
            [item.result for item in log_items],
            ["cancelled", "cancelled"],
        )

    def test_changed_or_cancelled_source_does_not_mark_exported(self) -> None:
        changed = self.root / "changed.jpg"
        cancelled = self.root / "cancelled.jpg"
        _write_jpeg(changed)
        _write_jpeg(cancelled)
        project = self.service.create_project("Project")
        self.service.import_files(project["id"], [changed, cancelled])
        self.assertTrue(self.service.wait_for_metadata())
        members = {
            member["file_name"]: member
            for member in self.service.list_members(project["id"])["members"]
        }

        changed.write_bytes(changed.read_bytes() + b"changed")
        failed = self.service.export_files(
            project["id"], [members["changed.jpg"]["id"]], self.root / "out"
        )
        self.assertEqual(failed["failed"], 1)
        self.assertFalse(
            self.service.get_member(members["changed.jpg"]["id"])["is_exported"]
        )

        cancel = threading.Event()
        cancel.set()
        result = self.service.export_files(
            project["id"],
            [members["cancelled.jpg"]["id"]],
            self.root / "out",
            cancel_event=cancel,
        )
        self.assertEqual(result["cancelled"], 1)
        self.assertFalse(
            self.service.get_member(members["cancelled.jpg"]["id"])["is_exported"]
        )

    def test_new_source_version_resets_member_export_state(self) -> None:
        source = self.root / "versioned.jpg"
        _write_jpeg(source)
        project = self.service.create_project("Versioned")
        self.service.import_files(project["id"], [source])
        member = self.service.list_members(project["id"])["members"][0]
        result = self.service.export_files(
            project["id"], [member["id"]], self.root / "versioned-out"
        )
        self.assertEqual(result["exported"], 1)
        self.assertTrue(self.service.get_member(member["id"])["is_exported"])

        source.write_bytes(source.read_bytes() + b"new source version")
        reimported = self.service.import_files(project["id"], [source])

        self.assertEqual(reimported["registered"], 0)
        self.assertFalse(self.service.get_member(member["id"])["is_exported"])

    def test_source_change_after_copy_cannot_mark_new_version_exported(self) -> None:
        source = self.root / "copy-race.jpg"
        _write_jpeg(source)
        project = self.service.create_project("Copy race")
        self.service.import_files(project["id"], [source])
        member = self.service.list_members(project["id"])["members"][0]
        copy_finished = threading.Event()
        allow_export_commit = threading.Event()
        original_copy = raw_selection_service._copy_source_exact

        def gated_copy(*args, **kwargs):
            destination = original_copy(*args, **kwargs)
            copy_finished.set()
            self.assertTrue(allow_export_commit.wait(5))
            return destination

        with (
            patch(
                "image_vector_service.raw_selection.service._copy_source_exact",
                side_effect=gated_copy,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            try:
                request = pool.submit(
                    self.service.export_files,
                    project["id"],
                    [member["id"]],
                    self.root / "copy-race-out",
                )
                self.assertTrue(copy_finished.wait(5))
                source.write_bytes(source.read_bytes() + b"changed after copy")
                self.service.import_files(project["id"], [source])
                allow_export_commit.set()
                result = request.result(5)
            finally:
                allow_export_commit.set()

        self.assertEqual(result["exported"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(self.service.get_member(member["id"])["is_exported"])


class RawSelectionDeleteFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.service = RawSelectionService(self.root / "state")
        self.addCleanup(self.service.close)

    def _import_one(self, file_name: str = "source.jpg") -> tuple[str, Path, dict]:
        source = self.root / file_name
        _write_jpeg(source)
        project = self.service.create_project(file_name)
        self.service.import_files(project["id"], [source])
        self.assertTrue(self.service.wait_for_metadata())
        member = self.service.list_members(project["id"])["members"][0]
        return project["id"], source, member

    def test_replaced_identity_and_directory_are_not_deleted_or_cleaned(self) -> None:
        project_id, source, member = self._import_one("replace.jpg")
        original_stat = source.stat()
        replacement = self.root / "replacement.jpg"
        replacement.write_bytes(source.read_bytes())
        os.replace(replacement, source)
        os.utime(
            source,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        result = self.service.permanent_delete([member["id"]], confirmed=True)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(source.is_file())
        self.assertEqual(self.service.get_member_count(project_id), 1)

        directory_project, directory_source, directory_member = self._import_one(
            "directory.jpg"
        )
        directory_source.unlink()
        directory_source.mkdir()
        directory_result = self.service.permanent_delete(
            [directory_member["id"]], confirmed=True
        )
        self.assertEqual(directory_result["failed"], 1)
        self.assertTrue(directory_source.is_dir())
        self.assertEqual(self.service.get_member_count(directory_project), 1)

    def test_pending_confirmed_delete_resumes_safely(self) -> None:
        project_id, source, member = self._import_one("recover.jpg")
        target = OperationTarget(
            asset_id=member["asset_id"],
            normalized_path=member["normalized_path"],
            source_file_size=member["file_size"],
            source_mtime_ns=member["mtime_ns"],
            source_file_identity=member["file_identity"],
            source_extension=member["extension"],
        )
        log_id = self.service._db.create_operation_log(
            "permanent_delete", [target], payload_json='{"confirmed": true}'
        )
        self.service._db.set_operation_log_status(log_id, "in_progress")
        self.assertEqual(self.service.recover_pending_deletes(), 1)
        self.assertFalse(source.exists())
        self.assertEqual(self.service.get_member_count(project_id), 0)
        self.assertEqual(
            self.service._db.get_operation_log_entry(log_id).status,
            "completed",
        )
        self.assertEqual(self.service.recover_pending_deletes(), 0)

    def test_shared_source_is_deleted_once_without_touching_sidecars(self) -> None:
        source = self.root / "shared.jpg"
        sidecar = self.root / "shared.xmp"
        sibling = self.root / "shared.png"
        _write_jpeg(source)
        sidecar.write_bytes(b"keep sidecar")
        Image.new("RGBA", (20, 20), (1, 2, 3, 4)).save(sibling, "PNG")
        first = self.service.create_project("First")
        second = self.service.create_project("Second")
        self.service.import_files(first["id"], [source])
        self.service.import_files(second["id"], [source])
        first_member = self.service.list_members(first["id"])["members"][0]
        second_member = self.service.list_members(second["id"])["members"][0]

        result = self.service.permanent_delete(
            [first_member["id"], second_member["id"]], confirmed=True
        )

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(source.exists())
        self.assertEqual(sidecar.read_bytes(), b"keep sidecar")
        self.assertTrue(sibling.exists())
        self.assertEqual(self.service.get_member_count(first["id"]), 0)
        self.assertEqual(self.service.get_member_count(second["id"]), 0)

    def test_access_error_is_not_treated_as_missing(self) -> None:
        project_id, source, member = self._import_one("denied.jpg")
        with patch(
            "image_vector_service.raw_selection.service.os.lstat",
            side_effect=PermissionError("denied"),
        ):
            result = self.service.permanent_delete([member["id"]], confirmed=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["already_missing"], 0)
        self.assertTrue(source.exists())
        self.assertEqual(self.service.get_member_count(project_id), 1)


class RawSelectionSchedulerCacheIntegrationFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.service = RawSelectionService(self.root / "state")
        self.addCleanup(self.service.close)
        self.project = self.service.create_project("Scheduling")

    def _import_jpeg(self, name: str = "source.jpg") -> dict:
        source = self.root / name
        _write_jpeg(source)
        self.service.import_files(self.project["id"], [source])
        return self.service.list_members(self.project["id"])["members"][0]

    def test_thumbnail_single_flight_uses_identity_aware_cache(self) -> None:
        member = self._import_jpeg()
        started = threading.Event()
        release = threading.Event()
        two_submissions = threading.Event()
        caller_barrier = threading.Barrier(3)
        call_lock = threading.Lock()
        calls = 0
        submissions = 0
        original_submit = self.service._scheduler.submit_single_flight

        def fake_decode(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            with call_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(5))
            image = Image.new("RGB", (120, 80), (30, 60, 90))
            return DecodeResult(image, 120, 80, "thumbnail", "pillow")

        def wrapped_submit(*args, **kwargs):
            nonlocal submissions
            with call_lock:
                submissions += 1
                if submissions == 2:
                    two_submissions.set()
            return original_submit(*args, **kwargs)

        def request_thumbnail():
            caller_barrier.wait()
            return self.service.get_thumbnail_bytes(member["id"], priority="visible")

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_thumbnail",
                side_effect=fake_decode,
            ),
            patch.object(
                self.service._scheduler,
                "submit_single_flight",
                side_effect=wrapped_submit,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(request_thumbnail)
            second = pool.submit(request_thumbnail)
            caller_barrier.wait()
            self.assertTrue(started.wait(5))
            self.assertTrue(two_submissions.wait(5))
            release.set()
            results = (first.result(5), second.result(5))

        self.assertEqual(calls, 1)
        self.assertEqual(results[0].data, results[1].data)
        cached = self.service._derived_cache.get(
            normalized_path=member["normalized_path"],
            file_size=member["file_size"],
            mtime_ns=member["mtime_ns"],
            file_identity=member["file_identity"],
            kind="thumbnails",
        )
        self.assertEqual(cached, results[0].data)

    def test_clear_makes_inflight_cache_write_stale(self) -> None:
        member = self._import_jpeg("stale.jpg")
        started = threading.Event()
        release = threading.Event()

        def fake_decode(_path: str, _extension: str) -> DecodeResult:
            started.set()
            self.assertTrue(release.wait(5))
            image = Image.new("RGB", (120, 80), (90, 60, 30))
            return DecodeResult(image, 120, 80, "thumbnail", "pillow")

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_thumbnail",
                side_effect=fake_decode,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            request = pool.submit(self.service.get_thumbnail_bytes, member["id"])
            self.assertTrue(started.wait(5))
            self.service.clear_project_cache(self.project["id"])
            release.set()
            result = request.result(5)

        self.assertTrue(result.retryable)
        self.assertIsNotNone(result.error)
        self.assertIsNone(
            self.service._derived_cache.get(
                normalized_path=member["normalized_path"],
                file_size=member["file_size"],
                mtime_ns=member["mtime_ns"],
                file_identity=member["file_identity"],
                kind="thumbnails",
            )
        )

    def test_cancel_project_work_makes_inflight_result_stale(self) -> None:
        member = self._import_jpeg("cancel-stale.jpg")
        started = threading.Event()
        release = threading.Event()

        def fake_decode(_path: str, _extension: str) -> DecodeResult:
            started.set()
            self.assertTrue(release.wait(5))
            image = Image.new("RGB", (120, 80), (90, 60, 30))
            return DecodeResult(image, 120, 80, "thumbnail", "pillow")

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_thumbnail",
                side_effect=fake_decode,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            try:
                request = pool.submit(self.service.get_thumbnail_bytes, member["id"])
                self.assertTrue(started.wait(5))
                self.service.cancel_project_work(self.project["id"])
                release.set()
                result = request.result(5)
            finally:
                release.set()

        self.assertTrue(result.retryable)
        self.assertIsNotNone(result.error)
        self.assertIsNone(
            self.service._derived_cache.get(
                normalized_path=member["normalized_path"],
                file_size=member["file_size"],
                mtime_ns=member["mtime_ns"],
                file_identity=member["file_identity"],
                kind="thumbnails",
            )
        )

    def test_scheduler_rejections_are_explicitly_retryable(self) -> None:
        member = self._import_jpeg("retry.jpg")
        for failure in (
            SchedulerQueueFull("full"),
            StaleGeneration("stale"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(
                    self.service._scheduler,
                    "submit_single_flight",
                    side_effect=failure,
                ),
            ):
                result = self.service.get_thumbnail_bytes(member["id"])
                self.assertTrue(result.retryable)
                self.assertIsNotNone(result.error)

    def test_uncalibrated_looks_are_rejected_before_full_decode(self) -> None:
        source = self.root / "sample.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member = self.service.list_members(self.project["id"])["members"][0]

        with patch(
            "image_vector_service.raw_selection.service.decode_full_base",
        ) as decode_full:
            for look in ("VV", "BW"):
                with (
                    self.subTest(look=look),
                    self.assertRaisesRegex(ValueError, "Invalid creative look"),
                ):
                    self.service.get_preview_bytes(member["id"], look=look)

        decode_full.assert_not_called()
        self.assertEqual(len(self.service._full_bases), 0)

    def test_missing_arw_embedded_preview_falls_back_to_full_decode(self) -> None:
        source = self.root / "no-preview.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member = self.service.list_members(self.project["id"])["members"][0]
        full_calls = 0

        def missing_preview(*_args, **_kwargs) -> DecodeResult:
            return DecodeResult(
                None,
                0,
                0,
                "preview",
                "embedded_preview",
                error="No embedded preview in ARW",
            )

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal full_calls
            full_calls += 1
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_preview",
                side_effect=missing_preview,
            ),
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
        ):
            result = self.service.get_preview_bytes(member["id"], look="as_shot")

        self.assertIsNone(result.error)
        self.assertEqual(result.content_type, "image/jpeg")
        self.assertGreater(len(result.data), 0)
        self.assertEqual(full_calls, 1)

    def test_failed_full_base_releases_single_flight_for_retry(self) -> None:
        source = self.root / "retry-base.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member = self.service.list_members(self.project["id"])["members"][0]
        calls = 0

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return DecodeResult(
                    None,
                    0,
                    0,
                    "full",
                    "full_decode",
                    error="decode failed",
                )
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        def missing_preview(*_args, **_kwargs) -> DecodeResult:
            return DecodeResult(
                None,
                0,
                0,
                "preview",
                "embedded_preview",
                error="No embedded preview in ARW",
            )

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_preview",
                side_effect=missing_preview,
            ),
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
        ):
            failed = self.service.get_preview_bytes(member["id"], look="as_shot")
            retried = self.service.get_preview_bytes(member["id"], look="as_shot")

        self.assertIsNotNone(failed.error)
        self.assertIsNone(retried.error)
        self.assertEqual(calls, 2)

    def test_full_base_closes_scheduler_to_lru_single_flight_window(self) -> None:
        source = self.root / "flight-window.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member_dict = self.service.list_members(self.project["id"])["members"][0]
        member = self.service._db.get_member(member_dict["id"])
        self.assertIsNotNone(member)
        assert member is not None
        token = self.service._scheduler.capture_generation(self.project["id"])

        decode_started = threading.Event()
        allow_decode = threading.Event()
        scheduler_flight_released = threading.Event()
        result_window_open = threading.Event()
        allow_lru_publish = threading.Event()
        second_arrived = threading.Event()
        third_arrived = threading.Event()
        state_lock = threading.Lock()
        calls = 0
        submissions = 0
        pending_waiters = 0
        original_submit = self.service._scheduler.submit_single_flight
        pending_future: Future = Future()
        original_pending_result = pending_future.result

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            with state_lock:
                calls += 1
            decode_started.set()
            self.assertTrue(allow_decode.wait(5))
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        def observed_pending_result(timeout=None):
            nonlocal pending_waiters
            with state_lock:
                pending_waiters += 1
                if pending_waiters == 1:
                    second_arrived.set()
                elif pending_waiters == 2:
                    third_arrived.set()
            return original_pending_result(timeout)

        def wrapped_submit(*args, **kwargs):
            nonlocal submissions
            scheduled = original_submit(*args, **kwargs)
            with state_lock:
                submissions += 1
                if submissions == 2:
                    second_arrived.set()
                elif submissions == 3:
                    third_arrived.set()
            scheduled.future.add_done_callback(
                lambda _future: scheduler_flight_released.set()
            )

            class _ResultGate:
                def result(self, timeout=None):
                    result = scheduled.result(timeout)
                    if not scheduler_flight_released.wait(5):
                        raise AssertionError("scheduler flight was not released")
                    result_window_open.set()
                    if not allow_lru_publish.wait(5):
                        raise AssertionError("LRU publish gate was not released")
                    return result

            return _ResultGate()

        def borrow_base() -> int:
            with self.service._borrow_full_base(
                member,
                token,
                TaskPriority.CURRENT,
            ) as base:
                return id(base.image)

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
            patch(
                "image_vector_service.raw_selection.service.Future",
                return_value=pending_future,
            ),
            patch.object(
                pending_future,
                "result",
                side_effect=observed_pending_result,
            ),
            patch.object(
                self.service._scheduler,
                "submit_single_flight",
                side_effect=wrapped_submit,
            ),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            try:
                first = pool.submit(borrow_base)
                self.assertTrue(decode_started.wait(5))
                second = pool.submit(borrow_base)
                self.assertTrue(second_arrived.wait(5))
                allow_decode.set()
                self.assertTrue(result_window_open.wait(5))
                self.assertEqual(
                    self.service._scheduler.metrics()["inflight_single_flight"],
                    0,
                )
                third = pool.submit(borrow_base)
                self.assertTrue(third_arrived.wait(5))
                allow_lru_publish.set()
                image_ids = (
                    first.result(5),
                    second.result(5),
                    third.result(5),
                )
            finally:
                allow_decode.set()
                allow_lru_publish.set()

        self.assertEqual(calls, 1)
        self.assertEqual(submissions, 1)
        self.assertEqual(len(set(image_ids)), 1)
        self.assertEqual(self.service._full_base_pending, {})

    def test_clear_during_full_base_publish_does_not_restore_stale_lru(self) -> None:
        source = self.root / "stale-full-base.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member = self.service.list_members(self.project["id"])["members"][0]
        decode_started = threading.Event()
        allow_decode = threading.Event()
        publish_window = threading.Event()
        allow_publish = threading.Event()
        original_submit = self.service._scheduler.submit_single_flight

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            decode_started.set()
            self.assertTrue(allow_decode.wait(5))
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        def missing_preview(*_args, **_kwargs) -> DecodeResult:
            return DecodeResult(
                None,
                0,
                0,
                "preview",
                "embedded_preview",
                error="No embedded preview in ARW",
            )

        def wrapped_submit(*args, **kwargs):
            scheduled = original_submit(*args, **kwargs)
            if args[2] != "full":
                return scheduled
            flight_released = threading.Event()
            scheduled.future.add_done_callback(lambda _future: flight_released.set())

            class _ResultGate:
                def result(self, timeout=None):
                    result = scheduled.result(timeout)
                    if not flight_released.wait(5):
                        raise AssertionError("scheduler flight was not released")
                    publish_window.set()
                    if not allow_publish.wait(5):
                        raise AssertionError("publish gate was not released")
                    return result

            return _ResultGate()

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_preview",
                side_effect=missing_preview,
            ),
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
            patch.object(
                self.service._scheduler,
                "submit_single_flight",
                side_effect=wrapped_submit,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            try:
                request = pool.submit(
                    self.service.get_preview_bytes,
                    member["id"],
                    look="as_shot",
                )
                self.assertTrue(decode_started.wait(5))
                allow_decode.set()
                self.assertTrue(publish_window.wait(5))
                self.service.clear_project_cache(self.project["id"])
                allow_publish.set()
                result = request.result(5)
            finally:
                allow_decode.set()
                allow_publish.set()

        self.assertTrue(result.retryable)
        self.assertEqual(len(self.service._full_bases), 0)
        self.assertEqual(self.service._full_base_pending, {})

    def test_full_base_different_keys_decode_concurrently(self) -> None:
        for name in ("parallel-a.arw", "parallel-b.arw"):
            source = self.root / name
            _write_minimal_arw(source)
            self.service.import_files(self.project["id"], [source])
        members = [
            self.service._db.get_member(item["id"])
            for item in self.service.list_members(self.project["id"])["members"]
        ]
        self.assertTrue(all(member is not None for member in members))
        concrete_members = [member for member in members if member is not None]
        token = self.service._scheduler.capture_generation(self.project["id"])
        decode_barrier = threading.Barrier(2)
        calls_lock = threading.Lock()
        calls = 0

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            with calls_lock:
                calls += 1
            decode_barrier.wait(timeout=5)
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        def borrow_base(member) -> int:
            with self.service._borrow_full_base(
                member,
                token,
                TaskPriority.CURRENT,
            ) as base:
                return id(base.image)

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            image_ids = tuple(pool.map(borrow_base, concrete_members))

        self.assertEqual(calls, 2)
        self.assertEqual(len(set(image_ids)), 2)

    def test_busy_full_bases_can_temporarily_exceed_capacity(self) -> None:
        for name in ("busy-a.arw", "busy-b.arw", "busy-c.arw"):
            source = self.root / name
            _write_minimal_arw(source)
            self.service.import_files(self.project["id"], [source])
        members = [
            self.service._db.get_member(item["id"])
            for item in self.service.list_members(self.project["id"])["members"]
        ]
        self.assertTrue(all(member is not None for member in members))
        concrete_members = [member for member in members if member is not None]
        token = self.service._scheduler.capture_generation(self.project["id"])
        calls = 0
        entries = []
        duplicate = None

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            calls += 1
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        with patch(
            "image_vector_service.raw_selection.service.decode_full_base",
            side_effect=fake_full_base,
        ):
            try:
                entries = [
                    self.service._acquire_full_base(
                        member,
                        token,
                        TaskPriority.CURRENT,
                    )
                    for member in concrete_members
                ]
                self.assertEqual(len(self.service._full_bases), 3)
                self.assertTrue(
                    all(entry.key in self.service._full_bases for entry in entries)
                )
                duplicate = self.service._acquire_full_base(
                    concrete_members[0],
                    token,
                    TaskPriority.CURRENT,
                )
                self.assertIs(duplicate, entries[0])
                self.assertEqual(calls, 3)
            finally:
                if duplicate is not None:
                    self.service._release_full_base(duplicate)
                for entry in reversed(entries):
                    self.service._release_full_base(entry)

        self.assertLessEqual(len(self.service._full_bases), 2)

    def test_concurrent_full_base_failure_is_shared_then_retry_succeeds(self) -> None:
        source = self.root / "shared-failure.arw"
        _write_minimal_arw(source)
        self.service.import_files(self.project["id"], [source])
        member_dict = self.service.list_members(self.project["id"])["members"][0]
        member = self.service._db.get_member(member_dict["id"])
        self.assertIsNotNone(member)
        assert member is not None
        token = self.service._scheduler.capture_generation(self.project["id"])
        decode_started = threading.Event()
        allow_failure = threading.Event()
        waiter_started = threading.Event()
        calls_lock = threading.Lock()
        calls = 0
        pending_future: Future = Future()
        original_pending_result = pending_future.result
        pending_future_creations = 0

        def fake_full_base(_path: str, _extension: str) -> DecodeResult:
            nonlocal calls
            with calls_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                decode_started.set()
                self.assertTrue(allow_failure.wait(5))
                return DecodeResult(
                    None,
                    0,
                    0,
                    "full",
                    "full_decode",
                    error="decode failed",
                )
            image = Image.new("RGB", (120, 80), (80, 100, 120))
            return DecodeResult(image, 120, 80, "full", "full_decode")

        def observed_pending_result(timeout=None):
            waiter_started.set()
            return original_pending_result(timeout)

        def make_pending_future() -> Future:
            nonlocal pending_future_creations
            pending_future_creations += 1
            if pending_future_creations == 1:
                return pending_future
            return Future()

        def borrow_base() -> int:
            with self.service._borrow_full_base(
                member,
                token,
                TaskPriority.CURRENT,
            ) as base:
                return id(base.image)

        with (
            patch(
                "image_vector_service.raw_selection.service.decode_full_base",
                side_effect=fake_full_base,
            ),
            patch(
                "image_vector_service.raw_selection.service.Future",
                side_effect=make_pending_future,
            ),
            patch.object(
                pending_future,
                "result",
                side_effect=observed_pending_result,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            try:
                first = pool.submit(borrow_base)
                self.assertTrue(decode_started.wait(5))
                second = pool.submit(borrow_base)
                self.assertTrue(waiter_started.wait(5))
                allow_failure.set()
                failures = (first.exception(5), second.exception(5))
            finally:
                allow_failure.set()

            self.assertTrue(all(isinstance(error, RuntimeError) for error in failures))
            self.assertTrue(all(str(error) == "decode failed" for error in failures))
            self.assertEqual(calls, 1)
            retried_image_id = borrow_base()

        self.assertIsInstance(retried_image_id, int)
        self.assertEqual(calls, 2)
        self.assertEqual(self.service._full_base_pending, {})

    def test_deleted_source_clears_cache_and_bumps_project_scope(self) -> None:
        member = self._import_jpeg("delete-cache.jpg")
        generated = self.service.get_thumbnail_bytes(member["id"])
        self.assertIsNone(generated.error)
        before = self.service._scheduler.capture_generation(self.project["id"])
        Path(member["normalized_path"]).unlink()
        self.service._cleanup_deleted_source(member["normalized_path"])
        after = self.service._scheduler.capture_generation(self.project["id"])

        self.assertEqual(after.generation, before.generation + 1)
        self.assertEqual(self.service.get_member_count(self.project["id"]), 0)
        self.assertIsNone(
            self.service._derived_cache.get(
                normalized_path=member["normalized_path"],
                file_size=member["file_size"],
                mtime_ns=member["mtime_ns"],
                file_identity=member["file_identity"],
                kind="thumbnails",
            )
        )

    def test_project_clear_preserves_unrelated_source_cache(self) -> None:
        first = self._import_jpeg("first-cache.jpg")
        other_project = self.service.create_project("Other")
        other_source = self.root / "other-cache.jpg"
        _write_jpeg(other_source, color=(90, 20, 40))
        self.service.import_files(other_project["id"], [other_source])
        other = self.service.list_members(other_project["id"])["members"][0]
        self.assertIsNone(self.service.get_thumbnail_bytes(first["id"]).error)
        other_result = self.service.get_thumbnail_bytes(other["id"])
        self.assertIsNone(other_result.error)

        self.service.clear_project_cache(self.project["id"])
        self.assertIsNone(
            self.service._derived_cache.get(
                normalized_path=first["normalized_path"],
                file_size=first["file_size"],
                mtime_ns=first["mtime_ns"],
                file_identity=first["file_identity"],
                kind="thumbnails",
            )
        )
        self.assertEqual(
            self.service._derived_cache.get(
                normalized_path=other["normalized_path"],
                file_size=other["file_size"],
                mtime_ns=other["mtime_ns"],
                file_identity=other["file_identity"],
                kind="thumbnails",
            ),
            other_result.data,
        )


if __name__ == "__main__":
    unittest.main()
