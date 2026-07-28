from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from image_vector_service.collection_write_coordinator import CollectionWriteResult
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


if __name__ == "__main__":
    unittest.main()
