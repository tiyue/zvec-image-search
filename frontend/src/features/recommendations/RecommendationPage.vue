<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import GalleryContextMenu from "../../components/GalleryContextMenu.vue";
import ImageViewer from "../../components/ImageViewer.vue";
import { useImageViewerSession, type ViewerImage } from "../../composables/useImageViewer";
import RecommendationDetail from "./RecommendationDetail.vue";
import type { RecommendationAction, RecommendationItem } from "./types";
import { useRecommendations } from "./useRecommendations";

type NativeRecommendationAction =
  | "open"
  | "reveal"
  | "copyImage"
  | "copyFile"
  | "copyPath"
  | "export";

const MENU_MARGIN = 8;
const MENU_WIDTH = 238;
const MENU_HEIGHT = 400;

const props = withDefaults(defineProps<{
  visible?: boolean;
  exportBusy?: boolean;
  openImage?: (mediaId: string) => Promise<boolean>;
  revealImage?: (mediaId: string) => Promise<boolean>;
  copyImage?: (mediaId: string) => Promise<boolean>;
  copyFile?: (mediaId: string) => Promise<boolean>;
  copyPath?: (mediaId: string) => Promise<boolean>;
  exportImage?: (mediaId: string) => Promise<boolean>;
}>(), {
  visible: true,
  exportBusy: false,
  openImage: undefined,
  revealImage: undefined,
  copyImage: undefined,
  copyFile: undefined,
  copyPath: undefined,
  exportImage: undefined,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
}>();

const recommendations = useRecommendations(undefined, {
  onError: (title, message) => emit("toast", title, message, "error"),
});
const nativePending = ref<string[]>([]);
const contextMenu = ref<{
  item: RecommendationItem | null;
  x: number;
  y: number;
}>({ item: null, x: MENU_MARGIN, y: MENU_MARGIN });
const detailItem = ref<RecommendationItem | null>(null);
const selectedItemId = ref("");

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
const personalizationLabel = computed(() => {
  const personalization = batch.value?.personalization;
  if (!personalization) return "";
  if (personalization.applied) {
    return `个性化已应用（${personalization.effectiveCount} 张有效偏好）`;
  }
  if (personalization.reason === "insufficient_preferences") {
    return `个性化将在累计 10 张有效偏好后启用（当前 ${personalization.effectiveCount} 张）`;
  }
  if (personalization.reason === "vectors_unavailable") {
    return "个性化未应用：有效偏好暂无可用向量";
  }
  if (personalization.reason === "incompatible_vector_spaces") {
    return "个性化未应用：图库向量空间不兼容";
  }
  if (personalization.reason === "replayed") {
    return "当前批次为幂等回放，未重新计算个性化排序";
  }
  return "个性化未应用";
});

const bucketLabels: Record<RecommendationItem["bucket"], string> = {
  quality: "技术质量",
  recent: "推荐",
  low_exposure: "低曝光",
  random: "随机发现",
};

// Recommendations stay inside the already-shown batch: no page loading and no
// extra shown exposure while stepping through the shared viewer.
function toViewerImage(item: RecommendationItem): ViewerImage {
  return {
    id: item.itemId,
    name: item.name,
    relativePath: "",
    libraryName: item.libraryName,
    width: item.width,
    height: item.height,
    tags: item.tags,
    thumbnailUrl: item.thumbnailUrl,
    imageUrl: item.previewUrl || item.thumbnailUrl,
    nativeId: item.mediaId,
  };
}

const imageViewer = useImageViewerSession({
  items: () => batch.value?.items.map(toViewerImage) ?? [],
  startId: () => "",
  hasMore: () => false,
  loadAdjacent: async () => null,
});

const imageViewerPosition = computed(() => {
  const items = batch.value?.items ?? [];
  const index = items.findIndex((item) => item.itemId === imageViewer.currentId.value);
  return index < 0 ? "" : `${index + 1} / ${items.length}`;
});

const imageViewerPreloads = computed(() => {
  const items = batch.value?.items ?? [];
  const index = items.findIndex((item) => item.itemId === imageViewer.currentId.value);
  if (index < 0) return [];
  const urls: string[] = [];
  const previous = items[index - 1];
  const next = items[index + 1];
  if (previous) urls.push(previous.previewUrl || previous.thumbnailUrl);
  if (next) urls.push(next.previewUrl || next.thumbnailUrl);
  return urls.filter(Boolean);
});

// A single click only selects/focuses the card; the shared viewer opens on a
// double click or the space key so the whole batch stays one shown exposure.
function selectItem(item: RecommendationItem): void {
  selectedItemId.value = item.itemId;
}

function openViewer(item: RecommendationItem): void {
  selectedItemId.value = item.itemId;
  imageViewer.open(item.itemId);
}

function closeViewer(): void {
  const itemId = imageViewer.currentId.value;
  imageViewer.close();
  void nextTick(() => {
    const target = itemId
      ? Array.from(document.querySelectorAll<HTMLElement>(".recommendation-media")).find(
          (element) => element.dataset.itemId === itemId,
        )
      : null;
    target?.focus({ preventScroll: true });
  });
}

// Stepping inside the viewer moves the batch selection with the shown image,
// so closing returns focus to a card that is also the reported selection. It
// stays inside the already-shown batch and records no extra exposure.
watch(
  () => imageViewer.currentId.value,
  (id) => {
    if (imageViewer.visible.value && id) selectedItemId.value = id;
  },
);

function viewerItem(): RecommendationItem | null {
  return (
    batch.value?.items.find((item) => item.itemId === imageViewer.currentId.value) ?? null
  );
}

function openViewerNative(): void {
  const item = viewerItem();
  if (item) void runNativeAction(item, "open");
}

function revealViewerImage(): void {
  const item = viewerItem();
  if (item) void runNativeAction(item, "reveal");
}

watch(
  () => props.visible,
  (visible) => {
    recommendations.setVisible(visible);
    if (!visible) {
      closeContextMenu();
      detailItem.value = null;
      imageViewer.close();
    }
  },
  { immediate: true },
);

function nativeKey(itemId: string, action: NativeRecommendationAction): string {
  return `${itemId}:${action}`;
}

function actionBusy(itemId: string, action?: RecommendationAction): boolean {
  const nativePrefix = `${itemId}:`;
  return nativePending.value.some((key) => key.startsWith(nativePrefix)) ||
    recommendations.isActionPending(itemId, action);
}

function nativeHandler(
  action: NativeRecommendationAction,
): ((mediaId: string) => Promise<boolean>) | undefined {
  if (action === "open") return props.openImage;
  if (action === "reveal") return props.revealImage;
  if (action === "copyImage") return props.copyImage;
  if (action === "copyFile") return props.copyFile;
  if (action === "copyPath") return props.copyPath;
  return props.exportImage;
}

async function runNativeAction(
  item: RecommendationItem,
  action: NativeRecommendationAction,
): Promise<void> {
  const handler = nativeHandler(action);
  if (!handler || actionBusy(item.itemId) || (action === "export" && props.exportBusy)) return;
  const key = nativeKey(item.itemId, action);
  nativePending.value = [...nativePending.value, key];
  try {
    const succeeded = await handler(item.mediaId);
    if (succeeded && (action === "open" || action === "export")) {
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

function openContextMenu(item: RecommendationItem, event: MouseEvent): void {
  event.preventDefault();
  event.stopPropagation();
  const maxX = Math.max(MENU_MARGIN, window.innerWidth - MENU_WIDTH - MENU_MARGIN);
  const maxY = Math.max(MENU_MARGIN, window.innerHeight - MENU_HEIGHT - MENU_MARGIN);
  contextMenu.value = {
    item,
    x: Math.max(MENU_MARGIN, Math.min(event.clientX, maxX)),
    y: Math.max(MENU_MARGIN, Math.min(event.clientY, maxY)),
  };
}

function closeContextMenu(): void {
  contextMenu.value = { ...contextMenu.value, item: null };
}

function openContextDetail(): void {
  const item = contextMenu.value.item;
  if (!item) return;
  detailItem.value = item;
  closeContextMenu();
}

function runContextNative(action: NativeRecommendationAction): void {
  const item = contextMenu.value.item;
  if (item) void runNativeAction(item, action);
}

function setContextPreference(action: "like" | "dislike"): void {
  const item = contextMenu.value.item;
  if (item) void setPreference(item, action);
}

function runDetailNative(action: "open" | "reveal"): void {
  const item = detailItem.value;
  if (item) void runNativeAction(item, action);
}

function handleWindowPointerDown(event: PointerEvent): void {
  const target = event.target;
  if (target instanceof Element && target.closest(".gallery-context-menu")) return;
  closeContextMenu();
}

function handleWindowKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (contextMenu.value.item) {
    event.preventDefault();
    closeContextMenu();
  } else if (detailItem.value) {
    event.preventDefault();
    detailItem.value = null;
  }
}

function handleWindowResize(): void {
  closeContextMenu();
}

onMounted(() => {
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("keydown", handleWindowKeydown);
  window.addEventListener("resize", handleWindowResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("pointerdown", handleWindowPointerDown);
  window.removeEventListener("keydown", handleWindowKeydown);
  window.removeEventListener("resize", handleWindowResize);
});
</script>

<template>
  <section class="recommendation-page" aria-labelledby="recommendation-title">
    <header class="recommendation-heading">
      <div>
        <h1 id="recommendation-title">图片推荐</h1>
        <p>从技术质量、低曝光和随机图片中发现内容。</p>
      </div>
      <button
        class="refresh-button"
        type="button"
        :disabled="recommendations.loading.value"
        @click="recommendations.refresh"
      >
        {{ recommendations.loading.value ? "正在刷新" : "换一批" }}
      </button>
    </header>

    <div v-if="batch" class="recommendation-status" role="status" aria-live="polite">
      <span>{{ batch.count }} 张</span>
      <span>已避开最近 {{ batch.historyWindow }} 张</span>
      <span>{{ diversityLabel }}</span>
      <span>{{ personalizationLabel }}</span>
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
            :aria-label="`选择 ${item.name}`"
            :data-item-id="item.itemId"
            :aria-pressed="selectedItemId === item.itemId"
            :disabled="actionBusy(item.itemId)"
            @click="selectItem(item)"
            @dblclick="openViewer(item)"
            @keydown.space.prevent="openViewer(item)"
            @contextmenu="openContextMenu(item, $event)"
          >
            <img :src="item.thumbnailUrl" :alt="item.name" draggable="false" />
            <span>{{ bucketLabels[item.bucket] }}</span>
          </button>
        </article>
      </div>
    </div>

    <div v-else-if="batch" class="recommendation-empty" role="status">
      <strong>当前没有可推荐的图片</strong>
      <p>图库完成索引后再试一次。</p>
    </div>

    <GalleryContextMenu
      :visible="contextMenu.item !== null"
      :x="contextMenu.x"
      :y="contextMenu.y"
      :selection-count="contextMenu.item ? 1 : 0"
      :exporting="props.exportBusy"
      mode="recommendation"
      :action-pending="contextMenu.item ? actionBusy(contextMenu.item.itemId) : false"
      :feedback-available="true"
      :feedback-pending="contextMenu.item
        ? recommendations.isActionPending(contextMenu.item.itemId)
        : false"
      :feedback-action="contextMenu.item
        ? recommendations.preferenceFor(contextMenu.item.itemId)
        : ''"
      @detail="openContextDetail"
      @open="runContextNative('open')"
      @reveal="runContextNative('reveal')"
      @like="setContextPreference('like')"
      @dislike="setContextPreference('dislike')"
      @copy-image="runContextNative('copyImage')"
      @copy-files="runContextNative('copyFile')"
      @copy-paths="runContextNative('copyPath')"
      @export="runContextNative('export')"
      @close="closeContextMenu"
    />

    <div
      v-if="detailItem"
      class="recommendation-detail-backdrop"
      role="presentation"
      @click.self="detailItem = null"
    >
      <RecommendationDetail
        :item="detailItem"
        :pending="actionBusy(detailItem.itemId)"
        @close="detailItem = null"
        @open="runDetailNative('open')"
        @reveal="runDetailNative('reveal')"
      />
    </div>

    <ImageViewer
      v-if="imageViewer.visible.value"
      :image="imageViewer.current.value"
      :can-previous="imageViewer.hasPrevious.value"
      :can-next="imageViewer.hasNext.value"
      :loading="imageViewer.loading.value"
      :error="imageViewer.error.value"
      :position-text="imageViewerPosition"
      :preload-urls="imageViewerPreloads"
      can-reveal
      @close="closeViewer"
      @previous="imageViewer.step(-1)"
      @next="imageViewer.step(1)"
      @retry="imageViewer.retry()"
      @open-native="openViewerNative"
      @reveal="revealViewerImage"
    />
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

.refresh-button { white-space: nowrap; }

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

.recommendation-skeleton {
  aspect-ratio: 1;
  border-radius: 12px;
  background: linear-gradient(100deg, var(--recommendation-surface) 35%, var(--recommendation-surface-strong) 50%, var(--recommendation-surface) 65%);
  background-size: 240% 100%;
  animation: recommendation-shimmer 1.2s linear infinite;
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

.recommendation-detail-backdrop {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgb(0 0 0 / 24%);
}

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
  .recommendation-detail-backdrop { padding: 8px; }
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
}

@media (prefers-reduced-motion: reduce) {
  .recommendation-skeleton { animation: none; }
}
</style>
