<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { RecommendationAction, RecommendationItem } from "./types";
import { useRecommendations } from "./useRecommendations";

const props = withDefaults(defineProps<{
  visible?: boolean;
  exportBusy?: boolean;
  openImage?: (mediaId: string) => Promise<boolean>;
  exportImage?: (mediaId: string) => Promise<boolean>;
}>(), {
  visible: true,
  exportBusy: false,
  openImage: undefined,
  exportImage: undefined,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
}>();

const recommendations = useRecommendations(undefined, {
  onError: (title, message) => emit("toast", title, message, "error"),
});
const nativePending = ref<string[]>([]);

const batch = computed(() => recommendations.currentBatch.value);
const diversityLabel = computed(() => {
  const diversity = batch.value?.diversity;
  if (!diversity) return "";
  if (!diversity.applied) {
    return diversity.reason
      ? `向量多样性未应用：${diversity.reason}`
      : "向量多样性未应用";
  }
  if (diversity.missingVectors > 0) {
    return `向量多样性已应用，${diversity.missingVectors} 张缺少向量`;
  }
  return "向量多样性已应用";
});

const bucketLabels: Record<RecommendationItem["bucket"], string> = {
  quality: "技术质量",
  recent: "最近入库",
  low_exposure: "低曝光",
  random: "随机发现",
};

watch(
  () => props.visible,
  (visible) => recommendations.setVisible(visible),
  { immediate: true },
);

function nativeKey(itemId: string, action: "open" | "export"): string {
  return `${itemId}:${action}`;
}

function actionBusy(itemId: string, action?: RecommendationAction): boolean {
  const nativePrefix = `${itemId}:`;
  return nativePending.value.some((key) => key.startsWith(nativePrefix)) ||
    recommendations.isActionPending(itemId, action);
}

async function runNativeAction(
  item: RecommendationItem,
  action: "open" | "export",
): Promise<void> {
  const handler = action === "open" ? props.openImage : props.exportImage;
  if (!handler || actionBusy(item.itemId) || (action === "export" && props.exportBusy)) return;
  const key = nativeKey(item.itemId, action);
  nativePending.value = [...nativePending.value, key];
  try {
    if (await handler(item.mediaId)) {
      await recommendations.recordAction(item.itemId, action);
    }
  } finally {
    nativePending.value = nativePending.value.filter((candidate) => candidate !== key);
  }
}

async function setPreference(
  item: RecommendationItem,
  action: "like" | "dislike",
): Promise<void> {
  if (recommendations.preferenceFor(item.itemId) === action || actionBusy(item.itemId)) return;
  await recommendations.recordAction(item.itemId, action);
}
</script>

<template>
  <section class="recommendation-page" aria-labelledby="recommendation-title">
    <header class="recommendation-heading">
      <div>
        <h1 id="recommendation-title">图片推荐</h1>
        <p>从技术质量、最近入库、低曝光和随机图片中发现内容。</p>
      </div>
      <button
        class="refresh-button"
        type="button"
        :disabled="recommendations.loading.value || recommendations.shownPending.value"
        @click="recommendations.refresh"
      >
        {{ recommendations.loading.value ? "正在刷新" : "换一批" }}
      </button>
    </header>

    <div v-if="batch" class="recommendation-status" role="status" aria-live="polite">
      <span>{{ batch.count }} 张</span>
      <span>已避开最近 {{ batch.historyWindow }} 张</span>
      <span>{{ diversityLabel }}</span>
      <span v-if="batch.quotaDegraded">部分来源已由其他候选补足</span>
      <span v-if="batch.partial">本批数量不足 15 张</span>
    </div>

    <div v-if="recommendations.shownError.value" class="sync-notice" role="status">
      <span>展示记录尚未同步。</span>
      <button type="button" @click="recommendations.retryShown()">重试</button>
    </div>

    <div v-if="!batch && recommendations.loading.value" class="recommendation-grid" aria-label="正在准备推荐">
      <div v-for="index in 15" :key="index" class="recommendation-skeleton" aria-hidden="true" />
    </div>

    <div v-else-if="!batch && recommendations.error.value" class="recommendation-empty" role="alert">
      <strong>暂时无法读取推荐</strong>
      <p>{{ recommendations.error.value }}</p>
      <button type="button" @click="recommendations.refresh">重试</button>
    </div>

    <div v-else-if="batch && batch.items.length" class="recommendation-frame">
      <div class="recommendation-grid" role="list" :aria-busy="recommendations.loading.value">
        <article v-for="item in batch.items" :key="item.itemId" class="recommendation-item" role="listitem">
          <button
            class="recommendation-media"
            type="button"
            :aria-label="`打开 ${item.name}`"
            :disabled="actionBusy(item.itemId)"
            @click="runNativeAction(item, 'open')"
          >
            <img :src="item.thumbnailUrl" :alt="item.name" draggable="false" />
            <span>{{ bucketLabels[item.bucket] }}</span>
          </button>
          <div class="recommendation-copy">
            <strong :title="item.name">{{ item.name }}</strong>
            <small>{{ item.libraryName }}</small>
          </div>
          <div class="recommendation-actions" :aria-label="`${item.name} 操作`">
            <button type="button" :disabled="actionBusy(item.itemId)" @click="runNativeAction(item, 'open')">打开</button>
            <button
              type="button"
              :class="{ active: recommendations.preferenceFor(item.itemId) === 'like' }"
              :aria-pressed="recommendations.preferenceFor(item.itemId) === 'like'"
              :disabled="actionBusy(item.itemId)"
              @click="setPreference(item, 'like')"
            >喜欢</button>
            <button
              type="button"
              :disabled="props.exportBusy || actionBusy(item.itemId)"
              @click="runNativeAction(item, 'export')"
            >导出</button>
            <button
              type="button"
              :class="{ active: recommendations.preferenceFor(item.itemId) === 'dislike' }"
              :aria-pressed="recommendations.preferenceFor(item.itemId) === 'dislike'"
              :disabled="actionBusy(item.itemId)"
              @click="setPreference(item, 'dislike')"
            >不喜欢</button>
          </div>
        </article>
      </div>
      <div v-if="recommendations.loading.value" class="recommendation-loading" role="status">
        正在准备下一批，当前图片会继续保留。
      </div>
    </div>

    <div v-else-if="batch" class="recommendation-empty" role="status">
      <strong>当前没有可推荐的图片</strong>
      <p>图库完成索引后再试一次。</p>
    </div>
  </section>
</template>

<style scoped>
.recommendation-page {
  --recommendation-bg: #fff;
  --recommendation-surface: #f5f5f3;
  --recommendation-surface-strong: #e9e9e6;
  --recommendation-text: #171717;
  --recommendation-muted: #6d6d69;
  --recommendation-border: rgb(0 0 0 / 9%);
  --recommendation-action: #242421;
  position: relative;
  width: 100%;
  min-height: 100%;
  padding: 24px;
  color: var(--recommendation-text);
  background: var(--recommendation-bg);
}

.recommendation-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.recommendation-heading h1 { margin: 0; font-size: 22px; font-weight: 600; }
.recommendation-heading p { margin: 4px 0 0; color: var(--recommendation-muted); font-size: 13px; }
.refresh-button,
.recommendation-empty button,
.sync-notice button {
  min-height: 36px;
  padding: 0 14px;
  border: 0;
  border-radius: 9px;
  color: var(--recommendation-bg);
  background: var(--recommendation-action);
}

.recommendation-status,
.sync-notice {
  display: flex;
  min-height: 32px;
  align-items: center;
  gap: 8px 16px;
  margin-bottom: 16px;
  color: var(--recommendation-muted);
  font-size: 12px;
}

.recommendation-status { flex-wrap: wrap; }
.recommendation-status span + span { padding-left: 16px; border-left: 1px solid var(--recommendation-border); }
.sync-notice { justify-content: space-between; padding: 8px 12px; border-radius: 10px; background: var(--recommendation-surface); }
.sync-notice button { min-height: 30px; padding-inline: 10px; }

.recommendation-frame { position: relative; }
.recommendation-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
}

