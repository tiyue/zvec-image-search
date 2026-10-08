<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";

import { acquireViewerOverlay } from "../../composables/useImageViewer";
import type { OrganizeImage } from "./types";

const props = defineProps<{
  /** Selected images already materialised by the gallery's current window. */
  images: OrganizeImage[];
  selectedCount: number;
  description: string;
}>();

const emit = defineEmits<{
  close: [];
  open: [imageId: string];
}>();

const rootRef = ref<HTMLElement | null>(null);
const closeButtonRef = ref<HTMLButtonElement | null>(null);
// Below this width the pane leaves the grid and floats item on top of the
// gallery, so it becomes modal for shortcuts and outside clicks.
const floating = ref(false);
let floatingQuery: MediaQueryList | null = null;
let releaseOverlay: (() => void) | null = null;

function syncOverlay(): void {
  if (floating.value && !releaseOverlay) releaseOverlay = acquireViewerOverlay();
  else if (!floating.value && releaseOverlay) {
    releaseOverlay();
    releaseOverlay = null;
  }
}

function handleFloatingChange(event: MediaQueryListEvent): void {
  floating.value = event.matches;
  syncOverlay();
}

function dimensions(image: OrganizeImage): string {
  return image.width > 0 && image.height > 0
    ? `${image.width} × ${image.height}`
    : "尺寸未知";
}

function openViewer(): void {
  const image = props.images[0];
  if (image) emit("open", image.id);
}

function focusable(): HTMLElement[] {
  const root = rootRef.value;
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  );
}

function trapTab(event: KeyboardEvent): void {
  const elements = focusable();
  if (elements.length === 0) return;
  const first = elements[0];
  const last = elements[elements.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && Boolean(rootRef.value?.contains(active));
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
  // Only the floating pane is modal; the wide sidebar leaves background
  // shortcuts and selection alone.
  if (floating.value) {
    event.stopPropagation();
    if (event.key === "Tab") {
      trapTab(event);
      return;
    }
  }
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  }
}

onMounted(() => {
  closeButtonRef.value?.focus({ preventScroll: true });
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    floatingQuery = window.matchMedia("(max-width: 1180px)");
    floating.value = floatingQuery.matches;
    floatingQuery.addEventListener("change", handleFloatingChange);
  }
  syncOverlay();
});

onBeforeUnmount(() => {
  floatingQuery?.removeEventListener("change", handleFloatingChange);
  floatingQuery = null;
  releaseOverlay?.();
  releaseOverlay = null;
});
</script>

<template>
  <div
    v-if="floating"
    class="details-backdrop"
    role="presentation"
    @click="emit('close')"
  />
  <aside
    ref="rootRef"
    class="gallery-details"
    aria-labelledby="gallery-details-title"
    :role="floating ? 'dialog' : undefined"
    :aria-modal="floating ? true : undefined"
    @keydown="handleKeydown"
  >
    <header class="details-head">
      <h2 id="gallery-details-title">图片详情</h2>
      <button
        ref="closeButtonRef"
        class="details-close"
        type="button"
        aria-label="关闭图片详情"
        @click="emit('close')"
      >
        ×
      </button>
    </header>

    <p v-if="!selectedCount" class="details-note">未选择图片。</p>

    <template v-else-if="images.length === 1 && selectedCount === 1">
      <dl class="details-meta">
        <div>
          <dt>文件名</dt>
          <dd :title="images[0].fileName">{{ images[0].fileName }}</dd>
        </div>
        <div>
          <dt>尺寸</dt>
          <dd>{{ dimensions(images[0]) }}</dd>
        </div>
        <div v-if="images[0].relativePath">
          <dt>相对路径</dt>
          <dd :title="images[0].relativePath">{{ images[0].relativePath }}</dd>
        </div>
      </dl>
      <section class="details-tags" aria-label="标签">
        <span v-for="tag in images[0].effectiveTags" :key="tag" class="details-tag">
          {{ tag }}
        </span>
        <p v-if="!images[0].effectiveTags.length" class="details-note">无标签</p>
      </section>
      <button class="details-view" type="button" @click="openViewer">查看大图</button>
    </template>

    <template v-else>
      <p class="details-summary">
        {{ description }}<template v-if="images.length < selectedCount">（详情基于已加载的 {{ images.length }} 张）</template>
      </p>
      <ul v-if="images.length" class="details-list" aria-label="已加载的选中图片">
        <li v-for="image in images" :key="image.id">
          <strong :title="image.fileName">{{ image.fileName }}</strong>
          <small>{{ dimensions(image) }}</small>
        </li>
      </ul>
      <p v-else class="details-note">尚未加载选中图片的详情。</p>
    </template>
  </aside>
</template>

<style scoped>
.gallery-details { display: grid; min-width: 0; min-height: 0; align-content: start; gap: 12px; padding: 16px; overflow: auto; color: var(--text); background: var(--surface); }
.details-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.details-head h2 { margin: 0; font-size: 15px; font-weight: 600; }
.details-close { display: grid; width: 32px; height: 32px; place-items: center; padding: 0; border: 1px solid var(--border-strong); border-radius: 8px; color: var(--muted); background: var(--surface-soft); font-size: 17px; line-height: 1; }
.details-note { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
.details-summary { margin: 0; color: var(--text); font-size: 12px; line-height: 1.55; }
.details-meta { display: grid; gap: 0; margin: 0; }
.details-meta > div { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--border); }
.details-meta dt { color: var(--muted); font-size: 11px; }
.details-meta dd { margin: 0; overflow-wrap: anywhere; font-size: 12px; }
.details-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.details-tag { max-width: 100%; overflow: hidden; border-radius: 999px; padding: 4px 8px; color: var(--text); background: var(--surface-soft); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.details-list { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.details-list li { display: flex; min-width: 0; align-items: baseline; justify-content: space-between; gap: 8px; padding: 8px; border-radius: 8px; background: var(--surface-soft); }
.details-list strong { overflow: hidden; font-size: 11px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.details-list small { flex: 0 0 auto; color: var(--muted); font-size: 10px; }
.details-view { min-height: 32px; border: 1px solid var(--border-strong); border-radius: 8px; color: var(--text); background: var(--surface-soft); font-size: 12px; font-weight: 500; }

/* Narrow windows: the details pane leaves the grid and floats over the gallery. */
@media (max-width: 1180px) {
  .details-backdrop { position: fixed; z-index: 35; inset: 0; background: rgb(0 0 0 / 28%); }
  .gallery-details { position: fixed; z-index: 36; top: 16px; right: 16px; bottom: 16px; width: min(340px, calc(100vw - 48px)); border: 1px solid var(--border-strong); border-radius: 12px; box-shadow: var(--shadow-float); }
}
</style>
