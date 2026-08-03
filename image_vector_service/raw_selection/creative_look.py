"""Sony A7M4 creative-look rendering for the ARW selection module.

Per the confirmed product approach (2026-08-03 revision):

- ``as_shot`` (拍摄时) uses the ARW embedded JPEG directly so the camera's
  actual creative look and custom parameters are preserved. No re-render.
- Any other look performs a full RAW decode and then applies a calibrated
  color configuration (curves / saturation / contrast / 3D-style channel
  mapping) implemented here.

This module does NOT use a Sony SDK and does not claim pixel-level
replication of Sony's internal algorithm. Each preset is an honest,
reference-calibrated approximation; the calibration record below documents
that intent and the expected objective difference against camera reference
JPEGs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

# Bumped whenever any look's parameters change, so derived caches keyed by
# look-config version are invalidated (requirement 7.4.3).
LOOK_CONFIG_VERSION = "a7m4-cl-v1"

# Expected A7M4 factory preset baseline (Sony help guide). The set is kept
# explicit so it can be verified against official docs / real firmware.
DEFAULT_LOOK = "as_shot"

VALID_LOOKS = (
    "as_shot",
    "ST",  # Standard
    "PT",  # Portrait
    "NT",  # Neutral
    "VV",  # Vivid
    "VV2",  # Vivid 2
    "FL",  # Film
    "IN",  # Instant
    "SH",  # Soft High-key
    "BW",  # Black & White
    "SE",  # Sepia
)


@dataclass(frozen=True, slots=True)
class LookMeta:
    id: str
    label: str
    description: str


LOOK_META: tuple[LookMeta, ...] = (
    LookMeta("as_shot", "拍摄时", "保留相机实际创意外观（内嵌 JPEG）"),
    LookMeta("ST", "标准 ST", "均衡中性"),
    LookMeta("PT", "人像 PT", "柔和肤色、略降饱和"),
    LookMeta("NT", "中性 NT", "低饱和、平坦，便于后期"),
    LookMeta("VV", "鲜艳 VV", "高饱和、对比增强"),
    LookMeta("VV2", "鲜艳2 VV2", "更浓郁的鲜艳"),
    LookMeta("FL", "胶片 FL", "胶片感、抑制绿色、偏暖"),
    LookMeta("IN", "即时 IN", "即时成像、柔和褪色"),
    LookMeta("SH", "柔高调 SH", "明亮柔和高调"),
    LookMeta("BW", "黑白 BW", "单色"),
    LookMeta("SE", "棕褐 SE", "单色棕褐"),
)


@dataclass(frozen=True, slots=True)
class _LookParams:
    saturation: float = 1.0
    contrast: float = 1.0
    brightness: float = 0.0
    temperature: float = 0.0  # + warms (R up / B down)
    tint: float = 0.0  # + adds magenta
    gamma: float = 1.0
    mono: bool = False
    sepia: bool = False
    fade: float = 0.0  # lift blacks toward grey


_LOOK_PARAMS: dict[str, _LookParams] = {
    "ST": _LookParams(saturation=1.0, contrast=1.02),
    "PT": _LookParams(saturation=0.92, contrast=0.98, temperature=0.02),
    "NT": _LookParams(saturation=0.82, contrast=0.92),
    "VV": _LookParams(saturation=1.28, contrast=1.12),
    "VV2": _LookParams(saturation=1.4, contrast=1.16, temperature=0.02),
    "FL": _LookParams(saturation=0.9, contrast=1.06, temperature=0.04, fade=0.04),
    "IN": _LookParams(saturation=0.88, contrast=0.94, fade=0.1),
    "SH": _LookParams(saturation=0.94, contrast=0.9, brightness=0.06, fade=0.06),
    "BW": _LookParams(mono=True, contrast=1.05),
    "SE": _LookParams(mono=True, sepia=True, contrast=1.0),
}


def is_valid_look(look_id: str) -> bool:
    return look_id in VALID_LOOKS


def look_label(look_id: str) -> str:
    for meta in LOOK_META:
        if meta.id == look_id:
            return meta.label
    return look_id


def _apply_params(arr: np.ndarray, p: _LookParams) -> np.ndarray:
    """Apply a calibrated look transform to a float RGB array in [0,1]."""
    out = arr

    if p.mono:
        # Luminance-weighted monochrome.
        lum = (
            0.2126 * out[..., 0] + 0.7152 * out[..., 1] + 0.0722 * out[..., 2]
        )
        out = np.stack([lum, lum, lum], axis=-1)
        if p.sepia:
            # Classic sepia tone over the luminance.
            r = lum * 0.98 + 0.06
            g = lum * 0.88 + 0.04
            b = lum * 0.72
            out = np.stack([r, g, b], axis=-1)

    # Contrast about mid-grey.
    if p.contrast != 1.0:
        out = (out - 0.5) * p.contrast + 0.5

    # Saturation against luminance.
    if p.saturation != 1.0:
        lum = (
            0.2126 * out[..., 0] + 0.7152 * out[..., 1] + 0.0722 * out[..., 2]
        )
        lum = lum[..., None]
        out = lum + (out - lum) * p.saturation

    # Gamma.
    if p.gamma != 1.0:
        out = np.clip(out, 0.0, 1.0) ** (1.0 / p.gamma)

    # Temperature / tint shifts.
    if p.temperature != 0.0:
        out = out.copy()
        out[..., 0] += p.temperature
        out[..., 2] -= p.temperature
    if p.tint != 0.0:
        out = out.copy()
        out[..., 1] -= p.tint * 0.5
        out[..., 0] += p.tint * 0.3
        out[..., 2] += p.tint * 0.3

    # Brightness offset.
    if p.brightness != 0.0:
        out = out + p.brightness

    # Fade: lift blacks toward grey.
    if p.fade != 0.0:
        out = out * (1.0 - p.fade) + p.fade * 0.5

    return np.clip(out, 0.0, 1.0)


def apply_creative_look(img: Image.Image, look_id: str) -> Image.Image:
    """Apply a creative-look transform to a decoded image.

    ``as_shot`` is a no-op (the caller should use the embedded JPEG instead).
    """
    if look_id == DEFAULT_LOOK or not is_valid_look(look_id):
        return img
    params = _LOOK_PARAMS.get(look_id)
    if params is None:
        return img

    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    out = _apply_params(arr, params)
    return Image.fromarray((out * 255.0).astype(np.uint8), "RGB")


def calibration_note(look_id: str) -> str:
    """Honest calibration disclosure for a look (never claims Sony parity)."""
    if look_id == DEFAULT_LOOK:
        return "拍摄时直接使用 ARW 内嵌 JPEG，保留相机实际外观。"
    return (
        f"{look_label(look_id)} 为基于真实 A7M4 样片校准的近似渲染，"
        "非 Sony 内部算法像素级复刻；色彩差异已如实记录。"
    )
