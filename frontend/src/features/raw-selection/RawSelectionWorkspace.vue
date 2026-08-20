<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";

import AppIcon from "../../components/AppIcon.vue";
import {
  cancelJob,
  cancelProjectWork,
  exportFiles,
  getJob,
  getMember,
  getSourceStatus,
  getWorkspaceState,
  importFiles,
  importFolder,
  listCreativeLooks,
  listMembers,
  permanentDelete,
  removeMembers,
  saveWorkspaceState,
  updateCreativeLook,
  updateRating,
} from "./api";
import type { CreativeLook } from "./api";
import RawSelectionFilmstrip from "./RawSelectionFilmstrip.vue";
import type {
  ColorLabel,
  ExportResult,
  FilterExported,
  FilterRated,
  FilterStarMode,
  ImportResult,
  OrientationFilter,
  RawFormatFilter,
  RawMember,
  RawSelectionJob,
  SortDirection,
  SortField,
  StarRating,
} from "./types";
import {
  type RawViewportElements,
  type RawViewportTarget,
  useRawSelectionViewport,
} from "./useRawSelectionViewport";

const props = defineProps<{
  projectId: string;
  projectName: string;
}>();

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
  back: [];
  selectFolder: [callback: (path: string) => void];
  selectFiles: [callback: (paths: string[]) => void];
}>();

type StarFilterKind = "none" | "exact" | "at_least" | "unrated";
type MenuName = "import" | "sort" | "look" | "selection" | "more";

interface FilmstripRange {
  start: number;
  end: number;
  visibleStart: number;
  visibleEnd: number;
}

interface FilmstripController {
  scrollByWheel(delta: number): void;
  scrollToActive(): void;
  setScrollLeft(left: number): void;
}

interface CompareRestore {
  memberId: string | null;
  selectedIds: Set<string>;
  scroll: number;
}

const colorLabelOptions: Array<{ value: ColorLabel; label: string; color: string }> = [
  { value: "none", label: "无色标", color: "transparent" },
  { value: "red", label: "红", color: "#d94a52" },
  { value: "yellow", label: "黄", color: "#c99416" },
  { value: "green", label: "绿", color: "#328262" },
  { value: "blue", label: "蓝", color: "#3f70a8" },
  { value: "purple", label: "紫", color: "#80579f" },
];

const sortOptions: Array<{ value: SortField; label: string }> = [
  { value: "shot_time", label: "拍摄时间" },
  { value: "mtime_ns", label: "修改时间" },
  { value: "star_rating", label: "星级" },
  { value: "extension", label: "文件类型" },
  { value: "file_size", label: "文件大小" },
  { value: "filename", label: "文件名" },
  { value: "import_order", label: "导入顺序" },
];

const formatOptions: Array<{ value: RawFormatFilter; label: string }> = [
  { value: "arw", label: "ARW" },
  { value: "jpeg", label: "JPG/JPEG" },
  { value: "png", label: "PNG" },
];

const orientationOptions: Array<{ value: OrientationFilter; label: string }> = [
  { value: "landscape", label: "横图" },
  { value: "portrait", label: "竖图" },
  { value: "square", label: "方图" },
];

const members = ref<RawMember[]>([]);
const currentIndex = ref(0);
const total = ref(0);
const filtered = ref(0);
const loading = ref(false);
const importing = ref(false);
const importJob = ref<RawSelectionJob | null>(null);
const exportJob = ref<RawSelectionJob | null>(null);
const selectedIds = ref<Set<string>>(new Set());
const filmStripScroll = ref(0);
const filmstrip = ref<FilmstripController | null>(null);
const filmRange = ref<FilmstripRange>({ start: 0, end: 0, visibleStart: 0, visibleEnd: 0 });

const filterStarMode = ref<FilterStarMode>("none");
const filterStarValue = ref<StarRating>(0);
const filterColorLabels = ref<ColorLabel[]>([]);
const filterFilename = ref("");
const filterRated = ref<FilterRated>("all");
const filterExported = ref<FilterExported>("all");
const filterFormats = ref<RawFormatFilter[]>([]);
const filterOrientations = ref<OrientationFilter[]>([]);
const sortField = ref<SortField>("filename");
const sortDirection = ref<SortDirection>("asc");

const rootElement = ref<HTMLElement | null>(null);
const showFilters = ref(false);
const openMenu = ref<MenuName | null>(null);
let transientTrigger: HTMLElement | null = null;

const showDeleteModal = ref(false);
const deleteConfirmed = ref(false);
const deleteBusy = ref(false);
const deleteTargets = ref<RawMember[]>([]);
const deleteCheck = ref<HTMLInputElement | null>(null);
const deleteTrigger = ref<HTMLButtonElement | null>(null);

const compareMode = ref(false);
const compareItems = ref<[RawMember, RawMember] | null>(null);
const compareRestore = ref<CompareRestore | null>(null);
const singlePreviewError = ref(false);
const singlePreviewMessage = ref("");
const comparePreviewErrors = ref<[boolean, boolean]>([false, false]);
const comparePreviewMessages = ref<[string, string]>(["", ""]);
const singleDisplayedUrl = ref("");
const singleDisplayedStage = ref<"thumbnail" | "embedded" | "best">("thumbnail");
const singleDisplayedMemberId = ref("");
const compareDisplayedUrls = ref<[string, string]>(["", ""]);
const compareDisplayedStages = ref<
  ["thumbnail" | "embedded" | "best", "thumbnail" | "embedded" | "best"]
>(["thumbnail", "thumbnail"]);
const compareDisplayedMemberIds = ref<[string, string]>(["", ""]);
const viewport = useRawSelectionViewport();

const singleContainer = ref<HTMLElement | null>(null);
const singleImage = ref<HTMLImageElement | null>(null);
const compareContainers = ref<[HTMLElement | null, HTMLElement | null]>([null, null]);
const compareImages = ref<[HTMLImageElement | null, HTMLImageElement | null]>([null, null]);
const singleDisplaySize = ref({ width: 0, height: 0 });
const compareDisplaySizes = ref([{ width: 0, height: 0 }, { width: 0, height: 0 }]);

const creativeLooks = ref<CreativeLook[]>([]);

let abortController: AbortController | null = null;
let loadSequence = 0;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let hydrating = true;
let previewResizeObserver: ResizeObserver | null = null;
let intersectionObserver: IntersectionObserver | null = null;
let workspaceIntersecting = true;
const workspaceVisible = ref(true);
let prefetchController: AbortController | null = null;
let prefetchGeneration = 0;
let progressiveController: AbortController | null = null;
let progressiveGeneration = 0;
let cancelWorkPromise: Promise<unknown> | null = null;
let destroyed = false;

const currentMember = computed(() => members.value[currentIndex.value] ?? null);
const activeMember = computed(() => {
  if (!compareMode.value || !compareItems.value) return currentMember.value;
  return compareItems.value[viewport.activeCompareSide.value];
});
const activeMemberIndex = computed(() => {
  const member = activeMember.value;
  if (!member) return -1;
  return members.value.findIndex((candidate) => candidate.id === member.id);
});
const selectedCount = computed(() => selectedIds.value.size);
const compareAvailable = computed(() => selectedIds.value.size === 2);
const activeIsRaw = computed(() => activeMember.value?.extension.toLowerCase() === ".arw");
const deleteTotalSize = computed(() => deleteTargets.value.reduce(
  (sum, member) => sum + member.file_size,
  0,
));

const selectedMemberIds = computed(() => {
  if (selectedIds.value.size > 0) return [...selectedIds.value];
  return activeMember.value ? [activeMember.value.id] : [];
});

const starFilterKind = computed<StarFilterKind>({
  get() {
    if (filterRated.value === "unrated") return "unrated";
    return filterStarMode.value;
  },
  set(value) {
    if (value === "unrated") {
      filterRated.value = "unrated";
      filterStarMode.value = "none";
      filterStarValue.value = 0;
      return;
    }
    filterRated.value = "all";
    filterStarMode.value = value;
    if (value === "none") filterStarValue.value = 0;
    else if (filterStarValue.value === 0) filterStarValue.value = 1;
  },
});

const activeFilterCount = computed(() => [
  starFilterKind.value !== "none",
  filterColorLabels.value.length > 0,
  filterExported.value !== "all",
  filterFormats.value.length > 0,
  filterOrientations.value.length > 0,
  filterFilename.value.trim().length > 0,
].filter(Boolean).length);

const filterSummary = computed(() => activeFilterCount.value > 0
  ? `已筛选 ${filtered.value}/${total.value}`
  : "未筛选");

const sortSummary = computed(() => {
  const option = sortOptions.find((candidate) => candidate.value === sortField.value);
  return `${option?.label ?? "排序"} ${sortDirection.value === "asc" ? "升序" : "降序"}`;
});

const activeLookLabel = computed(() => {
  const look = activeMember.value?.creative_look || "as_shot";
  return creativeLooks.value.find((candidate) => candidate.id === look)?.label ?? look;
});

const importProgressText = computed(() => {
  const job = importJob.value;
  if (!job) return "";
  const registered = job.progress.registered ?? 0;
  const complete = job.progress.thumbnails_completed ?? 0;
  const totalThumbnails = job.progress.thumbnails_total ?? registered;
  if (job.phase === "thumbnails") {
    return `已登记 ${registered} 张 · 缩略图 ${complete}/${totalThumbnails}`;
  }
  return `正在扫描 · 已登记 ${registered} 张`;
});

const exportBusy = computed(() => {
  const status = exportJob.value?.status;
  return status === "queued" || status === "running";
});

const exportProgressText = computed(() => {
  const job = exportJob.value;
  if (!job) return "";
  const completed = job.progress.completed ?? 0;
  const totalFiles = job.progress.total ?? selectedCount.value;
  return `正在导出 ${completed}/${totalFiles}`;
});

