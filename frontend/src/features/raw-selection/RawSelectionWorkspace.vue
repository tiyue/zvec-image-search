<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import AppIcon from "../../components/AppIcon.vue";
import { listMembers, updateRating, updateCreativeLook, getWorkspaceState, saveWorkspaceState, exportFiles, removeMembers, permanentDelete, listCreativeLooks } from "./api";
import type { CreativeLook } from "./api";
import type { RawMember } from "./types";

const props = defineProps<{
  projectId: string;
  projectName: string;
}>();

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
  back: [];
  selectFolder: [callback: (path: string) => void];
}>();

const members = ref<RawMember[]>([]);
const currentIndex = ref(0);
const total = ref(0);
const filtered = ref(0);
const loading = ref(false);
const selectedIds = ref<Set<string>>(new Set());
const filmStripScroll = ref(0);

// Filter state
const filterStarMode = ref("none");
const filterStarValue = ref(0);
const filterColorLabels = ref("");
const filterFilename = ref("");
const filterRated = ref("all");
const sortField = ref("filename");
const sortDirection = ref("asc");

// UI state
const showFilters = ref(false);
const showSortMenu = ref(false);
const zoom = ref(1);
const panX = ref(0);
const panY = ref(0);
const isPanning = ref(false);
const lastPanX = ref(0);
const lastPanY = ref(0);
const previewError = ref(false);

// Permanent delete confirmation modal state (requirement 11.2)
const showDeleteModal = ref(false);
const deleteConfirmed = ref(false);
const deleteBusy = ref(false);
const deleteTargets = ref<RawMember[]>([]);

const deleteTotalSize = computed(() =>
  deleteTargets.value.reduce((sum, m) => sum + m.file_size, 0),
);

// Film strip virtualization (4.4): only render viewport ± 2 screens.
const FILM_ITEM_WIDTH = 86; // 84px thumb + 2px gap
const FILM_OVERSCAN_SCREENS = 2;
const filmStripEl = ref<HTMLElement | null>(null);
const filmScrollLeft = ref(0);
const filmViewportWidth = ref(800);

const filmVisibleRange = computed(() => {
  const total = members.value.length;
  if (total === 0) return { start: 0, end: 0 };
  const screenItems = Math.max(1, Math.ceil(filmViewportWidth.value / FILM_ITEM_WIDTH));
  const overscan = screenItems * FILM_OVERSCAN_SCREENS;
  const firstVisible = Math.floor(filmScrollLeft.value / FILM_ITEM_WIDTH);
  const start = Math.max(0, firstVisible - overscan);
  const end = Math.min(total, firstVisible + screenItems + overscan);
  return { start, end };
});

const filmVisibleItems = computed(() =>
  members.value
    .slice(filmVisibleRange.value.start, filmVisibleRange.value.end)
    .map((m, i) => ({ member: m, index: filmVisibleRange.value.start + i })),
);

const filmInnerWidth = computed(() => members.value.length * FILM_ITEM_WIDTH);

// Dual image comparison mode (Section 6)
const compareMode = ref(false);
const compareItems = ref<[RawMember, RawMember] | null>(null);
const compareSync = ref(true);
const compareZoomA = ref(1);
const compareZoomB = ref(1);
const comparePanA = ref({ x: 0, y: 0 });
const comparePanB = ref({ x: 0, y: 0 });
const compareRestore = ref<{ index: number; scroll: number } | null>(null);

let abortController: AbortController | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;

const currentMember = computed(() => members.value[currentIndex.value] ?? null);

const selectedCount = computed(() => selectedIds.value.size);

const selectedMemberIds = computed(() => {
  if (selectedIds.value.size > 0) return [...selectedIds.value];
  return currentMember.value ? [currentMember.value.id] : [];
});

function buildQueryParams(): Record<string, string> {
  const params: Record<string, string> = {};
  if (filterStarMode.value !== "none") {
    params["star_mode"] = filterStarMode.value;
    params["star_value"] = String(filterStarValue.value);
  }
  if (filterColorLabels.value) params["color_labels"] = filterColorLabels.value;
  if (filterFilename.value) params["filename"] = filterFilename.value;
  if (filterRated.value !== "all") params["rated"] = filterRated.value;
  params["sort"] = sortField.value;
  params["dir"] = sortDirection.value;
  return params;
}

