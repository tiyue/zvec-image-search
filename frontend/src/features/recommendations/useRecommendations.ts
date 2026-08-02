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
const THUMBNAIL_TIMEOUT_MS = 15_000;
const BUCKETS = new Set<RecommendationBucket>([
  "quality",
  "recent",
  "low_exposure",
  "random",
]);
const DEFAULT_QUOTA: Record<RecommendationBucket, number> = {
  quality: 5,
  recent: 4,
  low_exposure: 4,
  random: 2,
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
  let createRequestId = "";
  let generationController: AbortController | null = null;
  let shownController: AbortController | null = null;
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

  async function syncShown({ quiet = false }: { quiet?: boolean } = {}): Promise<boolean> {
    const pending = pendingShown.value;
    if (
      !pending ||
      shownSyncing.value ||
      !visible.value ||
      !documentVisible.value ||
      !isCurrentBatch(pending.batchId)
    ) return pending === null;
    shownSyncing.value = true;
    shownController?.abort();
    const controller = new AbortController();
    shownController = controller;
    try {
      await api.shown(pending.batchId, pending.eventId, controller.signal);
      if (pendingShown.value?.eventId === pending.eventId) pendingShown.value = null;
      shownError.value = "";
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        shownError.value = errorMessage(caught);
        if (!quiet) reportError("展示记录待同步", caught);
      }
      return false;
    } finally {
      if (shownController === controller) shownController = null;
      shownSyncing.value = false;
    }
  }

  async function commitStaged(): Promise<boolean> {
    const batch = stagedBatch.value;
    if (!batch || !visible.value || !documentVisible.value) return false;
    stagedBatch.value = null;
    currentBatch.value = batch;
    preferences.value = Object.fromEntries(
      batch.items
        .filter((item) => item.preference !== null)
        .map((item) => [item.itemId, item.preference as RecommendationPreference]),
    );
    pendingShown.value = batch.items.length
      ? { batchId: batch.batchId, eventId: idFactory() }
      : null;
    shownError.value = "";
    await nextTick();
    if (!visible.value || !documentVisible.value || !isCurrentBatch(batch.batchId)) return true;
    await syncShown();
    return true;
  }

  async function refresh(): Promise<boolean> {
    if (loading.value) return false;
    if (pendingShown.value && !(await syncShown())) return false;
    initialized = true;
    const controller = new AbortController();
    generationController = controller;
    const requestId = createRequestId || idFactory();
    createRequestId = requestId;
    loading.value = true;
    error.value = "";
    try {
      const payload = await api.create(requestId, controller.signal);
      createRequestId = "";
      const batch = normalizeRecommendationBatch(payload);
      await Promise.all(
        batch.items.map((item) => preloadImage(item.thumbnailUrl, controller.signal)),
      );
      if (generationController !== controller) return false;
      stagedBatch.value = batch;
      if (visible.value && documentVisible.value) await commitStaged();
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        error.value = errorMessage(caught);
        reportError("无法刷新推荐", caught);
      }
      return false;
    } finally {
      if (generationController === controller) generationController = null;
      loading.value = false;
    }
  }

  async function recordAction(itemId: string, action: RecommendationAction): Promise<boolean> {
    const batch = currentBatch.value;
    if (!batch || !batch.items.some((item) => item.itemId === itemId)) return false;
    if (isActionPending(itemId)) return false;
    const key = `${batch.batchId}:${itemId}:${action}`;
    const eventId = retryEventIds.get(key) ?? idFactory();
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
      if (action === "like" || action === "dislike") {
        retryEventIds.delete(`${batch.batchId}:${itemId}:like`);
        retryEventIds.delete(`${batch.batchId}:${itemId}:dislike`);
        if (isCurrentBatch(batch.batchId)) {
          const preference = response.preference === undefined
            ? action
            : normalizePreference(response.preference);
          const updated = { ...preferences.value };
          if (preference === null) delete updated[itemId];
          else updated[itemId] = preference;
          preferences.value = updated;
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
    if (!nextVisible || !documentVisible.value) return;
    if (stagedBatch.value) {
      void commitStaged();
      return;
    }
    if (pendingShown.value) void syncShown({ quiet: true });
    if (!initialized) {
      initialized = true;
      void refresh();
    }
  }

  function handleDocumentVisibilityChange(): void {
    documentVisible.value = document.visibilityState !== "hidden";
    if (!documentVisible.value || !visible.value) return;
    if (stagedBatch.value) void commitStaged();
    else if (pendingShown.value) void syncShown({ quiet: true });
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleDocumentVisibilityChange);
  });

  onBeforeUnmount(() => {
    document.removeEventListener("visibilitychange", handleDocumentVisibilityChange);
    generationController?.abort();
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
