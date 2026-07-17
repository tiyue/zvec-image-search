from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from image_vector_service.tags import (
    clean_generated_tag,
    folder_tags_for_image,
    is_technical_metadata_tag,
)


class GeneratedTagTest(unittest.TestCase):
    def test_folder_name_removes_image_count_and_file_size(self):
        self.assertEqual(
            clean_generated_tag("【原神】_刻晴-旗袍-120P-1.2GB"),
            "原神-刻晴-旗袍",
        )

    def test_metadata_only_folder_is_empty(self):
        self.assertEqual(clean_generated_tag("120P-1.2GB"), "")
        self.assertEqual(clean_generated_tag("80张-2.4G"), "")
        self.assertTrue(is_technical_metadata_tag("120 张 / 850 MB"))

    def test_non_metadata_numbers_are_preserved(self):
        self.assertEqual(clean_generated_tag("初音未来2026-写真"), "初音未来2026-写真")
        self.assertEqual(clean_generated_tag("尼尔-2B-Cosplay"), "尼尔-2B-Cosplay")

    def test_nearest_meaningful_parent_is_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "原神合集"
            folder = root / "120P-1.2GB"
            folder.mkdir(parents=True)
            image = folder / "001.jpg"
            image.touch()

            self.assertEqual(folder_tags_for_image(image, root), ("原神合集",))

    def test_direct_parent_is_preferred(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "图库"
            folder = root / "原神-刻晴-80张-900MB"
            folder.mkdir(parents=True)
            image = folder / "001.jpg"
            image.touch()

            self.assertEqual(folder_tags_for_image(image, root), ("原神-刻晴",))

    def test_lookup_does_not_escape_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "120P-1.2GB"
            root.mkdir()
            image = root / "001.jpg"
            image.touch()

            self.assertEqual(folder_tags_for_image(image, root), ())


if __name__ == "__main__":
    unittest.main()
