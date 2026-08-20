from __future__ import annotations

import unittest

from image_vector_service.image_edit.models import (
    DEFAULT_IMAGE_EDIT_MODEL,
    IMAGE_EDIT_MODELS,
    ImageEditValidationError,
    image_edit_size_rule,
    validate_image_edit_request,
)


def _payload(model: str, **overrides: object) -> dict[str, object]:
    return {
        "model": model,
        "prompt": "保留人物，把背景改成海边",
        "negative_prompt": "模糊",
        "prompt_extend": True,
        **overrides,
    }


class ImageEditModelTests(unittest.TestCase):
    def test_catalog_contains_only_the_confirmed_fourteen_models(self) -> None:
        self.assertEqual(DEFAULT_IMAGE_EDIT_MODEL, "qwen-image-edit-plus")
        self.assertEqual(len(IMAGE_EDIT_MODELS), 14)
        self.assertEqual(len(set(IMAGE_EDIT_MODELS)), 14)
        self.assertIn("qwen-image-edit-plus-2025-10-30", IMAGE_EDIT_MODELS)
        self.assertIn("qwen-image-3.0", IMAGE_EDIT_MODELS)

    def test_qwen_three_and_two_models_use_the_total_pixel_rule(self) -> None:
        models = [
            "qwen-image-3.0-pro",
            "qwen-image-3.0",
            "qwen-image-2.0-pro",
            "qwen-image-2.0-2026-03-03",
        ]
        for model in models:
            with self.subTest(model=model):
                self.assertEqual(image_edit_size_rule(model), "pixel_range")
                self.assertEqual(
                    validate_image_edit_request(_payload(model, size="4096*1024")).size,
                    "4096*1024",
                )
                with self.assertRaisesRegex(ImageEditValidationError, "总像素"):
                    validate_image_edit_request(_payload(model, size="4096*1025"))

    def test_qwen_three_models_also_enforce_the_documented_aspect_ratio(
        self,
    ) -> None:
        self.assertEqual(
            validate_image_edit_request(
                _payload("qwen-image-3.0", size="4096*512")
            ).size,
            "4096*512",
        )
        with self.assertRaisesRegex(ImageEditValidationError, "1:8"):
            validate_image_edit_request(_payload("qwen-image-3.0-pro", size="4097*512"))

        self.assertEqual(
            validate_image_edit_request(
                _payload("qwen-image-2.0-pro", size="4097*512")
            ).size,
            "4097*512",
        )

    def test_plus_and_max_models_validate_each_side(self) -> None:
        models = [
            "qwen-image-edit-plus",
            "qwen-image-edit-plus-2025-12-15",
            "qwen-image-edit-max",
            "qwen-image-edit-max-2026-01-16",
        ]
        for model in models:
            with self.subTest(model=model):
                self.assertEqual(image_edit_size_rule(model), "side_range")
                self.assertEqual(
                    validate_image_edit_request(_payload(model, size="512*2048")).size,
                    "512*2048",
                )
                with self.assertRaisesRegex(ImageEditValidationError, "宽度和高度"):
                    validate_image_edit_request(_payload(model, size="511*2048"))

    def test_legacy_image_edit_rejects_size_and_disables_prompt_rewrite(
        self,
    ) -> None:
        with self.assertRaisesRegex(ImageEditValidationError, "不支持指定输出尺寸"):
            validate_image_edit_request(_payload("qwen-image-edit", size="1024*1024"))

        request = validate_image_edit_request(_payload("qwen-image-edit"))
        self.assertIsNone(request.size)
        self.assertFalse(request.prompt_extend)

    def test_invalid_seed_container_is_reported_as_validation_error(self) -> None:
        with self.assertRaisesRegex(ImageEditValidationError, "随机种子必须是整数"):
            validate_image_edit_request(
                _payload("qwen-image-edit-plus", seed={"unexpected": True})
            )


if __name__ == "__main__":
    unittest.main()
