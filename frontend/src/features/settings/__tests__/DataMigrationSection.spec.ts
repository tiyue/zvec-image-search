import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import DataMigrationSection from "../DataMigrationSection.vue";
import type { DataMigrationApi } from "../migrationTypes";
import type { SettingsLibrary } from "../types";

const libraries: SettingsLibrary[] = [
  {
    id: "lib-1",
    name: "人物图库",
    imageRoot: "D:\\Pictures",
    workspaceDirectory: "D:\\Zvec\\workspace",
    enabled: true,
    isDefault: true,
  },
  {
    id: "lib-2",
    name: "Cosplay 图库",
    imageRoot: "E:\\Cosplay",
    workspaceDirectory: "E:\\Zvec\\workspace",
    enabled: true,
    isDefault: false,
  },
];

function fakeApi(overrides: Partial<DataMigrationApi> = {}): DataMigrationApi {
  return {
    precheck: vi.fn(async () => ({
      preview: {
        preview_id: "preview-1",
        confirmation_token: "token-1",
        dry_run: true,
        vectors_recomputed: 0,
        model_api_requests: 0,
        precheck: {
          status: "ready",
          source: "D:\\Zvec\\workspace",
          target: "D:\\Zvec\\workspace",
          collection_documents: 2_074,
          sqlite_entries: 2_074,
          collection_status: "ready",
          sqlite_status: "ok",
          blockers: [],
          warnings: [],
          backup_plan: {
            destination: "D:\\Zvec\\migration-backups\\migration-1",
            estimated_payload_bytes: 1_073_741_824,
            backup_mode: "full",
          },
        },
      },
    })),
    start: vi.fn(async () => ({
      migration: {
        id: "migration-1",
        status: "running",
        stage: "backup",
        progress: 20,
        message: "正在创建完整备份",
      },
    })),
    get: vi.fn(async (id) => ({ migration: { id, status: "running", progress: 20 } })),
    cancel: vi.fn(async (id) => ({ migration: { id, status: "cancelling", progress: 20 } })),
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

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const result = wrapper.findAll("button").find((item) => item.text().includes(label));
  if (!result) throw new Error(`button not found: ${label}`);
  return result;
}

describe("DataMigrationSection", () => {
  it("shows a Windows-only explanation without rendering unsafe controls elsewhere", () => {
    const wrapper = mount(DataMigrationSection, {
      props: { platform: "Linux", libraries },
    });

    expect(wrapper.text()).toContain("只支持 Windows");
    expect(wrapper.find('select[name="migration_type"]').exists()).toBe(false);
  });

  it("shows crash recovery before allowing another migration", async () => {
    const restoreRecovery = vi.fn(async () => ({
      migration: {
        id: "recovery-job",
        status: "queued",
        stage: "recovery_queued",
        progress: 0,
      },
    }));
    const api = fakeApi({
      recovery: vi.fn(async () => ({
        recovery: {
          pending: true,
          operation_id: "migration-old",
          migration_type: "schema",
          library_id: "lib-1",
          library_name: "人物图库",
          backup_directory: "C:\\Backups\\migration-old",
          stage: "migrate",
          updated_at_epoch: 1_700_000_100,
        },
      })),
      restoreRecovery,
    });
    const wrapper = mount(DataMigrationSection, {
      props: { api, libraries, pollIntervalMs: 60_000 },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("检测到未完整结束的数据迁移");
    expect(wrapper.text()).toContain("C:\\Backups\\migration-old");
    expect(button(wrapper, "预检查").attributes("disabled")).toBeDefined();
    expect(button(wrapper, "从完整备份恢复").attributes("disabled")).toBeDefined();

    await wrapper.get('.migration-recovery input[type="checkbox"]').setValue(true);
    await wrapper.get('input[name="migration_recovery_confirmation"]').setValue("RESTORE");
    await button(wrapper, "从完整备份恢复").trigger("click");
    await flushPromises();

    expect(restoreRecovery).toHaveBeenCalledWith({
      operation_id: "migration-old",
      confirmation_phrase: "RESTORE",
    }, expect.any(AbortSignal));
    wrapper.unmount();
  });

  it("offers all four migration types and fills the selected library paths", async () => {
    const wrapper = mount(DataMigrationSection, {
      props: { api: fakeApi(), libraries },
    });

    const type = wrapper.get('select[name="migration_type"]');
    expect(type.findAll("option").map((option) => option.attributes("value"))).toEqual([
      "schema",
      "root",
      "legacy_config",
      "docker_workspace",
    ]);
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    expect((wrapper.get('input[name="migration_source"]').element as HTMLInputElement).value)
      .toBe("D:\\Zvec\\workspace");
    expect((wrapper.get('input[name="migration_target"]').element as HTMLInputElement).value)
      .toBe("D:\\Zvec\\workspace");
  });

  it("resets incompatible paths when switching types and libraries", async () => {
    const wrapper = mount(DataMigrationSection, {
      props: { api: fakeApi(), libraries },
    });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get('select[name="migration_type"]').setValue("docker_workspace");

    expect((wrapper.get('input[name="migration_source"]').element as HTMLInputElement).value)
      .toBe("");
    expect((wrapper.get('input[name="migration_target"]').element as HTMLInputElement).value)
      .toBe("");
    await wrapper.get('input[name="migration_source"]').setValue("old-volume");
    await wrapper.get('select[name="migration_library"]').setValue("lib-2");
    await wrapper.get('select[name="migration_type"]').setValue("schema");
    await wrapper.get('select[name="migration_type"]').setValue("docker_workspace");

    expect((wrapper.get('input[name="migration_source"]').element as HTMLInputElement).value)
      .toBe("");
    expect((wrapper.get('input[name="migration_image_root"]').element as HTMLInputElement).value)
      .toBe("E:\\Cosplay");
  });

  it("locks legacy config output to the active config path", async () => {
    const wrapper = mount(DataMigrationSection, {
      props: {
        api: fakeApi(),
        libraries,
        configPath: "C:\\Users\\tester\\AppData\\Local\\zvec-image-search\\config.json",
      },
    });
    await wrapper.get('select[name="migration_type"]').setValue("legacy_config");
    const target = wrapper.get('input[name="migration_target"]');

    expect((target.element as HTMLInputElement).value).toContain("config.json");
    expect(target.attributes("readonly")).toBeDefined();
  });

  it("renders dry-run counts, backup size, and zero model requests", async () => {
    const api = fakeApi();
    const wrapper = mount(DataMigrationSection, {
      props: { api, libraries },
    });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get(".migration-form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("可以迁移");
    expect(wrapper.text()).toContain("2,074");
    expect(wrapper.text()).toContain("1.0 GB");
    expect(wrapper.text()).toContain("模型请求");
    expect(wrapper.text()).toContain("Dry-run");
    expect(api.precheck).toHaveBeenCalledTimes(1);
  });

  it("keeps the start button disabled until both confirmations are complete", async () => {
    const api = fakeApi();
    const wrapper = mount(DataMigrationSection, {
      props: { api, libraries, pollIntervalMs: 60_000 },
    });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get(".migration-form").trigger("submit");
    await flushPromises();
    const start = button(wrapper, "开始后台迁移");

    expect(start.attributes("disabled")).toBeDefined();
    await wrapper.get('.confirmation-check input[type="checkbox"]').setValue(true);
    await wrapper.get('input[name="migration_confirmation"]').setValue("MIGRATE");
    expect(start.attributes("disabled")).toBeUndefined();
    await start.trigger("click");
    await flushPromises();

    expect(api.start).toHaveBeenCalledWith({
      request: expect.objectContaining({
        migration_type: "schema",
        automatic_backup: true,
      }),
      confirmation_token: "token-1",
      confirmation_phrase: "MIGRATE",
    }, expect.any(AbortSignal));
    expect(wrapper.text()).toContain("正在创建完整备份");
    wrapper.unmount();
  });

  it("shows root-specific fields and preserves safe cancellation", async () => {
    const api = fakeApi();
    const wrapper = mount(DataMigrationSection, {
      props: { api, libraries, pollIntervalMs: 60_000 },
    });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get('select[name="migration_type"]').setValue("root");

    expect(wrapper.find('input[name="migration_root_id"]').exists()).toBe(true);
    expect(wrapper.find('input[name="migration_workspace"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("Root ID（可选）");
    expect(wrapper.text()).toContain("预检查会按原路径自动选择");
  });

  it("uses the desktop directory chooser for target and backup paths", async () => {
    const selectDirectory = vi.fn()
      .mockResolvedValueOnce({ ok: true, path: "F:\\Migrated" })
      .mockResolvedValueOnce({ ok: true, path: "F:\\Backups" });
    Object.defineProperty(window, "pywebview", {
      configurable: true,
      value: { api: { select_directory: selectDirectory } },
    });
    const wrapper = mount(DataMigrationSection, { props: { api: fakeApi(), libraries } });
    const targetField = wrapper.get('input[name="migration_target"]').element as HTMLInputElement;
    const backupField = wrapper.get('input[name="migration_backup_directory"]').element as HTMLInputElement;
    const targetButton = wrapper.get('input[name="migration_target"] + .path-button');
    const backupButton = wrapper.get('input[name="migration_backup_directory"] + .path-button');

    await targetButton.trigger("click");
    await flushPromises();
    await backupButton.trigger("click");
    await flushPromises();

    expect(targetField.value).toBe("F:\\Migrated");
    expect(backupField.value).toBe("F:\\Backups");
    expect(selectDirectory).toHaveBeenCalledTimes(2);
    wrapper.unmount();
    Reflect.deleteProperty(window, "pywebview");
  });

  it("shows root choices returned by a blocked multi-root precheck", async () => {
    const api = fakeApi({
      precheck: vi.fn(async () => ({
        preview: {
          preview_id: "root-preview",
          confirmation_token: "root-token",
          precheck: {
            status: "blocked",
            source: "D:\\Pictures",
            target: "F:\\Pictures",
            blockers: ["Workspace contains multiple roots."],
            warnings: [],
            details: {
              root_options: [
                { root_id: "root-a", current_path: "D:\\Pictures" },
                { root_id: "root-b", current_path: "E:\\Archive" },
              ],
            },
            backup_plan: {},
          },
        },
      })),
    });
    const wrapper = mount(DataMigrationSection, { props: { api, libraries } });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get('select[name="migration_type"]').setValue("root");
    await wrapper.get('input[name="migration_target"]').setValue("F:\\Pictures");
    await wrapper.get(".migration-form").trigger("submit");
    await flushPromises();

    const rootSelect = wrapper.get('select[name="migration_root_id"]');
    expect(rootSelect.text()).toContain("root-a · D:\\Pictures");
    expect(rootSelect.text()).toContain("root-b · E:\\Archive");
  });

  it("displays blockers and never renders a migration confirmation form", async () => {
    const api = fakeApi({
      precheck: vi.fn(async () => ({
        preview: {
          preview_id: "blocked",
          confirmation_token: "token-blocked",
          precheck: {
            status: "blocked",
            source: "D:\\Zvec\\workspace",
            target: "D:\\Zvec\\workspace",
            blockers: ["Collection 与 SQLite 文档数不一致"],
            warnings: [],
            backup_plan: {},
          },
        },
      })),
    });
    const wrapper = mount(DataMigrationSection, { props: { api, libraries } });
    await wrapper.get('select[name="migration_library"]').setValue("lib-1");
    await wrapper.get(".migration-form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("暂时无法迁移");
    expect(wrapper.text()).toContain("文档数不一致");
    expect(wrapper.find('input[name="migration_confirmation"]').exists()).toBe(false);
  });
});
