export type ColorLabel = "none" | "red" | "yellow" | "green" | "blue" | "purple";
export type StarRating = 0 | 1 | 2 | 3 | 4 | 5;
export type SortField = "filename" | "import_order" | "shot_time" | "star_rating";
export type SortDirection = "asc" | "desc";
export type FilterStarMode = "none" | "exact" | "at_least";
export type FilterRated = "all" | "rated" | "unrated";

export interface RawProject {
  id: string;
  name: string;
  member_count: number;
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
}

export interface MemberListResult {
  members: RawMember[];
  total: number;
  filtered: number;
}

export interface ImportResult {
  registered: number;
  skipped_unsupported: number;
  skipped_raw_formats: number;
  skipped_reparse: number;
  errors: number;
  error_details: string[];
}

export interface WorkspaceState {
  last_member_id: string | null;
  filter_star_mode: string;
  filter_star_value: number;
  filter_color_labels: string;
  filter_filename: string;
  filter_rated: string;
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
  details: Array<{ path: string; dest?: string; result: string; error?: string; member_id?: string }>;
}
