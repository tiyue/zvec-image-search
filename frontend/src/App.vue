<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import GalleryContextMenu from "./components/GalleryContextMenu.vue";
import GalleryGrid from "./components/GalleryGrid.vue";
import ImagePreview from "./components/ImagePreview.vue";
import PaginationBar from "./components/PaginationBar.vue";
import StatusToast from "./components/StatusToast.vue";
import { useNativeImageActions } from "./composables/useNativeImageActions";
import { useSearch } from "./composables/useSearch";
import { installGlobalDiagnostics, reportFrontendDiagnostic } from "./diagnostics";
import {
  SEARCH_RESULTS_KEEP_LATEST,
  useSearchResultsCleanup,
} from "./features/cleanup/useSearchResultsCleanup";
import { OrganizePage } from "./features/organize";
import { SettingsPage } from "./features/settings";
import type { SettingsLibrary } from "./features/settings";
import { TasksPage } from "./features/tasks";
import type {
  GalleryContextIntent,
  GallerySelectionIntent,
  ToastMessage,
} from "./types/contracts";

type PageName = "search" | "tasks" | "organize" | "settings";

const pageDefinitions: Array<{
  id: PageName;
  label: string;
  description: string;
  glyph: string;
  shortcut: `Alt+${1 | 2 | 3 | 4}`;
}> = [
  {
    id: "search",
    label: "图片搜索",
    description: "语义、图片与标签",
    glyph: "⌕",
    shortcut: "Alt+1",
  },
  {
    id: "tasks",
    label: "图库任务",
    description: "索引、同步与标注",
    glyph: "☷",
    shortcut: "Alt+2",
  },
  {
    id: "organize",
    label: "批量标签",
    description: "文件夹标签与别名",
    glyph: "✓",
    shortcut: "Alt+3",
  },
  {
    id: "settings",
    label: "设置",
    description: "图库、模型与密钥",
    glyph: "⚙",
    shortcut: "Alt+4",
  },
];

const activePage = ref<PageName>("search");
const visitedPages = ref<PageName[]>(["search"]);
const recentSearches = ref<string[]>([]);

const toasts = ref<ToastMessage[]>([]);
let toastSequence = 0;

function addToast(title: string, message: string, kind: ToastMessage["kind"]): void {
  const id = `toast-${Date.now()}-${toastSequence += 1}`;
  toasts.value = [...toasts.value, { id, title, message, kind }];
  window.setTimeout(() => removeToast(id), 4300);
}

function removeToast(id: string): void {
  toasts.value = toasts.value.filter((toast) => toast.id !== id);
}

const search = useSearch(undefined, {
  onError: (title, message) => addToast(title, message, "error"),
  onInfo: (title, message) => addToast(title, message, "info"),
  onDiagnostic: (event, details) => reportFrontendDiagnostic(event, details),
});
const nativeActions = useNativeImageActions({
  onError: (title, message) => addToast(title, message, "error"),
  onInfo: (title, message) => addToast(title, message, "info"),
});
const resultsCleanup = useSearchResultsCleanup(undefined, {
  onError: (title, message) => addToast(title, message, "error"),
  onInfo: (title, message) => addToast(title, message, "info"),
});

const searchWorkspace = ref<HTMLElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const paginationBar = ref<{ focusInput: () => void } | null>(null);
const previewVisible = ref(true);
const queryDropActive = ref(false);
const selectedImageIds = ref<string[]>([]);
const selectionAnchorId = ref("");
const contextMenu = ref({ visible: false, x: 0, y: 0, imageId: "" });
let previewMedia: MediaQueryList | null = null;
let removeGlobalDiagnostics: (() => void) | null = null;

const selectedCount = computed(() => selectedImageIds.value.length);
const activeActionIds = computed(() => {
  if (selectedImageIds.value.length > 0) return selectedImageIds.value;
  const fallback = contextMenu.value.imageId || search.selectedId.value;
  return fallback ? [fallback] : [];
});

const statusLabel = computed(() => {
  if (search.status.value === "succeeded" && search.totalItems.value === 0) {
    return "无可靠结果";
  }
  const labels: Record<string, string> = {
    idle: "等待搜索",
    running: "搜索中",
    succeeded: "已完成",
    partial: "部分完成",
    needs_attention: "需要处理",
    failed: "搜索失败",
    cancelled: "已取消",
  };
  return labels[search.status.value] ?? (search.status.value || "等待搜索");
});

