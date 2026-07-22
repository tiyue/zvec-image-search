<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  GalleryContextIntent,
  GallerySelectionIntent,
  SearchResultItem,
} from "../types/contracts";

const props = defineProps<{
  item: SearchResultItem;
  index: number;
  selected: boolean;
  primary?: boolean;
}>();

const emit = defineEmits<{
  select: [intent: GallerySelectionIntent];
  open: [id: string];
  context: [intent: GalleryContextIntent];
}>();

const imageFailed = ref(false);

const imageSource = computed(() => props.item.thumbnailUrl || props.item.imageUrl);
const loadingMode = computed(() => (props.index < 5 ? "eager" : "lazy"));
const fetchPriority = computed(() => (props.index < 3 ? "high" : "low"));
const rankLabel = computed(() => String(props.item.rank || props.index + 1).padStart(2, "0"));
const matchClass = computed(() => ({
  "is-high": props.item.matchState === "high",
  "is-weak": props.item.matchState === "weak",
}));

watch(imageSource, () => {
  imageFailed.value = false;
});

function selectImage(event: MouseEvent): void {
  emit("select", {
    id: props.item.id,
    index: props.index,
    additive: event.ctrlKey || event.metaKey,
    range: event.shiftKey,
  });
}

function openImage(): void {
  emit("open", props.item.id);
}

function handleImageError(): void {
  imageFailed.value = true;
}

function openContextMenu(event: MouseEvent): void {
  emit("context", {
    id: props.item.id,
    index: props.index,
    clientX: event.clientX,
    clientY: event.clientY,
  });
}
</script>

<template>
  <article class="image-card-item" role="listitem">
    <button
      class="image-card"
      :class="{ 'is-selected': selected, 'is-primary': primary }"
      type="button"
      :aria-label="`选择 ${item.name}`"
      :aria-pressed="selected"
      :data-image-id="item.id"
      @click="selectImage"
      @contextmenu.stop.prevent="openContextMenu"
    >
      <span class="thumbnail-stage" @dblclick.stop.prevent="openImage">
        <img
          v-if="imageSource && !imageFailed"
          class="thumbnail-image media-contain"
          :src="imageSource"
          :alt="item.name"
          :loading="loadingMode"
          decoding="async"
          :fetchpriority="fetchPriority"
          draggable="false"
          @error="handleImageError"
        />
        <span
          v-else
          class="thumbnail-fallback"
          data-testid="thumbnail-fallback"
          role="img"
          :aria-label="`${item.name} 缩略图不可用`"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <rect x="4" y="5" width="16" height="14" rx="3" />
            <path d="m7.5 15 3.2-3.4 2.4 2.3 1.8-1.8 2.6 2.9" />
            <circle cx="15.8" cy="9.2" r="1.35" />
          </svg>
        </span>
        <span class="rank-badge card-rank" :aria-label="`排名 ${item.rank}`">{{ rankLabel }}</span>
        <span v-if="selected" class="selection-check" aria-hidden="true">✓</span>
        <span class="match-stripe" :class="matchClass" aria-hidden="true" />
      </span>
      <span class="image-card-copy">
        <strong :title="item.name">{{ item.name }}</strong>
        <small :title="item.libraryName">{{ item.libraryName }}</small>
      </span>
    </button>
  </article>
</template>

<style scoped>
.image-card-item {
  min-width: 0;
  min-height: 0;
}

.image-card {
  position: relative;
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-rows: minmax(0, 1fr) auto;
  padding: 0;
  overflow: hidden;
  border: 1px solid #e7e7e7;
  border-radius: 8px;
  color: var(--text, #171e2e);
  text-align: left;
  background: #f2f2f2;
  cursor: pointer;
  transition:
    transform 150ms ease,
    border-color 150ms ease,
    box-shadow 150ms ease;
}

.image-card:hover {
  z-index: 1;
  border-color: #bdbdbd;
  box-shadow: 0 6px 18px rgb(0 0 0 / 10%);
  transform: none;
}

.image-card.is-selected {
  border-color: #777;
  box-shadow: 0 0 0 2px rgb(0 0 0 / 10%);
}

.image-card.is-primary {
  box-shadow: 0 0 0 2px rgb(0 0 0 / 14%);
}

.thumbnail-stage {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 0;
  place-items: center;
  overflow: hidden;
  background:linear-gradient(145deg,#f0f0f0,#e4e4e4);
}

/* Product invariant: user images must never be cropped. */
.media-contain {
  display: block;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain !important;
  object-position: center center !important;
}

.thumbnail-fallback {
  display: grid;
  width: 100%;
  height: 100%;
  place-items: center;
  color: #7f8ba1;
}

.thumbnail-fallback svg {
  width: 28px;
  height: 28px;
  fill: none;
  stroke: currentcolor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.7;
}

.rank-badge {
  display: inline-flex;
  min-width: 27px;
  min-height: 27px;
  align-items: center;
  justify-content: center;
  padding: 0 7px;
  border-radius: 999px;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  background: #666;
}

.card-rank {
  position: absolute;
  z-index: 1;
  top: 6px;
  left: 6px;
}

.selection-check {
  position: absolute;
  z-index: 2;
  top: 7px;
  right: 8px;
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border: 2px solid rgb(255 255 255 / 88%);
  border-radius: 999px;
  color: #fff;
  font-size: 14px;
  font-weight: 900;
  background: #555;
  box-shadow: 0 3px 10px rgb(0 0 0 / 18%);
}

.match-stripe {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 3px;
  background: #777;
}

.match-stripe.is-high {
  background: var(--success, #0e735f);
}

.match-stripe.is-weak {
  background: var(--warning, #92500b);
}

.image-card-copy {
  display: grid;
  gap: 2px;
  padding: 7px 8px 8px;
  border-top: 1px solid var(--border, #e1e6ef);
  background: var(--surface, #fff);
}

.image-card-copy strong,
.image-card-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.image-card-copy strong {
  font-size: 13px;
}

.image-card-copy small {
  color: var(--muted, #596478);
  font-size: 12px;
}
</style>
