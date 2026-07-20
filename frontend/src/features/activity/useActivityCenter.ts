import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
} from "vue";

import { activityApi } from "./api";
import type {
  ActivityApi,
  ActivityExportFormat,
  ActivityLevel,
  ActivityLogFilters,
  ActivityLogItem,
  ActivityLogWire,
  CursorPage,
  CursorPageWire,
  JobHistoryFilters,
  JobHistoryItem,
  JobHistoryWire,
} from "./types";
import { EMPTY_JOB_FILTERS, EMPTY_LOG_FILTERS } from "./types";

export const ACTIVITY_PAGE_SIZE = 50;
const ACTIVITY_EXPORT_PAGE_SIZE = 200;
const MAX_ACTIVITY_EXPORT_ITEMS = 50_000;
const MAX_DETAIL_KEYS = 24;
const MAX_DETAIL_ARRAY_ITEMS = 20;
const MAX_DETAIL_TEXT = 2_000;
const MAX_MESSAGE_TEXT = 4_000;
const ACTIVE_STATUSES = new Set(["queued", "pending", "running", "cancelling"]);
const ALLOWED_LEVELS = new Set<ActivityLevel>(["debug", "info", "warning", "error"]);
const SENSITIVE_DETAIL_KEY =
  /(?:api[_-]?key|authorization|bearer|cookie|password|passwd|secret|token|prompt|response|base64|binary)/iu;

