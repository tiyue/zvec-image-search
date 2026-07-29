from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import image_service
from image_service import build_parser


class CommandLineParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_index_accepts_trailing_tags(self):
        args = self.parser.parse_args(["index", "/images", "人物", "旅行"])
        self.assertEqual(args.tags, ["人物", "旅行"])

    def test_index_help_distinguishes_new_tags_from_root_wide_clear(self):
        help_text = (
            self.parser._subparsers._group_actions[0].choices["index"].format_help()
        )
        self.assertIn("only to images first added", help_text)
        self.assertIn("all existing images", help_text)

    def test_index_main_preserves_new_clear_and_no_change_tag_intent(self):
        cases = (
            (["new", "featured"], ["new", "featured"]),
            (["--clear-tags"], []),
            ([], None),
        )
        for arguments, expected_tags in cases:
            report = Mock(failed=0)
            report.to_dict.return_value = {"operation": "index"}
            with patch.object(image_service, "ImageVectorService") as service_type:
                service_type.return_value.index_folder.return_value = report
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    exit_code = image_service.main(["index", "/images", *arguments])
                self.assertEqual(exit_code, 0)
                call = service_type.return_value.index_folder.call_args
                self.assertEqual(call.kwargs["tags"], expected_tags)

    def test_index_main_rejects_new_tags_with_root_wide_clear(self):
        with patch.object(image_service, "ImageVectorService") as service_type:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(
                    ["index", "/images", "new", "--clear-tags"]
                )
            self.assertEqual(exit_code, 1)
            service_type.return_value.index_folder.assert_not_called()

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
        self.assertEqual(args.search_mode, "semantic")
        self.assertFalse(args.show_low_confidence)
        self.assertFalse(args.show_all_series)

    def test_search_low_confidence_override_is_explicit_and_default_off(self):
        default_args = self.parser.parse_args(["search", "--text", "sunset"])
        override_args = self.parser.parse_args(
            ["search", "--text", "sunset", "--show-low-confidence"]
        )
        self.assertFalse(default_args.show_low_confidence)
        self.assertTrue(override_args.show_low_confidence)

    def test_search_series_diversity_can_be_disabled_explicitly(self):
        default_args = self.parser.parse_args(["search", "--text", "sunset"])
        override_args = self.parser.parse_args(
            ["search", "--text", "sunset", "--show-all-series"]
        )
        self.assertFalse(default_args.show_all_series)
        self.assertTrue(override_args.show_all_series)

    def test_search_main_forwards_low_confidence_override(self):
        report = Mock()
        report.to_dict.return_value = {"operation": "search"}
        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.search_by_text.return_value = report
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(
                    ["search", "--text", "sunset", "--show-low-confidence"]
                )
        self.assertEqual(exit_code, 0)
        self.assertTrue(
            service_type.return_value.search_by_text.call_args.kwargs[
                "show_low_confidence"
            ]
        )

    def test_search_main_forwards_series_diversity_override(self):
        report = Mock()
        report.to_dict.return_value = {"operation": "search"}
        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.search_by_text.return_value = report
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(
                    ["search", "--text", "sunset", "--show-all-series"]
                )
        self.assertEqual(exit_code, 0)
        self.assertFalse(
            service_type.return_value.search_by_text.call_args.kwargs[
                "diversify_results"
            ]
        )

    def test_tag_search_mode_uses_local_tag_search_entrypoint(self):
        report = Mock()
        report.to_dict.return_value = {"operation": "tag-search"}
        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.search_by_tags.return_value = report
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(
                    [
                        "search",
                        "--text",
                        "\u795e",
                        "--search-mode",
                        "tags",
                        "--tags",
                        "\u89d2\u8272",
                        "--tag-mode",
                        "any",
                    ]
                )
        self.assertEqual(exit_code, 0)
        service_type.return_value.search_by_tags.assert_called_once()
        call = service_type.return_value.search_by_tags.call_args
        self.assertEqual(call.args, ("\u795e",))
        self.assertEqual(call.kwargs["tags"], ["\u89d2\u8272"])
        self.assertEqual(call.kwargs["tag_mode"], "any")
        service_type.return_value.search_by_text.assert_not_called()

    def test_tag_search_mode_rejects_image_input(self):
        with (
            patch.object(image_service, "ImageVectorService") as service_type,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = image_service.main(
                [
                    "search",
                    "--text",
                    "tag",
                    "--image",
                    "query.png",
                    "--search-mode",
                    "tags",
                ]
            )
        self.assertEqual(exit_code, 1)
        service_type.return_value.search_by_tags.assert_not_called()

    def test_legacy_top_k_alias_remains_compatible(self):
        args = self.parser.parse_args(["search", "--text", "sunset", "--top-k", "3"])
        self.assertEqual(args.tk, 3)

    def test_metadata_backfill_is_explicit_and_forwards_request_limit(self):
        args = self.parser.parse_args(["metadata-backfill", "--max-images", "321"])
        self.assertEqual(args.max_images, 321)

        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.backfill_metadata_embeddings.return_value = {
                "operation": "metadata_backfill",
                "failed": 0,
            }
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(
                    ["metadata-backfill", "--max-images", "321"]
                )

        self.assertEqual(exit_code, 0)
        service_type.return_value.backfill_metadata_embeddings.assert_called_once_with(
            max_images=321
        )

    def test_metadata_backfill_returns_partial_exit_code_for_item_failures(self):
        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.backfill_metadata_embeddings.return_value = {
                "operation": "metadata_backfill",
                "failed": 2,
            }
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(["metadata-backfill"])

        self.assertEqual(exit_code, 4)

    def test_main_closes_service_after_successful_command(self):
        with patch.object(image_service, "ImageVectorService") as service_type:
            service_type.return_value.stats.return_value = {"count": 0}
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = image_service.main(["stats"])

        self.assertEqual(exit_code, 0)
        service_type.return_value.close.assert_called_once_with()

    def test_migration_does_not_create_image_service(self):
        with (
            patch.object(image_service, "ImageVectorService") as service_type,
            patch.object(image_service, "migrate_schema", return_value={"changed": 0}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = image_service.main(["migrate-schema", "--dry-run"])

        self.assertEqual(exit_code, 0)
        service_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
