<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { searchLearningApi } from "./api";
import type {
  SearchLearningApi,
  SearchLearningEvaluationMetrics,
  SearchLearningModelVersion,
  SearchLearningStatus,
} from "./types";

const props = withDefaults(defineProps<{ api?: SearchLearningApi }>(), {
  api: () => searchLearningApi,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "info" | "success" | "error"];
}>();

const status = ref<SearchLearningStatus | null>(null);
const busy = ref("");
const message = ref("");
const error = ref("");
let refreshTimer: number | null = null;

const readiness = computed(() => {
  const value = status.value;
  if (!value) return { sessions: 0, labels: 0, ready: false };
  const sessionCount = value.training_counts.query_sessions;
  const labelCount = value.training_counts.explicit_samples;
  const requiredSessions = Math.max(1, value.minimum_requirements.query_sessions);
  const requiredLabels = Math.max(1, value.minimum_requirements.explicit_samples);
  return {
    sessions: Math.min(100, Math.round((sessionCount / requiredSessions) * 100)),
    labels: Math.min(100, Math.round((labelCount / requiredLabels) * 100)),
    ready:
      sessionCount >= value.minimum_requirements.query_sessions &&
      labelCount >= value.minimum_requirements.explicit_samples &&
      value.training_counts.positive_samples > 0 &&
      value.training_counts.negative_samples > 0,
  };
});
const modelVersions = computed(() => status.value?.model_versions?.slice(0, 6) ?? []);
const activeModelVersion = computed(
  () => status.value?.settings.active_model_version ?? "",
);
const activeShadowMode = computed(() => status.value?.settings.shadow_mode ?? false);

type EvaluationMetricKey = keyof Pick<
  SearchLearningEvaluationMetrics,
  | "precision_at_15"
  | "recall_at_15"
  | "no_answer_false_positive_rate"
  | "cross_collection_bias"
  | "p95_latency_ms"
  | "external_api_calls"
>;

interface EvaluationMetricDefinition {
  key: EvaluationMetricKey;
  label: string;
  format: "percent" | "milliseconds" | "count";
}

interface EvaluationMetricRow extends EvaluationMetricDefinition {
  current: number;
  candidate: number;
}

const EVALUATION_METRICS: readonly EvaluationMetricDefinition[] = [
  { key: "precision_at_15", label: "Precision@15", format: "percent" },
  { key: "recall_at_15", label: "Recall@15", format: "percent" },
  {
    key: "no_answer_false_positive_rate",
    label: "无答案误报率",
    format: "percent",
  },
  {
    key: "cross_collection_bias",
    label: "跨图库偏置",
    format: "percent",
  },
  { key: "p95_latency_ms", label: "P95 延迟", format: "milliseconds" },
  { key: "external_api_calls", label: "额外 API 调用", format: "count" },
] as const;

const GATE_REASON_LABELS: Readonly<Record<string, string>> = {
  invalid_report: "固定评测报告格式无效。",
  fixed_evaluation_set_required: "评测报告不是已确认的固定评测集。",
  evaluation_set_id_required: "固定评测集缺少版本标识。",
  current_and_candidate_metrics_required: "评测报告缺少当前版本或候选版本指标。",
  precision_at_15_regressed: "Precision@15 低于当前版本。",
  recall_at_15_regressed_more_than_3pp: "Recall@15 下降超过 3 个百分点。",
  no_answer_false_positive_rate_above_10_percent: "无答案误报率超过 10%。",
  cross_collection_bias_regressed: "跨图库偏置高于当前版本。",
  invalid_latency_baseline: "当前版本的延迟基线无效。",
  p95_latency_increased_more_than_15_percent: "P95 延迟增加超过 15%。",
  external_api_calls_not_zero: "候选排序产生了额外 API 调用。",
  fixed_evaluator_unavailable: "尚未安装固定评测包，候选版本保持待评测。",
  fixed_evaluator_failed: "固定评测执行失败，请检查评测包后重新训练。",
  invalid_evaluation_report: "固定评测报告中的指标无效。",
};

