<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import type { RemoteAccessApi, RemoteConnectionState } from "./remoteTypes";
import type { ToastKind } from "./types";
import { useRemoteAccess } from "./useRemoteAccess";

const props = withDefaults(defineProps<{
  api?: RemoteAccessApi;
  visible?: boolean;
}>(), {
  visible: true,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
}>();

const remote = useRemoteAccess(props.api, {
  onToast: (title, message, kind) => emit("toast", title, message, kind),
}, { visible: props.visible });

watch(() => props.visible, (value) => remote.setVisible(value));

const STATE_LABELS: Record<RemoteConnectionState, string> = {
  unconfigured: "未配置",
  disabled: "已关闭",
  connecting: "连接中",
  connected: "已连接",
  reconnecting: "重连中",
  error: "失败",
};

const STATE_TONES: Record<RemoteConnectionState, string> = {
  unconfigured: "",
  disabled: "",
  connecting: "pending",
  connected: "success",
  reconnecting: "pending",
  error: "danger",
};

const state = computed<RemoteConnectionState>(
  () => remote.status.value?.state ?? "unconfigured",
);
const configured = computed(() => remote.status.value?.configured === true);
const enabled = computed(() => remote.status.value?.enabled === true);
const stateLabel = computed(() => STATE_LABELS[state.value]);
const stateTone = computed(() => STATE_TONES[state.value]);
const serverLabel = computed(() => {
  const host = remote.status.value?.serverHost;
  if (!host) return "";
  const port = remote.status.value?.serverPort;
  return port ? `${host}:${port}` : host;
});

const showImport = ref(false);
const selectedPath = ref("");
const picking = ref(false);
const importing = computed(() => remote.action.value === "import");
const modalRef = ref<HTMLElement | null>(null);
let importTrigger: HTMLElement | null = null;

function openImport(event?: MouseEvent): void {
  if (importing.value) return;
  importTrigger = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  selectedPath.value = "";
  showImport.value = true;
  void nextTick(() => modalRef.value?.focus({ preventScroll: true }));
}

function dismissImport(): void {
  if (importing.value) return;
  showImport.value = false;
  selectedPath.value = "";
  const trigger = importTrigger;
  importTrigger = null;
  if (trigger) void nextTick(() => trigger.focus({ preventScroll: true }));
}

function modalFocusable(): HTMLElement[] {
  const root = modalRef.value;
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(
    "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href]",
  ));
}

function onModalKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.preventDefault();
    dismissImport();
    return;
  }
  if (event.key !== "Tab") return;
  const items = modalFocusable();
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && Boolean(modalRef.value?.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
  } else if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

async function pickFile(): Promise<void> {
  if (picking.value || importing.value) return;
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_frpc_config_file !== "function") {
    emit("toast", "无法选择配置文件", "桌面文件选择器尚未就绪，请稍后重试。", "error");
    return;
  }
  picking.value = true;
  try {
    const result = await bridge.select_frpc_config_file();
    if (!result || result.ok !== true) {
      throw new Error(result?.error || result?.message || "文件选择失败。");
    }
    if (result.path) selectedPath.value = result.path;
  } catch (error) {
    emit(
      "toast",
      "无法选择配置文件",
      error instanceof Error ? error.message : String(error),
      "error",
    );
  } finally {
    picking.value = false;
  }
}

async function confirmImport(): Promise<void> {
  if (!selectedPath.value || importing.value) return;
  if (await remote.importConfig(selectedPath.value)) {
    showImport.value = false;
    selectedPath.value = "";
    const trigger = importTrigger;
    importTrigger = null;
    if (trigger) void nextTick(() => trigger.focus({ preventScroll: true }));
  }
}
</script>

