<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import type { ViewerImage } from "../composables/useImageViewer";
import { acquireViewerOverlay, useViewerGeometry } from "../composables/useImageViewer";

const props = withDefaults(defineProps<{
  image: ViewerImage | null;
  canPrevious: boolean;
  canNext: boolean;
  loading?: boolean;
  error?: string;
  positionText?: string;
  canReveal?: boolean;
  preloadUrls?: string[];
}>(), {
  loading: false,
  error: "",
  positionText: "",
  canReveal: false,
  preloadUrls: () => [],
});

const emit = defineEmits<{
  close: [];
  previous: [];
  next: [];
  retry: [];
  openNative: [];
  reveal: [];
}>();

const {
  stageRef,
  isFit,
  dragging,
  zoomLabel,
  canvasStyle,
  setNaturalSize,
  clearNaturalSize,
  resetToFit,
  setFit,
  setActual,
  toggleFit,
  handleWheel,
  startPan,
  movePan,
  endPan,
  remeasure,
} = useViewerGeometry();

const dialogRef = ref<HTMLElement | null>(null);
const closeButtonRef = ref<HTMLButtonElement | null>(null);
const fullImageRef = ref<HTMLImageElement | null>(null);
const sidebarCollapsed = ref(false);
const imageLoaded = ref(false);
const imageFailed = ref(false);
const preloads: HTMLImageElement[] = [];
let resizeObserver: ResizeObserver | null = null;
let releaseOverlay: (() => void) | null = null;

const thumbnailUrl = computed(() => props.image?.thumbnailUrl || props.image?.imageUrl || "");
const fullUrl = computed(() => props.image?.imageUrl || props.image?.thumbnailUrl || "");
// A separate low-resolution preview lets the thumbnail cover the stage while
// the full image decodes; the full layer stays hidden until its own `load`.
const hasSeparatePreview = computed(() =>
  Boolean(thumbnailUrl.value && fullUrl.value && thumbnailUrl.value !== fullUrl.value),
);
const showPlaceholder = computed(() => hasSeparatePreview.value && !imageLoaded.value);
const showFullImage = computed(() => !hasSeparatePreview.value || imageLoaded.value);
const dimensions = computed(() =>
  props.image && props.image.width > 0 && props.image.height > 0
    ? `${props.image.width} × ${props.image.height}`
    : "—",
);

/**
 * Switching images or URLs must not keep the previous pixels or natural size:
 * the layer is keyed by its URL so Vue replaces the element, and the geometry
 * only reuses declared dimensions when the new image actually has them.
 */
watch(
  () => `${props.image?.id ?? ""}\u0000${fullUrl.value}`,
  () => {
    imageLoaded.value = false;
    imageFailed.value = false;
    const image = props.image;
    if (image && image.width > 0 && image.height > 0) setNaturalSize(image.width, image.height);
    else clearNaturalSize();
    resetToFit();
  },
  { immediate: true },
);

// The overlay holds background shortcuts for every source it renders for.
watch(
  () => props.image,
  (value) => syncOverlay(Boolean(value)),
  { immediate: true },
);

function syncOverlay(open: boolean): void {
  if (open && !releaseOverlay) {
    releaseOverlay = acquireViewerOverlay();
  } else if (!open && releaseOverlay) {
    releaseOverlay();
    releaseOverlay = null;
  }
}

// Only the immediately adjacent known images are warmed; the references are
// dropped whenever the current image (or the viewer) changes.
watch(
  () => props.preloadUrls.join("\n"),
  () => {
    clearPreloads();
    for (const url of props.preloadUrls.slice(0, 2)) {
      if (!url) continue;
      const preload = new Image();
      preload.decoding = "async";
      preload.fetchPriority = "low";
      preload.src = prefetchUrl(url);
      preloads.push(preload);
    }
  },
  { immediate: true },
);

/**
 * Only this application's generic image endpoint may carry the prefetch hint.
 * Relative URLs are resolved against the document base so a deployment base
 * path or the runtime token prefix (`/<token>/api/image/{id}`) still matches,
 * while blob, external, raw-selection and already-prioritised URLs stay as-is.
 */
