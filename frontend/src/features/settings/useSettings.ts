import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from "vue";

import { settingsApi } from "./api";
import type {
  LibraryDraft,
  ModelChoice,
  ModelChoiceWire,
  ModelSettingsWire,
  NewLibraryDraft,
  SettingsApi,
  SettingsLibrary,
  SettingsLibraryWire,
  SettingsResponse,
  ToastKind,
} from "./types";

interface SettingsEvents {
  onToast?: (title: string, message: string, kind: ToastKind) => void;
  onLibrariesUpdated?: (libraries: SettingsLibrary[]) => void;
  autoLoad?: boolean;
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? [...new Set(value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim()))]
    : [];
}

function normalizeLibrary(raw: SettingsLibraryWire, index = 0): SettingsLibrary {
  return {
    id: text(raw.id, `library-${index + 1}`),
    name: text(raw.name, `图库 ${index + 1}`),
    imageRoot: text(raw.image_root),
    workspaceDirectory: text(raw.workspace_directory),
    enabled: raw.enabled !== false,
    isDefault: raw.is_default === true,
  };
}

function draftFromLibrary(library: SettingsLibrary): LibraryDraft {
  return { ...library, saving: false };
}

function modelChoice(raw: ModelChoiceWire, index = 0): ModelChoice {
  return {
    id: text(raw.id, `model-${index + 1}`),
    displayName: text(raw.display_name, raw.id, `模型 ${index + 1}`),
    protocol: text(raw.protocol),
    roles: stringArray(raw.roles),
  };
}