async function loadMembers() {
  abortController?.abort();
  abortController = new AbortController();
  loading.value = true;
  try {
    const result = await listMembers(props.projectId, buildQueryParams(), abortController.signal);
    members.value = result.members;
    total.value = result.total;
    filtered.value = result.filtered;
    if (currentIndex.value >= members.value.length) {
      currentIndex.value = Math.max(0, members.value.length - 1);
    }
  } catch (err) {
    if (!(err instanceof DOMException && err.name === "AbortError")) {
      emit("toast", "加载失败", "无法获取图片列表", "error");
    }
  } finally {
    loading.value = false;
  }
}

async function loadWorkspaceState() {
  try {
    const ws = await getWorkspaceState(props.projectId);
    if (ws.last_member_id) {
      const idx = members.value.findIndex(m => m.id === ws.last_member_id);
      if (idx >= 0) currentIndex.value = idx;
    }
    filterStarMode.value = ws.filter_star_mode || "none";
    filterStarValue.value = ws.filter_star_value || 0;
    filterColorLabels.value = ws.filter_color_labels || "";
    filterFilename.value = ws.filter_filename || "";
    filterRated.value = ws.filter_rated || "all";
    sortField.value = ws.sort_field || "filename";
    sortDirection.value = ws.sort_direction || "asc";
    filmStripScroll.value = ws.filmstrip_scroll || 0;
  } catch {
    // Workspace state not yet saved
  }
}

function scheduleSaveState() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    void saveWorkspaceState(props.projectId, {
      last_member_id: currentMember.value?.id ?? null,
      filter_star_mode: filterStarMode.value,
      filter_star_value: filterStarValue.value,
      filter_color_labels: filterColorLabels.value,
      filter_filename: filterFilename.value,
      filter_rated: filterRated.value,
      sort_field: sortField.value,
      sort_direction: sortDirection.value,
      filmstrip_scroll: filmStripScroll.value,
    });
  }, 500);
}

function navigate(delta: number) {
  const next = currentIndex.value + delta;
  if (next >= 0 && next < members.value.length) {
    currentIndex.value = next;
    zoom.value = 1;
    panX.value = 0;
    panY.value = 0;
    previewError.value = false;
    scheduleSaveState();
  }
}

function toggleSelect(memberId: string) {
  const next = new Set(selectedIds.value);
  if (next.has(memberId)) next.delete(memberId);
  else next.add(memberId);
  selectedIds.value = next;
}

function selectAllFiltered() {
  selectedIds.value = new Set(members.value.map(m => m.id));
}

function clearSelection() {
  selectedIds.value = new Set();
}

async function setRating(star: number) {
  if (!currentMember.value) return;
  const memberId = currentMember.value.id;
  const oldStar = currentMember.value.star_rating;
  const oldColor = currentMember.value.color_label;
  try {
    await updateRating(memberId, star, oldColor);
    currentMember.value.star_rating = star;
  } catch {
    emit("toast", "评级失败", "无法保存星级", "error");
  }
}

async function setColorLabel(color: string) {
  if (!currentMember.value) return;
  const memberId = currentMember.value.id;
  const oldStar = currentMember.value.star_rating;
  try {
    await updateRating(memberId, oldStar, color);
    currentMember.value.color_label = color;
  } catch {
    emit("toast", "评级失败", "无法保存色标", "error");
  }
}

function handleWheel(event: WheelEvent) {
  if (event.ctrlKey) {
    event.preventDefault();
    const delta = event.deltaY > 0 ? -0.1 : 0.1;
    zoom.value = Math.max(0.1, Math.min(8, zoom.value + delta));
  }
}

function handleDoubleClick() {
  zoom.value = zoom.value === 1 ? 1 : 1;
  panX.value = 0;
  panY.value = 0;
}

function handleRightMouseDown(event: MouseEvent) {
  if (event.button !== 2) return;
  event.preventDefault();
  isPanning.value = true;
  lastPanX.value = event.clientX;
  lastPanY.value = event.clientY;
}

