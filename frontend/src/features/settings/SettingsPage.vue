<script setup lang="ts">
import { ref } from "vue";

import {
  SearchLearningSettingsCard,
  type SearchLearningApi,
} from "../search-learning";
import DataMigrationSection from "./DataMigrationSection.vue";
import type { DataMigrationApi } from "./migrationTypes";
import type {
  LibraryDraft,
  ModelChoice,
  SettingsApi,
  SettingsLibrary,
  ToastKind,
} from "./types";
import { useSettings } from "./useSettings";

const props = withDefaults(defineProps<{
  api?: SettingsApi;
  migrationApi?: DataMigrationApi;
  learningApi?: SearchLearningApi;
  autoLoad?: boolean;
}>(), {
  autoLoad: true,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
  "libraries-updated": [libraries: SettingsLibrary[]];
}>();

const settings = useSettings(props.api, {
  autoLoad: props.autoLoad,
  onToast: (title, message, kind) => emit("toast", title, message, kind),
  onLibrariesUpdated: (libraries) => emit("libraries-updated", libraries),
});
const apiKey = ref("");
const choosingDirectory = ref("");

function selectDefault(libraryId: string): void {
  settings.libraryDrafts.value.forEach((draft) => {
    draft.isDefault = draft.id === libraryId;
    if (draft.isDefault) draft.enabled = true;
  });
}

function containsChoice(choices: ModelChoice[], modelId: string): boolean {
  return choices.some((choice) => choice.id === modelId);
}

async function submitCredentials(): Promise<void> {
  const secret = apiKey.value;
  // Clear before awaiting so a failed request never leaves a secret visible.
  apiKey.value = "";
  await settings.saveCredentials(secret);
}

function libraryStatus(draft: LibraryDraft): string {
  if (!draft.enabled) return "已停用";
  return draft.isDefault ? "默认 · 已启用" : "已启用";
}

async function chooseDirectory(
  key: string,
  apply: (path: string) => void,
): Promise<void> {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_directory !== "function") {
    emit("toast", "无法选择文件夹", "桌面桥接尚未就绪，请稍后重试。", "error");
    return;
  }
  if (choosingDirectory.value) return;
  choosingDirectory.value = key;
  try {
    const result = await bridge.select_directory();
    if (!result || result.ok !== true) {
      throw new Error(result?.error || result?.message || "文件夹选择失败。");
    }
    if (result.path) apply(result.path);
  } catch (error) {
    emit(
      "toast",
      "无法选择文件夹",
      error instanceof Error ? error.message : String(error),
      "error",
    );
  } finally {
    choosingDirectory.value = "";
  }
}

function chooseLibraryPath(
  draft: { imageRoot: string; workspaceDirectory: string },
  field: "imageRoot" | "workspaceDirectory",
  key: string,
): void {
  void chooseDirectory(key, (path) => {
    draft[field] = path;
  });
}

function chooseResultsDirectory(): void {
  void chooseDirectory("results", (path) => {
    settings.resultsDirectory.value = path;
  });
}
</script>