function isActiveJob(job: RawSelectionJob): boolean {
  return job.status === "queued" || job.status === "running";
}

function jobStorageKey(kind: "import" | "export"): string {
  return `zvec-raw-${kind}-job:${props.projectId}`;
}

function rememberJob(kind: "import" | "export", jobId: string | null): void {
  try {
    if (jobId) sessionStorage.setItem(jobStorageKey(kind), jobId);
    else sessionStorage.removeItem(jobStorageKey(kind));
  } catch {
    // Session storage is optional; the backend job remains authoritative.
  }
}

function waitForPoll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 350));
}

async function monitorImportJob(initial: RawSelectionJob): Promise<void> {
  importing.value = true;
  rememberJob("import", initial.id);
  let job = initial;
  let lastRegistered = -1;
  while (!destroyed) {
    importJob.value = job;
    const registered = job.progress.registered ?? 0;
    if (registered > 0 && registered !== lastRegistered) {
      lastRegistered = registered;
      await loadMembers(currentMember.value?.id ?? null);
    }
    if (!isActiveJob(job)) break;
    await waitForPoll();
    try {
      job = await getJob(job.id);
    } catch {
      importJob.value = null;
      importing.value = false;
      rememberJob("import", null);
      emit("toast", "导入状态丢失", "后台任务已停止或应用已重启。", "error");
      return;
    }
  }
  if (destroyed) return;
  importJob.value = job;
  importing.value = false;
  rememberJob("import", null);
  await loadMembers(currentMember.value?.id ?? null);
  if (job.status === "failed") {
    emit("toast", "导入失败", job.error || "无法导入文件夹", "error");
    return;
  }
  const result = job.result as ImportResult | null;
  if (job.status === "cancelled") {
    emit("toast", "导入已取消", `已登记 ${result?.registered ?? 0} 张`, "info");
    return;
  }
  if (result) {
    const skipped = result.skipped_unsupported
      + result.skipped_raw_formats
      + result.skipped_unsupported_camera;
    emit(
      "toast",
      "导入完成",
      `已登记 ${result.registered} 张，跳过 ${skipped} 张，失败 ${result.errors} 张`,
      result.errors > 0 ? "error" : result.registered > 0 ? "success" : "info",
    );
  }
}

async function monitorExportJob(initial: RawSelectionJob): Promise<void> {
  exportJob.value = initial;
  rememberJob("export", initial.id);
  let job = initial;
  while (!destroyed && isActiveJob(job)) {
    await waitForPoll();
    try {
      job = await getJob(job.id);
      exportJob.value = job;
    } catch {
      exportJob.value = null;
      rememberJob("export", null);
      emit("toast", "导出状态丢失", "后台任务已停止或应用已重启。", "error");
      return;
    }
  }
  if (destroyed) return;
  rememberJob("export", null);
  if (job.status === "failed") {
    emit("toast", "导出失败", job.error || "无法导出文件", "error");
    return;
  }
  const result = job.result as ExportResult | null;
  if (job.status === "cancelled") {
    emit("toast", "导出已取消", `已完成 ${result?.exported ?? 0} 张`, "info");
  } else if (result) {
    emit(
      "toast",
      "导出完成",
      `成功 ${result.exported} 张，跳过 ${result.skipped} 张，失败 ${result.failed} 张`,
      result.failed > 0 ? "error" : "success",
    );
  }
  await loadMembers(currentMember.value?.id ?? null);
}