function finiteMetric(
  metrics: SearchLearningEvaluationMetrics | undefined,
  key: EvaluationMetricKey,
): number | null {
  const value = metrics?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function evaluationRows(version: SearchLearningModelVersion): EvaluationMetricRow[] {
  const report = version.metrics?.metrics;
  return EVALUATION_METRICS.flatMap((definition) => {
    const current = finiteMetric(report?.current, definition.key);
    const candidate = finiteMetric(report?.candidate, definition.key);
    return current === null || candidate === null
      ? []
      : [{ ...definition, current, candidate }];
  });
}

function gateReasons(version: SearchLearningModelVersion): string[] {
  const reasons = version.metrics?.reasons;
  if (!Array.isArray(reasons)) return [];
  return reasons
    .filter((reason): reason is string => typeof reason === "string" && Boolean(reason.trim()))
    .map((reason) => GATE_REASON_LABELS[reason] ?? `未通过门槛：${reason}`);
}

function formatMetric(value: number, format: EvaluationMetricDefinition["format"]): string {
  if (format === "percent") return `${(value * 100).toFixed(1)}%`;
  if (format === "milliseconds") return `${value.toFixed(1)} ms`;
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatDelta(row: EvaluationMetricRow): string {
  const delta = row.candidate - row.current;
  const prefix = delta > 0 ? "+" : "";
  if (row.format === "percent") return `${prefix}${(delta * 100).toFixed(1)} 个百分点`;
  if (row.format === "milliseconds") return `${prefix}${delta.toFixed(1)} ms`;
  return `${prefix}${Number.isInteger(delta) ? delta : delta.toFixed(2)}`;
}

function comparisonSummary(version: SearchLearningModelVersion): string {
  const rows = evaluationRows(version);
  const precision = rows.find((row) => row.key === "precision_at_15");
  const recall = rows.find((row) => row.key === "recall_at_15");
  const setId = version.metrics?.metrics?.evaluation_set_id;
  const conclusion =
    version.gate_status === "passed"
      ? "全部质量门槛通过"
      : version.gate_status === "failed"
        ? `未通过 ${Math.max(1, gateReasons(version).length)} 项质量门槛`
        : "尚未完成固定评测";
  const changes = [
    precision ? `Precision@15 ${formatDelta(precision)}` : "",
    recall ? `Recall@15 ${formatDelta(recall)}` : "",
  ].filter(Boolean);
  return [setId ? `评测集 ${setId}` : "", conclusion, ...changes].filter(Boolean).join(" · ");
}

function currentMetricLabel(version: SearchLearningModelVersion): string {
  return version.metrics?.metrics?.current_model_version || "当前排序";
}

function candidateMetricLabel(version: SearchLearningModelVersion): string {
  return version.metrics?.metrics?.candidate_model_version || "候选排序";
}

function scheduleRefresh(): void {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
  refreshTimer = null;
  if (!status.value?.training_running) return;
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    void refresh();
  }, 1_500);
}

