export type LibraryTaskType = "index" | "sync" | "index_and_auto_tag" | "auto_tag";

export type AutoTagScope =
  | "untagged"
  | "latest_index_run"
  | "failed"
  | "failed_all"
  | "all";

export interface TaskLibrary {
  id: string;
  name: string;
  enabled?: boolean;
}

export interface JobSubmission {
  task_type: LibraryTaskType;
  library_id: string;
  max_images?: number;
  scope?: AutoTagScope;
  max_budget_cny?: number;
  external_processing_confirmed?: true;
  all_scope_confirmed?: true;
}

export interface AutoTagEstimateSubmission {
  task_type: "auto_tag_estimate";
  library_id: string;
  scope: AutoTagScope;
  max_images: number;
  max_budget_cny?: number;
}

export interface AutoTagEstimate {
  candidateCount: number;
  uniqueImageCount: number;
  cachedCount: number;
  apiRequestCount: number;
  estimatedInputTokens: number;
  estimatedOutputTokens: number;
  estimatedCostCny: number | null;
  overBudget: boolean;
}

export interface JobErrorImageWire {
  id?: string;
  name?: string;
  filename?: string;
  relative_path?: string;
  reason?: string;
  message?: string;
  error?: string;
  thumbnail_url?: string;
  image_url?: string;
}

export interface JobProgressWire {
  processed?: number;
  completed?: number;
  current?: number;
  total?: number;
  candidate_count?: number;
  items?: number;
  percent?: number;
  percentage?: number;
  message?: string;
}

export interface JobWire {
  id?: string;
  job_id?: string;
  task_type?: string;
  type?: string;
  command?: string;
  library_id?: string;
  library_name?: string;
  status?: string;
  state?: string;
  progress?: JobProgressWire;
  progress_percent?: number;
  progress_message?: string;
  message?: string;
  processed?: number;
  total?: number;
  failure_count?: number;
  failed?: number;
  error_images?: JobErrorImageWire[];
  failures?: JobErrorImageWire[];
  created_at?: string;
  submitted_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: unknown;
  result?: Record<string, unknown>;
}

export interface JobsResponse {
  jobs?: JobWire[];
  job?: JobWire;
}

export interface JobErrorImage {
  id: string;
  name: string;
  reason: string;
  thumbnailUrl: string;
  imageUrl: string;
}

export interface LibraryJob {
  id: string;
  type: string;
  libraryId: string;
  libraryName: string;
  status: string;
  progressPercent: number;
  message: string;
  processed: number;
  total: number;
  failureCount: number;
  errorImages: JobErrorImage[];
  createdAt: string;
  startedAt: string;
  finishedAt: string;
}

export interface JobsApi {
  list(signal?: AbortSignal): Promise<JobsResponse | JobWire[]>;
  submit(
    body: JobSubmission | AutoTagEstimateSubmission,
    signal?: AbortSignal,
  ): Promise<JobsResponse | JobWire>;
  detail(jobId: string, signal?: AbortSignal): Promise<JobsResponse | JobWire>;
  cancel(jobId: string, signal?: AbortSignal): Promise<JobsResponse | JobWire>;
}
