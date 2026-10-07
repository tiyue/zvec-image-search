<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  defineComponent,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import type { AsyncComponentLoader, Component } from "vue";

import yaolensLogoUrl from "./assets/yaolens-logo.png";
import AsyncPageState from "./components/AsyncPageState.vue";
import GalleryContextMenu from "./components/GalleryContextMenu.vue";
import GalleryGrid from "./components/GalleryGrid.vue";
import ImagePreview from "./components/ImagePreview.vue";
import ImageViewer from "./components/ImageViewer.vue";
import PaginationBar from "./components/PaginationBar.vue";
import StatusToast from "./components/StatusToast.vue";
import AppIcon from "./components/AppIcon.vue";
import {
  isViewerOverlayOpen,
  useImageViewerSession,
  type ViewerImage,
} from "./composables/useImageViewer";
import { useNativeImageActions } from "./composables/useNativeImageActions";
import {
  computeGalleryLayout,
  useGalleryDensity,
  type GalleryLayout,
} from "./composables/useGalleryDensity";
import { createLazyPageLoader } from "./composables/useLazyPage";
import { useSearch } from "./composables/useSearch";
import { installGlobalDiagnostics, reportFrontendDiagnostic } from "./diagnostics";
import type { ImageEditIncomingSource } from "./features/image-edit";
import { canRecordFeedback, useSearchFeedback } from "./features/search-learning";
import type { SettingsLibrary } from "./features/settings";
import type {
  GalleryContextIntent,
  GallerySelectionIntent,
  NativeDirectorySelection,
  NativeFileSelection,
  SearchHistoryEntry,
  SearchResultItem,
  ToastMessage,
} from "./types/contracts";

type PageName = "search" | "tasks" | "batch" | "groups" | "recommendations" | "raw-selection" | "image-edit" | "settings";

const pageDefinitions: Array<{
  id: PageName;
  label: string;
  description: string;
  icon: "search" | "tasks" | "tags" | "groups" | "image";
  shortcut: `Alt+${1 | 2 | 3 | 4 | 5 | 6 | 7 | 8}`;
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
    id: "recommendations",
    label: "图片推荐",
    description: "多样发现与随机探索",
    icon: "image",
    shortcut: "Alt+6",
  },
  {
    id: "raw-selection",
    label: "ARW 选片",
    description: "导入、评级与导出",
    icon: "image",
    shortcut: "Alt+7",
  },
  {
    id: "image-edit",
    label: "图片编辑",
    description: "Qwen 单图指令编辑",
    icon: "image",
    shortcut: "Alt+8",
  },
];

/**
 * Off-screen feature pages load on first visit through a direct `.vue` import
 * so no aggregation module pulls them all back into the entry chunk. The
 * search landing page stays a synchronous import.
 */
const pageKeys = ref<Partial<Record<PageName, number>>>({});

function pageKey(page: PageName): number {
  return pageKeys.value[page] ?? 0;
}

function reloadPage(page: PageName): void {
  pageKeys.value = { ...pageKeys.value, [page]: pageKey(page) + 1 };
}

function lazyPageState(label: string, page?: PageName): Component {
  return defineComponent({
    name: "AsyncPageStateSurface",
    inheritAttrs: false,
    props: { error: { type: [Object, String], default: null } },
    setup(stateProps) {
      return () =>
        h(AsyncPageState, {
          label,
          error: stateProps.error,
          // Vue 3.5 does not forward the async error component's `retry`
          // emit, so the manual retry remounts the page through a new key.
          onRetry: page ? () => reloadPage(page) : undefined,
        });
    },
  });
}

function lazyPage<P extends Component>(
  page: PageName,
  label: string,
  loader: () => Promise<{ default: P }>,
): P {
  // Resolve `default` explicitly. Vue only unwraps real ES module namespaces,
  // so a plain module object (for example a test mock) would otherwise be
  // treated as the component itself.
  const direct = () => loader().then((module) => module.default);
  // The first attempt is the plain `.vue` dynamic import; after a failure the
  // manual retry re-requests the emitted chunk, because the browser keeps the
  // failed module in its module map for the rest of the document.
  const load: AsyncComponentLoader<P> = createLazyPageLoader(page, direct);
  return defineAsyncComponent({
    loader: load,
    delay: 0,
    loadingComponent: lazyPageState(label),
    errorComponent: lazyPageState(label, page),
  });
}

