from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

from image_vector_service.collection_write_coordinator import (
    CollectionWriteCoordinator,
    PreparedCollectionUpsert,
)
from image_vector_service.collection_write_outbox import CollectionWriteItem
from image_vector_service.config import ServiceConfig
from image_vector_service.models import ImageRecord
from image_vector_service.optimize_policy import OptimizePolicyStore
from image_vector_service.service import ImageVectorService
from image_vector_service.state import IndexState


class _NoModelClient:
    def __init__(self) -> None:
        self.request_count = 0

    def embed_images(self, _paths: list[Path]) -> None:
        self.request_count += 1
        raise AssertionError("Collection recovery must not call the embedding model.")


class _MemoryRepository:
    collection_uuid = "outbox-integration-collection"
    created = False

    def __init__(self) -> None:
        self.documents: dict[str, tuple[list[float], tuple[str, ...]]] = {}
        self.upsert_batch_sizes: list[int] = []
        self.upsert_vector_batch_sizes: list[int] = []
        self.delete_batch_sizes: list[int] = []
        self.item_failure_ids: set[str] = set()
        self.systemic_failure = False
        self.optimize_count = 0

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    def set_tag_catalog(self, _tags: Iterable[str], **_kwargs: object) -> None:
        return None

    def upsert_records(
        self,
        records: list[ImageRecord],
        vector: list[float],
        tags: Iterable[str] = (),
    ) -> tuple[list[str], dict[str, str]]:
        self.upsert_batch_sizes.append(len(records))
        if self.systemic_failure:
            raise OSError(28, "disk full")
        normalized_tags = tuple(tags)
        succeeded: list[str] = []
        failures: dict[str, str] = {}
        for record in records:
            if record.doc_id in self.item_failure_ids:
                failures[record.doc_id] = "invalid document item rejected"
                continue
            self.documents[record.doc_id] = (list(vector), normalized_tags)
            succeeded.append(record.doc_id)
        return succeeded, failures

    def upsert_record_vectors(
        self,
        items: Iterable[tuple[ImageRecord, list[float], Iterable[str]]],
    ) -> tuple[list[str], dict[str, str]]:
        materialized = list(items)
        self.upsert_vector_batch_sizes.append(len(materialized))
        if self.systemic_failure:
            raise OSError(28, "disk full")
        succeeded: list[str] = []
        failures: dict[str, str] = {}
        for record, vector, tags in materialized:
            if record.doc_id in self.item_failure_ids:
                failures[record.doc_id] = "invalid document item rejected"
                continue
            self.documents[record.doc_id] = (list(vector), tuple(tags))
            succeeded.append(record.doc_id)
        return succeeded, failures

    def delete_resilient(
        self, doc_ids: Iterable[str]
    ) -> tuple[list[str], dict[str, str]]:
        ids = list(doc_ids)
        self.delete_batch_sizes.append(len(ids))
        if self.systemic_failure:
            raise OSError(28, "disk full")
        for doc_id in ids:
            self.documents.pop(doc_id, None)
        # A missing id is still a successful idempotent delete.
        return ids, {}

    def optimize(self) -> None:
        self.optimize_count += 1


def _record(index: int = 0) -> ImageRecord:
    doc_id = f"doc-{index:05d}"
    return ImageRecord(
        doc_id=doc_id,
        root_id="root-a",
        relative_path=f"album/{doc_id}.jpg",
        absolute_path=f"C:/test-only/{doc_id}.jpg",
        file_name=f"{doc_id}.jpg",
        extension=".jpg",
        mime_type="image/jpeg",
        sha256=f"{index + 1:064x}",
        size_bytes=100 + index,
        mtime_ns=1_000 + index,
        width=32,
        height=48,
    )


