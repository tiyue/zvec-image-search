from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

DEFAULT_IMAGE_EDIT_MODEL: Final = "qwen-image-edit-plus"
IMAGE_EDIT_MODELS: Final = (
    "qwen-image-3.0-pro",
    "qwen-image-3.0",
    "qwen-image-2.0-pro",
    "qwen-image-2.0-pro-2026-06-22",
    "qwen-image-2.0-pro-2026-04-22",
    "qwen-image-2.0-pro-2026-03-03",
    "qwen-image-2.0",
    "qwen-image-2.0-2026-03-03",
    "qwen-image-edit-max",
    "qwen-image-edit-max-2026-01-16",
    "qwen-image-edit-plus",
    "qwen-image-edit-plus-2025-12-15",
    "qwen-image-edit-plus-2025-10-30",
    "qwen-image-edit",
)

_MODEL_SET = frozenset(IMAGE_EDIT_MODELS)
_PIXEL_RANGE_MODELS = frozenset(
    model
    for model in IMAGE_EDIT_MODELS
    if model.startswith("qwen-image-3.0") or model.startswith("qwen-image-2.0")
)
_SIDE_RANGE_MODELS = frozenset(
    model
    for model in IMAGE_EDIT_MODELS
    if model.startswith("qwen-image-edit-max")
    or model.startswith("qwen-image-edit-plus")
)
_SIZE_PATTERN = re.compile(r"^(?P<width>\d{1,5})\*(?P<height>\d{1,5})$")
_MIN_TOTAL_PIXELS = 512 * 512
_MAX_TOTAL_PIXELS = 2048 * 2048
_MAX_PROMPT_CHARACTERS = 32_000

ImageEditSizeRule = Literal["pixel_range", "side_range", "automatic_only"]


class ImageEditValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageEditRequest:
    model: str
    prompt: str
    negative_prompt: str
    size: str | None
    seed: int | None
    prompt_extend: bool

    @property
    def size_rule(self) -> ImageEditSizeRule:
        return image_edit_size_rule(self.model)


def image_edit_size_rule(model: str) -> ImageEditSizeRule:
    if model in _PIXEL_RANGE_MODELS:
        return "pixel_range"
    if model in _SIDE_RANGE_MODELS:
        return "side_range"
    if model == "qwen-image-edit":
        return "automatic_only"
    raise ImageEditValidationError("unsupported_model", "所选模型不支持图片编辑。")


def validate_image_edit_request(payload: Mapping[str, Any]) -> ImageEditRequest:
    allowed = {
        "model",
        "prompt",
        "negative_prompt",
        "size",
        "seed",
        "prompt_extend",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ImageEditValidationError(
            "unknown_fields",
            f"图片编辑请求包含不支持的字段：{', '.join(sorted(unknown))}。",
        )

    model = payload.get("model")
    if not isinstance(model, str) or model not in _MODEL_SET:
        raise ImageEditValidationError("unsupported_model", "所选模型不支持图片编辑。")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageEditValidationError("invalid_prompt", "编辑指令不能为空。")
    prompt = prompt.strip()
    if len(prompt) > _MAX_PROMPT_CHARACTERS:
        raise ImageEditValidationError("invalid_prompt", "编辑指令过长，请精简后重试。")

    negative = payload.get("negative_prompt", "")
    if not isinstance(negative, str):
        raise ImageEditValidationError(
            "invalid_negative_prompt", "反向提示词必须是文本。"
        )
    negative = negative.strip()
    if not model.startswith("qwen-image-3.0") and len(negative) > 500:
        raise ImageEditValidationError(
            "invalid_negative_prompt", "当前模型的反向提示词最多为 500 个字符。"
        )

    prompt_extend = payload.get("prompt_extend", True)
    if not isinstance(prompt_extend, bool):
        raise ImageEditValidationError(
            "invalid_prompt_extend", "提示词智能改写参数必须是布尔值。"
        )

    seed_value = payload.get("seed")
    seed: int | None
    if seed_value is None or seed_value == "":
        seed = None
    elif isinstance(seed_value, bool) or not isinstance(seed_value, int):
        raise ImageEditValidationError("invalid_seed", "随机种子必须是整数。")
    elif not 0 <= seed_value <= 2_147_483_647:
        raise ImageEditValidationError(
            "invalid_seed", "随机种子必须在 0 到 2147483647 之间。"
        )
    else:
        seed = seed_value

    raw_size = payload.get("size")
    size = raw_size.strip() if isinstance(raw_size, str) else None
    if raw_size is not None and not isinstance(raw_size, str):
        raise ImageEditValidationError("invalid_size", "输出尺寸格式无效。")
    if not size:
        size = None
    _validate_size(model, size)

    if model == "qwen-image-edit":
        prompt_extend = False

    return ImageEditRequest(
        model=model,
        prompt=prompt,
        negative_prompt=negative,
        size=size,
        seed=seed,
        prompt_extend=prompt_extend,
    )


def _validate_size(model: str, size: str | None) -> None:
    rule = image_edit_size_rule(model)
    if size is None:
        return
    if rule == "automatic_only":
        raise ImageEditValidationError(
            "unsupported_size", "qwen-image-edit 不支持指定输出尺寸。"
        )
    match = _SIZE_PATTERN.fullmatch(size)
    if match is None:
        raise ImageEditValidationError(
            "invalid_size", "输出尺寸必须使用“宽*高”格式，例如 1024*1024。"
        )
    width = int(match.group("width"))
    height = int(match.group("height"))
    if rule == "pixel_range":
        pixels = width * height
        if not _MIN_TOTAL_PIXELS <= pixels <= _MAX_TOTAL_PIXELS:
            raise ImageEditValidationError(
                "invalid_size",
                "当前模型的输出总像素必须在 512*512 到 2048*2048 之间。",
            )
        if model.startswith("qwen-image-3.0") and (
            width > height * 8 or height > width * 8
        ):
            raise ImageEditValidationError(
                "invalid_size", "Qwen Image 3.0 的输出宽高比必须在 1:8 到 8:1 之间。"
            )
        return
    if not 512 <= width <= 2048 or not 512 <= height <= 2048:
        raise ImageEditValidationError(
            "invalid_size", "当前模型的输出宽度和高度都必须在 512 到 2048 之间。"
        )
