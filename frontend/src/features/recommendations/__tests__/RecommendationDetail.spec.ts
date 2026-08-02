import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import RecommendationDetail from "../RecommendationDetail.vue";
import type { RecommendationItem } from "../types";

function item(): RecommendationItem {
  return {
    itemId: "item-1",
    mediaId: "media-1",
    name: "雷电将军.jpg",
    width: 1200,
    height: 1800,
    tags: ["角色", "紫色"],
    libraryId: "library-1",
    libraryName: "人物图库",
    contentType: "image/jpeg",
    sizeBytes: 4096,
    bucket: "quality",
    thumbnailUrl: "http://localhost/image/thumb",
    previewUrl: "http://localhost/image/preview",
    preference: "like",
  };
}

describe("RecommendationDetail", () => {
  it("shows only metadata present on a recommendation item", () => {
    const wrapper = mount(RecommendationDetail, { props: { item: item() } });

    expect(wrapper.get("img").attributes("src")).toBe("http://localhost/image/preview");
    expect(wrapper.text()).toContain("雷电将军.jpg");
    expect(wrapper.text()).toContain("人物图库");
    expect(wrapper.text()).toContain("技术质量");
    expect(wrapper.text()).toContain("1200 × 1800");
    expect(wrapper.text()).toContain("4 KB");
    expect(wrapper.text()).toContain("角色");
    expect(wrapper.text()).not.toContain("匹配度");
    expect(wrapper.text()).not.toContain("搜索排名");
    expect(wrapper.text()).not.toContain("查询相关性");
  });

  it("emits close, system-open, and reveal actions for the current media id", async () => {
    const wrapper = mount(RecommendationDetail, { props: { item: item() } });

    await wrapper.get(".recommendation-detail-close").trigger("click");
    const actionButtons = wrapper.findAll(".recommendation-detail-actions button");
    await actionButtons[0].trigger("click");
    await actionButtons[1].trigger("click");

    expect(wrapper.emitted("close")).toHaveLength(1);
    expect(wrapper.emitted("open")).toEqual([["media-1"]]);
    expect(wrapper.emitted("reveal")).toEqual([["media-1"]]);
  });
});