function handleMouseMove(event: MouseEvent) {
  if (!isPanning.value) return;
  panX.value += event.clientX - lastPanX.value;
  panY.value += event.clientY - lastPanY.value;
  lastPanX.value = event.clientX;
  lastPanY.value = event.clientY;
}

function handleMouseUp() {
  isPanning.value = false;
}

function handleKeydown(event: KeyboardEvent) {
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return;
  switch (event.key) {
    case "ArrowLeft":
      event.preventDefault();
      navigate(-1);
      break;
    case "ArrowRight":
      event.preventDefault();
      navigate(1);
      break;
    case "0": setRating(0); break;
    case "1": setRating(1); break;
    case "2": setRating(2); break;
    case "3": setRating(3); break;
    case "4": setRating(4); break;
    case "5": setRating(5); break;
    case "Escape":
      if (showDeleteModal.value) {
        closeDeleteModal();
      } else if (compareMode.value) {
        exitCompare();
      } else if (showFilters.value || showSortMenu.value) {
        showFilters.value = false;
        showSortMenu.value = false;
      } else {
        emit("back");
      }
      break;
  }
}

function handleFilmStripScroll(event: Event) {
  const target = event.target as HTMLElement;
  filmScrollLeft.value = target.scrollLeft;
  filmViewportWidth.value = target.clientWidth;
  filmStripScroll.value = target.scrollLeft;
}

function scrollFilmStripToActive() {
  const el = filmStripEl.value;
  if (!el) return;
  const left = currentIndex.value * FILM_ITEM_WIDTH;
  const visibleLeft = el.scrollLeft;
  const visibleRight = visibleLeft + el.clientWidth;
  if (left < visibleLeft || left + FILM_ITEM_WIDTH > visibleRight) {
    el.scrollLeft = Math.max(0, left - el.clientWidth / 2 + FILM_ITEM_WIDTH / 2);
  }
}

function enterCompare() {
  if (selectedIds.value.size !== 2) {
    emit(
      "toast",
      "无法对比",
      "请先选中恰好两张图片（双击胶片缩略图可切换选择）。",
      "info",
    );
    return;
  }
  const idSet = new Set(selectedIds.value);
  const picked = members.value.filter((m) => idSet.has(m.id));
  if (picked.length !== 2) return;
  compareRestore.value = {
    index: currentIndex.value,
    scroll: filmStripEl.value?.scrollLeft ?? 0,
  };
  compareItems.value = [picked[0], picked[1]];
  compareZoomA.value = 1;
  compareZoomB.value = 1;
  comparePanA.value = { x: 0, y: 0 };
  comparePanB.value = { x: 0, y: 0 };
  compareSync.value = true;
  compareMode.value = true;
}

function exitCompare() {
  compareMode.value = false;
  compareItems.value = null;
  const restore = compareRestore.value;
  compareRestore.value = null;
  if (restore) {
    currentIndex.value = restore.index;
    nextTick(() => {
      const el = filmStripEl.value;
      if (el) {
        el.scrollLeft = restore.scroll;
        filmScrollLeft.value = restore.scroll;
      }
    });
  }
}

function handleCompareWheel(event: WheelEvent) {
  if (!event.ctrlKey) return;
  event.preventDefault();
  const delta = event.deltaY > 0 ? -0.1 : 0.1;
  const target = event.target as HTMLElement;
  const sideB = target.closest(".rs-compare-side-b") !== null;
  const nextA = Math.max(0.1, Math.min(8, compareZoomA.value + delta));
  const nextB = Math.max(0.1, Math.min(8, compareZoomB.value + delta));
  if (compareSync.value) {
    const shared = sideB ? nextB : nextA;
    compareZoomA.value = shared;
    compareZoomB.value = shared;
  } else if (sideB) {
    compareZoomB.value = nextB;
  } else {
    compareZoomA.value = nextA;
  }
}

async function compareSetRating(side: 0 | 1, star: number) {
  const items = compareItems.value;
  if (!items) return;
  const m = items[side];
  try {
    await updateRating(m.id, star, m.color_label);
    m.star_rating = star;
    const main = members.value.find((x) => x.id === m.id);
    if (main) main.star_rating = star;
  } catch {
    emit("toast", "评级失败", "无法保存星级", "error");
  }
}

