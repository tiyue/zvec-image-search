<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";

import {
  activityLevelLabel,
  detailValue,
  formatActivityTime,
} from "./presentation";
import type {
  ActivityExportFormat,
  ActivityLibrary,
  ActivityLogFilters,
  ActivityLogItem,
} from "./types";

const props = withDefaults(defineProps<{
  items: ActivityLogItem[];
  filters: ActivityLogFilters;
  libraries?: ActivityLibrary[];
  loading?: boolean;
  page?: number;
  totalCount?: number;
  hasPrevious?: boolean;
  hasNext?: boolean;
  following?: boolean;
  exporting?: boolean;
  selectedSequences?: ReadonlySet<number>;
  errorMessage?: string;
}>(), {
  libraries: () => [],
  loading: false,
  page: 1,
  totalCount: 0,
  hasPrevious: false,
  hasNext: false,
  following: true,
  exporting: false,
  selectedSequences: () => new Set<number>(),
  errorMessage: "",
});

const emit = defineEmits<{
  applyFilters: [filters: ActivityLogFilters];
  refresh: [];
  previous: [];
  next: [];
  followingChange: [enabled: boolean];
  toggleRow: [sequence: number];
  togglePage: [selected: boolean];
  copy: [];
  export: [format: ActivityExportFormat];
  selectJob: [jobId: string];
}>();

const draft = reactive<ActivityLogFilters>({ ...props.filters });
const expandedSequence = ref<number | null>(null);
const visibleItems = computed(() => props.items.slice(0, 50));
const selectedCount = computed(
  () => visibleItems.value.filter((item) => props.selectedSequences.has(item.sequence)).length,
);
const allSelected = computed(
  () => visibleItems.value.length > 0 && selectedCount.value === visibleItems.value.length,
);

watch(
  () => props.filters,
  (filters) => Object.assign(draft, filters),
  { deep: true },
);

function applyFilters(): void {
  emit("applyFilters", { ...draft, query: draft.query.trim() });
}

function clearFilters(): void {
  Object.assign(draft, { level: "", category: "", libraryId: "", jobId: "", query: "" });
  applyFilters();
}

function clearJobFilter(): void {
  draft.jobId = "";
  applyFilters();
}

function toggleDetails(sequence: number): void {
  expandedSequence.value = expandedSequence.value === sequence ? null : sequence;
}

function detailEntries(item: ActivityLogItem): Array<[string, string]> {
  return Object.entries(item.details).map(([key, value]) => [key, detailValue(value)]);
}
</script>

