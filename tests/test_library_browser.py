from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.library_browser import LibraryBrowser
from image_vector_service.state import IndexState


def _entry(doc_id: str, root_id: str, relative_path: str) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "root_id": root_id,
        "relative_path": relative_path,
        "file_name": Path(relative_path).name,
        "extension": "jpg",
        "mime_type": "image/jpeg",
        "sha256": (doc_id[-1:] or "0") * 64,
        "size_bytes": 123,
        "mtime_ns": 456,
        "width": 800,
        "height": 1200,
        "tags": ["人工"],
        "folder_tags": ["文件夹"],
        "accepted_auto_tags": ["模型"],
        "inherited_tags": ["继承"],
    }


class LibraryBrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state_path = root / "state.sqlite3"
        self.state = IndexState(self.state_path)
        self.addCleanup(self.state.close)
        self.state.ensure_collection_uuid("collection-a")
        image_a = root / "images-a"
        image_b = root / "images-b"
        image_a.mkdir()
        image_b.mkdir()
        self.state.record_root("root-a", str(image_a), True)
        self.state.record_root("root-b", str(image_b), True)
        self.state.set_many(
            [
                _entry("doc-a", "root-a", "原神/雷电将军/a.jpg"),
                _entry("doc-b", "root-b", "原神/雷电将军/b.jpg"),
                _entry("doc-c", "root-a", "原神/雷电将军/子目录/c.jpg"),
            ]
        )

    def test_same_named_folders_are_isolated_by_root_and_collection(self) -> None:
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            listing = browser.list_folders(query="雷电将军")
            self.assertEqual(listing["total"], 3)
            exact = [
                folder
                for folder in listing["folders"]
                if folder["relative_folder"] == "原神/雷电将军"
            ]
            self.assertEqual(
                {folder["root_id"] for folder in exact}, {"root-a", "root-b"}
            )
            root_a = next(folder for folder in exact if folder["root_id"] == "root-a")
            page = browser.folder_images(root_a["folder_key"])
            self.assertEqual([item["doc_id"] for item in page["items"]], ["doc-a"])
            recursive = browser.folder_images(
                root_a["folder_key"], include_subfolders=True
            )
            self.assertEqual(
                {item["doc_id"] for item in recursive["items"]},
                {"doc-a", "doc-c"},
            )
            self.assertNotIn("current_path", root_a)
            self.assertNotIn("source_path", page["items"][0])

            foreign_key = root_a["folder_key"]
        with (
            LibraryBrowser(library_id="library-b", state_path=self.state_path) as other,
            self.assertRaisesRegex(ValueError, "different library"),
        ):
            other.folder_images(foreign_key)

    def test_query_only_reader_remains_available_during_a_write_transaction(
        self,
    ) -> None:
        self.state.connection.execute("BEGIN IMMEDIATE")
        try:
            self.state.connection.execute(
                "UPDATE entries SET tags_json = '[\"未提交\"]' WHERE doc_id = 'doc-a'"
            )
            with LibraryBrowser(
                library_id="library-a", state_path=self.state_path
            ) as browser:
                listing = browser.list_folders(limit=10)
                self.assertEqual(listing["total"], 3)
                roots = {item["root_id"]: item for item in listing["roots"]}
                self.assertEqual(roots["root-a"]["folder_count"], 2)
                self.assertEqual(roots["root-a"]["image_count"], 2)
                self.assertTrue(
                    roots["root-a"]["folder_key"].startswith("zvec-folder-v1.")
                )
        finally:
            self.state.connection.rollback()

    def test_folder_image_payload_keeps_tag_sources_separate(self) -> None:
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            folder = next(
                value
                for value in browser.list_folders()["folders"]
                if value["root_id"] == "root-a"
                and value["relative_folder"] == "原神/雷电将军"
            )
            item = browser.folder_images(folder["folder_key"])["items"][0]
        self.assertEqual(item["manual_tags"], ["人工"])
        self.assertEqual(item["folder_tags"], ["文件夹"])
        self.assertEqual(item["model_tags"], ["模型"])
        self.assertEqual(item["inherited_tags"], ["继承"])
        sources = {value["tag"]: value["sources"] for value in item["tag_sources"]}
        self.assertEqual(sources["继承"], ["inherited"])

    def test_only_direct_non_empty_folders_are_actionable(self) -> None:
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            folders = browser.list_folders(root_id="root-a")["folders"]
        relative = {str(value["relative_folder"]): value for value in folders}
        self.assertNotIn("", relative)
        self.assertNotIn("原神", relative)
        self.assertEqual(relative["原神/雷电将军"]["image_count"], 1)
        self.assertEqual(relative["原神/雷电将军"]["direct_image_count"], 1)
        self.assertEqual(relative["原神/雷电将军"]["descendant_image_count"], 2)

    def test_empty_registered_root_remains_browsable_after_last_image_delete(
        self,
    ) -> None:
        self.state.remove_many_for_folder_delete(["doc-a", "doc-c"])
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            listing = browser.list_folders(root_id="root-a")
            root = next(
                item for item in listing["roots"] if item["root_id"] == "root-a"
            )
            empty_page = browser.folder_images(
                root["folder_key"], include_subfolders=True
            )

        self.assertEqual(listing["folders"], [])
        self.assertEqual(listing["total"], 0)
        self.assertTrue(root["is_empty"])
        self.assertEqual(root["image_count"], 0)
        self.assertEqual(root["folder_count"], 0)
        self.assertEqual(root["first_indexed_at"], "")
        self.assertEqual(root["last_indexed_at"], "")
        self.assertEqual(empty_page["total"], 0)
        self.assertEqual(empty_page["items"], [])