async function compareSetColor(side: 0 | 1, color: string) {
  const items = compareItems.value;
  if (!items) return;
  const m = items[side];
  try {
    await updateRating(m.id, m.star_rating, color);
    m.color_label = color;
    const main = members.value.find((x) => x.id === m.id);
    if (main) main.color_label = color;
  } catch {
    emit("toast", "评级失败", "无法保存色标", "error");
  }
}

function compareStyle(side: 0 | 1) {
  const zoom = side === 0 ? compareZoomA.value : compareZoomB.value;
  const pan = side === 0 ? comparePanA.value : comparePanB.value;
  return {
    transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
  };
}

function handleExport() {
  const ids = selectedMemberIds.value;
  if (!ids.length) {
    emit("toast", "未选择", "请先选择要导出的图片", "info");
    return;
  }
  emit("selectFolder", async (path: string) => {
    if (!path) return;
    try {
      const result = await exportFiles(props.projectId, ids, path);
      emit("toast", "导出完成", `成功 ${result.exported} 张，跳过 ${result.skipped}，失败 ${result.failed}`, result.failed > 0 ? "error" : "success");
    } catch {
      emit("toast", "导出失败", "无法导出文件", "error");
    }
  });
}

async function handleRemove() {
  const ids = selectedMemberIds.value;
  if (!ids.length) return;
  if (!confirm(`确定将 ${ids.length} 张图片移出项目？\n\n此操作不会删除源文件。`)) return;
  try {
    const result = await removeMembers(ids);
    emit("toast", "已移出", `${result.removed} 张图片已移出项目`, "info");
    clearSelection();
    await loadMembers();
  } catch {
    emit("toast", "操作失败", "无法移出图片", "error");
  }
}

async function handlePermanentDelete() {
  const ids = selectedMemberIds.value;
  if (!ids.length) return;
  // Collect target members with sizes for the confirmation dialog.
  const idSet = new Set(ids);
  deleteTargets.value = members.value.filter((m) => idSet.has(m.id));
  deleteConfirmed.value = false;
  showDeleteModal.value = true;
}

function closeDeleteModal() {
  if (deleteBusy.value) return;
  showDeleteModal.value = false;
  deleteTargets.value = [];
  deleteConfirmed.value = false;
}

async function confirmPermanentDelete() {
  if (!deleteConfirmed.value || deleteBusy.value) return;
  deleteBusy.value = true;
  const ids = deleteTargets.value.map((m) => m.id);
  try {
    const result = await permanentDelete(ids, true);
    emit(
      "toast",
      "删除完成",
      `已删除 ${result.deleted}，缺失 ${result.already_missing}，失败 ${result.failed}`,
      result.failed > 0 ? "error" : "success",
    );
    clearSelection();
    showDeleteModal.value = false;
    deleteTargets.value = [];
    await loadMembers();
  } catch {
    emit("toast", "删除失败", "无法执行永久删除", "error");
  } finally {
    deleteBusy.value = false;
  }
}

function formatSize(bytes: number): string {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  return `${bytes} B`;
}

const isRawFile = computed(() => {
  const ext = currentMember.value?.extension;
  return ext === ".arw";
});

const thumbnailUrl = computed(() => {
  if (!currentMember.value) return "";
  return `api/raw-selection/members/${currentMember.value.id}/thumbnail`;
});

const previewUrl = computed(() => {
  if (!currentMember.value) return "";
  const look = currentMember.value.creative_look || "as_shot";
  return `api/raw-selection/members/${currentMember.value.id}/preview?look=${encodeURIComponent(look)}`;
});

const imageUrl = computed(() => {
  if (!currentMember.value) return "";
  // For ARW without rawpy, show placeholder
  if (isRawFile.value) return previewUrl.value;
  // For JPG/PNG, use preview endpoint (no creative look re-render)
  return previewUrl.value;
});

// Creative look selector state (7.4)
const creativeLooks = ref<CreativeLook[]>([]);
const showLookMenu = ref(false);

const currentLookLabel = computed(() => {
  const look = currentMember.value?.creative_look || "as_shot";
  const found = creativeLooks.value.find((l) => l.id === look);
  return found ? found.label : look;
});

