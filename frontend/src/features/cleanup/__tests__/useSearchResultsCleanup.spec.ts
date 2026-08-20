import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type CleanupJobResponse,
  type SearchResultsCleanupApi,
  useSearchResultsCleanup,
} from "../useSearchResultsCleanup";

function fakeApi(overrides: Partial<SearchResultsCleanupApi> = {}): SearchResultsCleanupApi {
  return {
    submit: vi.fn(async () => ({ job: { id: "cleanup-1", status: "queued" } })),
    detail: vi.fn(async () => ({ job: { id: "cleanup-1", status: "running" } })),
    ...overrides,
  };
}

function mountComposable(
  api: SearchResultsCleanupApi,
  events: Parameters<typeof useSearchResultsCleanup>[1] = {},
): { state: ReturnType<typeof useSearchResultsCleanup>; wrapper: VueWrapper } {
  let state: ReturnType<typeof useSearchResultsCleanup> | undefined;
  const wrapper = mount(
    defineComponent({
      setup() {
        state = useSearchResultsCleanup(api, events);
        return () => h("div");
      },
    }),
  );
  if (!state) throw new Error("cleanup composable test harness did not initialize");
  return { state, wrapper };
}

describe("useSearchResultsCleanup", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("submits the fixed retention policy and reports completed cleanup counts", async () => {
    const onInfo = vi.fn();
    const api = fakeApi({
      detail: vi.fn(async () => ({
        job: {
          id: "cleanup-1",
          status: "succeeded",
          result: { deleted_count: 8, skipped_count: 2, failed_count: 1 },
        },
      })),
    });
    const { state, wrapper } = mountComposable(api, { onInfo, pollIntervalMs: 50 });

    expect(await state.submit()).toBe(true);
    expect(api.submit).toHaveBeenCalledWith(
      { task_type: "search_results_cleanup", keep_latest: 3 },
      expect.any(AbortSignal),
    );
    expect(state.running.value).toBe(true);

    await vi.advanceTimersByTimeAsync(50);
    await flushPromises();

    expect(api.detail).toHaveBeenCalledWith("cleanup-1", expect.any(AbortSignal));
    expect(state.running.value).toBe(false);
    expect(state.job.value).toMatchObject({
      status: "succeeded",
      progressPercent: 100,
      deleted: 8,
      skipped: 2,
      failed: 1,
    });
    expect(onInfo).toHaveBeenLastCalledWith(
      "搜索结果清理完成",
      "已删除 8 项，跳过 2 项，失败 1 项。",
    );
    wrapper.unmount();
  });

  it("ignores repeated submissions while the first request is still pending", async () => {
    let resolveSubmit: ((value: CleanupJobResponse) => void) | undefined;
    const api = fakeApi({
      submit: vi.fn(
        () =>
          new Promise<CleanupJobResponse>((resolve) => {
            resolveSubmit = resolve;
          }),
      ),
    });
    const { state, wrapper } = mountComposable(api);

    const first = state.submit();
    expect(state.running.value).toBe(true);
    expect(await state.submit()).toBe(false);
    expect(api.submit).toHaveBeenCalledTimes(1);

    resolveSubmit?.({
      job: {
        id: "cleanup-1",
        status: "succeeded",
        result: { deleted: 3, skipped: 0, failed: 0 },
      },
    });
    expect(await first).toBe(true);
    expect(state.running.value).toBe(false);
    wrapper.unmount();
  });

  it("reenables cleanup after a submission failure so the user can retry", async () => {
    const onError = vi.fn();
    const api = fakeApi({
      submit: vi
        .fn()
        .mockRejectedValueOnce(new Error("本地服务暂不可用"))
        .mockResolvedValueOnce({
          job: {
            id: "cleanup-2",
            status: "succeeded",
            result: { deleted: 1, skipped: 0, failed: 0 },
          },
        }),
    });
    const { state, wrapper } = mountComposable(api, { onError });

    expect(await state.submit()).toBe(false);
    expect(state.running.value).toBe(false);
    expect(onError).toHaveBeenCalledWith("无法清理搜索结果", "本地服务暂不可用");

    expect(await state.submit()).toBe(true);
    expect(api.submit).toHaveBeenCalledTimes(2);
    expect(state.job.value?.deleted).toBe(1);
    wrapper.unmount();
  });
});
