import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.vue";

const state = vi.hoisted(() => {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    gate,
    release: () => release(),
    taskFactoryCalls: 0,
    taskMounts: 0,
    settingsFactoryCalls: 0,
  };
});

// The task page waits on a gate so the loading surface can be asserted
// deterministically; the settings page always fails to load.
vi.mock("../features/tasks/TasksPage.vue", async () => {
  state.taskFactoryCalls += 1;
  await state.gate;
  const { defineComponent, h, onBeforeUnmount } = await import("vue");
  return {
    default: defineComponent({
      name: "TasksPage",
      props: { visible: { type: Boolean, default: true } },
      emits: ["toast"],
      setup(props) {
        state.taskMounts += 1;
        onBeforeUnmount(() => {
          state.taskMounts -= 1;
        });
        return () =>
          h(
            "div",
            { "data-testid": "lazy-tasks", "data-visible": String(props.visible) },
            "图库任务内容",
          );
      },
    }),
  };
});

vi.mock("../features/recommendations/RecommendationPage.vue", async () => {
  const { defineComponent, h } = await import("vue");
  return {
    default: defineComponent({
      name: "RecommendationPage",
      props: { visible: { type: Boolean, default: true } },
      emits: ["toast"],
      setup: () => () => h("div", { "data-testid": "lazy-recommendations" }, "推荐内容"),
    }),
  };
});

vi.mock("../features/settings/SettingsPage.vue", () => {
  state.settingsFactoryCalls += 1;
  throw new Error("设置页模块加载失败");
});

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App lazy feature pages", () => {
  let wrapper: VueWrapper;

  beforeEach(() => {
    vi.clearAllMocks();
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
        if (url.includes("api/results/history")) {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        return Promise.resolve(
          jsonResponse({
            id: "latest",
            status: "succeeded",
            page: 1,
            page_size: 15,
            total_items: 0,
            total_pages: 0,
            items: [],
          }),
        );
      }),
    );
    wrapper = mount(App, { attachTo: document.body });
  });

  afterEach(() => {
    wrapper.unmount();
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("shows a lightweight loading surface, then loads the page on first visit", async () => {
    await flushPromises();
    expect(wrapper.find("[data-testid='lazy-tasks']").exists()).toBe(false);
    expect(state.taskFactoryCalls).toBe(0);

    await wrapper.get(".nav-item[data-page='tasks']").trigger("click");
    expect(wrapper.find(".async-page-state").exists()).toBe(true);
    expect(wrapper.text()).toContain("正在加载图库任务");
    expect(wrapper.find("[data-testid='lazy-tasks']").exists()).toBe(false);

    state.release();
    await flushPromises();

    expect(wrapper.find("[data-testid='lazy-tasks']").exists()).toBe(true);
    expect(wrapper.text()).toContain("图库任务内容");
    expect(state.taskFactoryCalls).toBe(1);
  });

  it("keeps an already-visited page mounted and only pauses it while hidden", async () => {
    state.release();
    await flushPromises();
    await wrapper.get(".nav-item[data-page='tasks']").trigger("click");
    await flushPromises();
    expect(state.taskMounts).toBe(1);

    await wrapper.get(".nav-item[data-page='recommendations']").trigger("click");
    await flushPromises();
    expect(wrapper.get("[data-testid='lazy-tasks']").attributes("data-visible")).toBe("false");
    expect(state.taskMounts).toBe(1);

    await wrapper.get(".nav-item[data-page='tasks']").trigger("click");
    await flushPromises();
    expect(state.taskMounts).toBe(1);
    expect(wrapper.get("[data-testid='lazy-tasks']").attributes("data-visible")).toBe("true");
  });

  it("surfaces a load failure with a manual retry and never requests on its own", async () => {
    await flushPromises();
    await wrapper.get(".settings-button").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("无法加载设置");
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);
    expect(state.settingsFactoryCalls).toBe(1);

    await flushPromises();
    expect(state.settingsFactoryCalls).toBe(1);

    await wrapper.get("[data-testid='async-page-retry']").trigger("click");
    await flushPromises();
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);

    // The failed page must not stop the rest of the shell.
    await wrapper.get(".nav-item[data-page='recommendations']").trigger("click");
    await flushPromises();
    expect(wrapper.find("[data-testid='lazy-recommendations']").exists()).toBe(true);
  });

  it("keeps the manual retry reachable after leaving and returning to the failed page", async () => {
    await flushPromises();
    await wrapper.get(".settings-button").trigger("click");
    await flushPromises();
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);

    await wrapper.get(".nav-item[data-page='recommendations']").trigger("click");
    await flushPromises();
    await wrapper.get(".settings-button").trigger("click");
    await flushPromises();

    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);
    await wrapper.get("[data-testid='async-page-retry']").trigger("click");
    await flushPromises();

    // The retry re-runs the loader; the module stays unavailable, so the
    // manual retry is offered again instead of a blank page or a loop.
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);
  });
});
