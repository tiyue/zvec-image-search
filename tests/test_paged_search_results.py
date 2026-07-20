from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import zvec_desktop.result_catalog as result_catalog_module
from image_vector_service.config import ServiceConfig
from image_vector_service.models import SearchHit
from image_vector_service.result_exporter import (
    RESULT_MANIFEST_SCHEMA_VERSION,
    RESULT_OWNERSHIP_KIND,
    RESULT_OWNERSHIP_MARKER,
    RESULT_OWNERSHIP_SCHEMA_VERSION,
    cleanup_search_results,
    export_results,
    search_report_payload,
)
from image_vector_service.search_result_store import (
    RESULT_STORE_APPLICATION_ID,
    RESULT_STORE_FILENAME,
    RESULT_STORE_SCHEMA_VERSION,
    read_result_range,
    read_results_after,
    result_count,
    write_result_store,
)
from zvec_desktop.result_catalog import ResultCatalog, ResultCatalogError


class PagedSearchResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.images = self.root / "images"
        self.results = self.root / "results"
        self.workspace = self.root / "workspace"
        self.images.mkdir()
        self.results.mkdir()
        self.workspace.mkdir()
        self.config_path = self.root / "config.json"
        self._write_json(
            self.config_path,
            {
                "schema_version": 3,
                "results_directory": str(self.results),
                "default_library_id": "library-main",
                "libraries": [
                    {
                        "id": "library-main",
                        "name": "Main Library",
                        "image_root": str(self.images),
                        "workspace_directory": str(self.workspace),
                        "enabled": True,
                    }
                ],
            },
        )

    def test_paged_manifest_reads_only_requested_rank_range(self) -> None:
        manifest = self._paged_manifest("paged", 100_000)
        catalog = ResultCatalog.from_config(self.config_path)

        with (
            mock.patch(
                "zvec_desktop.result_catalog.read_result_range",
                wraps=result_catalog_module.read_result_range,
            ) as range_read,
            mock.patch.object(
                catalog,
                "_manifest_result",
                wraps=catalog._manifest_result,
            ) as materialized,
        ):
            page = catalog.load_manifest(manifest, page=6_667, page_size=15)

        self.assertEqual(page.total_items, 100_000)
        self.assertEqual(page.total_pages, 6_667)
        self.assertEqual(
            [item.rank for item in page.items], list(range(99_991, 100_001))
        )
        self.assertEqual(range_read.call_count, 1)
        self.assertEqual(range_read.call_args.kwargs["start_rank"], 99_991)
        self.assertEqual(range_read.call_args.kwargs["limit"], 15)
        self.assertEqual(materialized.call_count, 10)
        self.assertEqual(catalog._manifest_cache_results, 0)

    def test_million_result_store_supports_bounded_range_and_cursor_reads(self) -> None:
        manifest = self._paged_manifest("million", 1_000_000)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        reference = payload["result_store"]
        store_path = manifest.parent / reference["path"]

        page = read_result_range(
            store_path,
            session_id=reference["session_id"],
            start_rank=999_991,
            limit=15,
        )
        cursor_page = read_results_after(
            store_path,
            session_id=reference["session_id"],
            after_rank=999_985,
            limit=15,
        )

        self.assertEqual(result_count(store_path, reference["session_id"]), 1_000_000)
        self.assertEqual([item.rank for item in page], list(range(999_991, 1_000_001)))
        self.assertEqual(
            [item.rank for item in cursor_page], list(range(999_986, 1_000_001))
        )
        self.assertLess(manifest.stat().st_size, 16 * 1024)
        self.assertEqual(len(payload["results"]), 1)
        self.assertTrue(payload["results_truncated"])

    def test_source_only_export_streams_100k_results_without_copying_images(
        self,
    ) -> None:
        source = self.images / "shared.jpg"
        source.write_bytes(b"image")
        self._state_roots({"root-main": self.images})
        resolver = mock.Mock(return_value=source)

        def hits():
            for rank in range(1, 100_002):
                hash_rank = 1 if rank == 50_000 else rank
                yield SearchHit(
                    doc_id=f"doc-{rank}",
                    distance=rank / 1_000_000,
                    fields={
                        "library_id": "library-main",
                        "root_id": "root-main",
                        "relative_path": source.name,
                        "sha256": f"{hash_rank:064x}",
                    },
                )

        with mock.patch(
            "image_vector_service.result_exporter.shutil.copy2"
        ) as copy_file:
            report = export_results(
                ServiceConfig(
                    workspace=self.workspace,
                    library_id="library-main",
                    library_image_root=self.images,
                    results_directory=self.results,
                ),
                query_type="text",
                hits=hits(),
                top_k=100_000,
                query={"text": "large"},
                request_ids=[],
                usage=[],
                resolve_source=resolver,
                library_ids=["library-main"],
                library_names=["Main Library"],
                candidate_count=100_001,
                copy_files=False,
            )

        copy_file.assert_not_called()
        resolver.assert_not_called()
        self.assertEqual(report.result_count, 100_000)
        self.assertEqual(report.filtered_count, 1)
        self.assertEqual(len(report.results), 15)
        self.assertTrue(report.results_truncated)
        self.assertEqual(report.result_storage, "source_only")
        output = Path(report.output_dir)
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {RESULT_OWNERSHIP_MARKER, "results.json", RESULT_STORE_FILENAME},
        )
        manifest = output / "results.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["manifest_schema_version"], RESULT_MANIFEST_SCHEMA_VERSION
        )
        self.assertEqual(payload["result_storage"], "source_only")
        self.assertIsNone(payload["results"][0]["copied_file"])
        self.assertLess(manifest.stat().st_size, 64 * 1024)

        page = ResultCatalog.from_config(self.config_path).load_manifest(
            manifest, page=6_667, page_size=15
        )
        self.assertEqual(
            [item.rank for item in page.items], list(range(99_991, 100_001))
        )
        self.assertTrue(all(item.display_path == source for item in page.items))
        self.assertTrue(all(item.copied_path is None for item in page.items))

    def test_source_only_result_uses_root_id_for_multi_root_library(self) -> None:
        configured_source = self.images / "same.jpg"
        configured_source.write_bytes(b"configured")
        second_root = self.root / "second-root"
        second_root.mkdir()
        actual_source = second_root / "same.jpg"
        actual_source.write_bytes(b"second")
        self._state_roots({"root-configured": self.images, "root-second": second_root})
        report = export_results(
            ServiceConfig(
                workspace=self.workspace,
                library_id="library-main",
                results_directory=self.results,
            ),
            query_type="text",
            hits=[
                SearchHit(
                    doc_id="doc-second",
                    distance=0.1,
                    fields={
                        "library_id": "library-main",
                        "root_id": "root-second",
                        "relative_path": actual_source.name,
                        "sha256": "a" * 64,
                    },
                )
            ],
            top_k=1,
            query={"text": "root"},
            request_ids=[],
            usage=[],
            resolve_source=lambda _hit: actual_source,
            library_ids=["library-main"],
            copy_files=False,
            report_result_limit=1,
        )

        page = ResultCatalog.from_config(self.config_path).load_manifest(
            Path(report.output_dir) / "results.json"
        )

        self.assertEqual(page.items[0].display_path, actual_source)
        self.assertNotEqual(page.items[0].display_path, configured_source)

    def test_source_only_unknown_root_never_falls_back_to_same_named_main_file(
        self,
    ) -> None:
        configured_source = self.images / "same.jpg"
        configured_source.write_bytes(b"wrong-main-root")
        self._state_roots({"root-configured": self.images})
        manifest = self._source_only_manifest(
            "unknown-root",
            root_id="root-does-not-exist",
            relative_path=configured_source.name,
        )

        with self.assertRaisesRegex(
            ResultCatalogError,
            "root_id does not identify a current library root",
        ):
            ResultCatalog.from_config(self.config_path).load_manifest(manifest)

    def test_source_only_missing_state_never_guesses_the_configured_main_root(
        self,
    ) -> None:
        configured_source = self.images / "same.jpg"
        configured_source.write_bytes(b"not-authoritative")
        manifest = self._source_only_manifest(
            "missing-state",
            root_id="root-main",
            relative_path=configured_source.name,
        )

        with self.assertRaisesRegex(
            ResultCatalogError,
            "root_id does not identify a current library root",
        ):
            ResultCatalog.from_config(self.config_path).load_manifest(manifest)

    def test_source_only_single_root_keeps_configured_main_root_compatibility(
        self,
    ) -> None:
        source = self.images / "single.jpg"
        source.write_bytes(b"single-root")
        self._state_roots({"root-main": self.images})
        manifest = self._source_only_manifest(
            "single-root",
            root_id="root-main",
            relative_path=source.name,
        )

        page = ResultCatalog.from_config(self.config_path).load_manifest(manifest)

        self.assertEqual(page.items[0].display_path, source)
        self.assertEqual(page.items[0].original_path, source)

    def test_source_root_lookup_batches_all_unique_roots_on_one_page(self) -> None:
        roots: dict[str, Path] = {}
        expected: list[Path] = []
        for rank in range(1, 16):
            root_id = f"root-{rank}"
            source_root = self.root / root_id
            source_root.mkdir()
            source = source_root / "same.jpg"
            source.write_bytes(f"root-{rank}".encode())
            roots[root_id] = source_root
            expected.append(source)
        self._state_roots(roots)
        directory = self.results / "bounded-root-lookups"
        directory.mkdir()
        manifest = directory / "results.json"
        self._write_json(
            manifest,
            {
                "manifest_schema_version": 3,
                "result_storage": "source_only",
                "created_at": "2026-07-20T12:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "result_count": 15,
                "results": [
                    {
                        "rank": rank,
                        "copied_file": None,
                        "relative_path": "same.jpg",
                        "root_id": f"root-{rank}",
                        "library_id": "library-main",
                        "doc_id": f"source-{rank}",
                    }
                    for rank in range(1, 16)
                ],
            },
        )
        catalog = ResultCatalog.from_config(self.config_path)

        with mock.patch(
            "zvec_desktop.result_catalog._root_paths_from_state",
            wraps=result_catalog_module._root_paths_from_state,
        ) as lookup:
            page = catalog.load_manifest(manifest, page_size=15)

        self.assertEqual(len(page.items), 15)
        self.assertEqual([item.display_path for item in page.items], expected)
        self.assertEqual(lookup.call_count, 1)

    def test_source_root_cache_refreshes_after_wal_rebind_in_same_process(
        self,
    ) -> None:
        previous_root = self.root / "previous-root"
        rebound_root = self.root / "rebound-root"
        previous_root.mkdir()
        rebound_root.mkdir()
        previous_source = previous_root / "same.jpg"
        rebound_source = rebound_root / "same.jpg"
        previous_source.write_bytes(b"previous")
        rebound_source.write_bytes(b"rebound")
        state_path = self.workspace / "image_collection.state.sqlite3"
        writer = sqlite3.connect(state_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE roots ("
            "root_id TEXT PRIMARY KEY, current_path TEXT NOT NULL UNIQUE, "
            "recursive INTEGER NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]')"
        )
        writer.execute(
            "INSERT INTO roots(root_id, current_path, recursive) VALUES (?, ?, 1)",
            ("root-main", str(previous_root.resolve())),
        )
        writer.commit()
        manifest = self._source_only_manifest(
            "rebound-root",
            root_id="root-main",
            relative_path="same.jpg",
        )
        catalog = ResultCatalog.from_config(self.config_path)

        before = catalog.load_manifest(manifest)
        writer.execute(
            "UPDATE roots SET current_path = ? WHERE root_id = ?",
            (str(rebound_root.resolve()), "root-main"),
        )
        writer.commit()
        after = catalog.load_manifest(manifest)

        self.assertEqual(before.items[0].display_path, previous_source)
        self.assertEqual(after.items[0].display_path, rebound_source)
        self.assertNotEqual(before.items[0].display_path, after.items[0].display_path)

    def test_source_validation_failures_keep_only_a_bounded_preview(self) -> None:
        report = export_results(
            ServiceConfig(
                workspace=self.workspace,
                library_id="library-main",
                results_directory=self.results,
            ),
            query_type="text",
            hits=(
                SearchHit(
                    doc_id=f"missing-{rank}",
                    distance=0.1,
                    fields={
                        "library_id": "library-main",
                        "root_id": "root-main",
                        "relative_path": "",
                    },
                )
                for rank in range(1_000)
            ),
            top_k=1_000,
            query={"text": "missing"},
            request_ids=[],
            usage=[],
            resolve_source=mock.Mock(side_effect=AssertionError("must stay lazy")),
            candidate_count=1_000,
            copy_files=False,
            report_result_limit=15,
        )

        self.assertEqual(report.result_count, 0)
        self.assertEqual(report.copy_failure_count, 1_000)
        self.assertEqual(len(report.copy_failures), 200)
        payload = search_report_payload(report)
        self.assertEqual(payload["copy_failure_count"], 1_000)
        self.assertTrue(payload["copy_failures_truncated"])

    def test_schema_one_result_store_remains_readable(self) -> None:
        manifest = self._paged_manifest("schema-one", 1)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        store_path = manifest.parent / RESULT_STORE_FILENAME
        with closing(sqlite3.connect(store_path)) as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        payload["result_store"]["schema_version"] = 1
        payload["manifest_schema_version"] = 2
        self._write_json(manifest, payload)

        page = ResultCatalog.from_config(self.config_path).load_manifest(manifest)

        self.assertEqual(page.total_items, 1)
        self.assertEqual(page.items[0].name, "shared.jpg")

    def test_missing_sidecar_falls_back_to_latest_complete_legacy_manifest(
        self,
    ) -> None:
        legacy_directory = self.results / "legacy"
        legacy_directory.mkdir()
        (legacy_directory / "legacy.jpg").write_bytes(b"image")
        self._write_json(
            legacy_directory / "results.json",
            {
                "created_at": "2026-07-20T12:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "results": [
                    {
                        "rank": 1,
                        "copied_file": "legacy.jpg",
                        "relative_path": "missing/legacy.jpg",
                        "library_id": "library-main",
                    }
                ],
            },
        )
        newest = self._paged_manifest(
            "newest", 1, created_at="2026-07-20T13:00:00+08:00"
        )
        (newest.parent / RESULT_STORE_FILENAME).unlink()

        page = ResultCatalog.from_config(self.config_path).load_latest()

        self.assertEqual(page.items[0].name, "legacy.jpg")

    def test_result_store_path_traversal_is_rejected(self) -> None:
        manifest = self._paged_manifest("unsafe", 1)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["result_store"]["path"] = "../outside.sqlite3"
        self._write_json(manifest, payload)

        with self.assertRaisesRegex(ResultCatalogError, "result_store.path"):
            ResultCatalog.from_config(self.config_path).load_manifest(manifest)

    def test_corrupt_sidecar_is_rejected_and_latest_falls_back(self) -> None:
        legacy_directory = self.results / "legacy-valid"
        legacy_directory.mkdir()
        (legacy_directory / "legacy.jpg").write_bytes(b"image")
        self._write_json(
            legacy_directory / "results.json",
            {
                "created_at": "2026-07-20T12:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "results": [
                    {
                        "rank": 1,
                        "copied_file": "legacy.jpg",
                        "library_id": "library-main",
                    }
                ],
            },
        )
        corrupt_manifest = self._paged_manifest(
            "newest-corrupt", 1, created_at="2026-07-20T13:00:00+08:00"
        )
        store_path = corrupt_manifest.parent / RESULT_STORE_FILENAME
        store_path.unlink()
        with closing(sqlite3.connect(store_path)) as connection:
            connection.execute(f"PRAGMA application_id = {RESULT_STORE_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {RESULT_STORE_SCHEMA_VERSION}")
            connection.commit()

        catalog = ResultCatalog.from_config(self.config_path)
        with self.assertRaisesRegex(ResultCatalogError, "could not read result store"):
            catalog.load_manifest(corrupt_manifest)
        self.assertEqual(catalog.load_latest().items[0].name, "legacy.jpg")

    def test_cleanup_understands_paged_sidecars_and_remains_fail_closed(self) -> None:
        directories = [
            self._owned_paged_result(f"query_20260720_12000{index}_000", index)
            for index in range(4)
        ]

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 1)
        self.assertFalse(directories[0].exists())
        self.assertTrue(all(path.is_dir() for path in directories[1:]))

    def test_cleanup_source_only_sessions_never_deletes_library_sources(self) -> None:
        source = self.images / "owned-by-library.jpg"
        source.write_bytes(b"source")
        directories = [
            self._owned_source_only_result(
                f"query_20260720_12000{index}_000", index, source
            )
            for index in range(4)
        ]

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 1)
        self.assertFalse(directories[0].exists())
        self.assertTrue(source.is_file())

    def _paged_manifest(
        self,
        name: str,
        count: int,
        *,
        created_at: str = "2026-07-20T12:00:00+08:00",
    ) -> Path:
        directory = self.results / name
        directory.mkdir()
        copied = directory / "shared.jpg"
        copied.write_bytes(b"image")
        session_id = f"session-{name}"
        reference = write_result_store(
            directory / RESULT_STORE_FILENAME,
            session_id=session_id,
            created_at=created_at,
            results=(
                {
                    "rank": rank,
                    "copied_file": copied.name,
                    "relative_path": "missing/shared.jpg",
                    "library_id": "library-main",
                    "doc_id": f"doc-{rank}",
                }
                for rank in range(1, count + 1)
            ),
        )
        manifest = directory / "results.json"
        self._write_json(
            manifest,
            {
                "manifest_schema_version": 2,
                "created_at": created_at,
                "query_type": "text",
                "status": "ok",
                "sort_mode": "confidence",
                "library_ids": ["library-main"],
                "result_count": count,
                "results": [
                    {
                        "rank": 1,
                        "copied_file": copied.name,
                        "relative_path": "missing/shared.jpg",
                        "library_id": "library-main",
                    }
                ]
                if count
                else [],
                "results_truncated": count > 1,
                "result_store": reference.to_dict(),
            },
        )
        return manifest

    def _source_only_manifest(
        self,
        name: str,
        *,
        root_id: str,
        relative_path: str,
        count: int = 1,
    ) -> Path:
        directory = self.results / name
        directory.mkdir()
        manifest = directory / "results.json"
        self._write_json(
            manifest,
            {
                "manifest_schema_version": 3,
                "result_storage": "source_only",
                "created_at": "2026-07-20T12:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "result_count": count,
                "results": [
                    {
                        "rank": rank,
                        "copied_file": None,
                        "relative_path": relative_path,
                        "root_id": root_id,
                        "library_id": "library-main",
                        "doc_id": f"source-{rank}",
                    }
                    for rank in range(1, count + 1)
                ],
            },
        )
        return manifest

    def _owned_paged_result(self, name: str, rank: int) -> Path:
        directory = self.results / name
        directory.mkdir()
        copied_name = f"copy-{rank}.jpg"
        (directory / copied_name).write_bytes(b"image")
        created_at = f"2026-07-20T12:00:0{rank}+08:00"
        reference = write_result_store(
            directory / RESULT_STORE_FILENAME,
            session_id=f"cleanup-{rank}",
            created_at=created_at,
            results=(
                {
                    "rank": 1,
                    "copied_file": copied_name,
                    "relative_path": f"missing/{copied_name}",
                    "library_id": "library-main",
                },
            ),
        )
        self._write_json(
            directory / "results.json",
            {
                "created_at": created_at,
                "query_type": "text",
                "output_dir": str(directory.resolve()),
                "result_count": 1,
                "results": [
                    {
                        "rank": 1,
                        "copied_file": copied_name,
                    }
                ],
                "result_store": reference.to_dict(),
            },
        )
        self._write_json(
            directory / RESULT_OWNERSHIP_MARKER,
            {
                "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
                "kind": RESULT_OWNERSHIP_KIND,
            },
        )
        return directory

    def _owned_source_only_result(self, name: str, rank: int, source: Path) -> Path:
        directory = self.results / name
        directory.mkdir()
        created_at = f"2026-07-20T12:00:0{rank}+08:00"
        reference = write_result_store(
            directory / RESULT_STORE_FILENAME,
            session_id=f"source-cleanup-{rank}",
            created_at=created_at,
            results=(
                {
                    "rank": 1,
                    "copied_file": None,
                    "relative_path": source.name,
                    "root_id": "root-main",
                    "library_id": "library-main",
                    "doc_id": f"source-{rank}",
                },
            ),
        )
        self._write_json(
            directory / "results.json",
            {
                "manifest_schema_version": 3,
                "result_storage": "source_only",
                "created_at": created_at,
                "query_type": "text",
                "output_dir": str(directory.resolve()),
                "result_count": 1,
                "results": [
                    {
                        "rank": 1,
                        "copied_file": None,
                        "relative_path": source.name,
                        "root_id": "root-main",
                        "library_id": "library-main",
                    }
                ],
                "result_store": reference.to_dict(),
            },
        )
        self._write_json(
            directory / RESULT_OWNERSHIP_MARKER,
            {
                "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
                "kind": RESULT_OWNERSHIP_KIND,
            },
        )
        return directory

    def _state_roots(self, roots: dict[str, Path]) -> None:
        state_path = self.workspace / "image_collection.state.sqlite3"
        with closing(sqlite3.connect(state_path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS roots ("
                "root_id TEXT PRIMARY KEY, current_path TEXT NOT NULL UNIQUE, "
                "recursive INTEGER NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]')"
            )
            connection.executemany(
                "INSERT OR REPLACE INTO roots(root_id, current_path, recursive) "
                "VALUES (?, ?, 1)",
                ((root_id, str(path.resolve())) for root_id, path in roots.items()),
            )
            connection.commit()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
