import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TasksPage from "../TasksPage.vue";

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn(async () => payload),
  } as unknown as Response;
}

describe("TasksPage", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("blocks smart annotation until the user confirms external processing", async () => {
    const fetchMock = vi.fn(async (_path: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job: { id: "job-new", task_type: "index_and_auto_tag", status: "queued" } }, 202);
      }
      return jsonResponse({ jobs: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(TasksPage, {
      props: { libraries: [{ id: "people", name: "人物图库", enabled: true }] },
    });
    await flushPromises();
    expect(wrapper.find(".task-form").exists()).toBe(false);
    await wrapper.get(".new-task-button").trigger("click");

    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    expect(wrapper.text()).toContain("需要确认外部处理");
    expect(wrapper.emitted("toast")?.[0]).toEqual([
      "需要确认外部处理",
      "请确认图片会发送至阿里云大模型后再提交智能标注任务。",
      "error",
    ]);

    await wrapper.get('input[name="external_processing_confirmed"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeDefined();
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      task_type: "index_and_auto_tag",
      library_id: "people",
      max_images: 300,
      external_processing_confirmed: true,
    });
    wrapper.unmount();
  });

  it.each(["index", "sync"] as const)(
    "submits %s from other tasks without model fields",
    async (taskType) => {
      const fetchMock = vi.fn(async (_path: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          return jsonResponse({ job: { id: "job-policy", status: "queued", ...body } }, 202);
        }
        return jsonResponse({ jobs: [] });
      });
      vi.stubGlobal("fetch", fetchMock);
      const wrapper = mount(TasksPage, {
        props: { libraries: [{ id: "people", name: "人物图库", enabled: true }] },
      });
      await flushPromises();
      await wrapper.get(".other-task-button").trigger("click");
      const label = taskType === "index" ? "建立索引" : "同步图库";
      await wrapper.get(".other-tasks").get("button");
      const choice = wrapper.findAll(".other-tasks button").find((button) => button.text().includes(label));
      expect(choice).toBeDefined();
      await choice?.trigger("click");
      const policy = wrapper.get('[data-testid="task-execution-policy"]');
      expect(policy.text()).toContain("单张失败会跳过");
      expect(policy.text()).not.toContain("scan_concurrency");
      expect(wrapper.find('input[name="concurrency"]').exists()).toBe(false);

      await wrapper.get("form").trigger("submit");
      await flushPromises();
      const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({
        task_type: taskType,
        library_id: "people",
      });
      wrapper.unmount();
    },
  );

  it("exposes independent smart annotation, scopes, risk confirmation and estimate", async () => {
    const fetchMock = vi.fn(async (_path: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        if (body.task_type === "auto_tag_estimate") {
          return jsonResponse({
            job: {
              id: "estimate-1",
              task_type: "auto_tag_estimate",
              status: "succeeded",
              result: {
                candidate_count: 20,
                unique_image_count: 18,
                cached_count: 8,
                api_request_count: 10,
                estimated_input_tokens: 10000,
                estimated_output_tokens: 2000,
                estimated_cost_cny: 0.88,
                over_budget: false,
              },
            },
          }, 202);
        }
        return jsonResponse({ job: { id: "auto-1", task_type: "auto_tag", status: "queued" } }, 202);
      }
      return jsonResponse({ jobs: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(TasksPage, {
      props: { libraries: [{ id: "people", name: "人物图库", enabled: true }] },
    });
    await flushPromises();
    await wrapper.get(".other-task-button").trigger("click");
    const annotation = wrapper.findAll(".other-tasks button").find((button) => button.text().includes("智能标注"));
    await annotation?.trigger("click");
    expect(wrapper.get('select[name="auto_tag_scope"]').text()).toContain(
      "未标注或源图片已变化（推荐）",
    );
    expect(wrapper.text()).toContain("单张失败会跳过，其余图片继续处理。");
    expect(wrapper.find('input[name="concurrency"]').exists()).toBe(false);

    await wrapper.get('button.estimate-button').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("智能标注估算");
    expect(wrapper.text()).toContain("20");
    const estimatePost = fetchMock.mock.calls.find(([, init]) => {
      if (init?.method !== "POST") return false;
      return JSON.parse(String(init.body)).task_type === "auto_tag_estimate";
    });
    expect(JSON.parse(String(estimatePost?.[1]?.body))).toEqual({
      task_type: "auto_tag_estimate",
      library_id: "people",
      scope: "untagged",
      max_images: 300,
    });

    await wrapper.get('select[name="auto_tag_scope"]').setValue("all");
    await wrapper.get('input[name="external_processing_confirmed"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("需要确认全部重跑");

    await wrapper.get('input[name="all_scope_confirmed"]').setValue(true);
    await wrapper.get('input[name="external_processing_confirmed"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const autoPost = fetchMock.mock.calls.find(([, init]) => {
      if (init?.method !== "POST") return false;
      return JSON.parse(String(init.body)).task_type === "auto_tag";
    });
    expect(JSON.parse(String(autoPost?.[1]?.body))).toMatchObject({
      task_type: "auto_tag",
      library_id: "people",
      scope: "all",
      max_images: 300,
      external_processing_confirmed: true,
      all_scope_confirmed: true,
    });
    wrapper.unmount();
  });

  it("shows isolated error images without hiding the rest of the task history", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("api/job-history")) {
        return jsonResponse({
          items: [
            {
              job_id: "job-partial",
              task_type: "index",
              library_name: "写真图库",
              status: "partial",
              processed: 49,
              total: 50,
              failed: 1,
            },
            {
              job_id: "job-success",
              task_type: "sync",
              library_name: "动漫图库",
              status: "succeeded",
              processed: 120,
              total: 120,
            },
          ],
          total_count: 2,
        });
      }
      if (path.startsWith("api/activity-logs")) {
        if (path.includes("category=image_failure")) {
          return jsonResponse({
            items: [
              {
                sequence: 11,
                level: "warning",
                category: "image_failure",
                event: "image_failed",
                job_id: "job-partial",
                message: "文件无法读取",
                details: { relative_path: "bad.jpg", reason: "文件无法读取" },
              },
            ],
            total_count: 1,
          });
        }
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ jobs: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(TasksPage, {
      props: { libraries: [{ id: "photos", name: "写真图库" }] },
    });
    await flushPromises();

    expect(wrapper.find(".task-form").exists()).toBe(false);
    expect(wrapper.find(".task-view-tabs").exists()).toBe(false);
    expect(wrapper.text()).toContain("自动增量已关闭");
    expect(wrapper.findAll(".job-history-table tr.activity-row")).toHaveLength(2);
    expect(wrapper.text()).toContain("同步图库");

    await wrapper.get(".error-count-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("bad.jpg");
    expect(wrapper.text()).toContain("文件无法读取");
    expect(wrapper.text()).toContain("job-partial");
    expect(fetchMock.mock.calls.some(([path]) =>
      String(path).includes("category=image_failure") && String(path).includes("job_id=job-partial"),
    )).toBe(true);

    const logsButton = wrapper.findAll("button").find((button) => button.text().includes("查看操作日志"));
    await logsButton?.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("建立索引 的操作日志");
    expect(wrapper.find(".job-history-table").exists()).toBe(false);
    wrapper.unmount();
  });

  it("cancels only the selected active task", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return jsonResponse({ job: { id: "job-a", task_type: "index", status: "cancelled" } }, 202);
      }
      if (String(input).startsWith("api/job-history")) {
        return jsonResponse({
          items: [
            { job_id: "job-a", task_type: "index", status: "running" },
            { job_id: "job-b", task_type: "sync", status: "running" },
          ],
          total_count: 2,
        });
      }
      return jsonResponse({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(TasksPage, {
      props: { libraries: [{ id: "people", name: "人物图库" }] },
    });
    await flushPromises();

    await wrapper.findAll(".job-actions button")[0]?.trigger("click");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
    await wrapper.get(".activity-button-danger").trigger("click");
    await flushPromises();
    const deleted = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(String(deleted?.[0])).toBe("api/jobs/job-a");
    expect(wrapper.findAll(".job-history-table tr.activity-row")).toHaveLength(2);
    wrapper.unmount();
  });

  it("treats interrupted history as terminal and stops polling while hidden", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("api/job-history")) {
        return jsonResponse({
          items: [{ job_id: "job-old", task_type: "auto_tag", status: "interrupted" }],
          total_count: 1,
        });
      }
      return jsonResponse({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(TasksPage, {
      props: {
        libraries: [{ id: "people", name: "人物图库" }],
        visible: true,
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("已中断");
    expect(wrapper.find(".job-actions button").exists()).toBe(false);
    const requestsBeforeHide = fetchMock.mock.calls.length;
    await wrapper.setProps({ visible: false });
    await vi.advanceTimersByTimeAsync(10_000);
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(requestsBeforeHide);
    wrapper.unmount();
  });
});
