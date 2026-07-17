from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import zvec

from .config import ConfigurationError, ServiceConfig
from .models import ImageRecord, RankSource, SearchHit
from .tag_aliases import TagAliasDictionary
from .tag_search import TagCatalog, TagMatchMode, TagSearchPlan, matched_tags_for_result
from .tags import build_tags_filter, normalize_tags

COLLECTION_SCHEMA_VERSION = 4

METADATA_FIELDS = [
    "metadata_text",
    "metadata_text_hash",
]

OUTPUT_FIELDS = [
    "root_id",
    "relative_path",
    "file_name",
    "extension",
    "mime_type",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "width",
    "height",
    "model",
    "tags",
]


class ZvecImageRepository:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.config.validate()
        self.created = False
        self.collection, metadata = self._open_or_create()
        self.collection_uuid = str(metadata["collection_uuid"])
        self._tag_catalog: TagCatalog | None = None

    def set_tag_catalog(
        self,
        tags: Iterable[str],
        aliases: TagAliasDictionary | None = None,
    ) -> None:
        """Replace the immutable catalog used for partial tag expansion."""

        self._tag_catalog = TagCatalog(tags, aliases=aliases)

    def _schema(self) -> zvec.CollectionSchema:
        return collection_schema(self.config)

    def _open_or_create(self) -> tuple[zvec.Collection, dict]:
        path = self.config.collection_path
        meta_path = self.config.collection_meta_path
        if path.exists():
            metadata = self._validate_metadata(meta_path)
            collection = zvec.open(str(path))
            self._validate_collection_schema(collection)
            if not metadata.get("collection_uuid"):
                metadata["collection_uuid"] = str(uuid.uuid4())
                self._atomic_json_write(meta_path, metadata)
            return collection, metadata

        path.parent.mkdir(parents=True, exist_ok=True)
        collection = zvec.create_and_open(str(path), self._schema())
        self.created = True
        metadata = {
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "collection_uuid": str(uuid.uuid4()),
            "model": self.config.model,
            "mode": "independent",
            "dimension": self.config.dimension,
            "metric": self.config.metric,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._atomic_json_write(meta_path, metadata)
        except Exception:
            collection.destroy()
            raise
        return collection, metadata

    def _validate_metadata(self, meta_path: Path) -> dict:
        if not meta_path.is_file():
            raise ConfigurationError(
                f"Collection metadata is missing: {meta_path}. Refusing to open it."
            )
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "model": self.config.model,
            "mode": "independent",
            "dimension": self.config.dimension,
            "metric": self.config.metric,
        }
        mismatches = {
            key: (metadata.get(key), value)
            for key, value in expected.items()
            if metadata.get(key) != value
        }
        if mismatches:
            if metadata.get("schema_version") in {1, 2, 3}:
                raise ConfigurationError(
                    "Collection schema requires migration. Run "
                    "'image_service.py migrate-schema --dry-run' first."
                )
            raise ConfigurationError(f"Collection metadata mismatch: {mismatches}")
        return metadata

    def _validate_collection_schema(self, collection: zvec.Collection) -> None:
        fields = {field.name: field for field in collection.schema.fields}
        expected_fields = {
            "tags": zvec.DataType.ARRAY_STRING,
            "metadata_text": zvec.DataType.STRING,
            "metadata_text_hash": zvec.DataType.STRING,
        }
        invalid_fields = {
            name
            for name, data_type in expected_fields.items()
            if name not in fields or fields[name].data_type != data_type
        }
        vectors = {vector.name: vector for vector in collection.schema.vectors}
        invalid_vectors = {
            name
            for name in ("embedding", "metadata_embedding")
            if name not in vectors
            or vectors[name].data_type != zvec.DataType.VECTOR_FP32
            or vectors[name].dimension != self.config.dimension
        }
        if invalid_fields or invalid_vectors:
            raise ConfigurationError(
                "Collection schema is missing required metadata fields or vectors. Run "
                "'image_service.py migrate-schema --dry-run' first."
            )

    @staticmethod
    def _atomic_json_write(path: Path, data: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def fetch_vector(self, doc_id: str) -> list[float] | None:
        documents = self.collection.fetch(
            doc_id, output_fields=["sha256"], include_vector=True
        )
        document = documents.get(doc_id)
        if not document:
            return None
        vector = document.vectors.get("embedding")
        return list(vector) if vector is not None else None

    def fetch_metadata(self, doc_id: str) -> dict[str, object] | None:
        """Return the persisted metadata text, hash, and vector for one document."""

        documents = self.collection.fetch(
            doc_id,
            output_fields=METADATA_FIELDS,
            include_vector=True,
        )
        document = documents.get(doc_id)
        if not document:
            return None
        vector = document.vectors.get("metadata_embedding")
        return {
            "metadata_text": str(document.fields.get("metadata_text", "")),
            "metadata_text_hash": str(document.fields.get("metadata_text_hash", "")),
            "metadata_embedding": list(vector) if vector is not None else [],
        }

    def upsert_metadata_embedding(
        self,
        doc_id: str,
        metadata_text: str,
        metadata_text_hash: str,
        vector: list[float],
    ) -> None:
        """Update one metadata vector without changing image fields or tags."""

        normalized_text = str(metadata_text).strip()
        normalized_hash = str(metadata_text_hash).strip().lower()
        if not normalized_text:
            raise ValueError("metadata_text must not be empty.")
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("metadata_text_hash must be a SHA-256 hex digest.")
        if len(vector) != self.config.dimension:
            raise ValueError(
                "metadata_embedding dimension mismatch: "
                f"{len(vector)} != {self.config.dimension}."
            )

        documents = self.collection.fetch(
            doc_id,
            output_fields=[*OUTPUT_FIELDS, *METADATA_FIELDS],
            include_vector=True,
        )
        document = documents.get(doc_id)
        if not document:
            raise ConfigurationError(f"Unknown Collection document: {doc_id}")
        image_vector = document.vectors.get("embedding")
        if image_vector is None or len(image_vector) != self.config.dimension:
            raise ConfigurationError(
                f"Missing or invalid image vector for document {doc_id}."
            )

        fields = dict(document.fields)
        fields["metadata_text"] = normalized_text
        fields["metadata_text_hash"] = normalized_hash
        status = self.collection.upsert(
            zvec.Doc(
                id=doc_id,
                fields=fields,
                vectors={
                    "embedding": list(image_vector),
                    "metadata_embedding": list(vector),
                },
            )
        )
        if not status.ok():
            raise ConfigurationError(
                f"Metadata embedding upsert failed for document {doc_id}: {status}"
            )

    def contains(self, doc_id: str) -> bool:
        return bool(
            self.collection.fetch(doc_id, output_fields=[], include_vector=False).get(
                doc_id
            )
        )

    def upsert_records(
        self,
        records: list[ImageRecord],
        vector: list[float],
        tags: Iterable[str] = (),
    ) -> tuple[list[str], dict[str, str]]:
        normalized_tags = list(normalize_tags(tags))
        documents = [
            self._to_doc(record, vector, normalized_tags) for record in records
        ]
        statuses = self.collection.upsert(documents)
        if not isinstance(statuses, list):
            statuses = [statuses]

        succeeded: list[str] = []
        failed: dict[str, str] = {}
        for record, status in zip(records, statuses, strict=True):
            if status.ok():
                succeeded.append(record.doc_id)
            else:
                failed[record.doc_id] = str(status)
        return succeeded, failed

    def _to_doc(
        self, record: ImageRecord, vector: list[float], tags: list[str]
    ) -> zvec.Doc:
        return zvec.Doc(
            id=record.doc_id,
            fields={
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
                "model": self.config.model,
                "tags": tags,
                # Image or accepted-tag changes make any previous metadata
                # embedding stale. Empty values explicitly mark it for backfill.
                "metadata_text": "",
                "metadata_text_hash": "",
            },
            vectors={
                "embedding": vector,
                "metadata_embedding": empty_metadata_embedding(self.config.dimension),
            },
        )

    def query(
        self,
        vector: list[float],
        top_k: int,
        tags: Iterable[str] = (),
        tag_mode: str = "all",
        rank_source: RankSource = "text",
    ) -> list[SearchHit]:
        tag_plan: TagSearchPlan | None = None
        if self._tag_catalog is None:
            tag_filter = build_tags_filter(tags, tag_mode)
        else:
            tag_plan = self._tag_catalog.resolve(
                tags, mode=cast(TagMatchMode, tag_mode)
            )
            if tag_plan.matches_nothing:
                return []
            tag_filter = tag_plan.zvec_filter
        documents = self.collection.query(
            queries=zvec.Query(field_name="embedding", vector=vector),
            topk=top_k,
            filter=tag_filter,
            include_vector=False,
            output_fields=OUTPUT_FIELDS,
        )
        hits = []
        for index, document in enumerate(documents, start=1):
            # zvec COSINE reports cosine distance (1 - cosine similarity), not a
            # relevance score. A perfect match is 0 and lower values rank first.
            raw_score = float(document.score) if document.score is not None else 0.0
            fields = dict(document.fields)
            hits.append(
                SearchHit(
                    doc_id=document.id,
                    distance=raw_score,
                    raw_score=raw_score,
                    fields=fields,
                    rank=index,
                    rank_source=rank_source,
                    matched_tags=(
                        matched_tags_for_result(fields.get("tags"), tag_plan)
                        if tag_plan is not None
                        else ()
                    ),
                )
            )
        return hits

    def query_metadata(
        self,
        vector: list[float],
        top_k: int,
        tags: Iterable[str] = (),
        tag_mode: str = "all",
        rank_source: RankSource = "metadata",
    ) -> list[SearchHit]:
        """Query only completed metadata embeddings, with optional tag filtering."""

        tag_plan: TagSearchPlan | None = None
        if self._tag_catalog is None:
            tag_filter = build_tags_filter(tags, tag_mode)
        else:
            tag_plan = self._tag_catalog.resolve(
                tags, mode=cast(TagMatchMode, tag_mode)
            )
            if tag_plan.matches_nothing:
                return []
            tag_filter = tag_plan.zvec_filter
        completed_filter = "metadata_text_hash != ''"
        query_filter = (
            f"({completed_filter}) AND ({tag_filter})"
            if tag_filter is not None
            else completed_filter
        )
        documents = self.collection.query(
            queries=zvec.Query(field_name="metadata_embedding", vector=vector),
            topk=top_k,
            filter=query_filter,
            include_vector=False,
            output_fields=OUTPUT_FIELDS,
        )
        hits = []
        for index, document in enumerate(documents, start=1):
            raw_score = float(document.score) if document.score is not None else 0.0
            fields = dict(document.fields)
            hits.append(
                SearchHit(
                    doc_id=document.id,
                    distance=raw_score,
                    raw_score=raw_score,
                    fields=fields,
                    rank=index,
                    rank_source=rank_source,
                    matched_tags=(
                        matched_tags_for_result(fields.get("tags"), tag_plan)
                        if tag_plan is not None
                        else ()
                    ),
                )
            )
        return hits

    def delete(self, doc_ids: Iterable[str]) -> tuple[list[str], dict[str, str]]:
        ids = list(doc_ids)
        if not ids:
            return [], {}
        statuses = self.collection.delete(ids)
        if not isinstance(statuses, list):
            statuses = [statuses]
        succeeded: list[str] = []
        failed: dict[str, str] = {}
        for doc_id, status in zip(ids, statuses, strict=True):
            if status.ok():
                succeeded.append(doc_id)
            else:
                failed[doc_id] = str(status)
        return succeeded, failed

    def optimize(self) -> None:
        self.collection.optimize()

    @property
    def stats(self):
        return self.collection.stats

    @property
    def doc_count(self) -> int:
        return int(self.collection.stats.doc_count)


def collection_schema(config: ServiceConfig) -> zvec.CollectionSchema:
    return zvec.CollectionSchema(
        name="image_collection",
        fields=[
            zvec.FieldSchema(
                "root_id",
                zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("relative_path", zvec.DataType.STRING),
            zvec.FieldSchema("file_name", zvec.DataType.STRING),
            zvec.FieldSchema(
                "extension",
                zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("mime_type", zvec.DataType.STRING),
            zvec.FieldSchema(
                "sha256",
                zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("size_bytes", zvec.DataType.INT64),
            zvec.FieldSchema("mtime_ns", zvec.DataType.INT64),
            zvec.FieldSchema("width", zvec.DataType.INT32),
            zvec.FieldSchema("height", zvec.DataType.INT32),
            zvec.FieldSchema("model", zvec.DataType.STRING),
            zvec.FieldSchema(
                "tags",
                zvec.DataType.ARRAY_STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("metadata_text", zvec.DataType.STRING),
            zvec.FieldSchema(
                "metadata_text_hash",
                zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
        ],
        vectors=[
            zvec.VectorSchema(
                "embedding",
                zvec.DataType.VECTOR_FP32,
                dimension=config.dimension,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
            zvec.VectorSchema(
                "metadata_embedding",
                zvec.DataType.VECTOR_FP32,
                dimension=config.dimension,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        ],
    )


def empty_metadata_embedding(dimension: int) -> list[float]:
    """Return the required zero-vector placeholder for pending metadata."""

    if dimension < 1:
        raise ValueError("Embedding dimension must be positive.")
    return [0.0] * dimension
