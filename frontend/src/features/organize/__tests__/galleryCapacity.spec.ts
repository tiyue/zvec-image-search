import { describe, expect, it } from "vitest";

import {
  calculateOrganizeGalleryCapacity,
  GALLERY_META_OVERHEAD,
  GRID_GAP,
  gridRowSpan,
  gridSpan,
  ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
} from "../galleryCapacity";

describe("calculateOrganizeGalleryCapacity", () => {
  it("returns a fixed page size regardless of viewport", () => {
    expect(calculateOrganizeGalleryCapacity(1547, 1030)).toEqual({
      pageSize: ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
    });
    expect(calculateOrganizeGalleryCapacity(0, 0)).toEqual({
      pageSize: ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
    });
    expect(calculateOrganizeGalleryCapacity(470, 300)).toEqual({
      pageSize: ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
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
