import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import { lanAccessApi } from "./lanApi";
import type {
  LanAccessApi,
  LanAccessStatus,
  LanAccessStatusWire,
  LanAccessUpdate,
  LanDevice,
  LanHostOption,
  LanPairing,
} from "./lanTypes";

const POLL_INTERVAL_MS = 2_000;

interface LanAccessEvents {
  onToast?: (title: string, message: string, kind: "info" | "success" | "error") => void;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function integer(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function hostOptions(value: unknown): LanHostOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const address = text(raw.address);
    if (!address) return [];
    return [{ address, label: text(raw.label) || address }];
  });
}

function pairings(value: unknown): LanPairing[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const id = text(raw.id);
    if (!id) return [];
    return [{
      id,
      deviceName: text(raw.device_name) || "Android 设备",
      code: text(raw.code),
      expiresAt: text(raw.expires_at),
      status: text(raw.status) || "pending",
    }];
  });
}

function device(value: unknown): LanDevice | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const deviceId = text(raw.device_id);
  if (!deviceId) return null;
  return {
    deviceId,
    deviceName: text(raw.device_name) || "Android 设备",
    pairedAt: text(raw.paired_at),
    lastSeenAt: text(raw.last_seen_at),
  };
}

export function normalizeLanAccessStatus(raw: LanAccessStatusWire): LanAccessStatus {
  return {
    enabled: raw.enabled === true,
    running: raw.running === true,
    bindHost: text(raw.bind_host),
    port: integer(raw.port, 38_522),
    displayName: text(raw.display_name) || "Zvec 图片库",
    discoveryPort: integer(raw.discovery_port, 38_521),
    address: text(raw.address),
    availableHosts: hostOptions(raw.available_hosts),
    pendingPairings: pairings(raw.pending_pairings),
    device: device(raw.device),
    message: text(raw.message),
  };
}

export function useLanAccess(
  suppliedApi?: LanAccessApi,
  events: LanAccessEvents = {},
  autoLoad = true,
) {
  const api = suppliedApi ?? lanAccessApi;
  const status = ref<LanAccessStatus | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  const action = ref("");
  const lastError = ref("");
  const draft = reactive({
    enabled: false,
    bindHost: "",
    port: 38_522,
    displayName: "Zvec 图片库",
  });
  let controller: AbortController | null = null;
  let pollHandle: number | null = null;

  const busy = computed(() => loading.value || saving.value || Boolean(action.value));

  function apply(payload: LanAccessStatusWire): LanAccessStatus {
    const normalized = normalizeLanAccessStatus(payload);
    status.value = normalized;
    draft.enabled = normalized.enabled;
    draft.bindHost = normalized.bindHost || normalized.availableHosts[0]?.address || "";
    draft.port = normalized.port;
    draft.displayName = normalized.displayName;
    schedulePoll();
    return normalized;
  }

  function schedulePoll(): void {
    if (pollHandle !== null) window.clearTimeout(pollHandle);
    pollHandle = null;
    if (!status.value?.running) return;
    pollHandle = window.setTimeout(() => void load(true), POLL_INTERVAL_MS);
  }

  async function load(silent = false): Promise<void> {
    controller?.abort();
    const localController = new AbortController();
    controller = localController;
    if (!silent) loading.value = true;
    try {
      apply(await api.status(localController.signal));
      lastError.value = "";
    } catch (error) {
      if (localController.signal.aborted) return;
      lastError.value = error instanceof Error ? error.message : String(error);
      if (!silent) events.onToast?.("局域网状态读取失败", lastError.value, "error");
      schedulePoll();
    } finally {
      if (controller === localController) {
        controller = null;
        if (!silent) loading.value = false;
      }
    }
  }

  function updateBody(): LanAccessUpdate {
    const port = Number(draft.port);
    if (!draft.bindHost.trim()) throw new Error("请选择一个局域网 IPv4 地址。");
    if (!Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new Error("端口必须是 1 到 65535 之间的整数。");
    }
    if (!draft.displayName.trim()) throw new Error("设备名称不能为空。");
    return {
      enabled: Boolean(draft.enabled),
      bind_host: draft.bindHost.trim(),
      port,
      display_name: draft.displayName.trim(),
    };
  }

  async function save(): Promise<boolean> {
    saving.value = true;
    try {
      apply(await api.update(updateBody()));
      lastError.value = "";
      events.onToast?.("局域网设置已保存", "新设置已写入本机。", "success");
      return true;
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error);
      events.onToast?.("局域网设置无效", lastError.value, "error");
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function runAction(
    key: string,
    call: (signal: AbortSignal) => Promise<LanAccessStatusWire>,
    successTitle: string,
  ): Promise<void> {
    if (action.value) return;
    action.value = key;
    const local = new AbortController();
    try {
      apply(await call(local.signal));
      lastError.value = "";
      events.onToast?.(successTitle, status.value?.message || "操作已完成。", "success");
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error);
      events.onToast?.("局域网操作失败", lastError.value, "error");
    } finally {
      action.value = "";
    }
  }

  async function start(): Promise<void> {
    if (!(await save())) return;
    await runAction("start", (signal) => api.start(signal), "局域网访问已开启");
  }

  const stop = () => runAction("stop", (signal) => api.stop(signal), "局域网访问已停止");
  const approve = (id: string) =>
    runAction(`approve:${id}`, (signal) => api.approve(id, signal), "Android 设备已允许");
  const reject = (id: string) =>
    runAction(`reject:${id}`, (signal) => api.reject(id, signal), "配对请求已拒绝");
  const revokeDevice = () =>
    runAction("revoke", (signal) => api.revokeDevice(signal), "Android 设备已断开");

  onMounted(() => {
    if (autoLoad) void load();
  });
  onBeforeUnmount(() => {
    controller?.abort();
    if (pollHandle !== null) window.clearTimeout(pollHandle);
  });

  return {
    status,
    draft,
    loading,
    saving,
    action,
    busy,
    lastError,
    load,
    save,
    start,
    stop,
    approve,
    reject,
    revokeDevice,
  };
}
