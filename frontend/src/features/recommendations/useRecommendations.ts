import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
} from "vue";

import { recommendationApi } from "./api";
import type {
  RecommendationAction,
  RecommendationApi,
  RecommendationBatch,
  RecommendationBatchWire,
  RecommendationBucket,
  RecommendationItem,
  RecommendationItemWire,
  RecommendationPersonalizationReason,
  RecommendationPreference,
} from "./types";

const RECOMMENDATION_COUNT = 15;
const CRITICAL_PRELOAD_COUNT = 6;
const THUMBNAIL_TIMEOUT_MS = 15_000;
const BUCKETS = new Set<RecommendationBucket>([
  "quality",
  "recent",
  "low_exposure",
  "random",
]);
const DEFAULT_QUOTA: Record<RecommendationBucket, number> = {
  quality: 5,
  recent: 0,
  low_exposure: 6,
  random: 4,
};
const PERSONALIZATION_REASONS = new Set<RecommendationPersonalizationReason>([
  "insufficient_preferences",
  "vectors_unavailable",
  "incompatible_vector_spaces",
  "replayed",
]);

interface RecommendationEvents {
  idFactory?: () => string;
  preloadImage?: (url: string, signal: AbortSignal) => Promise<void>;
  onError?: (title: string, message: string) => void;
}