const TasksPage = lazyPage("tasks", "图库任务", () => import("./features/tasks/TasksPage.vue"));
const OrganizePage = lazyPage(
  "batch",
  "批量标签",
  () => import("./features/organize/OrganizePage.vue"),
);
const RecommendationPage = lazyPage(
  "recommendations",
  "图片推荐",
  () => import("./features/recommendations/RecommendationPage.vue"),
);
const RawSelectionPage = lazyPage(
  "raw-selection",
  "ARW 选片",
  () => import("./features/raw-selection/RawSelectionPage.vue"),
);
const ImageEditPage = lazyPage(
  "image-edit",
  "图片编辑",
  () => import("./features/image-edit/ImageEditPage.vue"),
);
const SettingsPage = lazyPage(
  "settings",
  "设置",
  () => import("./features/settings/SettingsPage.vue"),
);

const activePage = ref<PageName>("search");
const visitedPages = ref<PageName[]>(["search"]);
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

const galleryDensity = useGalleryDensity();
const galleryDensityOptions = galleryDensity.options;
const searchWorkspace = ref<HTMLElement | null>(null);
const searchGalleryViewport = ref<HTMLElement | null>(null);
const searchGalleryLayout = ref<GalleryLayout>(
  computeGalleryLayout({
    width: 0,
    height: 0,
    density: galleryDensity.density.value,
    mode: "search",
  }),
);
const searchInput = ref<HTMLInputElement | null>(null);
const paginationBar = ref<{ focusInput: () => void } | null>(null);
const previewVisible = ref(false);
const queryDropActive = ref(false);
const modeMenuOpen = ref(false);
const selectedImageIds = ref<string[]>([]);
const incomingImageEditSource = ref<ImageEditIncomingSource | null>(null);
const selectionAnchorId = ref("");
const contextMenu = ref({ visible: false, x: 0, y: 0, imageId: "" });
const knownSearchItems = new Map<string, SearchResultItem>();
let knownFeedbackSessions = "";
let removeGlobalDiagnostics: (() => void) | null = null;
let imageEditImportSequence = 0;
let searchGalleryObserver: ResizeObserver | null = null;
let searchGalleryResizeTimer: number | null = null;

const searchViewer = useImageViewerSession({
  items: () => search.items.value.map(toViewerImage),
  startId: () => search.selectedId.value || search.items.value[0]?.id || "",
  hasMore: (direction) => (direction > 0 ? search.hasNext.value : search.hasPrevious.value),
  loadAdjacent: async (direction) => {
    const anchorId = search.selectedId.value;
    const targetPage = search.page.value + direction;
    if (!(await search.goToPage(targetPage))) return null;
    const items = search.items.value;
    if (items.length === 0) return null;
    // A capacity change can carry the anchor into the freshly read window; keep
    // its absolute neighbour instead of assuming the page starts at the anchor.
    const index = items.findIndex((item) => item.id === anchorId);
    if (index >= 0) {
      const neighbor = items[index + direction]?.id;
      if (neighbor) return neighbor;
    }
    return direction > 0 ? items[0].id : items[items.length - 1].id;
  },
});

const searchViewerPosition = computed(() => {
  const index = search.items.value.findIndex((item) => item.id === searchViewer.currentId.value);
  if (index < 0) return "";
  const absolute = (search.page.value - 1) * search.pageSize.value + index + 1;
  const total = search.totalItems.value;
  return total > 0 ? `${absolute} / ${total}` : "";
});

const searchViewerPreloads = computed(() => {
  const items = search.items.value;
  const index = items.findIndex((item) => item.id === searchViewer.currentId.value);
  if (index < 0) return [];
  const urls: string[] = [];
  const previous = items[index - 1];
  const next = items[index + 1];
  if (previous) urls.push(previous.imageUrl || previous.thumbnailUrl);
  if (next) urls.push(next.imageUrl || next.thumbnailUrl);
  return urls.filter(Boolean);
});

