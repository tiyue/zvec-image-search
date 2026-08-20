from __future__ import annotations

import hashlib
import unittest

from image_vector_service.metadata_text import (
    build_metadata_text,
    metadata_text_sha256,
)


def _accepted_annotation() -> dict[str, object]:
    return {
        "status": "accepted",
        "accepted_tags": [
            "坐姿",
            "微笑",
            "礼服",
            "花或花束",
            "海边",
            "摆拍",
            "刻晴",
            "原神",
        ],
        "proposed_tags": ["不得进入文本"],
        "rejected_tags": ["也不得进入文本"],
        "structured": {
            "description": "  刻晴\t海边写真  ",
            "fields": {
                "pose": {
                    "values": ["pose_sitting"],
                    "labels": ["坐姿"],
                    "confidence": 0.98,
                },
                "action": {
                    "values": ["action_posing"],
                    "labels": ["摆拍"],
                    "confidence": 0.97,
                },
                "expression": {
                    "values": ["expression_smile", "expression_serious"],
                    "labels": ["微笑", "严肃"],
                    "confidence": 0.96,
                },
                "clothing": {
                    "values": ["clothing_evening_gown"],
                    "labels": ["礼服"],
                    "confidence": 0.95,
                },
                "prop": {
                    "values": ["prop_flower"],
                    "labels": ["花或花束"],
                    "confidence": 0.94,
                },
                "scene": {
                    "values": ["scene_beach"],
                    "labels": ["海边"],
                    "confidence": 0.93,
                },
                "lighting": {
                    "values": ["lighting_natural"],
                    "labels": ["自然光"],
                    "confidence": 0.92,
                },
            },
            "entities": {
                "character": [
                    {
                        "name": "刻晴",
                        "state": "confirmed",
                        "confidence": 0.99,
                    }
                ],
                "work": [
                    {
                        "name": "原神",
                        "state": "confirmed",
                        "confidence": 0.99,
                    }
                ],
            },
        },
    }


