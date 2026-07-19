import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "../SettingsPage.vue";
import type { SettingsApi, SettingsResponse } from "../types";

function settingsPayload(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    libraries: [{
      id: "lib-1",
      name: "人物图库",
      image_root: "D:\\Pictures",
      workspace_directory: "D:\\Zvec\\workspace",
      enabled: true,
      is_default: true,
    }],
    results_directory: "D:\\Zvec\\results",
    models: {
      provider: "aliyun",
      embedding_model: "qwen3-vl-embedding",
      auto_tag_primary_model: "qwen3-vl-flash",
      auto_tag_escalation_model: "qwen3-vl-plus",
      catalog: [
        { id: "qwen3-vl-embedding", display_name: "Qwen3 VL Embedding", roles: ["embedding"] },
        { id: "qwen3-vl-flash", display_name: "Qwen3 VL Flash", roles: ["auto_tag_primary"] },
        { id: "qwen3-vl-plus", display_name: "Qwen3 VL Plus", roles: ["auto_tag_escalation"] },
      ],
    },
    credentials: { configured: true, persistent: true },
    ...overrides,
  };
}

function fakeApi(): SettingsApi {
  return {
    settings: vi.fn(async () => settingsPayload()),
    addLibrary: vi.fn(async (body) => settingsPayload({
      libraries: [
        ...(settingsPayload().libraries ?? []),
        {
          id: "lib-2",
          name: body.name ?? "新增图库",
          image_root: body.image_root,
          workspace_directory: body.workspace_directory ?? "E:\\Zvec\\workspace",
          enabled: body.enabled ?? true,
          is_default: false,
        },
      ],
      results_directory: body.results_directory ?? "D:\\Zvec\\results",
    })),
    updateLibrary: vi.fn(async (_libraryId, body) => settingsPayload({
      libraries: [{
        id: "lib-1",
        name: body.name ?? "人物图库",
        image_root: body.image_root ?? "D:\\Pictures",
        workspace_directory: body.workspace_directory ?? "D:\\Zvec\\workspace",
        enabled: body.enabled ?? true,
        is_default: body.is_default ?? true,
      }],
      results_directory: body.results_directory ?? "D:\\Zvec\\results",
      restart_required: true,
      restart: { required: true, active_jobs: false },
    })),
    updateModels: vi.fn(async (body) => ({ ...body, provider: "aliyun", catalog: [] })),
    saveCredentials: vi.fn(async () => ({ configured: true, persistent: true })),
    deleteCredentials: vi.fn(async () => ({ configured: false, persistent: true })),
  };
}

function buttonWithText(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  return button;
}

describe("SettingsPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("loads editable library fields while never displaying the stored API key", async () => {
    const wrapper = mount(SettingsPage, { props: { api: fakeApi() } });
    await flushPromises();

    expect(wrapper.get('.library-editor input[name="name"]').element).toHaveProperty("value", "人物图库");
    expect(wrapper.get('.library-editor input[name="image_root"]').element).toHaveProperty("value", "D:\\Pictures");
    expect(wrapper.get('.library-editor input[name="workspace_directory"]').element).toHaveProperty("value", "D:\\Zvec\\workspace");
    expect(wrapper.get('input[name="api_key"]').attributes("type")).toBe("password");
    expect((wrapper.get('input[name="api_key"]').element as HTMLInputElement).value).toBe("");
    expect(wrapper.text()).not.toContain("sk-");
    expect(wrapper.text()).toContain("已配置");
  });

  it("validates and saves all editable library fields plus the global results directory", async () => {
    const api = fakeApi();
    const wrapper = mount(SettingsPage, { props: { api } });
    await flushPromises();

    await wrapper.get('.library-editor input[name="name"]').setValue("Cosplay 图库");
    await wrapper.get('.library-editor input[name="image_root"]').setValue("E:\\Cosplay");
    await wrapper.get('.library-editor input[name="workspace_directory"]').setValue("E:\\Zvec\\workspace");
    await wrapper.get('.results-form input[name="results_directory"]').setValue("E:\\Zvec\\results");
    await wrapper.get(".library-editor").trigger("submit");
    await flushPromises();

    expect(api.updateLibrary).toHaveBeenCalledWith("lib-1", {
      name: "Cosplay 图库",
      image_root: "E:\\Cosplay",
      workspace_directory: "E:\\Zvec\\workspace",
      enabled: true,
      is_default: true,
      results_directory: "E:\\Zvec\\results",
    });
    expect(wrapper.emitted("libraries-updated")?.at(-1)?.[0]).toEqual([
      expect.objectContaining({ id: "lib-1", name: "Cosplay 图库", imageRoot: "E:\\Cosplay" }),
    ]);
    expect(wrapper.text()).toContain("重启软件后新路径生效");
  });

  it("rejects relative Windows paths without sending a save request", async () => {
    const api = fakeApi();
    const wrapper = mount(SettingsPage, { props: { api } });
    await flushPromises();

    await wrapper.get('.library-editor input[name="image_root"]').setValue("relative/images");
    await wrapper.get(".library-editor").trigger("submit");
    await flushPromises();

    expect(api.updateLibrary).not.toHaveBeenCalled();
    expect(wrapper.emitted("toast")?.at(-1)?.[0]).toBe("图库设置无效");
    expect(String(wrapper.emitted("toast")?.at(-1)?.[1])).toContain("Windows 绝对路径");
  });

  it("saves the global result directory through the default library endpoint", async () => {
    const api = fakeApi();
    const wrapper = mount(SettingsPage, { props: { api } });
    await flushPromises();

    await wrapper.get('.results-form input[name="results_directory"]').setValue("F:\\SearchResults");
    await wrapper.get(".results-form").trigger("submit");
    await flushPromises();

    expect(api.updateLibrary).toHaveBeenCalledWith("lib-1", {
      results_directory: "F:\\SearchResults",
    });
  });

  it("updates three model roles and clears credentials before or after every write", async () => {
    const api = fakeApi();
    const wrapper = mount(SettingsPage, { props: { api } });
    await flushPromises();

    await wrapper.get(".model-form").trigger("submit");
    await flushPromises();
    expect(api.updateModels).toHaveBeenCalledWith({
      embedding_model: "qwen3-vl-embedding",
      auto_tag_primary_model: "qwen3-vl-flash",
      auto_tag_escalation_model: "qwen3-vl-plus",
    });

    const keyInput = wrapper.get('input[name="api_key"]');
    await keyInput.setValue("sk-test-secret");
    await wrapper.get(".credential-form").trigger("submit");
    expect((keyInput.element as HTMLInputElement).value).toBe("");
    await flushPromises();
    expect(api.saveCredentials).toHaveBeenCalledWith("sk-test-secret");
    expect(wrapper.text()).not.toContain("sk-test-secret");

    await buttonWithText(wrapper, "删除已保存密钥").trigger("click");
    await flushPromises();
    expect(api.deleteCredentials).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("未配置");
  });
});