async function loadLooks() {
  try {
    const result = await listCreativeLooks();
    creativeLooks.value = result.looks;
  } catch {
    creativeLooks.value = [];
  }
}

async function setCreativeLook(lookId: string) {
  showLookMenu.value = false;
  if (!currentMember.value) return;
  // JPG/PNG pixels are final — do not re-render.
  if (!isRawFile.value) return;
  const memberId = currentMember.value.id;
  try {
    await updateCreativeLook(memberId, lookId);
    currentMember.value.creative_look = lookId;
    // Force preview reload with the new look by busting the img src.
    previewError.value = false;
  } catch {
    emit("toast", "外观切换失败", "无法保存创意外观", "error");
  }
}

const imageStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
  cursor: isPanning.value ? "grabbing" : zoom.value > 1 ? "grab" : "default",
}));

const colorLabelOptions = [
  { value: "none", label: "无", color: "transparent" },
  { value: "red", label: "红", color: "#e74c3c" },
  { value: "yellow", label: "黄", color: "#f1c40f" },
  { value: "green", label: "绿", color: "#2ecc71" },
  { value: "blue", label: "蓝", color: "#3498db" },
  { value: "purple", label: "紫", color: "#9b59b6" },
];

const sortOptions = [
  { value: "filename", label: "文件名" },
  { value: "import_order", label: "导入顺序" },
  { value: "star_rating", label: "星级" },
];

watch(currentIndex, () => {
  scheduleSaveState();
  nextTick(() => scrollFilmStripToActive());
});
watch([filterStarMode, filterStarValue, filterColorLabels, filterFilename, filterRated, sortField, sortDirection], () => {
  void loadMembers();
});

onMounted(async () => {
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("mousemove", handleMouseMove);
  window.addEventListener("mouseup", handleMouseUp);
  await loadMembers();
  await loadWorkspaceState();
  await loadLooks();
  await nextTick();
  const strip = filmStripEl.value;
  if (strip) {
    filmViewportWidth.value = strip.clientWidth;
    strip.scrollLeft = filmStripScroll.value;
    filmScrollLeft.value = strip.scrollLeft;
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleKeydown);
  window.removeEventListener("mousemove", handleMouseMove);
  window.removeEventListener("mouseup", handleMouseUp);
  abortController?.abort();
  if (saveTimer) clearTimeout(saveTimer);
});
</script>

