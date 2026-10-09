import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RemoteAccessSection from "../RemoteAccessSection.vue";
import type { RemoteAccessApi, RemoteAccessStatusWire } from "../remoteTypes";

enableAutoUnmount(afterEach);

function payload(
  overrides: Partial<RemoteAccessStatusWire> = {},
): RemoteAccessStatusWire {
  return {
    configured: true,
    enabled: false,
    state: "disabled",
    message: "FRPC 配置已导入，开启后即可远程访问。",
    public_url: "http://39.105.48.52:38522",
    server_host: "39.105.48.52",
    server_port: 7000,
    remote_port: 38522,
    ...overrides,
  };
}

function unconfigured(): RemoteAccessStatusWire {
  return payload({
    configured: false,
    enabled: false,
    state: "unconfigured",
    public_url: "",
    server_host: "",
    server_port: null,
    remote_port: null,
    message: "尚未导入 FRPC 配置。",
  });
}

function fakeApi(overrides: Partial<RemoteAccessApi> = {}): RemoteAccessApi {
  return {
    status: vi.fn(async () => payload()),
    importConfig: vi.fn(async () =>
      payload({ configured: true, message: "FRPC 配置已导入，开启后即可远程访问。" }),
    ),
    update: vi.fn(async (body) =>
      payload({ enabled: body.enabled, state: body.enabled ? "connecting" : "disabled" }),
    ),
    start: vi.fn(async () => payload({ enabled: true, state: "connecting" })),
    stop: vi.fn(async () => payload({ enabled: false, state: "disabled" })),
    ...overrides,
  };
}

function buttonWithText(wrapper: Pick<VueWrapper, "findAll">, label: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  return button;
}

function stubBridge(implementation: () => Promise<unknown>): void {
  Object.defineProperty(window, "pywebview", {
    configurable: true,
    value: { api: { select_frpc_config_file: implementation } },
  });
}

