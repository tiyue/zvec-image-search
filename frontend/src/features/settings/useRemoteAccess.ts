import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { remoteAccessApi } from "./remoteApi";
import type {
  RemoteAccessApi,
  RemoteAccessStatus,
  RemoteAccessStatusWire,
  RemoteConnectionState,
} from "./remoteTypes";

const POLL_INTERVAL_MS = 2_000;

const KNOWN_STATES: readonly RemoteConnectionState[] = [
  "unconfigured",
  "disabled",
  "connecting",
  "connected",
  "reconnecting",
  "error",
];

interface RemoteAccessEvents {
  onToast?: (title: string, message: string, kind: "info" | "success" | "error") => void;
}

interface RemoteAccessOptions {
  visible?: boolean;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function optionalPort(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 65_535 ? parsed : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function normalizeState(
  value: unknown,
  configured: boolean,
  enabled: boolean,
): RemoteConnectionState {
  const raw = text(value);
  if ((KNOWN_STATES as readonly string[]).includes(raw)) {
    return raw as RemoteConnectionState;
  }
  if (!configured) return "unconfigured";
  return enabled ? "connecting" : "disabled";
}

export function normalizeRemoteAccessStatus(
  raw: RemoteAccessStatusWire,
): RemoteAccessStatus {
  const configured = raw.configured === true;
  const enabled = raw.enabled === true;
  return {
    configured,
    enabled,
    state: normalizeState(raw.state, configured, enabled),
    message: text(raw.message),
    publicUrl: text(raw.public_url),
    serverHost: text(raw.server_host),
    serverPort: optionalPort(raw.server_port),
    remotePort: optionalPort(raw.remote_port),
  };
}

export function useRemoteAccess(
  suppliedApi?: RemoteAccessApi,
  events: RemoteAccessEvents = {},
  options: RemoteAccessOptions = {},
) {
  const api = suppliedApi ?? remoteAccessApi;
  const status = ref<RemoteAccessStatus | null>(null);
  const loading = ref(false);
  const action = ref("");
  const lastError = ref("");
  const visible = ref(options.visible ?? false);
  let controller: AbortController | null = null;
  let pollHandle: number | null = null;

  const busy = computed(() => loading.value || Boolean(action.value));

  function canPoll(): boolean {
    return (
      visible.value &&
      !document.hidden &&
      status.value !== null &&
      status.value.enabled
    );
  }

  function clearPoll(): void {
    if (pollHandle !== null) {
      window.clearTimeout(pollHandle);
      pollHandle = null;
    }
  }

  function schedulePoll(): void {
    clearPoll();
    if (busy.value || !canPoll()) return;
    pollHandle = window.setTimeout(() => {
      pollHandle = null;
      void load(true);
    }, POLL_INTERVAL_MS);
  }

  function abortInFlight(): void {
    controller?.abort();
    controller = null;
  }

  function apply(payload: RemoteAccessStatusWire): RemoteAccessStatus {
    const normalized = normalizeRemoteAccessStatus(payload);
    status.value = normalized;
    return normalized;
  }

  async function load(silent = false): Promise<void> {
    if (!visible.value || action.value) return;
    abortInFlight();
    const local = new AbortController();
    controller = local;
    if (!silent) loading.value = true;
    try {
      const payload = await api.status(local.signal);
      if (local.signal.aborted) return;
      apply(payload);
      lastError.value = "";
    } catch (error) {
      if (local.signal.aborted || !visible.value) return;
      lastError.value = errorMessage(error);
      if (!silent) {
        events.onToast?.("无法读取远程访问状态", lastError.value, "error");
      }
    } finally {
      if (controller === local) {
        controller = null;
        if (!silent) loading.value = false;
      }
      schedulePoll();
    }
  }

  async function runAction(
    key: string,
    call: (signal: AbortSignal) => Promise<RemoteAccessStatusWire>,
    successTitle: string,
    failureTitle: string,
  ): Promise<boolean> {
    if (busy.value) return false;
    clearPoll();
    abortInFlight();
    const local = new AbortController();
    controller = local;
    action.value = key;
    try {
      const payload = await call(local.signal);
      if (local.signal.aborted) return false;
      apply(payload);
      lastError.value = "";
      events.onToast?.(successTitle, status.value?.message || "操作已完成。", "success");
      return true;
    } catch (error) {
      if (local.signal.aborted) return false;
      lastError.value = errorMessage(error);
      events.onToast?.(failureTitle, lastError.value, "error");
      return false;
    } finally {
      if (controller === local) controller = null;
      action.value = "";
      schedulePoll();
    }
  }

  function setVisible(next: boolean): void {
    if (visible.value === next) return;
    visible.value = next;
    if (next) {
      void load();
      return;
    }
    clearPoll();
    abortInFlight();
    loading.value = false;
  }

  function refresh(): Promise<void> {
    return load(false);
  }

  function enable(): Promise<boolean> {
    return runAction(
      "enable",
      (signal) => api.update({ enabled: true }, signal),
      "远程访问已开启",
      "无法开启远程访问",
    );
  }

  function disable(): Promise<boolean> {
    return runAction(
      "disable",
      (signal) => api.update({ enabled: false }, signal),
      "远程访问已关闭",
      "无法关闭远程访问",
    );
  }

  function retry(): Promise<boolean> {
    return runAction(
      "retry",
      (signal) => api.start(signal),
      "远程访问正在重连",
      "无法启动远程访问",
    );
  }

  function importConfig(path: string): Promise<boolean> {
    const trimmed = path.trim();
    if (!trimmed) {
      lastError.value = "请选择要导入的 FRPC TOML 配置文件。";
      events.onToast?.("无法导入 FRPC 配置", lastError.value, "error");
      return Promise.resolve(false);
    }
    return runAction(
      "import",
      (signal) => api.importConfig({ source_path: trimmed }, signal),
      "FRPC 配置已导入",
      "无法导入 FRPC 配置",
    );
  }

  function handleVisibilityChange(): void {
    if (document.hidden) {
      clearPoll();
      abortInFlight();
      loading.value = false;
      return;
    }
    if (visible.value) void load(true);
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleVisibilityChange);
    if (visible.value) void load();
  });
  onBeforeUnmount(() => {
    clearPoll();
    abortInFlight();
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  });

  return {
    status,
    loading,
    action,
    busy,
    lastError,
    visible,
    setVisible,
    load,
    refresh,
    enable,
    disable,
    retry,
    importConfig,
  };
}
