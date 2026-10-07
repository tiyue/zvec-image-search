import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.vue";
import { setGalleryDensity } from "../composables/useGalleryDensity";

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

// A second overlay source whose viewer is a different component instance: the
// shared viewer must suspend background shortcuts for it just the same.
vi.mock("../features/recommendations/RecommendationPage.vue", async () => {
  const { defineComponent, h, ref } = await import("vue");
  const ImageViewer = (await import("../components/ImageViewer.vue")).default;
  const image = {
    id: "rec-1",
    name: "rec.jpg",
    relativePath: "",
    libraryName: "",
    width: 800,
    height: 600,
    tags: [],
    thumbnailUrl: "api/image/rec-1?variant=thumbnail",
    imageUrl: "api/image/rec-1?variant=preview",
    nativeId: "rec-1",
  };
  return {
    default: defineComponent({
      name: "RecommendationPageStub",
      props: { visible: { type: Boolean, default: true } },
      setup() {
        const open = ref(false);
        return () =>
          h("div", { "data-testid": "stub-recommendation" }, [
            h(
              "button",
              { class: "stub-open-viewer", type: "button", onClick: () => (open.value = true) },
              "打开查看器",
            ),
            open.value
              ? h(ImageViewer, {
                  image,
                  canPrevious: false,
                  canNext: false,
                  onClose: () => (open.value = false),
                })
              : null,
          ]);
      },
    }),
  };
});

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function resultItem(id: string, rank: number) {
  return {
    id,
    search_session_id: "session-1",
    library_id: "library-1",
    doc_id: `doc-${id}`,
    name: `${id}.jpg`,
    relative_path: `场景/${id}.jpg`,
    library_name: "图片数据库",
    rank,
    match_state: "high",
    rank_source: "fused",
    width: 2048,
    height: 3072,
    tags: ["标签", id],
    thumbnail_url: `api/image/${id}?variant=thumbnail`,
    image_url: `api/image/${id}?variant=preview`,
  };
}

const OPERATION_ID = "op-1";

function pageOne(id = OPERATION_ID) {
  return {
    id,
    status: "succeeded",
    page: 1,
    page_size: 15,
    total_items: 4,
    total_pages: 2,
    has_previous: false,
    has_next: true,
    items: [resultItem("i1", 1), resultItem("i2", 2), resultItem("i3", 3)],
  };
}

function pageTwo(id = OPERATION_ID) {
  return {
    id,
    status: "succeeded",
    page: 2,
    page_size: 15,
    total_items: 4,
    total_pages: 2,
    has_previous: true,
    has_next: false,
    items: [resultItem("i4", 4)],
  };
}

function viewer(): HTMLElement | null {
  return document.body.querySelector<HTMLElement>("[data-testid='image-viewer']");
}

