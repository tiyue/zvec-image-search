import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import type { SearchApi } from "../../api/client";
import type { SearchPageResponse, SearchSubmission } from "../../types/contracts";
import { useSearch } from "../useSearch";

function resultPayload(query: string, sequence: number): SearchPageResponse {
  return {
    id: `search-${sequence}`,
    status: "succeeded",
    query,
    page: 1,
    page_size: 15,
    total_items: 1,
    total_pages: 1,
    source_label: `snapshot-${sequence}`,
    summary: { sequence },
    items: [
      {
        id: `image-${sequence}`,
        name: `${query}-${sequence}.jpg`,
        relative_path: `${query}/${sequence}.jpg`,
        library_name: "测试图库",
        rank: 1,
      },
    ],
  };
}

function fakeApi(): SearchApi {
  return {
    bootstrap: vi.fn(async () => ({
      service: { status: "ready", ready: true },
      libraries: [{ id: "library-1", name: "测试图库", enabled: true }],
    })),
    latest: vi.fn(async () => ({
      id: "latest",
      status: "succeeded",
      page: 1,
      page_size: 15,
      total_items: 0,
      total_pages: 0,
      items: [],
    })),
    submit: vi.fn(async (body: SearchSubmission) => resultPayload(body.text ?? "", 1)),
    page: vi.fn(async (operationId: string) => resultPayload(operationId, 1)),
    cancel: vi.fn(async (operationId: string) => ({
      id: operationId,
      status: "cancelled",
    })),
  };
}