async function resumeRememberedJob(kind: "import" | "export"): Promise<void> {
  let jobId: string | null = null;
  try {
    jobId = sessionStorage.getItem(jobStorageKey(kind));
  } catch {
    return;
  }
  if (!jobId) return;
  try {
    const job = await getJob(jobId);
    if (job.project_id !== props.projectId || job.kind !== kind) {
      rememberJob(kind, null);
      return;
    }
    if (kind === "import") void monitorImportJob(job);
    else void monitorExportJob(job);
  } catch {
    rememberJob(kind, null);
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function sourceVersion(member: RawMember): string {
  return `${member.file_size}-${member.mtime_ns}-${member.file_identity ?? ""}`;
}

function isRaw(member: RawMember | null): boolean {
  return member?.extension.toLowerCase() === ".arw";
}

function previewUrl(
  member: RawMember,
  priority: "current" | "compare" | "adjacent" = "current",
  size = { width: 0, height: 0 },
  quality: "embedded" | "best" = "best",
): string {
  const query = new URLSearchParams({
    look: isRaw(member) ? member.creative_look || "as_shot" : "as_shot",
    priority,
    v: sourceVersion(member),
    quality,
  });
  if (size.width > 0) query.set("dw", String(Math.round(size.width)));
  if (size.height > 0) query.set("dh", String(Math.round(size.height)));
  return `api/raw-selection/members/${member.id}/preview?${query}`;
}

function thumbnailPrefetchUrl(member: RawMember, priority: "visible" | "overscan"): string {
  const query = new URLSearchParams({ priority, v: sourceVersion(member) });
  return `api/raw-selection/members/${member.id}/thumbnail?${query}`;
}

const singleImageUrl = computed(() => currentMember.value
  ? previewUrl(currentMember.value, "current", singleDisplaySize.value)
  : "");

function compareImageUrl(member: RawMember, side: 0 | 1): string {
  return previewUrl(member, side === 0 ? "current" : "compare", compareDisplaySizes.value[side]);
}

function buildQueryParams(): Record<string, string> {
  const params: Record<string, string> = {
    dir: sortDirection.value,
    limit: "10000",
    sort: sortField.value,
  };
  if (filterStarMode.value !== "none") {
    params.star_mode = filterStarMode.value;
    params.star_value = String(filterStarValue.value);
  }
  if (filterRated.value !== "all") params.rated = filterRated.value;
  if (filterColorLabels.value.length) params.color_labels = filterColorLabels.value.join(",");
  if (filterFilename.value.trim()) params.filename = filterFilename.value.trim();
  if (filterExported.value !== "all") params.exported = filterExported.value;
  if (filterFormats.value.length) params.formats = filterFormats.value.join(",");
  if (filterOrientations.value.length) params.orientations = filterOrientations.value.join(",");
  return params;
}

async function loadMembers(
  preferredMemberId: string | null = currentMember.value?.id ?? null,
): Promise<void> {
  const request = ++loadSequence;
  abortController?.abort();
  const controller = new AbortController();
  abortController = controller;
  loading.value = true;
  try {
    const result = await listMembers(props.projectId, buildQueryParams(), controller.signal);
    if (request !== loadSequence || controller.signal.aborted) return;
    members.value = result.members;
    total.value = result.total;
    filtered.value = result.filtered;
    const restoredIndex = preferredMemberId
      ? result.members.findIndex((member) => member.id === preferredMemberId)
      : -1;
    currentIndex.value = restoredIndex >= 0 ? restoredIndex : 0;
    singlePreviewError.value = false;
  } catch (error) {
    if (!isAbortError(error)) emit("toast", "加载失败", "无法获取图片列表", "error");
  } finally {
    if (request === loadSequence) loading.value = false;
  }
}

function parseCsv<T extends string>(value: unknown, allowed: readonly T[]): T[] {
  if (typeof value !== "string") return [];
  const allowedValues = new Set<string>(allowed);
  return value.split(",").map((item) => item.trim()).filter(
    (item): item is T => allowedValues.has(item),
  );
}

async function loadWorkspace(): Promise<string | null> {
  try {
    const state = await getWorkspaceState(props.projectId);
    filterStarMode.value = ["none", "exact", "at_least"].includes(state.filter_star_mode)
      ? state.filter_star_mode as FilterStarMode
      : "none";
    filterStarValue.value = Math.max(0, Math.min(5, Number(state.filter_star_value) || 0)) as StarRating;
    filterColorLabels.value = parseCsv(
      state.filter_color_labels,
      colorLabelOptions.slice(1).map((option) => option.value),
    );
    filterFilename.value = state.filter_filename || "";
    filterRated.value = ["all", "rated", "unrated"].includes(state.filter_rated)
      ? state.filter_rated as FilterRated
      : "all";
    if (filterRated.value === "rated") {
      filterRated.value = "all";
      filterStarMode.value = "at_least";
      filterStarValue.value = 1;
    } else if (filterRated.value === "unrated") {
      filterStarMode.value = "none";
      filterStarValue.value = 0;
    } else if (filterStarMode.value !== "none" && filterStarValue.value === 0) {
      filterStarValue.value = 1;
    }
    filterExported.value = ["all", "exported", "unexported"].includes(state.filter_exported)
      ? state.filter_exported as FilterExported
      : "all";
    filterFormats.value = parseCsv(state.filter_formats, formatOptions.map((option) => option.value));
    filterOrientations.value = parseCsv(
      state.filter_orientations,
      orientationOptions.map((option) => option.value),
    );
    sortField.value = sortOptions.some((option) => option.value === state.sort_field)
      ? state.sort_field as SortField
      : "filename";
    sortDirection.value = state.sort_direction === "desc" ? "desc" : "asc";
    filmStripScroll.value = Math.max(0, Number(state.filmstrip_scroll) || 0);
    return state.last_member_id || null;
  } catch {
    return null;
  }
}

function saveState(): void {
  void saveWorkspaceState(props.projectId, {
    last_member_id: currentMember.value?.id ?? null,
    filter_star_mode: filterStarMode.value,
    filter_star_value: filterStarValue.value,
    filter_color_labels: filterColorLabels.value.join(","),
    filter_filename: filterFilename.value,
    filter_rated: filterRated.value,
    filter_exported: filterExported.value,
    filter_formats: filterFormats.value.join(","),
    filter_orientations: filterOrientations.value.join(","),
    sort_field: sortField.value,
    sort_direction: sortDirection.value,
    filmstrip_scroll: filmStripScroll.value,
  }).catch(() => undefined);
}

function scheduleSaveState(): void {
  if (hydrating) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveState, 400);
}

function closeTransient(restoreFocus = false): void {
  const trigger = transientTrigger;
  const filtersWereOpen = showFilters.value;
  openMenu.value = null;
  showFilters.value = false;
  transientTrigger = null;
  if (filtersWereOpen) void nextTick(fitActive);
  if (restoreFocus && trigger) void nextTick(() => trigger.focus({ preventScroll: true }));
}

function toggleMenu(name: MenuName, event: MouseEvent): void {
  const trigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  if (openMenu.value === name) {
    closeTransient(true);
    return;
  }
  const filtersWereOpen = showFilters.value;
  showFilters.value = false;
  openMenu.value = name;
  transientTrigger = trigger;
  if (filtersWereOpen) void nextTick(fitActive);
}

function toggleFilters(event: MouseEvent): void {
  const trigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  if (showFilters.value) {
    closeTransient(true);
    return;
  }
  openMenu.value = null;
  showFilters.value = true;
  transientTrigger = trigger;
  void nextTick(fitActive);
}

function toggleArrayValue<T extends string>(values: T[], value: T): T[] {
  return values.includes(value)
    ? values.filter((candidate) => candidate !== value)
    : [...values, value];
}

function toggleFilterColor(value: ColorLabel): void {
  if (value === "none") return;
  filterColorLabels.value = toggleArrayValue(filterColorLabels.value, value);
}

function clearFilters(): void {
  filterStarMode.value = "none";
  filterStarValue.value = 0;
  filterRated.value = "all";
  filterColorLabels.value = [];
  filterFilename.value = "";
  filterExported.value = "all";
  filterFormats.value = [];
  filterOrientations.value = [];
}

function activateMember(index: number): void {
  if (index < 0 || index >= members.value.length) return;
  currentIndex.value = index;
  viewport.fit("single", elementsFor("single"));
  singlePreviewError.value = false;
}

function navigate(delta: number): void {
  if (compareMode.value) return;
  const next = currentIndex.value + delta;
  if (next >= 0 && next < members.value.length) activateMember(next);
}

function selectAllFiltered(): void {
  selectedIds.value = new Set(members.value.map((member) => member.id));
  closeTransient(true);
}

function clearSelection(): void {
  selectedIds.value = new Set();
  closeTransient(true);
}

function updateMemberLocally(memberId: string, patch: Partial<RawMember>): void {
  const main = members.value.find((candidate) => candidate.id === memberId);
  if (main) Object.assign(main, patch);
  const compared = compareItems.value?.find((candidate) => candidate.id === memberId);
  if (compared && compared !== main) Object.assign(compared, patch);
}

async function setRating(star: number): Promise<void> {
  const member = activeMember.value;
  if (!member || !Number.isInteger(star) || star < 0 || star > 5) return;
  try {
    await updateRating(member.id, star, member.color_label);
    updateMemberLocally(member.id, { star_rating: star });
    if (
      filterStarMode.value !== "none"
      || filterRated.value !== "all"
      || sortField.value === "star_rating"
    ) await loadMembers(member.id);
  } catch {
    emit("toast", "评级失败", "无法保存星级", "error");
  }
}

async function setColorLabel(color: ColorLabel): Promise<void> {
  const member = activeMember.value;
  if (!member) return;
  try {
    await updateRating(member.id, member.star_rating, color);
    updateMemberLocally(member.id, { color_label: color });
    if (filterColorLabels.value.length) await loadMembers(member.id);
  } catch {
    emit("toast", "评级失败", "无法保存色标", "error");
  }
}

async function setCreativeLook(lookId: string): Promise<void> {
  const member = activeMember.value;
  closeTransient(true);
  if (!member || !isRaw(member)) return;
  try {
    await updateCreativeLook(member.id, lookId);
    updateMemberLocally(member.id, { creative_look: lookId });
    if (compareMode.value) {
      const errors = [...comparePreviewErrors.value] as [boolean, boolean];
      errors[viewport.activeCompareSide.value] = false;
      comparePreviewErrors.value = errors;
    } else {
      singlePreviewError.value = false;
    }
    schedulePrefetch();
  } catch {
    emit("toast", "外观切换失败", "无法保存 Sony 创意外观", "error");
  }
}

async function loadSelectedMembers(ids: string[]): Promise<RawMember[]> {
  const visible = new Map(members.value.map((member) => [member.id, member]));
  const result = await Promise.all(ids.map(async (id) => visible.get(id) ?? getMember(id)));
  return result.filter((member) => member.project_id === props.projectId);
}

async function enterCompare(): Promise<void> {
  if (selectedIds.value.size !== 2) {
    emit("toast", "无法对比", "请先明确选择恰好两张图片。", "info");
    return;
  }
  try {
    const ids = [...selectedIds.value];
    const picked = await loadSelectedMembers(ids);
    if (picked.length !== 2) {
      emit("toast", "无法对比", "选择中包含已移出项目的图片。", "error");
      return;
    }
    picked.sort((left, right) => {
      const leftIndex = members.value.findIndex((member) => member.id === left.id);
      const rightIndex = members.value.findIndex((member) => member.id === right.id);
      return (leftIndex < 0 ? Number.MAX_SAFE_INTEGER : leftIndex)
        - (rightIndex < 0 ? Number.MAX_SAFE_INTEGER : rightIndex);
    });
    compareRestore.value = {
      memberId: currentMember.value?.id ?? null,
      selectedIds: new Set(selectedIds.value),
      scroll: filmStripScroll.value,
    };
    compareItems.value = [picked[0], picked[1]];
    comparePreviewErrors.value = [false, false];
    viewport.resetCompare();
    compareMode.value = true;
    closeTransient(false);
    await nextTick();
    measurePreviewContainers();
  } catch {
    emit("toast", "无法对比", "无法读取所选图片。", "error");
  }
}

function exitCompare(restore = true): void {
  compareMode.value = false;
  compareItems.value = null;
  viewport.resetCompare();
  viewport.fit("single", elementsFor("single"));
  const snapshot = compareRestore.value;
  compareRestore.value = null;
  if (!restore || !snapshot) return;
  selectedIds.value = new Set(snapshot.selectedIds);
  if (snapshot.memberId) {
    const index = members.value.findIndex((member) => member.id === snapshot.memberId);
    if (index >= 0) currentIndex.value = index;
  }
  filmStripScroll.value = snapshot.scroll;
  void nextTick(() => filmstrip.value?.setScrollLeft(snapshot.scroll));
}

function viewportTarget(): RawViewportTarget {
  return compareMode.value ? viewport.activeCompareSide.value : "single";
}

function elementsFor(target: RawViewportTarget): RawViewportElements {
  if (target === "single") {
    return { container: singleContainer.value, image: singleImage.value };
  }
  return { container: compareContainers.value[target], image: compareImages.value[target] };
}

function peerElements(target: RawViewportTarget): RawViewportElements | undefined {
  if (target === "single") return undefined;
  return elementsFor(target === 0 ? 1 : 0);
}

function fitActive(): void {
  const target = viewportTarget();
  viewport.fit(target, elementsFor(target), peerElements(target));
}

function actualSizeActive(): void {
  const target = viewportTarget();
  viewport.showActualSize(target, elementsFor(target), peerElements(target));
}

function handleImageWheel(target: RawViewportTarget, event: WheelEvent): void {
  if (viewport.handleWheel(target, event, elementsFor(target), peerElements(target))) return;
  event.preventDefault();
  filmstrip.value?.scrollByWheel(event.deltaY || event.deltaX);
}

function handleImageDoubleClick(target: RawViewportTarget, event: MouseEvent): void {
  viewport.handleDoubleClick(target, event, elementsFor(target), peerElements(target));
}

function handleImageMouseDown(target: RawViewportTarget, event: MouseEvent): void {
  if (target !== "single") viewport.activeCompareSide.value = target;
  viewport.startPan(target, event, elementsFor(target));
}

function setCompareContainer(side: number, element: unknown): void {
  const resolved = element instanceof HTMLElement ? element : null;
  if (compareContainers.value[side] === resolved) return;
  const previous = compareContainers.value[side];
  if (previous) previewResizeObserver?.unobserve(previous);
  const next = [...compareContainers.value] as [HTMLElement | null, HTMLElement | null];
  next[side] = resolved;
  compareContainers.value = next;
  if (next[side]) previewResizeObserver?.observe(next[side] as HTMLElement);
}

function setCompareImage(side: number, element: unknown): void {
  const resolved = element instanceof HTMLImageElement ? element : null;
  if (compareImages.value[side] === resolved) return;
  const next = [...compareImages.value] as [HTMLImageElement | null, HTMLImageElement | null];
  next[side] = resolved;
  compareImages.value = next;
}

function measurePreviewContainers(): void {
  if (singleContainer.value) {
    const nextSingleSize = {
      width: singleContainer.value.clientWidth,
      height: singleContainer.value.clientHeight,
    };
    if (
      nextSingleSize.width !== singleDisplaySize.value.width
      || nextSingleSize.height !== singleDisplaySize.value.height
    ) {
      singleDisplaySize.value = nextSingleSize;
    }
  }
  const nextCompareSizes = compareContainers.value.map((container) => ({
    width: container?.clientWidth ?? 0,
    height: container?.clientHeight ?? 0,
  })) as [{ width: number; height: number }, { width: number; height: number }];
  if (nextCompareSizes.some((size, side) => (
    size.width !== compareDisplaySizes.value[side].width
    || size.height !== compareDisplaySizes.value[side].height
  ))) {
    compareDisplaySizes.value = nextCompareSizes;
  }
  viewport.handleResize("single", elementsFor("single"));
  viewport.handleResize(0, elementsFor(0));
  viewport.handleResize(1, elementsFor(1));
}

function setCompareError(side: number, failed: boolean): void {
  const next = [...comparePreviewErrors.value] as [boolean, boolean];
  next[side] = failed;
  comparePreviewErrors.value = next;
}

function handleSingleImageLoad(): void {
  singlePreviewError.value = false;
  singlePreviewMessage.value = "";
  measurePreviewContainers();
  if (singleDisplayedStage.value === "thumbnail") {
    viewport.fit("single", elementsFor("single"));
  }
}

function handleCompareImageLoad(side: number): void {
  setCompareError(side, false);
  const messages = [...comparePreviewMessages.value] as [string, string];
  messages[side] = "";
  comparePreviewMessages.value = messages;
  measurePreviewContainers();
  if (compareDisplayedStages.value[side] === "thumbnail") {
    viewport.fit(side as 0 | 1, elementsFor(side as 0 | 1));
  }
}

function handleSingleDisplayedError(): void {
  if (singleDisplayedStage.value === "best") void handleSingleImageError();
}

function handleCompareDisplayedError(side: 0 | 1): void {
  if (compareDisplayedStages.value[side] === "best") {
    void handleCompareImageError(side);
  }
}

async function handleSingleImageError(): Promise<void> {
  const member = currentMember.value;
  if (!member) return;
  singlePreviewError.value = true;
  singlePreviewMessage.value = "正在检查源文件…";
  try {
    const status = await getSourceStatus(member.id);
    singlePreviewMessage.value = status.message;
    if (status.status === "refreshed") {
      const refreshed = await getMember(member.id);
      updateMemberLocally(member.id, refreshed);
      singlePreviewError.value = false;
      singlePreviewMessage.value = "";
    }
  } catch {
    singlePreviewMessage.value = "预览解码失败。";
  }
}

async function handleCompareImageError(side: 0 | 1): Promise<void> {
  const member = compareItems.value?.[side];
  if (!member) return;
  setCompareError(side, true);
  const messages = [...comparePreviewMessages.value] as [string, string];
  messages[side] = "正在检查源文件…";
  comparePreviewMessages.value = messages;
  try {
    const status = await getSourceStatus(member.id);
    const next = [...comparePreviewMessages.value] as [string, string];
    next[side] = status.message;
    comparePreviewMessages.value = next;
    if (status.status === "refreshed") {
      const refreshed = await getMember(member.id);
      updateMemberLocally(member.id, refreshed);
      setCompareError(side, false);
    }
  } catch {
    const next = [...comparePreviewMessages.value] as [string, string];
    next[side] = "预览解码失败。";
    comparePreviewMessages.value = next;
  }
}

function handleImportFolder(): void {
  closeTransient(true);
  importing.value = true;
  emit("selectFolder", async (path: string) => {
    if (!path) {
      importing.value = false;
      return;
    }
    try {
      const job = await importFolder(props.projectId, path);
      emit("toast", "开始导入", "首批图片登记后会立即显示。", "info");
      void monitorImportJob(job);
    } catch {
      emit("toast", "导入失败", "无法导入文件夹", "error");
      importing.value = false;
    }
  });
}

function handleImportFiles(): void {
  closeTransient(true);
  importing.value = true;
  emit("selectFiles", async (paths: string[]) => {
    if (!paths.length) {
      importing.value = false;
      return;
    }
    try {
      const result = await importFiles(props.projectId, paths);
      emit(
        "toast",
        "导入完成",
        `已登记 ${result.registered} 张，跳过 ${result.skipped_unsupported + result.skipped_raw_formats + result.skipped_unsupported_camera} 张，失败 ${result.errors} 张`,
        result.errors > 0 ? "error" : result.registered > 0 ? "success" : "info",
      );
      await loadMembers();
    } catch {
      emit("toast", "导入失败", "无法导入所选图片", "error");
    } finally {
      importing.value = false;
    }
  });
}

async function handleExport(): Promise<void> {
  const ids = selectedMemberIds.value;
  if (!ids.length) {
    emit("toast", "未选择", "请先选择要导出的图片", "info");
    return;
  }
  emit("selectFolder", async (path: string) => {
    if (!path) return;
    try {
      const job = await exportFiles(props.projectId, ids, path);
      void monitorExportJob(job);
    } catch {
      emit("toast", "导出失败", "无法导出文件", "error");
    }
  });
}

async function cancelImportJob(): Promise<void> {
  if (!importJob.value || !isActiveJob(importJob.value)) return;
  await cancelJob(importJob.value.id);
}

async function cancelExportJob(): Promise<void> {
  if (!exportJob.value || !isActiveJob(exportJob.value)) return;
  await cancelJob(exportJob.value.id);
}

async function handleRemove(): Promise<void> {
  const ids = selectedMemberIds.value;
  if (!ids.length) return;
  if (!confirm(`确定将 ${ids.length} 张图片移出项目？\n\n此操作不会删除源文件。`)) return;
  try {
    const result = await removeMembers(ids);
    emit("toast", "已移出", `${result.removed} 张图片已移出项目`, "info");
    exitCompare(false);
    selectedIds.value = new Set();
    await loadMembers();
  } catch {
    emit("toast", "操作失败", "无法移出图片", "error");
  }
}

function handleRemoveFromMenu(): void {
  closeTransient(true);
  void handleRemove();
}

async function handlePermanentDelete(): Promise<void> {
  const ids = selectedMemberIds.value;
  if (!ids.length) return;
  try {
    const targets = await loadSelectedMembers(ids);
    if (targets.length !== ids.length) {
      emit("toast", "无法删除", "部分图片已不属于当前项目，请刷新后重试。", "error");
      return;
    }
    deleteTargets.value = targets;
    deleteConfirmed.value = false;
    showDeleteModal.value = true;
    await nextTick();
    deleteCheck.value?.focus({ preventScroll: true });
  } catch {
    emit("toast", "无法删除", "无法读取待删除图片。", "error");
  }
}

function handlePermanentDeleteFromMenu(): void {
  closeTransient(true);
  void handlePermanentDelete();
}

function closeDeleteModal(restoreFocus = true): void {
  if (deleteBusy.value) return;
  showDeleteModal.value = false;
  deleteTargets.value = [];
  deleteConfirmed.value = false;
  if (restoreFocus) void nextTick(() => deleteTrigger.value?.focus({ preventScroll: true }));
}

async function confirmPermanentDelete(): Promise<void> {
  if (!deleteConfirmed.value || deleteBusy.value) return;
  deleteBusy.value = true;
  const attemptedIds = new Set(deleteTargets.value.map((member) => member.id));
  try {
    const result = await permanentDelete([...attemptedIds], true);
    emit(
      "toast",
      "删除完成",
      `已删除 ${result.deleted}，缺失 ${result.already_missing}，失败 ${result.failed}`,
      result.failed > 0 ? "error" : "success",
    );
    showDeleteModal.value = false;
    deleteTargets.value = [];
    exitCompare(false);
    await loadMembers();
    selectedIds.value = new Set(
      members.value.filter((member) => attemptedIds.has(member.id)).map((member) => member.id),
    );
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

function handleFilmScroll(left: number): void {
  filmStripScroll.value = left;
  scheduleSaveState();
}

function handleWindowPointerDown(event: PointerEvent): void {
  const target = event.target;
  if (target instanceof Element && target.closest("[data-raw-transient]")) return;
  if (openMenu.value || showFilters.value) closeTransient(false);
}

function handleWindowResize(): void {
  viewport.endPan();
  if (openMenu.value || showFilters.value) closeTransient(true);
  void nextTick(measurePreviewContainers);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    if (showDeleteModal.value) {
      if (!deleteBusy.value) {
        event.preventDefault();
        closeDeleteModal(true);
      }
      return;
    }
    if (openMenu.value || showFilters.value) {
      event.preventDefault();
      closeTransient(true);
      return;
    }
    if (compareMode.value) {
      event.preventDefault();
      exitCompare(true);
      return;
    }
  }

  const target = event.target;
  const editing = target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement
    || (target instanceof HTMLElement && target.isContentEditable);
  if (editing) return;

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    navigate(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    navigate(1);
  } else if (/^[0-5]$/.test(event.key)) {
    event.preventDefault();
    void setRating(Number(event.key));
  }
}

function preloadImage(
  url: string,
  signal: AbortSignal,
  priority: "high" | "low",
): Promise<HTMLImageElement> {
  if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const image = new Image();
    (image as HTMLImageElement & { fetchPriority?: string }).fetchPriority = priority;
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      image.onload = null;
      image.onerror = null;
      if (error) reject(error);
      else resolve(image);
    };
    const abort = () => {
      image.src = "";
      finish(new DOMException("Aborted", "AbortError"));
    };
    image.onload = () => finish();
    image.onerror = () => finish(new Error("Image preload failed"));
    signal.addEventListener("abort", abort, { once: true });
    image.src = url;
  });
}

