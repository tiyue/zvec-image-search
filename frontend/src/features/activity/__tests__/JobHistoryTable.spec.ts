import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import JobHistoryTable from "../JobHistoryTable.vue";
import type { ActivityLogItem, JobHistoryItem } from "../types";

function job(index: number, overrides: Partial<JobHistoryItem> = {}): JobHistoryItem {
  return {
    id: `job-${index}`,
    taskType: "auto_tag",
    libraryId: "people",
    libraryName: "人物图库",
    status: "succeeded",
    submittedAt: "2026-07-19T08:00:00Z",
    startedAt: "2026-07-19T08:00:01Z",
    finishedAt: "2026-07-19T08:01:01Z",
    updatedAt: "2026-07-19T08:01:01Z",
    processed: 100,
    total: 100,
    failed: 0,
    progressPercent: 100,
    message: "完成",
    resultSummary: {},
    errorCode: "",
    errorMessage: "",
    ...overrides,
  };
}

function errorLog(sequence: number): ActivityLogItem {
  return {
    sequence,
    timestamp: "2026-07-19T08:01:00Z",
    level: "warning",
    category: "image_failure",
    event: "decode_failed",
    source: "worker",
    libraryId: "people",
    libraryName: "人物图库",
    jobId: "job-1",
    operationId: "",
    message: "图片损坏",
    details: { relative_path: "角色/坏图.jpg", reason: "无法解码" },
    thumbnailUrl: "",
    imageUrl: "http://127.0.0.1:8000/api/image/doc-broken",
  };
}

function mountTable(overrides: Record<string, unknown> = {}) {
  return mount(JobHistoryTable, {
    props: {
      items: [job(1)],
      filters: { status: "", taskType: "", libraryId: "", query: "" },
      ...overrides,
    },
  });
}

describe("JobHistoryTable", () => {
  it("renders at most 50 rows and emits row selection", async () => {
    const wrapper = mountTable({
      items: Array.from({ length: 1_000 }, (_, index) => job(index)),
      totalCount: 1_000,
    });

    expect(wrapper.findAll("tr.activity-row")).toHaveLength(50);
    expect(wrapper.text()).toContain("共 1000 条");
    await wrapper.findAll("tr.activity-row")[0]?.trigger("click");
    expect(wrapper.emitted("select")?.[0]).toEqual(["job-0"]);
  });

  it("applies status, task, library and text filters", async () => {
    const wrapper = mountTable({ libraries: [{ id: "people", name: "人物图库" }] });
    await wrapper.get('select[name="job_status"]').setValue("failed");
    await wrapper.get('select[name="job_task_type"]').setValue("auto_tag");
    await wrapper.get('select[name="job_library"]').setValue("people");
    await wrapper.get('input[name="job_query"]').setValue("  图片损坏  ");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("applyFilters")?.[0]).toEqual([
      { status: "failed", taskType: "auto_tag", libraryId: "people", query: "图片损坏" },
    ]);
  });

  it("requires a second click before emitting cancellation", async () => {
    const wrapper = mountTable({ items: [job(1, { status: "running" })] });
    await wrapper.get(".job-actions button").trigger("click");
    expect(wrapper.emitted("cancel")).toBeUndefined();
    expect(wrapper.text()).toContain("确认取消？");

    await wrapper.get(".activity-button-danger").trigger("click");
    expect(wrapper.emitted("cancel")?.[0]).toEqual(["job-1"]);
  });

  it("opens an error-image interface and offers retry after failure", async () => {
    const wrapper = mountTable({
      items: [job(1, { failed: 2, status: "partial" })],
      expandedErrorJobId: "job-1",
      errorLogs: [errorLog(1)],
      errorsError: "临时读取失败",
    });

    expect(wrapper.text()).toContain("错误图片 · 2 张");
    expect(wrapper.text()).toContain("临时读取失败");
    await wrapper.get(".inline-error button").trigger("click");
    expect(wrapper.emitted("retryErrors")).toHaveLength(1);

    await wrapper.setProps({ errorsError: "" });
    expect(wrapper.text()).toContain("角色/坏图.jpg");
    expect(wrapper.text()).toContain("无法解码");
    expect(wrapper.get(".error-image-action").attributes("href")).toContain("api/image/doc-broken");
  });

  it("uses cursor pagination instead of rendering all history", async () => {
    const wrapper = mountTable({ page: 3, hasPrevious: true, hasNext: true });
    const buttons = wrapper.findAll(".cursor-pagination button");
    await buttons[0]?.trigger("click");
    await buttons[1]?.trigger("click");
    expect(wrapper.emitted("previous")).toHaveLength(1);
    expect(wrapper.emitted("next")).toHaveLength(1);
    expect(wrapper.text()).toContain("第 3 页");
  });

  it("uses the backend folder-deletion command in labels and filters", async () => {
    const wrapper = mountTable({
      items: [
        job(1, { taskType: "folder_delete_commit" }),
        job(2, { taskType: "folder_delete_preview" }),
      ],
    });
    expect(
      wrapper
        .findAll(".job-history-table tbody tr.activity-row td:nth-child(2) strong")
        .map((item) => item.text()),
    ).toEqual(["文件夹清理", "文件夹清理"]);

    await wrapper.get('select[name="job_task_type"]').setValue("folder_delete_commit");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("applyFilters")?.[0]?.[0]).toMatchObject({
      taskType: "folder_delete_commit",
    });
  });
});
