from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from image_vector_service.cluster_operation_store import ClusterOperationStore
from image_vector_service.config import ServiceConfig
from image_vector_service.models import RankSource, SearchHit
from image_vector_service.service import ImageVectorService


def _entry(index: int, *, valid_sha: bool = True) -> dict[str, Any]:
    return {
        "doc_id": f"doc-{index:05d}",
        "sha256": f"{index + 1:064x}" if valid_sha else "invalid-digest",
        "relative_path": f"images/{index:05d}.jpg",
        "tags": [],
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


class _State:
    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        reported_count: int | None = None,
        forbid_full_read: bool = False,
    ) -> None:
        self.entries = [dict(entry) for entry in entries]
        self.reported_count = reported_count
        self.forbid_full_read = forbid_full_read
        self.full_read_calls = 0
        self.iter_page_calls = 0
        self.max_iter_page = 0
        self.joined_page_calls = 0
        self.max_joined_page = 0

    def count(self) -> int:
        return self.reported_count or len(self.entries)

    def list_entries(self) -> list[dict[str, Any]]:
        self.full_read_calls += 1
        if self.forbid_full_read:
            raise AssertionError("large clustering must not load the full state table")
        return [dict(entry) for entry in self.entries]

    def iter_entries(self, *, chunk_size: int = 256) -> Iterator[list[dict[str, Any]]]:
        for offset in range(0, len(self.entries), chunk_size):
            page = [dict(entry) for entry in self.entries[offset : offset + chunk_size]]
            self.iter_page_calls += 1
            self.max_iter_page = max(self.max_iter_page, len(page))
            yield page

    def iter_entries_with_annotations(
        self, *, chunk_size: int = 256
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
        for offset in range(0, len(self.entries), chunk_size):
            page = [
                (dict(entry), None)
                for entry in self.entries[offset : offset + chunk_size]
            ]
            self.joined_page_calls += 1
            self.max_joined_page = max(self.max_joined_page, len(page))
            yield page

    def get_document_annotations(
        self, doc_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        del doc_ids
        return {}

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        del doc_id
        return None


class _Repository:
    def __init__(self) -> None:
        self.fetch_calls = 0
        self.query_calls = 0

    def fetch_vector(self, doc_id: str) -> list[float] | None:
        del doc_id
        self.fetch_calls += 1
        raise AssertionError("exact-only clustering must not read embeddings")

    def query(
        self,
        vector: list[float],
        top_k: int,
        tags: Iterable[str] = (),
        tag_mode: str = "all",
        rank_source: RankSource = "text",
    ) -> list[SearchHit]:
        del vector, top_k, tags, tag_mode, rank_source
        self.query_calls += 1
        raise AssertionError("exact-only clustering must not query embeddings")


class _Resolver:
    def resolve_fields(self, fields: dict[str, Any]) -> Path:
        del fields
        return Path("unused")


def _service(root: Path, state: _State) -> ImageVectorService:
    service = ImageVectorService.__new__(ImageVectorService)
    service.config = ServiceConfig(
        workspace=root / "workspace",
        config_home=root / "config",
        library_id="large-library-test",
        dimension=2,
    )
    service.state = state
    service.repository = _Repository()
    service.source_resolver = _Resolver()
    service.cancel_check = lambda: None
    service.progress = lambda _message: None
    service.cluster_operations = ClusterOperationStore(
        service.config.state_path,
        recover_interrupted=False,
    )
    return service


class LargeClusterServiceRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_large_route_streams_state_and_reuses_active_snapshot(self) -> None:
        state = _State([_entry(index) for index in range(12)], forbid_full_read=True)
        service = _service(self.root, state)

        with patch(
            "image_vector_service.service.should_use_large_cluster_engine",
            return_value=True,
        ):
            first = service.cluster_images(
                scope="new_or_changed",
                cluster_types=("exact",),
            )
            second = service.cluster_images(
                scope="new_or_changed",
                cluster_types=("exact",),
            )

        self.assertEqual(first["cluster_engine"], "streaming_sqlite")
        self.assertEqual(first["snapshot_storage"], "sqlite")
        self.assertNotIn("snapshot", first)
        self.assertEqual(first["api_requests"], 0)
        self.assertEqual(first["embedding_api_requests"], 0)
        self.assertEqual(first["qwen_api_requests"], 0)
        self.assertFalse(first["embedding_recomputed"])
        self.assertLessEqual(int(first["max_materialized_batch"]), 512)
        self.assertEqual(second["new_or_changed_count"], 0)
        self.assertEqual(state.full_read_calls, 0)
        self.assertGreater(state.joined_page_calls, 0)
        self.assertLessEqual(state.max_joined_page, 256)

    def test_active_manual_rules_keep_large_library_on_compatibility_route(
        self,
    ) -> None:
        state = _State(
            [_entry(0), _entry(1)],
            reported_count=10_001,
        )
        service = _service(self.root, state)
        service._active_cluster_rules = lambda: [  # type: ignore[method-assign]
            {
                "rule_kind": "must_link",
                "left_doc_id": "doc-00000",
                "right_doc_id": "doc-00001",
                "active": True,
            }
        ]

        result = service.cluster_images(scope="all", cluster_types=("exact",))

        self.assertNotIn("cluster_engine", result)
        self.assertEqual(result["cluster_count"], 1)
        self.assertEqual(state.full_read_calls, 0)
        self.assertGreater(state.iter_page_calls, 0)
        self.assertLessEqual(state.max_iter_page, 256)
        self.assertEqual(state.joined_page_calls, 0)
        self.assertEqual(result["api_requests"], 0)

    def test_large_route_returns_only_bounded_failure_details(self) -> None:
        invalid_entries = [_entry(index, valid_sha=False) for index in range(250)]
        invalid_entries[0]["relative_path"] = r"C:\Users\secret\private.jpg"
        state = _State([*invalid_entries, _entry(251)], forbid_full_read=True)
        service = _service(self.root, state)

        with patch(
            "image_vector_service.service.should_use_large_cluster_engine",
            return_value=True,
        ):
            result = service.cluster_images(scope="all", cluster_types=("exact",))

        failures = result["failures"]
        self.assertIsInstance(failures, list)
        self.assertEqual(result["failure_count"], 250)
        self.assertEqual(len(failures), 200)
        self.assertTrue(result["failure_details_truncated"])
        self.assertNotIn("failures", result["preparation"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(r"C:\Users\secret", rendered)
        self.assertLess(len(rendered.encode("utf-8")), 160_000)
        self.assertNotIn("snapshot", result)


if __name__ == "__main__":
    unittest.main()