<template>
  <section class="remote-card panel" aria-labelledby="remote-access-title">
    <header class="remote-heading">
      <div>
        <p class="eyebrow">WINDOWS 远程访问</p>
        <h2 id="remote-access-title">远程访问</h2>
        <p>在外网访问这台电脑的图库，软件启动后自动连接。</p>
      </div>
      <span class="status-pill" :class="stateTone">{{ stateLabel }}</span>
    </header>

    <dl class="remote-facts">
      <div>
        <dt>公网地址</dt>
        <dd>{{ remote.status.value?.publicUrl || "尚未配置" }}</dd>
      </div>
      <div v-if="serverLabel">
        <dt>FRP 服务器</dt>
        <dd>{{ serverLabel }}</dd>
      </div>
    </dl>

    <p class="remote-status" role="status" aria-live="polite">
      {{ remote.status.value?.message || "正在读取远程访问状态…" }}
    </p>

    <div class="remote-actions">
      <button
        v-if="!configured"
        class="button primary"
        type="button"
        :disabled="remote.busy.value"
        @click="openImport"
      >
        导入 FRPC 配置
      </button>
      <button
        v-else-if="state === 'error' && enabled"
        class="button primary"
        type="button"
        :disabled="remote.busy.value"
        @click="remote.retry"
      >
        {{ remote.action.value === "retry" ? "重试中…" : "重试连接" }}
      </button>
      <button
        v-else-if="!enabled"
        class="button primary"
        type="button"
        :disabled="remote.busy.value"
        @click="remote.enable"
      >
        {{ remote.action.value === "enable" ? "开启中…" : "开启远程访问" }}
      </button>
      <button
        v-else
        class="button danger"
        type="button"
        :disabled="remote.busy.value"
        @click="remote.disable"
      >
        {{ remote.action.value === "disable" ? "关闭中…" : "关闭远程访问" }}
      </button>
      <button
        v-if="state === 'error' && enabled"
        class="button danger"
        type="button"
        :disabled="remote.busy.value"
        @click="remote.disable"
      >
        关闭远程访问
      </button>
      <button
        v-if="configured"
        class="button secondary"
        type="button"
        :disabled="remote.busy.value"
        @click="openImport"
      >
        替换配置
      </button>
      <button
        class="button quiet"
        type="button"
        :disabled="remote.loading.value"
        @click="remote.refresh"
      >
        刷新
      </button>
    </div>

    <div class="remote-guidance">
      <strong>使用步骤</strong>
      <ol>
        <li>先在“局域网访问”里开启手机网关并保存，远程访问会复用它的本机地址与端口。</li>
        <li>导入一次 FRPC 配置；副本保存在应用配置目录，不会修改原文件。</li>
        <li>保持 YaoLens 常驻运行（关闭窗口即可），软件重启后会自动恢复。</li>
      </ol>
      <p>首次连接可选择局域网或外网入口，在电脑“局域网访问”页面核对并批准配对码。以后自动连接。</p>
    </div>

    <p v-if="remote.lastError.value" class="remote-error" role="alert">
      {{ remote.lastError.value }}
    </p>

    <div v-if="showImport" class="remote-modal-backdrop" role="presentation" @click.self="dismissImport">
      <section
        ref="modalRef"
        class="remote-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="remote-import-title"
        tabindex="-1"
        @keydown="onModalKeydown"
      >
        <header>
          <h3 id="remote-import-title">导入 FRPC 配置</h3>
          <button
            class="remote-modal-close"
            type="button"
            aria-label="关闭导入"
            :disabled="importing"
            @click="dismissImport"
          >
            ×
          </button>
        </header>
        <p>选择现有 FRPC TOML 文件。YaoLens 只在应用配置目录保留副本，不修改原文件，也不显示令牌。</p>
        <div class="remote-file">
          <span class="remote-file-path" :class="{ empty: !selectedPath }">
            {{ selectedPath || "尚未选择文件" }}
          </span>
          <button
            class="button secondary"
            type="button"
            :disabled="picking || importing"
            @click="pickFile"
          >
            {{ picking ? "选择中…" : selectedPath ? "重新选择" : "选择 TOML 文件" }}
          </button>
        </div>
        <div class="remote-modal-actions">
          <button
            class="button secondary"
            type="button"
            :disabled="importing"
            @click="dismissImport"
          >
            取消
          </button>
          <button
            class="button primary"
            type="button"
            :disabled="!selectedPath || importing"
            @click="confirmImport"
          >
            {{ importing ? "导入中…" : "导入并保存" }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.remote-card {
  --remote-surface: #fff;
  --remote-text: #242424;
  --remote-muted: #626262;
  --remote-line: #dedede;
  --remote-soft: #f3f3f3;
  --remote-primary: #555;
  --remote-primary-text: #fff;
  --remote-danger: #992f3d;
  --remote-danger-soft: #fff2f3;
  display: grid;
  gap: 16px;
  padding: 0;
  line-height: 1.6;
}
:global(.settings-feature-page .remote-card.panel) {
  color: var(--remote-text) !important;
  background: var(--remote-surface) !important;
}
.remote-heading, .remote-modal header, .remote-modal-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.remote-heading { align-items: flex-start; }
.remote-heading h2 { margin: 0; font-size: 18px; font-weight: 600; }
.remote-heading p:not(.eyebrow) { margin: 8px 0 0; color: var(--remote-muted); font-size: 13px; }
.eyebrow { display: none; }
.status-pill { padding: 4px 8px; border-radius: 8px; color: var(--remote-muted); background: var(--remote-soft); font-size: 12px; white-space: nowrap; }
.status-pill.success { color: #176b55; background: #e9f8f1; }
.status-pill.pending { color: #6c4a12; background: #fdf3e2; }
.status-pill.danger { color: var(--remote-danger); background: var(--remote-danger-soft); }
.remote-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 0; padding: 16px 0; border-block: 1px solid var(--remote-line); }
.remote-facts dt { color: var(--remote-muted); font-size: 12px; }
.remote-facts dd { margin: 4px 0 0; font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.remote-status { min-height: 20px; margin: 0; color: var(--remote-muted); font-size: 13px; }
.remote-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.remote-card .button { min-height: 36px; padding: 8px 12px; border: 1px solid var(--remote-line); border-radius: 8px; color: var(--remote-text); background: var(--remote-soft); font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; }
:global(.settings-feature-page .remote-card .button.primary) { color: var(--remote-primary-text) !important; border-color: var(--remote-primary) !important; background: var(--remote-primary) !important; }
:global(.settings-feature-page .remote-card .button.secondary) { color: var(--remote-text) !important; border-color: var(--remote-line) !important; background: var(--remote-soft) !important; }
.remote-card .button.danger { color: var(--remote-danger); background: var(--remote-danger-soft); }
.remote-card button:disabled { cursor: not-allowed; opacity: .5; }
.remote-card button:focus-visible { outline: 2px solid var(--remote-text); outline-offset: 2px; }
.remote-guidance { display: grid; gap: 8px; padding-top: 16px; border-top: 1px solid var(--remote-line); font-size: 13px; }
.remote-guidance strong { font-weight: 600; }
.remote-guidance ol { margin: 0; padding-left: 24px; color: var(--remote-muted); }
.remote-guidance li + li { margin-top: 4px; }
.remote-guidance p { margin: 0; color: var(--remote-muted); }
.remote-error { margin: 0; color: var(--remote-danger); }
.remote-modal-backdrop { position: fixed; z-index: 1200; inset: 0; display: grid; place-items: center; padding: 16px; background: rgb(0 0 0 / 42%); }
.remote-modal { display: grid; width: min(480px, 100%); max-height: calc(100dvh - 32px); overflow: auto; box-sizing: border-box; gap: 16px; padding: 16px; border: 1px solid var(--remote-line); border-radius: 12px; color: var(--remote-text); background: var(--remote-surface); box-shadow: 0 16px 48px rgb(0 0 0 / 24%); }
.remote-modal:focus-visible { outline: none; }
.remote-modal h3 { margin: 0; font-size: 16px; }
.remote-modal p { margin: 0; color: var(--remote-muted); font-size: 13px; }
.remote-modal-close { width: 36px; height: 36px; flex-shrink: 0; border: 0; border-radius: 8px; color: var(--remote-text); background: var(--remote-soft); font-size: 20px; cursor: pointer; }
.remote-file { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; padding-block: 8px; border-block: 1px solid var(--remote-line); }
.remote-file-path { color: var(--remote-text); font-size: 13px; overflow-wrap: anywhere; }
.remote-file-path.empty { color: var(--remote-muted); }
.remote-modal-actions { justify-content: flex-end; }
@media (max-width: 720px) {
  .remote-facts { grid-template-columns: 1fr; }
  .remote-file { grid-template-columns: 1fr; }
  .remote-card .button, .remote-modal-close { min-height: 44px; }
  .remote-modal-close { width: 44px; }
}
@media (prefers-color-scheme: dark) {
  .remote-card { --remote-surface: #1c1c1c; --remote-text: #ededed; --remote-muted: #b8b8b8; --remote-line: #444; --remote-soft: #303030; --remote-primary: #d0d0d0; --remote-primary-text: #1c1c1c; --remote-danger: #ffd8dd; --remote-danger-soft: #492b30; }
  .status-pill.success { color: #b0e3ce; background: #1d4235; }
  .status-pill.pending { color: #f1d7a5; background: #44351d; }
}
</style>
