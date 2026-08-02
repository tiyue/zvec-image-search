<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { RecommendationItem } from "./types";

const props = withDefaults(defineProps<{
  item: RecommendationItem;
  pending?: boolean;
}>(), {
  pending: false,
});

const emit = defineEmits<{
  close: [];
  open: [mediaId: string];
  reveal: [mediaId: string];
}>();

const imageFailed = ref(false);
const imageSource = computed(() => props.item.previewUrl || props.item.thumbnailUrl);
const tags = computed(() => [
  ...new Set(props.item.tags.map((tag) => tag.trim()).filter(Boolean)),
]);

const bucketLabels: Record<RecommendationItem["bucket"], string> = {
  quality: "技术质量",
  recent: "最近入库",
  low_exposure: "低曝光",
  random: "随机发现",
};

watch(imageSource, () => {
  imageFailed.value = false;
});

function formatFileSize(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const digits = amount >= 100 || unitIndex === 0 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits).replace(/\.0+$/u, "")} ${units[unitIndex]}`;
}

function dimensions(): string {
  return props.item.width > 0 && props.item.height > 0
    ? `${props.item.width} × ${props.item.height}`
    : "—";
}
</script>

<template>
  <aside
    class="recommendation-detail"
    role="dialog"
    aria-modal="true"
    aria-labelledby="recommendation-detail-title"
  >
    <header class="recommendation-detail-heading">
      <div>
        <p>推荐图片</p>
        <h2 id="recommendation-detail-title">图片详情</h2>
      </div>
      <button
        class="recommendation-detail-close"
        type="button"
        aria-label="关闭详情"
        @click="emit('close')"
      >×</button>
    </header>

    <div class="recommendation-detail-stage">
      <img
        v-if="imageSource && !imageFailed"
        :src="imageSource"
        :alt="item.name"
        draggable="false"
        @error="imageFailed = true"
      />
      <p v-else role="status">图片预览不可用</p>
    </div>

    <div class="recommendation-detail-title">
      <h3 :title="item.name">{{ item.name }}</h3>
      <p>{{ item.libraryName }}</p>
    </div>

    <section class="recommendation-detail-tags" aria-labelledby="recommendation-tags-title">
      <h3 id="recommendation-tags-title">标签</h3>
      <div>
        <span v-for="tag in tags" :key="tag">{{ tag }}</span>
        <span v-if="tags.length === 0">—</span>
      </div>
    </section>

    <dl class="recommendation-detail-metadata">
      <div>
        <dt>图库</dt>
        <dd :title="item.libraryName">{{ item.libraryName || "—" }}</dd>
      </div>
      <div>
        <dt>推荐来源</dt>
        <dd>{{ bucketLabels[item.bucket] }}</dd>
      </div>
      <div>
        <dt>图片尺寸</dt>
        <dd>{{ dimensions() }}</dd>
      </div>
      <div>
        <dt>文件大小</dt>
        <dd>{{ formatFileSize(item.sizeBytes) }}</dd>
      </div>
    </dl>

    <div class="recommendation-detail-actions">
      <button
        type="button"
        :disabled="pending"
        @click="emit('open', item.mediaId)"
      >系统打开</button>
      <button
        type="button"
        :disabled="pending"
        @click="emit('reveal', item.mediaId)"
      >所在文件夹</button>
    </div>
  </aside>
</template>

<style scoped>
.recommendation-detail {
  --detail-bg: #fff;
  --detail-surface: #f5f5f3;
  --detail-surface-strong: #e9e9e6;
  --detail-text: #171717;
  --detail-muted: #6d6d69;
  --detail-border: rgb(0 0 0 / 10%);
  display: grid;
  width: min(520px, calc(100vw - 32px));
  max-height: calc(100vh - 48px);
  grid-template-rows: auto minmax(180px, 42vh) auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  padding: 16px;
  border: 1px solid var(--detail-border);
  border-radius: 14px;
  color: var(--detail-text);
  background: var(--detail-bg);
  box-shadow: 0 22px 70px rgb(0 0 0 / 22%);
}

.recommendation-detail-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.recommendation-detail-heading p,
.recommendation-detail-heading h2,
.recommendation-detail-title h3,
.recommendation-detail-title p,
.recommendation-detail-tags h3,
.recommendation-detail-stage p {
  margin: 0;
}

.recommendation-detail-heading p {
  color: var(--detail-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.recommendation-detail-heading h2 {
  margin-top: 2px;
  font-size: 16px;
}

.recommendation-detail-close {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 50%;
  color: var(--detail-muted);
  font: inherit;
  font-size: 21px;
  background: transparent;
}

.recommendation-detail-close:hover,
.recommendation-detail-close:focus-visible {
  color: var(--detail-text);
  background: var(--detail-surface-strong);
}

.recommendation-detail-stage {
  display: grid;
  min-height: 0;
  place-items: center;
  overflow: hidden;
  border-radius: 10px;
  color: var(--detail-muted);
  background: var(--detail-surface);
}

.recommendation-detail-stage img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.recommendation-detail-title {
  min-width: 0;
  padding: 12px 0;
  border-bottom: 1px solid var(--detail-border);
}

.recommendation-detail-title h3,
.recommendation-detail-title p {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recommendation-detail-title h3 { font-size: 14px; }
.recommendation-detail-title p { margin-top: 3px; color: var(--detail-muted); font-size: 12px; }

.recommendation-detail-tags {
  padding: 12px 0;
  border-bottom: 1px solid var(--detail-border);
}

.recommendation-detail-tags h3 {
  color: var(--detail-muted);
  font-size: 12px;
}

.recommendation-detail-tags div {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.recommendation-detail-tags span {
  padding: 3px 7px;
  border-radius: 999px;
  color: var(--detail-text);
  font-size: 12px;
  background: var(--detail-surface);
}

.recommendation-detail-metadata {
  min-height: 0;
  margin: 0;
  overflow-y: auto;
}

.recommendation-detail-metadata > div {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 8px;
  padding: 9px 0;
  border-bottom: 1px solid var(--detail-border);
}

.recommendation-detail-metadata dt,
.recommendation-detail-metadata dd { margin: 0; font-size: 12px; }
.recommendation-detail-metadata dt { color: var(--detail-muted); }
.recommendation-detail-metadata dd { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.recommendation-detail-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding-top: 12px;
}

.recommendation-detail-actions button {
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--detail-border);
  border-radius: 9px;
  color: var(--detail-text);
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  background: var(--detail-surface);
}

.recommendation-detail button:disabled { opacity: 0.5; }

@media (max-width: 520px) {
  .recommendation-detail {
    width: calc(100vw - 16px);
    max-height: calc(100vh - 16px);
    padding: 12px;
    grid-template-rows: auto minmax(160px, 34vh) auto auto minmax(0, 1fr) auto;
  }
}

@media (prefers-color-scheme: dark) {
  .recommendation-detail {
    color-scheme: dark;
    --detail-bg: #171816;
    --detail-surface: #22231f;
    --detail-surface-strong: #30312c;
    --detail-text: #f1f1eb;
    --detail-muted: #b1b2aa;
    --detail-border: rgb(255 255 255 / 11%);
    box-shadow: 0 22px 70px rgb(0 0 0 / 48%);
  }
}
</style>