.recommendation-item {
  display: grid;
  min-width: 0;
  align-content: start;
  overflow: hidden;
  border: 1px solid var(--recommendation-border);
  border-radius: 12px;
  background: var(--recommendation-surface);
}

.recommendation-media {
  position: relative;
  display: grid;
  aspect-ratio: 1;
  min-width: 0;
  place-items: center;
  overflow: hidden;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: var(--recommendation-surface-strong);
}

.recommendation-media img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.recommendation-media span {
  position: absolute;
  right: 8px;
  bottom: 8px;
  padding: 3px 7px;
  border-radius: 7px;
  color: #fff;
  font-size: 11px;
  background: rgb(0 0 0 / 68%);
}

.recommendation-copy { display: grid; gap: 2px; padding: 10px 10px 8px; }
.recommendation-copy strong,
.recommendation-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.recommendation-copy strong { font-size: 13px; font-weight: 600; }
.recommendation-copy small { color: var(--recommendation-muted); font-size: 11px; }

.recommendation-actions {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid var(--recommendation-border);
}

.recommendation-actions button {
  min-width: 0;
  min-height: 32px;
  padding: 0 4px;
  border: 0;
  border-right: 1px solid var(--recommendation-border);
  color: var(--recommendation-muted);
  font-size: 11px;
  background: transparent;
}

.recommendation-actions button:last-child { border-right: 0; }
.recommendation-actions button:hover:not(:disabled),
.recommendation-actions button.active { color: var(--recommendation-text); background: var(--recommendation-surface-strong); }

.recommendation-skeleton {
  aspect-ratio: 1;
  border-radius: 12px;
  background: linear-gradient(100deg, var(--recommendation-surface) 35%, var(--recommendation-surface-strong) 50%, var(--recommendation-surface) 65%);
  background-size: 240% 100%;
  animation: recommendation-shimmer 1.2s linear infinite;
}

.recommendation-loading {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  color: var(--recommendation-text);
  font-size: 13px;
  background: rgb(255 255 255 / 68%);
}

.recommendation-empty {
  display: grid;
  min-height: 280px;
  place-items: center;
  align-content: center;
  gap: 8px;
  color: var(--recommendation-muted);
  text-align: center;
}

.recommendation-empty strong { color: var(--recommendation-text); font-size: 16px; }
.recommendation-empty p { max-width: 520px; margin: 0; }

@keyframes recommendation-shimmer { to { background-position: -240% 0; } }

@media (max-width: 1180px) {
  .recommendation-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 820px) {
  .recommendation-page { padding: 16px; }
  .recommendation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .recommendation-heading { align-items: flex-start; }
  .recommendation-heading p { max-width: 440px; }
  .recommendation-status span + span { padding-left: 0; border-left: 0; }
}

@media (prefers-color-scheme: dark) {
  .recommendation-page {
    color-scheme: dark;
    --recommendation-bg: #171816;
    --recommendation-surface: #22231f;
    --recommendation-surface-strong: #30312c;
    --recommendation-text: #f1f1eb;
    --recommendation-muted: #b1b2aa;
    --recommendation-border: rgb(255 255 255 / 10%);
    --recommendation-action: #efefe9;
  }
  .recommendation-loading { background: rgb(23 24 22 / 72%); }
}

@media (prefers-reduced-motion: reduce) {
  .recommendation-skeleton { animation: none; }
}
</style>
