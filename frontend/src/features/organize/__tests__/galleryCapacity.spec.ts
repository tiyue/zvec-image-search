import { describe, expect, it } from "vitest";

import {
  calculateOrganizeGalleryCapacity,
  ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
} from "../galleryCapacity";

describe("calculateOrganizeGalleryCapacity", () => {
  it.each([
    { viewport: "2559×1398", width: 1547, height: 1030, columns: 8, rows: 3, pageSize: 24 },
    { viewport: "1920×1080", width: 908, height: 716, columns: 5, rows: 3, pageSize: 15 },
    { viewport: "1600×900", width: 980, height: 536, columns: 5, rows: 2, pageSize: 10 },
    { viewport: "1440×900", width: 860, height: 536, columns: 4, rows: 2, pageSize: 8 },
  ])("fits the $viewport acceptance viewport", ({ width, height, columns, rows, pageSize }) => {
    expect(calculateOrganizeGalleryCapacity(width, height)).toEqual({
      columns,
      rows,
      pageSize,
      scrollRequired: false,
    });
  });

  it("keeps the maximum page bounded even on an oversized gallery", () => {
    expect(calculateOrganizeGalleryCapacity(4_000, 2_000)).toEqual({
      columns: 8,
      rows: 3,
      pageSize: 24,
      scrollRequired: false,
    });
  });

  it("uses measured gallery space rather than the outer window dimensions", () => {
    const measured = calculateOrganizeGalleryCapacity(908, 716);
    expect(measured).toEqual({
      columns: 5,
      rows: 3,
      pageSize: 15,
      scrollRequired: false,
    });
  });

  it("keeps readable cards and enables scrolling in a short viewport", () => {
    expect(calculateOrganizeGalleryCapacity(470, 300)).toEqual({
      columns: 2,
      rows: 2,
      pageSize: 4,
      scrollRequired: true,
    });
  });

  it("uses a stable default before the browser can measure the gallery", () => {
    expect(calculateOrganizeGalleryCapacity(0, 0).pageSize).toBe(
      ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
    );
  });
});
