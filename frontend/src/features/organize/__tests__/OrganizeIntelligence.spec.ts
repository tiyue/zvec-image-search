import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ActiveLearningPanel from "../ActiveLearningPanel.vue";
import SimilarityGroupsPanel from "../SimilarityGroupsPanel.vue";
import type { OrganizeApi, OrganizeJobResponse } from "../types";
import {
  normalizeLearningQueue,
  normalizeLearningReviewResult,
} from "../useOrganizeIntelligence";

function success(result: Record<string, unknown>): OrganizeJobResponse {
  return { job: { id: "", status: "succeeded", result } };
}

function clusterList(offset = 0): Record<string, unknown> {
  return {
    offset,
    limit: 12,
    total: 13,
    clusters: [
      {
        cluster_id: offset ? "cluster-last" : "cluster-raiden",
        cluster_type: offset ? "semantic" : "exact",
        member_count: offset ? 2 : 4,
        edge_kinds: offset ? ["semantic"] : ["exact", "perceptual"],
        representative: {
          doc_id: offset ? "doc-last" : "doc-1",
          image_id: offset ? "native-last" : "native-doc-1",
          file_name: offset ? "last.jpg" : "01.jpg",
          relative_path: offset ? "其他/last.jpg" : "原神/雷电将军/01.jpg",
          thumbnail_url: offset
            ? "api/image/doc-last?variant=thumbnail"
            : "api/image/doc-1?variant=thumbnail",
          image_available: true,
        },
        identity_anchors: [
          {
            category: "character",
            value: "雷电将军",
            source: "folder",
            confidence: 0.98,
            conflict: false,
          },
        ],
      },
      ...(
        offset
          ? []
          : [
              {
                cluster_id: "cluster-klee",
                cluster_type: "semantic",
                member_count: 3,
                edge_kinds: ["semantic"],
                representative: {
                  doc_id: "doc-klee",
                  image_id: "native-klee",
                  file_name: "klee.jpg",
                  relative_path: "原神/可莉/klee.jpg",
                  thumbnail_url: "api/image/doc-klee?variant=thumbnail",
                  image_available: true,
                },
                identity_anchors: [
                  {
                    category: "character",
                    value: "可莉",
                    source: "manual",
                    confidence: 1,
                    conflict: false,
                  },
                ],
              },
            ]
      ),
    ],
  };
}

function clusterDetail(offset = 0): Record<string, unknown> {
  return {
    offset,
    limit: 20,
    total: 22,
    cluster: {
      cluster_id: "cluster-raiden",
      cluster_type: "exact",
      member_count: 22,
      edge_kinds: ["exact"],
      representative: {
        doc_id: "doc-1",
        image_id: "native-doc-1",
        file_name: "01.jpg",
      },
    },
    members: offset
      ? [
          {
            doc_id: "doc-21",
            image_id: "native-doc-21",
            file_name: "21.jpg",
            relative_path: "原神/雷电将军/21.jpg",
            thumbnail_url: "api/image/doc-21?variant=thumbnail",
            image_available: true,
          },
          {
            doc_id: "doc-22",
            image_id: "native-doc-22",
            file_name: "22.jpg",
            relative_path: "原神/雷电将军/22.jpg",
            thumbnail_url: "api/image/doc-22?variant=thumbnail",
            image_available: true,
          },
        ]
      : [
      {
        doc_id: "doc-1",
        image_id: "native-doc-1",
        file_name: "01.jpg",
        relative_path: "原神/雷电将军/01.jpg",
        thumbnail_url: "api/image/doc-1?variant=thumbnail",
        image_available: true,
      },
      {
        doc_id: "doc-2",
        image_id: "native-doc-2",
        file_name: "02.jpg",
        relative_path: "原神/雷电将军/02.jpg",
        thumbnail_url: "api/image/doc-2?variant=thumbnail",
        image_available: true,
      },
    ],
  };
}

