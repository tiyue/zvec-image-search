import { computed, ref, shallowRef } from "vue";

import { ApiError, searchApi, type SearchApi } from "../api/client";
import type {
  LibrarySummary,
  SearchMode,
  SearchPageResponse,
  SearchResultItem,
  SearchResultWire,
} from "../types/contracts";

const PAGE_SIZE = 15 as const;
const CACHE_LIMIT = 5;
const LATEST_CACHE_TTL_MS = 30_000;
const DEFAULT_SEARCH_TIMEOUT_MS = 120_000;
const TERMINAL = new Set([
  "succeeded",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
]);

type SearchSource = "latest" | "search";

interface CacheEntry {
  payload: SearchPageResponse;
  cachedAt: number;
  scope: string;
  snapshot: string;
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

function payloadItems(payload: SearchPageResponse): SearchResultItem[] {
  const source = payload.items?.length
    ? payload.items
    : payload.results?.length
      ? payload.results
      : payload.result?.items ?? [];
  return source.slice(0, PAGE_SIZE).map(normalizeItem);
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

export function useSearch(api: SearchApi = searchApi, events: SearchEvents = {}) {
  const query = ref("");
  const mode = ref<SearchMode>("semantic");
  const libraryId = ref("");
  const resultLimit = ref<string | number>("15");
  const queryImageId = ref("");
  const queryImageName = ref("");
  const libraries = ref<LibrarySummary[]>([]);
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
  const message = ref("正在读取本地结果");
  const status = ref("idle");
  const searching = ref(false);
  const pageLoading = ref(false);

  let source: SearchSource = "latest";
  let activeOperationId = "";
  let generation = 0;
  let scope = "";
  let snapshot = "";
  let searchController: AbortController | null = null;
  let searchWatchdogHandle: number | null = null;
  let foregroundController: AbortController | null = null;
  let idleHandle: number | null = null;
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
    const nextScope =
      nextSource === "latest" ? "latest" : `search:${operationId(payload) || activeOperationId}`;
    const nextSnapshot = snapshotOf(payload);
    if ((scope && scope !== nextScope) || (snapshot && snapshot !== nextSnapshot)) {
      cache.clear();
      cancelPrefetch();
    }
    scope = nextScope;
    snapshot = nextSnapshot;
  }

  function store(payload: SearchPageResponse): void {
    const targetPage = Math.max(1, integer(payload.page, 1));
    cache.delete(targetPage);
    cache.set(targetPage, {
      payload,
      cachedAt: Date.now(),
      scope,
      snapshot,
    });
    while (cache.size > CACHE_LIMIT) {
      const oldest = cache.keys().next().value as number | undefined;
      if (oldest === undefined) break;
      cache.delete(oldest);
    }
  }

  function cached(targetPage: number): SearchPageResponse | null {
    const entry = cache.get(targetPage);
    if (!entry || entry.scope !== scope || entry.snapshot !== snapshot) return null;
    if (source === "latest" && Date.now() - entry.cachedAt > LATEST_CACHE_TTL_MS) {
      cache.delete(targetPage);
      return null;
    }
    cache.delete(targetPage);
    cache.set(targetPage, entry);
    return entry.payload;
  }

  function apply(payload: SearchPageResponse, nextSource: SearchSource, fallbackTitle: string): void {
    const nextItems = payloadItems(payload);
    const nextPage = Math.max(1, integer(payload.page, 1));
    const minimumKnown = (nextPage - 1) * PAGE_SIZE + nextItems.length;
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
      integer(payload.total_pages, nextTotal ? Math.ceil(nextTotal / PAGE_SIZE) : 0),
    );
    const nextStatus = statusOf(payload);

    source = nextSource;
    updateContext(payload, nextSource);
    if (nextStatus !== "failed") store(payload);
    items.value = nextItems;
    selectedId.value = nextItems[0]?.id ?? "";
    highResolutionPreview.value = false;
    page.value = nextPage;
    totalItems.value = nextTotal;
    totalPages.value = nextPages;
    hasPrevious.value = payload.has_previous ?? nextPage > 1;
    hasNext.value = payload.has_next ?? nextPage < nextPages;
    duration.value = finite(payload.elapsed_ms, payload.duration_ms);
    title.value = typeof payload.query === "string" ? payload.query : fallbackTitle;
    message.value =
      nextStatus === "failed"
        ? text(payload.error?.message, payload.message, "搜索失败")
        : nextItems.length
          ? `第 ${nextPage} 页，当前显示 ${nextItems.length} 张可信结果`
          : "没有找到可靠匹配";
    status.value = nextStatus;
    scheduleAdjacent();
  }

  function endpointPage(targetPage: number, signal: AbortSignal): Promise<SearchPageResponse> {
    return source === "latest"
      ? api.latest(targetPage, PAGE_SIZE, signal)
      : api.page(activeOperationId, targetPage, PAGE_SIZE, signal);
  }

