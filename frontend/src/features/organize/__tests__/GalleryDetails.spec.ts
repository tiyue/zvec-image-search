import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import GalleryDetails from "../GalleryDetails.vue";
import type { OrganizeImage } from "../types";
import { isViewerOverlayOpen } from "../../../composables/useImageViewer";

enableAutoUnmount(afterEach);

function image(overrides: Partial<OrganizeImage> = {}): OrganizeImage {
  return {
    id: "doc-1",
    nativeImageId: "native-1",
    libraryId: "lib-1",
    fileName: "01.jpg",
    relativePath: "原神/雷电将军/01.jpg",
    width: 1200,
    height: 1800,
    manualTags: ["写真"],
    folderTags: ["原神"],
    modelTags: [],
    inheritedTags: ["雷电将军"],
    effectiveTags: ["写真", "原神", "雷电将军"],
    tagSources: [],
    annotationStatus: "",
    thumbnailUrl: "api/image/doc-1?variant=thumbnail",
    imageUrl: "api/image/doc-1?variant=preview",
    imageAvailable: true,
    ...overrides,
  };
}

describe("GalleryDetails", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("shows the loaded image facts and opens the shared viewer", async () => {
    const wrapper = mount(GalleryDetails, {
      props: { images: [image()], selectedCount: 1, description: "已选 1 张图片" },
    });

    expect(wrapper.get("#gallery-details-title").text()).toBe("图片详情");
    expect(wrapper.text()).toContain("01.jpg");
    expect(wrapper.text()).toContain("1200 × 1800");
    expect(wrapper.text()).toContain("原神/雷电将军/01.jpg");
    expect(wrapper.findAll(".details-tag").map((tag) => tag.text())).toEqual([
      "写真",
      "原神",
      "雷电将军",
    ]);

    await wrapper.get(".details-view").trigger("click");
    expect(wrapper.emitted("open")).toEqual([["doc-1"]]);
  });

  it("summarises a multi-image selection from the loaded entries only", () => {
    const wrapper = mount(GalleryDetails, {
      props: {
        images: [image(), image({ id: "doc-2", fileName: "02.jpg" })],
        selectedCount: 65,
        description: "已选整文件夹全部图片",
      },
    });

    expect(wrapper.text()).toContain("已选整文件夹全部图片");
    expect(wrapper.text()).toContain("详情基于已加载的 2 张");
    expect(wrapper.findAll(".details-list li strong").map((item) => item.text())).toEqual([
      "01.jpg",
      "02.jpg",
    ]);
    expect(wrapper.find(".details-view").exists()).toBe(false);
  });

  it("keeps an empty pane when nothing is selected and never invents details", () => {
    const wrapper = mount(GalleryDetails, {
      props: { images: [], selectedCount: 0, description: "尚未选择图片" },
    });

    expect(wrapper.text()).toContain("未选择图片");
    expect(wrapper.find(".details-list").exists()).toBe(false);
  });

  it("reports an unloaded selection without reading the whole catalog", () => {
    const wrapper = mount(GalleryDetails, {
      props: { images: [], selectedCount: 65, description: "已选整文件夹全部图片" },
    });

    expect(wrapper.text()).toContain("尚未加载选中图片的详情");
  });

  it("focuses the close control and closes on the button and on escape", async () => {
    const wrapper = mount(GalleryDetails, {
      attachTo: document.body,
      props: { images: [image()], selectedCount: 1, description: "已选 1 张图片" },
    });

    expect(document.activeElement).toBe(wrapper.get(".details-close").element);

    await wrapper.get(".details-close").trigger("click");
    expect(wrapper.emitted("close")).toHaveLength(1);

    await wrapper.get(".gallery-details").trigger("keydown", { key: "Escape" });
    expect(wrapper.emitted("close")).toHaveLength(2);

    wrapper.unmount();
  });

  it("owns floating shortcuts, closes outside and releases on a wide layout or unmount", async () => {
    let change: ((event: MediaQueryListEvent) => void) | undefined;
    const media = {
      matches: true,
      addEventListener: vi.fn((_type, handler) => { change = handler; }),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal("matchMedia", vi.fn(() => media));
    const wrapper = mount(GalleryDetails, {
      attachTo: document.body,
      props: { images: [image()], selectedCount: 1, description: "已选 1 张图片" },
    });
    await flushPromises();
    expect(wrapper.get(".gallery-details").attributes("aria-modal")).toBe("true");
    expect(isViewerOverlayOpen()).toBe(true);
    await wrapper.get(".details-view").trigger("keydown", { key: "Tab" });
    expect(document.activeElement).toBe(wrapper.get(".details-close").element);
    await wrapper.get(".details-close").trigger("keydown", { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(wrapper.get(".details-view").element);
    await wrapper.get(".details-backdrop").trigger("click");
    expect(wrapper.emitted("close")).toHaveLength(1);

    change?.({ matches: false } as MediaQueryListEvent);
    await flushPromises();
    expect(wrapper.find(".details-backdrop").exists()).toBe(false);
    expect(isViewerOverlayOpen()).toBe(false);
    change?.({ matches: true } as MediaQueryListEvent);
    await flushPromises();
    expect(isViewerOverlayOpen()).toBe(true);
    wrapper.unmount();
    expect(isViewerOverlayOpen()).toBe(false);
    expect(media.removeEventListener).toHaveBeenCalled();
  });
});