function fitPreloadedSingle(image: HTMLImageElement): void {
  if (
    viewport.single.mode !== "fit"
    || !Number.isFinite(image.naturalWidth)
    || !Number.isFinite(image.naturalHeight)
    || image.naturalWidth <= 0
    || image.naturalHeight <= 0
  ) return;
  viewport.fit("single", { container: singleContainer.value, image });
}

function fitPreloadedCompare(side: 0 | 1, image: HTMLImageElement): void {
  if (
    viewport.compare[side].mode !== "fit"
    || !Number.isFinite(image.naturalWidth)
    || !Number.isFinite(image.naturalHeight)
    || image.naturalWidth <= 0
    || image.naturalHeight <= 0
  ) return;
  const peer = side === 0 ? 1 : 0;
  viewport.fit(
    side,
    { container: compareContainers.value[side], image },
    { container: compareContainers.value[peer], image: compareImages.value[peer] },
  );
}

async function progressSinglePreview(
  member: RawMember,
  generation: number,
  controller: AbortController,
): Promise<void> {
  if (singleDisplayedMemberId.value !== member.id || !singleDisplayedUrl.value) {
    singleDisplayedStage.value = "thumbnail";
    singleDisplayedUrl.value = thumbnailPrefetchUrl(member, "visible");
    singleDisplayedMemberId.value = member.id;
  }
  singlePreviewError.value = false;
  singlePreviewMessage.value = "";

  const embedded = previewUrl(
    member,
    "current",
    singleDisplaySize.value,
    "embedded",
  );
  try {
    const embeddedImage = await preloadImage(embedded, controller.signal, "high");
    if (controller.signal.aborted || generation !== progressiveGeneration) return;
    if (singleDisplayedStage.value !== "best") {
      fitPreloadedSingle(embeddedImage);
      singleDisplayedStage.value = "embedded";
      singleDisplayedUrl.value = embedded;
    }
  } catch (error) {
    if (isAbortError(error)) return;
  }

  const best = previewUrl(member, "current", singleDisplaySize.value, "best");
  try {
    const bestImage = await preloadImage(best, controller.signal, "high");
    if (controller.signal.aborted || generation !== progressiveGeneration) return;
    fitPreloadedSingle(bestImage);
    singleDisplayedStage.value = "best";
    singleDisplayedUrl.value = best;
  } catch (error) {
    if (!isAbortError(error) && generation === progressiveGeneration) {
      singleDisplayedStage.value = "best";
      void handleSingleImageError();
    }
  }
}

