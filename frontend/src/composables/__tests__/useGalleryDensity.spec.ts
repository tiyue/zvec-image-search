import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clampGalleryPageSize,
  computeGalleryLayout,
  GALLERY_DENSITY_STORAGE_KEY,
  isGalleryDensity,
  readStoredGalleryDensity,
  setGalleryDensity,
  useGalleryDensity,
} from "../useGalleryDensity";

function storageWith(value: string | null): Pick<Storage, "getItem"> {
  return { getItem: () => value };
}

describe("gallery density storage", () => {
  afterEach(() => {
    setGalleryDensity("standard");
    try {
      localStorage.clear();
    } catch {
      // jsdom always provides localStorage; ignore exotic environments.
    }
  });

  it("accepts only the three known tiers", () => {
    expect(isGalleryDensity("compact")).toBe(true);
    expect(isGalleryDensity("standard")).toBe(true);
    expect(isGalleryDensity("large")).toBe(true);
    expect(isGalleryDensity("huge")).toBe(false);
    expect(isGalleryDensity(3)).toBe(false);
    expect(isGalleryDensity(null)).toBe(false);
  });

  it("falls back to standard for missing, unknown or unreadable storage", () => {
    expect(readStoredGalleryDensity(storageWith(null))).toBe("standard");
    expect(readStoredGalleryDensity(storageWith("enormous"))).toBe("standard");
    expect(readStoredGalleryDensity(storageWith('{"density":"large"}'))).toBe("standard");
    expect(readStoredGalleryDensity(undefined)).toBe("standard");
    expect(
      readStoredGalleryDensity({
        getItem: () => {
          throw new Error("blocked");
        },
      }),
    ).toBe("standard");
    expect(readStoredGalleryDensity(storageWith("compact"))).toBe("compact");
  });

  it("persists the shared choice and ignores invalid new values", () => {
    const { density, setDensity } = useGalleryDensity();

    setDensity("large");
    expect(density.value).toBe("large");
    expect(localStorage.getItem(GALLERY_DENSITY_STORAGE_KEY)).toBe("large");

    // A second consumer observes the same choice.
    expect(useGalleryDensity().density.value).toBe("large");

    setDensity("nonsense");
    expect(density.value).toBe("standard");
    expect(localStorage.getItem(GALLERY_DENSITY_STORAGE_KEY)).toBe("standard");
  });

  it("does not throw when storage writes are blocked", () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded");
      });
    try {
      expect(setGalleryDensity("compact")).toBe("compact");
    } finally {
      setItem.mockRestore();
    }
  });
});

describe("computeGalleryLayout", () => {
  it("uses the three tiers' target widths at a wide viewport", () => {
    const base = { width: 1600, height: 900, mode: "search" as const };
    expect(computeGalleryLayout({ ...base, density: "compact" })).toMatchObject({
      cardWidth: 144,
      cardHeight: 144,
    });
    expect(computeGalleryLayout({ ...base, density: "standard" })).toMatchObject({
      cardWidth: 192,
      cardHeight: 192,
    });
    expect(computeGalleryLayout({ ...base, density: "large" })).toMatchObject({
      cardWidth: 272,
      cardHeight: 272,
    });
    // Smaller target widths always fit at least as many columns.
    const columns = (density: "compact" | "standard" | "large") =>
      computeGalleryLayout({ ...base, density }).columns;
    expect(columns("compact")).toBeGreaterThan(columns("standard"));
    expect(columns("standard")).toBeGreaterThan(columns("large"));
  });

  it("reduces the column count on a narrow window", () => {
    const wide = computeGalleryLayout({
      width: 1600,
      height: 900,
      density: "standard",
      mode: "search",
    });
    const narrow = computeGalleryLayout({
      width: 420,
      height: 900,
      density: "standard",
      mode: "search",
    });
    expect(wide.columns).toBeGreaterThan(narrow.columns);
    expect(narrow.columns).toBe(2);
  });

  it("falls back to the previous defaults when nothing was measured", () => {
    expect(
      computeGalleryLayout({ width: 0, height: 0, density: "standard", mode: "search" }),
    ).toEqual({ columns: 5, rows: 0, pageSize: 15, cardWidth: 192, cardHeight: 0 });
    expect(
      computeGalleryLayout({ width: 900, height: 0, density: "standard", mode: "search" }),
    ).toEqual({ columns: 5, rows: 0, pageSize: 15, cardWidth: 192, cardHeight: 0 });
    expect(
      computeGalleryLayout({
        width: 0,
        height: 0,
        density: "standard",
        mode: "organize",
        metaOverhead: 75,
      }),
    ).toEqual({ columns: 3, rows: 0, pageSize: 6, cardWidth: 192, cardHeight: 0 });
  });

  it("clamps tiny viewports to one column and one row", () => {
    expect(
      computeGalleryLayout({ width: 40, height: 30, density: "large", mode: "search" }),
    ).toEqual({ columns: 1, rows: 1, pageSize: 1, cardWidth: 272, cardHeight: 272 });
  });

  it("caps the page size at the mode limit and the columns at twelve", () => {
    const search = computeGalleryLayout({
      width: 6000,
      height: 4000,
      density: "compact",
      mode: "search",
    });
    expect(search.columns).toBe(12);
    expect(search.pageSize).toBe(60);

    const organize = computeGalleryLayout({
      width: 6000,
      height: 4000,
      density: "compact",
      mode: "organize",
      metaOverhead: 75,
    });
    expect(organize.columns).toBe(12);
    expect(organize.pageSize).toBe(32);
  });

  it("adds the metadata overhead to organize tiles only", () => {
    const search = computeGalleryLayout({
      width: 1200,
      height: 900,
      density: "standard",
      mode: "search",
    });
    const organize = computeGalleryLayout({
      width: 1200,
      height: 900,
      density: "standard",
      mode: "organize",
      metaOverhead: 75,
    });
    expect(organize.cardHeight).toBe(search.cardHeight + 75);
    expect(organize.rows).toBeLessThan(search.rows);
  });
});

describe("clampGalleryPageSize", () => {
  it("bounds capacities per gallery mode", () => {
    expect(clampGalleryPageSize(0)).toBe(1);
    expect(clampGalleryPageSize(-8)).toBe(1);
    expect(clampGalleryPageSize(200)).toBe(60);
    expect(clampGalleryPageSize(200, "organize")).toBe(32);
    expect(clampGalleryPageSize(Number.NaN)).toBe(15);
    expect(clampGalleryPageSize(Number.NaN, "organize")).toBe(6);
    expect(clampGalleryPageSize(7.9)).toBe(7);
  });
});
