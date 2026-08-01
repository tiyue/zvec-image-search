import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RecommendationBatch } from "../types";

const state = vi.hoisted(() => ({
  currentBatch: { value: null as RecommendationBatch | null },
  loading: { value: false },
  error: { value: "" },
  shownError: { value: "" },
  visible: { value: false },
  shownPending: { value: false },
  refresh: vi.fn().mockResolvedValue(true),
  recordAction: vi.fn().mockResolvedValue(true),
  isActionPending: vi.fn().mockReturnValue(false),
  preferenceFor: vi.fn().mockReturnValue(""),
  setVisible: vi.fn(),
  retryShown: vi.fn().mockResolvedValue(true),
}));

vi.mock("../useRecommendations", () => ({
  useRecommendations: () => state,
}));

import RecommendationPage from "../RecommendationPage.vue";

function batch(): RecommendationBatch {
  return {
    requestId: "request-1",
    batchId: "batch-1",
    count: 1,
    partial: true,
    partialReason: "insufficient_candidates",
    quotaDegraded: true,
    historyWindow: 45,
    quota: { quality: 5, recent: 4, low_exposure: 4, random: 2 },
    diversity: {
      applied: true,
      reason: "",
      missingVectors: 2,
      vectorSpace: "clip-test",
    },
    items: [{
      itemId: "item-1",
      mediaId: "media-1",
      name: "雷电将军.jpg",
      width: 1200,
      height: 1800,
      tags: ["角色"],
      libraryId: "library-1",
      libraryName: "人物图库",
      contentType: "image/jpeg",
      sizeBytes: 4096,
      bucket: "quality",
      thumbnailUrl: "http://localhost/api/image/media-1?variant=thumbnail",
      previewUrl: "http://localhost/api/image/media-1?variant=preview",
    }],
  };
}

describe("RecommendationPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.currentBatch.value = null;
    state.loading.value = false;
    state.error.value = "";
    state.shownError.value = "";
    state.preferenceFor.mockReturnValue("");
  });

  it("shows fifteen skeletons during the first atomic preload", () => {
    state.loading.value = true;
    const wrapper = mount(RecommendationPage, { props: { visible: true } });

    expect(wrapper.findAll(".recommendation-skeleton")).toHaveLength(15);
    expect(state.setVisible).toHaveBeenCalledWith(true);
  });

  it("renders partial and vector diversity state and records successful actions", async () => {
    state.currentBatch.value = batch();
    const openImage = vi.fn().mockResolvedValue(true);
    const exportImage = vi.fn().mockResolvedValue(true);
    const wrapper = mount(RecommendationPage, {
      props: { visible: true, openImage, exportImage },
    });

    expect(wrapper.get(".recommendation-status").text()).toContain("本批数量不足 15 张");
    expect(wrapper.get(".recommendation-status").text()).toContain("向量多样性已应用，2 张缺少向量");
    expect(wrapper.get(".recommendation-media span").text()).toBe("技术质量");

    const buttons = wrapper.findAll(".recommendation-actions button");
    await buttons[0].trigger("click");
    await flushPromises();
    expect(openImage).toHaveBeenCalledWith("media-1");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "open");

    await buttons[1].trigger("click");
    await buttons[2].trigger("click");
    await buttons[3].trigger("click");
    await flushPromises();
    expect(exportImage).toHaveBeenCalledWith("media-1");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "like");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "export");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "dislike");
  });

  it("does not record open or export when the native operation fails", async () => {
    state.currentBatch.value = batch();
    const wrapper = mount(RecommendationPage, {
      props: {
        visible: true,
        openImage: vi.fn().mockResolvedValue(false),
        exportImage: vi.fn().mockResolvedValue(false),
      },
    });
    const buttons = wrapper.findAll(".recommendation-actions button");

    await buttons[0].trigger("click");
    await buttons[2].trigger("click");
    await flushPromises();

    expect(state.recordAction).not.toHaveBeenCalledWith("item-1", "open");
    expect(state.recordAction).not.toHaveBeenCalledWith("item-1", "export");
  });
});