function failure(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

async function refresh(showFailureToast = false): Promise<void> {
  error.value = "";
  try {
    status.value = await props.api.status();
  } catch (reason) {
    error.value = failure(reason);
    if (showFailureToast) emit("toast", "无法读取搜索学习状态", error.value, "error");
  } finally {
    scheduleRefresh();
  }
}

async function updateSetting(
  field: "learning_enabled" | "implicit_feedback_enabled" | "save_query_text",
  value: boolean,
): Promise<void> {
  if (!status.value || busy.value) return;
  busy.value = field;
  error.value = "";
  try {
    status.value.settings = await props.api.updateSettings({ [field]: value });
    const labels = {
      learning_enabled: "本地搜索学习",
      implicit_feedback_enabled: "隐式反馈",
      save_query_text: "查询文字保存",
    };
    emit("toast", "设置已保存", `${labels[field]}已${value ? "开启" : "关闭"}。`, "success");
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "搜索学习设置未保存", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function train(): Promise<void> {
  if (busy.value) return;
  busy.value = "train";
  error.value = "";
  message.value = "";
  try {
    await props.api.train();
    message.value = "候选版本已进入本地后台训练；通过固定评测前不会改变搜索排序。";
    emit("toast", "训练已开始", message.value, "success");
    await refresh();
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法开始训练", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function installEvaluation(): Promise<void> {
  if (busy.value) return;
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge.select_json_file !== "function") {
    error.value = "桌面 JSON 文件选择器尚未就绪。";
    emit("toast", "无法导入固定评测包", error.value, "error");
    return;
  }
  busy.value = "evaluation";
  error.value = "";
  message.value = "";
  try {
    const selected = await bridge.select_json_file();
    if (!selected || selected.ok !== true) {
      throw new Error(selected?.error || selected?.message || "文件选择失败。");
    }
    if (!selected.path) return;
    await props.api.installEvaluation(selected.path);
    message.value = "固定评测包已校验并安装，后续候选训练会自动执行质量门禁。";
    emit("toast", "固定评测包已安装", message.value, "success");
    await refresh();
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法导入固定评测包", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function activate(modelVersion: string, shadowMode: boolean): Promise<void> {
  if (
    busy.value ||
    (!shadowMode && !window.confirm("该版本已通过固定评测。正式启用后会改变搜索排序，继续吗？"))
  ) return;
  busy.value = `activate:${modelVersion}:${shadowMode ? "shadow" : "live"}`;
  error.value = "";
  message.value = "";
  try {
    await props.api.activate(modelVersion, shadowMode);
    message.value = shadowMode
      ? "候选版本已进入影子模式，只记录新排序，不改变当前结果。"
      : "通过固定评测的排序版本已正式启用。";
    emit("toast", "搜索排序版本已更新", message.value, "success");
    await refresh();
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法启用排序版本", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function rollback(): Promise<void> {
  if (busy.value) return;
  busy.value = "rollback";
  error.value = "";
  message.value = "";
  try {
    await props.api.rollback();
    message.value = "已切回上一个通过固定评测的排序版本。";
    emit("toast", "排序版本已切换", message.value, "success");
    await refresh();
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法切回上一版本", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function clearData(): Promise<void> {
  if (
    busy.value ||
    !window.confirm("只清除本机搜索学习记录和候选版本，不会删除图库。继续吗？")
  ) return;
  busy.value = "clear";
  error.value = "";
  message.value = "";
  try {
    await props.api.clear();
    message.value = "学习记录已清除，搜索已恢复默认排序。";
    emit("toast", "搜索学习记录已清除", message.value, "success");
    await refresh();
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法清除学习记录", error.value, "error");
  } finally {
    busy.value = "";
  }
}

async function exportData(): Promise<void> {
  if (busy.value) return;
  busy.value = "export";
  error.value = "";
  let url = "";
  try {
    const payload = await props.api.exportData();
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "zvec-search-learning-anonymous.json";
    anchor.style.display = "none";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    emit("toast", "匿名数据已导出", "导出内容不包含图片、API Key 或绝对路径。", "success");
  } catch (reason) {
    error.value = failure(reason);
    emit("toast", "无法导出匿名数据", error.value, "error");
  } finally {
    if (url) URL.revokeObjectURL(url);
    busy.value = "";
  }
}

onMounted(() => void refresh());
onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
});
</script>

<template>
  <section class="learning-card" aria-labelledby="search-learning-title">
    <header>
      <div>
        <p class="eyebrow">本地 · 隐私优先</p>
        <h2 id="search-learning-title">搜索学习</h2>
        <p>根据明确反馈改善排序；候选版本通过固定评测后才允许启用。</p>
      </div>
      <span class="safety-pill">不调用大模型</span>
    </header>

    <div v-if="status" class="setting-list">
      <label>
        <span><strong>启用本地搜索学习</strong><small>关闭后立即使用默认排序，已保存数据不会丢失。</small></span>
        <input
          type="checkbox"
          :checked="status.settings.learning_enabled"
          :disabled="Boolean(busy)"
          @change="updateSetting('learning_enabled', ($event.target as HTMLInputElement).checked)"
        />
      </label>
      <label>
        <span><strong>记录打开、复制和导出</strong><small>这些是低权重隐式反馈，未点击不会被视为负样本。</small></span>
        <input
          type="checkbox"
          :checked="status.settings.implicit_feedback_enabled"
          :disabled="Boolean(busy)"
          @change="updateSetting('implicit_feedback_enabled', ($event.target as HTMLInputElement).checked)"
        />
      </label>
      <label>
        <span><strong>保存查询文字</strong><small>默认关闭；排序学习本身不需要保存原始查询文字。</small></span>
        <input
          type="checkbox"
          :checked="status.settings.save_query_text"
          :disabled="Boolean(busy)"
          @change="updateSetting('save_query_text', ($event.target as HTMLInputElement).checked)"
        />
      </label>
    </div>

    <div v-if="status" class="readiness">
      <div>
        <span>有效查询</span>
        <strong>{{ status.training_counts.query_sessions }} / {{ status.minimum_requirements.query_sessions }}</strong>
        <progress :value="readiness.sessions" max="100" />
      </div>
      <div>
        <span>明确判断</span>
        <strong>{{ status.training_counts.explicit_samples }} / {{ status.minimum_requirements.explicit_samples }}</strong>
        <progress :value="readiness.labels" max="100" />
      </div>
      <p>
        当前版本：{{ status.settings.active_model_version || "默认排序" }}
        <template v-if="status.settings.shadow_mode"> · 影子模式</template>
      </p>
    </div>

    <section v-if="status" class="evaluation-status" aria-label="固定评测状态">
      <div>
        <strong>固定评测集</strong>
        <span v-if="status.fixed_evaluation?.available">
          {{ status.fixed_evaluation.evaluation_set_id }} ·
          {{ status.fixed_evaluation.case_count }} 条查询 ·
          {{ status.fixed_evaluation.no_answer_count }} 条无结果查询
        </span>
        <span v-else-if="status.fixed_evaluation?.status === 'invalid'">
          评测包无效，候选版本不会被允许启用。
        </span>
        <span v-else>尚未安装固定评测包，候选版本将保持待评测。</span>
      </div>
      <div class="evaluation-actions">
        <span
          class="gate-pill"
          :class="status.fixed_evaluation?.available ? 'passed' : 'pending'"
        >
          {{ status.fixed_evaluation?.available ? "可评测" : "待配置" }}
        </span>
        <button
          type="button"
          class="secondary compact"
          :disabled="Boolean(busy)"
          @click="installEvaluation"
        >
          {{ busy === "evaluation" ? "导入中…" : "导入评测包" }}
        </button>
      </div>
    </section>

    <section v-if="modelVersions.length" class="version-list" aria-label="排序候选版本">
      <header><strong>候选与历史版本</strong><span>只有通过固定评测的版本可以启用</span></header>
      <article v-for="version in modelVersions" :key="version.model_version">
        <div>
          <strong>{{ version.model_version }}</strong>
          <small>{{ version.created_at || "时间未知" }}</small>
        </div>
        <span class="gate-pill" :class="version.gate_status || 'pending'">
          {{ version.gate_status === "passed" ? "已通过" : version.gate_status === "failed" ? "未通过" : "待评测" }}
        </span>
        <div class="version-actions">
          <template v-if="version.gate_status === 'passed'">
            <button
              v-if="version.model_version !== activeModelVersion"
              type="button"
              class="secondary compact"
              :disabled="Boolean(busy)"
              @click="activate(version.model_version, true)"
            >影子验证</button>
            <button
              v-if="version.model_version !== activeModelVersion || activeShadowMode"
              type="button"
              class="compact"
              :disabled="Boolean(busy)"
              @click="activate(version.model_version, false)"
            >正式启用</button>
          </template>
          <span v-if="version.model_version === activeModelVersion" class="active-version">
            {{ activeShadowMode ? "当前影子版本" : "当前正式版本" }}
          </span>
        </div>
        <div
          v-if="evaluationRows(version).length || gateReasons(version).length"
          class="version-report"
        >
          <p class="comparison-summary">{{ comparisonSummary(version) }}</p>
          <ul v-if="gateReasons(version).length" class="gate-reasons" aria-label="质量门槛未通过原因">
            <li v-for="reason in gateReasons(version)" :key="reason">{{ reason }}</li>
          </ul>
          <details v-if="evaluationRows(version).length" :open="version === modelVersions[0]">
            <summary>查看固定评测指标</summary>
            <div class="evaluation-table-wrap">
              <table class="evaluation-table">
                <thead>
                  <tr>
                    <th scope="col">指标</th>
                    <th scope="col">{{ currentMetricLabel(version) }}</th>
                    <th scope="col">{{ candidateMetricLabel(version) }}</th>
                    <th scope="col">变化</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in evaluationRows(version)" :key="row.key">
                    <th scope="row">{{ row.label }}</th>
                    <td>{{ formatMetric(row.current, row.format) }}</td>
                    <td>{{ formatMetric(row.candidate, row.format) }}</td>
                    <td>{{ formatDelta(row) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </div>
      </article>
    </section>

    <p v-else-if="!error" class="loading" role="status">正在读取搜索学习状态…</p>
    <p v-if="message" class="message" role="status">{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <footer v-if="status">
      <button type="button" :disabled="Boolean(busy) || !readiness.ready" @click="train">
        {{ busy === "train" ? "正在启动…" : "训练候选版本" }}
      </button>
      <button
        type="button"
        class="secondary"
        :disabled="Boolean(busy) || !status.settings.previous_model_version"
        @click="rollback"
      >
        切回上一版本
      </button>
      <button type="button" class="secondary" :disabled="Boolean(busy)" @click="exportData">
        导出匿名数据
      </button>
      <button type="button" class="danger" :disabled="Boolean(busy)" @click="clearData">
        清除学习记录
      </button>
    </footer>
  </section>
</template>

<style scoped>
.learning-card {
  display: grid;
  gap: 18px;
  padding: 22px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 20px;
  background: linear-gradient(145deg, rgb(255 255 255 / 98%), rgb(247 248 255 / 94%));
  box-shadow: 0 16px 45px rgb(31 42 74 / 8%);
}

header,
footer,
.setting-list label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

h2,
p {
  margin: 0;
}

h2 {
  margin: 2px 0 4px;
  font-size: 20px;
}

header p:not(.eyebrow),
small,
.readiness span,
.loading {
  color: var(--muted, #596478);
}

.eyebrow {
  color: var(--brand-strong, #443ab7);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.safety-pill {
  flex: 0 0 auto;
  padding: 7px 10px;
  border-radius: 999px;
  color: #22624c;
  font-size: 12px;
  font-weight: 750;
  background: #e8f7f0;
}

.setting-list {
  display: grid;
  border-block: 1px solid var(--border, #e1e6ef);
}

.setting-list label {
  min-height: 66px;
  border-bottom: 1px solid var(--border, #e1e6ef);
}

.setting-list label:last-child {
  border-bottom: 0;
}

.setting-list span {
  display: grid;
  gap: 3px;
}

.setting-list input {
  width: 20px;
  height: 20px;
  accent-color: var(--brand-strong, #443ab7);
}

.readiness {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.readiness > div {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 7px 12px;
  padding: 13px;
  border-radius: 13px;
  background: rgb(239 241 252 / 80%);
}

.readiness progress {
  grid-column: 1 / -1;
  width: 100%;
  height: 6px;
  accent-color: var(--brand-strong, #443ab7);
}

.readiness p {
  grid-column: 1 / -1;
  color: var(--muted, #596478);
  font-size: 12px;
}

.evaluation-status,
.version-list {
  display: grid;
  gap: 10px;
  padding: 13px;
  border: 1px solid var(--border, #e1e6ef);
  border-radius: 13px;
  background: rgb(250 250 254 / 88%);
}

.evaluation-status {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}

.evaluation-status > div,
.version-list article > div:first-child {
  display: grid;
  gap: 3px;
}

.evaluation-actions {
  display: flex !important;
  align-items: center;
  gap: 7px !important;
}

.evaluation-status span:not(.gate-pill),
.version-list header span,
.version-list small {
  color: var(--muted, #596478);
  font-size: 11px;
}

.version-list header,
.version-list article {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 10px;
}

.version-list header {
  grid-template-columns: minmax(0, 1fr) auto;
}

.version-list article {
  min-height: 52px;
  padding-top: 9px;
  border-top: 1px solid var(--border, #e1e6ef);
}

.version-report {
  grid-column: 1 / -1;
  display: grid;
  gap: 8px;
  min-width: 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgb(239 241 252 / 66%);
}

.comparison-summary {
  color: var(--text, #171e2e);
  font-size: 12px;
  font-weight: 700;
}

.gate-reasons {
  display: grid;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
  color: #a12c3c;
  font-size: 12px;
}

.version-report details {
  min-width: 0;
}

.version-report summary {
  width: fit-content;
  color: var(--brand-strong, #443ab7);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}

.evaluation-table-wrap {
  max-width: 100%;
  margin-top: 8px;
  overflow-x: auto;
}

.evaluation-table {
  width: 100%;
  min-width: 560px;
  border-collapse: collapse;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.evaluation-table th,
.evaluation-table td {
  padding: 7px 9px;
  border-bottom: 1px solid var(--border, #e1e6ef);
  text-align: right;
  white-space: nowrap;
}

.evaluation-table th:first-child {
  text-align: left;
}

.evaluation-table thead th {
  color: var(--muted, #596478);
  font-weight: 700;
}

.evaluation-table tbody tr:last-child th,
.evaluation-table tbody tr:last-child td {
  border-bottom: 0;
}

.gate-pill,
.active-version {
  padding: 5px 8px;
  border-radius: 999px;
  color: #8b651c;
  font-size: 10px;
  font-weight: 800;
  background: #fff3dc;
  white-space: nowrap;
}

.gate-pill.passed,
.active-version {
  color: #22624c;
  background: #e8f7f0;
}

.gate-pill.failed {
  color: #a12c3c;
  background: #fff0f2;
}

.version-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px;
}

button.compact {
  min-height: 32px;
  padding: 5px 9px;
  font-size: 11px;
}

footer {
  justify-content: flex-start;
  flex-wrap: wrap;
}

button {
  min-height: 38px;
  padding: 8px 14px;
  border: 1px solid transparent;
  border-radius: 10px;
  color: white;
  font: inherit;
  font-weight: 700;
  background: var(--brand-strong, #443ab7);
  cursor: pointer;
}

button.secondary {
  border-color: var(--border, #e1e6ef);
  color: var(--text, #171e2e);
  background: white;
}

button.danger {
  margin-left: auto;
  color: #a12c3c;
  background: #fff0f2;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.message,
.error,
.loading {
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13px;
}

.message {
  color: #22624c;
  background: #e8f7f0;
}

.error {
  color: #a12c3c;
  background: #fff0f2;
}

.loading {
  background: #f5f6fa;
}

@media (max-width: 720px) {
  .readiness {
    grid-template-columns: 1fr;
  }

  button.danger {
    margin-left: 0;
  }

  .evaluation-status,
  .version-list header,
  .version-list article {
    grid-template-columns: 1fr;
  }
}
</style>