function prefetchUrl(url: string): string {
  let resolved: URL;
  try {
    resolved = new URL(url, document.baseURI);
  } catch {
    return url;
  }
  if (resolved.origin !== window.location.origin) return url;
  const base = new URL(document.baseURI);
  const basePath = base.pathname.endsWith("/")
    ? base.pathname
    : base.pathname.slice(0, base.pathname.lastIndexOf("/") + 1);
  if (!resolved.pathname.startsWith(basePath)) return url;
  const segments = resolved.pathname.slice(basePath.length).split("/");
  if (segments.length !== 3 || segments[0] !== "api" || segments[1] !== "image" || !segments[2]) {
    return url;
  }
  if (resolved.searchParams.has("priority")) return url;
  resolved.searchParams.set("priority", "prefetch");
  return `${resolved.pathname}${resolved.search}${resolved.hash}`;
}

function clearPreloads(): void {
  preloads.splice(0).forEach((preload) => {
    preload.src = "";
  });
}

function handleImageLoad(event: Event): void {
  const target = event.target;
  if (!(target instanceof HTMLImageElement) || target !== fullImageRef.value) return;
  imageLoaded.value = true;
  imageFailed.value = false;
  setNaturalSize(target.naturalWidth, target.naturalHeight);
  if (isFit.value) resetToFit();
}

function handleImageError(event: Event): void {
  const target = event.target;
  if (!(target instanceof HTMLImageElement) || target !== fullImageRef.value) return;
  imageFailed.value = true;
}

function isTextEntry(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    (target instanceof HTMLElement && target.isContentEditable)
  );
}

function focusableElements(): HTMLElement[] {
  const root = dialogRef.value;
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(isDisplayed);
}

