import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RawSelectionFilmstrip from "../RawSelectionFilmstrip.vue";
import type { RawMember } from "../types";

function member(index: number): RawMember {
  return {
    id: `member-${index}`,
    project_id: "project-1",
    asset_id: `asset-${index}`,
    import_order: index,
    star_rating: index % 5,
    color_label: "none",
    creative_look: "as_shot",
    file_name: `DSC_${String(index).padStart(4, "0")}.ARW`,
    normalized_path: `D:\\photos\\${index}.ARW`,
    extension: ".arw",
    file_size: 1_000 + index,
    mtime_ns: 10_000 + index,
    file_identity: `identity-${index}`,
    shot_time: null,
    width: 7008,
    height: 4672,
    metadata_status: "ready",
    exported_at: null,
  };
}

describe("RawSelectionFilmstrip", () => {
  let width: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(430);
  });

  afterEach(() => width.mockRestore());

  it("renders only the viewport plus two screens on each side for 1000 members", async () => {
    const wrapper = mount(RawSelectionFilmstrip, {
      props: {
        members: Array.from({ length: 1000 }, (_, index) => member(index)),
        currentIndex: 0,
        selectedIds: new Set<string>(),
        initialScroll: 0,
      },
    });

    await wrapper.vm.$nextTick();
    expect(wrapper.findAll(".rs-thumb")).toHaveLength(15);
    expect(wrapper.findAll(".rs-thumb-img")).toHaveLength(15);
    expect(wrapper.findAll(".rs-thumb-img")[0]?.attributes("src")).toContain("priority=visible");
    expect(wrapper.findAll(".rs-thumb-img")[5]?.attributes("src")).toContain("priority=overscan");
    expect(wrapper.get(".rs-thumb").attributes("aria-current")).toBe("true");
    wrapper.unmount();
  });

  it("keeps current activation separate from Ctrl and Shift explicit selection", async () => {
    const wrapper = mount(RawSelectionFilmstrip, {
      props: {
        members: Array.from({ length: 20 }, (_, index) => member(index)),
        currentIndex: 0,
        selectedIds: new Set<string>(),
        initialScroll: 0,
      },
    });
    const thumbnails = wrapper.findAll(".rs-thumb");

    await thumbnails[1]?.trigger("click");
    expect(wrapper.emitted("activate")?.at(-1)).toEqual([1]);
    expect(wrapper.emitted("selection")).toBeUndefined();

    await thumbnails[2]?.trigger("click", { ctrlKey: true });
    expect([...((wrapper.emitted("selection")?.at(-1)?.[0] as Set<string>))]).toEqual(["member-2"]);
    await wrapper.setProps({ selectedIds: new Set(["member-2"]), currentIndex: 2 });
    await thumbnails[5]?.trigger("click", { shiftKey: true });
    expect([...((wrapper.emitted("selection")?.at(-1)?.[0] as Set<string>))]).toEqual([
      "member-2",
      "member-3",
      "member-4",
      "member-5",
    ]);
    wrapper.unmount();
  });

  it("emits scroll state and shows a non-color-only explicit selection marker", async () => {
    const wrapper = mount(RawSelectionFilmstrip, {
      props: {
        members: Array.from({ length: 20 }, (_, index) => member(index)),
        currentIndex: 3,
        selectedIds: new Set(["member-3"]),
        initialScroll: 0,
      },
    });
    const strip = wrapper.get(".rs-film-strip").element as HTMLElement;
    Object.defineProperty(strip, "scrollLeft", { configurable: true, writable: true, value: 172 });
    await wrapper.get(".rs-film-strip").trigger("scroll");

    expect(wrapper.emitted("scroll")?.at(-1)).toEqual([172]);
    const current = wrapper.findAll(".rs-thumb")[3];
    expect(current?.classes()).toEqual(expect.arrayContaining(["is-current", "is-selected"]));
    expect(current?.find(".rs-thumb-selection").text()).toBe("✓");
    expect(current?.attributes("aria-pressed")).toBe("true");
    wrapper.unmount();
  });
});