  async function prefetch(targetPage: number, context: {
    generation: number;
    scope: string;
    snapshot: string;
    totalPages: number;
    source: SearchSource;
    operationId: string;
  }): Promise<void> {
    if (targetPage < 1 || targetPage > context.totalPages || cached(targetPage)) return;
    const controller = new AbortController();
    prefetchControllers.add(controller);
    try {
      const payload =
        context.source === "latest"
          ? await api.latest(targetPage, PAGE_SIZE, controller.signal)
          : await api.page(context.operationId, targetPage, PAGE_SIZE, controller.signal);
      if (
        generation !== context.generation ||
        scope !== context.scope ||
        snapshot !== context.snapshot
      ) return;
      if (context.source === "search" && operationId(payload) !== context.operationId) return;
      if (snapshotOf(payload) !== context.snapshot) {
        cache.clear();
        return;
      }
      store(payload);
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
      source,
      operationId: activeOperationId,
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
    const [bootstrapResult, latestResult] = await Promise.allSettled([
      api.bootstrap(),
      api.latest(1, PAGE_SIZE),
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
      apply(latestResult.value, "latest", "最近结果");
    } else {
      message.value = "暂无最近结果，可以开始新的搜索。";
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
      const payload = await awaitWithAbort(
        api.page(operation, 1, PAGE_SIZE, signal),
        signal,
      );
      if (localGeneration !== generation || operation !== activeOperationId) return;
      const nextStatus = statusOf(payload);
      if (TERMINAL.has(nextStatus)) {
        apply(payload, "search", query.value.trim() || "搜索结果");
        return;
      }
      status.value = nextStatus || "running";
      message.value = text(payload.message, "正在计算相关结果");
    }
    throw new ApiError("搜索等待时间过长，可取消后重新提交。", 408);
  }

  async function submit(): Promise<boolean> {
    const searchText = query.value.trim();
    if (!searchText) {
      events.onError?.("请输入搜索内容", "可以输入人物、作品、动作、神态或标签。");
      return false;
    }
    const topK = positiveInteger(resultLimit.value);
    if (topK === null) {
      events.onError?.("取图数量无效", "请输入大于或等于 1 的整数。");
      return false;
    }
    if (mode.value === "combined" && !queryImageId.value) {
      events.onError?.("请选择查询图片", "图文联合搜索必须同时包含文字和图片。");
      return false;
    }

    generation += 1;
    const localGeneration = generation;
    abortActivity(true);
    const controller = new AbortController();
    searchController = controller;
    searching.value = true;
    status.value = "running";
    message.value = "正在等待本地后端返回结果";
    activeOperationId = "";
    source = "search";
    const startedAt = Date.now();
    armSearchWatchdog(localGeneration, controller);
    emitDiagnostic("search_submit_started", {
      mode: mode.value,
      top_k: topK,
      library_count: libraryId.value ? 1 : libraries.value.filter((item) => item.enabled !== false).length,
      has_query_image: Boolean(queryImageId.value),
      query_length: searchText.length,
    });
    try {
      const body = {
        text: searchText,
        mode: mode.value,
        library_ids: libraryId.value ? [libraryId.value] : [],
        top_k: topK,
        page: 1 as const,
        page_size: PAGE_SIZE,
        ...(mode.value === "combined" ? { query_image_id: queryImageId.value } : {}),
      };
      const payload = await awaitWithAbort(api.submit(body, controller.signal), controller.signal);
      if (localGeneration !== generation) return false;
      activeOperationId = operationId(payload);
      emitDiagnostic("search_submit_accepted", {
        has_operation: Boolean(activeOperationId),
        initial_status: statusOf(payload),
      });
      const nextStatus = statusOf(payload);
      if (TERMINAL.has(nextStatus) || payloadItems(payload).length) {
        apply(payload, "search", searchText);
      } else if (activeOperationId) {
        emitDiagnostic("search_poll_started", { initial_status: nextStatus || "running" });
        await poll(activeOperationId, localGeneration, controller.signal);
      } else {
        throw new ApiError("搜索请求没有返回可跟踪的任务编号。", 502, payload);
      }
      emitDiagnostic("search_completed", {
        status: status.value,
        total_items: totalItems.value,
        elapsed_ms: Math.max(0, Date.now() - startedAt),
      });
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

  async function goToPage(targetPage: number): Promise<boolean> {
    if (
      pageLoading.value ||
      targetPage === page.value ||
      targetPage < 1 ||
      targetPage > totalPages.value
    ) return false;
    const localGeneration = generation;
    const expectedScope = scope;
    pageLoading.value = true;
    try {
      let payload = cached(targetPage);
      if (!payload) {
        foregroundController = new AbortController();
        payload = await endpointPage(targetPage, foregroundController.signal);
      }
      if (localGeneration !== generation || expectedScope !== scope) return false;
      if (source === "search" && operationId(payload) !== activeOperationId) {
        throw new ApiError("搜索结果已经更新，请重新提交搜索。", 409, payload);
      }
      apply(payload, source, title.value);
      return true;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        emitError("无法加载搜索结果", error);
      }
      return false;
    } finally {
      foregroundController = null;
      if (localGeneration === generation) pageLoading.value = false;
    }
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
    if (!items.value.some((item) => item.id === id)) return;
    selectedId.value = id;
    highResolutionPreview.value = loadHighResolution;
  }

  function setQueryImage(id = "", name = ""): void {
    queryImageId.value = id;
    queryImageName.value = name;
  }

  function dispose(): void {
    generation += 1;
    abortActivity(true);
  }

  return {
    query,
    mode,
    libraryId,
    resultLimit,
    queryImageId,
    queryImageName,
    libraries,
    connectionMessage,
    items,
    selectedId,
    selectedItem,
    highResolutionPreview,
    page,
    totalItems,
    totalPages,
    hasPrevious,
    hasNext,
    duration,
    title,
    message,
    status,
    searching,
    pageLoading,
    initialize,
    submit,
    cancel,
    goToPage,
    select,
    setQueryImage,
    dispose,
  };
}
