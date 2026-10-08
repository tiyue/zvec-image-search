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
import ImageViewer from "../../components/ImageViewer.vue";
import { useGalleryDensity } from "../../composables/useGalleryDensity";
import {
  acquireViewerOverlay,
  isViewerOverlayOpen,
  useImageViewerSession,
  type ViewerImage,
} from "../../composables/useImageViewer";
import {
  calculateOrganizeGalleryCapacity,
  GALLERY_META_OVERHEAD,
  GRID_GAP,
  GRID_HORIZONTAL_PADDING,
  gridRowSpan,
  gridSpan,
  type OrganizeGalleryCapacity,
} from "./galleryCapacity";
import SimilarityGroupsPanel from "./SimilarityGroupsPanel.vue";
import GalleryDetails from "./GalleryDetails.vue";

import type {
  FolderNameTagMode,
  FolderNameTagSelection,
  ManualTagOperation,
  OrganizeAlias,
  OrganizeApi,
  OrganizeImage,
  TagPresence,
  ToastKind,
} from "./types";
import { parseTagInput, useOrganize } from "./useOrganize";

type WorkspaceTab = "batch" | "clusters";

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
  /** The gallery asks the shell to switch between the browse and group views. */
  tabChange: [tab: WorkspaceTab];
}>();

const organize = useOrganize(props.api, {
  autoLoad: false,
  onToast: (title, message, kind) => emit("toast", title, message, kind),
});
const galleryDensity = useGalleryDensity();
const galleryDensityOptions = galleryDensity.options;
const aliasCanonical = ref("");
const aliasInput = ref("");
const failedThumbnails = ref<Set<string>>(new Set());
const galleryViewport = ref<HTMLElement | null>(null);
const containerWidth = ref(0);
const galleryCapacity = ref<OrganizeGalleryCapacity>(
  calculateOrganizeGalleryCapacity(0, 0, galleryDensity.density.value),
);
const batchDialogOpen = ref(false);
const detailsOpen = ref(false);
const batchDialogRef = ref<HTMLElement | null>(null);
const batchButtonRef = ref<HTMLButtonElement | null>(null);
const detailsButtonRef = ref<HTMLButtonElement | null>(null);
let viewerDetailsFocus: HTMLElement | null = null;
const folderPanelRef = ref<HTMLElement | null>(null);
const folderPanelToggleRef = ref<HTMLButtonElement | null>(null);
const deleteConfirmation = ref("");
const deleteTargetKey = ref("");
const folderNameTagModalOpen = ref(false);
const folderNameTagScope = ref<"folder" | "library">("folder");
const folderNameTagMode = ref<FolderNameTagMode>("normal");
const folderNameTagForce = ref(true);
const folderPanelCollapsed = ref(false);
// Under the drawer breakpoint the folder navigation leaves the grid and opens
// on demand so the narrow workspace stays reachable.
const narrowLayout = ref(false);
const folderDrawerOpen = ref(false);
const activeWorkspaceTab = ref<WorkspaceTab>(props.activeTab);
const visitedWorkspaceTabs = ref<Set<WorkspaceTab>>(
  new Set([props.activeTab]),
);
let galleryObserver: ResizeObserver | null = null;
let galleryResizeTimer: number | null = null;
let narrowMediaQuery: MediaQueryList | null = null;
let releaseModalOverlay: (() => void) | null = null;
let initialLoadStarted = false;
const taskSubmissionLocked = computed(
  () => Boolean(organize.busyAction.value) || organize.jobRunning.value,
);

watch(organize.deletePreview, () => {
  deleteConfirmation.value = "";
});

function changeLibrary(event: Event): void {
  void organize.selectLibrary((event.target as HTMLSelectElement).value);
}

function selectWorkspaceTab(tab: WorkspaceTab): void {
  activeWorkspaceTab.value = tab;
  visitedWorkspaceTabs.value = new Set([...visitedWorkspaceTabs.value, tab]);
  if (tab !== "batch") {
    batchDialogOpen.value = false;
    folderDrawerOpen.value = false;
    detailsOpen.value = false;
  }
}

watch(
  () => props.activeTab,
  (tab) => selectWorkspaceTab(tab),
);

// The gallery tools drive both views of the same mounted page. Switching also
// tells the shell so its navigation stays in sync with Alt+3 / Alt+4.
function requestWorkspaceTab(tab: WorkspaceTab): void {
  selectWorkspaceTab(tab);
  emit("tabChange", tab);
}

function openBatchDialog(): void {
  if (activeWorkspaceTab.value !== "batch") requestWorkspaceTab("batch");
  batchDialogOpen.value = true;
}

function closeBatchDialog(): void {
  if (!batchDialogOpen.value) return;
  batchDialogOpen.value = false;
  void nextTick(() => batchButtonRef.value?.focus({ preventScroll: true }));
}

function openDetails(): void {
  detailsOpen.value = true;
}

function closeDetails(): void {
  if (!detailsOpen.value) return;
  detailsOpen.value = false;
  void nextTick(() => {
    detailsButtonRef.value?.focus({ preventScroll: true });
    // The floating details pane holds the overlay slot, so a change deferred
    // while it was open is re-measured once it is gone.
    updateGalleryCapacity(true);
  });
}

function toggleDetails(): void {
  if (detailsOpen.value) closeDetails();
  else openDetails();
}

function openDetailsImage(imageId: string): void {
  const image =
    organize.images.value.find((item) => item.id === imageId) ??
    organize.selectedKnownImages.value.find((item) => item.id === imageId);
  if (image) openViewer(image);
}

// Native `summary` is focusable but carries no tabindex attribute, so it has to
// be selected explicitly; the collapsed `details` descendants are filtered out
// below so Tab cannot land on a control inside a collapsed form.
const DIALOG_FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

function firstSummary(details: HTMLElement): HTMLElement | null {
  for (const child of Array.from(details.children)) {
    if (child.tagName === "SUMMARY") return child as HTMLElement;
  }
  return null;
}

/** A control inside a closed `details` is hidden unless it is the summary. */
function hiddenByClosedDetails(element: HTMLElement, root: HTMLElement): boolean {
  let node: HTMLElement | null = element.parentElement;
  while (node && node !== root) {
    if (node instanceof HTMLDetailsElement && !node.open) {
      const summary = firstSummary(node);
      if (!summary || !summary.contains(element)) return true;
    }
    node = node.parentElement;
  }
  return false;
}