interface ActivityCenterEvents {
  autoStart?: boolean;
  pollIntervalMs?: number;
  onError?: (title: string, message: string) => void;
  onInfo?: (title: string, message: string) => void;
  copyText?: (value: string) => Promise<void>;
  downloadText?: (filename: string, content: string, mimeType: string) => void;
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function integer(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function boundedText(value: unknown, maximum = MAX_MESSAGE_TEXT): string {
  const normalized = firstText(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/gu, "");
  return normalized.length <= maximum ? normalized : `${normalized.slice(0, maximum)}…`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function safeLocalUrl(value: unknown): string {
  const candidate = firstText(value);
  if (!candidate) return "";
  try {
    const url = new URL(candidate, document.baseURI);
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    const supported = url.protocol === "http:" || url.protocol === "https:";
    return supported && (url.origin === window.location.origin || loopback) ? url.href : "";
  } catch {
    return "";
  }
}

function sanitizeDetailValue(value: unknown, depth = 0): unknown {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return boundedText(value, MAX_DETAIL_TEXT);
  if (depth >= 2) return "[详情已折叠]";
  if (Array.isArray(value)) {
    return value
      .slice(0, MAX_DETAIL_ARRAY_ITEMS)
      .map((item) => sanitizeDetailValue(item, depth + 1));
  }
  if (!value || typeof value !== "object") return String(value ?? "");
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value).slice(0, MAX_DETAIL_KEYS)) {
    if (SENSITIVE_DETAIL_KEY.test(key)) {
      result[key] = "[已隐藏]";
      continue;
    }
    result[key] = sanitizeDetailValue(item, depth + 1);
  }
  return result;
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return sanitizeDetailValue(value) as Record<string, unknown>;
}

function detailsRecord(raw: ActivityLogWire): Record<string, unknown> {
  if (raw.details && typeof raw.details === "object") return record(raw.details);
  if (typeof raw.details_json !== "string" || !raw.details_json.trim()) return {};
  try {
    return record(JSON.parse(raw.details_json) as unknown);
  } catch {
    return { parse_error: "日志详情格式无效" };
  }
}

function progressPercent(raw: JobHistoryWire, processed: number, total: number): number {
  const explicit = Number(raw.progress);
  if (Number.isFinite(explicit)) {
    const normalized = explicit >= 0 && explicit <= 1 ? explicit * 100 : explicit;
    return Math.min(100, Math.max(0, Math.round(normalized)));
  }
  return total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
}

export function normalizeJobHistory(raw: JobHistoryWire, index = 0): JobHistoryItem {
  const processed = Math.max(0, integer(raw.processed));
  const total = Math.max(0, integer(raw.total));
  return {
    id: firstText(raw.job_id, raw.id, `job-${index + 1}`),
    taskType: firstText(raw.task_type, "unknown"),
    libraryId: firstText(raw.library_id),
    libraryName: firstText(raw.library_name, raw.library_id, "全部图库"),
    status: firstText(raw.status, "unknown").toLowerCase(),
    submittedAt: firstText(raw.submitted_at, raw.created_at),
    startedAt: firstText(raw.started_at),
    finishedAt: firstText(raw.finished_at),
    updatedAt: firstText(raw.updated_at, raw.finished_at, raw.started_at, raw.submitted_at),
    processed,
    total,
    failed: Math.max(0, integer(raw.failed ?? raw.failure_count)),
    progressPercent: progressPercent(raw, processed, total),
    message: boundedText(raw.message),
    resultSummary: record(raw.result_summary),
    errorCode: firstText(raw.error_code),
    errorMessage: boundedText(raw.error_message),
  };
}

export function normalizeActivityLog(raw: ActivityLogWire, index = 0): ActivityLogItem {
  const rawLevel = firstText(raw.level, "info").toLowerCase() as ActivityLevel;
  const level = ALLOWED_LEVELS.has(rawLevel) ? rawLevel : "info";
  const details = detailsRecord(raw);
  return {
    sequence: Math.max(0, integer(raw.sequence, index + 1)),
    timestamp: firstText(raw.timestamp),
    level,
    category: firstText(raw.category, "general"),
    event: firstText(raw.event, "event"),
    source: firstText(raw.source, "backend"),
    libraryId: firstText(raw.library_id),
    libraryName: firstText(raw.library_name, raw.library_id),
    jobId: firstText(raw.job_id),
    operationId: firstText(raw.operation_id),
    message: boundedText(raw.message),
    details,
    thumbnailUrl: safeLocalUrl(raw.thumbnail_url ?? details.thumbnail_url),
    imageUrl: safeLocalUrl(raw.image_url ?? details.image_url),
  };
}

function wireItems<T>(payload: CursorPageWire<T>, aliases: Array<keyof CursorPageWire<T>>): T[] {
  for (const alias of aliases) {
    const value = payload[alias];
    if (Array.isArray(value)) return value as T[];
  }
  return [];
}

export function normalizeJobHistoryPage(
  payload: CursorPageWire<JobHistoryWire>,
): CursorPage<JobHistoryItem> {
  const items = wireItems(payload, ["items", "jobs"])
    .map(normalizeJobHistory)
    .sort((left, right) => {
      const activeDifference = Number(ACTIVE_STATUSES.has(right.status)) - Number(ACTIVE_STATUSES.has(left.status));
      if (activeDifference) return activeDifference;
      return right.updatedAt.localeCompare(left.updatedAt);
    })
    .slice(0, ACTIVITY_PAGE_SIZE);
  return {
    items,
    nextCursor: firstText(payload.next_cursor),
    hasMore: payload.has_more === true && Boolean(firstText(payload.next_cursor)),
    totalCount: Math.max(items.length, integer(payload.total_count ?? payload.total, items.length)),
  };
}

export function normalizeActivityLogPage(
  payload: CursorPageWire<ActivityLogWire>,
): CursorPage<ActivityLogItem> {
  const items = wireItems(payload, ["items", "logs"])
    .slice(0, ACTIVITY_PAGE_SIZE)
    .map(normalizeActivityLog);
  return {
    items,
    nextCursor: firstText(payload.next_cursor),
    hasMore: payload.has_more === true && Boolean(firstText(payload.next_cursor)),
    totalCount: Math.max(items.length, integer(payload.total_count ?? payload.total, items.length)),
  };
}

function csvCell(value: unknown): string {
  let text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  // Prevent spreadsheet formula execution when a user opens the exported CSV.
  if (/^[=+\-@]/u.test(text)) text = `'${text}`;
  return `"${text.replace(/"/gu, '""')}"`;
}

function exportRecord(item: ActivityLogItem): Record<string, unknown> {
  return {
    sequence: item.sequence,
    timestamp: item.timestamp,
    level: item.level,
    category: item.category,
    event: item.event,
    source: item.source,
    library_id: item.libraryId,
    library_name: item.libraryName,
    job_id: item.jobId,
    operation_id: item.operationId,
    message: item.message,
    details: item.details,
  };
}

export function serializeActivityLogs(
  items: ActivityLogItem[],
  format: ActivityExportFormat,
): string {
  if (format === "jsonl") return items.map((item) => JSON.stringify(exportRecord(item))).join("\n");
  const columns = [
    "sequence",
    "timestamp",
    "level",
    "category",
    "event",
    "source",
    "library_id",
    "library_name",
    "job_id",
    "operation_id",
    "message",
    "details",
  ] as const;
  const rows = items.map((item) => {
    const row = exportRecord(item);
    return columns.map((column) => csvCell(row[column])).join(",");
  });
  return [columns.join(","), ...rows].join("\r\n");
}

function copyLine(item: ActivityLogItem): string {
  const context = [item.libraryName, item.jobId].filter(Boolean).join(" / ");
  return `${item.timestamp} [${item.level.toUpperCase()}] ${item.category}/${item.event}${context ? ` (${context})` : ""} ${item.message}`.trim();
}

async function copyToClipboard(value: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("当前环境无法访问剪贴板");
  await navigator.clipboard.writeText(value);
}

function downloadText(filename: string, content: string, mimeType: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mimeType }));
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

