export type SearchMode = "semantic" | "tags";
export type SearchRequestMode = "text" | "tag" | "image" | "combined";
export type TagMatchMode = "all" | "any";
export type MatchState = "high" | "possible" | "weak" | string;

export interface LibrarySummary {
  id: string;
  name: string;
  enabled?: boolean;
  is_default?: boolean;
}

export interface SearchResultItem {
  id: string;
  searchSessionId: string;
  libraryId: string;
  docId: string;
  sha256: string;
  name: string;
  relativePath: string;
  libraryName: string;
  rank: number;
  matchState: MatchState;
  rankSource: string;
  width: number;
  height: number;
  tags: string[];
  matchedTags: string[];
  confidence: number | null;
  rawScore: number | null;
  normalizedScore: number | null;
  sizeBytes: number;
  imageUrl: string;
  thumbnailUrl: string;
}

export interface GallerySelectionIntent {
  id: string;
  index: number;
  additive: boolean;
  range: boolean;
}

export interface GalleryContextIntent {
  id: string;
  index: number;
  clientX: number;
  clientY: number;
}

export interface SearchPageResponse {
  id?: string;
  search_id?: string;
  job_id?: string;
  status?: string;
  message?: string;
  query?: string | Record<string, unknown>;
  elapsed_ms?: number | null;
  duration_ms?: number | null;
  items?: SearchResultWire[];
  results?: SearchResultWire[];
  result?: {
    items?: SearchResultWire[];
    total_items?: number;
    total_count?: number;
    candidate_count?: number;
  };
  page?: number;
  page_size?: number;
  total_items?: number;
  total_count?: number;
  candidate_count?: number;
  total_pages?: number;
  has_previous?: boolean;
  has_next?: boolean;
  source_label?: string;
  query_type?: string;
  history_id?: string;
  summary?: Record<string, unknown>;
  error?: { code?: string; message?: string };
  job?: { id?: string; status?: string };
}

export interface SearchHistoryEntry {
  id: string;
  label: string;
  query_type: string;
  created_at: string;
  total_items: number;
  status: string;
}

export interface SearchHistoryResponse {
  items: SearchHistoryEntry[];
}

export interface SearchResultWire {
  id?: string;
  search_session_id?: string;
  sha256?: string;
  document_id?: string;
  doc_id?: string;
  name?: string;
  filename?: string;
  relative_path?: string;
  display_path?: string;
  path?: string;
  library_name?: string;
  library_id?: string;
  collection_name?: string;
  rank?: number;
  match_state?: string;
  rank_source?: string;
  match_source?: string;
  width?: number;
  height?: number;
  tags?: unknown[];
  matched_tags?: unknown[];
  ranking_confidence?: number;
  confidence?: number;
  raw_score?: number;
  score?: number;
  normalized_score?: number;
  size_bytes?: number;
  file_size?: number;
  image_url?: string;
  thumbnail_url?: string;
}

export interface SearchSubmission {
  text?: string;
  mode: SearchRequestMode;
  library_ids: string[];
  top_k: number;
  page: 1;
  page_size: 15;
  query_image_id?: string;
  tag_mode?: TagMatchMode;
}

export interface BootstrapResponse {
  service?: {
    status?: string;
    ready?: boolean;
    backend_ready?: boolean;
  };
  libraries?: LibrarySummary[];
}

export interface NativeResult {
  ok: boolean;
  code?: string;
  error?: string;
  message?: string;
  action?: string;
  count?: number;
  selected?: boolean;
  cancelled?: boolean;
}

export interface NativeImageSelection extends NativeResult {
  id?: string;
  image_id?: string;
  name?: string;
  image?: {
    id?: string;
    image_id?: string;
    name?: string;
    thumbnail_url?: string;
  } | null;
}

export interface NativeExportJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "partial" | "failed" | string;
  destination_name?: string;
  total: number;
  processed: number;
  exported: number;
  skipped: number;
  message: string;
}

export interface NativeExportResult extends NativeResult {
  job?: NativeExportJob;
}

export interface NativeDirectorySelection extends NativeResult {
  path?: string | null;
}

export interface NativeFileSelection extends NativeResult {
  paths?: string[] | null;
}

export interface PywebviewApi {
  select_directory(): Promise<NativeDirectorySelection>;
  select_raw_images?(): Promise<NativeFileSelection>;
  select_json_file(): Promise<NativeDirectorySelection>;
  select_query_image(): Promise<NativeImageSelection>;
  exit_application?(): Promise<NativeResult>;
  open_image(imageId: string): Promise<NativeResult>;
  reveal_image(imageId: string): Promise<NativeResult>;
  copy_image(imageId: string): Promise<NativeResult>;
  copy_files(imageIds: string[]): Promise<NativeResult>;
  copy_image_paths(imageIds: string[]): Promise<NativeResult>;
  export_images(imageIds: string[]): Promise<NativeExportResult>;
  export_status(jobId: string): Promise<NativeExportResult>;
}

export interface ToastMessage {
  id: string;
  title: string;
  message: string;
  kind: "info" | "success" | "error";
}

declare global {
  interface Window {
    pywebview?: { api?: PywebviewApi };
  }
}