function isDisplayed(element: HTMLElement, root: HTMLElement): boolean {
  let node: HTMLElement | null = element;
  while (node) {
    if (node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (node === root) break;
    node = node.parentElement;
  }
  return !hiddenByClosedDetails(element, root);
}

// The tag dialog is a real modal: Tab cycles inside it, Escape closes it, and
// the held overlay keeps the background page shortcuts idle while it is open.
function dialogFocusable(root: HTMLElement | null = batchDialogRef.value): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(DIALOG_FOCUSABLE)).filter(
    (element) => isDisplayed(element, root),
  );
}

function trapTab(event: KeyboardEvent, root: HTMLElement | null = batchDialogRef.value): void {
  const elements = dialogFocusable(root);
  if (elements.length === 0) return;
  const first = elements[0];
  const last = elements[elements.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && Boolean(root?.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
  } else if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

function handleDialogKeydown(event: KeyboardEvent): void {
  event.stopPropagation();
  if (event.key === "Tab") trapTab(event);
  else if (event.key === "Escape") {
    event.preventDefault();
    closeBatchDialog();
  }
}

watch([batchDialogOpen, taskSubmissionLocked, organize.selectedCount], ([open], [wasOpen]) => {
  if (!open) return;
  void nextTick(() => {
    const root = batchDialogRef.value;
    if (!root) return;
    const active = document.activeElement;
    if (
      wasOpen && active instanceof HTMLElement && root.contains(active) &&
      isDisplayed(active, root) && !active.matches(":disabled")
    ) return;
    const input = root.querySelector<HTMLElement>(".tag-input-label textarea");
    const target = input && isDisplayed(input, root) ? input : dialogFocusable()[0] ?? root;
    target.focus({ preventScroll: true });
  });
});

// The tag dialog and the narrow folder drawer are the page's modal surfaces.
// Holding the shared viewer-overlay slot keeps the shell's window-level
// shortcuts idle while either is open, whatever element currently has focus.
const modalSurfaceOpen = computed(() => batchDialogOpen.value || folderDrawerOpen.value);

watch(modalSurfaceOpen, (open) => {
  if (open) {
    if (!releaseModalOverlay) releaseModalOverlay = acquireViewerOverlay();
    return;
  }
  if (!releaseModalOverlay) return;
  releaseModalOverlay();
  releaseModalOverlay = null;
  // A capacity change deferred while the modal was open is re-measured here.
  void nextTick(() => updateGalleryCapacity(true));
});

function toggleFolderPanel(): void {
  if (!narrowLayout.value) {
    folderPanelCollapsed.value = !folderPanelCollapsed.value;
    return;
  }
  if (folderDrawerOpen.value) closeFolderDrawer();
  else folderDrawerOpen.value = true;
}

function handleNarrowChange(event: MediaQueryListEvent): void {
  narrowLayout.value = event.matches;
  if (!narrowLayout.value) folderDrawerOpen.value = false;
}

function closeFolderDrawer(): void {
  if (!folderDrawerOpen.value) return;
  folderDrawerOpen.value = false;
  void nextTick(() => folderPanelToggleRef.value?.focus({ preventScroll: true }));
}

function handleFolderDrawerKeydown(event: KeyboardEvent): void {
  if (!narrowLayout.value || !folderDrawerOpen.value) return;
  event.stopPropagation();
  if (event.key === "Tab") trapTab(event, folderPanelRef.value);
  else if (event.key === "Escape") {
    event.preventDefault();
    closeFolderDrawer();
  }
}

// Picking a folder loads its images; the narrow drawer then gets out of the way.
function selectFolderFromPanel(folderKey: string): void {
  void organize.selectFolder(folderKey);
  if (narrowLayout.value) closeFolderDrawer();
}

watch(folderDrawerOpen, (open) => {
  if (open) void nextTick(() => folderPanelRef.value?.focus({ preventScroll: true }));
});

// An empty selection (or a data-source switch that clears it) must not leave a
// details pane describing images that are no longer selected.
watch(
  () => organize.selectedCount.value,
  (count) => {
    if (!count) detailsOpen.value = false;
  },
);

// The density tier is shared with the search grid: recompute the folder
// capacity whenever it changes elsewhere. Hidden (0×0) workspaces are
// skipped by updateGalleryCapacity and pick the change up when shown again.
watch(
  () => galleryDensity.density.value,
  () => updateGalleryCapacity(true),
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

// The shared overlay keeps display/geometry; this page only supplies the
// folder page data and the existing native open action.
function toViewerImage(image: OrganizeImage): ViewerImage {
  return {
    id: image.id,
    name: image.fileName,
    relativePath: image.relativePath,
    libraryName: "",
    width: image.width,
    height: image.height,
    tags: image.effectiveTags,
    thumbnailUrl: image.thumbnailUrl,
    imageUrl: image.imageUrl,
    nativeId: image.nativeImageId,
  };
}

const imageViewer = useImageViewerSession({
  items: () => organize.images.value.map(toViewerImage),
  startId: () => organize.images.value[0]?.id ?? "",
  hasMore: (direction) =>
    direction > 0
      ? organize.imagePage.value < organize.imageTotalPages.value
      : organize.imagePage.value > 1,
  loadAdjacent: async (direction) => {
    if (!(await organize.loadFolderImages(organize.imagePage.value + direction))) return null;
    const images = organize.images.value;
    if (images.length === 0) return null;
    return direction > 0 ? images[0].id : images[images.length - 1].id;
  },
});

const imageViewerPosition = computed(() => {
  const images = organize.images.value;
  const index = images.findIndex((image) => image.id === imageViewer.currentId.value);
  if (index < 0) return "";
  const absolute = (organize.imagePage.value - 1) * organize.imagePageSize.value + index + 1;
  const total = organize.imageTotal.value;
  return total > 0 ? `${absolute} / ${total}` : "";
});

const imageViewerPreloads = computed(() => {
  const images = organize.images.value;
  const index = images.findIndex((image) => image.id === imageViewer.currentId.value);
  if (index < 0) return [];
  const urls: string[] = [];
  const previous = images[index - 1];
  const next = images[index + 1];
  if (previous) urls.push(previous.imageUrl || previous.thumbnailUrl);
  if (next) urls.push(next.imageUrl || next.thumbnailUrl);
  return urls.filter(Boolean);
});

function escapeSelector(value: string): string {
  return typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(value)
    : value.replace(/["\\]/gu, "\\$&");
}

function focusTile(imageId: string): void {
  const target = imageId
    ? galleryViewport.value?.querySelector<HTMLElement>(
        `[data-image-id="${escapeSelector(imageId)}"] .image-select`,
      )
    : null;
  target?.focus({ preventScroll: true });
}

function openViewer(image: OrganizeImage): void {
  if (!image.thumbnailUrl && !image.imageUrl) {
    emit("toast", "无法查看大图", "当前图片文件不可用。", "error");
    return;
  }
  const active = document.activeElement;
  viewerDetailsFocus =
    active instanceof HTMLElement && active.closest(".gallery-details") ? active : null;
  // A capacity re-read may already be in flight; cancel it so it cannot unload
  // the window under the overlay. The capacity the gallery actually shows is
  // reported back so closing re-reads at the measured one.
  const applied = organize.cancelPendingImageLoad(true);
  if (applied !== galleryCapacity.value.pageSize) {
    galleryCapacity.value = { ...galleryCapacity.value, pageSize: applied };
  }
  imageViewer.open(image.id);
}

// Space opens the viewer for the primary selection of the focused grid.
function openViewerFromSelection(): void {
  const target =
    organize.images.value.find((image) => organize.isImageSelected(image.id)) ??
    organize.images.value[0];
  if (target) openViewer(target);
}

function closeViewer(): void {
  const imageId = imageViewer.currentId.value;
  const detailsFocus = viewerDetailsFocus;
  viewerDetailsFocus = null;
  imageViewer.close();
  // A cross-page step may still be loading; stop it so a late page cannot
  // replace the window or the selection the user returns to.
  organize.cancelPendingImageLoad();
  void nextTick(() => {
    if (detailsOpen.value && detailsFocus?.isConnected) detailsFocus.focus({ preventScroll: true });
    else focusTile(imageId);
  });
}

// Whatever closed the overlay, the capacity deferred while it was open is
// re-applied once the viewer has unmounted and released the shortcut hold.
watch(
  () => imageViewer.visible.value,
  (visible) => {
    if (!visible) void nextTick(() => updateGalleryCapacity(true));
  },
);

function openViewerNative(): void {
  const image = imageViewer.current.value;
  if (!image) return;
  if (!image.nativeId) {
    emit("toast", "无法打开原图", "当前图片文件不可用或尚未注册。", "error");
    return;
  }
  emit("openImage", image.nativeId);
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

function applyGalleryCapacity(next: OrganizeGalleryCapacity, reload: boolean): void {
  const sizeChanged = next.pageSize !== galleryCapacity.value.pageSize;
  if (!sizeChanged && next.columns === galleryCapacity.value.columns) return;
  // While a viewer overlay is open, defer the capacity change (and the page
  // reload it triggers) so the viewer window stays put; closing re-applies it.
  if (reload && isViewerOverlayOpen()) return;
  galleryCapacity.value = next;
  if (sizeChanged) void organize.setImagePageSize(next.pageSize, reload);
}

function updateGalleryCapacity(reload: boolean): void {
  const element = galleryViewport.value;
  if (!element) return;
  const width = element.clientWidth;
  const height = element.clientHeight;
  // Hidden workspaces (v-show) report 0×0; keep the last measured capacity.
  if (width <= 0 || height <= 0) return;
  // The grid's content box excludes its own horizontal padding, so tiles and
  // column counts are derived from the inner width the images really fill.
  const contentWidth = Math.max(0, width - GRID_HORIZONTAL_PADDING);
  containerWidth.value = contentWidth;
  applyGalleryCapacity(
    calculateOrganizeGalleryCapacity(contentWidth, height, galleryDensity.density.value),
    reload,
  );
}

function changeGalleryDensity(event: Event): void {
  galleryDensity.setDensity((event.target as HTMLSelectElement).value);
}

/** CSS grid column span the tile actually gets at the current column count. */
function columnSpanFor(image: OrganizeImage): 1 | 2 {
  // A single-column grid never renders the `span-2` class.
  return galleryCapacity.value.columns > 1 ? gridSpan(image.width, image.height) : 1;
}

function tileRowSpan(image: OrganizeImage): number {
  return gridRowSpan(
    image.width,
    image.height,
    columnSpanFor(image),
    containerWidth.value,
    galleryCapacity.value.columns,
    GRID_GAP,
    GALLERY_META_OVERHEAD,
  );
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

function openFolderNameTagModal(): void {
  organize.cancelFolderNameTags();
  folderNameTagScope.value = "library";
  folderNameTagMode.value = "normal";
  folderNameTagForce.value = true;
  folderNameTagModalOpen.value = true;
}

function closeFolderNameTagModal(): void {
  organize.cancelFolderNameTags();
  folderNameTagModalOpen.value = false;
}

function folderNameTagSelection(): FolderNameTagSelection | null {
  if (folderNameTagScope.value === "library") return { mode: "library" };
  if (!organize.selectedFolderKey.value) return null;
  return {
    mode: "folder",
    folder_key: organize.selectedFolderKey.value,
    include_subfolders: true,
  };
}

function requestFolderNameTagPreview(): void {
  const selection = folderNameTagSelection();
  if (selection) {
    void organize.previewFolderNameTags(selection, {
      mode: folderNameTagMode.value,
      force: folderNameTagForce.value,
    });
  }
}

async function applyFolderNameTags(): Promise<void> {
  if (await organize.applyFolderNameTags()) {
    folderNameTagModalOpen.value = false;
  }
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
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    narrowMediaQuery = window.matchMedia("(max-width: 760px)");
    narrowLayout.value = narrowMediaQuery.matches;
    narrowMediaQuery.addEventListener("change", handleNarrowChange);
  }
  initialLoadStarted = true;
  if (props.autoLoad) void organize.load();
});

onBeforeUnmount(() => {
  galleryObserver?.disconnect();
  if (galleryResizeTimer !== null) window.clearTimeout(galleryResizeTimer);
  narrowMediaQuery?.removeEventListener("change", handleNarrowChange);
  narrowMediaQuery = null;
  releaseModalOverlay?.();
  releaseModalOverlay = null;
});
</script>

<template>
  <section class="organize-page" :class="{ 'embedded-navigation': !showTabs }" aria-label="图库">
    <header v-if="showTabs" class="page-heading">
      <div>
        <p class="eyebrow">图库</p>
        <h1>浏览与整理图片</h1>
        <p>选择文件夹浏览图片，按需打开图片详情或批量标签。</p>
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

    <nav class="gallery-tools" aria-label="图库工具">
      <button ref="batchButtonRef" class="tool-button" type="button" @click="openBatchDialog">
        批量标签
      </button>
      <button
        class="tool-button"
        type="button"
        @click="requestWorkspaceTab(activeWorkspaceTab === 'clusters' ? 'batch' : 'clusters')"
      >
        {{ activeWorkspaceTab === "clusters" ? "返回图库" : "相似分组" }}
      </button>
      <button
        ref="detailsButtonRef"
        v-if="activeWorkspaceTab === 'batch'"
        class="tool-button"
        type="button"
        :disabled="!organize.selectedCount.value"
        :aria-pressed="detailsOpen"
        @click="toggleDetails"
      >
        图片详情
      </button>
    </nav>

    <div
      v-show="activeWorkspaceTab === 'batch'"
      class="batch-tag-workspace"
      :class="{
        'is-folder-collapsed': folderPanelCollapsed && !narrowLayout,
        'with-details': detailsOpen,
      }"
    >
      <div
        v-if="narrowLayout && folderDrawerOpen"
        class="folder-drawer-backdrop"
        role="presentation"
        @click="closeFolderDrawer"
      />
      <aside
        ref="folderPanelRef"
        class="folder-panel panel"
        tabindex="-1"
        :class="{ 'is-collapsed': folderPanelCollapsed && !narrowLayout, 'is-drawer-open': narrowLayout && folderDrawerOpen }"
        aria-labelledby="folder-panel-title"
        :role="narrowLayout && folderDrawerOpen ? 'dialog' : undefined"
        :aria-modal="narrowLayout && folderDrawerOpen ? true : undefined"
        @keydown="handleFolderDrawerKeydown"
      >
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
            <button class="folder-select-button" type="button" @click="selectFolderFromPanel(folder.key)">
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
          <button
            ref="folderPanelToggleRef"
            class="folder-panel-toggle"
            :class="{ 'is-drawer-entry': narrowLayout }"
            type="button"
            :aria-label="narrowLayout ? '图库导航' : (folderPanelCollapsed ? '展开文件夹导航' : '收起文件夹导航')"
            :aria-expanded="narrowLayout ? folderDrawerOpen : undefined"
            @click="toggleFolderPanel"
          >
            {{ narrowLayout ? "目录" : (folderPanelCollapsed ? "›" : "‹") }}
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
          <label class="gallery-density-control">
            <span>缩略图</span>
            <select
              :value="galleryDensity.density.value"
              aria-label="缩略图密度"
              @change="changeGalleryDensity"
            >
              <option v-for="option in galleryDensityOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </label>
        </header>

        <section
          :class="organize.selectedCount.value ? 'selection-toolbar' : 'gallery-browse-actions'"
          :aria-label="organize.selectedCount.value ? '图片选择操作' : '文件夹浏览操作'"
        >
          <div v-if="organize.selectedCount.value" class="selection-summary">
            <strong>{{ organize.selectionDescription.value }}</strong>
            <span>单击单选，Ctrl 多选，Shift 连续选择</span>
          </div>
          <div class="selection-actions">
            <button class="toolbar-button" type="button" @click="organize.selectCurrentPage">全选当前页</button>
            <button class="toolbar-button" type="button" @click="organize.selectWholeFolder">选择整文件夹</button>
            <button
              class="toolbar-button"
              type="button"
              :disabled="!organize.selectedLibraryId.value || taskSubmissionLocked"
              @click="openFolderNameTagModal"
            >
              从文件夹名生成
            </button>
            <template v-if="organize.selectedCount.value">
              <span class="selection-count" :title="`已选择 ${organize.selectedCount.value} 张图片`">{{ organize.selectedCount.value }}</span>
              <button class="toolbar-button" type="button" @click="organize.invertCurrentPage">反选当前页</button>
              <button class="toolbar-button quiet" type="button" @click="organize.clearSelection">清空</button>
            </template>
          </div>
        </section>

        <div ref="galleryViewport" class="gallery-viewport">
          <div v-if="organize.imageLoading.value && !organize.images.value.length" class="empty-state gallery-empty">
            <strong>正在读取图片</strong>
            <span>大文件夹会分页加载，不会阻塞窗口。</span>
          </div>
          <div v-else-if="!organize.images.value.length" class="empty-state gallery-empty">
            <strong>{{ organize.selectedFolderKey.value ? "当前文件夹没有图片" : "请选择文件夹" }}</strong>
            <span>缩略图会完整显示，不会裁切主体。</span>
          </div>
          <div
            v-else
            class="image-grid"
            aria-label="文件夹图片"
            :style="{ '--organize-columns': galleryCapacity.columns }"
            @keydown.space.prevent="openViewerFromSelection"
          >
            <article
              v-for="image in organize.images.value"
              :key="image.id"
              class="image-tile"
              :class="{
                selected: organize.isImageSelected(image.id),
                'span-2': columnSpanFor(image) === 2,
              }"
              :style="{ gridRow: `span ${tileRowSpan(image)}` }"
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
                <span class="thumbnail-stage" @dblclick.stop.prevent="openViewer(image)">
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

      <GalleryDetails
        v-if="detailsOpen"
        :images="organize.selectedKnownImages.value"
        :selected-count="organize.selectedCount.value"
        :description="organize.selectionDescription.value"
        @close="closeDetails"
        @open="openDetailsImage"
      />
    </div>

    <div
      v-if="batchDialogOpen"
      class="batch-dialog-backdrop"
      role="presentation"
      @click.self="closeBatchDialog"
    >
      <section
        ref="batchDialogRef"
        class="batch-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tag-editor-title"
        tabindex="-1"
        @keydown="handleDialogKeydown"
      >
        <header class="panel-heading">
          <div>
            <p class="eyebrow">标签编辑</p>
            <h2 id="tag-editor-title">批量修改</h2>
          </div>
          <div class="tag-editor-heading-actions">
            <span class="batch-selection-count">{{ organize.selectedCount.value }}</span>
            <button class="editor-close" type="button" aria-label="关闭标签设置" @click="closeBatchDialog">×</button>
          </div>
        </header>

        <section v-if="!organize.selectedCount.value" class="tag-editor-empty">
          <span aria-hidden="true">◇</span>
          <div><strong>开始批量操作</strong><p>先选择文件夹，再选中要处理的图片。</p></div>
          <div class="empty-editor-actions"><button type="button" disabled>添加标签</button><button type="button" disabled>移除标签</button><button type="button" disabled>替换人工标签</button></div>
        </section>

        <details class="tag-editor-tools" :open="Boolean(organize.selectedCount.value || organize.activeJob.value)">
        <summary>标签与别名设置</summary>
        <div class="tag-editor-tools-content">
        <section class="selection-card">
          <strong>{{ organize.selectionDescription.value }}</strong>
          <p v-if="organize.selectionPreviewIncomplete.value">
            标签统计只预览已加载的 {{ organize.selectedKnownImages.value.length }} 张；提交仍会作用于完整选择。
          </p>
          <p v-else>本次操作只影响已选图片。</p>
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
                ? (organize.activeJobKind.value === "alias"
                  ? "正在保存别名"
                  : organize.activeJobKind.value === "folder-tags"
                    ? "正在更新文件夹名称标签"
                    : "后台处理中")
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
      </section>
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

    <div
      v-if="folderNameTagModalOpen"
      class="folder-tag-modal-backdrop"
      role="presentation"
      @click.self="closeFolderNameTagModal"
    >
      <section
        class="folder-tag-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="folder-name-tag-title"
      >
        <header>
          <div>
            <p class="eyebrow">本地规则</p>
            <h2 id="folder-name-tag-title">从文件夹名称生成标签</h2>
          </div>
          <button type="button" aria-label="关闭" @click="closeFolderNameTagModal">×</button>
        </header>
        <p>
          默认会清理手工与继承标签中的黑名单、历史脏标签和大小写重复项，再完整重建文件夹来源标签。模型标签与系统元数据保持原值；不调用模型，也不会重新生成图片向量。
        </p>
        <fieldset class="folder-tag-scope">
          <legend>作用范围</legend>
          <label>
            <input
              v-model="folderNameTagScope"
              type="radio"
              value="folder"
              :disabled="!organize.selectedFolderKey.value"
              @change="organize.cancelFolderNameTags"
            />
            <span>
              <strong>当前文件夹及子文件夹</strong>
              <small>{{ organize.selectedFolder.value?.relativeFolder || "尚未选择文件夹" }}</small>
            </span>
          </label>
          <label>
            <input
              v-model="folderNameTagScope"
              type="radio"
              value="library"
              @change="organize.cancelFolderNameTags"
            />
            <span>
              <strong>整个当前图库</strong>
              <small>检查当前图库中全部已索引图片</small>
            </span>
          </label>
        </fieldset>
        <fieldset class="folder-tag-scope folder-tag-mode">
          <legend>处理方式</legend>
          <label>
            <input v-model="folderNameTagMode" type="radio" value="normal" @change="organize.cancelFolderNameTags" />
            <span><strong>清理并重建</strong><small>推荐：按当前规则完整重跑</small></span>
          </label>
          <label>
            <input v-model="folderNameTagMode" type="radio" value="clean" @change="organize.cancelFolderNameTags" />
            <span><strong>仅清理旧标签</strong><small>不改文件夹来源标签</small></span>
          </label>
          <label>
            <input v-model="folderNameTagMode" type="radio" value="mark_all" @change="organize.cancelFolderNameTags" />
            <span><strong>仅标记已处理</strong><small>不写任何图片标签</small></span>
          </label>
        </fieldset>
        <label class="folder-tag-force">
          <input v-model="folderNameTagForce" type="checkbox" @change="organize.cancelFolderNameTags" />
          <span><strong>重新处理已完成文件夹</strong><small>本次首次整库重跑保持勾选；取消后只重试失败项和新文件夹。</small></span>
        </label>
        <div class="folder-tag-preview-action">
          <button
            class="button secondary"
            type="button"
            :disabled="organize.folderNameTagPreviewLoading.value || taskSubmissionLocked"
            @click="requestFolderNameTagPreview"
          >
            {{ organize.folderNameTagPreviewLoading.value ? "正在分析…" : "生成预览" }}
          </button>
          <span>会过滤数量、容量、扩展名和黑名单目录，并在需要时向上查找父目录。</span>
        </div>
        <div
          v-if="organize.folderNameTagPreviewLoading.value"
          class="folder-tag-preview-status"
          aria-live="polite"
        >
          正在读取索引元数据并计算标签…
        </div>
        <template v-else-if="organize.folderNameTagPreview.value">
          <dl class="folder-tag-summary">
            <div>
              <dt>检查图片</dt>
              <dd>{{ organize.folderNameTagPreview.value.selected }}</dd>
            </div>
            <div>
              <dt>将更新</dt>
              <dd>{{ organize.folderNameTagPreview.value.changed }}</dd>
            </div>
            <div>
              <dt>保持不变</dt>
              <dd>{{ organize.folderNameTagPreview.value.unchanged }}</dd>
            </div>
            <div>
              <dt>涉及文件夹</dt>
              <dd>{{ organize.folderNameTagPreview.value.changedFolders }}</dd>
            </div>
            <div>
              <dt>黑名单移除</dt>
              <dd>{{ organize.folderNameTagPreview.value.removedBlacklist }}</dd>
            </div>
            <div>
              <dt>脏标签移除</dt>
              <dd>{{ organize.folderNameTagPreview.value.removedLegacy }}</dd>
            </div>
            <div>
              <dt>重复项合并</dt>
              <dd>{{ organize.folderNameTagPreview.value.removedDuplicates }}</dd>
            </div>
            <div>
              <dt>规则数</dt>
              <dd>{{ organize.folderNameTagPreview.value.blacklistCount }}</dd>
            </div>
          </dl>
          <div
            v-if="organize.folderNameTagPreview.value.samples.length"
            class="folder-tag-sample-table"
          >
            <table>
              <thead>
                <tr>
                  <th>文件夹</th>
                  <th>当前</th>
                  <th>生成后</th>
                  <th>图片</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="sample in organize.folderNameTagPreview.value.samples"
                  :key="`${sample.rootId}:${sample.relativeFolder}`"
                >
                  <td :title="sample.relativeFolder">{{ sample.relativeFolder || "根目录" }}</td>
                  <td>{{ sample.currentTags.join("、") || "无" }}</td>
                  <td>{{ sample.proposedTags.join("、") || "不生成标签" }}</td>
                  <td>{{ sample.affectedImages }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="folder-tag-no-changes">
            {{ folderNameTagMode === "mark_all" ? "当前范围将只记录处理状态，不修改标签。" : "当前范围不需要更新标签。" }}
          </p>
          <p
            v-if="organize.folderNameTagPreview.value.samplesTruncated"
            class="folder-tag-preview-note"
          >
            预览仅显示前 100 个变化文件夹，确认后仍会处理完整范围。
          </p>
        </template>
        <footer>
          <button class="button secondary" type="button" @click="closeFolderNameTagModal">
            取消
          </button>
          <button
            class="button primary"
            type="button"
            :disabled="
              (!organize.folderNameTagPreview.value?.changed && folderNameTagMode !== 'mark_all') ||
              organize.folderNameTagPreviewLoading.value ||
              taskSubmissionLocked
            "
            @click="applyFolderNameTags"
          >
            确认应用
          </button>
        </footer>
      </section>
    </div>

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

    <ImageViewer
      v-if="imageViewer.visible.value"
      :image="imageViewer.current.value"
      :can-previous="imageViewer.hasPrevious.value"
      :can-next="imageViewer.hasNext.value"
      :loading="imageViewer.loading.value"
      :error="imageViewer.error.value"
      :position-text="imageViewerPosition"
      :preload-urls="imageViewerPreloads"
      @close="closeViewer"
      @previous="imageViewer.step(-1)"
      @next="imageViewer.step(1)"
      @retry="imageViewer.retry()"
      @open-native="openViewerNative"
    />
  </section>
</template>

<style scoped>
.organize-page { display: grid; height: 100%; min-height: 0; grid-template-rows: auto auto minmax(0,1fr); gap: 12px; overflow: hidden; color: #17203a; }
.organize-page.embedded-navigation { grid-template-rows:auto minmax(0,1fr); gap:8px; }
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
.gallery-tools { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 8px; }
.gallery-browse-actions { display: flex; min-width: 0; align-items: center; gap: 8px; padding-bottom: 8px; }
.tool-button { min-height: 32px; padding: 0 12px; border: 1px solid #dedede; border-radius: 8px; color: #333; background: #f5f5f5; font: inherit; font-size: 12px; font-weight: 500; cursor: pointer; }
.tool-button[aria-pressed="true"] { border-color: #999; background: #e8e8e8; }
.organize-intelligence-workspace { height:100%; min-height:0; }
.batch-tag-workspace { display: grid; height: 100%; min-height: 0; grid-template-columns: 320px minmax(0,1fr) var(--organize-details-width, 0px); gap: 14px; }
.batch-tag-workspace.is-folder-collapsed { grid-template-columns:44px minmax(0,1fr) var(--organize-details-width, 0px); gap:10px; }
.folder-panel { display: grid; grid-template-rows: auto auto auto auto minmax(0,1fr) auto; padding: 14px; overflow: hidden; }
.folder-panel-toggle { display:grid; width:28px; height:28px; flex:0 0 auto; place-items:center; padding:0; border:0; border-radius:8px; color:#555; font:inherit; font-size:20px; background:#ededed; }
.folder-panel-toggle.is-drawer-entry { width:auto; padding:0 10px; font-size:12px; font-weight:500; }
.folder-library-control { display:grid; grid-template-columns:minmax(0,1fr) 38px; align-items:center; gap:6px; margin-top:10px; }
.folder-library-control label { min-width:0; }
.folder-library-control select { width:100%; height:38px; padding:0 8px; font-size:11px; }
.folder-library-refresh { display:grid; width:38px; height:38px; place-items:center; padding:0; border:1px solid #dedede; border-radius:9px; color:#555; font:inherit; font-size:16px; background:#f5f5f5; }
.folder-panel.is-collapsed { padding:7px; }
.folder-panel.is-collapsed .panel-heading { justify-content:center; }
.folder-panel.is-collapsed .panel-heading>div,.folder-panel.is-collapsed .panel-heading>.count-pill,.folder-panel.is-collapsed>:not(.panel-heading) { display:none; }
.gallery-panel { display: grid; grid-template-rows: auto auto minmax(0,1fr) auto; padding: 14px; overflow: hidden; }
.panel-heading h2, .gallery-heading h2 { margin: 2px 0 0; font-size: 18px; }
.count-pill, .selection-count, .batch-selection-count { display: grid; min-width: 31px; height: 31px; place-items: center; border-radius: 999px; color: #5b4bd6; background: #efedff; font-size: 12px; font-weight: 850; }
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
.gallery-density-control { display: inline-flex; align-items: center; gap: 6px; color: #6f6f6f; font-size: 11px; }
.gallery-density-control select { height: 30px; padding: 0 8px; border: 1px solid #dedede; border-radius: 8px; color: #242424; background: #fff; }
.selection-toolbar { padding:10px 14px; }
.selection-summary { display: grid; gap: 2px; }
.selection-summary strong { font-size: 13px; }
.selection-summary span { color: #7c8498; font-size: 11px; }
.selection-actions { flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.toolbar-button { border: 1px solid #dde1ec; border-radius: 8px; padding: 6px 9px; color: #515b73; background: #f8f9fc; font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; transition: .12s ease; }
.toolbar-button.quiet { color: #8a5260; background: #fff6f7; }
.gallery-viewport { min-height: 0; overflow: auto; }
.image-grid { display: grid; min-height: 0; grid-template-columns: repeat(var(--organize-columns, 3), minmax(0, 1fr)); grid-auto-flow: row dense; grid-auto-rows: 10px; gap: 9px; padding: 2px 3px 8px 1px; }
.image-tile.span-2 { grid-column: span 2; }
.image-tile { position: relative; display: grid; grid-template-rows: minmax(0,1fr) auto; min-width: 0; min-height: 0; overflow: hidden; border: 1px solid #e1e5ee; border-radius: 12px; background: #fff; content-visibility: auto; contain-intrinsic-size: 210px 168px; }
.image-tile.selected { border-color: #6d5ee3; box-shadow: 0 0 0 2px rgba(109,94,227,.16); }
.image-select { display: grid; width: 100%; height: 100%; padding: 0; border: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; grid-template-rows: minmax(0,1fr) auto; }
.selection-marker { position: absolute; z-index: 2; top: 7px; left: 7px; display: grid; width: 21px; height: 21px; place-items: center; border: 1px solid rgba(255,255,255,.7); border-radius: 7px; color: white; background: rgba(22,27,45,.45); font-size: 12px; font-weight: 900; backdrop-filter: blur(6px); }
.image-tile.selected .selection-marker { background: #6655df; }
.thumbnail-stage { display: grid; min-height: 0; height: 100%; place-items: stretch; overflow: hidden; background: linear-gradient(145deg, #151b2b, #252c3f); }
.thumbnail-stage img { width: 100%; height: 100%; object-fit: contain !important; object-position: center center; }
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
.editor-close { display: grid; width: 30px; height: 30px; place-items: center; padding: 0; border: 0; border-radius: 8px; color: #6f5260; background: #fff0f2; font-size: 17px; cursor: pointer; }
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
.folder-tag-modal-backdrop { position: fixed; z-index: 45; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(16,22,38,.48); backdrop-filter: blur(4px); }
.folder-tag-modal { display: grid; width: min(760px,100%); max-height: min(780px,calc(100vh - 48px)); gap: 14px; overflow: auto; padding: 20px; border: 1px solid #dedede; border-radius: 14px; color: #242424; background: #fff; box-shadow: 0 24px 70px rgb(0 0 0 / 18%); }
.folder-tag-modal > header,.folder-tag-modal > footer,.folder-tag-preview-action { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.folder-tag-modal > header h2 { margin: 2px 0 0; font-size: 18px; }
.folder-tag-modal > header button { width: 32px; height: 32px; border: 0; border-radius: 8px; color: #555; background: #f2f2f2; font-size: 18px; cursor: pointer; }
.folder-tag-modal > p { margin: 0; color: #646464; font-size: 12px; line-height: 1.6; }
.folder-tag-scope { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 0 18px; margin: 0; padding: 0; border: 0; }
.folder-tag-scope legend { margin-bottom: 6px; color: #666; font-size: 11px; font-weight: 700; }
.folder-tag-scope label { display: flex; align-items: flex-start; gap: 9px; padding: 10px 0; border-top: 1px solid #e8e8e8; cursor: pointer; }
.folder-tag-scope input { flex: 0 0 auto; margin-top: 2px; accent-color: #666; }
.folder-tag-scope label > span { display: grid; min-width: 0; gap: 2px; }
.folder-tag-scope strong { font-size: 12px; }
.folder-tag-scope small { overflow: hidden; color: #777; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.folder-tag-mode { grid-template-columns: repeat(3,minmax(0,1fr)); }
.folder-tag-force { display: flex; align-items: flex-start; gap: 9px; padding: 10px 0; border-top: 1px solid #e8e8e8; color: #444; cursor: pointer; }
.folder-tag-force input { width: auto; flex: 0 0 auto; margin-top: 2px; accent-color: #666; }
.folder-tag-force > span { display: grid; gap: 2px; }
.folder-tag-force small { color: #777; font-size: 10px; }
.folder-tag-preview-action { align-items: flex-start; }
.folder-tag-preview-action > span { max-width: 470px; color: #777; font-size: 10px; line-height: 1.5; }
.folder-tag-preview-status,.folder-tag-no-changes { margin: 0; padding: 12px 0; border-top: 1px solid #e8e8e8; color: #666; font-size: 11px; text-align: center; }
.folder-tag-summary { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); margin: 0; border-block: 1px solid #e8e8e8; }
.folder-tag-summary > div { padding: 10px 12px; border-right: 1px solid #e8e8e8; }
.folder-tag-summary > div:last-child { border-right: 0; }
.folder-tag-summary dt { color: #777; font-size: 10px; }
.folder-tag-summary dd { margin: 2px 0 0; font-size: 15px; font-weight: 700; }
.folder-tag-sample-table { max-height: 280px; overflow: auto; border-bottom: 1px solid #e8e8e8; }
.folder-tag-sample-table table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; }
.folder-tag-sample-table th,.folder-tag-sample-table td { padding: 8px; border-bottom: 1px solid #ededed; overflow: hidden; text-align: left; text-overflow: ellipsis; white-space: nowrap; }
.folder-tag-sample-table th { position: sticky; top: 0; color: #666; background: #f5f5f5; }
.folder-tag-sample-table th:first-child { width: 34%; }
.folder-tag-sample-table th:last-child { width: 58px; text-align: right; }
.folder-tag-sample-table td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
.folder-tag-preview-note { margin: -4px 0 0 !important; color: #76521f !important; font-size: 10px !important; }
.folder-tag-modal > footer { justify-content: flex-end; padding-top: 2px; }
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
.batch-dialog-backdrop { position: fixed; z-index: 45; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(16,22,38,.52); backdrop-filter: blur(4px); }
.batch-dialog { display: grid; width: min(560px,100%); max-height: min(820px,calc(100vh - 48px)); align-content: start; gap: 10px; overflow: auto; padding: 16px; border: 1px solid #dedede; border-radius: 14px; color: #242424; background: #fff; box-shadow: 0 24px 70px rgb(0 0 0 / 18%); }
.batch-dialog .panel-heading { padding: 0 0 10px; border-bottom: 1px solid #eceef4; }
.batch-dialog input,.batch-dialog select,.batch-dialog textarea { color: var(--text); border-color: var(--border-strong); background: var(--surface); }
.batch-dialog :is(.muted, .tag-input-label small, .selection-card p, .operation-preview p, .operation-preview small, .progress-card p, .progress-card small) { color: var(--muted); }

/* Fullscreen desktop treatment from the approved prototype. The three
   work areas are continuous white panes; controls, selection and behavior stay
   intact while the old purple dashboard framing is removed. */
.organize-page { color:#242424; }
.batch-tag-workspace { grid-template-columns:260px minmax(380px,1fr) var(--organize-details-width, 0px); gap:0; background:#fff; }
.batch-tag-workspace.with-details { --organize-details-width:300px; }
.batch-tag-workspace.is-folder-collapsed { grid-template-columns:0 minmax(380px,1fr) var(--organize-details-width, 0px); gap:0; }
.folder-panel { grid-column:1; }
.gallery-panel { grid-column:2; }
.gallery-details { grid-column:3; }
.folder-panel,.gallery-panel,.gallery-details { border:0; border-radius:0; background:#fff; box-shadow:none; }
.folder-panel { padding:14px 12px 14px 0; }
.folder-panel.is-collapsed { display:none; }
.gallery-panel { padding:14px; }
.tag-editor-empty{display:grid;min-height:calc(100% - 52px);grid-template-rows:1fr auto;place-items:center;align-content:center;gap:12px;color:#888;text-align:center}.tag-editor-empty>span{align-self:end;font-size:28px;color:#aaa}.tag-editor-empty>div:not(.empty-editor-actions){align-self:start}.tag-editor-empty strong{display:block;color:#777;font-size:13px;font-weight:400}.tag-editor-empty p{margin:5px 0 0;font-size:11px;line-height:1.5}.empty-editor-actions{align-self:end;display:grid;width:100%;gap:6px}.empty-editor-actions button{min-height:32px;border:1px solid #e5e5e5;border-radius:8px;color:#aaa;background:#fafafa}
.tag-editor-tools{margin-top:8px}.tag-editor-tools>summary{padding:7px 0;color:#777;font-size:11px;text-align:center;cursor:pointer;list-style:none}.tag-editor-tools[open]>summary{margin-bottom:6px;border-bottom:1px solid #e7e7e7}.tag-editor-tools-content{display:grid}
.panel-heading .eyebrow,.gallery-heading .eyebrow,.batch-dialog .eyebrow { display:none; }
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
.toolbar-button,.toolbar-button.quiet,.tool-button,.folder-load-more { border-color:#dedede; color:#333; background:#f5f5f5; font-weight:500; }
.image-grid { grid-template-columns: repeat(var(--organize-columns, 3), minmax(0, 1fr)); grid-auto-flow: row dense; gap: 8px; }
.image-tile.span-2 { grid-column: span 2; }
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
  /* Under this width the details pane floats over the gallery instead of
     taking a grid column, so the workspace keeps two columns. */
  .batch-tag-workspace, .batch-tag-workspace.with-details { grid-template-columns: 300px minmax(0,1fr); }
  .batch-tag-workspace.is-folder-collapsed, .batch-tag-workspace.is-folder-collapsed.with-details { grid-template-columns:0 minmax(0,1fr); }
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
  /* One real column: the folder navigation becomes a drawer opened from the
     gallery heading instead of a fixed grid cell squeezed beside the images. */
  .batch-tag-workspace,
  .batch-tag-workspace.with-details,
  .batch-tag-workspace.is-folder-collapsed,
  .batch-tag-workspace.is-folder-collapsed.with-details { grid-template-columns: minmax(0,1fr); }
  .gallery-panel { grid-column: 1; min-height: 680px; }
  .folder-panel {
    position: fixed;
    z-index: 40;
    inset: 0 auto 0 0;
    display: none;
    width: min(320px, 86vw);
    min-height: 0;
    padding: 14px;
    border-radius: 0;
    box-shadow: 0 18px 48px rgb(0 0 0 / 32%);
  }
  .folder-panel.is-drawer-open { display: grid; }
  .folder-drawer-backdrop { position: fixed; z-index: 39; inset: 0; background: rgb(0 0 0 / 32%); }
  .folder-list { min-height: 220px; }
  .selection-actions { justify-content: flex-start; }
  .delete-modal dl { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .folder-tag-modal-backdrop { padding: 12px; }
  .folder-tag-modal { max-height: calc(100vh - 24px); padding: 16px; }
  .folder-tag-scope,.folder-tag-mode,.folder-tag-summary { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .folder-tag-summary > div:nth-child(2) { border-right: 0; }
  .folder-tag-summary > div:nth-child(-n+2) { border-bottom: 1px solid #e8e8e8; }
  .folder-tag-preview-action { align-items: stretch; flex-direction: column; }
  .batch-dialog-backdrop { padding: 12px; }
  .batch-dialog { max-height: calc(100vh - 24px); }
}

@media (max-width: 520px) {
  .folder-tag-scope,.folder-tag-mode { grid-template-columns: 1fr; }
  .folder-tag-scope small { white-space: normal; }
}

@media (max-width: 620px) {
  /* Keep the folder title readable: the collapse toggle stays beside it and the
     count/density controls move to their own row instead of squeezing it. */
  .gallery-heading {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 8px 12px;
  }
  .gallery-heading > div:first-of-type { min-width: 0; }
  .gallery-metrics { justify-items: start; }
  .image-grid { padding: 2px 2px 8px 2px; }
}

@media (prefers-color-scheme: dark) {
  .folder-tag-modal { border-color: #444; color: #f2f2f2; background: #202020; }
  .folder-tag-modal > header button,.folder-tag-sample-table th { color: #ddd; background: #333; }
  .folder-tag-modal > p,.folder-tag-scope legend,.folder-tag-scope small,.folder-tag-preview-action > span,.folder-tag-preview-status,.folder-tag-no-changes,.folder-tag-summary dt { color: #bbb; }
  .folder-tag-scope label,.folder-tag-force,.folder-tag-summary,.folder-tag-summary > div,.folder-tag-sample-table,.folder-tag-sample-table th,.folder-tag-sample-table td { border-color: #3d3d3d; }
  .folder-tag-force,.folder-tag-force small { color: #bbb; }
  .gallery-heading { border-bottom-color: rgb(255 255 255 / 12%); }
  .gallery-heading h2 { color: #f2f2f2; }
  .gallery-heading small { color: #b5b5b5; }
  .gallery-metrics strong { color: #f2f2f2; }
  .gallery-metrics span { color: #b5b5b5; }
  .gallery-density-control { color: #b5b5b5; }
  .gallery-density-control select { color: #f0f0f0; border-color: rgb(255 255 255 / 18%); background: #262626; }
  .selection-toolbar { color: #d5d5d5; background: #242424; }
  .selection-summary span { color: #b5b5b5; }
  .image-tile { border-color: rgb(255 255 255 / 12%); background: #262626; }
  .image-tile.selected { border-color: #e6e6e6; outline-color: #e6e6e6; }
  .thumbnail-stage { background: linear-gradient(140deg, #232323, #1b1b1b); }
  .thumbnail-placeholder { color: #a9b0be; }
  .image-meta strong { color: #ededed; }
  .image-meta small { color: #b0b0b0; }
  .mini-tag { color: #dedede; background: #333333; }
  .mini-tag.more { color: #c4c4c4; background: #2e2e2e; }
  .no-tag { color: #a5a5a5; }
  .empty-state { color: #c0c0c0; }
  .empty-state strong { color: #ededed; }
  .gallery-tools .tool-button { color: #e2e2e2; border-color: rgb(255 255 255 / 18%); background: #2b2b2b; }
  .gallery-tools .tool-button[aria-pressed="true"] { background: #3a3a3a; }
  .batch-dialog { border-color: #3d3d3d; color: #ededed; background: #202020; }
  .batch-dialog .panel-heading { border-bottom-color: #3d3d3d; }
  .batch-dialog .selection-card,.batch-dialog .operation-preview { border-color: #3d3d3d; background: #262626; }
  .batch-dialog .tag-input-label { color: #c8c8c8; }
  .batch-dialog .tag-editor-tools > summary { color: #b5b5b5; }
  .batch-dialog .tag-editor-tools[open] > summary { border-bottom-color: #3d3d3d; }
  .batch-dialog .tag-editor-empty { color: #a5a5a5; }
  .batch-dialog .tag-editor-empty strong { color: #d5d5d5; }
  .batch-dialog .alias-dictionary { border-color: #3d3d3d; background: #262626; }
  .batch-dialog .alias-entry { color: #d5d5d5; background: #2e2e2e; }
}
</style>
