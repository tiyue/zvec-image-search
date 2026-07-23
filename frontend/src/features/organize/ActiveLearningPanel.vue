<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, toRef } from "vue";

import PaginationBar from "../../components/PaginationBar.vue";
import {
  GRID_GAP,
  gridRowSpan,
  gridSpan,
  LEARNING_META_OVERHEAD,
} from "./galleryCapacity";
import type {
  IntelligenceImage,
  LearningCandidateKind,
  LearningDecisionValue,
  LearningReason,
} from "./intelligenceTypes";
import type { OrganizeApi, ToastKind } from "./types";
import { useActiveLearning } from "./useOrganizeIntelligence";

const props = defineProps<{
  api?: OrganizeApi;
  libraryId: string;
}>();

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
  openImage: [imageId: string];
}>();

const learning = useActiveLearning(toRef(props, "libraryId"), props.api, {
  onToast: (title, message, kind) => emit("toast", title, message, kind),
});

const decisionOptions: Array<{ value: LearningDecisionValue; label: string }> = [
  { value: "accept", label: "接受" },
  { value: "reject", label: "拒绝" },
  { value: "edit", label: "编辑" },
  { value: "skip", label: "跳过" },
];

function reasonLabel(reason: LearningReason): string {
  return {
    identity_conflict: "身份冲突",
    cluster_outlier: "分组离群",
    ranking_disagreement: "排序差异",
    combined_uncertainty: "综合不确定",
    low_information: "信息不足",
  }[reason];
}

function decisionLabel(value: LearningDecisionValue): string {
  return decisionOptions.find((option) => option.value === value)?.label ?? value;
}

function candidateKindLabel(value: LearningCandidateKind): string {
  return {
    tag_review: "标签审核",
    search_result: "搜索反馈",
    cluster_membership: "分组关系",
  }[value];
}

function canShowImage(image: IntelligenceImage): boolean {
  return image.imageAvailable && Boolean(image.thumbnailUrl || image.imageUrl);
}

function openImage(image: IntelligenceImage): void {
  if (!image.nativeImageId) {
    emit("toast", "无法打开原图", "当前图片文件不可用或尚未注册。", "error");
    return;
  }
  emit("openImage", image.nativeImageId);
}

function setSingleDecision(docId: string, decision: LearningDecisionValue): void {
  const labels = decision === "edit"
    ? learning.editTagInput.value.split(/[\n\r,，、;；]+/u).map((tag) => tag.trim()).filter(Boolean)
    : [];
  learning.setDecision(docId, decision, [...new Set(labels)]);
}

const LEARNING_PAGE_SIZE = 8;
const learningPage = ref(1);
const gridContainer = ref<HTMLElement | null>(null);
const containerWidth = ref(0);
let gridResizeObserver: ResizeObserver | null = null;
let gridResizeTimer: number | null = null;

const allSamples = computed(() => learning.queue.value?.samples ?? []);
const learningTotalPages = computed(() => Math.max(1, Math.ceil(allSamples.value.length / LEARNING_PAGE_SIZE)));
const pagedSamples = computed(() => {
  const start = (learningPage.value - 1) * LEARNING_PAGE_SIZE;
  return allSamples.value.slice(start, start + LEARNING_PAGE_SIZE);
});

function goLearningPage(page: number): void {
  learningPage.value = Math.max(1, Math.min(page, learningTotalPages.value));
}

function cardRowSpan(image: IntelligenceImage): number {
  const colSpan = gridSpan(image.width, image.height);
  const columnCount = containerWidth.value < 500 ? 1 : 2;
  return gridRowSpan(
    image.width,
    image.height,
    colSpan,
    containerWidth.value,
    columnCount,
    GRID_GAP,
    LEARNING_META_OVERHEAD,
  );
}

function updateContainerWidth(): void {
  if (gridContainer.value) containerWidth.value = gridContainer.value.clientWidth;
}