<template>
  <div class="rs-workspace" @contextmenu.prevent>
    <!-- Top toolbar -->
    <header class="rs-toolbar">
      <button type="button" class="rs-back-btn" @click="emit('back')">
        <AppIcon name="chevron-down" :size="16" />
        <span>{{ projectName }}</span>
      </button>

      <div class="rs-info">
        <span v-if="currentMember">{{ currentIndex + 1 }} / {{ members.length }}</span>
        <span v-if="selectedCount" class="rs-selected-count">已选 {{ selectedCount }}</span>
        <span class="rs-filter-count">筛选 {{ filtered }}/{{ total }}</span>
      </div>

      <div class="rs-tools">
        <button type="button" title="适合窗口" @click="zoom = 1; panX = 0; panY = 0">适合</button>
        <button type="button" title="100%" @click="zoom = 1; panX = 0; panY = 0">100%</button>

        <!-- Star rating -->
        <div class="rs-stars">
          <button
            v-for="s in 5"
            :key="s"
            type="button"
            class="rs-star"
            :class="{ active: currentMember && currentMember.star_rating >= s }"
            @click="setRating(s)"
          >★</button>
          <button
            v-if="currentMember && currentMember.star_rating > 0"
            type="button"
            class="rs-star-clear"
            @click="setRating(0)"
          >×</button>
        </div>

        <!-- Color labels -->
        <div class="rs-colors">
          <button
            v-for="opt in colorLabelOptions"
            :key="opt.value"
            type="button"
            class="rs-color-btn"
            :class="{ active: currentMember && currentMember.color_label === opt.value }"
            :style="{ '--dot-color': opt.color }"
            :title="opt.label"
            @click="setColorLabel(opt.value)"
          />
        </div>

        <!-- Creative look selector (7.4) — ARW only -->
        <div class="rs-look-wrap">
          <button
            type="button"
            class="rs-look-btn"
            :disabled="!isRawFile"
            :title="isRawFile ? 'Sony 创意外观' : 'JPG/PNG 不适用创意外观'"
            @click="showLookMenu = !showLookMenu"
          >
            {{ isRawFile ? currentLookLabel : '外观不适用' }}
          </button>
          <div v-if="showLookMenu && isRawFile" class="rs-dropdown">
            <button
              v-for="look in creativeLooks"
              :key="look.id"
              type="button"
              :class="{ active: currentMember && currentMember.creative_look === look.id }"
              :title="look.calibration"
              @click="setCreativeLook(look.id)"
            >{{ look.label }}</button>
          </div>
        </div>

        <!-- Sort menu -->
        <div class="rs-sort-wrap">
          <button type="button" @click="showSortMenu = !showSortMenu">
            <AppIcon name="sliders" :size="15" />
          </button>
          <div v-if="showSortMenu" class="rs-dropdown">
            <button
              v-for="opt in sortOptions"
              :key="opt.value"
              type="button"
              :class="{ active: sortField === opt.value }"
              @click="sortField = opt.value; showSortMenu = false"
            >{{ opt.label }}</button>
            <hr />
            <button type="button" :class="{ active: sortDirection === 'asc' }" @click="sortDirection = 'asc'">升序</button>
            <button type="button" :class="{ active: sortDirection === 'desc' }" @click="sortDirection = 'desc'">降序</button>
          </div>
        </div>

        <button type="button" title="全选筛选结果" @click="selectAllFiltered">全选</button>
        <button
          type="button"
          title="双图对比（需选中恰好两张）"
          :disabled="selectedCount !== 2"
          @click="enterCompare"
        >对比</button>
        <button type="button" title="导出" @click="handleExport">导出</button>
        <button type="button" title="移出项目" @click="handleRemove">移出</button>
        <button type="button" class="rs-danger-btn" title="永久删除源文件" @click="handlePermanentDelete">永久删除</button>
      </div>
    </header>

    <!-- Dual image comparison view (Section 6) -->
    <div v-if="compareMode && compareItems" class="rs-compare" @wheel="handleCompareWheel">
      <div class="rs-compare-bar">
        <button type="button" @click="exitCompare">返回</button>
        <label class="rs-compare-sync">
          <input v-model="compareSync" type="checkbox" />
          <span>同步缩放</span>
        </label>
        <span class="rs-compare-hint">Ctrl+滚轮缩放 · Esc 退出</span>
      </div>
      <div class="rs-compare-panes">
        <div
          v-for="(item, side) in compareItems"
          :key="item.id"
          class="rs-compare-side"
          :class="side === 1 ? 'rs-compare-side-b' : 'rs-compare-side-a'"
        >
          <div class="rs-compare-image-wrap">
            <img
              :src="`api/raw-selection/members/${item.id}/preview`"
              :alt="item.file_name"
              class="rs-compare-img"
              :style="compareStyle(side as 0 | 1)"
              draggable="false"
            />
          </div>
          <div class="rs-compare-side-bar">
            <span class="rs-compare-name">{{ item.file_name }}</span>
            <div class="rs-stars">
              <button
                v-for="s in 5"
                :key="s"
                type="button"
                class="rs-star"
                :class="{ active: item.star_rating >= s }"
                @click="compareSetRating(side as 0 | 1, s)"
              >★</button>
            </div>
            <div class="rs-colors">
              <button
                v-for="opt in colorLabelOptions"
                :key="opt.value"
                type="button"
                class="rs-color-btn"
                :class="{ active: item.color_label === opt.value }"
                :style="{ '--dot-color': opt.color }"
                :title="opt.label"
                @click="compareSetColor(side as 0 | 1, opt.value)"
              />
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Central preview -->
    <div
      v-else
      class="rs-preview-area"
      @wheel="handleWheel"
      @dblclick="handleDoubleClick"
      @mousedown="handleRightMouseDown"
    >
      <div v-if="!currentMember && !loading" class="rs-no-image">
        <p>项目中暂无图片</p>
      </div>
      <div v-else-if="loading" class="rs-loading">加载中…</div>
      <img
        v-else-if="currentMember"
        :src="imageUrl"
        :alt="currentMember.file_name"
        class="rs-preview-img"
        :style="imageStyle"
        draggable="false"
        @error="previewError = true"
        @load="previewError = false"
      />
      <div v-if="previewError && currentMember" class="rs-raw-placeholder">
        <p>无法加载预览</p>
        <small>{{ currentMember.file_name }}（{{ isRawFile ? 'ARW 解码器未安装' : '文件可能已损坏' }}）</small>
      </div>
    </div>

    <!-- Film strip (virtualized) -->
    <div ref="filmStripEl" class="rs-film-strip" @scroll="handleFilmStripScroll">
      <div class="rs-film-strip-inner" :style="{ width: filmInnerWidth + 'px' }">
        <button
          v-for="{ member: m, index: i } in filmVisibleItems"
          :key="m.id"
          type="button"
          class="rs-thumb"
          :style="{ left: i * FILM_ITEM_WIDTH + 'px' }"
          :class="{
            active: i === currentIndex,
            selected: selectedIds.has(m.id),
          }"
          @click="currentIndex = i; zoom = 1; panX = 0; panY = 0; previewError = false"
          @dblclick="toggleSelect(m.id)"
        >
          <img
            :src="`api/raw-selection/members/${m.id}/thumbnail`"
            :alt="m.file_name"
            class="rs-thumb-img"
            loading="lazy"
            @error="(e) => ((e.target as HTMLImageElement).style.display = 'none')"
          />
          <span class="rs-thumb-name">{{ m.file_name }}</span>
          <span v-if="m.star_rating > 0" class="rs-thumb-stars">{{ '★'.repeat(m.star_rating) }}</span>
        </button>
      </div>
    </div>

    <!-- Permanent delete confirmation modal (11.2) -->
    <div
      v-if="showDeleteModal"
      class="rs-delete-backdrop"
      role="presentation"
      @click.self="closeDeleteModal"
    >
      <div class="rs-delete-modal" role="alertdialog" aria-modal="true" aria-labelledby="rs-delete-title">
        <h3 id="rs-delete-title">永久删除源文件</h3>
        <p class="rs-delete-summary">
          将永久删除 <strong>{{ deleteTargets.length }}</strong> 个源文件，
          总大小 <strong>{{ formatSize(deleteTotalSize) }}</strong>。
        </p>
        <p class="rs-delete-warning">
          这些文件不会进入回收站，删除后无法恢复。
        </p>
        <label class="rs-delete-check">
          <input
            v-model="deleteConfirmed"
            type="checkbox"
            :disabled="deleteBusy"
          />
          <span>我理解这些源文件将被永久删除</span>
        </label>
        <div class="rs-delete-actions">
          <button
            type="button"
            class="rs-delete-cancel"
            :disabled="deleteBusy"
            @click="closeDeleteModal"
          >取消</button>
          <button
            type="button"
            class="rs-delete-confirm"
            :disabled="!deleteConfirmed || deleteBusy"
            @click="confirmPermanentDelete"
          >{{ deleteBusy ? '删除中…' : '永久删除' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rs-workspace {
  display: grid;
  grid-template-rows: 44px 1fr 100px;
  height: 100%;
  overflow: hidden;
}

.rs-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.rs-back-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  min-height: 30px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
  font-size: 13px;
}

.rs-back-btn:hover {
  background: var(--surface-soft);
}

.rs-info {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--muted);
}

