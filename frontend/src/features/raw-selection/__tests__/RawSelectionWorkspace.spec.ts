import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ExportResult,
  ImportResult,
  RawMember,
  RawSelectionJob,
  WorkspaceState,
} from "../types";
import workspaceSource from "../RawSelectionWorkspace.vue?raw";

const api = vi.hoisted(() => ({
  cancelJob: vi.fn(),
  cancelProjectWork: vi.fn(),
  exportFiles: vi.fn(),
  getJob: vi.fn(),
  getMember: vi.fn(),
  getSourceStatus: vi.fn(),
  getWorkspaceState: vi.fn(),
  importFiles: vi.fn(),
  importFolder: vi.fn(),
  listCreativeLooks: vi.fn(),
  listMembers: vi.fn(),
  permanentDelete: vi.fn(),
  removeMembers: vi.fn(),
  saveWorkspaceState: vi.fn(),
  updateCreativeLook: vi.fn(),
  updateRating: vi.fn(),
}));

vi.mock("../api", () => api);

import RawSelectionWorkspace from "../RawSelectionWorkspace.vue";

function member(index: number): RawMember {
  return {
    id: `member-${index}`,
    project_id: "project-1",
    asset_id: `asset-${index}`,
    import_order: index,
    star_rating: index % 5,
    color_label: index === 1 ? "red" : "none",
    creative_look: "as_shot",
    file_name: `DSC_${String(index).padStart(4, "0")}.ARW`,
    normalized_path: `D:\\photos\\${index}.ARW`,
    extension: ".arw",
    file_size: 20_000_000 + index,
    mtime_ns: 100_000 + index,
    file_identity: `identity-${index}`,
    shot_time: index < 4 ? `2026-08-03T12:0${index}:00` : null,
    width: 7008,
    height: 4672,
    metadata_status: "ready",
    exported_at: index === 2 ? "2026-08-03T13:00:00Z" : null,
  };
}

const members = Array.from({ length: 8 }, (_, index) => member(index));

function importResult(overrides: Partial<ImportResult> = {}): ImportResult {
  return {
    registered: 3,
    skipped_unsupported: 0,
    skipped_raw_formats: 0,
    skipped_unsupported_camera: 0,
    skipped_reparse: 0,
    errors: 0,
    error_details: [],
    unsupported_camera_details: [],
    ...overrides,
  };
}

function exportResult(overrides: Partial<ExportResult> = {}): ExportResult {
  return {
    exported: 1,
    skipped: 0,
    failed: 0,
    cancelled: 0,
    log_id: "log-export-1",
    details: [],
    ...overrides,
  };
}