const activeLibraryLabel = computed(() => {
  if (!search.libraryId.value) return `全部图库 · ${search.libraries.value.length}`;
  return (
    search.libraries.value.find((library) => library.id === search.libraryId.value)?.name ??
    "当前图库"
  );
});

async function submitSearch(): Promise<void> {
  if (search.searching.value) return;
  clearSelection();
  const recentQuery = search.query.value.trim();
  const succeeded = await search.submit();
  if (succeeded && recentQuery) {
    recentSearches.value = [
      recentQuery,
      ...recentSearches.value.filter((value) => value !== recentQuery),
    ].slice(0, 3);
  }
}

function handleSearchInputEnter(event: KeyboardEvent): void {
  if (event.isComposing) return;
  event.preventDefault();
  if (search.searching.value) return;
  void submitSearch();
}

async function changePage(target: number): Promise<void> {
  if (await search.goToPage(target)) {
    if (searchWorkspace.value) searchWorkspace.value.scrollTop = 0;
  }
}

async function chooseQueryImage(): Promise<void> {
  const selected = await nativeActions.selectQueryImage();
  if (selected) search.setQueryImage(selected.id, selected.name);
}

async function addQueryImageFile(file: File): Promise<void> {
  queryDropActive.value = false;
  const selected = await nativeActions.registerQueryImage(file);
  if (selected) search.setQueryImage(selected.id, selected.name);
}

function firstDroppedImage(files: ArrayLike<File> | null | undefined): File | null {
  if (!files?.length) return null;
  return (
    Array.from(files).find(
      (file) => file.type.startsWith("image/") || /\.(?:jpe?g|png|webp|bmp|gif|tiff?)$/iu.test(file.name),
    ) ?? null
  );
}

function clipboardImage(data: DataTransfer | null | undefined): File | null {
  const fromFiles = firstDroppedImage(data?.files);
  if (fromFiles) return fromFiles;
  const imageItem = Array.from(data?.items ?? []).find(
    (item) => item.kind === "file" && item.type.startsWith("image/"),
  );
  return imageItem?.getAsFile() ?? null;
}

function hasImageTransfer(data: DataTransfer | null | undefined): boolean {
  if (firstDroppedImage(data?.files)) return true;
  return Array.from(data?.items ?? []).some(
    (item) => item.kind === "file" && item.type.startsWith("image/"),
  );
}

function handleQueryDragOver(event: DragEvent): void {
  if (hasImageTransfer(event.dataTransfer)) {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    queryDropActive.value = true;
  }
}

function handleQueryDragLeave(event: DragEvent): void {
  const current = event.currentTarget;
  const next = event.relatedTarget;
  if (current instanceof HTMLElement && next instanceof Node && current.contains(next)) return;
  queryDropActive.value = false;
}

function handleQueryDrop(event: DragEvent): void {
  event.preventDefault();
  const file = firstDroppedImage(event.dataTransfer?.files);
  queryDropActive.value = false;
  if (!file) {
    addToast("无法添加图片", "请拖入 JPG、PNG、WebP、BMP、GIF 或 TIFF 图片。", "error");
    return;
  }
  void addQueryImageFile(file);
}

function handleQueryPaste(event: ClipboardEvent): void {
  const file = clipboardImage(event.clipboardData);
  if (!file) return;
  event.preventDefault();
  void addQueryImageFile(file);
}

function handleGallerySelection(intent: GallerySelectionIntent): void {
  const pageItems = search.items.value;
  const current = new Set(selectedImageIds.value);
  if (intent.range && selectionAnchorId.value) {
    const anchorIndex = pageItems.findIndex((item) => item.id === selectionAnchorId.value);
    if (anchorIndex >= 0) {
      const start = Math.min(anchorIndex, intent.index);
      const end = Math.max(anchorIndex, intent.index);
      const rangeIds = pageItems.slice(start, end + 1).map((item) => item.id);
      selectedImageIds.value = intent.additive
        ? [...new Set([...current, ...rangeIds])]
        : rangeIds;
    } else {
      selectedImageIds.value = [intent.id];
      selectionAnchorId.value = intent.id;
    }
  } else if (intent.additive) {
    if (current.has(intent.id)) current.delete(intent.id);
    else current.add(intent.id);
    selectedImageIds.value = [...current];
    selectionAnchorId.value = intent.id;
  } else {
    selectedImageIds.value = [intent.id];
    selectionAnchorId.value = intent.id;
  }
  search.select(intent.id, true);
  closeContextMenu();
}

