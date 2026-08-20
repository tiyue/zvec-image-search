"""Creative-look capability exposed by the ARW selection module.

Only the camera-rendered embedded JPEG is currently available.  Additional
Sony A7M4 looks stay unavailable until they have been calibrated against
trusted same-scene camera or official reference JPEGs.  This intentionally
prevents an uncalibrated approximation from being presented as a Sony look.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

LOOK_CONFIG_VERSION = "a7m4-as-shot-v2"
DEFAULT_LOOK = "as_shot"
VALID_LOOKS = (DEFAULT_LOOK,)


@dataclass(frozen=True, slots=True)
class LookMeta:
    id: str
    label: str
    description: str


LOOK_META: tuple[LookMeta, ...] = (
    LookMeta(
        DEFAULT_LOOK,
        "拍摄时",
        "使用 ARW 内嵌 JPEG，保留相机实际创意外观",
    ),
)


def is_valid_look(look_id: str) -> bool:
    return look_id in VALID_LOOKS


def look_label(look_id: str) -> str:
    for meta in LOOK_META:
        if meta.id == look_id:
            return meta.label
    return look_id


def apply_creative_look(img: Image.Image, look_id: str) -> Image.Image:
    """Return *img* unchanged for the sole supported camera-rendered look."""

    if not is_valid_look(look_id):
        raise ValueError("Creative look is unavailable without trusted calibration")
    return img


def calibration_note(look_id: str) -> str:
    if not is_valid_look(look_id):
        return "该外观尚无可信 A7M4 同场景参考 JPEG，当前不可用。"
    return "直接使用 ARW 内嵌 JPEG，不模拟 Sony 色彩处理。"


__all__ = [
    "DEFAULT_LOOK",
    "LOOK_CONFIG_VERSION",
    "LOOK_META",
    "VALID_LOOKS",
    "apply_creative_look",
    "calibration_note",
    "is_valid_look",
    "look_label",
]
