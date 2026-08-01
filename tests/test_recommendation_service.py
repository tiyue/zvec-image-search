from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from image_vector_service.service import ImageVectorService


def _entry(index: int) -> dict[str, Any]:
    doc_id = f"{index:064x}"
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": f"album/{index}.jpg",
        "parent_directory": "album",
        "file_name": f"{index}.jpg",
        "mime_type": "image/jpeg",
        "sha256": f"{index + 1:064x}",
        "size_bytes": 1_024,
        "mtime_ns": index,
        "width": 2_000,
        "height": 1_200,
        "tags": ["人工"],
        "folder_tags": ["文件夹"],
        "accepted_auto_tags": ["刻晴"],
        "inherited_tags": ["继承"],
    }


class _State:
    def __init__(self, values: list[tuple[dict[str, Any], dict[str, Any] | None]]):
        self.values = values
        self.calls: list[tuple[str, int]] = []

    def sample_recommendation_entries(
        self, *, random_cursor: str, limit_per_pool: int
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        self.calls.append((random_cursor, limit_per_pool))
        return self.values


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def fetch_vectors(
        self, doc_ids: list[str]
    ) -> tuple[dict[str, list[float]], dict[str, str]]:
        values = tuple(doc_ids)
        self.calls.append(values)
        return {doc_id: [1.0, 0.0] for doc_id in values}, {}


class RecommendationServiceTests(unittest.TestCase):
    def _service(
        self, values: list[tuple[dict[str, Any], dict[str, Any] | None]]
    ) -> tuple[ImageVectorService, _State, _Repository]:
        service = object.__new__(ImageVectorService)
        state = _State(values)
        repository = _Repository()
        service.state = state  # type: ignore[assignment]
        service.repository = repository  # type: ignore[assignment]
        service.config = SimpleNamespace(
            model="embedding-v1", dimension=2, metric="COSINE"
        )
        return service, state, repository

    def test_collection_batches_vector_reads_and_keeps_vectors_inside_worker_result(
        self,
    ) -> None:
        values = [(_entry(index), None) for index in range(257)]
        service, state, repository = self._service(values)

        result = service.collect_recommendation_candidates(
            random_cursor="a" * 64,
            limit_per_pool=100,
        )

        self.assertEqual(state.calls, [("a" * 64, 100)])
        self.assertEqual([len(call) for call in repository.calls], [256, 1])
        self.assertEqual(
            result["vector_space"],
            {"model": "embedding-v1", "dimension": 2, "metric": "COSINE"},
        )
        self.assertEqual(len(result["candidates"]), 257)
        self.assertEqual(result["candidates"][0]["vector"], (1.0, 0.0))

    def test_character_requires_current_accepted_confirmed_structured_identity(
        self,
    ) -> None:
        entry = _entry(1)
        annotation = {
            "status": "accepted",
            "structured": {
                "entities": {
                    "character": [
                        {"name": "甘雨", "state": "confirmed"},
                        {"name": "刻晴", "state": "confirmed"},
                        {"name": "胡桃", "state": "conflict"},
                    ]
                }
            },
        }
        service, _state, _repository = self._service([(entry, annotation)])

        result = service.collect_recommendation_candidates(
            random_cursor="b" * 64,
            limit_per_pool=1,
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["character"], "刻晴")
        self.assertEqual(candidate["album_id"], "root-a\0album")
        self.assertEqual(candidate["tags"], ["人工", "文件夹", "刻晴", "继承"])


if __name__ == "__main__":
    unittest.main()
