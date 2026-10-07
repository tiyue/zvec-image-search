import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import AsyncPageState from "../AsyncPageState.vue";

describe("AsyncPageState", () => {
  it("shows a lightweight loading state for the requested page", () => {
    const wrapper = mount(AsyncPageState, { props: { label: "设置" } });

    expect(wrapper.get(".async-page-state").attributes("aria-busy")).toBe("true");
    expect(wrapper.text()).toContain("正在加载设置…");
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(false);
  });

  it("renders an actionable notice and asks to retry when clicked", async () => {
    const wrapper = mount(AsyncPageState, {
      props: { label: "图片推荐", error: new Error("网络请求失败") },
    });

    expect(wrapper.text()).toContain("无法加载图片推荐");
    expect(wrapper.text()).toContain("请重试加载此页面。");
    await wrapper.get("[data-testid='async-page-retry']").trigger("click");
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });

  it("keeps the technical error out of the product copy", () => {
    const wrapper = mount(AsyncPageState, {
      props: {
        label: "设置",
        error: new Error(
          "Unable to preload CSS for http://127.0.0.1:49374/assets/SettingsPage-BosJCI-G.css",
        ),
      },
    });

    expect(wrapper.text()).toContain("无法加载设置");
    expect(wrapper.text()).toContain("请重试加载此页面。");
    expect(wrapper.text()).not.toContain("Unable to preload CSS");
    expect(wrapper.text()).not.toContain("127.0.0.1");
    expect(wrapper.text()).not.toContain("SettingsPage-BosJCI-G.css");
    expect(wrapper.find("[data-testid='async-page-retry']").exists()).toBe(true);
  });

  it("treats a plain string error as a failure and ignores empty values", () => {
    const failed = mount(AsyncPageState, { props: { error: "加载被中断" } });
    expect(failed.text()).toContain("请重试加载此页面。");
    expect(failed.text()).not.toContain("加载被中断");
    expect(failed.find("[data-testid='async-page-retry']").exists()).toBe(true);

    const loading = mount(AsyncPageState, { props: { error: "" } });
    expect(loading.find("[data-testid='async-page-retry']").exists()).toBe(false);
    expect(loading.text()).toContain("正在加载页面…");
  });
});
