"""Bounded image data-URI encoding shared by embedding and tagging clients.

DashScope's current multimodal embedding endpoint accepts at most 10 MiB per
data-URI item. A much larger source image can still be used safely: callers
apply a separate local-read ceiling, then this module converts an in-memory
copy until the outbound representation fits. The original image is never
modified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .image_scanner import PIL_FORMAT_MIME, SUPPORTED_PIL_FORMATS

DASHSCOPE_DATA_URI_MAX_BYTES = 10 * 1024 * 1024
DASHSCOPE_DATA_URI_TARGET_BYTES = DASHSCOPE_DATA_URI_MAX_BYTES - 64 * 1024
_MAX_DECODE_PIXELS = 100_000_000
_MIN_OUTPUT_DIMENSION = 512
_JPEG_QUALITIES = (90, 85, 80, 75, 70)
_MAX_RESIZE_ROUNDS = 8


class ImageDataUriError(ValueError):
    """Raised when an image cannot be represented inside the API limits."""


class ImageSourceChangedError(ImageDataUriError):
    """Raised when the bytes no longer match the indexed source identity."""


@dataclass(frozen=True)
class EncodedImageDataUri:
    data_uri: str
    mime_type: str
    source_size_bytes: int
    encoded_payload_bytes: int
    data_uri_bytes: int
    width: int
    height: int
    transformed: bool


def encode_image_data_uri(
    path: Path,
    *,
    max_source_bytes: int,
    max_data_uri_bytes: int = DASHSCOPE_DATA_URI_TARGET_BYTES,
    request_byte_budget: int | None = None,
    request_overhead_bytes: int = 0,
    max_output_dimension: int = 4096,
    expected_source_sha256: str | None = None,
    force_transcode: bool = False,
) -> EncodedImageDataUri:
    """Return a bounded data URI, transcoding an in-memory copy when needed."""

    source_path = path.expanduser().resolve()
    if max_source_bytes < 1:
        raise ValueError("max_source_bytes must be positive.")
    if max_data_uri_bytes < 1:
        raise ValueError("max_data_uri_bytes must be positive.")
    if request_byte_budget is not None and request_byte_budget < 1:
        raise ValueError("request_byte_budget must be positive when supplied.")
    if request_overhead_bytes < 0:
        raise ValueError("request_overhead_bytes cannot be negative.")
    if max_output_dimension < _MIN_OUTPUT_DIMENSION:
        raise ValueError(
            f"max_output_dimension must be at least {_MIN_OUTPUT_DIMENSION}."
        )
    if not isinstance(force_transcode, bool):
        raise TypeError("force_transcode must be a boolean.")
    expected_hash = _normalize_expected_sha256(expected_source_sha256)

    effective_limit = max_data_uri_bytes
    if request_byte_budget is not None:
        remaining = request_byte_budget - request_overhead_bytes
        if remaining < 1:
            raise ImageDataUriError(
                "The request metadata leaves no room for an image data URI."
            )
        effective_limit = min(effective_limit, remaining)

    try:
        stat_before = source_path.stat()
    except OSError as exc:
        raise ImageDataUriError(f"Cannot read image {source_path}: {exc}") from exc
    source_size = stat_before.st_size
    if source_size > max_source_bytes:
        raise ImageDataUriError(
            f"Image {source_path} is {source_size} bytes and exceeds the configured "
            f"source limit of {max_source_bytes} bytes."
        )

    try:
        source_payload = source_path.read_bytes()
        stat_after = source_path.stat()
    except OSError as exc:
        raise ImageDataUriError(f"Cannot encode image {source_path}: {exc}") from exc
    if (
        _stat_signature(stat_before) != _stat_signature(stat_after)
        or len(source_payload) != source_size
    ):
        raise ImageSourceChangedError(
            f"Image {source_path} changed while it was being read."
        )
    if expected_hash is not None:
        actual_hash = hashlib.sha256(source_payload).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise ImageSourceChangedError(
                f"Image {source_path} no longer matches its indexed SHA-256."
            )

    try:
        _format, mime_type, width, height = _inspect_image_payload(source_payload)
    except Exception as exc:
        raise ImageDataUriError(f"Cannot encode image {source_path}: {exc}") from exc

    original = _build_result(
        source_payload,
        mime_type=mime_type,
        source_size=source_size,
        width=width,
        height=height,
        transformed=False,
    )
    if not force_transcode and original.data_uri_bytes <= effective_limit:
        return original

    return _transcode_to_fit(
        source_path,
        source_payload=source_payload,
        source_size=source_size,
        original_width=width,
        original_height=height,
        effective_limit=effective_limit,
        max_output_dimension=max_output_dimension,
    )


def _transcode_to_fit(
    path: Path,
    *,
    source_payload: bytes,
    source_size: int,
    original_width: int,
    original_height: int,
    effective_limit: int,
    max_output_dimension: int,
) -> EncodedImageDataUri:
    try:
        # Decode the exact payload that passed the SHA/stat checks. Reopening
        # the path here would create a second time-of-check/time-of-use race.
        with Image.open(BytesIO(source_payload)) as opened:
            original_pixels = original_width * original_height
            if (
                original_pixels > _MAX_DECODE_PIXELS
                or max(opened.size) > max_output_dimension
            ):
                # JPEG decoders can subsample during decode. Requesting a draft
                # before load avoids expanding a very high-resolution source to
                # its full RGB footprint merely to shrink it immediately.
                opened.draft(
                    "RGB",
                    _decoder_draft_size(
                        opened.size,
                        max_dimension=max_output_dimension,
                    ),
                )
            decoded_pixels = opened.width * opened.height
            if decoded_pixels > _MAX_DECODE_PIXELS:
                raise ImageDataUriError(
                    f"Image {path} is {original_width}x{original_height} "
                    f"({original_pixels} pixels); its decoder would still expand "
                    f"to {opened.width}x{opened.height} ({decoded_pixels} pixels), "
                    f"above the safe decode limit of {_MAX_DECODE_PIXELS} pixels."
                )
            oriented = ImageOps.exif_transpose(opened)
            oriented.load()
            image = _flatten_to_rgb(oriented)
    except ImageDataUriError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ImageDataUriError(f"Cannot transcode image {path}: {exc}") from exc

    if max(image.size) > max_output_dimension:
        image.thumbnail(
            (max_output_dimension, max_output_dimension),
            Image.Resampling.LANCZOS,
        )

    current = image
    for resize_round in range(_MAX_RESIZE_ROUNDS):
        for quality in _JPEG_QUALITIES:
            buffer = BytesIO()
            try:
                current.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                    subsampling="4:2:0",
                )
            except OSError as exc:
                raise ImageDataUriError(
                    f"Cannot transcode image {path}: {exc}"
                ) from exc
            payload = buffer.getvalue()
            result = _build_result(
                payload,
                mime_type="image/jpeg",
                source_size=source_size,
                width=current.width,
                height=current.height,
                transformed=True,
            )
            if result.data_uri_bytes <= effective_limit:
                return result

        next_width = max(1, int(current.width * 0.8))
        next_height = max(1, int(current.height * 0.8))
        if max(next_width, next_height) < _MIN_OUTPUT_DIMENSION:
            break
        if (next_width, next_height) == current.size:
            break
        current = current.resize(
            (next_width, next_height),
            Image.Resampling.LANCZOS,
        )
        if resize_round == _MAX_RESIZE_ROUNDS - 1:
            break

    raise ImageDataUriError(
        f"Image {path} ({source_size} bytes, {original_width}x{original_height}) "
        f"cannot be reduced below the data-URI limit of {effective_limit} bytes."
    )


def _decoder_draft_size(
    size: tuple[int, int],
    *,
    max_dimension: int,
) -> tuple[int, int]:
    """Return an aspect-ratio target that lets JPEG draft subsampling engage."""

    width, height = size
    if max(width, height) <= max_dimension:
        return width, height
    if width >= height:
        return max_dimension, max(1, height * max_dimension // width)
    return max(1, width * max_dimension // height), max_dimension


def _inspect_image_payload(payload: bytes) -> tuple[str, str, int, int]:
    with Image.open(BytesIO(payload)) as image:
        actual_format = (image.format or "").upper()
        width, height = image.size
        image.verify()
    if actual_format not in SUPPORTED_PIL_FORMATS:
        raise ValueError(f"unsupported image content: {actual_format or 'unknown'}")
    return actual_format, PIL_FORMAT_MIME[actual_format], width, height


def _normalize_expected_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected_source_sha256 must be a string or None.")
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("expected_source_sha256 must be a 64-character hex digest.")
    return normalized


def _stat_signature(value: object) -> tuple[int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(getattr(value, "st_size", -1)),
        int(getattr(value, "st_mtime_ns", -1)),
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _build_result(
    payload: bytes,
    *,
    mime_type: str,
    source_size: int,
    width: int,
    height: int,
    transformed: bool,
) -> EncodedImageDataUri:
    encoded = base64.b64encode(payload).decode("ascii")
    data_uri = f"data:{mime_type};base64,{encoded}"
    return EncodedImageDataUri(
        data_uri=data_uri,
        mime_type=mime_type,
        source_size_bytes=source_size,
        encoded_payload_bytes=len(payload),
        data_uri_bytes=len(data_uri.encode("ascii")),
        width=width,
        height=height,
        transformed=transformed,
    )