<template>
  <section class="settings-page" aria-labelledby="settings-title">
    <header class="page-heading">
      <div>
        <p class="eyebrow">本地配置</p>
        <h1 id="settings-title">设置</h1>
        <p>编辑图库路径、阿里云模型角色和安全凭据。</p>
      </div>
      <button class="button secondary" type="button" :disabled="settings.loading.value" @click="settings.load">
        {{ settings.loading.value ? "刷新中…" : "刷新设置" }}
      </button>
    </header>

    <section class="settings-card panel libraries-card" aria-labelledby="libraries-title">
      <header class="section-heading">
        <div><p class="eyebrow">图库与路径</p><h2 id="libraries-title">已配置图库</h2></div>
        <div class="section-actions">
          <span class="status-pill">{{ settings.libraryDrafts.value.length }} 个</span>
          <button
            class="button secondary"
            type="button"
            :disabled="settings.loading.value || settings.newLibrary.saving"
            @click="settings.startAddLibrary"
          >
            ＋ 新增图库
          </button>
        </div>
      </header>
      <p class="section-copy">所有字段都可以修改。路径变化可能需要在当前任务结束后重启软件。</p>

      <form class="results-form" @submit.prevent="settings.saveResults">
        <label>
          <span>全局搜索结果目录</span>
          <span class="path-control">
            <input
              v-model="settings.resultsDirectory.value"
              name="results_directory"
              required
              autocomplete="off"
              spellcheck="false"
              placeholder="D:\Zvec\results"
            />
            <button
              class="path-picker"
              type="button"
              :disabled="Boolean(choosingDirectory)"
              @click="chooseResultsDirectory"
            >
              {{ choosingDirectory === "results" ? "选择中…" : "选择" }}
            </button>
          </span>
          <small>所有图库共用；必须是 Windows 绝对路径。</small>
        </label>
        <button class="button secondary" type="submit" :disabled="settings.savingResults.value">
          {{ settings.savingResults.value ? "保存中…" : "单独保存结果目录" }}
        </button>
      </form>

      <form
        v-if="settings.showAddLibrary.value"
        class="library-editor new-library-editor"
        aria-label="新增图库"
        @submit.prevent="settings.addLibrary"
      >
        <header>
          <div>
            <strong>新增图库</strong>
            <small>设置原图目录和独立向量工作区；不会移动或重新编码原图。</small>
          </div>
          <span class="status-pill">新配置</span>
        </header>
        <div class="library-fields">
          <label>
            <span>图库名称</span>
            <input
              v-model="settings.newLibrary.name"
              name="new_name"
              required
              maxlength="128"
              autocomplete="off"
              placeholder="例如：人物写真"
            />
          </label>
          <label>
            <span>图片目录</span>
            <span class="path-control">
              <input
                v-model="settings.newLibrary.imageRoot"
                name="new_image_root"
                required
                autocomplete="off"
                spellcheck="false"
                placeholder="D:\Pictures"
              />
              <button
                class="path-picker"
                type="button"
                :disabled="Boolean(choosingDirectory)"
                @click="chooseLibraryPath(settings.newLibrary, 'imageRoot', 'new-image-root')"
              >
                {{ choosingDirectory === "new-image-root" ? "选择中…" : "选择" }}
              </button>
            </span>
          </label>
          <label>
            <span>向量工作区</span>
            <span class="path-control">
              <input
                v-model="settings.newLibrary.workspaceDirectory"
                name="new_workspace_directory"
                required
                autocomplete="off"
                spellcheck="false"
                placeholder="D:\Zvec\workspace"
              />
              <button
                class="path-picker"
                type="button"
                :disabled="Boolean(choosingDirectory)"
                @click="chooseLibraryPath(settings.newLibrary, 'workspaceDirectory', 'new-workspace')"
              >
                {{ choosingDirectory === "new-workspace" ? "选择中…" : "选择" }}
              </button>
            </span>
          </label>
        </div>
        <div class="library-options single-option">
          <label class="toggle-option">
            <input v-model="settings.newLibrary.enabled" name="new_enabled" type="checkbox" />
            <span><strong>新增后立即启用</strong><small>首个图库会自动成为默认图库。</small></span>
          </label>
        </div>
        <footer>
          <span>已有后端正在运行时，保存不会中断任务，重启软件后载入新图库。</span>
          <span class="footer-actions">
            <button class="button secondary" type="button" :disabled="settings.newLibrary.saving" @click="settings.cancelAddLibrary">
              取消
            </button>
            <button class="button primary" type="submit" :disabled="settings.newLibrary.saving">
              {{ settings.newLibrary.saving ? "新增中…" : "保存并新增" }}
            </button>
          </span>
        </footer>
      </form>

      <div v-if="settings.loading.value && !settings.libraryDrafts.value.length" class="empty-state" role="status">
        正在读取图库设置…
      </div>
      <div v-else-if="!settings.libraryDrafts.value.length" class="empty-state" role="status">
        <strong>尚未配置图库</strong>
        <span>请使用上方“新增图库”直接完成首次配置。</span>
      </div>
      <div v-else class="library-list">
        <form
          v-for="draft in settings.libraryDrafts.value"
          :key="draft.id"
          class="library-editor"
          :data-library-id="draft.id"
          @submit.prevent="settings.saveLibrary(draft)"
        >
          <header>
            <div>
              <strong>{{ draft.name || "未命名图库" }}</strong>
              <small>图库编号：{{ draft.id }}</small>
            </div>
            <span class="status-pill" :class="draft.enabled ? 'success' : ''">{{ libraryStatus(draft) }}</span>
          </header>
          <div class="library-fields">
            <label>
              <span>图库名称</span>
              <input v-model="draft.name" name="name" required maxlength="128" autocomplete="off" />
            </label>
            <label>
              <span>图片目录</span>
              <span class="path-control">
                <input v-model="draft.imageRoot" name="image_root" required autocomplete="off" spellcheck="false" />
                <button
                  class="path-picker"
                  type="button"
                  :disabled="Boolean(choosingDirectory)"
                  @click="chooseLibraryPath(draft, 'imageRoot', `${draft.id}-image-root`)"
                >
                  {{ choosingDirectory === `${draft.id}-image-root` ? "选择中…" : "选择" }}
                </button>
              </span>
            </label>
            <label>
              <span>向量工作区</span>
              <span class="path-control">
                <input v-model="draft.workspaceDirectory" name="workspace_directory" required autocomplete="off" spellcheck="false" />
                <button
                  class="path-picker"
                  type="button"
                  :disabled="Boolean(choosingDirectory)"
                  @click="chooseLibraryPath(draft, 'workspaceDirectory', `${draft.id}-workspace`)"
                >
                  {{ choosingDirectory === `${draft.id}-workspace` ? "选择中…" : "选择" }}
                </button>
              </span>
            </label>
          </div>
          <div class="library-options">
            <label class="toggle-option">
              <input v-model="draft.enabled" name="enabled" type="checkbox" :disabled="draft.isDefault" />
              <span><strong>启用此图库</strong><small>停用后不会参与默认搜索和新任务。</small></span>
            </label>
            <label class="toggle-option">
              <input
                name="default_library"
                type="radio"
                :checked="draft.isDefault"
                @change="selectDefault(draft.id)"
              />
              <span><strong>设为默认图库</strong><small>智能整理和默认任务优先使用这个图库。</small></span>
            </label>
          </div>
          <footer>
            <span>保存时会同时写入上方的全局结果目录。</span>
            <button class="button primary" type="submit" :disabled="draft.saving">
              {{ draft.saving ? "保存中…" : "保存图库设置" }}
            </button>
          </footer>
        </form>
      </div>
      <p class="save-status" aria-live="polite">{{ settings.saveStatus.value }}</p>
    </section>

    <DataMigrationSection
      :api="props.migrationApi"
      :config-path="settings.configPath.value"
      :libraries="settings.libraries.value"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
      @completed="settings.load"
    />

    <SearchLearningSettingsCard
      :api="props.learningApi"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
    />

    <div class="settings-grid">
      <section class="settings-card panel" aria-labelledby="models-title">
        <header class="section-heading">
          <div><p class="eyebrow">阿里云模型</p><h2 id="models-title">模型角色</h2></div>
          <span class="status-pill success">{{ settings.models.provider || "aliyun" }}</span>
        </header>
        <p class="section-copy">配置只影响后续新任务，不会重新计算已经保存的图片向量。</p>
        <form class="model-form" @submit.prevent="settings.saveModels">
          <label>
            <span>图片与文字向量</span>
            <select v-model="settings.models.embedding" name="embedding_model" required>
              <option
                v-if="settings.models.embedding && !containsChoice(settings.embeddingChoices.value, settings.models.embedding)"
                :value="settings.models.embedding"
              >{{ settings.models.embedding }}</option>
              <option v-for="choice in settings.embeddingChoices.value" :key="choice.id" :value="choice.id">
                {{ choice.displayName }}
              </option>
            </select>
            <small>用于建立索引和语义检索。</small>
          </label>
          <label>
            <span>智能标注主模型</span>
            <select v-model="settings.models.primary" name="auto_tag_primary_model" required>
              <option
                v-if="settings.models.primary && !containsChoice(settings.primaryChoices.value, settings.models.primary)"
                :value="settings.models.primary"
              >{{ settings.models.primary }}</option>
              <option v-for="choice in settings.primaryChoices.value" :key="choice.id" :value="choice.id">
                {{ choice.displayName }}
              </option>
            </select>
            <small>常规标签识别，默认使用低成本模型。</small>
          </label>
          <label>
            <span>疑难升级模型</span>
            <select v-model="settings.models.escalation" name="auto_tag_escalation_model" required>
              <option
                v-if="settings.models.escalation && !containsChoice(settings.escalationChoices.value, settings.models.escalation)"
                :value="settings.models.escalation"
              >{{ settings.models.escalation }}</option>
              <option v-for="choice in settings.escalationChoices.value" :key="choice.id" :value="choice.id">
                {{ choice.displayName }}
              </option>
            </select>
            <small>只在置信度不足或身份冲突时使用。</small>
          </label>
          <button class="button primary" type="submit" :disabled="settings.savingModels.value">
            {{ settings.savingModels.value ? "保存中…" : "保存模型配置" }}
          </button>
        </form>
      </section>

      <section class="settings-card panel credentials-card" aria-labelledby="credentials-title">
        <header class="section-heading">
          <div><p class="eyebrow">安全凭据</p><h2 id="credentials-title">DashScope API Key</h2></div>
          <span class="status-pill" :class="settings.credentialsConfigured.value ? 'success' : ''">
            {{ settings.credentialsConfigured.value ? "已配置" : "未配置" }}
          </span>
        </header>
        <p class="section-copy">密钥只写入本机安全存储。页面和设置接口都不会读取、遮罩或回显旧密钥。</p>
        <form class="credential-form" @submit.prevent="submitCredentials">
          <label>
            <span>输入新的 API Key</span>
            <input
              v-model="apiKey"
              name="api_key"
              type="password"
              autocomplete="new-password"
              required
              placeholder="sk-…"
            />
          </label>
          <button class="button primary" type="submit" :disabled="settings.savingCredentials.value">
            {{ settings.savingCredentials.value ? "保存中…" : "保存新密钥" }}
          </button>
        </form>
        <div class="credential-note">
          <span>{{ settings.credentialsPersistent.value ? "密钥保存在 Windows 凭据管理器中。" : "当前环境可能只支持会话级保存。" }}</span>
          <button
            class="button danger"
            type="button"
            :disabled="!settings.credentialsConfigured.value || settings.deletingCredentials.value"
            @click="settings.deleteCredentials"
          >
            {{ settings.deletingCredentials.value ? "删除中…" : "删除已保存密钥" }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.settings-page { display: grid; gap: 14px; color: #17203a; }
.page-heading, .section-heading, .library-editor header, .library-editor footer, .credential-note { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-heading { align-items: flex-end; padding: 2px 2px 4px; }
.page-heading h1, .section-heading h2 { margin: 2px 0 0; line-height: 1.15; }
.page-heading h1 { font-size: clamp(22px, 2vw, 30px); }
.page-heading p, .section-copy { margin: 6px 0 0; color: #68718a; }
.eyebrow { margin: 0 !important; color: #6557e8 !important; font-size: 12px; font-weight: 800; letter-spacing: .08em; }
.panel { background: rgba(255,255,255,.96); border: 1px solid #e2e6f0; border-radius: 16px; box-shadow: 0 8px 24px rgba(44,54,90,.07); }
.settings-card { padding: 16px; }
.section-heading h2 { font-size: 18px; }
.section-actions, .footer-actions { display: flex; align-items: center; gap: 8px; }
.status-pill { display: inline-flex; border-radius: 999px; padding: 5px 9px; background: #f0f1f6; color: #626b80; font-size: 12px; font-weight: 800; white-space: nowrap; }
.status-pill.success { background: #e9f8f0; color: #237552; }
.button { border: 0; border-radius: 9px; padding: 9px 13px; font: inherit; font-weight: 750; cursor: pointer; transition: transform .12s ease, opacity .12s ease; }
.button:hover:not(:disabled) { transform: translateY(-1px); }
.button:disabled { cursor: not-allowed; opacity: .48; }
.primary { background: linear-gradient(135deg, #7364eb, #5b4bd6); color: white; }
.secondary { background: #efedff; color: #5647ce; }
.danger { background: #fff0f1; color: #be3b4b; }
input, select { width: 100%; min-width: 0; box-sizing: border-box; border: 1px solid #d9deea; border-radius: 9px; background: #fbfcff; color: #17203a; padding: 9px 10px; font: inherit; outline: none; }
input:focus, select:focus { border-color: #796cf0; box-shadow: 0 0 0 3px rgba(121,108,240,.12); }
.path-control { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 7px; }
.path-picker { border: 1px solid #d8d4ff; border-radius: 9px; background: #f0eeff; color: #5748cf; padding: 0 12px; font: inherit; font-weight: 800; cursor: pointer; }
.path-picker:hover:not(:disabled) { background: #e8e4ff; }
.path-picker:disabled { cursor: not-allowed; opacity: .5; }
label { display: grid; gap: 5px; color: #626b82; font-size: 12px; font-weight: 700; }
label small { color: #8a91a2; font-weight: 500; }
.results-form { display: grid; grid-template-columns: minmax(280px,1fr) auto; gap: 12px; align-items: end; margin-top: 14px; padding: 12px; border-radius: 12px; background: #f5f4ff; }
.library-list { display: grid; gap: 11px; margin-top: 13px; }
.library-editor { padding: 13px; border: 1px solid #dfe3ec; border-radius: 13px; background: #fbfcff; }
.new-library-editor { margin-top: 13px; border-color: #bfb7ff; background: linear-gradient(145deg, #fbfaff, #f5f7ff); box-shadow: 0 10px 26px rgba(90,73,205,.08); }
.library-editor header > div { display: grid; gap: 3px; }
.library-editor header small, .library-editor footer span { color: #68758b; font-size: 12px; }
.library-fields { display: grid; grid-template-columns: minmax(160px,.55fr) minmax(260px,1fr) minmax(260px,1fr); gap: 10px; margin-top: 12px; }
.library-options { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 9px; margin-top: 10px; }
.library-options.single-option { grid-template-columns: minmax(260px, .5fr); }
.toggle-option { display: flex; align-items: center; gap: 9px; padding: 10px; border: 1px solid #e3e6ee; border-radius: 10px; background: white; }
.toggle-option input { width: 17px; height: 17px; margin: 0; padding: 0; }
.toggle-option span { display: grid; gap: 2px; }
.library-editor footer { margin-top: 11px; padding-top: 10px; border-top: 1px solid #e7eaf1; }
.save-status { min-height: 18px; margin: 10px 2px 0; color: #68718a; font-size: 12px; }
.settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; }
.model-form { display: grid; gap: 11px; margin-top: 14px; }
.model-form .button { justify-self: start; }
.credential-form { display: grid; grid-template-columns: minmax(220px,1fr) auto; gap: 10px; align-items: end; margin-top: 14px; }
.credential-note { margin-top: 14px; padding: 11px; border-radius: 10px; background: #f7f8fc; color: #6f788e; font-size: 12px; }
.empty-state { display: grid; place-items: center; gap: 5px; min-height: 150px; color: #7d8598; text-align: center; }
.empty-state span { font-size: 12px; }
@media (max-width: 1100px) {
  .settings-grid { grid-template-columns: 1fr; }
  .library-fields { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  .page-heading, .library-editor footer, .credential-note { align-items: stretch; flex-direction: column; }
  .results-form, .credential-form, .library-options, .library-options.single-option { grid-template-columns: 1fr; }
  .section-actions { align-items: flex-end; flex-direction: column; }
}
</style>
