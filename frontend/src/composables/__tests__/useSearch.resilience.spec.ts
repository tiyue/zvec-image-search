import { afterEach, describe, expect, it, vi } from "vitest";

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
    submit: vi.fn(async (body: SearchSubmission) => resultPayload(body.text, 1)),
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
    submit.mockImplementation(async (body) => resultPayload(body.text, sequence += 1));
    const search = useSearch(api);
    search.mode.value = "tags";

    for (const query of ["原", "神", "原神"]) {
      search.query.value = query;
      await expect(search.submit()).resolves.toBe(true);
      expect(search.searching.value).toBe(false);
    }

    expect(submit).toHaveBeenCalledTimes(3);
    expect(submit.mock.calls.map(([body]) => [body.text, body.mode])).toEqual([
      ["原", "tags"],
      ["神", "tags"],
      ["原神", "tags"],
    ]);
    expect(search.items.value[0]?.id).toBe("image-3");
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
});