class FolderIndexMigrationTest(unittest.TestCase):
    def test_existing_schema_v2_is_backfilled_without_reindexing_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.sqlite3"
            images = root / "images"
            images.mkdir()
            connection = sqlite3.connect(state_path)
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES('state_schema_version', '2');
                INSERT INTO metadata VALUES('collection_uuid', 'collection-old');
                CREATE TABLE entries (
                    doc_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL, file_name TEXT NOT NULL,
                    extension TEXT NOT NULL, mime_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                    folder_tags_json TEXT NOT NULL DEFAULT '[]',
                    accepted_auto_tags_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE roots (
                    root_id TEXT PRIMARY KEY, current_path TEXT NOT NULL UNIQUE,
                    recursive INTEGER NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            connection.execute(
                "INSERT INTO roots VALUES('root-old', ?, 1, '[]')", (str(images),)
            )
            connection.execute(
                "INSERT INTO entries VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "doc-old",
                    "root-old",
                    "作品/角色/a.jpg",
                    "a.jpg",
                    "jpg",
                    "image/jpeg",
                    "a" * 64,
                    1,
                    2,
                    3,
                    4,
                    '["人工"]',
                    '["文件夹"]',
                    '["模型"]',
                ),
            )
            connection.commit()
            connection.close()

            state = IndexState(state_path)
            try:
                entry = state.get("doc-old")
                assert entry is not None
                self.assertEqual(entry["parent_directory"], "作品/角色")
                self.assertEqual(entry["inherited_tags"], [])
                _total, folders = state.list_folders(root_id="root-old")
                self.assertEqual(
                    [folder["relative_folder"] for folder in folders],
                    ["作品/角色"],
                )
                self.assertEqual(folders[0]["timestamp_source"], "migration")
                self.assertTrue(folders[0]["first_indexed_at"].endswith("Z"))
                self.assertEqual(
                    folders[0]["last_indexed_at"],
                    folders[0]["first_indexed_at"],
                )
            finally:
                state.close()

    def test_folder_catalog_recovers_index_run_time_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.sqlite3"
            images = root / "images"
            images.mkdir()
            state = IndexState(state_path)
            try:
                state.ensure_collection_uuid("collection-history")
                state.record_root("root-history", str(images), True)
                run_id = state.begin_index_run("root-history", str(images))
                state.set_many(
                    [_entry("doc-history", "root-history", "series/character/a.jpg")]
                )
                state.record_index_run_entries(run_id, ["doc-history"], "inserted")
                state.finish_index_run(
                    run_id,
                    status="succeeded",
                    inserted=1,
                    updated=0,
                    failed=0,
                )
                update_run_id = state.begin_index_run("root-history", str(images))
                state.record_index_run_entries(
                    update_run_id, ["doc-history"], "updated"
                )
                state.finish_index_run(
                    update_run_id,
                    status="succeeded",
                    inserted=0,
                    updated=1,
                    failed=0,
                )
            finally:
                state.close()

            connection = sqlite3.connect(state_path)
            connection.execute(
                "UPDATE index_runs SET started_at = '2025-01-02 03:04:05' "
                "WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "UPDATE index_runs SET started_at = '2025-02-03 04:05:06' "
                "WHERE run_id = ?",
                (update_run_id,),
            )
            connection.execute("DROP TABLE folder_catalog")
            connection.execute("DELETE FROM metadata WHERE key = 'folder_catalog_v1'")
            connection.execute("DELETE FROM metadata WHERE key = 'folder_catalog_v2'")
            connection.commit()
            connection.close()

            migrated = IndexState(state_path)
            try:
                _total, folders = migrated.list_folders(root_id="root-history")
                self.assertEqual(len(folders), 1)
                self.assertEqual(
                    folders[0]["first_indexed_at"], "2025-01-02T03:04:05.000Z"
                )
                self.assertEqual(
                    folders[0]["last_indexed_at"], "2025-02-03T04:05:06.000Z"
                )
                self.assertEqual(folders[0]["timestamp_source"], "index_run")
                first = migrated.connection.execute(
                    "SELECT first_indexed_at, last_indexed_at FROM folder_catalog"
                ).fetchone()
            finally:
                migrated.close()

            reopened = IndexState(state_path)
            try:
                second = reopened.connection.execute(
                    "SELECT first_indexed_at, last_indexed_at FROM folder_catalog"
                ).fetchone()
                self.assertEqual(tuple(second), tuple(first))
                self.assertEqual(reopened.get_metadata("folder_catalog_v1"), "1")
                self.assertEqual(reopened.get_metadata("folder_catalog_v2"), "1")
            finally:
                reopened.close()

    def test_existing_folder_catalog_v1_adds_last_indexed_without_reindexing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.sqlite3"
            images = root / "images"
            images.mkdir()
            state = IndexState(state_path)
            try:
                state.ensure_collection_uuid("collection-folder-v1")
                state.record_root("root-v1", str(images), True)
                run_id = state.begin_index_run("root-v1", str(images))
                with state.connection:
                    state.connection.execute(
                        "UPDATE index_runs SET started_at = '2024-03-04 05:06:07' "
                        "WHERE run_id = ?",
                        (run_id,),
                    )
                state.set_many([_entry("doc-v1", "root-v1", "work/role/a.jpg")])
                state.record_index_run_entries(run_id, ["doc-v1"], "inserted")
                state.finish_index_run(
                    run_id,
                    status="succeeded",
                    inserted=1,
                    updated=0,
                    failed=0,
                )
            finally:
                state.close()

            connection = sqlite3.connect(state_path)
            connection.executescript(
                "ALTER TABLE folder_catalog RENAME TO folder_catalog_v2_source;"
                "CREATE TABLE folder_catalog("
                "root_id TEXT NOT NULL, relative_folder TEXT NOT NULL, "
                "first_indexed_at TEXT NOT NULL, timestamp_source TEXT NOT NULL, "
                "PRIMARY KEY(root_id, relative_folder));"
                "INSERT INTO folder_catalog("
                "root_id, relative_folder, first_indexed_at, timestamp_source) "
                "SELECT root_id, relative_folder, first_indexed_at, "
                "timestamp_source FROM folder_catalog_v2_source;"
                "DROP TABLE folder_catalog_v2_source;"
                "DELETE FROM metadata WHERE key = 'folder_catalog_v2';"
            )
            connection.commit()
            connection.close()

            migrated = IndexState(state_path)
            try:
                _total, folders = migrated.list_folders(root_id="root-v1")
                self.assertEqual(len(folders), 1)
                self.assertEqual(
                    folders[0]["last_indexed_at"], "2024-03-04T05:06:07.000Z"
                )
                self.assertEqual(migrated.get_metadata("folder_catalog_v2"), "1")
                columns = {
                    str(row[1])
                    for row in migrated.connection.execute(
                        "PRAGMA table_info(folder_catalog)"
                    )
                }
                self.assertIn("last_indexed_at", columns)
            finally:
                migrated.close()


class FolderCatalogLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        images = root / "images"
        images.mkdir()
        self.images = images
        self.state_path = root / "state.sqlite3"
        self.state = IndexState(self.state_path)
        self.addCleanup(self.state.close)
        self.state.ensure_collection_uuid("collection-folders")
        self.state.record_root("root-a", str(images), True)

    def test_time_order_and_pages_over_two_hundred_are_stable(self) -> None:
        self.state.set_many(
            [
                _entry(
                    f"doc-{index:03d}",
                    "root-a",
                    f"folder-{index:03d}/image.jpg",
                )
                for index in range(205)
            ]
        )

        total, first_page = self.state.list_folders(offset=0, limit=200)
        second_total, second_page = self.state.list_folders(offset=200, limit=200)
        paths = [item["relative_folder"] for item in [*first_page, *second_page]]
        self.assertEqual(total, 205)
        self.assertEqual(second_total, 205)
        self.assertEqual(len(paths), 205)
        self.assertEqual(len(set(paths)), 205)
        self.assertEqual(paths, sorted(paths, key=str.casefold))

        with self.state.connection:
            self.state.connection.execute(
                "UPDATE folder_catalog SET first_indexed_at = "
                "'2099-01-01T00:00:00.000Z' WHERE root_id = 'root-a' "
                "AND relative_folder = 'folder-204'"
            )
        _total, reordered = self.state.list_folders(limit=1)
        self.assertEqual(reordered[0]["relative_folder"], "folder-204")
        roots = self.state.list_folder_roots()
        self.assertEqual(roots[0]["folder_count"], 205)
        self.assertEqual(roots[0]["image_count"], 205)

    def test_folder_delete_prunes_catalog_and_invalidates_undo_history(self) -> None:
        self.state.set_many(
            [
                _entry("doc-a", "root-a", "same-folder/a.jpg"),
                _entry("doc-b", "root-a", "same-folder/b.jpg"),
            ]
        )
        self.state.create_manual_tag_batch(
            batch_id="manual-batch",
            operation="add",
            selection={"mode": "selected"},
            tags=["tag"],
            total_count=2,
        )
        self.state.record_manual_tag_batch_entries(
            "manual-batch",
            [
                ("doc-a", [], ["tag"]),
                ("doc-b", [], ["tag"]),
            ],
        )
        self.state.update_manual_tag_batch_entries(
            "manual-batch", [("doc-b", "applied", "")]
        )
        self.state.finish_manual_tag_batch(
            "manual-batch",
            status="applied",
            processed=2,
            updated=2,
            unchanged=0,
            failed=0,
        )
        snapshots = [{"doc_id": "doc-a", "entry": {}, "annotation": {}}]
        self.state.create_auto_tag_review_batch(
            batch_id="review-batch", snapshots=snapshots
        )
        self.state.update_auto_tag_review_batch(
            "review-batch",
            status="applied",
            result={
                "undo_available": True,
                "after_snapshots": [{"doc_id": "doc-a"}],
            },
        )

        self.assertEqual(
            self.state.remove_many_for_folder_delete(["doc-a", "doc-a", "missing-doc"]),
            1,
        )
        self.assertEqual(
            self.state.manual_tag_batch_entries(
                "manual-batch", statuses=("source_deleted",)
            )[0]["doc_id"],
            "doc-a",
        )
        latest = self.state.latest_auto_tag_review_batch()
        assert latest is not None
        self.assertEqual(latest["status"], "invalidated")
        self.assertEqual(latest["snapshots"], snapshots)
        self.assertFalse(latest["result"]["undo_available"])
        self.assertEqual(latest["result"]["invalidated_reason"], "source_deleted")
        self.assertTrue(self.state.manual_tag_undo_available())
        _total, remaining = self.state.list_folders()
        self.assertEqual(remaining[0]["image_count"], 1)

        self.assertEqual(self.state.remove_many_for_folder_delete(["doc-b"]), 1)
        self.assertFalse(self.state.manual_tag_undo_available())
        self.assertEqual(self.state.list_folders(), (0, []))
        self.assertEqual(
            self.state.connection.execute(
                "SELECT COUNT(*) FROM folder_catalog"
            ).fetchone()[0],
            0,
        )
        roots = self.state.list_folder_roots()
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0]["is_empty"])
        self.assertEqual(roots[0]["image_count"], 0)
        self.assertTrue(self.images.is_dir())

    def test_first_and_last_indexed_times_survive_updates_and_restart(self) -> None:
        run_id = self.state.begin_index_run("root-a", str(self.images))
        entry = _entry("doc-time", "root-a", "timeline/image.jpg")
        self.state.set_many([entry])
        self.state.record_index_run_entries(run_id, ["doc-time"], "inserted")
        self.state.finish_index_run(
            run_id,
            status="succeeded",
            inserted=1,
            updated=0,
            failed=0,
        )
        _total, initial = self.state.list_folders(root_id="root-a")
        first_indexed = initial[0]["first_indexed_at"]
        initial_last = initial[0]["last_indexed_at"]

        self.state.set_many([{**entry, "tags": ["manual-only-change"]}])
        _total, after_manual = self.state.list_folders(root_id="root-a")
        self.assertEqual(after_manual[0]["first_indexed_at"], first_indexed)
        self.assertEqual(after_manual[0]["last_indexed_at"], initial_last)

        update_run = self.state.begin_index_run("root-a", str(self.images))
        with self.state.connection:
            self.state.connection.execute(
                "UPDATE index_runs SET started_at = '2030-04-05 06:07:08' "
                "WHERE run_id = ?",
                (update_run,),
            )
        self.state.record_index_run_entries(update_run, ["doc-time"], "updated")
        self.state.finish_index_run(
            update_run,
            status="succeeded",
            inserted=0,
            updated=1,
            failed=0,
        )
        _total, updated = self.state.list_folders(root_id="root-a")
        self.assertEqual(updated[0]["first_indexed_at"], first_indexed)
        self.assertEqual(updated[0]["last_indexed_at"], "2030-04-05T06:07:08.000Z")

        self.state.close()
        reopened = IndexState(self.state_path)
        self.addCleanup(reopened.close)
        _total, persisted = reopened.list_folders(root_id="root-a")
        self.assertEqual(persisted[0]["first_indexed_at"], first_indexed)
        self.assertEqual(persisted[0]["last_indexed_at"], "2030-04-05T06:07:08.000Z")

    def test_folder_delete_rolls_back_history_when_entry_delete_fails(self) -> None:
        self.state.set_many([_entry("doc-a", "root-a", "folder/a.jpg")])
        self.state.create_manual_tag_batch(
            batch_id="manual-batch",
            operation="add",
            selection={"mode": "selected"},
            tags=["tag"],
            total_count=1,
        )
        self.state.record_manual_tag_batch_entries(
            "manual-batch", [("doc-a", [], ["tag"])]
        )
        self.state.update_manual_tag_batch_entries(
            "manual-batch", [("doc-a", "applied", "")]
        )
        with self.state.connection:
            self.state.connection.execute(
                "CREATE TRIGGER fail_folder_delete BEFORE DELETE ON entries "
                "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated delete failure"):
            self.state.remove_many_for_folder_delete(["doc-a"])

        self.assertIsNotNone(self.state.get("doc-a"))
        history = self.state.manual_tag_batch_entries(
            "manual-batch", statuses=("applied",)
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(
            self.state.connection.execute(
                "SELECT COUNT(*) FROM folder_catalog"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
