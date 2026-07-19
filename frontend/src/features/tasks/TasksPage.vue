<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import {
  ActivityLogTable,
  JobHistoryTable,
  useActivityCenter,
} from "../activity";

import type {
  AutoTagEstimateSubmission,
  AutoTagScope,
  JobSubmission,
  LibraryTaskType,
  TaskLibrary,
} from "./types";
import { useJobs } from "./useJobs";

const props = withDefaults(
  defineProps<{
    libraries?: TaskLibrary[];
    visible?: boolean;
  }>(),
  {
    libraries: () => [],
    visible: true,
  },
);

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "success" | "error"];
}>();

const taskType = ref<LibraryTaskType>("index");
const libraryId = ref("");
const maxImages = ref(300);
const autoTagScope = ref<AutoTagScope>("untagged");
const maxBudgetCny = ref("");
const externalProcessingConfirmed = ref(false);
const allScopeConfirmed = ref(false);
const notice = ref("");
const noticeKind = ref<"success" | "error">("success");

const enabledLibraries = computed(() => props.libraries.filter((library) => library.enabled !== false));
const isAiTask = computed(
  () => taskType.value === "index_and_auto_tag" || taskType.value === "auto_tag",
);
const isAutoTagTask = computed(() => taskType.value === "auto_tag");
const executionPolicy = computed(() => {
  if (taskType.value === "auto_tag") {
    return "智能标注使用 auto_tag_concurrency，并受 60 RPM 与 100,000 TPM 限流；单图失败会隔离并继续。";
  }
  if (taskType.value === "index_and_auto_tag") {
    return "扫描、向量和标注分别使用 scan_concurrency、embedding_concurrency 与 auto_tag_concurrency；单图失败会隔离并继续。";
  }
  return "扫描与向量请求使用 scan_concurrency 和 embedding_concurrency；单图失败会隔离并继续。";
});
const requiresExternalConfirmation = isAiTask;
const requiresAllScopeConfirmation = computed(
  () => isAutoTagTask.value && autoTagScope.value === "all",
);

const jobsState = useJobs(undefined, {
  autoStart: false,
  onError: (title, message) => {
    noticeKind.value = "error";
    notice.value = `${title}：${message}`;
    emit("toast", title, message, "error");
  },
  onInfo: (title, message) => {
    noticeKind.value = "success";
    notice.value = `${title}：${message}`;
    emit("toast", title, message, "success");
  },
});

const activityState = useActivityCenter(undefined, {
  autoStart: false,
  onError: (title, message) => {
    noticeKind.value = "error";
    notice.value = `${title}：${message}`;
    emit("toast", title, message, "error");
  },
  onInfo: (title, message) => {
    noticeKind.value = "success";
    notice.value = `${title}：${message}`;
    emit("toast", title, message, "success");
  },
});

let activityInitialized = false;

async function setActivityVisible(visible: boolean): Promise<void> {
  activityState.setVisible(visible);
  if (!visible) return;
  if (!activityInitialized) {
    activityInitialized = true;
    activityState.startPolling();
  }
  await activityState.refreshAll();
}

watch(
  enabledLibraries,
  (libraries) => {
    if (!libraries.some((library) => library.id === libraryId.value)) {
      libraryId.value = libraries[0]?.id ?? "";
    }
  },
  { immediate: true },
);

watch(taskType, (type) => {
  externalProcessingConfirmed.value = false;
  allScopeConfirmed.value = false;
  jobsState.autoTagEstimate.value = null;
  if (type !== "auto_tag") autoTagScope.value = "untagged";
});

watch(autoTagScope, (scope) => {
  if (scope !== "all") allScopeConfirmed.value = false;
  jobsState.autoTagEstimate.value = null;
});

watch(
  () => props.visible,
  (visible) => void setActivityVisible(visible),
  { immediate: true },
);

