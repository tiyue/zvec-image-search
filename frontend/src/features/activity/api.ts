import { requestJson } from "../../api/gateway";
import type {
  ActivityApi,
  ActivityLogQuery,
  ActivityLogWire,
  CursorPageWire,
  JobHistoryQuery,
  JobHistoryWire,
} from "./types";

export { GatewayError as ActivityApiError } from "../../api/gateway";

function addFilter(parameters: URLSearchParams, name: string, value: string): void {
  const normalized = value.trim();
  if (normalized) parameters.set(name, normalized);
}

function boundedLimit(value: number | undefined): number {
  if (!Number.isFinite(value)) return 50;
  return Math.min(200, Math.max(1, Math.trunc(value ?? 50)));
}

export function jobHistoryQueryString(query: JobHistoryQuery): string {
  const parameters = new URLSearchParams({ limit: String(boundedLimit(query.limit)) });
  addFilter(parameters, "cursor", query.cursor ?? "");
  addFilter(parameters, "status", query.status);
  addFilter(parameters, "task_type", query.taskType);
  addFilter(parameters, "library_id", query.libraryId);
  addFilter(parameters, "query", query.query);
  return parameters.toString();
}

export function activityLogQueryString(query: ActivityLogQuery): string {
  const parameters = new URLSearchParams({ limit: String(boundedLimit(query.limit)) });
  addFilter(parameters, "cursor", query.cursor ?? "");
  addFilter(parameters, "level", query.level);
  addFilter(parameters, "category", query.category);
  addFilter(parameters, "library_id", query.libraryId);
  addFilter(parameters, "job_id", query.jobId);
  addFilter(parameters, "query", query.query);
  return parameters.toString();
}

export const activityApi: ActivityApi = {
  listJobHistory: (query, signal) =>
    requestJson<CursorPageWire<JobHistoryWire>>(
      `api/job-history?${jobHistoryQueryString(query)}`,
      { signal },
    ),
  listActivityLogs: (query, signal) =>
    requestJson<CursorPageWire<ActivityLogWire>>(
      `api/activity-logs?${activityLogQueryString(query)}`,
      { signal },
    ),
  cancelJob: (jobId, signal) =>
    requestJson(`api/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      signal,
    }),
};
