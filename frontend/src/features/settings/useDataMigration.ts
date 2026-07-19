import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from "vue";

import { dataMigrationApi } from "./migrationApi";
import type {
  DataMigrationApi,
  DataMigrationEvents,
  DataMigrationRequest,
  DataMigrationStatus,
  DataMigrationType,
  MigrationBackupPlan,
  MigrationJob,
  MigrationJobResponse,
  MigrationJobWire,
  MigrationPreview,
  MigrationPreviewResponse,
  MigrationPreviewWire,
  MigrationRecovery,
  MigrationRecoveryResponse,
  MigrationRecoveryWire,
  MigrationRootOption,
  MigrationVerification,
} from "./migrationTypes";
import { isAbsoluteWindowsPath } from "./useSettings";

const ACTIVE_STATUSES = new Set<DataMigrationStatus>(["queued", "running", "cancelling"]);
const MIGRATION_TYPES = new Set<DataMigrationType>([
  "schema",
  "root",
  "legacy_config",
  "docker_workspace",
]);
const KNOWN_STATUSES = new Set<DataMigrationStatus>([
  "idle",
  "ready",
  "blocked",
  "queued",
  "running",
  "cancelling",
  "cancelled",
  "succeeded",
  "failed",
  "failed_recovered",
  "needs_attention",
]);

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integer(value: unknown, fallback = 0): number {
  const parsed = numberOrNull(value);
  return parsed === null ? fallback : Math.trunc(parsed);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string").map((item) => item.trim()).filter(Boolean)
    : [];
}

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function previewWire(payload: MigrationPreviewResponse | MigrationPreviewWire): MigrationPreviewWire {
  const response = payload as MigrationPreviewResponse;
  return response.preview ?? response.migration_preview ?? payload as MigrationPreviewWire;
}

function jobWire(payload: MigrationJobResponse | MigrationJobWire): MigrationJobWire {
  const response = payload as MigrationJobResponse;
  return response.migration ?? response.job ?? payload as MigrationJobWire;
}

function normalizeBackup(value: unknown): MigrationBackupPlan {
  const raw = object(value);
  return {
    status: text(raw.status),
    destination: text(raw.destination),
    estimatedPayloadBytes: numberOrNull(raw.estimated_payload_bytes),
    requiredFreeBytes: numberOrNull(raw.required_free_bytes),
    availableFreeBytes: numberOrNull(raw.available_free_bytes),
    backupMode: text(raw.backup_mode),
  };
}

function normalizeRootOptions(value: unknown): MigrationRootOption[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => object(item))
    .map((item) => ({
      rootId: text(item.root_id),
      currentPath: text(item.current_path),
    }))
    .filter((item) => item.rootId && item.currentPath);
}

export function normalizeMigrationPreview(
  payload: MigrationPreviewResponse | MigrationPreviewWire,
): MigrationPreview {
  const raw = previewWire(payload);
  const precheck = raw.precheck ?? {};
  const details = object(precheck.details);
  const id = text(raw.preview_id);
  const confirmationToken = text(raw.confirmation_token);
  const rawStatus = text(precheck.status);
  const status = rawStatus === "ready" && id && confirmationToken ? "ready" : "blocked";
  const blockers = stringArray(precheck.blockers);
  if (status === "blocked" && !blockers.length && rawStatus !== "blocked") {
    blockers.push("预检查响应不完整或状态未知，请刷新后重试。");
  }
  return {
    id,
    confirmationToken,
    expiresAtEpoch: numberOrNull(raw.expires_at_epoch),
    status,
    source: text(precheck.source),
    target: text(precheck.target),
    collectionDocuments: numberOrNull(precheck.collection_documents),
    sqliteEntries: numberOrNull(precheck.sqlite_entries),
    collectionStatus: text(precheck.collection_status),
    sqliteStatus: text(precheck.sqlite_status),
    blockers,
    warnings: stringArray(precheck.warnings),
    rootOptions: normalizeRootOptions(details.root_options),
    backup: normalizeBackup(precheck.backup_plan),
    dryRun: raw.dry_run !== false,
    vectorsRecomputed: integer(raw.vectors_recomputed),
    modelApiRequests: integer(raw.model_api_requests),
  };
}

