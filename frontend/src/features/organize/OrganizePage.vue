<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";

import PaginationBar from "../../components/PaginationBar.vue";
import ActiveLearningPanel from "./ActiveLearningPanel.vue";
import {
  calculateOrganizeGalleryCapacity,
  type OrganizeGalleryCapacity,
} from "./galleryCapacity";
import SimilarityGroupsPanel from "./SimilarityGroupsPanel.vue";

import type {
  ManualTagOperation,
  OrganizeAlias,
  OrganizeApi,
  OrganizeImage,
  TagPresence,
  ToastKind,
} from "./types";
import { parseTagInput, useOrganize } from "./useOrganize";

type WorkspaceTab = "batch" | "clusters" | "learning";

const props = withDefaults(defineProps<{
  api?: OrganizeApi;
  autoLoad?: boolean;
  activeTab?: WorkspaceTab;
  showTabs?: boolean;
}>(), {
  autoLoad: true,
  activeTab: "batch",
  showTabs: true,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
  openImage: [imageId: string];
}>();

const organize = useOrganize(props.api, {
  autoLoad: false,
  onToast: (title, message, kind) => emit("toast", title, message, kind),
});
const aliasCanonical = ref("");
const aliasInput = ref("");
const failedThumbnails = ref<Set<string>>(new Set());
const galleryViewport = ref<HTMLElement | null>(null);
const galleryCapacity = ref<OrganizeGalleryCapacity>(
  calculateOrganizeGalleryCapacity(0, 0),
);
const editorOpen = ref(false);
const deleteConfirmation = ref("");
const deleteTargetKey = ref("");
const folderPanelCollapsed = ref(false);
const activeWorkspaceTab = ref<WorkspaceTab>(props.activeTab);
const visitedWorkspaceTabs = ref<Set<WorkspaceTab>>(
  new Set([props.activeTab]),
);
let galleryObserver: ResizeObserver | null = null;
let galleryResizeTimer: number | null = null;
let initialLoadStarted = false;
const taskSubmissionLocked = computed(
  () => Boolean(organize.busyAction.value) || organize.jobRunning.value,
);

watch(organize.deletePreview, () => {
  deleteConfirmation.value = "";
});

const galleryLayoutStyle = computed(() => ({
  "--organize-gallery-columns": String(galleryCapacity.value.columns),
  "--organize-gallery-rows": String(galleryCapacity.value.rows),
}));

function changeLibrary(event: Event): void {
  void organize.selectLibrary((event.target as HTMLSelectElement).value);
}

function selectWorkspaceTab(tab: WorkspaceTab): void {
  activeWorkspaceTab.value = tab;
  visitedWorkspaceTabs.value = new Set([...visitedWorkspaceTabs.value, tab]);
  if (tab !== "batch") editorOpen.value = false;
}

watch(
  () => props.activeTab,
  (tab) => selectWorkspaceTab(tab),
);

function handleImageClick(image: OrganizeImage, event: MouseEvent): void {
  organize.selectImage(image.id, {
    toggle: event.ctrlKey || event.metaKey,
    range: event.shiftKey,
  });
}

function sourceForImageTag(image: OrganizeImage, tag: string): string {
  const key = tag.toLocaleLowerCase("zh-CN");
  const labels = image.tagSources
    .filter((source) => source.tag.toLocaleLowerCase("zh-CN") === key)
    .map((source) => source.sourceLabel);
  return [...new Set(labels)].join(" / ") || "现有";
}

function sourceSummary(tag: TagPresence): string {
  return tag.sourceLabels.join(" / ") || "来源混合";
}

function submitOperation(operation: ManualTagOperation): void {
  void organize.applyManualTags(operation);
}

function canDisplayThumbnail(image: OrganizeImage): boolean {
  return (
    image.imageAvailable &&
    Boolean(image.thumbnailUrl || image.imageUrl) &&
    !failedThumbnails.value.has(image.id)
  );
}

function markThumbnailUnavailable(imageId: string): void {
  failedThumbnails.value = new Set([...failedThumbnails.value, imageId]);
}

function openImage(image: OrganizeImage): void {
  if (!image.nativeImageId) {
    emit("toast", "无法打开原图", "当前图片文件不可用或尚未注册。", "error");
    return;
  }
  emit("openImage", image.nativeImageId);
}

function formatIndexedAt(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  const amount = value / 1024 ** index;
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function updateGalleryCapacity(reload: boolean): void {
  const element = galleryViewport.value;
  if (!element) return;
  const next = calculateOrganizeGalleryCapacity(element.clientWidth, element.clientHeight);
  const changed =
    next.columns !== galleryCapacity.value.columns ||
    next.rows !== galleryCapacity.value.rows ||
    next.scrollRequired !== galleryCapacity.value.scrollRequired;
  if (!changed) return;
  galleryCapacity.value = next;
  void organize.setImagePageSize(next.pageSize, reload);
}

function scheduleGalleryCapacityUpdate(): void {
  if (galleryResizeTimer !== null) window.clearTimeout(galleryResizeTimer);
  galleryResizeTimer = window.setTimeout(() => {
    galleryResizeTimer = null;
    updateGalleryCapacity(initialLoadStarted);
  }, 140);
}

function handleFolderListScroll(event: Event): void {
  const element = event.currentTarget as HTMLElement;
  if (element.scrollHeight - element.scrollTop - element.clientHeight <= 96) {
    void organize.loadMoreFolders();
  }
}

async function requestFolderDeletion(folderKey: string): Promise<void> {
  if (!folderKey || deleteTargetKey.value) return;
  deleteTargetKey.value = folderKey;
  try {
    await organize.previewFolderDeletion(folderKey);
  } finally {
    deleteTargetKey.value = "";
  }
}

async function confirmFolderDeletion(): Promise<void> {
  await organize.confirmFolderDeletion(deleteConfirmation.value);
}

function editAlias(alias: OrganizeAlias): void {
  aliasCanonical.value = alias.canonical;
  aliasInput.value = alias.aliases.join("、");
}

async function saveAlias(): Promise<void> {
  const saved = await organize.upsertAlias(
    aliasCanonical.value,
    parseTagInput(aliasInput.value),
  );
  if (!saved) return;
  aliasCanonical.value = "";
  aliasInput.value = "";
}

onMounted(async () => {
  await nextTick();
  updateGalleryCapacity(false);
  if (typeof ResizeObserver !== "undefined" && galleryViewport.value) {
    galleryObserver = new ResizeObserver(scheduleGalleryCapacityUpdate);
    galleryObserver.observe(galleryViewport.value);
  }
  initialLoadStarted = true;
  if (props.autoLoad) void organize.load();
});

onBeforeUnmount(() => {
  galleryObserver?.disconnect();
  if (galleryResizeTimer !== null) window.clearTimeout(galleryResizeTimer);
});
</script>

<template>
  <section class="organize-page" :class="{ 'embedded-navigation': !showTabs }" aria-labelledby="batch-tag-title">
    <header v-if="showTabs" class="page-heading">
      <div>
        <p class="eyebrow">批量标签</p>
        <h1 id="batch-tag-title">按文件夹快速整理图片</h1>
        <p>选择图片后一次添加、移除或替换人工标签。模型身份无需逐项确认。</p>
      </div>
      <div class="heading-actions">
        <label>
          <span>当前图库</span>
          <select
            :value="organize.selectedLibraryId.value"
            aria-label="当前图库"
            @change="changeLibrary"
          >
            <option v-if="!organize.libraries.value?.length" value="">尚未配置图库</option>
            <option
              v-for="library in organize.libraries.value"
              :key="library.id"
              :value="library.id"
              :disabled="library.enabled === false"
            >
              {{ library.name }}{{ library.is_default ? "（默认）" : "" }}
            </option>
          </select>
        </label>
        <button
          class="button secondary"
          type="button"
          :disabled="organize.folderLoading.value || organize.imageLoading.value"
          @click="organize.load"
        >
          {{ organize.folderLoading.value ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <nav v-if="showTabs" class="workspace-tabs" aria-label="智能整理功能">
      <button
        type="button"
        :class="{ active: activeWorkspaceTab === 'batch' }"
        :aria-pressed="activeWorkspaceTab === 'batch'"
        @click="selectWorkspaceTab('batch')"
      >
        <strong>批量标签</strong>
        <small>按文件夹手工整理</small>
      </button>
      <button
        type="button"
        :class="{ active: activeWorkspaceTab === 'clusters' }"
        :aria-pressed="activeWorkspaceTab === 'clusters'"
        @click="selectWorkspaceTab('clusters')"
      >
        <strong>相似分组</strong>
        <small>重复图与语义近邻</small>
      </button>
      <button
        type="button"
        :class="{ active: activeWorkspaceTab === 'learning' }"
        :aria-pressed="activeWorkspaceTab === 'learning'"
        @click="selectWorkspaceTab('learning')"
      >
        <strong>待学习样本</strong>
        <small>优先审核高价值图片</small>
      </button>
    </nav>

    <div v-show="activeWorkspaceTab === 'batch'" class="batch-tag-workspace" :class="{ 'is-folder-collapsed': folderPanelCollapsed }">
      <aside class="folder-panel panel" :class="{ 'is-collapsed': folderPanelCollapsed }" aria-labelledby="folder-panel-title">
        <header class="panel-heading">
          <div>
            <p class="eyebrow">图库导航</p>
            <h2 id="folder-panel-title">图库与文件夹</h2>
          </div>
          <span class="count-pill">{{ organize.folderTotal.value }}</span>
        </header>

        <div v-if="!showTabs" class="folder-library-control">
          <label>
            <span class="sr-only">当前图库</span>
            <select
              :value="organize.selectedLibraryId.value"
              aria-label="当前图库"
              @change="changeLibrary"
            >
              <option v-if="!organize.libraries.value?.length" value="">尚未配置图库</option>
              <option
                v-for="library in organize.libraries.value"
                :key="library.id"
                :value="library.id"
                :disabled="library.enabled === false"
              >
                {{ library.name }}{{ library.is_default ? "（默认）" : "" }}
              </option>
            </select>
          </label>
          <button
            class="folder-library-refresh"
            type="button"
            :disabled="organize.folderLoading.value || organize.imageLoading.value"
            aria-label="刷新图库"
            title="刷新图库"
            @click="organize.load"
          >
            ↻
          </button>
        </div>

        <div class="root-list" aria-label="图库根目录">
          <div
            v-for="root in organize.rootGroups.value"
            :key="root.id"
            class="root-row"
            :class="{
              active: root.id === organize.selectedRootId.value,
              'direct-selected':
                Boolean(root.folderKey) && root.folderKey === organize.selectedFolderKey.value,
            }"
          >
            <button
              class="root-button"
              type="button"
              :aria-pressed="Boolean(root.folderKey) && root.folderKey === organize.selectedFolderKey.value"
              :title="root.folderKey ? '查看直接存放在此根目录的图片' : '选择此图库根目录'"
              @click="organize.selectRoot(root.id)"
            >
              <span class="root-icon" aria-hidden="true">▰</span>
              <span class="root-copy">
                <strong>{{ root.name }}</strong>
                <small>{{ root.folderCount }} 个文件夹 · {{ root.imageCount }} 张</small>
              </span>
            </button>
            <button
              class="root-delete-button"
              type="button"
              :disabled="!root.folderKey || Boolean(deleteTargetKey) || taskSubmissionLocked"
              :aria-label="`清理根目录 ${root.name}`"
              :title="root.folderKey ? '清理磁盘图片和图库记录' : '当前后端未提供根目录清理键'"
              @click="requestFolderDeletion(root.folderKey)"
            >
              {{ deleteTargetKey === root.folderKey ? "检查中" : "清理" }}
            </button>
          </div>
        </div>

        <form class="folder-search" role="search" @submit.prevent="organize.searchFolders">
          <input
            v-model="organize.folderQuery.value"
            type="search"
            aria-label="搜索文件夹"
            placeholder="搜索文件夹名称"
          />
          <button class="icon-button" type="submit" aria-label="执行文件夹搜索">⌕</button>
        </form>

        <div v-if="organize.folderLoading.value && !organize.folders.value.length" class="empty-state compact">
          正在读取文件夹…
        </div>
        <div v-else-if="!organize.visibleFolders.value.length" class="empty-state compact">
          没有找到文件夹
        </div>
        <nav
          v-else
          class="folder-list"
          aria-label="文件夹列表"
          @scroll.passive="handleFolderListScroll"
        >
          <div
            v-for="folder in organize.visibleFolders.value"
            :key="folder.key"
            class="folder-item"
            :class="{ active: folder.key === organize.selectedFolderKey.value }"
            :data-folder-key="folder.key"
          >
            <button class="folder-select-button" type="button" @click="organize.selectFolder(folder.key)">
              <span class="folder-glyph" aria-hidden="true">◆</span>
              <span class="folder-copy">
                <strong>{{ folder.name }}</strong>
                <small :title="folder.relativeFolder">{{ folder.relativeFolder || "根目录" }}</small>
                <span class="folder-stats">
                  <span>{{ folder.directImageCount }} 张</span>
                  <span v-if="folder.manualTaggedCount">人工 {{ folder.manualTaggedCount }}</span>
                  <span v-if="folder.modelTaggedCount">模型 {{ folder.modelTaggedCount }}</span>
                  <span v-if="folder.failedCount" class="warning">待处理 {{ folder.failedCount }}</span>
                  <span
                    v-if="formatIndexedAt(folder.firstIndexedAt)"
                    class="folder-time"
                    :title="folder.firstIndexedAt"
                  >
                    首次 {{ formatIndexedAt(folder.firstIndexedAt) }}
                  </span>
                  <span
                    v-if="formatIndexedAt(folder.lastIndexedAt)"
                    class="folder-time"
                    :title="folder.lastIndexedAt"
                  >
                    最近 {{ formatIndexedAt(folder.lastIndexedAt) }}
                  </span>
                </span>
              </span>
            </button>
            <button
              class="folder-delete-button"
              type="button"
              :disabled="Boolean(deleteTargetKey) || taskSubmissionLocked"
              :aria-label="`清理文件夹 ${folder.name}`"
              title="清理当前文件夹中的磁盘图片和图库记录"
              @click="requestFolderDeletion(folder.key)"
            >
              {{ deleteTargetKey === folder.key ? "…" : "×" }}
            </button>
          </div>
        </nav>
        <button
          v-if="organize.folderHasMore.value"
          class="folder-load-more"
          type="button"
          :disabled="organize.folderLoadingMore.value"
          @click="organize.loadMoreFolders"
        >
          {{ organize.folderLoadingMore.value
            ? "正在加载更多…"
            : `加载更多文件夹（${organize.folders.value.length}/${organize.folderTotal.value}）` }}
        </button>
      </aside>

      <main class="gallery-panel panel" aria-labelledby="folder-gallery-title">
        <header class="gallery-heading">
          <button class="folder-panel-toggle" type="button" :aria-label="folderPanelCollapsed ? '展开文件夹导航' : '收起文件夹导航'" @click="folderPanelCollapsed = !folderPanelCollapsed">
            {{ folderPanelCollapsed ? "›" : "‹" }}
          </button>
          <div>
            <p class="eyebrow">当前文件夹</p>
            <h2 id="folder-gallery-title">
              {{ organize.selectedFolder.value?.name || "请选择文件夹" }}
            </h2>
            <small>{{ organize.selectedFolder.value?.relativeFolder || "" }}</small>
          </div>
          <div class="gallery-metrics">
            <strong>{{ organize.imageTotal.value }}</strong>
            <span>张图片</span>
          </div>
        </header>

        <section class="selection-toolbar" aria-label="图片选择操作">
          <div class="selection-summary">
            <strong>{{ organize.selectionDescription.value }}</strong>
            <span>单击单选，Ctrl 多选，Shift 连续选择</span>
          </div>
          <div class="selection-actions">
            <button class="toolbar-button" type="button" @click="organize.selectCurrentPage">全选当前页</button>
            <button class="toolbar-button" type="button" @click="organize.selectWholeFolder">选择整文件夹</button>
            <button class="toolbar-button" type="button" @click="organize.invertCurrentPage">反选当前页</button>
            <button class="toolbar-button quiet" type="button" @click="organize.clearSelection">清空</button>
            <button class="toolbar-button editor-toggle" type="button" @click="editorOpen = true">
              标签设置
            </button>
          </div>
        </section>

        <div ref="galleryViewport" class="gallery-viewport">
          <div v-if="organize.imageLoading.value && !organize.images.value.length" class="empty-state gallery-empty">
            <strong>正在读取图片</strong>
            <span>大文件夹会分页加载，不会阻塞窗口。</span>
          </div>
          <div v-else-if="!organize.images.value.length" class="empty-state gallery-empty">
            <strong>{{ organize.selectedFolderKey.value ? "当前文件夹没有图片" : "请从左侧选择文件夹" }}</strong>
            <span>缩略图会完整显示，不会裁切主体。</span>
          </div>
          <div
            v-else
            class="image-grid"
            :class="{ 'is-scroll-layout': galleryCapacity.scrollRequired }"
            :style="galleryLayoutStyle"
            aria-label="文件夹图片"
          >
            <article
              v-for="image in organize.images.value"
              :key="image.id"
              class="image-tile"
              :class="{ selected: organize.isImageSelected(image.id) }"
              :data-image-id="image.id"
            >
              <button
                class="image-select"
                type="button"
                :aria-pressed="organize.isImageSelected(image.id)"
                @click="handleImageClick(image, $event)"
              >
                <span class="selection-marker" aria-hidden="true">
                  {{ organize.isImageSelected(image.id) ? "✓" : "" }}
                </span>
                <span class="thumbnail-stage" @dblclick.stop.prevent="openImage(image)">
                  <img
                    v-if="canDisplayThumbnail(image)"
                    class="media-contain"
                    :src="image.thumbnailUrl || image.imageUrl"
                    :alt="image.fileName"
                    loading="lazy"
                    decoding="async"
                    @error="markThumbnailUnavailable(image.id)"
                  />
                  <span v-else class="thumbnail-placeholder" role="img" :aria-label="`${image.fileName} 图片不可用`">
                    <span aria-hidden="true">▧</span>
                    <small>图片不可用</small>
                  </span>
                </span>
                <span class="image-meta">
                  <strong :title="image.fileName">{{ image.fileName }}</strong>
                  <small>{{ image.width && image.height ? `${image.width} × ${image.height}` : "尺寸未知" }}</small>
                </span>
              </button>
              <div class="image-tags">
                <span
                  v-for="tag in image.effectiveTags.slice(0, 3)"
                  :key="tag"
                  class="mini-tag"
                  :title="sourceForImageTag(image, tag)"
                >
                  {{ tag }}
                </span>
                <span v-if="image.effectiveTags.length > 3" class="mini-tag more">
                  +{{ image.effectiveTags.length - 3 }}
                </span>
                <span v-if="!image.effectiveTags.length" class="no-tag">无标签</span>
              </div>
            </article>
          </div>
        </div>

        <PaginationBar
          id-prefix="organize-gallery"
          aria-label="文件夹图片分页"
          :page="organize.imagePage.value"
          :total-pages="organize.imageTotalPages.value"
          :total-items="organize.imageTotal.value"
          :loading="organize.imageLoading.value"
          :has-previous="organize.imagePage.value > 1"
          :has-next="organize.imagePage.value < organize.imageTotalPages.value"
          @previous="organize.loadFolderImages(organize.imagePage.value - 1)"
          @next="organize.loadFolderImages(organize.imagePage.value + 1)"
          @jump="organize.loadFolderImages"
        />
      </main>

      <aside
        class="tag-editor panel"
        :class="{ 'is-open': editorOpen }"
        aria-labelledby="tag-editor-title"
      >
        <header class="panel-heading">
          <div>
            <p class="eyebrow">标签编辑</p>
            <h2 id="tag-editor-title">批量修改</h2>
          </div>
          <div class="tag-editor-heading-actions">
            <span class="selection-count">{{ organize.selectedCount.value }}</span>
            <button class="editor-close" type="button" aria-label="关闭标签设置" @click="editorOpen = false">×</button>
          </div>
        </header>

        <section v-if="!organize.selectedCount.value" class="tag-editor-empty">
          <span aria-hidden="true">◇</span>
          <div><strong>开始批量操作</strong><p>在左侧选择文件夹，然后点击中间图片进行选中</p></div>
          <div class="empty-editor-actions"><button type="button" disabled>添加标签</button><button type="button" disabled>移除标签</button><button type="button" disabled>替换人工标签</button></div>
        </section>

        <details class="tag-editor-tools" :open="Boolean(organize.selectedCount.value)">
        <summary>标签与别名设置</summary>
        <div class="tag-editor-tools-content">
        <section class="selection-card">
          <strong>{{ organize.selectionDescription.value }}</strong>
          <p v-if="organize.selectionPreviewIncomplete.value">
            标签统计只预览已加载的 {{ organize.selectedKnownImages.value.length }} 张；提交仍会作用于完整选择。
          </p>
          <p v-else>右侧操作只影响已选图片，不会改动其他文件。</p>
        </section>

        <label class="tag-input-label">
          <span>输入多个标签</span>
          <textarea
            v-model="organize.tagInput.value"
            rows="3"
            placeholder="例如：原神、雷电将军；写真"
            @keydown.enter.prevent="organize.commitTagInput"
            @blur="organize.commitTagInput"
          />
          <small>Enter、中英文逗号、顿号或分号均可分隔。</small>
        </label>

        <div class="draft-tags" aria-label="准备应用的标签">
          <button
            v-for="tag in organize.draftTags.value"
            :key="tag"
            class="draft-tag"
            type="button"
            :aria-label="`移除待应用标签 ${tag}`"
            @click="organize.removeDraftTag(tag)"
          >
            {{ tag }} <span aria-hidden="true">×</span>
          </button>
          <span v-if="!organize.draftTags.value.length" class="muted">尚未输入标签</span>
        </div>

        <section class="tag-state-section">
          <header>
            <h3>全部拥有</h3>
            <span>{{ organize.commonTags.value.length }}</span>
          </header>
          <div class="tag-cloud">
            <span v-for="tag in organize.commonTags.value" :key="tag.tag" class="state-tag common">
              {{ tag.tag }}<small>{{ sourceSummary(tag) }}</small>
            </span>
            <span v-if="!organize.commonTags.value.length" class="muted">暂无共同标签</span>
          </div>
        </section>

        <section class="tag-state-section">
          <header>
            <h3>部分拥有</h3>
            <span>{{ organize.partialTags.value.length }}</span>
          </header>
          <div class="tag-cloud">
            <span v-for="tag in organize.partialTags.value" :key="tag.tag" class="state-tag partial">
              {{ tag.tag }}<small>{{ tag.count }}/{{ tag.total }} · {{ sourceSummary(tag) }}</small>
            </span>
            <span v-if="!organize.partialTags.value.length" class="muted">暂无差异标签</span>
          </div>
        </section>

        <details class="alias-dictionary" data-testid="alias-dictionary">
          <summary>
            <span><strong>别名词典</strong><small>统一角色、作品与人物名称</small></span>
            <span class="alias-count">{{ organize.aliases.value.length }}</span>
          </summary>
          <div class="alias-content">
            <div v-if="organize.aliases.value.length" class="alias-list" aria-label="现有别名">
              <button
                v-for="alias in organize.aliases.value"
                :key="alias.canonical"
                class="alias-entry"
                type="button"
                :title="`编辑 ${alias.canonical}`"
                @click="editAlias(alias)"
              >
                <strong>{{ alias.canonical }}</strong>
                <small>{{ alias.aliases.join("、") || "暂无别名" }}</small>
              </button>
            </div>
            <p v-else class="muted">尚未维护别名关系</p>
            <form class="alias-form" data-testid="alias-form" @submit.prevent="saveAlias">
              <label>
                <span>规范名称</span>
                <input v-model="aliasCanonical" type="text" maxlength="256" placeholder="例如：雷电将军" />
              </label>
              <label>
                <span>别名</span>
                <input v-model="aliasInput" type="text" placeholder="例如：雷神、影" />
              </label>
              <button
                class="button secondary"
                type="submit"
                :disabled="!aliasCanonical.trim() || taskSubmissionLocked"
              >
                新增或更新
              </button>
            </form>
          </div>
        </details>

        <section class="operation-preview">
          <h3>操作预览</h3>
          <p>
            将对 <strong>{{ organize.selectedCount.value }}</strong> 张图片处理
            <strong>{{ organize.draftTags.value.length }}</strong> 个标签。
          </p>
          <div class="operation-buttons">
            <button
              class="button primary"
              type="button"
              :disabled="!organize.selectedCount.value || !organize.draftTags.value.length || taskSubmissionLocked"
              @click="submitOperation('add')"
            >
              添加标签
            </button>
            <button
              class="button secondary"
              type="button"
              :disabled="!organize.selectedCount.value || !organize.draftTags.value.length || taskSubmissionLocked"
              @click="submitOperation('remove')"
            >
              移除标签
            </button>
            <button
              class="button danger-soft"
              type="button"
              :disabled="!organize.selectedCount.value || !organize.draftTags.value.length || taskSubmissionLocked"
              @click="submitOperation('replace_manual')"
            >
              替换人工标签
            </button>
          </div>
          <small>“替换人工标签”不会删除文件夹标签或模型标签。</small>
        </section>

        <section v-if="organize.activeJob.value" class="progress-card" aria-live="polite">
          <div class="progress-heading">
            <strong>
              {{ organize.jobRunning.value
                ? (organize.activeJobKind.value === "alias" ? "正在保存别名" : "后台处理中")
                : "最近任务" }}
            </strong>
            <span>{{ organize.activeJob.value.percent }}%</span>
          </div>
          <div class="progress-track">
            <span :style="{ width: `${organize.activeJob.value.percent}%` }" />
          </div>
          <p>{{ organize.activeJob.value.message || organize.activeJob.value.status }}</p>
          <small>
            {{ organize.activeJob.value.processed }}/{{ organize.activeJob.value.total || organize.selectedCount.value }}，
            失败 {{ organize.activeJob.value.failed }}
          </small>
        </section>

        <button
          class="undo-button"
          type="button"
          :disabled="!organize.undoAvailable.value || taskSubmissionLocked"
          @click="organize.undoLastBatch"
        >
          <span aria-hidden="true">↶</span>
          <span><strong>撤销最近一次批量操作</strong><small>恢复人工标签和相关索引状态</small></span>
        </button>
        </div>
        </details>
      </aside>
    </div>

    <SimilarityGroupsPanel
      v-if="visitedWorkspaceTabs.has('clusters')"
      v-show="activeWorkspaceTab === 'clusters'"
      class="organize-intelligence-workspace"
      :api="props.api"
      :library-id="organize.selectedLibraryId.value"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
      @open-image="(imageId) => emit('openImage', imageId)"
    />

    <ActiveLearningPanel
      v-if="visitedWorkspaceTabs.has('learning')"
      v-show="activeWorkspaceTab === 'learning'"
      class="organize-intelligence-workspace"
      :api="props.api"
      :library-id="organize.selectedLibraryId.value"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
      @open-image="(imageId) => emit('openImage', imageId)"
    />

    <button
      v-if="editorOpen"
      class="editor-backdrop"
      type="button"
      aria-label="关闭标签设置"
      @click="editorOpen = false"
    />

    <div
      v-if="organize.deletePreview.value"
      class="delete-modal-backdrop"
      role="presentation"
      @click.self="organize.cancelFolderDeletion"
    >
      <section class="delete-modal" role="dialog" aria-modal="true" aria-labelledby="delete-folder-title">
        <header>
          <div>
            <p class="eyebrow">危险操作</p>
            <h2 id="delete-folder-title">清理文件夹和图库记录</h2>
          </div>
          <button type="button" aria-label="关闭" @click="organize.cancelFolderDeletion">×</button>
        </header>
        <p>
          将清理 <strong>{{ organize.deletePreview.value.folderName }}</strong>
          及全部子目录。图库根目录本身和图库绑定会保留。
        </p>
        <dl>
          <div><dt>入库图片</dt><dd>{{ organize.deletePreview.value.imageCount }} 张</dd></div>
          <div><dt>磁盘文件</dt><dd>{{ organize.deletePreview.value.fileCount }} 个</dd></div>
          <div><dt>文件大小</dt><dd>{{ formatBytes(organize.deletePreview.value.sizeBytes) }}</dd></div>
          <div><dt>已变化/缺失</dt><dd>{{ organize.deletePreview.value.changedCount + organize.deletePreview.value.missingCount }}</dd></div>
        </dl>
        <ul v-if="organize.deletePreview.value.warnings.length">
          <li v-for="warning in organize.deletePreview.value.warnings" :key="warning">{{ warning }}</li>
        </ul>
        <p v-if="organize.deletePreview.value.protectedCount" class="delete-blocked" role="alert">
          检测到 {{ organize.deletePreview.value.protectedCount }} 个受保护项目，当前操作已阻止。
        </p>
        <label>
          <span>输入“{{ organize.deletePreview.value.confirmationPhrase }}”确认</span>
          <input v-model="deleteConfirmation" type="text" autocomplete="off" />
        </label>
        <footer>
          <button class="button secondary" type="button" @click="organize.cancelFolderDeletion">取消</button>
          <button
            class="button danger-soft"
            type="button"
            :disabled="organize.deletePreview.value.blocked || organize.deleteCommitLoading.value"
            @click="confirmFolderDeletion"
          >
            {{ organize.deleteCommitLoading.value ? "正在提交…" : "清理文件夹和索引" }}
          </button>
        </footer>
      </section>
    </div>
  </section>
</template>

<style scoped>
.organize-page { display: grid; height: 100%; min-height: 0; grid-template-rows: auto auto minmax(0,1fr); gap: 12px; overflow: hidden; color: #17203a; }
.organize-page.embedded-navigation { grid-template-rows:minmax(0,1fr); gap:0; }
.page-heading, .heading-actions, .panel-heading, .gallery-heading, .selection-toolbar, .selection-actions, .pagination-bar, .progress-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-heading { align-items: flex-end; padding: 2px 2px 4px; }
.page-heading h1 { margin: 2px 0 0; font-size: clamp(23px, 2vw, 31px); line-height: 1.15; }
.page-heading p { margin: 7px 0 0; color: #68718a; }
.eyebrow { margin: 0 !important; color: #6557e8 !important; font-size: 11px; font-weight: 850; letter-spacing: .09em; text-transform: uppercase; }
.panel { min-width: 0; min-height: 0; border: 1px solid #e2e6f0; border-radius: 17px; background: rgba(255,255,255,.97); box-shadow: 0 10px 28px rgba(44,54,90,.075); }
.heading-actions label { display: grid; gap: 5px; color: #626b82; font-size: 12px; font-weight: 750; }
input, select, textarea { min-width: 0; border: 1px solid #d9deea; border-radius: 10px; background: #fbfcff; color: #17203a; padding: 9px 10px; font: inherit; outline: none; }
input:focus, select:focus, textarea:focus { border-color: #796cf0; box-shadow: 0 0 0 3px rgba(121,108,240,.12); }
textarea { resize: vertical; line-height: 1.5; }
.heading-actions select { min-width: 190px; }
.button { border: 0; border-radius: 10px; padding: 9px 13px; font: inherit; font-weight: 780; cursor: pointer; transition: transform .12s ease, opacity .12s ease; }
.button:hover:not(:disabled), .toolbar-button:hover:not(:disabled), .folder-item:hover, .root-button:hover { transform: translateY(-1px); }
.button:disabled, .toolbar-button:disabled, .undo-button:disabled { cursor: not-allowed; opacity: .45; }
.primary { color: white; background: linear-gradient(135deg, #7364eb, #5b4bd6); }
.secondary { color: #5647ce; background: #efedff; }
.danger-soft { color: #a74755; background: #fff0f2; }
.workspace-tabs { display: inline-flex; width: max-content; max-width: 100%; gap: 5px; padding: 4px; border: 1px solid #e1e4ee; border-radius: 13px; background: rgba(246,247,251,.94); }
.workspace-tabs button { display: grid; min-width: 150px; gap: 1px; padding: 7px 13px; border: 0; border-radius: 9px; color: #6c7589; background: transparent; text-align: left; cursor: pointer; }
.workspace-tabs button strong { font-size: 11px; }.workspace-tabs button small { font-size: 9px; opacity: .78; }
.workspace-tabs button.active { color: #5143c2; background: #fff; box-shadow: 0 3px 12px rgba(53,59,86,.1); }
.organize-intelligence-workspace { height:100%; min-height:0; }
.batch-tag-workspace { display: grid; height: 100%; min-height: 0; grid-template-columns: 320px minmax(0,1fr) 380px; gap: 14px; }
.batch-tag-workspace.is-folder-collapsed { grid-template-columns:44px minmax(0,1fr) 380px; gap:10px; }
.folder-panel { display: grid; grid-template-rows: auto auto auto auto minmax(0,1fr) auto; padding: 14px; overflow: hidden; }
.folder-panel-toggle { display:grid; width:28px; height:28px; flex:0 0 auto; place-items:center; padding:0; border:0; border-radius:8px; color:#555; font:inherit; font-size:20px; background:#ededed; }
.folder-library-control { display:grid; grid-template-columns:minmax(0,1fr) 38px; align-items:center; gap:6px; margin-top:10px; }
.folder-library-control label { min-width:0; }
.folder-library-control select { width:100%; height:38px; padding:0 8px; font-size:11px; }
.folder-library-refresh { display:grid; width:38px; height:38px; place-items:center; padding:0; border:1px solid #dedede; border-radius:9px; color:#555; font:inherit; font-size:16px; background:#f5f5f5; }
.folder-panel.is-collapsed { padding:7px; }
.folder-panel.is-collapsed .panel-heading { justify-content:center; }
.folder-panel.is-collapsed .panel-heading>div,.folder-panel.is-collapsed .panel-heading>.count-pill,.folder-panel.is-collapsed>:not(.panel-heading) { display:none; }
.tag-editor { padding: 14px; overflow: auto; }
.gallery-panel { display: grid; grid-template-rows: auto auto minmax(0,1fr) auto; padding: 14px; overflow: hidden; }
.panel-heading h2, .gallery-heading h2 { margin: 2px 0 0; font-size: 18px; }
.count-pill, .selection-count { display: grid; min-width: 31px; height: 31px; place-items: center; border-radius: 999px; color: #5b4bd6; background: #efedff; font-size: 12px; font-weight: 850; }
.root-list { display: grid; gap: 6px; margin-top: 13px; }
.root-row { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: stretch; gap: 5px; border: 1px solid transparent; border-radius: 11px; background: #f7f8fc; }
.root-row.active { border-color: #cbc4ff; background: #f0eeff; }
.root-row.direct-selected { box-shadow: inset 3px 0 #6557df; }
.root-button { display: grid; grid-template-columns: 27px minmax(0,1fr); gap: 8px; align-items: center; width: 100%; padding: 9px; border: 1px solid transparent; border-radius: 11px; background: #f7f8fc; color: inherit; text-align: left; cursor: pointer; transition: .12s ease; }
.root-row .root-button { background: transparent; }
.root-delete-button,.folder-delete-button { border: 0; border-radius: 8px; color: #9b4b59; background: transparent; font-size: 11px; font-weight: 750; cursor: pointer; }
.root-delete-button { margin: 5px; padding: 0 7px; }
.root-delete-button:hover,.folder-delete-button:hover { background: #fff0f2; }
.root-icon { color: #7466e8; }
.root-copy { display: grid; min-width: 0; gap: 2px; }
.root-copy strong, .root-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.root-copy strong { font-size: 13px; }
.root-copy small { color: #7a8297; font-size: 11px; }
.folder-search { display:grid; grid-template-columns:minmax(0,1fr) 38px; align-items:center; gap:6px; margin-top:12px; }
.folder-search input { width:100%; height:38px; }
.folder-search .icon-button { width:38px; height:38px; padding:0; }
.icon-button { border: 0; border-radius: 10px; color: white; background: #6557df; font-size: 18px; cursor: pointer; }
.folder-list { display: grid; align-content: start; gap: 6px; min-height: 0; margin-top: 10px; overflow: auto; padding-right: 3px; }
.folder-item { display: grid; grid-template-columns: minmax(0,1fr) 28px; gap: 4px; width: 100%; border: 1px solid transparent; border-radius: 11px; background: transparent; color: inherit; transition: .12s ease; }
.folder-select-button { display: grid; grid-template-columns: 22px minmax(0,1fr); gap: 8px; min-width: 0; padding: 9px; border: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; }
.folder-delete-button { margin: 5px 4px 5px 0; }
.folder-item:hover { background: #f7f8fc; }
.folder-item.active { border-color: #cfc9fb; background: linear-gradient(135deg, #f1efff, #f8f7ff); }
.folder-load-more { min-height: 34px; margin-top: 8px; border: 1px solid #dedbea; border-radius: 9px; color: #5b4bd6; background: #f6f4ff; font-size: 11px; cursor: pointer; }
.folder-glyph { color: #e0a44a; font-size: 12px; transform: rotate(45deg); }
.folder-copy { display: grid; min-width: 0; gap: 2px; }
.folder-copy strong, .folder-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.folder-copy strong { font-size: 13px; }
.folder-copy small { color: #82899b; font-size: 11px; }
.folder-stats { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 3px; }
.folder-stats span { padding: 2px 5px; border-radius: 999px; color: #667086; background: #edf0f5; font-size: 10px; }
.folder-stats .warning { color: #9a641d; background: #fff1d9; }
.gallery-heading { padding: 1px 2px 12px; border-bottom: 1px solid #edf0f5; }
.gallery-heading small { display: block; max-width: 520px; margin-top: 4px; overflow: hidden; color: #7a8397; text-overflow: ellipsis; white-space: nowrap; }
.gallery-metrics { display: grid; justify-items: end; }
.gallery-metrics strong { color: #5545d1; font-size: 23px; }
.gallery-metrics span { color: #7b8396; font-size: 11px; }
.selection-toolbar { padding:10px 14px; }
.selection-summary { display: grid; gap: 2px; }
.selection-summary strong { font-size: 13px; }
.selection-summary span { color: #7c8498; font-size: 11px; }
.selection-actions { flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.toolbar-button { border: 1px solid #dde1ec; border-radius: 8px; padding: 6px 9px; color: #515b73; background: #f8f9fc; font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; transition: .12s ease; }
.toolbar-button.quiet { color: #8a5260; background: #fff6f7; }
.gallery-viewport { min-height: 0; overflow: hidden; }
.image-grid { display: grid; height: 100%; min-height: 0; grid-template-columns: repeat(var(--organize-gallery-columns,5),minmax(0,1fr)); grid-template-rows: repeat(var(--organize-gallery-rows,3),minmax(0,1fr)); align-content: stretch; gap: 9px; overflow: hidden; padding: 2px 3px 8px 1px; }
.image-grid.is-scroll-layout { grid-auto-rows: minmax(210px,auto); grid-template-rows: none; overflow: auto; }
.image-tile { position: relative; min-width: 0; min-height: 0; overflow: hidden; border: 1px solid #e1e5ee; border-radius: 12px; background: #fff; content-visibility: auto; contain-intrinsic-size: 210px 168px; }
.image-tile.selected { border-color: #6d5ee3; box-shadow: 0 0 0 2px rgba(109,94,227,.16); }
.image-select { display: grid; width: 100%; height: 100%; padding: 0; border: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; grid-template-rows: minmax(0,1fr) auto; }
.selection-marker { position: absolute; z-index: 2; top: 7px; left: 7px; display: grid; width: 21px; height: 21px; place-items: center; border: 1px solid rgba(255,255,255,.7); border-radius: 7px; color: white; background: rgba(22,27,45,.45); font-size: 12px; font-weight: 900; backdrop-filter: blur(6px); }
.image-tile.selected .selection-marker { background: #6655df; }
.thumbnail-stage { display: grid; min-height: 0; height: 100%; place-items: center; overflow: hidden; background: linear-gradient(145deg, #151b2b, #252c3f); }
.thumbnail-stage img { width: 100%; height: 100%; object-fit: contain; }
.thumbnail-placeholder { display: grid; place-items: center; gap: 5px; color: #aab2c2; text-align: center; }
.thumbnail-placeholder > span { display: grid; width: 34px; height: 34px; place-items: center; border: 1px solid rgba(255,255,255,.1); border-radius: 11px; color: #8f99ab; background: rgba(255,255,255,.04); font-size: 17px; }
.thumbnail-placeholder small { font-size: 10px; }
.image-meta { display: grid; min-width: 0; gap: 2px; padding: 8px 8px 6px; }
.image-meta strong { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.image-meta small { color: #8990a1; font-size: 10px; }
.image-tags { display: flex; min-height: 25px; flex-wrap: wrap; gap: 3px; padding: 0 7px 7px; }
.mini-tag { max-width: 100%; overflow: hidden; border-radius: 999px; padding: 2px 5px; color: #5c51b6; background: #efedff; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.mini-tag.more { color: #697187; background: #eff1f5; }
.no-tag { color: #a0a6b5; font-size: 10px; }
.gallery-panel :deep(.gallery-pagination) { min-height: 52px; padding-top: 9px; border-top: 1px solid #edf0f5; }
.editor-toggle,.editor-close { display: none; }
.tag-editor-heading-actions { display: flex; align-items: center; gap: 7px; }
.selection-card, .operation-preview, .progress-card { margin-top: 12px; padding: 11px; border-radius: 12px; background: #f7f8fc; }
.selection-card { border: 1px solid #e5e7f0; }
.selection-card strong { font-size: 13px; }
.selection-card p, .operation-preview p, .progress-card p { margin: 5px 0 0; color: #727b90; font-size: 11px; line-height: 1.45; }
.tag-input-label { display: grid; gap: 6px; margin-top: 14px; color: #4e5870; font-size: 12px; font-weight: 780; }
.tag-input-label small, .operation-preview > small { color: #858c9d; font-size: 10px; font-weight: 500; }
.draft-tags, .tag-cloud { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.draft-tags { min-height: 31px; }
.draft-tag { border: 0; border-radius: 999px; padding: 5px 8px; color: #4f42bb; background: #eeebff; font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }
.draft-tag span { opacity: .6; }
.tag-state-section { margin-top: 14px; }
.tag-state-section header { display: flex; align-items: center; justify-content: space-between; }
.tag-state-section h3, .operation-preview h3 { margin: 0; font-size: 12px; }
.tag-state-section header span { color: #8a91a2; font-size: 10px; }
.state-tag { display: grid; gap: 1px; max-width: 100%; border-radius: 9px; padding: 5px 7px; color: #415066; background: #edf1f6; font-size: 10px; font-weight: 750; }
.state-tag small { overflow: hidden; color: inherit; opacity: .65; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.state-tag.common { color: #247252; background: #e8f7ef; }
.state-tag.partial { color: #8a611f; background: #fff3df; }
.alias-dictionary { margin-top: 14px; border: 1px solid #e3e6ef; border-radius: 12px; background: #fafbfe; }
.alias-dictionary > summary { display: flex; min-height: 46px; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; cursor: pointer; list-style: none; }
.alias-dictionary > summary::-webkit-details-marker { display: none; }
.alias-dictionary > summary > span:first-child { display: grid; min-width: 0; gap: 1px; }
.alias-dictionary > summary strong { font-size: 12px; }
.alias-dictionary > summary small { overflow: hidden; color: #858d9f; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.alias-count { display: grid; width: 24px; height: 24px; place-items: center; border-radius: 999px; color: #5a4bd0; background: #efedff; font-size: 10px; font-weight: 850; }
.alias-content { display: grid; gap: 9px; padding: 0 10px 10px; border-top: 1px solid #eceef4; }
.alias-list { display: grid; max-height: 122px; gap: 4px; margin-top: 9px; overflow: auto; }
.alias-entry { display: grid; min-width: 0; gap: 1px; padding: 6px 7px; border: 0; border-radius: 8px; color: #46516a; text-align: left; background: #f0f2f7; cursor: pointer; }
.alias-entry:hover { color: #5143c3; background: #eeebff; }
.alias-entry strong,.alias-entry small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alias-entry strong { font-size: 10px; }.alias-entry small { color: #81899b; font-size: 9px; }
.alias-form { display: grid; gap: 7px; }
.alias-form label { display: grid; gap: 4px; color: #626b82; font-size: 10px; font-weight: 750; }
.alias-form input { width: 100%; height: 35px; padding: 6px 8px; font-size: 11px; }
.alias-form .button { min-height: 35px; padding: 7px 10px; font-size: 11px; }
.operation-preview { border: 1px solid #e5e2fa; background: #faf9ff; }
.operation-buttons { display: grid; gap: 7px; margin-top: 10px; }
.progress-card { border: 1px solid #dcd7ff; background: #f4f2ff; }
.progress-heading span { color: #5a4bd0; font-size: 12px; font-weight: 850; }
.progress-track { height: 7px; margin-top: 8px; overflow: hidden; border-radius: 999px; background: #dedaf5; }
.progress-track span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #7666e7, #9c6ee8); transition: width .2s ease; }
.progress-card small { color: #737b8f; font-size: 10px; }
.undo-button { display: grid; grid-template-columns: 28px minmax(0,1fr); gap: 8px; width: 100%; margin-top: 12px; padding: 10px; border: 1px solid #dfe3ec; border-radius: 11px; color: #535d73; background: white; text-align: left; cursor: pointer; }
.undo-button > span:first-child { color: #6657dd; font-size: 22px; }
.undo-button > span:last-child { display: grid; gap: 2px; }
.undo-button strong { font-size: 11px; }
.undo-button small { color: #8990a1; font-size: 9px; }
.empty-state { display: grid; min-height: 180px; place-items: center; align-content: center; gap: 5px; color: #81899b; text-align: center; }
.empty-state span { font-size: 11px; }
.empty-state.compact { min-height: 90px; font-size: 12px; }
.gallery-empty { min-height: 390px; }
.muted { color: #999fae; font-size: 10px; }
.editor-backdrop { display: none; }
.delete-modal-backdrop { position: fixed; z-index: 45; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(16,22,38,.52); backdrop-filter: blur(4px); }
.delete-modal { width: min(620px,100%); max-height: min(760px,calc(100vh - 48px)); overflow: auto; padding: 18px; border: 1px solid #e2d7dc; border-radius: 17px; background: #fff; box-shadow: 0 24px 80px rgba(20,24,39,.28); }
.delete-modal > header,.delete-modal > footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.delete-modal > header h2 { margin: 2px 0 0; font-size: 19px; }
.delete-modal > header button { width: 32px; height: 32px; border: 0; border-radius: 9px; color: #7b5060; background: #fff0f2; font-size: 18px; }
.delete-modal > p { color: #626b80; line-height: 1.55; }
.delete-modal dl { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 8px; margin: 14px 0; }
.delete-modal dl > div { padding: 9px; border-radius: 10px; background: #f7f8fc; }
.delete-modal dt { color: #7a8295; font-size: 10px; }.delete-modal dd { margin: 2px 0 0; font-size: 13px; font-weight: 800; }
.delete-modal ul { padding-left: 20px; color: #8a5a20; font-size: 11px; }
.delete-modal label { display: grid; gap: 6px; margin: 14px 0; color: #5d667b; font-size: 12px; font-weight: 750; }
.delete-modal footer { justify-content: flex-end; }
.delete-blocked { padding: 9px 10px; border-radius: 9px; color: #9f3647 !important; background: #fff0f2; }

/* Fullscreen desktop treatment from the approved prototype. The three
   work areas are continuous white panes; controls, selection and behavior stay
   intact while the old purple dashboard framing is removed. */
.organize-page { color:#242424; }
.batch-tag-workspace { grid-template-columns:260px minmax(380px,1fr) 300px; gap:0; background:#fff; }
.batch-tag-workspace.is-folder-collapsed { grid-template-columns:0 minmax(380px,1fr) 300px; gap:0; }
.folder-panel { grid-column:1; }
.gallery-panel { grid-column:2; }
.tag-editor { grid-column:3; }
.folder-panel,.gallery-panel,.tag-editor { border:0; border-radius:0; background:#fff; box-shadow:none; }
.folder-panel { padding:14px 12px 14px 0; }
.folder-panel.is-collapsed { display:none; }
.gallery-panel { padding:14px; }
.tag-editor { padding:14px; }
.tag-editor-empty{display:grid;min-height:calc(100% - 52px);grid-template-rows:1fr auto;place-items:center;align-content:center;gap:12px;color:#888;text-align:center}.tag-editor-empty>span{align-self:end;font-size:28px;color:#aaa}.tag-editor-empty>div:not(.empty-editor-actions){align-self:start}.tag-editor-empty strong{display:block;color:#777;font-size:13px;font-weight:400}.tag-editor-empty p{margin:5px 0 0;font-size:11px;line-height:1.5}.empty-editor-actions{align-self:end;display:grid;width:100%;gap:6px}.empty-editor-actions button{min-height:32px;border:1px solid #e5e5e5;border-radius:8px;color:#aaa;background:#fafafa}
.tag-editor-tools{margin-top:8px}.tag-editor-tools>summary{padding:7px 0;color:#777;font-size:11px;text-align:center;cursor:pointer;list-style:none}.tag-editor-tools[open]>summary{margin-bottom:6px;border-bottom:1px solid #e7e7e7}.tag-editor-tools-content{display:grid}
.panel-heading .eyebrow,.gallery-heading .eyebrow,.tag-editor .eyebrow { display:none; }
.folder-panel-toggle { width:32px; height:32px; border:0; border-radius:8px; color:#666; background:transparent; }
.folder-panel-toggle:hover { color:#171717; background:#f2f2f2; }
.gallery-heading { justify-content:flex-start; }
.gallery-heading>div:first-of-type { min-width:0; flex:1; }
.gallery-metrics strong { color:#242424; font-weight:600; }
.root-row,.folder-item { border:0; border-radius:8px; background:transparent; box-shadow:none; }
.root-row:hover,.folder-item:hover,.root-row.active,.folder-item.active { border-color:transparent; color:#242424; background:#f2f2f2; box-shadow:none; }
.root-row.direct-selected { box-shadow:none; }
.root-icon,.folder-glyph { color:#777; background:transparent; }
.folder-library-control select,.folder-search input,input,select,textarea { border-color:#dedede; color:#242424; background:#fff; }
.folder-library-refresh,.folder-search .icon-button { border-color:#dedede; color:#555; background:#f5f5f5; }
.selection-toolbar { margin-bottom:10px; padding:8px 10px; border:0; border-radius:10px; background:#f7f7f7; }
.toolbar-button,.toolbar-button.quiet,.editor-toggle,.folder-load-more { border-color:#dedede; color:#333; background:#f5f5f5; font-weight:500; }
.image-grid { grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.image-tile { border:0; border-radius:10px; background:#f2f2f2; box-shadow:none; }
.image-tile:hover { transform:translateY(-2px); box-shadow:0 4px 12px -2px rgb(0 0 0 / 10%); }
.image-tile.selected { border-color:#777; outline:2px solid #777; outline-offset:-2px; box-shadow:none; }
.thumbnail-stage { background:linear-gradient(140deg,#f2f2f2,#e9e9e9); }
.mini-tag,.draft-tag,.state-tag,.alias-entry { color:#555; background:#f0f0f0; }
.selection-card,.operation-preview,.progress-card,.alias-dictionary { border-color:#e5e5e5; background:#f7f7f7; box-shadow:none; }
.button.primary { color:#fff; background:#606060; }
.button.secondary { border-color:#dedede; color:#333; background:#f3f3f3; }
.progress-track { background:#e3e3e3; }.progress-track span { background:#777; }

@media (max-width: 1180px) {
  .batch-tag-workspace { grid-template-columns: 300px minmax(0,1fr); }
  .batch-tag-workspace.is-folder-collapsed { grid-template-columns:0 minmax(0,1fr); }
  .editor-toggle { display: inline-flex; }
  .tag-editor { position: fixed; z-index: 35; top: 16px; right: 16px; bottom: 16px; display: block; width: min(390px,calc(100vw - 48px)); transform: translateX(calc(100% + 32px)); transition: transform .18s ease; box-shadow: 0 24px 70px rgba(18,24,42,.24); }
  .tag-editor.is-open { transform: translateX(0); }
  .editor-close { display: grid; width: 30px; height: 30px; place-items: center; border: 0; border-radius: 8px; color: #6f5260; background: #fff0f2; font-size: 17px; }
  .editor-backdrop { position: fixed; z-index: 34; inset: 0; display: block; border: 0; background: rgba(17,23,39,.28); }
}
@media (max-width: 1100px) {
  .organize-page { overflow: auto; }
  .batch-tag-workspace { height: auto; min-height: 0; grid-template-columns: 260px minmax(0,1fr); }
  .folder-panel,.gallery-panel { min-height: 680px; }
  .selection-toolbar { align-items: stretch; flex-direction: column; }
  .selection-actions { justify-content: flex-start; }
}
@media (max-width: 760px) {
  .page-heading, .heading-actions, .selection-toolbar { align-items: stretch; flex-direction: column; }
  .batch-tag-workspace { grid-template-columns: 1fr; }
  .folder-panel { min-height: 420px; }
  .folder-list { min-height: 220px; }
  .gallery-panel { min-height: 680px; }
  .selection-actions { justify-content: flex-start; }
  .delete-modal dl { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .workspace-tabs { display: grid; width: 100%; grid-template-columns: repeat(3,minmax(0,1fr)); }
  .workspace-tabs button { min-width: 0; padding-inline: 8px; }
}
</style>
