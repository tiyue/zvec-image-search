<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";

import {
  detailValue,
  formatActivityTime,
  formatDuration,
  isActiveStatus,
  jobStatusLabel,
  taskTypeLabel,
} from "./presentation";
import type {
  ActivityLibrary,
  ActivityLogItem,
  JobHistoryFilters,
  JobHistoryItem,
} from "./types";

const props = withDefaults(defineProps<{
  items: JobHistoryItem[];
  filters: JobHistoryFilters;
  libraries?: ActivityLibrary[];
  loading?: boolean;
  page?: number;
  totalCount?: number;
  hasPrevious?: boolean;
  hasNext?: boolean;
  selectedJobId?: string;
  cancellingJobId?: string;
  expandedErrorJobId?: string;
  errorLogs?: ActivityLogItem[];
  errorsLoading?: boolean;
  errorsError?: string;
  errorHasMore?: boolean;
}>(), {
  libraries: () => [],
  loading: false,
  page: 1,
  totalCount: 0,
  hasPrevious: false,
  hasNext: false,
  selectedJobId: "",
  cancellingJobId: "",
  expandedErrorJobId: "",
  errorLogs: () => [],
  errorsLoading: false,
  errorsError: "",
  errorHasMore: false,
});

const emit = defineEmits<{
  applyFilters: [filters: JobHistoryFilters];
  refresh: [];
  previous: [];
  next: [];
  select: [jobId: string];
  cancel: [jobId: string];
  inspectErrors: [jobId: string];
  retryErrors: [];
  loadMoreErrors: [];
  closeErrors: [];
}>();

const draft = reactive<JobHistoryFilters>({ ...props.filters });
const pendingCancelJobId = ref("");
const detailJobId = ref("");
const visibleItems = computed(() => props.items.slice(0, 50));
const detailJob = computed(() => visibleItems.value.find((job) => job.id === (detailJobId.value || props.expandedErrorJobId)) ?? null);

watch(
  () => props.filters,
  (filters) => Object.assign(draft, filters),
  { deep: true },
);

function applyFilters(): void {
  emit("applyFilters", { ...draft, query: draft.query.trim() });
}

function clearFilters(): void {
  Object.assign(draft, { status: "", taskType: "", libraryId: "", query: "" });
  applyFilters();
}

function requestCancel(jobId: string): void {
  pendingCancelJobId.value = jobId;
}

function confirmCancel(jobId: string): void {
  pendingCancelJobId.value = "";
  emit("cancel", jobId);
}

function openDetails(job: JobHistoryItem): void {
  detailJobId.value = job.id;
  emit("select", job.id);
  if (job.failed > 0) emit("inspectErrors", job.id);
}

function closeDetails(): void {
  detailJobId.value = "";
  emit("closeErrors");
}

function errorPath(item: ActivityLogItem): string {
  return detailValue(item.details.relative_path || item.details.path) || "路径不可用";
}

function errorReason(item: ActivityLogItem): string {
  return detailValue(item.details.reason || item.details.error_message) || item.message;
}
</script>

