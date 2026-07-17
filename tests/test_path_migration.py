from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import zvec
from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.models import ImageRecord
from image_vector_service.path_migration import migrate_path_schema, migrate_schema
from image_vector_service.source_resolver import SourcePathResolver
from image_vector_service.state import IndexState
from image_vector_service.zvec_repository import (
    COLLECTION_SCHEMA_VERSION,
    ZvecImageRepository,
    collection_schema,
)


class PathMigrationTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="zvec_path_migration_test_"))
        self.config = ServiceConfig(workspace=self.workspace)
        self.source_root = self.workspace / "old-images"
        self.source_root.mkdir()
        self.image_path = self.source_root / "nested" / "red.png"
        self.image_path.parent.mkdir()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(self.image_path)
        self.collection_uuid = str(uuid.uuid4())
        self.vector = [1.0, 0.0, 0.0, *([0.0] * 1021)]
        self._create_v1_collection()
        self._create_v1_state()

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _create_v1_collection(self):
        schema = zvec.CollectionSchema(
            name="image_collection",
            fields=[
                zvec.FieldSchema("root_path", zvec.DataType.STRING),
                zvec.FieldSchema("relative_path", zvec.DataType.STRING),
                zvec.FieldSchema("absolute_path", zvec.DataType.STRING),
                zvec.FieldSchema("file_name", zvec.DataType.STRING),
                zvec.FieldSchema("extension", zvec.DataType.STRING),
                zvec.FieldSchema("mime_type", zvec.DataType.STRING),
                zvec.FieldSchema("sha256", zvec.DataType.STRING),
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
                    dimension=1024,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                )
            ],
        )
        collection = zvec.create_and_open(str(self.config.collection_path), schema)
        self.old_doc_id = hashlib.sha256(
            str(self.image_path.resolve()).lower().encode()
        ).hexdigest()
        stat = self.image_path.stat()
        fields = {
            "root_path": str(self.source_root.resolve()).lower(),
            "relative_path": str(self.image_path.relative_to(self.source_root)),
            "absolute_path": str(self.image_path.resolve()),
            "file_name": self.image_path.name,
            "extension": "png",
            "mime_type": "image/png",
            "sha256": "a" * 64,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "width": 8,
            "height": 8,
            "model": "qwen3-vl-embedding",
        }
        status = collection.upsert(
            zvec.Doc(
                id=self.old_doc_id,
                fields=fields,
                vectors={"embedding": self.vector},
            )
        )
        self.assertTrue(status.ok())
        collection.optimize()
        del collection
        metadata = {
            "schema_version": 1,
            "collection_uuid": self.collection_uuid,
            "model": "qwen3-vl-embedding",
            "mode": "independent",
            "dimension": 1024,
            "metric": "COSINE",
        }
        self.config.collection_meta_path.write_text(
            json.dumps(metadata), encoding="utf-8"
        )

    def _create_v1_state(self):
        connection = sqlite3.connect(self.config.state_path)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entries (
                doc_id TEXT PRIMARY KEY, root_path TEXT NOT NULL,
                relative_path TEXT NOT NULL, absolute_path TEXT NOT NULL,
                file_name TEXT NOT NULL, extension TEXT NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                width INTEGER NOT NULL, height INTEGER NOT NULL
            );
            CREATE TABLE roots (root_path TEXT PRIMARY KEY, recursive INTEGER NOT NULL);
            CREATE TABLE embedding_cache (
                cache_key TEXT PRIMARY KEY, modality TEXT NOT NULL,
                embedding BLOB NOT NULL, dimension INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [
                ("state_schema_version", "1"),
                ("collection_uuid", self.collection_uuid),
            ],
        )
        stat = self.image_path.stat()
        root = str(self.source_root.resolve()).lower()
        connection.execute("INSERT INTO roots VALUES(?, 1)", (root,))
        connection.execute(
            "INSERT INTO entries VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.old_doc_id,
                root,
                str(self.image_path.relative_to(self.source_root)),
                str(self.image_path.resolve()),
                self.image_path.name,
                "png",
                "image/png",
                "a" * 64,
                stat.st_size,
                stat.st_mtime_ns,
                8,
                8,
            ),
        )
        connection.commit()
        connection.close()

    def test_migration_preserves_vector_and_supports_rebind(self):
        preview = migrate_path_schema(self.config, dry_run=True)
        self.assertEqual(preview["documents"], 1)
        self.assertEqual(preview["api_requests"], 0)

        report = migrate_path_schema(self.config)
        self.assertEqual(report["status"], "migrated")
        self.assertEqual(report["api_requests"], 0)
        metadata = json.loads(self.config.collection_meta_path.read_text("utf-8"))
        self.assertEqual(metadata["schema_version"], COLLECTION_SCHEMA_VERSION)

        repository = ZvecImageRepository(self.config)
        state = IndexState(self.config.state_path)
        try:
            roots = state.list_roots()
            self.assertEqual(len(roots), 1)
            entry = state.entries_for_root(roots[0]["root_id"])[0]
            self.assertNotEqual(entry["doc_id"], self.old_doc_id)
            self.assertNotIn("absolute_path", entry)
            migrated_vector = repository.fetch_vector(entry["doc_id"])
            self.assertEqual(migrated_vector, self.vector)
            self.assertEqual(repository.doc_count, 1)
            self.assertEqual(state.count(), 1)
            schema_names = {field.name for field in repository.collection.schema.fields}
            self.assertNotIn("absolute_path", schema_names)
            self.assertNotIn("root_path", schema_names)
            self.assertIn("tags", schema_names)
            self.assertIn("metadata_text", schema_names)
            self.assertIn("metadata_text_hash", schema_names)
            vector_names = {
                vector.name for vector in repository.collection.schema.vectors
            }
            self.assertEqual(
                vector_names,
                {"embedding", "metadata_embedding"},
            )
            metadata_values = repository.fetch_metadata(entry["doc_id"])
            self.assertIsNotNone(metadata_values)
            assert metadata_values is not None
            self.assertEqual(metadata_values["metadata_text"], "")
            self.assertEqual(metadata_values["metadata_text_hash"], "")
            self.assertEqual(
                metadata_values["metadata_embedding"],
                [0.0] * self.config.dimension,
            )
            self.assertEqual(entry["tags"], [])

            new_root = self.workspace / "moved-images"
            shutil.move(str(self.source_root), new_root)
            rebound = state.rebind_root(roots[0]["root_id"], str(new_root))
            self.assertEqual(rebound["current_path"], str(new_root.resolve()).lower())
            resolved = SourcePathResolver(state).resolve_fields(entry)
            self.assertTrue(resolved.is_file())
        finally:
            state.close()
            del repository


class TagSchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="zvec_tag_migration_test_"))
        self.config = ServiceConfig(workspace=self.workspace)
        self.source_root = self.workspace / "images"
        self.source_root.mkdir()
        self.image_path = self.source_root / "red.png"
        Image.new("RGB", (8, 8), (255, 0, 0)).save(self.image_path)
        self.collection_uuid = str(uuid.uuid4())
        self.root_id = str(uuid.uuid4())
        self.doc_id = "v2-document"
        self.vector = [1.0, 0.0, 0.0, *([0.0] * 1021)]
        self._create_v2_collection()
        self._create_v2_state()

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _create_v2_collection(self):
        schema = zvec.CollectionSchema(
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
            ],
            vectors=[
                zvec.VectorSchema(
                    "embedding",
                    zvec.DataType.VECTOR_FP32,
                    dimension=1024,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                )
            ],
        )
        collection = zvec.create_and_open(str(self.config.collection_path), schema)
        stat = self.image_path.stat()
        status = collection.upsert(
            zvec.Doc(
                id=self.doc_id,
                fields={
                    "root_id": self.root_id,
                    "relative_path": self.image_path.name,
                    "file_name": self.image_path.name,
                    "extension": "png",
                    "mime_type": "image/png",
                    "sha256": "b" * 64,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "width": 8,
                    "height": 8,
                    "model": "qwen3-vl-embedding",
                },
                vectors={"embedding": self.vector},
            )
        )
        self.assertTrue(status.ok())
        collection.optimize()
        del collection
        self.config.collection_meta_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "collection_uuid": self.collection_uuid,
                    "model": "qwen3-vl-embedding",
                    "mode": "independent",
                    "dimension": 1024,
                    "metric": "COSINE",
                }
            ),
            encoding="utf-8",
        )

    def _create_v2_state(self):
        connection = sqlite3.connect(self.config.state_path)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entries (
                doc_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
                relative_path TEXT NOT NULL, file_name TEXT NOT NULL,
                extension TEXT NOT NULL, mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL
            );
            CREATE TABLE roots (
                root_id TEXT PRIMARY KEY, current_path TEXT NOT NULL UNIQUE,
                recursive INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [
                ("state_schema_version", "2"),
                ("collection_uuid", self.collection_uuid),
            ],
        )
        stat = self.image_path.stat()
        connection.execute(
            "INSERT INTO roots VALUES(?, ?, 1)",
            (self.root_id, str(self.source_root.resolve()).lower()),
        )
        connection.execute(
            "INSERT INTO entries VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.doc_id,
                self.root_id,
                self.image_path.name,
                self.image_path.name,
                "png",
                "image/png",
                "b" * 64,
                stat.st_size,
                stat.st_mtime_ns,
                8,
                8,
            ),
        )
        connection.commit()
        connection.close()

    def test_v2_migration_preserves_ids_vectors_and_adds_tags(self):
        preview = migrate_schema(self.config, dry_run=True)
        self.assertEqual(preview["from_schema"], 2)
        self.assertEqual(preview["to_schema"], COLLECTION_SCHEMA_VERSION)
        self.assertEqual(preview["api_requests"], 0)

        report = migrate_schema(self.config)
        self.assertEqual(report["status"], "migrated")
        repository = ZvecImageRepository(self.config)
        state = IndexState(self.config.state_path)
        try:
            self.assertEqual(repository.doc_count, 1)
            self.assertEqual(state.count(), 1)
            document = repository.collection.fetch(
                self.doc_id,
                output_fields=["tags"],
                include_vector=True,
            )[self.doc_id]
            self.assertEqual(document.fields["tags"], [])
            self.assertEqual(document.vectors["embedding"], self.vector)
            entry = state.get(self.doc_id)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["tags"], [])
            self.assertEqual(state.list_roots()[0]["tags"], [])
            metadata_values = repository.fetch_metadata(self.doc_id)
            self.assertIsNotNone(metadata_values)
            assert metadata_values is not None
            self.assertEqual(metadata_values["metadata_text"], "")
            self.assertEqual(metadata_values["metadata_text_hash"], "")
            self.assertEqual(
                metadata_values["metadata_embedding"],
                [0.0] * self.config.dimension,
            )
        finally:
            state.close()
            del repository


class MetadataSchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="zvec_metadata_migration_test_"))
        self.config = ServiceConfig(workspace=self.workspace)
        self.source_root = self.workspace / "images"
        self.source_root.mkdir()
        self.image_path = self.source_root / "character.png"
        Image.new("RGB", (8, 8), (0, 0, 255)).save(self.image_path)
        self.collection_uuid = str(uuid.uuid4())
        self.root_id = str(uuid.uuid4())
        self.doc_id = "v3-document"
        self.vector = [1.0, 0.0, 0.0, *([0.0] * 1021)]
        self.tags = ["人工标签", "角色名"]
        self._create_v3_collection()
        self._create_current_state()

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _create_v3_collection(self):
        current_schema = collection_schema(self.config)
        schema = zvec.CollectionSchema(
            name="image_collection",
            fields=[
                field
                for field in current_schema.fields
                if field.name not in {"metadata_text", "metadata_text_hash"}
            ],
            vectors=[
                vector
                for vector in current_schema.vectors
                if vector.name != "metadata_embedding"
            ],
        )
        collection = zvec.create_and_open(str(self.config.collection_path), schema)
        stat = self.image_path.stat()
        status = collection.upsert(
            zvec.Doc(
                id=self.doc_id,
                fields={
                    "root_id": self.root_id,
                    "relative_path": self.image_path.name,
                    "file_name": self.image_path.name,
                    "extension": "png",
                    "mime_type": "image/png",
                    "sha256": "c" * 64,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "width": 8,
                    "height": 8,
                    "model": self.config.model,
                    "tags": self.tags,
                },
                vectors={"embedding": self.vector},
            )
        )
        self.assertTrue(status.ok())
        collection.optimize()
        del collection
        self.config.collection_meta_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "collection_uuid": self.collection_uuid,
                    "model": self.config.model,
                    "mode": "independent",
                    "dimension": self.config.dimension,
                    "metric": self.config.metric,
                }
            ),
            encoding="utf-8",
        )

    def _create_current_state(self):
        stat = self.image_path.stat()
        state = IndexState(self.config.state_path)
        try:
            state.ensure_collection_uuid(self.collection_uuid)
            state.record_root(self.root_id, str(self.source_root), True)
            state.set_many(
                [
                    {
                        "doc_id": self.doc_id,
                        "root_id": self.root_id,
                        "relative_path": self.image_path.name,
                        "file_name": self.image_path.name,
                        "extension": "png",
                        "mime_type": "image/png",
                        "sha256": "c" * 64,
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "width": 8,
                        "height": 8,
                        "tags": ["人工标签"],
                        "folder_tags": ["作品名"],
                        "accepted_auto_tags": ["角色名"],
                    }
                ]
            )
            state.checkpoint()
        finally:
            state.close()

    def test_v3_migration_preserves_documents_state_image_vectors_and_tags(self):
        state_digest = hashlib.sha256(self.config.state_path.read_bytes()).hexdigest()

        preview = migrate_schema(self.config, dry_run=True)
        self.assertEqual(preview["from_schema"], 3)
        self.assertEqual(preview["to_schema"], COLLECTION_SCHEMA_VERSION)
        self.assertEqual(preview["documents"], 1)
        self.assertEqual(preview["api_requests"], 0)

        report = migrate_schema(self.config)
        self.assertEqual(report["status"], "migrated")
        self.assertEqual(report["from_schema"], 3)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(
            hashlib.sha256(self.config.state_path.read_bytes()).hexdigest(),
            state_digest,
        )

        repository = ZvecImageRepository(self.config)
        state = IndexState(self.config.state_path)
        try:
            self.assertEqual(repository.doc_count, 1)
            self.assertEqual(state.count(), 1)
            document = repository.collection.fetch(
                self.doc_id,
                output_fields=["tags", "metadata_text", "metadata_text_hash"],
                include_vector=True,
            )[self.doc_id]
            self.assertEqual(document.fields["tags"], self.tags)
            self.assertEqual(document.fields["metadata_text"], "")
            self.assertEqual(document.fields["metadata_text_hash"], "")
            self.assertEqual(document.vectors["embedding"], self.vector)
            self.assertEqual(
                document.vectors["metadata_embedding"],
                [0.0] * self.config.dimension,
            )
            self.assertEqual(
                repository.query_metadata(self.vector, 10),
                [],
            )
            self.assertEqual(
                state.get(self.doc_id)["effective_tags"],
                ["人工标签", "作品名", "角色名"],
            )

            metadata_text = "角色：角色名；作品：作品名；标签：人工标签"
            metadata_hash = hashlib.sha256(metadata_text.encode("utf-8")).hexdigest()
            metadata_vector = [0.0, 1.0, *([0.0] * 1022)]
            repository.upsert_metadata_embedding(
                self.doc_id,
                metadata_text,
                metadata_hash,
                metadata_vector,
            )
            updated = repository.collection.fetch(
                self.doc_id,
                output_fields=["tags", "metadata_text", "metadata_text_hash"],
                include_vector=True,
            )[self.doc_id]
            self.assertEqual(updated.fields["tags"], self.tags)
            self.assertEqual(updated.fields["metadata_text"], metadata_text)
            self.assertEqual(updated.fields["metadata_text_hash"], metadata_hash)
            self.assertEqual(updated.vectors["embedding"], self.vector)
            self.assertEqual(
                updated.vectors["metadata_embedding"],
                metadata_vector,
            )
            metadata_hits = repository.query_metadata(
                metadata_vector,
                10,
                tags=["角色名"],
            )
            self.assertEqual([hit.doc_id for hit in metadata_hits], [self.doc_id])
            self.assertEqual(metadata_hits[0].rank_source, "metadata")
            repository.set_tag_catalog(self.tags)
            fuzzy_metadata_hits = repository.query_metadata(
                metadata_vector,
                10,
                tags=["角色"],
            )
            self.assertEqual(
                [hit.doc_id for hit in fuzzy_metadata_hits],
                [self.doc_id],
            )
            self.assertEqual(
                fuzzy_metadata_hits[0].matched_tags,
                ("角色名",),
            )

            stat = self.image_path.stat()
            succeeded, failures = repository.upsert_records(
                [
                    ImageRecord(
                        doc_id=self.doc_id,
                        root_id=self.root_id,
                        relative_path=self.image_path.name,
                        absolute_path=str(self.image_path),
                        file_name=self.image_path.name,
                        extension="png",
                        mime_type="image/png",
                        sha256="c" * 64,
                        size_bytes=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        width=8,
                        height=8,
                    )
                ],
                self.vector,
                tags=["更新标签"],
            )
            self.assertEqual(succeeded, [self.doc_id])
            self.assertEqual(failures, {})
            invalidated = repository.fetch_metadata(self.doc_id)
            self.assertIsNotNone(invalidated)
            assert invalidated is not None
            self.assertEqual(invalidated["metadata_text"], "")
            self.assertEqual(invalidated["metadata_text_hash"], "")
            self.assertEqual(
                invalidated["metadata_embedding"],
                [0.0] * self.config.dimension,
            )
        finally:
            state.close()
            del repository

        second_report = migrate_schema(self.config)
        self.assertEqual(
            second_report["status"],
            f"already_v{COLLECTION_SCHEMA_VERSION}",
        )
        self.assertEqual(second_report["api_requests"], 0)
