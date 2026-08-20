import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SearchLearningSettingsCard from "../SearchLearningSettingsCard.vue";
import type {
  SearchLearningApi,
  SearchLearningSettings,
  SearchLearningStatus,
} from "../types";

function settings(
  overrides: Partial<SearchLearningSettings> = {},
): SearchLearningSettings {
  return {
    learning_enabled: true,
    implicit_feedback_enabled: false,
    save_query_text: false,
    active_model_version: null,
    previous_model_version: "ranking-v0",
    shadow_mode: true,
    ...overrides,
  };
}

function status(): SearchLearningStatus {
  return {
    available: true,
    database: "search-learning.sqlite3",
    online_weight_updates: false,
    fixed_evaluation_gate_required: true,
    fixed_evaluation: {
      available: true,
      status: "ready",
      evaluation_set_id: "human-reviewed-v1",
      case_count: 60,
      no_answer_count: 15,
      candidate_count: 600,
    },
    training_running: false,
    settings: settings(),
    training_counts: {
      query_sessions: 100,
      explicit_samples: 300,
      positive_samples: 180,
      negative_samples: 120,
    },
    minimum_requirements: {
      query_sessions: 100,
      explicit_samples: 300,
      positive_and_negative_required: true,
    },
    model_versions: [
      {
        model_version: "ranking-v1",
        gate_status: "passed",
        created_at: "2026-07-19T10:00:00Z",
        metrics: {
          status: "passed",
          reasons: [],
          metrics: {
            fixed_evaluation_set: true,
            evaluation_set_id: "human-reviewed-v1",
            current_model_version: "ranking-base",
            candidate_model_version: "ranking-v1",
            current: {
              precision_at_15: 0.6,
              recall_at_15: 0.7,
              no_answer_false_positive_rate: 0.09,
              cross_collection_bias: 0.12,
              p95_latency_ms: 100,
              external_api_calls: 0,
            },
            candidate: {
              precision_at_15: 0.64,
              recall_at_15: 0.69,
              no_answer_false_positive_rate: 0.08,
              cross_collection_bias: 0.1,
              p95_latency_ms: 108,
              external_api_calls: 0,
            },
          },
        },
      },
      {
        model_version: "ranking-v0",
        gate_status: "failed",
        created_at: "2026-07-18T10:00:00Z",
        metrics: {
          status: "failed",
          reasons: [
            "precision_at_15_regressed",
            "no_answer_false_positive_rate_above_10_percent",
          ],
          metrics: {
            fixed_evaluation_set: true,
            evaluation_set_id: "human-reviewed-v1",
            current_model_version: "ranking-base",
            candidate_model_version: "ranking-v0",
            current: {
              precision_at_15: 0.6,
              recall_at_15: 0.7,
              no_answer_false_positive_rate: 0.09,
              cross_collection_bias: 0.12,
              p95_latency_ms: 100,
              external_api_calls: 0,
            },
            candidate: {
              precision_at_15: 0.58,
              recall_at_15: 0.69,
              no_answer_false_positive_rate: 0.12,
              cross_collection_bias: 0.11,
              p95_latency_ms: 104,
              external_api_calls: 0,
            },
          },
        },
      },
    ],
  };
}

function fakeApi(): SearchLearningApi {
  return {
    status: vi.fn().mockResolvedValue(status()),
    updateSettings: vi.fn().mockImplementation(async (body) => settings(body)),
    feedback: vi.fn(),
    revoke: vi.fn(),
    listFeedback: vi.fn(),
    train: vi.fn().mockResolvedValue({ queued: true }),
    installEvaluation: vi.fn().mockResolvedValue({ installed: true }),
    activate: vi.fn().mockResolvedValue(settings()),
    rollback: vi.fn().mockResolvedValue(settings()),
    clear: vi.fn().mockResolvedValue({ cleared: true }),
    exportData: vi.fn().mockResolvedValue({ schema_version: 1, examples: [] }),
  };
}