function learningQueue(): Record<string, unknown> {
  return {
    queue_id: "queue-1",
    candidate_count: 28,
    items: [
      {
        rank: 1,
        doc_id: "learn-1",
        image_id: "native-learn-1",
        group_id: "cluster-a",
        query_id: "query-a",
        file_name: "conflict.jpg",
        relative_path: "原神/角色/conflict.jpg",
        thumbnail_url: "api/image/learn-1?variant=thumbnail",
        image_available: true,
        candidate_kind: "tag_review",
        source_sha256: "a".repeat(64),
        uncertainty_score: 0.92,
        reasons: ["identity_conflict", "ranking_disagreement"],
        suggested_tags: ["原神", "雷电将军"],
      },
      {
        rank: 2,
        doc_id: "learn-2",
        image_id: "native-learn-2",
        group_id: "cluster-a",
        query_id: "query-b",
        file_name: "outlier.jpg",
        relative_path: "写真/outlier.jpg",
        thumbnail_url: "api/image/learn-2?variant=thumbnail",
        image_available: true,
        uncertainty_score: 0.74,
        reasons: ["cluster_outlier"],
      },
    ],
  };
}

function fakeApi(
  submitImplementation?: (
    body: Record<string, unknown>,
  ) => Promise<OrganizeJobResponse>,
  overrides: Partial<OrganizeApi> = {},
): OrganizeApi {
  return {
    bootstrap: vi.fn(async () => ({ libraries: [] })),
    listFolders: vi.fn(async () => ({ folders: [], total: 0 })),
    listFolderImages: vi.fn(async () => ({ items: [], total: 0 })),
    submitJob: vi.fn(
      submitImplementation ??
        (async (body) => {
          switch (body.task_type) {
            case "cluster_list":
              return success(clusterList(Number(body.offset ?? 0)));
            case "cluster_detail":
              return success(clusterDetail(Number(body.offset ?? 0)));
            case "cluster_merge":
            case "cluster_split":
            case "cluster_apply_identity":
              return success({
                batch_id: "cluster-batch-1",
                applied: 1,
                failed: 0,
                conflicts: 0,
                undo_available: true,
              });
            case "cluster_undo":
              return success({
                batch_id: "cluster-batch-1",
                applied: 1,
                failed: 0,
                conflicts: 0,
                undo_available: false,
              });
            case "active_learning_queue":
              return success(learningQueue());
            case "active_learning_review":
              return success({
                batch_id: "review-batch-1",
                applied: Array.isArray(body.decisions) ? body.decisions.length : 0,
                failed: 0,
                skipped: 0,
                conflicts: 0,
                undo_available: true,
                failures: [],
              });
            case "active_learning_review_undo":
              return success({
                batch_id: "review-batch-1",
                applied: 0,
                failed: 0,
                skipped: 0,
                conflicts: 0,
                undo_available: false,
                failures: [],
              });
            default:
              return success({ updated: 1 });
          }
        }),
    ),
    job: vi.fn(async (jobId) => ({ job: { id: jobId, status: "succeeded" } })),
    cancelJob: vi.fn(async (jobId) => ({ job: { id: jobId, status: "cancelled" } })),
    ...overrides,
  };
}

