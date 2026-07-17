from __future__ import annotations

import gc
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import zvec
from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.image_scanner import file_sha256
from image_vector_service.logical_paths import logical_document_id
from image_vector_service.state import IndexState
from image_vector_service.zvec_repository import (
    COLLECTION_SCHEMA_VERSION,
    collection_schema,
    empty_metadata_embedding,
)
from tests.search_quality import evaluate as quality_evaluate
from tests.search_quality import multicollection_fixture as fixture_builder


class MultiCollectionFixtureTest(unittest.TestCase):
    def test_direct_script_help_uses_this_checkout(self) -> None:
        script = Path(fixture_builder.__file__).resolve()
        with tempfile.TemporaryDirectory() as raw:
            completed = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=raw,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--source-workspace", completed.stdout)
        imported = Path(
            sys.modules[fixture_builder.ServiceConfig.__module__].__file__ or ""
        ).resolve()
        self.assertTrue(imported.is_relative_to(fixture_builder.REPOSITORY_ROOT))

    def _source_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        image_root = root / "images"
        workspace = root / "source-workspace"
        workspace.mkdir()
        collection_uuid = str(uuid.uuid4())
        source_root_id = str(uuid.uuid4())
        records: list[tuple[str, Path, list[float]]] = []
        for index, (top, name, color) in enumerate(
            (
                ("alpha", "one.png", (220, 20, 20)),
                ("alpha", "two.png", (180, 40, 40)),
                ("beta", "three.png", (20, 20, 220)),
                ("beta", "four.png", (40, 40, 180)),
            )
        ):
            path = image_root / top / name
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (8, 8), color).save(path)
            vector = [0.0] * fixture_builder.EXPECTED_DIMENSION
            vector[index] = 1.0
            records.append((f"{top}/{name}", path, vector))

        config = ServiceConfig(workspace=workspace)
        collection = zvec.create_and_open(
            str(config.collection_path), collection_schema(config)
        )
        state = IndexState(config.state_path)
        try:
            state.ensure_collection_uuid(collection_uuid)
            state.record_root(source_root_id, str(image_root), True)
            state_entries = []
            documents = []
            for relative_path, path, vector in records:
                stat = path.stat()
                digest = file_sha256(path)
                doc_id = logical_document_id(source_root_id, relative_path)
                fields = {
                    "root_id": source_root_id,
                    "relative_path": relative_path,
                    "file_name": path.name,
                    "extension": "png",
                    "mime_type": "image/png",
                    "sha256": digest,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "width": 8,
                    "height": 8,
                    "model": config.model,
                    "tags": ["fixture"],
                    "metadata_text": "",
                    "metadata_text_hash": "",
                }
                documents.append(
                    zvec.Doc(
                        id=doc_id,
                        fields=fields,
                        vectors={
                            "embedding": vector,
                            "metadata_embedding": empty_metadata_embedding(
                                config.dimension
                            ),
                        },
                    )
                )
                state_entries.append(
                    {
                        "doc_id": doc_id,
                        **{
                            key: value
                            for key, value in fields.items()
                            if key
                            not in {
                                "model",
                            }
                        },
                    }
                )
            statuses = collection.upsert(documents)
            self.assertTrue(all(status.ok() for status in statuses))
            collection.flush()
            state.set_many(state_entries)
            state.set_cached_vector(
                fixture_builder._cache_key(
                    collection_uuid,
                    config.model,
                    config.dimension,
                    "text",
                    "fixture query",
                ),
                "text",
                records[0][2],
            )
            state.checkpoint()
        finally:
            state.close()
            del collection
            gc.collect()
        config.collection_meta_path.write_text(
            json.dumps(
                {
                    "schema_version": COLLECTION_SCHEMA_VERSION,
                    "collection_uuid": collection_uuid,
                    "model": config.model,
                    "mode": "independent",
                    "dimension": config.dimension,
                    "metric": config.metric,
                    "created_at": "2026-07-13T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        config.lock_path.write_bytes(b"0")

        dataset_path = root / "dataset.local.json"
        dataset_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "split-source",
                    "description": "pending fixture",
                    "items": [
                        {
                            "id": "text-001",
                            "query_type": "text",
                            "mode": "text",
                            "query": {"text": "fixture query"},
                            "library_scope": {
                                "mode": "single",
                                "library_ids": ["source-library"],
                            },
                            "relevant_images": [],
                            "suggested_relevant_images": [
                                {"image_id": "source-library:alpha/one.png"}
                            ],
                            "annotation": {
                                "status": "pending",
                                "annotator": None,
                                "annotated_at": None,
                                "notes": "pending",
                            },
                        },
                        {
                            "id": "image-001",
                            "query_type": "image",
                            "mode": "image",
                            "query": {"image": "beta/three.png"},
                            "library_scope": {
                                "mode": "single",
                                "library_ids": ["source-library"],
                            },
                            "relevant_images": [],
                            "suggested_relevant_images": [
                                {"image_id": "source-library:beta/four.png"}
                            ],
                            "annotation": {
                                "status": "pending",
                                "annotator": None,
                                "annotated_at": None,
                                "notes": "pending",
                            },
                        },
                        {
                            "id": "combined-001",
                            "query_type": "combined",
                            "mode": "combined",
                            "query": {
                                "text": "fixture query",
                                "image": "alpha/two.png",
                            },
                            "library_scope": {
                                "mode": "single",
                                "library_ids": ["source-library"],
                            },
                            "relevant_images": [],
                            "suggested_relevant_images": [
                                {"image_id": "source-library:alpha/one.png"},
                                {"image_id": "source-library:beta/three.png"},
                            ],
                            "annotation": {
                                "status": "pending",
                                "annotator": None,
                                "annotated_at": None,
                                "notes": "pending",
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return workspace, image_root, dataset_path

    def test_builds_two_lineage_collections_and_runs_zero_api_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, image_root, dataset = self._source_fixture(root)
            before = fixture_builder._tree_fingerprint(source)
            output = root / "generated"
            libraries = root / "libraries-multicollection.local.json"
            transformed_dataset = root / "dataset-multicollection.local.json"
            review_run_path = root / "run-multicollection.local.json"

            report = fixture_builder.build_fixture(
                source_workspace=source,
                image_root=image_root,
                dataset_path=dataset,
                output_root=output,
                libraries_output=libraries,
                dataset_output=transformed_dataset,
                review_run_output=review_run_path,
            )

            self.assertEqual(fixture_builder._tree_fingerprint(source), before)
            self.assertEqual(report["source_document_count"], 4)
            self.assertEqual(report["private_images_copied"], 0)
            self.assertEqual(report["private_images_verified"], 4)
            self.assertEqual(
                report["zero_api_smoke"],
                {
                    "cases": 3,
                    "candidate_sets": 6,
                    "federated_cases": 3,
                    "federated_results": 10,
                    "dual_library_cases": 3,
                    "confidence_order_verified_cases": 3,
                    "ranking_modes": {"confidence_v2": 3},
                    "legacy_compatibility_modes": {
                        "distance": 1,
                        "hybrid_tag_vector": 1,
                        "weighted_rrf": 1,
                    },
                    "embedding_api_calls": 0,
                },
            )
            self.assertEqual(
                [value["documents"] for value in report["libraries"]], [2, 2]
            )
            self.assertEqual(report["review_run"]["path"], str(review_run_path))
            review_run = json.loads(review_run_path.read_text(encoding="utf-8"))
            review_cases = quality_evaluate.validate_run(review_run)
            self.assertTrue(review_run["draft"])
            self.assertFalse(review_run["baseline_eligible"])
            self.assertEqual(
                review_run["capture"]["kind"], ("zero-api-multicollection-fixture")
            )
            self.assertEqual(
                set(review_cases), {"text-001", "image-001", "combined-001"}
            )
            self.assertTrue(
                all(
                    result["image_id"].split(":", 1)[0]
                    in {library["id"] for library in report["libraries"]}
                    for case in review_cases.values()
                    for result in case["results"]
                )
            )
            source_uuid = json.loads(
                (source / "image_collection.meta.json").read_text(encoding="utf-8")
            )["collection_uuid"]
            manifest = json.loads(libraries.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["libraries"]), 2)
            for library in manifest["libraries"]:
                workspace = Path(library["workspace"])
                metadata = json.loads(
                    (workspace / "image_collection.meta.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(metadata["collection_uuid"], source_uuid)
                state = IndexState(workspace / "image_collection.state.sqlite3")
                try:
                    self.assertEqual(state.count(), 2)
                    self.assertEqual(state.get_metadata("collection_uuid"), source_uuid)
                    image_cache = state.connection.execute(
                        "SELECT COUNT(*) FROM embedding_cache WHERE modality='image'"
                    ).fetchone()[0]
                    self.assertEqual(image_cache, 4)
                finally:
                    state.close()

            derived = json.loads(transformed_dataset.read_text(encoding="utf-8"))
            for item in derived["items"]:
                self.assertEqual(
                    item["library_scope"],
                    {"mode": "all_enabled", "library_ids": []},
                )
                self.assertEqual(item["annotation"]["status"], "pending")
            self.assertEqual(derived["items"][1]["query"]["image"], "three.png")
            suggestions = [
                value["image_id"]
                for value in derived["items"][2]["suggested_relevant_images"]
            ]
            self.assertTrue(all("/alpha/" not in value for value in suggestions))
            self.assertTrue(all("/beta/" not in value for value in suggestions))

    def test_rejects_source_overlap_and_nonempty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, image_root, dataset = self._source_fixture(root)
            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "must not overlap"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=source,
                    libraries_output=root / "libraries.local.json",
                    dataset_output=root / "derived.local.json",
                    run_smoke=False,
                )

            output = root / "nonempty"
            output.mkdir()
            (output / "owned.txt").write_text("user data", encoding="utf-8")
            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "not an empty directory"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=output,
                    libraries_output=root / "libraries.local.json",
                    dataset_output=root / "derived.local.json",
                    run_smoke=False,
                )
            self.assertEqual(
                (output / "owned.txt").read_text(encoding="utf-8"), "user data"
            )

            same_output = root / "same.local.json"
            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "must differ"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=root / "unused-output",
                    libraries_output=same_output,
                    dataset_output=same_output,
                    run_smoke=False,
                )
            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "inside the source workspace"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=root / "another-output",
                    libraries_output=source / "libraries.local.json",
                    dataset_output=root / "derived.local.json",
                    run_smoke=False,
                )

    def test_invalid_mapping_is_atomic_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, image_root, dataset = self._source_fixture(root)
            payload = json.loads(dataset.read_text(encoding="utf-8"))
            payload["items"][0]["suggested_relevant_images"][0]["sha256"] = "0" * 64
            dataset.write_text(json.dumps(payload), encoding="utf-8")
            before = fixture_builder._tree_fingerprint(source)
            output = root / "generated"
            libraries = root / "libraries.local.json"
            derived = root / "derived.local.json"

            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "SHA-256 disagrees"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=output,
                    libraries_output=libraries,
                    dataset_output=derived,
                    run_smoke=False,
                )

            self.assertEqual(fixture_builder._tree_fingerprint(source), before)
            self.assertFalse(output.exists())
            self.assertFalse(libraries.exists())
            self.assertFalse(derived.exists())
            self.assertEqual(list(root.glob(".generated.tmp-*")), [])

    def test_rejects_query_path_ambiguous_across_split_roots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, image_root, dataset = self._source_fixture(root)
            Image.new("RGB", (8, 8), (1, 2, 3)).save(image_root / "alpha" / "three.png")
            with self.assertRaisesRegex(
                fixture_builder.FixtureBuildError, "ambiguous across Collection roots"
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=root / "generated",
                    libraries_output=root / "libraries.local.json",
                    dataset_output=root / "derived.local.json",
                    run_smoke=False,
                )
            self.assertFalse((root / "generated").exists())

    def test_rejects_reparse_split_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, image_root, dataset = self._source_fixture(root)
            original = fixture_builder._is_reparse_point

            def fake_reparse(path: Path) -> bool:
                return path.name == "alpha" or original(path)

            with (
                patch.object(
                    fixture_builder,
                    "_is_reparse_point",
                    side_effect=fake_reparse,
                ),
                self.assertRaisesRegex(
                    fixture_builder.FixtureBuildError, "symlink or reparse point"
                ),
            ):
                fixture_builder.build_fixture(
                    source_workspace=source,
                    image_root=image_root,
                    dataset_path=dataset,
                    output_root=root / "generated",
                    libraries_output=root / "libraries.local.json",
                    dataset_output=root / "derived.local.json",
                    run_smoke=False,
                )
            self.assertFalse((root / "generated").exists())


if __name__ == "__main__":
    unittest.main()
