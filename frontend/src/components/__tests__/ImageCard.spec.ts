import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ImageCard from "../ImageCard.vue";
import { makeSearchItem } from "./fixtures";

describe("ImageCard", () => {
  it("emits selection on click and open on thumbnail double click", async () => {
    const item = makeSearchItem();
    const wrapper = mount(ImageCard, {
      props: { item, index: 0, selected: false },
    });

    await wrapper.get("button.image-card").trigger("click");
    await wrapper.get(".thumbnail-stage").trigger("dblclick");

    expect(wrapper.emitted("select")).toEqual([
      [{ id: item.id, index: 0, additive: false, range: false }],
    ]);
    expect(wrapper.emitted("open")).toEqual([[item.id]]);
  });

  it("reports Ctrl/Shift selection intent and right-click coordinates", async () => {
    const item = makeSearchItem();
    const wrapper = mount(ImageCard, {
      props: { item, index: 4, selected: true, primary: true },
    });

    await wrapper.get("button.image-card").trigger("click", { ctrlKey: true, shiftKey: true });
    await wrapper.get("button.image-card").trigger("contextmenu", { clientX: 320, clientY: 240 });

    expect(wrapper.emitted("select")).toEqual([
      [{ id: item.id, index: 4, additive: true, range: true }],
    ]);
    expect(wrapper.emitted("context")).toEqual([
      [{ id: item.id, index: 4, clientX: 320, clientY: 240 }],
    ]);
    expect(wrapper.get(".selection-check").text()).toBe("✓");
  });

  it("renders a selected, non-cropping eager thumbnail with priority", () => {
    const wrapper = mount(ImageCard, {
      props: { item: makeSearchItem({ rank: 3 }), index: 2, selected: true },
    });
    const card = wrapper.get("button.image-card");
    const image = wrapper.get("img");

    expect(card.classes()).toContain("is-selected");
    expect(card.attributes("aria-pressed")).toBe("true");
    expect(image.classes()).toContain("media-contain");
    expect(image.attributes("loading")).toBe("eager");
    expect(image.attributes("decoding")).toBe("async");
    expect(image.attributes("fetchpriority")).toBe("high");
    expect(wrapper.get(".card-rank").text()).toBe("03");
    expect(wrapper.get(".card-rank").attributes("aria-label")).toBe("排名 3");
  });

  it("loads later thumbnails lazily at low priority", () => {
    const wrapper = mount(ImageCard, {
      props: { item: makeSearchItem(), index: 8, selected: false },
    });
    const image = wrapper.get("img");

    expect(image.attributes("loading")).toBe("lazy");
    expect(image.attributes("fetchpriority")).toBe("low");
  });

  it("shows a safe fallback after an image error and resets for a new source", async () => {
    const wrapper = mount(ImageCard, {
      props: { item: makeSearchItem(), index: 0, selected: false },
    });

    await wrapper.get("img").trigger("error");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.get('[data-testid="thumbnail-fallback"]').attributes("aria-label")).toContain(
      "缩略图不可用",
    );

    await wrapper.setProps({
      item: makeSearchItem({ id: "image-2", thumbnailUrl: "http://localhost/new-thumbnail" }),
    });
    expect(wrapper.get("img").attributes("src")).toBe("http://localhost/new-thumbnail");
  });

  it("uses the fallback immediately when no image URL exists", () => {
    const wrapper = mount(ImageCard, {
      props: {
        item: makeSearchItem({ thumbnailUrl: "", imageUrl: "" }),
        index: 0,
        selected: false,
      },
    });

    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find('[data-testid="thumbnail-fallback"]').exists()).toBe(true);
  });
});