async function submit(): Promise<void> {
  notice.value = "";
  const request: JobSubmission = {
    task_type: taskType.value,
    library_id: libraryId.value,
  };
  if (isAiTask.value) {
    request.max_images = maxImages.value;
    const budget = Number(maxBudgetCny.value);
    if (maxBudgetCny.value.trim()) request.max_budget_cny = budget;
  }
  if (isAutoTagTask.value) request.scope = autoTagScope.value;
  if (requiresExternalConfirmation.value && externalProcessingConfirmed.value) {
    request.external_processing_confirmed = true;
  }
  if (requiresAllScopeConfirmation.value && allScopeConfirmed.value) {
    request.all_scope_confirmed = true;
  }
  const submitted = await jobsState.submitJob(request);
  if (submitted) {
    externalProcessingConfirmed.value = false;
    allScopeConfirmed.value = false;
    await Promise.all([
      activityState.loadJobs({ quiet: true }),
      activityState.loadLogs({ quiet: true }),
    ]);
  }
}

async function estimate(): Promise<void> {
  notice.value = "";
  const request: AutoTagEstimateSubmission = {
    task_type: "auto_tag_estimate",
    library_id: libraryId.value,
    scope: autoTagScope.value,
    max_images: maxImages.value,
  };
  const budget = Number(maxBudgetCny.value);
  if (maxBudgetCny.value.trim()) request.max_budget_cny = budget;
  await jobsState.estimateAutoTag(request);
}

onBeforeUnmount(() => {
  activityState.stopPolling();
});
</script>