/** Tab must skip controls hidden by `v-show` (for example a collapsed sidebar). */
function isDisplayed(element: HTMLElement): boolean {
  const root = dialogRef.value;
  let node: HTMLElement | null = element;
  while (node) {
    if (node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (node === root) break;
    node = node.parentElement;
  }
  return true;
}

function trapTab(event: KeyboardEvent): void {
  const elements = focusableElements();
  if (elements.length === 0) return;
  const first = elements[0];
  const last = elements[elements.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && Boolean(dialogRef.value?.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
  } else if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

function handleKeydown(event: KeyboardEvent): void {
  // A dialog without a materialised image must not react to keys.
  if (!props.image) return;
  if (isTextEntry(event.target)) return;
  if (event.key === "Tab") {
    trapTab(event);
  } else if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    emit("previous");
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    emit("next");
  }
}

onMounted(() => {
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("resize", remeasure);
  closeButtonRef.value?.focus();
  remeasure();
  if (typeof ResizeObserver !== "undefined" && stageRef.value) {
    resizeObserver = new ResizeObserver(() => remeasure());
    resizeObserver.observe(stageRef.value);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleKeydown);
  window.removeEventListener("resize", remeasure);
  resizeObserver?.disconnect();
  resizeObserver = null;
  syncOverlay(false);
  clearPreloads();
});
</script>

<template>
  <Teleport to="body">
    <div v-if="image" class="image-viewer-backdrop" role="presentation" @click.self="emit('close')">
      <section
        ref="dialogRef"
        class="image-viewer"
        role="dialog"
        aria-modal="true"
        aria-label="图片查看器"
        data-testid="image-viewer"
      >
        <header class="viewer-head">
          <button
            ref="closeButtonRef"
            class="viewer-icon-button"
            type="button"
            aria-label="关闭查看器"
            data-testid="viewer-close"
            @click="emit('close')"
          >
            ×
          </button>
          <div class="viewer-heading">
            <strong :title="image.name">{{ image.name }}</strong>
            <small v-if="positionText">{{ positionText }}</small>
          </div>
          <div class="viewer-tools">
            <button
              class="viewer-tool"
              type="button"
              :aria-pressed="isFit"
              data-testid="viewer-fit"
              @click="setFit()"
            >
              适合窗口
            </button>
            <button
              class="viewer-tool"
              type="button"
              :aria-pressed="!isFit"
              data-testid="viewer-actual"
              @click="setActual()"
            >
              100%
            </button>
            <span class="viewer-zoom" data-testid="viewer-zoom">{{ zoomLabel }}</span>
            <button
              class="viewer-tool"
              type="button"
              :aria-pressed="!sidebarCollapsed"
              @click="sidebarCollapsed = !sidebarCollapsed"
            >
              {{ sidebarCollapsed ? "显示信息" : "隐藏信息" }}
            </button>
          </div>
        </header>

        <div
          ref="stageRef"
          class="viewer-stage"
          :class="{ 'is-dragging': dragging }"
          data-testid="viewer-stage"
          @wheel.prevent="handleWheel"
          @pointerdown="startPan"
          @pointermove="movePan"
          @pointerup="endPan"
          @pointercancel="endPan"
          @dblclick.prevent="toggleFit()"
        >
          <div class="viewer-canvas" :style="canvasStyle">
            <img
              v-if="showPlaceholder"
              :key="`preview-${thumbnailUrl}`"
              class="viewer-layer"
              :src="thumbnailUrl"
              alt=""
              aria-hidden="true"
              draggable="false"
            />
            <img
              ref="fullImageRef"
              :key="`full-${fullUrl}`"
              v-show="showFullImage"
              class="viewer-layer"
              data-testid="viewer-image"
              :src="fullUrl"
              :alt="image.name"
              draggable="false"
              @load="handleImageLoad"
              @error="handleImageError"
            />
          </div>

          <div v-if="loading" class="viewer-overlay" role="status" aria-live="polite">
            <span class="viewer-spinner" aria-hidden="true" />
            <span>正在读取图片…</span>
          </div>
          <div v-else-if="error" class="viewer-overlay is-error" role="alert">
            <span>{{ error }}</span>
            <button class="viewer-retry" type="button" @click="emit('retry')">重试</button>
          </div>
          <p v-else-if="imageFailed && !showPlaceholder" class="viewer-notice" role="alert">
            图片加载失败
          </p>
          <p v-else-if="imageFailed" class="viewer-notice" role="status">
            高清图片加载失败，正在使用缩略图
          </p>

          <button
            class="viewer-nav is-previous"
            type="button"
            aria-label="上一张"
            data-testid="viewer-previous"
            :disabled="!canPrevious || loading"
            @click="emit('previous')"
          >
            ‹
          </button>
          <button
            class="viewer-nav is-next"
            type="button"
            aria-label="下一张"
            data-testid="viewer-next"
            :disabled="!canNext || loading"
            @click="emit('next')"
          >
            ›
          </button>
        </div>

        <aside v-show="!sidebarCollapsed" class="viewer-info" aria-label="图片信息">
          <dl class="viewer-meta">
            <div>
              <dt>文件名</dt>
              <dd :title="image.name">{{ image.name }}</dd>
            </div>
            <div>
              <dt>图片尺寸</dt>
              <dd>{{ dimensions }}</dd>
            </div>
            <div v-if="image.libraryName">
              <dt>图库</dt>
              <dd :title="image.libraryName">{{ image.libraryName }}</dd>
            </div>
            <div v-if="image.relativePath">
              <dt>相对路径</dt>
              <dd :title="image.relativePath">{{ image.relativePath }}</dd>
            </div>
          </dl>
          <section class="viewer-tags" aria-label="标签">
            <template v-if="image.tags.length">
              <span v-for="tag in image.tags" :key="tag" class="viewer-tag">{{ tag }}</span>
            </template>
            <p v-else class="viewer-empty-tag">无标签</p>
          </section>
          <div class="viewer-actions">
            <button class="viewer-action is-secondary" type="button" @click="emit('openNative')">
              用系统程序打开
            </button>
            <button
              v-if="canReveal"
              class="viewer-action is-quiet"
              type="button"
              @click="emit('reveal')"
            >
              所在文件夹
            </button>
          </div>
        </aside>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.image-viewer-backdrop {
  position: fixed;
  z-index: 1500;
  inset: 0;
  display: grid;
  background: rgb(12 14 18 / 78%);
}

.image-viewer {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-rows: auto minmax(0, 1fr);
  color: var(--text, #0d0d0d);
  background: var(--surface, #fff);
}

.viewer-head {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
  grid-column: 1 / -1;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border, rgb(0 0 0 / 8%));
}

.viewer-heading {
  display: grid;
  min-width: 0;
  gap: 1px;
}

.viewer-heading strong {
  overflow: hidden;
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.viewer-heading small {
  color: var(--muted, #6f6f6f);
  font-size: 12px;
}

.viewer-tools {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.viewer-icon-button,
.viewer-tool,
.viewer-retry,
.viewer-action {
  border: 1px solid var(--border, rgb(0 0 0 / 8%));
  border-radius: 9px;
  color: inherit;
  font: inherit;
  font-size: 13px;
  background: var(--surface-soft, #f7f7f7);
  cursor: pointer;
}

.viewer-icon-button {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  padding: 0;
  font-size: 19px;
  line-height: 1;
}

.viewer-tool {
  min-height: 32px;
  padding: 0 10px;
}

.viewer-tool[aria-pressed="true"] {
  border-color: var(--border-strong, rgb(0 0 0 / 14%));
  background: var(--surface-strong, #ececec);
  font-weight: 700;
}

.viewer-zoom {
  min-width: 46px;
  color: var(--muted, #6f6f6f);
  font-size: 12px;
  text-align: center;
}

.viewer-stage {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
  background: var(--surface-soft, #f7f7f7);
}

.viewer-stage.is-dragging {
  cursor: grabbing;
}

.viewer-canvas {
  position: absolute;
  top: 50%;
  left: 50%;
  transform-origin: center center;
  will-change: transform;
}

.viewer-layer {
  position: absolute;
  inset: 0;
  display: block;
  width: 100%;
  height: 100%;
  object-fit: fill;
  user-select: none;
  -webkit-user-drag: none;
}

.viewer-overlay,
.viewer-notice {
  position: absolute;
  left: 50%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  border-radius: 10px;
  color: var(--text, #0d0d0d);
  font-size: 13px;
  background: var(--surface, #fff);
  box-shadow: var(--shadow-float, 0 10px 30px rgb(0 0 0 / 11%));
  transform: translateX(-50%);
}

.viewer-overlay {
  top: 16px;
}

.viewer-notice {
  bottom: 16px;
  margin: 0;
}

.viewer-overlay.is-error {
  bottom: 16px;
  top: auto;
}

.viewer-spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgb(0 0 0 / 14%);
  border-top-color: currentcolor;
  border-radius: 999px;
  animation: viewer-spin 700ms linear infinite;
}

@keyframes viewer-spin {
  to {
    transform: rotate(1turn);
  }
}

.viewer-nav {
  position: absolute;
  top: 50%;
  display: grid;
  width: 40px;
  height: 56px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--border, rgb(0 0 0 / 8%));
  border-radius: 12px;
  color: inherit;
  font-size: 22px;
  line-height: 1;
  background: var(--surface, #fff);
  box-shadow: var(--shadow-float, 0 10px 30px rgb(0 0 0 / 11%));
  cursor: pointer;
  transform: translateY(-50%);
}

.viewer-nav.is-previous {
  left: 12px;
}

.viewer-nav.is-next {
  right: 12px;
}

.viewer-nav:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}

.viewer-info {
  display: grid;
  width: 300px;
  min-width: 0;
  align-content: start;
  gap: 14px;
  padding: 14px;
  overflow: auto;
  border-left: 1px solid var(--border, rgb(0 0 0 / 8%));
  background: var(--surface, #fff);
}

.viewer-meta {
  display: grid;
  gap: 0;
  margin: 0;
}

.viewer-meta > div {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  gap: 8px;
  padding: 7px 0;
  border-bottom: 1px solid var(--border, rgb(0 0 0 / 8%));
}

.viewer-meta dt {
  color: var(--muted, #6f6f6f);
  font-size: 12px;
}

.viewer-meta dd {
  margin: 0;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.viewer-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.viewer-tag {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 999px;
  color: var(--text, #0d0d0d);
  font-size: 12px;
  background: var(--surface-strong, #ececec);
}

.viewer-empty-tag {
  margin: 0;
  color: var(--muted, #6f6f6f);
  font-size: 12px;
}

.viewer-actions {
  display: grid;
  gap: 8px;
}

.viewer-action {
  min-height: 38px;
  padding: 8px 12px;
  font-weight: 700;
}

.viewer-action.is-secondary {
  border-color: var(--border-strong, rgb(0 0 0 / 14%));
  background: var(--surface-strong, #ececec);
}

.viewer-action.is-quiet {
  background: var(--surface, #fff);
}

@media (max-width: 760px) {
  .image-viewer {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr) auto;
  }

  .viewer-info {
    width: auto;
    max-height: 42vh;
    grid-row: 3;
    border-top: 1px solid var(--border, rgb(0 0 0 / 8%));
    border-left: 0;
  }
}

@media (prefers-color-scheme: dark) {
  .image-viewer-backdrop {
    background: rgb(0 0 0 / 82%);
  }

  .image-viewer,
  .viewer-info {
    --surface: #262626;
    --surface-soft: #1f1f1f;
    --surface-strong: #303030;
    --text: #ededed;
    --muted: #b5b5b5;
    --border: rgb(255 255 255 / 13%);
    --border-strong: rgb(255 255 255 / 22%);
    color-scheme: dark;
    color: #ededed;
    background: #262626;
  }

  .viewer-stage {
    background: #1b1b1b;
  }
}

@media (prefers-reduced-motion: reduce) {
  .viewer-spinner {
    animation: none;
  }
}
</style>
