import { requestJson } from "../../api/gateway";
import type {
  FeedbackPage,
  SearchLearningApi,
  SearchLearningSettings,
  SearchLearningStatus,
} from "./types";

export const searchLearningApi: SearchLearningApi = {
  status: (signal) =>
    requestJson<SearchLearningStatus>("api/search-learning/status", { signal }),
  updateSettings: (body, signal) =>
    requestJson<SearchLearningSettings>("api/search-learning/settings", {
      method: "PUT",
      body,
      signal,
    }),
  feedback: (body, signal) =>
    requestJson("api/search-feedback", {
      method: "POST",
      body,
      signal,
    }),
  revoke: (eventId, signal) =>
    requestJson(`api/search-feedback/${encodeURIComponent(eventId)}`, {
      method: "DELETE",
      signal,
    }),
  listFeedback: (sessionId, cursor, signal) => {
    const query = new URLSearchParams({
      session_id: sessionId,
      limit: "200",
      active_only: "true",
    });
    if (cursor) query.set("cursor", cursor);
    return requestJson<FeedbackPage>(`api/search-feedback?${query.toString()}`, {
      signal,
    });
  },
  train: (signal) =>
    requestJson("api/search-learning/train", {
      method: "POST",
      body: {},
      signal,
    }),
  installEvaluation: (sourcePath, signal) =>
    requestJson("api/search-learning/fixed-evaluation", {
      method: "POST",
      body: { source_path: sourcePath },
      signal,
    }),
  activate: (modelVersion, shadowMode, signal) =>
    requestJson<SearchLearningSettings>("api/search-learning/activate", {
      method: "POST",
      body: { model_version: modelVersion, shadow_mode: shadowMode },
      signal,
    }),
  rollback: (signal) =>
    requestJson<SearchLearningSettings>("api/search-learning/rollback", {
      method: "POST",
      body: {},
      signal,
    }),
  clear: (signal) =>
    requestJson("api/search-learning/data", {
      method: "DELETE",
      body: { confirm: true },
      signal,
    }),
  exportData: (signal) =>
    requestJson("api/search-learning/export", { signal }),
};
