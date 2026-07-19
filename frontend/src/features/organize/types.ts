export interface OrganizeLibrary {
  id: string;
  name: string;
  enabled?: boolean;
  is_default?: boolean;
  image_root?: unknown;
  root_id?: unknown;
}

export interface OrganizeAliasWire {
  canonical_name?: unknown;
  canonical?: unknown;
  aliases?: unknown;
  [key: string]: unknown;
}

export interface OrganizeFolderWire {
  folder_key?: unknown;
  key?: unknown;
  root_id?: unknown;
  root_name?: unknown;
  root_path?: unknown;
  relative_folder?: unknown;
  relative_path?: unknown;
  name?: unknown;
  image_count?: unknown;
  direct_image_count?: unknown;
  descendant_image_count?: unknown;
  first_indexed_at?: unknown;
  last_indexed_at?: unknown;
  timestamp_source?: unknown;
  total?: unknown;
  manual_tagged_count?: unknown;
  model_tagged_count?: unknown;
  failed_count?: unknown;
  [key: string]: unknown;
}

export interface OrganizeRootWire {
  root_id?: unknown;
  root_name?: unknown;
  root_path?: unknown;
  folder_key?: unknown;
  folder_count?: unknown;
  image_count?: unknown;
  first_indexed_at?: unknown;
  last_indexed_at?: unknown;
  [key: string]: unknown;
}

export interface OrganizeTagSourceWire {
  tag?: unknown;
  source?: unknown;
  sources?: unknown;
  source_kind?: unknown;
  kind?: unknown;
  label?: unknown;
  field?: unknown;
  entity_type?: unknown;
  confidence?: unknown;
  inherited?: unknown;
  [key: string]: unknown;
}

export interface OrganizeImageWire {
  doc_id?: unknown;
  id?: unknown;
  image_id?: unknown;
  proposal_id?: unknown;
  library_id?: unknown;
  file_name?: unknown;
  filename?: unknown;
  name?: unknown;
  relative_path?: unknown;
  width?: unknown;
  height?: unknown;
  manual_tags?: unknown;
  tags?: unknown;
  folder_tags?: unknown;
  model_tags?: unknown;
  accepted_auto_tags?: unknown;
  inherited_tags?: unknown;
  effective_tags?: unknown;
  existing_tags?: unknown;
  tag_sources?: unknown;
  tag_details?: unknown;
  annotation_status?: unknown;
  status?: unknown;
  thumbnail_url?: unknown;
  image_url?: unknown;
  image_available?: unknown;
  [key: string]: unknown;
}

export interface OrganizeBootstrapResponse {
  libraries?: OrganizeLibrary[];
  organize?: {
    undo_available?: boolean;
    proposals?: OrganizeImageWire[];
    pending_reviews?: OrganizeImageWire[];
    aliases?: OrganizeAliasWire[];
    error?: { message?: string } | null;
  };
  proposals?: OrganizeImageWire[];
  aliases?: OrganizeAliasWire[];
  undo_available?: boolean;
}

export interface OrganizeFolderListResponse {
  folders?: OrganizeFolderWire[];
  roots?: OrganizeRootWire[];
  result?: {
    folders?: OrganizeFolderWire[];
    roots?: OrganizeRootWire[];
    offset?: unknown;
    limit?: unknown;
    total?: unknown;
    has_more?: unknown;
  };
  offset?: unknown;
  limit?: unknown;
  total?: unknown;
  has_more?: unknown;
}

export interface FolderDeletePreviewWire {
  operation_id?: unknown;
  confirmation_token?: unknown;
  preview_token?: unknown;
  preview_id?: unknown;
  token?: unknown;
  folder_key?: unknown;
  folder_name?: unknown;
  relative_folder?: unknown;
  root_name?: unknown;
  include_subfolders?: unknown;
  image_count?: unknown;
  file_count?: unknown;
  size_bytes?: unknown;
  protected_count?: unknown;
  changed_count?: unknown;
  missing_count?: unknown;
  confirmation_phrase?: unknown;
  expires_at?: unknown;
  warnings?: unknown;
  blocked?: unknown;
  result?: FolderDeletePreviewWire;
  [key: string]: unknown;
}

export interface FolderDeleteCommitRequest {
  operation_id: string;
  confirmation_token: string;
  confirm: true;
}

export interface OrganizeFolderImagesResponse {
  items?: OrganizeImageWire[];
  images?: OrganizeImageWire[];
  result?: {
    items?: OrganizeImageWire[];
    images?: OrganizeImageWire[];
    page?: unknown;
    page_size?: unknown;
    total?: unknown;
    total_items?: unknown;
    has_more?: unknown;
  };
  page?: unknown;
  page_size?: unknown;
  total?: unknown;
  total_items?: unknown;
  has_more?: unknown;
}

