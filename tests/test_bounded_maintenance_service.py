from __future__ import annotations

import math
import unittest
from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any

from image_vector_service.metadata_text import build_metadata_text
from image_vector_service.service import ImageVectorService


def _entry(doc_id: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": f"album/{doc_id}.jpg",
        "tags": list(tags if tags is not None else ["portrait"]),
        "folder_tags": [],
        "accepted_auto_tags": [],
    }


CURRENT_HASH = build_metadata_text(_entry("template"), None).sha256


class _Logger:
    def info(self, _message: str, *_args: object) -> None:
        pass


class _EmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_text(self, text: str) -> SimpleNamespace:
        self.calls.append(text)
        return SimpleNamespace(
            vectors=[[0.1, 0.2, 0.3]],
            request_id="",
            usage={},
        )


def _service(
    state: object,
    repository: object,
    *,
    client: _EmbeddingClient | None = None,
) -> ImageVectorService:
    service = object.__new__(ImageVectorService)
    service.config = SimpleNamespace(
        dimension=3,
        embedding_concurrency=2,
        rate_limit_requests_per_minute=60,
        rate_limit_tokens_per_minute=100_000,
        rate_limit_hard_requests_per_minute=60,
        rate_limit_hard_tokens_per_minute=100_000,
    )
    service.state = state
    service.repository = repository
    service._embedding_client = client or _EmbeddingClient()
    service.progress = lambda _message: None
    service.cancel_check = lambda: None
    service.logger = _Logger()
    return service


class _ScaleState:
    def __init__(self, total: int) -> None:
        self.total = total
        self.iterator_calls = 0
        self.chunk_sizes: list[int] = []

    def count(self) -> int:
        return self.total

    def iter_entries_with_annotations(
        self,
        *,
        chunk_size: int,
    ) -> Iterator[list[tuple[dict[str, Any], None]]]:
        self.iterator_calls += 1
        self.chunk_sizes.append(chunk_size)
        for offset in range(0, self.total, chunk_size):
            yield [
                (_entry(f"doc-{index:06d}"), None)
                for index in range(offset, min(self.total, offset + chunk_size))
            ]

    def list_entries(self) -> list[dict[str, Any]]:
        raise AssertionError("metadata backfill must not load all entries")

    def get_document_annotation(self, _doc_id: str) -> dict[str, Any] | None:
        raise AssertionError("discovery must not fetch annotations one at a time")


class _ScaleRepository:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.requested_batch_sizes: list[int] = []

    def fetch_metadata_many(
        self,
        doc_ids: Iterable[str],
        *,
        batch_size: int,
    ) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        ids = list(doc_ids)
        self.batch_sizes.append(len(ids))
        self.requested_batch_sizes.append(batch_size)
        return (
            {
                doc_id: {
                    "metadata_text": "current",
                    "metadata_text_hash": CURRENT_HASH,
                    "metadata_embedding": [],
                }
                for doc_id in ids
            },
            {},
        )

    def fetch_metadata(self, _doc_id: str) -> dict[str, object] | None:
        raise AssertionError("metadata backfill must not fetch one document at a time")

    def upsert_metadata_embedding(
        self,
        _doc_id: str,
        _metadata_text: str,
        _metadata_text_hash: str,
        _vector: list[float],
    ) -> None:
        raise AssertionError("current metadata must not be rewritten")


class _FailureState:
    def __init__(self) -> None:
        self.entries = [
            _entry("pending"),
            _entry("current"),
            _entry("empty", tags=[]),
            *[_entry(f"corrupt-{index:03d}") for index in range(105)],
            _entry("missing"),
        ]
        self.by_id = {str(entry["doc_id"]): entry for entry in self.entries}
        self.annotation_get_calls = 0

    def count(self) -> int:
        return len(self.entries)

    def iter_entries_with_annotations(
        self,
        *,
        chunk_size: int,
    ) -> Iterator[list[tuple[dict[str, Any], None]]]:
        for offset in range(0, len(self.entries), chunk_size):
            yield [
                (entry, None) for entry in self.entries[offset : offset + chunk_size]
            ]

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self.by_id.get(doc_id)

    def get_document_annotation(self, _doc_id: str) -> dict[str, Any] | None:
        self.annotation_get_calls += 1
        return None

    def list_entries(self) -> list[dict[str, Any]]:
        raise AssertionError("metadata backfill must not load all entries")


