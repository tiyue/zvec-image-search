from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from image_vector_service.state import IndexState


def _entry(doc_id: str, *, perceptual_hash: str | None = None) -> dict[str, object]:
    item: dict[str, object] = {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": f"{doc_id}.jpg",
        "file_name": f"{doc_id}.jpg",
        "extension": "jpg",
        "mime_type": "image/jpeg",
        "sha256": f"{len(doc_id):064x}",
        "size_bytes": 10,
        "mtime_ns": 1,
        "width": 8,
        "height": 8,
        "tags": [],
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }
    if perceptual_hash is not None:
        item["perceptual_hash"] = perceptual_hash
    return item


class PerceptualHashStateTests(unittest.TestCase):
    def test_hash_is_preserved_and_backfilled_without_rewriting_tags(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = IndexState(Path(temporary.name) / "state.sqlite3")
        state.set_many([_entry("kept", perceptual_hash="abcdefabcdefabcd")])
        state.set_many([{**_entry("kept"), "tags": ["角色"]}])
        kept = state.get("kept")
        assert kept is not None
        self.assertEqual(kept["perceptual_hash"], "abcdefabcdefabcd")
        self.assertEqual(kept["tags"], ["角色"])

        state.set_many([_entry("fresh")])
        state.update_perceptual_hashes(
            [
                ("fresh", "0123456789abcdef"),
                ("kept", "ffffffffffffffff"),
            ]
        )
        fresh = state.get("fresh")
        kept = state.get("kept")
        assert fresh is not None and kept is not None
        self.assertEqual(fresh["perceptual_hash"], "0123456789abcdef")
        self.assertEqual(kept["perceptual_hash"], "abcdefabcdefabcd")
        state.close()