watch(
  () => searchViewer.currentId.value,
  (id) => {
    if (searchViewer.visible.value && id) search.select(id);
  },
);

// Whatever closed the overlay, the capacity deferred while it was open is
// re-applied once the viewer has unmounted and released the shortcut hold.
watch(
  () => searchViewer.visible.value,
  (visible) => {
    if (!visible) void nextTick(() => applySearchGalleryLayout(true));
  },
);

function toViewerImage(item: SearchResultItem): ViewerImage {
  return {
    id: item.id,
    name: item.name,
    relativePath: item.relativePath,
    libraryName: item.libraryName,
    width: item.width,
    height: item.height,
    tags: item.tags,
    thumbnailUrl: item.thumbnailUrl,
    imageUrl: item.imageUrl,
    nativeId: item.id,
  };
}

function escapeSelector(value: string): string {
  return typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(value)
    : value.replace(/["\\]/gu, "\\$&");
}

function focusSearchImage(imageId: string): void {
  const target = imageId
    ? searchGalleryViewport.value?.querySelector<HTMLElement>(
        `[data-image-id="${escapeSelector(imageId)}"]`,
      )
    : null;
  if (target) target.focus({ preventScroll: true });
  else searchInput.value?.focus({ preventScroll: true });
}

function openSearchViewer(imageId: string): void {
  if (!imageId) return;
  search.select(imageId);
  // Drop a queued capacity change so it cannot reload the page under the overlay.
  if (searchGalleryResizeTimer !== null) {
    window.clearTimeout(searchGalleryResizeTimer);
    searchGalleryResizeTimer = null;
  }
  // A capacity re-read may already be in flight; cancel it so it cannot unload
  // the window under the overlay. The capacity the gallery actually shows is
  // reported back so closing re-reads at the measured one.
  const applied = search.cancelPendingPageLoad(true);
  if (applied > 0 && applied !== searchGalleryLayout.value.pageSize) {
    searchGalleryLayout.value = { ...searchGalleryLayout.value, pageSize: applied };
  }
  searchViewer.open(imageId);
}

function closeSearchViewer(): void {
  const imageId = searchViewer.currentId.value;
  searchViewer.close();
  // A cross-page step may still be loading; stop it so a late page cannot
  // replace the window or the selection the user returns to.
  search.cancelPendingPageLoad();
  void nextTick(() => focusSearchImage(imageId));
}

async function openViewerImageNative(): Promise<void> {
  const imageId = searchViewer.currentId.value;
  if (imageId) await openSearchImage(imageId);
}

function revealViewerImage(): void {
  const imageId = searchViewer.currentId.value;
  if (imageId) void nativeActions.reveal(imageId);
}

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
  if (hasSearchActivity.value) {
    const resultLabels: Record<string, string> = {
      text: "语义搜索",
      tag: "标签搜索",
      image: "以图搜图",
      image_text: "图文组合",
    };
    const resultLabel = resultLabels[search.resultQueryType.value];
    if (resultLabel) return resultLabel;
  }
  if (search.mode.value === "tags") return "标签搜索";
  if (search.queryImageId.value && search.query.value.trim()) return "图文组合";
  if (search.queryImageId.value) return "以图搜图";
  return "语义搜索";
});
const organizeTab = computed<"batch" | "clusters">(() =>
  activePage.value === "groups" ? "clusters" : "batch",
);
const organizeVisited = computed(() =>
  visitedPages.value.some((page) => page === "batch" || page === "groups"),
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
  if (searchViewer.visible.value) searchViewer.close();
  searchHasRun.value = true;
  clearSelection();
  await search.submit();
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

function editContextImage(): void {
  const imageId = contextImageId();
  const item = searchItemForId(imageId);
  const url = item?.imageUrl || item?.thumbnailUrl;
  if (!item || !url) {
    addToast("无法加入图片编辑", "当前图片没有可用的本地预览地址。", "error");
    return;
  }
  imageEditImportSequence += 1;
  incomingImageEditSource.value = {
    key: `${imageId}-${Date.now()}-${imageEditImportSequence}`,
    imageId,
    name: item.name,
    url,
    sizeBytes: item.sizeBytes,
  };
  setPage("image-edit");
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

async function exportRecommendedImage(mediaId: string): Promise<boolean> {
  if (!(await nativeActions.exportImages([mediaId]))) return false;
  return (nativeActions.exportJob.value?.exported ?? 0) > 0;
}

function copyRecommendedFile(mediaId: string): Promise<boolean> {
  return nativeActions.copyFiles([mediaId]);
}

function copyRecommendedPath(mediaId: string): Promise<boolean> {
  return nativeActions.copyPaths([mediaId]);
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
  } else {
    addToast(
      "反馈未保存",
      "搜索学习未启用或当前结果不支持反馈记录。",
      "error",
    );
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
  if (!(target instanceof Element && target.closest(".composer-mode"))) {
    modeMenuOpen.value = false;
  }
  if (target instanceof Element && target.closest(".gallery-context-menu")) return;
  closeContextMenu();
}

function handleWindowResize(): void {
  closeContextMenu();
}

/**
 * Measure the real search gallery box and hand the resulting capacity to the
 * search composable. Zero-sized (hidden) boxes are ignored so switching pages
 * through `v-show` cannot collapse the grid, and a change only reloads when
 * the capacity actually differs.
 */
function applySearchGalleryLayout(reload: boolean): void {
  const element = searchGalleryViewport.value;
  if (!element) return;
  const width = element.clientWidth;
  const height = element.clientHeight;
  if (width <= 0 || height <= 0) return;
  const next = computeGalleryLayout({
    width,
    height,
    density: galleryDensity.density.value,
    mode: "search",
  });
  const previous = searchGalleryLayout.value;
  if (
    next.pageSize === previous.pageSize &&
    next.columns === previous.columns &&
    next.cardHeight === previous.cardHeight
  ) return;
  // While a viewer overlay is open, defer the capacity change (and the page
  // reload it triggers) so the viewer window stays put; closing re-applies it.
  if (reload && isViewerOverlayOpen()) return;
  searchGalleryLayout.value = next;
  // Only a real capacity change re-reads the page; a pure visual change (tile
  // height) just re-renders the grid.
  if (next.pageSize !== previous.pageSize) void search.setPageSize(next.pageSize, reload);
}

function scheduleSearchGalleryLayout(): void {
  if (searchGalleryResizeTimer !== null) window.clearTimeout(searchGalleryResizeTimer);
  searchGalleryResizeTimer = window.setTimeout(() => {
    searchGalleryResizeTimer = null;
    applySearchGalleryLayout(true);
  }, 140);
}

function stopSearchGalleryObserver(): void {
  searchGalleryObserver?.disconnect();
  searchGalleryObserver = null;
  if (searchGalleryResizeTimer !== null) {
    window.clearTimeout(searchGalleryResizeTimer);
    searchGalleryResizeTimer = null;
  }
}

function handleCloseBlocked(): void {
  addToast("暂时无法退出", "仍有任务在运行，请完成或取消任务后重试。", "error");
}

function handleCloseFailed(): void {
  addToast("退出失败", "后台服务未能安全停止，请稍后重试。", "error");
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
  if (searchViewer.visible.value) searchViewer.close();
  search.query.value = "";
  search.setQueryImage();
  clearSelection();
  modeMenuOpen.value = false;
  searchHasRun.value = false;
  focusSearch();
}

async function openSearchHistory(entry: SearchHistoryEntry): Promise<void> {
  if (searchViewer.visible.value) searchViewer.close();
  setPage("search");
  clearSelection();
  searchHasRun.value = true;
  if (!(await search.openHistory(entry.id, entry.label))) {
    searchHasRun.value = false;
  }
}

function historyTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function handleFeatureToast(
  title: string,
  message: string,
  kind: ToastMessage["kind"],
): void {
  addToast(title, message, kind);
}

function changeGalleryDensity(event: Event): void {
  galleryDensity.setDensity((event.target as HTMLSelectElement).value);
}

function handleRawSelectionSelectFolder(callback: (path: string) => void): void {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_directory !== "function") {
    addToast("无法选择文件夹", "桌面桥接尚未就绪，请稍后重试。", "error");
    return;
  }
  void bridge.select_directory().then((result: NativeDirectorySelection) => {
    if (!result || result.ok !== true) {
      addToast("无法选择文件夹", result?.error ?? "文件夹选择失败。", "error");
      callback("");
      return;
    }
    callback(result.path ?? "");
  }).catch(() => {
    addToast("无法选择文件夹", "文件夹选择失败。", "error");
    callback("");
  });
}

function handleImageEditSelectDirectory(callback: (path: string) => void): void {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_directory !== "function") {
    addToast("无法选择输出目录", "桌面桥接尚未就绪，请稍后重试。", "error");
    callback("");
    return;
  }
  void bridge.select_directory().then((result: NativeDirectorySelection) => {
    if (!result || result.ok !== true) {
      if (result?.error) addToast("无法选择输出目录", result.error, "error");
      callback("");
      return;
    }
    callback(result.path ?? "");
  }).catch(() => {
    addToast("无法选择输出目录", "文件夹选择失败。", "error");
    callback("");
  });
}

