import { requestJson } from "../../api/gateway";
import type {
  DataMigrationApi,
  MigrationJobResponse,
  MigrationPreviewResponse,
  MigrationRecoveryResponse,
} from "./migrationTypes";

export const dataMigrationApi: DataMigrationApi = {
  precheck: (request, signal) =>
    requestJson<MigrationPreviewResponse>("api/data-migrations/precheck", {
      method: "POST",
      body: { request, dry_run: true },
      signal,
      timeoutMs: 10 * 60_000,
    }),
  start: (body, signal) =>
    requestJson<MigrationJobResponse>("api/data-migrations", {
      method: "POST",
      body,
      signal,
    }),
  get: (migrationId, signal) =>
    requestJson<MigrationJobResponse>(
      `api/data-migrations/${encodeURIComponent(migrationId)}`,
      { signal },
    ),
  cancel: (migrationId, signal) =>
    requestJson<MigrationJobResponse>(
      `api/data-migrations/${encodeURIComponent(migrationId)}`,
      { method: "DELETE", signal },
    ),
  recovery: (signal) =>
    requestJson<MigrationRecoveryResponse>("api/data-migrations/recovery", {
      signal,
    }),
  restoreRecovery: (body, signal) =>
    requestJson<MigrationJobResponse>("api/data-migrations/recovery/restore", {
      method: "POST",
      body,
      signal,
    }),
};
