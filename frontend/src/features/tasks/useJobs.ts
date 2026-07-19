import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
} from "vue";

import { GatewayError, requestJson } from "../../api/gateway";
import type {
  AutoTagEstimate,
  AutoTagEstimateSubmission,
  JobErrorImage,
  JobErrorImageWire,
  JobSubmission,
  JobsApi,
  JobsResponse,
  JobWire,
  LibraryJob,
} from "./types";

const ACTIVE_STATUSES = new Set(["queued", "pending", "running", "cancelling"]);
const TERMINAL_STATUSES = new Set([
  "succeeded",
  "completed",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
  "interrupted",
]);
const SUCCESS_STATUSES = new Set(["succeeded", "completed", "partial", "needs_attention"]);
const MAX_AI_IMAGES = 10_000;
const ESTIMATE_TIMEOUT_MS = 60_000;
const ESTIMATE_POLL_INTERVAL_MS = 250;

// Keep the public error name for existing callers while all pages share one
// hardened JSON transport implementation.
export { GatewayError as JobsApiError };

export const jobsApi: JobsApi = {
  list: (signal) => requestJson<JobsResponse>("api/jobs", { signal }),
  submit: (body, signal) =>
    requestJson<JobsResponse>("api/jobs", { method: "POST", body, signal }),
  detail: (jobId, signal) =>
    requestJson<JobsResponse>(`api/jobs/${encodeURIComponent(jobId)}`, { signal }),
  cancel: (jobId, signal) =>
    requestJson<JobsResponse>(`api/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      signal,
    }),
};

interface JobsEvents {
  onError?: (title: string, message: string) => void;
  onInfo?: (title: string, message: string) => void;
  pollIntervalMs?: number;
  autoStart?: boolean;
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

function boundedPercent(value: unknown): number {
  return Math.min(100, Math.max(0, integer(value)));
}

function safeLocalUrl(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "";
  try {
    const url = new URL(value.trim(), document.baseURI);
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    const supported = url.protocol === "http:" || url.protocol === "https:";
    return supported && (url.origin === window.location.origin || loopback) ? url.href : "";
  } catch {
    return "";
  }
}

function normalizeErrorImage(raw: JobErrorImageWire, index: number): JobErrorImage {
  return {
    id: firstText(raw.id, `error-${index + 1}`),
    name: firstText(raw.name, raw.filename, raw.relative_path, `错误图片 ${index + 1}`),
    reason: firstText(raw.reason, raw.message, raw.error, "处理失败"),
    thumbnailUrl: safeLocalUrl(raw.thumbnail_url),
    imageUrl: safeLocalUrl(raw.image_url),
  };
}

export function normalizeJob(raw: JobWire, index = 0): LibraryJob {
  const progress = raw.progress ?? {};
  const result = raw.result ?? {};
  const processed = Math.max(
    0,
    integer(raw.processed ?? progress.processed ?? progress.completed ?? progress.current),
  );
  const total = Math.max(
    0,
    integer(
      progress.candidate_count ??
        result.candidate_count ??
        raw.total ??
        progress.total ??
        progress.items,
    ),
  );
  const calculated = total > 0 ? Math.round((processed / total) * 100) : 0;
  const rawErrors = raw.error_images ?? raw.failures ?? [];
  const errors = rawErrors.map(normalizeErrorImage);
  return {
    id: firstText(raw.id, raw.job_id, `job-${index + 1}`),
    type: firstText(raw.task_type, raw.type, raw.command, "任务"),
    libraryId: firstText(raw.library_id),
    libraryName: firstText(raw.library_name, raw.library_id, "全部图库"),
    status: firstText(raw.status, raw.state, "queued").toLowerCase(),
    progressPercent: boundedPercent(
      raw.progress_percent ?? progress.percent ?? progress.percentage ?? calculated,
    ),
    message: firstText(raw.message, raw.progress_message, progress.message),
    processed,
    total,
    failureCount: Math.max(errors.length, integer(raw.failure_count ?? raw.failed)),
    errorImages: errors,
    createdAt: firstText(raw.created_at, raw.submitted_at),
    startedAt: firstText(raw.started_at),
    finishedAt: firstText(raw.finished_at),
  };
}

function extractJobs(payload: JobsResponse | JobWire[] | JobWire): JobWire[] {
  if (Array.isArray(payload)) return payload;
  const response = payload as JobsResponse;
  if (Array.isArray(response.jobs)) return response.jobs;
  if (response.job) return [response.job];
  const possibleJob = payload as JobWire;
  return possibleJob.id || possibleJob.job_id ? [possibleJob] : [];
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function positiveInteger(value: unknown, maximum: number): number | null {
  const text = String(value).trim();
  if (!/^[1-9]\d*$/u.test(text)) return null;
  const parsed = Number(text);
  return Number.isSafeInteger(parsed) && parsed <= maximum ? parsed : null;
}

function optionalPositiveNumber(value: unknown): number | null | undefined {
  if (value === undefined || value === null || String(value).trim() === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function nonNegativeInteger(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function normalizeEstimate(job: JobWire): AutoTagEstimate | null {
  const result = job.result;
  if (!result) return null;
  const estimatedCost = Number(result.estimated_cost_cny);
  return {
    candidateCount: nonNegativeInteger(result.candidate_count),
    uniqueImageCount: nonNegativeInteger(result.unique_image_count),
    cachedCount: nonNegativeInteger(result.cached_count),
    apiRequestCount: nonNegativeInteger(result.api_request_count),
    estimatedInputTokens: nonNegativeInteger(result.estimated_input_tokens),
    estimatedOutputTokens: nonNegativeInteger(result.estimated_output_tokens),
    estimatedCostCny: Number.isFinite(estimatedCost) ? Math.max(0, estimatedCost) : null,
    overBudget: result.over_budget === true,
  };
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export function isActiveJob(job: LibraryJob): boolean {
  return ACTIVE_STATUSES.has(job.status);
}

export function useJobs(api: JobsApi = jobsApi, events: JobsEvents = {}) {
  const jobs = shallowRef<LibraryJob[]>([]);
  const selectedJobId = ref("");
  const loading = ref(false);
  const detailLoading = ref(false);
  const submitting = ref(false);
  const estimating = ref(false);
  const autoTagEstimate = shallowRef<AutoTagEstimate | null>(null);
  const cancellingJobId = ref("");
  const lastError = ref("");

  const selectedJob = computed(
    () => jobs.value.find((job) => job.id === selectedJobId.value) ?? null,
  );
  const selectedErrors = computed(() => selectedJob.value?.errorImages ?? []);
  const hasActiveJobs = computed(() => jobs.value.some(isActiveJob));

  let pollTimer: number | null = null;
  let pollingEnabled = false;
  let refreshPromise: Promise<boolean> | null = null;
  let controller: AbortController | null = null;
  let estimateController: AbortController | null = null;

  function reportError(title: string, error: unknown): void {
    lastError.value = messageOf(error);
    events.onError?.(title, lastError.value);
  }

  function clearTimer(): void {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedulePoll(): void {
    clearTimer();
    if (!pollingEnabled || !hasActiveJobs.value) return;
    const interval = Math.max(250, events.pollIntervalMs ?? 2_500);
    pollTimer = window.setTimeout(async () => {
      pollTimer = null;
      await refreshJobs({ quiet: true });
    }, interval);
  }

  function replaceJobs(next: LibraryJob[]): void {
    const previous = new Map(jobs.value.map((job) => [job.id, job]));
    jobs.value = next.map((job) => {
      const cached = previous.get(job.id);
      // List responses can omit failure details. Preserve the selected detail
      // until a newer detail response replaces it.
      if (cached?.errorImages.length && !job.errorImages.length && job.failureCount > 0) {
        return { ...job, errorImages: cached.errorImages };
      }
      return job;
    });
    if (!jobs.value.some((job) => job.id === selectedJobId.value)) {
      selectedJobId.value =
        jobs.value.find((job) => job.failureCount > 0)?.id ?? jobs.value[0]?.id ?? "";
    }
  }

  function upsertJob(job: LibraryJob): void {
    const index = jobs.value.findIndex((item) => item.id === job.id);
    jobs.value =
      index < 0
        ? [job, ...jobs.value]
        : jobs.value.map((item, itemIndex) => (itemIndex === index ? job : item));
    selectedJobId.value = job.id;
  }

  async function refreshJobs({ quiet = false }: { quiet?: boolean } = {}): Promise<boolean> {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      if (!quiet) loading.value = true;
      controller?.abort();
      controller = new AbortController();
      try {
        const payload = await api.list(controller.signal);
        replaceJobs(extractJobs(payload).map(normalizeJob));
        lastError.value = "";
        return true;
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          reportError("无法刷新任务", error);
        }
        return false;
      } finally {
        if (!quiet) loading.value = false;
        refreshPromise = null;
        schedulePoll();
      }
    })();
    return refreshPromise;
  }

  async function selectJob(jobId: string): Promise<boolean> {
    if (!jobId) return false;
    selectedJobId.value = jobId;
    detailLoading.value = true;
    try {
      const payload = await api.detail(jobId);
      const detail = extractJobs(payload)[0];
      if (detail) upsertJob(normalizeJob(detail));
      lastError.value = "";
      return Boolean(detail);
    } catch (error) {
      reportError("无法读取任务详情", error);
      return false;
    } finally {
      detailLoading.value = false;
    }
  }

  async function submitJob(input: JobSubmission): Promise<boolean> {
    if (!input.library_id.trim()) {
      reportError("尚未选择图库", new Error("请先选择一个已启用的图库。"));
      return false;
    }
    const aiTask = input.task_type === "index_and_auto_tag" || input.task_type === "auto_tag";
    const request: JobSubmission = {
      task_type: input.task_type,
      library_id: input.library_id.trim(),
    };

    if (aiTask) {
      const maxImages = positiveInteger(input.max_images, MAX_AI_IMAGES);
      if (maxImages === null) {
        reportError(
          "图片上限无效",
          new Error(`请输入 1–${MAX_AI_IMAGES.toLocaleString("zh-CN")} 的整数。`),
        );
        return false;
      }
      const budget = optionalPositiveNumber(input.max_budget_cny);
      if (budget === null) {
        reportError("预算上限无效", new Error("预算上限必须是大于 0 的有限数字，或留空。"));
        return false;
      }
      if (input.external_processing_confirmed !== true) {
        reportError(
          "需要确认外部处理",
          new Error("请确认图片会发送至阿里云大模型后再提交智能标注任务。"),
        );
        return false;
      }
      request.max_images = maxImages;
      if (budget !== undefined) request.max_budget_cny = budget;
      request.external_processing_confirmed = true;
      if (input.task_type === "auto_tag") {
        request.scope = input.scope ?? "untagged";
        if (request.scope === "all" && input.all_scope_confirmed !== true) {
          reportError(
            "需要确认全部重跑",
            new Error("全部图片重新处理可能产生较高费用，请勾选额外确认后再提交。"),
          );
          return false;
        }
        if (request.scope === "all") request.all_scope_confirmed = true;
      }
    }

    submitting.value = true;
    try {
      const payload = await api.submit(request);
      const submitted = extractJobs(payload)[0];
      if (submitted) upsertJob(normalizeJob(submitted));
      events.onInfo?.(
        "任务已加入队列",
        "任务会按照后端运行配置的并发、限流与错误隔离策略安全执行。",
      );
      lastError.value = "";
      pollingEnabled = true;
      schedulePoll();
      return true;
    } catch (error) {
      reportError("无法创建任务", error);
      return false;
    } finally {
      submitting.value = false;
    }
  }

  async function estimateAutoTag(input: AutoTagEstimateSubmission): Promise<boolean> {
    const libraryId = input.library_id.trim();
    if (!libraryId) {
      reportError("尚未选择图库", new Error("请先选择一个已启用的图库。"));
      return false;
    }
    const maxImages = positiveInteger(input.max_images, MAX_AI_IMAGES);
    if (maxImages === null) {
      reportError(
        "图片上限无效",
        new Error(`请输入 1–${MAX_AI_IMAGES.toLocaleString("zh-CN")} 的整数。`),
      );
      return false;
    }
    const budget = optionalPositiveNumber(input.max_budget_cny);
    if (budget === null) {
      reportError("预算上限无效", new Error("预算上限必须是大于 0 的有限数字，或留空。"));
      return false;
    }

    estimateController?.abort();
    estimateController = new AbortController();
    const signal = estimateController.signal;
    estimating.value = true;
    autoTagEstimate.value = null;
    try {
      const request: AutoTagEstimateSubmission = {
        task_type: "auto_tag_estimate",
        library_id: libraryId,
        scope: input.scope,
        max_images: maxImages,
      };
      if (budget !== undefined) request.max_budget_cny = budget;
      let raw = extractJobs(await api.submit(request, signal))[0];
      if (!raw) throw new Error("后端没有返回估算任务。" );
      const deadline = Date.now() + ESTIMATE_TIMEOUT_MS;
      while (!TERMINAL_STATUSES.has(firstText(raw.status, raw.state).toLowerCase())) {
        if (!raw.id && !raw.job_id) throw new Error("估算任务缺少任务编号。" );
        if (Date.now() >= deadline) throw new Error("估算在 60 秒内未完成，请稍后重试。" );
        await abortableDelay(ESTIMATE_POLL_INTERVAL_MS, signal);
        const detail = extractJobs(await api.detail(firstText(raw.id, raw.job_id), signal))[0];
        if (!detail) throw new Error("无法读取估算结果。" );
        raw = detail;
      }
      const status = firstText(raw.status, raw.state).toLowerCase();
      if (!SUCCESS_STATUSES.has(status)) {
        throw new Error(firstText(raw.message, raw.progress_message) || "智能标注估算失败。" );
      }
      const estimate = normalizeEstimate(raw);
      if (!estimate) throw new Error("估算结果缺少费用与候选统计。" );
      autoTagEstimate.value = estimate;
      upsertJob(normalizeJob(raw));
      events.onInfo?.("估算完成", "估算只读取本地状态与缓存，没有调用模型。" );
      lastError.value = "";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      reportError("无法估算智能标注", error);
      return false;
    } finally {
      estimating.value = false;
    }
  }

  async function cancelJob(jobId: string): Promise<boolean> {
    if (!jobId || cancellingJobId.value) return false;
    cancellingJobId.value = jobId;
    try {
      const payload = await api.cancel(jobId);
      const cancelled = extractJobs(payload)[0];
      if (cancelled) upsertJob(normalizeJob(cancelled));
      events.onInfo?.("已请求取消", "任务会在安全边界停止，其他任务会继续运行。");
      lastError.value = "";
      await refreshJobs({ quiet: true });
      return true;
    } catch (error) {
      reportError("无法取消任务", error);
      return false;
    } finally {
      cancellingJobId.value = "";
    }
  }

  function startPolling(): void {
    pollingEnabled = true;
    schedulePoll();
  }

  function stopPolling(): void {
    pollingEnabled = false;
    clearTimer();
  }

  onMounted(() => {
    if (events.autoStart === false) return;
    pollingEnabled = true;
    void refreshJobs();
  });

  onBeforeUnmount(() => {
    stopPolling();
    controller?.abort();
    estimateController?.abort();
  });

  return {
    jobs,
    selectedJobId,
    selectedJob,
    selectedErrors,
    hasActiveJobs,
    loading,
    detailLoading,
    submitting,
    estimating,
    autoTagEstimate,
    cancellingJobId,
    lastError,
    refreshJobs,
    selectJob,
    submitJob,
    estimateAutoTag,
    cancelJob,
    startPolling,
    stopPolling,
  };
}
