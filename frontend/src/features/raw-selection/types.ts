export type ColorLabel = "none" | "red" | "yellow" | "green" | "blue" | "purple";
export type StarRating = 0 | 1 | 2 | 3 | 4 | 5;
export type SortField =
  | "filename"
  | "import_order"
  | "shot_time"
  | "mtime_ns"
  | "star_rating"
  | "extension"
  | "file_size";
export type SortDirection = "asc" | "desc";
export type FilterStarMode = "none" | "exact" | "at_least";
export type FilterRated = "all" | "rated" | "unrated";
export type FilterExported = "all" | "exported" | "unexported";
export type RawFormatFilter = "arw" | "jpeg" | "png";
export type OrientationFilter = "landscape" | "portrait" | "square";

export interface RawProject {
  id: string;
  name: string;
  member_count: number;
  cover_member_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface RawMember {
  id: string;
  project_id: string;
  asset_id: string;
  import_order: number;
  star_rating: number;
  color_label: string;
  creative_look: string;
  file_name: string;
  normalized_path: string;
  extension: string;
  file_size: number;
  mtime_ns: number;
  file_identity: string | null;
  shot_time: string | null;
  width: number | null;
  height: number | null;
  metadata_status: string;
  exported_at: string | null;
}

export interface MemberListResult {
  members: RawMember[];
  total: number;
  filtered: number;
}

export interface ImportResult {
  log_id?: string;
  registered: number;
  skipped_unsupported: number;
  skipped_raw_formats: number;
  skipped_unsupported_camera: number;
  skipped_reparse: number;
  errors: number;
  error_details: string[];
  unsupported_camera_details: string[];
}

export interface WorkspaceState {
  last_member_id: string | null;
  filter_star_mode: string;
  filter_star_value: number;
  filter_color_labels: string;
  filter_filename: string;
  filter_rated: string;
  filter_exported: string;
  filter_formats: string;
  filter_orientations: string;
  sort_field: string;
  sort_direction: string;
  filmstrip_scroll: number;
}

export interface DeleteResult {
  deleted: number;
  already_missing: number;
  failed: number;
  details: Array<{ path: string; result: string; error?: string }>;
  log_id?: string;
}

export interface ExportResult {
  exported: number;
  skipped: number;
  failed: number;
  cancelled: number;
  log_id: string;
  details: Array<{ path: string; dest?: string; result: string; error?: string; member_id?: string }>;
}

export interface RawSelectionJob {
  id: string;
  kind: "import" | "export";
  project_id: string;
  log_id: string | null;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  phase: string;
  progress: Record<string, number>;
  result: ImportResult | ExportResult | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface SourceStatus {
  status: "ok" | "refreshed" | "error";
  code: string;
  message: string;
}