.rs-selected-count {
  color: var(--text);
  font-weight: 500;
}

.rs-tools {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.rs-tools button {
  padding: 0 8px;
  min-height: 28px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
}

.rs-tools button:hover {
  background: var(--surface-soft);
}

.rs-danger-btn {
  color: var(--danger) !important;
}

.rs-stars {
  display: flex;
  align-items: center;
  gap: 1px;
}

.rs-star {
  border: 0 !important;
  background: transparent !important;
  color: var(--faint);
  font-size: 16px;
  padding: 0 2px !important;
}

.rs-star.active {
  color: #e8a800;
}

.rs-star-clear {
  border: 0 !important;
  background: transparent !important;
  color: var(--muted);
  font-size: 14px;
}

.rs-colors {
  display: flex;
  align-items: center;
  gap: 2px;
}

.rs-color-btn {
  width: 20px;
  height: 20px;
  border: 1px solid var(--border) !important;
  border-radius: 50%;
  background: var(--dot-color, transparent) !important;
  padding: 0 !important;
  min-height: 20px !important;
}

.rs-color-btn.active {
  box-shadow: 0 0 0 2px var(--brand);
}

.rs-sort-wrap {
  position: relative;
}

.rs-look-wrap {
  position: relative;
}

.rs-look-btn {
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-dropdown {
  position: absolute;
  top: 32px;
  right: 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: var(--shadow-float);
}

.rs-dropdown button {
  border: 0 !important;
  background: transparent !important;
  text-align: left;
  padding: 0 10px !important;
  min-height: 28px !important;
  border-radius: 6px;
}

.rs-dropdown button:hover,
.rs-dropdown button.active {
  background: var(--surface-soft) !important;
}

.rs-dropdown hr {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 2px 0;
}

.rs-preview-area {
  display: grid;
  place-items: center;
  overflow: hidden;
  background: var(--surface-soft);
  position: relative;
  user-select: none;
}

.rs-preview-img {
  max-width: 100%;
  max-height: 100%;
  transition: transform 0.05s linear;
  pointer-events: none;
}

.rs-no-image,
.rs-loading,
.rs-raw-placeholder {
  display: grid;
  place-items: center;
  color: var(--muted);
  gap: 4px;
}

.rs-raw-placeholder small {
  font-size: 12px;
  color: var(--faint);
}

.rs-film-strip {
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
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 84px;
  padding: 4px 6px;
  border: 2px solid transparent;
  border-radius: 6px;
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  cursor: pointer;
}

.rs-thumb.active {
  border-color: var(--brand);
  background: var(--surface);
  color: var(--text);
}

.rs-thumb.selected {
  border-color: var(--success);
}

.rs-thumb-img {
  width: 100%;
  height: 60px;
  object-fit: cover;
  border-radius: 4px;
  display: block;
}

.rs-thumb-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 72px;
}

.rs-thumb-stars {
  font-size: 10px;
  color: #e8a800;
}

/* Dual image comparison */
.rs-compare {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  background: var(--surface-soft);
}

.rs-compare-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.rs-compare-bar button {
  padding: 0 10px;
  min-height: 28px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  font-size: 12px;
}

.rs-compare-sync {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}

.rs-compare-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--faint);
}

