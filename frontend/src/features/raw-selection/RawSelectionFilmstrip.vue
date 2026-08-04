<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import type { RawMember } from "./types";

interface FilmstripRange {
  start: number;
  end: number;
  visibleStart: number;
  visibleEnd: number;
}

const props = defineProps<{
  members: RawMember[];
  currentIndex: number;
  selectedIds: Set<string>;
  initialScroll: number;
}>();

const emit = defineEmits<{
  activate: [index: number];
  selection: [ids: Set<string>];
  scroll: [left: number];
  range: [range: FilmstripRange];
}>();

const FILM_ITEM_WIDTH = 86;
const OVERSCAN_SCREENS = 2;
const strip = ref<HTMLElement | null>(null);
const scrollLeft = ref(0);
const viewportWidth = ref(800);
const failedKeys = ref<Set<string>>(new Set());
let selectionAnchor: number | null = null;
let resizeObserver: ResizeObserver | null = null;

const range = computed<FilmstripRange>(() => {
  const total = props.members.length;
  if (total === 0) return { start: 0, end: 0, visibleStart: 0, visibleEnd: 0 };
  const screenItems = Math.max(1, Math.ceil(viewportWidth.value / FILM_ITEM_WIDTH));
  const visibleStart = Math.max(0, Math.floor(scrollLeft.value / FILM_ITEM_WIDTH));
  const visibleEnd = Math.min(total, visibleStart + screenItems);
  const overscan = screenItems * OVERSCAN_SCREENS;
  return {
    start: Math.max(0, visibleStart - overscan),
    end: Math.min(total, visibleEnd + overscan),
    visibleStart,
    visibleEnd,
  };
});

const visibleItems = computed(() => props.members
  .slice(range.value.start, range.value.end)
  .map((member, offset) => ({ member, index: range.value.start + offset })));

const innerWidth = computed(() => props.members.length * FILM_ITEM_WIDTH);

function sourceVersion(member: RawMember): string {
  return `${member.file_size}-${member.mtime_ns}-${member.file_identity ?? ""}`;
}

function thumbnailUrl(member: RawMember, index: number): string {
  const priority = index >= range.value.visibleStart && index < range.value.visibleEnd
    ? "visible"
    : "overscan";
  const query = new URLSearchParams({ priority, v: sourceVersion(member) });
  return `api/raw-selection/members/${member.id}/thumbnail?${query}`;
}

function failureKey(member: RawMember): string {
  return `${member.id}:${sourceVersion(member)}`;
}

function markFailed(member: RawMember): void {
  failedKeys.value = new Set(failedKeys.value).add(failureKey(member));
}

function handleClick(index: number, event: MouseEvent): void {
  emit("activate", index);
  const member = props.members[index];
  if (!member) return;
  if (event.shiftKey) {
    const anchor = selectionAnchor ?? props.currentIndex;
    const next = event.ctrlKey || event.metaKey
      ? new Set(props.selectedIds)
      : new Set<string>();
    const start = Math.min(anchor, index);
    const end = Math.max(anchor, index);
    for (let cursor = start; cursor <= end; cursor += 1) {
      const candidate = props.members[cursor];
      if (candidate) next.add(candidate.id);
    }
    emit("selection", next);
    return;
  }
  selectionAnchor = index;
  if (!event.ctrlKey && !event.metaKey) return;
  const next = new Set(props.selectedIds);
  if (next.has(member.id)) next.delete(member.id);
  else next.add(member.id);
  emit("selection", next);
}

function measure(): void {
  if (!strip.value) return;
  viewportWidth.value = Math.max(1, strip.value.clientWidth);
}

function handleScroll(): void {
  if (!strip.value) return;
  scrollLeft.value = strip.value.scrollLeft;
  emit("scroll", scrollLeft.value);
}

function setScrollLeft(left: number): void {
  if (!strip.value) return;
  strip.value.scrollLeft = Math.max(0, left);
  scrollLeft.value = strip.value.scrollLeft;
  emit("scroll", scrollLeft.value);
}

function scrollToActive(): void {
  const element = strip.value;
  if (!element || props.currentIndex < 0) return;
  const left = props.currentIndex * FILM_ITEM_WIDTH;
  const right = left + FILM_ITEM_WIDTH;
  if (left < element.scrollLeft || right > element.scrollLeft + element.clientWidth) {
    setScrollLeft(left - element.clientWidth / 2 + FILM_ITEM_WIDTH / 2);
  }
}

