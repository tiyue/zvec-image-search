import { computed, ref, shallowRef } from "vue";

import { ApiError, searchApi, type SearchApi } from "../api/client";
import { clampGalleryPageSize } from "./useGalleryDensity";
import type {
  LibrarySummary,
  SearchHistoryEntry,
  SearchMode,
  SearchRequestMode,
  SearchPageResponse,
  SearchResultItem,
  SearchResultWire,
  SearchSubmission,
  TagMatchMode,
} from "../types/contracts";

const DEFAULT_PAGE_SIZE = 15;
const CACHE_LIMIT = 5;
const LATEST_CACHE_TTL_MS = 30_000;
const DEFAULT_SEARCH_TIMEOUT_MS = 120_000;
const HIGH_RESOLUTION_PREVIEW_DELAY_MS = 200;
const MAX_QUEUE_RETRIES = 3;
const DEFAULT_QUEUE_RETRY_MS = 1_000;
const TERMINAL = new Set([
  "succeeded",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
]);

type SearchSource = "latest" | "search" | "history";

interface CacheEntry {
  payload: SearchPageResponse;
  cachedAt: number;
  scope: string;
  snapshot: string;
  pageSize: number;
}

interface SearchEvents {
  onError?: (title: string, message: string) => void;
  onInfo?: (title: string, message: string) => void;
  onDiagnostic?: (event: string, details: Record<string, unknown>) => void;
  pollIntervalMs?: number;
  searchTimeoutMs?: number;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function abortError(): DOMException {
  return new DOMException("操作已取消。", "AbortError");
}

function queueRetryDelayMs(error: ApiError, attempt: number): number {
  const payload = error.details as {
    error?: { details?: { retry_after_seconds?: unknown } };
  } | null;
  const configured = payload?.error?.details?.retry_after_seconds;
  const seconds = typeof configured === "number" && Number.isFinite(configured)
    ? configured
    : 0;
  if (seconds > 0) return Math.min(5_000, Math.max(250, Math.trunc(seconds * 1_000)));
  return Math.min(5_000, DEFAULT_QUEUE_RETRY_MS * 2 ** attempt);
}

/**
 * Reject when the signal is aborted even if a mocked or embedded API ignores
 * AbortSignal. This prevents a stale WebView promise from keeping the UI busy.
 */
function awaitWithAbort<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void): void => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      callback();
    };
    const onAbort = (): void => finish(() => reject(abortError()));
    signal.addEventListener("abort", onAbort, { once: true });
    void work.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error)),
    );
  });
}

