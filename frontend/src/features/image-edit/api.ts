import { GatewayError, requestJson } from "../../api/gateway";
import type {
  ImageEditRequestPayload,
  ImageEditSettings,
  ImageEditTaskWire,
} from "./types";

const BASE = "api/image-edit";

interface TaskResponse {
  task: ImageEditTaskWire;
}

interface TaskListResponse {
  tasks: ImageEditTaskWire[];
  count: number;
  total_count: number;
}

async function readTaskResponse(response: Response): Promise<ImageEditTaskWire> {
  let payload: TaskResponse & {
    error?: { code?: string; message?: string };
  };
  try {
    payload = await response.json() as typeof payload;
  } catch (error) {
    throw new GatewayError("本地服务返回了无法识别的数据。", response.status, error);
  }
  if (!response.ok || !payload.task) {
    throw new GatewayError(
      payload.error?.message ?? payload.error?.code ?? `图片编辑请求失败（HTTP ${response.status}）。`,
      response.status,
      payload,
    );
  }
  return payload.task;
}

export const imageEditApi = {
  settings: (signal?: AbortSignal) =>
    requestJson<ImageEditSettings>(`${BASE}/settings`, { signal }),

  updateSettings: (outputDirectory: string, signal?: AbortSignal) =>
    requestJson<ImageEditSettings>(`${BASE}/settings`, {
      method: "PUT",
      body: { output_directory: outputDirectory },
      signal,
    }),

  submitFile: async (
    file: File,
    metadata: ImageEditRequestPayload,
    signal?: AbortSignal,
  ): Promise<ImageEditTaskWire> => {
    const form = new FormData();
    form.append("metadata", JSON.stringify(metadata));
    form.append("file", file, file.name);
    let response: Response;
    try {
      response = await fetch(`${BASE}/tasks`, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: form,
        signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new GatewayError("无法连接本地图片编辑服务。", 0, error);
    }
    return readTaskResponse(response);
  },

  submitRegistered: (
    sourceImageId: string,
    metadata: ImageEditRequestPayload,
    signal?: AbortSignal,
  ) => requestJson<TaskResponse>(`${BASE}/tasks`, {
    method: "POST",
    body: { source_image_id: sourceImageId, ...metadata },
    signal,
  }).then((payload) => payload.task),

  list: (signal?: AbortSignal) =>
    requestJson<TaskListResponse>(`${BASE}/tasks?limit=200`, { signal }),

  cancel: (taskId: string, signal?: AbortSignal) =>
    requestJson<TaskResponse>(`${BASE}/tasks/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
      signal,
    }).then((payload) => payload.task),

  abandonAll: (signal?: AbortSignal) =>
    requestJson<{ abandoned_task_ids: string[]; count: number }>(
      `${BASE}/tasks/abandon`,
      { method: "POST", body: { confirm: true }, signal },
    ),
};
