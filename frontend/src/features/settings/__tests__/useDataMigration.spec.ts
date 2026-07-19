import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DataMigrationApi,
  MigrationPreviewResponse,
} from "../migrationTypes";
import {
  normalizeMigrationJob,
  normalizeMigrationPreview,
  normalizeMigrationRecovery,
  useDataMigration,
} from "../useDataMigration";

function previewPayload(
  overrides: Record<string, unknown> = {},
): MigrationPreviewResponse {
  return {
    preview: {
      preview_id: "preview-1",
      confirmation_token: "token-1",
      expires_at_epoch: 2_000_000_000,
      dry_run: true,
      vectors_recomputed: 0,
      model_api_requests: 0,
      precheck: {
        status: "ready",
        source: "C:\\Zvec\\workspace",
        target: "C:\\Zvec\\workspace",
        collection_documents: 2_074,
        sqlite_entries: 2_074,
        collection_status: "ready",
        sqlite_status: "ok",
        blockers: [],
        warnings: ["将升级 schema"],
        backup_plan: {
          status: "ready",
          destination: "C:\\Backups\\migration-1",
          estimated_payload_bytes: 1024,
          backup_mode: "full",
        },
        api_requests: 0,
        ...overrides,
      },
    },
  };
}

function fakeApi(overrides: Partial<DataMigrationApi> = {}): DataMigrationApi {
  return {
    precheck: vi.fn(async () => previewPayload()),
    start: vi.fn(async () => ({
      migration: {
        id: "migration-1",
        status: "queued",
        stage: "backup",
        progress: 10,
        message: "正在创建备份",
      },
    })),
    get: vi.fn(async (migrationId) => ({
      migration: {
        id: migrationId,
        status: "succeeded",
        progress: 100,
        result: {
          verification: {
            collection_documents: 2_074,
            sqlite_entries: 2_074,
            sqlite_integrity: "ok",
            search_probe: "passed",
          },
        },
      },
    })),
    cancel: vi.fn(async (migrationId) => ({
      migration: { id: migrationId, status: "cancelling", progress: 30 },
    })),
    recovery: vi.fn(async () => ({ recovery: null })),
    restoreRecovery: vi.fn(async () => ({
      migration: {
        id: "migration-recovery-1",
        status: "queued",
        stage: "recovery_queued",
        progress: 0,
      },
    })),
    ...overrides,
  };
}

function mountComposable(api: DataMigrationApi): {
  state: ReturnType<typeof useDataMigration>;
  wrapper: VueWrapper;
} {
  let state!: ReturnType<typeof useDataMigration>;
  const wrapper = mount(defineComponent({
    setup() {
      state = useDataMigration(api, { pollIntervalMs: 60_000 });
      return () => h("div");
    },
  }));
  return { state, wrapper };
}

