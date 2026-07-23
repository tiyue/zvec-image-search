export const ORGANIZE_GALLERY_MAX_PAGE_SIZE = 32;
export const ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE = 12;

export interface OrganizeGalleryCapacity {
  pageSize: number;
}

/**
 * Dense grid layout: images span 1 or 2 columns based on aspect ratio.
 * The grid scrolls vertically with enough items per page to fill several rows.
 */
export function calculateOrganizeGalleryCapacity(
  _width: number,
  _height: number,
): OrganizeGalleryCapacity {
  return { pageSize: ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE };
}

/**
 * Determine the grid column span for an image based on its aspect ratio.
 * Landscape images (w/h > 1.5) span 2 columns; others span 1.
 */
export function gridSpan(width: number, height: number): 1 | 2 {
  if (!width || !height) return 1;
  return width / height > 1.5 ? 2 : 1;
}
