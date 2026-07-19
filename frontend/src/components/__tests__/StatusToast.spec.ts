import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import StatusToast from "../StatusToast.vue";

describe("StatusToast", () => {
  it("renders an assertive error and can be closed", async () => {
    const wrapper = mount(StatusToast, {
      props: {
        toast: {
          id: "toast-1",
          title: "搜索失败",
          message: "本地服务暂时不可用。",
          kind: "error",
        },
      },
    });

    expect(wrapper.classes()).toContain("is-error");
    expect(wrapper.attributes("role")).toBe("alert");
    expect(wrapper.attributes("aria-live")).toBe("assertive");
    expect(wrapper.text()).toContain("本地服务暂时不可用");

    await wrapper.get('button[aria-label="关闭提示"]').trigger("click");
    expect(wrapper.emitted("close")).toEqual([["toast-1"]]);
  });

  it("uses a polite status role for successful operations", () => {
    const wrapper = mount(StatusToast, {
      props: {
        toast: {
          id: "toast-2",
          title: "保存成功",
          message: "设置已经更新。",
          kind: "success",
        },
      },
    });

    expect(wrapper.classes()).toContain("is-success");
    expect(wrapper.attributes("role")).toBe("status");
    expect(wrapper.attributes("aria-live")).toBe("polite");
  });
});
