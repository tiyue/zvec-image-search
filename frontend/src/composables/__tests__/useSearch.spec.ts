import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SearchApi } from "../../api/client";
import type { SearchPageResponse } from "../../types/contracts";
import { useSearch } from "../useSearch";

function pagePayload(page: number, totalPages = 2, source = "snapshot-a"): SearchPageResponse {
  return {
    id: "latest",
    status: "succeeded",
    page,
    page_size: 15,
    total_items: totalPages * 15,
    total_pages: totalPages,
    has_previous: page > 1,
    has_next: page < totalPages,
    source_label: source,
    summary: { version: source },
    items: Array.from({ length: 15 }, (_, index) => {
      const rank = (page - 1) * 15 + index + 1;
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
    latest: vi.fn(async (page) => pagePayload(page)),
    submit: vi.fn(async (body) => ({
      ...pagePayload(1),
      id: "search-1",
      query: body.text,
    })),
    page: vi.fn(async (_operation, page) => ({
      ...pagePayload(page),
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
    api.historyPage = vi.fn(async (historyId, page) => ({
      ...pagePayload(page, 2, historyId),
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

  it("submits any positive top_k while keeping page_size fixed at 15", async () => {
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
      async (page) =>
        page === 1
          ? pagePayload(1)
          : await new Promise<SearchPageResponse>((resolve) => {
              resolvePage = resolve;
            }),
    );
    const search = useSearch(api);
    await search.initialize();
    const navigation = search.goToPage(2);
    await search.cancel();
    resolvePage?.(pagePayload(2));

    expect(await navigation).toBe(false);
    expect(search.page.value).toBe(1);
  });
});