<template>
  <section class="activity-panel job-history-panel" aria-labelledby="job-history-title">
    <h2 id="job-history-title" class="sr-only">任务历史</h2>
    <details class="activity-filter-disclosure">
      <summary>筛选</summary>
      <form class="activity-filters job-filters" aria-label="筛选任务历史" @submit.prevent="applyFilters">
      <label>
        <span>状态</span>
        <select v-model="draft.status" name="job_status">
          <option value="">全部状态</option>
          <option value="running">运行中</option>
          <option value="queued">排队中</option>
          <option value="succeeded">已完成</option>
          <option value="partial">部分完成</option>
          <option value="needs_attention">需要处理</option>
          <option value="failed">失败</option>
          <option value="cancelled">已取消</option>
          <option value="interrupted">已中断</option>
        </select>
      </label>
      <label>
        <span>任务类型</span>
        <select v-model="draft.taskType" name="job_task_type">
          <option value="">全部任务</option>
          <option value="index">建立索引</option>
          <option value="sync">同步图库</option>
          <option value="auto_tag">智能标注</option>
          <option value="index_and_auto_tag">索引并智能标注</option>
          <option value="manual_tag_batch">批量标签</option>
          <option value="folder_delete_commit">文件夹清理</option>
        </select>
      </label>
      <label>
        <span>图库</span>
        <select v-model="draft.libraryId" name="job_library">
          <option value="">全部图库</option>
          <option v-for="library in libraries" :key="library.id" :value="library.id">
            {{ library.name }}
          </option>
        </select>
      </label>
      <label class="filter-query">
        <span>搜索</span>
        <input v-model="draft.query" name="job_query" type="search" placeholder="任务编号、消息或错误" />
      </label>
      <button class="activity-button activity-button-primary" type="submit">筛选</button>
      <button class="activity-button activity-button-quiet" type="button" @click="clearFilters">重置</button>
      </form>
    </details>

    <div class="activity-list-region" :aria-busy="loading">
      <table class="job-history-table"><tbody>
      <tr
        v-for="job in visibleItems"
        :key="job.id"
        class="activity-row job-list-row"
        :class="{ 'is-selected': selectedJobId === job.id, 'is-active': isActiveStatus(job.status) }"
        tabindex="0"
        @click="emit('select', job.id)"
        @dblclick="openDetails(job)"
        @keydown.enter.prevent="openDetails(job)"
      >
        <td><span class="job-status-icon" :data-status="job.status" aria-hidden="true">{{ isActiveStatus(job.status) ? '◷' : job.status === 'succeeded' ? '✓' : '○' }}</span></td>
        <td class="job-list-copy"><strong>{{ taskTypeLabel(job.taskType) }}</strong><small>{{ job.id }}</small></td>
        <td class="job-list-summary"><small>{{ jobStatusLabel(job.status) }} · {{ job.libraryName }} · {{ job.processed.toLocaleString('zh-CN') }} / {{ job.total ? job.total.toLocaleString('zh-CN') : '—' }} · 失败 {{ job.failed }}</small><progress v-if="isActiveStatus(job.status)" max="100" :value="job.progressPercent" /></td>
        <td class="job-actions" @click.stop>
                <template v-if="pendingCancelJobId === job.id">
                  <span class="cancel-confirm-copy">确认取消？</span>
                  <button class="activity-button activity-button-danger activity-button-small" type="button" @click="confirmCancel(job.id)">
                    确认
                  </button>
                  <button class="activity-button activity-button-quiet activity-button-small" type="button" @click="pendingCancelJobId = ''">
                    返回
                  </button>
                </template>
                <button
                  v-else-if="isActiveStatus(job.status)"
                  class="activity-button activity-button-quiet activity-button-small"
                  type="button"
                  :disabled="Boolean(cancellingJobId)"
                  @click="requestCancel(job.id)"
                >
                  {{ cancellingJobId === job.id ? "取消中…" : "取消" }}
                </button>
        </td>
        <td class="job-detail-action" @click.stop><button :class="job.failed ? 'error-count-button' : 'activity-button activity-button-quiet activity-button-small'" type="button" @click="openDetails(job)">详情</button></td>
      </tr>
      </tbody></table>
      <p v-if="!loading && !visibleItems.length" class="empty-table-state">没有符合条件的任务记录。</p>
      <p v-if="loading && !visibleItems.length" class="loading-overlay" aria-live="polite">正在读取任务历史…</p>
    </div>

    <footer class="cursor-pagination" aria-label="任务历史分页">
      <span>第 {{ page }} 页 · 每页最多 50 条 · 共 {{ totalCount }} 条</span>
      <div>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="loading || !hasPrevious" @click="emit('previous')">上一页</button>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="loading || !hasNext" @click="emit('next')">下一页</button>
      </div>
    </footer>

    <div v-if="detailJob" class="activity-modal-backdrop" role="presentation" @click.self="closeDetails">
      <section class="job-detail-modal" role="dialog" aria-modal="true" aria-labelledby="job-detail-title">
        <header><h3 id="job-detail-title">任务详情 {{ taskTypeLabel(detailJob.taskType) }} · {{ detailJob.id }}</h3><button type="button" aria-label="关闭详情" @click="closeDetails">×</button></header>
        <dl class="job-detail-list">
          <div><dt>状态</dt><dd>{{ jobStatusLabel(detailJob.status) }}</dd></div>
          <div><dt>图库</dt><dd>{{ detailJob.libraryName }}</dd></div>
          <div><dt>进度</dt><dd>{{ detailJob.progressPercent }}%</dd></div>
          <div><dt>信息</dt><dd>{{ detailJob.processed }} / {{ detailJob.total || '—' }} · 失败 {{ detailJob.failed }}</dd></div>
          <div><dt>提交时间</dt><dd>{{ formatActivityTime(detailJob.submittedAt) }}</dd></div>
          <div><dt>耗时</dt><dd>{{ formatDuration(detailJob.startedAt || detailJob.submittedAt, detailJob.finishedAt) }}</dd></div>
        </dl>
        <h4 v-if="detailJob.failed">错误图片 · {{ detailJob.failed }} 张</h4>
        <p v-if="errorsLoading && !errorLogs.length" class="inline-state">正在读取错误图片…</p>
        <div v-else-if="errorsError" class="inline-error" role="alert"><span>{{ errorsError }}</span><button class="activity-button activity-button-quiet activity-button-small" type="button" @click="emit('retryErrors')">重新加载</button></div>
        <div v-else-if="detailJob.failed" class="error-image-list">
          <article v-for="entry in errorLogs" :key="entry.sequence" class="error-image-item"><span class="error-image-placeholder" aria-hidden="true">!</span><div><strong>{{ errorPath(entry) }}</strong><small>{{ errorReason(entry) }}</small><a v-if="entry.imageUrl" class="error-image-action" :href="entry.imageUrl" target="_blank" rel="noopener noreferrer">查看图片</a></div></article>
          <button v-if="errorHasMore" class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="errorsLoading" @click="emit('loadMoreErrors')">{{ errorsLoading ? '加载中…' : '加载更多错误' }}</button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.activity-panel{container-type:inline-size;display:grid;min-width:0;min-height:0;height:100%;grid-template-rows:auto auto minmax(0,1fr) auto;overflow:hidden;border:1px solid var(--border,#e1e6ef);border-radius:16px;background:var(--surface,#fff);box-shadow:var(--shadow-card,0 8px 24px rgb(28 39 59 / 7%))}.activity-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 16px 11px}.activity-panel-heading h2{margin:0;font-size:17px}.activity-eyebrow{margin:0 0 2px;color:var(--brand,#6258e7);font-size:11px;font-weight:800;letter-spacing:.08em}.activity-filters{display:grid;grid-template-columns:130px 170px 160px minmax(180px,1fr) auto auto;align-items:end;gap:8px;padding:0 16px 12px;border-bottom:1px solid var(--border,#e1e6ef)}.activity-filters label{display:grid;gap:4px;min-width:0}.activity-filters label>span{color:var(--muted,#596478);font-size:11px;font-weight:700}.activity-filters input,.activity-filters select{width:100%;height:36px;padding:0 9px;border:1px solid var(--border-strong,#cbd3df);border-radius:9px;color:var(--text,#171e2e);background:var(--surface,#fff)}.activity-filters input:focus,.activity-filters select:focus{outline:0;border-color:var(--brand,#6258e7);box-shadow:0 0 0 3px rgb(98 88 231 / 12%)}.activity-button{display:inline-flex;min-height:36px;align-items:center;justify-content:center;padding:0 12px;border:1px solid transparent;border-radius:9px;font:inherit;font-size:12px;font-weight:750;white-space:nowrap}.activity-button-primary{color:#fff;background:var(--brand,#6258e7)}.activity-button-quiet{border-color:var(--border,#e1e6ef);color:var(--text,#171e2e);background:var(--surface,#fff)}.activity-button-danger{color:#fff;background:var(--danger,#a93643)}.activity-button-small{min-height:30px;padding-inline:9px}.activity-table-region{position:relative;min-height:0;overflow:auto;scrollbar-gutter:stable}.activity-table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px}.activity-table th{position:sticky;z-index:2;top:0;padding:9px 10px;border-bottom:1px solid var(--border,#e1e6ef);color:var(--muted,#596478);font-size:11px;text-align:left;background:var(--surface-soft,#f8f9fc);white-space:nowrap}.activity-table td{padding:9px 10px;border-bottom:1px solid #edf0f5;vertical-align:middle}.activity-row{cursor:pointer}.activity-row:hover td{background:#fafaff}.activity-row.is-selected td{background:var(--brand-soft,#f0efff)}.activity-row.is-active td:first-child{box-shadow:inset 3px 0 var(--brand,#6258e7)}.cell-subtitle{display:block;max-width:180px;margin-top:2px;overflow:hidden;color:var(--muted,#596478);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.status-chip{display:inline-flex;min-height:25px;align-items:center;padding:3px 8px;border-radius:999px;color:#475569;font-size:11px;font-weight:750;background:#eef1f6}.status-chip[data-status="running"],.status-chip[data-status="queued"],.status-chip[data-status="pending"]{color:#4938b6;background:#eeecff}.status-chip[data-status="succeeded"],.status-chip[data-status="completed"]{color:#08705d;background:#e7f7f2}.status-chip[data-status="failed"],.status-chip[data-status="interrupted"]{color:#a93643;background:#fff0f2}.status-chip[data-status="partial"],.status-chip[data-status="needs_attention"]{color:#92500b;background:#fff5e7}.compact-progress{display:flex;min-width:94px;align-items:center;gap:6px;font-variant-numeric:tabular-nums}.compact-progress progress{width:62px;height:6px;border:0;border-radius:999px;overflow:hidden;appearance:none;background:#e7e9ef}.compact-progress progress::-webkit-progress-bar{background:#e7e9ef}.compact-progress progress::-webkit-progress-value{background:var(--brand,#6258e7)}.numeric-cell{font-variant-numeric:tabular-nums;white-space:nowrap}.error-count-button{padding:3px 7px;border:0;border-radius:7px;color:var(--danger,#a93643);font:inherit;font-weight:750;background:#fff0f2}.job-actions{min-width:112px}.job-actions>*+*{margin-left:5px}.cancel-confirm-copy{color:var(--danger,#a93643);font-size:11px;font-weight:750}.finished-mark{color:var(--faint,#68758b)}.job-errors-row td{padding:0;background:#fafbfe}.job-errors{display:grid;gap:10px;padding:12px 16px 14px;border-bottom:1px solid #e5e7ef}.job-errors>header{display:flex;align-items:center;justify-content:space-between;gap:12px}.job-errors>header small{display:block;margin-top:2px;color:var(--muted,#596478);font-size:11px}.error-image-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}.error-image-item{display:grid;grid-template-columns:44px minmax(0,1fr);align-items:center;gap:9px;min-width:0;padding:7px;border:1px solid var(--border,#e1e6ef);border-radius:10px;background:#fff}.error-image-item img,.error-image-placeholder{width:44px;height:44px;border-radius:7px;object-fit:contain;background:#eef1f6}.error-image-placeholder{display:grid;place-items:center;color:var(--danger,#a93643);font-weight:800}.error-image-item strong,.error-image-item small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.error-image-item strong{font-size:11px}.error-image-item small{margin-top:2px;color:var(--muted,#596478);font-size:10px}.error-image-action{display:inline-flex;margin-top:4px;color:var(--brand,#6258e7);font-size:10px;font-weight:750;text-decoration:none}.inline-error{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border-radius:9px;color:var(--danger,#a93643);background:#fff0f2}.inline-state,.empty-table-state{color:var(--muted,#596478);text-align:center}.empty-table-state{height:110px}.loading-overlay{position:absolute;inset:36px 0 0;display:grid;place-items:center;margin:0;color:var(--muted,#596478);background:rgb(255 255 255 / 82%)}.cursor-pagination{display:flex;min-height:48px;align-items:center;justify-content:space-between;gap:10px;padding:8px 16px;border-top:1px solid var(--border,#e1e6ef);color:var(--muted,#596478);font-size:11px}.cursor-pagination>div{display:flex;gap:6px}
@container(max-width:1100px){.activity-filters{grid-template-columns:repeat(3,minmax(120px,1fr)) auto auto}.filter-query{grid-column:1/4}.job-history-table{min-width:1050px}}@container(max-width:700px){.activity-filters{grid-template-columns:repeat(3,minmax(105px,1fr)) auto auto;overflow-x:auto}.activity-filters .activity-button{min-height:34px;padding-inline:8px}.job-history-table{min-width:1000px}}
.activity-panel{position:relative;display:grid;height:100%;grid-template-rows:auto minmax(0,1fr) auto;overflow:hidden;border:0;border-radius:0;background:#fff;box-shadow:none}
.activity-filter-disclosure{position:absolute;z-index:5;right:2px;top:-42px;color:#777}
.activity-filter-disclosure>summary{padding:6px 9px;border-radius:8px;cursor:pointer;list-style:none}
.activity-filter-disclosure>summary:hover{background:#f2f2f2;color:#222}
.activity-filter-disclosure[open]{top:0;right:0;width:min(850px,100%);padding:10px;border:1px solid #e5e5e5;border-radius:12px;background:#fff;box-shadow:0 12px 36px rgb(0 0 0 / 10%)}
.activity-filter-disclosure[open]>summary{position:absolute;right:8px;top:6px}
.activity-filters{grid-template-columns:repeat(4,minmax(120px,1fr)) auto auto;padding:34px 0 0;border:0}
.activity-filters label>span,.cursor-pagination{color:#777}
.activity-filters input,.activity-filters select{border-color:#dedede;border-radius:8px;color:#242424;background:#fff}
.activity-button{border-radius:8px;font-weight:400}.activity-button-primary{color:#fff;background:#606060}.activity-button-quiet{border-color:#dedede;color:#333;background:#fff}
.activity-list-region{min-height:0;overflow:auto;border-top:1px solid #e7e7e7}
.job-history-table{width:100%;min-width:0!important;border-collapse:collapse}.job-list-row{display:grid;grid-template-columns:22px 170px minmax(0,1fr) auto auto;align-items:center;gap:8px;min-height:64px;padding:9px 2px;border-bottom:1px solid #e7e7e7;background:#fff}.job-list-row td{padding:0;border:0}
.job-list-row:hover,.job-list-row.is-selected{background:#f7f7f7}
.job-status-icon{color:#555;font-size:16px;text-align:center}
.job-list-copy,.job-list-summary{display:grid;gap:2px;min-width:0}
.job-list-copy strong{font-size:14px;font-weight:500}.job-list-copy small{overflow:hidden;color:#777;text-overflow:ellipsis;white-space:nowrap}
.job-list-summary progress{width:min(780px,100%);height:5px;margin-top:4px;border:0;border-radius:999px;overflow:hidden;appearance:none;background:#ededed}
.job-list-summary progress::-webkit-progress-bar{background:#ededed}.job-list-summary progress::-webkit-progress-value{background:#4d4d4d}
.job-actions{display:flex;align-items:center;gap:6px;min-width:0}.job-actions>*+*{margin-left:0}
.empty-table-state{display:grid;min-height:180px;place-items:center;color:#777;text-align:center}
.cursor-pagination{justify-content:center;gap:12px;min-height:50px;padding:8px 2px;border-top:0}.cursor-pagination>div{display:flex;gap:6px}.cursor-pagination>span{order:2}.cursor-pagination>div{display:contents}.cursor-pagination button:first-child{order:1}.cursor-pagination button:last-child{order:3}
.activity-modal-backdrop{position:fixed;z-index:1300;inset:0;display:grid;place-items:center;padding:28px;background:rgb(0 0 0 / 15%)}
.job-detail-modal{width:min(520px,calc(100vw - 40px));max-height:calc(100vh - 56px);overflow:auto;padding:18px;border:1px solid #e3e3e3;border-radius:14px;background:#fff;box-shadow:0 20px 64px rgb(0 0 0 / 16%)}
.job-detail-modal>header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.job-detail-modal h3,.job-detail-modal h4{margin:0;font-size:15px}.job-detail-modal>header button{border:0;color:#777;font-size:20px;background:transparent}
.job-detail-list{margin:0;border-top:1px solid #e7e7e7}.job-detail-list>div{display:grid;grid-template-columns:74px minmax(0,1fr);gap:8px;padding:8px 0;border-bottom:1px solid #e7e7e7}.job-detail-list dt{color:#777}.job-detail-list dd{margin:0}.job-detail-modal h4{margin-top:14px}
.error-image-list{margin-top:8px;grid-template-columns:1fr}.error-image-item{border:0;border-bottom:1px solid #e7e7e7;border-radius:0}.inline-error{margin-top:8px}
</style>
