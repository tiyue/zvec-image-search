import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ActivityLogTable from "../ActivityLogTable.vue";
import type { ActivityLogItem } from "../types";

function log(index: number, overrides: Partial<ActivityLogItem> = {}): ActivityLogItem {
  return {
    sequence: index,
    timestamp: "2026-07-19T08:00:00Z",
    level: "info",
    category: "search",
    event: "completed",
    source: "gateway",
    libraryId: "people",
    libraryName: "人物图库",
    jobId: `job-${index}`,
    operationId: `op-${index}`,
    message: "搜索完成",
    details: { result_count: 15 },
    thumbnailUrl: "",
    imageUrl: "",
    ...overrides,
  };
}

function mountTable(overrides: Record<string, unknown> = {}) {
  return mount(ActivityLogTable, {
    props: {
      items: [log(1)],
      filters: { level: "", category: "", libraryId: "", jobId: "", query: "" },
      ...overrides,
    },
  });
}

describe("ActivityLogTable", () => {
  it("renders no more than 50 log rows", () => {
    const wrapper = mountTable({
      items: Array.from({ length: 1_000 }, (_, index) => log(index + 1)),
      totalCount: 1_000,
    });
    expect(wrapper.findAll("tr.activity-row")).toHaveLength(50);
    expect(wrapper.text()).toContain("共 1000 条");
  });

  it("supports filters and clears a linked job filter", async () => {
    const wrapper = mountTable({
      filters: { level: "", category: "", libraryId: "", jobId: "job-1", query: "" },
      libraries: [{ id: "people", name: "人物图库" }],
    });
    expect(wrapper.text()).toContain("当前任务：job-1");
    await wrapper.get('select[name="log_level"]').setValue("warning");
    await wrapper.get('select[name="log_category"]').setValue("image_failure");
    await wrapper.get('select[name="log_library"]').setValue("people");
    await wrapper.get('input[name="log_job_id"]').setValue("job-manual");
    await wrapper.get('input[name="log_query"]').setValue("  无法解码  ");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("applyFilters")?.[0]).toEqual([
      {
        level: "warning",
        category: "image_failure",
        libraryId: "people",
        jobId: "job-manual",
        query: "无法解码",
      },
    ]);

    await wrapper.get(".active-filter-chip button").trigger("click");
    expect(wrapper.emitted("applyFilters")?.[1]?.[0]).toMatchObject({ jobId: "" });
  });

  it("submits the settings category group instead of a nonexistent raw category", async () => {
    const wrapper = mountTable();
    await wrapper.get('select[name="log_category"]').setValue("settings");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("applyFilters")?.[0]?.[0]).toMatchObject({
      category: "settings",
    });
  });

  it("pauses following, selects rows, copies and exports", async () => {
    const wrapper = mountTable({ selectedSequences: new Set([1]) });
    await wrapper.get(".follow-button").trigger("click");
    expect(wrapper.emitted("followingChange")?.[0]).toEqual([false]);

    await wrapper.get('input[aria-label="选择日志 1"]').trigger("change");
    expect(wrapper.emitted("toggleRow")?.[0]).toEqual([1]);
    await wrapper.get(".selected-copy button").trigger("click");
    expect(wrapper.emitted("copy")).toHaveLength(1);

    const exportButtons = wrapper.findAll(".export-actions button");
    await exportButtons[0]?.trigger("click");
    await exportButtons[1]?.trigger("click");
    expect(wrapper.emitted("export")).toEqual([["jsonl"], ["csv"]]);
    expect(wrapper.get(".export-actions small").text()).toContain("导出选中日志");
  });

  it("disables export actions while the complete filtered result is being prepared", () => {
    const wrapper = mountTable({ exporting: true });
    expect(wrapper.get(".export-actions small").text()).toContain("正在整理日志");
    expect(wrapper.findAll(".export-actions button").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
  });

  it("expands bounded technical details and links back to a job", async () => {
    const wrapper = mountTable({
      items: [log(1, { imageUrl: "http://127.0.0.1:8000/api/image/doc-1" })],
    });
    await wrapper.get(".details-toggle").trigger("click");
    expect(wrapper.text()).toContain("result_count");
    expect(wrapper.text()).toContain("15");
    expect(wrapper.text()).toContain("操作编号");
    expect(wrapper.get(".image-action").attributes("href")).toContain("api/image/doc-1");

    await wrapper.get(".job-link").trigger("click");
    expect(wrapper.emitted("selectJob")?.[0]).toEqual(["job-1"]);
  });

  it("offers refresh after an API failure and restores navigation", async () => {
    const wrapper = mountTable({
      errorMessage: "数据库暂时忙碌",
      hasPrevious: true,
      hasNext: true,
    });
    expect(wrapper.get("[role='alert']").text()).toContain("数据库暂时忙碌");
    await wrapper.get("[role='alert'] button").trigger("click");
    expect(wrapper.emitted("refresh")).toHaveLength(1);

    const paging = wrapper.findAll(".cursor-pagination button");
    await paging[0]?.trigger("click");
    await paging[1]?.trigger("click");
    expect(wrapper.emitted("previous")).toHaveLength(1);
    expect(wrapper.emitted("next")).toHaveLength(1);
  });
});