function timestampForFilename(): string {
  return new Date().toISOString().replace(/[:.]/gu, "-");
}

export function useActivityCenter(
  api: ActivityApi = activityApi,
  events: ActivityCenterEvents = {},
) {
  const jobs = shallowRef<JobHistoryItem[]>([]);
  const logs = shallowRef<ActivityLogItem[]>([]);
  const jobFilters = ref<JobHistoryFilters>({ ...EMPTY_JOB_FILTERS });
  const logFilters = ref<ActivityLogFilters>({ ...EMPTY_LOG_FILTERS });
  const selectedJobId = ref("");
  const selectedLogSequences = shallowRef<Set<number>>(new Set());
  const expandedErrorJobId = ref("");
  const jobErrorLogs = shallowRef<ActivityLogItem[]>([]);
  const jobErrorsLoading = ref(false);
  const jobErrorsError = ref("");
  const jobErrorHasMore = ref(false);
  const jobErrorNextCursor = ref("");
  const jobsLoading = ref(false);
  const logsLoading = ref(false);
  const logsExporting = ref(false);
  const cancellingJobId = ref("");
  const jobsError = ref("");
  const logsError = ref("");
  const followingLogs = ref(true);
  const visible = ref(true);
  const documentVisible = ref(
    typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  const jobCursor = ref("");
  const logCursor = ref("");
  const jobNextCursor = ref("");
  const logNextCursor = ref("");
  const jobHasMore = ref(false);
  const logHasMore = ref(false);
  const jobCursorHistory = shallowRef<string[]>([]);
  const logCursorHistory = shallowRef<string[]>([]);
  const jobTotalCount = ref(0);
  const logTotalCount = ref(0);

  const jobPage = computed(() => jobCursorHistory.value.length + 1);
  const logPage = computed(() => logCursorHistory.value.length + 1);
  const hasPreviousJobs = computed(() => jobCursorHistory.value.length > 0);
  const hasPreviousLogs = computed(() => logCursorHistory.value.length > 0);
  const hasActiveJobs = computed(() =>
    jobs.value.some((job) => ACTIVE_STATUSES.has(job.status)),
  );
  const selectedLogs = computed(() => {
    const selection = selectedLogSequences.value;
    return logs.value.filter((item) => selection.has(item.sequence));
  });

  let jobsController: AbortController | null = null;
  let logsController: AbortController | null = null;
  let errorsController: AbortController | null = null;
  let cancelController: AbortController | null = null;
  let exportController: AbortController | null = null;
  let pollTimer: number | null = null;
  let pollingEnabled = false;
  let jobsRequestId = 0;
  let logsRequestId = 0;

  function reportError(title: string, error: unknown): void {
    events.onError?.(title, errorMessage(error));
  }

  function clearPollTimer(): void {
    if (pollTimer === null) return;
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(): void {
    clearPollTimer();
    if (!pollingEnabled || !visible.value || !documentVisible.value) return;
    const interval = Math.max(
      1_000,
      events.pollIntervalMs ?? (hasActiveJobs.value ? 1_000 : 5_000),
    );
    pollTimer = window.setTimeout(async () => {
      pollTimer = null;
      const requests: Array<Promise<boolean>> = [];
      if (!jobCursor.value) requests.push(loadJobs({ quiet: true }));
      if (followingLogs.value && !logCursor.value) requests.push(loadLogs({ quiet: true }));
      await Promise.all(requests);
      schedulePoll();
    }, interval);
  }

  async function loadJobs({ quiet = false }: { quiet?: boolean } = {}): Promise<boolean> {
    const requestId = ++jobsRequestId;
    jobsController?.abort();
    jobsController = new AbortController();
    if (!quiet) jobsLoading.value = true;
    try {
      const payload = await api.listJobHistory(
        {
          ...jobFilters.value,
          cursor: jobCursor.value || undefined,
          limit: ACTIVITY_PAGE_SIZE,
        },
        jobsController.signal,
      );
      if (requestId !== jobsRequestId) return false;
      const page = normalizeJobHistoryPage(payload);
      jobs.value = page.items;
      jobNextCursor.value = page.nextCursor;
      jobHasMore.value = page.hasMore;
      jobTotalCount.value = page.totalCount;
      jobsError.value = "";
      if (selectedJobId.value && !jobs.value.some((job) => job.id === selectedJobId.value)) {
        selectedJobId.value = "";
      }
      return true;
    } catch (error) {
      if (requestId === jobsRequestId && !isAbortError(error)) {
        jobsError.value = errorMessage(error);
        reportError("无法读取任务历史", error);
      }
      return false;
    } finally {
      if (requestId === jobsRequestId) jobsLoading.value = false;
    }
  }

  async function loadLogs({ quiet = false }: { quiet?: boolean } = {}): Promise<boolean> {
    const requestId = ++logsRequestId;
    logsController?.abort();
    logsController = new AbortController();
    if (!quiet) logsLoading.value = true;
    try {
      const payload = await api.listActivityLogs(
        {
          ...logFilters.value,
          cursor: logCursor.value || undefined,
          limit: ACTIVITY_PAGE_SIZE,
        },
        logsController.signal,
      );
      if (requestId !== logsRequestId) return false;
      const page = normalizeActivityLogPage(payload);
      logs.value = page.items;
      logNextCursor.value = page.nextCursor;
      logHasMore.value = page.hasMore;
      logTotalCount.value = page.totalCount;
      logsError.value = "";
      const available = new Set(logs.value.map((item) => item.sequence));
      selectedLogSequences.value = new Set(
        [...selectedLogSequences.value].filter((sequence) => available.has(sequence)),
      );
      return true;
    } catch (error) {
      if (requestId === logsRequestId && !isAbortError(error)) {
        logsError.value = errorMessage(error);
        reportError("无法读取操作日志", error);
      }
      return false;
    } finally {
      if (requestId === logsRequestId) logsLoading.value = false;
    }
  }

  async function applyJobFilters(filters: JobHistoryFilters): Promise<boolean> {
    jobFilters.value = { ...filters };
    jobCursor.value = "";
    jobCursorHistory.value = [];
    return loadJobs();
  }

  async function applyLogFilters(filters: ActivityLogFilters): Promise<boolean> {
    exportController?.abort();
    logFilters.value = { ...filters };
    selectedJobId.value = filters.jobId;
    logCursor.value = "";
    logCursorHistory.value = [];
    followingLogs.value = true;
    const result = await loadLogs();
    schedulePoll();
    return result;
  }

  async function nextJobs(): Promise<boolean> {
    if (jobsLoading.value || !jobHasMore.value || !jobNextCursor.value) return false;
    jobCursorHistory.value = [...jobCursorHistory.value, jobCursor.value];
    jobCursor.value = jobNextCursor.value;
    return loadJobs();
  }

  async function previousJobs(): Promise<boolean> {
    if (jobsLoading.value || !jobCursorHistory.value.length) return false;
    const history = [...jobCursorHistory.value];
    jobCursor.value = history.pop() ?? "";
    jobCursorHistory.value = history;
    return loadJobs();
  }

  async function nextLogs(): Promise<boolean> {
    if (logsLoading.value || !logHasMore.value || !logNextCursor.value) return false;
    followingLogs.value = false;
    logCursorHistory.value = [...logCursorHistory.value, logCursor.value];
    logCursor.value = logNextCursor.value;
    return loadLogs();
  }

  async function previousLogs(): Promise<boolean> {
    if (logsLoading.value || !logCursorHistory.value.length) return false;
    const history = [...logCursorHistory.value];
    logCursor.value = history.pop() ?? "";
    logCursorHistory.value = history;
    return loadLogs();
  }

  async function selectJob(jobId: string): Promise<boolean> {
    const normalized = jobId.trim();
    selectedJobId.value = normalized;
    return applyLogFilters({ ...logFilters.value, jobId: normalized });
  }

  async function clearSelectedJob(): Promise<boolean> {
    return selectJob("");
  }

  async function loadJobErrors(jobId: string, append = false): Promise<boolean> {
    const normalized = jobId.trim();
    if (!normalized) return false;
    errorsController?.abort();
    errorsController = new AbortController();
    jobErrorsLoading.value = true;
    try {
      const payload = await api.listActivityLogs(
        {
          level: "",
          category: "image_failure",
          libraryId: "",
          jobId: normalized,
          query: "",
          cursor: append ? jobErrorNextCursor.value : undefined,
          limit: ACTIVITY_PAGE_SIZE,
        },
        errorsController.signal,
      );
      const page = normalizeActivityLogPage(payload);
      jobErrorLogs.value = append ? [...jobErrorLogs.value, ...page.items] : page.items;
      jobErrorNextCursor.value = page.nextCursor;
      jobErrorHasMore.value = page.hasMore;
      jobErrorsError.value = "";
      return true;
    } catch (error) {
      if (!isAbortError(error)) {
        jobErrorsError.value = errorMessage(error);
        reportError("无法读取错误图片", error);
      }
      return false;
    } finally {
      jobErrorsLoading.value = false;
    }
  }

  async function openJobErrors(jobId: string): Promise<boolean> {
    expandedErrorJobId.value = jobId;
    jobErrorLogs.value = [];
    jobErrorNextCursor.value = "";
    jobErrorHasMore.value = false;
    await selectJob(jobId);
    return loadJobErrors(jobId);
  }

  async function retryJobErrors(): Promise<boolean> {
    return loadJobErrors(expandedErrorJobId.value);
  }

  async function loadMoreJobErrors(): Promise<boolean> {
    if (!jobErrorHasMore.value || jobErrorsLoading.value) return false;
    return loadJobErrors(expandedErrorJobId.value, true);
  }

  function closeJobErrors(): void {
    errorsController?.abort();
    expandedErrorJobId.value = "";
    jobErrorLogs.value = [];
    jobErrorsError.value = "";
    jobErrorNextCursor.value = "";
    jobErrorHasMore.value = false;
  }

  async function cancelJob(jobId: string): Promise<boolean> {
    if (!jobId.trim() || cancellingJobId.value) return false;
    cancellingJobId.value = jobId;
    cancelController?.abort();
    cancelController = new AbortController();
    try {
      await api.cancelJob(jobId, cancelController.signal);
      await Promise.all([loadJobs({ quiet: true }), loadLogs({ quiet: true })]);
      events.onInfo?.("取消请求已提交", "任务会在安全检查点停止。已完成的数据不会回滚。");
      return true;
    } catch (error) {
      if (!isAbortError(error)) reportError("无法取消任务", error);
      return false;
    } finally {
      cancellingJobId.value = "";
    }
  }

  function toggleLogSelection(sequence: number): void {
    const next = new Set(selectedLogSequences.value);
    if (next.has(sequence)) next.delete(sequence);
    else next.add(sequence);
    selectedLogSequences.value = next;
  }

  function toggleCurrentLogPage(selected: boolean): void {
    const next = new Set(selectedLogSequences.value);
    for (const item of logs.value) {
      if (selected) next.add(item.sequence);
      else next.delete(item.sequence);
    }
    selectedLogSequences.value = next;
  }

  async function copySelectedLogs(): Promise<boolean> {
    if (!selectedLogs.value.length) return false;
    try {
      const copy = events.copyText ?? copyToClipboard;
      await copy(selectedLogs.value.map(copyLine).join("\n"));
      events.onInfo?.("日志已复制", `已复制 ${selectedLogs.value.length} 条日志。`);
      return true;
    } catch (error) {
      reportError("无法复制日志", error);
      return false;
    }
  }

  async function collectFilteredLogs(signal: AbortSignal): Promise<ActivityLogItem[]> {
    const collected: ActivityLogItem[] = [];
    const seenSequences = new Set<number>();
    const seenCursors = new Set<string>();
    let cursor = "";

    while (collected.length < MAX_ACTIVITY_EXPORT_ITEMS) {
      const payload = await api.listActivityLogs(
        {
          ...logFilters.value,
          cursor: cursor || undefined,
          limit: ACTIVITY_EXPORT_PAGE_SIZE,
        },
        signal,
      );
      const pageItems = wireItems(payload, ["items", "logs"]).map(normalizeActivityLog);
      for (const item of pageItems) {
        if (seenSequences.has(item.sequence)) continue;
        seenSequences.add(item.sequence);
        collected.push(item);
        if (collected.length >= MAX_ACTIVITY_EXPORT_ITEMS) break;
      }

      const nextCursor = firstText(payload.next_cursor);
      const hasMore = payload.has_more === true && Boolean(nextCursor);
      if (!hasMore) return collected;
      if (collected.length >= MAX_ACTIVITY_EXPORT_ITEMS) {
        throw new Error("筛选结果超过 50,000 条，请缩小范围后再导出。");
      }
      if (nextCursor === cursor || seenCursors.has(nextCursor)) {
        throw new Error("日志分页游标重复，导出已安全停止。");
      }
      seenCursors.add(nextCursor);
      cursor = nextCursor;
    }
    return collected;
  }

  async function exportLogs(format: ActivityExportFormat): Promise<boolean> {
    if (logsExporting.value) return false;
    exportController?.abort();
    const controller = new AbortController();
    exportController = controller;
    logsExporting.value = true;
    try {
      const selected = selectedLogs.value;
      const items = selected.length ? selected : await collectFilteredLogs(controller.signal);
      if (!items.length) return false;
      const content = serializeActivityLogs(items, format);
      const mimeType = format === "jsonl" ? "application/x-ndjson;charset=utf-8" : "text/csv;charset=utf-8";
      const filename = `zvec-activity-${timestampForFilename()}.${format}`;
      (events.downloadText ?? downloadText)(filename, content, mimeType);
      const scope = selected.length ? "选中日志" : "当前筛选结果";
      events.onInfo?.("日志已导出", `已导出 ${items.length} 条${scope}。`);
      return true;
    } catch (error) {
      if (!isAbortError(error)) reportError("无法导出日志", error);
      return false;
    } finally {
      if (exportController === controller) exportController = null;
      logsExporting.value = false;
    }
  }

  async function setFollowing(enabled: boolean): Promise<boolean> {
    followingLogs.value = enabled;
    if (!enabled) return true;
    logCursor.value = "";
    logCursorHistory.value = [];
    const result = await loadLogs();
    schedulePoll();
    return result;
  }

  function setVisible(nextVisible: boolean): void {
    visible.value = nextVisible;
    if (nextVisible) {
      schedulePoll();
      return;
    }
    clearPollTimer();
    jobsController?.abort();
    logsController?.abort();
    exportController?.abort();
  }

  function handleDocumentVisibilityChange(): void {
    documentVisible.value = document.visibilityState !== "hidden";
    if (!documentVisible.value) {
      clearPollTimer();
      jobsController?.abort();
      logsController?.abort();
      return;
    }
    schedulePoll();
  }

  function startPolling(): void {
    pollingEnabled = true;
    schedulePoll();
  }

  function stopPolling(): void {
    pollingEnabled = false;
    clearPollTimer();
  }

  async function refreshAll(): Promise<boolean> {
    if (!visible.value || !documentVisible.value) return true;
    const [jobsLoaded, logsLoaded] = await Promise.all([loadJobs(), loadLogs()]);
    return jobsLoaded && logsLoaded;
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleDocumentVisibilityChange);
    if (events.autoStart === false) return;
    pollingEnabled = true;
    void refreshAll().finally(schedulePoll);
  });

  onBeforeUnmount(() => {
    document.removeEventListener("visibilitychange", handleDocumentVisibilityChange);
    stopPolling();
    jobsController?.abort();
    logsController?.abort();
    errorsController?.abort();
    cancelController?.abort();
    exportController?.abort();
  });

  return {
    jobs,
    logs,
    jobFilters,
    logFilters,
    selectedJobId,
    selectedLogSequences,
    selectedLogs,
    expandedErrorJobId,
    jobErrorLogs,
    jobErrorsLoading,
    jobErrorsError,
    jobErrorHasMore,
    jobsLoading,
    logsLoading,
    logsExporting,
    cancellingJobId,
    jobsError,
    logsError,
    followingLogs,
    visible,
    jobPage,
    logPage,
    jobHasMore,
    logHasMore,
    hasPreviousJobs,
    hasPreviousLogs,
    jobTotalCount,
    logTotalCount,
    loadJobs,
    loadLogs,
    refreshAll,
    applyJobFilters,
    applyLogFilters,
    nextJobs,
    previousJobs,
    nextLogs,
    previousLogs,
    selectJob,
    clearSelectedJob,
    openJobErrors,
    retryJobErrors,
    loadMoreJobErrors,
    closeJobErrors,
    cancelJob,
    toggleLogSelection,
    toggleCurrentLogPage,
    copySelectedLogs,
    exportLogs,
    setFollowing,
    setVisible,
    startPolling,
    stopPolling,
  };
}
