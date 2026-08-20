import { computed, reactive, ref } from "vue";

export type RawViewportTarget = "single" | 0 | 1;

export interface RawViewportElements {
  container: HTMLElement | null;
  image: HTMLImageElement | null;
}

interface RawViewportState {
  zoom: number;
  panX: number;
  panY: number;
  mode: "fit" | "actual" | "manual";
}

const MIN_ZOOM = 0.001;
// 100% on a 7008px A7M4 frame can exceed 8x in the supported narrow window.
const MAX_ZOOM = 64;

function freshViewport(): RawViewportState {
  return { zoom: 1, panX: 0, panY: 0, mode: "fit" };
}

function clampZoom(value: number): number {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
}

export function useRawSelectionViewport() {
  const single = reactive(freshViewport());
  const compare = [reactive(freshViewport()), reactive(freshViewport())] as const;
  const activeCompareSide = ref<0 | 1>(0);
  const compareSyncState = ref(true);
  const compareSync = computed({
    get: () => compareSyncState.value,
    set: (enabled: boolean) => {
      compareSyncState.value = enabled;
      if (!enabled) return;
      const source = compare[activeCompareSide.value];
      const peer = compare[activeCompareSide.value === 0 ? 1 : 0];
      peer.zoom = source.zoom;
      peer.panX = source.panX;
      peer.panY = source.panY;
      peer.mode = source.mode;
    },
  });
  const isPanning = ref(false);
  let panTarget: RawViewportTarget | null = null;
  let lastClientX = 0;
  let lastClientY = 0;

  function stateFor(target: RawViewportTarget): RawViewportState {
    return target === "single" ? single : compare[target];
  }

  function resetState(state: RawViewportState, zoom = 1): void {
    state.zoom = zoom;
    state.panX = 0;
    state.panY = 0;
    state.mode = "fit";
  }

  function fitZoom(elements?: RawViewportElements): number {
    const container = elements?.container;
    const image = elements?.image;
    if (!container || !image || image.naturalWidth <= 0 || image.naturalHeight <= 0) return 1;
    const rect = container.getBoundingClientRect();
    const width = container.clientWidth || rect.width;
    const height = container.clientHeight || rect.height;
    if (width <= 0 || height <= 0) return 1;
    const rawInset = getComputedStyle(container)
      .getPropertyValue("--raw-preview-safe-inset")
      .trim();
    const parsedInset = Number.parseFloat(rawInset);
    const inset = Number.isFinite(parsedInset) ? Math.max(0, parsedInset) : 0;
    const availableWidth = Math.max(0, width - inset * 2);
    const availableHeight = Math.max(0, height - inset * 2);
    return clampZoom(Math.min(
      availableWidth / image.naturalWidth,
      availableHeight / image.naturalHeight,
    ));
  }

  function fit(
    target: RawViewportTarget,
    elements?: RawViewportElements,
    peer?: RawViewportElements,
  ): void {
    resetState(stateFor(target), fitZoom(elements));
    if (target !== "single" && compareSync.value) {
      resetState(compare[target === 0 ? 1 : 0], fitZoom(peer));
    }
  }

  function resetCompare(): void {
    resetState(compare[0]);
    resetState(compare[1]);
    activeCompareSide.value = 0;
    compareSync.value = true;
  }

  function setZoomAt(
    target: RawViewportTarget,
    nextZoom: number,
    clientX: number,
    clientY: number,
    elements: RawViewportElements,
  ): void {
    const state = stateFor(target);
    const container = elements.container;
    const zoom = clampZoom(nextZoom);
    state.mode = "manual";
    if (!container || state.zoom <= 0) {
      state.zoom = zoom;
      return;
    }
    const rect = container.getBoundingClientRect();
    const pointerX = clientX - (rect.left + rect.width / 2);
    const pointerY = clientY - (rect.top + rect.height / 2);
    const imageX = (pointerX - state.panX) / state.zoom;
    const imageY = (pointerY - state.panY) / state.zoom;
    state.panX = pointerX - imageX * zoom;
    state.panY = pointerY - imageY * zoom;
    state.zoom = zoom;
  }

  function normalizedPoint(
    clientX: number,
    clientY: number,
    container: HTMLElement | null,
  ): { x: number; y: number } {
    if (!container) return { x: 0.5, y: 0.5 };
    const rect = container.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return { x: 0.5, y: 0.5 };
    return {
      x: Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (clientY - rect.top) / rect.height)),
    };
  }

  function pointFor(
    point: { x: number; y: number },
    container: HTMLElement | null,
  ): { clientX: number; clientY: number } {
    if (!container) return { clientX: 0, clientY: 0 };
    const rect = container.getBoundingClientRect();
    return {
      clientX: rect.left + rect.width * point.x,
      clientY: rect.top + rect.height * point.y,
    };
  }

  function zoomAt(
    target: RawViewportTarget,
    nextZoom: number,
    clientX: number,
    clientY: number,
    elements: RawViewportElements,
    peer?: RawViewportElements,
  ): void {
    setZoomAt(target, nextZoom, clientX, clientY, elements);
    if (target === "single" || !compareSync.value) return;
    const peerTarget: 0 | 1 = target === 0 ? 1 : 0;
    const point = normalizedPoint(clientX, clientY, elements.container);
    const peerPoint = pointFor(point, peer?.container ?? null);
    setZoomAt(
      peerTarget,
      nextZoom,
      peerPoint.clientX,
      peerPoint.clientY,
      peer ?? { container: null, image: null },
    );
  }

  function handleWheel(
    target: RawViewportTarget,
    event: WheelEvent,
    elements: RawViewportElements,
    peer?: RawViewportElements,
  ): boolean {
    if (!event.ctrlKey) return false;
    event.preventDefault();
    const current = stateFor(target).zoom;
    const factor = event.deltaY > 0 ? 1 / 1.15 : 1.15;
    zoomAt(target, current * factor, event.clientX, event.clientY, elements, peer);
    return true;
  }

  function showActualSize(
    target: RawViewportTarget,
    elements: RawViewportElements,
    peer?: RawViewportElements,
  ): void {
    const container = elements.container;
    const rect = container?.getBoundingClientRect();
    const clientX = rect ? rect.left + rect.width / 2 : 0;
    const clientY = rect ? rect.top + rect.height / 2 : 0;
    zoomAt(target, 1, clientX, clientY, elements, peer);
    stateFor(target).mode = "actual";
    if (target !== "single" && compareSync.value) {
      stateFor(target === 0 ? 1 : 0).mode = "actual";
    }
  }

  function handleResize(target: RawViewportTarget, elements: RawViewportElements): void {
    const state = stateFor(target);
    if (state.mode !== "fit") return;
    resetState(state, fitZoom(elements));
  }

  function handleDoubleClick(
    target: RawViewportTarget,
    event: MouseEvent,
    elements: RawViewportElements,
    peer?: RawViewportElements,
  ): void {
    if (stateFor(target).mode !== "fit") {
      fit(target, elements, peer);
      return;
    }
    zoomAt(target, 1, event.clientX, event.clientY, elements, peer);
    stateFor(target).mode = "actual";
    if (target !== "single" && compareSync.value) {
      stateFor(target === 0 ? 1 : 0).mode = "actual";
    }
  }

  function startPan(
    target: RawViewportTarget,
    event: MouseEvent,
    elements?: RawViewportElements,
  ): boolean {
    const state = stateFor(target);
    const image = elements?.image;
    const container = elements?.container;
    const rect = container?.getBoundingClientRect();
    const width = container ? container.clientWidth || rect?.width || 0 : 0;
    const height = container ? container.clientHeight || rect?.height || 0 : 0;
    const overflows = image && width > 0 && height > 0
      ? image.naturalWidth * state.zoom > width || image.naturalHeight * state.zoom > height
      : state.zoom > 1;
    if (event.button !== 2 || !overflows) return false;
    event.preventDefault();
    panTarget = target;
    isPanning.value = true;
    lastClientX = event.clientX;
    lastClientY = event.clientY;
    if (target !== "single") activeCompareSide.value = target;
    return true;
  }

  function movePan(event: MouseEvent): void {
    if (!isPanning.value || panTarget === null) return;
    const deltaX = event.clientX - lastClientX;
    const deltaY = event.clientY - lastClientY;
    const state = stateFor(panTarget);
    state.panX += deltaX;
    state.panY += deltaY;
    if (panTarget !== "single" && compareSync.value) {
      const peer = compare[panTarget === 0 ? 1 : 0];
      peer.panX += deltaX;
      peer.panY += deltaY;
    }
    lastClientX = event.clientX;
    lastClientY = event.clientY;
  }

  function endPan(): void {
    isPanning.value = false;
    panTarget = null;
  }

  function styleFor(target: RawViewportTarget): Record<string, string> {
    const state = stateFor(target);
    return {
      transform: `translate(${state.panX}px, ${state.panY}px) scale(${state.zoom})`,
      cursor: isPanning.value && panTarget === target
        ? "grabbing"
        : state.zoom > 1
          ? "grab"
          : "default",
    };
  }

  return {
    activeCompareSide,
    compare,
    compareSync,
    endPan,
    fit,
    handleDoubleClick,
    handleResize,
    handleWheel,
    isPanning,
    movePan,
    resetCompare,
    showActualSize,
    single,
    startPan,
    styleFor,
  };
}