function buttonByText(label: string): HTMLButtonElement {
  const button = Array.from(document.body.querySelectorAll<HTMLButtonElement>("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (!button) throw new Error(`missing button: ${label}`);
  return button;
}

describe("App in-app viewer", () => {
  let wrapper: VueWrapper;
  let pageTwoFailures = 0;
  let searchPosts = 0;

  beforeEach(() => {
    vi.clearAllMocks();
    pageTwoFailures = 0;
    searchPosts = 0;
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
        if (url === "api/search") {
          searchPosts += 1;
          return Promise.resolve(jsonResponse(pageOne(OPERATION_ID)));
        }
        if (url.includes(`api/search/${OPERATION_ID}`)) {
          if (url.includes("page=2")) {
            if (pageTwoFailures > 0) {
              pageTwoFailures -= 1;
              return Promise.reject(new Error("second page unavailable"));
            }
            return Promise.resolve(jsonResponse(pageTwo(OPERATION_ID)));
          }
          return Promise.resolve(jsonResponse(pageOne(OPERATION_ID)));
        }
        if (url.includes("api/results/latest")) {
          const page = /[?&]page=(\d+)/u.exec(url)?.[1] ?? "1";
          if (page === "2") {
            if (pageTwoFailures > 0) {
              pageTwoFailures -= 1;
              return Promise.reject(new Error("second page unavailable"));
            }
            return Promise.resolve(jsonResponse(pageTwo("latest")));
          }
          return Promise.resolve(jsonResponse(pageOne("latest")));
        }
        if (url.includes("api/results/history")) {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        return Promise.resolve(jsonResponse({}));
      }),
    );
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          TasksPage: true,
          OrganizePage: true,
          RecommendationPage: true,
          RawSelectionPage: true,
          ImageEditPage: true,
          SettingsPage: true,
        },
      },
    });
  });

  afterEach(() => {
    wrapper.unmount();
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  async function runSearch(): Promise<void> {
    await wrapper.get("input[type='search']").setValue("人物写真");
    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await flushPromises();
  }

  async function doubleClickCard(index: number): Promise<void> {
    const card = wrapper.findAll("button.image-card")[index];
    await card.find(".thumbnail-stage").trigger("dblclick");
    await flushPromises();
  }

  it("opens on double click with the current position and steps inside the page", async () => {
    await runSearch();
    await doubleClickCard(1);

    expect(viewer()).not.toBeNull();
    expect(viewer()?.textContent).toContain("i2.jpg");
    expect(viewer()?.textContent).toContain("2 / 4");
    expect(nativeActionMocks.open).not.toHaveBeenCalled();

    buttonByText("›").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("i3.jpg");
    expect(nativeActionMocks.open).not.toHaveBeenCalled();

    buttonByText("‹").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("i2.jpg");
  });

  it("crosses the page boundary through the existing pagination contract", async () => {
    await runSearch();
    const postsBefore = searchPosts;
    await doubleClickCard(2);
    expect(viewer()?.textContent).toContain("i3.jpg");

    buttonByText("›").click();
    await flushPromises();

    expect(viewer()?.textContent).toContain("i4.jpg");
    expect(wrapper.find("[data-image-id='i4']").exists()).toBe(true);
    expect(wrapper.find("[data-image-id='i1']").exists()).toBe(false);
    // Crossing a page must not resubmit the semantic search.
    expect(searchPosts).toBe(postsBefore);
    expect(
      vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("page=2")),
    ).toBe(true);
  });

  it("keeps the current image and offers a retry when the next page fails", async () => {
    await runSearch();
    await doubleClickCard(2);
    pageTwoFailures = 1;

    buttonByText("›").click();
    await flushPromises();

    expect(viewer()?.textContent).toContain("i3.jpg");
    expect(viewer()?.textContent).toContain("无法读取下一页图片");

    buttonByText("重试").click();
    await flushPromises();

    expect(viewer()?.textContent).toContain("i4.jpg");
  });

  it("opens the primary selection with space only from the gallery context", async () => {
    await runSearch();
    const card = wrapper.findAll("button.image-card")[0];
    await card.trigger("click");
    (card.element as HTMLElement).focus();

    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    card.element.dispatchEvent(space);
    await flushPromises();

    expect(space.defaultPrevented).toBe(true);
    expect(viewer()?.textContent).toContain("i1.jpg");
  });

  it("does not steal space from the search input", async () => {
    const input = wrapper.get("input[type='search']").element as HTMLInputElement;
    input.focus();

    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    input.dispatchEvent(space);
    await flushPromises();

    expect(space.defaultPrevented).toBe(false);
    expect(viewer()).toBeNull();
  });

  it("blocks background page shortcuts while the viewer is open and closes on escape", async () => {
    await runSearch();
    await doubleClickCard(0);
    expect(viewer()).not.toBeNull();

    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "2", code: "Digit2", altKey: true }),
    );
    await flushPromises();
    expect(wrapper.get("[data-page-section='search']").isVisible()).toBe(true);
    expect(wrapper.get(".nav-item[data-page='search']").attributes("aria-current")).toBe("page");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(viewer()).toBeNull();
    expect(document.activeElement).toBe(
      wrapper.find("[data-image-id='i1']").element,
    );
  });

  it("records a system open only from the explicit viewer action", async () => {
    await runSearch();
    await doubleClickCard(0);

    buttonByText("›").click();
    await flushPromises();
    expect(nativeActionMocks.open).not.toHaveBeenCalled();

    buttonByText("用系统程序打开").click();
    await flushPromises();
    expect(nativeActionMocks.open).toHaveBeenCalledWith("i2");
  });

  it("suspends background page shortcuts for an overlay from another source", async () => {
    wrapper.unmount();
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          TasksPage: true,
          OrganizePage: true,
          RawSelectionPage: true,
          ImageEditPage: true,
          SettingsPage: true,
        },
      },
    });
    await flushPromises();

    await wrapper.get(".nav-item[data-page='recommendations']").trigger("click");
    await flushPromises();
    await wrapper.get(".stub-open-viewer").trigger("click");
    await flushPromises();
    expect(document.body.querySelector("[data-testid='image-viewer']")).not.toBeNull();

    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "2", code: "Digit2", altKey: true }),
    );
    await flushPromises();
    expect(wrapper.get(".nav-item[data-page='recommendations']").attributes("aria-current")).toBe(
      "page",
    );

    // The overlay still closes on escape and then releases the shortcut hold.
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    expect(document.body.querySelector("[data-testid='image-viewer']")).toBeNull();

    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "2", code: "Digit2", altKey: true }),
    );
    await flushPromises();
    expect(wrapper.get(".nav-item[data-page='tasks']").attributes("aria-current")).toBe("page");
  });
});

