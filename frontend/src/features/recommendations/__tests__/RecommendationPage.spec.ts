import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RecommendationBatch } from "../types";

const state = vi.hoisted(() => ({
  currentBatch: { value: null as RecommendationBatch | null },
  loading: { value: false },
  error: { value: "" },
  shownError: { value: "" },
  visible: { value: false },
  shownPending: { value: false },
  refresh: vi.fn().mockResolvedValue(true),
  recordAction: vi.fn().mockResolvedValue(true),
  isActionPending: vi.fn().mockReturnValue(false),
  preferenceFor: vi.fn().mockReturnValue(""),
  setVisible: vi.fn(),
  retryShown: vi.fn().mockResolvedValue(true),
}));

vi.mock("../useRecommendations", () => ({
  useRecommendations: () => state,
}));

import RecommendationPage from "../RecommendationPage.vue";

const mountedWrappers: Array<{ unmount: () => void }> = [];

function batch(): RecommendationBatch {
  return {
    requestId: "request-1",
    batchId: "batch-1",
    count: 1,
    partial: true,
    partialReason: "insufficient_candidates",
    quotaDegraded: true,
    historyWindow: 45,
    quota: { quality: 5, recent: 0, low_exposure: 6, random: 4 },
    diversity: {
      applied: true,
      reason: "",
      missingVectors: 2,
      vectorSpace: "clip-test",
    },
    personalization: {
      applied: true,
      effectiveCount: 12,
      reason: "",
    },
    items: [{
      itemId: "item-1",
      mediaId: "media-1",
      name: "雷电将军.jpg",
      width: 1200,
      height: 1800,
      tags: ["角色"],
      libraryId: "library-1",
      libraryName: "人物图库",
      contentType: "image/jpeg",
      sizeBytes: 4096,
      bucket: "quality",
      thumbnailUrl: "http://localhost/api/image/media-1?variant=thumbnail",
      previewUrl: "http://localhost/api/image/media-1?variant=preview",
      preference: null,
    }],
  };
}

function openMenu(root: Element, x = 790, y = 590): MouseEvent {
  const event = new MouseEvent("contextmenu", {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
  });
  const media = root.querySelector(".recommendation-media");
  if (!media) throw new Error("Missing recommendation media");
  media.dispatchEvent(event);
  return event;
}

function menuButton(label: string): HTMLButtonElement {
  const button = Array.from(document.body.querySelectorAll<HTMLButtonElement>(
    ".gallery-context-menu button",
  )).find((candidate) => candidate.textContent?.trim() === label);
  if (!button) throw new Error(`Missing menu button: ${label}`);
  return button;
}

