import { describe, expect, it } from "vitest";

import {
  calculateOrganizeGalleryCapacity,
  GALLERY_META_OVERHEAD,
  GRID_GAP,
  gridRowSpan,
  gridSpan,
} from "../galleryCapacity";

describe("calculateOrganizeGalleryCapacity", () => {
  it("derives columns and page size from the measured viewport", () => {
    // 1547px / 192px target width => 7 columns; 1030px / (192 + 75 meta) => 3 rows.
    expect(calculateOrganizeGalleryCapacity(1547, 1030)).toEqual({
      pageSize: 21,
      columns: 7,
    });
    expect(calculateOrganizeGalleryCapacity(470, 300)).toEqual({
      pageSize: 2,
      columns: 2,
    });
  });

  it("keeps the previous bounded default when nothing was measured", () => {
    expect(calculateOrganizeGalleryCapacity(0, 0)).toEqual({
      pageSize: 6,
      columns: 3,
    });
    expect(calculateOrganizeGalleryCapacity(0, 1030)).toEqual({
      pageSize: 6,
      columns: 3,
    });
  });

  it("follows the shared density tier", () => {
    // compact (144px) and large (272px) targets on the same viewport.
    expect(calculateOrganizeGalleryCapacity(1547, 1030, "compact")).toEqual({
      pageSize: 32,
      columns: 10,
    });
    expect(calculateOrganizeGalleryCapacity(1547, 1030, "large")).toEqual({
      pageSize: 10,
      columns: 5,
    });
  });
});

describe("gridSpan", () => {
  it("returns 2 for landscape images", () => {
    expect(gridSpan(1920, 1080)).toBe(2);
    expect(gridSpan(2400, 1200)).toBe(2);
    expect(gridSpan(800, 500)).toBe(2);
  });

  it("returns 1 for portrait and square images", () => {
    expect(gridSpan(1080, 1920)).toBe(1);
    expect(gridSpan(1000, 1000)).toBe(1);
    expect(gridSpan(800, 1200)).toBe(1);
  });

  it("returns 1 when dimensions are missing", () => {
    expect(gridSpan(0, 0)).toBe(1);
    expect(gridSpan(100, 0)).toBe(1);
    expect(gridSpan(0, 100)).toBe(1);
  });

  it("treats ratio exactly at 1.5 as span 1", () => {
    expect(gridSpan(1500, 1000)).toBe(1);
  });

  it("treats ratio just above 1.5 as span 2", () => {
    expect(gridSpan(1501, 1000)).toBe(2);
  });
});

describe("gridRowSpan", () => {
  const containerWidth = 1200;
  const columnCount = 4;
  const metaOverhead = GALLERY_META_OVERHEAD;

  it("returns more rows for portrait than landscape than square", () => {
    const portrait = gridRowSpan(800, 1200, 1, containerWidth, columnCount, GRID_GAP, metaOverhead);
    const landscape = gridRowSpan(1920, 1080, 2, containerWidth, columnCount, GRID_GAP, metaOverhead);
    const square = gridRowSpan(1000, 1000, 1, containerWidth, columnCount, GRID_GAP, metaOverhead);
    expect(portrait).toBeGreaterThan(landscape);
    expect(landscape).toBeGreaterThan(square);
  });

  it("returns more rows for colSpan 2 than colSpan 1 for the same image", () => {
    const span1 = gridRowSpan(1920, 1080, 1, containerWidth, columnCount, GRID_GAP, metaOverhead);
    const span2 = gridRowSpan(1920, 1080, 2, containerWidth, columnCount, GRID_GAP, metaOverhead);
    expect(span2).toBeGreaterThan(span1);
  });

  it("returns 1 when dimensions or container width are missing", () => {
    expect(gridRowSpan(0, 0, 1, containerWidth, columnCount, GRID_GAP, metaOverhead)).toBe(1);
    expect(gridRowSpan(100, 0, 1, containerWidth, columnCount, GRID_GAP, metaOverhead)).toBe(1);
    expect(gridRowSpan(800, 1200, 1, 0, columnCount, GRID_GAP, metaOverhead)).toBe(1);
  });

  it("returns at least 1 for very wide panoramic images", () => {
    const panorama = gridRowSpan(3840, 1080, 2, containerWidth, columnCount, GRID_GAP, metaOverhead);
    expect(panorama).toBeGreaterThanOrEqual(1);
  });
});
