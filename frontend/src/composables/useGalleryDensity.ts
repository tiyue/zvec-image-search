import { ref, type Ref } from "vue";

export type GalleryDensity = "compact" | "standard" | "large";
export type GalleryCardMode = "search" | "organize";

export interface GalleryDensityOption {
  value: GalleryDensity;
  label: string;
}

export interface GalleryLayout {
  columns: number;
  rows: number;
  pageSize: number;
  cardWidth: number;
  /** Pixel height of a laid out tile; `0` means the caller did not measure yet. */
  cardHeight: number;
}

export const GALLERY_DENSITY_STORAGE_KEY = "yaolens.gallery-density";

export const GALLERY_DENSITY_OPTIONS: readonly GalleryDensityOption[] = [
  { value: "compact", label: "紧凑" },
  { value: "standard", label: "标准" },
  { value: "large", label: "大图" },
];

/** Gap between gallery tiles in pixels; must match the CSS `gap`. */
export const GALLERY_GRID_GAP = 8;

/** Target tile width for the three density tiers, in pixels. */
const DENSITY_CARD_WIDTH: Record<GalleryDensity, number> = {
  compact: 144,
  standard: 192,
  large: 272,
};

const MODE_MAX_PAGE_SIZE: Record<GalleryCardMode, number> = {
  search: 60,
  organize: 32,
};
const MODE_FALLBACK_PAGE_SIZE: Record<GalleryCardMode, number> = {
  search: 15,
  organize: 6,
};
const MODE_FALLBACK_COLUMNS: Record<GalleryCardMode, number> = {
  search: 5,
  organize: 3,
};
/** Upper bound so a very wide window cannot produce an unreadably dense grid. */
const MAX_COLUMNS = 12;

export function isGalleryDensity(value: unknown): value is GalleryDensity {
  return value === "compact" || value === "standard" || value === "large";
}

/** Read a persisted density, ignoring unknown, corrupt or unavailable storage. */
export function readStoredGalleryDensity(
  storage?: Pick<Storage, "getItem"> | null,
): GalleryDensity {
  try {
    const stored = storage?.getItem(GALLERY_DENSITY_STORAGE_KEY);
    return isGalleryDensity(stored) ? stored : "standard";
  } catch {
    return "standard";
  }
}

export function clampGalleryPageSize(
  pageSize: unknown,
  mode: GalleryCardMode = "search",
): number {
  const parsed = Number(pageSize);
  if (!Number.isFinite(parsed)) return MODE_FALLBACK_PAGE_SIZE[mode];
  return Math.min(MODE_MAX_PAGE_SIZE[mode], Math.max(1, Math.trunc(parsed)));
}

/**
 * Pure layout maths shared by the search grid and the organize gallery.
 * Search tiles are image only, so a tile is as tall as it is wide; organize
 * tiles carry file metadata and tags, so their height adds `metaOverhead`.
 * Unmeasured containers fall back to the previous fixed capacities.
 */
export function computeGalleryLayout(params: {
  width: number;
  height: number;
  density: GalleryDensity;
  mode: GalleryCardMode;
  metaOverhead?: number;
}): GalleryLayout {
  const mode = params.mode;
  const density = isGalleryDensity(params.density) ? params.density : "standard";
  const cardWidth = DENSITY_CARD_WIDTH[density];
  const metaOverhead = Math.max(0, Number(params.metaOverhead) || 0);
  const cardHeight = mode === "search" ? cardWidth : cardWidth + metaOverhead;
  const width = Number.isFinite(params.width) ? Math.max(0, params.width) : 0;
  const height = Number.isFinite(params.height) ? Math.max(0, params.height) : 0;
  if (width <= 0 || height <= 0) {
    return {
      columns: MODE_FALLBACK_COLUMNS[mode],
      rows: 0,
      pageSize: MODE_FALLBACK_PAGE_SIZE[mode],
      cardWidth,
      cardHeight: 0,
    };
  }
  const track = cardWidth + GALLERY_GRID_GAP;
  const columns = Math.min(
    MAX_COLUMNS,
    Math.max(1, Math.floor((width + GALLERY_GRID_GAP) / track)),
  );
  const rows = Math.max(
    1,
    Math.floor((height + GALLERY_GRID_GAP) / (cardHeight + GALLERY_GRID_GAP)),
  );
  return {
    columns,
    rows,
    pageSize: clampGalleryPageSize(columns * rows, mode),
    cardWidth,
    cardHeight,
  };
}

let sharedDensity: Ref<GalleryDensity> | null = null;

function browserStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/**
 * Shared, locally persisted density so the search grid and the organize
 * gallery always show the same tier.
 */
export function galleryDensityRef(): Ref<GalleryDensity> {
  sharedDensity ??= ref(readStoredGalleryDensity(browserStorage()));
  return sharedDensity;
}

export function setGalleryDensity(value: unknown): GalleryDensity {
  const density = galleryDensityRef();
  const next = isGalleryDensity(value) ? value : "standard";
  density.value = next;
  try {
    browserStorage()?.setItem(GALLERY_DENSITY_STORAGE_KEY, next);
  } catch {
    // Persisting is best effort; the in-memory choice still applies.
  }
  return next;
}

export function useGalleryDensity() {
  return {
    density: galleryDensityRef(),
    options: GALLERY_DENSITY_OPTIONS,
    setDensity: setGalleryDensity,
  };
}
