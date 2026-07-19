import type { SearchResultItem } from "../../types/contracts";

export type FeedbackAction =
  | "relevant"
  | "not_relevant"
  | "export"
  | "copy"
  | "open"
  | "detail";

export interface SearchFeedbackEvent {
  event_id: string;
  session_id: string;
  library_id: string;
  doc_id: string;
  action: FeedbackAction;
  feedback_weight: number;
  created_at: string;
  source: string;
  active: boolean;
  revoked_at?: string | null;
  created?: boolean;
}

export interface SearchLearningSettings {
  learning_enabled: boolean;
  implicit_feedback_enabled: boolean;
  save_query_text: boolean;
  active_model_version?: string | null;
  previous_model_version?: string | null;
  shadow_mode: boolean;
  updated_at?: string;
}

export interface SearchLearningStatus {
  available: boolean;
  database: string;
  online_weight_updates: boolean;
  fixed_evaluation_gate_required: boolean;
  fixed_evaluation?: {
    available?: boolean;
    status?: string;
    evaluation_set_id?: string;
    case_count?: number;
    no_answer_count?: number;
    candidate_count?: number;
    error_code?: string;
  };
  training_running: boolean;
  settings: SearchLearningSettings;
  training_counts: {
    query_sessions: number;
    explicit_samples: number;
    positive_samples: number;
    negative_samples: number;
  };
  minimum_requirements: {
    query_sessions: number;
    explicit_samples: number;
    positive_and_negative_required: boolean;
  };
  latest_job?: Record<string, unknown> | null;
  model_versions?: SearchLearningModelVersion[];
}

export interface SearchLearningModelVersion {
  model_version: string;
  status?: string;
  gate_status?: "passed" | "failed" | "pending" | string;
  created_at?: string;
  query_sessions?: number;
  explicit_samples?: number;
  metrics?: SearchLearningGateResult;
}

export interface SearchLearningEvaluationMetrics {
  precision_at_15?: number;
  recall_at_15?: number;
  no_answer_false_positive_rate?: number;
  cross_collection_bias?: number;
  p95_latency_ms?: number;
  external_api_calls?: number;
  returned_count?: number;
  relevant_returned_count?: number;
  case_count?: number;
}

export interface SearchLearningEvaluationReport {
  fixed_evaluation_set?: boolean;
  evaluation_set_id?: string;
  current_model_version?: string;
  candidate_model_version?: string;
  current?: SearchLearningEvaluationMetrics;
  candidate?: SearchLearningEvaluationMetrics;
}

export interface SearchLearningGateResult {
  status?: "passed" | "failed" | "pending" | string;
  reasons?: string[];
  metrics?: SearchLearningEvaluationReport;
}

export interface FeedbackPage {
  items: SearchFeedbackEvent[];
  has_more: boolean;
  next_cursor?: string | null;
}

export interface SearchLearningApi {
  status(signal?: AbortSignal): Promise<SearchLearningStatus>;
  updateSettings(
    body: Partial<
      Pick<
        SearchLearningSettings,
        "learning_enabled" | "implicit_feedback_enabled" | "save_query_text"
      >
    >,
    signal?: AbortSignal,
  ): Promise<SearchLearningSettings>;
  feedback(
    body: {
      session_id: string;
      library_id: string;
      doc_id: string;
      action: FeedbackAction;
      source: string;
    },
    signal?: AbortSignal,
  ): Promise<SearchFeedbackEvent | { accepted: false; reason: string }>;
  revoke(eventId: string, signal?: AbortSignal): Promise<SearchFeedbackEvent>;
  listFeedback(
    sessionId: string,
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<FeedbackPage>;
  train(signal?: AbortSignal): Promise<Record<string, unknown>>;
  installEvaluation(
    sourcePath: string,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>>;
  activate(
    modelVersion: string,
    shadowMode: boolean,
    signal?: AbortSignal,
  ): Promise<SearchLearningSettings>;
  rollback(signal?: AbortSignal): Promise<SearchLearningSettings>;
  clear(signal?: AbortSignal): Promise<Record<string, unknown>>;
  exportData(signal?: AbortSignal): Promise<Record<string, unknown>>;
}

export function feedbackKey(
  item: Pick<SearchResultItem, "searchSessionId" | "libraryId" | "docId">,
): string {
  return `${item.searchSessionId}\u0000${item.libraryId}\u0000${item.docId}`;
}

export function canRecordFeedback(
  item: Pick<SearchResultItem, "searchSessionId" | "libraryId" | "docId">,
): boolean {
  return Boolean(
    item.searchSessionId &&
      item.searchSessionId !== "latest" &&
      item.libraryId &&
      item.docId,
  );
}