describe("useDataMigration", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("normalizes preview diagnostics and full-backup estimates", () => {
    const preview = normalizeMigrationPreview(previewPayload());

    expect(preview).toMatchObject({
      id: "preview-1",
      status: "ready",
      collectionDocuments: 2_074,
      sqliteEntries: 2_074,
      dryRun: true,
      vectorsRecomputed: 0,
      modelApiRequests: 0,
      backup: {
        destination: "C:\\Backups\\migration-1",
        estimatedPayloadBytes: 1024,
        backupMode: "full",
      },
    });
  });

  it("normalizes a bounded crash-recovery marker", () => {
    expect(normalizeMigrationRecovery({
      recovery: {
        pending: true,
        operation_id: "migration-old",
        migration_type: "schema",
        library_id: "lib-1",
        library_name: "人物图库",
        backup_directory: "C:\\Backups\\migration-old",
        stage: "migrate",
        created_at_epoch: 1_700_000_000,
        updated_at_epoch: 1_700_000_100,
      },
    })).toMatchObject({
      operationId: "migration-old",
      migrationType: "schema",
      backupDirectory: "C:\\Backups\\migration-old",
      stage: "migrate",
    });
    expect(normalizeMigrationRecovery({ recovery: null })).toBeNull();
  });

  it("fails closed for incomplete previews and unknown job statuses", () => {
    const preview = normalizeMigrationPreview({
      preview: {
        preview_id: "preview-unknown",
        confirmation_token: "token",
        precheck: { status: "future_state" },
      },
    });
    const active = normalizeMigrationJob({
      id: "migration-future",
      status: "pausing_for_lock",
    });
    const attention = normalizeMigrationJob({
      id: "migration-future-error",
      status: "future_failed_state",
      error_message: "需要人工检查",
    });

    expect(preview.status).toBe("blocked");
    expect(preview.blockers).toContain("预检查响应不完整或状态未知，请刷新后重试。");
    expect(active.status).toBe("running");
    expect(attention.status).toBe("needs_attention");
  });

  it("submits a read-only precheck with automatic backup locked on", async () => {
    const api = fakeApi();
    const { state, wrapper } = mountComposable(api);
    state.form.source = "C:\\Zvec\\workspace";
    state.form.target = "C:\\Zvec\\workspace";
    state.form.libraryId = "lib-1";

    expect(await state.runPrecheck()).toBe(true);

    expect(api.precheck).toHaveBeenCalledWith(
      {
        migration_type: "schema",
        source: "C:\\Zvec\\workspace",
        target: "C:\\Zvec\\workspace",
        library_id: "lib-1",
        docker_image: "zvec-image-search:local",
        automatic_backup: true,
      },
      expect.any(AbortSignal),
    );
    expect(state.preview.value?.confirmationToken).toBe("token-1");
    wrapper.unmount();
  });

  it("requires both explicit checkbox and MIGRATE phrase", async () => {
    const api = fakeApi();
    const { state, wrapper } = mountComposable(api);
    state.form.source = "C:\\Zvec\\workspace";
    state.form.target = "C:\\Zvec\\workspace";
    await state.runPrecheck();

    expect(state.canStart.value).toBe(false);
    state.confirmationChecked.value = true;
    state.confirmationPhrase.value = "yes";
    expect(state.canStart.value).toBe(false);
    expect(await state.startMigration()).toBe(false);
    state.confirmationPhrase.value = "MIGRATE";

    expect(await state.startMigration()).toBe(true);
    expect(api.start).toHaveBeenCalledWith({
      request: expect.objectContaining({ automatic_backup: true }),
      confirmation_token: "token-1",
      confirmation_phrase: "MIGRATE",
    }, expect.any(AbortSignal));
    wrapper.unmount();
  });

  it("refreshes terminal verification and stops active polling", async () => {
    const api = fakeApi();
    const { state, wrapper } = mountComposable(api);
    state.form.source = "C:\\Zvec\\workspace";
    state.form.target = "C:\\Zvec\\workspace";
    await state.runPrecheck();
    state.confirmationChecked.value = true;
    state.confirmationPhrase.value = "MIGRATE";
    await state.startMigration();

    expect(state.active.value).toBe(true);
    expect(await state.refreshJob()).toBe(true);
    expect(state.job.value).toMatchObject({
      status: "succeeded",
      verification: {
        collectionDocuments: 2_074,
        sqliteEntries: 2_074,
        sqliteIntegrity: "ok",
        searchProbe: "passed",
      },
    });
    expect(state.active.value).toBe(false);
    wrapper.unmount();
  });

  it("restores cancellation controls after a failed cancel request", async () => {
    const api = fakeApi({
      cancel: vi.fn(async () => Promise.reject(new Error("后端暂时不可用"))),
    });
    const { state, wrapper } = mountComposable(api);
    state.job.value = normalizeMigrationJob({ id: "migration-1", status: "running" });

    const pending = state.cancelMigration();
    expect(state.cancelling.value).toBe(true);
    await expect(pending).resolves.toBe(false);
    expect(state.cancelling.value).toBe(false);
    expect(state.lastError.value).toBe("后端暂时不可用");
    wrapper.unmount();
  });

  it("requires explicit RESTORE confirmation and recovers button state", async () => {
    const recovery = {
      recovery: {
        pending: true,
        operation_id: "migration-old",
        migration_type: "root",
        library_id: "lib-1",
        library_name: "人物图库",
        backup_directory: "C:\\Backups\\migration-old",
        stage: "migrate",
        updated_at_epoch: 1_700_000_100,
      },
    };
    const restoreRecovery = vi.fn()
      .mockRejectedValueOnce(new Error("恢复服务暂时不可用"))
      .mockResolvedValueOnce({
        migration: {
          id: "recovery-job",
          status: "queued",
          stage: "recovery_queued",
          progress: 0,
        },
      });
    const api = fakeApi({
      recovery: vi.fn(async () => recovery),
      restoreRecovery,
    });
    const { state, wrapper } = mountComposable(api);
    await flushPromises();

    expect(state.recovery.value?.operationId).toBe("migration-old");
    expect(state.canRestore.value).toBe(false);
    state.recoveryConfirmationChecked.value = true;
    state.recoveryConfirmationPhrase.value = "restore";
    expect(state.canRestore.value).toBe(false);
    state.recoveryConfirmationPhrase.value = "RESTORE";

    expect(await state.restoreRecovery()).toBe(false);
    expect(state.restoringRecovery.value).toBe(false);
    expect(state.lastError.value).toBe("恢复服务暂时不可用");
    expect(await state.restoreRecovery()).toBe(true);
    expect(restoreRecovery).toHaveBeenLastCalledWith({
      operation_id: "migration-old",
      confirmation_phrase: "RESTORE",
    }, expect.any(AbortSignal));
    expect(state.job.value?.status).toBe("queued");
    wrapper.unmount();
  });

  it("keeps a blocked preview visible but never enables execution", async () => {
    const api = fakeApi({
      precheck: vi.fn(async () => previewPayload({
        status: "blocked",
        blockers: ["Collection 与 SQLite 数量不一致"],
      })),
    });
    const { state, wrapper } = mountComposable(api);
    state.form.source = "C:\\Zvec\\workspace";
    state.form.target = "C:\\Zvec\\workspace";

    expect(await state.runPrecheck()).toBe(true);
    state.confirmationChecked.value = true;
    state.confirmationPhrase.value = "MIGRATE";
    expect(state.preview.value?.status).toBe("blocked");
    expect(state.canStart.value).toBe(false);
    expect(api.start).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("clears all busy flags when precheck and start requests fail", async () => {
    const api = fakeApi({
      precheck: vi.fn(async () => Promise.reject(new Error("检查失败"))),
    });
    const { state, wrapper } = mountComposable(api);
    state.form.source = "C:\\Zvec\\workspace";
    state.form.target = "C:\\Zvec\\workspace";

    const pending = state.runPrecheck();
    expect(state.prechecking.value).toBe(true);
    await expect(pending).resolves.toBe(false);
    expect(state.prechecking.value).toBe(false);
    expect(state.lastError.value).toBe("检查失败");
    await flushPromises();
    wrapper.unmount();
  });
});
