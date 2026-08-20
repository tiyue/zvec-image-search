from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from image_vector_service.collection_write_coordinator import CollectionWriteResult
from image_vector_service.folder_name_tag_settings import FolderNameTagSettingsStore
from image_vector_service.service import ImageVectorService
from image_vector_service.state import IndexState


def _entry(
    doc_id: str,
    relative_path: str,
    folder_tags: list[str],
) -> dict[str, Any]:
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
        "tags": ["人工标签"],
        "folder_tags": folder_tags,
        "accepted_auto_tags": ["模型标签"],
        "inherited_tags": ["继承标签"],
    }


class _Repository:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.tags = {
            str(entry["doc_id"]): list(entry["effective_tags"]) for entry in entries
        }

    def fetch_vectors(self, doc_ids) -> tuple[dict[str, list[float]], dict[str, str]]:
        return (
            {
                str(doc_id): [0.0]
                for doc_id in dict.fromkeys(str(value) for value in doc_ids)
            },
            {},
        )


class _CollectionWrites:
    def __init__(self, state: IndexState, repository: _Repository) -> None:
        self.state = state
        self.repository = repository
        self.calls = 0

    def upsert(self, writes, *, operation_kind: str) -> CollectionWriteResult:
        self.assert_operation(operation_kind)
        prepared = list(writes)
        self.calls += 1
        self.state.set_many(dict(item.state_entry) for item in prepared)
        for item in prepared:
            self.repository.tags[item.record.doc_id] = list(item.effective_tags)
        return CollectionWriteResult(
            succeeded=[item.record.doc_id for item in prepared]
        )

    @staticmethod
    def assert_operation(operation_kind: str) -> None:
        if operation_kind != "folder_name_tag":
            raise AssertionError(f"Unexpected operation kind: {operation_kind}")


class FolderNameTaggingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state_path = root / "state.sqlite3"
        image_root = root / "图片数据库"
        image_root.mkdir()
        self.state = IndexState(self.state_path)
        self.addCleanup(self.state.close)
        self.state.ensure_collection_uuid("collection-a")
        self.state.record_root("root-a", str(image_root), True)
        self.state.set_many(
            [
                _entry(
                    "doc-a",
                    "原神/Raiden雷电将军 写真 [35P-417MB]_jpg/a.jpg",
                    ["旧文件夹"],
                ),
                _entry("doc-b", "原神/Vol.12/b.jpg", ["Vol.12"]),
                _entry(
                    "doc-c",
                    "原神/Raiden雷电将军 写真 [35P-417MB]_jpg/c.jpg",
                    ["旧文件夹"],
                ),
            ]
        )
        self.repository = _Repository(self.state.list_entries())
        self.collection_writes = _CollectionWrites(self.state, self.repository)
        self.progress: list[str] = []
        self.service = ImageVectorService.__new__(ImageVectorService)
        self.service.config = SimpleNamespace(state_path=self.state_path)
        self.service.state = self.state
        self.service.repository = self.repository
        self.service.collection_writes = self.collection_writes
        self.service.cancel_check = lambda: None
        self.service.progress = self.progress.append
        self.service._refresh_tag_catalog = lambda: None

    def test_preview_apply_and_second_apply_are_model_free_and_idempotent(
        self,
    ) -> None:
        selection = {"mode": "library"}
        preview = self.service.estimate_folder_name_tags(
            library_id="library-a",
            selection=selection,
        )
        self.assertEqual(preview["selected"], 3)
        self.assertEqual(preview["changed"], 3)
        self.assertEqual(preview["changed_folders"], 2)
        self.assertEqual(preview["api_requests"], 0)
        self.assertEqual(self.collection_writes.calls, 0)

        applied = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection=selection,
        )
        self.assertEqual(applied["updated"], 3)
        self.assertEqual(applied["failed"], 0)
        self.assertEqual(applied["api_requests"], 0)
        self.assertEqual(
            self.state.get("doc-a")["folder_tags"],
            ["Raiden雷电将军", "雷电将军", "写真"],
        )
        self.assertEqual(self.state.get("doc-b")["folder_tags"], ["原神"])
        self.assertEqual(self.state.get("doc-a")["tags"], ["人工标签"])
        self.assertEqual(
            self.repository.tags["doc-a"],
            [
                "人工标签",
                "Raiden雷电将军",
                "雷电将军",
                "写真",
                "模型标签",
                "继承标签",
            ],
        )
        self.assertIn("Updated folder-name tags 3/3 images.", self.progress)

        repeated = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection=selection,
        )
        self.assertEqual(repeated["updated"], 0)
        self.assertEqual(repeated["unchanged"], 3)
        self.assertEqual(self.collection_writes.calls, 1)

    def test_preview_counts_interleaved_parent_folders_once(self) -> None:
        self.state.set_many(
            [
                _entry("doc-d", "原神/0.jpg", ["旧文件夹"]),
                _entry("doc-e", "原神/a/child.jpg", ["旧文件夹"]),
                _entry("doc-f", "原神/z.jpg", ["旧文件夹"]),
            ]
        )

        preview = self.service.estimate_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
        )

        self.assertEqual(preview["selected"], 6)
        self.assertEqual(preview["folders_scanned"], 4)
        self.assertEqual(preview["changed_folders"], 4)

    def test_normal_run_cleans_only_manual_and_inherited_sources(self) -> None:
        entry = self.state.get("doc-a")
        entry["tags"] = ["原神", "Foo", "foo", "VIP预览", "[35P-1GB]"]
        entry["accepted_auto_tags"] = ["VIP模型", "原神", "foo"]
        entry["inherited_tags"] = ["继承", "Bar", "bar", "自拍预览", "_jpg"]
        self.state.set_many([entry])

        preview = self.service.estimate_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
        )
        applied = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
            expected_rule_revision=preview["rule_revision"],
        )

        updated = self.state.get("doc-a")
        self.assertEqual(updated["tags"], ["原神", "Foo"])
        self.assertEqual(updated["inherited_tags"], ["继承", "Bar"])
        self.assertEqual(updated["accepted_auto_tags"], ["VIP模型", "原神", "foo"])
        self.assertEqual(applied["removed_blacklist"], 2)
        self.assertEqual(applied["removed_legacy"], 2)
        self.assertEqual(applied["removed_duplicates"], 2)
        self.assertEqual(self.repository.tags["doc-a"].count("原神"), 1)
        self.assertIn("Foo", updated["effective_tags"])
        self.assertNotIn("foo", updated["effective_tags"])
        self.assertIn("VIP模型", self.repository.tags["doc-a"])

    def test_non_forced_run_keeps_completed_folder_state(self) -> None:
        first = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
        )

        repeated = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
            force=False,
            expected_rule_revision=first["rule_revision"],
        )

        self.assertEqual(repeated["skipped"], 3)
        self.assertEqual(repeated["updated"], 0)
        self.assertEqual(
            self.state.folder_name_tag_folder_status(
                root_id="root-a",
                relative_folder="原神/Raiden雷电将军 写真 [35P-417MB]_jpg",
                rule_revision=first["rule_revision"],
            ),
            "succeeded",
        )

    def test_normal_index_uses_the_saved_blacklist(self) -> None:
        config_home = Path(self.temporary.name) / "config"
        FolderNameTagSettingsStore(config_home).save({"blacklist": ["雷电"]})
        self.service.config = SimpleNamespace(
            state_path=self.state_path,
            config_home_path=config_home,
        )

        tags = self.service._folder_tags_for_staged_path(
            "原神/雷电将军/a.jpg",
            Path(self.state.root_path("root-a")),
            {},
        )

        self.assertEqual(tags, ("原神",))

    def test_mark_all_records_state_without_writing_tags(self) -> None:
        preview = self.service.estimate_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
            mode="mark_all",
        )

        applied = self.service.apply_folder_name_tags(
            library_id="library-a",
            selection={"mode": "library"},
            mode="mark_all",
            expected_rule_revision=preview["rule_revision"],
        )

        self.assertEqual(applied["updated"], 0)
        self.assertEqual(self.collection_writes.calls, 0)
        self.assertEqual(
            self.state.folder_name_tag_folder_status(
                root_id="root-a",
                relative_folder="原神/Vol.12",
                rule_revision=preview["rule_revision"],
            ),
            "marked",
        )


if __name__ == "__main__":
    unittest.main()
