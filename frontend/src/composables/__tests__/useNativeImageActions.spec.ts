import { afterEach, describe, expect, it, vi } from "vitest";

import type { PywebviewApi } from "../../types/contracts";
import { useNativeImageActions } from "../useNativeImageActions";

function bridge(overrides: Partial<PywebviewApi> = {}): PywebviewApi {
  return {
    select_directory: vi.fn(async () => ({ ok: true, path: null })),
    select_json_file: vi.fn(async () => ({ ok: true, path: null })),
    select_query_image: vi.fn(async () => ({
      ok: true,
      image: { id: "query-1", name: "query.jpg" },
    })),
    open_image: vi.fn(async () => ({ ok: true })),
    reveal_image: vi.fn(async () => ({ ok: true })),
    copy_image: vi.fn(async () => ({ ok: true, count: 1 })),
    copy_files: vi.fn(async () => ({ ok: true, count: 1 })),
    copy_image_paths: vi.fn(async () => ({ ok: true, count: 1 })),
    export_images: vi.fn(async () => ({
      ok: true,
      cancelled: false,
      job: {
        id: "export-1",
        status: "succeeded",
        total: 1,
        processed: 1,
        exported: 1,
        skipped: 0,
        message: "已导出 1 张图片",
      },
    })),
    export_status: vi.fn(async () => ({ ok: false, error: "unexpected poll" })),
    ...overrides,
  };
}

describe("useNativeImageActions", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("turns bridge ok:false into a visible error", async () => {
    const onError = vi.fn();
    const api = bridge({
      open_image: vi.fn(async () => ({ ok: false, error: "无法打开" })),
    });
    const actions = useNativeImageActions({ onError }, () => api);

    expect(await actions.open("image-1")).toBe(false);
    expect(onError).toHaveBeenCalledWith("无法系统打开图片", "无法打开");
  });

  it("suppresses a duplicate native launch while the first call is pending", async () => {
    let resolveOpen: ((result: { ok: true }) => void) | undefined;
    const open = vi.fn(
      async () =>
        await new Promise<{ ok: true }>((resolve) => {
          resolveOpen = resolve;
        }),
    );
    const onInfo = vi.fn();
    const actions = useNativeImageActions({ onInfo }, () => bridge({ open_image: open }));

    const first = actions.open("image-1");
    expect(await actions.open("image-1")).toBe(false);
    expect(open).toHaveBeenCalledTimes(1);
    expect(onInfo).toHaveBeenCalled();
    resolveOpen?.({ ok: true });
    expect(await first).toBe(true);
  });

  it("returns opaque query image information", async () => {
    const actions = useNativeImageActions({}, () => bridge());
    await expect(actions.selectQueryImage()).resolves.toEqual({
      id: "query-1",
      name: "query.jpg",
    });
  });

  it("uploads a dropped image through the token-scoped binary endpoint", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          ok: true,
          id: "query-upload-1",
          name: "雷电将军.png",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const actions = useNativeImageActions({}, () => bridge());
    const file = new File([new Uint8Array([1, 2, 3])], "雷电将军.png", {
      type: "image/png",
    });

    await expect(actions.registerQueryImage(file)).resolves.toEqual({
      id: "query-upload-1",
      name: "雷电将军.png",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "api/query-image?name=%E9%9B%B7%E7%94%B5%E5%B0%86%E5%86%9B.png",
      expect.objectContaining({ method: "POST", body: file }),
    );
  });

  it("deduplicates a batch before copying files", async () => {
    const copyFiles = vi.fn(async () => ({ ok: true, count: 2 }));
    const onInfo = vi.fn();
    const actions = useNativeImageActions({ onInfo }, () =>
      bridge({ copy_files: copyFiles }),
    );

    expect(await actions.copyFiles(["image-1", "image-1", "image-2"])).toBe(true);
    expect(copyFiles).toHaveBeenCalledWith(["image-1", "image-2"]);
    expect(onInfo).toHaveBeenCalledWith("复制文件完成", "已处理 2 张图片。");
  });

  it("reports a background export that completes immediately", async () => {
    const onInfo = vi.fn();
    const actions = useNativeImageActions({ onInfo }, () => bridge());

    expect(await actions.exportImages(["image-1"])).toBe(true);
    expect(actions.exportJob.value?.status).toBe("succeeded");
    expect(onInfo).toHaveBeenCalledWith("导出完成", "已导出 1 张图片");
  });
});
