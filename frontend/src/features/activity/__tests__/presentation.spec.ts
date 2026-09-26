import { describe, expect, it } from "vitest";

import { formatDuration, jobListHeadline, jobListParts, taskTypeLabel, type JobListSource } from "../presentation";

function source(overrides: Partial<JobListSource> = {}): JobListSource {
  return {
    taskType: "auto_tag",
    status: "succeeded",
    libraryName: "人物图库",
    processed: 10,
    total: 10,
    failed: 0,
    resultSummary: {},
    ...overrides,
  };
}

describe("activity presentation", () => {
  it("shows subsecond completed jobs as less than one second", () => {
    expect(
      formatDuration(
        "2026-07-26T04:36:36.216Z",
        "2026-07-26T04:36:36.256Z",
      ),
    ).toBe("< 1 秒");
  });

  it("shows only the counts that actually changed", () => {
    const job = source({ resultSummary: { inserted: 4, updated: 1, deleted: 0, failed: 0 } });
    expect(jobListParts(job).detail).toBe("新增 4 张 · 更新 1 张");
    expect(jobListHeadline(job)).toBe("智能标注 · 人物图库 · 新增 4 张 · 更新 1 张");
  });

  it("names the startup tag check in ordinary Chinese", () => {
    expect(taskTypeLabel("auto_tag_policy_migrate")).toBe("核对旧标注");
    expect(taskTypeLabel("active_learning_queue")).toBe("整理待学习样本");
    expect(taskTypeLabel("some_internal_job")).toBe("后台整理");
    expect(jobListHeadline(source({ taskType: "auto_tag_policy_migrate" }))).toBe(
      "核对旧标注 · 人物图库 · 没有变化",
    );
  });

  it("says nothing changed when every count is zero", () => {
    expect(jobListParts(source()).detail).toBe("没有变化");
    expect(jobListParts(source()).showStatus).toBe(false);
  });

  it("describes a running job by progress and keeps the failure count", () => {
    const job = source({
      taskType: "index",
      status: "running",
      processed: 12,
      total: 40,
      failed: 2,
    });
    expect(jobListParts(job).detail).toBe("已处理 12 / 40 张 · 失败 2 张");
    expect(jobListHeadline(job)).toBe("运行中 · 建立索引 · 人物图库 · 已处理 12 / 40 张 · 失败 2 张");
  });
});