function normalizeVerification(value: unknown): MigrationVerification | null {
  const raw = object(value);
  if (!Object.keys(raw).length) return null;
  return {
    collectionDocuments: numberOrNull(raw.collection_documents),
    sqliteEntries: numberOrNull(raw.sqlite_entries),
    sqliteIntegrity: text(raw.sqlite_integrity),
    searchProbe: text(raw.search_probe),
  };
}

export function normalizeMigrationJob(
  payload: MigrationJobResponse | MigrationJobWire,
): MigrationJob {
  const raw = jobWire(payload);
  const result = object(raw.result);
  const error = object(raw.error);
  const backup = object(raw.backup ?? result.backup);
  const statusText = text(raw.status) as DataMigrationStatus;
  const hasError = Boolean(text(raw.error_message ?? error.message ?? result.error));
  const status: DataMigrationStatus = KNOWN_STATUSES.has(statusText)
    ? statusText
    : hasError
      ? "needs_attention"
      : "running";
  return {
    id: text(raw.id ?? raw.migration_id ?? raw.job_id),
    status,
    stage: text(raw.stage ?? result.stage),
    progress: Math.max(0, Math.min(100, integer(raw.progress ?? result.progress))),
    message: text(raw.message ?? result.message),
    error: text(raw.error_message ?? error.message ?? result.error),
    backupDirectory: text(backup.destination),
    verification: normalizeVerification(raw.verification ?? result.verification),
  };
}

