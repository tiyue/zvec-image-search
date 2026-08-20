import { computed, onBeforeUnmount, ref } from "vue";

import { requestJson } from "../../api/gateway";

export const SEARCH_RESULTS_KEEP_LATEST = 3;
const TERMINAL_STATUSES = new Set([
  "succeeded",
  "completed",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
]);
const SUCCESS_STATUSES = new Set(["succeeded", "completed", "partial", "needs_attention"]);
const MAX_CONSECUTIVE_POLL_FAILURES = 4;

export interface SearchResultsCleanupRequest {
  task_type: "search_results_cleanup";
  keep_latest: number;
}

interface CleanupJobWire {
  id?: unknown;
  job_id?: unknown;
  status?: unknown;
  state?: unknown;
  message?: unknown;
  progress_percent?: unknown;
  progress?: {
    percent?: unknown;
    percentage?: unknown;
    processed?: unknown;
    completed?: unknown;
    current?: unknown;
    total?: unknown;
  };
  result?: Record<string, unknown>;
  deleted?: unknown;
  deleted_count?: unknown;
  skipped?: unknown;
  skipped_count?: unknown;
  failed?: unknown;
  failed_count?: unknown;
  failures?: unknown;
  failure_count?: unknown;
  error?: unknown;
}

export interface CleanupJobResponse extends CleanupJobWire {
  job?: CleanupJobWire;
  jobs?: CleanupJobWire[];
  result?: Record<string, unknown>;
}

export interface SearchResultsCleanupJob {
  id: string;
  status: string;
  progressPercent: number;
  message: string;
  deleted: number;
  skipped: number;
  failed: number;
}

export interface SearchResultsCleanupApi {
  submit(
    body: SearchResultsCleanupRequest,
    signal?: AbortSignal,
  ): Promise<CleanupJobResponse>;
  detail(jobId: string, signal?: AbortSignal): Promise<CleanupJobResponse>;
}

interface CleanupEvents {
  onError?: (title: string, message: string) => void;
  onInfo?: (title: string, message: string) => void;
  pollIntervalMs?: number;
}

export const searchResultsCleanupApi: SearchResultsCleanupApi = {
  submit: (body, signal) =>
    requestJson<CleanupJobResponse>("api/jobs", { method: "POST", body, signal }),
  detail: (jobId, signal) =>
    requestJson<CleanupJobResponse>(`api/jobs/${encodeURIComponent(jobId)}`, { signal }),
};

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function nonNegativeInteger(...values: unknown[]): number {
  for (const value of values) {
    if (Array.isArray(value)) return value.length;
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return Math.max(0, Math.trunc(parsed));
  }
  return 0;
}

function extractWire(payload: CleanupJobResponse): CleanupJobWire {
  const nested = payload.job ?? payload.jobs?.[0];
  if (!nested) return payload;
  if (nested.result || !payload.result) return nested;
  return { ...nested, result: payload.result };
}

function errorMessage(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    return text((value as { message?: unknown }).message);
  }
  return "";
}

export function normalizeSearchResultsCleanupJob(
  payload: CleanupJobResponse,
): SearchResultsCleanupJob {
  const raw = extractWire(payload);
  const result = raw.result ?? {};
  const progress = raw.progress ?? {};
  const id = text(raw.id, raw.job_id);
  const status = text(raw.status, raw.state, id ? "queued" : "succeeded").toLowerCase();
  const processed = nonNegativeInteger(progress.processed, progress.completed, progress.current);
  const total = nonNegativeInteger(progress.total);
  const calculatedPercent = total > 0 ? Math.round((processed / total) * 100) : 0;
  const terminalPercent = TERMINAL_STATUSES.has(status) ? 100 : calculatedPercent;
  const reportedPercent = nonNegativeInteger(
    raw.progress_percent,
    progress.percent,
    progress.percentage,
    calculatedPercent,
  );
  const failed = nonNegativeInteger(
    result.failed,
    result.failed_count,
    result.failures,
    raw.failed,
    raw.failed_count,
    raw.failures,
    raw.failure_count,
  );
  return {
    id,
    status,
    progressPercent: Math.min(100, Math.max(reportedPercent, terminalPercent)),
    message: text(raw.message, errorMessage(raw.error)),
    deleted: nonNegativeInteger(
      result.deleted,
      result.deleted_count,
      raw.deleted,
      raw.deleted_count,
    ),
    skipped: nonNegativeInteger(
      result.skipped,
      result.skipped_count,
      raw.skipped,
      raw.skipped_count,
    ),
    failed,
  };
}

