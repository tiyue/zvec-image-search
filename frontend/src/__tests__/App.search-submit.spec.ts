import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.vue";

const nativeActions = vi.hoisted(() => ({
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
  useNativeImageActions: () => nativeActions,
}));

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App search submission entry points", () => {
  let wrapper: VueWrapper | null = null;
  let searchBodies: Array<Record<string, unknown>>;

  beforeEach(() => {
    searchBodies = [];
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
      vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path.includes("api/bootstrap")) {
          return Promise.resolve(
            jsonResponse({
              service: { status: "ready", ready: true },
              libraries: [{ id: "library-1", name: "图片数据库", enabled: true }],
            }),
          );
        }
        if (path.includes("api/results/latest")) {
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
        }
        if (path === "api/search" && init?.method === "POST") {
          searchBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
          return Promise.resolve(
            jsonResponse(
              {
                id: `search-${searchBodies.length}`,
                status: "succeeded",
                query: searchBodies.at(-1)?.text,
                page: 1,
                page_size: 15,
                total_items: 0,
                total_pages: 0,
                items: [],
              },
              202,
            ),
          );
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    vi.unstubAllGlobals();
  });

  async function mountReadyApp(): Promise<VueWrapper> {
    const mounted = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          TasksPage: true,
          OrganizePage: true,
          SettingsPage: true,
        },
      },
    });
    await flushPromises();
    return mounted;
  }

  it("sends exactly one search when the explicit button is clicked", async () => {
    wrapper = await mountReadyApp();
    await wrapper.get("input[type='search']").setValue("人物写真");
    await wrapper.get("input[aria-label='本次搜索图片数量']").setValue("56");

    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await flushPromises();

    expect(searchBodies).toHaveLength(1);
    expect(searchBodies[0]).toMatchObject({
      text: "人物写真",
      mode: "text",
      top_k: 56,
    });
  });

  it("sends exactly one search when Enter is pressed in the search input", async () => {
    wrapper = await mountReadyApp();
    const input = wrapper.get("input[type='search']");
    await input.setValue("角色动作");

    await input.trigger("keydown", { key: "Enter", code: "Enter" });
    await flushPromises();

    expect(searchBodies).toHaveLength(1);
    expect(searchBodies[0]).toMatchObject({ text: "角色动作", mode: "text" });
  });
});
