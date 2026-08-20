export interface SettingsLibraryWire {
  id?: unknown;
  name?: unknown;
  image_root?: unknown;
  workspace_directory?: unknown;
  enabled?: unknown;
  is_default?: unknown;
  auto_index_enabled?: unknown;
}

export interface SettingsLibrary {
  id: string;
  name: string;
  imageRoot: string;
  workspaceDirectory: string;
  enabled: boolean;
  isDefault: boolean;
  autoIndexEnabled: boolean;
}

export interface LibraryDraft extends SettingsLibrary {
  saving: boolean;
}

export interface NewLibraryDraft {
  name: string;
  imageRoot: string;
  workspaceDirectory: string;
  enabled: boolean;
  saving: boolean;
}

export interface ModelChoiceWire {
  id?: unknown;
  display_name?: unknown;
  protocol?: unknown;
  roles?: unknown;
}

export interface ModelChoice {
  id: string;
  displayName: string;
  protocol: string;
  roles: string[];
}

export interface ModelSettingsWire {
  provider?: unknown;
  embedding_model?: unknown;
  embedding?: unknown;
  auto_tag_primary_model?: unknown;
  tagging_model?: unknown;
  auto_tag_primary?: unknown;
  auto_tag_escalation_model?: unknown;
  escalation_model?: unknown;
  auto_tag_escalation?: unknown;
  embedding_concurrency?: unknown;
  auto_tag_concurrency?: unknown;
  catalog?: unknown;
  restart_required?: unknown;
  restart?: {
    required?: unknown;
    active_jobs?: unknown;
    can_restart_now?: unknown;
    reason?: unknown;
  };
}

export interface SettingsResponse {
  libraries?: SettingsLibraryWire[];
  results_directory?: unknown;
  config_path?: unknown;
  models?: ModelSettingsWire;
  model_settings?: ModelSettingsWire;
  credentials?: {
    configured?: unknown;
    persistent?: unknown;
  };
  credentials_configured?: unknown;
  restart_required?: unknown;
  restart?: {
    required?: unknown;
    active_jobs?: unknown;
    can_restart_now?: unknown;
    reason?: unknown;
  };
}

export interface LibraryUpdate {
  name?: string;
  image_root?: string;
  workspace_directory?: string;
  enabled?: boolean;
  is_default?: boolean;
  auto_index_enabled?: boolean;
  results_directory?: string;
}

export interface LibraryCreate {
  name?: string;
  image_root: string;
  workspace_directory?: string;
  enabled?: boolean;
  results_directory?: string;
}

export interface CredentialsResponse {
  configured?: boolean;
  persistent?: boolean;
  restart_required?: boolean;
}

export interface FolderNameTagSettingsResponse {
  schema_version?: unknown;
  blacklist?: unknown;
  revision?: unknown;
  using_defaults?: unknown;
}

export interface SettingsApi {
  settings(signal?: AbortSignal): Promise<SettingsResponse>;
  addLibrary(body: LibraryCreate, signal?: AbortSignal): Promise<SettingsResponse>;
  updateLibrary(
    libraryId: string,
    body: LibraryUpdate,
    signal?: AbortSignal,
  ): Promise<SettingsResponse>;
  updateModels(body: {
    embedding_model: string;
    auto_tag_primary_model: string;
    auto_tag_escalation_model: string;
    embedding_concurrency: number;
    auto_tag_concurrency: number;
  }, signal?: AbortSignal): Promise<ModelSettingsWire>;
  saveCredentials(apiKey: string, signal?: AbortSignal): Promise<CredentialsResponse>;
  deleteCredentials(signal?: AbortSignal): Promise<CredentialsResponse>;
  folderNameTagSettings?(signal?: AbortSignal): Promise<FolderNameTagSettingsResponse>;
  updateFolderNameTagSettings?(
    blacklist: string[],
    signal?: AbortSignal,
  ): Promise<FolderNameTagSettingsResponse>;
}

export type ToastKind = "info" | "success" | "error";