<template>
  <section class="activity-panel log-panel" aria-labelledby="activity-log-title">
    <header class="activity-panel-heading">
      <div>
        <p class="activity-eyebrow">诊断与追踪</p>
        <h2 id="activity-log-title">操作日志</h2>
      </div>
      <div class="log-heading-actions">
        <button
          class="follow-button"
          type="button"
          :class="{ 'is-following': following }"
          :aria-pressed="following"
          @click="emit('followingChange', !following)"
        >
          <span class="follow-dot" aria-hidden="true" />
          {{ following ? "实时跟随" : "已暂停" }}
        </button>
        <button class="activity-button activity-button-quiet" type="button" :disabled="loading" @click="emit('refresh')">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <form class="activity-filters log-filters" aria-label="筛选操作日志" @submit.prevent="applyFilters">
      <label class="filter-level">
        <span>级别</span>
        <select v-model="draft.level" name="log_level">
          <option value="">全部级别</option>
          <option value="info">信息</option>
          <option value="warning">警告</option>
          <option value="error">错误</option>
          <option value="debug">调试</option>
        </select>
      </label>
      <label class="filter-category">
        <span>类别</span>
        <select v-model="draft.category" name="log_category">
          <option value="">全部类别</option>
          <option value="search">搜索</option>
          <option value="library_task">图库任务</option>
          <option value="auto_tag">智能标注</option>
          <option value="manual_tag">手工标签</option>
          <option value="folder_cleanup">文件夹清理</option>
          <option value="image_failure">错误图片</option>
          <option value="settings">设置</option>
          <option value="backend">后端服务</option>
          <option value="frontend">前端诊断</option>
        </select>
      </label>
      <label class="filter-library">
        <span>图库</span>
        <select v-model="draft.libraryId" name="log_library">
          <option value="">全部图库</option>
          <option v-for="library in libraries" :key="library.id" :value="library.id">
            {{ library.name }}
          </option>
        </select>
      </label>
      <label class="filter-job">
        <span>任务编号</span>
        <input v-model="draft.jobId" name="log_job_id" type="search" placeholder="job_id" />
      </label>
      <label class="filter-query">
        <span>搜索</span>
        <input v-model="draft.query" name="log_query" type="search" placeholder="操作、消息或编号" />
      </label>
      <button class="activity-button activity-button-primary filter-submit" type="submit">筛选</button>
      <button class="activity-button activity-button-quiet filter-reset" type="button" @click="clearFilters">重置</button>
    </form>

    <div class="log-action-strip">
      <div class="selected-copy">
        <strong>{{ selectedCount }}</strong>
        <span>条已选</span>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="selectedCount === 0" @click="emit('copy')">
          复制选中
        </button>
      </div>
      <div v-if="filters.jobId" class="active-filter-chip">
        当前任务：{{ filters.jobId }}
        <button type="button" aria-label="清除任务筛选" @click="clearJobFilter">×</button>
      </div>
      <div class="export-actions">
        <small role="status">{{ exporting ? "正在整理日志…" : selectedCount ? "导出选中日志" : "导出完整筛选结果" }}</small>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="exporting || !visibleItems.length" @click="emit('export', 'jsonl')">JSONL</button>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="exporting || !visibleItems.length" @click="emit('export', 'csv')">CSV</button>
      </div>
    </div>

    <div class="activity-table-region" :aria-busy="loading">
      <div v-if="errorMessage" class="log-error-banner" role="alert">
        <span>{{ errorMessage }}</span>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" @click="emit('refresh')">重新加载</button>
      </div>
      <table class="activity-table activity-log-table">
        <thead>
          <tr>
            <th class="selection-column">
              <input
                type="checkbox"
                aria-label="选择当前页日志"
                :checked="allSelected"
                :disabled="!visibleItems.length"
                @change="emit('togglePage', ($event.target as HTMLInputElement).checked)"
              />
            </th>
            <th>时间</th>
            <th>级别</th>
            <th>来源</th>
            <th>操作</th>
            <th>图库 / 任务</th>
            <th>消息</th>
            <th class="details-column">详情</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="item in visibleItems" :key="item.sequence">
            <tr class="activity-row" :class="{ 'is-selected': selectedSequences.has(item.sequence) }">
              <td class="selection-column">
                <input
                  type="checkbox"
                  :aria-label="`选择日志 ${item.sequence}`"
                  :checked="selectedSequences.has(item.sequence)"
                  @change="emit('toggleRow', item.sequence)"
                />
              </td>
              <td><time>{{ formatActivityTime(item.timestamp) }}</time></td>
              <td><span class="level-chip" :data-level="item.level">{{ activityLevelLabel(item.level) }}</span></td>
              <td>
                <strong>{{ item.source }}</strong>
                <small class="cell-subtitle">{{ item.category }}</small>
              </td>
              <td>{{ item.event }}</td>
              <td>
                <span>{{ item.libraryName || "—" }}</span>
                <button v-if="item.jobId" class="job-link" type="button" @click="emit('selectJob', item.jobId)">
                  {{ item.jobId }}
                </button>
              </td>
              <td class="message-cell" :title="item.message">{{ item.message || "—" }}</td>
              <td class="details-column">
                <button
                  class="details-toggle"
                  type="button"
                  :disabled="!Object.keys(item.details).length && !item.operationId"
                  :aria-expanded="expandedSequence === item.sequence"
                  @click="toggleDetails(item.sequence)"
                >
                  {{ expandedSequence === item.sequence ? "收起" : "展开" }}
                </button>
              </td>
            </tr>
            <tr v-if="expandedSequence === item.sequence" class="log-details-row">
              <td colspan="8">
                <section class="log-details" :class="{ 'has-image': item.thumbnailUrl }">
                  <img v-if="item.thumbnailUrl" :src="item.thumbnailUrl" alt="错误图片缩略图" loading="lazy" />
                  <div class="log-details-copy">
                    <dl>
                      <div v-if="item.operationId"><dt>操作编号</dt><dd>{{ item.operationId }}</dd></div>
                      <div v-if="item.jobId"><dt>任务编号</dt><dd>{{ item.jobId }}</dd></div>
                      <div v-for="([key, value]) in detailEntries(item)" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div>
                    </dl>
                    <a
                      v-if="item.imageUrl"
                      class="activity-button activity-button-quiet activity-button-small image-action"
                      :href="item.imageUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                    >查看错误图片</a>
                  </div>
                </section>
              </td>
            </tr>
          </template>
          <tr v-if="!loading && !visibleItems.length">
            <td class="empty-table-state" colspan="8">没有符合条件的操作日志。</td>
          </tr>
        </tbody>
      </table>
      <p v-if="loading && !visibleItems.length" class="loading-overlay" aria-live="polite">正在读取操作日志…</p>
    </div>

    <footer class="cursor-pagination" aria-label="操作日志分页">
      <span>第 {{ page }} 页 · 每页最多 50 条 · 共 {{ totalCount }} 条</span>
      <div>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="loading || !hasPrevious" @click="emit('previous')">上一页</button>
        <button class="activity-button activity-button-quiet activity-button-small" type="button" :disabled="loading || !hasNext" @click="emit('next')">下一页</button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.activity-panel{container-type:inline-size;display:grid;min-width:0;min-height:0;height:100%;grid-template-rows:auto auto auto minmax(0,1fr) auto;overflow:hidden;border:1px solid var(--border,#e1e6ef);border-radius:16px;background:var(--surface,#fff);box-shadow:var(--shadow-card,0 8px 24px rgb(28 39 59 / 7%))}.activity-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 16px 11px}.activity-panel-heading h2{margin:0;font-size:17px}.activity-eyebrow{margin:0 0 2px;color:var(--brand,#6258e7);font-size:11px;font-weight:800;letter-spacing:.08em}.log-heading-actions{display:flex;align-items:center;gap:7px}.follow-button{display:inline-flex;min-height:34px;align-items:center;gap:7px;padding:0 10px;border:1px solid var(--border,#e1e6ef);border-radius:999px;color:var(--muted,#596478);font:inherit;font-size:11px;font-weight:750;background:var(--surface-soft,#f8f9fc)}.follow-button.is-following{border-color:#bfe5d9;color:#0e735f;background:#eaf8f3}.follow-dot{width:7px;height:7px;border-radius:50%;background:#8d97a8}.is-following .follow-dot{background:#1ca47f;box-shadow:0 0 0 4px rgb(28 164 127 / 12%)}.activity-filters{display:grid;grid-template-columns:100px 130px 130px 150px minmax(160px,1fr) auto auto;grid-template-areas:"level category library job query submit reset";align-items:end;gap:8px;padding:0 16px 11px;border-bottom:1px solid var(--border,#e1e6ef)}.filter-level{grid-area:level}.filter-category{grid-area:category}.filter-library{grid-area:library}.filter-job{grid-area:job}.filter-query{grid-area:query}.filter-submit{grid-area:submit}.filter-reset{grid-area:reset}.activity-filters label{display:grid;gap:4px;min-width:0}.activity-filters label>span{color:var(--muted,#596478);font-size:11px;font-weight:700}.activity-filters input,.activity-filters select{width:100%;height:36px;padding:0 9px;border:1px solid var(--border-strong,#cbd3df);border-radius:9px;color:var(--text,#171e2e);background:var(--surface,#fff)}.activity-filters input:focus,.activity-filters select:focus{outline:0;border-color:var(--brand,#6258e7);box-shadow:0 0 0 3px rgb(98 88 231 / 12%)}.activity-button{display:inline-flex;min-height:36px;align-items:center;justify-content:center;padding:0 12px;border:1px solid transparent;border-radius:9px;font:inherit;font-size:12px;font-weight:750;white-space:nowrap}.activity-button-primary{color:#fff;background:var(--brand,#6258e7)}.activity-button-quiet{border-color:var(--border,#e1e6ef);color:var(--text,#171e2e);background:var(--surface,#fff)}.activity-button-small{min-height:30px;padding-inline:9px}.log-action-strip{display:flex;min-height:44px;align-items:center;gap:10px;padding:6px 16px;border-bottom:1px solid var(--border,#e1e6ef);background:var(--surface-soft,#f8f9fc)}.selected-copy,.export-actions{display:flex;align-items:center;gap:7px}.selected-copy>strong{color:var(--brand,#6258e7);font-size:14px}.selected-copy>span,.export-actions small{color:var(--muted,#596478);font-size:11px}.export-actions{margin-left:auto}.active-filter-chip{display:flex;min-width:0;align-items:center;gap:5px;padding:4px 8px;overflow:hidden;border:1px solid #d7d4ff;border-radius:999px;color:var(--brand-strong,#443ab7);font-size:10px;text-overflow:ellipsis;white-space:nowrap;background:var(--brand-soft,#f0efff)}.active-filter-chip button{padding:0;border:0;color:inherit;font:inherit;font-size:15px;background:transparent}.activity-table-region{position:relative;min-height:0;overflow:auto;scrollbar-gutter:stable}.activity-table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px}.activity-table th{position:sticky;z-index:2;top:0;padding:9px 8px;border-bottom:1px solid var(--border,#e1e6ef);color:var(--muted,#596478);font-size:11px;text-align:left;background:var(--surface-soft,#f8f9fc);white-space:nowrap}.activity-table td{padding:8px;border-bottom:1px solid #edf0f5;vertical-align:middle}.activity-row:hover td{background:#fafaff}.activity-row.is-selected td{background:var(--brand-soft,#f0efff)}.selection-column{width:36px;text-align:center!important}.selection-column input{width:16px;height:16px;accent-color:var(--brand,#6258e7)}.details-column{width:52px}.cell-subtitle{display:block;margin-top:2px;color:var(--muted,#596478);font-size:10px}.level-chip{display:inline-flex;min-height:23px;align-items:center;padding:2px 7px;border-radius:999px;color:#405063;font-size:10px;font-weight:800;background:#eef1f6}.level-chip[data-level="warning"]{color:#92500b;background:#fff5e7}.level-chip[data-level="error"]{color:#a93643;background:#fff0f2}.level-chip[data-level="debug"]{color:#555f70;background:#eceff4}.job-link{display:block;max-width:150px;margin-top:2px;padding:0;overflow:hidden;border:0;color:var(--brand,#6258e7);font:inherit;font-size:10px;text-overflow:ellipsis;white-space:nowrap;background:transparent}.message-cell{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.details-toggle{padding:3px 6px;border:0;border-radius:6px;color:var(--brand-strong,#443ab7);font:inherit;font-size:10px;font-weight:750;background:var(--brand-soft,#f0efff)}.details-toggle:disabled{color:#9aa3b2;background:#f1f3f7}.log-details-row td{padding:0;background:#fafbfe}.log-details{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;padding:11px 16px}.log-details img{width:72px;height:72px;border-radius:9px;object-fit:contain;background:#eef1f6}.log-details-copy{display:grid;grid-column:1/-1;gap:9px}.log-details.has-image .log-details-copy{grid-column:auto}.log-details dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:6px 14px;margin:0}.log-details dl>div{display:grid;grid-template-columns:86px minmax(0,1fr);gap:7px}.log-details dt{color:var(--muted,#596478);font-size:10px;font-weight:750}.log-details dd{min-width:0;margin:0;overflow-wrap:anywhere;font-size:10px}.image-action{justify-self:start;text-decoration:none}.log-error-banner{position:sticky;z-index:3;top:0;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 10px;color:var(--danger,#a93643);background:#fff0f2}.empty-table-state{height:110px;color:var(--muted,#596478);text-align:center}.loading-overlay{position:absolute;inset:36px 0 0;display:grid;place-items:center;margin:0;color:var(--muted,#596478);background:rgb(255 255 255 / 82%)}.cursor-pagination{display:flex;min-height:48px;align-items:center;justify-content:space-between;gap:10px;padding:8px 16px;border-top:1px solid var(--border,#e1e6ef);color:var(--muted,#596478);font-size:11px}.cursor-pagination>div{display:flex;gap:6px}
@container(max-width:950px){.activity-filters{grid-template-columns:repeat(3,minmax(105px,1fr)) auto auto;grid-template-areas:"level category library . ." "job query query submit reset"}.activity-log-table{min-width:920px}}@container(max-width:650px){.activity-filters{grid-template-columns:repeat(3,minmax(90px,1fr)) auto auto;overflow-x:auto}.activity-filters .activity-button{min-height:34px;padding-inline:8px}.log-action-strip{flex-wrap:wrap}.export-actions{margin-left:0}.activity-log-table{min-width:880px}}
.activity-panel{border:0;border-radius:0;background:#fff;box-shadow:none}.activity-panel-heading{padding:12px 2px 10px}.activity-eyebrow{display:none}.activity-panel-heading h2{color:#242424;font-size:16px;font-weight:600}.activity-filters{padding:8px 2px 12px;border-top:1px solid #e5e5e5;border-bottom:1px solid #e5e5e5}.activity-filters label>span,.cell-subtitle,.cursor-pagination{color:#777}.activity-filters input,.activity-filters select{border-color:#dedede;border-radius:8px;color:#242424;background:#fff}.activity-filters input:focus,.activity-filters select:focus{border-color:#9d9d9d;box-shadow:0 0 0 3px rgb(0 0 0 / 7%)}.activity-button{border-radius:8px;font-weight:500}.activity-button-primary{color:#fff;background:#606060}.activity-button-quiet{border-color:#dedede;color:#333;background:#f5f5f5}.log-action-strip{padding-inline:2px;border-bottom-color:#e5e5e5;background:#f7f7f7}.selected-copy>strong,.job-link{color:#4b4b4b}.active-filter-chip,.details-toggle{border-color:#dedede;color:#4b4b4b;background:#ededed}.activity-table th{color:#777;background:#f7f7f7}.activity-table td{border-bottom-color:#e9e9e9}.activity-row:hover td,.activity-row.is-selected td{background:#f3f3f3}.selection-column input{accent-color:#777}.cursor-pagination{padding-inline:2px}
</style>