class _FailureRepository:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.upserts: list[str] = []

    def fetch_metadata_many(
        self,
        doc_ids: Iterable[str],
        *,
        batch_size: int,
    ) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        ids = list(doc_ids)
        self.calls.append(ids)
        if batch_size != 256:
            raise AssertionError("metadata reads must remain bounded to 256")
        return (
            {
                "pending": {"metadata_text_hash": ""},
                "current": {"metadata_text_hash": CURRENT_HASH},
            },
            {
                doc_id: f"corrupt Collection document {doc_id}"
                for doc_id in ids
                if doc_id.startswith("corrupt-")
            },
        )

    def fetch_metadata(self, _doc_id: str) -> dict[str, object] | None:
        raise AssertionError("metadata backfill must not use N+1 Collection reads")

    def upsert_metadata_embedding(
        self,
        doc_id: str,
        _metadata_text: str,
        _metadata_text_hash: str,
        _vector: list[float],
    ) -> None:
        self.upserts.append(doc_id)


class BoundedMetadataBackfillServiceTest(unittest.TestCase):
    def test_100k_current_documents_use_only_bounded_page_reads(self) -> None:
        total = 100_000
        state = _ScaleState(total)
        repository = _ScaleRepository()
        service = _service(state, repository)

        report = service.backfill_metadata_embeddings(max_images=10_000)

        self.assertEqual(report["scanned"], total)
        self.assertEqual(report["already_current"], total)
        self.assertEqual(report["selected"], 0)
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(state.iterator_calls, 1)
        self.assertEqual(state.chunk_sizes, [256])
        self.assertEqual(len(repository.batch_sizes), math.ceil(total / 256))
        self.assertTrue(all(size <= 256 for size in repository.batch_sizes))
        self.assertEqual(set(repository.requested_batch_sizes), {256})

    def test_batch_failures_do_not_block_good_items_and_preview_is_bounded(
        self,
    ) -> None:
        state = _FailureState()
        repository = _FailureRepository()
        client = _EmbeddingClient()
        service = _service(state, repository, client=client)

        report = service.backfill_metadata_embeddings(max_images=10)

        self.assertEqual(report["scanned"], 109)
        self.assertEqual(report["skipped_empty"], 1)
        self.assertEqual(report["already_current"], 1)
        self.assertEqual(report["selected"], 1)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["eligible"], 107)
        self.assertEqual(report["failed"], 106)
        self.assertEqual(report["remaining"], 106)
        self.assertEqual(report["api_requests"], 1)
        self.assertEqual(len(report["failures"]), 100)
        self.assertEqual(repository.upserts, ["pending"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(state.annotation_get_calls, 1)
        self.assertEqual(len(repository.calls), 1)
        self.assertEqual(len(repository.calls[0]), 108)


class _RebindState:
    def __init__(self, total: int) -> None:
        self.total = total
        self.chunk_sizes: list[int] = []

    def rebind_root(self, root_id: str, new_path: str) -> dict[str, object]:
        return {
            "root_id": root_id,
            "current_path": new_path,
        }

    def iter_entries_for_root(
        self,
        root_id: str,
        *,
        chunk_size: int,
    ) -> Iterator[list[dict[str, Any]]]:
        self.chunk_sizes.append(chunk_size)
        for offset in range(0, self.total, chunk_size):
            yield [
                {
                    "doc_id": f"doc-{index}",
                    "root_id": root_id,
                    "relative_path": (
                        f"missing/{index}.jpg"
                        if index % 10 == 0
                        else f"present/{index}.jpg"
                    ),
                }
                for index in range(offset, min(self.total, offset + chunk_size))
            ]

    def entries_for_root(self, _root_id: str) -> list[dict[str, Any]]:
        raise AssertionError("rebind validation must not load the root into memory")


class _ResolvedSource:
    def __init__(self, exists: bool) -> None:
        self.exists = exists

    def is_file(self) -> bool:
        return self.exists


class _SourceResolver:
    def resolve_fields(self, entry: dict[str, Any]) -> _ResolvedSource:
        return _ResolvedSource(
            not str(entry.get("relative_path") or "").startswith("missing/")
        )


class BoundedRebindServiceTest(unittest.TestCase):
    def test_missing_file_validation_uses_bounded_root_pages(self) -> None:
        state = _RebindState(1_025)
        service = object.__new__(ImageVectorService)
        service.state = state
        service.source_resolver = _SourceResolver()
        service.cancel_check = lambda: None
        service.logger = _Logger()

        report = service.rebind_root("root-a", "D:/moved-library")

        self.assertEqual(report["missing_files"], 103)
        self.assertEqual(state.chunk_sizes, [256])


if __name__ == "__main__":
    unittest.main()
