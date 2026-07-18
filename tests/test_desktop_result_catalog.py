from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

import zvec_desktop.result_catalog as result_catalog_module
from zvec_desktop.result_catalog import ResultCatalog, ResultCatalogError, SearchResult


class ResultCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
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
        self.assertEqual(page.source_label, "最新搜索")

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

        page = ResultCatalog.from_config(self.config_path).load_latest()

        self.assertEqual(page.manifest_path, complete / "results.json")

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
                        "confidence": 0.91,
                        "match_state": "high",
                        "rank_source": "text",
                    }
                ],
            },
        )
        return output

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
