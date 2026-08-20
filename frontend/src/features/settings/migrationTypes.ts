import type { SettingsLibrary, ToastKind } from "./types";

export type DataMigrationType = "schema" | "root" | "legacy_config" | "docker_workspace";
export type DataMigrationStatus =
  | "idle"
  | "ready"
  | "blocked"
  | "queued"
  | "running"
  | "cancelling"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "failed_recovered"
  | "needs_attention";

export interface DataMigrationRequest {
  migration_type: DataMigrationType;
  source: string;
  target: string;
  library_id?: string;
  library_name?: string;
  workspace_directory?: string;
  image_root?: string;
  root_id?: string;
  docker_image?: string;
  backup_directory?: string;
  automatic_backup: true;
}

export interface MigrationPrecheckWire {
  status?: unknown;
  source?: unknown;
  target?: unknown;
  collection_documents?: unknown;
  sqlite_entries?: unknown;
  collection_status?: unknown;
  sqlite_status?: unknown;
  fingerprint?: unknown;
  backup_plan?: unknown;
  details?: unknown;
  blockers?: unknown;
  warnings?: unknown;
  api_requests?: unknown;
}

export interface MigrationPreviewWire {
  preview_id?: unknown;
  confirmation_token?: unknown;
  expires_at_epoch?: unknown;
  request?: unknown;
  precheck?: MigrationPrecheckWire;
  dry_run?: unknown;
  vectors_recomputed?: unknown;
  model_api_requests?: unknown;
}

export interface MigrationPreviewResponse {
  preview?: MigrationPreviewWire;
  migration_preview?: MigrationPreviewWire;
}

export interface MigrationBackupPlan {
  status: string;
  destination: string;
  estimatedPayloadBytes: number | null;
  requiredFreeBytes: number | null;
  availableFreeBytes: number | null;
  backupMode: string;
}

export interface MigrationRootOption {
  rootId: string;
  currentPath: string;
}

export interface MigrationPreview {
  id: string;
  confirmationToken: string;
  expiresAtEpoch: number | null;
  status: "ready" | "blocked";
  source: string;
  target: string;
  collectionDocuments: number | null;
  sqliteEntries: number | null;
  collectionStatus: string;
  sqliteStatus: string;
  blockers: string[];
  warnings: string[];
  rootOptions: MigrationRootOption[];
  backup: MigrationBackupPlan;
  dryRun: boolean;
  vectorsRecomputed: number;
  modelApiRequests: number;
}

export interface MigrationJobWire {
  id?: unknown;
  migration_id?: unknown;
  job_id?: unknown;
  status?: unknown;
  stage?: unknown;
  progress?: unknown;
  message?: unknown;
  result?: unknown;
  error?: unknown;
  error_message?: unknown;
  backup?: unknown;
  verification?: unknown;
}

export interface MigrationJobResponse {
  migration?: MigrationJobWire;
  job?: MigrationJobWire;
}

export interface MigrationRecoveryWire {
  pending?: unknown;
  operation_id?: unknown;
  migration_type?: unknown;
  library_id?: unknown;
  library_name?: unknown;
  backup_directory?: unknown;
  stage?: unknown;
  created_at_epoch?: unknown;
  updated_at_epoch?: unknown;
}

export interface MigrationRecoveryResponse {
  recovery?: MigrationRecoveryWire | null;
}

export interface MigrationRecovery {
  operationId: string;
  migrationType: DataMigrationType;
  libraryId: string;
  libraryName: string;
  backupDirectory: string;
  stage: string;
  createdAtEpoch: number | null;
  updatedAtEpoch: number | null;
}

export interface MigrationVerification {
  collectionDocuments: number | null;
  sqliteEntries: number | null;
  sqliteIntegrity: string;
  searchProbe: string;
}

export interface MigrationJob {
  id: string;
  status: DataMigrationStatus;
  stage: string;
  progress: number;
  message: string;
  error: string;
  backupDirectory: string;
  verification: MigrationVerification | null;
}

export interface DataMigrationApi {
  precheck(
    request: DataMigrationRequest,
    signal?: AbortSignal,
  ): Promise<MigrationPreviewResponse | MigrationPreviewWire>;
  start(
    body: {
      request: DataMigrationRequest;
      confirmation_token: string;
      confirmation_phrase: "MIGRATE";
    },
    signal?: AbortSignal,
  ): Promise<MigrationJobResponse | MigrationJobWire>;
  get(migrationId: string, signal?: AbortSignal): Promise<MigrationJobResponse | MigrationJobWire>;
  cancel(
    migrationId: string,
    signal?: AbortSignal,
  ): Promise<MigrationJobResponse | MigrationJobWire>;
  recovery(signal?: AbortSignal): Promise<MigrationRecoveryResponse>;
  restoreRecovery(
    body: {
      operation_id: string;
      confirmation_phrase: "RESTORE";
    },
    signal?: AbortSignal,
  ): Promise<MigrationJobResponse | MigrationJobWire>;
}

export interface DataMigrationEvents {
  onToast?: (title: string, message: string, kind: ToastKind) => void;
  onCompleted?: (job: MigrationJob) => void;
  pollIntervalMs?: number;
}

export interface DataMigrationSectionProps {
  api?: DataMigrationApi;
  libraries?: SettingsLibrary[];
  configPath?: string;
  platform?: string;
  pollIntervalMs?: number;
}
