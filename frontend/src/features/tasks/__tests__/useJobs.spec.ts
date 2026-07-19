import { defineComponent, h } from "vue";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { JobSubmission, JobsApi, JobsResponse, JobWire } from "../types";
import { useJobs } from "../useJobs";

function fakeApi(overrides: Partial<JobsApi> = {}): JobsApi {
  return {
    list: vi.fn(async () => ({ jobs: [] })),
    submit: vi.fn(async (body) => ({ job: { id: "job-new", status: "queued", ...body } })),
    detail: vi.fn(async (jobId) => ({ job: { id: jobId, status: "running" } })),
    cancel: vi.fn(async (jobId) => ({ job: { id: jobId, status: "cancelled" } })),
    ...overrides,
  };
}

function mountComposable(
  api: JobsApi,
  options: Parameters<typeof useJobs>[1] = { autoStart: false },
): { state: ReturnType<typeof useJobs>; wrapper: VueWrapper } {
  let state: ReturnType<typeof useJobs> | undefined;
  const wrapper = mount(
    defineComponent({
      setup() {
        state = useJobs(api, options);
        return () => h("div");
      },
    }),
  );
  if (!state) throw new Error("useJobs test harness did not initialize");
  return { state, wrapper };
}

describe("useJobs", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each(["index_and_auto_tag", "auto_tag"] as const)(
    "requires explicit external-processing consent for %s",
    async (taskType) => {
      const api = fakeApi();
      const onError = vi.fn();
      const { state, wrapper } = mountComposable(api, { autoStart: false, onError });
      const base: JobSubmission = {
        task_type: taskType,
        library_id: "people",
        max_images: 300,
        ...(taskType === "auto_tag" ? { scope: "untagged" as const } : {}),
      };

      expect(await state.submitJob(base)).toBe(false);
      expect(api.submit).not.toHaveBeenCalled();
      expect(onError).toHaveBeenCalledWith(
        "需要确认外部处理",
        "请确认图片会发送至阿里云大模型后再提交智能标注任务。",
      );

      expect(
        await state.submitJob({ ...base, external_processing_confirmed: true }),
      ).toBe(true);
      expect(api.submit).toHaveBeenCalledWith({
        ...base,
        external_processing_confirmed: true,
      });
      wrapper.unmount();
    },
  );

  it.each(["index", "sync"] as const)(
    "submits %s without pretend per-job tuning or the AI image limit",
    async (taskType) => {
      const api = fakeApi();
      const { state, wrapper } = mountComposable(api);
      const request = {
        task_type: taskType,
        library_id: "cosplay",
        max_images: 20_000,
        concurrency: 6,
        skip_errors: false,
      } as JobSubmission & {
        concurrency: number;
        skip_errors: boolean;
      };

      expect(await state.submitJob(request)).toBe(true);
      expect(api.submit).toHaveBeenCalledWith({
        task_type: taskType,
        library_id: "cosplay",
      });
      wrapper.unmount();
    },
  );

  it("enforces the 10,000 image AI limit and extra all-scope confirmation", async () => {
    const api = fakeApi();
    const onError = vi.fn();
    const { state, wrapper } = mountComposable(api, { autoStart: false, onError });
    const base: JobSubmission = {
      task_type: "auto_tag",
      library_id: "people",
      scope: "all",
      max_images: 10_000,
      external_processing_confirmed: true,
    };

    expect(await state.submitJob(base)).toBe(false);
    expect(onError).toHaveBeenLastCalledWith(
      "需要确认全部重跑",
      "全部图片重新处理可能产生较高费用，请勾选额外确认后再提交。",
    );
    expect(await state.submitJob({ ...base, max_images: 10_001 })).toBe(false);
    expect(onError).toHaveBeenLastCalledWith(
      "图片上限无效",
      "请输入 1–10,000 的整数。",
    );
    expect(await state.submitJob({ ...base, all_scope_confirmed: true })).toBe(true);
    expect(api.submit).toHaveBeenLastCalledWith({
      ...base,
      all_scope_confirmed: true,
    });
    wrapper.unmount();
  });

  it("returns a model-free auto-tag estimate from the backend result", async () => {
    const api = fakeApi({
      submit: vi.fn(async () => ({
        job: {
          id: "estimate-1",
          task_type: "auto_tag_estimate",
          status: "succeeded",
          result: {
            candidate_count: 12,
            unique_image_count: 10,
            cached_count: 4,
            api_request_count: 6,
            estimated_input_tokens: 6000,
            estimated_output_tokens: 1200,
            estimated_cost_cny: 0.42,
            over_budget: false,
          },
        },
      })),
    });
    const onInfo = vi.fn();
    const { state, wrapper } = mountComposable(api, { autoStart: false, onInfo });

    expect(
      await state.estimateAutoTag({
        task_type: "auto_tag_estimate",
        library_id: "people",
        scope: "untagged",
        max_images: 300,
      }),
    ).toBe(true);
    expect(api.submit).toHaveBeenCalledWith(
      {
        task_type: "auto_tag_estimate",
        library_id: "people",
        scope: "untagged",
        max_images: 300,
      },
      expect.any(AbortSignal),
    );
    expect(state.autoTagEstimate.value).toMatchObject({
      candidateCount: 12,
      cachedCount: 4,
      apiRequestCount: 6,
      estimatedCostCny: 0.42,
    });
    expect(onInfo).toHaveBeenCalledWith(
      "估算完成",
      "估算只读取本地状态与缓存，没有调用模型。",
    );
    wrapper.unmount();
  });

  it("cancels one active job without removing other jobs", async () => {
    const jobs: JobWire[] = [
      { id: "job-a", task_type: "index", status: "running" },
      { id: "job-b", task_type: "sync", status: "running" },
    ];
    const api = fakeApi({
      list: vi.fn(async () => ({ jobs })),
      cancel: vi.fn(async () => ({ job: { ...jobs[0], status: "cancelled" } })),
    });
    const { state, wrapper } = mountComposable(api);
    await state.refreshJobs();

    expect(await state.cancelJob("job-a")).toBe(true);
    expect(api.cancel).toHaveBeenCalledWith("job-a");
    expect(state.jobs.value.map((job) => job.id)).toEqual(["job-a", "job-b"]);
    wrapper.unmount();
  });

  it("polls while a job is active and stops after it reaches a terminal state", async () => {
    const active: JobsResponse = {
      jobs: [{ id: "job-a", task_type: "index", status: "running" }],
    };
    const finished: JobsResponse = {
      jobs: [{ id: "job-a", task_type: "index", status: "succeeded" }],
    };
    const api = fakeApi({
      list: vi.fn().mockResolvedValueOnce(active).mockResolvedValue(finished),
    });
    const { state, wrapper } = mountComposable(api, {
      autoStart: true,
      pollIntervalMs: 250,
    });
    await flushPromises();
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(state.hasActiveJobs.value).toBe(true);

    await vi.advanceTimersByTimeAsync(250);
    await flushPromises();
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(state.hasActiveJobs.value).toBe(false);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(api.list).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it("keeps successful work visible when one image fails", async () => {
    const api = fakeApi({
      list: vi.fn(async () => ({
        jobs: [
          {
            id: "job-partial",
            task_type: "index_and_auto_tag",
            status: "partial",
            processed: 99,
            total: 100,
            failure_count: 1,
            error_images: [
              {
                id: "bad-image",
                filename: "broken.jpg",
                reason: "图片损坏",
                thumbnail_url: "api/image/bad-image?variant=thumbnail",
              },
            ],
          },
          { id: "job-ok", task_type: "sync", status: "succeeded", processed: 50, total: 50 },
        ],
      })),
    });
    const { state, wrapper } = mountComposable(api);

    expect(await state.refreshJobs()).toBe(true);
    expect(state.jobs.value).toHaveLength(2);
    expect(state.jobs.value[0]).toMatchObject({
      id: "job-partial",
      status: "partial",
      processed: 99,
      failureCount: 1,
    });
    expect(state.selectedErrors.value[0]).toMatchObject({
      name: "broken.jpg",
      reason: "图片损坏",
    });
    wrapper.unmount();
  });
});
