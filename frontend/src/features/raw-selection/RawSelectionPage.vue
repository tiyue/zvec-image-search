<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";

import AppIcon from "../../components/AppIcon.vue";
import {
  clearProjectCache,
  createProject,
  deleteProject,
  importFolder,
  listProjects,
  renameProject,
} from "./api";
import RawSelectionWorkspace from "./RawSelectionWorkspace.vue";
import type { RawProject } from "./types";

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
  selectFolder: [callback: (path: string) => void];
}>();

const projects = ref<RawProject[]>([]);
const loading = ref(false);
const showCreateModal = ref(false);
const newName = ref("");
const renamingId = ref<string | null>(null);
const renameValue = ref("");
const importing = ref(false);

const workspaceProject = ref<RawProject | null>(null);

let abortController: AbortController | null = null;

async function refresh() {
  abortController?.abort();
  abortController = new AbortController();
  loading.value = true;
  try {
    const result = await listProjects(abortController.signal);
    projects.value = result.projects;
  } catch (err) {
    if (!(err instanceof DOMException && err.name === "AbortError")) {
      emit("toast", "加载失败", "无法获取项目列表", "error");
    }
  } finally {
    loading.value = false;
  }
}

async function handleCreate() {
  const name = newName.value.trim();
  if (!name || name.length > 40) {
    emit("toast", "名称无效", "项目名称需为 1-40 个字符", "error");
    return;
  }
  try {
    await createProject(name);
    showCreateModal.value = false;
    newName.value = "";
    emit("toast", "已创建", `项目「${name}」已创建`, "success");
    await refresh();
  } catch {
    emit("toast", "创建失败", "无法创建项目", "error");
  }
}

function startRename(p: RawProject) {
  renamingId.value = p.id;
  renameValue.value = p.name;
}

async function handleRename() {
  if (!renamingId.value) return;
  const name = renameValue.value.trim();
  if (!name || name.length > 40) {
    emit("toast", "名称无效", "项目名称需为 1-40 个字符", "error");
    return;
  }
  try {
    await renameProject(renamingId.value, name);
    renamingId.value = null;
    emit("toast", "已重命名", "项目名称已更新", "success");
    await refresh();
  } catch {
    emit("toast", "重命名失败", "无法重命名项目", "error");
  }
}

async function handleDelete(p: RawProject) {
  if (!confirm(`确定删除项目「${p.name}」？\n\n此操作只删除项目引用、评级和缓存，不会删除源文件。`)) return;
  try {
    await deleteProject(p.id);
    emit("toast", "已删除", `项目「${p.name}」已删除`, "info");
    await refresh();
  } catch {
    emit("toast", "删除失败", "无法删除项目", "error");
  }
}

async function handleClearCache(p: RawProject) {
  try {
    await clearProjectCache(p.id);
    emit("toast", "已清理", `项目「${p.name}」的缓存已清除`, "info");
  } catch {
    emit("toast", "清理失败", "无法清除缓存", "error");
  }
}

function handleImportFolder(p: RawProject) {
  importing.value = true;
  emit("selectFolder", async (path: string) => {
    if (!path) {
      importing.value = false;
      return;
    }
    try {
      const result = await importFolder(p.id, path);
      emit(
        "toast",
        "导入完成",
        `已登记 ${result.registered} 张` +
          (result.skipped_raw_formats > 0
            ? `，跳过 ${result.skipped_raw_formats} 张不支持的 RAW 格式`
            : ""),
        result.registered > 0 ? "success" : "info",
      );
      await refresh();
    } catch {
      emit("toast", "导入失败", "无法导入文件夹", "error");
    } finally {
      importing.value = false;
    }
  });
}

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(iso));
  } catch {
    return "";
  }
}

function openProject(p: RawProject) {
  workspaceProject.value = p;
}

onMounted(() => {
  void refresh();
});

onBeforeUnmount(() => {
  abortController?.abort();
});
</script>

