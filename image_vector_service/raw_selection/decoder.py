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
import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

THUMBNAIL_MAX_EDGE = 384
PREVIEW_MIN_SCALE = 1.25  # embedded preview must be >= 1.25x display size

_rawpy_available: bool | None = None
_rawpy_lock = threading.Lock()
_TIFF_SUB_IFDS_TAG = 0x014A
_TIFF_ORIENTATION_TAG = 0x0112
_TIFF_JPEG_OFFSET_TAG = 0x0201
_TIFF_JPEG_LENGTH_TAG = 0x0202
_MAX_TIFF_DIRECTORIES = 32
_MAX_TIFF_IFD_OFFSET = 16 * 1024 * 1024
_MAX_EMBEDDED_JPEG_BYTES = 64 * 1024 * 1024


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


@dataclass(frozen=True, slots=True)
class FullDecodeRuntimeFacts:
    """Truthful execution facts used by diagnostics and benchmarks."""

    execution_model: str
    scheduler_worker: str
    rawpy_internal_thread_limit: int | None
    rawpy_internal_thread_control: str
    process_isolated: bool
    output_bits_per_sample: int
    reusable_base_result: bool


def full_decode_runtime_facts() -> FullDecodeRuntimeFacts:
    """Describe current guarantees without claiming unavailable isolation."""

    return FullDecodeRuntimeFacts(
        execution_model="in_process",
        scheduler_worker="fixed_bounded_python_thread",
        rawpy_internal_thread_limit=None,
        rawpy_internal_thread_control="not_exposed_by_rawpy_0_27",
        process_isolated=False,
        output_bits_per_sample=8,
        reusable_base_result=True,
    )


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation and return the oriented image."""
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _apply_orientation_value(img: Image.Image, orientation: int) -> Image.Image:
    transpose = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)
    return img.transpose(transpose) if transpose is not None else img


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


def _normalize_pillow_mode(img: Image.Image, extension: str) -> Image.Image:
    if extension == ".png" and ("A" in img.getbands() or "transparency" in img.info):
        return img if img.mode == "RGBA" else img.convert("RGBA")
    return img if img.mode == "RGB" else img.convert("RGB")


def _tiff_scalar(entry: bytes, endian: str) -> int | None:
    if len(entry) != 12:
        return None
    value_type = struct.unpack(f"{endian}H", entry[2:4])[0]
    value_count = struct.unpack(f"{endian}I", entry[4:8])[0]
    if value_count != 1:
        return None
    if value_type == 3:
        return struct.unpack(f"{endian}H", entry[8:10])[0]
    if value_type == 4:
        return struct.unpack(f"{endian}I", entry[8:12])[0]
    return None


def read_arw_orientation(path: str | Path) -> int:
    """Read the A7M4 TIFF-container orientation without decoding RAW pixels."""

    file_path = Path(path)
    file_size = os.path.getsize(file_path)
    with file_path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8 or header[:2] not in {b"II", b"MM"}:
            raise ValueError("ARW has an invalid TIFF header")
        endian = "<" if header[:2] == b"II" else ">"
        if struct.unpack(f"{endian}H", header[2:4])[0] != 42:
            raise ValueError("ARW uses an unsupported TIFF layout")
        ifd_offset = struct.unpack(f"{endian}I", header[4:8])[0]
        if ifd_offset < 8 or ifd_offset > min(file_size - 2, _MAX_TIFF_IFD_OFFSET):
            raise ValueError("ARW has an invalid TIFF directory")
        handle.seek(ifd_offset)
        count_bytes = handle.read(2)
        if len(count_bytes) != 2:
            raise ValueError("ARW orientation directory is truncated")
        entry_count = struct.unpack(f"{endian}H", count_bytes)[0]
        if entry_count > 4096:
            raise ValueError("ARW orientation directory is invalid")
        for _ in range(entry_count):
            entry = handle.read(12)
            if len(entry) != 12:
                raise ValueError("ARW orientation entry is truncated")
            if struct.unpack(f"{endian}H", entry[:2])[0] != _TIFF_ORIENTATION_TAG:
                continue
            orientation = _tiff_scalar(entry, endian)
            if orientation is not None and 1 <= orientation <= 8:
                return orientation
            return 1
    return 1


def _apply_arw_orientation(img: Image.Image, path: str | Path) -> Image.Image:
    return _apply_orientation_value(img, _arw_orientation_value(img, path))


def _arw_orientation_value(img: Image.Image, path: str | Path) -> int:
    try:
        exif = img.getexif()
        if _TIFF_ORIENTATION_TAG in exif:
            orientation = int(exif.get(_TIFF_ORIENTATION_TAG, 1))
            return orientation if 1 <= orientation <= 8 else 1
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        return read_arw_orientation(path)
    except (OSError, ValueError):
        return 1


def _prepare_fast_arw_preview(
    img: Image.Image,
    path: str | Path,
    display_width: int,
    display_height: int,
) -> None:
    if display_width <= 0 or display_height <= 0 or not hasattr(img, "draft"):
        return
    orientation = _arw_orientation_value(img, path)
    width, height = img.size
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    scale = min(display_width / width, display_height / height)
    scale = min(1.0, scale * PREVIEW_MIN_SCALE)
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    if orientation in {5, 6, 7, 8}:
        target = (target[1], target[0])
    img.draft("RGB", target)


def _fit_fast_preview(
    img: Image.Image,
    display_width: int,
    display_height: int,
) -> Image.Image:
    if display_width <= 0 or display_height <= 0:
        return img
    scale = min(display_width / img.width, display_height / img.height)
    scale = min(1.0, scale * PREVIEW_MIN_SCALE)
    if scale >= 1.0:
        return img
    target = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(target, Image.Resampling.LANCZOS)


def extract_arw_embedded_jpeg(path: str | Path) -> bytes:
    """Return the largest valid JPEG referenced by an A7M4 TIFF directory."""

    file_path = Path(path)
    file_size = os.path.getsize(file_path)
    with file_path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8 or header[:2] not in {b"II", b"MM"}:
            raise ValueError("ARW has an invalid TIFF header")
        endian = "<" if header[:2] == b"II" else ">"
        if struct.unpack(f"{endian}H", header[2:4])[0] != 42:
            raise ValueError("ARW uses an unsupported TIFF layout")
        root_ifd = struct.unpack(f"{endian}I", header[4:8])[0]
        queue = [root_ifd]
        seen: set[int] = set()
        candidates: list[tuple[int, int]] = []

        while queue and len(seen) < _MAX_TIFF_DIRECTORIES:
            ifd_offset = queue.pop(0)
            if (
                ifd_offset in seen
                or ifd_offset < 8
                or ifd_offset > _MAX_TIFF_IFD_OFFSET
            ):
                continue
            seen.add(ifd_offset)
            handle.seek(ifd_offset)
            count_bytes = handle.read(2)
            if len(count_bytes) != 2:
                continue
            entry_count = struct.unpack(f"{endian}H", count_bytes)[0]
            if entry_count > 4096:
                continue
            entries: dict[int, bytes] = {}
            for _ in range(entry_count):
                entry = handle.read(12)
                if len(entry) != 12:
                    entries.clear()
                    break
                tag = struct.unpack(f"{endian}H", entry[:2])[0]
                entries[tag] = entry

            jpeg_offset = _tiff_scalar(entries.get(_TIFF_JPEG_OFFSET_TAG, b""), endian)
            jpeg_length = _tiff_scalar(entries.get(_TIFF_JPEG_LENGTH_TAG, b""), endian)
            if (
                jpeg_offset is not None
                and jpeg_length is not None
                and jpeg_length > 4
                and jpeg_length <= _MAX_EMBEDDED_JPEG_BYTES
                and jpeg_offset >= 0
                and jpeg_offset + jpeg_length <= file_size
            ):
                candidates.append((jpeg_length, jpeg_offset))

            sub_ifds = entries.get(_TIFF_SUB_IFDS_TAG)
            if sub_ifds is not None:
                value_type = struct.unpack(f"{endian}H", sub_ifds[2:4])[0]
                value_count = struct.unpack(f"{endian}I", sub_ifds[4:8])[0]
                if value_type == 4 and 1 <= value_count <= _MAX_TIFF_DIRECTORIES:
                    if value_count == 1:
                        queue.append(struct.unpack(f"{endian}I", sub_ifds[8:12])[0])
                    else:
                        offsets_position = struct.unpack(f"{endian}I", sub_ifds[8:12])[
                            0
                        ]
                        if offsets_position <= _MAX_TIFF_IFD_OFFSET:
                            return_position = handle.tell()
                            handle.seek(offsets_position)
                            raw_offsets = handle.read(value_count * 4)
                            handle.seek(return_position)
                            if len(raw_offsets) == value_count * 4:
                                queue.extend(
                                    struct.unpack(
                                        f"{endian}{value_count}I", raw_offsets
                                    )
                                )

            next_position = ifd_offset + 2 + entry_count * 12
            if next_position + 4 <= file_size:
                handle.seek(next_position)
                next_bytes = handle.read(4)
                if len(next_bytes) == 4:
                    next_ifd = struct.unpack(f"{endian}I", next_bytes)[0]
                    if next_ifd:
                        queue.append(next_ifd)

        for jpeg_length, jpeg_offset in sorted(candidates, reverse=True):
            handle.seek(jpeg_offset)
            payload = handle.read(jpeg_length)
            if payload.startswith(b"\xff\xd8") and payload.rstrip(b"\0").endswith(
                b"\xff\xd9"
            ):
                return payload
    raise ValueError("No valid embedded JPEG in ARW")


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
        direct_error: Exception | None = None
        try:
            img = Image.open(io.BytesIO(extract_arw_embedded_jpeg(file_path)))
            if hasattr(img, "draft"):
                img.draft(
                    "RGB",
                    (THUMBNAIL_MAX_EDGE * 2, THUMBNAIL_MAX_EDGE * 2),
                )
            img.load()
        except Exception as exc:  # rawpy fallback covers other valid ARW layouts
            direct_error = exc
        else:
            img = _apply_arw_orientation(img, file_path)
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

        if not is_rawpy_available():
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="thumbnail",
                source="embedded_jpeg",
                error=str(direct_error or "No embedded JPEG in ARW"),
            )
        try:
            import rawpy

            with rawpy.imread(str(file_path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format != rawpy.ThumbFormat.JPEG:
                    raise ValueError("No embedded JPEG in ARW")
                img = Image.open(io.BytesIO(thumb.data))
                if hasattr(img, "draft"):
                    img.draft(
                        "RGB",
                        (THUMBNAIL_MAX_EDGE * 2, THUMBNAIL_MAX_EDGE * 2),
                    )
                img.load()
            img = _apply_arw_orientation(img, file_path)
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
        img = _normalize_pillow_mode(img, ext)
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


def _embedded_preview_result(
    img: Image.Image,
    display_width: int,
    display_height: int,
    path: str | Path,
    *,
    fast: bool,
) -> DecodeResult:
    img = _apply_arw_orientation(img, path)
    if fast:
        img = _fit_fast_preview(img, display_width, display_height)
    if img.mode != "RGB":
        img = img.convert("RGB")
    error = None
    if (
        display_width > 0
        and display_height > 0
        and min(display_width / img.width, display_height / img.height)
        * PREVIEW_MIN_SCALE
        > 1.001
    ):
        error = "preview_below_threshold"
    return DecodeResult(
        image=img,
        width=img.width,
        height=img.height,
        level="preview",
        source="embedded_preview",
        error=error,
    )


def decode_preview(
    path: str,
    extension: str,
    *,
    display_width: int = 0,
    display_height: int = 0,
    fast: bool = False,
) -> DecodeResult:
    """Generate a quick preview from embedded preview or full-size image.

    For ARW: uses rawpy's embedded preview if it's >= 1.25x display size.
    For JPG/PNG: loads full image via Pillow.
    """
    ext = extension.casefold()
    file_path = Path(path)

    if ext == ".arw":
        direct_error: Exception | None = None
        try:
            img = Image.open(io.BytesIO(extract_arw_embedded_jpeg(file_path)))
            if fast:
                _prepare_fast_arw_preview(
                    img,
                    file_path,
                    display_width,
                    display_height,
                )
            img.load()
        except Exception as exc:  # rawpy fallback covers other valid ARW layouts
            direct_error = exc
        else:
            return _embedded_preview_result(
                img,
                display_width,
                display_height,
                file_path,
                fast=fast,
            )

        if not is_rawpy_available():
            return DecodeResult(
                image=None,
                width=0,
                height=0,
                level="preview",
                source="embedded_preview",
                error=str(direct_error or "No embedded preview in ARW"),
            )
        try:
            import rawpy

            with rawpy.imread(str(file_path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format != rawpy.ThumbFormat.JPEG:
                    raise ValueError("No embedded preview in ARW")
                img = Image.open(io.BytesIO(thumb.data))
                if fast:
                    _prepare_fast_arw_preview(
                        img,
                        file_path,
                        display_width,
                        display_height,
                    )
                img.load()
            return _embedded_preview_result(
                img,
                display_width,
                display_height,
                file_path,
                fast=fast,
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
        img = _normalize_pillow_mode(img, ext)
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


def decode_full_base(
    path: str,
    extension: str,
) -> DecodeResult:
    """Create one reusable full decode for ARW or original JPG/PNG pixels.

    For ARW: uses rawpy with camera white balance, sRGB output.
    The returned image is owned by the caller and can be passed repeatedly to
    :func:`render_full_decode_with_look` before it is closed.  rawpy 0.27 does
    not expose a LibRaw internal-thread limit or process isolation; the outer
    DecodeScheduler full-RAW lane is the bounded concurrency control.
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
                )
                # rawpy returns a numpy array; convert to PIL

                img = Image.fromarray(rgb, "RGB")
                # Leaving user_flip unset lets LibRaw apply the camera's
                # recorded orientation to the postprocessed pixels.
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


