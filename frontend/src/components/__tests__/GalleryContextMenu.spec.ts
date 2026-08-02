import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it } from "vitest";

import GalleryContextMenu from "../GalleryContextMenu.vue";

function mountMenu(props: Record<string, unknown> = {}) {
  return mount(GalleryContextMenu, {
    attachTo: document.body,
    props: {
      visible: true,
      x: 24,
      y: 32,
      selectionCount: 1,
      exporting: false,
      feedbackAvailable: true,
      ...props,
    },
  });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("GalleryContextMenu", () => {
  it("preserves the search selection and relevance wording by default", () => {
    const wrapper = mountMenu({ feedbackAction: "relevant" });

    expect(document.body.textContent).toContain("已选择 1 张");
    expect(document.body.textContent).toContain("✓ 已标记为相关");
    expect(document.body.textContent).toContain("标记为不相关");
    expect(document.body.textContent).toContain("复制所选文件");
    expect(document.body.textContent).toContain("清除选择");
    expect(wrapper.emitted("like")).toBeUndefined();
  });

  it("renders single-image recommendation actions without search selection controls", () => {
    mountMenu({ mode: "recommendation", feedbackAction: "like" });

    expect(document.body.textContent).not.toContain("已选择");
    expect(document.body.textContent).not.toContain("相关");
    expect(document.body.textContent).not.toContain("清除选择");
    expect(document.body.textContent).toContain("✓ 已喜欢");
    expect(document.body.textContent).toContain("不喜欢");
    expect(document.body.textContent).toContain("复制文件");
  });

  it("emits recommendation preference actions and closes the menu", async () => {
    const wrapper = mountMenu({ mode: "recommendation" });
    const buttons = Array.from(document.body.querySelectorAll("button"));
    const dislike = buttons.find((button) => button.textContent?.trim() === "不喜欢");

    expect(dislike).toBeDefined();
    dislike?.click();
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("dislike")).toHaveLength(1);
    expect(wrapper.emitted("close")).toHaveLength(1);
  });

  it("disables same-image native and preference operations while pending", () => {
    mountMenu({
      mode: "recommendation",
      actionPending: true,
      feedbackPending: true,
    });
    const buttons = Array.from(document.body.querySelectorAll("button"));
    const actionLabels = new Set([
      "系统打开",
      "所在文件夹",
      "喜欢",
      "不喜欢",
      "复制图片",
      "复制文件",
      "复制文件路径",
      "导出到文件夹…",
    ]);

    expect(buttons
      .filter((button) => actionLabels.has(button.textContent?.trim() ?? ""))
      .every((button) => button.disabled)).toBe(true);
  });
});
