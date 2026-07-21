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

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function menuButton(label: string): HTMLButtonElement {
  const button = Array.from(
    document.body.querySelectorAll<HTMLButtonElement>(".gallery-context-menu button"),
  ).find((candidate) => candidate.textContent?.includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  return button;
}

describe("App search feedback wiring", () => {
  let wrapper: VueWrapper | null = null;
  let feedbackBodies: Array<Record<string, string>>;
  let deletedEvents: string[];
  let failFeedbackWrites: boolean;

  beforeEach(() => {
    vi.clearAllMocks();
    feedbackBodies = [];
    deletedEvents = [];
    failFeedbackWrites = false;
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
        if (path === "api/bootstrap") {
          return Promise.resolve(response({
            service: { status: "ready", ready: true },
            libraries: [{ id: "lib-1", name: "人物图库", enabled: true }],
          }));
        }
        if (path.startsWith("api/results/latest")) {
          return Promise.resolve(response({
            id: "search-1",
            status: "succeeded",
            page: 1,
            page_size: 15,
            total_items: 1,
            total_pages: 1,
            items: [{
              id: "image-1",
              search_session_id: "search-1",
              library_id: "lib-1",
              doc_id: "doc-1",
              name: "雷电将军.jpg",
              relative_path: "原神/雷电将军.jpg",
              library_name: "人物图库",
              rank: 1,
              confidence: 0.91,
            }],
          }));
        }
        if (path === "api/search") {
          return Promise.resolve(response({
            id: "search-1",
            status: "succeeded",
            page: 1,
            page_size: 15,
            total_items: 1,
            total_pages: 1,
            items: [{
              id: "image-1",
              search_session_id: "search-1",
              library_id: "lib-1",
              doc_id: "doc-1",
              name: "雷电将军.jpg",
              relative_path: "原神/雷电将军.jpg",
              library_name: "人物图库",
              rank: 1,
              confidence: 0.91,
            }],
          }));
        }
        if (path === "api/search-learning/status") {
          return Promise.resolve(response({
            available: true,
            database: "search-learning.sqlite3",
            online_weight_updates: false,
            fixed_evaluation_gate_required: true,
            training_running: false,
            settings: {
              learning_enabled: true,
              implicit_feedback_enabled: true,
              save_query_text: false,
              shadow_mode: true,
            },
            training_counts: {
              query_sessions: 1,
              explicit_samples: 0,
              positive_samples: 0,
              negative_samples: 0,
            },
            minimum_requirements: {
              query_sessions: 100,
              explicit_samples: 300,
              positive_and_negative_required: true,
            },
          }));
        }
        if (path.startsWith("api/search-feedback?") && (!init?.method || init.method === "GET")) {
          return Promise.resolve(response({ items: [], has_more: false, next_cursor: null }));
        }
        if (path === "api/search-feedback" && init?.method === "POST") {
          if (failFeedbackWrites) {
            return Promise.reject(new Error("learning store unavailable"));
          }
          const body = JSON.parse(String(init.body)) as Record<string, string>;
          feedbackBodies.push(body);
          return Promise.resolve(response({
            event_id: `event-${feedbackBodies.length}`,
            ...body,
            feedback_weight: body.action === "not_relevant" ? -1 : 1,
            created_at: "2026-07-19T00:00:00Z",
            active: true,
          }));
        }
        if (path.startsWith("api/search-feedback/") && init?.method === "DELETE") {
          deletedEvents.push(path.split("/").at(-1) ?? "");
          return Promise.resolve(response({
            event_id: deletedEvents.at(-1),
            session_id: "search-1",
            library_id: "lib-1",
            doc_id: "doc-1",
            action: "relevant",
            feedback_weight: 1,
            created_at: "2026-07-19T00:00:00Z",
            active: false,
          }));
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

  async function openMenu(): Promise<void> {
    if (!wrapper) throw new Error("app is not mounted");
    await wrapper.get(".image-card").trigger("contextmenu", { clientX: 40, clientY: 50 });
    await wrapper.vm.$nextTick();
  }

  async function runVisibleSearch(): Promise<void> {
    if (!wrapper) throw new Error("app is not mounted");
    await wrapper.get("input[type='search']").setValue("雷电将军");
    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await flushPromises();
  }

  it("loads session state and records explicit plus successful implicit actions", async () => {
    wrapper = mount(App, {
      attachTo: document.body,
      global: { stubs: { TasksPage: true, OrganizePage: true, SettingsPage: true } },
    });
    await flushPromises();
    await runVisibleSearch();

    expect(vi.mocked(fetch).mock.calls.some(([path]) =>
      String(path).startsWith("api/search-feedback?session_id=search-1"),
    )).toBe(true);

    await openMenu();
    const relevantButton = document.body.querySelector<HTMLButtonElement>(".feedback-positive");
    expect(relevantButton?.textContent?.trim()).toBe("标记为相关");
    relevantButton?.click();
    await flushPromises();
    expect(feedbackBodies.at(-1)).toMatchObject({
      session_id: "search-1",
      library_id: "lib-1",
      doc_id: "doc-1",
      action: "relevant",
      source: "context_menu",
    });

    await openMenu();
    expect(document.body.querySelector(".feedback-positive")?.textContent).toContain("已标记为相关");
    menuButton("撤销反馈").click();
    await flushPromises();
    expect(deletedEvents).toEqual(["event-1"]);

    await wrapper.get(".image-card").trigger("click");
    await flushPromises();
    await wrapper.get(".thumbnail-stage").trigger("dblclick");
    await flushPromises();
    expect(nativeActions.open).toHaveBeenCalledWith("image-1");

    await openMenu();
    menuButton("复制图片").click();
    await flushPromises();
    await openMenu();
    menuButton("导出到文件夹").click();
    await flushPromises();

    expect(feedbackBodies.map((body) => body.action)).toEqual(
      expect.arrayContaining(["relevant", "detail", "open", "copy", "export"]),
    );
    expect(nativeActions.copyImage).toHaveBeenCalledWith("image-1");
    expect(nativeActions.exportImages).toHaveBeenCalledWith(["image-1"]);
  });

  it("does not let feedback failures change the successful native operation", async () => {
    failFeedbackWrites = true;
    wrapper = mount(App, {
      attachTo: document.body,
      global: { stubs: { TasksPage: true, OrganizePage: true, SettingsPage: true } },
    });
    await flushPromises();
    await runVisibleSearch();

    await wrapper.get(".thumbnail-stage").trigger("dblclick");
    await flushPromises();

    expect(nativeActions.open).toHaveBeenCalledWith("image-1");
    expect(wrapper.find(".gallery-panel").exists()).toBe(true);
    expect(wrapper.get(".image-card").attributes("data-image-id")).toBe("image-1");
  });
});
