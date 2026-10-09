"""Bounded re-encoding of original images for remote (public) delivery.

Remote delivery must fit a small byte budget so the Android client can browse
over the public tunnel.  This module deliberately makes only simple choices:
the source format/alpha decides a lossless or lossy path, candidate sizes are
compared, and the original bytes are reused only when they are already a small,
conforming image.  The original file is never modified.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from PIL import features as _features

from .image_registry import ImageRegistryError

# Bumping this invalidates every persisted remote variant because the registry
# folds it into the disk-cache namespace.
REMOTE_PROFILE_VERSION = "remote-v1"

_MAX_PIXELS = 200_000_000
_MAX_ENCODE_ATTEMPTS = 6
_HARD_CAP_BYTES = 1024 * 1024
_QUALITY_CEILING = 82
_QUALITY_FLOOR = 74
_LOSSY_STEPS: tuple[tuple[float, int], ...] = (
    (1.0, _QUALITY_CEILING),
    (1.0, _QUALITY_FLOOR),
    (0.82, _QUALITY_FLOOR),
    (0.66, _QUALITY_FLOOR),
    (0.54, _QUALITY_FLOOR),
    (0.44, _QUALITY_FLOOR),
)
_LOSSLESS_SCALES: tuple[float, ...] = (0.8, 0.65, 0.52, 0.42)
_REUSABLE_FORMATS = frozenset({"JPEG", "WEBP", "PNG"})
_CONTENT_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_WEBP_AVAILABLE = bool(_features.check("webp"))


@dataclass(frozen=True, slots=True)
class RemoteEncoding:
    """Encoded remote media bytes plus the HTTP content type."""

    content: bytes
    content_type: str


def encode_remote_media(
    source_path: Path,
    *,
    long_edge: int,
    target_bytes: int,
    hard_cap_bytes: int = _HARD_CAP_BYTES,
) -> RemoteEncoding:
    """Return bounded bytes for one remote variant of ``source_path``.

    ``target_bytes`` is a best-effort budget; ``hard_cap_bytes`` is enforced
    strictly and a failure raises :class:`ImageRegistryError` rather than
    falling back to the original.
    """

    if long_edge < 1 or target_bytes < 1 or hard_cap_bytes < 1:
        raise ValueError("remote media bounds must be positive")
    try:
        with Image.open(source_path) as source:
            source_format = str(source.format or "").upper()
            width, height = source.size
            if width < 1 or height < 1 or width * height > _MAX_PIXELS:
                raise ImageRegistryError("图片尺寸无效或像素数量过大。")
            orientation = int(source.getexif().get(0x0112, 1) or 1)
            icc_profile = source.info.get("icc_profile")
            if _source_is_reusable(
                source_format,
                orientation,
                width,
                height,
                source_path,
                long_edge=long_edge,
                target_bytes=target_bytes,
            ):
                return RemoteEncoding(
                    source_path.read_bytes(),
                    _CONTENT_TYPES[source_format],
                )
            has_alpha = _has_alpha(source)
            if source_format in {"JPEG", "MPO"}:
                # Ask the JPEG decoder for a smaller intermediate before the
                # unavoidable EXIF transpose and resize.
                source.draft("RGB", (long_edge, long_edge))
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in {"CMYK", "LAB"} and icc_profile:
                input_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                output_profile = ImageCms.ImageCmsProfile(
                    ImageCms.createProfile("sRGB")
                )
                working = ImageCms.profileToProfile(
                    oriented, input_profile, output_profile, outputMode="RGB"
                )
                icc_profile = output_profile.tobytes()
            else:
                working = _prepare(oriented)
            return _encode_within_budget(
                working,
                lossless=has_alpha or source_format == "PNG",
                long_edge=long_edge,
                target_bytes=target_bytes,
                hard_cap_bytes=hard_cap_bytes,
                icc_profile=icc_profile,
            )
    except ImageRegistryError:
        raise
    except (OSError, UnidentifiedImageError, ValueError, ImageCms.PyCMSError) as exc:
        raise ImageRegistryError("图片格式损坏或暂不支持预览。") from exc


def _encode_within_budget(
    image: Image.Image,
    *,
    lossless: bool,
    long_edge: int,
    target_bytes: int,
    hard_cap_bytes: int,
    icc_profile: bytes | None,
) -> RemoteEncoding:
    if lossless:
        return _encode_lossless(
            image,
            long_edge=long_edge,
            target_bytes=target_bytes,
            hard_cap_bytes=hard_cap_bytes,
            icc_profile=icc_profile,
        )
    return _encode_lossy(
        image,
        long_edge=long_edge,
        target_bytes=target_bytes,
        hard_cap_bytes=hard_cap_bytes,
        icc_profile=icc_profile,
    )


def _encode_lossy(
    image: Image.Image,
    *,
    long_edge: int,
    target_bytes: int,
    hard_cap_bytes: int,
    icc_profile: bytes | None,
) -> RemoteEncoding:
    """Photos: a JPEG quality ladder, then dimension reduction at the floor."""

    best: RemoteEncoding | None = None
    for scale, quality in _LOSSY_STEPS:
        candidate = _encode(
            _resize(image, long_edge, scale),
            "JPEG",
            quality=quality,
            icc_profile=icc_profile,
        )
        if best is None or len(candidate.content) < len(best.content):
            best = candidate
        if len(candidate.content) <= target_bytes:
            return candidate
    return _bounded(best, hard_cap_bytes)


def _encode_lossless(
    image: Image.Image,
    *,
    long_edge: int,
    target_bytes: int,
    hard_cap_bytes: int,
    icc_profile: bytes | None,
) -> RemoteEncoding:
    """Alpha/flat graphics: compare WebP-lossless with PNG, then shrink."""

    formats = ("WEBP", "PNG") if _WEBP_AVAILABLE else ("PNG",)
    full = _resize(image, long_edge, 1.0)
    best: RemoteEncoding | None = None
    preferred = formats[0]
    for fmt in formats:
        candidate = _encode(full, fmt, quality=None, icc_profile=icc_profile)
        if best is None or len(candidate.content) < len(best.content):
            best = candidate
            preferred = fmt
    if best is None:
        raise ImageRegistryError("图片远程预览生成失败。")
    if len(best.content) <= target_bytes:
        return best
    for scale in _LOSSLESS_SCALES:
        candidate = _encode(
            _resize(image, long_edge, scale),
            preferred,
            quality=None,
            icc_profile=icc_profile,
        )
        if len(candidate.content) < len(best.content):
            best = candidate
        if len(candidate.content) <= target_bytes:
            return candidate
    return _bounded(best, hard_cap_bytes)


def _bounded(best: RemoteEncoding | None, hard_cap_bytes: int) -> RemoteEncoding:
    if best is None:
        raise ImageRegistryError("图片远程预览生成失败。")
    if len(best.content) > hard_cap_bytes:
        raise ImageRegistryError("无法在大小限制内生成远程预览图片。")
    return best


def _encode(
    image: Image.Image,
    fmt: str,
    *,
    quality: int | None,
    icc_profile: bytes | None,
) -> RemoteEncoding:
    output = io.BytesIO()
    options: dict[str, object] = {}
    if icc_profile:
        options["icc_profile"] = icc_profile
    if fmt == "JPEG":
        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            **options,
        )
    elif fmt == "WEBP":
        if quality is None:
            image.save(output, format="WEBP", lossless=True, method=4, **options)
        else:
            image.convert("RGB").save(
                output,
                format="WEBP",
                quality=quality,
                method=4,
                **options,
            )
    else:
        image.save(output, format=fmt, optimize=True, **options)
    content = output.getvalue()
    if not content:
        raise ImageRegistryError("图片远程预览生成失败。")
    return RemoteEncoding(content, _CONTENT_TYPES[fmt])


def _resize(image: Image.Image, long_edge: int, scale: float) -> Image.Image:
    """Scale to fit ``long_edge * scale`` without ever upscaling."""

    limit = max(1, int(round(long_edge * min(scale, 1.0))))
    current = max(image.size)
    if current <= limit:
        return image
    ratio = limit / current
    target = (
        max(1, int(round(image.width * ratio))),
        max(1, int(round(image.height * ratio))),
    )
    return image.resize(target, Image.Resampling.LANCZOS)


def _prepare(image: Image.Image) -> Image.Image:
    if image.mode == "P":
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    if image.mode == "RGBA":
        return image
    if "A" in image.getbands():
        return image.convert("RGBA")
    if image.mode in {"RGB", "L", "LA"}:
        return image
    return image.convert("RGB")


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _source_is_reusable(
    source_format: str,
    orientation: int,
    width: int,
    height: int,
    source_path: Path,
    *,
    long_edge: int,
    target_bytes: int,
) -> bool:
    """Small already-correct sources skip a pointless lossy re-encode."""

    if source_format not in _REUSABLE_FORMATS or orientation != 1:
        return False
    if max(width, height) > long_edge:
        return False
    try:
        size = source_path.stat().st_size
    except OSError:
        return False
    return 0 < size <= target_bytes


__all__ = ["REMOTE_PROFILE_VERSION", "RemoteEncoding", "encode_remote_media"]