describe("RecommendationPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.currentBatch.value = null;
    state.loading.value = false;
    state.error.value = "";
    state.shownError.value = "";
    state.shownPending.value = false;
    state.preferenceFor.mockReturnValue("");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
  });

  afterEach(() => {
    mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount());
    document.body.innerHTML = "";
  });

  it("shows fifteen skeletons during the first atomic preload", () => {
    state.loading.value = true;
    const wrapper = mount(RecommendationPage, { props: { visible: true } });
    mountedWrappers.push(wrapper);

    expect(wrapper.findAll(".recommendation-skeleton")).toHaveLength(15);
    expect(state.setVisible).toHaveBeenCalledWith(true);
  });

  it("keeps an existing grid unobscured while the next batch reaches its critical preload", () => {
    state.currentBatch.value = batch();
    state.loading.value = true;
    const wrapper = mount(RecommendationPage, { props: { visible: true } });
    mountedWrappers.push(wrapper);

    expect(wrapper.find(".recommendation-grid").attributes("aria-busy")).toBe("true");
    expect(wrapper.find(".recommendation-media img").exists()).toBe(true);
    expect(wrapper.find(".recommendation-loading").exists()).toBe(false);
  });

  it("keeps refresh available while shown synchronization finishes in the background", () => {
    state.currentBatch.value = batch();
    state.shownPending.value = true;
    const wrapper = mount(RecommendationPage, { props: { visible: true } });
    mountedWrappers.push(wrapper);

    expect(wrapper.get(".refresh-button").attributes("disabled")).toBeUndefined();
    expect(wrapper.get(".refresh-button").text()).toBe("换一批");
  });

  it("renders image-only cards and records a successful image click", async () => {
    state.currentBatch.value = batch();
    const openImage = vi.fn().mockResolvedValue(true);
    const wrapper = mount(RecommendationPage, {
      props: { visible: true, openImage },
    });
    mountedWrappers.push(wrapper);

    expect(wrapper.get(".recommendation-status").text()).toContain("本批数量不足 15 张");
    expect(wrapper.get(".recommendation-status").text()).toContain("向量多样性已应用，2 张缺少向量");
    expect(wrapper.get(".recommendation-status").text()).toContain("个性化已应用（12 张有效偏好）");
    expect(wrapper.get(".recommendation-media span").text()).toBe("技术质量");
    expect(wrapper.text()).not.toContain("最近入库");
    expect(wrapper.get(".recommendation-media img").attributes("alt")).toBe("雷电将军.jpg");
    expect(wrapper.find(".recommendation-copy").exists()).toBe(false);
    expect(wrapper.find(".recommendation-actions").exists()).toBe(false);

    await wrapper.get(".recommendation-media").trigger("click");
    await flushPromises();
    expect(openImage).toHaveBeenCalledWith("media-1");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "open");
  });

  it("does not record open or export when the native operation fails", async () => {
    state.currentBatch.value = batch();
    const wrapper = mount(RecommendationPage, {
      props: {
        visible: true,
        openImage: vi.fn().mockResolvedValue(false),
        exportImage: vi.fn().mockResolvedValue(false),
      },
    });
    mountedWrappers.push(wrapper);

    await wrapper.get(".recommendation-media").trigger("click");
    openMenu(wrapper.element, 80, 80);
    await wrapper.vm.$nextTick();
    menuButton("导出到文件夹…").click();
    await flushPromises();

    expect(state.recordAction).not.toHaveBeenCalledWith("item-1", "open");
    expect(state.recordAction).not.toHaveBeenCalledWith("item-1", "export");
  });

  it("labels an idempotently replayed batch without claiming personalization was recomputed", () => {
    const replayed = batch();
    replayed.personalization = {
      applied: false,
      effectiveCount: 0,
      reason: "replayed",
    };
    state.currentBatch.value = replayed;
    const wrapper = mount(RecommendationPage, { props: { visible: true } });
    mountedWrappers.push(wrapper);

    expect(wrapper.get(".recommendation-status").text()).toContain(
      "当前批次为幂等回放，未重新计算个性化排序",
    );
  });

  it("opens a viewport-bounded single-image menu and prevents the browser menu", async () => {
    state.currentBatch.value = batch();
    const wrapper = mount(RecommendationPage, {
      attachTo: document.body,
      props: { visible: true },
    });
    mountedWrappers.push(wrapper);

    const event = openMenu(wrapper.element);
    await wrapper.vm.$nextTick();
    const menu = document.body.querySelector<HTMLElement>(".gallery-context-menu");

    expect(event.defaultPrevented).toBe(true);
    expect(menu).not.toBeNull();
    expect(menu?.style.left).toBe("554px");
    expect(menu?.style.top).toBe("192px");
    expect(menu?.textContent).not.toContain("已选择");
    expect(menu?.textContent).toContain("喜欢");
    expect(menu?.textContent).toContain("不喜欢");
  });

  it("routes every menu operation to only the current media and records only open/export", async () => {
    state.currentBatch.value = batch();
    const callbacks = {
      openImage: vi.fn().mockResolvedValue(true),
      revealImage: vi.fn().mockResolvedValue(true),
      copyImage: vi.fn().mockResolvedValue(true),
      copyFile: vi.fn().mockResolvedValue(true),
      copyPath: vi.fn().mockResolvedValue(true),
      exportImage: vi.fn().mockResolvedValue(true),
    };
    const wrapper = mount(RecommendationPage, {
      attachTo: document.body,
      props: { visible: true, ...callbacks },
    });
    mountedWrappers.push(wrapper);

    for (const label of [
      "系统打开",
      "所在文件夹",
      "复制图片",
      "复制文件",
      "复制文件路径",
      "导出到文件夹…",
      "喜欢",
      "不喜欢",
    ]) {
      openMenu(wrapper.element, 80, 80);
      await wrapper.vm.$nextTick();
      menuButton(label).click();
      await flushPromises();
    }

    for (const callback of Object.values(callbacks)) {
      expect(callback).toHaveBeenCalledOnce();
      expect(callback).toHaveBeenCalledWith("media-1");
    }
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "open");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "export");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "like");
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "dislike");
    expect(state.recordAction).toHaveBeenCalledTimes(4);
  });

  it("opens recommendation-only details and closes transient UI on escape, resize, outside click, and page change", async () => {
    state.currentBatch.value = batch();
    const wrapper = mount(RecommendationPage, {
      attachTo: document.body,
      props: { visible: true },
    });
    mountedWrappers.push(wrapper);

    openMenu(wrapper.element, 80, 80);
    await wrapper.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await wrapper.vm.$nextTick();
    expect(document.body.querySelector(".gallery-context-menu")).toBeNull();

    openMenu(wrapper.element, 80, 80);
    await wrapper.vm.$nextTick();
    menuButton("打开详情").click();
    await wrapper.vm.$nextTick();
    expect(document.body.textContent).toContain("雷电将军.jpg");
    expect(document.body.querySelector(".recommendation-detail")).not.toBeNull();
    expect(state.recordAction).not.toHaveBeenCalled();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await wrapper.vm.$nextTick();
    expect(document.body.querySelector(".recommendation-detail")).toBeNull();

    openMenu(wrapper.element, 80, 80);
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(document.body.querySelector(".gallery-context-menu")).toBeNull();

    openMenu(wrapper.element, 80, 80);
    window.dispatchEvent(new Event("resize"));
    await wrapper.vm.$nextTick();
    expect(document.body.querySelector(".gallery-context-menu")).toBeNull();

    openMenu(wrapper.element, 80, 80);
    await wrapper.setProps({ visible: false });
    expect(document.body.querySelector(".gallery-context-menu")).toBeNull();
  });

  it("prevents a repeated native operation for the same image while pending", async () => {
    state.currentBatch.value = batch();
    let finish!: (value: boolean) => void;
    const openImage = vi.fn(() => new Promise<boolean>((resolve) => {
      finish = resolve;
    }));
    const wrapper = mount(RecommendationPage, {
      attachTo: document.body,
      props: { visible: true, openImage },
    });
    mountedWrappers.push(wrapper);

    openMenu(wrapper.element, 80, 80);
    await wrapper.vm.$nextTick();
    menuButton("系统打开").click();
    await wrapper.vm.$nextTick();
    openMenu(wrapper.element, 80, 80);
    await wrapper.vm.$nextTick();
    expect(menuButton("系统打开").disabled).toBe(true);
    menuButton("系统打开").click();
    expect(openImage).toHaveBeenCalledOnce();

    finish(true);
    await flushPromises();
    expect(state.recordAction).toHaveBeenCalledWith("item-1", "open");
  });
});
