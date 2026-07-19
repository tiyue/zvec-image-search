import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.vue";

const nativeActionMocks = vi.hoisted(() => ({
  selectQueryImage: vi.fn().mockResolvedValue(null),
  open: vi.fn().mockResolvedValue(true),
  reveal: vi.fn().mockResolvedValue(true),
  copyImage: vi.fn().mockResolvedValue(true),
  copyFiles: vi.fn().mockResolvedValue(true),
  copyPaths: vi.fn().mockResolvedValue(true),
  exportImages: vi.fn().mockResolvedValue(true),
  registerQueryImage: vi.fn().mockResolvedValue(null),
  exporting: { value: false },
  exportJob: { value: null },
}));

vi.mock("../composables/useNativeImageActions", () => ({
  useNativeImageActions: () => nativeActionMocks,
}));

const TasksStub = defineComponent({
  name: "TasksPage",
  props: {
    visible: { type: Boolean, default: true },
  },
  emits: ["toast"],
  setup(props) {
    return () =>
      h(
        "section",
        {
          "data-testid": "tasks-page",
          "data-visible": String(props.visible),
        },
        "图库任务内容",
      );
  },
});

const OrganizeStub = defineComponent({
  name: "OrganizePage",
  emits: ["toast", "open-image"],
  setup(_props, { emit }) {
    return () =>
      h("section", { "data-testid": "organize-page" }, [
        "智能整理内容",
        h(
          "button",
          {
            type: "button",
            "data-testid": "organize-open-image",
            onClick: () => emit("open-image", "organize-image-1"),
          },
          "打开图片",
        ),
      ]);
  },
});

const SettingsStub = defineComponent({
  name: "SettingsPage",
  emits: ["toast", "libraries-updated"],
  setup(_props, { emit }) {
    return () =>
      h("section", { "data-testid": "settings-page" }, [
        h("input", { name: "image_root", value: "D:\\图片数据库" }),
        h("input", { name: "results_directory", value: "D:\\Zvec\\results" }),
        h(
          "button",
          {
            type: "button",
            "data-testid": "publish-libraries",
            onClick: () =>
              emit("libraries-updated", [
                {
                  id: "library-new",
                  name: "新图库",
                  imageRoot: "D:\\新图库",
                  workspaceDirectory: "D:\\Zvec\\workspace",
                  enabled: true,
                  isDefault: true,
                },
              ]),
          },
          "保存图库",
        ),
      ]);
  },
});

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function mountApp(): VueWrapper {
  return mount(App, {
    attachTo: document.body,
    global: {
      stubs: {
        TasksPage: TasksStub,
        OrganizePage: OrganizeStub,
        SettingsPage: SettingsStub,
      },
    },
  });
}

