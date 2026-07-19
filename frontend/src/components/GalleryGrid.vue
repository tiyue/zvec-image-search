<script setup lang="ts">
import ImageCard from "./ImageCard.vue";

import type {
  GalleryContextIntent,
  GallerySelectionIntent,
  SearchResultItem,
} from "../types/contracts";

const props = withDefaults(
  defineProps<{
    items: SearchResultItem[];
    selectedId?: string | null;
    selectedIds?: string[];
    loading?: boolean;
    loadingText?: string;
  }>(),
  {
    selectedId: null,
    selectedIds: () => [],
    loading: false,
    loadingText: "正在读取结果",
  },
);

const emit = defineEmits<{
  select: [intent: GallerySelectionIntent];
  open: [id: string];
  context: [intent: GalleryContextIntent];
}>();

function isSelected(id: string): boolean {
  return props.selectedIds.includes(id);
}
</script>

<template>
  <div class="gallery-frame">
    <div class="gallery-grid" role="list" :aria-busy="loading">
      <ImageCard
        v-for="(item, index) in items"
        :key="item.id"
        :item="item"
        :index="index"
        :selected="isSelected(item.id)"
        :primary="item.id === selectedId"
        @select="emit('select', $event)"
        @open="emit('open', $event)"
        @context="emit('context', $event)"
      />

      <div v-if="items.length === 0" class="state-panel" role="status" aria-live="polite">
        <span class="state-orb" aria-hidden="true">
          <svg viewBox="0 0 24 24" focusable="false">
            <circle cx="10.8" cy="10.8" r="5.8" />
            <path d="m15.2 15.2 4 4" />
          </svg>
        </span>
        <strong>暂无可显示的图片</strong>
        <p>输入内容开始搜索，或尝试调整搜索条件。</p>
      </div>
    </div>

    <!-- The previous page stays mounted below this translucent overlay. -->
    <div v-if="loading" class="gallery-loading-overlay" role="status" aria-live="polite">
      <span class="loading-spinner" aria-hidden="true" />
      <span>{{ loadingText }}</span>
    </div>
  </div>
</template>

<style scoped>
.gallery-frame {
  position: relative;
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}

.gallery-grid {
  display: grid;
  min-width: 0;
  min-height: 0;
  height: 100%;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  grid-template-rows: repeat(3, minmax(0, 1fr));
  gap: 9px;
  overflow: hidden;
  transition: opacity 100ms ease;
}

.gallery-grid[aria-busy="true"] {
  opacity: 0.72;
}

.gallery-loading-overlay {
  position: absolute;
  z-index: 4;
  inset: 0;
  display: grid;
  place-content: center;
  place-items: center;
  gap: 9px;
  color: var(--text, #171e2e);
  font-size: 13px;
  font-weight: 700;
  background: rgb(247 248 252 / 42%);
}

.loading-spinner {
  width: 25px;
  height: 25px;
  border: 3px solid rgb(89 91 220 / 18%);
  border-top-color: var(--brand, #6258e7);
  border-radius: 999px;
  animation: gallery-spin 700ms linear infinite;
}

@keyframes gallery-spin {
  to {
    transform: rotate(1turn);
  }
}

.state-panel {
  display: flex;
  min-height: 180px;
  grid-column: 1 / -1;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  color: var(--muted, #596478);
  font-size: 13px;
  text-align: center;
}

.state-panel strong {
  color: var(--text, #171e2e);
  font-size: 15px;
}

.state-panel p {
  margin: 4px 0 0;
}

.state-orb {
  display: grid;
  width: 44px;
  height: 44px;
  place-items: center;
  margin-bottom: 9px;
  border-radius: 15px;
  color: var(--brand, #6258e7);
  background: var(--brand-soft, #f0efff);
}

.state-orb svg {
  width: 21px;
  height: 21px;
  fill: none;
  stroke: currentcolor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.8;
}

@media (max-width: 1200px) {
  .gallery-frame {
    overflow: auto;
    scrollbar-gutter: stable;
  }

  .gallery-grid {
    height: auto;
    min-height: 100%;
    grid-template-columns: repeat(3, minmax(150px, 1fr));
    grid-template-rows: none;
    grid-auto-rows: 210px;
    overflow: visible;
  }

}

@media (max-width: 680px) {
  .gallery-grid {
    grid-template-columns: repeat(2, minmax(150px, 1fr));
  }
}
</style>
