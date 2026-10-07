import { computed, ref, watch, type ComputedRef, type Ref } from "vue";

/**
 * Minimal image shape the shared viewer renders. Source pages map their own
 * records onto this so the overlay never depends on a feature module.
 */
export interface ViewerImage {
  id: string;
  name: string;
  relativePath: string;
  libraryName: string;
  width: number;
  height: number;
  tags: string[];
  thumbnailUrl: string;
  imageUrl: string;
  nativeId: string;
}

export interface ViewerNavigation {
  /** Ordered images the source has already materialised. */
  items: () => ViewerImage[];
  /** Id to focus when the viewer opens without an explicit target. */
  startId: () => string;
  /** Whether a page/tile beyond the loaded window exists in this direction. */
  hasMore: (direction: -1 | 1) => boolean;
  /** Load the adjacent page; resolve to the id to focus, or null on failure. */
  loadAdjacent: (direction: -1 | 1) => Promise<string | null>;
}

export interface ViewerSession {
  visible: Ref<boolean>;
  currentId: Ref<string>;
  current: ComputedRef<ViewerImage | null>;
  loading: Ref<boolean>;
  error: Ref<string>;
  hasPrevious: ComputedRef<boolean>;
  hasNext: ComputedRef<boolean>;
  open: (id: string) => void;
  close: () => void;
  step: (direction: -1 | 1) => Promise<void>;
  retry: () => Promise<void>;
}

/**
 * Number of viewer overlays currently on screen. The application shell reads
 * this so a viewer from any source suspends background page shortcuts and
 * selection while it is displayed.
 */
let openViewerOverlays = 0;

export function isViewerOverlayOpen(): boolean {
  return openViewerOverlays > 0;
}

/**
 * Hold the "an overlay is on screen" state until the returned release runs.
 * Releasing is idempotent so a remount or unmount cannot skew the count.
 */
export function acquireViewerOverlay(): () => void {
  openViewerOverlays += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    openViewerOverlays -= 1;
  };
}

/**
 * Tracks which image the overlay shows and owns the "who may still apply"
 * rule: every open/step/close bumps a generation so a late page response can
 * never reopen the viewer or overwrite a newer selection.
 */
export function useImageViewerSession(navigation: ViewerNavigation): ViewerSession {
  const visible = ref(false);
  const currentId = ref("");
  const loading = ref(false);
  const error = ref("");
  let generation = 0;
  let pendingDirection: -1 | 1 = 1;

  const resolvedCurrent = computed<ViewerImage | null>(() => {
    if (!visible.value) return null;
    return navigation.items().find((item) => item.id === currentId.value) ?? null;
  });
  // One-slot snapshot of the image on screen. A cross-page step applies the new
  // window before the new id lands, so without it the overlay would fall to an
  // empty state (and churn its dialog) mid-transition. It never grows, and
  // closing or leaving the id behind clears it.
  let shown: ViewerImage | null = null;
  watch(
    resolvedCurrent,
    (found) => {
      if (found) shown = found;
      else if (shown && shown.id !== currentId.value) shown = null;
    },
    { flush: "sync", immediate: true },
  );
  const current = computed<ViewerImage | null>(() => {
    if (!visible.value) return null;
    return resolvedCurrent.value ?? (shown && shown.id === currentId.value ? shown : null);
  });

  function neighborId(direction: -1 | 1): string | null {
    const items = navigation.items();
    const index = items.findIndex((item) => item.id === currentId.value);
    if (index < 0) return null;
    return items[index + direction]?.id ?? null;
  }

  function stepAvailable(direction: -1 | 1): boolean {
    if (!visible.value) return false;
    return neighborId(direction) !== null || navigation.hasMore(direction);
  }

  const hasPrevious = computed(() => stepAvailable(-1));
  const hasNext = computed(() => stepAvailable(1));

  function open(id: string): void {
    // An explicit target must already be materialised; without one, only a
    // resolvable start id may open the overlay. Neither the unknown target nor
    // an empty window may create a visible-but-empty dialog.
    const requested = id || navigation.startId();
    const target = resolvable(requested) ? requested : "";
    generation += 1;
    loading.value = false;
    error.value = "";
    currentId.value = target;
    visible.value = Boolean(target);
  }

  function resolvable(id: string): boolean {
    if (!id) return false;
    // The overlay always renders an image, so an entry with no URL at all must
    // never open a visible-but-empty dialog. A thumbnail-only or full-only
    // entry is still viewable.
    return navigation.items().some(
      (entry) => entry.id === id && Boolean(entry.thumbnailUrl || entry.imageUrl),
    );
  }

  function close(): void {
    generation += 1;
    visible.value = false;
    currentId.value = "";
    loading.value = false;
    error.value = "";
  }

  async function step(direction: -1 | 1): Promise<void> {
    if (!visible.value || loading.value) return;
    pendingDirection = direction;
    error.value = "";
    const inWindow = neighborId(direction);
    if (inWindow !== null) {
      currentId.value = inWindow;
      return;
    }
    if (!navigation.hasMore(direction)) return;
    const run = ++generation;
    const failure = direction > 0 ? "无法读取下一页图片" : "无法读取上一页图片";
    loading.value = true;
    try {
      const id = await navigation.loadAdjacent(direction);
      if (run !== generation || !visible.value) return;
      if (id) currentId.value = id;
      else error.value = failure;
    } catch {
      if (run === generation && visible.value) error.value = failure;
    } finally {
      if (run === generation) loading.value = false;
    }
  }

  function retry(): Promise<void> {
    return step(pendingDirection);
  }

  return {
    visible,
    currentId,
    current,
    loading,
    error,
    hasPrevious,
    hasNext,
    open,
    close,
    step,
    retry,
  };
}

