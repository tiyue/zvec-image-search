from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.folder_name_tag_settings import (
    FolderNameTagSettingsStore,
)
from image_vector_service.folder_name_tags import (
    FolderNameTagPolicy,
    clean_existing_tags,
    derive_folder_name_tags,
    folder_name_tags_for_relative_path,
)


class FolderNameTagSettingsTest(unittest.TestCase):
    def test_settings_are_casefold_deduplicated_and_persisted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FolderNameTagSettingsStore(Path(temporary))

            saved = store.save({"blacklist": [" Vol ", "vol", "V", "日期"]})

            self.assertEqual(saved["blacklist"], ["Vol", "V", "日期"])
            self.assertEqual(store.load(), saved)
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["blacklist"], ["Vol", "V", "日期"])

    def test_bad_or_empty_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FolderNameTagSettingsStore(Path(temporary))
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text('{"blacklist": []}', encoding="utf-8")

            loaded = store.load()

            self.assertTrue(loaded["using_defaults"])
            self.assertIn("自拍", loaded["blacklist"])


class FolderNameTagPolicyTest(unittest.TestCase):
    def test_short_rules_are_prefixes_and_space_rules_match_whole_folder(self) -> None:
        policy = FolderNameTagPolicy(
            ("V", "cosplay打赏群 预览目录（部分） – Telegraph_files")
        )

        self.assertEqual(derive_folder_name_tags("VIP 原神", policy=policy), ("原神",))
        self.assertEqual(
            derive_folder_name_tags(
                "cosplay打赏群 预览目录（部分） – Telegraph_files",
                policy=policy,
            ),
            (),
        )
        self.assertEqual(
            derive_folder_name_tags(
                "cosplay打赏群 预览目录 原神",
                policy=policy,
            ),
            ("cosplay打赏群", "打赏群", "预览目录", "原神"),
        )

    def test_fallback_uses_only_one_parent(self) -> None:
        policy = FolderNameTagPolicy(("图包",))

        self.assertEqual(
            folder_name_tags_for_relative_path(
                "原神/图包/2/001.jpg",
                "图库",
                policy=policy,
            ),
            (),
        )

    def test_existing_tags_are_filtered_and_deduplicated(self) -> None:
        policy = FolderNameTagPolicy(("V", "自拍"))

        cleaned = clean_existing_tags(
            [" 原神 ", "原神", "原神", "VIP", "自拍预览", "[35P-1GB]", "_jpg"],
            policy=policy,
        )

        self.assertEqual(cleaned.tags, ("原神",))
        self.assertEqual(cleaned.removed_blacklist, 2)
        self.assertEqual(cleaned.removed_legacy, 2)
        self.assertEqual(cleaned.removed_duplicates, 2)


if __name__ == "__main__":
    unittest.main()