function waitWithAbort(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const timeoutHandle = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(0, delayMs));
    const onAbort = (): void => {
      window.clearTimeout(timeoutHandle);
      signal.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function integer(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function finite(...values: unknown[]): number | null {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(String).map((item) => item.trim()).filter(Boolean)
    : [];
}

function safeImageUrl(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "";
  try {
    const url = new URL(value.trim(), document.baseURI);
    const localHost = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    const supported = url.protocol === "http:" || url.protocol === "https:";
    return supported && (url.origin === window.location.origin || localHost) ? url.href : "";
  } catch {
    return "";
  }
}

function normalizeItem(raw: SearchResultWire, index: number): SearchResultItem {
  return {
    id: text(raw.id, raw.sha256, raw.document_id, `result-${index + 1}`),
    searchSessionId: text(raw.search_session_id),
    libraryId: text(raw.library_id),
    docId: text(raw.doc_id, raw.document_id),
    sha256: text(raw.sha256),
    name: text(raw.name, raw.filename, raw.relative_path, `图片 ${index + 1}`),
    relativePath: text(raw.relative_path, raw.display_path, raw.path),
    libraryName: text(raw.library_name, raw.collection_name, "未知图库"),
    rank: Math.max(1, integer(raw.rank, index + 1)),
    matchState: text(raw.match_state, "possible"),
    rankSource: text(raw.rank_source, raw.match_source, "fused"),
    width: Math.max(0, integer(raw.width)),
    height: Math.max(0, integer(raw.height)),
    tags: strings(raw.tags),
    matchedTags: strings(raw.matched_tags),
    confidence: finite(raw.ranking_confidence, raw.confidence),
    rawScore: finite(raw.raw_score, raw.score),
    normalizedScore: finite(raw.normalized_score),
    sizeBytes: Math.max(0, integer(raw.size_bytes ?? raw.file_size)),
    imageUrl: safeImageUrl(raw.image_url),
    thumbnailUrl: safeImageUrl(raw.thumbnail_url),
  };
}

function payloadItems(
  payload: SearchPageResponse,
  size: number = DEFAULT_PAGE_SIZE,
): SearchResultItem[] {
  const source = payload.items?.length
    ? payload.items
    : payload.results?.length
      ? payload.results
      : payload.result?.items ?? [];
  return source.slice(0, Math.max(1, size)).map(normalizeItem);
}

function operationId(payload: SearchPageResponse | null | undefined): string {
  return text(payload?.id, payload?.search_id, payload?.job_id, payload?.job?.id);
}

function statusOf(payload: SearchPageResponse): string {
  return text(payload.status, payload.job?.status, "succeeded").toLowerCase();
}

function snapshotOf(payload: SearchPageResponse): string {
  let summary = "";
  try {
    summary = payload.summary ? JSON.stringify(payload.summary) : "";
  } catch {
    summary = "";
  }
  return [
    text(payload.source_label),
    String(Math.max(0, integer(payload.total_items))),
    String(Math.max(0, integer(payload.total_pages))),
    summary,
  ].join("|");
}

function positiveInteger(value: unknown): number | null {
  // Vue normalizes v-model on <input type="number"> to a number after the
  // user edits it, while the initial ref is text. Normalize both forms before
  // validation so changing the result limit cannot abort submission.
  const normalized = String(value ?? "").trim();
  if (!/^[1-9]\d*$/u.test(normalized)) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) && Number.isInteger(parsed) ? parsed : null;
}

function normalizeHistory(value: unknown): SearchHistoryEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Partial<SearchHistoryEntry>;
    const id = text(item.id);
    const label = text(item.label);
    if (!id || !label) return [];
    return [{
      id,
      label,
      query_type: text(item.query_type, "unknown"),
      created_at: text(item.created_at),
      total_items: Math.max(0, integer(item.total_items)),
      status: text(item.status, "succeeded"),
    }];
  }).slice(0, 12);
}

function deriveRequestMode(
  uiMode: SearchMode,
  hasText: boolean,
  hasImage: boolean,
): SearchRequestMode {
  if (uiMode === "tags") return "tag";
  if (hasImage && hasText) return "combined";
  if (hasImage) return "image";
  return "text";
}

