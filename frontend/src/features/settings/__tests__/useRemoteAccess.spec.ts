import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RemoteAccessApi, RemoteAccessStatusWire } from "../remoteTypes";
import { normalizeRemoteAccessStatus, useRemoteAccess } from "../useRemoteAccess";

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

function fakeApi(overrides: Partial<RemoteAccessApi> = {}): RemoteAccessApi {
  return {
    status: vi.fn(async () => payload()),
    importConfig: vi.fn(async () =>
      payload({ message: "FRPC 配置已导入，开启后即可远程访问。" }),
    ),
    update: vi.fn(async (body) =>
      payload({ enabled: body.enabled, state: body.enabled ? "connecting" : "disabled" }),
    ),
    start: vi.fn(async () => payload({ enabled: true, state: "connecting" })),
    stop: vi.fn(async () => payload({ enabled: false, state: "disabled" })),
    ...overrides,
  };
}

let toasts: Array<[string, string, string]> = [];

function mountRemote(
  api: RemoteAccessApi,
  visible = true,
): { state: ReturnType<typeof useRemoteAccess>; wrapper: VueWrapper } {
  let state: ReturnType<typeof useRemoteAccess> | undefined;
  const wrapper = mount(
    defineComponent({
      setup() {
        state = useRemoteAccess(
          api,
          { onToast: (title, message, kind) => toasts.push([title, message, kind]) },
          { visible },
        );
        return () => h("div");
      },
    }),
  );
  if (!state) throw new Error("useRemoteAccess test harness did not initialize");
  return { state, wrapper };
}

