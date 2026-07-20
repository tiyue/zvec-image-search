import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LanAccessSection from "../LanAccessSection.vue";
import type { LanAccessApi, LanAccessStatusWire } from "../lanTypes";

function payload(overrides: Partial<LanAccessStatusWire> = {}): LanAccessStatusWire {
  return {
    enabled: true,
    running: true,
    bind_host: "192.168.1.20",
    port: 38522,
    display_name: "Zvec on DESKTOP",
    discovery_port: 38521,
    address: "http://192.168.1.20:38522",
    available_hosts: [{ address: "192.168.1.20", label: "以太网" }],
    pending_pairings: [{
      id: "pair-1",
      device_name: "Galaxy Tab",
      code: "483921",
      expires_at: "2026-07-20T12:00:00Z",
      status: "pending",
    }],
    device: null,
    message: "服务运行正常。",
    ...overrides,
  };
}

function fakeApi(): LanAccessApi {
  return {
    status: vi.fn(async () => payload()),
    update: vi.fn(async (body) => payload({
      enabled: body.enabled,
      bind_host: body.bind_host,
      port: body.port,
      display_name: body.display_name,
    })),
    start: vi.fn(async () => payload()),
    stop: vi.fn(async () => payload({ running: false })),
    approve: vi.fn(async () => payload({
      pending_pairings: [],
      device: {
        device_id: "android-1",
        device_name: "Galaxy Tab",
        paired_at: "2026-07-20T11:00:00Z",
        last_seen_at: "2026-07-20T11:30:00Z",
      },
    })),
    reject: vi.fn(async () => payload({ pending_pairings: [] })),
    revokeDevice: vi.fn(async () => payload({ device: null })),
  };
}

describe("LanAccessSection", () => {
  beforeEach(() => vi.clearAllMocks());

  it("loads a running service and exposes only safe pairing information", async () => {
    const wrapper = mount(LanAccessSection, { props: { api: fakeApi() } });
    await flushPromises();

    expect(wrapper.text()).toContain("192.168.1.20:38522");
    expect(wrapper.text()).toContain("Galaxy Tab");
    expect(wrapper.text()).toContain("483921");
    expect(wrapper.text()).not.toContain("Bearer");
    expect(wrapper.text()).not.toContain("token");
  });

  it("saves validated settings before starting the service", async () => {
    const api = fakeApi();
    const wrapper = mount(LanAccessSection, {
      props: { api },
    });
    await flushPromises();

    await wrapper.get('input[name="lan_display_name"]').setValue("我的 Zvec");
    await wrapper.get('input[name="lan_port"]').setValue("39000");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(api.update).toHaveBeenCalledWith({
      enabled: true,
      bind_host: "192.168.1.20",
      port: 39000,
      display_name: "我的 Zvec",
    });
  });

  it("approves a pending Android device without receiving its bearer token", async () => {
    const api = fakeApi();
    const wrapper = mount(LanAccessSection, { props: { api } });
    await flushPromises();

    const allow = wrapper.findAll("button").find((button) => button.text() === "允许");
    expect(allow).toBeTruthy();
    await allow!.trigger("click");
    await flushPromises();

    expect(api.approve).toHaveBeenCalledWith("pair-1", expect.any(AbortSignal));
    expect(wrapper.text()).toContain("已配对设备");
  });
});
