import { mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { describe, expect, it, vi } from "vitest";

import type {
  OrganizeApi,
  OrganizeBootstrapResponse,
  OrganizeFolderImagesResponse,
  OrganizeFolderListResponse,
} from "../types";
import { useOrganize } from "../useOrganize";

function mountComposable(api: OrganizeApi): {
  state: ReturnType<typeof useOrganize>;
  wrapper: VueWrapper;
} {
  let state!: ReturnType<typeof useOrganize>;
  const wrapper = mount(defineComponent({
    setup() {
      state = useOrganize(api, { autoLoad: false });
      return () => h("div");
    },
  }));
  return { state, wrapper };
}

function bootstrapPayload(): OrganizeBootstrapResponse {
  return {
    libraries: [{ id: "lib-1", name: "人物图库", enabled: true, is_default: true }],
  };
}

function folderList(): OrganizeFolderListResponse {
  return {
    folders: [
      {
        folder_key: "folder-1",
        root_id: "root-1",
        root_name: "人物图库根目录",
        relative_folder: "相册",
        name: "相册",
        image_count: 40,
        manual_tagged_count: 0,
        model_tagged_count: 0,
      },
    ],
    total: 1,
  };
}

function folderImages(page: number, size: number, total = 40): OrganizeFolderImagesResponse {
  const start = (page - 1) * size;
  const count = Math.max(0, Math.min(size, total - start));
  return {
    page,
    page_size: size,
    total_items: total,
    items: Array.from({ length: count }, (_, index) => ({
      doc_id: `doc-${start + index + 1}`,
      file_name: `${start + index + 1}.jpg`,
      relative_path: `相册/${start + index + 1}.jpg`,
      image_available: true,
    })),
  };
}

function fakeApi(overrides: Partial<OrganizeApi> = {}): OrganizeApi {
  return {
    bootstrap: vi.fn(async () => bootstrapPayload()),
    listFolders: vi.fn(async () => folderList()),
    listFolderImages: vi.fn(async (_libraryId, _folderKey, page, pageSize) =>
      folderImages(page, pageSize),
    ),
    submitJob: vi.fn(async () => ({ job: { id: "job-1", status: "succeeded" } })),
    job: vi.fn(async (jobId) => ({ job: { id: jobId, status: "succeeded" } })),
    ...overrides,
  };
}

describe("useOrganize folder image paging", () => {
  it("cancels an in-flight page read and ignores its late response", async () => {
    const api = fakeApi();
    let resolvePage: ((payload: OrganizeFolderImagesResponse) => void) | undefined;
    api.listFolderImages = vi.fn(
      async (_libraryId, _folderKey, page, pageSize) =>
        page === 2
          ? new Promise<OrganizeFolderImagesResponse>((resolve) => {
              resolvePage = resolve;
            })
          : folderImages(page, pageSize),
    );
    const { state: organize, wrapper } = mountComposable(api);
    await organize.load();
    expect(organize.images.value.map((image) => image.id)).toEqual([
      "doc-1",
      "doc-2",
      "doc-3",
      "doc-4",
      "doc-5",
      "doc-6",
    ]);

    const navigation = organize.loadFolderImages(2);
    expect(organize.imageLoading.value).toBe(true);
    organize.cancelPendingImageLoad();
    expect(organize.imageLoading.value).toBe(false);

    resolvePage?.(folderImages(2, 6));
    expect(await navigation).toBe(false);
    expect(organize.imagePage.value).toBe(1);
    expect(organize.images.value[0]?.id).toBe("doc-1");
    wrapper.unmount();
  });

  it("falls back to the rendered capacity when a capacity read is cancelled", async () => {
    const api = fakeApi();
    let resolveCapacity: ((payload: OrganizeFolderImagesResponse) => void) | undefined;
    api.listFolderImages = vi.fn(
      async (_libraryId, _folderKey, page, pageSize) =>
        pageSize === 10
          ? new Promise<OrganizeFolderImagesResponse>((resolve) => {
              resolveCapacity = resolve;
            })
          : folderImages(page, pageSize),
    );
    const { state: organize, wrapper } = mountComposable(api);
    await organize.load();
    expect(organize.images.value).toHaveLength(6);

    const reload = organize.setImagePageSize(10);
    expect(organize.imagePageSize.value).toBe(10);
    expect(organize.imageLoading.value).toBe(true);

    organize.cancelPendingImageLoad();
    expect(organize.imagePageSize.value).toBe(6);
    expect(organize.imageLoading.value).toBe(false);

    resolveCapacity?.(folderImages(1, 10));
    expect(await reload).toBe(false);
    expect(organize.imagePageSize.value).toBe(6);
    expect(organize.images.value).toHaveLength(6);

    // The next read uses the capacity that is actually displayed.
    expect(await organize.loadFolderImages(2)).toBe(true);
    expect(api.listFolderImages).toHaveBeenLastCalledWith(
      "lib-1",
      "folder-1",
      2,
      6,
      false,
      expect.any(AbortSignal),
    );
    wrapper.unmount();
  });
});