describe("RemoteAccessSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    Reflect.deleteProperty(window, "pywebview");
  });

  it("shows an import entry and a placeholder address when unconfigured", async () => {
    const api = fakeApi({ status: vi.fn(async () => unconfigured()) });
    const wrapper = mount(RemoteAccessSection, { props: { api, visible: true } });
    await flushPromises();

    expect(wrapper.text()).toContain("未配置");
    expect(wrapper.text()).toContain("尚未配置");
    expect(wrapper.text()).toContain("尚未导入 FRPC 配置。");
    expect(buttonWithText(wrapper, "导入 FRPC 配置").exists()).toBe(true);
    expect(wrapper.text()).not.toContain("token");
  });

  it("enables a configured but disabled tunnel and shows the public address", async () => {
    const api = fakeApi();
    const wrapper = mount(RemoteAccessSection, { props: { api, visible: true } });
    await flushPromises();

    expect(wrapper.text()).toContain("已关闭");
    expect(wrapper.text()).toContain("39.105.48.52:38522");

    await buttonWithText(wrapper, "开启远程访问").trigger("click");
    await flushPromises();

    expect(api.update).toHaveBeenCalledWith({ enabled: true }, expect.any(AbortSignal));
    expect(wrapper.text()).toContain("连接中");
    expect(buttonWithText(wrapper, "关闭远程访问").exists()).toBe(true);
  });

  it("shows a connected tunnel with server info and a disable action", async () => {
    const api = fakeApi({
      status: vi.fn(async () =>
        payload({ enabled: true, state: "connected", message: "远程访问已连接，代理已注册。" }),
      ),
    });
    const wrapper = mount(RemoteAccessSection, { props: { api, visible: true } });
    await flushPromises();

    expect(wrapper.text()).toContain("已连接");
    expect(wrapper.text()).toContain("远程访问已连接，代理已注册。");
    expect(wrapper.text()).toContain("39.105.48.52:7000");
    expect(buttonWithText(wrapper, "关闭远程访问").exists()).toBe(true);
  });

  it("shows a retry action for an errored tunnel", async () => {
    const api = fakeApi({
      status: vi.fn(async () =>
        payload({ enabled: true, state: "error", message: "远程访问连接中断。" }),
      ),
    });
    const wrapper = mount(RemoteAccessSection, { props: { api, visible: true } });
    await flushPromises();

    expect(wrapper.text()).toContain("失败");
    expect(buttonWithText(wrapper, "重试连接").exists()).toBe(true);
  });

  it("allows disabling an errored enabled tunnel", async () => {
    const api = fakeApi({
      status: vi.fn(async () => payload({ enabled: true, state: "error" })),
    });
    const wrapper = mount(RemoteAccessSection, { props: { api, visible: true } });
    await flushPromises();
    await buttonWithText(wrapper, "关闭远程访问").trigger("click");
    await flushPromises();
    expect(api.update).toHaveBeenCalledWith({ enabled: false }, expect.any(AbortSignal));
    expect(wrapper.text()).toContain("已关闭");
  });

  it("keeps the import confirm disabled when the picker is cancelled", async () => {
    stubBridge(async () => ({ ok: true, path: null }));
    const wrapper = mount(RemoteAccessSection, {
      props: { api: fakeApi({ status: vi.fn(async () => unconfigured()) }), visible: true },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    expect(wrapper.find(".remote-modal").exists()).toBe(true);
    expect(wrapper.text()).toContain("尚未选择文件");

    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("尚未选择文件");
    expect(buttonWithText(wrapper, "导入并保存").attributes("disabled")).toBeDefined();
  });

  it("imports the selected TOML path and closes the modal", async () => {
    stubBridge(async () => ({ ok: true, path: "D:\\frp\\frpc.toml" }));
    const importConfig = vi.fn(async () => payload());
    const wrapper = mount(RemoteAccessSection, {
      props: {
        api: fakeApi({ importConfig, status: vi.fn(async () => unconfigured()) }),
        visible: true,
      },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("frpc.toml");

    await buttonWithText(wrapper, "导入并保存").trigger("click");
    await flushPromises();

    expect(importConfig).toHaveBeenCalledWith(
      { source_path: "D:\\frp\\frpc.toml" },
      expect.any(AbortSignal),
    );
    expect(wrapper.find(".remote-modal").exists()).toBe(false);
  });

  it("reports a picker failure while keeping the modal open", async () => {
    stubBridge(async () => ({ ok: false, error: "请选择 TOML 文件。" }));
    const wrapper = mount(RemoteAccessSection, {
      props: { api: fakeApi({ status: vi.fn(async () => unconfigured()) }), visible: true },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();

    expect(wrapper.find(".remote-modal").exists()).toBe(true);
    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "无法选择配置文件",
      "请选择 TOML 文件。",
      "error",
    ]);
  });

  it("explains when the desktop picker is unavailable", async () => {
    const wrapper = mount(RemoteAccessSection, {
      props: { api: fakeApi({ status: vi.fn(async () => unconfigured()) }), visible: true },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();

    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "无法选择配置文件",
      "桌面文件选择器尚未就绪，请稍后重试。",
      "error",
    ]);
  });

  it("keeps the modal open and reports an import failure", async () => {
    stubBridge(async () => ({ ok: true, path: "D:\\frp\\frpc.toml" }));
    const importConfig = vi.fn(async () => {
      throw new Error("FRPC 配置包含不受支持的顶层字段：foo");
    });
    const wrapper = mount(RemoteAccessSection, {
      props: {
        api: fakeApi({ importConfig, status: vi.fn(async () => unconfigured()) }),
        visible: true,
      },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();
    await buttonWithText(wrapper, "导入并保存").trigger("click");
    await flushPromises();

    expect(wrapper.find(".remote-modal").exists()).toBe(true);
    expect(wrapper.emitted("toast")?.at(-1)).toEqual([
      "无法导入 FRPC 配置",
      "FRPC 配置包含不受支持的顶层字段：foo",
      "error",
    ]);
  });

  it("closes the modal on outside click and on Escape", async () => {
    const wrapper = mount(RemoteAccessSection, {
      props: { api: fakeApi({ status: vi.fn(async () => unconfigured()) }), visible: true },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await wrapper.get(".remote-modal-backdrop").trigger("click");
    expect(wrapper.find(".remote-modal").exists()).toBe(false);

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await wrapper.get(".remote-modal").trigger("keydown", { key: "Escape" });
    expect(wrapper.find(".remote-modal").exists()).toBe(false);
  });

  it("blocks dismissal while an import is running", async () => {
    stubBridge(async () => ({ ok: true, path: "D:\\frp\\frpc.toml" }));
    let resolveImport: (value: RemoteAccessStatusWire) => void = () => {};
    const importConfig = vi.fn(
      () =>
        new Promise<RemoteAccessStatusWire>((resolve) => {
          resolveImport = resolve;
        }),
    );
    const wrapper = mount(RemoteAccessSection, {
      props: {
        api: fakeApi({ importConfig, status: vi.fn(async () => unconfigured()) }),
        visible: true,
      },
    });
    await flushPromises();

    await buttonWithText(wrapper, "导入 FRPC 配置").trigger("click");
    await buttonWithText(wrapper, "选择 TOML 文件").trigger("click");
    await flushPromises();
    await buttonWithText(wrapper, "导入并保存").trigger("click");
    await flushPromises();

    expect(wrapper.find(".remote-modal").exists()).toBe(true);
    expect(buttonWithText(wrapper, "取消").attributes("disabled")).toBeDefined();

    await wrapper.get(".remote-modal").trigger("keydown", { key: "Escape" });
    await wrapper.get(".remote-modal-backdrop").trigger("click");
    expect(wrapper.find(".remote-modal").exists()).toBe(true);

    resolveImport(payload());
    await flushPromises();
    expect(wrapper.find(".remote-modal").exists()).toBe(false);
  });
});