interface PreparedBatchTask {
  requestId: string;
  source: "foreground" | "prefetch";
  controller: AbortController;
  batch: RecommendationBatch | null;
  createSucceeded: boolean;
  criticalReady: boolean;
  allSettled: boolean;
  invalid: boolean;
  preloadErrors: unknown[];
  tailFailureReported: boolean;
  critical: Promise<RecommendationBatch>;
  all: Promise<void>;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function integer(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(String).map((item) => item.trim()).filter(Boolean)
    : [];
}

function vectorSpace(value: unknown): string {
  const legacy = text(value);
  if (legacy) return legacy;
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const structured = value as Record<string, unknown>;
  const dimension = integer(structured.dimension);
  return [
    text(structured.model),
    dimension > 0 ? `${dimension}D` : "",
    text(structured.metric),
  ].filter(Boolean).join(" · ");
}

function safeLocalUrl(value: unknown): string {
  const candidate = text(value);
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

function normalizeBucket(value: unknown): RecommendationBucket {
  const bucket = text(value) as RecommendationBucket;
  if (!BUCKETS.has(bucket)) throw new Error("推荐结果包含未知来源。");
  return bucket;
}

function normalizePreference(value: unknown): RecommendationPreference | null {
  return value === "like" || value === "dislike" ? value : null;
}

function normalizePersonalizationReason(
  value: unknown,
): RecommendationPersonalizationReason | "" {
  const reason = text(value) as RecommendationPersonalizationReason;
  return PERSONALIZATION_REASONS.has(reason) ? reason : "";
}

function normalizeItem(raw: RecommendationItemWire): RecommendationItem {
  const itemId = text(raw.item_id);
  const mediaId = text(raw.media_id);
  const thumbnailUrl = safeLocalUrl(raw.thumbnail_url);
  if (!itemId || !mediaId || !thumbnailUrl) {
    throw new Error("推荐结果缺少可用的图片标识或缩略图。");
  }
  return {
    itemId,
    mediaId,
    name: text(raw.name) || "未命名图片",
    width: Math.max(0, integer(raw.width)),
    height: Math.max(0, integer(raw.height)),
    tags: strings(raw.tags),
    libraryId: text(raw.library_id),
    libraryName: text(raw.library_name) || "未知图库",
    contentType: text(raw.content_type),
    sizeBytes: Math.max(0, integer(raw.size_bytes)),
    bucket: normalizeBucket(raw.bucket),
    thumbnailUrl,
    previewUrl: safeLocalUrl(raw.preview_url),
    preference: normalizePreference(raw.preference),
  };
}

export function normalizeRecommendationBatch(raw: RecommendationBatchWire): RecommendationBatch {
  const requestId = text(raw.request_id);
  const batchId = text(raw.batch_id);
  if (!requestId || !batchId) throw new Error("推荐服务没有返回批次标识。");
  if (!Array.isArray(raw.items) || raw.items.length > RECOMMENDATION_COUNT) {
    throw new Error("推荐服务返回的图片数量无效。");
  }
  const items = raw.items.map(normalizeItem);
  if (new Set(items.map((item) => item.itemId)).size !== items.length) {
    throw new Error("推荐批次包含重复的图片项。");
  }
  const quota = { ...DEFAULT_QUOTA };
  for (const bucket of BUCKETS) {
    const value = raw.quota?.[bucket];
    if (value !== undefined) quota[bucket] = Math.max(0, integer(value));
  }
  return {
    requestId,
    batchId,
    count: items.length,
    partial: raw.partial === true || items.length < RECOMMENDATION_COUNT,
    partialReason: text(raw.partial_reason),
    quotaDegraded: raw.quota_degraded === true,
    historyWindow: Math.max(0, integer(raw.history_window)),
    items,
    quota,
    diversity: {
      applied: raw.diversity?.applied === true,
      reason: text(raw.diversity?.reason),
      missingVectors: Math.max(0, integer(raw.diversity?.missing_vectors)),
      vectorSpace: vectorSpace(raw.diversity?.vector_space),
    },
    personalization: {
      applied: raw.personalization?.applied === true,
      effectiveCount: Math.max(0, integer(raw.personalization?.effective_count)),
      reason: normalizePersonalizationReason(raw.personalization?.reason),
    },
  };
}

function abortError(): DOMException {
  return new DOMException("操作已取消。", "AbortError");
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function defaultIdFactory(): string {
  return crypto.randomUUID();
}

function defaultPreloadImage(url: string, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const image = new Image();
    let settled = false;
    const finish = (error?: Error): void => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", onAbort);
      image.onload = null;
      image.onerror = null;
      if (error) reject(error);
      else resolve();
    };
    const onAbort = (): void => {
      image.src = "";
      finish(abortError());
    };
    const timeout = window.setTimeout(
      () => finish(new Error("缩略图加载超时。")),
      THUMBNAIL_TIMEOUT_MS,
    );
    signal.addEventListener("abort", onAbort, { once: true });
    image.onload = () => finish();
    image.onerror = () => finish(new Error("缩略图加载失败。"));
    image.decoding = "async";
    image.src = url;
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useRecommendations(
  api: RecommendationApi = recommendationApi,
  events: RecommendationEvents = {},
) {
  const currentBatch = shallowRef<RecommendationBatch | null>(null);
  const stagedBatch = shallowRef<RecommendationBatch | null>(null);
  const loading = ref(false);
  const error = ref("");
  const shownError = ref("");
  const visible = ref(false);
  const documentVisible = ref(
    typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  const preferences = ref<Record<string, RecommendationPreference>>({});
  const pendingActionKeys = ref<string[]>([]);
  const pendingShown = shallowRef<{ batchId: string; eventId: string } | null>(null);
  const shownSyncing = ref(false);

  const hasBatch = computed(() => currentBatch.value !== null);
  const shownPending = computed(() => pendingShown.value !== null || shownSyncing.value);

  let initialized = false;
  let destroyed = false;
  let createRequestId = "";
  let foregroundTask: PreparedBatchTask | null = null;
  let stagedTask: PreparedBatchTask | null = null;
  let currentTask: PreparedBatchTask | null = null;
  let prefetchTask: PreparedBatchTask | null = null;
  let shownCompletedBatchId = "";
  let shownController: AbortController | null = null;
  let shownRequest: Promise<boolean> | null = null;
  const actionControllers = new Set<AbortController>();
  const retryEventIds = new Map<string, string>();
  const idFactory = events.idFactory ?? defaultIdFactory;
  const preloadImage = events.preloadImage ?? defaultPreloadImage;

  function reportError(title: string, caught: unknown): void {
    events.onError?.(title, errorMessage(caught));
  }

  function isCurrentBatch(batchId: string): boolean {
    return currentBatch.value?.batchId === batchId;
  }

  function isTaskActive(task: PreparedBatchTask): boolean {
    return !destroyed && !task.invalid;
  }

  function discardTask(task: PreparedBatchTask, abort = true): void {
    if (task.invalid) return;
    task.invalid = true;
    if (abort) task.controller.abort();
    if (foregroundTask === task) foregroundTask = null;
    if (stagedTask === task) {
      stagedTask = null;
      stagedBatch.value = null;
    }
    if (currentTask === task) currentTask = null;
    if (prefetchTask === task) prefetchTask = null;
  }

  function discardPrefetch(): void {
    if (prefetchTask) discardTask(prefetchTask);
  }

  function reportTailFailure(task: PreparedBatchTask): void {
    if (
      task.tailFailureReported ||
      !task.criticalReady ||
      !task.preloadErrors.length ||
      currentTask !== task
    ) return;
    task.tailFailureReported = true;
    const message = `${task.preloadErrors.length} 张非关键缩略图加载失败。`;
    error.value = message;
    reportError("部分缩略图加载失败", message);
  }

  function canPrefetch(): boolean {
    const batch = currentBatch.value;
    return Boolean(
      batch?.items.length &&
      currentTask?.allSettled &&
      shownCompletedBatchId === batch.batchId &&
      visible.value &&
      documentVisible.value &&
      !loading.value &&
      !createRequestId &&
      !foregroundTask &&
      !stagedTask &&
      !prefetchTask &&
      !pendingShown.value &&
      !shownSyncing.value,
    );
  }

  function handleTaskSettled(task: PreparedBatchTask): void {
    if (!isTaskActive(task)) return;
    if (prefetchTask === task) {
      if (task.preloadErrors.length) {
        discardTask(task);
      }
      return;
    }
    if (currentTask === task) {
      reportTailFailure(task);
      maybeStartPrefetch();
    }
  }

  function createPreparedTask(
    requestId: string,
    source: PreparedBatchTask["source"],
  ): PreparedBatchTask {
    const controller = new AbortController();
    const task: PreparedBatchTask = {
      requestId,
      source,
      controller,
      batch: null,
      createSucceeded: false,
      criticalReady: false,
      allSettled: false,
      invalid: false,
      preloadErrors: [],
      tailFailureReported: false,
      critical: Promise.resolve(null as unknown as RecommendationBatch),
      all: Promise.resolve(),
    };
    const initializedPreloads = (async () => {
      const payload = await api.create(requestId, controller.signal);
      if (!isTaskActive(task) || controller.signal.aborted) throw abortError();
      task.createSucceeded = true;
      if (source === "foreground" && createRequestId === requestId) createRequestId = "";
      const batch = normalizeRecommendationBatch(payload);
      task.batch = batch;
      const preloads = batch.items.map((item) => preloadImage(
        item.thumbnailUrl,
        controller.signal,
      ));
      const outcomes = preloads.map((preload) => preload.then(
        () => null,
        (caught: unknown) => caught,
      ));
      return { batch, preloads, outcomes };
    })();
    task.critical = initializedPreloads.then(async ({ batch, preloads }) => {
      const criticalCount = Math.min(CRITICAL_PRELOAD_COUNT, preloads.length);
      await Promise.all(preloads.slice(0, criticalCount));
      if (!isTaskActive(task) || controller.signal.aborted) throw abortError();
      task.criticalReady = true;
      return batch;
    });
    task.all = initializedPreloads.then(async ({ outcomes }) => {
      const preloadOutcomes = await Promise.all(outcomes);
      task.preloadErrors = preloadOutcomes.filter((caught) => caught !== null);
      task.allSettled = true;
      handleTaskSettled(task);
    }).catch((caught: unknown) => {
      task.allSettled = true;
      if (!isTaskActive(task)) return;
      if (prefetchTask === task) {
        if (!isAbortError(caught) && !task.createSucceeded && !createRequestId) {
          createRequestId = task.requestId;
        }
        discardTask(task);
      }
    });
    void task.critical.catch((caught: unknown) => {
      if (!isTaskActive(task) || prefetchTask !== task) return;
      if (!isAbortError(caught) && !task.createSucceeded && !createRequestId) {
        createRequestId = task.requestId;
      }
      discardTask(task);
    });
    return task;
  }

  function maybeStartPrefetch(): void {
    if (!canPrefetch()) return;
    const task = createPreparedTask(idFactory(), "prefetch");
    prefetchTask = task;
  }

  function syncShown({ quiet = false }: { quiet?: boolean } = {}): Promise<boolean> {
    const pending = pendingShown.value;
    if (shownRequest) return shownRequest;
    if (
      !pending ||
      !visible.value ||
      !documentVisible.value ||
      !isCurrentBatch(pending.batchId)
    ) return Promise.resolve(pending === null);
    shownSyncing.value = true;
    shownController?.abort();
    const controller = new AbortController();
    shownController = controller;
    const request = Promise.resolve().then(async () => {
      let succeeded = false;
      try {
        await api.shown(pending.batchId, pending.eventId, controller.signal);
        if (destroyed || controller.signal.aborted) throw abortError();
        if (pendingShown.value?.eventId === pending.eventId) pendingShown.value = null;
        if (isCurrentBatch(pending.batchId)) shownCompletedBatchId = pending.batchId;
        shownError.value = "";
        succeeded = true;
        return true;
      } catch (caught) {
        if (!isAbortError(caught)) {
          shownError.value = errorMessage(caught);
          if (!quiet) reportError("展示记录待同步", caught);
        }
        return false;
      } finally {
        if (shownController === controller) shownController = null;
        if (shownRequest === request) shownRequest = null;
        shownSyncing.value = false;
        if (succeeded) maybeStartPrefetch();
      }
    });
    shownRequest = request;
    return request;
  }

  async function commitPrepared(task: PreparedBatchTask): Promise<boolean> {
    const batch = task.batch;
    if (!batch || !isTaskActive(task)) return false;
    if (!visible.value || !documentVisible.value) {
      stagedTask = task;
      stagedBatch.value = batch;
      if (foregroundTask === task) foregroundTask = null;
      return true;
    }
    if (stagedTask && stagedTask !== task) discardTask(stagedTask);
    stagedBatch.value = null;
    stagedTask = null;
    if (currentTask && currentTask !== task && !currentTask.allSettled) {
      discardTask(currentTask);
    }
    if (foregroundTask === task) foregroundTask = null;
    currentTask = task;
    currentBatch.value = batch;
    shownCompletedBatchId = "";
    preferences.value = Object.fromEntries(
      batch.items
        .filter((item) => item.preference !== null)
        .map((item) => [item.itemId, item.preference as RecommendationPreference]),
    );
    pendingShown.value = batch.items.length
      ? { batchId: batch.batchId, eventId: idFactory() }
      : null;
    shownError.value = "";
    reportTailFailure(task);
    await nextTick();
    if (!visible.value || !documentVisible.value || !isCurrentBatch(batch.batchId)) return true;
    void syncShown();
    return true;
  }

  async function commitStaged(): Promise<boolean> {
    const task = stagedTask;
    if (!task || !visible.value || !documentVisible.value) return false;
    return commitPrepared(task);
  }

  async function refresh(): Promise<boolean> {
    if (loading.value) return false;
    initialized = true;
    loading.value = true;
    let task: PreparedBatchTask | null = null;
    try {
      if (pendingShown.value && !(await syncShown())) return false;
      error.value = "";
      if (stagedTask) discardTask(stagedTask);
      task = prefetchTask;
      if (task) {
        prefetchTask = null;
      } else {
        if (currentTask && !currentTask.allSettled) discardTask(currentTask);
        const requestId = createRequestId || idFactory();
        createRequestId = requestId;
        task = createPreparedTask(requestId, "foreground");
      }
      foregroundTask = task;
      for (;;) {
        try {
          await task.critical;
          if (!isTaskActive(task) || foregroundTask !== task) return false;
          if (createRequestId === task.requestId) createRequestId = "";
          return await commitPrepared(task);
        } catch (caught) {
          const shouldFallback = task.source === "prefetch" && !isAbortError(caught);
          if (!task.createSucceeded && !isAbortError(caught)) createRequestId = task.requestId;
          discardTask(task);
          if (shouldFallback && !destroyed) {
            const requestId = createRequestId || idFactory();
            createRequestId = requestId;
            task = createPreparedTask(requestId, "foreground");
            foregroundTask = task;
            continue;
          }
          if (!isAbortError(caught)) {
            error.value = errorMessage(caught);
            reportError("无法刷新推荐", caught);
          }
          return false;
        }
      }
    } finally {
      if (task && foregroundTask === task) foregroundTask = null;
      loading.value = false;
      maybeStartPrefetch();
    }
  }

  async function recordAction(itemId: string, action: RecommendationAction): Promise<boolean> {
    const batch = currentBatch.value;
    if (!batch || !batch.items.some((item) => item.itemId === itemId)) return false;
    if (isActionPending(itemId)) return false;
    const key = `${batch.batchId}:${itemId}:${action}`;
    const eventId = retryEventIds.get(key) ?? idFactory();
    const previousPreference = preferences.value[itemId] ?? null;
    const controller = new AbortController();
    actionControllers.add(controller);
    pendingActionKeys.value = [...pendingActionKeys.value, key];
    try {
      const response = await api.action(
        batch.batchId,
        eventId,
        itemId,
        action,
        controller.signal,
      );
      if (destroyed || controller.signal.aborted) throw abortError();
      if (action === "like" || action === "dislike") {
        retryEventIds.delete(`${batch.batchId}:${itemId}:like`);
        retryEventIds.delete(`${batch.batchId}:${itemId}:dislike`);
        const preference = response.preference === undefined
          ? action
          : normalizePreference(response.preference);
        if (isCurrentBatch(batch.batchId)) {
          const updated = { ...preferences.value };
          if (preference === null) delete updated[itemId];
          else updated[itemId] = preference;
          preferences.value = updated;
        }
        if (preference !== previousPreference) {
          createRequestId = "";
          discardPrefetch();
          if (foregroundTask) discardTask(foregroundTask);
          if (stagedTask) discardTask(stagedTask);
          maybeStartPrefetch();
        }
      } else {
        retryEventIds.delete(key);
      }
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        retryEventIds.set(key, eventId);
        reportError("无法记录推荐操作", caught);
      }
      return false;
    } finally {
      actionControllers.delete(controller);
      pendingActionKeys.value = pendingActionKeys.value.filter((candidate) => candidate !== key);
    }
  }

  function isActionPending(itemId: string, action?: RecommendationAction): boolean {
    const batchId = currentBatch.value?.batchId;
    if (!batchId) return false;
    const prefix = `${batchId}:${itemId}:`;
    return pendingActionKeys.value.some(
      (key) => key.startsWith(prefix) && (!action || key === `${prefix}${action}`),
    );
  }

  function preferenceFor(itemId: string): RecommendationPreference | "" {
    return preferences.value[itemId] ?? "";
  }

  function setVisible(nextVisible: boolean): void {
    visible.value = nextVisible;
    if (!nextVisible || !documentVisible.value) {
      createRequestId = "";
      discardPrefetch();
      return;
    }
    if (stagedBatch.value) {
      void commitStaged();
      return;
    }
    if (pendingShown.value) void syncShown({ quiet: true });
    if (!initialized) {
      initialized = true;
      void refresh();
      return;
    }
    maybeStartPrefetch();
  }

  function handleDocumentVisibilityChange(): void {
    documentVisible.value = document.visibilityState !== "hidden";
    if (!documentVisible.value || !visible.value) {
      if (!documentVisible.value) createRequestId = "";
      discardPrefetch();
      return;
    }
    if (!initialized) {
      initialized = true;
      void refresh();
      return;
    }
    if (stagedBatch.value) void commitStaged();
    else if (pendingShown.value) void syncShown({ quiet: true });
    else maybeStartPrefetch();
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleDocumentVisibilityChange);
  });

  onBeforeUnmount(() => {
    destroyed = true;
    document.removeEventListener("visibilitychange", handleDocumentVisibilityChange);
    if (foregroundTask) discardTask(foregroundTask);
    if (stagedTask) discardTask(stagedTask);
    if (currentTask) discardTask(currentTask);
    discardPrefetch();
    shownController?.abort();
    actionControllers.forEach((controller) => controller.abort());
    actionControllers.clear();
  });

  return {
    currentBatch,
    loading,
    error,
    shownError,
    visible,
    hasBatch,
    shownPending,
    refresh,
    recordAction,
    isActionPending,
    preferenceFor,
    setVisible,
    retryShown: syncShown,
  };
}