function buttonWithText(wrapper: Pick<VueWrapper, "findAll">, label: string) {
  const button = wrapper.findAll("button").find((value) => value.text().includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  return button;
}

describe("organize similarity groups", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("loads, filters and pages clusters through api/jobs", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_list",
        library_id: "lib-1",
        offset: 0,
        limit: 12,
        cluster_type: "all",
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.text()).toContain("雷电将军");
    expect(wrapper.text()).toContain("4 张");
    expect(wrapper.text()).toContain("不调用模型 · 无新增费用");

    await wrapper.get('.cluster-list-heading select').setValue("semantic");
    await flushPromises();
    expect(api.submitJob).toHaveBeenLastCalledWith(
      expect.objectContaining({
        task_type: "cluster_list",
        cluster_type: "semantic",
        offset: 0,
      }),
      expect.any(AbortSignal),
    );

    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(api.submitJob).toHaveBeenLastCalledWith(
      expect.objectContaining({ task_type: "cluster_list", offset: 12 }),
      expect.any(AbortSignal),
    );
    expect(wrapper.text()).toContain("last.jpg");
  });

  it("submits incremental clustering with selected sources and reloads the list", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    const sourceOptions = wrapper.findAll(".cluster-type-option");
    const exact = sourceOptions.find((value) => value.text().includes("完全重复"));
    const perceptual = sourceOptions.find((value) =>
      value.text().includes("视觉近似"),
    );
    const semantic = sourceOptions.find((value) =>
      value.text().includes("内容相似"),
    );
    if (!exact || !perceptual || !semantic) {
      throw new Error("clustering source option missing");
    }
    expect(
      (exact.get('input[type="checkbox"]').element as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      (perceptual.get('input[type="checkbox"]').element as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(
      (semantic.get('input[type="checkbox"]').element as HTMLInputElement).checked,
    ).toBe(false);
    expect(wrapper.text()).toContain("大图库耗时较长，按需启用");
    expect(wrapper.find(".semantic-performance-note").exists()).toBe(false);

    await semantic.get('input[type="checkbox"]').setValue(true);
    expect(wrapper.text()).toContain("不调用模型，也不会产生 API 费用");

    await wrapper.get('.cluster-controls select').setValue("all");
    await perceptual.get('input[type="checkbox"]').setValue(false);
    await buttonWithText(wrapper, "运行增量聚类").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_images",
        library_id: "lib-1",
        scope: "all",
        cluster_types: ["exact", "semantic"],
      },
      expect.any(AbortSignal),
    );
    expect(
      vi.mocked(api.submitJob).mock.calls.filter(
        ([body]) => body.task_type === "cluster_list",
      ),
    ).toHaveLength(2);
  });

  it("opens cluster detail and double-clicks a member through the native preview", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    await wrapper.get('[data-cluster-id="cluster-raiden"] .cluster-preview').trigger("click");
    await flushPromises();
    const dialog = wrapper.get('[role="dialog"]');
    expect(api.submitJob).toHaveBeenLastCalledWith(
      {
        task_type: "cluster_detail",
        library_id: "lib-1",
        cluster_id: "cluster-raiden",
        offset: 0,
        limit: 20,
      },
      expect.any(AbortSignal),
    );
    expect(dialog.text()).toContain("22 张相似图片");
    expect(dialog.text()).toContain("人工操作会写入分组或身份标签");

    await dialog.findAll(".cluster-member")[1]?.trigger("dblclick");
    expect(wrapper.emitted("openImage")).toEqual([["native-doc-2"]]);

    await buttonWithText(dialog, "下一页").trigger("click");
    await flushPromises();
    expect(api.submitJob).toHaveBeenLastCalledWith(
      expect.objectContaining({
        task_type: "cluster_detail",
        cluster_id: "cluster-raiden",
        offset: 20,
        limit: 20,
      }),
      expect.any(AbortSignal),
    );
    expect(wrapper.get('[role="dialog"]').text()).toContain("21.jpg");
  });

  it("merges multiple selected groups and exposes the undo action", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    const selectors = wrapper.findAll('.cluster-select input[type="checkbox"]');
    await selectors[0]?.setValue(true);
    await selectors[1]?.setValue(true);
    expect(wrapper.text()).toContain("已选 2 组");
    await buttonWithText(wrapper, "合并所选").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_merge",
        library_id: "lib-1",
        cluster_ids: ["cluster-klee", "cluster-raiden"],
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.get(".cluster-operation-summary").text()).toContain("已应用1");
    expect(wrapper.get(".cluster-operation-summary").text()).toContain("失败0");
    expect(wrapper.get(".cluster-operation-summary").text()).toContain("冲突0");
    expect(buttonWithText(wrapper, "撤销最近一次整理").attributes("disabled"))
      .toBeUndefined();

    await buttonWithText(wrapper, "撤销最近一次整理").trigger("click");
    await flushPromises();
    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_undo",
        library_id: "lib-1",
      },
      expect.any(AbortSignal),
    );
    expect(buttonWithText(wrapper, "撤销最近一次整理").attributes("disabled"))
      .toBeDefined();
  });

  it("keeps selected groups when the backend reports a partial conflict", async () => {
    const api = fakeApi(async (body) => {
      if (body.task_type === "cluster_list") return success(clusterList());
      if (body.task_type === "cluster_merge") {
        return success({
          batch_id: "cluster-partial",
          applied: 1,
          failed: 0,
          conflicts: 1,
          undo_available: true,
        });
      }
      return success({});
    });
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    for (const selector of wrapper.findAll('.cluster-select input[type="checkbox"]')) {
      await selector.setValue(true);
    }

    await buttonWithText(wrapper, "合并所选").trigger("click");
    await flushPromises();

    expect(wrapper.get(".cluster-operation-summary").text()).toContain("冲突1");
    expect(wrapper.text()).toContain("已选 2 组");
    expect(buttonWithText(wrapper, "合并所选").attributes("disabled"))
      .toBeUndefined();
  });

  it("splits selected wrong members while keeping double-click preview", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    await wrapper
      .get('[data-cluster-id="cluster-raiden"] .cluster-preview')
      .trigger("click");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    await dialog.findAll(".cluster-member")[1]?.trigger("click");
    expect(dialog.text()).toContain("已选 1 张错误图片");
    await buttonWithText(dialog, "拆分所选").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_split",
        library_id: "lib-1",
        cluster_id: "cluster-raiden",
        doc_ids: ["doc-2"],
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.get('[role="dialog"]').text()).toContain("已选 0 张错误图片");
  });

  it("applies only supported identity categories to the entire cluster", async () => {
    const api = fakeApi();
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    await wrapper
      .get('[data-cluster-id="cluster-raiden"] .cluster-preview')
      .trigger("click");
    await flushPromises();

    const dialog = wrapper.get('[role="dialog"]');
    const category = dialog.get('.cluster-identity-editor select');
    expect(category.findAll("option").map((option) => option.attributes("value")))
      .toEqual(["real_person", "cosplayer", "character", "work"]);
    expect(category.text()).not.toContain("动作");
    expect(category.text()).not.toContain("神态");
    await category.setValue("cosplayer");
    await dialog.get('.cluster-identity-editor input').setValue("织羽");
    await dialog.get(".cluster-identity-editor").trigger("submit");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "cluster_apply_identity",
        library_id: "lib-1",
        cluster_id: "cluster-raiden",
        identity_category: "cosplayer",
        identity_value: "织羽",
      },
      expect.any(AbortSignal),
    );
  });

  it("keeps selections and restores merge controls after an operation failure", async () => {
    let mergeAttempts = 0;
    const api = fakeApi(async (body) => {
      if (body.task_type === "cluster_list") return success(clusterList());
      if (body.task_type === "cluster_merge") {
        mergeAttempts += 1;
        if (mergeAttempts === 1) throw new Error("merge temporarily unavailable");
        return success({
          applied: 1,
          failed: 0,
          conflicts: 0,
          undo_available: true,
        });
      }
      return success(clusterDetail(Number(body.offset ?? 0)));
    });
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    for (const selector of wrapper.findAll('.cluster-select input[type="checkbox"]')) {
      await selector.setValue(true);
    }

    await buttonWithText(wrapper, "合并所选").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "合并所选").attributes("disabled"))
      .toBeUndefined();
    expect(wrapper.text()).toContain("已选 2 组");

    await buttonWithText(wrapper, "合并所选").trigger("click");
    await flushPromises();
    expect(mergeAttempts).toBe(2);
  });

  it("cancels a queued clustering task and restores its controls", async () => {
    const submitJob = vi.fn(async (body: Record<string, unknown>) => {
      if (body.task_type === "cluster_images") {
        return { job: { id: "cluster-job", status: "queued" } };
      }
      return success(clusterList());
    });
    const cancelJob = vi.fn(async () => ({
      job: { id: "cluster-job", status: "cancelled" },
    }));
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api: fakeApi(submitJob, { cancelJob }), libraryId: "lib-1" },
    });
    await flushPromises();

    await buttonWithText(wrapper, "运行增量聚类").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "取消当前操作").exists()).toBe(true);
    await buttonWithText(wrapper, "取消当前操作").trigger("click");
    await flushPromises();

    expect(cancelJob).toHaveBeenCalledWith("cluster-job", expect.any(AbortSignal));
    expect(buttonWithText(wrapper, "运行增量聚类").attributes("disabled")).toBeUndefined();
  });

  it("recovers after a failed submission and allows retry", async () => {
    let attempts = 0;
    const api = fakeApi(async (body) => {
      if (body.task_type === "cluster_list") return success(clusterList());
      attempts += 1;
      if (attempts === 1) throw new Error("temporary failure");
      return success({ clustered_count: 2 });
    });
    const wrapper = mount(SimilarityGroupsPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    await buttonWithText(wrapper, "运行增量聚类").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "运行增量聚类").attributes("disabled")).toBeUndefined();

    await buttonWithText(wrapper, "运行增量聚类").trigger("click");
    await flushPromises();
    expect(attempts).toBe(2);
  });
});

