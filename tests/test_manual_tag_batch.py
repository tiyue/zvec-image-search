from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

from image_vector_service.collection_write_coordinator import CollectionWriteResult
from image_vector_service.config import ServiceConfig
from image_vector_service.library_browser import LibraryBrowser
from image_vector_service.service import ImageVectorService
from image_vector_service.state import IndexState
from tests.test_image_service import FakeEmbeddingClient


def _entry(doc_id: str, relative_path: str, manual: list[str]) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": relative_path,
        "file_name": Path(relative_path).name,
        "extension": "jpg",
        "mime_type": "image/jpeg",
        "sha256": doc_id[-1] * 64,
        "size_bytes": 10,
        "mtime_ns": 20,
        "width": 100,
        "height": 200,
        "tags": manual,
        "folder_tags": ["文件夹角色"],
        "accepted_auto_tags": ["模型作品"],
        "inherited_tags": ["继承身份"],
    }


class _Repository:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.tags = {
            str(entry["doc_id"]): list(entry["effective_tags"]) for entry in entries
        }
        self.fail_ids: set[str] = set()
        self.optimize_count = 0

    def update_record_tags(self, items):
        succeeded = []
        failures = {}
        for record, tags in items:
            if record.doc_id in self.fail_ids:
                failures[record.doc_id] = "simulated Collection failure"
                continue
            self.tags[record.doc_id] = list(tags)
            succeeded.append(record.doc_id)
        return succeeded, failures

    def fetch_vectors(self, doc_ids) -> tuple[dict[str, list[float]], dict[str, str]]:
        vectors: dict[str, list[float]] = {}
        failures: dict[str, str] = {}
        for doc_id in dict.fromkeys(str(value) for value in doc_ids):
            if doc_id not in self.tags:
                failures[doc_id] = "The indexed image vector is missing."
                continue
            vectors[doc_id] = [0.0]
        return vectors, failures

    def optimize(self) -> None:
        self.optimize_count += 1


class _CollectionWrites:
    """Small transactional writer used to exercise the service contract."""

    def __init__(self, state: IndexState, repository: _Repository) -> None:
        self.state = state
        self.repository = repository

    def upsert(self, writes, *, operation_kind: str) -> CollectionWriteResult:
        prepared = list(writes)
        if not prepared:
            return CollectionWriteResult()
        if operation_kind != "manual_tag":
            raise AssertionError(f"Unexpected operation kind: {operation_kind}")

        previous_tags = {
            item.record.doc_id: list(self.repository.tags[item.record.doc_id])
            for item in prepared
        }
        succeeded, failures = self.repository.update_record_tags(
            (item.record, item.effective_tags) for item in prepared
        )
        result = CollectionWriteResult(failures=dict(failures))
        succeeded_set = set(succeeded)
        committed = [item for item in prepared if item.record.doc_id in succeeded_set]
        if not committed:
            return result

        try:
            self.state.set_many(dict(item.state_entry) for item in committed)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            _restored, rollback_failures = self.repository.update_record_tags(
                (item.record, previous_tags[item.record.doc_id]) for item in committed
            )
            for item in committed:
                doc_id = item.record.doc_id
                rollback_error = rollback_failures.get(doc_id)
                result.failures[doc_id] = (
                    f"{message}; Collection rollback failed: {rollback_error}"
                    if rollback_error
                    else message
                )
                result.failure_kinds[doc_id] = "systemic"
            result.systemic_failure = True
            return result

        result.succeeded.extend(succeeded)
        return result


class ManualTagBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state_path = root / "state.sqlite3"
        image_root = root / "images"
        image_root.mkdir()
        self.state = IndexState(self.state_path)
        self.addCleanup(self.state.close)
        self.state.ensure_collection_uuid("collection-a")
        self.state.record_root("root-a", str(image_root), True)
        self.state.set_many(
            [
                _entry("doc-a", "原神/雷电将军/a.jpg", ["原标签A"]),
                _entry("doc-b", "原神/雷电将军/b.jpg", ["原标签B"]),
                _entry("doc-c", "原神/雷电将军/子目录/c.jpg", ["原标签C"]),
            ]
        )
        entries = self.state.list_entries()
        self.repository = _Repository(entries)
        self.progress: list[str] = []
        self.service = ImageVectorService.__new__(ImageVectorService)
        self.service.config = SimpleNamespace(state_path=self.state_path)
        self.service.state = self.state
        self.service.repository = self.repository
        self.service.collection_writes = _CollectionWrites(self.state, self.repository)
        self.service.cancel_check = lambda: None
        self.service.progress = self.progress.append
        self.service._refresh_tag_catalog = lambda: None

    def _folder_key(self) -> str:
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            folder = next(
                item
                for item in browser.list_folders()["folders"]
                if item["relative_folder"] == "原神/雷电将军"
            )
            return str(folder["folder_key"])

    def test_item_failure_does_not_stop_batch_and_undo_restores_both_stores(
        self,
    ) -> None:
        self.repository.fail_ids.add("doc-b")
        result = self.service.manual_tag_batch(
            library_id="library-a",
            selection={"mode": "selected", "doc_ids": ["doc-a", "doc-b"]},
            operation="add",
            tags=["新增标签", "原标签A"],
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["undo_available"])
        self.assertIn("新增标签", self.state.get("doc-a")["tags"])
        self.assertEqual(self.state.get("doc-b")["tags"], ["原标签B"])
        self.assertEqual(
            self.repository.tags["doc-a"],
            ["原标签A", "新增标签", "文件夹角色", "模型作品", "继承身份"],
        )
        self.assertIn("Updated manual tags 2/2 images.", self.progress)
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            self.assertTrue(browser.list_folders()["undo_available"])

        self.repository.fail_ids.clear()
        undone = self.service.undo_latest_manual_tag_batch()
        self.assertTrue(undone["undone"])
        self.assertEqual(undone["restored"], 1)
        self.assertEqual(self.state.get("doc-a")["tags"], ["原标签A"])
        self.assertEqual(
            self.repository.tags["doc-a"],
            ["原标签A", "文件夹角色", "模型作品", "继承身份"],
        )
        with LibraryBrowser(
            library_id="library-a", state_path=self.state_path
        ) as browser:
            self.assertFalse(browser.list_folders()["undo_available"])

    def test_folder_selection_stays_exact_by_default_and_honours_exclusions(
        self,
    ) -> None:
        result = self.service.manual_tag_batch(
            library_id="library-a",
            selection={
                "mode": "folder",
                "folder_key": self._folder_key(),
                "include_subfolders": False,
                "excluded_doc_ids": ["doc-b"],
            },
            operation="replace_manual",
            tags=["统一角色", "原神"],
        )
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.state.get("doc-a")["tags"], ["统一角色", "原神"])
        self.assertEqual(self.state.get("doc-b")["tags"], ["原标签B"])
        self.assertEqual(self.state.get("doc-c")["tags"], ["原标签C"])

    def test_remove_changes_only_manual_tags_and_keeps_other_sources(self) -> None:
        result = self.service.manual_tag_batch(
            library_id="library-a",
            selection={"mode": "selected", "doc_ids": ["doc-a"]},
            operation="remove",
            tags=["原标签A", "不存在"],
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.state.get("doc-a")["tags"], [])
        self.assertEqual(
            self.repository.tags["doc-a"],
            ["文件夹角色", "模型作品", "继承身份"],
        )

    def test_sqlite_failure_rolls_collection_back_and_is_reported(self) -> None:
        original_set_many = self.state.set_many
        calls = 0

        def fail_once(entries):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated SQLite write failure")
            return original_set_many(entries)

        self.state.set_many = fail_once  # type: ignore[method-assign]
        result = self.service.manual_tag_batch(
            library_id="library-a",
            selection={"mode": "selected", "doc_ids": ["doc-a"]},
            operation="add",
            tags=["不会残留"],
        )
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("不会残留", self.repository.tags["doc-a"])
        self.assertEqual(self.state.get("doc-a")["tags"], ["原标签A"])


class ManualTagRealCollectionIntegrationTest(unittest.TestCase):
    def test_batch_and_undo_update_real_zvec_tags_without_embedding_calls(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zvec_manual_tag_real_"))
        try:
            images = root / "images"
            images.mkdir()
            image = images / "a.png"
            Image.new("RGB", (16, 16), (100, 120, 140)).save(image)
            config = ServiceConfig(workspace=root / "workspace")
            client = FakeEmbeddingClient(config.dimension)
            service = ImageVectorService(config=config, embedding_client=client)
            try:
                indexed = service.index_folder(str(images))
                self.assertEqual(indexed.inserted, 1)
                entry = service.state.find_entry_for_path(image)
                assert entry is not None
                requests_before = client.request_count

                result = service.manual_tag_batch(
                    library_id="default",
                    selection={
                        "mode": "selected",
                        "doc_ids": [entry["doc_id"]],
                    },
                    operation="add",
                    tags=["手工新增"],
                )
                self.assertEqual(result["updated"], 1)
                stored = service.repository.collection.fetch(
                    str(entry["doc_id"]),
                    output_fields=["tags"],
                    include_vector=False,
                )[str(entry["doc_id"])]
                self.assertIn("手工新增", stored.fields["tags"])
                self.assertEqual(client.request_count, requests_before)

                undone = service.undo_latest_manual_tag_batch()
                self.assertTrue(undone["undone"])
                restored = service.repository.collection.fetch(
                    str(entry["doc_id"]),
                    output_fields=["tags"],
                    include_vector=False,
                )[str(entry["doc_id"])]
                self.assertNotIn("手工新增", restored.fields["tags"])
                self.assertEqual(client.request_count, requests_before)
            finally:
                service.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
