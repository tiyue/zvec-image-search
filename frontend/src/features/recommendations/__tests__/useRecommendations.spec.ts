import { flushPromises, mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RecommendationApi, RecommendationBatchWire } from "../types";
import { normalizeRecommendationBatch, useRecommendations } from "../useRecommendations";

function wireBatch(batchId: string, count = 15): RecommendationBatchWire {
  return {
    request_id: `request-${batchId}`,
    batch_id: batchId,
    count,
    partial: count < 15,
    partial_reason: count < 15 ? "insufficient_candidates" : null,
    quota_degraded: count < 15,
    history_window: 60,
    quota: { quality: 5, low_exposure: 6, random: 4 },
    diversity: {
      applied: true,
      reason: "",
      missing_vectors: 2,
      vector_space: "clip-test",
    },
    items: Array.from({ length: count }, (_, index) => ({
      item_id: `${batchId}-item-${index + 1}`,
      media_id: `${batchId}-media-${index + 1}`,
      name: `图片 ${index + 1}`,
      width: 1200,
      height: 800,
      tags: ["角色"],
      library_id: "library-1",
      library_name: "人物图库",
      content_type: "image/jpeg",
      size_bytes: 2048,
      bucket: index < 5 ? "quality" : index < 11 ? "low_exposure" : "random",
      thumbnail_url: `api/image/${batchId}-${index + 1}?variant=thumbnail`,
      preview_url: `api/image/${batchId}-${index + 1}?variant=preview`,
    })),
  };
}

function apiMock(first = wireBatch("batch-1")): RecommendationApi {
  return {
    create: vi.fn().mockResolvedValue(first),
    shown: vi.fn().mockResolvedValue({ ok: true }),
    action: vi.fn().mockResolvedValue({ ok: true }),
  };
}