export interface OrganizeJobWire {
  id?: unknown;
  job_id?: unknown;
  status?: unknown;
  state?: unknown;
  message?: unknown;
  processed?: unknown;
  total?: unknown;
  progress_percent?: unknown;
  progress?: {
    processed?: unknown;
    completed?: unknown;
    current?: unknown;
    total?: unknown;
    percent?: unknown;
    percentage?: unknown;
    message?: unknown;
  };
  result?: {
    updated?: unknown;
    failed?: unknown;
    undo_available?: unknown;
    [key: string]: unknown;
  };
  error?: { message?: unknown } | unknown;
  [key: string]: unknown;
}

export interface OrganizeJobResponse {
  job?: OrganizeJobWire;
  jobs?: OrganizeJobWire[];
  result?: Record<string, unknown>;
  undo_available?: unknown;
}

export interface OrganizeApi {
  bootstrap(signal?: AbortSignal): Promise<OrganizeBootstrapResponse>;
  listFolders(
    libraryId: string,
    query: string,
    offset: number,
    limit: number,
    signal?: AbortSignal,
  ): Promise<OrganizeFolderListResponse>;
  listFolderImages(
    libraryId: string,
    folderKey: string,
    page: number,
    pageSize: number,
    includeSubfolders: boolean,
    signal?: AbortSignal,
  ): Promise<OrganizeFolderImagesResponse>;
  submitJob(
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<OrganizeJobResponse>;
  job(jobId: string, signal?: AbortSignal): Promise<OrganizeJobResponse>;
  cancelJob?(
    jobId: string,
    signal?: AbortSignal,
  ): Promise<OrganizeJobResponse>;
  previewFolderDelete?(
    libraryId: string,
    folderKey: string,
    includeSubfolders: boolean,
    signal?: AbortSignal,
  ): Promise<FolderDeletePreviewWire>;
  commitFolderDelete?(
    libraryId: string,
    request: FolderDeleteCommitRequest,
    signal?: AbortSignal,
  ): Promise<OrganizeJobResponse>;
}

export interface OrganizeFolder {
  key: string;
  rootId: string;
  rootName: string;
  relativeFolder: string;
  name: string;
  imageCount: number;
  directImageCount: number;
  descendantImageCount: number;
  firstIndexedAt: string;
  lastIndexedAt: string;
  timestampSource: string;
  manualTaggedCount: number;
  modelTaggedCount: number;
  failedCount: number;
}

export interface OrganizeTagSource {
  tag: string;
  source: string;
  sourceLabel: string;
  field: string;
  entityType: string;
  confidence: number | null;
  inherited: boolean;
}

export interface OrganizeImage {
  id: string;
  nativeImageId: string;
  libraryId: string;
  fileName: string;
  relativePath: string;
  width: number;
  height: number;
  manualTags: string[];
  folderTags: string[];
  modelTags: string[];
  inheritedTags: string[];
  effectiveTags: string[];
  tagSources: OrganizeTagSource[];
  annotationStatus: string;
  thumbnailUrl: string;
  imageUrl: string;
  imageAvailable: boolean;
}

export interface FolderDeletePreview {
  operationId: string;
  confirmationToken: string;
  folderKey: string;
  folderName: string;
  relativeFolder: string;
  rootName: string;
  includeSubfolders: true;
  imageCount: number;
  fileCount: number;
  sizeBytes: number;
  protectedCount: number;
  changedCount: number;
  missingCount: number;
  confirmationPhrase: string;
  expiresAt: string;
  warnings: string[];
  blocked: boolean;
}

export interface OrganizeAlias {
  canonical: string;
  aliases: string[];
}

export interface TagPresence {
  tag: string;
  count: number;
  total: number;
  sourceLabels: string[];
}

export interface OrganizeJob {
  id: string;
  status: string;
  message: string;
  processed: number;
  total: number;
  percent: number;
  updated: number;
  failed: number;
  undoAvailable: boolean;
}

export interface ExplicitSelection {
  mode: "selected";
  doc_ids: string[];
}

export interface FolderSelection {
  mode: "folder";
  folder_key: string;
  include_subfolders: false;
  excluded_doc_ids: string[];
}

export type OrganizeSelection = ExplicitSelection | FolderSelection;
export type ManualTagOperation = "add" | "remove" | "replace_manual";
export type ToastKind = "info" | "success" | "error";