class MetadataTextTests(unittest.TestCase):
    def test_builds_fixed_sections_and_deduplicates_accepted_values(self) -> None:
        entry = {
            "tags": ["精选", "原神", "刻晴"],
            "folder_tags": ["套图A", "原神"],
            "accepted_auto_tags": [
                "坐姿",
                "摆拍",
                "微笑",
                "礼服",
                "花或花束",
                "海边",
                "刻晴",
                "原神",
            ],
        }

        result = build_metadata_text(entry, _accepted_annotation())

        self.assertEqual(
            result.text,
            "\n".join(
                [
                    "角色：刻晴",
                    "作品：原神",
                    "姿势：坐姿",
                    "动作：摆拍",
                    "神态：微笑",
                    "服装：礼服",
                    "道具：花或花束",
                    "场景：海边",
                    "标签：精选、套图A",
                    "描述：刻晴 海边写真",
                ]
            ),
        )
        self.assertNotIn("自然光", result.text)
        self.assertNotIn("严肃", result.text)
        self.assertNotIn("不得进入文本", result.text)
        self.assertNotIn("也不得进入文本", result.text)
        self.assertEqual(result.source_counts["manual_tags"], 3)
        self.assertEqual(result.source_counts["folder_tags"], 1)
        self.assertEqual(result.source_counts["fields"], 6)
        self.assertEqual(result.source_counts["entities"], 2)
        self.assertFalse(result.truncated)
        self.assertEqual(
            result.sha256,
            hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
        )

    def test_pending_includes_only_individually_accepted_values(self) -> None:
        entry = {
            "tags": [],
            "folder_tags": [],
            "accepted_auto_tags": ["刻晴", "坐姿"],
        }
        annotation = _accepted_annotation()
        annotation["status"] = "pending_review"
        annotation["accepted_tags"] = ["刻晴", "坐姿"]

        result = build_metadata_text(entry, annotation)

        self.assertEqual(result.text, "角色：刻晴\n姿势：坐姿")
        self.assertNotIn("原神", result.text)
        self.assertNotIn("微笑", result.text)
        self.assertNotIn("描述", result.text)

    def test_pending_proposed_and_rejected_tags_never_enter_text(self) -> None:
        annotation = {
            "status": "pending_review",
            "accepted_tags": ["已审核"],
            "proposed_tags": ["待审核"],
            "rejected_tags": ["已拒绝"],
            "description": "未经审核的描述",
            "entities": {"character": [{"name": "猜测角色", "state": "suggested"}]},
        }

        result = build_metadata_text(
            {
                "tags": ["人工标签"],
                "folder_tags": ["文件夹标签"],
                "accepted_auto_tags": [],
            },
            annotation,
        )

        self.assertEqual(result.text, "标签：人工标签、文件夹标签、已审核")
        self.assertNotIn("待审核", result.text)
        self.assertNotIn("已拒绝", result.text)
        self.assertNotIn("未经审核", result.text)
        self.assertNotIn("猜测角色", result.text)

    def test_accepted_tag_can_confirm_an_older_suggested_entity(self) -> None:
        result = build_metadata_text(
            {"accepted_auto_tags": ["大凤"]},
            {
                "status": "pending_review",
                "structured": {
                    "entities": {
                        "character": [
                            {
                                "name": "大凤",
                                "state": "suggested",
                                "confidence": 0.70,
                            }
                        ]
                    }
                },
            },
        )

        self.assertEqual(result.text, "角色：大凤")

    def test_machine_codes_are_converted_when_labels_are_absent(self) -> None:
        result = build_metadata_text(
            {"accepted_auto_tags": ["坐姿"]},
            {
                "status": "pending_review",
                "structured": {
                    "fields": {
                        "pose": {
                            "values": ["pose_sitting"],
                            "confidence": 0.99,
                        }
                    }
                },
            },
        )

        self.assertEqual(result.text, "姿势：坐姿")

    def test_normalization_and_sorting_make_hash_independent_of_input_order(
        self,
    ) -> None:
        first = build_metadata_text(
            {
                "tags": ["Ｂ", "A", "A"],
                "folder_tags": ["  C\tD  "],
                "accepted_auto_tags": [],
            },
            None,
        )
        second = build_metadata_text(
            {
                "tags": ["A", "B"],
                "folder_tags": ["C D"],
                "accepted_auto_tags": [],
            },
            {},
        )

        self.assertEqual(first.text, "标签：A、B、C D")
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.sha256, second.sha256)

    def test_length_limit_is_deterministic_and_marks_truncation(self) -> None:
        entry = {"tags": [f"标签-{index:02d}" for index in range(30)]}

        first = build_metadata_text(entry, None, max_chars=24)
        second = build_metadata_text(entry, None, max_chars=24)

        self.assertLessEqual(len(first.text), 24)
        self.assertTrue(first.text.endswith("…"))
        self.assertTrue(first.truncated)
        self.assertEqual(first, second)

    def test_empty_input_has_the_standard_empty_digest(self) -> None:
        result = build_metadata_text(None, None)

        self.assertTrue(result.empty)
        self.assertEqual(result.text, "")
        self.assertEqual(result.sha256, hashlib.sha256(b"").hexdigest())
        self.assertFalse(result.truncated)

    def test_malformed_nested_values_are_ignored_but_root_contract_is_strict(
        self,
    ) -> None:
        result = build_metadata_text(
            {"tags": [None, 123, "", "有效标签"]},
            {
                "status": "accepted",
                "structured": {
                    "fields": ["invalid"],
                    "entities": "invalid",
                    "description": 123,
                },
            },
        )

        self.assertEqual(result.text, "标签：有效标签")
        with self.assertRaisesRegex(TypeError, "entry must be a mapping"):
            build_metadata_text(["invalid"], None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "max_chars"):
            build_metadata_text({}, {}, max_chars=0)

    def test_hash_helper_rejects_non_text_values(self) -> None:
        self.assertEqual(
            metadata_text_sha256("abc"),
            hashlib.sha256(b"abc").hexdigest(),
        )
        with self.assertRaisesRegex(TypeError, "must be a string"):
            metadata_text_sha256(b"abc")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
