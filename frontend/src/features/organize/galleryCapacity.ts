import {
  computeGalleryLayout,
  type GalleryDensity,
} from "../../composables/useGalleryDensity";

export const ORGANIZE_GALLERY_MAX_PAGE_SIZE = 32;
export const ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE = 6;

export interface OrganizeGalleryCapacity {
  pageSize: number;
  columns: number;
}

/**
 * Dense grid layout: images span 1 or 2 columns based on aspect ratio.
 * Columns and page capacity follow the real container size and the shared
 * thumbnail density; unmeasured containers keep the previous 6-image default.
 */
export function calculateOrganizeGalleryCapacity(
  width: number,
  height: number,
  density: GalleryDensity = "standard",
): OrganizeGalleryCapacity {
  const layout = computeGalleryLayout({
    width,
    height,
    density,
    mode: "organize",
    metaOverhead: GALLERY_META_OVERHEAD,
  });
  return { pageSize: layout.pageSize, columns: layout.columns };
}

/**
 * Determine the grid column span for an image based on its aspect ratio.
 * Landscape images (w/h > 1.5) span 2 columns; others span 1.
 */
export function gridSpan(width: number, height: number): 1 | 2 {
  if (!width || !height) return 1;
  return width / height > 1.5 ? 2 : 1;
}

/** Masonry row-unit size in pixels. */
export const GRID_ROW_UNIT = 10;
/** Gap between grid tracks in pixels (must match CSS `gap`). */
export const GRID_GAP = 8;
/** Horizontal padding of `.image-grid` (1px left + 3px right) inside the viewport. */
export const GRID_HORIZONTAL_PADDING = 4;
/** Estimated non-image metadata height for gallery tiles (image-meta + image-tags). */
export const GALLERY_META_OVERHEAD = 75;

/**
 * Calculate the grid row span for an image so its cell height matches the
 * image's aspect ratio. Uses a small row unit (GRID_ROW_UNIT) so the grid
 * behaves like a masonry layout when combined with `grid-auto-flow: row dense`.
 *
 * Each row track is GRID_ROW_UNIT px tall; adjacent tracks are separated by
 * `gap` px. An item spanning N rows has height `N*unit + (N-1)*gap`.
 * We solve for the smallest N satisfying `N*unit + (N-1)*gap >= totalHeight`.
 */
export function gridRowSpan(
  imgWidth: number,
  imgHeight: number,
  colSpan: 1 | 2,
  containerWidth: number,
  columnCount: number,
  gap: number,
  metaOverhead: number,
): number {
  if (!imgWidth || !imgHeight || !containerWidth) return 1;
  const colWidth = (containerWidth - (columnCount - 1) * gap) / columnCount;
  const cellWidth =
    colSpan === 2
      ? colSpan * colWidth + (colSpan - 1) * gap
      : colWidth;
  const imageHeight = (cellWidth * imgHeight) / imgWidth;
  const totalHeight = imageHeight + metaOverhead;
  return Math.max(1, Math.ceil((totalHeight + gap) / (GRID_ROW_UNIT + gap)));
}
