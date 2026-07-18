from __future__ import annotations

import unittest

from zvec_desktop.search_service import SearchValidationError
from zvec_desktop.search_ui_state import (
    SEMANTIC_MODE_LABEL,
    TAG_MODE_LABEL,
    SearchFormValues,
    build_search_request,
    format_search_progress,
    outcome_error_text,
)


class SearchFormStateTest(unittest.TestCase):
    def test_text_image_combined_and_fuzzy_tag_inputs_use_fixed_budgets(self) -> None:
        text = build_search_request(
            SearchFormValues("角色动作", "", SEMANTIC_MODE_LABEL, True, ()),
            enabled_library_ids=("people", "anime"),
        )
        self.assertEqual(text.text, "角色动作")
        self.assertIsNone(text.image_path)
        self.assertEqual((text.top_k, text.candidate_k), (15, 50))
        self.assertEqual(text.library_ids, ())

        image = build_search_request(
            SearchFormValues("", "query.jpg", SEMANTIC_MODE_LABEL, False, ("people",)),
            enabled_library_ids=("people", "anime"),
        )
        self.assertEqual(str(image.image_path), "query.jpg")
        self.assertEqual(image.library_ids, ("people",))

        combined = build_search_request(
            SearchFormValues(
                "微笑",
                "query.jpg",
                SEMANTIC_MODE_LABEL,
                False,
                ("people", "anime"),
            ),
            enabled_library_ids=("people", "anime"),
        )
        self.assertEqual(combined.text, "微笑")
        self.assertIsNotNone(combined.image_path)

        tags = build_search_request(
            SearchFormValues("原", "", TAG_MODE_LABEL, True, ()),
            enabled_library_ids=("people", "anime"),
        )
        self.assertEqual(tags.search_mode, "tags")
        self.assertEqual(tags.text, "原")

    def test_custom_library_selection_is_required_and_cannot_be_stale(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "至少选择一个图库"):
            build_search_request(
                SearchFormValues("角色", "", SEMANTIC_MODE_LABEL, False, ()),
                enabled_library_ids=("people",),
            )
        with self.assertRaisesRegex(SearchValidationError, "已停用"):
            build_search_request(
                SearchFormValues(
                    "角色",
                    "",
                    SEMANTIC_MODE_LABEL,
                    False,
                    ("removed",),
                ),
                enabled_library_ids=("people",),
            )

    def test_tag_mode_rejects_query_image_before_submission(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "不能同时提供图片"):
            build_search_request(
                SearchFormValues("原", "query.jpg", TAG_MODE_LABEL, True, ()),
                enabled_library_ids=("people",),
            )


class SearchStatusFormattingTest(unittest.TestCase):
    def test_progress_prefers_backend_message_then_numeric_progress(self) -> None:
        self.assertEqual(
            format_search_progress(
                {
                    "status": "running",
                    "progress": {"message": "正在搜索人物图库", "current": 1},
                }
            ),
            "正在搜索 · 正在搜索人物图库",
        )
        self.assertEqual(
            format_search_progress(
                {"status": "running", "progress": {"current": 12, "total": 50}}
            ),
            "正在搜索 · 12/50",
        )

    def test_failure_and_cancel_messages_promise_to_keep_old_results(self) -> None:
        self.assertIn("原有结果保持不变", outcome_error_text("cancelled", None))
        failed = outcome_error_text(
            "failed",
            {"code": "model_error", "message": "模型请求失败"},
        )
        self.assertIn("模型请求失败", failed)
        self.assertIn("model_error", failed)
        self.assertIn("原有结果保持不变", failed)


if __name__ == "__main__":
    unittest.main()
