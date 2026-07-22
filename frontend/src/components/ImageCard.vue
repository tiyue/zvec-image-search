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
const scoreLabel = computed(() => {
  const score = props.item.confidence ?? props.item.rawScore;
  if (score === null || score === undefined || !Number.isFinite(Number(score))) return "—";
  const normalized = Number(score) <= 1 ? Number(score) * 100 : Number(score);
  return `${Math.round(normalized)}%`;
});
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
        <span class="score-badge" :aria-label="`匹配度 ${scoreLabel}`">{{ scoreLabel }}</span>
        <span class="rank-badge card-rank" :aria-label="`排名 ${item.rank}`">{{ rankLabel }}</span>
        <span v-if="selected" class="selection-check" aria-hidden="true">✓</span>
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
  grid-template-rows: minmax(0, 1fr);
  padding: 0;
  overflow: hidden;
  border: 0;
  border-radius: 0;
  color: var(--text, #171e2e);
  text-align: left;
  background: #f2f2f2;
  cursor: pointer;
  transition:
    transform 150ms ease,
    border-color 150ms ease,
    box-shadow 150ms ease;
}

.image-card:hover { z-index: 1; filter: brightness(.97); }

.image-card.is-selected {
  outline: 2px solid #171717;
  outline-offset: -2px;
  box-shadow: none;
}

.image-card.is-primary {
  box-shadow: none;
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

.rank-badge, .score-badge {
  display: inline-flex;
  min-width: 0;
  min-height: 0;
  align-items: center;
  justify-content: center;
  padding: 0 7px;
  border-radius: 999px;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  background: rgb(23 23 23 / 72%);
}

.card-rank {
  position: absolute;
  z-index: 1;
  top: auto;
  bottom: 6px;
  left: 6px;
}
.card-rank::before { content:"#"; }

.score-badge { position:absolute; z-index:1; top:6px; right:6px; }

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

</style>
