import { nextTick } from "vue";
import { describe, expect, it } from "vitest";

import { useRawSelectionViewport } from "../useRawSelectionViewport";

function elements(
  width = 200,
  height = 100,
  naturalWidth = 800,
  naturalHeight = 400,
  imageWidth = width,
  imageHeight = height,
  safeInset = 24,
) {
  const container = document.createElement("div");
  container.style.setProperty("--raw-preview-safe-inset", `${safeInset}px`);
  const image = document.createElement("img");
  Object.defineProperties(container, {
    clientWidth: { configurable: true, value: width },
    clientHeight: { configurable: true, value: height },
    getBoundingClientRect: {
      value: () => ({
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: width,
        bottom: height,
        width,
        height,
        toJSON: () => ({}),
      }),
    },
  });
  Object.defineProperties(image, {
    clientWidth: { configurable: true, value: imageWidth },
    clientHeight: { configurable: true, value: imageHeight },
    naturalWidth: { configurable: true, value: naturalWidth },
    naturalHeight: { configurable: true, value: naturalHeight },
  });
  return { container, image };
}

describe("useRawSelectionViewport", () => {
  it("keeps fit and 100 percent distinct and zooms around the pointer", () => {
    const viewport = useRawSelectionViewport();
    const target = elements(200, 100, 800, 400);

    viewport.fit("single", target);
    expect(viewport.single.zoom).toBe(0.13);
    viewport.showActualSize("single", target);
    expect(viewport.single.zoom).toBe(1);
    expect(viewport.single.panX).toBe(0);
    viewport.fit("single", target);
    expect(viewport.single.zoom).toBe(0.13);

    const event = new WheelEvent("wheel", {
      cancelable: true,
      clientX: 150,
      clientY: 50,
      ctrlKey: true,
      deltaY: -1,
    });
    expect(viewport.handleWheel("single", event, target)).toBe(true);
    expect(viewport.single.zoom).toBeCloseTo(0.1495);
    expect(viewport.single.panX).toBeCloseTo(-7.5);
    expect(event.defaultPrevented).toBe(true);
  });

  it("pans only the pointed side unless synchronization is enabled", async () => {
    const viewport = useRawSelectionViewport();
    viewport.compare[0].zoom = 2;
    viewport.compare[1].zoom = 2;
    viewport.compareSync.value = false;

    expect(viewport.startPan(1, new MouseEvent("mousedown", {
      button: 2,
      clientX: 10,
      clientY: 20,
    }))).toBe(true);
    viewport.movePan(new MouseEvent("mousemove", { clientX: 17, clientY: 29 }));
    viewport.endPan();
    expect(viewport.compare[0]).toMatchObject({ panX: 0, panY: 0 });
    expect(viewport.compare[1]).toMatchObject({ panX: 7, panY: 9 });

    viewport.compareSync.value = true;
    await nextTick();
    expect(viewport.compare[0]).toMatchObject({ zoom: 2, panX: 7, panY: 9 });
    expect(viewport.startPan(1, new MouseEvent("mousedown", {
      button: 2,
      clientX: 17,
      clientY: 29,
    }))).toBe(true);
    viewport.movePan(new MouseEvent("mousemove", { clientX: 20, clientY: 31 }));
    viewport.endPan();
    expect(viewport.compare[0]).toMatchObject({ panX: 10, panY: 11 });
    expect(viewport.compare[1]).toMatchObject({ panX: 10, panY: 11 });
  });

  it("uses the pointed compare side as the synchronized zoom anchor", () => {
    const viewport = useRawSelectionViewport();
    const left = elements();
    const right = elements();
    viewport.fit(0, left, right);
    const event = new WheelEvent("wheel", {
      cancelable: true,
      clientX: 150,
      clientY: 50,
      ctrlKey: true,
      deltaY: -1,
    });

    viewport.handleWheel(0, event, left, right);
    expect(viewport.compare[0].zoom).toBeCloseTo(0.1495);
    expect(viewport.compare[1].zoom).toBeCloseTo(0.1495);
    expect(viewport.compare[0].panX).toBeCloseTo(viewport.compare[1].panX);
  });

  it("keeps horizontal, vertical and square images fully fitted and maps 100% to one image pixel", () => {
    const cases = [
      elements(300, 200, 1200, 800, 300, 200),
      elements(300, 200, 800, 1200, 133.333, 200),
      elements(300, 200, 1000, 1000, 200, 200),
    ];
    const expectedFit = [0.19, 152 / 1200, 0.152];

    cases.forEach((target, index) => {
      const viewport = useRawSelectionViewport();
      viewport.fit("single", target);
      expect(viewport.single.zoom).toBeCloseTo(expectedFit[index] ?? 1);
      expect(viewport.single).toMatchObject({ panX: 0, panY: 0, mode: "fit" });
      const renderedWidth = target.image.naturalWidth * viewport.single.zoom;
      const renderedHeight = target.image.naturalHeight * viewport.single.zoom;
      expect((300 - renderedWidth) / 2).toBeGreaterThanOrEqual(23.999);
      expect((200 - renderedHeight) / 2).toBeGreaterThanOrEqual(23.999);
      viewport.showActualSize("single", target);
      expect(viewport.single.zoom).toBe(1);
      expect(viewport.single.mode).toBe("actual");
    });
  });

  it("recomputes only fit mode on resize and preserves actual or manual zoom", () => {
    const viewport = useRawSelectionViewport();
    const target = elements(400, 300, 800, 400, 400, 200);
    viewport.fit("single", target);
    expect(viewport.single.zoom).toBe(0.44);
    Object.defineProperty(target.container, "clientWidth", { configurable: true, value: 300 });
    Object.defineProperty(target.container, "clientHeight", { configurable: true, value: 200 });
    viewport.handleResize("single", target);
    expect(viewport.single).toMatchObject({ zoom: 0.315, mode: "fit" });

    viewport.showActualSize("single", target);
    expect(viewport.single.zoom).toBe(1);
    Object.defineProperty(target.container, "clientWidth", { configurable: true, value: 280 });
    Object.defineProperty(target.container, "clientHeight", { configurable: true, value: 180 });
    viewport.handleResize("single", target);
    expect(viewport.single).toMatchObject({ zoom: 1, mode: "actual" });

    viewport.fit("single", target);
    const wheel = new WheelEvent("wheel", {
      cancelable: true,
      clientX: 100,
      clientY: 50,
      ctrlKey: true,
      deltaY: -1,
    });
    viewport.handleWheel("single", wheel, target);
    const manualZoom = viewport.single.zoom;
    Object.defineProperty(target.container, "clientWidth", { configurable: true, value: 260 });
    Object.defineProperty(target.container, "clientHeight", { configurable: true, value: 160 });
    viewport.handleResize("single", target);
    expect(viewport.single.zoom).toBe(manualZoom);
    expect(viewport.single.mode).toBe("manual");
  });

  it("does not cap a narrow-window A7M4 100 percent view at the old 8x limit", () => {
    const viewport = useRawSelectionViewport();
    const target = elements(600, 400, 7008, 4672, 600, 400);
    viewport.fit("single", target);
    expect(viewport.single.zoom).toBeCloseTo(352 / 4672);
    expect(viewport.single.zoom).toBeLessThan(0.1);
    viewport.showActualSize("single", target);
    expect(viewport.single.zoom).toBe(1);
    expect(viewport.single.mode).toBe("actual");
  });

  it("fits both compare sides independently before synchronizing them at 100 percent", () => {
    const viewport = useRawSelectionViewport();
    const horizontal = elements(300, 200, 1200, 800);
    const vertical = elements(300, 200, 800, 1200);

    viewport.fit(0, horizontal, vertical);
    expect(viewport.compare[0]).toMatchObject({ zoom: 0.19, mode: "fit" });
    expect(viewport.compare[1].zoom).toBeCloseTo(152 / 1200);
    expect((300 - horizontal.image.naturalWidth * viewport.compare[0].zoom) / 2).toBeGreaterThanOrEqual(24);
    expect((200 - horizontal.image.naturalHeight * viewport.compare[0].zoom) / 2).toBeGreaterThanOrEqual(24);
    expect((300 - vertical.image.naturalWidth * viewport.compare[1].zoom) / 2).toBeGreaterThanOrEqual(24);
    expect((200 - vertical.image.naturalHeight * viewport.compare[1].zoom) / 2).toBeGreaterThanOrEqual(24);

    viewport.showActualSize(0, horizontal, vertical);
    expect(viewport.compare[0]).toMatchObject({ zoom: 1, mode: "actual" });
    expect(viewport.compare[1]).toMatchObject({ zoom: 1, mode: "actual" });
  });
});