function selectCurrentPage(): void {
  if (search.items.value.length === 0) return;
  selectedImageIds.value = [
    ...new Set([...selectedImageIds.value, ...search.items.value.map((item) => item.id)]),
  ];
  selectionAnchorId.value ||= search.items.value[0]?.id ?? "";
}

function clearSelection(): void {
  selectedImageIds.value = [];
  selectionAnchorId.value = "";
  closeContextMenu();
}

function openContextMenu(intent: GalleryContextIntent): void {
  if (!selectedImageIds.value.includes(intent.id)) {
    selectedImageIds.value = [intent.id];
    selectionAnchorId.value = intent.id;
  }
  search.select(intent.id, true);
  contextMenu.value = {
    visible: true,
    imageId: intent.id,
    x: Math.max(8, Math.min(intent.clientX, window.innerWidth - 250)),
    y: Math.max(8, Math.min(intent.clientY, window.innerHeight - 390)),
  };
}

function closeContextMenu(): void {
  contextMenu.value = { ...contextMenu.value, visible: false };
}

function contextImageId(): string {
  return contextMenu.value.imageId || activeActionIds.value[0] || "";
}

function copySelectedImage(): void {
  const imageId = activeActionIds.value.length === 1 ? activeActionIds.value[0] : "";
  if (imageId) void nativeActions.copyImage(imageId);
}

function handleWindowPointerDown(event: PointerEvent): void {
  const target = event.target;
  if (target instanceof Element && target.closest(".gallery-context-menu")) return;
  closeContextMenu();
}

function handleWindowResize(): void {
  closeContextMenu();
}

function setPage(page: PageName): void {
  activePage.value = page;
  if (!visitedPages.value.includes(page)) {
    visitedPages.value = [...visitedPages.value, page];
  }
}

function focusSearch(selectText = false): void {
  setPage("search");
  void nextTick(() => {
    searchInput.value?.focus({ preventScroll: true });
    if (selectText) searchInput.value?.select();
  });
}

function newSearch(): void {
  search.query.value = "";
  search.setQueryImage();
  clearSelection();
  focusSearch();
}

async function addQueryImageFromSidebar(): Promise<void> {
  setPage("search");
  search.mode.value = "combined";
  await nextTick();
  await chooseQueryImage();
}

function reuseSearch(value: string): void {
  search.query.value = value;
  focusSearch(true);
}

function handleFeatureToast(
  title: string,
  message: string,
  kind: ToastMessage["kind"],
): void {
  addToast(title, message, kind);
}

function handleLibrariesUpdated(libraries: SettingsLibrary[]): void {
  search.libraries.value = libraries.map((library) => ({
    id: library.id,
    name: library.name,
    enabled: library.enabled,
    is_default: library.isDefault,
  }));
  if (
    search.libraryId.value &&
    !search.libraries.value.some(
      (library) => library.id === search.libraryId.value && library.enabled !== false,
    )
  ) {
    search.libraryId.value = "";
  }
}

