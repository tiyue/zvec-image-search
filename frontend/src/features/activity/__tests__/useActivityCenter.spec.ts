import { flushPromises, mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ActivityApi,
  ActivityLogItem,
  ActivityLogQuery,
  JobHistoryQuery,
} from "../types";
import {
  normalizeActivityLog,
  normalizeJobHistoryPage,
  serializeActivityLogs,
  useActivityCenter,
} from "../useActivityCenter";

function fakeApi(overrides: Partial<ActivityApi> = {}): ActivityApi {
  return {
    listJobHistory: vi.fn(async () => ({ items: [] })),
    listActivityLogs: vi.fn(async () => ({ items: [] })),
    cancelJob: vi.fn(async () => ({})),
    ...overrides,
  };
}

function mountComposable(
  api: ActivityApi,
  events: Parameters<typeof useActivityCenter>[1] = { autoStart: false },
) {
  let state!: ReturnType<typeof useActivityCenter>;
  const Host = defineComponent({
    setup() {
      state = useActivityCenter(api, events);
      return () => h("div");
    },
  });
  const wrapper = mount(Host);
  return { state, wrapper };
}

function logItem(sequence: number, message = "完成"): ActivityLogItem {
  return {
    sequence,
    timestamp: "2026-07-19T08:00:00Z",
    level: "info",
    category: "search",
    event: "completed",
    source: "gateway",
    libraryId: "people",
    libraryName: "人物图库",
    jobId: "job-1",
    operationId: "op-1",
    message,
    details: {},
    thumbnailUrl: "",
    imageUrl: "",
  };
}