describe("App page shell", () => {
  let wrapper: VueWrapper | null = null;
  let latestPayload: Record<string, unknown>;

  beforeEach(() => {
    vi.clearAllMocks();
    nativeActionMocks.selectQueryImage.mockResolvedValue(null);
    nativeActionMocks.registerQueryImage.mockResolvedValue(null);
    nativeActionMocks.exporting.value = false;
    nativeActionMocks.exportJob.value = null;
    latestPayload = {
      id: "latest",
      status: "succeeded",
      page: 1,
      page_size: 15,
      total_items: 0,
      total_pages: 0,
      items: [],
    };
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: true,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("api/bootstrap")) {
          return Promise.resolve(
            jsonResponse({
              service: { status: "ready", ready: true },
              libraries: [{ id: "library-1", name: "图片数据库", enabled: true }],
            }),
          );
        }
        return Promise.resolve(jsonResponse(latestPayload));
      }),
    );
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    vi.unstubAllGlobals();
  });

  it("starts on the compact search page and exposes all four navigation items", async () => {
    wrapper = mountApp();
    await flushPromises();

    expect(wrapper.findAll(".nav-item")).toHaveLength(4);
    expect(wrapper.get(".nav-item[data-page='search']").attributes("aria-current")).toBe("page");
    expect(wrapper.get("[data-page-section='search']").isVisible()).toBe(true);
    expect(wrapper.find("[data-page-section='search'] .page-heading").exists()).toBe(false);
    expect(wrapper.find("input[aria-label='本次搜索图片数量']").exists()).toBe(true);
    expect(wrapper.get(".toolbar-selection-count").text()).toContain("已选择 0 张");
    expect(wrapper.get(".sidebar-footer").attributes("aria-label")).toBe("本地服务状态");
    expect(wrapper.get("[data-testid='cleanup-search-results']").text()).toContain(
      "默认保留最近 3 次",
    );
    expect(wrapper.find(".gallery-pagination").exists()).toBe(true);
    expect(wrapper.find("button[aria-label='首页']").exists()).toBe(true);
    expect(wrapper.find("button[aria-label='末页']").exists()).toBe(true);
  });

  it("runs search-result cleanup from the global quick actions and shows its summary", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("api/bootstrap")) {
        return Promise.resolve(
          jsonResponse({
            service: { status: "ready", ready: true },
            libraries: [{ id: "library-1", name: "图片数据库", enabled: true }],
          }),
        );
      }
      if (url === "api/jobs" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse({
            job: {
              id: "cleanup-now",
              status: "succeeded",
              result: { deleted: 12, skipped: 2, failed: 1 },
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse(latestPayload));
    });
    wrapper = mountApp();
    await flushPromises();

    await wrapper.get("[data-testid='cleanup-search-results']").trigger("click");
    await flushPromises();

    const request = fetchMock.mock.calls.find(
      ([input, init]) => String(input) === "api/jobs" && init?.method === "POST",
    );
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      task_type: "search_results_cleanup",
      keep_latest: 3,
    });
    expect(wrapper.get("[data-testid='cleanup-search-results-status']").text()).toContain(
      "清理完成",
    );
    expect(wrapper.get("[data-testid='cleanup-search-results-status']").text()).toContain(
      "删除 12",
    );
    expect(wrapper.get("[data-testid='cleanup-search-results-status']").text()).toContain(
      "跳过 2",
    );
    expect(wrapper.get("[data-testid='cleanup-search-results-status']").text()).toContain(
      "失败 1",
    );
  });

  it("keeps the requested result count uncapped without a redundant fixed-page summary", async () => {
    wrapper = mountApp();
    await flushPromises();

    const limit = wrapper.get("input[aria-label='本次搜索图片数量']");
    expect(limit.attributes("type")).toBe("number");
    expect(limit.attributes("min")).toBe("1");
    expect(limit.attributes("max")).toBeUndefined();
    await limit.setValue("1000000");
    expect((limit.element as HTMLInputElement).value).toBe("1000000");
    expect(wrapper.find(".search-summary").exists()).toBe(false);
    expect(wrapper.find(".summary-metrics").exists()).toBe(false);
    expect(wrapper.get("#gallery-title").text()).toContain("相关图片 · 0 张");
    expect(wrapper.get(".status-pill").attributes("aria-live")).toBe("polite");
    expect(wrapper.get(".status-pill").text()).toContain("无可靠结果");
  });

  it("keeps running, cancelled, and failed search states visible in the compact live region", async () => {
    const fetchMock = vi.mocked(fetch);
    let searchAttempt = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("api/bootstrap")) {
        return Promise.resolve(
          jsonResponse({
            service: { status: "ready", ready: true },
            libraries: [{ id: "library-1", name: "图片数据库", enabled: true }],
          }),
        );
      }
      if (url === "api/search") {
        searchAttempt += 1;
        if (searchAttempt === 1) return new Promise<Response>(() => undefined);
        return Promise.reject(new Error("temporary failure"));
      }
      return Promise.resolve(jsonResponse(latestPayload));
    });
    wrapper = mountApp();
    await flushPromises();
    await wrapper.get("input[type='search']").setValue("原神");

    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await wrapper.vm.$nextTick();
    expect(wrapper.get(".status-pill").text()).toContain("搜索中");
    expect(wrapper.get(".status-pill").attributes("role")).toBe("status");

    await wrapper.get(".search-actions .button-quiet").trigger("click");
    await flushPromises();
    expect(wrapper.get(".status-pill").text()).toContain("已取消");
    expect(wrapper.get(".status-pill .sr-only").text()).toContain("搜索已停止");

    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await flushPromises();
    expect(wrapper.get(".status-pill").text()).toContain("搜索失败");
    expect(wrapper.get(".status-pill .sr-only").text()).toContain("无法连接本地服务");
  });

  it("opens an editable settings surface and synchronizes saved libraries back to search", async () => {
    wrapper = mountApp();
    await flushPromises();

    await wrapper.get(".nav-item[data-page='settings']").trigger("click");
    expect(wrapper.get("[data-page-section='settings']").isVisible()).toBe(true);
    expect(wrapper.find("[data-testid='settings-page'] input[name='image_root']").exists()).toBe(true);
    expect(
      wrapper.find("[data-testid='settings-page'] input[name='results_directory']").exists(),
    ).toBe(true);

    await wrapper.get("[data-testid='publish-libraries']").trigger("click");
    await wrapper.get(".nav-item[data-page='search']").trigger("click");
    expect(wrapper.get("select option[value='library-new']").text()).toBe("新图库");
  });

  it("forwards organize image open requests to the native image action", async () => {
    wrapper = mountApp();
    await flushPromises();

    await wrapper.get(".nav-item[data-page='organize']").trigger("click");
    await wrapper.get("[data-testid='organize-open-image']").trigger("click");

    expect(nativeActionMocks.open).toHaveBeenCalledWith("organize-image-1");
  });

  it("unmounts the detail preview in the small-window scroll layout", async () => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    wrapper = mountApp();
    await flushPromises();

    expect(wrapper.find(".preview-card").exists()).toBe(false);
    expect(wrapper.find(".gallery-panel").exists()).toBe(true);
  });

  it("supports Alt+1 through Alt+4 plus slash, Ctrl+K and Ctrl+G", async () => {
    latestPayload = {
      ...latestPayload,
      total_items: 30,
      total_pages: 2,
    };
    wrapper = mountApp();
    await flushPromises();

    const destinations = ["search", "tasks", "organize", "settings"];
    for (const [index, page] of destinations.entries()) {
      const digit = String(index + 1);
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: digit, code: `Digit${digit}`, altKey: true }),
      );
      await wrapper.vm.$nextTick();
      expect(wrapper.get(`[data-page-section='${page}']`).isVisible()).toBe(true);
      expect(wrapper.get(`.nav-item[data-page='${page}']`).attributes("aria-current")).toBe("page");
    }

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "/" }));
    await wrapper.vm.$nextTick();
    await wrapper.vm.$nextTick();
    expect(wrapper.get("[data-page-section='search']").isVisible()).toBe(true);
    expect(document.activeElement).toBe(wrapper.get("input[type='search']").element);

    await wrapper.get("input[type='search']").setValue("雷电将军");
    await wrapper.get(".nav-item[data-page='settings']").trigger("click");
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }));
    await wrapper.vm.$nextTick();
    await wrapper.vm.$nextTick();
    const searchInput = wrapper.get("input[type='search']").element as HTMLInputElement;
    expect(wrapper.get("[data-page-section='search']").isVisible()).toBe(true);
    expect(document.activeElement).toBe(searchInput);
    expect(searchInput.selectionStart).toBe(0);
    expect(searchInput.selectionEnd).toBe(searchInput.value.length);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "g", ctrlKey: true }));
    await wrapper.vm.$nextTick();
    await wrapper.vm.$nextTick();
    expect(document.activeElement).toBe(wrapper.get("input[aria-label='跳转页码']").element);
  });

  it("tells the task page to stop live polling while it is hidden", async () => {
    wrapper = mountApp();
    await flushPromises();

    await wrapper.get(".nav-item[data-page='tasks']").trigger("click");
    expect(wrapper.get("[data-testid='tasks-page']").attributes("data-visible")).toBe("true");

    await wrapper.get(".nav-item[data-page='search']").trigger("click");
    expect(wrapper.get("[data-testid='tasks-page']").attributes("data-visible")).toBe("false");
  });

  it("connects gallery double click and preview folder action to the native bridge", async () => {
    latestPayload = {
      id: "latest-with-image",
      status: "succeeded",
      page: 1,
      page_size: 15,
      total_items: 1,
      total_pages: 1,
      items: [
        {
          id: "image-1",
          name: "示例人物写真.jpg",
          relative_path: "人物写真/示例人物写真.jpg",
          library_name: "图片数据库",
          rank: 1,
          match_state: "high",
          rank_source: "fused",
          thumbnail_url: "http://127.0.0.1:8765/api/images/image-1/thumbnail",
          image_url: "http://127.0.0.1:8765/api/images/image-1/preview",
        },
      ],
    };
    wrapper = mountApp();
    await flushPromises();

    await wrapper.get(".thumbnail-stage").trigger("dblclick");
    expect(nativeActionMocks.open).toHaveBeenCalledWith("image-1");

    await wrapper.get(".preview-actions .button-quiet").trigger("click");
    expect(nativeActionMocks.reveal).toHaveBeenCalledWith("image-1");
  });

  it("accepts a dropped image for combined search without exposing its path", async () => {
    wrapper = mountApp();
    await flushPromises();
    nativeActionMocks.registerQueryImage.mockResolvedValue({
      id: "query-drop-1",
      name: "粘贴图片.png",
    });
    await wrapper.get("select").setValue("combined");
    const file = new File([new Uint8Array([1, 2, 3])], "粘贴图片.png", {
      type: "image/png",
    });

    await wrapper.get(".query-image-control").trigger("drop", {
      dataTransfer: { files: [file] },
    });
    await flushPromises();

    expect(nativeActionMocks.registerQueryImage).toHaveBeenCalledWith(file);
    expect(wrapper.get(".query-image-chip").text()).toContain("粘贴图片.png");
  });

  it("keeps Ctrl multi-selection and exposes right-click batch actions", async () => {
    latestPayload = {
      id: "latest-multi",
      status: "succeeded",
      page: 1,
      page_size: 15,
      total_items: 2,
      total_pages: 1,
      items: [
        {
          id: "image-1",
          name: "图片一.jpg",
          relative_path: "人物/图片一.jpg",
          library_name: "图片数据库",
          rank: 1,
          confidence: 0.92,
          raw_score: 0.08,
          thumbnail_url: "api/image/image-1?variant=thumbnail",
        },
        {
          id: "image-2",
          name: "图片二.jpg",
          relative_path: "人物/图片二.jpg",
          library_name: "图片数据库",
          rank: 2,
          confidence: 0.88,
          raw_score: 0.12,
          thumbnail_url: "api/image/image-2?variant=thumbnail",
        },
      ],
    };
    wrapper = mountApp();
    await flushPromises();
    const cards = wrapper.findAll("button.image-card");

    await cards[0].trigger("click");
    await cards[1].trigger("click", { ctrlKey: true });
    expect(wrapper.get(".toolbar-selection-count").text()).toContain("已选择 2 张");
    expect(wrapper.get(".toolbar-selection-count").attributes("aria-label")).toBe(
      "已选择 2 张图片",
    );
    expect(wrapper.find(".panel-heading .selection-count").exists()).toBe(false);

    await cards[1].trigger("contextmenu", { clientX: 400, clientY: 300 });
    const menuButtons = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>(".gallery-context-menu button"),
    );
    const copyFiles = menuButtons.find((button) => button.textContent?.includes("复制所选文件"));
    expect(copyFiles).toBeDefined();
    copyFiles?.click();
    await flushPromises();

    expect(nativeActionMocks.copyFiles).toHaveBeenCalledWith(["image-1", "image-2"]);
  });
});