def _state_entry(record: ImageRecord) -> dict[str, object]:
    return {
        **record.state_dict(),
        "tags": ["manual"],
        "folder_tags": ["folder"],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


def _prepared(index: int = 0) -> PreparedCollectionUpsert:
    record = _record(index)
    return PreparedCollectionUpsert(
        record=record,
        image_vector=[float(index + 1) / 1000.0] * 1024,
        effective_tags=("manual", "folder"),
        state_entry=_state_entry(record),
    )


def _outbox_item(index: int = 0) -> CollectionWriteItem:
    prepared = _prepared(index)
    record = prepared.record
    return CollectionWriteItem(
        doc_id=record.doc_id,
        action="upsert",
        collection_fields={
            "root_id": record.root_id,
            "relative_path": record.relative_path,
            "file_name": record.file_name,
            "extension": record.extension,
            "mime_type": record.mime_type,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "mtime_ns": record.mtime_ns,
            "width": record.width,
            "height": record.height,
            "model": "qwen3-vl-embedding",
            "tags": ["manual", "folder"],
            "metadata_text": "",
            "metadata_text_hash": "",
        },
        vectors={
            "embedding": list(prepared.image_vector),
            "metadata_embedding": [0.0] * 1024,
        },
        state_entry=prepared.state_entry,
    )


class CollectionWriteCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_collection_write_integration_"
        )
        self.state = IndexState(Path(self.temporary.name) / "state.sqlite3")
        self.repository = _MemoryRepository()
        self.state.ensure_collection_uuid(self.repository.collection_uuid)
        self.coordinator = CollectionWriteCoordinator(
            state=self.state,
            repository=self.repository,
            model="qwen3-vl-embedding",
            dimension=1024,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_strict_order_is_enqueue_claim_zvec_sqlite_then_applied(self) -> None:
        events: list[str] = []

        def record(name: str, function):
            def invoke(*args, **kwargs):
                events.append(name)
                return function(*args, **kwargs)

            return invoke

        with (
            patch.object(
                self.state.write_outbox,
                "enqueue",
                side_effect=record("enqueue", self.state.write_outbox.enqueue),
            ),
            patch.object(
                self.state.write_outbox,
                "claim_operation",
                side_effect=record("claim", self.state.write_outbox.claim_operation),
            ),
            patch.object(
                self.repository,
                "upsert_records",
                side_effect=record("zvec", self.repository.upsert_records),
            ),
            patch.object(
                self.state,
                "set_many",
                side_effect=record("sqlite", self.state.set_many),
            ),
            patch.object(
                self.state.write_outbox,
                "mark_applied",
                side_effect=record("applied", self.state.write_outbox.mark_applied),
            ),
        ):
            result = self.coordinator.upsert([_prepared()])

        self.assertEqual(result.succeeded, ["doc-00000"])
        self.assertEqual(events, ["enqueue", "claim", "zvec", "sqlite", "applied"])

    def test_mutation_observer_runs_after_completion_and_cannot_fail_write(
        self,
    ) -> None:
        events: list[str] = []

        def observer(
            _operation_id: str,
            *,
            changes: int,
            deletes: int,
        ) -> None:
            events.append(f"observer:{changes}:{deletes}")
            raise RuntimeError("optional observer unavailable")

        coordinator = CollectionWriteCoordinator(
            state=self.state,
            repository=self.repository,
            model="qwen3-vl-embedding",
            dimension=1024,
            mutation_observer=observer,
        )
        original_mark_applied = self.state.write_outbox.mark_applied

        def mark_applied(*args, **kwargs):
            result = original_mark_applied(*args, **kwargs)
            events.append("applied")
            return result

        with patch.object(
            self.state.write_outbox,
            "mark_applied",
            side_effect=mark_applied,
        ):
            result = coordinator.upsert([_prepared()])

        self.assertEqual(result.succeeded, ["doc-00000"])
        self.assertFalse(result.systemic_failure)
        self.assertEqual(events, ["applied", "observer:1:0"])

    def test_recovery_mutation_accounting_is_operation_idempotent(self) -> None:
        store = OptimizePolicyStore(
            Path(self.temporary.name) / "optimize-policy.sqlite3"
        )
        coordinator = CollectionWriteCoordinator(
            state=self.state,
            repository=self.repository,
            model="qwen3-vl-embedding",
            dimension=1024,
            mutation_observer=store.mark_changes,
        )
        operation_id = "index:recovery-observer"
        self.state.write_outbox.enqueue(
            operation_id,
            "index_upsert",
            [_outbox_item()],
        )

        recovery = coordinator.recover_pending()
        duplicate = store.mark_changes(operation_id, changes=1)
        status = store.status()

        self.assertEqual(recovery["recovered"], 1)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(status.pending_changes, 1)
        self.assertEqual(status.generation, 1)

    def test_delete_observer_counts_only_successfully_applied_items(self) -> None:
        observed: list[tuple[int, int]] = []

        def observer(
            _operation_id: str,
            *,
            changes: int,
            deletes: int,
        ) -> None:
            observed.append((changes, deletes))

        coordinator = CollectionWriteCoordinator(
            state=self.state,
            repository=self.repository,
            model="qwen3-vl-embedding",
            dimension=1024,
            mutation_observer=observer,
        )
        coordinator.upsert([_prepared()])
        observed.clear()

        result = coordinator.delete(["doc-00000"])

        self.assertEqual(result.succeeded, ["doc-00000"])
        self.assertEqual(observed, [(0, 1)])

    def test_writes_are_bounded_and_one_bad_item_does_not_stop_later_batches(
        self,
    ) -> None:
        self.repository.item_failure_ids.add("doc-00001")
        result = self.coordinator.upsert(_prepared(index) for index in range(600))

        self.assertFalse(result.systemic_failure)
        self.assertEqual(
            result.failures,
            {"doc-00001": "invalid document item rejected"},
        )
        self.assertEqual(len(result.succeeded), 599)
        self.assertEqual(self.state.count(), 599)
        self.assertEqual(self.repository.upsert_vector_batch_sizes, [256, 256, 88])
        operations = self.state.connection.execute(
            "SELECT status, item_count FROM collection_write_operations "
            "ORDER BY sequence"
        ).fetchall()
        self.assertEqual(
            [(str(row["status"]), int(row["item_count"])) for row in operations],
            [("failed", 256), ("applied", 256), ("applied", 88)],
        )

    def test_systemic_failure_stops_before_enqueuing_later_batches(self) -> None:
        self.repository.systemic_failure = True
        result = self.coordinator.upsert(_prepared(index) for index in range(300))

        self.assertTrue(result.systemic_failure)
        self.assertEqual(len(result.failures), 256)
        self.assertEqual(result.deferred_count, 44)
        self.assertEqual(self.state.count(), 0)
        self.assertEqual(self.repository.upsert_vector_batch_sizes, [256])
        operation_count = self.state.connection.execute(
            "SELECT COUNT(*) FROM collection_write_operations"
        ).fetchone()[0]
        self.assertEqual(operation_count, 1)

    def test_sqlite_failure_keeps_vectors_for_zero_model_retry(self) -> None:
        original_set_many = self.state.set_many
        with patch.object(
            self.state,
            "set_many",
            side_effect=OSError(5, "state unavailable"),
        ):
            first = self.coordinator.upsert([_prepared()])
        self.assertTrue(first.systemic_failure)
        self.assertEqual(self.state.count(), 0)
        row = self.state.connection.execute(
            "SELECT status, vectors_blob FROM collection_write_items"
        ).fetchone()
        self.assertEqual(str(row["status"]), "failed")
        self.assertIsNotNone(row["vectors_blob"])

        # The exact persisted vector is reused; recovery has no model object.
        self.state.set_many = original_set_many  # type: ignore[method-assign]
        recovery = self.coordinator.recover_pending()
        self.assertEqual(recovery["recovered"], 1)
        self.assertEqual(self.state.count(), 1)
        self.assertEqual(self.repository.upsert_batch_sizes, [1, 1])

    def test_recovery_skips_nonretryable_bad_item_and_continues(self) -> None:
        self.repository.item_failure_ids.add("doc-00000")
        self.state.write_outbox.enqueue(
            "index:startup-item-failure",
            "index_upsert",
            [_outbox_item(0), _outbox_item(1)],
        )

        recovery = self.coordinator.recover_pending()

        self.assertEqual(recovery["recovered"], 1)
        self.assertEqual(recovery["failed"], 1)
        self.assertIsNone(self.state.get("doc-00000"))
        self.assertIsNotNone(self.state.get("doc-00001"))
        self.assertEqual(self.state.write_outbox.pending_count(), 0)
        operation = self.state.write_outbox.get_operation("index:startup-item-failure")
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation.status, "failed")


