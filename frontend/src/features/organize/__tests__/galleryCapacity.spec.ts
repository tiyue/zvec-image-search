import { describe, expect, it } from "vitest";

import {
  calculateOrganizeGalleryCapacity,
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
