import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import {
  useImageViewerSession,
  useViewerGeometry,
  type ViewerImage,
} from "../useImageViewer";

function image(id: string, overrides: Partial<ViewerImage> = {}): ViewerImage {
  return {
    id,
    name: `${id}.jpg`,
    relativePath: `folder/${id}.jpg`,
    libraryName: "图库",
    width: 800,
    height: 600,
    tags: ["标签"],
    thumbnailUrl: `api/image/${id}?variant=thumbnail`,
    imageUrl: `api/image/${id}?variant=preview`,
    nativeId: id,
    ...overrides,
  };
}

function fakeStage(width: number, height: number): HTMLElement {
  return {
    clientWidth: width,
    clientHeight: height,
    getBoundingClientRect: () => ({
      left: 0,
      top: 0,
      width,
      height,
      right: width,
      bottom: height,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }),
    setPointerCapture: () => undefined,
    releasePointerCapture: () => undefined,
  } as unknown as HTMLElement;
}

function pointer(type: string, x: number, y: number): PointerEvent {
  return { button: 0, pointerId: 1, clientX: x, clientY: y, currentTarget: null, type } as
    unknown as PointerEvent;
}

describe("useImageViewerSession", () => {
  function makeSession(overrides: {
    items: () => ViewerImage[];
    hasMore?: (direction: -1 | 1) => boolean;
    loadAdjacent?: (direction: -1 | 1) => Promise<string | null>;
  }) {
    return useImageViewerSession({
      items: overrides.items,
      startId: () => overrides.items()[0]?.id ?? "",
      hasMore: overrides.hasMore ?? (() => false),
      loadAdjacent: overrides.loadAdjacent ?? (async () => null),
    });
  }

  it("opens on the requested image and resolves it from the current window", () => {
    const items = ref([image("a"), image("b")]);
    const session = makeSession({ items: () => items.value });

    session.open("b");
    expect(session.visible.value).toBe(true);
    expect(session.current.value?.id).toBe("b");
    expect(session.hasNext.value).toBe(false);
    expect(session.hasPrevious.value).toBe(true);

    session.close();
    expect(session.visible.value).toBe(false);
    expect(session.current.value).toBeNull();
  });

  it("never opens without a usable target", () => {
    const session = makeSession({ items: () => [] });
    session.open("missing");
    expect(session.visible.value).toBe(false);
  });

  it("ignores an unknown id and only falls back to a materialised start id", () => {
    const items = ref([image("a"), image("b")]);
    const session = makeSession({ items: () => items.value });

    session.open("missing");
    expect(session.visible.value).toBe(false);
    expect(session.current.value).toBeNull();

    session.open("");
    expect(session.visible.value).toBe(true);
    expect(session.currentId.value).toBe("a");
  });

  it("never opens an entry that has no displayable image URL", () => {
    const items = ref([image("a", { thumbnailUrl: "", imageUrl: "" }), image("b")]);
    const session = makeSession({ items: () => items.value });

    session.open("a");
    expect(session.visible.value).toBe(false);
    expect(session.current.value).toBeNull();
  });

  it("does not open from a start id whose entry has no image URL", () => {
    const items = ref([image("a", { thumbnailUrl: "", imageUrl: "" })]);
    const session = makeSession({ items: () => items.value });

    session.open("");
    expect(session.visible.value).toBe(false);
    expect(session.current.value).toBeNull();
  });

  it("opens a thumbnail-only entry", () => {
    const items = ref([image("a", { imageUrl: "" })]);
    const session = makeSession({ items: () => items.value });

    session.open("a");
    expect(session.visible.value).toBe(true);
    expect(session.current.value?.id).toBe("a");
  });

  it("opens a full-image-only entry", () => {
    const items = ref([image("a", { thumbnailUrl: "" })]);
    const session = makeSession({ items: () => items.value });

    session.open("a");
    expect(session.visible.value).toBe(true);
    expect(session.current.value?.id).toBe("a");
  });

  it("steps inside the loaded window without touching the loader", async () => {
    const items = ref([image("a"), image("b"), image("c")]);
    const loadAdjacent = vi.fn(async () => null);
    const session = makeSession({ items: () => items.value, loadAdjacent });

    session.open("a");
    await session.step(1);
    expect(session.currentId.value).toBe("b");
    await session.step(-1);
    expect(session.currentId.value).toBe("a");
    expect(loadAdjacent).not.toHaveBeenCalled();
  });

  it("keeps showing the current image while the window swaps between pages", () => {
    const items = ref([image("a"), image("b")]);
    const session = makeSession({ items: () => items.value });

    session.open("b");
    expect(session.current.value?.id).toBe("b");

    // The next page is applied before the new id is adopted.
    items.value = [image("c")];
    expect(session.current.value?.id).toBe("b");

    session.close();
    expect(session.current.value).toBeNull();
  });

  it("loads the adjacent page once at the window boundary and adopts its id", async () => {
    const items = ref([image("a"), image("b")]);
    const loadAdjacent = vi.fn(async () => "c");
    const session = makeSession({
      items: () => items.value,
      hasMore: () => true,
      loadAdjacent,
    });

    session.open("b");
    await session.step(1);

    expect(loadAdjacent).toHaveBeenCalledWith(1);
    expect(session.currentId.value).toBe("c");
  });

  it("keeps the current image and exposes a retry when a page load fails", async () => {
    const items = ref([image("a")]);
    const loadAdjacent = vi.fn(async (_direction: -1 | 1) => null as string | null);
    loadAdjacent.mockResolvedValueOnce(null).mockResolvedValueOnce("b");
    const session = makeSession({
      items: () => items.value,
      hasMore: () => true,
      loadAdjacent,
    });

    session.open("a");
    await session.step(1);
    expect(session.currentId.value).toBe("a");
    expect(session.error.value).toBe("无法读取下一页图片");
    expect(session.loading.value).toBe(false);

    await session.retry();
    expect(session.error.value).toBe("");
    expect(session.currentId.value).toBe("b");
  });

  it("ignores a late page response after the viewer was closed", async () => {
    const items = ref([image("a")]);
    let release: (value: string | null) => void = () => undefined;
    const session = makeSession({
      items: () => items.value,
      hasMore: () => true,
      loadAdjacent: () => new Promise((resolve) => { release = resolve; }),
    });

    session.open("a");
    const pending = session.step(1);
    session.close();
    release("b");
    await pending;

    expect(session.visible.value).toBe(false);
    expect(session.currentId.value).toBe("");
  });

  it("coalesces repeated presses while a page is still loading", async () => {
    const items = ref([image("a")]);
    let release: (value: string | null) => void = () => undefined;
    const loadAdjacent = vi.fn(
      () => new Promise<string | null>((resolve) => { release = resolve; }),
    );
    const session = makeSession({ items: () => items.value, hasMore: () => true, loadAdjacent });

    session.open("a");
    const first = session.step(1);
    await session.step(1);
    expect(loadAdjacent).toHaveBeenCalledTimes(1);
    release("b");
    await first;
    expect(session.currentId.value).toBe("b");
  });
});

