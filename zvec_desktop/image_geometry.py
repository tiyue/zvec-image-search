"""Geometry and rendering helpers for desktop image previews.

The UI uses two deliberately different fitting policies:

``contain``
    Preserve the complete image and letterbox the unused viewport area.
``cover``
    Fill the complete viewport and crop equal amounts from opposite edges.

Keeping this policy in a small, toolkit-independent module makes preview
behaviour identical in every Python desktop view and straightforward to test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from PIL import Image

SizeLike: TypeAlias = tuple[int, int]

# Geometry calculations do not allocate an image, so they may safely accept a
# larger source than the renderer.  A finite limit still rejects corrupt image
# headers containing absurd dimensions before they reach later calculations.
MAX_GEOMETRY_DIMENSION = 1_000_000
MAX_GEOMETRY_PIXELS = 1_000_000_000
MAX_RENDER_DIMENSION = 32_768
MAX_RENDER_PIXELS = 33_554_432


class ImageGeometryError(ValueError):
    """Raised when image or viewport geometry is invalid."""


class ImageFitMode(str, Enum):
    """How an image should occupy its destination viewport."""

    CONTAIN = "contain"
    COVER = "cover"


@dataclass(frozen=True, slots=True)
class ImageSize:
    """A validated, non-empty integer image size."""

    width: int
    height: int

    def __post_init__(self) -> None:
        _validate_dimensions(self.width, self.height, label="image size")

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def as_tuple(self) -> SizeLike:
        return self.width, self.height


@dataclass(frozen=True, slots=True)
class ImageRect:
    """A floating-point rectangle in source or destination coordinates."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if not all(math.isfinite(value) for value in values):
            raise ImageGeometryError("rectangle values must be finite")
        if self.width <= 0 or self.height <= 0:
            raise ImageGeometryError("rectangle width and height must be positive")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def as_box(self) -> tuple[float, float, float, float]:
        """Return Pillow's ``(left, top, right, bottom)`` box format."""

        return self.x, self.y, self.right, self.bottom


@dataclass(frozen=True, slots=True)
class ImageLayout:
    """The source crop and destination rectangle for one fitted image."""

    source_size: ImageSize
    target_size: ImageSize
    mode: ImageFitMode
    source_crop: ImageRect
    destination: ImageRect
    scale: float

    @property
    def is_cropped(self) -> bool:
        return not math.isclose(
            self.source_crop.width, self.source_size.width
        ) or not math.isclose(self.source_crop.height, self.source_size.height)

    @property
    def is_letterboxed(self) -> bool:
        return not math.isclose(
            self.destination.width, self.target_size.width
        ) or not math.isclose(self.destination.height, self.target_size.height)


def _validate_dimensions(width: object, height: object, *, label: str) -> None:
    # ``bool`` is an ``int`` subclass but accepting True as one pixel hides UI
    # binding mistakes, so it is rejected explicitly.
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
    ):
        raise ImageGeometryError(f"{label} must contain two integers")
    if width <= 0 or height <= 0:
        raise ImageGeometryError(f"{label} width and height must be positive")
    if width > MAX_GEOMETRY_DIMENSION or height > MAX_GEOMETRY_DIMENSION:
        raise ImageGeometryError(f"{label} exceeds the dimension limit")
    if width * height > MAX_GEOMETRY_PIXELS:
        raise ImageGeometryError(f"{label} exceeds the pixel limit")


def coerce_image_size(value: ImageSize | SizeLike, *, label: str) -> ImageSize:
    """Convert a two-item tuple to :class:`ImageSize` with clear errors."""

    if isinstance(value, ImageSize):
        return value
    if not isinstance(value, tuple) or len(value) != 2:
        raise ImageGeometryError(f"{label} must be a (width, height) tuple")
    try:
        return ImageSize(value[0], value[1])
    except ImageGeometryError as exc:
        raise ImageGeometryError(f"invalid {label}: {exc}") from exc


def coerce_fit_mode(value: ImageFitMode | str) -> ImageFitMode:
    """Normalize a fit mode without silently choosing a surprising default."""

    if isinstance(value, ImageFitMode):
        return value
    if isinstance(value, str):
        try:
            return ImageFitMode(value.strip().lower())
        except ValueError as exc:
            raise ImageGeometryError(f"unsupported image fit mode: {value!r}") from exc
    raise ImageGeometryError("image fit mode must be 'contain' or 'cover'")