<template>
  <section class="tasks-page" aria-labelledby="tasks-title">
    <div class="summary-grid" aria-label="任务处理策略">
      <article>
        <span class="summary-icon is-violet" aria-hidden="true">⇄</span>
        <div><small>执行方式</small><strong>受控并发</strong></div>
        <p>按模型限额调度，避免窗口卡死</p>
      </article>
      <article>
        <span class="summary-icon is-green" aria-hidden="true">✓</span>
        <div><small>任务控制</small><strong>安全取消</strong></div>
        <p>仅停止所选任务，不影响其他队列</p>
      </article>
      <article>
        <span class="summary-icon is-amber" aria-hidden="true">!</span>
        <div><small>失败处理</small><strong>错误图片隔离</strong></div>
        <p>单张失败会记录，剩余图片继续处理</p>
      </article>
    </div>

    <section class="composer panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">新建任务</p>
          <h2 id="tasks-title">并行处理图库</h2>
          <p>索引、同步和智能标注可同时排队，由本地后端统一控制速度。</p>
        </div>
        <span class="limit-chip">60 RPM · 100,000 TPM</span>
      </div>

      <form class="task-form" novalidate @submit.prevent="submit">
        <label>
          <span>任务类型</span>
          <select v-model="taskType" name="task_type">
            <option value="index">建立索引</option>
            <option value="sync">同步图库</option>
            <option value="index_and_auto_tag">索引并智能标注</option>
            <option value="auto_tag">智能标注</option>
          </select>
        </label>
        <label>
          <span>目标图库</span>
          <select v-model="libraryId" name="library_id" :disabled="enabledLibraries.length === 0">
            <option v-if="enabledLibraries.length === 0" value="">暂无已启用图库</option>
            <option v-for="library in enabledLibraries" :key="library.id" :value="library.id">
              {{ library.name }}
            </option>
          </select>
        </label>
        <label v-if="isAutoTagTask">
          <span>标注范围</span>
          <select v-model="autoTagScope" name="auto_tag_scope">
            <option value="untagged">未标注或源图片已变化（推荐）</option>
            <option value="latest_index_run">最近一次索引新增图片</option>
            <option value="failed">仅重试可恢复失败</option>
            <option value="failed_all">重试全部失败</option>
            <option value="all">全部图片重新处理</option>
          </select>
        </label>
        <label v-if="isAiTask">
          <span>本批图片上限</span>
          <input v-model.number="maxImages" name="max_images" type="number" min="1" max="10000" />
          <small>范围 1–10,000</small>
        </label>
        <label v-if="isAiTask">
          <span>本批预算上限（元）</span>
          <input
            v-model="maxBudgetCny"
            name="max_budget_cny"
            type="number"
            min="0.01"
            step="0.01"
            placeholder="留空表示不额外限制"
          />
          <small>使用设置页当前标注模型</small>
        </label>

        <div class="task-policy-note" data-testid="task-execution-policy">
          <strong>并发与失败处理由后端运行配置统一控制</strong>
          <small>{{ executionPolicy }}</small>
        </div>

        <label v-if="requiresAllScopeConfirmation" class="risk-field">
          <input v-model="allScopeConfirmed" name="all_scope_confirmed" type="checkbox" />
          <span>
            <strong>我确认要重新处理全部图片</strong>
            <small>此范围可能绕过已有当前标注并产生较高费用，请先使用估算。</small>
          </span>
        </label>

        <label v-if="requiresExternalConfirmation" class="consent-field">
          <input v-model="externalProcessingConfirmed" name="external_processing_confirmed" type="checkbox" />
          <span>
            <strong>我确认图片会发送至阿里云大模型</strong>
            <small>智能标注涉及外部处理，必须逐次明确确认。</small>
          </span>
        </label>

        <div class="form-actions">
          <button
            v-if="isAutoTagTask"
            class="estimate-button"
            type="button"
            :disabled="jobsState.estimating.value || jobsState.submitting.value || enabledLibraries.length === 0"
            @click="estimate"
          >
            {{ jobsState.estimating.value ? "正在估算…" : "估算费用" }}
          </button>
          <button
            class="primary-button"
            type="submit"
            :disabled="jobsState.submitting.value || jobsState.estimating.value || enabledLibraries.length === 0"
          >
            {{ jobsState.submitting.value ? "正在提交…" : "加入任务队列" }}
          </button>
        </div>
      </form>

      <section v-if="jobsState.autoTagEstimate.value" class="estimate-result" aria-live="polite">
        <header>
          <strong>智能标注估算</strong>
          <span :class="{ warning: jobsState.autoTagEstimate.value.overBudget }">
            {{ jobsState.autoTagEstimate.value.overBudget ? "超过预算" : "预算范围内" }}
          </span>
        </header>
        <dl>
          <div><dt>候选图片</dt><dd>{{ jobsState.autoTagEstimate.value.candidateCount }}</dd></div>
          <div><dt>去重图片</dt><dd>{{ jobsState.autoTagEstimate.value.uniqueImageCount }}</dd></div>
          <div><dt>缓存命中</dt><dd>{{ jobsState.autoTagEstimate.value.cachedCount }}</dd></div>
          <div><dt>预计请求</dt><dd>{{ jobsState.autoTagEstimate.value.apiRequestCount }}</dd></div>
          <div><dt>输入 Token</dt><dd>{{ jobsState.autoTagEstimate.value.estimatedInputTokens }}</dd></div>
          <div><dt>输出 Token</dt><dd>{{ jobsState.autoTagEstimate.value.estimatedOutputTokens }}</dd></div>
          <div>
            <dt>预计费用</dt>
            <dd>
              {{ jobsState.autoTagEstimate.value.estimatedCostCny === null
                ? "—"
                : `¥${jobsState.autoTagEstimate.value.estimatedCostCny.toFixed(4)}` }}
            </dd>
          </div>
        </dl>
        <p>估算只读取本地索引、标注状态和缓存，不会发送图片，也不会授予执行同意。</p>
      </section>

      <p
        v-if="notice"
        class="notice"
        :class="`is-${noticeKind}`"
        :role="noticeKind === 'error' ? 'alert' : 'status'"
      >
        {{ notice }}
      </p>
    </section>

    <div class="workspace-grid">
      <JobHistoryTable
        :items="activityState.jobs.value"
        :filters="activityState.jobFilters.value"
        :libraries="enabledLibraries"
        :loading="activityState.jobsLoading.value"
        :page="activityState.jobPage.value"
        :total-count="activityState.jobTotalCount.value"
        :has-previous="activityState.hasPreviousJobs.value"
        :has-next="activityState.jobHasMore.value"
        :selected-job-id="activityState.selectedJobId.value"
        :cancelling-job-id="activityState.cancellingJobId.value"
        :expanded-error-job-id="activityState.expandedErrorJobId.value"
        :error-logs="activityState.jobErrorLogs.value"
        :errors-loading="activityState.jobErrorsLoading.value"
        :errors-error="activityState.jobErrorsError.value"
        :error-has-more="activityState.jobErrorHasMore.value"
        @apply-filters="activityState.applyJobFilters"
        @refresh="activityState.loadJobs()"
        @previous="activityState.previousJobs()"
        @next="activityState.nextJobs()"
        @select="activityState.selectJob"
        @cancel="activityState.cancelJob"
        @inspect-errors="activityState.openJobErrors"
        @retry-errors="activityState.retryJobErrors()"
        @load-more-errors="activityState.loadMoreJobErrors()"
        @close-errors="activityState.closeJobErrors()"
      />

      <ActivityLogTable
        :items="activityState.logs.value"
        :filters="activityState.logFilters.value"
        :libraries="enabledLibraries"
        :loading="activityState.logsLoading.value"
        :page="activityState.logPage.value"
        :total-count="activityState.logTotalCount.value"
        :has-previous="activityState.hasPreviousLogs.value"
        :has-next="activityState.logHasMore.value"
        :following="activityState.followingLogs.value"
        :exporting="activityState.logsExporting.value"
        :selected-sequences="activityState.selectedLogSequences.value"
        :error-message="activityState.logsError.value"
        @apply-filters="activityState.applyLogFilters"
        @refresh="activityState.loadLogs()"
        @previous="activityState.previousLogs()"
        @next="activityState.nextLogs()"
        @following-change="activityState.setFollowing"
        @toggle-row="activityState.toggleLogSelection"
        @toggle-page="activityState.toggleCurrentLogPage"
        @copy="activityState.copySelectedLogs()"
        @export="activityState.exportLogs"
        @select-job="activityState.selectJob"
      />
    </div>
  </section>
