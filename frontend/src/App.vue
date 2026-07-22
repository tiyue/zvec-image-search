<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import GalleryContextMenu from "./components/GalleryContextMenu.vue";
import GalleryGrid from "./components/GalleryGrid.vue";
import ImagePreview from "./components/ImagePreview.vue";
import PaginationBar from "./components/PaginationBar.vue";
import StatusToast from "./components/StatusToast.vue";
import AppIcon from "./components/AppIcon.vue";
import { useNativeImageActions } from "./composables/useNativeImageActions";
import { useSearch } from "./composables/useSearch";
import { installGlobalDiagnostics, reportFrontendDiagnostic } from "./diagnostics";
import { OrganizePage } from "./features/organize";
import { canRecordFeedback, useSearchFeedback } from "./features/search-learning";
import { SettingsPage } from "./features/settings";
import type { SettingsLibrary } from "./features/settings";
import { TasksPage } from "./features/tasks";
import type {
  GalleryContextIntent,
  GallerySelectionIntent,
  SearchResultItem,
  ToastMessage,
} from "./types/contracts";

type PageName = "search" | "tasks" | "batch" | "groups" | "learning" | "settings";

const pageDefinitions: Array<{
  id: PageName;
  label: string;
  description: string;
  icon: "search" | "tasks" | "tags" | "groups" | "learning";
  shortcut: `Alt+${1 | 2 | 3 | 4 | 5}`;
}> = [
  {
    id: "search",
    label: "图片搜索",
    description: "语义、图片与标签",
    icon: "search",
    shortcut: "Alt+1",
  },
  {
    id: "tasks",
    label: "图库任务",
    description: "索引、同步与标注",
    icon: "tasks",
    shortcut: "Alt+2",
  },
  {
    id: "batch",
    label: "批量标签",
    description: "添加、移除与替换",
    icon: "tags",
    shortcut: "Alt+3",
  },
  {
    id: "groups",
    label: "相似分组",
    description: "重复图与近似图",
    icon: "groups",
    shortcut: "Alt+4",
  },
  {
    id: "learning",
    label: "待学习样本",
    description: "审核搜索与分组反馈",
    icon: "learning",
    shortcut: "Alt+5",
  },
];

const activePage = ref<PageName>("search");
const visitedPages = ref<PageName[]>(["search"]);
const recentSearches = ref<string[]>([]);
const sidebarCollapsed = ref(false);
const searchHasRun = ref(false);

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
const searchFeedback = useSearchFeedback();

const searchWorkspace = ref<HTMLElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const paginationBar = ref<{ focusInput: () => void } | null>(null);
const previewVisible = ref(false);
const queryDropActive = ref(false);
const modeMenuOpen = ref(false);
const selectedImageIds = ref<string[]>([]);
const selectionAnchorId = ref("");
const contextMenu = ref({ visible: false, x: 0, y: 0, imageId: "" });
const knownSearchItems = new Map<string, SearchResultItem>();
let knownFeedbackSessions = "";
let removeGlobalDiagnostics: (() => void) | null = null;

