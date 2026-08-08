import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ImageEditPage from "../ImageEditPage.vue";
import { validateImageEditDimensions } from "../modelCatalog";
import type { ImageEditTaskWire } from "../types";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function taskWire(
  status: ImageEditTaskWire["status"],
  overrides: Partial<ImageEditTaskWire> = {},
): ImageEditTaskWire {
  return {
    id: "a".repeat(32),
    source_name: "portrait.png",
    submitted_at: "2026-08-08T00:00:00.000Z",
    status,
    stage: status,
    model: "qwen-image-edit-plus",
    prompt: "修改背景",
    negative_prompt: "",
    size: null,
    seed: null,
    prompt_extend: true,
    error: null,
    result: null,
    ...overrides,
  };
}

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

describe("ImageEditPage", () => {
  let wrapper: PageWrapper | null = null;
  let objectUrlSequence = 0;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    objectUrlSequence = 0;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => `blob:image-edit-${(objectUrlSequence += 1)}`),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "api/image-edit/settings" && method === "GET") {
        return jsonResponse({
          configured: true,
          output_directory: "D:\\Pictures\\Qwen 编辑结果",
        });
      }
      if (url === "api/image-edit/settings" && method === "PUT") {
        const body = JSON.parse(String(init?.body)) as {
          output_directory: string;
        };
        return jsonResponse({
          configured: true,
          output_directory: body.output_directory,
        });
      }
      if (url === "api/image-edit/tasks" && method === "POST") {
        return jsonResponse({ task: taskWire("queued") }, 202);
      }
      if (url === "api/image-edit/tasks?limit=200") {
        return jsonResponse({
          tasks: [taskWire("queued")],
          count: 1,
          total_count: 1,
        });
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("imports one file as a draft and exposes the exact 14-model catalog", async () => {
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1, 2, 3])], "portrait.png", {
        type: "image/png",
      }),
    ]);

    expect(wrapper.findAll(".task-row")).toHaveLength(1);
    expect(wrapper.get(".model-field select").element).toHaveProperty(
      "value",
      "qwen-image-edit-plus",
    );
    expect(wrapper.findAll(".model-field option")).toHaveLength(14);
    expect(wrapper.findAll(".ratio-field option")).toHaveLength(10);
    expect(wrapper.findAll(".size-field option")).toHaveLength(1);
    expect(wrapper.text()).not.toContain("添加 Qwen-Image 水印");
    expect(wrapper.text()).not.toContain("我已知晓");
    expect(wrapper.text()).not.toContain("前端本地预览");
    expect(wrapper.text()).toContain("D:\\Pictures\\Qwen 编辑结果");
    expect(wrapper.emitted("toast")?.[0]).toEqual([
      "图片已导入",
      "已建立 1 个本地草稿，尚未上传。",
      "success",
    ]);
  });

  it("rejects more than 50 imported files without creating partial tasks", async () => {
    wrapper = mountPage();
    await flushPromises();
    const files = Array.from(
      { length: 51 },
      (_, index) =>
        new File([new Uint8Array([index])], `image-${index}.png`, {
          type: "image/png",
        }),
    );

    await importFiles(wrapper, files);

    expect(wrapper.findAll(".task-row")).toHaveLength(0);
    expect(wrapper.emitted("toast")?.[0]).toEqual([
      "没有导入图片",
      "单次最多导入 50 张，本次选择了 51 张。",
      "error",
    ]);
  });

  it("disables size and prompt-extension controls for qwen-image-edit", async () => {
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "legacy.jpg", { type: "image/jpeg" }),
    ]);

    await wrapper.get(".model-field select").setValue("qwen-image-edit");

    expect(
      wrapper.get(".ratio-field select").attributes("disabled"),
    ).toBeDefined();
    expect(
      wrapper.get(".size-field select").attributes("disabled"),
    ).toBeDefined();
    expect(
      wrapper.findAll(".check-setting input")[0]?.attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.text()).toContain("当前模型不支持指定尺寸");
  });

  it("uses the documented exact sizes for every selected aspect ratio", async () => {
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "ratio.png", { type: "image/png" }),
    ]);

    await wrapper.get(".ratio-field select").setValue("21:9");
    expect(
      wrapper
        .findAll(".size-field option")
        .map((option) => option.attributes("value")),
    ).toEqual(["1344*576", "2048*872"]);

    await wrapper.get(".model-field select").setValue("qwen-image-3.0");
    await wrapper.get(".ratio-field select").setValue("custom");
    expect(wrapper.findAll(".size-inputs input")[0]?.attributes("min")).toBe(
      "1",
    );
    expect(wrapper.text()).toContain("总像素为 512×512–2048×2048");

    await wrapper.get(".model-field select").setValue("qwen-image-edit-max");
    expect(wrapper.findAll(".size-inputs input")[0]?.attributes("min")).toBe(
      "512",
    );
    expect(wrapper.findAll(".size-inputs input")[0]?.attributes("max")).toBe(
      "2048",
    );

    expect(
      validateImageEditDimensions("qwen-image-3.0-pro", 4097, 512),
    ).toContain("1:8");
    expect(
      validateImageEditDimensions("qwen-image-2.0-pro", 4097, 512),
    ).toBeNull();
  });

  it("submits manually and follows the real backend lifecycle to a saved result", async () => {
    vi.useFakeTimers();
    let listCount = 0;
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "api/image-edit/settings") {
          return jsonResponse({
            configured: true,
            output_directory: "D:\\Output",
          });
        }
        if (url === "api/image-edit/tasks" && method === "POST") {
          expect(init?.body).toBeInstanceOf(FormData);
          const metadata = JSON.parse(
            String((init?.body as FormData).get("metadata")),
          ) as { seed?: number };
          expect(metadata.seed).toBe(1234);
          return jsonResponse({ task: taskWire("queued") }, 202);
        }
        if (url === "api/image-edit/tasks?limit=200") {
          listCount += 1;
          const task =
            listCount === 1
              ? taskWire("generating")
              : taskWire("succeeded", {
                  result: {
                    output_filename: "portrait-qwen-edit.png",
                    image_url: "api/image/result-id?variant=preview",
                  },
                });
          return jsonResponse({ tasks: [task], count: 1, total_count: 1 });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "portrait.png", { type: "image/png" }),
    ]);
    await wrapper
      .get(".prompt-field textarea")
      .setValue("把背景改成雨后的东京街道");
    await wrapper.get("input[placeholder='随机']").setValue("1234");

    await wrapper.get(".generate-button").trigger("click");
    await flushPromises();
    expect(wrapper.get(".canvas-status").text()).toBe("排队中");

    await vi.advanceTimersByTimeAsync(300);
    await flushPromises();
    expect(wrapper.get(".canvas-status").text()).toBe("正在生成");

    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(wrapper.get(".canvas-status").text()).toBe("已保存");
    expect(wrapper.get(".result-label").text()).toContain(
      "portrait-qwen-edit.png",
    );
    expect(wrapper.findAll(".canvas-mode button")[1]?.classes()).toContain(
      "active",
    );
  });

  it("shows the seed validation error from the explicit generate click", async () => {
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "portrait.png", { type: "image/png" }),
    ]);
    await wrapper.get(".prompt-field textarea").setValue("修改背景");
    await wrapper
      .get("input[placeholder='随机']")
      .setValue("2147483648");

    await wrapper.get(".generate-button").trigger("click");
    await flushPromises();

    expect(wrapper.get(".form-error").text()).toContain(
      "随机种子必须是 0 到 2147483647 之间的整数",
    );
    expect(
      fetchMock.mock.calls.some(([input, init]) =>
        String(input) === "api/image-edit/tasks" && init?.method === "POST"),
    ).toBe(false);
  });

  it("keeps the source and prompt after a provider failure so the task can retry", async () => {
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "api/image-edit/settings") {
          return jsonResponse({
            configured: true,
            output_directory: "D:\\Output",
          });
        }
        if (url === "api/image-edit/tasks" && init?.method === "POST") {
          return jsonResponse(
            {
              error: {
                code: "Throttling",
                message: "请求触发阿里云限流，请降低提交频率后重试。",
              },
            },
            429,
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    wrapper = mountPage();
    await flushPromises();
    await importFiles(wrapper, [
      new File([new Uint8Array([1])], "retry.png", { type: "image/png" }),
    ]);
    const prompt = "保留人物，把背景换成海边";
    await wrapper.get(".prompt-field textarea").setValue(prompt);

    await wrapper.get(".prompt-composer").trigger("submit");
    await flushPromises();

    expect(wrapper.get(".prompt-field textarea").element).toHaveProperty(
      "value",
      prompt,
    );
    expect(wrapper.get(".image-canvas img").attributes("src")).toBe(
      "blob:image-edit-1",
    );
    expect(wrapper.get(".form-error").text()).toContain("阿里云限流");
    expect(wrapper.get(".generate-button").text()).toContain("重新生成");
  });

  it("submits a search-result image by opaque registry ID", async () => {
    wrapper = mountPage({
      incomingSource: {
        key: "search-image-1",
        imageId: "registry-image-1",
        name: "搜索结果.jpg",
        url: "api/image/registry-image-1?variant=preview",
        sizeBytes: 2048,
      },
    });
    await flushPromises();
    await wrapper.get(".prompt-field textarea").setValue("改成黑白胶片风格");

    await wrapper.get(".prompt-composer").trigger("submit");
    await flushPromises();

    const submission = fetchMock.mock.calls.find(
      (call) =>
        String(call[0]) === "api/image-edit/tasks" &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(submission?.[1]?.body))).toMatchObject({
      source_image_id: "registry-image-1",
      model: "qwen-image-edit-plus",
      prompt: "改成黑白胶片风格",
      prompt_extend: true,
    });
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});
