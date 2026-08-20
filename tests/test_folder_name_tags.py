from __future__ import annotations

import unittest

from image_vector_service.folder_name_tags import (
    derive_folder_name_tags,
    folder_name_tags_for_relative_path,
)


class FolderNameTagsTest(unittest.TestCase):
    def test_mixed_name_keeps_full_and_chinese_only_tags(self) -> None:
        self.assertEqual(
            derive_folder_name_tags("Raiden雷电将军 写真 [35P-417MB]_jpg"),
            ("Raiden雷电将军", "雷电将军", "写真"),
        )

    def test_blacklisted_or_ascii_folder_falls_back_to_parent(self) -> None:
        self.assertEqual(
            folder_name_tags_for_relative_path(
                "原神/自拍+小视频/001.jpg",
                "图库",
            ),
            ("原神",),
        )
        self.assertEqual(
            folder_name_tags_for_relative_path(
                "原神/Vol.12/001.jpg",
                "图库",
            ),
            ("原神",),
        )

    def test_metadata_is_removed_without_creating_dirty_tags(self) -> None:
        self.assertEqual(
            derive_folder_name_tags("雷电将军 120P-1.2GB_png"),
            ("雷电将军",),
        )

    def test_root_is_used_without_walking_above_it(self) -> None:
        self.assertEqual(
            folder_name_tags_for_relative_path("120P-1.2GB/001.jpg", "原神图库"),
            ("原神图库",),
        )
        self.assertEqual(
            folder_name_tags_for_relative_path("001.jpg", "ASCII-library"),
            (),
        )

    def test_invalid_relative_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            folder_name_tags_for_relative_path("../outside.jpg", "图库")


if __name__ == "__main__":
    unittest.main()
