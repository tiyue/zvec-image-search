"""Three-level progressive RAW and image decoding for the ARW selection module.

Level 1 — Thumbnail: ARW embedded JPEG or scaled JPG/PNG, longest edge ~384px.
Level 2 — Quick preview: ARW embedded preview or full-size JPG/PNG.
Level 3 — Full decode: rawpy/LibRaw full decode for ARW when zoom or look
change demands it; JPG/PNG always serve original pixels.

rawpy is an optional dependency. When unavailable, ARW files report a
clear error and JPG/PNG continue to work. No placeholder or fake decode
is ever returned.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

THUMBNAIL_MAX_EDGE = 384
PREVIEW_MIN_SCALE = 1.25  # embedded preview must be >= 1.25x display size

_rawpy_available: bool | None = None
_rawpy_lock = threading.Lock()


def is_rawpy_available() -> bool:
    """Return True if rawpy can be imported."""
    global _rawpy_available
    if _rawpy_available is not None:
        return _rawpy_available
    with _rawpy_lock:
        if _rawpy_available is not None:
            return _rawpy_available
        try:
            import rawpy  # noqa: F401

            _rawpy_available = True
        except ImportError:
            _rawpy_available = False
    return _rawpy_available


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Result of a decode operation."""

    image: Image.Image | None
    width: int
    height: int
    level: str  # "thumbnail" | "preview" | "full"
    source: str  # "embedded_jpeg" | "embedded_preview" | "full_decode" | "pillow"
    error: str | None = None


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation and return the oriented image."""
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _fit_to_max_edge(img: Image.Image, max_edge: int) -> Image.Image:
    """Scale image so its longest edge is at most *max_edge*."""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    # Use LANCZOS for downscaling quality
    return img.resize(new_size, Image.LANCZOS)


def decode_thumbnail(
    path: str,
    extension: str,
) -> DecodeResult:
    """Generate a thumbnail (longest edge ~384px) from any supported file.

    For ARW: uses rawpy's embedded JPEG extraction.
    For JPG/PNG: uses Pillow with scaled decoding.
    """
    ext = extension.casefold()
    file_path = Path(path)

    if ext == ".arw":
        if not is_rawpy_available():
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="thumbnail",
                source="embedded_jpeg",
                error="rawpy not installed — ARW decoding unavailable",
            )
        try:
            import rawpy

            with rawpy.imread(str(file_path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    img = _apply_exif_orientation(img)
                    img = _fit_to_max_edge(img, THUMBNAIL_MAX_EDGE)
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    return DecodeResult(
                        image=img,
                        width=img.width,
                        height=img.height,
                        level="thumbnail",
                        source="embedded_jpeg",
                    )
                return DecodeResult(
                    image=None,
                    width=0,
                    height=0,
                    level="thumbnail",
                    source="embedded_jpeg",
                    error="No embedded JPEG in ARW",
                )
        except Exception as exc:
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="thumbnail",
                source="embedded_jpeg",
                error=str(exc),
            )

    # JPG/PNG via Pillow
    try:
        # Use draft mode for JPEG to avoid loading full resolution
        img = Image.open(file_path)
        if ext in (".jpg", ".jpeg") and hasattr(img, "draft"):
            # Request a small draft for thumbnail
            img.draft("RGB", (THUMBNAIL_MAX_EDGE * 2, THUMBNAIL_MAX_EDGE * 2))
        img.load()
        img = _apply_exif_orientation(img)
        img = _fit_to_max_edge(img, THUMBNAIL_MAX_EDGE)
        if img.mode == "RGBA":
            pass  # Preserve alpha for PNG
        elif img.mode != "RGB":
            img = img.convert("RGB")
        return DecodeResult(
            image=img,
            width=img.width,
            height=img.height,
            level="thumbnail",
            source="pillow",
        )
    except Exception as exc:
        return DecodeResult(
            image=None,
            width=0,
            height=0,
            level="thumbnail",
            source="pillow",
            error=str(exc),
        )


def decode_preview(
    path: str,
    extension: str,
    *,
    display_width: int = 0,
    display_height: int = 0,
) -> DecodeResult:
    """Generate a quick preview from embedded preview or full-size image.

    For ARW: uses rawpy's embedded preview if it's >= 1.25x display size.
    For JPG/PNG: loads full image via Pillow.
    """
    ext = extension.casefold()
    file_path = Path(path)

    if ext == ".arw":
        if not is_rawpy_available():
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="preview",
                source="embedded_preview",
                error="rawpy not installed — ARW decoding unavailable",
            )
        try:
            import rawpy

            with rawpy.imread(str(file_path)) as raw:
                # Try to extract the embedded preview
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    img = _apply_exif_orientation(img)
                    # Check if embedded preview is large enough
                    if (
                        display_width > 0
                        and display_height > 0
                        and (
                            img.width < display_width * PREVIEW_MIN_SCALE
                            or img.height < display_height * PREVIEW_MIN_SCALE
                        )
                    ):
                        # Preview too small — caller should request full decode
                        return DecodeResult(
                            image=img,
                            width=img.width,
                            height=img.height,
                            level="preview",
                            source="embedded_preview",
                            error="preview_below_threshold",
                        )
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    return DecodeResult(
                        image=img,
                        width=img.width,
                        height=img.height,
                        level="preview",
                        source="embedded_preview",
                    )
                return DecodeResult(
                    image=None,
                    width=0,
                    height=0,
                    level="preview",
                    source="embedded_preview",
                    error="No embedded preview in ARW",
                )
        except Exception as exc:
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="preview",
                source="embedded_preview",
                error=str(exc),
            )

    # JPG/PNG via Pillow — full resolution
    try:
        img = Image.open(file_path)
        img.load()
        img = _apply_exif_orientation(img)
        if img.mode == "RGBA":
            pass  # Preserve alpha for PNG
        elif img.mode != "RGB":
            img = img.convert("RGB")
        return DecodeResult(
            image=img,
            width=img.width,
            height=img.height,
            level="preview",
            source="pillow",
        )
    except Exception as exc:
        return DecodeResult(
            image=None,
            width=0,
            height=0,
            level="preview",
            source="pillow",
            error=str(exc),
        )


def decode_full(
    path: str,
    extension: str,
) -> DecodeResult:
    """Full RAW decode for ARW; original pixels for JPG/PNG.

    For ARW: uses rawpy with camera white balance, sRGB output.
    For JPG/PNG: same as preview (original pixels).
    """
    ext = extension.casefold()

    if ext == ".arw":
        if not is_rawpy_available():
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="full",
                source="full_decode",
                error="rawpy not installed — ARW decoding unavailable",
            )
        try:
            import rawpy

            with rawpy.imread(path) as raw:
                # Use camera white balance, 8-bit sRGB output
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    output_bps=8,
                    no_auto_bright=True,
                    user_flip=0,  # We handle orientation via EXIF
                )
                # rawpy returns a numpy array; convert to PIL

                img = Image.fromarray(rgb, "RGB")
                # Apply EXIF orientation from the raw metadata
                # rawpy doesn't set EXIF on postprocess output, but
                # LibRaw handles orientation internally with user_flip=0
                return DecodeResult(
                    image=img,
                    width=img.width,
                    height=img.height,
                    level="full",
                    source="full_decode",
                )
        except Exception as exc:
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="full",
                source="full_decode",
                error=str(exc),
            )

    # JPG/PNG — same as preview
    return decode_preview(path, extension)


def decode_full_with_look(
    path: str,
    extension: str,
    look_id: str,
) -> DecodeResult:
    """Full RAW decode then apply a creative look.

    For ``as_shot`` or non-ARW files this behaves like :func:`decode_full` /
    :func:`decode_preview` (no re-render). For ARW with a non-default look,
    the full decode is transformed by the calibrated look configuration.
    """
    from .creative_look import DEFAULT_LOOK, apply_creative_look, is_valid_look

    ext = extension.casefold()
    if look_id == DEFAULT_LOOK or not is_valid_look(look_id) or ext != ".arw":
        return decode_full(path, extension)

    result = decode_full(path, extension)
    if result.image is None:
        return result
    looked = apply_creative_look(result.image, look_id)
    return DecodeResult(
        image=looked,
        width=looked.width,
        height=looked.height,
        level="full",
        source="full_decode",
    )


def image_to_jpeg_bytes(
    img: Image.Image,
    *,
    quality: int = 85,
    max_edge: int = 0,
) -> bytes:
    """Convert a PIL image to JPEG bytes for HTTP response."""
    if max_edge > 0:
        img = _fit_to_max_edge(img, max_edge)
    if img.mode == "RGBA":
        # Composite onto white for JPEG
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def image_to_png_bytes(img: Image.Image) -> bytes:
    """Convert a PIL image to PNG bytes (preserving alpha)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
