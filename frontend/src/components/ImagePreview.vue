<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { SearchResultItem } from "../types/contracts";

const props = defineProps<{
  item: SearchResultItem | null;
  highResolution: boolean;
  previewVisible: boolean;
}>();

const emit = defineEmits<{
  open: [id: string];
  reveal: [id: string];
}>();

const imageFailed = ref(false);

const imageSource = computed(() => {
  if (!props.previewVisible || !props.item) {
    return "";
  }
  return props.highResolution
    ? props.item.imageUrl || props.item.thumbnailUrl
    : props.item.thumbnailUrl || props.item.imageUrl;
});

const uniqueMatchedTags = computed(() => uniqueTags(props.item?.matchedTags));
const uniqueTagsList = computed(() => uniqueTags(props.item?.tags));

watch(imageSource, () => {
  imageFailed.value = false;
});

function uniqueTags(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map((value) => value.trim()).filter(Boolean))];
}

function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(4).replace(/0+$/u, "").replace(/\.$/u, "");
}

function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  const percentage = value >= 0 && value <= 1 ? value * 100 : value;
  return `${Math.max(0, percentage).toFixed(1).replace(/\.0$/u, "")}%`;
}

function formatFileSize(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) {
    return "—";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const digits = amount >= 100 || unitIndex === 0 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unitIndex]}`;
}

function matchSourceLabel(value: string | undefined): string {
  const normalized = value?.trim().toLowerCase() ?? "";
  const labels: Record<string, string> = {
    image: "图片",
    text: "文字",
    tags: "标签",
    tag: "标签",
    fused: "联合",
    combined: "联合",
    mix: "联合",
  };
  return labels[normalized] ?? value?.trim() ?? "—";
}

function matchStateLabel(value: string | undefined): string {
  const labels: Record<string, string> = {
    high: "高度相关",
    possible: "可能相关",
    weak: "低置信度",
  };
  const normalized = value?.trim().toLowerCase() ?? "";
  return labels[normalized] ?? value?.trim() ?? "—";
}

function formatDimensions(item: SearchResultItem | null): string {
  return item?.width && item.height ? `${item.width} × ${item.height}` : "—";
}

function openImage(): void {
  if (props.item) {
    emit("open", props.item.id);
  }
}

function revealImage(): void {
  if (props.item) {
    emit("reveal", props.item.id);
  }
}
</script>

<template>
  <aside class="image-preview" aria-label="标签与图片详情">
    <header class="preview-heading">
      <div>
        <p class="eyebrow">当前选择</p>
        <h2>标签与详细信息</h2>
      </div>
      <span class="rank-badge">{{ item ? String(item.rank).padStart(2, "0") : "—" }}</span>
    </header>

    <div class="preview-overview" :class="{ 'is-empty': !item }">
      <div class="preview-stage preview-stage-compact">
        <img
          v-if="imageSource && !imageFailed"
          class="preview-image media-contain"
          :src="imageSource"
          :alt="item?.name ?? ''"
          loading="eager"
          decoding="async"
          :fetchpriority="highResolution ? 'high' : 'auto'"
          draggable="false"
          @error="imageFailed = true"
        />
        <div v-else class="preview-placeholder" data-testid="preview-placeholder">
          <span class="preview-placeholder-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
              <rect x="4" y="5" width="16" height="14" rx="3" />
              <path d="m7.5 15 3.2-3.4 2.4 2.3 1.8-1.8 2.6 2.9" />
              <circle cx="15.8" cy="9.2" r="1.35" />
            </svg>
          </span>
          <p v-if="!item">选择一张图片查看标签与详情</p>
          <p v-else-if="!previewVisible">当前窗口不加载图片预览</p>
          <p v-else>图片预览不可用</p>
        </div>
      </div>
      <div class="preview-copy">
        <h3 :title="item?.name">{{ item?.name ?? "尚未选择图片" }}</h3>
        <p :title="item?.relativePath">{{ item?.relativePath || "—" }}</p>
        <div v-if="item" class="selection-highlights" aria-label="匹配摘要">
          <span>{{ matchStateLabel(item.matchState) }}</span>
          <span>{{ matchSourceLabel(item.rankSource) }}命中</span>
        </div>
      </div>
    </div>

    <div class="preview-details-scroll">
      <template v-if="item">
        <section class="preview-tag-group" aria-labelledby="matched-tags-title">
          <h3 id="matched-tags-title">命中标签</h3>
          <div class="tag-row">
            <span v-for="tag in uniqueMatchedTags" :key="tag" class="tag tag-match">{{ tag }}</span>
            <span v-if="uniqueMatchedTags.length === 0" class="empty-inline">—</span>
          </div>
        </section>

        <section class="preview-tag-group" aria-labelledby="all-tags-title">
          <h3 id="all-tags-title">全部标签</h3>
          <div class="tag-row">
            <span v-for="tag in uniqueTagsList" :key="tag" class="tag">{{ tag }}</span>
            <span v-if="uniqueTagsList.length === 0" class="empty-inline">—</span>
          </div>
        </section>

        <dl class="metadata-grid">
          <div>
            <dt>图库</dt>
            <dd :title="item.libraryName">{{ item.libraryName || "—" }}</dd>
          </div>
          <div>
            <dt>匹配来源</dt>
            <dd>{{ matchSourceLabel(item.rankSource) }}</dd>
          </div>
          <div>
            <dt>相关程度</dt>
            <dd>{{ matchStateLabel(item.matchState) }}</dd>
          </div>
          <div>
            <dt>置信度</dt>
            <dd>{{ formatConfidence(item.confidence) }}</dd>
          </div>
          <div>
            <dt>原始分数</dt>
            <dd>{{ formatScore(item.rawScore) }}</dd>
          </div>
          <div>
            <dt>图片尺寸</dt>
            <dd>{{ formatDimensions(item) }}</dd>
          </div>
          <div>
            <dt>文件大小</dt>
            <dd>{{ formatFileSize(item.sizeBytes) }}</dd>
          </div>
          <div class="metadata-wide">
            <dt>相对路径</dt>
            <dd :title="item.relativePath">{{ item.relativePath || "—" }}</dd>
          </div>
        </dl>
      </template>
    </div>

    <div class="preview-actions">
      <button class="button button-secondary" type="button" :disabled="!item" @click="openImage">
        系统打开
      </button>
      <button class="button button-quiet" type="button" :disabled="!item" @click="revealImage">
        所在文件夹
      </button>
    </div>
  </aside>
</template>

<style scoped>
.image-preview {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  overflow: hidden;
}

.preview-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 11px;
}

.preview-heading h2,
.preview-copy h3 {
  margin: 0;
  color: var(--text, #171e2e);
}

.preview-heading h2 {
  font-size: 15px;
  line-height: 1.3;
}

.eyebrow {
  margin: 0 0 2px;
  color: var(--brand, #6258e7);
  font-size: 12px;
  font-weight: 750;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}

.rank-badge,
.tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  white-space: nowrap;
}

.rank-badge {
  min-width: 27px;
  min-height: 27px;
  padding: 0 7px;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  background: #0d1322;
}

.preview-overview {
  display: grid;
  grid-template-columns: 84px minmax(0, 1fr);
  align-items: stretch;
  gap: 11px;
  min-width: 0;
}

.preview-overview.is-empty {
  grid-template-columns: 1fr;
}

.preview-overview.is-empty .preview-copy {
  display: none;
}

.preview-stage {
  position: relative;
  display: grid;
  height: 104px;
  min-height: 0;
  place-items: center;
  overflow: hidden;
  border: 1px solid rgb(255 255 255 / 5%);
  border-radius: 12px;
  background:
    radial-gradient(circle at 50% 12%, rgb(119 108 242 / 10%), transparent 38%),
    linear-gradient(160deg, #171d2b, #121827);
}

/* Keep the complete portrait/landscape visible; letterboxing is intentional. */
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

.preview-placeholder {
  display: grid;
  place-items: center;
  gap: 6px;
  padding: 12px;
  color: #9da7b9;
  text-align: center;
}

.preview-placeholder-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border: 1px solid rgb(255 255 255 / 8%);
  border-radius: 16px;
  color: #a9b2c4;
  background: rgb(255 255 255 / 4%);
}

.preview-placeholder-icon svg {
  width: 24px;
  height: 24px;
  fill: none;
  stroke: currentcolor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.7;
}

.preview-placeholder p {
  margin: 0;
  font-size: 13px;
}

.preview-details-scroll {
  min-height: 0;
  margin-top: 11px;
  padding-top: 11px;
  border-top: 1px solid var(--border, #e1e6ef);
  overflow: auto;
  scrollbar-gutter: stable;
}

.preview-copy {
  min-width: 0;
  padding: 4px 2px;
}

.preview-copy h3 {
  overflow: hidden;
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview-copy > p {
  margin: 3px 0 0;
  overflow: hidden;
  color: var(--muted, #596478);
  font-size: 12px;
  overflow-wrap: anywhere;
  text-overflow: ellipsis;
}

.selection-highlights {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 10px;
}

.selection-highlights span {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 3px 7px;
  border-radius: 999px;
  color: var(--brand-strong, #443ab7);
  font-size: 12px;
  font-weight: 700;
  background: var(--brand-soft, #f0efff);
}

.preview-tag-group + .preview-tag-group {
  margin-top: 10px;
}

.preview-tag-group h3 {
  margin: 0;
  color: var(--faint, #747f93);
  font-size: 12px;
  font-weight: 700;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}

.tag {
  min-height: 22px;
  padding: 2px 7px;
  color: var(--brand-strong, #443ab7);
  font-size: 12px;
  background: var(--brand-soft, #f0efff);
}

.tag-match {
  border: 1px solid #d7d4ff;
  background: #e8e6ff;
}

.empty-inline {
  color: var(--faint, #747f93);
  font-size: 12px;
}

.metadata-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin: 11px 0 0;
}

.metadata-grid > div {
  min-width: 0;
  padding: 8px 9px;
  border-radius: 10px;
  background: var(--surface-soft, #f8f9fc);
}

.metadata-grid dt,
.metadata-grid dd {
  margin: 0;
}

.metadata-grid dt {
  color: var(--faint, #747f93);
  font-size: 12px;
}

.metadata-grid dd {
  margin-top: 2px;
  overflow: hidden;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.metadata-grid .metadata-wide {
  grid-column: 1 / -1;
}

.metadata-grid .metadata-wide dd {
  overflow-wrap: anywhere;
  white-space: normal;
}

.preview-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}

.button {
  min-height: 38px;
  padding: 8px 13px;
  border: 1px solid transparent;
  border-radius: 10px;
  color: var(--text, #171e2e);
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}

.button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.button-secondary {
  border-color: #d6d4ff;
  color: var(--brand-strong, #443ab7);
  background: var(--brand-soft, #f0efff);
}

.button-quiet {
  border-color: var(--border, #e1e6ef);
  background: var(--surface, #fff);
}
</style>
