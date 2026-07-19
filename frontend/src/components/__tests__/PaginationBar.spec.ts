import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import PaginationBar from "../PaginationBar.vue";

describe("PaginationBar", () => {
  it("disables previous at the first page and emits next", async () => {
    const wrapper = mount(PaginationBar, {
      props: {
        page: 1,
        totalPages: 3,
        totalItems: 38,
        loading: false,
        hasPrevious: false,
        hasNext: true,
      },
    });
    const previous = wrapper.get("button[aria-label='上一页']");
    const next = wrapper.get("button[aria-label='下一页']");

    expect((previous.element as HTMLButtonElement).disabled).toBe(true);
    expect((next.element as HTMLButtonElement).disabled).toBe(false);
    expect((wrapper.get("input[aria-label='跳转页码']").element as HTMLInputElement).value).toBe(
      "1",
    );
    expect(wrapper.text()).toContain("/ 3 页");
    expect(wrapper.text()).toContain("当前第 1 页");
    expect(wrapper.text()).toContain("共 38 张");

    await next.trigger("click");
    expect(wrapper.emitted("next")).toHaveLength(1);
  });

  it("disables next at the last page and emits previous", async () => {
    const wrapper = mount(PaginationBar, {
      props: {
        page: 3,
        totalPages: 3,
        totalItems: 38,
        loading: false,
        hasPrevious: true,
        hasNext: false,
      },
    });
    const previous = wrapper.get("button[aria-label='上一页']");
    const next = wrapper.get("button[aria-label='下一页']");

    expect((previous.element as HTMLButtonElement).disabled).toBe(false);
    expect((next.element as HTMLButtonElement).disabled).toBe(true);
    await previous.trigger("click");
    expect(wrapper.emitted("previous")).toHaveLength(1);
  });

  it("disables both boundaries while a page is loading", () => {
    const wrapper = mount(PaginationBar, {
      props: {
        page: 2,
        totalPages: 4,
        totalItems: 60,
        loading: true,
        hasPrevious: true,
        hasNext: true,
      },
    });

    expect(wrapper.findAll("button").every((button) => button.element.disabled)).toBe(true);
  });

  it("shows a no-results state and never permits invalid navigation", () => {
    const wrapper = mount(PaginationBar, {
      props: {
        page: 1,
        totalPages: 0,
        totalItems: 0,
        loading: false,
        hasPrevious: true,
        hasNext: true,
      },
    });

    expect(wrapper.text()).toContain("暂无结果");
    expect(wrapper.findAll("button").every((button) => button.element.disabled)).toBe(true);
  });

  it("accepts an in-range page and rejects invalid input", async () => {
    const wrapper = mount(PaginationBar, {
      props: {
        page: 2,
        totalPages: 8,
        totalItems: 120,
        loading: false,
        hasPrevious: true,
        hasNext: true,
      },
    });
    const input = wrapper.get("input[aria-label='跳转页码']");

    await input.setValue("6");
    await input.trigger("keydown", { key: "Enter" });
    expect(wrapper.emitted("jump")).toEqual([[6]]);

    await input.setValue("9");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.get("[role='alert']").text()).toContain("1～8");
    expect(wrapper.emitted("jump")).toEqual([[6]]);
  });
});
