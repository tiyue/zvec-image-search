import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SearchApi } from "../../api/client";
import type { SearchPageResponse } from "../../types/contracts";
import { useSearch } from "../useSearch";

/**
 * Size-aware page fixture: page boundaries, totals and global ranks all follow
 * the requested capacity. The 15/30 defaults keep the original two-page
 * scenario used by the fixed-capacity tests.
 */
function sizedPagePayload(
  page: number,
  size = 15,
  total = 30,
  source = "snapshot-a",
): SearchPageResponse {
  const totalPages = Math.max(1, Math.ceil(total / size));
  const firstIndex = (page - 1) * size;
  const itemCount = Math.max(0, Math.min(size, total - firstIndex));
  return {
    id: "latest",
    status: "succeeded",
    page,
    page_size: size,
    total_items: total,
    total_pages: totalPages,
    has_previous: page > 1,
    has_next: page < totalPages,
    source_label: source,
    summary: { version: source },
    items: Array.from({ length: itemCount }, (_, index) => {
      const rank = firstIndex + index + 1;
      return {
        id: `image-${rank}`,
        name: `图片 ${rank}`,
        rank,
        ranking_confidence: 0.81,
        confidence: 0.93,
        thumbnail_url: `/image/${rank}?variant=thumbnail`,
        image_url: `/image/${rank}?variant=preview`,
      };
    }),
  };
}

function fakeApi(): SearchApi {
  return {
    bootstrap: vi.fn(async () => ({
      service: { status: "ready", ready: true },
      libraries: [{ id: "library-1", name: "图库" }],
    })),
    latest: vi.fn(async (page, pageSize) => sizedPagePayload(page, pageSize)),
    submit: vi.fn(async (body) => ({
      ...sizedPagePayload(1, body.page_size),
      id: "search-1",
      query: body.text,
    })),
    page: vi.fn(async (_operation, page, pageSize) => ({
      ...sizedPagePayload(page, pageSize),
      id: "search-1",
    })),
    cancel: vi.fn(async () => ({ id: "search-1", status: "cancelled" })),
  };
}