def calculate_image_layout(
    source_size: ImageSize | SizeLike,
    target_size: ImageSize | SizeLike,
    mode: ImageFitMode | str,
) -> ImageLayout:
    """Calculate centered contain/cover geometry without allocating pixels."""

    source = coerce_image_size(source_size, label="source size")
    target = coerce_image_size(target_size, label="target size")
    fit_mode = coerce_fit_mode(mode)

    width_scale = target.width / source.width
    height_scale = target.height / source.height

    if fit_mode is ImageFitMode.CONTAIN:
        scale = min(width_scale, height_scale)
        rendered_width = source.width * scale
        rendered_height = source.height * scale
        source_crop = ImageRect(0.0, 0.0, source.width, source.height)
        destination = ImageRect(
            (target.width - rendered_width) / 2.0,
            (target.height - rendered_height) / 2.0,
            rendered_width,
            rendered_height,
        )
    else:
        scale = max(width_scale, height_scale)
        crop_width = min(float(source.width), target.width / scale)
        crop_height = min(float(source.height), target.height / scale)
        source_crop = ImageRect(
            max(0.0, (source.width - crop_width) / 2.0),
            max(0.0, (source.height - crop_height) / 2.0),
            crop_width,
            crop_height,
        )
        destination = ImageRect(0.0, 0.0, target.width, target.height)

    return ImageLayout(
        source_size=source,
        target_size=target,
        mode=fit_mode,
        source_crop=source_crop,
        destination=destination,
        scale=scale,
    )


def _resize_as_rgba(
    source: Image.Image,
    size: SizeLike,
    *,
    resample: Image.Resampling,
    box: tuple[float, float, float, float] | None = None,
) -> Image.Image:
    """Resize first and convert the smaller result to RGBA when possible.

    Converting a 40-megapixel RGB photo to RGBA before making a thumbnail adds
    another large full-resolution allocation.  Pillow can resize the common
    display modes directly, so conversion normally happens only after the
    output has reached viewport size.  The fallback preserves support for
    uncommon Pillow modes whose resizer requires RGB/RGBA input.
    """

    try:
        resized = source.resize(size, resample=resample, box=box)
    except ValueError:
        compatible = source.convert("RGBA")
        try:
            return compatible.resize(size, resample=resample, box=box)
        finally:
            compatible.close()

    if resized.mode == "RGBA":
        return resized
    try:
        return resized.convert("RGBA")
    finally:
        resized.close()


def fit_image(
    source: Image.Image,
    target_size: ImageSize | SizeLike,
    mode: ImageFitMode | str,
    *,
    resample: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """Return an independent RGBA image exactly matching ``target_size``.

    ``contain`` uses transparent letterboxing so the UI's own background remains
    visible.  ``cover`` uses a centered source crop and therefore fills every
    destination pixel.  The input image is never mutated or closed.
    """

    if not isinstance(source, Image.Image):
        raise ImageGeometryError("source must be a Pillow image")
    target = coerce_image_size(target_size, label="target size")
    if (
        target.width > MAX_RENDER_DIMENSION
        or target.height > MAX_RENDER_DIMENSION
        or target.pixels > MAX_RENDER_PIXELS
    ):
        raise ImageGeometryError("target size exceeds the safe render limit")

    layout = calculate_image_layout(source.size, target, mode)
    if layout.mode is ImageFitMode.COVER:
        # Pillow accepts a floating source box, avoiding asymmetric rounding
        # when an odd number of source pixels must be cropped.
        return _resize_as_rgba(
            source,
            target.as_tuple(),
            resample=resample,
            box=layout.source_crop.as_box(),
        )

    rendered_width = min(target.width, max(1, int(round(layout.destination.width))))
    rendered_height = min(target.height, max(1, int(round(layout.destination.height))))
    rendered = _resize_as_rgba(
        source,
        (rendered_width, rendered_height),
        resample=resample,
    )
    try:
        canvas = Image.new("RGBA", target.as_tuple(), (0, 0, 0, 0))
        left = (target.width - rendered_width) // 2
        top = (target.height - rendered_height) // 2
        canvas.alpha_composite(rendered, (left, top))
        return canvas
    finally:
        rendered.close()


# Short alias useful to callers that already use the word "layout".
calculate_layout = calculate_image_layout