function scheduleContainerWidthUpdate(): void {
  if (gridResizeTimer !== null) window.clearTimeout(gridResizeTimer);
  gridResizeTimer = window.setTimeout(() => {
    gridResizeTimer = null;
    updateContainerWidth();
  }, 140);
}

onMounted(() => {
  if (props.libraryId) void learning.loadQueue();
  void nextTick(() => updateContainerWidth());
  if (typeof ResizeObserver !== "undefined" && gridContainer.value) {
    gridResizeObserver = new ResizeObserver(scheduleContainerWidthUpdate);
    gridResizeObserver.observe(gridContainer.value);
  }
});

onBeforeUnmount(() => {
  gridResizeObserver?.disconnect();
  if (gridResizeTimer !== null) window.clearTimeout(gridResizeTimer);
});
</script>

<template>
  <section class="learning-panel" aria-labelledby="active-learning-title">
    <h2 id="active-learning-title" class="sr-only">待学习样本</h2>

    <div class="learning-notice">
      <span aria-hidden="true">◇</span>
      <div class="learning-notice-body">
        <strong>审核成功后会真实写入对应的本地数据</strong>
        <small>标签样本更新图片标签，搜索样本写入相关性反馈，分组样本更新成员关系；不会调用 Qwen 或产生新增模型费用。</small>
      </div>
      <div class="learning-controls">
        <label>
          <span>本轮审核预算</span>
          <input
            v-model.number="learning.reviewBudget.value"
            type="number"
            min="20"
            max="30"
            step="1"
            :disabled="learning.busy.value"
            aria-label="本轮审核预算"
          />
        </label>
        <button
          class="learning-button primary"
          type="button"
          :disabled="learning.busy.value || !libraryId"
          @click="learning.loadQueue"
        >
          {{ learning.busy.value ? "正在读取…" : "生成待学习队列" }}
        </button>
        <button
          class="learning-button secondary"
          type="button"
          :disabled="learning.busy.value || !learning.undoAvailable.value || !libraryId"
          @click="learning.undoLastReview"
        >
          撤销最近一次审核
        </button>
        <button
          v-if="learning.busy.value"
          class="learning-button danger"
          type="button"
          :disabled="learning.cancelling.value"
          @click="learning.cancel"
        >
          {{ learning.cancelling.value ? "取消中…" : "取消" }}
        </button>
      </div>
    </div>

    <div class="learning-feedback">
      <section
        v-if="learning.lastReviewResult.value"
        class="review-summary"
        aria-label="最近一次主动学习审核结果"
        aria-live="polite"
      >
        <header>
          <strong>最近审核结果</strong>
          <span v-if="learning.lastReviewResult.value.batchId">
            {{ learning.lastReviewResult.value.batchId }}
          </span>
        </header>
        <dl>
          <div><dt>已应用</dt><dd>{{ learning.lastReviewResult.value.applied }}</dd></div>
          <div><dt>失败</dt><dd>{{ learning.lastReviewResult.value.failed }}</dd></div>
          <div><dt>跳过</dt><dd>{{ learning.lastReviewResult.value.skipped }}</dd></div>
          <div><dt>冲突</dt><dd>{{ learning.lastReviewResult.value.conflicts }}</dd></div>
        </dl>
        <small v-if="learning.pendingDecisionCount.value">
          未成功项目已保留，可修改决定后重新提交。
        </small>
        <small v-if="learning.undoMessage.value" class="undo-message">
          {{ learning.undoMessage.value }}
        </small>
      </section>
    </div>

    <section class="decision-toolbar" aria-label="批量审核操作">
      <div class="selection-summary">
        <strong>已选 {{ learning.selectedCount.value }} 张</strong>
        <span class="sr-only">已设置 {{ learning.pendingDecisionCount.value }} 项决定</span>
      </div>
      <div class="selection-buttons">
        <button type="button" @click="learning.selectAll">全选本轮</button>
        <button type="button" @click="learning.clearSelection">清空选择</button>
      </div>
      <label class="edit-tag-input">
        <span class="sr-only">编辑标签（支持逗号、顿号、分号）</span>
        <input
          v-model="learning.editTagInput.value"
          type="text"
          placeholder="例如：原神、雷电将军"
          :disabled="learning.busy.value"
        />
      </label>
      <div class="batch-decisions">
        <button
          v-for="option in decisionOptions"
          :key="option.value"
          type="button"
          :data-decision="option.value"
          :disabled="learning.busy.value"
          @click="learning.applyDecisionToSelection(option.value)"
        >
          批量{{ option.label }}
        </button>
      </div>
      <button
        class="learning-button primary submit-review"
        type="button"
        :disabled="learning.busy.value || !learning.pendingDecisionCount.value"
        @click="learning.submitReview"
      >
        提交 {{ learning.pendingDecisionCount.value }} 项审核
      </button>
    </section>

    <div v-if="!learning.queue.value?.samples.length && !learning.busy.value" class="learning-empty">
      <strong>当前没有待学习样本</strong>
      <span>生成新队列后，将按不确定性从高到低排列。</span>
    </div>
    <div v-else ref="gridContainer" class="learning-grid" aria-label="待学习样本列表">
      <article
        v-for="sample in pagedSamples"
        :key="sample.docId"
        class="learning-card"
        :class="{
          selected: learning.selectedIds.value.has(sample.docId),
          decided: Boolean(learning.decisionFor(sample.docId)),
          failed: Boolean(learning.failureFor(sample.docId)),
          'span-2': gridSpan(sample.width, sample.height) === 2,
        }"
        :style="{ gridRow: `span ${cardRowSpan(sample)}` }"
        :data-learning-id="sample.docId"
        :data-candidate-kind="sample.candidateKind"
      >
        <label class="learning-select">
          <input
            type="checkbox"
            :checked="learning.selectedIds.value.has(sample.docId)"
            @change="learning.toggleSelection(sample.docId, ($event.target as HTMLInputElement).checked)"
          />
          <span>选择</span>
        </label>
        <span class="learning-image" @dblclick.prevent="openImage(sample)">
          <img
            v-if="canShowImage(sample)"
            :src="sample.thumbnailUrl || sample.imageUrl"
            :alt="sample.fileName"
            loading="lazy"
            decoding="async"
          />
          <span v-else class="learning-placeholder">□</span>
          <strong>{{ Math.round(sample.uncertaintyScore * 100) }}</strong>
        </span>
        <div class="learning-copy">
          <strong :title="sample.fileName">{{ sample.fileName || sample.docId }}</strong>
          <small :title="sample.relativePath">{{ sample.relativePath }}</small>
          <div class="sample-kind">
            <span>{{ candidateKindLabel(sample.candidateKind) }}</span>
            <span
              v-if="sample.sourceSha256"
              :title="`来源摘要：${sample.sourceSha256}`"
            >
              来源 {{ sample.sourceSha256.slice(0, 8) }}
            </span>
          </div>
          <div class="reason-list">
            <span v-for="reason in sample.reasons" :key="reason" :data-reason="reason">
              {{ reasonLabel(reason) }}
            </span>
          </div>
          <div v-if="sample.suggestedTags.length" class="suggested-tags">
            <span v-for="tag in sample.suggestedTags.slice(0, 3)" :key="tag">{{ tag }}</span>
          </div>
        </div>
        <div class="single-decisions">
          <button
            v-for="option in decisionOptions"
            :key="option.value"
            type="button"
            :class="{ active: learning.decisionFor(sample.docId)?.decision === option.value }"
            :data-decision="option.value"
            :disabled="learning.busy.value"
            @click="setSingleDecision(sample.docId, option.value)"
          >
            {{ option.label }}
          </button>
        </div>
        <div v-if="learning.decisionFor(sample.docId)" class="decision-state" aria-live="polite">
          已设为{{ decisionLabel(learning.decisionFor(sample.docId)!.decision) }}
        </div>
        <div
          v-if="learning.failureFor(sample.docId)"
          class="review-failure"
          role="status"
        >
          <strong>上次未应用</strong>
          <span>{{ learning.failureFor(sample.docId)!.message }}</span>
          <small v-if="learning.failureFor(sample.docId)!.code">
            {{ learning.failureFor(sample.docId)!.code }}
          </small>
        </div>
      </article>
    </div>

    <PaginationBar
      v-if="allSamples.length > LEARNING_PAGE_SIZE"
      id-prefix="learning-panel"
      aria-label="待学习样本分页"
      :page="learningPage"
      :total-pages="learningTotalPages"
      :total-items="allSamples.length"
      :loading="learning.busy.value"
      :has-previous="learningPage > 1"
      :has-next="learningPage < learningTotalPages"
      item-label="张样本"
      @previous="goLearningPage(learningPage - 1)"
      @next="goLearningPage(learningPage + 1)"
      @jump="goLearningPage"
    />

  </section>