export function normalizeMigrationRecovery(
  payload: MigrationRecoveryResponse,
): MigrationRecovery | null {
  const raw = payload.recovery;
  if (!raw) return null;
  const wire = raw as MigrationRecoveryWire;
  const operationId = text(wire.operation_id);
  const migrationType = text(wire.migration_type) as DataMigrationType;
  if (!operationId || !MIGRATION_TYPES.has(migrationType)) {
    throw new Error("数据迁移恢复记录不完整或版本不受支持。");
  }
  return {
    operationId,
    migrationType,
    libraryId: text(wire.library_id),
    libraryName: text(wire.library_name),
    backupDirectory: text(wire.backup_directory),
    stage: text(wire.stage),
    createdAtEpoch: numberOrNull(wire.created_at_epoch),
    updatedAtEpoch: numberOrNull(wire.updated_at_epoch),
  };
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useDataMigration(
  api: DataMigrationApi = dataMigrationApi,
  events: DataMigrationEvents = {},
) {
  const form = reactive({
    migrationType: "schema" as DataMigrationType,
    source: "",
    target: "",
    libraryId: "",
    libraryName: "",
    workspaceDirectory: "",
    imageRoot: "",
    rootId: "",
    dockerImage: "zvec-image-search:local",
    backupDirectory: "",
  });
  const preview = shallowRef<MigrationPreview | null>(null);
  const job = shallowRef<MigrationJob | null>(null);
  const recovery = shallowRef<MigrationRecovery | null>(null);
  const prechecking = ref(false);
  const starting = ref(false);
  const cancelling = ref(false);
  const loadingRecovery = ref(false);
  const restoringRecovery = ref(false);
  const confirmationChecked = ref(false);
  const confirmationPhrase = ref("");
  const recoveryConfirmationChecked = ref(false);
  const recoveryConfirmationPhrase = ref("");
  const lastError = ref("");
  let previewController: AbortController | null = null;
  let jobController: AbortController | null = null;
  let cancelController: AbortController | null = null;
  let recoveryController: AbortController | null = null;
  let restoreController: AbortController | null = null;
  let pollTimer: number | null = null;

  const active = computed(() => Boolean(job.value && ACTIVE_STATUSES.has(job.value.status)));
  const canStart = computed(() => Boolean(
    preview.value?.status === "ready"
      && preview.value.confirmationToken
      && confirmationChecked.value
      && confirmationPhrase.value === "MIGRATE"
      && !starting.value
      && !active.value,
  ));
  const canRestore = computed(() => Boolean(
    recovery.value?.operationId
      && recoveryConfirmationChecked.value
      && recoveryConfirmationPhrase.value === "RESTORE"
      && !restoringRecovery.value
      && !active.value,
  ));

  function notify(title: string, message: string, kind: "info" | "success" | "error"): void {
    events.onToast?.(title, message, kind);
  }

  function clearPoll(): void {
    if (pollTimer === null) return;
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(): void {
    clearPoll();
    if (!job.value || !ACTIVE_STATUSES.has(job.value.status)) return;
    pollTimer = window.setTimeout(() => {
      pollTimer = null;
      void refreshJob(true);
    }, Math.max(500, events.pollIntervalMs ?? 1_500));
  }

  function requestBody(): DataMigrationRequest {
    return {
      migration_type: form.migrationType,
      source: form.source.trim(),
      target: form.target.trim(),
      ...(form.libraryId.trim() ? { library_id: form.libraryId.trim() } : {}),
      ...(form.libraryName.trim() ? { library_name: form.libraryName.trim() } : {}),
      ...(form.workspaceDirectory.trim() ? { workspace_directory: form.workspaceDirectory.trim() } : {}),
      ...(form.imageRoot.trim() ? { image_root: form.imageRoot.trim() } : {}),
      ...(form.rootId.trim() ? { root_id: form.rootId.trim() } : {}),
      ...(form.dockerImage.trim() ? { docker_image: form.dockerImage.trim() } : {}),
      ...(form.backupDirectory.trim() ? { backup_directory: form.backupDirectory.trim() } : {}),
      automatic_backup: true,
    };
  }

  function validate(): string {
    if (!form.source.trim()) return "请输入迁移来源。";
    if (!form.target.trim()) return "请输入迁移目标。";
    if (form.migrationType !== "docker_workspace" && !isAbsoluteWindowsPath(form.source)) {
      return "迁移来源必须是 Windows 绝对路径。";
    }
    if (!isAbsoluteWindowsPath(form.target)) return "迁移目标必须是 Windows 绝对路径。";
    if (form.workspaceDirectory && !isAbsoluteWindowsPath(form.workspaceDirectory)) {
      return "Workspace 必须是 Windows 绝对路径。";
    }
    if (form.imageRoot && !isAbsoluteWindowsPath(form.imageRoot)) {
      return "图片目录必须是 Windows 绝对路径。";
    }
    if (form.backupDirectory && !isAbsoluteWindowsPath(form.backupDirectory)) {
      return "备份目录必须是 Windows 绝对路径。";
    }
    if (form.migrationType === "schema" && form.source.toLocaleLowerCase() !== form.target.toLocaleLowerCase()) {
      return "Schema 迁移的来源和目标必须是同一个 Workspace。";
    }
    if (form.migrationType === "root" && !form.workspaceDirectory) {
      return "Root 重绑需要 Workspace；Root ID 可由预检查自动选择。";
    }
    if (form.migrationType === "docker_workspace" && !form.dockerImage.trim()) {
      return "Docker named volume 导出需要原镜像名称。";
    }
    return "";
  }

  function invalidatePreview(): void {
    preview.value = null;
    confirmationChecked.value = false;
    confirmationPhrase.value = "";
  }

  async function runPrecheck(): Promise<boolean> {
    const validation = validate();
    if (validation) {
      lastError.value = validation;
      notify("迁移参数不完整", validation, "error");
      return false;
    }
    previewController?.abort();
    previewController = new AbortController();
    prechecking.value = true;
    invalidatePreview();
    try {
      const payload = await api.precheck(requestBody(), previewController.signal);
      preview.value = normalizeMigrationPreview(payload);
      lastError.value = "";
      if (preview.value.status === "blocked") {
        notify("预检查发现阻塞项", preview.value.blockers.join("；"), "error");
      } else {
        notify("预检查完成", "迁移尚未执行，可以核对备份和文档数。", "success");
      }
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      notify("无法完成迁移预检查", lastError.value, "error");
      return false;
    } finally {
      prechecking.value = false;
    }
  }

  async function startMigration(): Promise<boolean> {
    if (!canStart.value || !preview.value) return false;
    starting.value = true;
    jobController?.abort();
    jobController = new AbortController();
    try {
      const payload = await api.start({
        request: requestBody(),
        confirmation_token: preview.value.confirmationToken,
        confirmation_phrase: "MIGRATE",
      }, jobController.signal);
      job.value = normalizeMigrationJob(payload);
      lastError.value = "";
      notify("迁移任务已提交", "任务将在后台备份、迁移并验证。", "info");
      schedulePoll();
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      notify("无法启动迁移", lastError.value, "error");
      return false;
    } finally {
      starting.value = false;
    }
  }

  async function refreshJob(quiet = false): Promise<boolean> {
    if (!job.value?.id) return false;
    jobController?.abort();
    jobController = new AbortController();
    try {
      const payload = await api.get(job.value.id, jobController.signal);
      job.value = normalizeMigrationJob(payload);
      lastError.value = "";
      if (job.value.status === "succeeded") {
        if (job.value.stage === "recovery_complete") {
          notify("完整备份已恢复", "迁移前数据已恢复，软件将重新检查本地后端。", "success");
        } else {
          notify("数据迁移完成", "文档数、SQLite 和本地搜索均已验证。", "success");
        }
        events.onCompleted?.(job.value);
      } else if (job.value.status === "failed_recovered") {
        notify("迁移已回滚", "迁移未完成，但完整备份已经安全恢复。", "info");
      } else if (["failed", "needs_attention"].includes(job.value.status)) {
        notify("数据迁移失败", job.value.error || "请查看恢复信息。", "error");
      }
      if (["succeeded", "failed", "failed_recovered", "cancelled"].includes(job.value.status)) {
        await loadRecovery(true);
      }
      schedulePoll();
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      if (!quiet) notify("无法读取迁移进度", lastError.value, "error");
      schedulePoll();
      return false;
    }
  }

  async function cancelMigration(): Promise<boolean> {
    if (!job.value?.id || !active.value || cancelling.value) return false;
    cancelling.value = true;
    cancelController?.abort();
    cancelController = new AbortController();
    try {
      const payload = await api.cancel(job.value.id, cancelController.signal);
      job.value = normalizeMigrationJob(payload);
      lastError.value = "";
      schedulePoll();
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      notify("无法取消迁移", lastError.value, "error");
      return false;
    } finally {
      cancelling.value = false;
    }
  }

  async function loadRecovery(quiet = false): Promise<boolean> {
    recoveryController?.abort();
    recoveryController = new AbortController();
    loadingRecovery.value = true;
    try {
      recovery.value = normalizeMigrationRecovery(
        await api.recovery(recoveryController.signal),
      );
      recoveryConfirmationChecked.value = false;
      recoveryConfirmationPhrase.value = "";
      if (!quiet) lastError.value = "";
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      if (!quiet) notify("无法读取迁移恢复状态", lastError.value, "error");
      return false;
    } finally {
      loadingRecovery.value = false;
    }
  }

  async function restoreRecovery(): Promise<boolean> {
    if (!canRestore.value || !recovery.value) return false;
    restoringRecovery.value = true;
    restoreController?.abort();
    restoreController = new AbortController();
    try {
      const payload = await api.restoreRecovery({
        operation_id: recovery.value.operationId,
        confirmation_phrase: "RESTORE",
      }, restoreController.signal);
      job.value = normalizeMigrationJob(payload);
      lastError.value = "";
      notify("恢复任务已提交", "软件将在后台校验并恢复完整备份。", "info");
      schedulePoll();
      return true;
    } catch (error) {
      if (isAbort(error)) return false;
      lastError.value = errorMessage(error);
      notify("无法启动备份恢复", lastError.value, "error");
      return false;
    } finally {
      restoringRecovery.value = false;
    }
  }

  onMounted(() => {
    void loadRecovery(true);
  });

  onBeforeUnmount(() => {
    clearPoll();
    previewController?.abort();
    jobController?.abort();
    cancelController?.abort();
    recoveryController?.abort();
    restoreController?.abort();
  });

  return {
    form,
    preview,
    job,
    recovery,
    prechecking,
    starting,
    cancelling,
    loadingRecovery,
    restoringRecovery,
    confirmationChecked,
    confirmationPhrase,
    recoveryConfirmationChecked,
    recoveryConfirmationPhrase,
    lastError,
    active,
    canStart,
    canRestore,
    requestBody,
    validate,
    invalidatePreview,
    runPrecheck,
    startMigration,
    refreshJob,
    cancelMigration,
    loadRecovery,
    restoreRecovery,
  };
}