describe("SearchLearningSettingsCard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders readable local-learning controls and emits a toast after saving", async () => {
    const api = fakeApi();
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("搜索学习");
    expect(wrapper.text()).toContain("不调用大模型");
    expect(wrapper.text()).toContain("100 / 100");
    expect(wrapper.text()).toContain("300 / 300");

    const implicitToggle = wrapper.findAll("input[type='checkbox']")[1];
    await implicitToggle.setValue(true);
    await flushPromises();

    expect(api.updateSettings).toHaveBeenCalledWith({
      implicit_feedback_enabled: true,
    });
    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "设置已保存",
      "隐式反馈已开启。",
      "success",
    ]);
  });

  it("uses the YaoLens product name for anonymous export downloads", async () => {
    const api = fakeApi();
    const downloads: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function captureDownload(this: HTMLAnchorElement) {
        downloads.push(this.download);
      },
    );
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue(
      "blob:yaolens-search-learning",
    );
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(
      () => undefined,
    );
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    const exportButton = wrapper.findAll("button").find((button) =>
      button.text().includes("导出匿名数据"),
    );
    expect(exportButton).toBeDefined();
    await exportButton?.trigger("click");
    await flushPromises();

    expect(api.exportData).toHaveBeenCalledOnce();
    expect(downloads).toEqual(["yaolens-search-learning-anonymous.json"]);
    wrapper.unmount();
    click.mockRestore();
    createObjectUrl.mockRestore();
    revokeObjectUrl.mockRestore();
  });

  it("starts gated training and reports failures without leaving controls busy", async () => {
    const api = fakeApi();
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    const trainButton = wrapper.findAll("button").find((button) =>
      button.text().includes("训练候选版本"),
    );
    expect(trainButton).toBeDefined();
    expect(trainButton?.attributes("disabled")).toBeUndefined();
    await trainButton?.trigger("click");
    await flushPromises();

    expect(api.train).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("通过固定评测前不会改变搜索排序");

    vi.mocked(api.updateSettings).mockRejectedValueOnce(new Error("disk full"));
    await wrapper.findAll("input[type='checkbox']")[0].setValue(false);
    await flushPromises();
    expect(wrapper.get("[role='alert']").text()).toContain("disk full");
    expect(wrapper.findAll("input[type='checkbox']")[0].attributes("disabled")).toBeUndefined();
    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "搜索学习设置未保存",
      "disk full",
      "error",
    ]);
  });

  it("shows the fixed set and only activates a gate-approved version", async () => {
    const api = fakeApi();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("human-reviewed-v1");
    expect(wrapper.text()).toContain("60 条查询");
    expect(wrapper.text()).toContain("ranking-v1");
    expect(wrapper.text()).toContain("已通过");
    expect(wrapper.text()).toContain("ranking-v0");
    expect(wrapper.text()).toContain("未通过");
    expect(wrapper.text()).toContain("Precision@15 +4.0 个百分点");
    expect(wrapper.text()).toContain("Recall@15 -1.0 个百分点");
    expect(wrapper.text()).toContain("无答案误报率");
    expect(wrapper.text()).toContain("跨图库偏置");
    expect(wrapper.text()).toContain("P95 延迟");
    expect(wrapper.text()).toContain("额外 API 调用");
    expect(wrapper.text()).toContain("Precision@15 低于当前版本");
    expect(wrapper.text()).toContain("无答案误报率超过 10%");

    const liveButton = wrapper.findAll("button").find((button) =>
      button.text().includes("正式启用"),
    );
    await liveButton?.trigger("click");
    await flushPromises();

    expect(api.activate).toHaveBeenCalledWith("ranking-v1", false);
    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "搜索排序版本已更新",
      "通过固定评测的排序版本已正式启用。",
      "success",
    ]);
  });

  it("keeps the live action available while the current version is in shadow mode", async () => {
    const api = fakeApi();
    const shadowStatus = status();
    shadowStatus.settings = settings({
      active_model_version: "ranking-v1",
      previous_model_version: "ranking-base",
      shadow_mode: true,
    });
    vi.mocked(api.status).mockResolvedValue(shadowStatus);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("当前影子版本");
    const liveButton = wrapper.findAll("button").find((button) =>
      button.text().includes("正式启用"),
    );
    expect(liveButton).toBeDefined();
    await liveButton?.trigger("click");
    await flushPromises();

    expect(api.activate).toHaveBeenCalledWith("ranking-v1", false);
  });

  it("keeps candidates pending when no fixed evaluation pack is installed", async () => {
    const api = fakeApi();
    const pendingStatus = status();
    pendingStatus.fixed_evaluation = {
      available: false,
      status: "missing",
    };
    pendingStatus.model_versions = [
      {
        model_version: "ranking-pending",
        gate_status: "pending",
        created_at: "2026-07-19T11:00:00Z",
        metrics: {
          status: "pending",
          reasons: ["fixed_evaluator_unavailable"],
          metrics: { fixed_evaluation_set: false },
        },
      },
    ];
    vi.mocked(api.status).mockResolvedValue(pendingStatus);
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    expect(wrapper.text()).toContain("候选版本将保持待评测");
    expect(wrapper.text()).toContain("尚未安装固定评测包，候选版本保持待评测");
    expect(wrapper.text()).toContain("待评测");
    expect(wrapper.findAll("button").some((button) => button.text().includes("正式启用"))).toBe(false);
  });

  it("imports a user-selected fixed evaluation JSON through the desktop bridge", async () => {
    const api = fakeApi();
    const selectJsonFile = vi.fn().mockResolvedValue({
      ok: true,
      path: "C:\\Evaluation\\fixed-evaluation.json",
    });
    Object.defineProperty(window, "pywebview", {
      configurable: true,
      value: { api: { select_json_file: selectJsonFile } },
    });
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    const importButton = wrapper.findAll("button").find((button) =>
      button.text().includes("导入评测包"),
    );
    await importButton?.trigger("click");
    await flushPromises();

    expect(api.installEvaluation).toHaveBeenCalledWith(
      "C:\\Evaluation\\fixed-evaluation.json",
    );
    expect(wrapper.emitted("toast")?.at(-1)?.[0]).toBe("固定评测包已安装");
    wrapper.unmount();
    Reflect.deleteProperty(window, "pywebview");
  });

  it("recovers the evaluation import button after a failure and allows retry", async () => {
    const api = fakeApi();
    vi.mocked(api.installEvaluation)
      .mockRejectedValueOnce(new Error("evaluation pack is invalid"))
      .mockResolvedValueOnce({ installed: true });
    const selectJsonFile = vi.fn().mockResolvedValue({
      ok: true,
      path: "C:\\Evaluation\\fixed-evaluation.json",
    });
    Object.defineProperty(window, "pywebview", {
      configurable: true,
      value: { api: { select_json_file: selectJsonFile } },
    });
    const wrapper = mount(SearchLearningSettingsCard, { props: { api } });
    await flushPromises();

    const importButton = wrapper.findAll("button").find((button) =>
      button.text().includes("导入评测包"),
    );
    await importButton?.trigger("click");
    await flushPromises();

    expect(wrapper.get("[role='alert']").text()).toContain("evaluation pack is invalid");
    expect(importButton?.attributes("disabled")).toBeUndefined();
    expect(wrapper.emitted("toast")?.at(-1)?.[0]).toBe("无法导入固定评测包");

    await importButton?.trigger("click");
    await flushPromises();

    expect(api.installEvaluation).toHaveBeenCalledTimes(2);
    expect(wrapper.find("[role='alert']").exists()).toBe(false);
    expect(wrapper.emitted("toast")?.at(-1)?.[0]).toBe("固定评测包已安装");
    wrapper.unmount();
    Reflect.deleteProperty(window, "pywebview");
  });
});