describe("useSearch", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("loads a 15 item page without requesting high resolution preview", async () => {
    const api = fakeApi();
    const search = useSearch(api);

    await search.initialize();

    expect(search.items.value).toHaveLength(15);
    expect(search.selectedId.value).toBe("image-1");
    expect(search.highResolutionPreview.value).toBe(false);
    expect(search.items.value[0]?.confidence).toBe(0.81);
    expect(search.connectionMessage.value).toBe("本地服务已就绪");
  });

  it("lists persisted history and reopens its matching result pages", async () => {
    const api = fakeApi();
    api.history = vi.fn(async () => ({
      items: [{
        id: "history-red-kimono",
        label: "红色和服 室内",
        query_type: "text",
        created_at: "2026-07-22T17:00:00+08:00",
        total_items: 30,
        status: "succeeded",
      }],
    }));
    api.historyPage = vi.fn(async (historyId, page, size) => ({
      ...sizedPagePayload(page, size, 30, historyId),
      id: `history:${historyId}`,
      history_id: historyId,
      query: "红色和服 室内",
      query_type: "text",
    }));
    const search = useSearch(api);

    await search.initialize();
    expect(search.history.value).toHaveLength(1);
    expect(await search.openHistory("history-red-kimono")).toBe(true);
    expect(search.title.value).toBe("红色和服 室内");
    expect(search.items.value[0]?.id).toBe("image-1");

    expect(await search.goToPage(2)).toBe(true);
    expect(search.items.value[0]?.id).toBe("image-16");
    expect(api.historyPage).toHaveBeenLastCalledWith(
      "history-red-kimono",
      2,
      15,
      expect.any(AbortSignal),
    );
  });

  it("delays full previews and discards superseded selections", async () => {
    const search = useSearch(fakeApi());
    await search.initialize();

    search.select("image-2");
    expect(search.highResolutionPreview.value).toBe(false);
    await vi.advanceTimersByTimeAsync(199);
    expect(search.highResolutionPreview.value).toBe(false);

    search.select("image-3");
    await vi.advanceTimersByTimeAsync(200);
    expect(search.selectedId.value).toBe("image-3");
    expect(search.highResolutionPreview.value).toBe(true);

    search.setPreviewEnabled(false);
    expect(search.highResolutionPreview.value).toBe(false);
    search.select("image-4");
    await vi.advanceTimersByTimeAsync(500);
    expect(search.highResolutionPreview.value).toBe(false);
  });

  it("submits any positive top_k with the current gallery capacity", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    search.query.value = "奔跑的人物";
    // Browser number inputs become numbers after user editing, even though the
    // initial value is text.
    search.resultLimit.value = 777;

    expect(await search.submit()).toBe(true);

    expect(api.submit).toHaveBeenCalledWith(
      expect.objectContaining({ top_k: 777, page_size: 15 }),
      expect.any(AbortSignal),
    );

    await search.setPageSize(30);
    expect(await search.submit()).toBe(true);
    expect(api.submit).toHaveBeenLastCalledWith(
      expect.objectContaining({ top_k: 777, page_size: 30 }),
      expect.any(AbortSignal),
    );
  });

  it("uses AND by default for tag search and can submit OR explicitly", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    search.mode.value = "tags";
    search.query.value = "人物 侧脸";

    expect(search.tagMode.value).toBe("all");
    expect(await search.submit()).toBe(true);
    expect(api.submit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        mode: "tag",
        tag_mode: "all",
      }),
      expect.any(AbortSignal),
    );

    search.tagMode.value = "any";
    expect(await search.submit()).toBe(true);
    expect(api.submit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        mode: "tag",
        tag_mode: "any",
      }),
      expect.any(AbortSignal),
    );
  });

  it("rejects more than eight semantic segments before calling the API", async () => {
    const api = fakeApi();
    const onError = vi.fn();
    const search = useSearch(api, { onError });
    search.query.value = Array.from({ length: 9 }, (_, index) => `词${index}`).join("|");

    expect(await search.submit()).toBe(false);
    expect(api.submit).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(
      "语义词过多",
      "使用 | 分隔时，最多支持 8 段语义词。",
    );
  });

  it("prefetches the adjacent page and reuses it without another request", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    await search.initialize();

    await vi.advanceTimersByTimeAsync(200);
    expect(api.latest).toHaveBeenCalledTimes(2);

    expect(await search.goToPage(2)).toBe(true);
    expect(search.page.value).toBe(2);
    expect(search.items.value[0]?.id).toBe("image-16");
    expect(api.latest).toHaveBeenCalledTimes(2);
  });

  it("re-reads the visible page at the new capacity without resubmitting the search", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    search.query.value = "人物";
    expect(await search.submit()).toBe(true);
    expect(api.submit).toHaveBeenCalledTimes(1);

    expect(await search.setPageSize(30)).toBe(true);

    expect(search.pageSize.value).toBe(30);
    expect(api.submit).toHaveBeenCalledTimes(1);
    expect(api.page).toHaveBeenLastCalledWith(
      "search-1",
      1,
      30,
      expect.any(AbortSignal),
    );
  });

  it("anchors the converted page on the first image of the previous page", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    await search.initialize();
    expect(await search.goToPage(2)).toBe(true);

    // Page 2 at 15 per page starts at absolute index 15, which is page 1 at 30.
    expect(await search.setPageSize(30)).toBe(true);
    expect(api.latest).toHaveBeenLastCalledWith(1, 30, expect.any(AbortSignal));
    expect(search.page.value).toBe(1);

    // The anchor image does not drift: index 15 is still page 4 at 5 per page.
    expect(await search.setPageSize(5)).toBe(true);
    expect(api.latest).toHaveBeenLastCalledWith(4, 5, expect.any(AbortSignal));
    expect(search.page.value).toBe(4);
  });

  it("prefers the selected image when converting the page capacity", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    await search.initialize();

    search.select("image-10");
    expect(await search.setPageSize(5)).toBe(true);

    // The tenth image (absolute index 9) is the second image of page 2.
    expect(api.latest).toHaveBeenLastCalledWith(2, 5, expect.any(AbortSignal));
    expect(search.page.value).toBe(2);
  });

  it("does not reuse a prefetched page fetched at another capacity", async () => {
    const api = fakeApi();
    // A fixed 60-item library keeps page 2 real at both 15 and 30 per page.
    api.latest = vi.fn(async (page, pageSize) => sizedPagePayload(page, pageSize, 60));
    const search = useSearch(api);
    await search.initialize();
    await vi.advanceTimersByTimeAsync(200);
    expect(api.latest).toHaveBeenCalledTimes(2);

    await search.setPageSize(30);
    await vi.advanceTimersByTimeAsync(200);
    const callsBeforeNavigation = vi.mocked(api.latest).mock.calls.length;
    expect(api.latest).toHaveBeenLastCalledWith(2, 30, expect.any(AbortSignal));

    expect(await search.goToPage(2)).toBe(true);
    expect(api.latest).toHaveBeenCalledTimes(callsBeforeNavigation);
    expect(search.items.value).toHaveLength(30);
    expect(search.items.value[0]?.id).toBe("image-31");
  });

  it("keeps capacity reloads working for history results", async () => {
    const api = fakeApi();
    api.history = vi.fn(async () => ({
      items: [{
        id: "history-red",
        label: "红色和服",
        query_type: "text",
        created_at: "2026-07-22T17:00:00+08:00",
        total_items: 30,
        status: "succeeded",
      }],
    }));
    api.historyPage = vi.fn(async (historyId, page, size) => ({
      ...sizedPagePayload(page, size, 30),
      id: `history:${historyId}`,
      history_id: historyId,
    }));
    const search = useSearch(api);

    await search.initialize();
    expect(await search.openHistory("history-red")).toBe(true);
    expect(await search.setPageSize(30)).toBe(true);

    expect(api.historyPage).toHaveBeenLastCalledWith(
      "history-red",
      1,
      30,
      expect.any(AbortSignal),
    );
    expect(api.submit).not.toHaveBeenCalled();
  });

  it("keeps the history view when a capacity reload supersedes its first read", async () => {
    const historyPayload = (page: number, size: number, name: string): SearchPageResponse => ({
      id: "history:history-1",
      history_id: "history-1",
      status: "succeeded",
      page,
      page_size: size,
      total_items: 60,
      total_pages: Math.ceil(60 / size),
      source_label: "history",
      summary: { version: "history" },
      items: Array.from({ length: size }, (_, index) => ({
        id: `history-image-${index + 1}`,
        name,
        rank: index + 1,
      })),
    });
    const api = fakeApi();
    let resolveFirstRead: ((payload: SearchPageResponse) => void) | undefined;
    let reads = 0;
    api.historyPage = vi.fn(async (_historyId, page, size) => {
      reads += 1;
      if (reads === 1) {
        return await new Promise<SearchPageResponse>((resolve) => {
          resolveFirstRead = resolve;
        });
      }
      return historyPayload(page, size, "最新容量");
    });
    const search = useSearch(api);
    await search.initialize();

    const opening = search.openHistory("history-1", "红色和服");
    await Promise.resolve();
    const reload = search.setPageSize(30);
    resolveFirstRead?.(historyPayload(1, 15, "过期历史"));

    await expect(opening).resolves.toBe(true);
    await expect(reload).resolves.toBe(true);
    expect(search.pageSize.value).toBe(30);
    expect(search.items.value).toHaveLength(30);
    expect(search.items.value[0]?.name).toBe("最新容量");
    expect(search.title.value).toBe("红色和服");
    expect(search.pageLoading.value).toBe(false);
  });

  it("keeps the selected image when the capacity changes", async () => {
    const api = fakeApi();
    const search = useSearch(api);
    await search.initialize();

    search.select("image-10", false);
    expect(await search.setPageSize(5)).toBe(true);

    // Absolute index 9 is the last image of page 2 at five per page.
    expect(api.latest).toHaveBeenLastCalledWith(2, 5, expect.any(AbortSignal));
    expect(search.items.value.some((item) => item.id === "image-10")).toBe(true);
    expect(search.selectedId.value).toBe("image-10");
  });

  it("shows the latest results at the capacity requested while initialize was running", async () => {
    const sized = (page: number, size: number): SearchPageResponse => ({
      id: "latest",
      status: "succeeded",
      page,
      page_size: size,
      total_items: 60,
      total_pages: Math.ceil(60 / size),
      items: Array.from({ length: size }, (_, index) => ({
        id: `image-${(page - 1) * size + index + 1}`,
        name: `图片 ${(page - 1) * size + index + 1}`,
        rank: (page - 1) * size + index + 1,
      })),
    });
    const api = fakeApi();
    let resolveFirst: ((payload: SearchPageResponse) => void) | undefined;
    let calls = 0;
    api.latest = vi.fn(async (page: number, size: number) => {
      calls += 1;
      if (calls === 1) {
        return await new Promise<SearchPageResponse>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return sized(page, size);
    });
    const search = useSearch(api);

    const initialization = search.initialize();
    await search.setPageSize(30);
    resolveFirst?.(sized(1, 15));
    await initialization;

    expect(search.pageSize.value).toBe(30);
    expect(search.items.value).toHaveLength(30);
    expect(api.latest).toHaveBeenLastCalledWith(1, 30, expect.any(AbortSignal));
  });

  it("drops a prefetched page that finished after the capacity returned", async () => {
    const api = fakeApi();
    let resolveStale: ((payload: SearchPageResponse) => void) | undefined;
    let calls = 0;
    api.latest = vi.fn(async (page: number, pageSize: number) => {
      calls += 1;
      if (calls === 2) {
        return await new Promise<SearchPageResponse>((resolve) => {
          resolveStale = resolve;
        });
      }
      return sizedPagePayload(page, pageSize);
    });
    const search = useSearch(api);
    await search.initialize();
    await vi.advanceTimersByTimeAsync(200);
    expect(calls).toBe(2);

    await search.setPageSize(30);
    await search.setPageSize(15);

    const stale = sizedPagePayload(2, 15);
    resolveStale?.({
      ...stale,
      items: stale.items?.map((item) => ({ ...item, name: "过期结果" })),
    });
    await vi.advanceTimersByTimeAsync(0);

    // The abandoned prefetch must not satisfy the page-2 navigation.
    expect(await search.goToPage(2)).toBe(true);
    expect(calls).toBe(5);
    expect(search.items.value[0]?.name).toBe("图片 16");
  });

  it("rejects invalid result counts before calling the API", async () => {
    const api = fakeApi();
    const onError = vi.fn();
    const search = useSearch(api, { onError });
    search.query.value = "人物";
    search.resultLimit.value = "0";

    expect(await search.submit()).toBe(false);
    expect(api.submit).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(
      "取图数量无效",
      "请输入大于或等于 1 的整数。",
    );
  });

  it("does not apply a page response after cancellation changes generation", async () => {
    const api = fakeApi();
    let resolvePage: ((payload: SearchPageResponse) => void) | undefined;
    api.latest = vi.fn(
      async (page, pageSize) =>
        page === 1
          ? sizedPagePayload(1, pageSize)
          : await new Promise<SearchPageResponse>((resolve) => {
              resolvePage = resolve;
            }),
    );
    const search = useSearch(api);
    await search.initialize();
    const navigation = search.goToPage(2);
    await search.cancel();
    resolvePage?.(sizedPagePayload(2, 15));

    expect(await navigation).toBe(false);
    expect(search.page.value).toBe(1);
  });

  it("cancels an in-flight page read and ignores its late response", async () => {
    const api = fakeApi();
    let resolvePage: ((payload: SearchPageResponse) => void) | undefined;
    api.latest = vi.fn(
      async (page, pageSize) =>
        page === 1
          ? sizedPagePayload(1, pageSize)
          : new Promise<SearchPageResponse>((resolve) => {
              resolvePage = resolve;
            }),
    );
    const search = useSearch(api);
    await search.initialize();

    const navigation = search.goToPage(2);
    expect(search.pageLoading.value).toBe(true);
    search.cancelPendingPageLoad();
    expect(search.pageLoading.value).toBe(false);

    resolvePage?.(sizedPagePayload(2, 15));
    expect(await navigation).toBe(false);
    expect(search.page.value).toBe(1);
    expect(search.items.value[0]?.id).toBe("image-1");
  });

  it("falls back to the applied capacity when a capacity reload is cancelled", async () => {
    const api = fakeApi();
    let resolveCapacity: ((payload: SearchPageResponse) => void) | undefined;
    api.latest = vi.fn(async (page, pageSize) =>
      pageSize === 30
        ? new Promise<SearchPageResponse>((resolve) => {
            resolveCapacity = resolve;
          })
        : sizedPagePayload(page, pageSize),
    );
    const search = useSearch(api);
    await search.initialize();
    search.select("image-10", false);

    const reload = search.setPageSize(30);
    expect(search.pageSize.value).toBe(30);
    expect(search.pageLoading.value).toBe(true);

    // The gallery never showed the new capacity, so it reports the one it shows.
    search.cancelPendingPageLoad();
    expect(search.pageSize.value).toBe(15);
    expect(search.pageLoading.value).toBe(false);

    resolveCapacity?.(sizedPagePayload(1, 30));
    expect(await reload).toBe(false);
    expect(search.pageSize.value).toBe(15);
    expect(search.selectedId.value).toBe("image-10");

    // The next read uses the capacity that is actually displayed.
    expect(await search.goToPage(2)).toBe(true);
    expect(api.latest).toHaveBeenLastCalledWith(2, 15, expect.any(AbortSignal));
  });
});
