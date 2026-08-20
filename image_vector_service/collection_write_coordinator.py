"""Apply durable SQLite/Zvec mutations in bounded, replayable batches.

The outbox owns durability; this coordinator owns the required execution
order.  Every vector in a replay item has already been generated before it is
enqueued, so startup recovery never reaches an embedding or vision client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from .collection_write_outbox import (
    CollectionWriteFailure,
    CollectionWriteItem,
    ReplayCollectionWrite,
)
from .folder_name_tags import merge_effective_tags
from .logical_paths import normalize_relative_path
from .models import ImageRecord
from .state import IndexState
from .tags import normalize_tags
from .zvec_repository import empty_metadata_embedding

DEFAULT_COLLECTION_WRITE_BATCH_SIZE = 256
_STATE_ENTRY_FIELDS = (
    "doc_id",
    "root_id",
    "relative_path",
    "parent_directory",
    "file_name",
    "extension",
    "mime_type",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "width",
    "height",
    "tags",
    "folder_tags",
    "accepted_auto_tags",
    "inherited_tags",
)


class CollectionWriteRecoveryError(RuntimeError):
    """Durable writes remain unapplied after a bounded recovery attempt."""


class _CollectionRepository(Protocol):
    def upsert_records(
        self,
        records: list[ImageRecord],
        vector: list[float],
        tags: Iterable[str] = (),
    ) -> tuple[list[str], dict[str, str]]: ...

    def upsert_record_vectors(
        self,
        items: Iterable[tuple[ImageRecord, list[float], Iterable[str]]],
    ) -> tuple[list[str], dict[str, str]]: ...

    def delete_resilient(
        self, doc_ids: Iterable[str]
    ) -> tuple[list[str], dict[str, str]]: ...


class CollectionMutationObserver(Protocol):
    """Best-effort notification after one durable operation is complete."""

    def __call__(
        self,
        operation_id: str,
        *,
        changes: int,
        deletes: int,
    ) -> None: ...


@dataclass(frozen=True)
class PreparedCollectionUpsert:
    """One generated image vector and the exact matching SQLite state row."""

    record: ImageRecord
    image_vector: Sequence[float]
    effective_tags: Sequence[str]
    state_entry: Mapping[str, Any]


@dataclass
class CollectionWriteResult:
    succeeded: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    failure_kinds: dict[str, str] = field(default_factory=dict)
    systemic_failure: bool = False
    deferred_count: int = 0

    def extend(self, other: CollectionWriteResult) -> None:
        self.succeeded.extend(other.succeeded)
        self.failures.update(other.failures)
        self.failure_kinds.update(other.failure_kinds)
        self.systemic_failure = self.systemic_failure or other.systemic_failure
        self.deferred_count += other.deferred_count


class CollectionWriteCoordinator:
    """Single-writer bridge between the durable outbox, Zvec and SQLite."""

    def __init__(
        self,
        *,
        state: IndexState,
        repository: _CollectionRepository,
        model: str,
        dimension: int,
        batch_size: int = DEFAULT_COLLECTION_WRITE_BATCH_SIZE,
        mutation_observer: CollectionMutationObserver | None = None,
    ) -> None:
        if not 100 <= batch_size <= 500:
            raise ValueError("Collection write batch size must be between 100 and 500.")
        if dimension <= 0:
            raise ValueError("Collection vector dimension must be positive.")
        self.state = state
        self.repository = repository
        self.model = str(model)
        self.dimension = int(dimension)
        self.batch_size = int(batch_size)
        self.mutation_observer = mutation_observer

    def recover_pending(self) -> dict[str, int]:
        """Replay pending/interrupted writes without scanning indexed entries."""

        pending_before = self.state.write_outbox.pending_count()
        if pending_before == 0:
            return {
                "pending_before": 0,
                "recovered": 0,
                "failed": 0,
                "interrupted": self.state.write_outbox.recovered_interrupted_count,
            }

        recovered = 0
        failed = 0
        while True:
            claimed = self.state.write_outbox.claim_recoverable(limit=self.batch_size)
            if not claimed:
                break
            result = self._apply_claimed(claimed)
            recovered += len(result.succeeded)
            failed += len(result.failures)
            if result.systemic_failure:
                raise CollectionWriteRecoveryError(
                    "Collection write recovery could not restore local consistency: "
                    f"{len(result.failures)} item(s) failed."
                )
        return {
            "pending_before": pending_before,
            "recovered": recovered,
            "failed": failed,
            "interrupted": self.state.write_outbox.recovered_interrupted_count,
        }

    def upsert(
        self,
        writes: Iterable[PreparedCollectionUpsert],
        *,
        operation_kind: str = "index_upsert",
    ) -> CollectionWriteResult:
        materialized = list(writes)
        if not materialized:
            return CollectionWriteResult()
        _require_unique_doc_ids(item.record.doc_id for item in materialized)

        result = CollectionWriteResult()
        for offset in range(0, len(materialized), self.batch_size):
            chunk = materialized[offset : offset + self.batch_size]
            operation_id = self.state.write_outbox.new_operation_id(operation_kind)
            self.state.write_outbox.enqueue(
                operation_id,
                operation_kind,
                [self._outbox_upsert(item) for item in chunk],
            )
            claimed = self.state.write_outbox.claim_operation(
                operation_id,
                limit=self.batch_size,
            )
            if len(claimed) != len(chunk):
                raise CollectionWriteRecoveryError(
                    "The durable collection write could not be claimed completely."
                )
            batch_result = self._apply_claimed(claimed)
            result.extend(batch_result)
            if batch_result.systemic_failure:
                result.deferred_count += len(materialized) - offset - len(chunk)
                break
        return result

    def delete(
        self,
        doc_ids: Iterable[str],
        *,
        operation_kind: str = "sync_delete",
    ) -> CollectionWriteResult:
        normalized = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not normalized:
            return CollectionWriteResult()
        _require_unique_doc_ids(normalized)

        result = CollectionWriteResult()
        for offset in range(0, len(normalized), self.batch_size):
            chunk = normalized[offset : offset + self.batch_size]
            operation_id = self.state.write_outbox.new_operation_id(operation_kind)
            self.state.write_outbox.enqueue(
                operation_id,
                operation_kind,
                [
                    CollectionWriteItem(doc_id=doc_id, action="delete")
                    for doc_id in chunk
                ],
            )
            claimed = self.state.write_outbox.claim_operation(
                operation_id,
                limit=self.batch_size,
            )
            if len(claimed) != len(chunk):
                raise CollectionWriteRecoveryError(
                    "The durable collection delete could not be claimed completely."
                )
            batch_result = self._apply_claimed(claimed)
            result.extend(batch_result)
            if batch_result.systemic_failure:
                result.deferred_count += len(normalized) - offset - len(chunk)
                break
        return result

    def _outbox_upsert(self, item: PreparedCollectionUpsert) -> CollectionWriteItem:
        record = item.record
        vector = [float(component) for component in item.image_vector]
        if len(vector) != self.dimension:
            raise ValueError(
                f"Embedding dimension mismatch: {len(vector)} != {self.dimension}."
            )
        tags = list(merge_effective_tags(item.effective_tags))
        state_entry = {
            key: item.state_entry[key]
            for key in _STATE_ENTRY_FIELDS
            if key in item.state_entry
        }
        if str(state_entry.get("doc_id") or "") != record.doc_id:
            raise ValueError("The state entry does not match its image record.")
        relative_path = normalize_relative_path(record.relative_path)
        state_entry["relative_path"] = relative_path
        if "parent_directory" in state_entry:
            parent = str(state_entry["parent_directory"]).replace("\\", "/").strip("/")
            state_entry["parent_directory"] = parent
        return CollectionWriteItem(
            doc_id=record.doc_id,
            action="upsert",
            collection_fields={
                "root_id": record.root_id,
                "relative_path": relative_path,
                "file_name": record.file_name,
                "extension": record.extension,
                "mime_type": record.mime_type,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "mtime_ns": record.mtime_ns,
                "width": record.width,
                "height": record.height,
                "model": self.model,
                "tags": tags,
                # Index/tag changes invalidate any old metadata description.
                "metadata_text": "",
                "metadata_text_hash": "",
            },
            vectors={
                "embedding": vector,
                "metadata_embedding": empty_metadata_embedding(self.dimension),
            },
            state_entry=state_entry,
        )

    def _apply_claimed(
        self, claimed: Sequence[ReplayCollectionWrite]
    ) -> CollectionWriteResult:
        result = CollectionWriteResult()
        batches = list(_contiguous_batches(claimed, self.batch_size))
        for index, batch in enumerate(batches):
            batch_result = self._apply_batch(batch)
            result.extend(batch_result)
            if not batch_result.systemic_failure:
                continue
            remaining = [item for later in batches[index + 1 :] for item in later]
            self._defer_claimed(remaining)
            result.deferred_count += len(remaining)
            break
        return result

    def _apply_batch(
        self, batch: Sequence[ReplayCollectionWrite]
    ) -> CollectionWriteResult:
        if not batch:
            return CollectionWriteResult()
        operation_id = batch[0].operation_id
        action = batch[0].action
        if any(
            item.operation_id != operation_id or item.action != action for item in batch
        ):
            raise CollectionWriteRecoveryError(
                "A collection write batch mixed operations or actions."
            )

        if action == "upsert":
            succeeded, raw_failures = self._upsert_claimed(batch)
        else:
            succeeded, raw_failures = self._delete_claimed(batch)
        expected_ids = {item.doc_id for item in batch}
        succeeded = [doc_id for doc_id in succeeded if doc_id in expected_ids]
        accounted = set(succeeded) | set(raw_failures)
        for doc_id in sorted(expected_ids - accounted):
            raw_failures[doc_id] = "Collection write returned no item status."

        failures = self._persist_zvec_failures(operation_id, batch, raw_failures)
        systemic_failure = "systemic" in failures.failure_kinds.values()
        successful_items = [item for item in batch if item.doc_id in set(succeeded)]
        if successful_items:
            state_result = self._apply_state_and_complete(
                operation_id,
                successful_items,
            )
            failures.extend(state_result)
            systemic_failure = systemic_failure or state_result.systemic_failure
        failures.systemic_failure = systemic_failure
        return failures

    def _upsert_claimed(
        self, batch: Sequence[ReplayCollectionWrite]
    ) -> tuple[list[str], dict[str, str]]:
        items: list[tuple[ImageRecord, list[float], Iterable[str]]] = []
        validation_failures: dict[str, str] = {}
        for item in batch:
            try:
                fields = _supported_index_payload(item, self.model, self.dimension)
                items.append(
                    (
                        _record_from_fields(item.doc_id, fields),
                        list(item.vectors["embedding"]),
                        tuple(str(tag) for tag in fields["tags"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                validation_failures[item.doc_id] = str(exc) or exc.__class__.__name__
        try:
            # One content-hash group commonly shares a vector, while manual and
            # inherited tags may split it into a handful of tag groups. Keep
            # the long-standing repository hook for those groups; its current
            # implementation delegates to the independent-vector bulk API.
            unique_vectors = {tuple(vector) for _record, vector, _tags in items}
            if len(unique_vectors) == 1:
                vector = list(next(iter(unique_vectors)))
                grouped: dict[tuple[str, ...], list[ImageRecord]] = {}
                for record, _vector, tags in items:
                    grouped.setdefault(normalize_tags(tags), []).append(record)
                succeeded = []
                failures = {}
                for tags, records in grouped.items():
                    stored, rejected = self.repository.upsert_records(
                        records,
                        vector,
                        tags,
                    )
                    succeeded.extend(stored)
                    failures.update(rejected)
            else:
                succeeded, failures = self.repository.upsert_record_vectors(items)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            failures = {record.doc_id: error for record, _vector, _tags in items}
            succeeded = []
        failures.update(validation_failures)
        return succeeded, failures

    def _delete_claimed(
        self, batch: Sequence[ReplayCollectionWrite]
    ) -> tuple[list[str], dict[str, str]]:
        ids = [item.doc_id for item in batch]
        try:
            return self.repository.delete_resilient(ids)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            return [], {doc_id: error for doc_id in ids}

    def _persist_zvec_failures(
        self,
        operation_id: str,
        batch: Sequence[ReplayCollectionWrite],
        raw_failures: Mapping[str, str],
    ) -> CollectionWriteResult:
        result = CollectionWriteResult()
        if not raw_failures:
            return result
        item_by_doc = {item.doc_id: item for item in batch}
        persisted: dict[str, CollectionWriteFailure] = {}
        for doc_id, raw_error in raw_failures.items():
            item = item_by_doc.get(doc_id)
            if item is None:
                continue
            kind = "systemic" if _storage_error_is_systemic(raw_error) else "item"
            result.failures[doc_id] = str(raw_error)
            result.failure_kinds[doc_id] = kind
            persisted[item.item_id] = CollectionWriteFailure(
                code=(
                    "zvec_write_failed" if kind == "systemic" else "zvec_item_rejected"
                ),
                message=str(raw_error),
                # A repeated user operation can retry safely. Keeping a known
                # failed item out of automatic startup replay prevents a bad
                # document from making every future launch fail.
                retryable=False,
            )
        self.state.write_outbox.mark_failed(operation_id, persisted)
        result.systemic_failure = "systemic" in result.failure_kinds.values()
        return result

    def _apply_state_and_complete(
        self,
        operation_id: str,
        items: Sequence[ReplayCollectionWrite],
    ) -> CollectionWriteResult:
        result = CollectionWriteResult()
        try:
            upserts = [
                dict(item.state_entry)
                for item in items
                if item.state_action == "upsert" and item.state_entry is not None
            ]
            deletes = [item.doc_id for item in items if item.state_action == "delete"]
            if upserts:
                self.state.set_many(upserts)
            if deletes:
                self.state.remove_many(deletes)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            failed = {
                item.item_id: CollectionWriteFailure(
                    code="sqlite_write_failed",
                    message=message,
                    retryable=True,
                )
                for item in items
            }
            self.state.write_outbox.mark_failed(operation_id, failed)
            result.failures.update({item.doc_id: message for item in items})
            result.failure_kinds.update({item.doc_id: "systemic" for item in items})
            result.systemic_failure = True
            return result

        try:
            self.state.write_outbox.mark_applied(
                operation_id,
                [item.item_id for item in items],
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            # Completion itself can fail after both engines are consistent.
            # Preserve the payload for an idempotent replay on restart.
            with suppress(Exception):
                self.state.write_outbox.mark_failed(
                    operation_id,
                    {
                        item.item_id: CollectionWriteFailure(
                            code="outbox_completion_failed",
                            message=message,
                            retryable=True,
                        )
                        for item in items
                    },
                )
            # If SQLite is unavailable the rows deliberately remain applying
            # and IndexState will requeue them after restart.
            result.failures.update({item.doc_id: message for item in items})
            result.failure_kinds.update({item.doc_id: "systemic" for item in items})
            result.systemic_failure = True
            return result

        # Optimization accounting is optional maintenance metadata, not part
        # of the Collection/SQLite durability boundary. Notify only after the
        # outbox completion commit, and never turn observer trouble into a
        # failed image operation. The operation id makes replay notifications
        # idempotent in the policy store.
        observer = self.mutation_observer
        if observer is not None:
            with suppress(Exception):
                observer(
                    operation_id,
                    changes=sum(item.action == "upsert" for item in items),
                    deletes=sum(item.action == "delete" for item in items),
                )

        result.succeeded.extend(item.doc_id for item in items)
        return result

    def _defer_claimed(self, items: Sequence[ReplayCollectionWrite]) -> None:
        by_operation: dict[str, dict[str, CollectionWriteFailure]] = {}
        for item in items:
            by_operation.setdefault(item.operation_id, {})[item.item_id] = (
                CollectionWriteFailure(
                    code="write_deferred",
                    message="A preceding systemic collection write failed.",
                    retryable=True,
                )
            )
        for operation_id, failures in by_operation.items():
            self.state.write_outbox.mark_failed(operation_id, failures)


def _contiguous_batches(
    items: Sequence[ReplayCollectionWrite],
    size: int,
) -> Iterable[list[ReplayCollectionWrite]]:
    batch: list[ReplayCollectionWrite] = []
    identity: tuple[str, str] | None = None
    for item in items:
        item_identity = (item.operation_id, item.action)
        if batch and (item_identity != identity or len(batch) >= size):
            yield batch
            batch = []
        if not batch:
            identity = item_identity
        batch.append(item)
    if batch:
        yield batch


def _supported_index_payload(
    item: ReplayCollectionWrite,
    model: str,
    dimension: int,
) -> dict[str, Any]:
    fields = item.collection_fields
    if fields is None:
        raise ValueError("Persisted upsert fields are missing.")
    if str(fields.get("model") or "") != model:
        raise ValueError("Persisted model does not match the Collection model.")
    if fields.get("metadata_text") or fields.get("metadata_text_hash"):
        raise ValueError("Persisted metadata payload is not an index/tag write.")
    image_vector = item.vectors.get("embedding")
    metadata_vector = item.vectors.get("metadata_embedding")
    if image_vector is None or len(image_vector) != dimension:
        raise ValueError("Persisted image vector has an invalid dimension.")
    if metadata_vector is None or len(metadata_vector) != dimension:
        raise ValueError("Persisted metadata vector has an invalid dimension.")
    if any(float(component) != 0.0 for component in metadata_vector):
        raise ValueError("Persisted metadata vector is not invalidated.")
    tags = fields.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Persisted tags are invalid.")
    return fields


def _record_from_fields(doc_id: str, fields: Mapping[str, Any]) -> ImageRecord:
    return ImageRecord(
        doc_id=doc_id,
        root_id=str(fields["root_id"]),
        relative_path=str(fields["relative_path"]),
        # Zvec persistence never reads the source file. Keeping this empty also
        # ensures the durable payload cannot leak an absolute user path.
        absolute_path="",
        file_name=str(fields["file_name"]),
        extension=str(fields["extension"]),
        mime_type=str(fields["mime_type"]),
        sha256=str(fields["sha256"]),
        size_bytes=int(fields["size_bytes"]),
        mtime_ns=int(fields["mtime_ns"]),
        width=int(fields["width"]),
        height=int(fields["height"]),
    )


def _require_unique_doc_ids(doc_ids: Iterable[str]) -> None:
    values = [str(doc_id) for doc_id in doc_ids]
    if not values or any(not value for value in values):
        raise ValueError("Collection writes require non-empty document ids.")
    if len(values) != len(set(values)):
        raise ValueError("Collection writes cannot contain duplicate document ids.")


def _storage_error_is_systemic(error: str) -> bool:
    normalized = str(error).casefold()
    item_markers = (
        "dimension mismatch",
        "duplicate document id",
        "invalid document",
        "invalid field",
        "item rejected",
        "unsupported persisted",
    )
    return not any(marker in normalized for marker in item_markers)


__all__ = [
    "CollectionMutationObserver",
    "CollectionWriteCoordinator",
    "CollectionWriteRecoveryError",
    "CollectionWriteResult",
    "PreparedCollectionUpsert",
]
