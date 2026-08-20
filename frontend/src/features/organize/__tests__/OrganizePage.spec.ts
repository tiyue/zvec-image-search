import { flushPromises, mount, type DOMWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import OrganizePage from "../OrganizePage.vue";
import type {
  OrganizeApi,
  OrganizeBootstrapResponse,
  OrganizeFolderImagesResponse,
} from "../types";

function bootstrapPayload(): OrganizeBootstrapResponse {
  return {
    libraries: [{ id: "lib-1", name: "人物图库", enabled: true, is_default: true }],
    organize: {
      undo_available: true,
      aliases: [{ canonical_name: "雷电将军", aliases: ["雷神", "影"] }],
    },
  };
}

function folderImages(page: number): OrganizeFolderImagesResponse {
  if (page === 2) {
    return {
      page: 2,
      page_size: 60,
      total_items: 65,
      items: [
        {
          doc_id: "doc-4",
          library_id: "lib-1",
          file_name: "04.jpg",
          relative_path: "原神/雷电将军/04.jpg",
          image_available: true,
          thumbnail_url: "api/image/doc-4?variant=thumbnail",
        },
      ],
    };
  }
  return {
    page: 1,
    page_size: 60,
    total_items: 65,
    items: [
      {
        doc_id: "doc-1",
        image_id: "native-doc-1",
        library_id: "lib-1",
        file_name: "01.jpg",
        relative_path: "原神/雷电将军/01.jpg",
        width: 1200,
        height: 1800,
        manual_tags: ["写真"],
        folder_tags: ["原神"],
        inherited_tags: ["雷电将军"],
        effective_tags: ["写真", "原神", "雷电将军"],
        image_available: true,
        thumbnail_url: "api/image/doc-1?variant=thumbnail",
      },
      {
        doc_id: "doc-2",
        library_id: "lib-1",
        file_name: "02.jpg",
        relative_path: "原神/雷电将军/02.jpg",
        manual_tags: ["写真"],
        model_tags: ["微笑"],
        effective_tags: ["写真", "微笑"],
        image_available: false,
      },
      {
        doc_id: "doc-3",
        library_id: "lib-1",
        file_name: "03.jpg",
        relative_path: "原神/雷电将军/03.jpg",
        manual_tags: ["户外"],
        effective_tags: ["户外"],
        image_available: true,
        image_url: "api/image/doc-3?variant=preview",
      },
    ],
  };
}

function fakeApi(overrides: Partial<OrganizeApi> = {}): OrganizeApi {
  return {
    bootstrap: vi.fn(async () => bootstrapPayload()),
    listFolders: vi.fn(async (_libraryId, query) => ({
      folders: [
        {
          folder_key: "folder-raiden",
          root_id: "root-people",
          root_name: "人物图库根目录",
          relative_folder: "原神/雷电将军",
          name: "雷电将军",
          image_count: 65,
          manual_tagged_count: 40,
          model_tagged_count: 30,
        },
        {
          folder_key: "folder-photo",
          root_id: "root-photo",
          root_name: "写真根目录",
          relative_folder: "写真/棚拍",
          name: "棚拍",
          image_count: 12,
        },
      ].filter((folder) => !query || `${folder.name} ${folder.relative_folder}`.includes(query)),
      total: query ? 1 : 2,
    })),
    listFolderImages: vi.fn(async (_libraryId, _folderKey, page) => folderImages(page)),
    submitJob: vi.fn(async () => ({
      job: {
        id: "job-1",
        status: "succeeded",
        result: { updated: 1, failed: 0, undo_available: true },
      },
    })),
    job: vi.fn(async (jobId) => ({
      job: { id: jobId, status: "succeeded", result: { updated: 1, failed: 0 } },
    })),
    ...overrides,
  };
}

type QueryWrapper = Pick<DOMWrapper<Element>, "findAll" | "get">;

function buttonWithText(wrapper: QueryWrapper, label: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  return button;
}

function imageButton(wrapper: QueryWrapper, id: string) {
  return wrapper.get(`[data-image-id="${id}"] .image-select`);
}

async function mountPage(api: OrganizeApi) {
  const wrapper = mount(OrganizePage, { props: { api } });
  await flushPromises();
  return wrapper;
}

async function selectOneAndEnterTags(
  wrapper: QueryWrapper,
  tags = "原神、雷电将军",
): Promise<void> {
  await imageButton(wrapper, "doc-1").trigger("click");
  const input = wrapper.get(".tag-input-label textarea");
  await input.setValue(tags);
  await input.trigger("keydown", { key: "Enter" });
}

describe("OrganizePage batch tags", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("loads roots and folders, searches and pages without cropping available thumbnails", async () => {
    const api = fakeApi();
    const wrapper = await mountPage(api);

    expect(wrapper.text()).toContain("人物图库根目录");
    expect(wrapper.text()).toContain("写真根目录");
    expect(api.listFolders).toHaveBeenCalledWith(
      "lib-1",
      "",
      0,
      200,
      expect.any(AbortSignal),
    );
    expect(api.listFolderImages).toHaveBeenCalledWith(
      "lib-1",
      "folder-raiden",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );

    const firstImage = wrapper.get('[data-image-id="doc-1"]');
    expect(firstImage.get("img").classes()).toContain("media-contain");
    expect(firstImage.text()).toContain("雷电将军");
    expect(firstImage.findAll(".mini-tag").find((tag) => tag.text() === "雷电将军")?.attributes("title"))
      .toContain("同文件夹继承");

    const unavailable = wrapper.get('[data-image-id="doc-2"]');
    expect(unavailable.find("img").exists()).toBe(false);
    expect(unavailable.get(".thumbnail-placeholder").text()).toContain("图片不可用");

    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(api.listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "folder-raiden",
      2,
      6,
      false,
      expect.any(AbortSignal),
    );
    expect(wrapper.find('[data-image-id="doc-4"]').exists()).toBe(true);

    await wrapper.get('input[aria-label="搜索文件夹"]').setValue("写真");
    await wrapper.get(".folder-search").trigger("submit");
    await flushPromises();
    expect(api.listFolders).toHaveBeenLastCalledWith(
      "lib-1",
      "写真",
      0,
      200,
      expect.any(AbortSignal),
    );
    expect(wrapper.find('[data-folder-key="folder-raiden"]').exists()).toBe(false);
    expect(wrapper.find('[data-folder-key="folder-photo"]').exists()).toBe(true);
  });

  it("keeps batch tags and exposes the two new intelligence tabs", async () => {
    const api = fakeApi();
    const wrapper = await mountPage(api);

    expect(wrapper.get(".workspace-tabs").text()).toContain("批量标签");
    expect(wrapper.get(".workspace-tabs").text()).toContain("相似分组");
    expect(wrapper.get(".workspace-tabs").text()).toContain("待学习样本");

    await buttonWithText(wrapper, "相似分组").trigger("click");
    await flushPromises();
    expect(wrapper.get("#similarity-groups-title").text()).toBe("相似分组");
    expect(api.submitJob).toHaveBeenCalledWith(
      expect.objectContaining({ task_type: "cluster_list", library_id: "lib-1" }),
      expect.any(AbortSignal),
    );

    await buttonWithText(wrapper, "待学习样本").trigger("click");
    await flushPromises();
    expect(wrapper.get("#active-learning-title").text()).toBe("待学习样本");
  });

  it("keeps direct root images selectable when root images and child folders coexist", async () => {
    const listFolderImages = vi.fn(
      async (_libraryId: string, folderKey: string, page: number) => {
        if (folderKey === "root-folder-key") {
          return {
            page,
            page_size: 15,
            total_items: 16,
            items:
              page === 2
                ? [
                    {
                      doc_id: "root-doc-16",
                      file_name: "root-16.jpg",
                      relative_path: "root-16.jpg",
                      image_available: true,
                    },
                  ]
                : [
                    {
                      doc_id: "root-doc-1",
                      file_name: "root-01.jpg",
                      relative_path: "root-01.jpg",
                      image_available: true,
                    },
                  ],
          };
        }
        return {
          page: 1,
          page_size: 15,
          total_items: 2,
          items: [
            {
              doc_id: "child-doc-1",
              file_name: "child-01.jpg",
              relative_path: "角色/child-01.jpg",
              image_available: true,
            },
          ],
        };
      },
    );
    const api = fakeApi({
      listFolders: vi.fn(async () => ({
        roots: [
          {
            root_id: "root-mixed",
            root_name: "混合图库",
            folder_key: "root-folder-key",
            folder_count: 1,
            image_count: 18,
          },
        ],
        folders: [
          {
            folder_key: "child-folder-key",
            root_id: "root-mixed",
            root_name: "混合图库",
            relative_folder: "角色",
            name: "角色",
            image_count: 2,
            direct_image_count: 2,
          },
        ],
        total: 1,
      })),
      listFolderImages,
    });
    const wrapper = await mountPage(api);

    // Backend root folder keys encode relative_folder="". The first request
    // must therefore target the root directly and remain non-recursive.
    expect(listFolderImages).toHaveBeenCalledWith(
      "lib-1",
      "root-folder-key",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );
    expect(wrapper.find('[data-image-id="root-doc-1"]').exists()).toBe(true);
    expect(wrapper.get("#folder-gallery-title").text()).toBe("混合图库");
    expect(wrapper.get(".gallery-heading small").text()).toBe("根目录");
    expect(wrapper.get(".root-button").attributes("aria-pressed")).toBe("true");

    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "root-folder-key",
      2,
      6,
      false,
      expect.any(AbortSignal),
    );
    expect(wrapper.find('[data-image-id="root-doc-16"]').exists()).toBe(true);

    await wrapper.get('[data-folder-key="child-folder-key"] .folder-select-button').trigger("click");
    await flushPromises();
    expect(listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "child-folder-key",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );

    await wrapper.get(".root-button").trigger("click");
    await flushPromises();
    expect(listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "root-folder-key",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );

    await buttonWithText(wrapper, "选择整文件夹").trigger("click");
    await wrapper.get(".tag-input-label textarea").setValue("根目录标签");
    await wrapper.get(".tag-input-label textarea").trigger("blur");
    await buttonWithText(wrapper, "添加标签").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith({
      task_type: "manual_tag_batch",
      library_id: "lib-1",
      selection: {
        mode: "folder",
        folder_key: "root-folder-key",
        include_subfolders: false,
        excluded_doc_ids: [],
      },
      operation: "add",
      tags: ["根目录标签"],
    });
  });

  it("shows valid first and latest indexed times and hides missing or invalid values", async () => {
    const firstIndexedAt = "2025-01-02T03:04:05.000Z";
    const lastIndexedAt = "2026-07-19T08:09:10.000Z";
    const api = fakeApi({
      listFolders: vi.fn(async () => ({
        folders: [
          {
            folder_key: "folder-timed",
            root_id: "root-time",
            root_name: "时间图库",
            relative_folder: "有时间",
            name: "有时间",
            image_count: 1,
            first_indexed_at: firstIndexedAt,
            last_indexed_at: lastIndexedAt,
          },
          {
            folder_key: "folder-missing-time",
            root_id: "root-time",
            root_name: "时间图库",
            relative_folder: "无时间",
            name: "无时间",
            image_count: 1,
          },
          {
            folder_key: "folder-invalid-time",
            root_id: "root-time",
            root_name: "时间图库",
            relative_folder: "错误时间",
            name: "错误时间",
            image_count: 1,
            first_indexed_at: "not-a-date",
            last_indexed_at: "still-not-a-date",
          },
        ],
        total: 3,
      })),
    });
    const wrapper = await mountPage(api);
    const formatter = new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    const timed = wrapper.get('[data-folder-key="folder-timed"]');

    expect(timed.findAll(".folder-time").map((item) => item.text())).toEqual([
      `首次 ${formatter.format(new Date(firstIndexedAt))}`,
      `最近 ${formatter.format(new Date(lastIndexedAt))}`,
    ]);
    expect(timed.findAll(".folder-time").map((item) => item.attributes("title"))).toEqual([
      firstIndexedAt,
      lastIndexedAt,
    ]);
    expect(
      wrapper.get('[data-folder-key="folder-missing-time"]').findAll(".folder-time"),
    ).toHaveLength(0);
    expect(
      wrapper.get('[data-folder-key="folder-invalid-time"]').findAll(".folder-time"),
    ).toHaveLength(0);
  });

  it("loads folder catalogs beyond the first 200 rows without replacing the first page", async () => {
    const listFolders = vi.fn(async (_libraryId: string, _query: string, offset: number) => {
      const count = offset === 0 ? 200 : 5;
      return {
        folders: Array.from({ length: count }, (_, index) => {
          const absoluteIndex = offset + index;
          return {
            folder_key: `folder-${absoluteIndex}`,
            root_id: "root-people",
            root_name: "人物图库根目录",
            relative_folder: `作品/角色-${absoluteIndex}`,
            name: `角色-${absoluteIndex}`,
            image_count: 1,
            direct_image_count: 1,
            first_indexed_at: new Date(
              Date.UTC(2026, 6, 19, 0, 0, 0) - absoluteIndex * 1_000,
            ).toISOString(),
          };
        }),
        total: 205,
        has_more: offset + count < 205,
      };
    });
    const wrapper = await mountPage(fakeApi({ listFolders }));

    expect(wrapper.findAll(".folder-item")).toHaveLength(200);
    await buttonWithText(wrapper, "加载更多文件夹").trigger("click");
    await flushPromises();

    expect(listFolders).toHaveBeenLastCalledWith(
      "lib-1",
      "",
      200,
      200,
      expect.any(AbortSignal),
    );
    expect(wrapper.findAll(".folder-item")).toHaveLength(205);
    expect(wrapper.find('[data-folder-key="folder-204"]').exists()).toBe(true);
  });

  it("uses the measured gallery capacity for the first image request", async () => {
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1_547);
    const height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(1_030);
    const api = fakeApi();

    const wrapper = await mountPage(api);

    expect(api.listFolderImages).toHaveBeenCalledWith(
      "lib-1",
      "folder-raiden",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );
    wrapper.unmount();
    width.mockRestore();
    height.mockRestore();
  });

  it("does not refetch after resize since the page size is fixed", async () => {
    vi.useFakeTimers();
    let viewportWidth = 1_547;
    let viewportHeight = 1_030;
    let resizeCallback: ResizeObserverCallback | undefined;
    const width = vi
      .spyOn(HTMLElement.prototype, "clientWidth", "get")
      .mockImplementation(() => viewportWidth);
    const height = vi
      .spyOn(HTMLElement.prototype, "clientHeight", "get")
      .mockImplementation(() => viewportHeight);

    class ResizeObserverMock {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }

      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }

    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    const api = fakeApi();
    const wrapper = await mountPage(api);

    expect(api.listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "folder-raiden",
      1,
      6,
      false,
      expect.any(AbortSignal),
    );

    const callCountBeforeResize = vi.mocked(api.listFolderImages).mock.calls.length;

    viewportWidth = 300;
    viewportHeight = 520;
    resizeCallback?.([], {} as ResizeObserver);
    await vi.advanceTimersByTimeAsync(160);
    await flushPromises();

    // Fixed page size means resize does not trigger a refetch.
    expect(vi.mocked(api.listFolderImages).mock.calls.length).toBe(callCountBeforeResize);

    wrapper.unmount();
    width.mockRestore();
    height.mockRestore();
  });

  it("supports single, Ctrl, Shift, page, folder, invert and clear selection", async () => {
    const wrapper = await mountPage(fakeApi());

    await imageButton(wrapper, "doc-1").trigger("click");
    expect(imageButton(wrapper, "doc-1").attributes("aria-pressed")).toBe("true");
    await imageButton(wrapper, "doc-3").trigger("click", { ctrlKey: true });
    expect(wrapper.findAll(".image-tile.selected")).toHaveLength(2);

    await buttonWithText(wrapper, "清空").trigger("click");
    await imageButton(wrapper, "doc-1").trigger("click");
    await imageButton(wrapper, "doc-3").trigger("click", { shiftKey: true });
    expect(wrapper.findAll(".image-tile.selected")).toHaveLength(3);

    await buttonWithText(wrapper, "清空").trigger("click");
    await buttonWithText(wrapper, "全选当前页").trigger("click");
    expect(wrapper.findAll(".image-tile.selected")).toHaveLength(3);
    await buttonWithText(wrapper, "反选当前页").trigger("click");
    expect(wrapper.findAll(".image-tile.selected")).toHaveLength(0);

    await buttonWithText(wrapper, "选择整文件夹").trigger("click");
    expect(wrapper.get(".selection-summary").text()).toContain("已选整文件夹全部图片");
    expect(wrapper.get(".selection-count").text()).toBe("65");
    await imageButton(wrapper, "doc-2").trigger("click", { ctrlKey: true });
    expect(wrapper.get(".selection-summary").text()).toContain("排除 1 张");
    expect(wrapper.get(".selection-count").text()).toBe("64");
    await buttonWithText(wrapper, "清空").trigger("click");
    expect(wrapper.get(".selection-count").text()).toBe("0");
  });

  it.each([
    ["添加标签", "add"],
    ["移除标签", "remove"],
    ["替换人工标签", "replace_manual"],
  ] as const)("parses multiple separators and submits %s for selected images", async (label, operation) => {
    const api = fakeApi();
    const wrapper = await mountPage(api);
    const initialImageRequests = vi.mocked(api.listFolderImages).mock.calls.length;

    await selectOneAndEnterTags(wrapper, "原神, 雷电将军、站姿；微笑");
    expect(wrapper.findAll(".draft-tag").map((tag) => tag.text().replace("×", "").trim()))
      .toEqual(["原神", "雷电将军", "站姿", "微笑"]);
    await buttonWithText(wrapper, label).trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith({
      task_type: "manual_tag_batch",
      library_id: "lib-1",
      selection: { mode: "selected", doc_ids: ["doc-1"] },
      operation,
      tags: ["原神", "雷电将军", "站姿", "微笑"],
    });
    // One refresh owns one image load. The former Promise.all implementation
    // issued two competing image requests here.
    expect(vi.mocked(api.listFolderImages).mock.calls.length).toBe(initialImageRequests + 1);
  });

  it("submits an opaque whole-folder selection with only explicit exclusions", async () => {
    const api = fakeApi();
    const wrapper = await mountPage(api);

    await buttonWithText(wrapper, "选择整文件夹").trigger("click");
    await imageButton(wrapper, "doc-2").trigger("click", { ctrlKey: true });
    await wrapper.get(".tag-input-label textarea").setValue("原神");
    await wrapper.get(".tag-input-label textarea").trigger("blur");
    await buttonWithText(wrapper, "添加标签").trigger("click");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith({
      task_type: "manual_tag_batch",
      library_id: "lib-1",
      selection: {
        mode: "folder",
        folder_key: "folder-raiden",
        include_subfolders: false,
        excluded_doc_ids: ["doc-2"],
      },
      operation: "add",
      tags: ["原神"],
    });
  });

  it("keeps one active job authoritative while browsing remains available", async () => {
    vi.useFakeTimers();
    const detail = vi
      .fn()
      .mockResolvedValueOnce({
        job: { id: "job-running", status: "running", progress_percent: 45 },
      })
      .mockResolvedValueOnce({
        job: {
          id: "job-running",
          status: "succeeded",
          result: { updated_count: 1, failed_count: 0 },
        },
      });
    const api = fakeApi({
      submitJob: vi.fn(async () => ({ job: { id: "job-running", status: "queued" } })),
      job: detail,
    });
    const wrapper = await mountPage(api);
    await selectOneAndEnterTags(wrapper);
    await wrapper.get('.alias-form input[placeholder="例如：雷电将军"]').setValue("刻晴");

    await buttonWithText(wrapper, "添加标签").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "移除标签").attributes("disabled")).toBeDefined();
    expect(buttonWithText(wrapper, "新增或更新").attributes("disabled")).toBeDefined();

    // Gallery navigation is deliberately independent from task submission.
    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-image-id="doc-4"]').exists()).toBe(true);
    expect(api.submitJob).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    expect(detail).toHaveBeenCalledTimes(1);
    expect(buttonWithText(wrapper, "新增或更新").attributes("disabled")).toBeDefined();

    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    expect(detail).toHaveBeenCalledTimes(2);
    expect(buttonWithText(wrapper, "新增或更新").attributes("disabled")).toBeUndefined();
  });

  it("recovers submission controls after an error and allows retry", async () => {
    const submitJob = vi
      .fn()
      .mockRejectedValueOnce(new Error("后端暂不可用"))
      .mockResolvedValueOnce({
        job: { id: "job-retry", status: "succeeded", result: { updated: 1 } },
      });
    const api = fakeApi({ submitJob });
    const wrapper = await mountPage(api);
    await selectOneAndEnterTags(wrapper);

    await buttonWithText(wrapper, "添加标签").trigger("click");
    await flushPromises();
    expect(buttonWithText(wrapper, "添加标签").attributes("disabled")).toBeUndefined();

    await buttonWithText(wrapper, "添加标签").trigger("click");
    await flushPromises();
    expect(submitJob).toHaveBeenCalledTimes(2);
  });

  it("opens the original image on thumbnail double click", async () => {
    const wrapper = await mountPage(fakeApi());

    await wrapper.get('[data-image-id="doc-1"] .thumbnail-stage').trigger("dblclick");

    expect(wrapper.emitted("openImage")).toEqual([["native-doc-1"]]);
  });

  it("previews and confirms a two-stage folder deletion", async () => {
    const previewFolderDelete = vi.fn(async () => ({
      operation_id: "delete-op-1",
      confirmation_token: "delete-token-1",
      folder_key: "folder-raiden",
      folder_name: "雷电将军",
      relative_folder: "原神/雷电将军",
      root_name: "人物图库根目录",
      include_subfolders: true,
      image_count: 65,
      file_count: 68,
      size_bytes: 1_258_291,
      protected_count: 0,
      changed_count: 1,
      missing_count: 2,
      confirmation_phrase: "雷电将军",
      warnings: ["提交前会再次校验文件是否变化。"],
      blocked: false,
    }));
    const commitFolderDelete = vi.fn(async () => ({
      job: {
        id: "delete-job-1",
        status: "succeeded",
        processed: 65,
        total: 65,
        result: { updated: 65, failed: 0 },
      },
    }));
    const wrapper = await mountPage(fakeApi({ previewFolderDelete, commitFolderDelete }));

    await wrapper.get('[data-folder-key="folder-raiden"] .folder-delete-button').trigger("click");
    await flushPromises();

    expect(previewFolderDelete).toHaveBeenCalledWith(
      "lib-1",
      "folder-raiden",
      true,
      expect.any(AbortSignal),
    );
    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.text()).toContain("图库根目录本身和图库绑定会保留");
    expect(dialog.text()).toContain("65 张");
    expect(dialog.text()).toContain("1.2 MB");

    await dialog.get('input[type="text"]').setValue("错误确认词");
    await buttonWithText(wrapper, "清理文件夹和索引").trigger("click");
    await flushPromises();
    expect(commitFolderDelete).not.toHaveBeenCalled();

    await dialog.get('input[type="text"]').setValue("雷电将军");
    await buttonWithText(wrapper, "清理文件夹和索引").trigger("click");
    await flushPromises();

    expect(commitFolderDelete).toHaveBeenCalledWith(
      "lib-1",
      {
        operation_id: "delete-op-1",
        confirmation_token: "delete-token-1",
        confirm: true,
      },
    );
  });

  it("previews folder-name tags before submitting the model-free batch", async () => {
    const previewFolderNameTags = vi.fn(async () => ({
      selected: 65,
      processed: 65,
      changed: 60,
      unchanged: 5,
      untagged: 0,
      folders_scanned: 3,
      changed_folders: 2,
      samples: [
        {
          root_id: "root-people",
          relative_folder: "原神/Raiden雷电将军 写真",
          current_tags: ["旧文件夹"],
          proposed_tags: ["Raiden雷电将军", "雷电将军", "写真"],
          affected_images: 40,
        },
      ],
      samples_truncated: false,
      mode: "normal",
      force: true,
      rule_revision: "rule-v1",
      blacklist_count: 20,
    }));
    const submitJob = vi.fn(async () => ({
      job: {
        id: "folder-name-tag-job",
        status: "succeeded",
        processed: 65,
        total: 65,
        result: { updated: 60, failed: 0 },
      },
    }));
    const wrapper = await mountPage(
      fakeApi({ previewFolderNameTags, submitJob }),
    );

    await buttonWithText(wrapper, "从文件夹名生成").trigger("click");
    const dialog = wrapper.get(".folder-tag-modal");
    expect(dialog.text()).toContain("模型标签与系统元数据保持原值");

    await buttonWithText(wrapper, "生成预览").trigger("click");
    await flushPromises();

    expect(previewFolderNameTags).toHaveBeenCalledWith(
      "lib-1",
      { mode: "library" },
      { mode: "normal", force: true },
      expect.any(AbortSignal),
    );
    expect(dialog.text()).toContain("Raiden雷电将军、雷电将军、写真");
    expect(dialog.text()).toContain("60");

    await buttonWithText(wrapper, "确认应用").trigger("click");
    await flushPromises();
    expect(submitJob).toHaveBeenCalledWith({
      task_type: "folder_name_tag_apply",
      library_id: "lib-1",
      selection: { mode: "library" },
      mode: "normal",
      force: true,
      expected_rule_revision: "rule-v1",
    });
  });

  it("shows and updates the compact alias dictionary without identity confirmation controls", async () => {
    const api = fakeApi();
    const wrapper = await mountPage(api);

    expect(wrapper.get('[data-testid="alias-dictionary"]').text()).toContain("雷电将军");
    expect(wrapper.get('[data-testid="alias-dictionary"]').text()).toContain("雷神、影");
    expect(wrapper.find(".identity-options").exists()).toBe(false);

    await wrapper.get('.alias-entry[title="编辑 雷电将军"]').trigger("click");
    expect(
      (wrapper.get('.alias-form input[placeholder="例如：雷电将军"]').element as HTMLInputElement)
        .value,
    ).toBe("雷电将军");

    await wrapper.get('.alias-form input[placeholder="例如：雷电将军"]').setValue("荧");
    await wrapper.get('.alias-form input[placeholder="例如：雷神、影"]').setValue("旅行者, 主角");
    await wrapper.get('[data-testid="alias-form"]').trigger("submit");
    await flushPromises();

    expect(api.submitJob).toHaveBeenCalledWith({
      task_type: "tag_alias_upsert",
      library_id: "lib-1",
      canonical_name: "荧",
      aliases: ["旅行者", "主角"],
    });
    expect(wrapper.get('[data-testid="alias-dictionary"]').text()).toContain("荧");
    expect(wrapper.get('[data-testid="alias-dictionary"]').text()).toContain("旅行者、主角");
  });

  it("submits the current-library manual tag undo", async () => {
    const api = fakeApi();
    const wrapper = await mountPage(api);

    await buttonWithText(wrapper, "撤销最近一次批量操作").trigger("click");
    await flushPromises();
    expect(api.submitJob).toHaveBeenCalledWith({
      task_type: "manual_tag_undo",
      library_id: "lib-1",
    });
  });
});
