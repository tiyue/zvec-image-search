from __future__ import annotations

import unittest

from image_service import build_parser


class CommandLineParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_index_accepts_trailing_tags(self):
        args = self.parser.parse_args(["index", "/data/roots/main", "人物", "旅行"])
        self.assertEqual(args.tags, ["人物", "旅行"])

    def test_search_uses_tk_and_tag_filter_options(self):
        args = self.parser.parse_args(
            [
                "search",
                "--text",
                "海边日落",
                "--tk",
                "20",
                "--tags",
                "风景",
                "日落",
                "--tag-mode",
                "any",
            ]
        )
        self.assertEqual(args.tk, 20)
        self.assertEqual(args.tags, ["风景", "日落"])
        self.assertEqual(args.tag_mode, "any")

    def test_legacy_top_k_alias_remains_compatible(self):
        args = self.parser.parse_args(["search", "--text", "sunset", "--top-k", "3"])
        self.assertEqual(args.tk, 3)


if __name__ == "__main__":
    unittest.main()