describe("useRemoteAccess", () => {
  beforeEach(() => {
    toasts = [];
  });
  afterEach(() => {
    vi.useRealTimers();
    Reflect.deleteProperty(document, "hidden");
  });

  it("normalizes unknown states and ignores invalid ports", () => {
    const normalized = normalizeRemoteAccessStatus({
      configured: true,
      enabled: true,
      state: "mystery",
      public_url: "  http://1.2.3.4:38522  ",
      server_port: 0,
      remote_port: "not-a-port",
    });
    expect(normalized.state).toBe("connecting");
    expect(normalized.publicUrl).toBe("http://1.2.3.4:38522");
    expect(normalized.serverPort).toBeNull();
    expect(normalized.remotePort).toBeNull();
    expect(
      normalizeRemoteAccessStatus({ configured: false, enabled: true }).state,
    ).toBe("unconfigured");
    expect(
      normalizeRemoteAccessStatus({ configured: true, enabled: false, state: "connected" }).state,
    ).toBe("connected");
  });

  it("loads once on mount and does not poll while the remote is disabled", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    const { state } = mountRemote(api);
    await flushPromises();

    expect(api.status).toHaveBeenCalledTimes(1);
    expect(state.status.value?.state).toBe("disabled");

    await vi.advanceTimersByTimeAsync(6_000);
    expect(api.status).toHaveBeenCalledTimes(1);
  });

  it("loads when the section becomes visible and never while hidden", async () => {
    const api = fakeApi();
    const { state } = mountRemote(api, false);
    await flushPromises();
    expect(api.status).not.toHaveBeenCalled();

    state.setVisible(true);
    await flushPromises();
    expect(api.status).toHaveBeenCalledTimes(1);

    state.setVisible(true);
    await flushPromises();
    expect(api.status).toHaveBeenCalledTimes(1);
  });

  it("polls while visible and stops after unmount", async () => {
    vi.useFakeTimers();
    const api = fakeApi({
      status: vi.fn(async () => payload({ enabled: true, state: "connected" })),
    });
    const { state, wrapper } = mountRemote(api);
    await flushPromises();

    expect(state.status.value?.state).toBe("connected");
    expect(api.status).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(api.status).toHaveBeenCalledTimes(2);

    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(6_000);
    expect(api.status).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale status response after a manual refresh", async () => {
    const pending: Array<(value: RemoteAccessStatusWire) => void> = [];
    const api = fakeApi({
      status: vi.fn(
        () =>
          new Promise<RemoteAccessStatusWire>((resolve) => {
            pending.push(resolve);
          }),
      ),
    });
    const { state } = mountRemote(api);
    await flushPromises();
    expect(state.loading.value).toBe(true);

    void state.refresh();
    await flushPromises();
    expect(api.status).toHaveBeenCalledTimes(2);

    pending[0](payload({ enabled: true, state: "connected", public_url: "http://old:1" }));
    await flushPromises();
    expect(state.status.value).toBeNull();

    pending[1](payload({ enabled: true, state: "connected", public_url: "http://new:2" }));
    await flushPromises();
    expect(state.status.value?.publicUrl).toBe("http://new:2");
  });

  it("does not fire a poll while a mutation is in flight", async () => {
    vi.useFakeTimers();
    let resolveUpdate: (value: RemoteAccessStatusWire) => void = () => {};
    const update = vi.fn(
      () =>
        new Promise<RemoteAccessStatusWire>((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    const api = fakeApi({
      status: vi.fn(async () => payload({ enabled: true, state: "connected" })),
      update,
    });
    const { state } = mountRemote(api);
    await flushPromises();
    expect(api.status).toHaveBeenCalledTimes(1);

    const disabling = state.disable();
    await flushPromises();
    expect(update).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(6_000);
    expect(api.status).toHaveBeenCalledTimes(1);

    resolveUpdate(payload({ enabled: false, state: "disabled" }));
    expect(await disabling).toBe(true);
    expect(state.status.value?.state).toBe("disabled");
  });

  it("aborts the in-flight refresh when the page is hidden", async () => {
    const pending: Array<(value: RemoteAccessStatusWire) => void> = [];
    const api = fakeApi({
      status: vi.fn(
        () =>
          new Promise<RemoteAccessStatusWire>((resolve) => {
            pending.push(resolve);
          }),
      ),
    });
    const { state } = mountRemote(api);
    await flushPromises();
    expect(state.loading.value).toBe(true);

    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(state.loading.value).toBe(false);

    pending[0](payload({ enabled: true, state: "connected" }));
    await flushPromises();
    expect(state.status.value).toBeNull();
  });

  it("imports a frpc TOML path and rejects an empty selection", async () => {
    const api = fakeApi();
    const { state } = mountRemote(api);
    await flushPromises();

    expect(await state.importConfig("   ")).toBe(false);
    expect(api.importConfig).not.toHaveBeenCalled();
    expect(state.lastError.value).toBe("请选择要导入的 FRPC TOML 配置文件。");

    expect(await state.importConfig("D:\\frp\\frpc.toml")).toBe(true);
    expect(api.importConfig).toHaveBeenCalledWith(
      { source_path: "D:\\frp\\frpc.toml" },
      expect.any(AbortSignal),
    );
  });

  it("reports an import failure through the toast sink", async () => {
    const api = fakeApi({
      importConfig: vi.fn(async () => {
        throw new Error("FRPC 配置不是有效的 TOML。");
      }),
    });
    const { state } = mountRemote(api);
    await flushPromises();

    expect(await state.importConfig("D:\\frpc.toml")).toBe(false);
    expect(state.lastError.value).toBe("FRPC 配置不是有效的 TOML。");
    expect(toasts.at(-1)).toEqual([
      "无法导入 FRPC 配置",
      "FRPC 配置不是有效的 TOML。",
      "error",
    ]);
  });

  it("surfaces an enable failure and keeps the remote disabled", async () => {
    const api = fakeApi({
      update: vi.fn(async () => {
        throw new Error("请先开启 Android 局域网访问并保存设置。");
      }),
    });
    const { state } = mountRemote(api);
    await flushPromises();

    expect(await state.enable()).toBe(false);
    expect(state.lastError.value).toBe("请先开启 Android 局域网访问并保存设置。");
    expect(toasts.at(-1)).toEqual([
      "无法开启远程访问",
      "请先开启 Android 局域网访问并保存设置。",
      "error",
    ]);
  });

  it("retries an errored tunnel and reflects the connecting state", async () => {
    const api = fakeApi({
      status: vi.fn(async () =>
        payload({ enabled: true, state: "error", message: "远程访问连接中断。" }),
      ),
    });
    const { state } = mountRemote(api);
    await flushPromises();
    expect(state.status.value?.state).toBe("error");

    expect(await state.retry()).toBe(true);
    expect(state.status.value?.state).toBe("connecting");
  });
});
