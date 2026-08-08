import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ImageEditPage from "../ImageEditPage.vue";

function mountPage(props: Record<string, unknown> = {}) {
  return mount(ImageEditPage, {
    attachTo: document.body,
    props,
  });
}

type PageWrapper = ReturnType<typeof mountPage>;

async function importFiles(wrapper: PageWrapper, files: File[]): Promise<void> {
  const input = wrapper.get("[data-testid='image-edit-file-input']");
  Object.defineProperty(input.element, "files", {
    configurable: true,
    value: files,
  });
  await input.trigger("change");
  await flushPromises();
}

describe("ImageEditPage local preview", () => {
  let wrapper: PageWrapper | null = null;
  let objectUrlSequence = 0;

  beforeEach(() => {
    objectUrlSequence = 0;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => `blob:image-edit-${objectUrlSequence += 1}`),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("imports one local file as a draft and exposes the exact 14-model catalog", async () => {
    wrapper = mountPage();
    expect(wrapper.text()).toContain("导入要编辑的图片");

    await importFiles(wrapper, [
      new File([new Uint8Array([1, 2, 3])], "portrait.png", { type: "image/png" }),
    ]);

    expect(wrapper.findAll(".task-row")).toHaveLength(1);
    expect(wrapper.get(".task-row").text()).toContain("portrait.png");
    expect(wrapper.get(".model-field select").element).toHaveProperty(
      "value",
      "qwen-image-edit-plus",
    );
    expect(wrapper.findAll(".model-field option")).toHaveLength(14);
    expect(wrapper.text()).toContain("前端本地预览");
    expect(wrapper.emitted("toast")?.[0]).toEqual([
      "图片已导入",
      "已建立 1 个本地草稿，尚未上传。",
      "success",
    ]);
  });

  it("rejects an import larger than 50 files without creating partial tasks", async () => {
    wrapper = mountPage();
    const files = Array.from({ length: 51 }, (_, index) =>
      new File([new Uint8Array([index])], `image-${index}.png`, { type: "image/png" }),
    );

    await importFiles(wrapper, files);

    expect(wrapper.findAll(".task-row")).toHaveLength(0);
    expect(wrapper.emitted("toast")?.[0]).toEqual([
      "没有导入图片",
      "单次最多导入 50 张，本次选择了 51 张。",
      "error",
    ]);
  });

  it("disables unsupported controls for qwen-image-edit", async () => {
    wrapper = mountPage();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "legacy.jpg", { type: "image/jpeg" }),
    ]);

    await wrapper.get(".model-field select").setValue("qwen-image-edit");

    const sizeSelect = wrapper.get(".advanced-grid select");
    const promptExtend = wrapper.findAll(".check-setting input")[0];
    expect(sizeSelect.attributes("disabled")).toBeDefined();
    expect(promptExtend.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("当前模型不支持指定尺寸");
    expect(wrapper.text()).toContain("当前模型不支持");
  });

  it("simulates the visible lifecycle locally and clearly labels the reused image", async () => {
    vi.useFakeTimers();
    wrapper = mountPage();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "street.webp", { type: "image/webp" }),
    ]);
    await wrapper.get(".prompt-field textarea").setValue("把背景改为雨后的东京街道");
    await wrapper.get(".consent-check input").setValue(true);

    await wrapper.get(".generate-button").trigger("click");
    expect(wrapper.get(".result-pane").text()).toContain("排队中");
    expect((wrapper.get(".consent-check input").element as HTMLInputElement).checked).toBe(false);

    await vi.advanceTimersByTimeAsync(1000);
    expect(wrapper.get(".result-pane").text()).toContain("正在生成");

    await vi.advanceTimersByTimeAsync(2000);
    expect(wrapper.get(".result-pane").text()).toContain("已保存（模拟）");
    expect(wrapper.get(".simulation-label").text()).toContain("不代表模型效果");
    expect(wrapper.findAll(".result-pane img")).toHaveLength(1);
  });

  it("accepts a single search-result source as an independent draft", async () => {
    wrapper = mountPage({
      incomingSource: {
        key: "search-image-1",
        name: "搜索结果.jpg",
        url: "http://127.0.0.1:8765/api/images/search-image-1/preview",
        sizeBytes: 2048,
      },
    });
    await flushPromises();

    expect(wrapper.findAll(".task-row")).toHaveLength(1);
    expect(wrapper.get(".task-row").text()).toContain("搜索结果.jpg");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});
