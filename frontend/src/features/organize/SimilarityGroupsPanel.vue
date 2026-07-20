<script setup lang="ts">
import { onMounted, toRef } from "vue";

import type {
  ClusterIdentityCategory,
  ClusterRunType,
  ClusterType,
  IntelligenceImage,
} from "./intelligenceTypes";
import type { OrganizeApi, ToastKind } from "./types";
import { useSimilarityGroups } from "./useOrganizeIntelligence";

const props = defineProps<{
  api?: OrganizeApi;
  libraryId: string;
}>();

const emit = defineEmits<{
  toast: [title: string, message: string, kind: ToastKind];
  openImage: [imageId: string];
}>();

const groups = useSimilarityGroups(toRef(props, "libraryId"), props.api, {
  onToast: (title, message, kind) => emit("toast", title, message, kind),
});

const runTypeOptions: Array<{ value: ClusterRunType; label: string; description: string }> = [
  { value: "exact", label: "完全重复", description: "SHA-256 完全一致" },
  { value: "perceptual", label: "视觉近似", description: "可选感知哈希" },
  {
    value: "semantic",
    label: "内容相似",
    description: "大图库耗时较长，按需启用",
  },
];

const filterOptions: Array<{ value: ClusterType; label: string }> = [
  { value: "all", label: "全部分组" },
  { value: "exact", label: "完全重复" },
  { value: "perceptual", label: "视觉近似" },
  { value: "semantic", label: "内容相似" },
  { value: "single", label: "暂未成组" },
];

const identityCategoryOptions: Array<{
  value: ClusterIdentityCategory;
  label: string;
}> = [
  { value: "real_person", label: "真人" },
  { value: "cosplayer", label: "Cosplayer" },
  { value: "character", label: "角色" },
  { value: "work", label: "作品" },
];

function typeLabel(value: ClusterType): string {
  return filterOptions.find((option) => option.value === value)?.label ?? value;
}