.rs-compare-panes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  flex: 1;
  min-height: 0;
  background: var(--border);
}

.rs-compare-side {
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  min-width: 0;
  min-height: 0;
  background: var(--surface-soft);
}

.rs-compare-image-wrap {
  display: grid;
  place-items: center;
  overflow: hidden;
  min-height: 0;
}

.rs-compare-img {
  max-width: 100%;
  max-height: 100%;
  pointer-events: none;
  transition: transform 0.05s linear;
}

.rs-compare-side-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

.rs-compare-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--muted);
}

/* Permanent delete confirmation modal */
.rs-delete-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 35%);
}

.rs-delete-modal {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(400px, calc(100vw - 32px));
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  box-shadow: var(--shadow-float);
}

.rs-delete-modal h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  color: var(--danger);
}

.rs-delete-summary {
  margin: 0;
  font-size: 13px;
  color: var(--text);
}

.rs-delete-warning {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}

.rs-delete-check {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-soft);
  font-size: 13px;
  cursor: pointer;
}

.rs-delete-check input {
  width: 16px;
  height: 16px;
}

.rs-delete-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.rs-delete-cancel,
.rs-delete-confirm {
  padding: 0 14px;
  min-height: 32px;
  border: 1px solid var(--border);
  border-radius: 9px;
  font-size: 13px;
}

.rs-delete-cancel {
  background: var(--surface);
  color: var(--text);
}

.rs-delete-confirm {
  background: var(--danger);
  border-color: var(--danger);
  color: #fff;
}

.rs-delete-confirm:disabled {
  opacity: 0.45;
}
</style>