function handleRawSelectionSelectFiles(callback: (paths: string[]) => void): void {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_raw_images !== "function") {
    addToast("无法选择图片", "桌面桥接尚未就绪，请稍后重试。", "error");
    callback([]);
    return;
  }
  void bridge.select_raw_images().then((result: NativeFileSelection) => {
    if (!result || result.ok !== true) {
      addToast("无法选择图片", result?.error ?? "图片选择失败。", "error");
      callback([]);
      return;
    }
    callback(Array.isArray(result.paths) ? result.paths.filter((path) => typeof path === "string") : []);
  }).catch(() => {
    addToast("无法选择图片", "图片选择失败。", "error");
    callback([]);
  });
}


function handleLibrariesUpdated(libraries: SettingsLibrary[]): void {
  search.libraries.value = libraries.map((library) => ({
    id: library.id,
    name: library.name,
    enabled: library.enabled,
    is_default: library.isDefault,
    auto_index_enabled: library.autoIndexEnabled,
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
  // The viewer owns its own keys; background shortcuts and selection must not
  // react while the overlay is open.
  if (isViewerOverlayOpen()) return;
  // Use the physical digit code as a fallback so Alt shortcuts work under
  // keyboard layouts that transform event.key. Ignore AltGr (Ctrl+Alt).
  const shortcutKey = /^Digit([1-8])$/u.exec(event.code)?.[1] ?? event.key;
  if (
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    ["1", "2", "3", "4", "5", "6", "7", "8"].includes(shortcutKey)
  ) {
    event.preventDefault();
    const page = pageDefinitions.find((item) => item.shortcut === `Alt+${shortcutKey}`)?.id;
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
  if (
    event.key === " " &&
    !editing &&
    activePage.value === "search" &&
    searchGalleryViewport.value?.contains(document.activeElement) === true
  ) {
    event.preventDefault();
    if (search.selectedId.value) openSearchViewer(search.selectedId.value);
  } else if (commandSelectPage && activePage.value === "search" && !editing) {
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

watch(hasSearchActivity, async (active) => {
  if (!active) {
    stopSearchGalleryObserver();
    return;
  }
  await nextTick();
  applySearchGalleryLayout(true);
  const element = searchGalleryViewport.value;
  if (typeof ResizeObserver === "undefined" || !element) return;
  searchGalleryObserver ??= new ResizeObserver(scheduleSearchGalleryLayout);
  searchGalleryObserver.disconnect();
  searchGalleryObserver.observe(element);
});

watch(
  () => galleryDensity.density.value,
  () => {
    void nextTick(() => applySearchGalleryLayout(true));
  },
);

onMounted(() => {
  removeGlobalDiagnostics = installGlobalDiagnostics();
  search.setPreviewEnabled(false);
  window.addEventListener("keydown", handleGlobalKeydown);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
  window.addEventListener("yaolens-close-blocked", handleCloseBlocked);
  window.addEventListener("yaolens-close-failed", handleCloseFailed);
  void search.initialize();
  void searchFeedback.initialize();
});

onBeforeUnmount(() => {
  removeGlobalDiagnostics?.();
  removeGlobalDiagnostics = null;
  stopSearchGalleryObserver();
  window.removeEventListener("keydown", handleGlobalKeydown);
  window.removeEventListener("pointerdown", handleWindowPointerDown);
  window.removeEventListener("resize", handleWindowResize);
  window.removeEventListener("yaolens-close-blocked", handleCloseBlocked);
  window.removeEventListener("yaolens-close-failed", handleCloseFailed);
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
  <div
    class="app-shell"
    :class="{
      'sidebar-collapsed': sidebarCollapsed,
      'raw-selection-active': activePage === 'raw-selection',
      'image-edit-active': activePage === 'image-edit',
    }"
  >
    <aside class="sidebar" aria-label="主导航">
      <div class="sidebar-head">
        <button
          class="brand-mark"
          type="button"
          :aria-label="sidebarCollapsed ? '展开侧边栏' : 'YaoLens'"
          :title="sidebarCollapsed ? '展开侧边栏' : 'YaoLens'"
          @click="sidebarCollapsed = false"
        >
          <img :src="yaolensLogoUrl" alt="" aria-hidden="true" />
        </button>
        <span class="brand-copy"><strong class="brand-name">YaoLens</strong><small>本地智能图片检索</small></span>
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

      <section v-if="search.history.value.length" class="sidebar-recents" aria-labelledby="recent-title">
        <p id="recent-title">搜索记录</p>
        <button
          v-for="entry in search.history.value"
          :key="entry.id"
          type="button"
          :title="`${entry.label} · ${entry.total_items} 张`"
          @click="openSearchHistory(entry)"
        >
          <strong>{{ entry.label }}</strong>
          <small>{{ historyTime(entry.created_at) }} · {{ entry.total_items }} 张</small>
        </button>
      </section>

      <button class="settings-button" type="button" :class="{ 'is-active': activePage === 'settings' }" title="设置" @click="setPage('settings')">
        <AppIcon name="settings" />
        <span><strong>设置</strong><small>图库、模型与访问</small></span>
      </button>
    </aside>

    <main
      class="page-host"
      :class="{
        'raw-selection-active': activePage === 'raw-selection',
        'image-edit-active': activePage === 'image-edit',
      }"
    >
      <header class="app-topbar">
        <div v-if="activePage === 'search' || activePage === 'tasks'" class="top-switch" aria-label="主要视图">
          <button type="button" :class="{ active: activePage === 'search' }" @click="setPage('search')">搜索</button>
          <button type="button" :class="{ active: activePage === 'tasks' }" @click="setPage('tasks')">图库任务</button>
        </div>
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
                :placeholder="search.mode.value === 'tags' ? '输入多个标签，默认同时包含' : '搜索人物、场景、动作或作品；用 | 分开多组语义'"
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
                  <label>
                    <span>缩略图</span>
                    <select
                      :value="galleryDensity.density.value"
                      aria-label="缩略图密度"
                      @change="changeGalleryDensity"
                    >
                      <option v-for="option in galleryDensityOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </label>
                  <label v-if="search.mode.value === 'tags'" class="tag-match-setting">
                    <span>标签匹配</span>
                    <select v-model="search.tagMode.value" aria-label="标签匹配方式">
                      <option value="all">同时包含全部</option>
                      <option value="any">包含任一标签</option>
                    </select>
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
                <p>{{ search.title.value || "当前搜索" }}</p>
              </div>
              <div class="result-status-actions">
                <label class="result-density-control">
                  <span>缩略图</span>
                  <select
                    data-testid="result-density"
                    aria-label="缩略图密度"
                    :value="galleryDensity.density.value"
                    @change="changeGalleryDensity"
                  >
                    <option v-for="option in galleryDensityOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                  </select>
                </label>
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
            <div ref="searchGalleryViewport" class="search-gallery-viewport">
              <GalleryGrid
                :items="search.items.value"
                :selected-id="search.selectedId.value"
                :selected-ids="selectedImageIds"
                :loading="search.pageLoading.value"
                :loading-text="`正在读取第 ${search.page.value} 页`"
                :columns="searchGalleryLayout.columns"
                :card-height="searchGalleryLayout.cardHeight"
                @select="handleGallerySelection"
                @open="openSearchViewer"
                @context="openContextMenu"
              />
            </div>
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
        <TasksPage :key="pageKey('tasks')" :libraries="search.libraries.value" :visible="activePage === 'tasks'" @toast="handleFeatureToast" />
      </div>

      <div v-if="visitedPages.includes('recommendations')" v-show="activePage === 'recommendations'" class="feature-page" data-page-section="recommendations">
        <RecommendationPage
          :key="pageKey('recommendations')"
          :visible="activePage === 'recommendations'"
          :export-busy="nativeActions.exporting.value"
          :open-image="nativeActions.open"
          :reveal-image="nativeActions.reveal"
          :copy-image="nativeActions.copyImage"
          :copy-file="copyRecommendedFile"
          :copy-path="copyRecommendedPath"
          :export-image="exportRecommendedImage"
          @toast="handleFeatureToast"
        />
      </div>

      <div v-if="visitedPages.includes('raw-selection')" v-show="activePage === 'raw-selection'" class="feature-page" data-page-section="raw-selection">
        <RawSelectionPage
          :key="pageKey('raw-selection')"
          @toast="handleFeatureToast"
          @select-folder="handleRawSelectionSelectFolder"
          @select-files="handleRawSelectionSelectFiles"
        />
      </div>

      <div v-if="visitedPages.includes('image-edit')" v-show="activePage === 'image-edit'" class="feature-page" data-page-section="image-edit">
        <ImageEditPage
          :key="pageKey('image-edit')"
          :visible="activePage === 'image-edit'"
          :incoming-source="incomingImageEditSource"
          @toast="handleFeatureToast"
          @select-directory="handleImageEditSelectDirectory"
        />
      </div>

      <div v-if="organizeVisited" v-show="activePage === 'batch' || activePage === 'groups'" class="feature-page" data-page-section="organize">
        <OrganizePage :key="pageKey('batch')" :active-tab="organizeTab" :show-tabs="false" @toast="handleFeatureToast" @open-image="nativeActions.open" />
      </div>

      <div v-if="visitedPages.includes('settings')" v-show="activePage === 'settings'" class="settings-feature-page" data-page-section="settings">
        <SettingsPage :key="pageKey('settings')" @toast="handleFeatureToast" @libraries-updated="handleLibrariesUpdated" />
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
      @edit-image="editContextImage"
      @copy-files="copySelectedFiles(activeActionIds, 'context_menu_copy_files')"
      @copy-paths="copySelectedPaths(activeActionIds)"
      @export="exportSelectedImages(activeActionIds, 'context_menu_export')"
      @mark-relevant="markContextFeedback('relevant')"
      @mark-not-relevant="markContextFeedback('not_relevant')"
      @undo-feedback="undoContextFeedback"
      @clear="clearSelection"
      @close="closeContextMenu"
    />

    <ImageViewer
      v-if="searchViewer.visible.value"
      :image="searchViewer.current.value"
      :can-previous="searchViewer.hasPrevious.value"
      :can-next="searchViewer.hasNext.value"
      :loading="searchViewer.loading.value"
      :error="searchViewer.error.value"
      :position-text="searchViewerPosition"
      :preload-urls="searchViewerPreloads"
      can-reveal
      @close="closeSearchViewer"
      @previous="searchViewer.step(-1)"
      @next="searchViewer.step(1)"
      @retry="searchViewer.retry()"
      @open-native="openViewerImageNative"
      @reveal="revealViewerImage"
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
