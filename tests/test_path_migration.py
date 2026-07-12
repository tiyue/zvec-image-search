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
from image_vector_service.path_migration import migrate_path_schema
from image_vector_service.source_resolver import SourcePathResolver
from image_vector_service.state import IndexState
from image_vector_service.zvec_repository import ZvecImageRepository


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
        self.assertEqual(metadata["schema_version"], 2)

        repository = ZvecImageRepository(self.config)
        state = IndexState(self.config.state_path)
        try:
            roots = state.list_roots()
            self.assertEqual(len(roots), 1)
            entry = state.entries_for_root(roots[0]["root_id"])[0]
            self.assertNotEqual(entry["doc_id"], self.old_doc_id)
            self.assertNotIn("absolute_path", entry)
            migrated_vector = repository.fetch_vector(entry["doc_id"])
            self.assertEqual(len(migrated_vector or []), 1024)
            schema_names = {field.name for field in repository.collection.schema.fields}
            self.assertNotIn("absolute_path", schema_names)
            self.assertNotIn("root_path", schema_names)

            new_root = self.workspace / "moved-images"
            shutil.move(str(self.source_root), new_root)
            rebound = state.rebind_root(roots[0]["root_id"], str(new_root))
            self.assertEqual(rebound["current_path"], str(new_root.resolve()).lower())
            resolved = SourcePathResolver(state).resolve_fields(entry)
            self.assertTrue(resolved.is_file())
        finally:
            state.close()
            del repository