describe("useViewerGeometry", () => {
  it("fits the real pixel size into the stage and reports the zoom label", () => {
    const geometry = useViewerGeometry();
    geometry.setNaturalSize(800, 600);
    geometry.stageRef.value = fakeStage(400, 300);
    geometry.resetToFit();

    expect(geometry.scale.value).toBeCloseTo(0.5);
    expect(geometry.isFit.value).toBe(true);
    expect(geometry.zoomLabel.value).toBe("50%");
  });

  it("toggles between fit and 100% with the labelled controls", () => {
    const geometry = useViewerGeometry();
    geometry.setNaturalSize(800, 600);
    geometry.stageRef.value = fakeStage(400, 300);
    geometry.resetToFit();

    geometry.setActual();
    expect(geometry.scale.value).toBe(1);
    expect(geometry.isFit.value).toBe(false);
    expect(geometry.zoomLabel.value).toBe("100%");

    geometry.toggleFit();
    expect(geometry.isFit.value).toBe(true);
    expect(geometry.zoomLabel.value).toBe("50%");
  });

  it("zooms around the pointer and clamps the pan to the visible area", () => {
    const geometry = useViewerGeometry();
    geometry.setNaturalSize(800, 600);
    geometry.stageRef.value = fakeStage(400, 300);
    geometry.resetToFit();

    // The pointer sits 50px right/below the stage centre at scale 0.5.
    geometry.handleWheel({
      deltaY: -400,
      clientX: 250,
      clientY: 200,
      preventDefault: () => undefined,
    } as unknown as WheelEvent);
    expect(geometry.scale.value).toBeGreaterThan(0.5);
    expect(geometry.isFit.value).toBe(false);

    geometry.setActual();
    geometry.startPan(pointer("pointerdown", 0, 0));
    geometry.movePan(pointer("pointermove", 4000, 4000));
    expect(geometry.offsetX.value).toBeCloseTo(200);
    expect(geometry.offsetY.value).toBeCloseTo(150);
    geometry.movePan(pointer("pointermove", -4000, -4000));
    expect(geometry.offsetX.value).toBeCloseTo(-200);
    expect(geometry.offsetY.value).toBeCloseTo(-150);
    geometry.endPan(pointer("pointerup", -4000, -4000));
    expect(geometry.dragging.value).toBe(false);
  });

  it("keeps a fitted image centred and re-fits after a window resize", () => {
    const geometry = useViewerGeometry();
    geometry.setNaturalSize(800, 600);
    geometry.stageRef.value = fakeStage(400, 300);
    geometry.resetToFit();

    geometry.setActual();
    geometry.startPan(pointer("pointerdown", 0, 0));
    geometry.movePan(pointer("pointermove", 100, 100));
    geometry.endPan(pointer("pointerup", 100, 100));
    expect(geometry.offsetX.value).not.toBe(0);

    geometry.setFit();
    expect(geometry.offsetX.value).toBe(0);
    expect(geometry.offsetY.value).toBe(0);
    expect(geometry.scale.value).toBeCloseTo(0.5);

    geometry.stageRef.value = fakeStage(800, 600);
    geometry.remeasure();
    expect(geometry.scale.value).toBeCloseTo(1);
    expect(geometry.isFit.value).toBe(true);
  });

  it("ignores a pan that starts on an interactive control", () => {
    const geometry = useViewerGeometry();
    geometry.setNaturalSize(800, 600);
    geometry.stageRef.value = fakeStage(400, 300);
    geometry.resetToFit();

    const stage = document.createElement("div");
    const press = (target: EventTarget) =>
      ({
        button: 0,
        pointerId: 1,
        clientX: 5,
        clientY: 5,
        target,
        currentTarget: stage,
      }) as unknown as PointerEvent;

    geometry.startPan(press(document.createElement("button")));
    expect(geometry.dragging.value).toBe(false);

    geometry.startPan(press(stage));
    expect(geometry.dragging.value).toBe(true);
    geometry.endPan(press(stage));
    expect(geometry.dragging.value).toBe(false);
  });
});
