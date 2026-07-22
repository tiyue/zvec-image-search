from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import zvec_desktop.result_catalog as result_catalog_module
from zvec_desktop.result_catalog import ResultCatalog, ResultCatalogError, SearchResult


class ResultCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.images = self.root / "图库（原神）"
        self.results = self.root / "搜索结果"
        self.workspace = self.root / "工作区"
        self.images.mkdir()
        self.results.mkdir()
        self.workspace.mkdir()
        self.config_path = self.root / "配置.json"
        self._write_json(
            self.config_path,
            {
                "schema_version": 3,
                "results_directory": str(self.results.resolve()),
                "default_library_id": "library-main",
                "libraries": [
                    {
                        "id": "library-main",
                        "name": "二次元图库",
                        "image_root": str(self.images.resolve()),
                        "workspace_directory": str(self.workspace.resolve()),
                        "enabled": True,
                    }
                ],
            },
        )

    def test_unicode_config_and_folder_pagination(self) -> None:
        nested = self.images / "雷电将军"
        nested.mkdir()
        for name in ("神态.png", "动作.jpg", "忽略.txt"):
            (nested / name).write_bytes(b"image")

        catalog = ResultCatalog.from_config(self.config_path)
        first = catalog.from_folder(self.images, page=1, page_size=1)
        second = catalog.from_folder(self.images, page=2, page_size=1)

        self.assertEqual(catalog.libraries[0].name, "二次元图库")
        self.assertEqual(first.total_items, 2)
        self.assertEqual(first.total_pages, 2)
        self.assertEqual(first.summary, "共 2 张图片 · 第 1/2 页")
        self.assertEqual(first.source_label, "图库（原神）")
        self.assertTrue(first.has_next)
        self.assertEqual(second.items[0].library_name, "图库（原神）")
        self.assertIn("雷电将军", second.items[0].relative_path)

    def test_folder_pages_reuse_one_recursive_scan_until_cache_is_cleared(self) -> None:
        for index in range(3):
            (self.images / f"image-{index}.jpg").write_bytes(b"image")
        catalog = ResultCatalog.from_config(self.config_path)

        with mock.patch(
            "zvec_desktop.result_catalog._images_below",
            wraps=result_catalog_module._images_below,
        ) as scan:
            catalog.from_folder(self.images, page=1, page_size=1)
            catalog.from_folder(self.images, page=2, page_size=1)
            self.assertEqual(scan.call_count, 1)

            catalog.clear_folder_cache(self.images)
            catalog.from_folder(self.images, page=3, page_size=1)
            self.assertEqual(scan.call_count, 2)

    def test_large_folder_constructs_only_the_requested_page_with_global_ranks(
        self,
    ) -> None:
        image_paths = tuple(
            self.images / f"image-{index:06}.jpg" for index in range(100_000)
        )
        catalog = ResultCatalog.from_config(self.config_path)

        with (
            mock.patch(
                "zvec_desktop.result_catalog._images_below",
                return_value=list(image_paths),
            ),
            mock.patch.object(
                result_catalog_module, "SearchResult", wraps=SearchResult
            ) as result_factory,
        ):
            page = catalog.from_folder(self.images, page=6667, page_size=15)

        self.assertEqual(page.total_items, 100_000)
        self.assertEqual(page.total_pages, 6667)
        self.assertEqual(len(page.items), 10)
        self.assertEqual(page.items[0].rank, 99_991)
        self.assertEqual(page.items[-1].rank, 100_000)
        self.assertEqual(result_factory.call_count, 10)

    def test_folder_scan_can_be_cancelled_without_caching_partial_results(self) -> None:
        for index in range(20):
            (self.images / f"image-{index}.jpg").write_bytes(b"image")
        catalog = ResultCatalog.from_config(self.config_path)
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 6

        with self.assertRaises(CancelledError):
            catalog.from_folder(self.images, cancelled=cancelled)

        page = catalog.from_folder(self.images)
        self.assertEqual(page.total_items, 20)

    def test_default_config_path_uses_config_home(self) -> None:
        config_home = self.root / "用户配置"
        config_home.mkdir()
        (config_home / "config.json").write_bytes(self.config_path.read_bytes())

        with mock.patch.dict(
            os.environ, {"ZVEC_CONFIG_HOME": str(config_home)}, clear=False
        ):
            catalog = ResultCatalog.from_config()

        self.assertEqual(catalog.config.config_path, config_home / "config.json")

    def test_load_latest_uses_created_at_and_preserves_unicode_metadata(self) -> None:
        old = self._manifest(
            "旧搜索", "2026-07-18T12:00:00+08:00", copied_file="旧图.jpg"
        )
        new = self._manifest(
            "最新搜索", "2026-07-18T13:00:00+08:00", copied_file="新图.jpg"
        )
        # Deliberately make the older logical result newer on disk. created_at is
        # the authoritative search timestamp; mtime is only a tie breaker.
        os.utime(old / "results.json", (2_000_000_000, 2_000_000_000))
        os.utime(new / "results.json", (1_000_000_000, 1_000_000_000))

        page = ResultCatalog.from_config(self.config_path).load_latest()

        self.assertEqual(page.manifest_path, new / "results.json")
        self.assertEqual(page.items[0].name, "新图.jpg")
        self.assertEqual(page.items[0].tags, ("原神", "雷电将军"))
        self.assertEqual(page.items[0].matched_tags, ("雷神",))
        self.assertAlmostEqual(page.items[0].raw_score, 0.18)
        self.assertAlmostEqual(page.items[0].normalized_score, 0.91)
        self.assertAlmostEqual(page.items[0].confidence, 0.91)
        self.assertAlmostEqual(page.items[0].image_confidence or 0.0, 0.88)
        self.assertAlmostEqual(page.items[0].text_confidence or 0.0, 0.93)
        self.assertEqual(page.items[0].image_rank, 2)
        self.assertEqual(page.items[0].text_rank, 1)
        self.assertEqual(page.items[0].ranking_model_version, "ranker-test-v1")
        self.assertAlmostEqual(page.items[0].ranking_score or 0.0, 0.94)
        self.assertEqual(page.items[0].feature_schema_version, 1)
        self.assertEqual(page.items[0].calibration_version, "calibration-test-v1")
        self.assertEqual(
            page.items[0].search_features,
            {
                "feature_schema_version": 1,
                "vector_raw_score": 0.18,
                "vector_confidence": 0.92,
                "tag_match_score": 0.72,
                "manual_tag_matches": 1.0,
                "folder_tag_matches": 1.0,
                "alias_tag_matches": 0.0,
                "model_high_confidence_tag_matches": 0.0,
                "model_tag_matches": 1.0,
                "vector_tag_matches": 1.0,
                "identity_match": 1.0,
                "work_match": 1.0,
                "action_match": 0.0,
                "expression_match": 0.0,
                "scene_match": 0.0,
                "image_text_agreement": 0.8,
                "collection_rank": 1.0,
                "collection_size": 2_074.0,
                "duplicate_group_size": 1.0,
                "query_type": "text",
            },
        )
        self.assertEqual(page.sort_mode, "confidence")
        self.assertEqual(
            page.ranking_diagnostics,
            {"primary": "ranking_confidence_or_confidence_desc"},
        )
        self.assertEqual(page.source_label, "最新搜索")

    def test_search_history_lists_and_reopens_persisted_result_sets(self) -> None:
        self._manifest("旧搜索", "2026-07-18T12:00:00+08:00", copied_file="旧图.jpg")
        new = self._manifest(
            "最新搜索", "2026-07-18T13:00:00+08:00", copied_file="新图.jpg"
        )
        payload = json.loads((new / "results.json").read_text(encoding="utf-8"))
        payload["query"] = {"text": "红色和服 室内", "tags": []}
        self._write_json(new / "results.json", payload)

        catalog = ResultCatalog.from_config(self.config_path)
        history = catalog.list_history(limit=12)

        self.assertEqual(history[0].label, "红色和服 室内")
        self.assertTrue(history[0].history_id.startswith("v1_"))
        self.assertNotIn("最新搜索", history[0].history_id)
        self.assertEqual(history[1].label, "语义搜索")
        self.assertEqual(history[0].total_items, 1)
        reopened = catalog.load_history(history[0].history_id, page=1, page_size=15)
        self.assertEqual(reopened.items[0].name, "新图.jpg")

    def test_search_history_rejects_path_traversal(self) -> None:
        self._manifest("安全搜索", "2026-07-18T13:00:00+08:00", copied_file="图片.jpg")
        catalog = ResultCatalog.from_config(self.config_path)

        for history_id in ("../安全搜索", r"..\安全搜索", ".", ""):
            with (
                self.subTest(history_id=history_id),
                self.assertRaisesRegex(ResultCatalogError, "history id"),
            ):
                catalog.load_history(history_id)

    def test_load_latest_falls_back_when_newest_manifest_is_incomplete(self) -> None:
        complete = self._manifest(
            "完整搜索", "2026-07-18T12:00:00+08:00", copied_file="完整.jpg"
        )
        incomplete = self.results / "写入中的搜索"
        incomplete.mkdir()
        self._write_json(
            incomplete / "results.json",
            {
                "created_at": "2026-07-18T13:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "sort_mode": "confidence",
                "ranking_diagnostics": {
                    "primary": "ranking_confidence_or_confidence_desc"
                },
                "library_ids": ["library-main"],
                # The exporter has created valid JSON but has not written the
                # required results array yet.
            },
        )

        page = ResultCatalog.from_config(self.config_path).load_latest()

        self.assertEqual(page.manifest_path, complete / "results.json")
        self.assertEqual(page.items[0].name, "完整.jpg")

    def test_load_latest_falls_back_when_newest_result_file_is_not_ready(self) -> None:
        complete = self._manifest(
            "完整搜索", "2026-07-18T12:00:00+08:00", copied_file="完整.jpg"
        )
        incomplete = self._manifest(
            "写入中的搜索",
            "2026-07-18T13:00:00+08:00",
            copied_file="尚未写入.jpg",
        )
        (incomplete / "尚未写入.jpg").unlink()

        catalog = ResultCatalog.from_config(self.config_path)
        page = catalog.load_latest()

        self.assertEqual(page.manifest_path, complete / "results.json")
        (incomplete / "尚未写入.jpg").write_bytes(b"completed")
        recovered = catalog.load_latest()
        self.assertEqual(recovered.manifest_path, incomplete / "results.json")

    def test_load_manifest_opens_an_explicit_history_item(self) -> None:
        old = self._manifest(
            "旧搜索", "2026-07-18T12:00:00+08:00", copied_file="旧图.jpg"
        )
        self._manifest("最新搜索", "2026-07-18T13:00:00+08:00", copied_file="新图.jpg")

        page = ResultCatalog.from_config(self.config_path).load_manifest(
            old / "results.json"
        )

        self.assertEqual(page.manifest_path, old / "results.json")
        self.assertEqual(page.source_label, "旧搜索")
        self.assertEqual(page.items[0].name, "旧图.jpg")

    def test_original_image_is_preferred_then_copied_file_is_fallback(self) -> None:
        original = self.images / "角色" / "刻晴.jpg"
        original.parent.mkdir()
        original.write_bytes(b"original")
        output = self._manifest(
            "优先级",
            "2026-07-18T13:00:00+08:00",
            copied_file="副本.jpg",
            relative_path="角色/刻晴.jpg",
        )

        catalog = ResultCatalog.from_config(self.config_path)
        first = catalog.load_latest().items[0]
        self.assertEqual(first.original_path, original.resolve())
        self.assertEqual(first.display_path, original.resolve())
        self.assertEqual(first.copied_path, (output / "副本.jpg").resolve())

        original.unlink()
        fallback = catalog.load_latest().items[0]
        self.assertIsNone(fallback.original_path)
        self.assertEqual(fallback.display_path, (output / "副本.jpg").resolve())

    def test_large_manifest_is_parsed_once_then_pages_are_constant_work(self) -> None:
        manifest, copied = self._bulk_manifest("large", 10_000)
        catalog = ResultCatalog.from_config(self.config_path)

        def materialize(
            _raw: object,
            *,
            index: int,
            manifest_path: Path,
            manifest: object,
        ) -> SearchResult:
            del manifest_path, manifest
            return SearchResult(
                display_path=copied,
                copied_path=copied,
                original_path=None,
                name=copied.name,
                rank=index + 1,
                relative_path="",
                library_name="二次元图库",
            )

        with (
            mock.patch.object(
                catalog, "_manifest_result", side_effect=materialize
            ) as parsed,
            mock.patch(
                "zvec_desktop.result_catalog._read_json_object",
                wraps=result_catalog_module._read_json_object,
            ) as reads,
        ):
            first = catalog.load_manifest(manifest, page=1, page_size=15)
            second = catalog.load_manifest(manifest, page=2, page_size=15)

        self.assertEqual(first.total_items, 10_000)
        self.assertEqual([item.rank for item in second.items], list(range(16, 31)))
        self.assertEqual(parsed.call_count, 10_000)
        self.assertEqual(reads.call_count, 1)

    def test_manifest_version_change_invalidates_cached_snapshot(self) -> None:
        manifest, _copied = self._bulk_manifest("mutable", 1)
        catalog = ResultCatalog.from_config(self.config_path)
        first = catalog.load_manifest(manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["results"].append({**payload["results"][0], "rank": 2})
        self._write_json(manifest, payload)

        second = catalog.load_manifest(manifest)

        self.assertEqual(first.total_items, 1)
        self.assertEqual(second.total_items, 2)

    def test_manifest_cache_key_uses_path_mtime_and_size(self) -> None:
        first, _copied = self._bulk_manifest("fingerprint-a", 1)
        second, _copied = self._bulk_manifest("fingerprint-b", 1)
        catalog = ResultCatalog.from_config(self.config_path)
        with mock.patch(
            "zvec_desktop.result_catalog._read_json_object",
            wraps=result_catalog_module._read_json_object,
        ) as reads:
            catalog.load_manifest(first)
            original = first.stat()
            first.write_text(first.read_text(encoding="utf-8") + " ", encoding="utf-8")
            os.utime(
                first,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            catalog.load_manifest(first)

            resized = first.stat()
            os.utime(
                first,
                ns=(resized.st_atime_ns, resized.st_mtime_ns + 10_000_000),
            )
            catalog.load_manifest(first)

            second.write_text(
                second.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )
            current = first.stat()
            os.utime(
                second,
                ns=(second.stat().st_atime_ns, current.st_mtime_ns),
            )
            catalog.load_manifest(second)

        self.assertEqual(first.stat().st_size, second.stat().st_size)
        self.assertEqual(first.stat().st_mtime_ns, second.stat().st_mtime_ns)
        self.assertEqual(reads.call_count, 4)

    def test_manifest_errors_are_not_cached_and_can_be_retried(self) -> None:
        manifest, _copied = self._bulk_manifest("retry", 1)
        catalog = ResultCatalog.from_config(self.config_path)
        with (
            mock.patch(
                "zvec_desktop.result_catalog._read_json_object",
                side_effect=ResultCatalogError("transient read failure"),
            ),
            self.assertRaisesRegex(ResultCatalogError, "transient"),
        ):
            catalog.load_manifest(manifest)

        recovered = catalog.load_manifest(manifest)
        self.assertEqual(recovered.total_items, 1)

    def test_deleted_manifest_or_result_never_survives_cache(self) -> None:
        explicit, copied = self._bulk_manifest("explicit-delete", 1)
        catalog = ResultCatalog.from_config(self.config_path)
        self.assertEqual(catalog.load_manifest(explicit).total_items, 1)
        copied.unlink()
        self.assertEqual(catalog.load_manifest(explicit).total_items, 0)
        explicit.unlink()
        with self.assertRaisesRegex(ResultCatalogError, "does not exist"):
            catalog.load_manifest(explicit)

        old = self._manifest(
            "old-complete", "2026-07-18T12:00:00+08:00", copied_file="old.jpg"
        )
        newest = self._manifest(
            "newest-delete", "2026-07-18T13:00:00+08:00", copied_file="new.jpg"
        )
        self.assertEqual(catalog.load_latest().manifest_path, newest / "results.json")
        (newest / "results.json").unlink()
        self.assertEqual(catalog.load_latest().manifest_path, old / "results.json")

    def test_latest_cache_discovers_manifest_created_in_existing_directory(
        self,
    ) -> None:
        old = self._manifest("old", "2026-07-18T12:00:00+08:00", copied_file="old.jpg")
        pending = self.results / "pending"
        pending.mkdir()
        catalog = ResultCatalog.from_config(self.config_path)
        self.assertEqual(catalog.load_latest().manifest_path, old / "results.json")

        (pending / "new.jpg").write_bytes(b"copy")
        self._write_json(
            pending / "results.json",
            {
                "created_at": "2026-07-18T14:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "results": [
                    {
                        "rank": 1,
                        "copied_file": "new.jpg",
                        "relative_path": "missing/new.jpg",
                        "library_id": "library-main",
                    }
                ],
            },
        )

        self.assertEqual(catalog.load_latest().manifest_path, pending / "results.json")

    def test_latest_cache_reorders_when_existing_manifest_changes(self) -> None:
        old = self._manifest(
            "old-reordered", "2026-07-18T12:00:00+08:00", copied_file="old.jpg"
        )
        current = self._manifest(
            "current", "2026-07-18T13:00:00+08:00", copied_file="current.jpg"
        )
        catalog = ResultCatalog.from_config(self.config_path)
        self.assertEqual(catalog.load_latest().manifest_path, current / "results.json")

        manifest = old / "results.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["created_at"] = "2026-07-18T14:00:00+08:00"
        self._write_json(manifest, payload)

        self.assertEqual(catalog.load_latest().manifest_path, manifest)

    def test_concurrent_pages_share_one_manifest_parse(self) -> None:
        manifest, _copied = self._bulk_manifest("concurrent", 120)
        catalog = ResultCatalog.from_config(self.config_path)
        original_read = result_catalog_module._read_json_object

        def slow_read(*args: object, **kwargs: object) -> dict[str, object]:
            time.sleep(0.05)
            return original_read(*args, **kwargs)

        with (
            mock.patch(
                "zvec_desktop.result_catalog._read_json_object",
                side_effect=slow_read,
            ) as reads,
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            pages = list(
                executor.map(
                    lambda page: catalog.load_manifest(
                        manifest, page=page, page_size=15
                    ),
                    range(1, 9),
                )
            )

        self.assertEqual(reads.call_count, 1)
        self.assertEqual(
            [page.items[0].rank for page in pages], list(range(1, 107, 15))
        )

    def test_manifest_cache_is_lru_bounded(self) -> None:
        first, _copied = self._bulk_manifest("cache-a", 1)
        second, _copied = self._bulk_manifest("cache-b", 1)
        base = ResultCatalog.from_config(self.config_path)
        catalog = ResultCatalog(
            base.config, manifest_cache_entries=1, manifest_cache_results=10
        )
        with mock.patch(
            "zvec_desktop.result_catalog._read_json_object",
            wraps=result_catalog_module._read_json_object,
        ) as reads:
            catalog.load_manifest(first)
            catalog.load_manifest(second)
            catalog.load_manifest(first)
        self.assertEqual(reads.call_count, 3)

    def test_rejects_copied_file_path_traversal(self) -> None:
        self._manifest("穿越", "2026-07-18T13:00:00+08:00", copied_file="../逃逸.jpg")
        catalog = ResultCatalog.from_config(self.config_path)

        with self.assertRaisesRegex(ResultCatalogError, "unsafe path traversal"):
            catalog.load_latest()

    def test_rejects_absolute_copied_file_on_every_platform(self) -> None:
        self._manifest(
            "绝对路径",
            "2026-07-18T13:00:00+08:00",
            copied_file=r"C:\\Users\\attacker\\image.jpg",
        )

        with self.assertRaisesRegex(ResultCatalogError, "absolute path"):
            ResultCatalog.from_config(self.config_path).load_latest()

    def test_rejects_original_path_outside_image_root(self) -> None:
        self._manifest(
            "原图穿越",
            "2026-07-18T13:00:00+08:00",
            copied_file="副本.jpg",
            relative_path="../../图库外.jpg",
        )

        with self.assertRaisesRegex(ResultCatalogError, "unsafe path traversal"):
            ResultCatalog.from_config(self.config_path).load_latest()

    def _manifest(
        self,
        directory_name: str,
        created_at: str,
        *,
        copied_file: str,
        relative_path: str = "不存在/原图.jpg",
    ) -> Path:
        output = self.results / directory_name
        output.mkdir()
        # Only safe copied-file fixtures should create a file. Malicious names
        # must never be interpreted by test setup itself.
        if "/" not in copied_file and "\\" not in copied_file:
            (output / copied_file).write_bytes(b"copy")
        self._write_json(
            output / "results.json",
            {
                "created_at": created_at,
                "query_type": "text",
                "status": "ok",
                "sort_mode": "confidence",
                "ranking_diagnostics": {
                    "primary": "ranking_confidence_or_confidence_desc"
                },
                "library_ids": ["library-main"],
                "results": [
                    {
                        "rank": 1,
                        "copied_file": copied_file,
                        "relative_path": relative_path,
                        "library_id": "library-main",
                        "library_name": "二次元图库",
                        "doc_id": "doc-001",
                        "tags": ["原神", "雷电将军"],
                        "matched_tags": ["雷神"],
                        "raw_score": 0.18,
                        "normalized_score": 0.91,
                        "confidence": 0.91,
                        "ranking_confidence": 0.92,
                        "image_confidence": 0.88,
                        "text_confidence": 0.93,
                        "metadata_confidence": 0.72,
                        "image_rank": 2,
                        "text_rank": 1,
                        "metadata_rank": 3,
                        "rank_agreement": 0.8,
                        "match_state": "high",
                        "rank_source": "text",
                        "ranking_model_version": "ranker-test-v1",
                        "ranking_score": 0.94,
                        "feature_schema_version": 1,
                        "ranking_fallback": False,
                        "calibrated_minimum_confidence": 0.35,
                        "calibration_version": "calibration-test-v1",
                        "calibration_scope": "query_type:text",
                        "calibration_fallback": False,
                        "search_features": {
                            "feature_schema_version": 1,
                            "vector_raw_score": 0.18,
                            "vector_confidence": 0.92,
                            "tag_match_score": 0.72,
                            "manual_tag_matches": 1.0,
                            "folder_tag_matches": 1.0,
                            "alias_tag_matches": 0.0,
                            "model_high_confidence_tag_matches": 0.0,
                            "model_tag_matches": 1.0,
                            "vector_tag_matches": 1.0,
                            "identity_match": 1.0,
                            "work_match": 1.0,
                            "action_match": 0.0,
                            "expression_match": 0.0,
                            "scene_match": 0.0,
                            "image_text_agreement": 0.8,
                            "collection_rank": 1.0,
                            "collection_size": 2_074.0,
                            "duplicate_group_size": 1.0,
                            "query_type": "text",
                        },
                    }
                ],
            },
        )
        return output

    def _bulk_manifest(self, directory_name: str, count: int) -> tuple[Path, Path]:
        output = self.results / directory_name
        output.mkdir()
        copied = output / "shared.jpg"
        copied.write_bytes(b"copy")
        manifest = output / "results.json"
        self._write_json(
            manifest,
            {
                "created_at": "2026-07-18T13:00:00+08:00",
                "query_type": "text",
                "status": "ok",
                "library_ids": ["library-main"],
                "results": [
                    {
                        "rank": index + 1,
                        "copied_file": copied.name,
                        "relative_path": "missing/original.jpg",
                        "library_id": "library-main",
                    }
                    for index in range(count)
                ],
            },
        )
        return manifest, copied.resolve()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
