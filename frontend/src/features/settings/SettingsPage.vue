<script setup lang="ts">
import { ref } from "vue";

import {
  SearchLearningSettingsCard,
  type SearchLearningApi,
} from "../search-learning";
import DataMigrationSection from "./DataMigrationSection.vue";
import LanAccessSection from "./LanAccessSection.vue";
import type { LanAccessApi } from "./lanTypes";
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
  lanApi?: LanAccessApi;
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
const confirmingExit = ref(false);
const exiting = ref(false);
const activeSection = ref<
  "libraries" | "models" | "credentials" | "learning" | "lan" | "migration" | "application"
>("libraries");

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

async function exitApplication(): Promise<void> {
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.exit_application !== "function") {
    confirmingExit.value = false;
    emit("toast", "无法退出 YaoLens", "桌面桥接尚未就绪，请稍后重试。", "error");
    return;
  }
  if (exiting.value) return;
  exiting.value = true;
  try {
    const result = await bridge.exit_application();
    if (!result || result.ok !== true) {
      throw new Error(result?.error || result?.message || "后台服务暂时无法安全退出。");
    }
  } catch (error) {
    confirmingExit.value = false;
    emit(
      "toast",
      "无法退出 YaoLens",
      error instanceof Error ? error.message : String(error),
      "error",
    );
  } finally {
    exiting.value = false;
  }
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

    <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分组">
        <button type="button" :class="{ active: activeSection === 'libraries' }" @click="activeSection = 'libraries'">
          <span>图库与路径</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'models' }" @click="activeSection = 'models'">
          <span>模型角色</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'credentials' }" @click="activeSection = 'credentials'">
          <span>安全凭据</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'lan' }" @click="activeSection = 'lan'">
          <span>局域网访问</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'learning' }" @click="activeSection = 'learning'">
          <span>搜索学习</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'migration' }" @click="activeSection = 'migration'">
          <span>数据迁移</span>
        </button>
        <button type="button" :class="{ active: activeSection === 'application' }" @click="activeSection = 'application'">
          <span>应用</span>
        </button>
      </nav>

      <div class="settings-content">

    <section v-show="activeSection === 'libraries'" class="settings-card panel libraries-card" aria-labelledby="libraries-title">
      <header class="section-heading">
         <div><p class="eyebrow">本地图库</p><h2 id="libraries-title">图库与路径</h2></div>
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
        <span class="results-form-label">全局搜索结果目录</span>
        <input
          v-model="settings.resultsDirectory.value"
          name="results_directory"
          required
          autocomplete="off"
          spellcheck="false"
          placeholder="D:\YaoLens\results"
        />
        <button
          class="path-picker"
          type="button"
          :disabled="Boolean(choosingDirectory)"
          @click="chooseResultsDirectory"
        >
          {{ choosingDirectory === "results" ? "选择中…" : "选择" }}
        </button>
        <button class="button secondary" type="submit" :disabled="settings.savingResults.value">
          {{ settings.savingResults.value ? "保存中…" : "单独保存结果目录" }}
        </button>
        <small class="results-form-hint">所有图库共用；必须是 Windows 绝对路径。</small>
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
                placeholder="D:\YaoLens\workspace"
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
          <div class="library-auto-index">
            <label class="toggle-option">
              <input v-model="draft.autoIndexEnabled" name="auto_index_enabled" type="checkbox" />
              <span><strong>自动增量索引与标注</strong><small>检测到新图片时自动索引并智能标注，无需手动触发。需重启软件后生效。</small></span>
            </label>
          </div>
        </form>
      </div>
      <p class="save-status" aria-live="polite">{{ settings.saveStatus.value }}</p>
    </section>

    <DataMigrationSection
      v-show="activeSection === 'migration'"
      :api="props.migrationApi"
      :config-path="settings.configPath.value"
      :libraries="settings.libraries.value"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
      @completed="settings.load"
    />

    <LanAccessSection
      v-show="activeSection === 'lan'"
      :api="props.lanApi"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
    />

    <SearchLearningSettingsCard
      v-show="activeSection === 'learning'"
      :active="activeSection === 'learning'"
      :api="props.learningApi"
      @toast="(title, message, kind) => emit('toast', title, message, kind)"
    />

    <div v-show="activeSection === 'models' || activeSection === 'credentials'" class="settings-grid">
      <section v-show="activeSection === 'models'" class="settings-card panel" aria-labelledby="models-title">
        <header class="section-heading">
          <div><p class="eyebrow">阿里云模型</p><h2 id="models-title">模型角色</h2></div>
          <span class="status-pill success">{{ settings.models.provider || "aliyun" }}</span>
        </header>
        <p class="section-copy">
          配置只影响后续新任务，不会重新计算已经保存的图片向量；运行中的后台需重启后加载，并发仍受模型服务速率限制。
        </p>
        <form class="model-form" @submit.prevent="settings.saveModels">
          <fieldset class="model-role-group">
            <legend>向量角色</legend>
            <div class="model-role-fields">
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
                <span>向量并发</span>
                <select
                  v-model.number="settings.models.embeddingConcurrency"
                  name="embedding_concurrency"
                  required
                >
                  <option
                    v-for="value in settings.modelConcurrencyOptions"
                    :key="`embedding-${value}`"
                    :value="value"
                  >{{ value }} 路</option>
                </select>
                <small>所有图库统一使用，不按单个任务覆盖；每个请求最多处理 5 张图片。</small>
              </label>
            </div>
          </fieldset>
          <fieldset class="model-role-group">
            <legend>智能标注角色</legend>
            <div class="model-role-fields">
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
              <label>
                <span>智能标注并发</span>
                <select
                  v-model.number="settings.models.autoTagConcurrency"
                  name="auto_tag_concurrency"
                  required
                >
                  <option
                    v-for="value in settings.modelConcurrencyOptions"
                    :key="`auto-tag-${value}`"
                    :value="value"
                  >{{ value }} 路</option>
                </select>
                <small>所有图库统一使用，主模型与疑难升级模型共享，不按单个任务覆盖。</small>
              </label>
            </div>
          </fieldset>
          <button class="button primary" type="submit" :disabled="settings.savingModels.value">
            {{ settings.savingModels.value ? "保存中…" : "保存模型配置" }}
          </button>
        </form>
      </section>

      <section v-show="activeSection === 'credentials'" class="settings-card panel credentials-card" aria-labelledby="credentials-title">
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
      <section
        v-show="activeSection === 'application'"
        class="settings-card panel application-card"
        aria-labelledby="application-title"
      >
        <header class="section-heading">
          <div><p class="eyebrow">运行方式</p><h2 id="application-title">应用</h2></div>
        </header>
        <p class="section-copy">
          关闭主窗口后，YaoLens 会继续在后台运行，正在处理的任务和局域网访问不会中断。
        </p>
        <div class="application-action">
          <div>
            <strong>退出 YaoLens</strong>
            <small>后台空闲时会关闭本地服务；存在活动任务时将拒绝退出。</small>
          </div>
          <div class="application-action-buttons">
            <template v-if="confirmingExit">
              <button
                class="button danger"
                type="button"
                :disabled="exiting"
                @click="exitApplication"
              >
                {{ exiting ? "退出中…" : "确认退出" }}
              </button>
              <button
                class="button secondary"
                type="button"
                :disabled="exiting"
                @click="confirmingExit = false"
              >
                取消
              </button>
            </template>
            <button
              v-else
              class="button danger"
              type="button"
              @click="confirmingExit = true"
            >
              退出 YaoLens
            </button>
          </div>
        </div>
      </section>
    </div>
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
.results-form { display: grid; grid-template-columns: 1fr auto auto; gap: 8px 12px; align-items: center; margin-top: 14px; padding: 12px; border-radius: 12px; background: #f5f4ff; }
.results-form-label { grid-column: 1 / -1; color: #626b82; font-size: 12px; font-weight: 700; }
.results-form-hint { grid-column: 1 / -1; color: #8a91a2; font-size: 12px; font-weight: 500; }
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
.library-auto-index { margin-top: 10px; }
.save-status { min-height: 18px; margin: 10px 2px 0; color: #68718a; font-size: 12px; }
.settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; }
.model-form { display: grid; gap: 11px; margin-top: 14px; }
.model-role-group { display: grid; min-width: 0; gap: 8px; margin: 0; padding: 0; border: 0; }
.model-role-group + .model-role-group { padding-top: 12px; border-top: 1px solid var(--border); }
.model-role-group legend { padding: 0; color: var(--text); font-size: 13px; font-weight: 700; }
.model-role-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.model-form .button { justify-self: start; }
.credential-form { display: grid; grid-template-columns: minmax(220px,1fr) auto; gap: 10px; align-items: end; margin-top: 14px; }
.credential-note { margin-top: 14px; padding: 11px; border-radius: 10px; background: #f7f8fc; color: #6f788e; font-size: 12px; }
.application-action { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 16px; padding: 12px 0; border-top: 1px solid #e7e7e7; }
.application-action > div:first-child { display: grid; gap: 4px; }
.application-action small { color: #777; }
.application-action-buttons { display: flex; align-items: center; gap: 8px; }
.empty-state { display: grid; place-items: center; gap: 5px; min-height: 150px; color: #7d8598; text-align: center; }
.empty-state span { font-size: 12px; }

/* The standalone desktop preview is the visual source of truth: settings are
   grouped in a quiet left rail instead of stacking every feature at once. */
.settings-page { height:100%; min-height:0; grid-template-rows:minmax(0,1fr); gap:0; overflow:hidden; color:#242424; }
.page-heading{display:contents}.page-heading>div{display:none}.page-heading>button{position:fixed;z-index:20;right:48px;top:9px;min-height:34px}
.settings-layout { display:grid; height:100%; min-height:0; grid-template-columns:142px minmax(0,1fr); gap:16px; }
.settings-nav { display:grid; min-height:0; align-content:start; gap:2px; padding-right:10px; border-right:1px solid #e5e5e5; }
.settings-nav button { display:flex; min-height:38px; align-items:center; gap:8px; padding:8px; border:0; border-radius:8px; color:#707070; font:inherit; text-align:left; background:transparent; }
.settings-nav button:hover,.settings-nav button.active { color:#171717; background:#f2f2f2; }
.settings-content { min-width:0; min-height:0; overflow:auto; padding:0 4px 20px 0; scrollbar-gutter:stable; }
.settings-card { padding:0; }
.settings-grid{grid-template-columns:1fr}
.panel { border:0; border-radius:0; background:#fff; box-shadow:none; }
.section-heading { padding-bottom:10px; border-bottom:1px solid #e7e7e7; }
.section-heading h2 { color:#242424; font-size:18px; font-weight:600; }
.eyebrow { display:none; }
.section-copy { color:#777; }
.button { min-height:36px; border:1px solid #dedede; border-radius:9px; color:#333; background:#f3f3f3; font-weight:500; transform:none !important; }
.button.primary { color:#fff; border-color:#606060; background:#606060; }
.button.secondary { color:#333; border-color:#dedede; background:#f3f3f3; }
.button.danger { color:#992f3d; border-color:#e6c6ca; background:#fff2f3; }
.path-picker { border-color:#dedede; color:#444; background:#f5f5f5; font-weight:500; }
.results-form { grid-template-columns: 1fr auto auto; }
.results-form,.library-editor,.new-library-editor,.toggle-option,.credential-note { border-color:#e5e5e5; background:#fff; box-shadow:none; }
.results-form { padding:12px 0; border-radius:0; border-bottom:1px solid #e7e7e7; }
.library-editor { padding:13px 0; border-width:0 0 1px; border-radius:0; }
.status-pill { color:#555; background:#f0f0f0; }
.settings-content :deep(.panel),.settings-content :deep(.lan-card),.settings-content :deep(.migration-card) { border:0; border-radius:0; background:#fff; box-shadow:none; }
.settings-content :deep(.eyebrow) { display:none; }
.settings-content :deep(.button.primary) { color:#fff; border-color:#606060; background:#606060; }
.settings-content :deep(.button.secondary) { color:#333; border-color:#dedede; background:#f3f3f3; }
.settings-content :deep(input:focus),.settings-content :deep(select:focus),.settings-content :deep(textarea:focus) { border-color:#9d9d9d; box-shadow:0 0 0 3px rgb(0 0 0 / 7%); }
@media (max-width: 1100px) {
  .settings-grid { grid-template-columns: 1fr; }
  .library-fields { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  .settings-layout { grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); }
  .settings-nav { display:flex; padding:0 0 8px; overflow:auto; border-right:0; border-bottom:1px solid #e5e5e5; }
  .settings-nav button { flex:0 0 auto; justify-content:center; white-space:nowrap; }
  .page-heading, .library-editor footer, .credential-note, .application-action { align-items: stretch; flex-direction: column; }
  .model-role-fields { grid-template-columns: 1fr; }
  .results-form, .credential-form, .library-options, .library-options.single-option { grid-template-columns: 1fr; }
  .section-actions { align-items: flex-end; flex-direction: column; }
}
@media (prefers-color-scheme: dark) {
  .application-action { border-color: #3f3f3f; }
  .application-action small { color: #b8b8b8; }
  .button.danger { color: #ffd8dd; border-color: #713f47; background: #492b30; }
}
</style>