function mountState(
  api: RecommendationApi,
  options: Parameters<typeof useRecommendations>[1] = {},
) {
  let state!: ReturnType<typeof useRecommendations>;
  const wrapper = mount(defineComponent({
    setup() {
      state = useRecommendations(api, options);
      return () => h("div");
    },
  }));
  return { wrapper, state };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

interface PreloadCall extends Deferred<void> {
  url: string;
  signal: AbortSignal;
}

function controlledPreloader() {
  const calls: PreloadCall[] = [];
  const preloadImage = vi.fn((url: string, signal: AbortSignal) => {
    const pending = deferred<void>();
    const call = { ...pending, url, signal };
    calls.push(call);
    const abort = () => pending.reject(new DOMException("aborted", "AbortError"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
    return pending.promise.finally(() => signal.removeEventListener("abort", abort));
  });
  return { calls, preloadImage };
}

function resolveCalls(calls: PreloadCall[]): void {
  calls.forEach((call) => call.resolve());
}

describe("useRecommendations", () => {
  const wrappers: Array<{ unmount: () => void }> = [];

  afterEach(() => {
    wrappers.splice(0).forEach((wrapper) => wrapper.unmount());
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  it("starts the first refresh when an initially hidden document becomes visible", async () => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    const api = apiMock();
    const preloads = controlledPreloader();
    const mounted = mountState(api, { preloadImage: preloads.preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.create).not.toHaveBeenCalled();

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(preloads.calls).toHaveLength(15);

    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("starts all thumbnails but commits after only the first six and reports tail failures once", async () => {
    const api = apiMock();
    const preloads = controlledPreloader();
    const onError = vi.fn();
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: preloads.preloadImage,
      onError,
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(preloads.calls).toHaveLength(15);
    expect(mounted.state.currentBatch.value).toBeNull();
    expect(mounted.state.loading.value).toBe(true);

    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(mounted.state.loading.value).toBe(false);
    expect(api.shown).toHaveBeenCalledTimes(1);
    expect(preloads.calls.slice(6, 15).every((call) => !call.signal.aborted)).toBe(true);

    preloads.calls[6].reject(new Error("tail thumbnail failed"));
    resolveCalls(preloads.calls.slice(7, 15));
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(
      "部分缩略图加载失败",
      "1 张非关键缩略图加载失败。",
    );
  });

  it("clears foreground loading after the critical commit without waiting for shown", async () => {
    const shown = deferred<{ ok: boolean }>();
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"));
    vi.mocked(api.shown).mockImplementationOnce(() => shown.promise);
    const mounted = mountState(api, { preloadImage: vi.fn().mockResolvedValue(undefined) });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(mounted.state.loading.value).toBe(false);
    expect(mounted.state.shownPending.value).toBe(true);
    expect(api.shown).toHaveBeenCalledTimes(1);
    expect(api.create).toHaveBeenCalledTimes(1);

    const refreshing = mounted.state.refresh();
    expect(mounted.state.loading.value).toBe(true);
    expect(api.shown).toHaveBeenCalledTimes(1);

    shown.resolve({ ok: true });
    await expect(refreshing).resolves.toBe(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(3);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-2");
  });

  it("keeps the old batch and skips shown when a critical thumbnail fails", async () => {
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"));
    const preloads = controlledPreloader();
    const onError = vi.fn();
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: preloads.preloadImage,
      onError,
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");

    const refreshing = mounted.state.refresh();
    await flushPromises();
    const secondBatchCalls = preloads.calls.filter((call) => call.url.includes("batch-2"));
    expect(secondBatchCalls).toHaveLength(15);
    secondBatchCalls[2].reject(new Error("critical thumbnail failed"));

    await expect(refreshing).resolves.toBe(false);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith("无法刷新推荐", "critical thumbnail failed");
  });

  it("stages a foreground batch while hidden and commits and reports it only once on return", async () => {
    const api = apiMock();
    const preloads = controlledPreloader();
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: preloads.preloadImage,
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    mounted.state.setVisible(false);
    expect(preloads.calls.slice(0, 6).every((call) => !call.signal.aborted)).toBe(true);
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    expect(mounted.state.currentBatch.value).toBeNull();
    expect(api.shown).not.toHaveBeenCalled();

    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
    mounted.state.setVisible(false);
    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("keeps a fully prepared next batch unshown and consumes it without another create", async () => {
    const api = apiMock();
    const secondShown = deferred<{ ok: boolean }>();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockResolvedValue(wireBatch("batch-3"));
    vi.mocked(api.shown).mockResolvedValueOnce({ ok: true })
      .mockImplementationOnce(() => secondShown.promise)
      .mockResolvedValue({ ok: true });
    const preloadImage = vi.fn().mockResolvedValue(undefined);
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage,
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);
    expect(preloadImage).toHaveBeenCalledTimes(30);
    expect(api.shown).toHaveBeenCalledTimes(1);

    const refreshing = mounted.state.refresh();
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-2");
    expect(api.create).toHaveBeenCalledTimes(2);
    expect(preloadImage).toHaveBeenCalledTimes(30);
    expect(api.shown).toHaveBeenCalledTimes(2);

    secondShown.resolve({ ok: true });
    await refreshing;
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(3);
  });

  it("takes over an in-flight prefetch without duplicating create or preload", async () => {
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"));
    const secondBatchCalls: PreloadCall[] = [];
    const preloadImage = vi.fn((url: string, signal: AbortSignal) => {
      if (url.includes("batch-1")) return Promise.resolve();
      const pending = deferred<void>();
      secondBatchCalls.push({ ...pending, url, signal });
      return pending.promise;
    });
    const mounted = mountState(api, { preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);
    expect(secondBatchCalls).toHaveLength(15);
    expect(api.shown).toHaveBeenCalledTimes(1);

    const refreshing = mounted.state.refresh();
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);
    expect(preloadImage).toHaveBeenCalledTimes(30);
    resolveCalls(secondBatchCalls.slice(0, 6));
    await expect(refreshing).resolves.toBe(true);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-2");
    expect(api.shown).toHaveBeenCalledTimes(2);
    expect(api.create).toHaveBeenCalledTimes(2);
  });

  it("waits for both shown success and tail settlement before prefetching", async () => {
    const api = apiMock();
    vi.mocked(api.shown).mockRejectedValueOnce(new Error("shown failed"))
      .mockResolvedValueOnce({ ok: true });
    const tailCalls: PreloadCall[] = [];
    const preloadImage = vi.fn((url: string, signal: AbortSignal) => {
      const index = Number(new URL(url).pathname.split("-").at(-1));
      if (index <= 6) return Promise.resolve();
      const pending = deferred<void>();
      tailCalls.push({ ...pending, url, signal });
      return pending.promise;
    });
    const mounted = mountState(api, { preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(mounted.state.shownError.value).toBe("shown failed");

    await expect(mounted.state.retryShown()).resolves.toBe(true);
    expect(api.create).toHaveBeenCalledTimes(1);
    resolveCalls(tailCalls);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);
  });

  it("invalidates only when the server-final preference differs, including a replay", async () => {
    const first = wireBatch("batch-1", 1);
    Object.assign(first.items?.[0] ?? {}, { preference: "like" });
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(first)
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockResolvedValue(wireBatch("batch-3"));
    vi.mocked(api.action)
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true, recorded: false, preference: "like" })
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true, recorded: false, preference: "dislike" })
      .mockResolvedValueOnce({ ok: true, preference: "dislike" });
    const mounted = mountState(api, { preloadImage: vi.fn().mockResolvedValue(undefined) });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    const preparedSignal = vi.mocked(api.create).mock.calls[1]?.[1];
    expect(api.create).toHaveBeenCalledTimes(2);

    await mounted.state.recordAction(itemId, "open");
    await mounted.state.recordAction(itemId, "like");
    await mounted.state.recordAction(itemId, "export");
    expect(preparedSignal?.aborted).toBe(false);
    expect(api.create).toHaveBeenCalledTimes(2);

    await mounted.state.recordAction(itemId, "dislike");
    await flushPromises();
    expect(preparedSignal?.aborted).toBe(true);
    expect(api.create).toHaveBeenCalledTimes(3);
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");

    const replacementSignal = vi.mocked(api.create).mock.calls[2]?.[1];
    await mounted.state.recordAction(itemId, "dislike");
    expect(replacementSignal?.aborted).toBe(false);
    expect(api.create).toHaveBeenCalledTimes(3);
  });

  it("prevents an invalidated in-flight prefetch taken by refresh from committing late", async () => {
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockResolvedValue(wireBatch("batch-3"));
    vi.mocked(api.action).mockResolvedValueOnce({ ok: true, preference: "like" });
    const secondBatchCalls: PreloadCall[] = [];
    const preloadImage = vi.fn((url: string, signal: AbortSignal) => {
      if (!url.includes("batch-2")) return Promise.resolve();
      const pending = deferred<void>();
      secondBatchCalls.push({ ...pending, url, signal });
      return pending.promise;
    });
    const mounted = mountState(api, { preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    const refreshing = mounted.state.refresh();
    await flushPromises();
    expect(secondBatchCalls).toHaveLength(15);

    await mounted.state.recordAction(itemId, "like");
    expect(secondBatchCalls.every((call) => call.signal.aborted)).toBe(true);
    resolveCalls(secondBatchCalls);
    await expect(refreshing).resolves.toBe(false);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
    expect(api.create).toHaveBeenCalledTimes(3);
  });

  it("invalidates an ordinary in-flight foreground refresh before it can commit", async () => {
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"));
    vi.mocked(api.action).mockResolvedValueOnce({ ok: true, preference: "like" });
    const preloads = controlledPreloader();
    const mounted = mountState(api, { preloadImage: preloads.preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    const refreshing = mounted.state.refresh();
    await flushPromises();
    const secondBatchCalls = preloads.calls.filter((call) => call.url.includes("batch-2"));
    expect(secondBatchCalls).toHaveLength(15);
    await mounted.state.recordAction(itemId, "like");

    expect(secondBatchCalls.every((call) => call.signal.aborted)).toBe(true);
    await expect(refreshing).resolves.toBe(false);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("drops an ordinary staged batch when preference changes while hidden", async () => {
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockResolvedValue(wireBatch("batch-3"));
    vi.mocked(api.action).mockResolvedValueOnce({ ok: true, preference: "like" });
    const preloads = controlledPreloader();
    const mounted = mountState(api, { preloadImage: preloads.preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    mounted.state.setVisible(false);
    resolveCalls(preloads.calls.slice(6, 15));
    await flushPromises();

    const refreshing = mounted.state.refresh();
    await flushPromises();
    const secondBatchCalls = preloads.calls.filter((call) => call.url.includes("batch-2"));
    resolveCalls(secondBatchCalls.slice(0, 6));
    await expect(refreshing).resolves.toBe(true);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");

    await mounted.state.recordAction(itemId, "like");
    expect(secondBatchCalls.every((call) => call.signal.aborted)).toBe(true);
    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("does not cancel the visible batch tail when preference changes", async () => {
    const api = apiMock();
    vi.mocked(api.action).mockResolvedValueOnce({ ok: true, preference: "like" });
    const preloads = controlledPreloader();
    const mounted = mountState(api, { preloadImage: preloads.preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    const tail = preloads.calls.slice(6, 15);

    await mounted.state.recordAction(itemId, "like");
    expect(tail.every((call) => !call.signal.aborted)).toBe(true);
    resolveCalls(tail);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);
  });

  it("aborts and drops a prepared batch while the document is hidden", async () => {
    const nextCreate = deferred<RecommendationBatchWire>();
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockImplementationOnce(() => nextCreate.promise)
      .mockResolvedValue(wireBatch("batch-3"));
    const preloadImage = vi.fn().mockResolvedValue(undefined);
    const mounted = mountState(api, { preloadImage });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const preparedSignal = vi.mocked(api.create).mock.calls[1]?.[1];
    expect(api.create).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(preparedSignal?.aborted).toBe(true);
    nextCreate.resolve(wireBatch("batch-2"));
    await flushPromises();
    expect(preloadImage).toHaveBeenCalledTimes(15);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(3);
    const resumedSignal = vi.mocked(api.create).mock.calls[2]?.[1];
    mounted.state.setVisible(false);
    expect(resumedSignal?.aborted).toBe(true);
  });

  it("aborts generation on unmount and ignores a late create result", async () => {
    const create = deferred<RecommendationBatchWire>();
    const api = apiMock();
    vi.mocked(api.create).mockImplementationOnce(() => create.promise);
    const preloadImage = vi.fn().mockResolvedValue(undefined);
    const mounted = mountState(api, { preloadImage });

    mounted.state.setVisible(true);
    await flushPromises();
    const signal = vi.mocked(api.create).mock.calls[0]?.[1];
    mounted.wrapper.unmount();
    expect(signal?.aborted).toBe(true);

    create.resolve(wireBatch("late-batch"));
    await flushPromises();
    expect(preloadImage).not.toHaveBeenCalled();
    expect(mounted.state.currentBatch.value).toBeNull();
  });

  it("aborts tail preload, shown, and action writes on unmount", async () => {
    const shown = deferred<{ ok: boolean }>();
    const action = deferred<{ ok: boolean; preference: "like" }>();
    const api = apiMock();
    vi.mocked(api.shown).mockImplementationOnce(() => shown.promise);
    vi.mocked(api.action).mockImplementationOnce(() => action.promise);
    const preloads = controlledPreloader();
    const mounted = mountState(api, { preloadImage: preloads.preloadImage });

    mounted.state.setVisible(true);
    await flushPromises();
    resolveCalls(preloads.calls.slice(0, 6));
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    const recording = mounted.state.recordAction(itemId, "like");
    await flushPromises();
    const shownSignal = vi.mocked(api.shown).mock.calls[0]?.[2];
    const actionSignal = vi.mocked(api.action).mock.calls[0]?.[4];

    mounted.wrapper.unmount();
    expect(preloads.calls.slice(6, 15).every((call) => call.signal.aborted)).toBe(true);
    expect(shownSignal?.aborted).toBe(true);
    expect(actionSignal?.aborted).toBe(true);

    shown.resolve({ ok: true });
    action.resolve({ ok: true, preference: "like" });
    await recording;
  });

  it("falls back from a failed in-flight prefetch with the same request id", async () => {
    const failedPrefetch = deferred<RecommendationBatchWire>();
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockImplementationOnce(() => failedPrefetch.promise)
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockResolvedValue(wireBatch("batch-3"));
    const onError = vi.fn();
    const mounted = mountState(api, {
      preloadImage: vi.fn().mockResolvedValue(undefined),
      onError,
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const refreshing = mounted.state.refresh();
    failedPrefetch.reject(new Error("temporary create failure"));

    await expect(refreshing).resolves.toBe(true);
    await flushPromises();
    const requestIds = vi.mocked(api.create).mock.calls.slice(0, 3).map((call) => call[0]);
    expect(requestIds[2]).toBe(requestIds[1]);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-2");
    expect(onError).not.toHaveBeenCalledWith("无法刷新推荐", expect.anything());
  });

  it("drops a failed prefetch request id across document visibility invalidation", async () => {
    const failedPrefetch = deferred<RecommendationBatchWire>();
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockImplementationOnce(() => failedPrefetch.promise)
      .mockResolvedValueOnce(wireBatch("batch-2"));
    const mounted = mountState(api, { preloadImage: vi.fn().mockResolvedValue(undefined) });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const failedRequestId = vi.mocked(api.create).mock.calls[1]?.[0];
    failedPrefetch.reject(new Error("silent prefetch failure"));
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(2);

    mounted.state.setVisible(false);
    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(3);
    expect(vi.mocked(api.create).mock.calls[2]?.[0]).not.toBe(failedRequestId);
  });

  it("keeps only the latest failed prefetch id after preference invalidation and success", async () => {
    const staleFailure = deferred<RecommendationBatchWire>();
    const latestFailure = deferred<RecommendationBatchWire>();
    const api = apiMock();
    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-1"))
      .mockImplementationOnce(() => staleFailure.promise)
      .mockResolvedValueOnce(wireBatch("batch-2"))
      .mockImplementationOnce(() => latestFailure.promise)
      .mockResolvedValueOnce(wireBatch("batch-3"));
    vi.mocked(api.action).mockResolvedValueOnce({ ok: true, preference: "like" });
    const mounted = mountState(api, { preloadImage: vi.fn().mockResolvedValue(undefined) });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const staleRequestId = vi.mocked(api.create).mock.calls[1]?.[0] ?? "";
    staleFailure.reject(new Error("stale prefetch failure"));
    await flushPromises();

    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    await mounted.state.recordAction(itemId, "like");
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(3);
    expect(vi.mocked(api.create).mock.calls[2]?.[0]).not.toBe(staleRequestId);

    await expect(mounted.state.refresh()).resolves.toBe(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(4);
    const latestRequestId = vi.mocked(api.create).mock.calls[3]?.[0] ?? "";
    latestFailure.reject(new Error("latest prefetch failure"));
    await flushPromises();

    await expect(mounted.state.refresh()).resolves.toBe(true);
    expect(vi.mocked(api.create).mock.calls[4]?.[0]).toBe(latestRequestId);
    expect(latestRequestId).not.toBe(staleRequestId);
  });

  it("reuses event ids when shown or action writes are retried", async () => {
    const api = apiMock();
    vi.mocked(api.shown)
      .mockRejectedValueOnce(new Error("temporary shown failure"))
      .mockResolvedValueOnce({ ok: true });
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("temporary action failure"))
      .mockResolvedValueOnce({ ok: true });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();

    await mounted.state.retryShown();
    const shownIds = vi.mocked(api.shown).mock.calls.map((call) => call[1]);
    expect(shownIds).toHaveLength(2);
    expect(shownIds[1]).toBe(shownIds[0]);

    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);
    const actionIds = vi.mocked(api.action).mock.calls.map((call) => call[1]);
    expect(actionIds).toHaveLength(2);
    expect(actionIds[1]).toBe(actionIds[0]);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("starts a new preference event after the opposite action succeeds", async () => {
    const api = apiMock(wireBatch("preference-retry", 1));
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("like response was lost"))
      .mockResolvedValue({ ok: true });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "dislike")).resolves.toBe(true);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);

    const actionIds = vi.mocked(api.action).mock.calls.map((call) => call[1]);
    expect(actionIds).toHaveLength(3);
    expect(new Set(actionIds)).toHaveProperty("size", 3);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("keeps the server-final preference when an old event id is replayed", async () => {
    const api = apiMock(wireBatch("cross-viewer-replay", 1));
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("like was stored but its response was lost"))
      .mockResolvedValueOnce({
        ok: true,
        recorded: false,
        preference: "dislike",
      });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);

    const actionIds = vi.mocked(api.action).mock.calls.map((call) => call[1]);
    expect(actionIds).toHaveLength(2);
    expect(actionIds[1]).toBe(actionIds[0]);
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");
  });

  it("normalizes partial and vector-diversity status without exposing viewer identity", () => {
    const batch = normalizeRecommendationBatch(wireBatch("partial", 7));

    expect(batch.count).toBe(7);
    expect(batch.partial).toBe(true);
    expect(batch.partialReason).toBe("insufficient_candidates");
    expect(batch.quotaDegraded).toBe(true);
    expect(batch.quota).toEqual({
      quality: 5,
      recent: 0,
      low_exposure: 6,
      random: 4,
    });
    expect(batch.diversity).toEqual({
      applied: true,
      reason: "",
      missingVectors: 2,
      vectorSpace: "clip-test",
    });
    expect(batch).not.toHaveProperty("viewerId");
  });

  it("keeps legacy recent items readable for persisted batch replay", () => {
    const wire = wireBatch("legacy-recent", 1);
    Object.assign(wire.items?.[0] ?? {}, { bucket: "recent" });

    const batch = normalizeRecommendationBatch(wire);

    expect(batch.items[0].bucket).toBe("recent");
  });

  it("normalizes the server-final preference and safe personalization status", () => {
    const wire = wireBatch("personalized", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "dislike" });
    Object.assign(wire, {
      personalization: {
        applied: true,
        effective_count: 14,
        reason: null,
      },
    });

    const batch = normalizeRecommendationBatch(wire);

    expect(batch.items[0]).toMatchObject({ preference: "dislike" });
    expect(batch.personalization).toEqual({
      applied: true,
      effectiveCount: 14,
      reason: "",
    });
  });

  it("restores a shared preference and preserves it when a replacement write fails", async () => {
    const wire = wireBatch("shared", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "dislike" });
    const api = apiMock(wire);
    vi.mocked(api.action).mockRejectedValueOnce(new Error("conflict write failed"));
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");
  });

  it("safely degrades invalid preference and personalization metadata", () => {
    const wire = wireBatch("invalid-personalization", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "favorite" });
    Object.assign(wire, {
      personalization: {
        applied: false,
        effective_count: -4,
        reason: "viewer-secret-value",
      },
    });

    const batch = normalizeRecommendationBatch(wire);

    expect(batch.items[0]?.preference).toBeNull();
    expect(batch.personalization).toEqual({
      applied: false,
      effectiveCount: 0,
      reason: "",
    });
  });

  it("keeps the safe replay reason without inventing personalization state", () => {
    const wire = wireBatch("replayed", 1);
    Object.assign(wire, {
      personalization: {
        applied: false,
        effective_count: 0,
        reason: "replayed",
      },
    });

    expect(normalizeRecommendationBatch(wire).personalization).toEqual({
      applied: false,
      effectiveCount: 0,
      reason: "replayed",
    });
  });

  it("does not submit conflicting preference writes for one item concurrently", async () => {
    const api = apiMock(wireBatch("pending-preference", 1));
    let finish!: () => void;
    vi.mocked(api.action).mockImplementationOnce(() => new Promise((resolve) => {
      finish = () => resolve({ ok: true });
    }));
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    const liking = mounted.state.recordAction(itemId, "like");
    await flushPromises();
    await expect(mounted.state.recordAction(itemId, "dislike")).resolves.toBe(false);
    expect(api.action).toHaveBeenCalledTimes(1);

    finish();
    await expect(liking).resolves.toBe(true);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("formats structured vector-space metadata", () => {
    const wire = wireBatch("structured-space");
    wire.diversity!.vector_space = {
      model: "embedding-v1",
      dimension: 1024,
      metric: "COSINE",
    };

    expect(normalizeRecommendationBatch(wire).diversity.vectorSpace).toBe(
      "embedding-v1 · 1024D · COSINE",
    );
  });
});
