import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ImagePreview from "../ImagePreview.vue";
import { makeSearchItem } from "./fixtures";

describe("ImagePreview", () => {
  it("shows a placeholder and disables native actions without a selection", () => {
    const wrapper = mount(ImagePreview, {
      props: { item: null, highResolution: false, previewVisible: true },
    });

    expect(wrapper.text()).toContain("选择一张图片查看标签与详情");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.findAll("button").every((button) => button.element.disabled)).toBe(true);
  });

  it("shows tags, calibrated confidence and the original score", () => {
    const wrapper = mount(ImagePreview, {
      props: { item: makeSearchItem(), highResolution: false, previewVisible: true },
    });

    expect(wrapper.text()).toContain("命中标签");
    expect(wrapper.text()).toContain("雷电将军");
    expect(wrapper.text()).toContain("原始分数");
    expect(wrapper.text()).toContain("0.8123");
    expect(wrapper.text()).toContain("92.3%");
    expect(wrapper.text()).not.toContain("归一化分数");
    expect(wrapper.text()).toContain("2048 × 3072");
    expect(wrapper.text()).toContain("1.50 MB");
    expect(wrapper.text()).toContain("联合");
    expect(wrapper.text()).toContain("高度相关");
    expect(wrapper.find(".preview-overview").exists()).toBe(true);
    expect(wrapper.get(".preview-stage").classes()).toContain("preview-stage-compact");
    expect(wrapper.get(".preview-details-scroll").text()).toContain("命中标签");
    expect(wrapper.get(".preview-details-scroll").text()).toContain("匹配来源");
  });

  it("chooses thumbnail or full preview according to component props", async () => {
    const item = makeSearchItem();
    const wrapper = mount(ImagePreview, {
      props: { item, highResolution: false, previewVisible: true },
    });

    expect(wrapper.get("img").attributes("src")).toBe(item.thumbnailUrl);
    expect(wrapper.get("img").classes()).toContain("media-contain");
    expect(wrapper.get("img").attributes("fetchpriority")).toBe("auto");

    await wrapper.setProps({ highResolution: true });
    expect(wrapper.get("img").attributes("src")).toBe(item.imageUrl);
    expect(wrapper.get("img").attributes("fetchpriority")).toBe("high");

    await wrapper.setProps({ previewVisible: false });
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.text()).toContain("当前窗口不加载图片预览");
  });

  it("falls back to the thumbnail when the full preview URL is unavailable", () => {
    const item = makeSearchItem({ imageUrl: "" });
    const wrapper = mount(ImagePreview, {
      props: { item, highResolution: true, previewVisible: true },
    });

    expect(wrapper.get("img").attributes("src")).toBe(item.thumbnailUrl);
  });

  it("replaces a broken preview with a clear fallback", async () => {
    const wrapper = mount(ImagePreview, {
      props: { item: makeSearchItem(), highResolution: true, previewVisible: true },
    });

    await wrapper.get("img").trigger("error");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.text()).toContain("图片预览不可用");
  });

  it("emits open and reveal actions with the opaque image id", async () => {
    const wrapper = mount(ImagePreview, {
      props: { item: makeSearchItem(), highResolution: true, previewVisible: true },
    });
    const [openButton, revealButton] = wrapper.findAll("button");

    await openButton.trigger("click");
    await revealButton.trigger("click");

    expect(wrapper.emitted("open")).toEqual([["image-1"]]);
    expect(wrapper.emitted("reveal")).toEqual([["image-1"]]);
  });
});
