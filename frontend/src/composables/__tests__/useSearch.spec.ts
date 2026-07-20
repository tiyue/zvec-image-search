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
