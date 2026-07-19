<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  DataMigrationSectionProps,
  DataMigrationType,
} from "./migrationTypes";
import { useDataMigration } from "./useDataMigration";

const props = withDefaults(defineProps<DataMigrationSectionProps>(), {
  libraries: () => [],
  configPath: "",
  platform: "Windows",
  pollIntervalMs: 1_500,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
  completed: [migrationId: string];
}>();

const migration = useDataMigration(props.api, {
  pollIntervalMs: props.pollIntervalMs,
  onToast: (title, message, kind) => emit("toast", title, message, kind),
  onCompleted: (job) => emit("completed", job.id),
});

const supported = computed(() => props.platform.toLocaleLowerCase() === "windows");
const selectedLibrary = computed(() =>
  props.libraries.find((library) => library.id === migration.form.libraryId),
);
const choosingPath = ref("");

interface TypeDraft {
  source: string;
  target: string;
  workspaceDirectory: string;
  imageRoot: string;
  rootId: string;
  dockerImage: string;
}

const typeDrafts = new Map<DataMigrationType, TypeDraft>();

const migrationTypeCopy: Record<DataMigrationType, { title: string; description: string }> = {
  schema: {
    title: "Collection Schema",
    description: "升级旧 Collection 结构，保留全部已有向量。",
  },
  root: {
    title: "图片 Root 重绑",
    description: "图片移动后更新 Root 路径，不重建索引。",
  },
  legacy_config: {
    title: "旧配置迁移",
    description: "把 Docker 时代的 bind 配置转换为 Windows 原生配置。",
  },
  docker_workspace: {
    title: "Docker Workspace",
    description: "一次性导出 named volume；仅此步骤需要 Docker CLI。",
  },
};

function formatCount(value: number | null): string {
  return value === null ? "无法读取" : value.toLocaleString("zh-CN");
}

function formatBytes(value: number | null): string {
  if (value === null) return "待计算";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`;
}

function formatTime(value: number | null): string {
  if (value === null) return "未知";
  const date = new Date(value * 1_000);
  return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN");
}

function currentTypeDraft(): TypeDraft {
  return {
    source: migration.form.source,
    target: migration.form.target,
    workspaceDirectory: migration.form.workspaceDirectory,
    imageRoot: migration.form.imageRoot,
    rootId: migration.form.rootId,
    dockerImage: migration.form.dockerImage,
  };
}

function applyTypeDefaults(type: DataMigrationType): void {
  const library = selectedLibrary.value;
  migration.form.source = "";
  migration.form.target = "";
  migration.form.workspaceDirectory = "";
  migration.form.imageRoot = library?.imageRoot ?? "";
  migration.form.rootId = "";
  migration.form.dockerImage = "zvec-image-search:local";
  if (library) migration.form.libraryName = library.name;
  if (type === "schema" && library) {
    migration.form.source = library.workspaceDirectory;
    migration.form.target = library.workspaceDirectory;
    migration.form.workspaceDirectory = library.workspaceDirectory;
  } else if (type === "root" && library) {
    migration.form.source = library.imageRoot;
    migration.form.workspaceDirectory = library.workspaceDirectory;
  } else if (type === "legacy_config") {
    migration.form.source = props.configPath;
    migration.form.target = props.configPath;
  }
}

function applyLibraryDefaults(): void {
  typeDrafts.clear();
  applyTypeDefaults(migration.form.migrationType);
  migration.invalidatePreview();
}

async function chooseDirectory(
  field: "source" | "target" | "workspaceDirectory" | "backupDirectory" | "imageRoot",
): Promise<void> {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_directory !== "function") {
    emit("toast", "无法选择文件夹", "桌面文件夹选择器尚未就绪。", "error");
    return;
  }
  if (choosingPath.value) return;
  choosingPath.value = field;
  try {
    const result = await bridge.select_directory();
    if (!result || result.ok !== true) {
      throw new Error(result?.error || result?.message || "文件夹选择失败。");
    }
    if (result.path) {
      migration.form[field] = result.path;
      if (field === "source" && migration.form.migrationType === "schema") {
        migration.form.target = result.path;
      }
      migration.invalidatePreview();
    }
  } catch (error) {
    emit(
      "toast",
      "无法选择文件夹",
      error instanceof Error ? error.message : String(error),
      "error",
    );
  } finally {
    choosingPath.value = "";
  }
}

watch(
  () => migration.form.libraryId,
  applyLibraryDefaults,
);
watch(
  () => migration.form.migrationType,
  (next, previous) => {
    typeDrafts.set(previous, currentTypeDraft());
    const saved = typeDrafts.get(next);
    if (saved) Object.assign(migration.form, saved);
    else applyTypeDefaults(next);
    if (next === "legacy_config") migration.form.target = props.configPath;
    migration.invalidatePreview();
  },
);
watch(
  () => props.configPath,
  (value) => {
    if (migration.form.migrationType !== "legacy_config" || !value) return;
    migration.form.target = value;
    if (!migration.form.source) migration.form.source = value;
    migration.invalidatePreview();
  },
);
</script>

<template>
  <section class="migration-card panel" aria-labelledby="data-migration-title">
    <header class="migration-heading">
      <div>
        <p class="eyebrow">本地数据维护</p>
        <h2 id="data-migration-title">数据迁移</h2>
        <p>先做只读预检查，再完整备份、后台迁移和本地搜索验证。</p>
      </div>
      <span class="status-pill" :class="supported ? 'success' : 'warning'">
        {{ supported ? "仅 Windows" : "当前平台不支持" }}
      </span>
    </header>

    <div v-if="!supported" class="migration-empty" role="status">
      此功能只支持 Windows 桌面版，不提供 macOS 或 Linux 迁移入口。
    </div>

    <template v-else>
      <div class="migration-safety" role="note">
        <strong>不会调用大模型，也不会重新生成 embedding。</strong>
        <span>任何写入前都会创建完整恢复备份；预检查本身不会修改文件。</span>
      </div>

      <section
        v-if="migration.recovery.value"
        class="migration-recovery"
        role="alert"
        aria-labelledby="migration-recovery-title"
      >
        <header>
          <div>
            <p class="eyebrow">需要处理</p>
            <h3 id="migration-recovery-title">检测到未完整结束的数据迁移</h3>
          </div>
          <span class="status-pill warning">已保留完整备份</span>
        </header>
        <p>
          上次程序可能在迁移写入阶段异常退出。恢复完成前，新的搜索、图库任务和迁移都会被安全阻止。
        </p>
        <dl class="recovery-details">
          <div><dt>迁移类型</dt><dd>{{ migrationTypeCopy[migration.recovery.value.migrationType].title }}</dd></div>
          <div><dt>图库</dt><dd>{{ migration.recovery.value.libraryName || migration.recovery.value.libraryId || "未指定" }}</dd></div>
          <div><dt>中断阶段</dt><dd>{{ migration.recovery.value.stage }}</dd></div>
          <div><dt>记录时间</dt><dd>{{ formatTime(migration.recovery.value.updatedAtEpoch) }}</dd></div>
        </dl>
        <div class="path-summary">
          <span><strong>完整备份</strong>{{ migration.recovery.value.backupDirectory }}</span>
        </div>
        <div class="recovery-confirmation">
          <label class="confirmation-check">
            <input v-model="migration.recoveryConfirmationChecked.value" type="checkbox" />
            <span>我确认使用上方完整备份恢复迁移前的数据。</span>
          </label>
          <label>
            <span>输入 RESTORE 确认恢复</span>
            <input
              v-model="migration.recoveryConfirmationPhrase.value"
              name="migration_recovery_confirmation"
              autocomplete="off"
              spellcheck="false"
            />
          </label>
          <button
            class="button danger"
            type="button"
            :disabled="!migration.canRestore.value"
            @click="migration.restoreRecovery"
          >
            {{ migration.restoringRecovery.value ? "提交中…" : "从完整备份恢复" }}
          </button>
        </div>
      </section>

      <form class="migration-form" @submit.prevent="migration.runPrecheck">
        <label>
          <span>迁移类型</span>
          <select v-model="migration.form.migrationType" name="migration_type">
            <option
              v-for="(copy, value) in migrationTypeCopy"
              :key="value"
              :value="value"
            >
              {{ copy.title }}
            </option>
          </select>
          <small>{{ migrationTypeCopy[migration.form.migrationType].description }}</small>
        </label>

        <label v-if="props.libraries.length">
          <span>关联图库</span>
          <select v-model="migration.form.libraryId" name="migration_library">
            <option value="">不关联现有图库</option>
            <option v-for="library in props.libraries" :key="library.id" :value="library.id">
              {{ library.name }}
            </option>
          </select>
          <small>用于自动带入当前图片目录和 Workspace。</small>
        </label>

        <label>
          <span>{{ migration.form.migrationType === "docker_workspace" ? "Docker volume 名称" : "迁移来源" }}</span>
          <div class="path-control">
            <input
              v-model.trim="migration.form.source"
              name="migration_source"
              autocomplete="off"
              spellcheck="false"
              :placeholder="migration.form.migrationType === 'docker_workspace' ? 'zvec_workspace' : 'C:\\Zvec\\workspace'"
              @input="migration.invalidatePreview"
            />
            <button
              v-if="['schema', 'root'].includes(migration.form.migrationType)"
              class="path-button"
              type="button"
              :disabled="Boolean(choosingPath)"
              @click="chooseDirectory('source')"
            >
              {{ choosingPath === "source" ? "选择中…" : "选择" }}
            </button>
          </div>
        </label>

        <label>
          <span>迁移目标</span>
          <div class="path-control">
            <input
              v-model.trim="migration.form.target"
              name="migration_target"
              autocomplete="off"
              spellcheck="false"
              placeholder="D:\Zvec\workspace"
              :readonly="migration.form.migrationType === 'legacy_config'"
              @input="migration.invalidatePreview"
            />
            <button
              v-if="migration.form.migrationType !== 'legacy_config'"
              class="path-button"
              type="button"
              :disabled="Boolean(choosingPath)"
              @click="chooseDirectory('target')"
            >
              {{ choosingPath === "target" ? "选择中…" : "选择" }}
            </button>
          </div>
          <small v-if="migration.form.migrationType === 'schema'">必须与来源为同一个 Workspace。</small>
          <small v-else-if="migration.form.migrationType === 'root'">填写图片移动后的新目录。</small>
          <small v-else-if="migration.form.migrationType === 'docker_workspace'">必须为空目录，导出完成后转为原生 Workspace。</small>
        </label>

        <label v-if="migration.form.migrationType === 'root'">
          <span>Root ID（可选）</span>
          <select
            v-if="migration.preview.value?.rootOptions.length"
            v-model="migration.form.rootId"
            name="migration_root_id"
            @change="migration.invalidatePreview"
          >
            <option value="">自动匹配原路径</option>
            <option
              v-for="option in migration.preview.value.rootOptions"
              :key="option.rootId"
              :value="option.rootId"
            >
              {{ option.rootId }} · {{ option.currentPath }}
            </option>
          </select>
          <input
            v-else
            v-model.trim="migration.form.rootId"
            name="migration_root_id"
            autocomplete="off"
            placeholder="留空自动匹配"
            @input="migration.invalidatePreview"
          />
          <small>留空时预检查会按原路径自动选择；多 Root 图库再从详情中指定。</small>
        </label>

        <label v-if="migration.form.migrationType === 'root'">
          <span>Workspace</span>
          <div class="path-control">
            <input
              v-model.trim="migration.form.workspaceDirectory"
              name="migration_workspace"
              autocomplete="off"
              spellcheck="false"
              @input="migration.invalidatePreview"
            />
            <button
              class="path-button"
              type="button"
              :disabled="Boolean(choosingPath)"
              @click="chooseDirectory('workspaceDirectory')"
            >
              {{ choosingPath === "workspaceDirectory" ? "选择中…" : "选择" }}
            </button>
          </div>
        </label>

        <label v-if="migration.form.migrationType === 'docker_workspace'">
          <span>原 Docker 镜像</span>
          <input
            v-model.trim="migration.form.dockerImage"
            name="migration_docker_image"
            autocomplete="off"
            spellcheck="false"
            @input="migration.invalidatePreview"
          />
          <small>只用于只读挂载并导出 named volume。</small>
        </label>

        <label v-if="migration.form.migrationType === 'docker_workspace'">
          <span>原图片目录</span>
          <div class="path-control">
            <input
              v-model.trim="migration.form.imageRoot"
              name="migration_image_root"
              autocomplete="off"
              spellcheck="false"
              placeholder="D:\Pictures"
              @input="migration.invalidatePreview"
            />
            <button
              class="path-button"
              type="button"
              :disabled="Boolean(choosingPath)"
              @click="chooseDirectory('imageRoot')"
            >
              {{ choosingPath === "imageRoot" ? "选择中…" : "选择" }}
            </button>
          </div>
          <small>用于把容器内旧 Root 重绑到当前 Windows 图片目录。</small>
        </label>

        <label>
          <span>备份目录（可选）</span>
          <div class="path-control">
            <input
              v-model.trim="migration.form.backupDirectory"
              name="migration_backup_directory"
              autocomplete="off"
              spellcheck="false"
              placeholder="留空则保存到应用迁移备份目录"
              @input="migration.invalidatePreview"
            />
            <button
              class="path-button"
              type="button"
              :disabled="Boolean(choosingPath)"
              @click="chooseDirectory('backupDirectory')"
            >
              {{ choosingPath === "backupDirectory" ? "选择中…" : "选择" }}
            </button>
          </div>
        </label>

        <label class="locked-option">
          <input type="checkbox" checked disabled />
          <span>自动创建完整恢复备份</span>
        </label>

        <div class="migration-actions">
          <button
            class="button secondary"
            type="submit"
            :disabled="migration.prechecking.value || migration.active.value || Boolean(migration.recovery.value)"
          >
            {{ migration.prechecking.value ? "预检查中…" : "预检查 / Dry-run" }}
          </button>
          <span>预检查不会修改 Collection、SQLite 或配置。</span>
        </div>
      </form>

      <section
        v-if="migration.preview.value"
        class="migration-preview"
        :class="{ blocked: migration.preview.value.status === 'blocked' }"
        aria-live="polite"
      >
        <header>
          <div>
            <p class="eyebrow">预检查结果</p>
            <h3>{{ migration.preview.value.status === "ready" ? "可以迁移" : "暂时无法迁移" }}</h3>
          </div>
          <span class="status-pill" :class="migration.preview.value.status === 'ready' ? 'success' : 'warning'">
            {{ migration.preview.value.dryRun ? "Dry-run" : "检查完成" }}
          </span>
        </header>

        <dl class="migration-metrics">
          <div><dt>Collection 文档</dt><dd>{{ formatCount(migration.preview.value.collectionDocuments) }}</dd></div>
          <div><dt>SQLite 条目</dt><dd>{{ formatCount(migration.preview.value.sqliteEntries) }}</dd></div>
          <div><dt>Collection 状态</dt><dd>{{ migration.preview.value.collectionStatus || "未知" }}</dd></div>
          <div><dt>SQLite 状态</dt><dd>{{ migration.preview.value.sqliteStatus || "未知" }}</dd></div>
          <div><dt>预计备份</dt><dd>{{ formatBytes(migration.preview.value.backup.estimatedPayloadBytes) }}</dd></div>
          <div><dt>模型请求</dt><dd>{{ migration.preview.value.modelApiRequests }}</dd></div>
        </dl>

        <div class="path-summary">
          <span><strong>来源</strong>{{ migration.preview.value.source }}</span>
          <span><strong>目标</strong>{{ migration.preview.value.target }}</span>
          <span><strong>备份</strong>{{ migration.preview.value.backup.destination || "自动分配" }}</span>
        </div>

        <ul v-if="migration.preview.value.blockers.length" class="issue-list blockers">
          <li v-for="item in migration.preview.value.blockers" :key="item">{{ item }}</li>
        </ul>
        <ul v-if="migration.preview.value.warnings.length" class="issue-list warnings">
          <li v-for="item in migration.preview.value.warnings" :key="item">{{ item }}</li>
        </ul>

        <div v-if="migration.preview.value.status === 'ready'" class="migration-confirmation">
          <label class="confirmation-check">
            <input v-model="migration.confirmationChecked.value" type="checkbox" />
            <span>我已核对来源、目标、文档数和备份位置。</span>
          </label>
          <label>
            <span>输入 MIGRATE 确认执行</span>
            <input
              v-model="migration.confirmationPhrase.value"
              name="migration_confirmation"
              autocomplete="off"
              spellcheck="false"
            />
          </label>
          <button
            class="button primary"
            type="button"
            :disabled="!migration.canStart.value"
            @click="migration.startMigration"
          >
            {{ migration.starting.value ? "提交中…" : "开始后台迁移" }}
          </button>
        </div>
      </section>

      <section v-if="migration.job.value" class="migration-progress" aria-live="polite">
        <header>
          <div>
            <p class="eyebrow">后台任务</p>
            <h3>{{ migration.job.value.message || "正在处理迁移" }}</h3>
          </div>
          <span class="status-pill">{{ migration.job.value.status }}</span>
        </header>
        <div class="progress-line">
          <progress :value="migration.job.value.progress" max="100" />
          <strong>{{ migration.job.value.progress }}%</strong>
        </div>
        <p v-if="migration.job.value.stage">当前阶段：{{ migration.job.value.stage }}</p>
        <p v-if="migration.job.value.backupDirectory">恢复备份：{{ migration.job.value.backupDirectory }}</p>
        <div v-if="migration.job.value.verification" class="verification-summary">
          <span>文档 {{ formatCount(migration.job.value.verification.collectionDocuments) }}</span>
          <span>SQLite {{ migration.job.value.verification.sqliteIntegrity || "未知" }}</span>
          <span>搜索 {{ migration.job.value.verification.searchProbe || "未知" }}</span>
        </div>
        <p v-if="migration.job.value.error" class="inline-error">{{ migration.job.value.error }}</p>
        <button
          v-if="migration.active.value"
          class="button danger"
          type="button"
          :disabled="migration.cancelling.value"
          @click="migration.cancelMigration"
        >
          {{ migration.cancelling.value ? "正在安全取消…" : "取消迁移" }}
        </button>
      </section>

      <p v-if="migration.lastError.value" class="inline-error" role="alert">
        {{ migration.lastError.value }}
      </p>
    </template>
  </section>
</template>

<style scoped>
.migration-card { display: grid; gap: 14px; padding: 18px; color: #17203a; }
.migration-heading, .migration-preview header, .migration-progress header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.migration-heading h2, .migration-preview h3, .migration-progress h3 { margin: 2px 0 0; }
.migration-heading p:not(.eyebrow), .migration-progress p { margin: 6px 0 0; color: #68718a; }
.eyebrow { margin: 0; color: #6258e7; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.status-pill { display: inline-flex; min-height: 28px; align-items: center; padding: 4px 10px; border-radius: 999px; color: #596478; font-size: 11px; font-weight: 800; background: #eef1f6; white-space: nowrap; }
.status-pill.success { color: #08705d; background: #e7f7f2; }
.status-pill.warning { color: #92500b; background: #fff5e7; }
.migration-safety { display: grid; gap: 4px; padding: 12px 14px; border: 1px solid #d9d4ff; border-radius: 12px; color: #5147ad; background: #f5f3ff; }
.migration-safety span { color: #6a648f; font-size: 12px; }
.migration-recovery { display: grid; gap: 12px; padding: 15px; border: 1px solid #efbc7c; border-radius: 14px; background: #fff9ef; }
.migration-recovery header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.migration-recovery h3 { margin: 2px 0 0; color: #713d0a; }
.migration-recovery > p { margin: 0; color: #7b5a31; font-size: 12px; line-height: 1.6; }
.recovery-details { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0; }
.recovery-details div { min-width: 0; padding: 9px 10px; border-radius: 9px; background: rgb(255 255 255 / 72%); }
.recovery-details dt { color: #8a6a43; font-size: 10px; font-weight: 700; }
.recovery-details dd { margin: 4px 0 0; overflow: hidden; color: #5e3b16; font-size: 12px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }
.recovery-confirmation { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(220px, .65fr) auto; align-items: end; gap: 12px; padding-top: 2px; }
.recovery-confirmation label { display: grid; align-content: start; gap: 6px; min-width: 0; }
.recovery-confirmation label > span { font-size: 12px; font-weight: 750; }
.recovery-confirmation input { width: 100%; min-height: 40px; padding: 0 11px; border: 1px solid #dfc394; border-radius: 10px; color: #4f3519; background: #fff; }
.recovery-confirmation .confirmation-check input { width: auto; min-height: 0; }
.migration-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.migration-form label, .migration-confirmation label { display: grid; align-content: start; gap: 6px; min-width: 0; }
.migration-form label > span, .migration-confirmation label > span { font-size: 12px; font-weight: 750; }
.migration-form input, .migration-form select, .migration-confirmation input { width: 100%; min-height: 40px; padding: 0 11px; border: 1px solid #cfd5e2; border-radius: 10px; color: #17203a; background: #fff; }
.migration-form input:focus, .migration-form select:focus, .migration-confirmation input:focus { outline: 0; border-color: #6258e7; box-shadow: 0 0 0 3px rgb(98 88 231 / 12%); }
.path-control { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px; }
.path-button { min-width: 62px; border: 1px solid #d5d9e4; border-radius: 10px; color: #343d52; font: inherit; font-size: 12px; font-weight: 750; background: #fff; }
.path-button:disabled { opacity: .55; }
.migration-form small { color: #737d91; font-size: 11px; line-height: 1.45; }
.locked-option { display: flex !important; align-items: center; align-self: end; grid-template-columns: auto 1fr; min-height: 40px; padding: 10px 12px; border-radius: 10px; background: #f7f8fc; }
.locked-option input { width: auto; min-height: 0; }
.migration-actions { display: flex; align-items: center; gap: 12px; grid-column: 1 / -1; padding-top: 2px; }
.migration-actions span { color: #737d91; font-size: 11px; }
.migration-preview, .migration-progress { display: grid; gap: 13px; padding: 15px; border: 1px solid #dfe3ec; border-radius: 14px; background: #fbfcff; }
.migration-preview.blocked { border-color: #f1c9a4; background: #fffaf3; }
.migration-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; margin: 0; }
.migration-metrics div { padding: 10px; border-radius: 10px; background: #f2f4f8; }
.migration-metrics dt { color: #69748a; font-size: 10px; font-weight: 700; }
.migration-metrics dd { margin: 4px 0 0; overflow: hidden; font-size: 14px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }
.path-summary { display: grid; gap: 6px; padding: 10px 12px; border-radius: 10px; background: #f7f8fc; }
.path-summary span { display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 8px; overflow-wrap: anywhere; color: #5f687b; font-size: 11px; }
.path-summary strong { color: #2c3448; }
.issue-list { display: grid; gap: 5px; margin: 0; padding: 10px 12px 10px 30px; border-radius: 10px; font-size: 12px; }
.issue-list.blockers { color: #9e3441; background: #fff0f2; }
.issue-list.warnings { color: #8a550f; background: #fff7e9; }
.migration-confirmation { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(220px, .6fr) auto; align-items: end; gap: 12px; padding-top: 4px; border-top: 1px solid #e4e7ee; }
.confirmation-check { display: flex !important; min-height: 40px; align-items: center; grid-template-columns: auto 1fr; }
.confirmation-check input { width: auto; min-height: 0; }
.progress-line { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; }
.progress-line progress { width: 100%; height: 9px; border: 0; border-radius: 999px; overflow: hidden; appearance: none; background: #e7e9ef; }
.progress-line progress::-webkit-progress-bar { background: #e7e9ef; }
.progress-line progress::-webkit-progress-value { background: #6258e7; }
.verification-summary { display: flex; flex-wrap: wrap; gap: 8px; }
.verification-summary span { padding: 6px 9px; border-radius: 8px; color: #08705d; font-size: 11px; font-weight: 750; background: #e7f7f2; }
.inline-error { margin: 0; padding: 9px 11px; border-radius: 9px; color: #a93643; background: #fff0f2; }
.migration-empty { padding: 20px; border-radius: 12px; color: #68718a; text-align: center; background: #f7f8fc; }
.button { display: inline-flex; min-height: 40px; align-items: center; justify-content: center; padding: 0 14px; border: 1px solid transparent; border-radius: 10px; font: inherit; font-size: 12px; font-weight: 800; }
.button.primary { color: #fff; background: #6258e7; }
.button.secondary { border-color: #d5d9e4; color: #343d52; background: #fff; }
.button.danger { justify-self: start; color: #fff; background: #a93643; }
.button:disabled { cursor: not-allowed; opacity: .55; }
@media (max-width: 900px) {
  .migration-form, .migration-confirmation, .migration-metrics, .recovery-details, .recovery-confirmation { grid-template-columns: 1fr; }
  .migration-actions, .migration-heading, .migration-preview header, .migration-progress header, .migration-recovery header { align-items: stretch; flex-direction: column; }
}
</style>