describe("useSearch resilience", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("supports three consecutive tag searches without retaining a busy lock", async () => {
    const api = fakeApi();
    const submit = vi.mocked(api.submit);
    let sequence = 0;
    submit.mockImplementation(async (body) => resultPayload(body.text ?? "", sequence += 1));
    const search = useSearch(api);
    search.mode.value = "tags";

    for (const query of ["原", "神", "原神"]) {
      search.query.value = query;
      await expect(search.submit()).resolves.toBe(true);
      expect(search.searching.value).toBe(false);
    }

    expect(submit).toHaveBeenCalledTimes(3);
    expect(submit.mock.calls.map(([body]) => [body.text, body.mode])).toEqual([
      ["原", "tag"],
      ["神", "tag"],
      ["原神", "tag"],
    ]);
    expect(search.items.value[0]?.id).toBe("image-3");
  });

  it("derives image, combined and tag API modes from the visible inputs", async () => {
    const api = fakeApi();
    const submit = vi.mocked(api.submit);
    const search = useSearch(api);

    search.setQueryImage("query-1", "query.jpg");
    await expect(search.submit()).resolves.toBe(true);
    expect(submit.mock.calls[0]?.[0]).toMatchObject({ mode: "image", query_image_id: "query-1" });

    search.query.value = "红色和服";
    await expect(search.submit()).resolves.toBe(true);
    expect(submit.mock.calls[1]?.[0]).toMatchObject({
      mode: "combined",
      text: "红色和服",
      query_image_id: "query-1",
    });

    search.mode.value = "tags";
    await expect(search.submit()).resolves.toBe(true);
    expect(submit.mock.calls[2]?.[0]).toMatchObject({ mode: "tag", text: "红色和服" });
    expect(submit.mock.calls[2]?.[0]).not.toHaveProperty("query_image_id");
  });

  it("uses the watchdog to recover when the search request never settles", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    let capturedSignal: AbortSignal | undefined;
    vi.mocked(api.submit).mockImplementation(
      async (_body, signal) =>
        await new Promise<SearchPageResponse>(() => {
          capturedSignal = signal;
        }),
    );
    const onError = vi.fn();
    const onDiagnostic = vi.fn();
    const search = useSearch(api, {
      onError,
      onDiagnostic,
      searchTimeoutMs: 40,
    });
    search.query.value = "永久挂起请求";

    const submission = search.submit();
    expect(search.searching.value).toBe(true);
    await vi.advanceTimersByTimeAsync(41);

    await expect(submission).resolves.toBe(false);
    expect(capturedSignal?.aborted).toBe(true);
    expect(search.searching.value).toBe(false);
    expect(search.status.value).toBe("failed");
    expect(search.message.value).toContain("已自动恢复");
    expect(onError).toHaveBeenCalledWith("搜索等待超时", expect.stringContaining("可以直接重试"));
    expect(onDiagnostic).toHaveBeenCalledWith(
      "search_watchdog_timeout",
      expect.objectContaining({ timeout_ms: 40 }),
    );
  });

  it("allows a successful retry after a request failure", async () => {
    const api = fakeApi();
    const submit = vi.mocked(api.submit);
    submit
      .mockRejectedValueOnce(new Error("临时连接失败"))
      .mockResolvedValueOnce(resultPayload("重试成功", 2));
    const onError = vi.fn();
    const search = useSearch(api, { onError });
    search.query.value = "首次失败";

    await expect(search.submit()).resolves.toBe(false);
    expect(search.searching.value).toBe(false);
    expect(search.status.value).toBe("failed");
    expect(onError).toHaveBeenCalledWith("搜索失败", "临时连接失败");

    search.query.value = "重试成功";
    await expect(search.submit()).resolves.toBe(true);
    expect(submit).toHaveBeenCalledTimes(2);
    expect(search.searching.value).toBe(false);
    expect(search.status.value).toBe("succeeded");
    expect(search.items.value[0]?.id).toBe("image-2");
  });

  it("automatically retries bounded queue backpressure without leaving the UI busy", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    const submit = vi.mocked(api.submit);
    submit
      .mockRejectedValueOnce(
        new ApiError("图库任务队列已满", 429, {
          error: { details: { retry_after_seconds: 1 } },
        }),
      )
      .mockRejectedValueOnce(new ApiError("图库任务队列已满", 429))
      .mockResolvedValueOnce(resultPayload("自动重试", 3));
    const onDiagnostic = vi.fn();
    const search = useSearch(api, { onDiagnostic });
    search.query.value = "自动重试";

    const pending = search.submit();
    await vi.advanceTimersByTimeAsync(3_000);

    await expect(pending).resolves.toBe(true);
    expect(submit).toHaveBeenCalledTimes(3);
    expect(search.searching.value).toBe(false);
    expect(search.items.value[0]?.id).toBe("image-3");
    expect(onDiagnostic).toHaveBeenCalledWith(
      "search_queue_backpressure",
      expect.objectContaining({ attempt: 1, delay_ms: 1_000 }),
    );
  });

  it("keeps a late cancelled response from replacing a newer result", async () => {
    const api = fakeApi();
    let resolveOld: ((payload: SearchPageResponse) => void) | undefined;
    const oldRequest = new Promise<SearchPageResponse>((resolve) => {
      resolveOld = resolve;
    });
    const submit = vi.mocked(api.submit);
    submit
      .mockImplementationOnce(async () => await oldRequest)
      .mockResolvedValueOnce(resultPayload("新搜索", 2));
    const search = useSearch(api);
    search.query.value = "旧搜索";

    const cancelledSubmission = search.submit();
    await Promise.resolve();
    await search.cancel();
    await expect(cancelledSubmission).resolves.toBe(false);

    search.query.value = "新搜索";
    await expect(search.submit()).resolves.toBe(true);
    expect(search.items.value[0]?.id).toBe("image-2");

    resolveOld?.(resultPayload("旧搜索", 1));
    await Promise.resolve();
    await Promise.resolve();

    expect(search.title.value).toBe("新搜索");
    expect(search.items.value[0]?.id).toBe("image-2");
    expect(search.status.value).toBe("succeeded");
  });

  it("drops a page response that arrives after the capacity changed", async () => {
    const paged = (page: number): SearchPageResponse => ({
      id: "latest",
      status: "succeeded",
      page,
      page_size: 15,
      total_items: 30,
      total_pages: 2,
      has_previous: page > 1,
      has_next: page < 2,
      items: [{ id: `image-${page}`, name: `图片 ${page}`, rank: page }],
    });
    const api = fakeApi();
    let resolveSlowPage: ((payload: SearchPageResponse) => void) | undefined;
    const slowPage = new Promise<SearchPageResponse>((resolve) => {
      resolveSlowPage = resolve;
    });
    vi.mocked(api.latest)
      .mockImplementationOnce(async () => paged(1))
      .mockImplementationOnce(async () => await slowPage);
    const search = useSearch(api);
    await search.initialize();

    const navigation = search.goToPage(2);
    await search.setPageSize(30);
    resolveSlowPage?.(paged(2));

    await expect(navigation).resolves.toBe(false);
    expect(search.pageSize.value).toBe(30);
    expect(search.page.value).toBe(1);
  });

  it("renders the final page at the newest capacity when it changes mid-search", async () => {
    const api = fakeApi();
    let resolveSubmit: ((payload: SearchPageResponse) => void) | undefined;
    vi.mocked(api.submit).mockImplementation(
      async () =>
        await new Promise<SearchPageResponse>((resolve) => {
          resolveSubmit = resolve;
        }),
    );
    const search = useSearch(api);
    search.query.value = "人物";

    const submission = search.submit();
    await Promise.resolve();
    await search.setPageSize(30);
    resolveSubmit?.(resultPayload("人物", 1));

    await expect(submission).resolves.toBe(true);
    expect(api.submit).toHaveBeenCalledTimes(1);
    expect(api.page).toHaveBeenLastCalledWith(
      "search-1",
      1,
      30,
      expect.any(AbortSignal),
    );
    expect(search.pageSize.value).toBe(30);
  });

  it("rejects a late page response when the capacity round-trips (A→B→A)", async () => {
    const sized = (page: number, size: number, name: string): SearchPageResponse => ({
      id: "latest",
      status: "succeeded",
      page,
      page_size: size,
      total_items: 60,
      total_pages: Math.ceil(60 / size),
      source_label: "snapshot-a",
      summary: { version: "snapshot-a" },
      items: Array.from({ length: size }, (_, index) => ({
        id: `image-${(page - 1) * size + index + 1}`,
        name,
        rank: (page - 1) * size + index + 1,
      })),
    });
    const api = fakeApi();
    let finishOld: ((payload: SearchPageResponse) => void) | undefined;
    let finishNewest: ((payload: SearchPageResponse) => void) | undefined;
    let calls = 0;
    // The mock ignores AbortSignal on purpose: only request ownership may
    // reject the abandoned response.
    vi.mocked(api.latest).mockImplementation(async (page: number, size: number) => {
      calls += 1;
      if (calls === 2) {
        return await new Promise<SearchPageResponse>((resolve) => {
          finishOld = resolve;
        });
      }
      if (calls === 4) {
        return await new Promise<SearchPageResponse>((resolve) => {
          finishNewest = resolve;
        });
      }
      return sized(page, size, "当前结果");
    });
    const search = useSearch(api);
    await search.initialize();

    const old = search.goToPage(2);
    await search.setPageSize(30);
    const newest = search.setPageSize(15);
    finishOld?.(sized(2, 15, "过期结果"));
    await old;

    expect(search.items.value[0]?.name).not.toBe("过期结果");
    expect(search.pageLoading.value).toBe(true);

    finishNewest?.(sized(1, 15, "最新结果"));
    await expect(newest).resolves.toBe(true);
    expect(search.items.value[0]?.name).toBe("最新结果");
    expect(search.pageLoading.value).toBe(false);
  });
});