export interface ViewerGeometry {
  stageRef: Ref<HTMLElement | null>;
  naturalWidth: Ref<number>;
  naturalHeight: Ref<number>;
  scale: Ref<number>;
  offsetX: Ref<number>;
  offsetY: Ref<number>;
  isFit: Ref<boolean>;
  dragging: Ref<boolean>;
  zoomLabel: ComputedRef<string>;
  canvasStyle: ComputedRef<Record<string, string>>;
  setNaturalSize: (width: number, height: number) => void;
  clearNaturalSize: () => void;
  resetToFit: () => void;
  setFit: () => void;
  setActual: () => void;
  toggleFit: () => void;
  handleWheel: (event: WheelEvent) => void;
  startPan: (event: PointerEvent) => void;
  movePan: (event: PointerEvent) => void;
  endPan: (event: PointerEvent) => void;
  remeasure: () => void;
}

const MIN_ZOOM_FACTOR = 8;
const WHEEL_SENSITIVITY = 0.0015;

/**
 * A press that starts on an interactive control (the navigation, retry, zoom
 * or close buttons) is a click, not a canvas drag. Capturing the pointer on the
 * stage there would retarget the following mouse events to the stage and the
 * button would never see its own click.
 */
function startsOnControl(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest("button, a, input, select, textarea, [role='button']") !== null
  );
}

/**
 * Fit/100% zoom plus pointer-centred wheel zoom and clamped panning. Sizes are
 * real pixel dimensions so 100% never derives from a scaled thumbnail.
 */
