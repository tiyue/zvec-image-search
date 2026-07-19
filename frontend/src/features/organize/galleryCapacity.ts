export const ORGANIZE_GALLERY_MAX_PAGE_SIZE = 32;
export const ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE = 15;

const CARD_MIN_WIDTH = 168;
const CARD_MIN_HEIGHT = 210;
const GRID_GAP = 9;
const MIN_COLUMNS = 2;
const MAX_COLUMNS = 8;
const MIN_ROWS = 2;
const MAX_ROWS = 4;

export interface OrganizeGalleryCapacity {
  columns: number;
  rows: number;
  pageSize: number;
  scrollRequired: boolean;
}

function boundedInteger(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.trunc(value)));
}

/**
 * Calculate a readable folder-gallery page from the space that is actually
 * available after the heading, selection tools and paginator are laid out.
 * Small windows retain two rows and scroll instead of shrinking thumbnails.
 */
export function calculateOrganizeGalleryCapacity(
  width: number,
  height: number,
): OrganizeGalleryCapacity {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return {
      columns: 5,
      rows: 3,
      pageSize: ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
      scrollRequired: false,
    };
  }

  const naturalColumns = Math.floor((width + GRID_GAP) / (CARD_MIN_WIDTH + GRID_GAP));
  const naturalRows = Math.floor((height + GRID_GAP) / (CARD_MIN_HEIGHT + GRID_GAP));
  const columns = boundedInteger(naturalColumns, MIN_COLUMNS, MAX_COLUMNS);
  const rows = boundedInteger(naturalRows, MIN_ROWS, MAX_ROWS);
  return {
    columns,
    rows,
    pageSize: Math.min(ORGANIZE_GALLERY_MAX_PAGE_SIZE, columns * rows),
    scrollRequired: naturalColumns < MIN_COLUMNS || naturalRows < MIN_ROWS,
  };
}