function handleGlobalKeydown(event: KeyboardEvent): void {
  // Use the physical digit code as a fallback so Alt shortcuts work under
  // keyboard layouts that transform event.key. Ignore AltGr (Ctrl+Alt).
  const shortcutKey = /^Digit([1-4])$/u.exec(event.code)?.[1] ?? event.key;
  if (
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    ["1", "2", "3", "4"].includes(shortcutKey)
  ) {
    event.preventDefault();
    const page = pageDefinitions[Number(shortcutKey) - 1]?.id;
    if (page) setPage(page);
    return;
  }

  const target = event.target;
  const editing =
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    (target instanceof HTMLElement && target.isContentEditable);
  const commandSearch =
    (event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k";
  const commandPageJump =
    (event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "g";
  const commandSelectPage =
    (event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "a";
  if (commandSelectPage && activePage.value === "search" && !editing) {
    event.preventDefault();
    selectCurrentPage();
  } else if (commandPageJump) {
    event.preventDefault();
    setPage("search");
    void nextTick(() => paginationBar.value?.focusInput());
  } else if (commandSearch) {
    event.preventDefault();
    focusSearch(true);
  } else if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    focusSearch();
  } else if (event.key === "Escape" && contextMenu.value.visible) {
    closeContextMenu();
  } else if (event.key === "Escape" && activePage.value === "search" && search.searching.value) {
    void search.cancel();
  }
}

function updatePreviewVisibility(event?: MediaQueryListEvent): void {
  previewVisible.value = event ? event.matches : Boolean(previewMedia?.matches);
}

onMounted(() => {
  removeGlobalDiagnostics = installGlobalDiagnostics();
  previewMedia = window.matchMedia("(min-width: 1201px)");
  updatePreviewVisibility();
  previewMedia.addEventListener("change", updatePreviewVisibility);
  window.addEventListener("keydown", handleGlobalKeydown);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
  void search.initialize();
});

onBeforeUnmount(() => {
  removeGlobalDiagnostics?.();
  removeGlobalDiagnostics = null;
  previewMedia?.removeEventListener("change", updatePreviewVisibility);
  window.removeEventListener("keydown", handleGlobalKeydown);
  window.removeEventListener("pointerdown", handleWindowPointerDown);
  window.removeEventListener("resize", handleWindowResize);
  search.dispose();
});

watch(
  () => search.page.value,
  () => closeContextMenu(),
);
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主导航">
      <div class="brand-block">
        <span class="brand-mark" aria-hidden="true">▧</span>
        <span><strong>Zvec</strong><small>智能图片库</small></span>
      </div>
      <nav class="nav-list">
        <button
          v-for="page in pageDefinitions"
          :key="page.id"
          class="nav-item"
          :class="{ 'is-active': activePage === page.id }"
          type="button"
          :aria-current="activePage === page.id ? 'page' : undefined"
          :aria-keyshortcuts="page.shortcut"
          :data-page="page.id"
          :title="`${page.label}（${page.shortcut}）`"
          @click="setPage(page.id)"
        >
          <span class="nav-glyph" aria-hidden="true">{{ page.glyph }}</span>
          <span><strong>{{ page.label }}</strong><small>{{ page.description }}</small></span>
        </button>
      </nav>
      <div class="sidebar-content">
        <section class="sidebar-section sidebar-quick-actions" aria-labelledby="quick-actions-title">
          <p id="quick-actions-title">快捷操作</p>
          <button type="button" @click="newSearch">
            <span aria-hidden="true">＋</span>
            <strong>新建搜索</strong>
          </button>
          <button type="button" @click="addQueryImageFromSidebar">
            <span aria-hidden="true">▧</span>
            <strong>添加查询图片</strong>
          </button>
          <button
            class="cleanup-results-action"
            data-testid="cleanup-search-results"
            type="button"
            :disabled="resultsCleanup.running.value"
            :aria-busy="resultsCleanup.running.value"
            @click="resultsCleanup.submit"
          >
            <span aria-hidden="true">⌫</span>
            <span class="quick-action-copy">
              <strong>{{ resultsCleanup.running.value ? "正在清理" : "清理搜索结果" }}</strong>
              <small>默认保留最近 {{ SEARCH_RESULTS_KEEP_LATEST }} 次</small>
            </span>
          </button>
          <output
            v-if="resultsCleanup.job.value"
            class="sidebar-cleanup-status"
            data-testid="cleanup-search-results-status"
            aria-live="polite"
            aria-atomic="true"
          >
            <span class="cleanup-status-heading">
              <strong>{{ resultsCleanup.statusLabel.value }}</strong>
              <small>{{ resultsCleanup.job.value.progressPercent }}%</small>
            </span>
            <progress
              :value="resultsCleanup.job.value.progressPercent"
              max="100"
              aria-label="清理搜索结果进度"
            />
            <span class="cleanup-counts">
              <small>删除 {{ resultsCleanup.job.value.deleted }}</small>
              <small>跳过 {{ resultsCleanup.job.value.skipped }}</small>
              <small>失败 {{ resultsCleanup.job.value.failed }}</small>
            </span>
          </output>
        </section>

        <section class="sidebar-section sidebar-overview" aria-labelledby="work-overview-title">
          <p id="work-overview-title">工作概览</p>
          <dl>
            <div><dt>图库</dt><dd :title="activeLibraryLabel">{{ activeLibraryLabel }}</dd></div>
            <div><dt>结果</dt><dd>{{ search.totalItems.value.toLocaleString("zh-CN") }} 张</dd></div>
            <div><dt>状态</dt><dd>{{ statusLabel }}</dd></div>
          </dl>
        </section>

        <section v-if="recentSearches.length" class="sidebar-section sidebar-recents" aria-labelledby="recent-title">
          <p id="recent-title">最近搜索</p>
          <button
            v-for="recent in recentSearches"
            :key="recent"
            type="button"
            :title="recent"
            @click="reuseSearch(recent)"
          >
            {{ recent }}
          </button>
        </section>
      </div>
      <div class="sidebar-footer" aria-label="本地服务状态">
        <span class="local-dot" aria-hidden="true" />
        <span><strong>{{ search.connectionMessage.value }}</strong><small>{{ activeLibraryLabel }}</small></span>
      </div>
    </aside>

    <main class="page-host">
      <section v-show="activePage === 'search'" class="search-page" data-page-section="search">
        <form class="search-toolbar card" @submit.prevent>
          <label class="search-field">
            <span class="search-icon" aria-hidden="true">⌕</span>
            <span class="sr-only">搜索内容</span>
            <input
              ref="searchInput"
              v-model="search.query.value"
              aria-keyshortcuts="Control+K Meta+K /"
              type="search"
              maxlength="4096"
              autocomplete="off"
              placeholder="输入人物、角色、作品、动作、神态或标签"
              @keydown.enter="handleSearchInputEnter"
            />
          </label>
          <label class="compact-field">
            <span>本次搜索数量</span>
            <span class="number-field-with-unit">
              <input
                v-model="search.resultLimit.value"
                aria-label="本次搜索图片数量"
                type="number"
                min="1"
                step="1"
                inputmode="numeric"
                title="输入本次搜索需要返回的图片总数，不设固定上限"
              />
              <span aria-hidden="true">张</span>
            </span>
          </label>
          <label class="compact-field">
            <span>搜索方式</span>
            <select v-model="search.mode.value">
              <option value="semantic">语义搜索</option>
              <option value="tags">标签搜索</option>
              <option value="combined">图文联合</option>
            </select>
          </label>
          <label class="compact-field">
            <span>图库</span>
            <select v-model="search.libraryId.value">
              <option value="">全部图库</option>
              <option v-for="library in search.libraries.value" :key="library.id" :value="library.id">
                {{ library.name }}
              </option>
            </select>
          </label>
          <div class="search-actions">
            <output
              class="selection-count toolbar-selection-count"
              aria-live="polite"
              aria-atomic="true"
              :aria-label="`已选择 ${selectedCount} 张图片`"
            >
              <span class="selection-count-dot" aria-hidden="true" />
              <span>已选择 <strong>{{ selectedCount }}</strong> 张</span>
            </output>
            <button
              class="button button-primary"
              type="button"
              data-testid="submit-search"
              :disabled="search.searching.value"
              @click="submitSearch"
            >
              {{ search.searching.value ? "搜索中" : "开始搜索" }}
            </button>
            <button
              v-if="search.searching.value"
              class="button button-quiet"
              type="button"
              @click="search.cancel"
            >
              取消
            </button>
          </div>
          <div
            v-if="search.mode.value === 'combined'"
            class="query-image-control"
            :class="{ 'is-drag-active': queryDropActive }"
            tabindex="0"
            aria-label="查询图片拖放或粘贴区域"
            @dragenter.prevent="queryDropActive = true"
            @dragover="handleQueryDragOver"
            @dragleave="handleQueryDragLeave"
            @drop="handleQueryDrop"
            @paste="handleQueryPaste"
          >
            <span class="query-drop-icon" aria-hidden="true">＋</span>
            <span class="query-drop-copy">
              <strong>查询图片</strong>
              <small>拖动图片到这里，或点击此区域后按 Ctrl+V 粘贴</small>
            </span>
            <button class="button button-secondary button-small" type="button" @click="chooseQueryImage">
              {{ search.queryImageId.value ? "更换图片" : "选择图片" }}
            </button>
            <span v-if="search.queryImageId.value" class="query-image-chip">
              {{ search.queryImageName.value }}
              <button type="button" aria-label="移除查询图片" @click="search.setQueryImage()">×</button>
            </span>
          </div>
        </form>

        <div ref="searchWorkspace" class="search-workspace">
          <section class="gallery-panel card" aria-labelledby="gallery-title">
            <header class="panel-heading">
              <div>
                <p class="eyebrow">搜索结果</p>
                <h2 id="gallery-title">
                  相关图片 · {{ search.totalItems.value.toLocaleString("zh-CN") }} 张
                </h2>
              </div>
              <div class="panel-heading-actions">
                <output
                  class="status-pill"
                  role="status"
                  aria-live="polite"
                  aria-atomic="true"
                  :title="search.message.value"
                >
                  {{ statusLabel }}
                  <span class="sr-only">。{{ search.title.value }}。{{ search.message.value }}</span>
                </output>
              </div>
            </header>
            <div class="gallery-operations">
              <div v-if="selectedCount" class="selection-toolbar" aria-label="批量图片操作">
                <span>Ctrl 点击多选，Shift 点击连续选择，Ctrl+A 选择本页</span>
                <button class="button button-quiet button-small" type="button" @click="nativeActions.copyFiles(activeActionIds)">
                  复制文件
                </button>
                <button
                  class="button button-secondary button-small"
                  type="button"
                  :disabled="nativeActions.exporting.value"
                  @click="nativeActions.exportImages(activeActionIds)"
                >
                  {{ nativeActions.exporting.value ? "导出中…" : "导出所选" }}
                </button>
                <button class="button button-quiet button-small" type="button" @click="clearSelection">
                  清除
                </button>
              </div>
              <div v-if="nativeActions.exporting.value" class="export-progress" role="status">
                {{ nativeActions.exportJob.value?.message }}
              </div>
            </div>
            <GalleryGrid
              :items="search.items.value"
              :selected-id="search.selectedId.value"
              :selected-ids="selectedImageIds"
              :loading="search.pageLoading.value"
              :loading-text="`正在读取第 ${search.page.value} 页`"
              @select="handleGallerySelection"
              @open="nativeActions.open"
              @context="openContextMenu"
            />
            <PaginationBar
              ref="paginationBar"
              :page="search.page.value"
              :total-pages="search.totalPages.value"
              :total-items="search.totalItems.value"
              :loading="search.pageLoading.value"
              :has-previous="search.hasPrevious.value"
              :has-next="search.hasNext.value"
              @previous="changePage(search.page.value - 1)"
              @next="changePage(search.page.value + 1)"
              @jump="changePage"
            />
          </section>
          <ImagePreview
            v-if="previewVisible"
            class="preview-card card"
            :item="search.selectedItem.value"
            :high-resolution="search.highResolutionPreview.value"
            :preview-visible="previewVisible"
            @open="nativeActions.open"
            @reveal="nativeActions.reveal"
          />
        </div>
      </section>

      <div
        v-if="visitedPages.includes('tasks')"
        v-show="activePage === 'tasks'"
        class="feature-page"
        data-page-section="tasks"
      >
        <TasksPage
          :libraries="search.libraries.value"
          :visible="activePage === 'tasks'"
          @toast="handleFeatureToast"
        />
      </div>

      <div
        v-if="visitedPages.includes('organize')"
        v-show="activePage === 'organize'"
        class="feature-page"
        data-page-section="organize"
      >
        <OrganizePage
          @toast="handleFeatureToast"
          @open-image="nativeActions.open"
        />
      </div>

      <div
        v-if="visitedPages.includes('settings')"
        v-show="activePage === 'settings'"
        class="feature-page"
        data-page-section="settings"
      >
        <SettingsPage
          @toast="handleFeatureToast"
          @libraries-updated="handleLibrariesUpdated"
        />
      </div>
    </main>

    <div class="toast-region" aria-live="polite">
      <StatusToast v-for="toast in toasts" :key="toast.id" :toast="toast" @close="removeToast" />
    </div>

    <GalleryContextMenu
      :visible="contextMenu.visible"
      :x="contextMenu.x"
      :y="contextMenu.y"
      :selection-count="activeActionIds.length"
      :exporting="nativeActions.exporting.value"
      @open="nativeActions.open(contextImageId())"
      @reveal="nativeActions.reveal(contextImageId())"
      @copy-image="copySelectedImage"
      @copy-files="nativeActions.copyFiles(activeActionIds)"
      @copy-paths="nativeActions.copyPaths(activeActionIds)"
      @export="nativeActions.exportImages(activeActionIds)"
      @clear="clearSelection"
      @close="closeContextMenu"
    />
  </div>
</template>