async function progressComparePreview(
  member: RawMember,
  side: 0 | 1,
  generation: number,
  controller: AbortController,
): Promise<void> {
  const stages = [...compareDisplayedStages.value] as [
    "thumbnail" | "embedded" | "best",
    "thumbnail" | "embedded" | "best",
  ];
  const urls = [...compareDisplayedUrls.value] as [string, string];
  if (compareDisplayedMemberIds.value[side] !== member.id || !urls[side]) {
    const memberIds = [...compareDisplayedMemberIds.value] as [string, string];
    stages[side] = "thumbnail";
    urls[side] = thumbnailPrefetchUrl(member, "visible");
    memberIds[side] = member.id;
    compareDisplayedStages.value = stages;
    compareDisplayedUrls.value = urls;
    compareDisplayedMemberIds.value = memberIds;
  }
  setCompareError(side, false);

  const embedded = previewUrl(
    member,
    side === 0 ? "current" : "compare",
    compareDisplaySizes.value[side],
    "embedded",
  );
  try {
    const embeddedImage = await preloadImage(embedded, controller.signal, "high");
    if (controller.signal.aborted || generation !== progressiveGeneration) return;
    if (compareDisplayedStages.value[side] !== "best") {
      fitPreloadedCompare(side, embeddedImage);
      const nextStages = [...compareDisplayedStages.value] as typeof stages;
      const nextUrls = [...compareDisplayedUrls.value] as [string, string];
      nextStages[side] = "embedded";
      nextUrls[side] = embedded;
      compareDisplayedStages.value = nextStages;
      compareDisplayedUrls.value = nextUrls;
    }
  } catch (error) {
    if (isAbortError(error)) return;
  }

  const best = previewUrl(
    member,
    side === 0 ? "current" : "compare",
    compareDisplaySizes.value[side],
    "best",
  );
  try {
    const bestImage = await preloadImage(best, controller.signal, "high");
    if (controller.signal.aborted || generation !== progressiveGeneration) return;
    fitPreloadedCompare(side, bestImage);
    const nextStages = [...compareDisplayedStages.value] as typeof stages;
    const nextUrls = [...compareDisplayedUrls.value] as [string, string];
    nextStages[side] = "best";
    nextUrls[side] = best;
    compareDisplayedStages.value = nextStages;
    compareDisplayedUrls.value = nextUrls;
  } catch (error) {
    if (!isAbortError(error) && generation === progressiveGeneration) {
      const nextStages = [...compareDisplayedStages.value] as typeof stages;
      nextStages[side] = "best";
      compareDisplayedStages.value = nextStages;
      void handleCompareImageError(side);
    }
  }
}

function startProgressivePreviews(): void {
  progressiveController?.abort();
  const generation = ++progressiveGeneration;
  const controller = new AbortController();
  progressiveController = controller;
  if (compareMode.value && compareItems.value) {
    void progressComparePreview(compareItems.value[0], 0, generation, controller);
    void progressComparePreview(compareItems.value[1], 1, generation, controller);
    return;
  }
  if (currentMember.value) {
    void progressSinglePreview(currentMember.value, generation, controller);
  } else {
    singleDisplayedUrl.value = "";
    singleDisplayedMemberId.value = "";
  }
}

function schedulePrefetch(): void {
  prefetchController?.abort();
  const generation = ++prefetchGeneration;
  if (!workspaceVisible.value || document.hidden || !members.value.length) return;
  const controller = new AbortController();
  prefetchController = controller;

  const high = new Set<string>();
  const low = new Set<string>();
  if (currentMember.value && !compareMode.value) {
    high.add(previewUrl(currentMember.value, "current", singleDisplaySize.value, "embedded"));
    low.add(previewUrl(currentMember.value, "current", singleDisplaySize.value, "best"));
  }
  if (compareItems.value) {
    high.add(previewUrl(compareItems.value[0], "current", compareDisplaySizes.value[0], "embedded"));
    high.add(previewUrl(compareItems.value[1], "compare", compareDisplaySizes.value[1], "embedded"));
    low.add(compareImageUrl(compareItems.value[0], 0));
    low.add(compareImageUrl(compareItems.value[1], 1));
  }
  for (let index = filmRange.value.visibleStart; index < filmRange.value.visibleEnd; index += 1) {
    const member = members.value[index];
    if (member) high.add(thumbnailPrefetchUrl(member, "visible"));
  }
  for (let index = filmRange.value.start; index < filmRange.value.end; index += 1) {
    if (index >= filmRange.value.visibleStart && index < filmRange.value.visibleEnd) continue;
    const member = members.value[index];
    if (member) low.add(thumbnailPrefetchUrl(member, "overscan"));
  }
  if (!compareMode.value) {
    for (const index of [currentIndex.value - 1, currentIndex.value + 1]) {
      const member = members.value[index];
      if (member) low.add(previewUrl(member, "adjacent", singleDisplaySize.value));
    }
  }

  void (async () => {
    await Promise.allSettled([...high].map((url) => preloadImage(url, controller.signal, "high")));
    if (controller.signal.aborted || generation !== prefetchGeneration) return;
    await Promise.allSettled([...low].map((url) => preloadImage(url, controller.signal, "low")));
  })();
}

function cancelBackendWork(): void {
  if (cancelWorkPromise) return;
  cancelWorkPromise = cancelProjectWork(props.projectId)
    .catch(() => undefined)
    .finally(() => {
      cancelWorkPromise = null;
      if (!destroyed && workspaceVisible.value) schedulePrefetch();
    });
}

function updateDocumentVisibility(): void {
  workspaceVisible.value = workspaceIntersecting && !document.hidden;
  if (!workspaceVisible.value) {
    prefetchController?.abort();
    prefetchGeneration += 1;
    cancelBackendWork();
  } else if (!cancelWorkPromise) {
    schedulePrefetch();
  }
}

watch(
  () => [
    filterStarMode.value,
    filterStarValue.value,
    filterRated.value,
    filterColorLabels.value.join(","),
    filterFilename.value,
    filterExported.value,
    filterFormats.value.join(","),
    filterOrientations.value.join(","),
    sortField.value,
    sortDirection.value,
  ],
  () => {
    if (hydrating) return;
    const preferred = currentMember.value?.id ?? null;
    prefetchController?.abort();
    prefetchGeneration += 1;
    scheduleSaveState();
    void loadMembers(preferred);
  },
);

watch(
  () => [
    compareMode.value,
    currentMember.value
      ? `${currentMember.value.id}:${sourceVersion(currentMember.value)}:${currentMember.value.creative_look}`
      : "",
    compareItems.value
      ?.map((member) => `${member.id}:${sourceVersion(member)}:${member.creative_look}`)
      .join("|") ?? "",
    singleDisplaySize.value.width,
    singleDisplaySize.value.height,
    compareDisplaySizes.value[0].width,
    compareDisplaySizes.value[0].height,
    compareDisplaySizes.value[1].width,
    compareDisplaySizes.value[1].height,
  ],
  startProgressivePreviews,
  { flush: "post" },
);

watch(
  () => currentMember.value
    ? `${currentMember.value.id}:${sourceVersion(currentMember.value)}`
    : "",
  () => {
    if (!hydrating) scheduleSaveState();
    if (!compareMode.value) viewport.fit("single", elementsFor("single"));
    singlePreviewError.value = false;
    schedulePrefetch();
  },
);

watch(
  () => [
    filmRange.value.start,
    filmRange.value.end,
    filmRange.value.visibleStart,
    filmRange.value.visibleEnd,
    compareMode.value,
    compareItems.value?.map((member) => `${member.id}:${member.creative_look}`).join("|") ?? "",
    members.value.map((member) => `${member.id}:${sourceVersion(member)}`).join("|"),
  ],
  schedulePrefetch,
);

watch(() => activeMember.value?.id, () => {
  if (openMenu.value === "look") closeTransient(false);
});

watch(singleContainer, (current, previous) => {
  if (previous) previewResizeObserver?.unobserve(previous);
  if (current) previewResizeObserver?.observe(current);
  void nextTick(measurePreviewContainers);
});