</template>

<style scoped>
.tasks-page {
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-rows: auto auto minmax(0, 1fr);
  gap: 14px;
  overflow: hidden;
  color: var(--text, #171e2e);
  font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.summary-grid article {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 8px 10px;
  padding: 13px 14px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 15px;
  background: var(--surface, #fff);
  box-shadow: 0 6px 22px rgb(32 43 70 / 5%);
}

.summary-grid small,
.summary-grid strong {
  display: block;
}

.summary-grid small {
  color: var(--muted, #667085);
  font-size: 12px;
}

.summary-grid strong {
  margin-top: 1px;
  font-size: 13px;
}

.summary-grid p {
  grid-column: 2;
  margin: -5px 0 0;
  color: var(--muted, #667085);
  font-size: 12px;
}

.summary-icon {
  display: grid;
  width: 34px;
  height: 34px;
  grid-row: span 2;
  place-items: center;
  border-radius: 11px;
  font-size: 15px;
  font-weight: 800;
}

.summary-icon.is-violet { color: #6258e7; background: #f0efff; }
.summary-icon.is-green { color: #08755e; background: #eaf8f3; }
.summary-icon.is-amber { color: #9b6510; background: #fff5dc; }

.panel {
  min-width: 0;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 17px;
  background: var(--surface, #fff);
  box-shadow: 0 10px 35px rgb(32 43 70 / 6%);
}

.composer { padding: 16px; }

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.section-heading.is-compact { align-items: center; }
.section-heading h2,
.section-heading p { margin: 0; }
.section-heading h2 { font-size: 16px; line-height: 1.3; }
.section-heading > div > p:last-child {
  margin-top: 3px;
  color: var(--muted, #667085);
  font-size: 12px;
}

.eyebrow {
  margin-bottom: 3px !important;
  color: var(--brand, #6258e7) !important;
  font-size: 12px !important;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.limit-chip {
  flex: 0 0 auto;
  padding: 5px 8px;
  border-radius: 999px;
  color: #5650c8;
  background: #f0efff;
  font-size: 12px;
  font-weight: 750;
}

.task-form {
  display: grid;
  grid-template-columns: 1.1fr 1.1fr 0.75fr 0.85fr auto;
  align-items: end;
  gap: 10px;
  margin-top: 14px;
}

.task-form label:not(.consent-field) {
  display: grid;
  gap: 5px;
  min-width: 0;
  color: var(--muted, #596478);
  font-size: 13px;
  font-weight: 650;
}

.task-form label > small { font-size: 12px; font-weight: 500; }

.task-form select,
.task-form input[type="number"] {
  min-width: 0;
  height: 42px;
  padding: 0 10px;
  border: 1px solid var(--border, #dce2ec);
  border-radius: 10px;
  outline: none;
  color: var(--text, #171e2e);
  font: inherit;
  font-size: 14px;
  background: var(--surface, #fff);
}

.task-form select:focus,
.task-form input:focus {
  border-color: var(--brand, #6258e7);
  box-shadow: 0 0 0 3px rgb(98 88 231 / 12%);
}

.consent-field,
.risk-field,
.task-policy-note {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 11px;
  background: var(--surface-soft, #f8f9fc);
}

.consent-field {
  grid-column: 3 / -1;
  border-color: #eed99d;
  background: #fffaec;
}
.risk-field {
  grid-column: 1 / -1;
  border-color: #efb9a6;
  background: #fff2ed;
}
.task-policy-note {
  grid-column: 1 / 3;
  display: grid;
  align-content: center;
}

.consent-field input,
.risk-field input { accent-color: var(--brand, #6258e7); }
.consent-field span,
.risk-field span { min-width: 0; }
.consent-field strong,
.consent-field small,
.risk-field strong,
.risk-field small,
.task-policy-note strong,
.task-policy-note small { display: block; }
.consent-field strong,
.risk-field strong,
.task-policy-note strong { font-size: 13px; }
.consent-field small,
.risk-field small,
.task-policy-note small { margin-top: 2px; color: var(--muted, #667085); font-size: 12px; }

.primary-button,
.estimate-button {
  border: 0;
  border-radius: 10px;
  font: inherit;
  font-size: 13px;
  font-weight: 750;
  cursor: pointer;
}

.primary-button {
  height: 42px;
  padding: 0 15px;
  color: #fff;
  background: linear-gradient(135deg, #7267ed, #5a50dc);
  box-shadow: 0 7px 18px rgb(90 80 220 / 20%);
}
.form-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.estimate-button {
  height: 42px;
  padding: 0 14px;
  border: 1px solid #d7d4ff;
  color: #4f46b8;
  background: #f2f0ff;
}

.estimate-result {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid #dcd8ff;
  border-radius: 13px;
  background: #f8f7ff;
}
.estimate-result header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.estimate-result header span { color: #08755e; font-size: 12px; font-weight: 750; }
.estimate-result header span.warning { color: #a14d12; }
.estimate-result dl { display: grid; grid-template-columns: repeat(7, minmax(90px, 1fr)); gap: 8px; margin: 10px 0 0; }
.estimate-result dl > div { padding: 8px; border-radius: 9px; background: #fff; }
.estimate-result dt { color: var(--muted, #667085); font-size: 11px; }
.estimate-result dd { margin: 2px 0 0; font-size: 13px; font-weight: 750; }
.estimate-result p { margin: 9px 0 0; color: var(--muted, #667085); font-size: 11px; }

button:disabled { opacity: 0.55; cursor: default; }

.notice {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 10px;
  font-size: 12px;
}

.notice.is-success { color: #07624f; background: #ecf8f4; }
.notice.is-error { color: #9a303f; background: #fff0f2; }

.workspace-grid {
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-columns: minmax(0, 58fr) minmax(0, 42fr);
  gap: 12px;
  overflow: hidden;
}

@media (max-width: 1180px) {
  .workspace-grid {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(360px, 1fr) minmax(360px, 1fr);
    padding-right: 4px;
    overflow-y: auto;
    scrollbar-gutter: stable;
  }
}

@media (max-height: 780px) {
  .tasks-page {
    grid-template-rows: auto auto auto;
    padding-right: 4px;
    overflow-y: auto;
    scrollbar-gutter: stable;
  }

  .summary-grid article {
    padding-block: 10px;
  }

  .summary-grid p {
    display: none;
  }

  .composer {
    padding-block: 12px;
  }

  .workspace-grid {
    height: auto;
    min-height: 360px;
  }
}

@media (max-width: 1180px) and (max-height: 780px) {
  .workspace-grid {
    min-height: 732px;
  }
}

@media (max-width: 1050px) {
  .summary-grid { grid-template-columns: 1fr; }
  .task-form { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .consent-field { grid-column: 1 / -1; }
}

@media (max-width: 620px) {
  .task-form { grid-template-columns: 1fr; }
  .consent-field { grid-column: auto; }
  .section-heading { flex-direction: column; }
}
</style>
