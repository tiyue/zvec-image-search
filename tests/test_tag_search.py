from __future__ import annotations

import unittest
from typing import Any, cast

from image_vector_service.tag_search import (
    TagCatalog,
    TagExpansionTooBroadError,
    TagSearchError,
    normalize_tag_search_text,
    quote_zvec_filter_string,
    resolve_tag_search,
    result_matches_tag_plan,
    split_tag_search_query,
)


class TagCatalogTest(unittest.TestCase):
    def test_chinese_fragment_matches_any_position_in_full_tag(self):
        catalog = TagCatalog(["原神", "神里绫华", "崩坏：星穹铁道"])

        prefix = catalog.resolve("原")
        suffix = catalog.resolve("神")

        self.assertEqual(prefix.expansions[0].matches, ("原神",))
        self.assertEqual(suffix.expansions[0].matches, ("原神", "神里绫华"))
        self.assertFalse(prefix.matches_nothing)
        self.assertEqual(prefix.zvec_filter, "(tags CONTAIN_ANY ('原神'))")

    def test_matching_uses_nfkc_and_is_case_insensitive(self):
        catalog = TagCatalog(["ＣｏｓＰｌａｙ", "Anime", "写真"])

        plan = catalog.resolve(["cosplay", "ＡＮＩＭＥ"], mode="all")

        self.assertEqual(
            [expansion.matches for expansion in plan.expansions],
            [("ＣｏｓＰｌａｙ",), ("Anime",)],
        )
        self.assertEqual(normalize_tag_search_text(" ＡＢＣ "), "abc")

    def test_all_mode_requires_a_match_group_for_every_fragment(self):
        catalog = TagCatalog(["原神", "原画", "女神", "刻晴"])

        plan = catalog.resolve(["原", "神"], mode="all")

        self.assertFalse(plan.matches_nothing)
        self.assertEqual(plan.matched_tags, ("原画", "原神", "女神"))
        self.assertEqual(
            plan.zvec_filter,
            "(tags CONTAIN_ANY ('原画','原神')) AND (tags CONTAIN_ANY ('原神','女神'))",
        )

        missing = catalog.resolve(["原", "不存在"], mode="all")
        self.assertTrue(missing.matches_nothing)
        self.assertIsNone(missing.zvec_filter)

    def test_any_mode_ignores_empty_groups_but_empty_union_returns_no_results(self):
        catalog = TagCatalog(["原神", "刻晴"])

        partial = catalog.resolve(["不存在", "神"], mode="any")
        empty = catalog.resolve(["不存在", "也不存在"], mode="any")

        self.assertFalse(partial.matches_nothing)
        self.assertEqual(partial.zvec_filter, "tags CONTAIN_ANY ('原神')")
        self.assertTrue(empty.matches_nothing)
        self.assertIsNone(empty.zvec_filter)

    def test_no_fragments_means_no_filter_instead_of_an_empty_result(self):
        plan = TagCatalog(["原神"]).resolve([])

        self.assertFalse(plan.matches_nothing)
        self.assertFalse(plan.has_filter)
        self.assertEqual(plan.expansions, ())

    def test_catalog_and_fragments_are_deduplicated_with_stable_order(self):
        catalog = TagCatalog(["beta", "Alpha", "alpha", "beta", "原神"])

        first = catalog.resolve(["a", "Ａ", "a"], mode="any")
        second = catalog.resolve(["Ａ", "a"], mode="any")

        self.assertEqual(catalog.tags, ("Alpha", "alpha", "beta", "原神"))
        self.assertEqual(len(first.expansions), 1)
        self.assertEqual(first.matched_tags, ("Alpha", "alpha", "beta"))
        self.assertEqual(first.matched_tags, second.matched_tags)
        self.assertEqual(first.zvec_filter, second.zvec_filter)

    def test_case_variants_remain_as_exact_filter_values(self):
        # Matching is case-insensitive, but existing Zvec values are exact. Both
        # spellings must remain so documents using either legacy form are found.
        plan = TagCatalog(["Cosplay", "cosplay"]).resolve("COS")

        self.assertEqual(plan.matched_tags, ("Cosplay", "cosplay"))
        self.assertEqual(
            plan.zvec_filter,
            "(tags CONTAIN_ANY ('Cosplay','cosplay'))",
        )

    def test_expansion_limit_raises_instead_of_silently_truncating(self):
        catalog = TagCatalog(["原神", "原画", "原宿"])

        with self.assertRaises(TagExpansionTooBroadError) as raised:
            catalog.resolve("原", max_expansions_per_fragment=2)

        self.assertEqual(raised.exception.fragment, "原")
        self.assertEqual(raised.exception.match_count, 3)
        self.assertEqual(raised.exception.limit, 2)
        self.assertIn("Refine the tag query", str(raised.exception))

    def test_filter_literals_escape_quotes_backslashes_and_injection_text(self):
        self.assertEqual(quote_zvec_filter_string("O'Reilly"), "'O\\'Reilly'")
        self.assertEqual(
            quote_zvec_filter_string(r"C:\sets\hero"),
            r"'C:\\sets\\hero'",
        )
        self.assertEqual(
            quote_zvec_filter_string("x') OR tags CONTAIN_ANY ('admin"),
            "'x\\') OR tags CONTAIN_ANY (\\'admin'",
        )

        plan = TagCatalog(["O'Reilly"]).resolve("reilly")
        self.assertEqual(
            plan.zvec_filter,
            "(tags CONTAIN_ANY ('O\\'Reilly'))",
        )

    def test_functional_api_builds_the_same_plan(self):
        plan = resolve_tag_search(["原神", "刻晴"], "神", mode="any")

        self.assertEqual(plan.matched_tags, ("原神",))
        self.assertEqual(plan.zvec_filter, "tags CONTAIN_ANY ('原神')")

    def test_tag_query_splitter_supports_common_separators_and_deduplicates(self):
        self.assertEqual(
            split_tag_search_query(" 人物，侧脸 | 夜景/人物； 长发 "),
            ("人物", "侧脸", "夜景", "长发"),
        )
        self.assertEqual(split_tag_search_query("||| 人物 ||"), ("人物",))
        with self.assertRaises(TagSearchError):
            split_tag_search_query("|||")

    def test_invalid_input_is_rejected(self):
        invalid_calls = (
            lambda: TagCatalog([""]),
            lambda: TagCatalog(cast(Any, ["valid", 3])),
            lambda: TagCatalog(["bad\nvalue"]),
            lambda: TagCatalog(["原神"]).resolve(" "),
            lambda: TagCatalog(["原神"]).resolve("原", mode=cast(Any, "invalid")),
            lambda: TagCatalog(["原神"]).resolve("原", max_expansions_per_fragment=0),
        )

        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(TagSearchError):
                call()

    def test_resolved_plan_can_be_applied_without_a_vector_query(self):
        catalog = TagCatalog(["\u539f\u795e", "\u523b\u6674", "\u5d29\u574f"])
        all_plan = catalog.resolve(["\u539f", "\u6674"], mode="all")
        any_plan = catalog.resolve(["\u795e", "\u5d29"], mode="any")

        self.assertTrue(
            result_matches_tag_plan(["\u539f\u795e", "\u523b\u6674"], all_plan)
        )
        self.assertFalse(result_matches_tag_plan(["\u539f\u795e"], all_plan))
        self.assertTrue(result_matches_tag_plan(["\u539f\u795e"], any_plan))
        self.assertTrue(result_matches_tag_plan(["\u5d29\u574f"], any_plan))


if __name__ == "__main__":
    unittest.main()