onMounted(async () => {
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
  window.addEventListener("mousemove", viewport.movePan);
  window.addEventListener("mouseup", viewport.endPan);
  window.addEventListener("blur", viewport.endPan);
  document.addEventListener("visibilitychange", updateDocumentVisibility);

  const preferredMemberId = await loadWorkspace();
  await loadMembers(preferredMemberId);
  await resumeRememberedJob("import");
  await resumeRememberedJob("export");
  try {
    creativeLooks.value = (await listCreativeLooks()).looks;
  } catch {
    creativeLooks.value = [];
  }
  hydrating = false;
  await nextTick();
  filmstrip.value?.setScrollLeft(filmStripScroll.value);
  measurePreviewContainers();

  if (typeof ResizeObserver !== "undefined") {
    previewResizeObserver = new ResizeObserver(measurePreviewContainers);
    if (singleContainer.value) previewResizeObserver.observe(singleContainer.value);
    compareContainers.value.forEach((container) => {
      if (container) previewResizeObserver?.observe(container);
    });
  }
  if (typeof IntersectionObserver !== "undefined" && rootElement.value) {
    intersectionObserver = new IntersectionObserver((entries) => {
      workspaceIntersecting = entries[0]?.isIntersecting ?? true;
      updateDocumentVisibility();
    });
    intersectionObserver.observe(rootElement.value);
  }
  schedulePrefetch();
});

onBeforeUnmount(() => {
  destroyed = true;
  workspaceVisible.value = false;
  window.removeEventListener("keydown", handleKeydown);
  window.removeEventListener("pointerdown", handleWindowPointerDown);
  window.removeEventListener("resize", handleWindowResize);
  window.removeEventListener("mousemove", viewport.movePan);
  window.removeEventListener("mouseup", viewport.endPan);
  window.removeEventListener("blur", viewport.endPan);
  document.removeEventListener("visibilitychange", updateDocumentVisibility);
  abortController?.abort();
  prefetchController?.abort();
  progressiveController?.abort();
  prefetchGeneration += 1;
  progressiveGeneration += 1;
  cancelBackendWork();
  previewResizeObserver?.disconnect();
  intersectionObserver?.disconnect();
  if (saveTimer) clearTimeout(saveTimer);
  if (!hydrating) saveState();
});
</script>

