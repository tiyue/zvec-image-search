import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import GalleryGrid from "../GalleryGrid.vue";
import { makeSearchItem } from "./fixtures";

describe("GalleryGrid", () => {
  const items = Array.from({ length: 15 }, (_, index) =>
    makeSearchItem({
      id: `image-${index + 1}`,
      name: `图片 ${index + 1}`,
      rank: index + 1,
      thumbnailUrl: `http://localhost/thumb-${index + 1}`,
    }),
  );

  it("renders one 15-image page and marks the selected result", () => {
    const wrapper = mount(GalleryGrid, {
      props: {
        items,
        selectedId: "image-7",
        selectedIds: ["image-7"],
        loading: false,
      },
    });

    expect(wrapper.findAll(".image-card-item")).toHaveLength(15);
    expect(wrapper.find('[data-image-id="image-7"]').classes()).toContain("is-selected");
    expect(wrapper.get('[role="list"]').attributes("aria-busy")).toBe("false");
  });

  it("forwards select and open events from a card", async () => {
    const wrapper = mount(GalleryGrid, {
      props: { items: items.slice(0, 1), loading: false },
    });

    await wrapper.get("button.image-card").trigger("click");
    await wrapper.get(".thumbnail-stage").trigger("dblclick");

    expect(wrapper.emitted("select")).toEqual([
      [{ id: "image-1", index: 0, additive: false, range: false }],
    ]);
    expect(wrapper.emitted("open")).toEqual([["image-1"]]);
  });

  it("keeps existing cards mounted beneath the loading overlay", () => {
    const wrapper = mount(GalleryGrid, {
      props: { items: items.slice(0, 4), loading: true, loadingText: "正在读取下一页" },
    });

    expect(wrapper.findAll(".image-card-item")).toHaveLength(4);
    expect(wrapper.get(".gallery-loading-overlay").text()).toContain("正在读取下一页");
    expect(wrapper.get('[role="list"]').attributes("aria-busy")).toBe("true");
  });

  it("renders an accessible empty state", () => {
    const wrapper = mount(GalleryGrid, {
      props: { items: [], loading: false },
    });

    expect(wrapper.get(".state-panel").attributes("role")).toBe("status");
    expect(wrapper.text()).toContain("暂无可显示的图片");
  });
});
