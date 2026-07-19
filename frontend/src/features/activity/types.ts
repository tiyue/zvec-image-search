export type ActivityLevel = "debug" | "info" | "warning" | "error";

export interface ActivityLibrary {
  id: string;
  name: string;
}

export interface JobHistoryFilters {
  status: string;
  taskType: string;
  libraryId: string;
  query: string;
}

export interface ActivityLogFilters {
  level: string;
  category: string;
  libraryId: string;
  jobId: string;
  query: string;
}

export interface JobHistoryWire {
  id?: unknown;
  job_id?: unknown;
  task_type?: unknown;
  library_id?: unknown;
  library_name?: unknown;
  status?: unknown;
  submitted_at?: unknown;
  created_at?: unknown;
  started_at?: unknown;
  finished_at?: unknown;
  updated_at?: unknown;
  processed?: unknown;
  total?: unknown;
  failed?: unknown;
  failure_count?: unknown;
  progress?: unknown;
  message?: unknown;
  result_summary?: unknown;
  error_code?: unknown;
  error_message?: unknown;
  [key: string]: unknown;
}

export interface ActivityLogWire {
  sequence?: unknown;
  timestamp?: unknown;
  level?: unknown;
  category?: unknown;
  event?: unknown;
  source?: unknown;
  library_id?: unknown;
  library_name?: unknown;
  job_id?: unknown;
  operation_id?: unknown;
  message?: unknown;
  details?: unknown;
  details_json?: unknown;
  thumbnail_url?: unknown;
  image_url?: unknown;
  [key: string]: unknown;
}

export interface CursorPageWire<T> {
  items?: T[];
  jobs?: T[];
  logs?: T[];
  next_cursor?: unknown;
  has_more?: unknown;
  total_count?: unknown;
  total?: unknown;
}

export interface JobHistoryItem {
  id: string;
  taskType: string;
  libraryId: string;
  libraryName: string;
  status: string;
  submittedAt: string;
  startedAt: string;
  finishedAt: string;
  updatedAt: string;
  processed: number;
  total: number;
  failed: number;
  progressPercent: number;
  message: string;
  resultSummary: Record<string, unknown>;
  errorCode: string;
  errorMessage: string;
}

export interface ActivityLogItem {
  sequence: number;
  timestamp: string;
  level: ActivityLevel;
  category: string;
  event: string;
  source: string;
  libraryId: string;
  libraryName: string;
  jobId: string;
  operationId: string;
  message: string;
  details: Record<string, unknown>;
  thumbnailUrl: string;
  imageUrl: string;
}

export interface CursorPage<T> {
  items: T[];
  nextCursor: string;
  hasMore: boolean;
  totalCount: number;
}

export interface JobHistoryQuery extends JobHistoryFilters {
  cursor?: string;
  limit?: number;
}

export interface ActivityLogQuery extends ActivityLogFilters {
  cursor?: string;
  limit?: number;
}

export interface ActivityApi {
  listJobHistory(
    query: JobHistoryQuery,
    signal?: AbortSignal,
  ): Promise<CursorPageWire<JobHistoryWire>>;
  listActivityLogs(
    query: ActivityLogQuery,
    signal?: AbortSignal,
  ): Promise<CursorPageWire<ActivityLogWire>>;
  cancelJob(jobId: string, signal?: AbortSignal): Promise<unknown>;
}

export type ActivityExportFormat = "jsonl" | "csv";

export const EMPTY_JOB_FILTERS: Readonly<JobHistoryFilters> = Object.freeze({
  status: "",
  taskType: "",
  libraryId: "",
  query: "",
});

export const EMPTY_LOG_FILTERS: Readonly<ActivityLogFilters> = Object.freeze({
  level: "",
  category: "",
  libraryId: "",
  jobId: "",
  query: "",
});
