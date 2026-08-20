from __future__ import annotations

import hashlib
import logging
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from image_vector_service.annotation_service import AutoTaggingCoordinator
from image_vector_service.auto_tag_cache import SharedAutoTagCache
from image_vector_service.cluster_operation_store import ClusterOperationStore
from image_vector_service.collection_write_coordinator import CollectionWriteResult
from image_vector_service.config import ServiceConfig
from image_vector_service.image_clustering import (
    ClusterEdge,
    ClusterItemState,
    ClusterSnapshot,
    ImageCluster,
)
from image_vector_service.models import ImageRecord, SearchHit
from image_vector_service.service import ImageVectorService


class _State:
    """Small in-memory IndexState contract used with the real coordinator."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = {str(entry["doc_id"]): dict(entry) for entry in entries}
        self.annotations: dict[str, dict[str, Any]] = {}

    def count(self) -> int:
        return len(self.entries)

    def iter_entries(self, *, chunk_size: int = 256):
        values = list(self.entries.values())
        for offset in range(0, len(values), chunk_size):
            yield [dict(entry) for entry in values[offset : offset + chunk_size]]

    def list_entries(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.entries.values()]

    def get(self, doc_id: str) -> dict[str, Any] | None:
        entry = self.entries.get(str(doc_id))
        return None if entry is None else dict(entry)

    def get_many(self, doc_ids: Any) -> dict[str, dict[str, Any]]:
        requested = {str(value) for value in doc_ids}
        return {
            doc_id: dict(entry)
            for doc_id, entry in self.entries.items()
            if doc_id in requested
        }

    def set_many(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            doc_id = str(record["doc_id"])
            current = self.entries.get(doc_id, {})
            self.entries[doc_id] = {**current, **dict(record)}

    def get_document_annotation(self, doc_id: str) -> dict[str, Any] | None:
        annotation = self.annotations.get(str(doc_id))
        return None if annotation is None else dict(annotation)

    def get_document_annotations(self, doc_ids: Any) -> dict[str, dict[str, Any]]:
        requested = {str(value) for value in doc_ids}
        return {
            doc_id: dict(annotation)
            for doc_id, annotation in self.annotations.items()
            if doc_id in requested
        }

    def set_document_annotation(self, **values: Any) -> None:
        doc_id = str(values["doc_id"])
        self.annotations[doc_id] = {
            "doc_id": doc_id,
            "source_sha256": str(values["source_sha256"]),
            "cache_key": values.get("cache_key"),
            "status": str(values.get("status") or "accepted"),
            "proposed_tags": list(values.get("proposed_tags") or ()),
            "accepted_tags": list(values.get("accepted_tags") or ()),
            "rejected_tags": list(values.get("rejected_tags") or ()),
            "description": str(values.get("description") or ""),
            "entities": dict(values.get("entities") or {}),
            "warnings": list(values.get("warnings") or ()),
            "structured": dict(values.get("structured") or {}),
            "policy": dict(values.get("policy") or {}),
            "error": str(values.get("error") or ""),
        }

    def delete_document_annotation(self, doc_id: str) -> None:
        self.annotations.pop(str(doc_id), None)

    def list_effective_tags(self) -> list[str]:
        tags: set[str] = set()
        for entry in self.entries.values():
            for key in (
                "tags",
                "folder_tags",
                "accepted_auto_tags",
                "inherited_tags",
            ):
                tags.update(str(value) for value in entry.get(key, ()))
        return sorted(tags)


class _Resolver:
    def resolve_fields(self, fields: dict[str, Any]) -> Path:
        return Path(str(fields["source_path"]))


class _Repository:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.collection_uuid = "cluster-p1-tests"
        self.vectors = {key: list(value) for key, value in vectors.items()}
        self.doc_count = len(vectors)
        self.fail_once: set[str] = set()
        self.upserted_tags: dict[str, list[str]] = {}
        self.optimize_count = 0
        self.tag_catalog: list[str] = []

    def fetch_vector(self, doc_id: str) -> list[float] | None:
        vector = self.vectors.get(str(doc_id))
        return None if vector is None else list(vector)

    def query(
        self,
        vector: list[float],
        top_k: int,
        _tags: tuple[str, ...],
        _tag_mode: str,
        _rank_source: str,
    ) -> list[SearchHit]:
        left_norm = math.sqrt(sum(value * value for value in vector))
        scored: list[tuple[float, str]] = []
        for doc_id, candidate in self.vectors.items():
            right_norm = math.sqrt(sum(value * value for value in candidate))
            similarity = sum(
                left * right for left, right in zip(vector, candidate, strict=True)
            ) / (left_norm * right_norm)
            scored.append((1.0 - similarity, doc_id))
        scored.sort()
        return [
            SearchHit(doc_id=doc_id, distance=distance, fields={})
            for distance, doc_id in scored[:top_k]
        ]

    def upsert_records(
        self,
        records: list[ImageRecord],
        _vector: list[float],
        effective_tags: list[str],
    ) -> tuple[list[str], dict[str, str]]:
        doc_id = records[0].doc_id
        if doc_id in self.fail_once:
            self.fail_once.remove(doc_id)
            return [], {doc_id: "synthetic Collection write failure"}
        self.upserted_tags[doc_id] = list(effective_tags)
        return [doc_id], {}

    def set_tag_catalog(self, tags: list[str]) -> None:
        self.tag_catalog = list(tags)

    def optimize(self) -> None:
        self.optimize_count += 1


class _CollectionWrites:
    """Test-only durable-writer seam with the same success boundary as production."""

    def __init__(self, repository: _Repository, state: _State) -> None:
        self.repository = repository
        self.state = state

    def upsert(
        self,
        writes: Any,
        *,
        operation_kind: str = "test_upsert",
    ) -> CollectionWriteResult:
        del operation_kind
        result = CollectionWriteResult()
        for prepared in writes:
            succeeded, failures = self.repository.upsert_records(
                [prepared.record],
                list(prepared.image_vector),
                list(prepared.effective_tags),
            )
            result.succeeded.extend(succeeded)
            result.failures.update(failures)
            if succeeded:
                self.state.set_many([dict(prepared.state_entry)])
        return result


def _entry(root: Path, doc_id: str, index: int) -> dict[str, Any]:
    path = root / f"{doc_id}.png"
    Image.new("RGB", (12, 10), (index % 255, 90, 150)).save(path)
    stat = path.stat()
    return {
        "doc_id": doc_id,
        "root_id": "root-tests",
        "relative_path": path.name,
        "source_path": str(path),
        "file_name": path.name,
        "extension": ".png",
        "mime_type": "image/png",
        "sha256": hashlib.sha256(doc_id.encode("utf-8")).hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "width": 12,
        "height": 10,
        "tags": [],
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
        "effective_tags": [],
    }


def _snapshot(groups: list[list[str]]) -> ClusterSnapshot:
    items: list[ClusterItemState] = []
    edges: list[ClusterEdge] = []
    clusters: list[ImageCluster] = []
    for group_index, doc_ids in enumerate(groups):
        cluster_id = f"cluster-{group_index:04d}"
        for doc_id in doc_ids:
            items.append(
                ClusterItemState(
                    doc_id=doc_id,
                    fingerprint=hashlib.sha256(
                        f"fingerprint:{doc_id}".encode()
                    ).hexdigest(),
                    cluster_id=cluster_id,
                )
            )
        for left, right in zip(doc_ids, doc_ids[1:], strict=False):
            edges.append(ClusterEdge(left, right, "semantic", 0.99))
        clusters.append(
            ImageCluster(
                cluster_id=cluster_id,
                member_doc_ids=tuple(doc_ids),
                edge_kinds=("semantic",) if len(doc_ids) > 1 else (),
            )
        )
    return ClusterSnapshot(
        algorithm_version="cluster-p1-test",
        embedding_version="test-vector-v1",
        items=tuple(items),
        edges=tuple(edges),
        clusters=tuple(clusters),
    )


class ClusterManualOperationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.caches: list[SharedAutoTagCache] = []

    def _service(
        self,
        doc_ids: list[str],
        vectors: dict[str, list[float]] | None = None,
    ) -> ImageVectorService:
        entries = [
            _entry(self.root, doc_id, index) for index, doc_id in enumerate(doc_ids, 1)
        ]
        repository = _Repository(
            vectors
            or {
                doc_id: [1.0, float(index + 1) / 1_000.0]
                for index, doc_id in enumerate(doc_ids)
            }
        )
        state = _State(entries)
        config = ServiceConfig(
            workspace=self.root / "workspace",
            config_home=self.root / "config",
            library_id="lib-cluster-p1",
            dimension=2,
        )
        cache = SharedAutoTagCache(self.root / f"cache-{len(self.caches)}.sqlite3")
        self.caches.append(cache)
        self.addCleanup(cache.close)
        resolver = _Resolver()
        service = ImageVectorService.__new__(ImageVectorService)
        service.config = config
        service.state = state
        service.repository = repository
        service.source_resolver = resolver
        service.cancel_check = lambda: None
        service.progress = lambda _message: None
        service.logger = logging.getLogger("cluster-p1-tests")
        collection_writes = _CollectionWrites(repository, state)
        service.collection_writes = collection_writes
        service.auto_tagging = AutoTaggingCoordinator(
            config=config,
            state=state,
            repository=repository,
            cache=cache,
            collection_writes=collection_writes,
            source_resolver=resolver,
            progress=lambda _message: None,
            cancel_check=lambda: None,
        )
        service.cluster_operations = ClusterOperationStore(
            config.state_path,
            recover_interrupted=False,
        )
        return service

    @staticmethod
    def _activate_snapshot(
        service: ImageVectorService,
        groups: list[list[str]],
    ) -> None:
        service.cluster_operations.save_snapshot(
            service._cluster_library_key(),  # noqa: SLF001
            _snapshot(groups),
        )

    def test_real_coordinator_adds_identity_without_annotation_and_undoes(self) -> None:
        service = self._service(["doc-a", "doc-b"])
        self._activate_snapshot(service, [["doc-a", "doc-b"]])

        result = service.apply_image_cluster_identity(
            "cluster-0000",
            identity_category="character",
            identity_value="雷电将军",
        )

        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["undo_available"])
        for doc_id in ("doc-a", "doc-b"):
            entry = service.state.get(doc_id)
            assert entry is not None
            self.assertEqual(entry["inherited_tags"], ["雷电将军"])
            annotation = service.state.get_document_annotation(doc_id)
            assert annotation is not None
            entities = annotation["structured"]["entities"]["character"]
            self.assertEqual(entities[0]["name"], "雷电将军")
            self.assertEqual(entities[0]["source"], "cluster_inherited")

        undone = service.undo_latest_cluster_operation()

        self.assertTrue(undone["undone"])
        self.assertEqual(undone["restored"], 2)
        self.assertEqual(undone["conflicts"], 0)
        for doc_id in ("doc-a", "doc-b"):
            entry = service.state.get(doc_id)
            assert entry is not None
            self.assertEqual(entry["inherited_tags"], [])
            self.assertIsNone(service.state.get_document_annotation(doc_id))

    def test_real_coordinator_continues_after_one_identity_write_failure(self) -> None:
        service = self._service(["doc-a", "doc-b", "doc-c"])
        self._activate_snapshot(service, [["doc-a", "doc-b", "doc-c"]])
        service.repository.fail_once.add("doc-b")

        result = service.apply_image_cluster_identity(
            "cluster-0000",
            identity_category="work",
            identity_value="原神",
        )

        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failures"][0]["doc_id"], "doc-b")
        self.assertEqual(service.state.get("doc-b")["inherited_tags"], [])
        self.assertEqual(service.state.get("doc-c")["inherited_tags"], ["原神"])
        # Tag writes are immediately durable; expensive Collection compaction
        # is now deferred to the library worker's idle maintenance policy.
        self.assertEqual(service.repository.optimize_count, 0)

    def test_merge_and_split_rules_survive_full_reclustering(self) -> None:
        vectors = {
            "doc-a": [1.0, 0.0],
            "doc-b": [0.999, 0.001],
            "doc-c": [0.0, 1.0],
            "doc-d": [0.001, 0.999],
        }
        service = self._service(list(vectors), vectors)
        service.cluster_images(scope="all", cluster_types=("semantic",))
        initial = service.list_image_clusters(limit=20)
        cluster_ids = [str(item["cluster_id"]) for item in initial["items"]]
        self.assertEqual(len(cluster_ids), 2)

        service.merge_image_clusters(cluster_ids)
        service.cluster_images(scope="all", cluster_types=("semantic",))
        merged = service._active_cluster_snapshot()  # noqa: SLF001
        assert merged is not None
        self.assertEqual(len(merged.clusters), 1)
        self.assertEqual(set(merged.clusters[0].member_doc_ids), set(vectors))

        self.assertTrue(service.undo_latest_cluster_operation()["undone"])
        restored = service.list_image_clusters(limit=20)
        pair = next(
            item
            for item in restored["items"]
            if {"doc-a", "doc-b"}.issubset(
                {
                    str(value["doc_id"])
                    for value in service.image_cluster_detail(str(item["cluster_id"]))[
                        "items"
                    ]
                }
            )
        )
        service.split_image_cluster(str(pair["cluster_id"]), ("doc-a",))
        service.cluster_images(scope="all", cluster_types=("semantic",))
        split = service._active_cluster_snapshot()  # noqa: SLF001
        assert split is not None
        cluster_by_doc = {item.doc_id: item.cluster_id for item in split.items}
        self.assertNotEqual(cluster_by_doc["doc-a"], cluster_by_doc["doc-b"])

    def test_cluster_detail_member_pages_have_no_duplicates_or_omissions(self) -> None:
        doc_ids = [f"doc-{index:03d}" for index in range(37)]
        service = self._service(doc_ids)
        self._activate_snapshot(service, [doc_ids])
        observed: list[str] = []
        offset = 0

        while True:
            page = service.image_cluster_detail(
                "cluster-0000",
                offset=offset,
                limit=7,
                edge_offset=0,
                edge_limit=5,
            )
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["limit"], 7)
            self.assertEqual(page["member_count"], len(doc_ids))
            values = [str(item["doc_id"]) for item in page["items"]]
            self.assertLessEqual(len(values), 7)
            observed.extend(values)
            if not page["has_more"]:
                break
            offset += len(values)

        self.assertEqual(len(observed), len(doc_ids))
        self.assertEqual(len(set(observed)), len(doc_ids))
        self.assertEqual(set(observed), set(doc_ids))


if __name__ == "__main__":
    unittest.main()