</template>

<style scoped>
.learning-panel{display:grid;height:100%;min-height:0;grid-template-rows:auto auto auto auto minmax(0,1fr) auto;gap:12px;overflow:hidden;padding:16px;border:1px solid #e2e6f0;border-radius:18px;color:#17203a;background:rgba(255,255,255,.98);box-shadow:0 12px 34px rgba(44,54,90,.08)}.learning-controls,.decision-toolbar,.selection-buttons,.batch-decisions,.review-summary header{display:flex;align-items:center;justify-content:space-between;gap:10px}.learning-controls label{display:grid;gap:4px;color:#606a80;font-size:10px;font-weight:780}.learning-controls input{width:92px;height:37px;border:1px solid #d9deea;border-radius:9px;padding:0 9px;color:#17203a;background:#fff;font:inherit}.learning-button{min-height:37px;padding:0 12px;border:1px solid transparent;border-radius:9px;font:inherit;font-size:11px;font-weight:800;cursor:pointer}.learning-button:disabled,.batch-decisions button:disabled,.single-decisions button:disabled{cursor:not-allowed;opacity:.45}.learning-button.primary{color:#fff;background:linear-gradient(135deg,#7364eb,#5847d2)}.learning-button.secondary{border-color:#dcd8fa;color:#594bc3;background:#f4f2ff}.learning-button.danger{color:#9d4050;background:#fff0f2}.learning-notice{display:flex;align-items:center;gap:9px;padding:10px 12px;border:1px solid #cceade;border-radius:12px;color:#1d705c;background:#edfaf5}.learning-notice>span{font-size:19px}.learning-notice>div{display:grid;gap:2px}.learning-notice strong{font-size:11px}.learning-notice small{font-size:9px}.learning-feedback{display:grid;gap:7px;min-height:0;overflow:hidden}.review-summary{display:grid;grid-template-columns:minmax(140px,.7fr) minmax(300px,1.3fr);gap:6px 14px;padding:9px 11px;border:1px solid #dedaf8;border-radius:11px;color:#5044aa;background:#f8f7ff}.review-summary header{min-width:0}.review-summary header span{overflow:hidden;color:#8b84ba;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.review-summary dl{display:grid;grid-template-columns:repeat(4,minmax(54px,1fr));gap:5px;margin:0}.review-summary dl div{display:flex;align-items:baseline;justify-content:space-between;gap:5px;padding:4px 7px;border-radius:7px;background:#fff}.review-summary dt{font-size:8px}.review-summary dd{margin:0;font-size:11px;font-weight:850}.review-summary small{grid-column:1/-1;color:#7c759d;font-size:8px}.review-summary .undo-message{color:#34705e}.decision-toolbar{display:grid;grid-template-columns:auto auto minmax(220px,1fr) auto auto;padding:11px;border:1px solid #e5e3f2;border-radius:13px;background:#faf9ff}.selection-summary{display:grid;gap:1px}.selection-summary strong{font-size:11px}.selection-summary span{color:#858da0;font-size:9px}.selection-buttons{gap:5px}.selection-buttons button,.batch-decisions button,.single-decisions button{min-height:22px;padding:0 6px;border:1px solid #dde1ec;border-radius:6px;color:#566078;background:#fff;font:inherit;font-size:9px;font-weight:700;cursor:pointer}.edit-tag-input{display:grid;gap:4px;color:#646d83;font-size:9px;font-weight:750}.edit-tag-input input{height:32px;border:1px solid #d9deea;border-radius:8px;padding:0 9px;color:#17203a;background:#fff;font:inherit;font-size:10px}.batch-decisions{justify-content:flex-start;gap:4px}.batch-decisions button[data-decision="accept"]{color:#16705a;background:#edfaf5}.batch-decisions button[data-decision="reject"]{color:#9d4050;background:#fff0f2}.batch-decisions button[data-decision="edit"]{color:#5647c4;background:#efedff}.submit-review{white-space:nowrap}.learning-grid{display:grid;min-height:0;grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;gap:8px;overflow:auto;padding:1px 3px 8px}.learning-card.span-2{grid-column:span 2}.learning-card{position:relative;display:grid;grid-template-rows:minmax(0,1fr) auto auto;min-width:0;overflow:hidden;border:1px solid #e1e5ee;border-radius:10px;background:#fff;box-shadow:none}.learning-card.selected{border-color:#6b5ce0;box-shadow:0 0 0 2px rgba(107,92,224,.14)}.learning-card.decided{background:#fbfaff}.learning-card.failed{border-color:#e7a5af;background:#fffafb}.learning-select{position:absolute;z-index:2;top:6px;left:6px;display:flex;align-items:center;gap:4px;padding:2px 4px;border-radius:6px;color:#fff;background:rgba(21,27,43,.62);font-size:8px;backdrop-filter:blur(5px)}.learning-select input{margin:0;accent-color:#7464e7}.learning-image{position:relative;display:grid;min-height:0;height:100%;place-items:stretch;overflow:hidden;background:linear-gradient(145deg,#f0f0f0,#e4e4e4)}.learning-image img{width:100%;height:100%;object-fit:contain !important}.learning-image>strong{position:absolute;right:5px;bottom:5px;display:grid;width:22px;height:22px;place-items:center;border-radius:50%;color:#fff;background:rgba(70,70,70,.72);font-size:9px}.learning-placeholder{display:grid;width:40px;height:40px;place-items:center;border:1px solid rgba(255,255,255,.15);border-radius:11px;color:#9da7b9}.learning-copy{display:flex;flex-wrap:wrap;align-items:center;gap:2px 4px;padding:3px 6px;overflow:hidden}.learning-copy>strong{font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%}.learning-copy>small{color:#858d9f;font-size:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%}.sample-kind,.reason-list,.suggested-tags{display:inline-flex;min-height:0;flex-wrap:nowrap;gap:2px;overflow:hidden}.sample-kind span,.reason-list span,.suggested-tags span{padding:0 4px;border-radius:3px;font-size:8px;font-weight:500;line-height:14px;white-space:nowrap}.sample-kind span{color:#536079;background:#eef1f6}.sample-kind span:first-child{color:#5547bd;background:#efedff}.reason-list span,.suggested-tags span{color:#76531e;background:#fff2dc}.reason-list span[data-reason="identity_conflict"]{color:#9c3f50;background:#fff0f2}.reason-list span[data-reason="ranking_disagreement"]{color:#5547bd;background:#efedff}.suggested-tags span{color:#33705d;background:#ebf8f2}.single-decisions{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;padding:2px 4px 4px}.single-decisions button{min-width:0;min-height:22px;padding:0 2px;font-size:9px;border-radius:4px}.single-decisions button.active{border-color:#796bec;color:#fff;background:#6858dd}.decision-state{padding:2px 6px;color:#5849c4;background:#f0eeff;font-size:8px;font-weight:800;text-align:center}.review-failure{display:grid;gap:2px;padding:4px 6px;color:#954150;background:#fff0f2;font-size:8px}.review-failure span{line-height:1.35}.review-failure small{color:#b46c78}.learning-empty{display:grid;min-height:260px;place-items:center;align-content:center;gap:5px;color:#7d8699;text-align:center}.learning-empty span{font-size:11px}
.learning-controls{align-items:flex-end}.edit-tag-input{display:block}.edit-tag-input input{width:100%;height:30px}
@media(max-width:1550px){.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px}.decision-toolbar{grid-template-columns:auto auto minmax(220px,1fr) auto}.submit-review{grid-column:1/-1;justify-self:end}}
@media(max-width:1150px){.learning-panel{height:auto;overflow:visible}.review-summary{grid-template-columns:1fr}.decision-toolbar{grid-template-columns:1fr 1fr}.edit-tag-input{grid-column:1/-1}.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;overflow:visible}.submit-review{grid-column:auto;justify-self:stretch}}
@media(max-width:760px){.learning-controls{align-items:stretch;flex-wrap:wrap}.review-summary dl{grid-template-columns:repeat(2,minmax(54px,1fr))}.decision-toolbar{grid-template-columns:1fr}.edit-tag-input,.submit-review{grid-column:auto}.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px}}
@media(max-width:500px){.learning-grid{grid-template-columns:1fr}}