function modelsFrom(payload: SettingsResponse | ModelSettingsWire): ModelSettingsWire {
  const response = payload as SettingsResponse;
  return response.models ?? response.model_settings ?? (payload as ModelSettingsWire);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isAbsoluteWindowsPath(value: string): boolean {
  const normalized = value.trim();
  return /^[a-z]:[\\/]/iu.test(normalized) || /^\\\\[^\\/]+[\\/][^\\/]+/u.test(normalized);
}

export function useSettings(api: SettingsApi = settingsApi, events: SettingsEvents = {}) {
  const libraries = shallowRef<SettingsLibrary[]>([]);
  const libraryDrafts = ref<LibraryDraft[]>([]);
  const newLibrary = reactive<NewLibraryDraft>({
    name: "",
    imageRoot: "",
    workspaceDirectory: "",
    enabled: true,
    saving: false,
  });
  const showAddLibrary = ref(false);
  const resultsDirectory = ref("");
  const modelCatalog = shallowRef<ModelChoice[]>([]);
  const models = reactive({
    provider: "aliyun",
    embedding: "",
    primary: "",
    escalation: "",
  });
  const credentialsConfigured = ref(false);
  const credentialsPersistent = ref(false);
  const loading = ref(false);
  const savingResults = ref(false);
  const savingModels = ref(false);
  const savingCredentials = ref(false);
  const deletingCredentials = ref(false);
  const lastError = ref("");
  const saveStatus = ref("");
  let controller: AbortController | null = null;

  const embeddingChoices = computed(() => choicesFor("embedding"));
  const primaryChoices = computed(() => choicesFor("auto_tag_primary"));
  const escalationChoices = computed(() => choicesFor("auto_tag_escalation"));

  function notify(title: string, message: string, kind: ToastKind): void {
    events.onToast?.(title, message, kind);
  }

  function choicesFor(role: string): ModelChoice[] {
    const roleChoices = modelCatalog.value.filter((choice) => choice.roles.includes(role));
    return roleChoices.length ? roleChoices : modelCatalog.value;
  }

  function applyModels(payload: SettingsResponse | ModelSettingsWire): void {
    const source = modelsFrom(payload);
    models.provider = text(source.provider, models.provider, "aliyun");
    models.embedding = text(source.embedding_model, source.embedding, models.embedding, "qwen3-vl-embedding");
    models.primary = text(source.auto_tag_primary_model, source.tagging_model, source.auto_tag_primary, models.primary, "qwen3-vl-flash");
    models.escalation = text(source.auto_tag_escalation_model, source.escalation_model, source.auto_tag_escalation, models.escalation, "qwen3-vl-plus");
    if (Array.isArray(source.catalog)) {
      modelCatalog.value = source.catalog
        .filter((item): item is ModelChoiceWire => Boolean(item) && typeof item === "object")
        .map(modelChoice)
        .filter((item) => item.id);
    }
  }

  function applyLibraries(rawLibraries: SettingsLibraryWire[] | undefined): void {
    if (!rawLibraries) return;
    libraries.value = rawLibraries.map(normalizeLibrary);
    libraryDrafts.value = libraries.value.map(draftFromLibrary);
    if (libraries.value.length === 0) showAddLibrary.value = true;
    events.onLibrariesUpdated?.(libraries.value);
  }

  function applySettings(payload: SettingsResponse): void {
    applyLibraries(payload.libraries);
    resultsDirectory.value = text(payload.results_directory, resultsDirectory.value);
    applyModels(payload);
    const credentialState = payload.credentials ?? {};
    credentialsConfigured.value = Boolean(
      credentialState.configured ?? payload.credentials_configured,
    );
    credentialsPersistent.value = credentialState.persistent === true;
  }

  async function load(): Promise<boolean> {
    loading.value = true;
    controller?.abort();
    controller = new AbortController();
    try {
      const payload = await api.settings(controller.signal);
      applySettings(payload);
      lastError.value = "";
      saveStatus.value = "设置已刷新";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      lastError.value = errorMessage(error);
      notify("无法读取设置", lastError.value, "error");
      return false;
    } finally {
      loading.value = false;
    }
  }

  function validateDraft(draft: LibraryDraft, includeResults = true): string {
    return validateLibraryFields(
      draft.name,
      draft.imageRoot,
      draft.workspaceDirectory,
      draft.enabled,
      draft.isDefault,
      includeResults,
    );
  }

  function validateLibraryFields(
    name: string,
    imageRoot: string,
    workspaceDirectory: string,
    enabled: boolean,
    isDefault: boolean,
    includeResults: boolean,
  ): string {
    if (!name.trim()) return "图库名称不能为空。";
    const paths: [string, string][] = [
      ["图片目录", imageRoot],
      ["向量工作区", workspaceDirectory],
    ];
    if (includeResults && resultsDirectory.value.trim()) {
      paths.push(["搜索结果目录", resultsDirectory.value]);
    }
    for (const [label, value] of paths) {
      if (!isAbsoluteWindowsPath(value)) {
        return `${label}必须是 Windows 绝对路径，例如 D:\\Zvec\\data。`;
      }
    }
    if (isDefault && !enabled) return "默认图库必须保持启用。";
    return "";
  }

  function resetNewLibrary(): void {
    newLibrary.name = "";
    newLibrary.imageRoot = "";
    newLibrary.workspaceDirectory = "";
    newLibrary.enabled = true;
    newLibrary.saving = false;
  }

  function startAddLibrary(): void {
    showAddLibrary.value = true;
  }

  function cancelAddLibrary(): void {
    if (newLibrary.saving) return;
    resetNewLibrary();
    showAddLibrary.value = false;
  }

  function applyUpdateResponse(payload: SettingsResponse, fallbackDraft?: LibraryDraft): void {
    if (payload.libraries) applyLibraries(payload.libraries);
    else if (fallbackDraft) {
      const updatedLibrary: SettingsLibrary = {
        id: fallbackDraft.id,
        name: fallbackDraft.name,
        imageRoot: fallbackDraft.imageRoot,
        workspaceDirectory: fallbackDraft.workspaceDirectory,
        enabled: fallbackDraft.enabled,
        isDefault: fallbackDraft.isDefault,
      };
      libraries.value = libraries.value.map((item) =>
        item.id === fallbackDraft.id ? updatedLibrary : item,
      );
      events.onLibrariesUpdated?.(libraries.value);
    }
    resultsDirectory.value = text(payload.results_directory, resultsDirectory.value);
    const restart = payload.restart ?? {};
    if (restart.active_jobs === true) {
      saveStatus.value = "设置已保存；当前任务完成并重启软件后生效。";
    } else if (payload.restart_required === true || restart.required === true) {
      saveStatus.value = "设置已保存；重启软件后新路径生效。";
    } else {
      saveStatus.value = "设置已保存并可用于后续操作。";
    }
  }

  async function saveLibrary(draft: LibraryDraft): Promise<boolean> {
    const validation = validateDraft(draft);
    if (validation) {
      notify("图库设置无效", validation, "error");
      return false;
    }
    if (draft.saving) return false;
    draft.saving = true;
    saveStatus.value = `正在保存 ${draft.name}…`;
    try {
      const payload = await api.updateLibrary(draft.id, {
        name: draft.name.trim(),
        image_root: draft.imageRoot.trim(),
        workspace_directory: draft.workspaceDirectory.trim(),
        enabled: draft.enabled,
        is_default: draft.isDefault,
        results_directory: resultsDirectory.value.trim(),
      });
      applyUpdateResponse(payload, draft);
      lastError.value = "";
      notify("图库设置已保存", saveStatus.value, "success");
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      saveStatus.value = `保存失败：${lastError.value}`;
      notify("无法保存图库设置", lastError.value, "error");
      return false;
    } finally {
      const current = libraryDrafts.value.find((item) => item.id === draft.id);
      if (current) current.saving = false;
      draft.saving = false;
    }
  }

  async function addLibrary(): Promise<boolean> {
    const validation = validateLibraryFields(
      newLibrary.name,
      newLibrary.imageRoot,
      newLibrary.workspaceDirectory,
      newLibrary.enabled,
      false,
      libraries.value.length === 0,
    );
    if (validation) {
      notify("图库设置无效", validation, "error");
      return false;
    }
    if (newLibrary.saving) return false;
    newLibrary.saving = true;
    saveStatus.value = `正在新增 ${newLibrary.name.trim()}…`;
    try {
      const firstLibrary = libraries.value.length === 0;
      const results = resultsDirectory.value.trim();
      const payload = await api.addLibrary({
        name: newLibrary.name.trim(),
        image_root: newLibrary.imageRoot.trim(),
        workspace_directory: newLibrary.workspaceDirectory.trim(),
        enabled: newLibrary.enabled,
        ...(firstLibrary && results ? { results_directory: results } : {}),
      });
      applyUpdateResponse(payload);
      lastError.value = "";
      resetNewLibrary();
      showAddLibrary.value = false;
      notify("图库已新增", saveStatus.value, "success");
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      saveStatus.value = `新增失败：${lastError.value}`;
      notify("无法新增图库", lastError.value, "error");
      return false;
    } finally {
      newLibrary.saving = false;
    }
  }

  async function saveResults(): Promise<boolean> {
    const target = libraryDrafts.value.find((item) => item.isDefault) ?? libraryDrafts.value[0];
    if (!target) {
      notify("尚未配置图库", "请先完成首次图库设置。", "error");
      return false;
    }
    if (!isAbsoluteWindowsPath(resultsDirectory.value)) {
      notify("搜索结果目录无效", "请输入 Windows 绝对路径，例如 D:\\Zvec\\results。", "error");
      return false;
    }
    if (savingResults.value) return false;
    savingResults.value = true;
    try {
      const payload = await api.updateLibrary(target.id, {
        results_directory: resultsDirectory.value.trim(),
      });
      applyUpdateResponse(payload);
      lastError.value = "";
      notify("搜索结果目录已保存", saveStatus.value, "success");
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法保存搜索结果目录", lastError.value, "error");
      return false;
    } finally {
      savingResults.value = false;
    }
  }

  async function saveModels(): Promise<boolean> {
    if (!models.embedding || !models.primary || !models.escalation) {
      notify("模型配置不完整", "请为三个模型角色分别选择一个模型。", "error");
      return false;
    }
    if (savingModels.value) return false;
    savingModels.value = true;
    try {
      const payload = await api.updateModels({
        embedding_model: models.embedding,
        auto_tag_primary_model: models.primary,
        auto_tag_escalation_model: models.escalation,
      });
      applyModels(payload);
      lastError.value = "";
      notify("模型配置已保存", "只影响后续新任务，不会重算已有向量。", "success");
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法保存模型配置", lastError.value, "error");
      return false;
    } finally {
      savingModels.value = false;
    }
  }

  async function saveCredentials(apiKeyValue: string): Promise<boolean> {
    const apiKey = apiKeyValue.trim();
    if (!apiKey) {
      notify("请输入 API Key", "页面不会读取或显示已经保存的密钥。", "error");
      return false;
    }
    if (savingCredentials.value) return false;
    savingCredentials.value = true;
    try {
      const payload = await api.saveCredentials(apiKey);
      credentialsConfigured.value = payload.configured !== false;
      credentialsPersistent.value = payload.persistent === true;
      lastError.value = "";
      notify("密钥已安全保存", "输入框已经清空，密钥不会在页面中回显。", "success");
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法保存密钥", lastError.value, "error");
      return false;
    } finally {
      savingCredentials.value = false;
    }
  }

  async function deleteCredentials(): Promise<boolean> {
    if (deletingCredentials.value || !credentialsConfigured.value) return false;
    deletingCredentials.value = true;
    try {
      const payload = await api.deleteCredentials();
      credentialsConfigured.value = false;
      credentialsPersistent.value = payload.persistent === true;
      lastError.value = "";
      notify(
        "密钥已删除",
        payload.restart_required ? "正在运行的后端会在软件重启后清除会话密钥。" : "后续联网任务需要重新配置密钥。",
        "success",
      );
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法删除密钥", lastError.value, "error");
      return false;
    } finally {
      deletingCredentials.value = false;
    }
  }

  onMounted(() => {
    if (events.autoLoad !== false) void load();
  });
  onBeforeUnmount(() => controller?.abort());

  return {
    libraries,
    libraryDrafts,
    newLibrary,
    showAddLibrary,
    resultsDirectory,
    modelCatalog,
    models,
    embeddingChoices,
    primaryChoices,
    escalationChoices,
    credentialsConfigured,
    credentialsPersistent,
    loading,
    savingResults,
    savingModels,
    savingCredentials,
    deletingCredentials,
    lastError,
    saveStatus,
    load,
    validateDraft,
    startAddLibrary,
    cancelAddLibrary,
    addLibrary,
    saveLibrary,
    saveResults,
    saveModels,
    saveCredentials,
    deleteCredentials,
  };
}
