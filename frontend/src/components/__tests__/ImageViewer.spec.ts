import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ImageViewer from "../ImageViewer.vue";
import { isViewerOverlayOpen, type ViewerImage } from "../../composables/useImageViewer";

function image(overrides: Partial<ViewerImage> = {}): ViewerImage {
  return {
    id: "image-1",
    name: "示例人物写真.jpg",
    relativePath: "人物写真/示例人物写真.jpg",
    libraryName: "图片数据库",
    width: 2048,
    height: 3072,
    tags: ["原神", "雷电将军"],
    thumbnailUrl: "api/image/image-1?variant=thumbnail",
    imageUrl: "api/image/image-1?variant=preview",
    nativeId: "image-1",
    ...overrides,
  };
}

function body(selector: string): HTMLElement {
  const element = document.body.querySelector<HTMLElement>(selector);
  if (!element) throw new Error(`missing element: ${selector}`);
  return element;
}

function mountViewer(props: Record<string, unknown> = {}) {
  return mount(ImageViewer, {
    attachTo: document.body,
    props: {
      image: image(),
      canPrevious: true,
      canNext: true,
      ...props,
    },
  });
}

describe("ImageViewer overlay", () => {
  let wrapper: ReturnType<typeof mountViewer> | null = null;

  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("shows the dialog semantics, file metadata and tags, and focuses the close control", () => {
    wrapper = mountViewer();

    const dialog = body("[data-testid='image-viewer']");
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.body.textContent).toContain("示例人物写真.jpg");
    expect(document.body.textContent).toContain("2048 × 3072");
    expect(document.body.textContent).toContain("人物写真/示例人物写真.jpg");
    expect(document.body.textContent).toContain("雷电将军");
    expect(document.activeElement).toBe(body("[data-testid='viewer-close']"));
  });

  it("omits the path row when the source has no relative path", () => {
    wrapper = mountViewer({ image: image({ relativePath: "" }) });
    expect(document.body.textContent).not.toContain("相对路径");
  });

  it("disables the boundary navigation and emits navigation intents", async () => {
    wrapper = mountViewer({ canPrevious: false, canNext: false });
    const previous = body("[data-testid='viewer-previous']") as HTMLButtonElement;
    const next = body("[data-testid='viewer-next']") as HTMLButtonElement;
    expect(previous.disabled).toBe(true);
    expect(next.disabled).toBe(true);

    wrapper.unmount();
    wrapper = mountViewer();
    body("[data-testid='viewer-previous']").click();
    body("[data-testid='viewer-next']").click();
    body("[data-testid='viewer-close']").click();
    expect(wrapper.emitted("previous")).toHaveLength(1);
    expect(wrapper.emitted("next")).toHaveLength(1);
    expect(wrapper.emitted("close")).toHaveLength(1);
  });

  it("drives fit and 100% with the labelled buttons and double click", async () => {
    wrapper = mountViewer();
    const zoom = () => body("[data-testid='viewer-zoom']").textContent;
    const fit = body("[data-testid='viewer-fit']") as HTMLButtonElement;
    const actual = body("[data-testid='viewer-actual']") as HTMLButtonElement;

    expect(fit.getAttribute("aria-pressed")).toBe("true");
    actual.click();
    await wrapper.vm.$nextTick();
    expect(zoom()).toBe("100%");
    expect(actual.getAttribute("aria-pressed")).toBe("true");

    fit.click();
    await wrapper.vm.$nextTick();
    expect(fit.getAttribute("aria-pressed")).toBe("true");
    expect(zoom()).toBe("10%");

    // Double click toggles away from fit and back.
    body("[data-testid='viewer-stage']").dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true, cancelable: true }),
    );
    await wrapper.vm.$nextTick();
    expect(zoom()).toBe("100%");
    body("[data-testid='viewer-stage']").dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true, cancelable: true }),
    );
    await wrapper.vm.$nextTick();
    expect(fit.getAttribute("aria-pressed")).toBe("true");
  });

  it("uses the loaded pixel size for 100% instead of the placeholder size", async () => {
    wrapper = mountViewer({ image: image({ width: 0, height: 0 }) });
    const imageElement = body("[data-testid='viewer-image']") as HTMLImageElement;

    Object.defineProperty(imageElement, "naturalWidth", { configurable: true, value: 800 });
    Object.defineProperty(imageElement, "naturalHeight", { configurable: true, value: 600 });
    imageElement.dispatchEvent(new Event("load"));
    await wrapper.vm.$nextTick();

    expect(body("[data-testid='viewer-zoom']").textContent).toBe("50%");
    expect(document.body.querySelectorAll(".viewer-layer").length).toBe(1);
  });

  it("preloads only the neighbour URLs and clears them on unmount", () => {
    const sources: string[] = [];
    class FakeImage {
      decoding = "";
      set src(value: string) {
        sources.push(value);
      }
      get src(): string {
        return "";
      }
    }
    vi.stubGlobal("Image", FakeImage);
    try {
      wrapper = mountViewer({ preloadUrls: ["api/next", "api/previous"] });
      expect(sources).toEqual(["api/next", "api/previous"]);
      wrapper.unmount();
      wrapper = null;
      expect(sources.slice(2)).toEqual(["", ""]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("routes keyboard navigation and traps Tab inside the dialog", async () => {
    wrapper = mountViewer();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(wrapper.emitted("previous")).toHaveLength(1);
    expect(wrapper.emitted("next")).toHaveLength(1);
    expect(wrapper.emitted("close")).toHaveLength(1);

    const buttons = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>(".image-viewer button:not([disabled])"),
    );
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    last.focus();
    last.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(first);
    first.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );
    expect(document.activeElement).toBe(last);
  });

  it("collapses the information sidebar without hiding the stage", async () => {
    wrapper = mountViewer();
    const toggle = Array.from(document.body.querySelectorAll<HTMLButtonElement>(".viewer-tool"))
      .find((button) => button.textContent?.includes("隐藏信息"));
    expect(toggle).toBeDefined();
    toggle?.click();
    await wrapper.vm.$nextTick();

    const info = document.body.querySelector<HTMLElement>(".viewer-info");
    expect(info?.style.display).toBe("none");
    expect(document.body.querySelector("[data-testid='viewer-stage']")).not.toBeNull();
  });

  it("keeps Tab inside the visible controls once the sidebar is collapsed", async () => {
    wrapper = mountViewer();
    const toggle = Array.from(document.body.querySelectorAll<HTMLButtonElement>(".viewer-tool"))
      .find((button) => button.textContent?.includes("隐藏信息"));
    toggle?.click();
    await wrapper.vm.$nextTick();

    const visibleButtons = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>(".image-viewer button:not([disabled])"),
    ).filter((button) => button.closest(".viewer-info") === null);
    const last = visibleButtons[visibleButtons.length - 1];
    last.focus();
    last.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(visibleButtons[0]);
    expect(document.activeElement?.closest(".viewer-info")).toBeNull();

    visibleButtons[0].dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );
    expect(document.activeElement).toBe(last);
  });

  it("replaces the layer and hides the old pixels when the image changes", async () => {
    wrapper = mountViewer({
      image: image({ id: "image-1", width: 900, height: 600, thumbnailUrl: "api/1-thumb", imageUrl: "api/1-full" }),
    });
    const first = body("[data-testid='viewer-image']") as HTMLImageElement;
    Object.defineProperty(first, "naturalWidth", { configurable: true, value: 900 });
    Object.defineProperty(first, "naturalHeight", { configurable: true, value: 600 });
    first.dispatchEvent(new Event("load"));
    await wrapper.vm.$nextTick();
    expect(body("[data-testid='viewer-zoom']").textContent).toBe("44%");
    expect(first.style.display).not.toBe("none");

    await wrapper.setProps({
      image: image({ id: "image-2", width: 1200, height: 800, thumbnailUrl: "api/2-thumb", imageUrl: "api/2-full" }),
    });
    await wrapper.vm.$nextTick();

    const second = body("[data-testid='viewer-image']") as HTMLImageElement;
    expect(second).not.toBe(first);
    expect(second.getAttribute("src")).toBe("api/2-full");
    expect(second.style.display).toBe("none");
    const layers = Array.from(document.body.querySelectorAll<HTMLImageElement>(".viewer-layer"));
    expect(layers).toHaveLength(2);
    expect(layers[0].getAttribute("src")).toBe("api/2-thumb");
    // The new image's own size replaces the previous one immediately.
    expect(body("[data-testid='viewer-zoom']").textContent).toBe("33%");

    // A late event from the replaced element must not mark the new one ready.
    first.dispatchEvent(new Event("load"));
    await wrapper.vm.$nextTick();
    expect(second.style.display).toBe("none");

    Object.defineProperty(second, "naturalWidth", { configurable: true, value: 1200 });
    Object.defineProperty(second, "naturalHeight", { configurable: true, value: 800 });
    second.dispatchEvent(new Event("load"));
    await wrapper.vm.$nextTick();
    expect(second.style.display).not.toBe("none");
    expect(document.body.querySelectorAll(".viewer-layer")).toHaveLength(1);
  });

  it("holds the background-shortcut isolation while an image is displayed", () => {
    expect(isViewerOverlayOpen()).toBe(false);
    wrapper = mountViewer();
    expect(isViewerOverlayOpen()).toBe(true);
    wrapper.unmount();
    wrapper = null;
    expect(isViewerOverlayOpen()).toBe(false);
  });

  it("surfaces a readable error with a retry action", async () => {
    wrapper = mountViewer({ error: "无法读取下一页图片" });
    expect(document.body.textContent).toContain("无法读取下一页图片");
    const retry = Array.from(document.body.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.trim() === "重试");
    retry?.click();
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });

  it("keeps a press on the navigation buttons out of the pan capture", async () => {
    const capture = vi.fn();
    (
      HTMLElement.prototype as unknown as { setPointerCapture: unknown }
    ).setPointerCapture = capture;
    try {
      wrapper = mountViewer();
      const stage = body("[data-testid='viewer-stage']");
      const next = body("[data-testid='viewer-next']");

      // A press that starts on a control is a click, not a canvas drag: the
      // stage must not capture the pointer and drag state must stay off.
      next.dispatchEvent(
        new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 12, clientY: 12 }),
      );
      await wrapper.vm.$nextTick();
      expect(capture).not.toHaveBeenCalled();
      expect(stage.classList.contains("is-dragging")).toBe(false);

      // The button still receives its own click.
      next.click();
      await wrapper.vm.$nextTick();
      expect(wrapper.emitted("next")).toHaveLength(1);

      // A press on the image itself still captures the pointer and pans.
      const imageLayer = body("[data-testid='viewer-image']");
      imageLayer.dispatchEvent(
        new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 20, clientY: 20 }),
      );
      await wrapper.vm.$nextTick();
      expect(capture).toHaveBeenCalledTimes(1);
      expect(stage.classList.contains("is-dragging")).toBe(true);

      stage.dispatchEvent(
        new MouseEvent("pointerup", { bubbles: true, button: 0, clientX: 20, clientY: 20 }),
      );
      await wrapper.vm.$nextTick();
      expect(stage.classList.contains("is-dragging")).toBe(false);
    } finally {
      delete (HTMLElement.prototype as unknown as { setPointerCapture?: unknown })
        .setPointerCapture;
    }
  });
});