export function useSearch(
  api: SearchApi = searchApi,
  events: SearchEvents = {},
) {
  const query = ref("");
  const mode = ref<SearchMode>("semantic");
  const tagMode = ref<TagMatchMode>("all");
  const libraryId = ref("");
  const resultLimit = ref<string | number>("15");
  const queryImageId = ref("");
  const queryImageName = ref("");
  const libraries = ref<LibrarySummary[]>([]);
  const history = ref<SearchHistoryEntry[]>([]);
  const connectionMessage = ref("正在连接本地服务");

  const items = shallowRef<SearchResultItem[]>([]);
  const selectedId = ref("");
  const highResolutionPreview = ref(false);
  const page = ref(1);
  const totalItems = ref(0);
  const totalPages = ref(0);
  const hasPrevious = ref(false);
  const hasNext = ref(false);
  const duration = ref<number | null>(null);
  const title = ref("最近结果");
  const resultQueryType = ref("unknown");
  const message = ref("正在读取本地结果");
  const status = ref("idle");
  const searching = ref(false);
  const pageLoading = ref(false);
  const pageSize = ref(DEFAULT_PAGE_SIZE);

  let source: SearchSource = "latest";
  let activeOperationId = "";
  let activeHistoryId = "";
  let generation = 0;
  // Monotonic id of the foreground page request that currently owns the
  // gallery: an older response must never replace or unload a newer one, even
  // when the API ignores the abort signal and the capacity round-trips (A→B→A).
  let pageRequestSequence = 0;
  let scope = "";
  let snapshot = "";
  // Size of the page currently rendered in `items`; the desired `pageSize`
  // only diverges from it while a running search still returns its first page.
  let appliedPageSize = 0;
  // Absolute index of the image that must stay visible across capacity
  // changes: the last explicit selection, or the start of the last page.
  let anchorAbsolute = 0;
  let searchController: AbortController | null = null;
  let searchWatchdogHandle: number | null = null;
  let foregroundController: AbortController | null = null;
  let idleHandle: number | null = null;
  let highResolutionPreviewHandle: number | null = null;
  let previewEnabled = true;
  const prefetchControllers = new Set<AbortController>();
  const cache = new Map<number, CacheEntry>();

  const selectedItem = computed(
    () => items.value.find((item) => item.id === selectedId.value) ?? null,
  );

  function emitError(errorTitle: string, error: unknown): void {
    const errorMessage = error instanceof Error ? error.message : String(error);
    events.onError?.(errorTitle, errorMessage);
  }

  function emitDiagnostic(event: string, details: Record<string, unknown> = {}): void {
    try {
      events.onDiagnostic?.(event, details);
    } catch {
      // Diagnostics must never break searching or leave the busy state stuck.
    }
  }

  function clearSearchWatchdog(): void {
    if (searchWatchdogHandle === null) return;
    window.clearTimeout(searchWatchdogHandle);
    searchWatchdogHandle = null;
  }

  function clearHighResolutionPreview(): void {
    if (highResolutionPreviewHandle !== null) {
      window.clearTimeout(highResolutionPreviewHandle);
      highResolutionPreviewHandle = null;
    }
    highResolutionPreview.value = false;
  }

  function scheduleHighResolutionPreview(id: string): void {
    clearHighResolutionPreview();
    if (!previewEnabled || !id) return;
    highResolutionPreviewHandle = window.setTimeout(() => {
      highResolutionPreviewHandle = null;
      if (previewEnabled && selectedId.value === id) {
        highResolutionPreview.value = true;
      }
    }, HIGH_RESOLUTION_PREVIEW_DELAY_MS);
  }

  function armSearchWatchdog(localGeneration: number, controller: AbortController): void {
    clearSearchWatchdog();
    const configuredTimeout = events.searchTimeoutMs ?? DEFAULT_SEARCH_TIMEOUT_MS;
    const timeoutMs = Number.isFinite(configuredTimeout)
      ? Math.max(1, Math.trunc(configuredTimeout))
      : DEFAULT_SEARCH_TIMEOUT_MS;
    searchWatchdogHandle = window.setTimeout(() => {
      searchWatchdogHandle = null;
      if (
        localGeneration !== generation ||
        searchController !== controller ||
        !searching.value
      ) return;

      const operation = activeOperationId;
      generation += 1;
      controller.abort();
      searchController = null;
      cancelPrefetch();
      foregroundController?.abort();
      foregroundController = null;
      pageLoading.value = false;
      activeOperationId = "";
      searching.value = false;
      status.value = "failed";
      message.value = `搜索超过 ${Math.ceil(timeoutMs / 1000)} 秒，已自动恢复，可以直接重试。`;
      emitDiagnostic("search_watchdog_timeout", {
        timeout_ms: timeoutMs,
        had_operation: Boolean(operation),
      });
      events.onError?.("搜索等待超时", message.value);

      // Best-effort cleanup. The local UI has already recovered even if this
      // cancellation request also fails or the backend operation has ended.
      if (operation) void api.cancel(operation).catch(() => undefined);
    }, timeoutMs);
  }

  function cancelPrefetch(): void {
    if (idleHandle !== null) {
      const idleWindow = window as Window & {
        cancelIdleCallback?: (handle: number) => void;
      };
      if (idleWindow.cancelIdleCallback) idleWindow.cancelIdleCallback(idleHandle);
      else window.clearTimeout(idleHandle);
      idleHandle = null;
    }
    prefetchControllers.forEach((controller) => controller.abort());
    prefetchControllers.clear();
  }

  function abortActivity(clearCache = false): void {
    searchController?.abort();
    searchController = null;
    clearSearchWatchdog();
    foregroundController?.abort();
    foregroundController = null;
    cancelPrefetch();
    pageLoading.value = false;
    if (clearCache) {
      cache.clear();
      scope = "";
      snapshot = "";
    }
  }

  function updateContext(payload: SearchPageResponse, nextSource: SearchSource): void {
    const nextScope = nextSource === "latest"
      ? "latest"
      : nextSource === "history"
        ? `history:${text(payload.history_id, activeHistoryId)}`
        : `search:${operationId(payload) || activeOperationId}`;
    const nextSnapshot = snapshotOf(payload);
    if ((scope && scope !== nextScope) || (snapshot && snapshot !== nextSnapshot)) {
      cache.clear();
      cancelPrefetch();
    }
    scope = nextScope;
    snapshot = nextSnapshot;
  }

  function store(payload: SearchPageResponse, size: number): void {
    const targetPage = Math.max(1, integer(payload.page, 1));
    cache.delete(targetPage);
    cache.set(targetPage, {
      payload,
      cachedAt: Date.now(),
      scope,
      snapshot,
      pageSize: size,
    });
    while (cache.size > CACHE_LIMIT) {
      const oldest = cache.keys().next().value as number | undefined;
      if (oldest === undefined) break;
      cache.delete(oldest);
    }
  }

  function cached(targetPage: number, size: number): SearchPageResponse | null {
    const entry = cache.get(targetPage);
    if (
      !entry ||
      entry.pageSize !== size ||
      entry.scope !== scope ||
      entry.snapshot !== snapshot
    ) return null;
    if (source === "latest" && Date.now() - entry.cachedAt > LATEST_CACHE_TTL_MS) {
      cache.delete(targetPage);
      return null;
    }
    cache.delete(targetPage);
    cache.set(targetPage, entry);
    return entry.payload;
  }

  function apply(
    payload: SearchPageResponse,
    nextSource: SearchSource,
    fallbackTitle: string,
    size: number = pageSize.value,
    preserveSelection = false,
  ): void {
    const nextItems = payloadItems(payload, size);
    const nextPage = Math.max(1, integer(payload.page, 1));
    const minimumKnown = (nextPage - 1) * size + nextItems.length;
    const explicitTotal = finite(
      payload.total_items,
      payload.total_count,
      payload.candidate_count,
      payload.result?.total_items,
      payload.result?.total_count,
      payload.result?.candidate_count,
    );
    const nextTotal = Math.max(minimumKnown, Math.trunc(explicitTotal ?? minimumKnown));
    const nextPages = Math.max(
      0,
      integer(payload.total_pages, nextTotal ? Math.ceil(nextTotal / size) : 0),
    );
    const nextStatus = statusOf(payload);

    source = nextSource;
    updateContext(payload, nextSource);
    if (nextStatus !== "failed") store(payload, size);
    items.value = nextItems;
    // A capacity reload keeps the primary selection when it is still on the
    // re-read page; a new search or an ordinary page turn starts at the top.
    const previousSelectedId = selectedId.value;
    selectedId.value =
      preserveSelection && nextItems.some((item) => item.id === previousSelectedId)
        ? previousSelectedId
        : nextItems[0]?.id ?? "";
    clearHighResolutionPreview();
    page.value = nextPage;
    appliedPageSize = size;
    totalItems.value = nextTotal;
    totalPages.value = nextPages;
    hasPrevious.value = payload.has_previous ?? nextPage > 1;
    hasNext.value = payload.has_next ?? nextPage < nextPages;
    duration.value = finite(payload.elapsed_ms, payload.duration_ms);
    title.value = typeof payload.query === "string" ? payload.query : fallbackTitle;
    resultQueryType.value = text(payload.query_type, "unknown");
    message.value =
      nextStatus === "failed"
        ? text(payload.error?.message, payload.message, "搜索失败")
        : nextItems.length
          ? `第 ${nextPage} 页，当前显示 ${nextItems.length} 张可信结果`
          : "没有找到可靠匹配";
    status.value = nextStatus;
    scheduleAdjacent();
  }

  function canLoadPages(): boolean {
    if (source === "latest") return true;
    if (source === "history") return Boolean(api.historyPage && activeHistoryId);
    return Boolean(activeOperationId);
  }

  function endpointPage(
    targetPage: number,
    signal: AbortSignal,
    size: number,
  ): Promise<SearchPageResponse> {
    if (source === "latest") return api.latest(targetPage, size, signal);
    if (source === "history") {
      if (!api.historyPage || !activeHistoryId) {
        return Promise.reject(new ApiError("搜索历史暂不可用。", 503));
      }
      return api.historyPage(activeHistoryId, targetPage, size, signal);
    }
    return api.page(activeOperationId, targetPage, size, signal);
  }

  async function prefetch(targetPage: number, context: {
    generation: number;
    scope: string;
    snapshot: string;
    totalPages: number;
    pageSize: number;
    source: SearchSource;
    operationId: string;
    historyId: string;
  }): Promise<void> {
    if (targetPage < 1 || targetPage > context.totalPages) return;
    if (context.pageSize !== pageSize.value || cached(targetPage, context.pageSize)) return;
    const controller = new AbortController();
    prefetchControllers.add(controller);
    try {
      let payload: SearchPageResponse;
      if (context.source === "latest") {
        payload = await api.latest(targetPage, context.pageSize, controller.signal);
      } else if (context.source === "history") {
        if (!api.historyPage) return;
        payload = await api.historyPage(
          context.historyId,
          targetPage,
          context.pageSize,
          controller.signal,
        );
      } else {
        payload = await api.page(
          context.operationId,
          targetPage,
          context.pageSize,
          controller.signal,
        );
      }
      if (
        controller.signal.aborted ||
        generation !== context.generation ||
        scope !== context.scope ||
        snapshot !== context.snapshot ||
        pageSize.value !== context.pageSize
      ) return;
      if (context.source === "search" && operationId(payload) !== context.operationId) return;
      if (snapshotOf(payload) !== context.snapshot) {
        cache.clear();
        return;
      }
      store(payload, context.pageSize);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        // Prefetch is opportunistic; foreground navigation reports failures.
      }
    } finally {
      prefetchControllers.delete(controller);
    }
  }

  function scheduleAdjacent(): void {
    cancelPrefetch();
    if (totalPages.value < 2 || !scope || !snapshot) return;
    const context = {
      generation,
      scope,
      snapshot,
      totalPages: totalPages.value,
      pageSize: pageSize.value,
      source,
      operationId: activeOperationId,
      historyId: activeHistoryId,
    };
    const callback = () => {
      idleHandle = null;
      void prefetch(page.value - 1, context);
      void prefetch(page.value + 1, context);
    };
    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: () => void, options: { timeout: number }) => number;
    };
    idleHandle = idleWindow.requestIdleCallback
      ? idleWindow.requestIdleCallback(callback, { timeout: 800 })
      : window.setTimeout(callback, 180);
  }

  async function initialize(): Promise<void> {
    const localGeneration = generation;
    const initialSize = pageSize.value;
    const [bootstrapResult, latestResult, historyResult] = await Promise.allSettled([
      api.bootstrap(),
      api.latest(1, initialSize),
      api.history ? api.history() : Promise.resolve({ items: [] }),
    ]);
    if (localGeneration !== generation) return;
    if (bootstrapResult.status === "fulfilled") {
      libraries.value = bootstrapResult.value.libraries ?? [];
      const service = bootstrapResult.value.service;
      const ready = Boolean(service?.ready || service?.backend_ready || service?.status === "ready");
      connectionMessage.value = ready ? "本地服务已就绪" : "本地服务需要处理";
    } else {
      connectionMessage.value = "本地服务不可用";
      emitError("无法连接本地服务", bootstrapResult.reason);
    }
    if (latestResult.status === "fulfilled") {
      apply(latestResult.value, "latest", "最近结果", initialSize);
      // The capacity may have changed while the first read was in flight; the
      // latest results must still be shown at the newest capacity.
      await reconcileCapacity();
    } else {
      message.value = "暂无最近结果，可以开始新的搜索。";
    }
    if (historyResult.status === "fulfilled") {
      history.value = normalizeHistory(historyResult.value.items);
    }
  }

  async function refreshHistory(): Promise<void> {
    if (!api.history) return;
    try {
      history.value = normalizeHistory((await api.history()).items);
    } catch (error) {
      emitDiagnostic("search_history_refresh_failed", {
        error_name: error instanceof Error ? error.name : "UnknownError",
      });
    }
  }

  async function openHistory(historyId: string, fallbackTitle = "搜索记录"): Promise<boolean> {
    if (!api.historyPage) {
      events.onError?.("无法打开搜索记录", "当前桌面服务不支持恢复历史结果。请更新后重试。");
      return false;
    }
    generation += 1;
    const localGeneration = generation;
    abortActivity(true);
    activeOperationId = "";
    activeHistoryId = historyId;
    source = "history";
    // A history view always opens on its first page, and the superseding
    // capacity reload must be able to render it with the right title.
    anchorAbsolute = 0;
    title.value = fallbackTitle;
    pageLoading.value = true;
    status.value = "running";
    message.value = "正在恢复历史搜索结果";
    const size = pageSize.value;
    const controller = new AbortController();
    foregroundController = controller;
    const requestId = ++pageRequestSequence;
    try {
      const payload = await api.historyPage(historyId, 1, size, controller.signal);
      if (localGeneration !== generation || activeHistoryId !== historyId) return false;
      // A newer foreground request owns the gallery now; it renders this same
      // history view at the newest capacity.
      if (requestId !== pageRequestSequence) return true;
      apply(payload, "history", fallbackTitle, size);
      await reconcileCapacity();
      return true;
    } catch (error) {
      if (isAbortError(error)) {
        // A capacity reload superseded this first read and renders the same
        // history results itself, so the history view stays available.
        return localGeneration === generation && requestId !== pageRequestSequence;
      }
      status.value = "failed";
      message.value = error instanceof Error ? error.message : String(error);
      emitError("无法打开搜索记录", error);
      return false;
    } finally {
      if (foregroundController === controller) foregroundController = null;
      if (requestId === pageRequestSequence) pageLoading.value = false;
    }
  }

  async function poll(
    operation: string,
    localGeneration: number,
    signal: AbortSignal,
  ): Promise<void> {
    const interval = events.pollIntervalMs ?? 700;
    for (let attempt = 0; attempt < 600; attempt += 1) {
      await waitWithAbort(interval, signal);
      if (localGeneration !== generation || operation !== activeOperationId) return;
      const size = pageSize.value;
      const payload = await awaitWithAbort(
        api.page(operation, 1, size, signal),
        signal,
      );
      if (localGeneration !== generation || operation !== activeOperationId) return;
      const nextStatus = statusOf(payload);
      // Discard a page fetched at a capacity the user has already left behind.
      if (size !== pageSize.value) continue;
      if (TERMINAL.has(nextStatus)) {
        apply(
          payload,
          "search",
          query.value.trim() || queryImageName.value || "搜索结果",
          size,
        );
        return;
      }
      status.value = nextStatus || "running";
      message.value = text(payload.message, "正在计算相关结果");
    }
    throw new ApiError("搜索等待时间过长，可取消后重新提交。", 408);
  }

  async function submit(): Promise<boolean> {
    const searchText = query.value.trim();
    const hasText = !!searchText;
    const hasImage = !!queryImageId.value;
    const requestMode = deriveRequestMode(mode.value, hasText, hasImage);

    if (!hasText && !hasImage) {
      events.onError?.("请输入文字或添加图片", "可以输入人物、作品、动作、神态、标签，或选择一张查询图片。");
      return false;
    }
    if (mode.value === "tags" && !hasText) {
      events.onError?.("请输入标签", "标签搜索需要至少输入一个标签。");
      return false;
    }
    const semanticQueries = mode.value === "semantic" && hasText
      ? [...new Set(searchText.split("|").map((value) => value.trim()).filter(Boolean))]
      : [];
    if (mode.value === "semantic" && searchText.includes("|") && !semanticQueries.length) {
      events.onError?.("请输入语义词", "使用 | 分隔时，至少需要一段非空语义词。");
      return false;
    }
    if (semanticQueries.length > 8) {
      events.onError?.("语义词过多", "使用 | 分隔时，最多支持 8 段语义词。");
      return false;
    }
    const topK = positiveInteger(resultLimit.value);
    if (topK === null) {
      events.onError?.("取图数量无效", "请输入大于或等于 1 的整数。");
      return false;
    }

    generation += 1;
    const localGeneration = generation;
    abortActivity(true);
    const size = pageSize.value;
    const controller = new AbortController();
    searchController = controller;
    searching.value = true;
    status.value = "running";
    message.value = "正在等待本地后端返回结果";
    activeOperationId = "";
    activeHistoryId = "";
    source = "search";
    const startedAt = Date.now();
    armSearchWatchdog(localGeneration, controller);
    emitDiagnostic("search_submit_started", {
      mode: requestMode,
      top_k: topK,
      library_count: libraryId.value ? 1 : libraries.value.filter((item) => item.enabled !== false).length,
      has_query_image: hasImage,
      query_length: searchText.length,
    });
    try {
      const body: SearchSubmission = {
        ...(hasText ? { text: searchText } : {}),
        mode: requestMode,
        library_ids: libraryId.value ? [libraryId.value] : [],
        top_k: topK,
        page: 1 as const,
        page_size: size,
      };
      if (requestMode === "tag") {
        body.tag_mode = tagMode.value;
      }
      if (hasImage && requestMode !== "tag") {
        body.query_image_id = queryImageId.value;
      }
      let payload: SearchPageResponse | null = null;
      for (let attempt = 0; attempt <= MAX_QUEUE_RETRIES; attempt += 1) {
        try {
          payload = await awaitWithAbort(
            api.submit(body, controller.signal),
            controller.signal,
          );
          break;
        } catch (error) {
          if (
            !(error instanceof ApiError) ||
            error.status !== 429 ||
            attempt >= MAX_QUEUE_RETRIES
          ) throw error;
          const delayMs = queueRetryDelayMs(error, attempt);
          status.value = "queued";
          message.value = `图库任务较多，${Math.ceil(delayMs / 1000)} 秒后自动重试搜索`;
          emitDiagnostic("search_queue_backpressure", {
            attempt: attempt + 1,
            delay_ms: delayMs,
          });
          await waitWithAbort(delayMs, controller.signal);
        }
      }
      if (payload === null) {
        throw new ApiError("搜索请求未能进入任务队列。", 503);
      }
      if (localGeneration !== generation) return false;
      activeOperationId = operationId(payload);
      emitDiagnostic("search_submit_accepted", {
        has_operation: Boolean(activeOperationId),
        initial_status: statusOf(payload),
      });
      const nextStatus = statusOf(payload);
      if (TERMINAL.has(nextStatus) || payloadItems(payload).length) {
        apply(payload, "search", searchText || queryImageName.value || "以图搜图", size);
        anchorAbsolute = 0;
      } else if (activeOperationId) {
        emitDiagnostic("search_poll_started", { initial_status: nextStatus || "running" });
        await poll(activeOperationId, localGeneration, controller.signal);
      } else {
        throw new ApiError("搜索请求没有返回可跟踪的任务编号。", 502, payload);
      }
      // The capacity may have changed while the request was in flight; the
      // final page must still be rendered at the newest capacity.
      await reconcileCapacity();
      emitDiagnostic("search_completed", {
        status: status.value,
        total_items: totalItems.value,
        elapsed_ms: Math.max(0, Date.now() - startedAt),
      });
      void refreshHistory();
      return true;
    } catch (error) {
      if (localGeneration === generation) {
        if (isAbortError(error)) {
          status.value = "cancelled";
          message.value = "搜索已停止";
        } else {
          status.value = "failed";
          message.value = error instanceof Error ? error.message : String(error);
          emitError("搜索失败", error);
          emitDiagnostic("search_failed", {
            error_name: error instanceof Error ? error.name : "UnknownError",
            error_message: message.value,
            elapsed_ms: Math.max(0, Date.now() - startedAt),
          });
        }
      }
      return false;
    } finally {
      if (localGeneration === generation) {
        clearSearchWatchdog();
        if (searchController === controller) searchController = null;
        searching.value = false;
      }
    }
  }

  /** Page that keeps the anchored image visible at `size`. */
  function pageForAnchor(size: number): number {
    const targetPage = Math.floor(anchorAbsolute / size) + 1;
    const totalForSize = totalItems.value > 0 ? Math.ceil(totalItems.value / size) : 0;
    return totalForSize > 0 ? Math.min(Math.max(1, targetPage), totalForSize) : Math.max(1, targetPage);
  }

  async function fetchPage(
    targetPage: number,
    options: { force?: boolean; preserveSelection?: boolean } = {},
  ): Promise<boolean> {
    if (targetPage < 1 || !canLoadPages()) return false;
    if (!options.force) {
      if (pageLoading.value || targetPage === page.value) return false;
      if (totalPages.value > 0 && targetPage > totalPages.value) return false;
    }
    const localGeneration = generation;
    const expectedScope = scope;
    const size = pageSize.value;
    const requestId = ++pageRequestSequence;
    let controller: AbortController | null = null;
    pageLoading.value = true;
    try {
      let payload = options.force ? null : cached(targetPage, size);
      if (!payload) {
        foregroundController?.abort();
        controller = new AbortController();
        foregroundController = controller;
        payload = await endpointPage(targetPage, controller.signal, size);
      }
      // A response for a superseded request, source, snapshot or capacity must
      // never replace the page the user is looking at now.
      if (
        requestId !== pageRequestSequence ||
        localGeneration !== generation ||
        expectedScope !== scope ||
        size !== pageSize.value
      ) return false;
      if (source === "search" && operationId(payload) !== activeOperationId) {
        throw new ApiError("搜索结果已经更新，请重新提交搜索。", 409, payload);
      }
      apply(payload, source, title.value, size, options.preserveSelection === true);
      if (!options.force) anchorAbsolute = Math.max(0, (targetPage - 1) * size);
      return true;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        emitError("无法加载搜索结果", error);
      }
      return false;
    } finally {
      if (foregroundController === controller) foregroundController = null;
      // Only the newest request may end the busy state; a stale response must
      // not clear the loading flag of the request that replaced it.
      if (requestId === pageRequestSequence) pageLoading.value = false;
    }
  }

  function goToPage(targetPage: number): Promise<boolean> {
    return fetchPage(targetPage);
  }

  /**
   * Change the display capacity, re-reading only the existing result page.
   * A semantic search is never resubmitted and no embedding runs.
   */
  async function setPageSize(size: number, reload = true): Promise<boolean> {
    const next = clampGalleryPageSize(size);
    if (next === pageSize.value) return false;
    cancelPrefetch();
    const targetPage = pageForAnchor(next);
    pageSize.value = next;
    if (!reload || !appliedPageSize || !canLoadPages() || searching.value) return true;
    return fetchPage(targetPage, { force: true, preserveSelection: true });
  }

  /** Re-read the visible page when its capacity changed while it was loading. */
  async function reconcileCapacity(): Promise<void> {
    if (appliedPageSize === pageSize.value || !appliedPageSize || !canLoadPages()) return;
    const targetPage = pageForAnchor(pageSize.value);
    cancelPrefetch();
    await fetchPage(targetPage, { force: true, preserveSelection: true });
  }

  async function cancel(): Promise<void> {
    const operation = activeOperationId;
    generation += 1;
    abortActivity(true);
    activeOperationId = "";
    searching.value = false;
    status.value = "cancelled";
    message.value = "搜索已停止";
    emitDiagnostic("search_cancelled", { had_operation: Boolean(operation) });
    if (!operation) return;
    try {
      await api.cancel(operation);
    } catch (error) {
      emitError("无法取消搜索", error);
    }
  }

  function select(id: string, loadHighResolution = true): void {
    const index = items.value.findIndex((item) => item.id === id);
    if (index < 0) return;
    selectedId.value = id;
    anchorAbsolute = Math.max(0, (page.value - 1) * (appliedPageSize || pageSize.value) + index);
    if (loadHighResolution) scheduleHighResolutionPreview(id);
    else clearHighResolutionPreview();
  }

  function setPreviewEnabled(enabled: boolean): void {
    previewEnabled = enabled;
    if (!enabled) {
      clearHighResolutionPreview();
      return;
    }
    if (selectedId.value) scheduleHighResolutionPreview(selectedId.value);
  }

  function setQueryImage(id = "", name = ""): void {
    queryImageId.value = id;
    queryImageName.value = name;
  }

  function dispose(): void {
    generation += 1;
    abortActivity(true);
    clearHighResolutionPreview();
  }

  return {
    query,
    mode,
    tagMode,
    libraryId,
    resultLimit,
    queryImageId,
    queryImageName,
    libraries,
    history,
    connectionMessage,
    items,
    selectedId,
    selectedItem,
    highResolutionPreview,
    setPreviewEnabled,
    page,
    totalItems,
    totalPages,
    hasPrevious,
    hasNext,
    duration,
    title,
    resultQueryType,
    message,
    status,
    searching,
    pageLoading,
    pageSize,
    initialize,
    refreshHistory,
    openHistory,
    submit,
    cancel,
    goToPage,
    setPageSize,
    select,
    setQueryImage,
    dispose,
  };
}
