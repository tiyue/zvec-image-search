from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import zvec

from .config import ConfigurationError, ServiceConfig
from .models import ImageRecord, SearchHit


OUTPUT_FIELDS = [
    "root_path",
    "relative_path",
    "absolute_path",
    "file_name",
    "extension",
    "mime_type",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "width",
    "height",
    "model",
]


class ZvecImageRepository:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.config.validate()
        self.collection = self._open_or_create()

    def _schema(self) -> zvec.CollectionSchema:
        return zvec.CollectionSchema(
            name="image_collection",
            fields=[
                zvec.FieldSchema(
                    "root_path",
                    zvec.DataType.STRING,
                    index_param=zvec.InvertIndexParam(),
                ),
                zvec.FieldSchema("relative_path", zvec.DataType.STRING),
                zvec.FieldSchema("absolute_path", zvec.DataType.STRING),
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
            ],
            vectors=[
                zvec.VectorSchema(
                    "embedding",
                    zvec.DataType.VECTOR_FP32,
                    dimension=self.config.dimension,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                )
            ],
        )

    def _open_or_create(self) -> zvec.Collection:
        path = self.config.collection_path
        meta_path = self.config.collection_meta_path
        if path.exists():
            self._validate_metadata(meta_path)
            return zvec.open(str(path))

        path.parent.mkdir(parents=True, exist_ok=True)
        collection = zvec.create_and_open(str(path), self._schema())
        metadata = {
            "schema_version": 1,
            "model": self.config.model,
            "mode": "independent",
            "dimension": self.config.dimension,
            "metric": self.config.metric,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_json_write(meta_path, metadata)
        return collection

    def _validate_metadata(self, meta_path: Path) -> None:
        if not meta_path.is_file():
            raise ConfigurationError(
                f"Collection metadata is missing: {meta_path}. Refusing to open it."
            )
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": 1,
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
            raise ConfigurationError(f"Collection metadata mismatch: {mismatches}")

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

    def upsert_records(
        self, records: list[ImageRecord], vector: list[float]
    ) -> tuple[list[str], dict[str, str]]:
        documents = [self._to_doc(record, vector) for record in records]
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

    def _to_doc(self, record: ImageRecord, vector: list[float]) -> zvec.Doc:
        return zvec.Doc(
            id=record.doc_id,
            fields={
                "root_path": record.root_path,
                "relative_path": record.relative_path,
                "absolute_path": record.absolute_path,
                "file_name": record.file_name,
                "extension": record.extension,
                "mime_type": record.mime_type,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "mtime_ns": record.mtime_ns,
                "width": record.width,
                "height": record.height,
                "model": self.config.model,
            },
            vectors={"embedding": vector},
        )

    def query(self, vector: list[float], top_k: int) -> list[SearchHit]:
        documents = self.collection.query(
            queries=zvec.Query(field_name="embedding", vector=vector),
            topk=top_k,
            include_vector=False,
            output_fields=OUTPUT_FIELDS,
        )
        return [
            SearchHit(
                doc_id=document.id,
                distance=float(document.score or 0.0),
                fields=dict(document.fields),
                rank=index,
            )
            for index, document in enumerate(documents, start=1)
        ]

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