<template>
  <section
    ref="rootElement"
    class="rs-workspace"
    :class="{ 'has-filters': showFilters && total > 0, 'is-empty': total === 0 }"
    aria-label="ARW 选片工作区"
  >
    <div class="rs-task-stack" aria-live="polite">
      <section v-if="importJob && isActiveJob(importJob)" class="rs-task-status">
        <span>{{ importProgressText }}</span>
        <button type="button" @click="cancelImportJob">取消导入</button>
      </section>
      <section v-if="exportJob && isActiveJob(exportJob)" class="rs-task-status">
        <label>
          <span>{{ exportProgressText }}</span>
          <progress
            :value="exportJob.progress.completed ?? 0"
            :max="Math.max(1, exportJob.progress.total ?? 1)"
          />
        </label>
        <button type="button" @click="cancelExportJob">取消导出</button>
      </section>
    </div>

    <div v-if="total === 0 && !loading" class="rs-empty-workspace">
      <button type="button" class="rs-empty-back" @click="emit('back')">返回项目列表</button>
      <div>
        <h2>{{ projectName }}</h2>
        <p>导入 Sony A7M4 ARW 或配套 JPG/PNG 开始选片</p>
      </div>
      <div class="rs-empty-imports">
        <button type="button" :disabled="importing" @click="handleImportFiles">导入图片</button>
        <button type="button" :disabled="importing" @click="handleImportFolder">导入文件夹</button>
      </div>
    </div>

    <div v-else-if="compareMode && compareItems" class="rs-preview-stage rs-compare-stage">
      <div class="rs-compare-panes">
        <div
          v-for="(item, sideValue) in compareItems"
          :key="item.id"
          :ref="(element) => setCompareContainer(sideValue, element)"
          class="rs-compare-pane"
          :class="{ 'is-active': viewport.activeCompareSide.value === sideValue }"
          role="group"
          :aria-label="`${sideValue === 0 ? '左侧' : '右侧'}对比图片${viewport.activeCompareSide.value === sideValue ? '，当前操作侧' : ''}`"
          @click="viewport.activeCompareSide.value = sideValue as 0 | 1"
          @wheel="handleImageWheel(sideValue as 0 | 1, $event)"
          @dblclick="handleImageDoubleClick(sideValue as 0 | 1, $event)"
          @mousedown="handleImageMouseDown(sideValue as 0 | 1, $event)"
          @contextmenu.prevent
        >
          <div class="rs-preview-media">
            <img
              :ref="(element) => setCompareImage(sideValue, element)"
              :src="compareDisplayedUrls[sideValue] || compareImageUrl(item, sideValue as 0 | 1)"
              :alt="item.file_name"
              class="rs-preview-img"
              :style="viewport.styleFor(sideValue as 0 | 1)"
              draggable="false"
              @load="handleCompareImageLoad(sideValue)"
              @error="handleCompareDisplayedError(sideValue as 0 | 1)"
            />
          </div>
          <div v-if="comparePreviewErrors[sideValue]" class="rs-preview-error" role="status">
            <strong>无法加载预览</strong>
            <span>{{ comparePreviewMessages[sideValue] || item.file_name }}</span>
          </div>
        </div>
      </div>
    </div>

    <div
      v-else
      ref="singleContainer"
      class="rs-preview-stage"
      @wheel="handleImageWheel('single', $event)"
      @dblclick="handleImageDoubleClick('single', $event)"
      @mousedown="handleImageMouseDown('single', $event)"
      @contextmenu.prevent
    >
      <div v-if="!currentMember && !loading" class="rs-preview-empty">项目中暂无图片</div>
      <div v-else-if="!currentMember" class="rs-preview-empty">加载中…</div>
      <div v-else class="rs-preview-media">
        <img
          ref="singleImage"
          :src="singleDisplayedUrl || singleImageUrl"
          :alt="currentMember.file_name"
          class="rs-preview-img"
          :style="viewport.styleFor('single')"
          draggable="false"
          @load="handleSingleImageLoad"
          @error="handleSingleDisplayedError"
        />
      </div>
      <div v-if="singlePreviewError && currentMember" class="rs-preview-error" role="status">
        <strong>无法加载预览</strong>
        <span>{{ singlePreviewMessage || currentMember.file_name }}</span>
      </div>
    </div>

    <section
      v-if="total > 0 && showFilters"
      id="raw-filter-bar"
      class="rs-filter-bar"
      data-raw-transient
      aria-label="筛选条件"
    >
      <label class="rs-filter-field">
        <span>星级</span>
        <select v-model="starFilterKind">
          <option value="none">全部</option>
          <option value="exact">精确</option>
          <option value="at_least">至少</option>
          <option value="unrated">未评级</option>
        </select>
      </label>
      <label v-if="starFilterKind === 'exact' || starFilterKind === 'at_least'" class="rs-filter-field rs-star-value">
        <span>数量</span>
        <select v-model.number="filterStarValue">
          <option v-for="star in 5" :key="star" :value="star">{{ star }} 星</option>
        </select>
      </label>
      <fieldset class="rs-filter-group">
        <legend>色标</legend>
        <button
          v-for="option in colorLabelOptions.slice(1)"
          :key="option.value"
          type="button"
          :aria-pressed="filterColorLabels.includes(option.value)"
          :class="{ active: filterColorLabels.includes(option.value) }"
          @click="toggleFilterColor(option.value)"
        >{{ option.label }}</button>
      </fieldset>
      <label class="rs-filter-field">
        <span>导出状态</span>
        <select v-model="filterExported">
          <option value="all">全部</option>
          <option value="unexported">未导出</option>
          <option value="exported">已导出</option>
        </select>
      </label>
      <fieldset class="rs-filter-group">
        <legend>格式</legend>
        <button
          v-for="option in formatOptions"
          :key="option.value"
          type="button"
          :aria-pressed="filterFormats.includes(option.value)"
          :class="{ active: filterFormats.includes(option.value) }"
          @click="filterFormats = toggleArrayValue(filterFormats, option.value)"
        >{{ option.label }}</button>
      </fieldset>
      <fieldset class="rs-filter-group">
        <legend>构图</legend>
        <button
          v-for="option in orientationOptions"
          :key="option.value"
          type="button"
          :aria-pressed="filterOrientations.includes(option.value)"
          :class="{ active: filterOrientations.includes(option.value) }"
          @click="filterOrientations = toggleArrayValue(filterOrientations, option.value)"
        >{{ option.label }}</button>
      </fieldset>
      <label class="rs-filter-field rs-filter-filename">
        <span>文件名包含</span>
        <input v-model="filterFilename" type="search" maxlength="200" placeholder="输入文件名" />
      </label>
      <button class="rs-clear-filter" type="button" :disabled="activeFilterCount === 0" @click="clearFilters">
        清除筛选
      </button>
    </section>

    <footer v-if="total > 0" class="rs-control-bar">
      <div class="rs-control-left">
        <button type="button" class="rs-project-back" :title="`返回项目列表 · ${projectName}`" @click="emit('back')">
          <AppIcon name="chevron-down" :size="15" />
          <span class="rs-control-label">{{ projectName }}</span>
        </button>

        <div class="rs-menu-wrap" data-raw-transient>
          <button
            type="button"
            class="rs-import-button"
            aria-label="导入"
            aria-haspopup="menu"
            :aria-expanded="openMenu === 'import'"
            :disabled="importing"
            @click="toggleMenu('import', $event)"
          >
            <AppIcon name="plus" :size="15" />
            <span class="rs-import-label">导入</span>
          </button>
          <div v-if="openMenu === 'import'" class="rs-menu" role="menu" aria-label="导入">
            <button type="button" role="menuitem" @click="handleImportFiles">导入图片</button>
            <button type="button" role="menuitem" @click="handleImportFolder">导入文件夹</button>
          </div>
        </div>

        <button
          type="button"
          data-raw-transient
          :aria-expanded="showFilters"
          aria-controls="raw-filter-bar"
          @click="toggleFilters"
        >{{ filterSummary }}</button>

        <div class="rs-menu-wrap" data-raw-transient>
          <button
            type="button"
            aria-haspopup="menu"
            :aria-expanded="openMenu === 'sort'"
            :title="sortSummary"
            @click="toggleMenu('sort', $event)"
          >排序</button>
          <div v-if="openMenu === 'sort'" class="rs-menu rs-sort-menu" role="menu" aria-label="排序">
            <button
              v-for="option in sortOptions"
              :key="option.value"
              type="button"
              role="menuitemradio"
              :aria-checked="sortField === option.value"
              :class="{ active: sortField === option.value }"
              @click="sortField = option.value; closeTransient(true)"
            >{{ option.label }}</button>
            <hr />
            <button type="button" role="menuitemradio" :aria-checked="sortDirection === 'asc'" :class="{ active: sortDirection === 'asc' }" @click="sortDirection = 'asc'; closeTransient(true)">升序</button>
            <button type="button" role="menuitemradio" :aria-checked="sortDirection === 'desc'" :class="{ active: sortDirection === 'desc' }" @click="sortDirection = 'desc'; closeTransient(true)">降序</button>
          </div>
        </div>

        <div class="rs-control-status">
          <span class="rs-file-name" :title="activeMember?.file_name">{{ activeMember?.file_name || '未选择图片' }}</span>
          <span class="rs-position">{{ activeMemberIndex >= 0 ? `${activeMemberIndex + 1} / ${members.length}` : '—' }}</span>
          <div class="rs-menu-wrap" data-raw-transient>
            <button
              type="button"
              aria-haspopup="menu"
              :aria-expanded="openMenu === 'selection'"
              @click="toggleMenu('selection', $event)"
            >已选 {{ selectedCount }}</button>
            <div v-if="openMenu === 'selection'" class="rs-menu" role="menu" aria-label="选择">
              <button type="button" role="menuitem" :disabled="!members.length" @click="selectAllFiltered">全选筛选结果</button>
              <button type="button" role="menuitem" :disabled="!selectedCount" @click="clearSelection">清除选择</button>
            </div>
          </div>
        </div>
      </div>

      <div class="rs-control-center" role="group" aria-label="图片标记">
        <div class="rs-control-group">
          <span class="rs-control-group-label">评分</span>
          <div class="rs-stars" role="group" aria-label="星级">
            <button
              v-for="star in 5"
              :key="star"
              type="button"
              class="rs-star"
              :class="{ active: activeMember && activeMember.star_rating >= star }"
              :aria-label="`${star} 星`"
              :aria-pressed="Boolean(activeMember && activeMember.star_rating === star)"
              :disabled="!activeMember"
              @click="setRating(star)"
            >★</button>
            <button type="button" class="rs-star-clear" aria-label="清除星级" :disabled="!activeMember || activeMember.star_rating === 0" @click="setRating(0)">×</button>
          </div>
        </div>
        <div class="rs-control-group">
          <span class="rs-control-group-label">色标</span>
          <div class="rs-colors" role="group" aria-label="色标">
            <button
              v-for="option in colorLabelOptions"
              :key="option.value"
              type="button"
              class="rs-color-button"
              :class="{ active: activeMember?.color_label === option.value }"
              :style="{ '--label-color': option.color }"
              :aria-label="option.label"
              :aria-pressed="activeMember?.color_label === option.value"
              :disabled="!activeMember"
              @click="setColorLabel(option.value)"
            ><span aria-hidden="true">{{ option.value === 'none' ? '–' : '' }}</span></button>
          </div>
        </div>
        <div class="rs-menu-wrap rs-look-control" data-raw-transient>
          <button
            type="button"
            class="rs-look-button"
            aria-haspopup="menu"
            :aria-expanded="openMenu === 'look'"
            :disabled="!activeMember || !activeIsRaw || creativeLooks.length <= 1"
            :title="!activeIsRaw ? 'JPG/PNG 不应用 Sony 创意外观' : creativeLooks.length <= 1 ? '未经可信参考校准的外观暂不可用' : 'Sony 创意外观'"
            @click="toggleMenu('look', $event)"
          >{{ activeIsRaw ? `外观：${activeLookLabel}` : '外观：不适用' }}</button>
          <div v-if="openMenu === 'look' && activeIsRaw && creativeLooks.length > 1" class="rs-menu rs-look-menu" role="menu" aria-label="Sony 创意外观">
            <button
              v-for="look in creativeLooks"
              :key="look.id"
              type="button"
              role="menuitemradio"
              :aria-checked="activeMember?.creative_look === look.id"
              :class="{ active: activeMember?.creative_look === look.id }"
              :title="look.calibration"
              @click="setCreativeLook(look.id)"
            >{{ look.label }}</button>
          </div>
        </div>
      </div>

      <div class="rs-control-right">
        <div class="rs-zoom-controls" role="group" aria-label="缩放">
          <button type="button" :disabled="!activeMember" @click="fitActive">适合</button>
          <button type="button" :disabled="!activeMember" @click="actualSizeActive">100%</button>
        </div>
        <div class="rs-view-switch" role="group" aria-label="预览模式">
          <button type="button" :aria-pressed="!compareMode" :class="{ active: !compareMode }" @click="compareMode && exitCompare(true)">单图</button>
          <button type="button" :aria-pressed="compareMode" :class="{ active: compareMode }" :disabled="!compareMode && !compareAvailable" :title="!compareMode && !compareAvailable ? '请先选择两张图片' : '对比视图'" @click="!compareMode && enterCompare()">对比</button>
        </div>
        <label v-if="compareMode" class="rs-sync-toggle">
          <input v-model="viewport.compareSync.value" type="checkbox" />
          <span>同步缩放</span>
        </label>
        <button type="button" :disabled="!activeMember || exportBusy" @click="handleExport">{{ exportBusy ? '导出中…' : '导出' }}</button>
        <div class="rs-menu-wrap" data-raw-transient>
          <button
            ref="deleteTrigger"
            type="button"
            aria-label="更多操作"
            aria-haspopup="menu"
            :aria-expanded="openMenu === 'more'"
            :disabled="!activeMember"
            @click="toggleMenu('more', $event)"
          >更多</button>
          <div v-if="openMenu === 'more'" class="rs-menu rs-more-menu" role="menu" aria-label="更多操作">
            <button type="button" role="menuitem" @click="handleRemoveFromMenu">移出项目</button>
            <hr />
            <button type="button" role="menuitem" class="rs-danger" @click="handlePermanentDeleteFromMenu">永久删除</button>
          </div>
        </div>
      </div>
    </footer>

    <RawSelectionFilmstrip
      v-if="total > 0"
      ref="filmstrip"
      :members="members"
      :current-index="currentIndex"
      :selected-ids="selectedIds"
      :initial-scroll="filmStripScroll"
      @activate="activateMember"
      @selection="selectedIds = $event"
      @scroll="handleFilmScroll"
      @range="filmRange = $event"
    />

    <div v-if="showDeleteModal" class="rs-modal-backdrop" role="presentation" @click.self="closeDeleteModal(true)">
      <div class="rs-delete-modal" role="alertdialog" aria-modal="true" aria-labelledby="raw-delete-title" aria-describedby="raw-delete-warning">
        <h3 id="raw-delete-title">永久删除源文件</h3>
        <p>将永久删除 <strong>{{ deleteTargets.length }}</strong> 个源文件，总大小 <strong>{{ formatSize(deleteTotalSize) }}</strong>。</p>
        <p id="raw-delete-warning" class="rs-delete-warning">文件不会进入回收站，删除后无法恢复。</p>
        <ul class="rs-delete-list" aria-label="待删除文件">
          <li v-for="member in deleteTargets.slice(0, 5)" :key="member.id">{{ member.file_name }}</li>
          <li v-if="deleteTargets.length > 5">以及其他 {{ deleteTargets.length - 5 }} 个文件</li>
        </ul>
        <label class="rs-delete-check">
          <input ref="deleteCheck" v-model="deleteConfirmed" type="checkbox" :disabled="deleteBusy" />
          <span>我理解这些源文件将被永久删除</span>
        </label>
        <div class="rs-delete-actions">
          <button type="button" :disabled="deleteBusy" @click="closeDeleteModal(true)">取消</button>
          <button type="button" class="rs-delete-confirm" :disabled="!deleteConfirmed || deleteBusy" @click="confirmPermanentDelete">
            {{ deleteBusy ? '删除中…' : '永久删除' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.rs-workspace {
  --raw-preview-safe-inset: 24px;

  position: relative;
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-rows: minmax(0, 1fr) auto 100px;
  grid-template-areas:
    "preview"
    "controls"
    "filmstrip";
  overflow: hidden;
  color: var(--text);
  background: var(--surface);
}

.rs-workspace.is-empty {
  grid-template-rows: minmax(0, 1fr);
  grid-template-areas: "preview";
}

.rs-empty-workspace {
  display: grid;
  grid-area: preview;
  align-content: center;
  justify-items: center;
  gap: 20px;
  padding: 24px;
  text-align: center;
  background: var(--surface-soft);
}

.rs-empty-workspace h2 {
  margin: 0 0 4px;
  font-size: 17px;
  font-weight: 500;
}

.rs-empty-workspace p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}

.rs-empty-back {
  position: absolute;
  top: 16px;
  left: 16px;
}

.rs-empty-imports {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}

.rs-empty-imports button {
  min-width: 112px;
  min-height: 36px;
}

.rs-task-stack {
  position: absolute;
  z-index: 30;
  top: 12px;
  right: 12px;
  display: grid;
  width: min(360px, calc(100% - 24px));
  gap: 8px;
  pointer-events: none;
}

.rs-task-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-strong);
  box-shadow: var(--shadow-float);
  color: var(--text);
  font-size: 12px;
  pointer-events: auto;
}

.rs-task-status label {
  display: grid;
  flex: 1;
  gap: 4px;
}

.rs-task-status progress {
  width: 100%;
  height: 5px;
}

