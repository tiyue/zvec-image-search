import { flushPromises, mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  clearProjectCache: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  importFolder: vi.fn(),
  listProjects: vi.fn(),
  renameProject: vi.fn(),
}));

vi.mock("../api", () => api);

import RawSelectionPage from "../RawSelectionPage.vue";

const WorkspaceStub = defineComponent({
  name: "RawSelectionWorkspace",
  emits: ["back", "selectFolder", "selectFiles", "toast"],
  setup(_props, { emit }) {
    return () => h("div", { "data-testid": "raw-workspace" }, [
      h("button", {
        type: "button",
        "data-testid": "request-files",
        onClick: () => emit("selectFiles", (paths: string[]) => paths),
      }, "选择图片"),
      h("button", {
        type: "button",
        "data-testid": "request-folder",
        onClick: () => emit("selectFolder", (path: string) => path),
      }, "选择文件夹"),
      h("button", { type: "button", "data-testid": "back", onClick: () => emit("back") }, "返回"),
    ]);
  },
});

describe("RawSelectionPage", () => {
  beforeEach(() => {
    api.listProjects.mockResolvedValue({
      projects: [{
        id: "project-1",
        name: "婚礼精选",
        member_count: 384,
        cover_member_ids: ["member-cover-1", "member-cover-2"],
        created_at: "2026-08-03T10:00:00Z",
        updated_at: "2026-08-03T12:00:00Z",
      }],
    });
    api.createProject.mockResolvedValue({ id: "project-2" });
  });

  afterEach(() => vi.clearAllMocks());

  it("closes the create modal on Escape and restores the trigger focus", async () => {
    const wrapper = mount(RawSelectionPage, { attachTo: document.body });
    await flushPromises();
    const trigger = wrapper.get(".rs-new-btn");
    await trigger.trigger("click");
    expect(wrapper.find(".rs-modal").exists()).toBe(true);
    expect(document.activeElement).toBe(wrapper.get(".rs-modal-input").element);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rs-modal").exists()).toBe(false);
    expect(document.activeElement).toBe(trigger.element);
    wrapper.unmount();
  });

  it("passes native multi-file and folder callbacks through to the workspace", async () => {
    const wrapper = mount(RawSelectionPage, {
      global: { stubs: { RawSelectionWorkspace: WorkspaceStub } },
    });
    await flushPromises();
    await wrapper.get("button[title='进入工作区']").trigger("click");
    await wrapper.get("[data-testid='request-files']").trigger("click");
    await wrapper.get("[data-testid='request-folder']").trigger("click");

    expect(wrapper.emitted("selectFiles")).toHaveLength(1);
    expect(wrapper.emitted("selectFolder")).toHaveLength(1);
    await wrapper.get("[data-testid='back']").trigger("click");
    await flushPromises();
    expect(api.listProjects).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it("renders only the first project member thumbnail as the cover", async () => {
    const wrapper = mount(RawSelectionPage);
    await flushPromises();

    const covers = wrapper.findAll<HTMLImageElement>(".rs-card-thumb img");
    expect(covers).toHaveLength(1);
    expect(covers[0]?.attributes("src")).toContain(
      "members/member-cover-1/thumbnail?",
    );
    expect(covers[0]?.attributes("src")).toContain("priority=background");
    expect(covers[0]?.attributes("src")).not.toContain("member-cover-2");
    wrapper.unmount();
  });
});
