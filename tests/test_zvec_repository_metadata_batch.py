from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from image_vector_service.zvec_repository import ZvecImageRepository


def _document(
    text: str,
    text_hash: str,
    vector: list[float] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        fields={
            "metadata_text": text,
            "metadata_text_hash": text_hash,
        },
        vectors={"metadata_embedding": vector or [0.1, 0.2, 0.3]},
    )


class _Collection:
    def __init__(
        self,
        documents: dict[str, Any],
        *,
        fail_batches: bool = False,
        failed_ids: set[str] | None = None,
    ) -> None:
        self.documents = documents
        self.fail_batches = fail_batches
        self.failed_ids = set(failed_ids or ())
        self.calls: list[str | list[str]] = []
        self.include_vector_calls: list[bool] = []

    def fetch(
        self,
        doc_ids: str | list[str],
        *,
        output_fields: list[str],
        include_vector: bool,
    ) -> dict[str, Any]:
        self.calls.append(doc_ids)
        self.include_vector_calls.append(include_vector)
        if isinstance(doc_ids, list):
            if self.fail_batches:
                raise RuntimeError("injected batch failure")
            ids = doc_ids
        else:
            if doc_ids in self.failed_ids:
                raise RuntimeError(f"corrupt document {doc_ids}")
            ids = [doc_ids]
        return {
            doc_id: self.documents[doc_id] for doc_id in ids if doc_id in self.documents
        }


class ZvecRepositoryMetadataBatchTest(unittest.TestCase):
    @staticmethod
    def _repository(collection: _Collection) -> ZvecImageRepository:
        repository = object.__new__(ZvecImageRepository)
        repository.config = SimpleNamespace(dimension=3)
        repository.collection = collection
        return repository

    def test_healthy_documents_use_bounded_batch_fetches(self) -> None:
        collection = _Collection(
            {
                f"doc-{index}": _document(
                    f"text-{index}",
                    f"{index:064x}",
                )
                for index in range(1_100)
            }
        )
        repository = self._repository(collection)

        metadata, failures = repository.fetch_metadata_many(
            (f"doc-{index}" for index in range(1_100)),
            batch_size=500,
        )

        self.assertEqual(len(metadata), 1_100)
        self.assertEqual(failures, {})
        self.assertEqual([len(call) for call in collection.calls], [500, 500, 100])
        self.assertEqual(collection.include_vector_calls, [False, False, False])
        self.assertEqual(metadata["doc-1099"]["metadata_text"], "text-1099")
        self.assertEqual(metadata["doc-1099"]["metadata_embedding"], [])

    def test_embedding_vector_is_opt_in(self) -> None:
        collection = _Collection({"doc-a": _document("a", "a" * 64)})
        repository = self._repository(collection)

        metadata, failures = repository.fetch_metadata_many(
            ["doc-a"],
            include_embedding=True,
        )

        self.assertEqual(failures, {})
        self.assertEqual(metadata["doc-a"]["metadata_embedding"], [0.1, 0.2, 0.3])
        self.assertEqual(collection.include_vector_calls, [True])

    def test_failed_batch_falls_back_to_item_level_isolation(self) -> None:
        collection = _Collection(
            {
                "doc-a": _document("a", "a" * 64),
                "doc-b": _document("b", "b" * 64),
                "doc-c": _document("c", "c" * 64),
            },
            fail_batches=True,
            failed_ids={"doc-b"},
        )
        repository = self._repository(collection)

        metadata, failures = repository.fetch_metadata_many(
            ["doc-a", "doc-b", "doc-c"],
            batch_size=500,
        )

        self.assertEqual(set(metadata), {"doc-a", "doc-c"})
        self.assertEqual(failures, {"doc-b": "corrupt document doc-b"})
        self.assertEqual(
            collection.calls,
            [["doc-a", "doc-b", "doc-c"], "doc-a", "doc-b", "doc-c"],
        )

    def test_missing_document_matches_single_fetch_semantics(self) -> None:
        collection = _Collection({"doc-a": _document("a", "a" * 64)})
        repository = self._repository(collection)

        metadata, failures = repository.fetch_metadata_many(
            ["doc-a", "missing", "doc-a"]
        )

        self.assertEqual(set(metadata), {"doc-a"})
        self.assertEqual(failures, {})
        self.assertEqual(collection.calls, [["doc-a", "missing"]])

    def test_batch_size_is_strictly_bounded(self) -> None:
        repository = self._repository(_Collection({}))
        for invalid in (True, 0, 1_001):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "batch_size"),
            ):
                repository.fetch_metadata_many([], batch_size=invalid)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