function scrollByWheel(delta: number): void {
  if (!strip.value) return;
  setScrollLeft(strip.value.scrollLeft + delta);
}

watch(range, (value) => emit("range", value), { immediate: true });
watch(() => props.currentIndex, () => void nextTick(scrollToActive));
watch(() => props.initialScroll, (left) => void nextTick(() => setScrollLeft(left)));
watch(() => props.members, () => {
  selectionAnchor = null;
  void nextTick(measure);
});

onMounted(() => {
  measure();
  setScrollLeft(props.initialScroll);
  if (typeof ResizeObserver !== "undefined" && strip.value) {
    resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(strip.value);
  }
  window.addEventListener("resize", measure);
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  resizeObserver = null;
  window.removeEventListener("resize", measure);
});

defineExpose({ scrollByWheel, scrollToActive, setScrollLeft });
</script>

<template>
  <div ref="strip" class="rs-film-strip" aria-label="项目胶片栏" @scroll="handleScroll">
    <div class="rs-film-strip-inner" :style="{ width: `${innerWidth}px` }">
      <button
        v-for="{ member, index } in visibleItems"
        :key="member.id"
        type="button"
        class="rs-thumb"
        :style="{ left: `${index * FILM_ITEM_WIDTH}px` }"
        :class="{
          'is-current': index === currentIndex,
          'is-selected': selectedIds.has(member.id),
        }"
        :aria-current="index === currentIndex ? 'true' : undefined"
        :aria-pressed="selectedIds.has(member.id)"
        :aria-label="`${member.file_name}${index === currentIndex ? '，当前图片' : ''}${selectedIds.has(member.id) ? '，已明确选择' : ''}`"
        @click="handleClick(index, $event)"
      >
        <img
          v-if="!failedKeys.has(failureKey(member))"
          :src="thumbnailUrl(member, index)"
          :alt="member.file_name"
          class="rs-thumb-img"
          loading="lazy"
          draggable="false"
          @error="markFailed(member)"
        />
        <span v-else class="rs-thumb-error" aria-hidden="true">无法预览</span>
        <span class="rs-thumb-name">{{ member.file_name }}</span>
        <span v-if="member.star_rating > 0" class="rs-thumb-stars" aria-hidden="true">{{ '★'.repeat(member.star_rating) }}</span>
        <span v-if="selectedIds.has(member.id)" class="rs-thumb-selection" aria-hidden="true">✓</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.rs-film-strip {
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
  border-top: 1px solid var(--border);
  background: var(--surface);
  scrollbar-width: thin;
}

.rs-film-strip-inner {
  position: relative;
  height: 100%;
}

.rs-thumb {
  position: absolute;
  top: 6px;
  bottom: 6px;
  display: flex;
  width: 84px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  overflow: hidden;
  padding: 4px 6px;
  border: 2px solid transparent;
  border-radius: 6px;
  color: var(--muted);
  font-size: 11px;
  background: var(--surface-soft);
}

.rs-thumb.is-current {
  border-color: var(--brand);
  color: var(--text);
  background: var(--surface);
  box-shadow: inset 0 -3px 0 var(--brand);
}

.rs-thumb.is-selected::before {
  position: absolute;
  inset: 3px;
  border: 1px dashed var(--success);
  border-radius: 4px;
  content: "";
  pointer-events: none;
}

.rs-thumb-img,
.rs-thumb-error {
  display: block;
  width: 100%;
  height: 58px;
  border-radius: 4px;
}

.rs-thumb-img { object-fit: cover; }

.rs-thumb-error {
  display: grid;
  place-items: center;
  color: var(--faint);
  font-size: 10px;
  background: var(--surface-strong);
}

.rs-thumb-name {
  max-width: 72px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-thumb-stars {
  color: #b77d00;
  font-size: 10px;
}

.rs-thumb-selection {
  position: absolute;
  top: 5px;
  right: 5px;
  display: grid;
  width: 16px;
  height: 16px;
  place-items: center;
  border-radius: 4px;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  background: var(--success);
}
</style>