function sourceLabel(value: string): string {
  return {
    manual: "人工",
    folder: "文件夹",
    accepted: "已采用标签",
    model: "模型",
    inherited: "继承",
  }[value] ?? value;
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

onMounted(() => {
  if (props.libraryId) void groups.loadClusters(0);
});
</script>

<template>
  <section class="intelligence-panel" aria-labelledby="similarity-groups-title">
    <header class="intelligence-heading">
      <div>
        <p class="intelligence-eyebrow">本地智能整理</p>
        <h2 id="similarity-groups-title">相似分组</h2>
        <p>默认查找重复图和近似图；内容相似可按需启用。</p>
      </div>
      <div class="no-cost-badge" title="聚类不会请求阿里云模型">
        <span aria-hidden="true">◇</span>
        不调用模型 · 无新增费用
      </div>
    </header>

    <div class="cluster-controls">
      <label>
        <span>处理范围</span>
        <select v-model="groups.scope.value" :disabled="groups.busy.value">
          <option value="new_or_changed">仅新增或变化图片（推荐）</option>
          <option value="all">重新检查全部图片</option>
        </select>
      </label>
      <fieldset :disabled="groups.busy.value">
        <legend>分组依据</legend>
        <label
          v-for="option in runTypeOptions"
          :key="option.value"
          class="cluster-type-option"
        >
          <input
            type="checkbox"
            :checked="groups.clusterTypes.value.includes(option.value)"
            @change="groups.toggleClusterType(option.value)"
          />
          <span>
            <strong>{{ option.label }}</strong>
            <small>{{ option.description }}</small>
          </span>
        </label>
      </fieldset>
      <div class="cluster-run-actions">
        <button
          v-if="groups.busy.value"
          class="intelligence-button quiet danger"
          type="button"
          :disabled="groups.cancelling.value"
          @click="groups.cancel"
        >
          {{ groups.cancelling.value ? "正在取消…" : "取消当前操作" }}
        </button>
        <button
          class="intelligence-button primary"
          type="button"
          :disabled="groups.busy.value || !libraryId"
          @click="groups.runClustering"
        >
          {{ groups.busy.value ? "后台处理中…" : "运行增量聚类" }}
        </button>
      </div>
    </div>

    <div class="cluster-feedback">
      <p
        v-if="groups.clusterTypes.value.includes('semantic')"
        class="semantic-performance-note"
        role="status"
      >
        内容相似会逐张读取已有向量，大图库可能需要较长时间；任务可取消，不调用模型，也不会产生 API 费用。
      </p>
      <div v-if="groups.busy.value || groups.statusMessage.value" class="operation-status" aria-live="polite">
        <div>
          <strong>{{ groups.statusMessage.value || "正在处理" }}</strong>
          <span>{{ groups.progressPercent.value }}%</span>
        </div>
        <progress :value="groups.progressPercent.value" max="100" />
      </div>
      <section
        v-if="groups.lastOperationResult.value"
        class="cluster-operation-summary"
        aria-label="最近一次分组整理结果"
        aria-live="polite"
      >
        <strong>最近整理结果</strong>
        <dl>
          <div><dt>已应用</dt><dd>{{ groups.lastOperationResult.value.applied }}</dd></div>
          <div><dt>失败</dt><dd>{{ groups.lastOperationResult.value.failed }}</dd></div>
          <div><dt>冲突</dt><dd>{{ groups.lastOperationResult.value.conflicts }}</dd></div>
        </dl>
        <small v-if="groups.lastOperationResult.value.batchId">
          批次 {{ groups.lastOperationResult.value.batchId }}
        </small>
      </section>
    </div>

    <header class="cluster-list-heading">
      <div>
        <strong>分组结果</strong>
        <span>{{ groups.total.value }} 组</span>
      </div>
      <div class="cluster-list-tools">
        <output>已选 {{ groups.selectedClusterCount.value }} 组</output>
        <button
          class="intelligence-button primary"
          type="button"
          :disabled="groups.busy.value || groups.selectedClusterCount.value < 2"
          @click="groups.mergeSelectedClusters"
        >
          合并所选
        </button>
        <button
          class="intelligence-button quiet"
          type="button"
          :disabled="groups.busy.value || !groups.selectedClusterCount.value"
          @click="groups.clearClusterSelection"
        >
          清空选择
        </button>
        <button
          class="intelligence-button quiet"
          type="button"
          :disabled="groups.busy.value || !groups.undoAvailable.value"
          @click="groups.undoLastClusterOperation"
        >
          撤销最近一次整理
        </button>
        <label>
          <span>类型过滤</span>
          <select v-model="groups.clusterTypeFilter.value" :disabled="groups.busy.value">
            <option v-for="option in filterOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
      </div>
    </header>

    <div v-if="!groups.clusters.value.length && !groups.busy.value" class="intelligence-empty">
      <strong>暂无相似分组</strong>
      <span>运行增量聚类，或切换上方类型过滤。</span>
    </div>
    <div v-else class="cluster-grid" aria-label="相似图片分组">
      <article
        v-for="cluster in groups.clusters.value"
        :key="cluster.id"
        class="cluster-card"
        :class="{ selected: groups.selectedClusterIds.value.has(cluster.id) }"
        :data-cluster-id="cluster.id"
      >
        <label class="cluster-select">
          <input
            type="checkbox"
            :checked="groups.selectedClusterIds.value.has(cluster.id)"
            :disabled="groups.busy.value"
            @change="groups.toggleClusterSelection(cluster.id, ($event.target as HTMLInputElement).checked)"
          />
          <span>选择分组</span>
        </label>
        <button
          class="cluster-preview"
          type="button"
          :disabled="groups.busy.value"
          @click="groups.openCluster(cluster.id, 0)"
        >
          <span class="cluster-image-stage" @dblclick.stop.prevent="openImage(cluster.representative)">
            <img
              v-if="canShowImage(cluster.representative)"
              :src="cluster.representative.thumbnailUrl || cluster.representative.imageUrl"
              :alt="cluster.representative.fileName"
              loading="lazy"
              decoding="async"
            />
            <span v-else class="cluster-placeholder" aria-label="图片不可用">□</span>
            <span class="member-count">{{ cluster.memberCount }} 张</span>
          </span>
          <span class="cluster-copy">
            <span class="cluster-title-row">
              <strong :title="cluster.representative.fileName">
                {{ cluster.representative.fileName || "未命名分组" }}
              </strong>
              <span class="cluster-type-chip" :data-type="cluster.type">
                {{ typeLabel(cluster.type) }}
              </span>
            </span>
            <small :title="cluster.representative.relativePath">
              {{ cluster.representative.relativePath || cluster.id }}
            </small>
          </span>
        </button>
        <div v-if="cluster.identityAnchors.length" class="anchor-list">
          <span
            v-for="anchor in cluster.identityAnchors.slice(0, 3)"
            :key="`${anchor.category}-${anchor.value}`"
            class="anchor-chip"
            :class="{ conflict: anchor.conflict }"
            :title="`${sourceLabel(anchor.source)} · ${Math.round(anchor.confidence * 100)}%`"
          >
            {{ anchor.value }}
          </span>
        </div>
      </article>
    </div>

    <footer class="cluster-pagination">
      <span>第 {{ groups.page.value }} / {{ groups.totalPages.value }} 页</span>
      <div>
        <button
          class="intelligence-button quiet"
          type="button"
          :disabled="groups.busy.value || groups.offset.value <= 0"
          @click="groups.previousPage"
        >
          上一页
        </button>
        <button
          class="intelligence-button quiet"
          type="button"
          :disabled="groups.busy.value || groups.offset.value + groups.limit.value >= groups.total.value"
          @click="groups.nextPage"
        >
          下一页
        </button>
      </div>
    </footer>

    <aside class="propagation-policy" aria-label="身份标签传播规则">
      <strong>批量传播边界</strong>
      <span>只允许真人、Cosplayer、角色和作品身份；绝不传播动作、神态、场景、镜头或构图。</span>
    </aside>

    <div v-if="groups.detail.value" class="cluster-modal-backdrop" @click.self="groups.closeDetail">
      <section class="cluster-modal" role="dialog" aria-modal="true" aria-labelledby="cluster-detail-title">
        <header>
          <div>
            <p class="intelligence-eyebrow">分组详情</p>
            <h3 id="cluster-detail-title">
              {{ groups.detail.value.cluster.memberCount }} 张相似图片
            </h3>
          </div>
          <button type="button" aria-label="关闭分组详情" @click="groups.closeDetail">×</button>
        </header>
        <p>单击选择错误成员，双击缩略图调用系统查看原图；人工操作会写入分组或身份标签，并支持最近一次撤销。</p>
        <form class="cluster-identity-editor" @submit.prevent="groups.applyIdentityToCluster">
          <label>
            <span>身份类别</span>
            <select v-model="groups.identityCategory.value" :disabled="groups.busy.value">
              <option
                v-for="option in identityCategoryOptions"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </option>
            </select>
          </label>
          <label class="identity-value-input">
            <span>身份名称</span>
            <input
              v-model="groups.identityValue.value"
              type="text"
              maxlength="256"
              placeholder="例如：雷电将军、原神、某位 Cosplayer"
              :disabled="groups.busy.value"
            />
          </label>
          <button
            class="intelligence-button primary"
            type="submit"
            :disabled="groups.busy.value || !groups.identityValue.value.trim()"
          >
            整组添加身份标签
          </button>
        </form>
        <div class="cluster-member-toolbar">
          <strong>已选 {{ groups.selectedMemberCount.value }} 张错误图片</strong>
          <div>
            <button
              class="intelligence-button primary"
              type="button"
              :disabled="groups.busy.value || !groups.selectedMemberCount.value"
              @click="groups.splitSelectedMembers"
            >
              拆分所选
            </button>
            <button
              class="intelligence-button quiet"
              type="button"
              :disabled="groups.busy.value || !groups.selectedMemberCount.value"
              @click="groups.clearMemberSelection"
            >
              清空选择
            </button>
          </div>
        </div>
        <div class="cluster-member-grid">
          <button
            v-for="image in groups.detail.value.members"
            :key="image.docId"
            class="cluster-member"
            :class="{ selected: groups.selectedMemberIds.value.has(image.docId) }"
            type="button"
            :title="image.relativePath"
            :aria-pressed="groups.selectedMemberIds.value.has(image.docId)"
            :disabled="groups.busy.value"
            @click="groups.toggleMemberSelection(image.docId)"
            @dblclick.prevent="openImage(image)"
          >
            <span>
              <img
                v-if="canShowImage(image)"
                :src="image.thumbnailUrl || image.imageUrl"
                :alt="image.fileName"
                loading="lazy"
              />
              <span v-else class="cluster-placeholder">□</span>
            </span>
            <strong>{{ image.fileName || image.docId }}</strong>
          </button>
        </div>
        <footer class="cluster-detail-pagination">
          <span>
            第 {{ groups.detailPage.value }} / {{ groups.detailTotalPages.value }} 页 ·
            共 {{ groups.detail.value.total }} 张
          </span>
          <div>
            <button
              class="intelligence-button quiet"
              type="button"
              :disabled="groups.busy.value || groups.detailOffset.value <= 0"
              @click="groups.previousDetailPage"
            >
              上一页
            </button>
            <button
              class="intelligence-button quiet"
              type="button"
              :disabled="groups.busy.value || groups.detailOffset.value + groups.detailLimit.value >= groups.detail.value.total"
              @click="groups.nextDetailPage"
            >
              下一页
            </button>
          </div>
        </footer>
      </section>
    </div>
  </section>
</template>

<style scoped>
.intelligence-panel{display:grid;height:100%;min-height:0;grid-template-rows:auto auto auto auto minmax(0,1fr) auto auto;gap:12px;overflow:hidden;padding:16px;border:1px solid #e2e6f0;border-radius:18px;color:#17203a;background:rgba(255,255,255,.98);box-shadow:0 12px 34px rgba(44,54,90,.08)}
.intelligence-heading,.cluster-list-heading,.cluster-pagination,.cluster-run-actions,.cluster-title-row,.operation-status>div,.cluster-modal>header{display:flex;align-items:center;justify-content:space-between;gap:12px}.intelligence-heading h2{margin:2px 0 0;font-size:22px}.intelligence-heading p:not(.intelligence-eyebrow){margin:5px 0 0;color:#737c91;font-size:12px}.intelligence-eyebrow{margin:0;color:#6557e8;font-size:10px;font-weight:850;letter-spacing:.09em;text-transform:uppercase}.no-cost-badge{display:flex;align-items:center;gap:7px;padding:8px 11px;border:1px solid #caeadf;border-radius:999px;color:#176c57;background:#edfaf5;font-size:11px;font-weight:800;white-space:nowrap}.cluster-controls{display:grid;grid-template-columns:minmax(210px,.8fr) minmax(440px,1.7fr) auto;align-items:end;gap:12px;padding:12px;border:1px solid #e5e3f6;border-radius:14px;background:#faf9ff}.cluster-controls>label,.cluster-list-heading label{display:grid;gap:5px;color:#59637b;font-size:11px;font-weight:780}.cluster-controls select,.cluster-list-heading select{height:39px;border:1px solid #d9deea;border-radius:10px;padding:0 10px;color:#17203a;background:#fff;font:inherit}.cluster-controls fieldset{display:flex;min-width:0;gap:7px;margin:0;padding:0;border:0}.cluster-controls legend{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.cluster-type-option{display:flex;flex:1;align-items:center;gap:7px;min-width:0;padding:7px 9px;border:1px solid #e0ddee;border-radius:10px;background:#fff;cursor:pointer}.cluster-type-option input{accent-color:#6557df}.cluster-type-option span{display:grid;min-width:0;gap:1px}.cluster-type-option strong{font-size:11px}.cluster-type-option small{overflow:hidden;color:#858da0;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.intelligence-button{min-height:36px;padding:0 12px;border:1px solid transparent;border-radius:9px;font:inherit;font-size:11px;font-weight:800;cursor:pointer}.intelligence-button:disabled{cursor:not-allowed;opacity:.45}.intelligence-button.primary{color:#fff;background:linear-gradient(135deg,#7364eb,#5847d2)}.intelligence-button.quiet{border-color:#dfe3ec;color:#535d73;background:#fff}.intelligence-button.danger{color:#9f4050;background:#fff3f4}.operation-status{display:grid;gap:6px;padding:9px 11px;border-radius:11px;color:#5447bb;background:#f1efff;font-size:11px}.operation-status progress{width:100%;height:6px;border:0;accent-color:#6557df}.cluster-list-heading>div{display:flex;align-items:baseline;gap:8px}.cluster-list-heading>div strong{font-size:14px}.cluster-list-heading>div span{color:#81899b;font-size:11px}.cluster-list-heading label{grid-template-columns:auto 150px;align-items:center}.cluster-list-heading select{height:35px}.cluster-grid{display:grid;min-height:0;grid-template-columns:repeat(4,minmax(0,1fr));align-content:start;gap:10px;overflow:auto;padding:1px 3px 8px}.cluster-card{min-width:0;overflow:hidden;border:1px solid #e1e5ee;border-radius:13px;background:#fff;box-shadow:0 6px 18px rgba(47,55,82,.055)}.cluster-preview{display:grid;width:100%;padding:0;border:0;color:inherit;background:transparent;text-align:left;cursor:pointer}.cluster-image-stage{position:relative;display:grid;height:155px;place-items:center;overflow:hidden;background:linear-gradient(145deg,#151b2b,#272e42)}.cluster-image-stage img,.cluster-member>span img{width:100%;height:100%;object-fit:contain}.cluster-placeholder{display:grid;width:42px;height:42px;place-items:center;border:1px solid rgba(255,255,255,.15);border-radius:12px;color:#9da7b9;background:rgba(255,255,255,.05)}.member-count{position:absolute;right:8px;bottom:8px;padding:4px 7px;border-radius:999px;color:#fff;background:rgba(22,27,45,.72);font-size:10px;font-weight:850;backdrop-filter:blur(6px)}.cluster-copy{display:grid;gap:4px;padding:9px}.cluster-title-row{min-width:0}.cluster-title-row strong{overflow:hidden;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.cluster-copy>small{overflow:hidden;color:#858d9f;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.cluster-type-chip{padding:3px 6px;border-radius:999px;color:#5547be;background:#efedff;font-size:9px;font-weight:800;white-space:nowrap}.cluster-type-chip[data-type="exact"]{color:#176c57;background:#e9f8f2}.cluster-type-chip[data-type="perceptual"]{color:#8b5b18;background:#fff3df}.anchor-list{display:flex;min-height:28px;gap:4px;padding:0 9px 9px;overflow:hidden}.anchor-chip{max-width:100%;overflow:hidden;padding:3px 6px;border-radius:999px;color:#4f43b2;background:#f0eeff;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.anchor-chip.conflict{color:#9d3e4f;background:#fff0f2}.cluster-pagination{min-height:40px;border-top:1px solid #edf0f5;color:#788196;font-size:11px}.cluster-pagination>div{display:flex;gap:7px}.propagation-policy{display:flex;align-items:center;gap:9px;padding:9px 11px;border:1px solid #f0dfbf;border-radius:11px;color:#76521f;background:#fff9ec;font-size:10px}.propagation-policy strong{white-space:nowrap}.intelligence-empty{display:grid;min-height:220px;place-items:center;align-content:center;gap:5px;color:#7d8699;text-align:center}.intelligence-empty span{font-size:11px}.cluster-modal-backdrop{position:fixed;z-index:55;inset:0;display:grid;place-items:center;padding:24px;background:rgba(16,22,38,.55);backdrop-filter:blur(5px)}.cluster-modal{display:grid;width:min(960px,100%);max-height:calc(100vh - 48px);grid-template-rows:auto auto minmax(0,1fr);gap:12px;overflow:hidden;padding:17px;border:1px solid #e2e5ee;border-radius:18px;background:#fff;box-shadow:0 28px 90px rgba(20,24,39,.3)}.cluster-modal h3{margin:2px 0 0;font-size:19px}.cluster-modal>header>button{width:32px;height:32px;border:0;border-radius:9px;color:#754b5a;background:#fff0f2;font-size:18px}.cluster-modal>p{margin:0;color:#70798e;font-size:11px}.cluster-member-grid{display:grid;min-height:0;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;overflow:auto}.cluster-member{display:grid;min-width:0;gap:6px;padding:6px;border:1px solid #e3e6ef;border-radius:11px;color:inherit;background:#fafbfe;text-align:left}.cluster-member>span{display:grid;height:130px;place-items:center;overflow:hidden;border-radius:8px;background:#171d2d}.cluster-member strong{overflow:hidden;font-size:10px;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:1450px){.cluster-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.cluster-controls{grid-template-columns:minmax(190px,.7fr) minmax(360px,1.5fr)}.cluster-run-actions{grid-column:1/-1;justify-content:flex-end}}
@media(max-width:980px){.intelligence-panel{height:auto;overflow:visible}.intelligence-heading{align-items:flex-start;flex-direction:column}.cluster-controls{grid-template-columns:1fr}.cluster-controls fieldset{flex-wrap:wrap}.cluster-type-option{min-width:150px}.cluster-grid{grid-template-columns:repeat(2,minmax(0,1fr));max-height:none;overflow:visible}.cluster-member-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:620px){.cluster-grid{grid-template-columns:1fr}.cluster-controls fieldset{display:grid;grid-template-columns:1fr}.cluster-list-heading{align-items:stretch;flex-direction:column}.cluster-list-heading label{grid-template-columns:1fr}.cluster-member-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.propagation-policy{align-items:flex-start;flex-direction:column}}
.cluster-feedback{display:grid;gap:7px;min-height:0}.semantic-performance-note{margin:0;padding:9px 11px;border:1px solid #e4d9a7;border-radius:11px;color:#69551e;background:#fffbea;font-size:10px;line-height:1.5}.cluster-operation-summary{display:grid;grid-template-columns:auto minmax(240px,1fr) auto;align-items:center;gap:10px;padding:8px 11px;border:1px solid #dedaf8;border-radius:11px;color:#5044aa;background:#f8f7ff}.cluster-operation-summary dl{display:grid;grid-template-columns:repeat(3,minmax(60px,1fr));gap:5px;margin:0}.cluster-operation-summary dl div{display:flex;align-items:baseline;justify-content:space-between;gap:5px;padding:4px 7px;border-radius:7px;background:#fff}.cluster-operation-summary dt{font-size:8px}.cluster-operation-summary dd{margin:0;font-size:11px;font-weight:850}.cluster-operation-summary small{overflow:hidden;color:#8b84ba;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.cluster-list-heading>div:first-child{display:flex;align-items:baseline;gap:8px}.cluster-list-tools{display:flex;align-items:center;justify-content:flex-end;gap:7px}.cluster-list-tools>output{color:#6256b7;font-size:10px;font-weight:800}.cluster-card{position:relative}.cluster-card.selected{border-color:#6b5ce0;box-shadow:0 0 0 2px rgba(107,92,224,.14)}.cluster-select{position:absolute;z-index:3;top:8px;left:8px;display:flex;align-items:center;gap:4px;padding:4px 6px;border-radius:8px;color:#fff;background:rgba(21,27,43,.68);font-size:8px;backdrop-filter:blur(5px)}.cluster-select input{margin:0;accent-color:#7464e7}.cluster-preview:disabled{cursor:wait}.cluster-modal{width:min(1040px,100%);grid-template-rows:auto auto auto auto minmax(0,1fr) auto}.cluster-identity-editor{display:grid;grid-template-columns:160px minmax(260px,1fr) auto;align-items:end;gap:9px;padding:10px;border:1px solid #e4e1f5;border-radius:12px;background:#faf9ff}.cluster-identity-editor label{display:grid;gap:5px;color:#59637b;font-size:11px;font-weight:780}.cluster-identity-editor select,.cluster-identity-editor input{height:39px;border:1px solid #d9deea;border-radius:10px;padding:0 10px;color:#17203a;background:#fff;font:inherit}.identity-value-input{min-width:0}.cluster-member-toolbar,.cluster-detail-pagination{display:flex;align-items:center;justify-content:space-between;gap:12px}.cluster-member-toolbar{padding:8px 10px;border:1px solid #e5e8ef;border-radius:11px;color:#5e687f;background:#fafbfe;font-size:10px}.cluster-member-toolbar>div,.cluster-detail-pagination>div{display:flex;gap:7px}.cluster-member{cursor:pointer}.cluster-member.selected{border-color:#6b5ce0;background:#f3f1ff;box-shadow:0 0 0 2px rgba(107,92,224,.13)}.cluster-member:disabled{cursor:wait;opacity:.7}.cluster-detail-pagination{min-height:38px;border-top:1px solid #edf0f5;color:#788196;font-size:10px}
@media(max-width:1180px){.cluster-list-heading{align-items:flex-start;flex-direction:column}.cluster-list-tools{width:100%;flex-wrap:wrap;justify-content:flex-start}.cluster-operation-summary{grid-template-columns:1fr auto}.cluster-operation-summary dl{grid-column:1/-1;grid-row:2}}
@media(max-width:980px){.cluster-identity-editor{grid-template-columns:1fr 2fr}.cluster-identity-editor button{grid-column:1/-1}}
@media(max-width:620px){.cluster-list-tools{display:grid;grid-template-columns:1fr 1fr}.cluster-list-tools label,.cluster-list-tools output{grid-column:1/-1}.cluster-operation-summary{grid-template-columns:1fr}.cluster-operation-summary dl{grid-template-columns:1fr}.cluster-identity-editor{grid-template-columns:1fr}.cluster-member-toolbar,.cluster-detail-pagination{align-items:flex-start;flex-direction:column}}
</style>