export function isCleanupJobTerminal(job: SearchResultsCleanupJob): boolean {
  return TERMINAL_STATUSES.has(job.status);
}

function completionSummary(job: SearchResultsCleanupJob): string {
  return `已删除 ${job.deleted} 项，跳过 ${job.skipped} 项，失败 ${job.failed} 项。`;
}

export function useSearchResultsCleanup(
  api: SearchResultsCleanupApi = searchResultsCleanupApi,
  events: CleanupEvents = {},
) {
  const job = ref<SearchResultsCleanupJob | null>(null);
  const submitting = ref(false);
  const running = computed(
    () => submitting.value || Boolean(job.value && !isCleanupJobTerminal(job.value)),
  );
  const statusLabel = computed(() => {
    const status = job.value?.status ?? "";
    if (running.value) return "正在清理";
    if (SUCCESS_STATUSES.has(status)) return "清理完成";
    if (status === "cancelled") return "清理已取消";
    if (status === "failed") return "清理失败";
    return "等待清理";
  });

  let pollTimer: number | null = null;
  let controller: AbortController | null = null;
  let disposed = false;
  let consecutivePollFailures = 0;

  function clearPollTimer(): void {
    if (pollTimer === null) return;
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function finish(nextJob: SearchResultsCleanupJob): void {
    job.value = nextJob;
    clearPollTimer();
    if (SUCCESS_STATUSES.has(nextJob.status)) {
      events.onInfo?.("搜索结果清理完成", nextJob.message || completionSummary(nextJob));
    } else {
      events.onError?.(
        nextJob.status === "cancelled" ? "搜索结果清理已取消" : "搜索结果清理失败",
        nextJob.message || completionSummary(nextJob),
      );
    }
  }

  function schedulePoll(): void {
    clearPollTimer();
    if (disposed || !job.value?.id || isCleanupJobTerminal(job.value)) return;
    pollTimer = window.setTimeout(
      () => void poll(),
      Math.max(50, Math.trunc(events.pollIntervalMs ?? 900)),
    );
  }

  async function poll(): Promise<void> {
    const jobId = job.value?.id;
    if (!jobId || disposed) return;
    controller?.abort();
    controller = new AbortController();
    try {
      const payload = await api.detail(jobId, controller.signal);
      const nextJob = normalizeSearchResultsCleanupJob(payload);
      job.value = nextJob;
      consecutivePollFailures = 0;
      if (isCleanupJobTerminal(nextJob)) finish(nextJob);
      else schedulePoll();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      consecutivePollFailures += 1;
      if (consecutivePollFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        finish({
          ...(job.value ?? normalizeSearchResultsCleanupJob({ status: "failed" })),
          status: "failed",
          message: errorMessage(error) || "无法读取后台清理进度，请稍后重试。",
        });
      } else {
        schedulePoll();
      }
    }
  }

  async function submit(): Promise<boolean> {
    if (running.value) return false;
    submitting.value = true;
    controller?.abort();
    controller = new AbortController();
    try {
      const payload = await api.submit(
        { task_type: "search_results_cleanup", keep_latest: SEARCH_RESULTS_KEEP_LATEST },
        controller.signal,
      );
      const nextJob = normalizeSearchResultsCleanupJob(payload);
      job.value = nextJob;
      consecutivePollFailures = 0;
      if (isCleanupJobTerminal(nextJob)) finish(nextJob);
      else {
        events.onInfo?.("清理任务已提交", "正在后台清理搜索结果，默认保留最近 3 次。");
        schedulePoll();
      }
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      events.onError?.(
        "无法清理搜索结果",
        errorMessage(error) || "本地服务没有接受清理任务，请稍后重试。",
      );
      return false;
    } finally {
      submitting.value = false;
    }
  }

  function dispose(): void {
    disposed = true;
    clearPollTimer();
    controller?.abort();
  }

  onBeforeUnmount(dispose);

  return {
    job,
    submitting,
    running,
    statusLabel,
    submit,
    dispose,
  };
}