class ImageVectorServiceStartupRecoveryTests(unittest.TestCase):
    def _config(self, root: Path) -> ServiceConfig:
        return ServiceConfig(
            workspace=root / "workspace",
            results_directory=root / "results",
        )

    def test_all_upsert_crash_windows_recover_without_embedding_requests(self) -> None:
        for crash_window in ("enqueued", "claimed", "zvec", "sqlite"):
            with (
                self.subTest(crash_window=crash_window),
                tempfile.TemporaryDirectory(
                    prefix=f"zvec_outbox_{crash_window}_",
                    ignore_cleanup_errors=True,
                ) as temporary,
            ):
                root = Path(temporary)
                config = self._config(root)
                repository = _MemoryRepository()
                first_client = _NoModelClient()
                first = ImageVectorService(
                    config=config,
                    repository=repository,  # type: ignore[arg-type]
                    embedding_client=first_client,
                )
                operation_id = f"index:{crash_window}"
                first.state.write_outbox.enqueue(
                    operation_id,
                    "index_upsert",
                    [_outbox_item()],
                )
                if crash_window != "enqueued":
                    claimed = first.state.write_outbox.claim_operation(operation_id)
                    self.assertEqual(len(claimed), 1)
                    if crash_window in {"zvec", "sqlite"}:
                        repository.upsert_records(
                            [_record()],
                            [0.001] * 1024,
                            ("manual", "folder"),
                        )
                    if crash_window == "sqlite":
                        first.state.set_many([_state_entry(_record())])
                first.close()

                recovery_client = _NoModelClient()
                recovered = ImageVectorService(
                    config=config,
                    repository=repository,  # type: ignore[arg-type]
                    embedding_client=recovery_client,
                )
                try:
                    self.assertEqual(first_client.request_count, 0)
                    self.assertEqual(recovery_client.request_count, 0)
                    self.assertEqual(repository.doc_count, 1)
                    self.assertIsNotNone(recovered.state.get("doc-00000"))
                    operation = recovered.state.write_outbox.get_operation(operation_id)
                    self.assertIsNotNone(operation)
                    assert operation is not None
                    self.assertEqual(operation.status, "applied")
                    calls_after_recovery = len(repository.upsert_batch_sizes)
                finally:
                    recovered.close()

                # A clean third startup performs only the indexed pending-count
                # lookup; it does not scan entries or rewrite Zvec documents.
                clean_client = _NoModelClient()
                clean = ImageVectorService(
                    config=config,
                    repository=repository,  # type: ignore[arg-type]
                    embedding_client=clean_client,
                )
                try:
                    self.assertEqual(clean_client.request_count, 0)
                    self.assertEqual(
                        len(repository.upsert_batch_sizes), calls_after_recovery
                    )
                finally:
                    clean.close()

    def test_interrupted_sync_delete_is_replayed_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zvec_outbox_delete_") as temporary:
            root = Path(temporary)
            config = self._config(root)
            repository = _MemoryRepository()
            repository.documents["doc-00000"] = ([0.001] * 1024, ())
            first = ImageVectorService(
                config=config,
                repository=repository,  # type: ignore[arg-type]
                embedding_client=_NoModelClient(),
            )
            first.state.set_many([_state_entry(_record())])
            first.state.write_outbox.enqueue(
                "sync:delete-crash",
                "sync_delete",
                [CollectionWriteItem(doc_id="doc-00000", action="delete")],
            )
            claimed = first.state.write_outbox.claim_operation("sync:delete-crash")
            repository.delete_resilient([claimed[0].doc_id])
            first.close()

            recovery_client = _NoModelClient()
            recovered = ImageVectorService(
                config=config,
                repository=repository,  # type: ignore[arg-type]
                embedding_client=recovery_client,
            )
            try:
                self.assertEqual(recovery_client.request_count, 0)
                self.assertEqual(repository.doc_count, 0)
                self.assertIsNone(recovered.state.get("doc-00000"))
                operation = recovered.state.write_outbox.get_operation(
                    "sync:delete-crash"
                )
                self.assertIsNotNone(operation)
                assert operation is not None
                self.assertEqual(operation.status, "applied")
            finally:
                recovered.close()

    def test_startup_stays_available_when_one_replay_item_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zvec_outbox_item_skip_",
            ignore_cleanup_errors=True,
        ) as temporary:
            root = Path(temporary)
            config = self._config(root)
            repository = _MemoryRepository()
            first = ImageVectorService(
                config=config,
                repository=repository,  # type: ignore[arg-type]
                embedding_client=_NoModelClient(),
            )
            first.state.write_outbox.enqueue(
                "index:startup-item-skip",
                "index_upsert",
                [_outbox_item(0), _outbox_item(1)],
            )
            first.close()
            repository.item_failure_ids.add("doc-00000")

            client = _NoModelClient()
            recovered = ImageVectorService(
                config=config,
                repository=repository,  # type: ignore[arg-type]
                embedding_client=client,
            )
            try:
                self.assertEqual(client.request_count, 0)
                self.assertIsNone(recovered.state.get("doc-00000"))
                self.assertIsNotNone(recovered.state.get("doc-00001"))
                self.assertEqual(recovered.state.write_outbox.pending_count(), 0)
            finally:
                recovered.close()


if __name__ == "__main__":
    unittest.main()
