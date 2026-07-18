"""Toolkit-independent sizing rules for the 15-image desktop gallery.

The normal desktop target is five columns by three rows.  Narrow or short
windows keep usable card sizes and let the widget expose vertical scrolling
instead of shrinking thumbnails until they are unreadable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_GALLERY_PAGE_SIZE = 15
DEFAULT_GALLERY_COLUMNS = 5
DEFAULT_GALLERY_ROWS = 3


@dataclass(frozen=True, slots=True)
class GalleryLayout:
    columns: int
    rows: int
    card_width: int
    card_height: int
    content_height: int
    scroll_required: bool


def calculate_gallery_layout(
    viewport_width: int,
    viewport_height: int,
    item_count: int,
    *,
    max_columns: int = DEFAULT_GALLERY_COLUMNS,
    target_rows: int = DEFAULT_GALLERY_ROWS,
    minimum_card_width: int = 104,
    minimum_card_height: int = 128,
    compact_card_height: int = 154,
    gap: int = 8,
) -> GalleryLayout:
    """Fill a normal viewport with 5x3 cards and scroll compact windows.

    ``minimum_card_width`` determines when another column can be introduced;
    it is deliberately not forced on a one-column viewport.  That distinction
    keeps extremely narrow windows inside their visible horizontal bounds
    while vertical space remains scrollable.
    """

    values = {
        "viewport_width": viewport_width,
        "viewport_height": viewport_height,
        "item_count": item_count,
        "max_columns": max_columns,
        "target_rows": target_rows,
        "minimum_card_width": minimum_card_width,
        "minimum_card_height": minimum_card_height,
        "compact_card_height": compact_card_height,
        "gap": gap,
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in values.values()
    ):
        raise ValueError("gallery layout values must be integers")
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("gallery viewport must be positive")
    if item_count < 0:
        raise ValueError("item_count cannot be negative")
    if (
        min(
            max_columns,
            target_rows,
            minimum_card_width,
            minimum_card_height,
            compact_card_height,
            gap,
        )
        <= 0
    ):
        raise ValueError("gallery sizing constants must be positive")

    columns = max(
        1,
        min(
            max_columns,
            (viewport_width - gap) // (minimum_card_width + gap),
        ),
    )
    available_width = max(1, viewport_width - gap * (columns + 1))
    card_width = max(1, available_width // columns)
    if item_count == 0:
        return GalleryLayout(
            columns=columns,
            rows=0,
            card_width=card_width,
            card_height=0,
            content_height=0,
            scroll_required=False,
        )

    rows = math.ceil(item_count / columns)
    available_height = viewport_height - gap * (rows + 1)
    normal_grid_fits = (
        rows <= target_rows and available_height >= rows * minimum_card_height
    )
    card_height = available_height // rows if normal_grid_fits else compact_card_height
    content_height = rows * card_height + (rows + 1) * gap
    return GalleryLayout(
        columns=columns,
        rows=rows,
        card_width=card_width,
        card_height=card_height,
        content_height=content_height,
        scroll_required=content_height > viewport_height,
    )
