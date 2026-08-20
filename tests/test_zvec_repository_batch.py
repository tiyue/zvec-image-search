from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from image_vector_service.models import ImageRecord
from image_vector_service.zvec_repository import ZvecImageRepository


class _Status:
    def __init__(self, ok: bool, message: str = "failed") -> None:
        self._ok = ok
        self._message = message

    def ok(self) -> bool:
        return self._ok

    def __str__(self) -> str:
        return self._message


class _CapturingCollection:
    def __init__(self, *, fail_bulk: bool = False) -> None:
        self.fail_bulk = fail_bulk
        self.calls: list[Any] = []

    def upsert(self, documents: Any) -> Any:
        self.calls.append(documents)
        if isinstance(documents, list):
            if self.fail_bulk:
                raise RuntimeError("bulk write failed")
            return [_Status(True) for _document in documents]
        return _Status(documents.id != "doc-b", "item rejected")


def _record(doc_id: str) -> ImageRecord:
    return ImageRecord(
        doc_id=doc_id,
        root_id="root-a",
        relative_path=f"{doc_id}.png",
        absolute_path=f"C:/images/{doc_id}.png",
        file_name=f"{doc_id}.png",
        extension="png",
        mime_type="image/png",
        sha256=("a" if doc_id == "doc-a" else "b") * 64,
        size_bytes=100,
        mtime_ns=1,
        width=10,
        height=10,
    )


class ZvecRepositoryBatchTests(unittest.TestCase):
    def _repository(self, collection: _CapturingCollection) -> ZvecImageRepository:
        repository = object.__new__(ZvecImageRepository)
        repository.config = SimpleNamespace(dimension=4, model="embedding-test")
        repository.collection = collection
        return repository

    def test_independent_vectors_and_tags_use_one_collection_upsert(self) -> None:
        collection = _CapturingCollection()
        repository = self._repository(collection)

        succeeded, failures = repository.upsert_record_vectors(
            [
                (_record("doc-a"), [1.0, 0.0, 0.0, 0.0], ["角色", "角色"]),
                (_record("doc-b"), [0.0, 1.0, 0.0, 0.0], ["作品"]),
            ]
        )

        self.assertEqual(succeeded, ["doc-a", "doc-b"])
        self.assertEqual(failures, {})
        self.assertEqual(len(collection.calls), 1)
        documents = collection.calls[0]
        self.assertEqual(len(documents), 2)
        self.assertEqual(list(documents[0].vectors["embedding"]), [1.0, 0.0, 0.0, 0.0])
        self.assertEqual(list(documents[1].vectors["embedding"]), [0.0, 1.0, 0.0, 0.0])
        self.assertEqual(documents[0].fields["tags"], ["角色"])
        self.assertEqual(documents[1].fields["tags"], ["作品"])

    def test_bulk_failure_falls_back_to_item_isolation(self) -> None:
        collection = _CapturingCollection(fail_bulk=True)
        repository = self._repository(collection)

        succeeded, failures = repository.upsert_record_vectors(
            [
                (_record("doc-a"), [1.0, 0.0, 0.0, 0.0], []),
                (_record("doc-b"), [0.0, 1.0, 0.0, 0.0], []),
            ]
        )

        self.assertEqual(succeeded, ["doc-a"])
        self.assertEqual(failures, {"doc-b": "item rejected"})
        self.assertEqual(len(collection.calls), 3)

    def test_legacy_same_vector_api_reuses_generator_tags_for_every_record(
        self,
    ) -> None:
        collection = _CapturingCollection()
        repository = self._repository(collection)

        succeeded, failures = repository.upsert_records(
            [_record("doc-a"), _record("doc-b")],
            [1.0, 0.0, 0.0, 0.0],
            (tag for tag in ["角色"]),
        )

        self.assertEqual(succeeded, ["doc-a", "doc-b"])
        self.assertEqual(failures, {})
        documents = collection.calls[0]
        self.assertEqual(documents[0].fields["tags"], ["角色"])
        self.assertEqual(documents[1].fields["tags"], ["角色"])


if __name__ == "__main__":
    unittest.main()