const selectedCount = computed(() => selectedImageIds.value.length);
const hasSearchActivity = computed(
  () => searchHasRun.value || search.searching.value,
);
const canSubmitSearch = computed(() => {
  if (search.searching.value) return false;
  if (search.mode.value === "tags") return Boolean(search.query.value.trim());
  return Boolean(search.query.value.trim() || search.queryImageId.value);
});
const inferredSearchLabel = computed(() => {
  if (search.mode.value === "tags") return "标签搜索";
  if (search.queryImageId.value && search.query.value.trim()) return "图文组合";
  if (search.queryImageId.value) return "以图搜图";
  return "语义搜索";
});
const organizeTab = computed<"batch" | "clusters" | "learning">(() => {
  if (activePage.value === "groups") return "clusters";
  if (activePage.value === "learning") return "learning";
  return "batch";
});
const organizeVisited = computed(() =>
  visitedPages.value.some((page) => page === "batch" || page === "groups" || page === "learning"),
);
const activeActionIds = computed(() => {
  if (selectedImageIds.value.length > 0) return selectedImageIds.value;
  const fallback = contextMenu.value.imageId || search.selectedId.value;
  return fallback ? [fallback] : [];
});
const contextFeedbackItem = computed(() => searchItemForId(contextImageId()));
const contextFeedbackEvent = computed(() =>
  searchFeedback.feedbackFor(contextFeedbackItem.value),
);
const contextFeedbackAvailable = computed(() =>
  searchFeedback.enabled.value &&
  Boolean(contextFeedbackItem.value && canRecordFeedback(contextFeedbackItem.value)),
);

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
  searchHasRun.value = true;
  clearSelection();
  const recentQuery = search.query.value.trim();
  const succeeded = await search.submit();
  if (succeeded && recentQuery) {
    recentSearches.value = [
      recentQuery,
      ...recentSearches.value.filter((value) => value !== recentQuery),
    ].slice(0, 3);
    window.localStorage.setItem("zvec.recent-searches", JSON.stringify(recentSearches.value));
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
  recordImplicitFeedback([intent.id], "detail", "gallery_selection");
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

function openImageDetails(): void {
  const imageId = contextImageId();
  if (!imageId) return;
  search.select(imageId, true);
  previewVisible.value = true;
  search.setPreviewEnabled(true);
  closeContextMenu();
}

function closeImageDetails(): void {
  previewVisible.value = false;
  search.setPreviewEnabled(false);
}

function contextImageId(): string {
  return contextMenu.value.imageId || activeActionIds.value[0] || "";
}

function searchItemForId(imageId: string): SearchResultItem | null {
  if (!imageId) return null;
  return (
    search.items.value.find((item) => item.id === imageId) ??
    knownSearchItems.get(imageId) ??
    null
  );
}

function recordImplicitFeedback(
  imageIds: string[],
  action: "export" | "copy" | "open" | "detail",
  source: string,
): void {
  for (const imageId of new Set(imageIds.filter(Boolean))) {
    const item = searchItemForId(imageId);
    if (item) void searchFeedback.recordImplicit(item, action, source);
  }
}

async function openSearchImage(imageId: string): Promise<void> {
  if (await nativeActions.open(imageId)) {
    recordImplicitFeedback([imageId], "open", "gallery_open");
  }
}

async function copySelectedImage(): Promise<void> {
  const imageId = activeActionIds.value.length === 1 ? activeActionIds.value[0] : "";
  if (imageId && await nativeActions.copyImage(imageId)) {
    recordImplicitFeedback([imageId], "copy", "context_menu_copy_image");
  }
}

async function copySelectedFiles(imageIds: string[], source: string): Promise<void> {
  const ids = [...imageIds];
  if (await nativeActions.copyFiles(ids)) {
    recordImplicitFeedback(ids, "copy", source);
  }
}

async function copySelectedPaths(imageIds: string[]): Promise<void> {
  const ids = [...imageIds];
  if (await nativeActions.copyPaths(ids)) {
    recordImplicitFeedback(ids, "copy", "context_menu_copy_paths");
  }
}

async function exportSelectedImages(imageIds: string[], source: string): Promise<void> {
  const ids = [...imageIds];
  if (await nativeActions.exportImages(ids)) {
    recordImplicitFeedback(ids, "export", source);
  }
}

async function markContextFeedback(action: "relevant" | "not_relevant"): Promise<void> {
  const item = contextFeedbackItem.value;
  if (!item) return;
  const saved = action === "relevant"
    ? await searchFeedback.markRelevant(item)
    : await searchFeedback.markNotRelevant(item);
  if (saved) {
    addToast(
      "反馈已保存",
      action === "relevant" ? "已标记为相关。" : "已标记为不相关。",
      "success",
    );
  } else if (searchFeedback.lastError.value) {
    addToast("反馈未保存", searchFeedback.lastError.value, "error");
  }
}

async function undoContextFeedback(): Promise<void> {
  const item = contextFeedbackItem.value;
  if (!item) return;
  if (await searchFeedback.undo(item)) {
    addToast("已撤销反馈", "这张图片恢复为未判断状态。", "info");
  } else if (searchFeedback.lastError.value) {
    addToast("无法撤销反馈", searchFeedback.lastError.value, "error");
  }
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

watch(
  () => search.mode.value,
  (next) => {
    if (next === "tags") {
      search.setQueryImage();
    }
  },
);

function selectSearchMode(mode: "semantic" | "tags"): void {
  search.mode.value = mode;
  modeMenuOpen.value = false;
}

function newSearch(): void {
  search.query.value = "";
  search.setQueryImage();
  clearSelection();
  modeMenuOpen.value = false;
  searchHasRun.value = false;
  focusSearch();
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
  const shortcutKey = /^Digit([1-5])$/u.exec(event.code)?.[1] ?? event.key;
  if (
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    ["1", "2", "3", "4", "5"].includes(shortcutKey)
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
  } else if (event.key === "Escape" && previewVisible.value) {
    closeImageDetails();
  } else if (event.key === "Escape" && contextMenu.value.visible) {
    closeContextMenu();
  } else if (event.key === "Escape" && activePage.value === "search" && search.searching.value) {
    void search.cancel();
  }
}

onMounted(() => {
  removeGlobalDiagnostics = installGlobalDiagnostics();
  search.setPreviewEnabled(false);
  try {
    const saved = JSON.parse(window.localStorage.getItem("zvec.recent-searches") ?? "[]");
    if (Array.isArray(saved)) {
      recentSearches.value = saved.filter((value): value is string => typeof value === "string").slice(0, 12);
    }
  } catch {
    recentSearches.value = [];
  }
  window.addEventListener("keydown", handleGlobalKeydown);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
  void search.initialize();
  void searchFeedback.initialize();
});

onBeforeUnmount(() => {
  removeGlobalDiagnostics?.();
  removeGlobalDiagnostics = null;
  window.removeEventListener("keydown", handleGlobalKeydown);
  window.removeEventListener("pointerdown", handleWindowPointerDown);
  window.removeEventListener("resize", handleWindowResize);
  search.dispose();
});

watch(
  () => search.page.value,
  () => closeContextMenu(),
);

watch(
  () => search.items.value,
  (items) => {
    const sessions = [...new Set(
      items
        .map((item) => item.searchSessionId)
        .filter((sessionId) => sessionId && sessionId !== "latest"),
    )].sort();
    const sessionSignature = sessions.join("\u0000");
    if (sessionSignature && sessionSignature !== knownFeedbackSessions) {
      knownSearchItems.clear();
      knownFeedbackSessions = sessionSignature;
    } else if (items.length > 0 && !sessionSignature) {
      knownSearchItems.clear();
      knownFeedbackSessions = "";
    }
    items.forEach((item) => knownSearchItems.set(item.id, item));
    sessions.forEach((sessionId) => void searchFeedback.loadSession(sessionId));
  },
  { immediate: true },
);
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <aside class="sidebar" aria-label="主导航">
      <div class="sidebar-head">
        <button
          class="brand-mark"
          type="button"
          :aria-label="sidebarCollapsed ? '展开侧边栏' : 'Zvec'"
          :title="sidebarCollapsed ? '展开侧边栏' : 'Zvec'"
          @click="sidebarCollapsed = false"
        >
          <span aria-hidden="true">Z</span>
        </button>
        <span class="brand-copy"><strong class="brand-name">Zvec</strong><small>智能图片库</small></span>
        <button class="icon-button sidebar-toggle" type="button" :aria-label="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'" @click="sidebarCollapsed = !sidebarCollapsed">
          <AppIcon name="panel" />
        </button>
      </div>

      <button class="new-search-button" type="button" title="新建搜索" @click="newSearch">
        <AppIcon name="compose" />
        <span>新建搜索</span>
      </button>

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
          <AppIcon :name="page.icon" />
          <span><strong>{{ page.label }}</strong><small>{{ page.description }}</small></span>
        </button>
      </nav>

      <section v-if="recentSearches.length" class="sidebar-recents" aria-labelledby="recent-title">
        <p id="recent-title">最近结果</p>
        <button v-for="recent in recentSearches" :key="recent" type="button" :title="recent" @click="reuseSearch(recent)">
          {{ recent }}
        </button>
      </section>

      <button class="settings-button" type="button" :class="{ 'is-active': activePage === 'settings' }" title="设置" @click="setPage('settings')">
        <AppIcon name="settings" />
        <span><strong>设置</strong><small>图库、模型与访问</small></span>
      </button>
    </aside>

    <main class="page-host">
      <header class="app-topbar">
        <div v-if="activePage === 'search' || activePage === 'tasks'" class="top-switch" aria-label="主要视图">
          <button type="button" :class="{ active: activePage === 'search' }" @click="setPage('search')">搜索</button>
          <button type="button" :class="{ active: activePage === 'tasks' }" @click="setPage('tasks')">图库任务</button>
        </div>
        <button class="icon-button help-button" type="button" aria-label="帮助" title="快捷键：Ctrl+K 搜索，Alt+1–5 切换模块">
          <AppIcon name="help" :size="17" />
        </button>
      </header>

      <section v-show="activePage === 'search'" class="search-page" :class="{ 'is-landing': !hasSearchActivity }" data-page-section="search">
        <div v-if="!hasSearchActivity" class="search-surface">
          <div class="search-hero">
            <h1>你今天想找什么？</h1>
          </div>

          <form
            class="search-composer-wrap"
            @submit.prevent="submitSearch"
            @dragenter.prevent="queryDropActive = true"
            @dragover="handleQueryDragOver"
            @dragleave="handleQueryDragLeave"
            @drop="handleQueryDrop"
            @paste="handleQueryPaste"
          >
            <div class="search-composer" :class="{ 'is-drag-active': queryDropActive }">
              <button v-if="search.mode.value !== 'tags'" class="composer-icon-button" type="button" aria-label="选择查询图片" title="选择查询图片" @click="chooseQueryImage">
                <AppIcon name="plus" />
              </button>
              <AppIcon v-else class="composer-leading-icon" name="tags" />
              <input
                ref="searchInput"
                v-model="search.query.value"
                aria-label="搜索内容"
                aria-keyshortcuts="Control+K Meta+K /"
                type="search"
                maxlength="4096"
                autocomplete="off"
                :placeholder="search.mode.value === 'tags' ? '输入标签，多个标签用空格分隔' : '搜索人物、场景、动作或作品'"
                @keydown.enter="handleSearchInputEnter"
              />
              <div class="composer-mode">
                <button
                  class="composer-mode-trigger"
                  type="button"
                  aria-label="搜索方式"
                  aria-haspopup="menu"
                  :aria-expanded="modeMenuOpen"
                  @click="modeMenuOpen = !modeMenuOpen"
                >
                  {{ search.mode.value === "tags" ? "标签" : "语义" }}
                  <AppIcon name="chevron-down" :size="13" />
                </button>
                <div v-if="modeMenuOpen" class="composer-mode-menu" role="menu" aria-label="选择搜索方式">
                  <button type="button" role="menuitem" :class="{ active: search.mode.value === 'semantic' }" @click="selectSearchMode('semantic')">语义</button>
                  <button type="button" role="menuitem" :class="{ active: search.mode.value === 'tags' }" @click="selectSearchMode('tags')">标签</button>
                </div>
              </div>
              <button class="search-submit-button" type="button" data-testid="submit-search" :disabled="!search.searching.value && !canSubmitSearch" :aria-label="search.searching.value ? '取消搜索' : '搜索'" @click="search.searching.value ? search.cancel() : submitSearch()">
                <span v-if="search.searching.value" aria-hidden="true">×</span>
                <AppIcon v-else name="arrow-up" />
              </button>
            </div>

            <div v-if="search.queryImageId.value && search.mode.value !== 'tags'" class="query-image-chip">
              <AppIcon name="image" :size="17" />
              <span><strong>{{ search.queryImageName.value }}</strong><small>只传图片为以图搜图；输入文字后为图文组合</small></span>
              <button type="button" aria-label="移除查询图片" @click="search.setQueryImage()">×</button>
            </div>

            <div class="search-meta-row">
              <details class="search-settings">
                <summary><AppIcon name="sliders" :size="16" />搜索设置</summary>
                <div class="search-settings-panel">
                  <label>
                    <span>图库</span>
                    <select v-model="search.libraryId.value">
                      <option value="">全部图库</option>
                      <option v-for="library in search.libraries.value" :key="library.id" :value="library.id">{{ library.name }}</option>
                    </select>
                  </label>
                  <label>
                    <span>结果数量</span>
                    <input v-model="search.resultLimit.value" aria-label="本次搜索图片数量" type="number" min="1" step="1" inputmode="numeric" />
                  </label>
                </div>
              </details>
            </div>
          </form>
        </div>

        <div v-if="hasSearchActivity" ref="searchWorkspace" class="search-workspace">
          <section class="gallery-panel card" aria-labelledby="gallery-title">
            <header class="panel-heading">
              <div>
                <h2 id="gallery-title">{{ inferredSearchLabel }} · {{ search.totalItems.value.toLocaleString("zh-CN") }} 张 · {{ activeLibraryLabel }}<span class="sr-only">{{ search.totalItems.value.toLocaleString("zh-CN") }} 张图片</span></h2>
                <p>{{ search.query.value || search.queryImageName.value || "当前搜索" }}</p>
              </div>
              <div class="result-status-actions">
                <button class="result-action-button" type="button" @click="searchInput?.focus()">筛选</button>
                <button class="result-action-button" type="button" :disabled="!search.items.value.length || nativeActions.exporting.value" @click="exportSelectedImages(search.items.value.map((item) => item.id), 'result_header_export')">导出</button>
                <output class="status-pill" :class="{ 'is-passive': !search.searching.value && search.status.value !== 'failed' }" role="status" aria-live="polite" :title="search.message.value">{{ statusLabel }}</output>
                <button v-if="search.searching.value" class="result-cancel-button" type="button" data-testid="cancel-search" @click="search.cancel()">取消</button>
              </div>
            </header>
            <div class="gallery-operations">
              <button v-if="!selectedCount && search.items.value.length" class="select-page-button" type="button" @click="selectCurrentPage">全选当前页</button>
              <div v-if="selectedCount" class="selection-toolbar" aria-label="批量图片操作">
                <span>已选择 {{ selectedCount }} 张；Ctrl 点击多选，Shift 点击连续选择</span>
                <button class="button button-quiet button-small" type="button" @click="copySelectedFiles(activeActionIds, 'selection_toolbar_copy')">复制文件</button>
                <button class="button button-secondary button-small" type="button" :disabled="nativeActions.exporting.value" @click="exportSelectedImages(activeActionIds, 'selection_toolbar_export')">
                  {{ nativeActions.exporting.value ? "导出中…" : "导出所选" }}
                </button>
                <button class="button button-quiet button-small" type="button" @click="clearSelection">清除</button>
              </div>
              <div v-if="nativeActions.exporting.value" class="export-progress" role="status">{{ nativeActions.exportJob.value?.message }}</div>
            </div>
            <GalleryGrid
              :items="search.items.value"
              :selected-id="search.selectedId.value"
              :selected-ids="selectedImageIds"
              :loading="search.pageLoading.value"
              :loading-text="`正在读取第 ${search.page.value} 页`"
              @select="handleGallerySelection"
              @open="openSearchImage"
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
        </div>
      </section>

      <div v-if="visitedPages.includes('tasks')" v-show="activePage === 'tasks'" class="feature-page" data-page-section="tasks">
        <TasksPage :libraries="search.libraries.value" :visible="activePage === 'tasks'" @toast="handleFeatureToast" />
      </div>

      <div v-if="organizeVisited" v-show="activePage === 'batch' || activePage === 'groups' || activePage === 'learning'" class="feature-page" data-page-section="organize">
        <OrganizePage :active-tab="organizeTab" :show-tabs="false" @toast="handleFeatureToast" @open-image="nativeActions.open" />
      </div>

      <div v-if="visitedPages.includes('settings')" v-show="activePage === 'settings'" class="settings-feature-page" data-page-section="settings">
        <SettingsPage @toast="handleFeatureToast" @libraries-updated="handleLibrariesUpdated" />
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
      :feedback-available="contextFeedbackAvailable"
      :feedback-pending="searchFeedback.isPending(contextFeedbackItem)"
      :feedback-action="contextFeedbackEvent?.action ?? ''"
      @detail="openImageDetails"
      @open="openSearchImage(contextImageId())"
      @reveal="nativeActions.reveal(contextImageId())"
      @copy-image="copySelectedImage"
      @copy-files="copySelectedFiles(activeActionIds, 'context_menu_copy_files')"
      @copy-paths="copySelectedPaths(activeActionIds)"
      @export="exportSelectedImages(activeActionIds, 'context_menu_export')"
      @mark-relevant="markContextFeedback('relevant')"
      @mark-not-relevant="markContextFeedback('not_relevant')"
      @undo-feedback="undoContextFeedback"
      @clear="clearSelection"
      @close="closeContextMenu"
    />

    <div
      v-if="previewVisible"
      class="image-preview-backdrop"
      role="presentation"
      @click.self="closeImageDetails"
    >
      <ImagePreview
        class="preview-dialog"
        :item="search.selectedItem.value"
        :high-resolution="search.highResolutionPreview.value"
        :preview-visible="previewVisible"
        @close="closeImageDetails"
        @open="openSearchImage"
        @reveal="nativeActions.reveal"
      />
    </div>
  </div>
</template>