.rs-workspace.has-filters {
  grid-template-rows: minmax(0, 1fr) auto auto 100px;
  grid-template-areas:
    "preview"
    "filters"
    "controls"
    "filmstrip";
}

.rs-preview-stage {
  position: relative;
  display: grid;
  grid-area: preview;
  min-width: 0;
  min-height: 0;
  place-items: center;
  overflow: hidden;
  user-select: none;
  background: var(--surface-soft);
}

.rs-preview-media {
  position: absolute;
  inset: var(--raw-preview-safe-inset);
  display: flex;
  min-width: 0;
  min-height: 0;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.rs-preview-img {
  display: block;
  width: auto;
  height: auto;
  max-width: none;
  max-height: none;
  flex: 0 0 auto;
  pointer-events: none;
  transform-origin: center;
}

.rs-preview-empty,
.rs-preview-error {
  display: grid;
  place-items: center;
  gap: 4px;
  color: var(--muted);
  font-size: 13px;
}

.rs-preview-error {
  position: absolute;
  inset: 0;
  align-content: center;
  background: var(--surface-soft);
}

.rs-preview-error span {
  max-width: min(360px, 80%);
  overflow: hidden;
  color: var(--faint);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-compare-panes {
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 1px;
  background: var(--border);
}

.rs-compare-pane {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 0;
  place-items: center;
  overflow: hidden;
  background: var(--surface-soft);
}

.rs-compare-pane.is-active {
  box-shadow: inset 0 0 0 2px var(--brand);
}

.rs-filter-bar {
  display: grid;
  grid-area: filters;
  min-width: 0;
  grid-template-columns: repeat(7, minmax(110px, auto));
  align-items: end;
  gap: 8px;
  max-height: 176px;
  overflow: auto;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

.rs-filter-field {
  display: grid;
  min-width: 0;
  gap: 4px;
  color: var(--muted);
  font-size: 11px;
}

.rs-filter-field :is(select, input) {
  min-width: 0;
  height: 30px;
  padding: 0 8px;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  color: var(--text);
  background: var(--surface);
}

.rs-filter-filename { min-width: 150px; }

.rs-filter-group {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 3px;
  margin: 0;
  padding: 0;
  border: 0;
}

.rs-filter-group legend {
  width: 100%;
  margin-bottom: 4px;
  color: var(--muted);
  font-size: 11px;
}

.rs-filter-group button,
.rs-clear-filter {
  min-height: 30px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  font-size: 11px;
  white-space: nowrap;
  background: var(--surface);
}

.rs-filter-group button.active {
  border-color: var(--border-strong);
  background: var(--surface-strong);
  box-shadow: inset 0 -2px 0 var(--brand);
}

.rs-control-bar {
  position: relative;
  z-index: 20;
  display: grid;
  grid-area: controls;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr) max-content;
  grid-template-areas:
    "left left"
    "center right";
  align-items: center;
  column-gap: 16px;
  row-gap: 8px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

.rs-workspace > .rs-film-strip { grid-area: filmstrip; }

.rs-control-left,
.rs-control-center,
.rs-control-right {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 4px;
}

.rs-control-left {
  grid-area: left;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}

.rs-control-center {
  grid-area: center;
  justify-content: flex-start;
  gap: 12px;
}

.rs-control-right {
  grid-area: right;
  justify-content: flex-end;
  gap: 8px;
}

.rs-control-bar button {
  min-height: 30px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  font-size: 12px;
  white-space: nowrap;
  background: var(--surface);
}

.rs-control-bar button:not(:disabled):hover,
.rs-control-bar button[aria-expanded="true"] {
  background: var(--surface-soft);
}

.rs-project-back {
  display: inline-flex;
  max-width: 150px;
  align-items: center;
  gap: 5px;
}

.rs-import-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.rs-project-back .app-icon { transform: rotate(90deg); }

.rs-control-label,
.rs-file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-control-status,
.rs-control-group,
.rs-zoom-controls {
  display: flex;
  min-width: 0;
  align-items: center;
}

.rs-control-status {
  gap: 8px;
  margin-left: auto;
  padding-left: 16px;
}

.rs-control-group { gap: 6px; }

.rs-control-group + .rs-control-group,
.rs-look-control {
  padding-left: 12px;
  border-left: 1px solid var(--border);
}

.rs-control-group-label {
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

.rs-zoom-controls {
  gap: 4px;
  padding-right: 8px;
  border-right: 1px solid var(--border);
}

.rs-position {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

.rs-file-name {
  max-width: 220px;
  color: var(--muted);
  font-size: 12px;
}

.rs-stars,
.rs-colors,
.rs-view-switch {
  display: flex;
  align-items: center;
  gap: 1px;
}

.rs-star,
.rs-star-clear {
  min-width: 20px;
  padding: 0 2px !important;
  border: 0 !important;
  color: var(--faint) !important;
  font-size: 16px !important;
  background: transparent !important;
}

.rs-star.active { color: #b77d00 !important; }
.rs-star-clear { font-size: 14px !important; }

.rs-color-button {
  display: grid;
  width: 20px;
  min-width: 20px;
  height: 20px;
  min-height: 20px !important;
  place-items: center;
  padding: 0 !important;
  border-radius: 50% !important;
  background: var(--label-color, transparent) !important;
}

.rs-color-button.active {
  box-shadow: 0 0 0 2px var(--surface), 0 0 0 3px var(--brand);
}

.rs-look-button {
  max-width: 112px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.rs-danger { color: var(--danger) !important; }

.rs-view-switch {
  gap: 0;
  overflow: hidden;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-soft);
}

.rs-view-switch button { border: 0; border-radius: 5px; background: transparent; }
.rs-view-switch button.active { background: var(--surface-strong); }

.rs-sync-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

.rs-menu-wrap { position: relative; }

.rs-menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  z-index: 60;
  display: grid;
  width: max-content;
  min-width: 136px;
  max-width: min(260px, calc(100vw - 16px));
  max-height: min(420px, calc(100vh - 32px));
  gap: 2px;
  overflow: auto;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text);
  background: color-mix(in srgb, var(--surface) 97%, transparent);
  box-shadow: var(--shadow-float);
}

.rs-control-left .rs-menu-wrap:first-of-type .rs-menu { right: auto; left: 0; }

.rs-menu button {
  width: 100%;
  min-height: 30px;
  padding: 0 10px;
  border: 0;
  text-align: left;
  background: transparent;
}

.rs-menu button:hover,
.rs-menu button.active { background: var(--surface-soft); }

.rs-menu hr {
  width: 100%;
  margin: 3px 0;
  border: 0;
  border-top: 1px solid var(--border);
}

.rs-look-menu {
  right: auto;
  left: 50%;
  transform: translateX(-50%);
}

.rs-more-menu { min-width: 160px; }

.rs-modal-backdrop {
  position: fixed;
  z-index: 1100;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 16px;
  background: rgb(0 0 0 / 38%);
}

.rs-delete-modal {
  display: grid;
  width: min(420px, 100%);
  gap: 12px;
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 14px;
  color: var(--text);
  background: var(--surface);
  box-shadow: var(--shadow-float);
}

.rs-delete-modal h3,
.rs-delete-modal p { margin: 0; }
.rs-delete-modal h3 { color: var(--danger); font-size: 15px; font-weight: 500; }
.rs-delete-modal p { font-size: 13px; }
.rs-delete-warning { color: var(--danger); font-size: 12px !important; }

.rs-delete-list {
  max-height: 110px;
  margin: 0;
  overflow: auto;
  padding-left: 20px;
  color: var(--muted);
  font-size: 12px;
}

.rs-delete-check {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  font-size: 13px;
  background: var(--surface-soft);
}

.rs-delete-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.rs-delete-actions button {
  min-height: 32px;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: 9px;
  color: var(--text);
  background: var(--surface);
}

.rs-delete-confirm {
  border-color: var(--danger) !important;
  color: #fff !important;
  background: var(--danger) !important;
}

@media (max-width: 1180px) {
  .rs-workspace { --raw-preview-safe-inset: 16px; }

  .rs-control-bar { column-gap: 12px; }
  .rs-control-center,
  .rs-control-right { flex-wrap: wrap; }
  .rs-file-name { max-width: 140px; }
  .rs-filter-bar { grid-template-columns: repeat(4, minmax(120px, 1fr)); }
}

@media (max-width: 760px) {
  .rs-workspace { --raw-preview-safe-inset: 12px; }

  .rs-control-bar {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas:
      "left"
      "center"
      "right";
    padding-inline: 6px;
  }
  .rs-control-label { display: none; }
  .rs-import-label { display: none; }
  .rs-file-name { display: none; }
  .rs-filter-bar { grid-template-columns: repeat(3, minmax(110px, 1fr)); }
  .rs-control-bar button { padding-inline: 6px; }
  .rs-control-left { flex-wrap: nowrap; }
  .rs-control-center,
  .rs-control-right { flex-wrap: wrap; }
  .rs-control-right { justify-content: flex-start; }
  .rs-control-status { padding-left: 8px; }
  .rs-sync-toggle span { font-size: 0; }
  .rs-sync-toggle span::after { content: "同步"; font-size: 11px; }
}

@media (prefers-color-scheme: dark) {
  .rs-workspace {
    color-scheme: dark;
    --surface: #20211e;
    --surface-soft: #181916;
    --surface-strong: #30312c;
    --text: #f1f1eb;
    --muted: #b1b2aa;
    --faint: #8e9088;
    --border: rgb(255 255 255 / 10%);
    --border-strong: rgb(255 255 255 / 18%);
    --brand: #efefe9;
    --success: #71b89a;
    --danger: #ff9aa7;
    --shadow-float: 0 18px 48px rgb(0 0 0 / 46%);
  }

  .rs-star.active,
  .rs-thumb-stars { color: #f0bd54 !important; }
}

@media (prefers-reduced-motion: reduce) {
  .rs-preview-img { transition: none; }
}
</style>
