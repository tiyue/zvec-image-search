from __future__ import annotations

import pytest

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


def test_catalog_contains_only_the_confirmed_fourteen_models() -> None:
    assert DEFAULT_IMAGE_EDIT_MODEL == "qwen-image-edit-plus"
    assert len(IMAGE_EDIT_MODELS) == 14
    assert len(set(IMAGE_EDIT_MODELS)) == 14
    assert "qwen-image-edit-plus-2025-10-30" in IMAGE_EDIT_MODELS
    assert "qwen-image-3.0" in IMAGE_EDIT_MODELS


@pytest.mark.parametrize(
    "model",
    [
        "qwen-image-3.0-pro",
        "qwen-image-3.0",
        "qwen-image-2.0-pro",
        "qwen-image-2.0-2026-03-03",
    ],
)
def test_qwen_three_and_two_models_use_the_total_pixel_rule(model: str) -> None:
    assert image_edit_size_rule(model) == "pixel_range"
    assert validate_image_edit_request(_payload(model, size="4096*1024")).size == (
        "4096*1024"
    )
    with pytest.raises(ImageEditValidationError, match="总像素"):
        validate_image_edit_request(_payload(model, size="4096*1025"))


def test_qwen_three_models_also_enforce_the_documented_aspect_ratio() -> None:
    assert (
        validate_image_edit_request(_payload("qwen-image-3.0", size="4096*512")).size
        == "4096*512"
    )
    with pytest.raises(ImageEditValidationError, match="1:8"):
        validate_image_edit_request(_payload("qwen-image-3.0-pro", size="4097*512"))

    assert (
        validate_image_edit_request(
            _payload("qwen-image-2.0-pro", size="4097*512")
        ).size
        == "4097*512"
    )


@pytest.mark.parametrize(
    "model",
    [
        "qwen-image-edit-plus",
        "qwen-image-edit-plus-2025-12-15",
        "qwen-image-edit-max",
        "qwen-image-edit-max-2026-01-16",
    ],
)
def test_plus_and_max_models_validate_each_side(model: str) -> None:
    assert image_edit_size_rule(model) == "side_range"
    assert validate_image_edit_request(_payload(model, size="512*2048")).size == (
        "512*2048"
    )
    with pytest.raises(ImageEditValidationError, match="宽度和高度"):
        validate_image_edit_request(_payload(model, size="511*2048"))


def test_legacy_image_edit_rejects_size_and_disables_prompt_rewrite() -> None:
    with pytest.raises(ImageEditValidationError, match="不支持指定输出尺寸"):
        validate_image_edit_request(_payload("qwen-image-edit", size="1024*1024"))

    request = validate_image_edit_request(_payload("qwen-image-edit"))
    assert request.size is None
    assert request.prompt_extend is False


def test_invalid_seed_container_is_reported_as_validation_error() -> None:
    with pytest.raises(ImageEditValidationError, match="随机种子必须是整数"):
        validate_image_edit_request(
            _payload("qwen-image-edit-plus", seed={"unexpected": True})
        )