describe("App viewer across a gallery capacity change", () => {
  const TOTAL = 120;
  let wrapper: VueWrapper;
  let gatePageTwo = false;
  let releasePageTwo: (() => void) | null = null;
  let gateCapacity = false;
  let releaseCapacity: (() => void) | null = null;

  function qaItem(number: number) {
    const id = `qa-${number}`;
    return {
      ...resultItem(id, number),
      id,
      search_session_id: "qa-search",
      name: `${id}.png`,
      relative_path: `场景/${id}.png`,
    };
  }

  function pageWindow(page: number, pageSize: number) {
    const start = (page - 1) * pageSize;
    const count = Math.max(0, Math.min(pageSize, TOTAL - start));
    return {
      id: "qa-search",
      status: "succeeded",
      page,
      page_size: pageSize,
      total_items: TOTAL,
      total_pages: Math.ceil(TOTAL / pageSize),
      has_previous: page > 1,
      has_next: start + count < TOTAL,
      items: Array.from({ length: count }, (_, index) => qaItem(start + index + 1)),
    };
  }

  function viewer(): HTMLElement | null {
    return document.body.querySelector<HTMLElement>("[data-testid='image-viewer']");
  }

  function buttonByText(label: string): HTMLButtonElement {
    const button = Array.from(document.body.querySelectorAll<HTMLButtonElement>("button")).find(
      (candidate) => candidate.textContent?.trim() === label,
    );
    if (!button) throw new Error(`missing button: ${label}`);
    return button;
  }

  function pageSizeRequests(pageSize: number): number {
    return vi
      .mocked(fetch)
      .mock.calls.filter(([input]) => String(input).includes(`page_size=${pageSize}`)).length;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    gatePageTwo = false;
    releasePageTwo = null;
    gateCapacity = false;
    releaseCapacity = null;
    // 1000x600 yields a 15-tile first page at the default density; the large
    // density would shrink it to 6.
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1000);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
    setGalleryDensity("standard");
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
        if (url === "api/search") {
          return Promise.resolve(jsonResponse(pageWindow(1, 15)));
        }
        const pageMatch = /api\/search\/qa-search\?page=(\d+)&page_size=(\d+)/u.exec(url);
        if (pageMatch) {
          const page = Number(pageMatch[1]);
          const size = Number(pageMatch[2]);
          if (page === 2 && gatePageTwo) {
            return new Promise<Response>((resolve) => {
              releasePageTwo = () => resolve(jsonResponse(pageWindow(page, size)));
            });
          }
          if (size === 6 && gateCapacity) {
            return new Promise<Response>((resolve) => {
              releaseCapacity = () => resolve(jsonResponse(pageWindow(page, size)));
            });
          }
          return Promise.resolve(jsonResponse(pageWindow(page, size)));
        }
        if (url.includes("api/results/latest")) {
          return Promise.resolve(jsonResponse(pageWindow(1, 15)));
        }
        if (url.includes("api/results/history")) {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        return Promise.resolve(jsonResponse({}));
      }),
    );
    wrapper = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          TasksPage: true,
          OrganizePage: true,
          RecommendationPage: true,
          RawSelectionPage: true,
          ImageEditPage: true,
          SettingsPage: true,
        },
      },
    });
  });

  afterEach(() => {
    wrapper.unmount();
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    setGalleryDensity("standard");
  });

  async function runSearch(): Promise<void> {
    await flushPromises();
    await wrapper.get("input[type='search']").setValue("人物写真");
    await wrapper.get("[data-testid='submit-search']").trigger("click");
    await flushPromises();
  }

  it("keeps the absolute position when the capacity shrinks under the viewer", async () => {
    await runSearch();
    const cards = wrapper.findAll("button.image-card");
    expect(cards).toHaveLength(15);

    await cards[14].find(".thumbnail-stage").trigger("dblclick");
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-15.png");
    expect(viewer()?.textContent).toContain("15 / 120");

    // The density control would shrink the page from 15 to 6 tiles while the
    // viewer is open; that capacity change must wait for the overlay.
    await wrapper.get("[data-testid='result-density']").setValue("large");
    await flushPromises();
    expect(pageSizeRequests(6)).toBe(0);

    buttonByText("›").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-16.png");
    expect(viewer()?.textContent).toContain("16 / 120");
    expect(pageSizeRequests(6)).toBe(0);

    buttonByText("‹").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-15.png");
    expect(viewer()?.textContent).toContain("15 / 120");

    buttonByText("×").click();
    await flushPromises();
    expect(viewer()).toBeNull();

    // Closing re-applies the deferred capacity at the current image's page.
    expect(pageSizeRequests(6)).toBeGreaterThan(0);
    expect(wrapper.find("[data-image-id='qa-15']").exists()).toBe(true);
  });

  it("does not let a late cross-page read replace the window after the viewer closes", async () => {
    await runSearch();
    const cards = wrapper.findAll("button.image-card");
    expect(cards).toHaveLength(15);

    await cards[14].find(".thumbnail-stage").trigger("dblclick");
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-15.png");

    // The next page read stays in flight while the overlay is closed.
    gatePageTwo = true;
    buttonByText("›").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("正在读取图片");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(viewer()).toBeNull();
    expect(wrapper.find("[data-image-id='qa-1']").exists()).toBe(true);

    // The user goes back to the first image of the window.
    await wrapper.findAll("button.image-card")[0].trigger("click");
    await flushPromises();

    // The abandoned page must not replace the window or the selection.
    releasePageTwo?.();
    await flushPromises();
    expect(wrapper.find("[data-image-id='qa-1']").exists()).toBe(true);
    expect(wrapper.find("[data-image-id='qa-16']").exists()).toBe(false);
    expect(wrapper.find("[data-image-id='qa-1']").classes()).toContain("is-selected");
  });

  it("keeps the shown capacity when a capacity read is in flight as the viewer opens", async () => {
    await runSearch();
    const cards = wrapper.findAll("button.image-card");
    await cards[14].trigger("click");
    await flushPromises();

    // The measured capacity shrinks to six while its re-read stays in flight.
    gateCapacity = true;
    await wrapper.get("[data-testid='result-density']").setValue("large");
    await flushPromises();
    expect(pageSizeRequests(6)).toBeGreaterThan(0);

    await wrapper.findAll("button.image-card")[14].find(".thumbnail-stage").trigger("dblclick");
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-15.png");
    expect(viewer()?.textContent).toContain("15 / 120");

    // The abandoned capacity must not unload the window under the overlay.
    releaseCapacity?.();
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-15.png");

    // Stepping uses the capacity the gallery actually shows.
    buttonByText("›").click();
    await flushPromises();
    expect(viewer()?.textContent).toContain("qa-16.png");
    expect(viewer()?.textContent).toContain("16 / 120");
  });
});
