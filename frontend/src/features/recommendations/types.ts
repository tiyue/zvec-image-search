export type RecommendationBucket = "quality" | "recent" | "low_exposure" | "random";
export type RecommendationAction = "open" | "like" | "export" | "dislike";
export type RecommendationPreference = "like" | "dislike";

export interface RecommendationItemWire {
  item_id?: unknown;
  media_id?: unknown;
  name?: unknown;
  width?: unknown;
  height?: unknown;
  tags?: unknown;
  library_id?: unknown;
  library_name?: unknown;
  content_type?: unknown;
  size_bytes?: unknown;
  bucket?: unknown;
  thumbnail_url?: unknown;
  preview_url?: unknown;
}

export interface RecommendationDiversityWire {
  applied?: unknown;
  reason?: unknown;
  missing_vectors?: unknown;
  vector_space?: unknown;
}

export interface RecommendationBatchWire {
  request_id?: unknown;
  batch_id?: unknown;
  count?: unknown;
  partial?: unknown;
  partial_reason?: unknown;
  quota_degraded?: unknown;
  history_window?: unknown;
  items?: RecommendationItemWire[];
  quota?: Partial<Record<RecommendationBucket, unknown>>;
  diversity?: RecommendationDiversityWire;
}

export interface RecommendationItem {
  itemId: string;
  mediaId: string;
  name: string;
  width: number;
  height: number;
  tags: string[];
  libraryId: string;
  libraryName: string;
  contentType: string;
  sizeBytes: number;
  bucket: RecommendationBucket;
  thumbnailUrl: string;
  previewUrl: string;
}

export interface RecommendationDiversity {
  applied: boolean;
  reason: string;
  missingVectors: number;
  vectorSpace: string;
}

export interface RecommendationBatch {
  requestId: string;
  batchId: string;
  count: number;
  partial: boolean;
  partialReason: string;
  quotaDegraded: boolean;
  historyWindow: number;
  items: RecommendationItem[];
  quota: Record<RecommendationBucket, number>;
  diversity: RecommendationDiversity;
}

export interface RecommendationEventResponse {
  ok?: boolean;
  event_id?: string;
}

export interface RecommendationApi {
  create(requestId: string, signal?: AbortSignal): Promise<RecommendationBatchWire>;
  shown(batchId: string, eventId: string, signal?: AbortSignal): Promise<RecommendationEventResponse>;
  action(
    batchId: string,
    eventId: string,
    itemId: string,
    action: RecommendationAction,
    signal?: AbortSignal,
  ): Promise<RecommendationEventResponse>;
}