def decode_full(
    path: str,
    extension: str,
) -> DecodeResult:
    """Backward-compatible alias for the reusable base decode path."""

    return decode_full_base(path, extension)


def render_full_decode_with_look(
    base: DecodeResult,
    extension: str,
    look_id: str,
) -> DecodeResult:
    """Render one look from an existing base without consuming the base image."""

    from .creative_look import DEFAULT_LOOK, apply_creative_look, is_valid_look

    if base.image is None:
        return DecodeResult(
            image=None,
            width=0,
            height=0,
            level="full",
            source="full_decode_reused",
            error=base.error or "Base full decode is unavailable",
        )
    ext = extension.casefold()
    try:
        if ext == ".arw" and look_id != DEFAULT_LOOK and is_valid_look(look_id):
            rendered = apply_creative_look(base.image, look_id)
        else:
            # A copy gives the caller clear ownership while the reusable base
            # remains valid for another side/look.
            rendered = base.image.copy()
    except Exception as exc:
        return DecodeResult(
            image=None,
            width=0,
            height=0,
            level="full",
            source="full_decode_reused",
            error=str(exc),
        )
    return DecodeResult(
        image=rendered,
        width=rendered.width,
        height=rendered.height,
        level="full",
        source="full_decode_reused",
    )


def decode_full_with_look(
    path: str,
    extension: str,
    look_id: str,
) -> DecodeResult:
    """Full-decode a supported look; currently only ``as_shot`` is available."""
    from .creative_look import DEFAULT_LOOK, is_valid_look

    ext = extension.casefold()
    if look_id == DEFAULT_LOOK or not is_valid_look(look_id) or ext != ".arw":
        return decode_full(path, extension)

    base = decode_full_base(path, extension)
    if base.image is None:
        return base
    try:
        return render_full_decode_with_look(base, extension, look_id)
    finally:
        base.image.close()


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


__all__ = [
    "DecodeResult",
    "FullDecodeRuntimeFacts",
    "PREVIEW_MIN_SCALE",
    "THUMBNAIL_MAX_EDGE",
    "decode_full",
    "decode_full_base",
    "decode_full_with_look",
    "decode_preview",
    "decode_thumbnail",
    "extract_arw_embedded_jpeg",
    "full_decode_runtime_facts",
    "image_to_jpeg_bytes",
    "image_to_png_bytes",
    "is_rawpy_available",
    "read_arw_orientation",
    "render_full_decode_with_look",
]