describe("organize active learning", () => {
  beforeEach(() => vi.clearAllMocks());

  it("normalizes new candidate metadata while accepting legacy queue items", () => {
    const normalized = normalizeLearningQueue(learningQueue());

    expect(normalized?.samples[0]).toMatchObject({
      candidateKind: "tag_review",
      sourceSha256: "a".repeat(64),
    });
    expect(normalized?.samples[1]).toMatchObject({
      candidateKind: "tag_review",
      sourceSha256: "",
    });
    expect(
      normalizeLearningReviewResult({
        batch_id: "batch-1",
        applied: 3,
        failed: 1,
        skipped: 2,
        conflicts: 1,
        undo_available: true,
        failures: [
          {
            doc_id: "learn-2",
            code: "source_changed",
            message: "图片已变化",
          },
        ],
      }),
    ).toMatchObject({
      batchId: "batch-1",
      applied: 3,
      failed: 1,
      skipped: 2,
      conflicts: 1,
      undoAvailable: true,
      failures: [
        {
          docId: "learn-2",
          code: "source_changed",
          message: "图片已变化",
          retryable: true,
        },
      ],
    });
  });

  it("loads a 20-30 item budget and explains every uncertainty reason", async () => {
    const api = fakeApi();
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "active_learning_queue",
        library_id: "lib-1",
        review_budget: 25,
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.text()).toContain("身份冲突");
    expect(wrapper.text()).toContain("分组离群");
    expect(wrapper.text()).toContain("排序差异");
    expect(wrapper.text()).toContain("不会调用 Qwen");
    expect(wrapper.text()).toContain("审核成功后会真实写入");
    expect(wrapper.text()).toContain("标签审核");
    expect(wrapper.text()).toContain("来源 aaaaaaaa");
  });

  it("supports mixed individual and multi-select decisions", async () => {
    const api = fakeApi();
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    await wrapper
      .get('[data-learning-id="learn-1"] .single-decisions [data-decision="accept"]')
      .trigger("click");
    await wrapper
      .get('[data-learning-id="learn-2"] .learning-select input')
      .setValue(true);
    await wrapper.get('.edit-tag-input input').setValue("写真、棚拍");
    await wrapper.get('.batch-decisions [data-decision="edit"]').trigger("click");
    expect(wrapper.text()).toContain("已设置 2 项决定");

    await buttonWithText(wrapper, "提交 2 项审核").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith(
      {
        task_type: "active_learning_review",
        library_id: "lib-1",
        queue_id: "queue-1",
        decisions: [
          { doc_id: "learn-1", decision: "accept" },
          { doc_id: "learn-2", decision: "edit", labels: ["写真", "棚拍"] },
        ],
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.text()).toContain("已应用");
    expect(wrapper.text()).toContain("2");
  });

  it("keeps failed and conflicting decisions visible for a direct retry", async () => {
    let reviewAttempt = 0;
    const api = fakeApi(async (body) => {
      if (body.task_type === "active_learning_queue") {
        return success(learningQueue());
      }
      if (body.task_type === "active_learning_review") {
        reviewAttempt += 1;
        return success(
          reviewAttempt === 1
            ? {
                batch_id: "partial-batch",
                applied: 1,
                failed: 0,
                skipped: 0,
                conflicts: 1,
                undo_available: true,
                failures: [
                  {
                    doc_id: "learn-2",
                    code: "source_changed",
                    message: "源图片已变化",
                  },
                ],
              }
            : {
                batch_id: "retry-batch",
                applied: 1,
                failed: 0,
                skipped: 0,
                conflicts: 0,
                undo_available: true,
                failures: [],
              },
        );
      }
      return success({});
    });
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    await wrapper
      .get('[data-learning-id="learn-1"] [data-decision="accept"]')
      .trigger("click");
    await wrapper
      .get('[data-learning-id="learn-2"] [data-decision="reject"]')
      .trigger("click");
    await buttonWithText(wrapper, "提交 2 项审核").trigger("click");
    await flushPromises();

    expect(wrapper.get(".review-summary").text()).toContain("已应用1");
    expect(wrapper.get(".review-summary").text()).toContain("失败0");
    expect(wrapper.get(".review-summary").text()).toContain("冲突1");
    expect(wrapper.get('[data-learning-id="learn-2"] .review-failure').text())
      .toContain("源图片已变化");
    expect(wrapper.find('[data-learning-id="learn-1"] .decision-state').exists())
      .toBe(false);
    expect(buttonWithText(wrapper, "提交 1 项审核").attributes("disabled"))
      .toBeUndefined();

    await buttonWithText(wrapper, "提交 1 项审核").trigger("click");
    await flushPromises();

    expect(reviewAttempt).toBe(2);
    expect(wrapper.find(".review-failure").exists()).toBe(false);
    expect(buttonWithText(wrapper, "提交 0 项审核").attributes("disabled"))
      .toBeDefined();
  });

  it("submits the current-library review undo and restores editable decisions", async () => {
    const api = fakeApi();
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();

    await wrapper
      .get('[data-learning-id="learn-1"] [data-decision="accept"]')
      .trigger("click");
    await buttonWithText(wrapper, "提交 1 项审核").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "撤销最近一次审核").attributes("disabled"))
      .toBeUndefined();

    await buttonWithText(wrapper, "撤销最近一次审核").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenLastCalledWith(
      {
        task_type: "active_learning_review_undo",
        library_id: "lib-1",
      },
      expect.any(AbortSignal),
    );
    expect(wrapper.text()).toContain("review-batch-1 已撤销");
    expect(buttonWithText(wrapper, "提交 1 项审核").attributes("disabled"))
      .toBeUndefined();
    expect(buttonWithText(wrapper, "撤销最近一次审核").attributes("disabled"))
      .toBeDefined();
  });

  it("preserves review decisions and restores submit controls after failure", async () => {
    let reviewAttempts = 0;
    const api = fakeApi(async (body) => {
      if (body.task_type === "active_learning_queue") {
        return success(learningQueue());
      }
      if (body.task_type === "active_learning_review") {
        reviewAttempts += 1;
        if (reviewAttempts === 1) throw new Error("审核服务暂不可用");
        return success({
          batch_id: "retry-ok",
          applied: 1,
          failed: 0,
          skipped: 0,
          conflicts: 0,
          undo_available: true,
          failures: [],
        });
      }
      return success({});
    });
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    await wrapper
      .get('[data-learning-id="learn-1"] [data-decision="accept"]')
      .trigger("click");

    await buttonWithText(wrapper, "提交 1 项审核").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "提交 1 项审核").attributes("disabled"))
      .toBeUndefined();

    await buttonWithText(wrapper, "提交 1 项审核").trigger("click");
    await flushPromises();
    expect(reviewAttempts).toBe(2);
  });

  it("restores review controls after cancelling an active review job", async () => {
    const cancelJob = vi.fn(async () => ({
      job: { id: "review-running", status: "cancelled" },
    }));
    const api = fakeApi(
      async (body) =>
        body.task_type === "active_learning_queue"
          ? success(learningQueue())
          : { job: { id: "review-running", status: "queued" } },
      {
        job: vi.fn(async () => ({
          job: { id: "review-running", status: "running" },
        })),
        cancelJob,
      },
    );
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    await wrapper
      .get('[data-learning-id="learn-1"] [data-decision="accept"]')
      .trigger("click");

    void buttonWithText(wrapper, "提交 1 项审核").trigger("click");
    await flushPromises();
    await buttonWithText(wrapper, "取消").trigger("click");
    await flushPromises();

    expect(cancelJob).toHaveBeenCalledWith(
      "review-running",
      expect.any(AbortSignal),
    );
    expect(buttonWithText(wrapper, "提交 1 项审核").attributes("disabled"))
      .toBeUndefined();
  });

  it("offers accept reject edit and skip and opens the original image", async () => {
    const wrapper = mount(ActiveLearningPanel, {
      props: { api: fakeApi(), libraryId: "lib-1" },
    });
    await flushPromises();

    const decisions = wrapper.findAll('[data-learning-id="learn-1"] .single-decisions button');
    expect(decisions.map((value) => value.text())).toEqual(["接受", "拒绝", "编辑", "跳过"]);
    await wrapper.get('[data-learning-id="learn-1"] .learning-image').trigger("dblclick");
    expect(wrapper.emitted("openImage")).toEqual([["native-learn-1"]]);
  });

  it("rejects an invalid review budget without submitting", async () => {
    const api = fakeApi();
    const wrapper = mount(ActiveLearningPanel, {
      props: { api, libraryId: "lib-1" },
    });
    await flushPromises();
    vi.mocked(api.submitJob).mockClear();

    await wrapper.get('input[aria-label="本轮审核预算"]').setValue(31);
    await buttonWithText(wrapper, "生成待学习队列").trigger("click");
    await flushPromises();

    expect(api.submitJob).not.toHaveBeenCalled();
    expect(wrapper.emitted("toast")?.at(-1)?.[0]).toBe("审核预算无效");
  });
});