export function useViewerGeometry(): ViewerGeometry {
  const stageRef = ref<HTMLElement | null>(null);
  const naturalWidth = ref(0);
  const naturalHeight = ref(0);
  const containerWidth = ref(0);
  const containerHeight = ref(0);
  const scale = ref(1);
  const offsetX = ref(0);
  const offsetY = ref(0);
  const isFit = ref(true);
  const dragging = ref(false);
  let panStartX = 0;
  let panStartY = 0;
  let panOriginX = 0;
  let panOriginY = 0;

  const fitScale = computed(() => {
    if (naturalWidth.value <= 0 || naturalHeight.value <= 0) return 1;
    if (containerWidth.value <= 0 || containerHeight.value <= 0) return 1;
    return Math.min(
      containerWidth.value / naturalWidth.value,
      containerHeight.value / naturalHeight.value,
    );
  });
  const minScale = computed(() => Math.min(fitScale.value, 1));
  const maxScale = computed(() => Math.max(1, fitScale.value) * MIN_ZOOM_FACTOR);

  function clampOffsets(): void {
    const maxX = Math.max(0, (naturalWidth.value * scale.value - containerWidth.value) / 2);
    const maxY = Math.max(0, (naturalHeight.value * scale.value - containerHeight.value) / 2);
    offsetX.value = Math.min(maxX, Math.max(-maxX, offsetX.value));
    offsetY.value = Math.min(maxY, Math.max(-maxY, offsetY.value));
  }

  function measure(): void {
    const element = stageRef.value;
    containerWidth.value = element?.clientWidth ?? 0;
    containerHeight.value = element?.clientHeight ?? 0;
  }

  function setNaturalSize(width: number, height: number): void {
    if (width > 0) naturalWidth.value = width;
    if (height > 0) naturalHeight.value = height;
  }

  function clearNaturalSize(): void {
    naturalWidth.value = 0;
    naturalHeight.value = 0;
  }

  function applyScale(next: number, clientX?: number, clientY?: number): void {
    const clamped = Math.min(maxScale.value, Math.max(minScale.value, next));
    const element = stageRef.value;
    if (element && clientX !== undefined && clientY !== undefined) {
      const rect = element.getBoundingClientRect();
      const pointerX = clientX - (rect.left + rect.width / 2);
      const pointerY = clientY - (rect.top + rect.height / 2);
      const ratio = clamped / scale.value;
      offsetX.value = pointerX - (pointerX - offsetX.value) * ratio;
      offsetY.value = pointerY - (pointerY - offsetY.value) * ratio;
    }
    scale.value = clamped;
    isFit.value = Math.abs(clamped - fitScale.value) < 1e-4;
    clampOffsets();
  }

  function resetToFit(): void {
    measure();
    isFit.value = true;
    offsetX.value = 0;
    offsetY.value = 0;
    scale.value = fitScale.value;
    clampOffsets();
  }

  function setActual(): void {
    measure();
    offsetX.value = 0;
    offsetY.value = 0;
    applyScale(1);
  }

  function toggleFit(): void {
    if (isFit.value) setActual();
    else resetToFit();
  }

  function handleWheel(event: WheelEvent): void {
    event.preventDefault();
    if (!event.deltaY) return;
    measure();
    applyScale(scale.value * Math.exp(-event.deltaY * WHEEL_SENSITIVITY), event.clientX, event.clientY);
  }

  function startPan(event: PointerEvent): void {
    if (event.button !== 0) return;
    if (startsOnControl(event.target)) return;
    const element = event.currentTarget;
    if (element instanceof HTMLElement) {
      element.setPointerCapture?.(event.pointerId);
    }
    dragging.value = true;
    panStartX = event.clientX;
    panStartY = event.clientY;
    panOriginX = offsetX.value;
    panOriginY = offsetY.value;
  }

  function movePan(event: PointerEvent): void {
    if (!dragging.value) return;
    offsetX.value = panOriginX + (event.clientX - panStartX);
    offsetY.value = panOriginY + (event.clientY - panStartY);
    clampOffsets();
  }

  function endPan(event: PointerEvent): void {
    if (!dragging.value) return;
    dragging.value = false;
    const element = event.currentTarget;
    if (element instanceof HTMLElement) {
      element.releasePointerCapture?.(event.pointerId);
    }
  }

  function remeasure(): void {
    const wasFit = isFit.value;
    measure();
    if (wasFit) {
      scale.value = fitScale.value;
    } else {
      scale.value = Math.min(maxScale.value, Math.max(minScale.value, scale.value));
    }
    isFit.value = Math.abs(scale.value - fitScale.value) < 1e-4;
    clampOffsets();
  }

  const zoomLabel = computed(() => `${Math.round(scale.value * 100)}%`);
  const canvasStyle = computed<Record<string, string>>(() => ({
    width: naturalWidth.value > 0 ? `${naturalWidth.value}px` : "100%",
    height: naturalHeight.value > 0 ? `${naturalHeight.value}px` : "100%",
    transform:
      `translate(-50%, -50%) translate(${offsetX.value}px, ${offsetY.value}px) scale(${scale.value})`,
  }));

  return {
    stageRef,
    naturalWidth,
    naturalHeight,
    scale,
    offsetX,
    offsetY,
    isFit,
    dragging,
    zoomLabel,
    canvasStyle,
    setNaturalSize,
    clearNaturalSize,
    resetToFit,
    setFit: resetToFit,
    setActual,
    toggleFit,
    handleWheel,
    startPan,
    movePan,
    endPan,
    remeasure,
  };
}