/* The approved desktop UI presents learning candidates as quiet review rows,
   not a wall of dashboard cards. */
.learning-panel{height:100%;padding:0;border:0;border-radius:0;color:#242424;background:#fff;box-shadow:none;overflow:hidden}
.learning-controls{align-items:flex-end}.learning-controls input{border-color:#dedede;border-radius:8px;color:#242424;background:#fff}
.learning-button{border-radius:8px;font-weight:500}.learning-button.primary{color:#fff;background:#606060}.learning-button.secondary{border-color:#dedede;color:#333;background:#f3f3f3}
.learning-notice{border-color:#d8e8e1;color:#426d5f;background:#f3faf7}
.review-summary,.decision-toolbar{border-color:#e2e2e2;color:#4b4b4b;background:#f7f7f7}
.review-summary dl div{background:#fff}
.selection-buttons button,.batch-decisions button,.single-decisions button{border-color:#dedede;border-radius:8px;color:#4b4b4b;background:#f5f5f5;font-weight:500}
.edit-tag-input input{border-color:#dedede;color:#242424;background:#fff}
.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;gap:8px;padding:2px 3px 8px 1px;overflow:auto}
.learning-card.span-2{grid-column:span 2}
.learning-card{display:grid;grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) auto auto;align-items:stretch;border:1px solid #e5e5e5;border-radius:10px;background:#fff;box-shadow:none;overflow:hidden;padding:0;min-height:0}
.learning-card.selected{border-color:#999;background:#f8f8f8;box-shadow:none}.learning-card.decided{background:#fafafa}
.learning-select{position:absolute;z-index:2;top:6px;left:6px;padding:0;color:#555;background:rgba(255,255,255,.85);border-radius:6px;width:22px;height:22px;display:grid;place-items:center;backdrop-filter:none}.learning-select input{accent-color:#777;margin:0}.learning-select span{display:none}
.learning-image{position:relative;width:100%;min-height:0;height:100%;border-radius:0;background:linear-gradient(140deg,#f2f2f2,#e5e5e5);overflow:hidden}.learning-image img{width:100%;height:100%;object-fit:contain !important}
.learning-image>strong{position:absolute;right:5px;bottom:5px;width:22px;height:22px;border:0;border-radius:50%;color:#fff;background:rgb(70 70 70 / 72%);font-size:9px;display:grid;place-items:center}
.learning-copy{padding:3px 6px;overflow:hidden;display:flex;flex-wrap:wrap;align-items:center;gap:2px 4px}.learning-copy>strong{font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%}.learning-copy>small{color:#777;font-size:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%}
.sample-kind,.reason-list,.suggested-tags{min-height:0;display:inline-flex;flex-wrap:nowrap;gap:2px;overflow:hidden}.sample-kind span,.reason-list span,.suggested-tags span{padding:0 4px;border-radius:3px;font-size:8px;font-weight:500;line-height:14px;white-space:nowrap}.sample-kind span{color:#536079;background:#eef1f6}.sample-kind span:first-child{color:#5547bd;background:#efedff}.reason-list span{color:#76531e;background:#fff2dc}.reason-list span[data-reason="identity_conflict"]{color:#9c3f50;background:#fff0f2}.reason-list span[data-reason="ranking_disagreement"]{color:#5547bd;background:#efedff}.suggested-tags span{color:#33705d;background:#ebf8f2}
.single-decisions{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;padding:2px 4px 4px}.single-decisions button{min-height:22px;padding:0 2px;font-size:9px;border-radius:4px}.single-decisions button.active{border-color:#555;color:#fff;background:#555;box-shadow:none}
.decision-state,.review-failure{grid-column:1/-1}.decision-state{color:#555;background:#ededed;font-size:8px;padding:2px 6px}
@media(max-width:1180px){.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;overflow:auto}}
@media(max-width:760px){.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;overflow:auto}.learning-notice{flex-wrap:wrap}.learning-notice .learning-controls{width:100%}}
@media(max-width:500px){.learning-grid{grid-template-columns:1fr;grid-template-rows:none;overflow:auto}}
.learning-panel{display:grid;height:100%;min-height:0;grid-template-rows:auto auto auto minmax(0,1fr) auto;gap:0;overflow:hidden;border:0;border-radius:0;color:#222;background:#fff;box-shadow:none}
.learning-notice{display:flex;align-items:center;gap:10px;margin:0 0 12px;padding:9px 12px;border:1px solid rgb(36 122 89 / 18%);border-radius:10px;color:#555;background:rgb(36 122 89 / 6%)}.learning-notice>span{color:#47806b;flex-shrink:0}.learning-notice-body{flex:1;min-width:0}.learning-notice-body strong{font-size:12px;font-weight:500}.learning-notice-body small{display:block;margin-top:2px;color:#777;font-size:10px}.learning-notice .learning-controls{display:flex;align-items:flex-end;gap:7px;flex-shrink:0}.learning-notice .learning-controls label{display:grid;gap:3px;color:#777;font-size:10px}.learning-notice .learning-controls input{width:76px;height:34px;padding:0 9px;border:1px solid #dedede;border-radius:8px;background:#fff}.learning-notice .learning-button{min-height:34px;padding:0 10px;border:1px solid #dedede;border-radius:8px;font-weight:400}.learning-notice .learning-button.primary{color:#fff;border-color:#aaa;background:#aaa}.learning-notice .learning-button.secondary{color:#555;background:#f5f5f5}
.learning-feedback{display:grid;gap:7px;min-height:0;overflow:hidden}.review-summary{border-color:#e1e1e1!important;border-radius:8px!important;color:#555!important;background:#f6f6f6!important}
.decision-toolbar{display:grid;min-height:56px;grid-template-columns:auto auto minmax(160px,1fr) auto auto;align-items:center;gap:8px;margin:12px 0;padding:8px 10px;border:0;border-radius:10px;background:#f2f2f2}.selection-summary{display:flex;align-items:center;gap:7px}.selection-summary strong{font-size:12px;font-weight:500}.selection-buttons,.batch-decisions{display:flex;gap:5px}.selection-buttons button,.batch-decisions button{min-height:30px;padding:0 8px;border:1px solid #dedede;border-radius:8px;color:#444;background:#fff}.edit-tag-input input{width:100%;height:34px;border:1px solid #dedede;border-radius:8px;background:#fff}.submit-review{white-space:nowrap}
.learning-empty{display:grid;min-height:0;place-items:center;align-content:center;color:#777}.learning-empty strong{font-weight:500}.learning-empty span{margin-top:4px;font-size:11px}.learning-grid{grid-template-columns:repeat(2,1fr);grid-auto-flow:row dense;grid-auto-rows:10px;gap:8px;padding:2px 3px 8px 1px;overflow:auto}.learning-card.span-2{grid-column:span 2}
</style>
