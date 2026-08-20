from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from image_vector_service.result_exporter import (
    RESULT_OWNERSHIP_KIND,
    RESULT_OWNERSHIP_MARKER,
    RESULT_OWNERSHIP_SCHEMA_VERSION,
    cleanup_search_results,
)


class SearchResultsCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = Path(self.temporary.name) / "results"
        self.results.mkdir()

    def _owned(self, name: str, *, extra: str | None = None) -> Path:
        directory = self.results / name
        directory.mkdir()
        copied = directory / "001_result.jpg"
        copied.write_bytes(b"image")
        (directory / RESULT_OWNERSHIP_MARKER).write_text(
            json.dumps(
                {
                    "schema_version": RESULT_OWNERSHIP_SCHEMA_VERSION,
                    "kind": RESULT_OWNERSHIP_KIND,
                }
            ),
            encoding="utf-8",
        )
        (directory / "results.json").write_text(
            json.dumps(
                {
                    "output_dir": str(directory.resolve()),
                    "created_at": "2026-01-01T00:00:00+08:00",
                    "results": [{"copied_file": copied.name}],
                }
            ),
            encoding="utf-8",
        )
        if extra is not None:
            (directory / extra).write_text("user data", encoding="utf-8")
        return directory

    def test_keeps_latest_three_by_generated_name_not_mutable_mtime(self) -> None:
        directories = [
            self._owned(f"原神_2026010{day}_120000_000") for day in range(1, 6)
        ]
        # Deliberately make the oldest generated directory look newest on disk.
        os.utime(directories[0], (2_000_000_000, 2_000_000_000))
        os.utime(directories[-1], (1_000_000_000, 1_000_000_000))

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 2)
        self.assertFalse(directories[0].exists())
        self.assertFalse(directories[1].exists())
        self.assertTrue(all(path.exists() for path in directories[2:]))

    def test_unknown_or_user_added_content_is_never_deleted(self) -> None:
        safe_latest = [
            self._owned(f"查询_2026020{day}_120000_000") for day in range(2, 5)
        ]
        unsafe = self._owned(
            "查询_20260201_120000_000",
            extra="用户后来放入的说明.txt",
        )
        quarantine = self.results / "quarantine"
        quarantine.mkdir()
        (quarantine / "failed.jpg").write_bytes(b"failed")

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 0)
        self.assertTrue(unsafe.exists())
        self.assertTrue(quarantine.exists())
        reasons = {
            Path(item["path"]).name: item["reason"] for item in report["skipped_paths"]
        }
        self.assertIn("undeclared content", reasons[unsafe.name])
        self.assertIn("not a generated search result", reasons[quarantine.name])
        self.assertTrue(all(path.exists() for path in safe_latest))

    def test_dry_run_reports_candidates_without_deleting(self) -> None:
        directories = [
            self._owned(f"图片搜索_2026030{day}_120000_000") for day in range(1, 5)
        ]
        report = cleanup_search_results(
            self.results,
            keep_latest=3,
            dry_run=True,
        )
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["deleted"], 0)
        self.assertEqual(len(report["would_delete_paths"]), 1)
        self.assertTrue(all(path.exists() for path in directories))

    def test_nested_content_blocks_recursive_deletion(self) -> None:
        unsafe = self._owned("查询_20260401_120000_000")
        nested = unsafe / "用户目录"
        nested.mkdir()
        (nested / "keep.txt").write_text("keep", encoding="utf-8")
        for day in range(2, 5):
            self._owned(f"查询_2026040{day}_120000_000")

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 0)
        self.assertTrue((nested / "keep.txt").is_file())
        reason = next(
            item["reason"]
            for item in report["skipped_paths"]
            if Path(item["path"]).name == unsafe.name
        )
        self.assertIn("undeclared content", reason)

    def test_invalid_generated_timestamp_is_skipped_without_stopping_cleanup(
        self,
    ) -> None:
        invalid = self._owned("query_20269999_999999_999")
        valid = [self._owned(f"query_2026050{day}_120000_000") for day in range(1, 5)]

        report = cleanup_search_results(self.results, keep_latest=3)

        self.assertEqual(report["deleted"], 1)
        self.assertTrue(invalid.exists())
        self.assertFalse(valid[0].exists())
        self.assertTrue(all(path.exists() for path in valid[1:]))
        reason = next(
            item["reason"]
            for item in report["skipped_paths"]
            if Path(item["path"]).name == invalid.name
        )
        self.assertEqual(reason, "directory timestamp is invalid")


if __name__ == "__main__":
    unittest.main()