<template>
  <RawSelectionWorkspace
    v-if="workspaceProject"
    :project-id="workspaceProject.id"
    :project-name="workspaceProject.name"
    @toast="(t, m, k) => emit('toast', t, m, k)"
    @select-folder="(cb) => emit('selectFolder', cb)"
    @back="workspaceProject = null"
  />
  <div v-else class="raw-selection-page">
    <header class="rs-header">
      <div>
        <h2>ARW 选片</h2>
        <p>导入 Sony A7M4 ARW 和 JPG/PNG，快速浏览、评级、筛选与导出</p>
      </div>
      <button class="rs-new-btn" type="button" @click="showCreateModal = true">
        <AppIcon name="plus" :size="16" />
        <span>新建项目</span>
      </button>
    </header>

    <div v-if="loading && !projects.length" class="rs-loading">加载中…</div>

    <div v-else-if="!projects.length" class="rs-empty">
      <p>暂无选片项目</p>
      <button class="rs-empty-btn" type="button" @click="showCreateModal = true">
        创建第一个项目
      </button>
    </div>

    <div v-else class="rs-grid">
      <article
        v-for="p in projects"
        :key="p.id"
        class="rs-card"
        @dblclick="openProject(p)"
      >
        <div class="rs-card-thumb">
          <span class="rs-card-count">{{ p.member_count }}</span>
        </div>
        <div class="rs-card-body">
          <template v-if="renamingId === p.id">
            <input
              v-model="renameValue"
              class="rs-rename-input"
              maxlength="40"
              @keydown.enter="handleRename"
              @keydown.escape="renamingId = null"
              @blur="handleRename"
            />
          </template>
          <template v-else>
            <strong class="rs-card-name" @dblclick="startRename(p)">{{ p.name }}</strong>
          </template>
          <small class="rs-card-date">{{ formatDate(p.updated_at) }} 更新</small>
        </div>
        <div class="rs-card-actions">
          <button type="button" title="进入工作区" @click="openProject(p)">
            <AppIcon name="image" :size="15" />
          </button>
          <button type="button" title="导入文件夹" :disabled="importing" @click="handleImportFolder(p)">
            <AppIcon name="plus" :size="15" />
          </button>
          <button type="button" title="重命名" @click="startRename(p)">
            <AppIcon name="compose" :size="15" />
          </button>
          <button type="button" title="清除缓存" @click="handleClearCache(p)">
            <AppIcon name="sliders" :size="15" />
          </button>
          <button type="button" title="删除项目" class="rs-danger-btn" @click="handleDelete(p)">
            ×
          </button>
        </div>
      </article>
    </div>

    <div v-if="showCreateModal" class="rs-modal-backdrop" @click.self="showCreateModal = false">
      <div class="rs-modal" role="dialog" aria-modal="true">
        <h3>新建选片项目</h3>
        <input
          v-model="newName"
          class="rs-modal-input"
          placeholder="项目名称（1-40 字符）"
          maxlength="40"
          @keydown.enter="handleCreate"
          @keydown.escape="showCreateModal = false"
        />
        <div class="rs-modal-actions">
          <button type="button" class="rs-modal-cancel" @click="showCreateModal = false">取消</button>
          <button type="button" class="rs-modal-confirm" :disabled="!newName.trim()" @click="handleCreate">创建</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.raw-selection-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow: auto;
  padding: 16px;
}

.rs-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.rs-header h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
}

.rs-header p {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.rs-new-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  min-height: 32px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
}

.rs-new-btn:hover {
  background: var(--surface-soft);
}

.rs-loading,
.rs-empty {
  display: grid;
  place-items: center;
  flex: 1;
  color: var(--muted);
  gap: 12px;
}

.rs-empty-btn {
  padding: 0 16px;
  min-height: 34px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
}

.rs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.rs-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--surface);
  cursor: pointer;
  transition: box-shadow 0.15s;
}

.rs-card:hover {
  box-shadow: var(--shadow-float);
}

.rs-card-thumb {
  display: grid;
  place-items: center;
  height: 120px;
  background: var(--surface-soft);
  color: var(--faint);
  font-size: 20px;
  font-weight: 500;
}

.rs-card-count::after {
  content: " 张";
  font-size: 12px;
  font-weight: 400;
}

.rs-card-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
}

.rs-card-name {
  font-size: 14px;
  font-weight: 400;
  cursor: text;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-card-date {
  color: var(--muted);
  font-size: 11px;
}

.rs-rename-input {
  width: 100%;
  padding: 4px 6px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  font-size: 14px;
  background: var(--surface);
}

.rs-card-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px 8px;
}

.rs-card-actions button {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
}

.rs-card-actions button:hover {
  background: var(--surface-soft);
  color: var(--text);
}

.rs-danger-btn:hover {
  color: var(--danger) !important;
}

.rs-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 25%);
}

.rs-modal {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(360px, calc(100vw - 32px));
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  box-shadow: var(--shadow-float);
}

.rs-modal h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
}

.rs-modal-input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  font-size: 14px;
  background: var(--surface);
}

.rs-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.rs-modal-cancel,
.rs-modal-confirm {
  padding: 0 14px;
  min-height: 32px;
  border: 1px solid var(--border);
  border-radius: 9px;
  font-size: 13px;
}

.rs-modal-cancel {
  background: var(--surface);
  color: var(--text);
}

.rs-modal-confirm {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}

.rs-modal-confirm:disabled {
  opacity: 0.45;
}
</style>
