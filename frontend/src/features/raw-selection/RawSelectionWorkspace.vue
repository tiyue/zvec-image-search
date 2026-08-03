<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import AppIcon from "../../components/AppIcon.vue";
import { listMembers, updateRating, getWorkspaceState, saveWorkspaceState, exportFiles, removeMembers, permanentDelete } from "./api";
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
      if (showFilters.value || showSortMenu.value) {
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
  filmStripScroll.value = target.scrollTop;
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
  const count = ids.length;
  if (!confirm(`警告：将永久删除 ${count} 张源文件。\n\n此操作不进入回收站，无法恢复！`)) return;
  if (!confirm(`再次确认：永久删除 ${count} 张源文件？`)) return;
  try {
    const result = await permanentDelete(ids, true);
    emit("toast", "删除完成", `已删除 ${result.deleted}，缺失 ${result.already_missing}，失败 ${result.failed}`, result.failed > 0 ? "error" : "success");
    clearSelection();
    await loadMembers();
  } catch {
    emit("toast", "删除失败", "无法执行永久删除", "error");
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
  return `api/raw-selection/members/${currentMember.value.id}/preview`;
});

const imageUrl = computed(() => {
  if (!currentMember.value) return "";
  // For ARW without rawpy, show placeholder
  if (isRawFile.value) return previewUrl.value;
  // For JPG/PNG, use preview endpoint
  return previewUrl.value;
});

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

watch(currentIndex, () => scheduleSaveState());
watch([filterStarMode, filterStarValue, filterColorLabels, filterFilename, filterRated, sortField, sortDirection], () => {
  void loadMembers();
});

onMounted(async () => {
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("mousemove", handleMouseMove);
  window.addEventListener("mouseup", handleMouseUp);
  await loadMembers();
  await loadWorkspaceState();
  await nextTick();
  const strip = document.querySelector(".rs-film-strip");
  if (strip) strip.scrollTop = filmStripScroll.value;
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
        <button type="button" title="导出" @click="handleExport">导出</button>
        <button type="button" title="移出项目" @click="handleRemove">移出</button>
        <button type="button" class="rs-danger-btn" title="永久删除源文件" @click="handlePermanentDelete">永久删除</button>
      </div>
    </header>

    <!-- Central preview -->
    <div
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

    <!-- Film strip -->
    <div class="rs-film-strip" @scroll="handleFilmStripScroll">
      <div class="rs-film-strip-inner">
        <button
          v-for="(m, i) in members"
          :key="m.id"
          type="button"
          class="rs-thumb"
          :class="{
            active: i === currentIndex,
            selected: selectedIds.has(m.id),
          }"
          @click="currentIndex = i; zoom = 1; panX = 0; panY = 0"
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
  display: flex;
  gap: 2px;
  padding: 6px;
  height: 100%;
}

.rs-thumb {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-width: 80px;
  height: 100%;
  padding: 4px 6px;
  border: 2px solid transparent;
  border-radius: 6px;
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  cursor: pointer;
  flex-shrink: 0;
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
</style>
