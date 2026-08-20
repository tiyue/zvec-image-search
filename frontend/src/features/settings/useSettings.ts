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

const MODEL_CONCURRENCY_OPTIONS = [1, 2, 4, 6] as const;

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
    autoIndexEnabled: raw.auto_index_enabled === true,
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

function modelConcurrency(value: unknown, fallback: number): number {
  return typeof value === "number"
    && MODEL_CONCURRENCY_OPTIONS.includes(value as (typeof MODEL_CONCURRENCY_OPTIONS)[number])
    ? value
    : fallback;
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
  const configPath = ref("");
  const modelCatalog = shallowRef<ModelChoice[]>([]);
  const models = reactive({
    provider: "aliyun",
    embedding: "",
    primary: "",
    escalation: "",
    embeddingConcurrency: 2,
    autoTagConcurrency: 4,
  });
  const credentialsConfigured = ref(false);
  const credentialsPersistent = ref(false);
  const loading = ref(false);
  const savingResults = ref(false);
  const savingModels = ref(false);
  const savingCredentials = ref(false);
  const deletingCredentials = ref(false);
  const folderTagBlacklistText = ref("");
  const folderTagRuleRevision = ref("");
  const folderTagUsingDefaults = ref(false);
  const savingFolderTagSettings = ref(false);
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
    models.embeddingConcurrency = modelConcurrency(
      source.embedding_concurrency,
      models.embeddingConcurrency,
    );
    models.autoTagConcurrency = modelConcurrency(
      source.auto_tag_concurrency,
      models.autoTagConcurrency,
    );
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
    configPath.value = text(payload.config_path, configPath.value);
    applyModels(payload);
    const credentialState = payload.credentials ?? {};
    credentialsConfigured.value = Boolean(
      credentialState.configured ?? payload.credentials_configured,
    );
    credentialsPersistent.value = credentialState.persistent === true;
  }

  function applyFolderTagSettings(payload: {
    blacklist?: unknown;
    revision?: unknown;
    using_defaults?: unknown;
  }): void {
    folderTagBlacklistText.value = stringArray(payload.blacklist).join("\n");
    folderTagRuleRevision.value = text(payload.revision);
    folderTagUsingDefaults.value = payload.using_defaults === true;
  }

  async function load(): Promise<boolean> {
    loading.value = true;
    controller?.abort();
    controller = new AbortController();
    try {
      const payload = await api.settings(controller.signal);
      applySettings(payload);
      if (api.folderNameTagSettings) {
        applyFolderTagSettings(
          await api.folderNameTagSettings(controller.signal),
        );
      }
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
        return `${label}必须是 Windows 绝对路径，例如 D:\\YaoLens\\data。`;
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
        autoIndexEnabled: fallbackDraft.autoIndexEnabled,
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
        auto_index_enabled: draft.autoIndexEnabled,
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
      notify("搜索结果目录无效", "请输入 Windows 绝对路径，例如 D:\\YaoLens\\results。", "error");
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
    if (
      !MODEL_CONCURRENCY_OPTIONS.includes(
        models.embeddingConcurrency as (typeof MODEL_CONCURRENCY_OPTIONS)[number],
      )
      || !MODEL_CONCURRENCY_OPTIONS.includes(
        models.autoTagConcurrency as (typeof MODEL_CONCURRENCY_OPTIONS)[number],
      )
    ) {
      notify("模型并发无效", "向量并发和智能标注并发必须选择 1、2、4 或 6。", "error");
      return false;
    }
    if (savingModels.value) return false;
    savingModels.value = true;
    try {
      const payload = await api.updateModels({
        embedding_model: models.embedding,
        auto_tag_primary_model: models.primary,
        auto_tag_escalation_model: models.escalation,
        embedding_concurrency: models.embeddingConcurrency,
        auto_tag_concurrency: models.autoTagConcurrency,
      });
      applyModels(payload);
      lastError.value = "";
      const restart = payload.restart ?? {};
      const requiresRestart = payload.restart_required === true || restart.required === true;
      notify(
        "模型配置已保存",
        requiresRestart
          ? restart.active_jobs === true
            ? "活动任务全部完成并重启 YaoLens 后生效，不会重算已有向量。"
            : "重启 YaoLens 后生效，不会重算已有向量。"
          : "将用于后续新任务，不会重算已有向量。",
        "success",
      );
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

  async function saveFolderTagSettings(): Promise<boolean> {
    if (!api.updateFolderNameTagSettings || savingFolderTagSettings.value) {
      return false;
    }
    const values: string[] = [];
    const seen = new Set<string>();
    for (const line of folderTagBlacklistText.value.split(/\r?\n/u)) {
      const value = line.normalize("NFKC").trim().replace(/\s+/gu, " ");
      if (!value) continue;
      const key = value.toLocaleLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        values.push(value);
      }
    }
    if (!values.length || values.length > 256) {
      notify("黑名单无效", "请保留 1 至 256 条非空规则，每行一条。", "error");
      return false;
    }
    savingFolderTagSettings.value = true;
    try {
      const payload = await api.updateFolderNameTagSettings(values);
      applyFolderTagSettings(payload);
      lastError.value = "";
      notify(
        "标签黑名单已保存",
        "新规则立即用于后续预览、重建和继承候选；正在运行的文件夹标签任务继续使用启动时快照。",
        "success",
      );
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法保存标签黑名单", lastError.value, "error");
      return false;
    } finally {
      savingFolderTagSettings.value = false;
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
    configPath,
    modelCatalog,
    models,
    modelConcurrencyOptions: MODEL_CONCURRENCY_OPTIONS,
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
    folderTagBlacklistText,
    folderTagRuleRevision,
    folderTagUsingDefaults,
    savingFolderTagSettings,
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
    saveFolderTagSettings,
  };
}