describe("useActivityCenter", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("limits every history page to 50 and keeps active jobs first", () => {
    const payload = {
      items: Array.from({ length: 60 }, (_, index) => ({
        job_id: `job-${index}`,
        status: index === 59 ? "running" : "succeeded",
        updated_at: `2026-07-19T08:${String(index % 60).padStart(2, "0")}:00Z`,
      })),
      total_count: 1_200,
      next_cursor: "page-2",
      has_more: true,
    };

    const page = normalizeJobHistoryPage(payload);

    expect(page.items).toHaveLength(50);
    expect(page.items[0]?.id).toBe("job-59");
    expect(page.totalCount).toBe(1_200);
    expect(page.hasMore).toBe(true);
  });

  it("sanitizes bounded details and rejects non-local image URLs", () => {
    const item = normalizeActivityLog({
      sequence: 7,
      level: "warning",
      message: "图片处理失败",
      thumbnail_url: "https://example.com/private.jpg",
      details: {
        relative_path: "角色/坏图.jpg",
        api_key: "secret-value",
        nested: { authorization: "Bearer secret" },
      },
    });

    expect(item.thumbnailUrl).toBe("");
    expect(item.details).toMatchObject({
      relative_path: "角色/坏图.jpg",
      api_key: "[已隐藏]",
      nested: { authorization: "[已隐藏]" },
    });
  });

  it("moves through opaque cursors and links a selected job to log filters", async () => {
    const listJobHistory = vi.fn(async (query: JobHistoryQuery) => ({
      items: [{ job_id: query.cursor ? "job-older" : "job-new", status: "succeeded" }],
      next_cursor: query.cursor ? "" : "opaque-next",
      has_more: !query.cursor,
      total_count: 2,
    }));
    const listActivityLogs = vi.fn(async (query: ActivityLogQuery) => ({
      items: [{ sequence: 1, job_id: query.jobId, message: "日志" }],
      total_count: 1,
    }));
    const { state, wrapper } = mountComposable(fakeApi({ listJobHistory, listActivityLogs }));

    expect(await state.loadJobs()).toBe(true);
    expect(state.jobs.value[0]?.id).toBe("job-new");
    expect(await state.nextJobs()).toBe(true);
    expect(listJobHistory.mock.calls[1]?.[0].cursor).toBe("opaque-next");
    expect(state.jobPage.value).toBe(2);
    expect(await state.previousJobs()).toBe(true);
    expect(state.jobPage.value).toBe(1);

    expect(await state.selectJob("job-new")).toBe(true);
    expect(state.logFilters.value.jobId).toBe("job-new");
    expect(listActivityLogs.mock.calls.at(-1)?.[0].jobId).toBe("job-new");
    wrapper.unmount();
  });

  it("recovers error-image loading after a request failure", async () => {
    let errorAttempts = 0;
    const listActivityLogs = vi.fn(async (query: ActivityLogQuery) => {
      if (query.category !== "image_failure") return { items: [] };
      errorAttempts += 1;
      if (errorAttempts === 1) throw new Error("临时读取失败");
      return {
        items: [
          {
            sequence: 9,
            category: "image_failure",
            job_id: query.jobId,
            message: "图片损坏",
            details: { relative_path: "坏图.jpg", reason: "无法解码" },
          },
        ],
      };
    });
    const { state, wrapper } = mountComposable(fakeApi({ listActivityLogs }));

    expect(await state.openJobErrors("job-failed")).toBe(false);
    expect(state.jobErrorsLoading.value).toBe(false);
    expect(state.jobErrorsError.value).toBe("临时读取失败");
    expect(await state.retryJobErrors()).toBe(true);
    expect(state.jobErrorsError.value).toBe("");
    expect(state.jobErrorLogs.value[0]?.details.relative_path).toBe("坏图.jpg");
    wrapper.unmount();
  });

  it("restores cancellation controls after backend failure", async () => {
    const api = fakeApi({ cancelJob: vi.fn(async () => Promise.reject(new Error("无法取消"))) });
    const { state, wrapper } = mountComposable(api);

    expect(await state.cancelJob("job-1")).toBe(false);
    expect(state.cancellingJobId.value).toBe("");
    wrapper.unmount();
  });

  it("pauses live log polling without stopping job-history refresh", async () => {
    const listJobHistory = vi.fn(async () => ({ items: [] }));
    const listActivityLogs = vi.fn(async () => ({ items: [] }));
    const { state, wrapper } = mountComposable(
      fakeApi({ listJobHistory, listActivityLogs }),
      { autoStart: false, pollIntervalMs: 1_000 },
    );
    await state.setFollowing(false);
    state.startPolling();

    await vi.advanceTimersByTimeAsync(1_000);
    await flushPromises();

    expect(listJobHistory).toHaveBeenCalledTimes(1);
    expect(listActivityLogs).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("copies selected rows and exports CSV or JSONL safely", async () => {
    const copyText = vi.fn(async () => undefined);
    const downloadText = vi.fn();
    const api = fakeApi({
      listActivityLogs: vi.fn(async () => ({
        items: [
          { sequence: 1, timestamp: "2026-07-19T08:00:00Z", message: "=FORMULA" },
          { sequence: 2, timestamp: "2026-07-19T08:01:00Z", message: "完成" },
        ],
      })),
    });
    const { state, wrapper } = mountComposable(api, {
      autoStart: false,
      copyText,
      downloadText,
    });
    await state.loadLogs();
    state.toggleLogSelection(1);

    expect(await state.copySelectedLogs()).toBe(true);
    expect(copyText).toHaveBeenCalledWith(expect.stringContaining("=FORMULA"));
    expect(await state.exportLogs("csv")).toBe(true);
    expect(downloadText.mock.calls[0]?.[1]).toContain("' =FORMULA".replace(" ", ""));
    expect(await state.exportLogs("jsonl")).toBe(true);
    expect(downloadText.mock.calls[1]?.[1]).toContain('"sequence":1');
    wrapper.unmount();
  });

  it("exports every cursor page for the active filters without rendering them all", async () => {
    const downloadText = vi.fn();
    const listActivityLogs = vi.fn(async (query: ActivityLogQuery) => {
      if (!query.cursor) {
        return {
          items: [
            { sequence: 3, job_id: "job-filtered", message: "第三条" },
            { sequence: 2, job_id: "job-filtered", message: "第二条" },
          ],
          next_cursor: "cursor-2",
          has_more: true,
          total_count: 3,
        };
      }
      return {
        items: [{ sequence: 1, job_id: "job-filtered", message: "第一条" }],
        next_cursor: "",
        has_more: false,
        total_count: 3,
      };
    });
    const { state, wrapper } = mountComposable(fakeApi({ listActivityLogs }), {
      autoStart: false,
      downloadText,
    });
    state.logFilters.value = {
      level: "warning",
      category: "image_failure",
      libraryId: "people",
      jobId: "job-filtered",
      query: "损坏",
    };

    expect(await state.exportLogs("jsonl")).toBe(true);
    expect(listActivityLogs).toHaveBeenCalledTimes(2);
    expect(listActivityLogs.mock.calls[0]?.[0]).toMatchObject({
      level: "warning",
      category: "image_failure",
      libraryId: "people",
      jobId: "job-filtered",
      query: "损坏",
      limit: 200,
    });
    expect(listActivityLogs.mock.calls[1]?.[0].cursor).toBe("cursor-2");
    expect(downloadText.mock.calls[0]?.[1].trim().split("\n")).toHaveLength(3);
    expect(state.logs.value).toHaveLength(0);
    expect(state.logsExporting.value).toBe(false);
    wrapper.unmount();
  });
});

describe("serializeActivityLogs", () => {
  it("produces one valid object per JSONL line", () => {
    const content = serializeActivityLogs([logItem(1), logItem(2)], "jsonl");
    expect(content.split("\n").map((line) => JSON.parse(line))).toHaveLength(2);
  });
});