function selectionJob(
  kind: "import" | "export",
  overrides: Partial<RawSelectionJob> = {},
): RawSelectionJob {
  return {
    id: `job-${kind}-1`,
    kind,
    project_id: "project-1",
    log_id: null,
    status: "queued",
    phase: kind === "import" ? "scan" : "copy",
    progress: {},
    result: null,
    error: null,
    created_at: "2026-08-04T08:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

function workspaceState(overrides: Partial<WorkspaceState> = {}): WorkspaceState {
  return {
    last_member_id: "member-0",
    filter_star_mode: "none",
    filter_star_value: 0,
    filter_color_labels: "",
    filter_filename: "",
    filter_rated: "all",
    filter_exported: "all",
    filter_formats: "",
    filter_orientations: "",
    sort_field: "filename",
    sort_direction: "asc",
    filmstrip_scroll: 0,
    ...overrides,
  };
}

function buttonByText(wrapper: VueWrapper<any>, text: string) {
  const button = wrapper.findAll("button").find((candidate) => candidate.text().trim() === text);
  if (!button) throw new Error(`Missing button: ${text}`);
  return button;
}

function filterField(wrapper: VueWrapper<any>, label: string) {
  const field = wrapper.findAll(".rs-filter-field").find((candidate) => candidate.text().includes(label));
  if (!field) throw new Error(`Missing filter field: ${label}`);
  return field;
}

function filmOrder(wrapper: VueWrapper<any>): string[] {
  return wrapper.findAll(".rs-thumb-name").map((item) => item.text());
}

function currentFilmName(wrapper: VueWrapper<any>): string {
  return wrapper.get(".rs-thumb[aria-current='true'] .rs-thumb-name").text();
}

class PreloadImage {
  static urls: string[] = [];
  static aborted = 0;
  static instances: PreloadImage[] = [];
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private value = "";

  constructor() {
    PreloadImage.instances.push(this);
  }

  static completeWhere(fragment: string, naturalSize?: [number, number]): number {
    const matches = PreloadImage.instances.filter((item) =>
      item.src.includes(fragment) && item.onload !== null,
    );
    matches.forEach((item) => {
      if (naturalSize) {
        Object.defineProperties(item, {
          naturalWidth: { configurable: true, value: naturalSize[0] },
          naturalHeight: { configurable: true, value: naturalSize[1] },
        });
      }
      item.onload?.();
    });
    return matches.length;
  }

  set src(value: string) {
    this.value = value;
    if (!value) PreloadImage.aborted += 1;
    else PreloadImage.urls.push(value);
  }

  get src() { return this.value; }
}

describe("RawSelectionWorkspace", () => {
  const mounted: Array<{ unmount(): void }> = [];
  let width: ReturnType<typeof vi.spyOn>;
  let height: ReturnType<typeof vi.spyOn>;

  async function mountWorkspace() {
    const wrapper = mount(RawSelectionWorkspace, {
      attachTo: document.body,
      props: { projectId: "project-1", projectName: "婚礼精选" },
    });
    mounted.push(wrapper);
    await flushPromises();
    await wrapper.vm.$nextTick();
    return wrapper;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    PreloadImage.urls = [];
    PreloadImage.aborted = 0;
    PreloadImage.instances = [];
    vi.stubGlobal("Image", PreloadImage);
    vi.stubGlobal("confirm", vi.fn(() => true));
    width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
    height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(520);
    api.getWorkspaceState.mockResolvedValue(workspaceState());
    api.cancelJob.mockResolvedValue({ cancel_requested: true });
    api.cancelProjectWork.mockResolvedValue({ ok: true });
    api.listMembers.mockImplementation(async (_projectId: string, params: Record<string, string>) => ({
      members,
      total: members.length,
      filtered: params.filename || params.formats || params.orientations || params.exported || params.star_mode
        ? 4
        : members.length,
    }));
    api.getMember.mockImplementation(async (id: string) => members.find((item) => item.id === id));
    api.getSourceStatus.mockResolvedValue({
      status: "error",
      code: "source_missing",
      message: "源文件不存在或已被移动。",
    });
    api.getJob.mockResolvedValue(selectionJob("import", {
      status: "completed",
      result: importResult(),
      completed_at: "2026-08-04T08:01:00Z",
    }));
    api.listCreativeLooks.mockResolvedValue({
      looks: [
        { id: "as_shot", label: "拍摄时", description: "", calibration: "embedded" },
      ],
    });
    api.saveWorkspaceState.mockResolvedValue({ ok: true });
    api.updateRating.mockResolvedValue({ ok: true });
    api.updateCreativeLook.mockResolvedValue({ ok: true });
    api.removeMembers.mockResolvedValue({ removed: 1 });
    api.permanentDelete.mockResolvedValue({ deleted: 1, already_missing: 0, failed: 0, details: [] });
    api.exportFiles.mockResolvedValue(selectionJob("export", {
      status: "completed",
      result: exportResult(),
      completed_at: "2026-08-04T08:01:00Z",
    }));
    api.importFiles.mockResolvedValue(importResult({ registered: 2 }));
    api.importFolder.mockResolvedValue(selectionJob("import"));
  });

  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount());
    Reflect.deleteProperty(document, "hidden");
    width.mockRestore();
    height.mockRestore();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("places every operation below a control-free preview and exposes only two import commands", async () => {
    const wrapper = await mountWorkspace();
    const children = Array.from(wrapper.get(".rs-workspace").element.children)
      .filter((child) => !child.classList.contains("rs-task-stack"));
    expect(children[0]?.classList.contains("rs-preview-stage")).toBe(true);
    expect(children[1]?.classList.contains("rs-control-bar")).toBe(true);
    expect(children[2]?.classList.contains("rs-film-strip")).toBe(true);
    expect(wrapper.find(".rs-preview-stage button").exists()).toBe(false);
    expect(wrapper.find(".rs-preview-stage > .rs-preview-media").exists()).toBe(true);

    await wrapper.get("button[aria-label='导入']").trigger("click");
    expect(wrapper.get(".rs-menu").findAll("button").map((button) => button.text())).toEqual([
      "导入图片",
      "导入文件夹",
    ]);

    await buttonByText(wrapper, "导入图片").trigger("click");
    const callback = wrapper.emitted("selectFiles")?.[0]?.[0] as ((paths: string[]) => Promise<void>);
    await callback(["D:\\photos\\A.ARW", "D:\\photos\\B.JPG"]);
    expect(api.importFiles).toHaveBeenCalledWith(
      "project-1",
      ["D:\\photos\\A.ARW", "D:\\photos\\B.JPG"],
    );
  });

  it("shows only the two import entries when a project has no members", async () => {
    api.listMembers.mockResolvedValue({ members: [], total: 0, filtered: 0 });
    const wrapper = await mountWorkspace();

    expect(wrapper.get(".rs-empty-imports").findAll("button").map((button) => button.text())).toEqual([
      "导入图片",
      "导入文件夹",
    ]);
    expect(wrapper.find(".rs-preview-stage").exists()).toBe(false);
    expect(wrapper.find(".rs-filter-bar").exists()).toBe(false);
    expect(wrapper.find(".rs-control-bar").exists()).toBe(false);
    expect(wrapper.find(".rs-film-strip").exists()).toBe(false);
  });

  it("keeps uncalibrated creative looks unavailable and explains why", async () => {
    const wrapper = await mountWorkspace();
    const lookButton = wrapper.get(".rs-look-button");

    expect(lookButton.text()).toBe("拍摄时");
    expect(lookButton.attributes()).toHaveProperty("disabled");
    expect(lookButton.attributes("title")).toBe("未经可信参考校准的外观暂不可用");
    expect(wrapper.find(".rs-look-menu").exists()).toBe(false);
    expect(api.updateCreativeLook).not.toHaveBeenCalled();
  });

  it("progresses from thumbnail to embedded and best preview, then reports the source error", async () => {
    const wrapper = await mountWorkspace();
    const preview = () => wrapper.get<HTMLImageElement>(".rs-preview-img");

    expect(preview().attributes("src")).toContain("/thumbnail?");
    expect(PreloadImage.completeWhere("quality=embedded")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=embedded");

    expect(PreloadImage.completeWhere("quality=best")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=best");
    expect(preview().attributes("src")).toContain("look=as_shot");

    await preview().trigger("error");
    await flushPromises();
    expect(api.getSourceStatus).toHaveBeenCalledWith("member-0");
    expect(wrapper.get(".rs-preview-error").text()).toContain("源文件不存在或已被移动。");
  });

  it("does not downgrade an embedded preview to the thumbnail when its container resizes", async () => {
    const wrapper = await mountWorkspace();
    const preview = () => wrapper.get<HTMLImageElement>(".rs-preview-img");

    expect(PreloadImage.completeWhere("quality=embedded")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=embedded");

    width.mockReturnValue(760);
    window.dispatchEvent(new Event("resize"));
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(preview().attributes("src")).toContain("quality=embedded");
    expect(preview().attributes("src")).not.toContain("/thumbnail?");
  });

  it("keeps a best preview stable after image load and while a resized replacement loads", async () => {
    const wrapper = await mountWorkspace();
    const preview = () => wrapper.get<HTMLImageElement>(".rs-preview-img");

    expect(PreloadImage.completeWhere("quality=embedded", [1074, 716])).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    const embeddedScale = Number(
      preview().attributes("style")?.match(/scale\(([^)]+)\)/)?.[1],
    );

    expect(PreloadImage.completeWhere("quality=best", [7008, 4672])).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=best");
    const bestScale = Number(
      preview().attributes("style")?.match(/scale\(([^)]+)\)/)?.[1],
    );
    expect(embeddedScale * 1074).toBeCloseTo(bestScale * 7008, 5);
    expect(embeddedScale * 716).toBeCloseTo(bestScale * 4672, 5);

    await preview().trigger("load");
    await flushPromises();
    PreloadImage.completeWhere("quality=embedded");
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=best");

    width.mockReturnValue(760);
    window.dispatchEvent(new Event("resize"));
    await flushPromises();
    expect(PreloadImage.completeWhere("quality=embedded")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=best");

    expect(PreloadImage.completeWhere("quality=best")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(preview().attributes("src")).toContain("quality=best");
    expect(preview().attributes("src")).toContain("dw=760");
  });

  it("keeps preview, filter, controls and filmstrip in independent rows at narrow width", async () => {
    const wrapper = await mountWorkspace();
    const stage = wrapper.get(".rs-preview-stage").element as HTMLElement;
    const image = wrapper.get(".rs-preview-img").element as HTMLImageElement;
    stage.style.setProperty("--raw-preview-safe-inset", "12px");
    let stageHeight = 360;
    Object.defineProperties(stage, {
      clientWidth: { configurable: true, get: () => 480 },
      clientHeight: { configurable: true, get: () => stageHeight },
      getBoundingClientRect: {
        configurable: true,
        value: () => ({
          x: 0,
          y: 0,
          left: 0,
          top: 0,
          right: 480,
          bottom: stageHeight,
          width: 480,
          height: stageHeight,
          toJSON: () => ({}),
        }),
      },
    });
    Object.defineProperties(image, {
      naturalWidth: { configurable: true, value: 4000 },
      naturalHeight: { configurable: true, value: 6000 },
    });
    await wrapper.get(".rs-preview-img").trigger("load");
    await wrapper.vm.$nextTick();
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(0.056)");

    await buttonByText(wrapper, "100%").trigger("click");
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(1)");

    stageHeight = 240;
    await buttonByText(wrapper, "未筛选").trigger("click");
    await wrapper.vm.$nextTick();
    const rows = Array.from(wrapper.get(".rs-workspace").element.children)
      .filter((child) => !child.classList.contains("rs-task-stack"));
    expect(rows.map((row) => row.classList[0])).toEqual([
      "rs-preview-stage",
      "rs-filter-bar",
      "rs-control-bar",
      "rs-film-strip",
    ]);
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(0.036)");
    expect(rows.every((row) => !["absolute", "fixed"].includes(getComputedStyle(row).position))).toBe(true);

    expect(workspaceSource).toContain('grid-template-areas:\n    "preview"\n    "filters"\n    "controls"\n    "filmstrip"');
    expect(workspaceSource).toContain(".rs-workspace > .rs-film-strip { grid-area: filmstrip; }");
    expect(workspaceSource).toContain("--raw-preview-safe-inset: 24px");
    expect(workspaceSource).toContain(".rs-workspace { --raw-preview-safe-inset: 16px; }");
    expect(workspaceSource).toContain(".rs-workspace { --raw-preview-safe-inset: 12px; }");
    expect(workspaceSource).toContain("inset: var(--raw-preview-safe-inset)");
    expect(workspaceSource).toMatch(/\.rs-compare-panes\s*\{[\s\S]*?gap: 1px;/);
  });

  it("returns to a complete fit whenever the filter dock opens or closes", async () => {
    const wrapper = await mountWorkspace();
    const preview = wrapper.get(".rs-preview-stage");
    const strip = wrapper.get(".rs-film-strip").element as HTMLElement;
    Object.defineProperty(strip, "scrollLeft", { configurable: true, writable: true, value: 0 });
    preview.element.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: 120,
    }));
    await wrapper.vm.$nextTick();
    expect(strip.scrollLeft).toBe(120);

    preview.element.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      deltaY: -1,
      clientX: 500,
      clientY: 260,
    }));
    await wrapper.vm.$nextTick();
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(1.15)");

    await buttonByText(wrapper, "未筛选").trigger("click");
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(1)");
    await buttonByText(wrapper, "未筛选").trigger("click");
    expect(wrapper.get(".rs-preview-img").attributes("style")).toContain("scale(1)");
  });

  it("builds every confirmed filter and sort query and reports the real result count", async () => {
    const wrapper = await mountWorkspace();
    await buttonByText(wrapper, "未筛选").trigger("click");
    const rows = Array.from(wrapper.get(".rs-workspace").element.children)
      .filter((child) => !child.classList.contains("rs-task-stack"));
    expect(rows[0]?.classList.contains("rs-preview-stage")).toBe(true);
    expect(rows[1]?.classList.contains("rs-filter-bar")).toBe(true);
    expect(rows[2]?.classList.contains("rs-control-bar")).toBe(true);
    expect(rows[3]?.classList.contains("rs-film-strip")).toBe(true);
    expect(rows.every((row) => getComputedStyle(row).position !== "absolute")).toBe(true);
    await filterField(wrapper, "星级").get("select").setValue("exact");
    await wrapper.vm.$nextTick();
    await filterField(wrapper, "数量").get("select").setValue("3");
    await filterField(wrapper, "导出状态").get("select").setValue("unexported");
    await buttonByText(wrapper, "ARW").trigger("click");
    await buttonByText(wrapper, "横图").trigger("click");
    await filterField(wrapper, "文件名包含").get("input").setValue("DSC_0");
    await flushPromises();

    const params = api.listMembers.mock.calls.at(-1)?.[1] as Record<string, string>;
    expect(params).toMatchObject({
      star_mode: "exact",
      star_value: "3",
      exported: "unexported",
      formats: "arw",
      orientations: "landscape",
      filename: "DSC_0",
    });
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(buttonByText(wrapper, "已筛选 4/8").text()).toBe("已筛选 4/8");

    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "修改时间").trigger("click");
    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "降序").trigger("click");
    await flushPromises();
    expect(api.listMembers.mock.calls.at(-1)?.[1]).toMatchObject({ sort: "mtime_ns", dir: "desc" });
  });

  it("preserves server ID order and the current member while filtering and clearing", async () => {
    const ordered = [members[5], members[2], members[7], members[1]] as RawMember[];
    api.listMembers.mockImplementation(async (_projectId: string, params: Record<string, string>) => {
      const visible = params.filename ? [ordered[0], ordered[2]] : ordered;
      return { members: visible, total: ordered.length, filtered: visible.length };
    });
    const wrapper = await mountWorkspace();
    expect(filmOrder(wrapper)).toEqual(ordered.map((item) => item.file_name));

    await wrapper.findAll(".rs-thumb")[2]?.trigger("click");
    expect(currentFilmName(wrapper)).toBe(ordered[2]?.file_name);
    await buttonByText(wrapper, "未筛选").trigger("click");
    const filename = filterField(wrapper, "文件名包含").get("input");
    await filename.setValue("keep-current");
    await flushPromises();
    expect(filmOrder(wrapper)).toEqual([ordered[0]?.file_name, ordered[2]?.file_name]);
    expect(currentFilmName(wrapper)).toBe(ordered[2]?.file_name);

    const filteredCalls = api.listMembers.mock.calls.length;
    await filename.setValue("keep-current");
    await flushPromises();
    expect(api.listMembers).toHaveBeenCalledTimes(filteredCalls);

    await buttonByText(wrapper, "清除筛选").trigger("click");
    await flushPromises();
    expect(filmOrder(wrapper)).toEqual(ordered.map((item) => item.file_name));
    expect(currentFilmName(wrapper)).toBe(ordered[2]?.file_name);
  });

  it("changes order only through explicit sort state and ignores repeated identical choices", async () => {
    const imported = [members[3], members[1], members[6]] as RawMember[];
    const byName = [members[1], members[3], members[6]] as RawMember[];
    api.getWorkspaceState.mockResolvedValue(workspaceState({
      last_member_id: "member-3",
      sort_field: "import_order",
      sort_direction: "asc",
    }));
    api.listMembers.mockImplementation(async (_projectId: string, params: Record<string, string>) => {
      const visible = params.sort === "filename"
        ? params.dir === "desc" ? [...byName].reverse() : byName
        : imported;
      return { members: visible, total: visible.length, filtered: visible.length };
    });
    const wrapper = await mountWorkspace();
    expect(filmOrder(wrapper)).toEqual(imported.map((item) => item.file_name));
    expect(currentFilmName(wrapper)).toBe("DSC_0003.ARW");

    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "文件名").trigger("click");
    await flushPromises();
    expect(api.listMembers.mock.calls.at(-1)?.[1]).toMatchObject({ sort: "filename", dir: "asc" });
    expect(filmOrder(wrapper)).toEqual(byName.map((item) => item.file_name));
    expect(currentFilmName(wrapper)).toBe("DSC_0003.ARW");

    let calls = api.listMembers.mock.calls.length;
    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "文件名").trigger("click");
    await flushPromises();
    expect(api.listMembers).toHaveBeenCalledTimes(calls);

    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "降序").trigger("click");
    await flushPromises();
    expect(api.listMembers.mock.calls.at(-1)?.[1]).toMatchObject({ sort: "filename", dir: "desc" });
    expect(filmOrder(wrapper)).toEqual([...byName].reverse().map((item) => item.file_name));
    expect(currentFilmName(wrapper)).toBe("DSC_0003.ARW");

    calls = api.listMembers.mock.calls.length;
    await buttonByText(wrapper, "排序").trigger("click");
    await buttonByText(wrapper, "降序").trigger("click");
    await flushPromises();
    expect(api.listMembers).toHaveBeenCalledTimes(calls);
  });

  it("closes upward transient menus on outside click, Escape and resize, restoring focus on Escape", async () => {
    const wrapper = await mountWorkspace();
    const trigger = wrapper.get("button[aria-label='导入']");
    await trigger.trigger("click");
    expect(wrapper.find(".rs-menu").exists()).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rs-menu").exists()).toBe(false);
    expect(document.activeElement).toBe(trigger.element);

    await trigger.trigger("click");
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rs-menu").exists()).toBe(false);

    await trigger.trigger("click");
    window.dispatchEvent(new Event("resize"));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rs-menu").exists()).toBe(false);
    expect(document.activeElement).toBe(trigger.element);
  });

  it("uses two independent preview sources, an active compare side and restores current/selection on Escape", async () => {
    const wrapper = await mountWorkspace();
    const thumbs = wrapper.findAll(".rs-thumb");
    await thumbs[0]?.trigger("click", { ctrlKey: true });
    await thumbs[1]?.trigger("click", { ctrlKey: true });
    expect(buttonByText(wrapper, "已选 2").text()).toBe("已选 2");

    await buttonByText(wrapper, "对比视图").trigger("click");
    await flushPromises();
    const images = wrapper.findAll<HTMLImageElement>(".rs-compare-pane .rs-preview-img");
    expect(images).toHaveLength(2);
    expect(wrapper.findAll(".rs-compare-pane > .rs-preview-media")).toHaveLength(2);
    expect(images[0]?.attributes("src")).toContain("members/member-0/thumbnail");
    expect(images[1]?.attributes("src")).toContain("members/member-1/thumbnail");
    expect(wrapper.find(".rs-compare-pane button").exists()).toBe(false);
    expect(wrapper.findAll(".rs-compare-pane")[0]?.classes()).toContain("is-active");

    expect(PreloadImage.completeWhere("quality=embedded")).toBeGreaterThan(0);
    await flushPromises();
    expect(PreloadImage.completeWhere("quality=best")).toBeGreaterThan(0);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(images.every((image) => image.attributes("src")?.includes("quality=best") === true)).toBe(true);

    await Promise.all(images.map((image) => image.trigger("load")));
    await flushPromises();
    PreloadImage.completeWhere("quality=embedded");
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(images.every((image) => image.attributes("src")?.includes("quality=best") === true)).toBe(true);

    await wrapper.findAll(".rs-compare-pane")[1]?.trigger("click");
    expect(wrapper.findAll(".rs-compare-pane")[1]?.classes()).toContain("is-active");
    await wrapper.findAll(".rs-star")[0]?.trigger("click");
    expect(api.updateRating).toHaveBeenLastCalledWith("member-1", 1, "red");

    await wrapper.findAll(".rs-thumb")[3]?.trigger("click");
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rs-compare-panes").exists()).toBe(false);
    const current = wrapper.findAll(".rs-thumb").find((thumb) => thumb.attributes("aria-current") === "true");
    expect(current?.attributes("aria-label")).toContain("DSC_0001.ARW");
    expect(buttonByText(wrapper, "已选 2").text()).toBe("已选 2");
  });

  it("hydrates filters before the first list request and persists filter, sort and film scroll state", async () => {
    vi.useFakeTimers();
    api.getWorkspaceState.mockResolvedValue(workspaceState({
      last_member_id: "member-3",
      filter_exported: "unexported",
      filter_formats: "arw,jpeg",
      filter_orientations: "portrait",
      sort_field: "file_size",
      sort_direction: "desc",
      filmstrip_scroll: 172,
    }));
    const wrapper = await mountWorkspace();
    expect(api.listMembers.mock.calls[0]?.[1]).toMatchObject({
      exported: "unexported",
      formats: "arw,jpeg",
      orientations: "portrait",
      sort: "file_size",
      dir: "desc",
    });

    const strip = wrapper.get(".rs-film-strip").element as HTMLElement;
    Object.defineProperty(strip, "scrollLeft", { configurable: true, writable: true, value: 258 });
    await wrapper.get(".rs-film-strip").trigger("scroll");
    await vi.advanceTimersByTimeAsync(410);
    expect(api.saveWorkspaceState.mock.calls.at(-1)?.[1]).toMatchObject({
      last_member_id: "member-3",
      filter_exported: "unexported",
      filter_formats: "arw,jpeg",
      filter_orientations: "portrait",
      sort_field: "file_size",
      sort_direction: "desc",
      filmstrip_scroll: 258,
    });
  });

  it("starts bounded current/visible preloads and aborts the stale generation after navigation", async () => {
    const wrapper = await mountWorkspace();
    expect(PreloadImage.urls.some((url) => url.includes("priority=current"))).toBe(true);
    expect(PreloadImage.urls.some((url) => url.includes("priority=visible"))).toBe(true);
    const before = PreloadImage.aborted;
    await wrapper.findAll(".rs-thumb")[2]?.trigger("click");
    await wrapper.vm.$nextTick();
    expect(PreloadImage.aborted).toBeGreaterThan(before);
    expect(PreloadImage.urls.length).toBeLessThan(80);
  });

  it("cancels backend work when the document hides before scheduling new preloads", async () => {
    const wrapper = await mountWorkspace();
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(api.cancelProjectWork).toHaveBeenCalledWith("project-1");

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(wrapper.find(".rs-preview-stage").exists()).toBe(true);
  });

  it("shows the final explicit delete count and requires confirmation before the destructive request", async () => {
    const wrapper = await mountWorkspace();
    const thumbs = wrapper.findAll(".rs-thumb");
    await thumbs[0]?.trigger("click", { ctrlKey: true });
    await thumbs[1]?.trigger("click", { ctrlKey: true });
    await buttonByText(wrapper, "永久删除").trigger("click");
    await flushPromises();
    expect(wrapper.get(".rs-delete-modal").text()).toContain("将永久删除 2 个源文件");
    const confirmButton = wrapper.get(".rs-delete-confirm");
    expect(confirmButton.attributes()).toHaveProperty("disabled");
    await wrapper.get(".rs-delete-check input").setValue(true);
    await confirmButton.trigger("click");
    await flushPromises();
    expect(api.permanentDelete).toHaveBeenCalledWith(["member-0", "member-1"], true);
  });

  it("polls folder-import progress and sends cancellation to the active job", async () => {
    vi.useFakeTimers();
    api.importFolder.mockResolvedValueOnce(selectionJob("import", {
      status: "running",
      phase: "scan",
      progress: { registered: 1 },
    }));
    api.getJob
      .mockResolvedValueOnce(selectionJob("import", {
        status: "running",
        phase: "thumbnails",
        progress: { registered: 3, thumbnails_completed: 1, thumbnails_total: 3 },
      }))
      .mockResolvedValueOnce(selectionJob("import", {
        status: "cancelled",
        phase: "cancelled",
        progress: { registered: 3, thumbnails_completed: 1, thumbnails_total: 3 },
        result: importResult(),
        completed_at: "2026-08-04T08:01:00Z",
      }));

    const wrapper = await mountWorkspace();
    await wrapper.get("button[aria-label='导入']").trigger("click");
    await buttonByText(wrapper, "导入文件夹").trigger("click");
    const importFrom = wrapper.emitted("selectFolder")?.at(-1)?.[0] as
      ((path: string) => Promise<void>);
    await importFrom("D:\\wedding");
    await flushPromises();
    expect(api.importFolder).toHaveBeenCalledWith("project-1", "D:\\wedding");
    expect(wrapper.get(".rs-task-status").text()).toContain("正在扫描 · 已登记 1 张");

    await vi.advanceTimersByTimeAsync(351);
    await flushPromises();
    expect(api.getJob).toHaveBeenCalledWith("job-import-1");
    expect(wrapper.get(".rs-task-status").text()).toContain("已登记 3 张 · 缩略图 1/3");

    await buttonByText(wrapper, "取消导入").trigger("click");
    expect(api.cancelJob).toHaveBeenCalledWith("job-import-1");
    await vi.advanceTimersByTimeAsync(351);
    await flushPromises();
    expect(wrapper.emitted("toast")?.some((args) => args[0] === "导入已取消")).toBe(true);
  });

  it("does not export after picker cancellation and polls the export job before refreshing facts", async () => {
    vi.useFakeTimers();
    api.exportFiles.mockResolvedValueOnce(selectionJob("export", {
      status: "running",
      progress: { completed: 0, total: 1 },
    }));
    api.getJob.mockResolvedValueOnce(selectionJob("export", {
      status: "completed",
      phase: "completed",
      progress: { completed: 1, total: 1 },
      result: exportResult({
        exported: 1,
        failed: 1,
        details: [
          { member_id: "member-0", path: "D:\\photos\\0.ARW", result: "exported" },
          { member_id: "member-1", path: "D:\\photos\\1.ARW", result: "failed", error: "locked" },
        ],
      }),
      completed_at: "2026-08-04T08:01:00Z",
    }));
    const wrapper = await mountWorkspace();
    await buttonByText(wrapper, "导出").trigger("click");
    const cancelled = wrapper.emitted("selectFolder")?.at(-1)?.[0] as ((path: string) => Promise<void>);
    await cancelled("");
    expect(api.exportFiles).not.toHaveBeenCalled();

    const listCalls = api.listMembers.mock.calls.length;
    await buttonByText(wrapper, "导出").trigger("click");
    const exportTo = wrapper.emitted("selectFolder")?.at(-1)?.[0] as ((path: string) => Promise<void>);
    await exportTo("D:\\temporary-export");
    expect(api.exportFiles).toHaveBeenCalledWith("project-1", ["member-0"], "D:\\temporary-export");
    expect(wrapper.get(".rs-task-status").text()).toContain("正在导出 0/1");
    expect(wrapper.get(".rs-task-status progress").attributes()).toMatchObject({ value: "0", max: "1" });

    await vi.advanceTimersByTimeAsync(351);
    await flushPromises();
    expect(api.getJob).toHaveBeenCalledWith("job-export-1");
    expect(api.listMembers.mock.calls.length).toBeGreaterThan(listCalls);
    expect(wrapper.emitted("toast")?.some((args) => args[0] === "导出完成")).toBe(true);
  });
});
