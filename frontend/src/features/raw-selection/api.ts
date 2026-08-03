import { requestJson } from "../../api/gateway";

import type {
  DeleteResult,
  ExportResult,
  ImportResult,
  MemberListResult,
  RawMember,
  RawProject,
  WorkspaceState,
} from "./types";

export { GatewayError as RawSelectionApiError } from "../../api/gateway";

const BASE = "api/raw-selection";

export async function listProjects(signal?: AbortSignal) {
  return requestJson<{ projects: RawProject[] }>(`${BASE}/projects`, { signal });
}

export async function createProject(name: string, signal?: AbortSignal) {
  return requestJson<RawProject>(`${BASE}/projects`, {
    method: "POST",
    body: { name },
    signal,
  });
}

export async function getProject(id: string, signal?: AbortSignal) {
  return requestJson<RawProject>(`${BASE}/projects/${id}`, { signal });
}

export async function renameProject(id: string, name: string, signal?: AbortSignal) {
  return requestJson<RawProject>(`${BASE}/projects/${id}`, {
    method: "PATCH",
    body: { name },
    signal,
  });
}

export async function deleteProject(id: string, signal?: AbortSignal) {
  return requestJson<{ ok: boolean }>(`${BASE}/projects/${id}`, {
    method: "DELETE",
    signal,
  });
}

export async function importFolder(
  projectId: string,
  path: string,
  signal?: AbortSignal,
) {
  return requestJson<ImportResult>(
    `${BASE}/projects/${projectId}/import-folder`,
    { method: "POST", body: { path }, signal },
  );
}

export async function importFiles(
  projectId: string,
  paths: string[],
  signal?: AbortSignal,
) {
  return requestJson<ImportResult>(
    `${BASE}/projects/${projectId}/import-files`,
    { method: "POST", body: { paths }, signal },
  );
}

export async function listMembers(
  projectId: string,
  params: Record<string, string>,
  signal?: AbortSignal,
) {
  const qs = new URLSearchParams(params).toString();
  return requestJson<MemberListResult>(
    `${BASE}/projects/${projectId}/members?${qs}`,
    { signal },
  );
}

export async function getMember(id: string, signal?: AbortSignal) {
  return requestJson<RawMember>(`${BASE}/members/${id}`, { signal });
}

export async function updateRating(
  memberId: string,
  starRating: number,
  colorLabel: string,
  signal?: AbortSignal,
) {
  return requestJson<{ ok: boolean }>(
    `${BASE}/members/${memberId}/rating`,
    { method: "PATCH", body: { star_rating: starRating, color_label: colorLabel }, signal },
  );
}

export async function updateCreativeLook(
  memberId: string,
  creativeLook: string,
  signal?: AbortSignal,
) {
  return requestJson<{ ok: boolean }>(
    `${BASE}/members/${memberId}/creative-look`,
    { method: "PATCH", body: { creative_look: creativeLook }, signal },
  );
}

export async function getWorkspaceState(projectId: string, signal?: AbortSignal) {
  return requestJson<WorkspaceState>(
    `${BASE}/projects/${projectId}/workspace`,
    { signal },
  );
}

export async function saveWorkspaceState(
  projectId: string,
  state: Partial<WorkspaceState>,
  signal?: AbortSignal,
) {
  return requestJson<{ ok: boolean }>(
    `${BASE}/projects/${projectId}/workspace`,
    { method: "PUT", body: state, signal },
  );
}

export async function exportFiles(
  projectId: string,
  memberIds: string[],
  destination: string,
  signal?: AbortSignal,
) {
  return requestJson<ExportResult>(
    `${BASE}/projects/${projectId}/export`,
    { method: "POST", body: { member_ids: memberIds, destination }, signal },
  );
}

export async function removeMembers(memberIds: string[], signal?: AbortSignal) {
  return requestJson<{ removed: number }>(`${BASE}/members/remove`, {
    method: "POST",
    body: { member_ids: memberIds },
    signal,
  });
}

export async function permanentDelete(
  memberIds: string[],
  confirmed: boolean,
  signal?: AbortSignal,
) {
  return requestJson<DeleteResult>(`${BASE}/members/delete-permanent`, {
    method: "POST",
    body: { member_ids: memberIds, confirmed },
    signal,
  });
}

export async function clearProjectCache(projectId: string, signal?: AbortSignal) {
  return requestJson<{ ok: boolean }>(
    `${BASE}/projects/${projectId}/clear-cache`,
    { method: "POST", signal },
  );
}
