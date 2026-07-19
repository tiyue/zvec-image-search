import type {
  FolderDeleteCommitRequest,
  FolderDeletePreviewWire,
  OrganizeApi,
  OrganizeBootstrapResponse,
  OrganizeFolderImagesResponse,
  OrganizeFolderListResponse,
  OrganizeJobResponse,
} from "./types";
import { requestJson } from "../../api/gateway";

export { GatewayError as OrganizeApiError } from "../../api/gateway";

export const organizeApi: OrganizeApi = {
  bootstrap: (signal) =>
    requestJson<OrganizeBootstrapResponse>("api/bootstrap", { signal }),
  listFolders: (libraryId, query, offset, limit, signal) => {
    const parameters = new URLSearchParams({
      query,
      offset: String(offset),
      limit: String(limit),
    });
    return requestJson<OrganizeFolderListResponse>(
      `api/libraries/${encodeURIComponent(libraryId)}/folders?${parameters}`,
      { signal },
    );
  },
  listFolderImages: (
    libraryId,
    folderKey,
    page,
    pageSize,
    includeSubfolders,
    signal,
  ) => {
    const parameters = new URLSearchParams({
      folder_key: folderKey,
      page: String(page),
      page_size: String(pageSize),
      include_subfolders: String(includeSubfolders),
    });
    return requestJson<OrganizeFolderImagesResponse>(
      `api/libraries/${encodeURIComponent(libraryId)}/folder-images?${parameters}`,
      { signal },
    );
  },
  submitJob: (body, signal) =>
    requestJson<OrganizeJobResponse>("api/jobs", {
      method: "POST",
      body,
      signal,
    }),
  job: (jobId, signal) =>
    requestJson<OrganizeJobResponse>(`api/jobs/${encodeURIComponent(jobId)}`, {
      signal,
    }),
  cancelJob: (jobId, signal) =>
    requestJson<OrganizeJobResponse>(`api/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      signal,
    }),
  previewFolderDelete: (libraryId, folderKey, includeSubfolders, signal) =>
    requestJson<FolderDeletePreviewWire>(
      `api/libraries/${encodeURIComponent(libraryId)}/folder-delete/preview`,
      {
        method: "POST",
        body: {
          folder_key: folderKey,
          include_subfolders: includeSubfolders,
        },
        signal,
      },
    ),
  commitFolderDelete: (libraryId, request: FolderDeleteCommitRequest, signal) =>
    requestJson<OrganizeJobResponse>(
      `api/libraries/${encodeURIComponent(libraryId)}/folder-delete/commit`,
      {
        method: "POST",
        body: request,
        signal,
      },
    ),
};
