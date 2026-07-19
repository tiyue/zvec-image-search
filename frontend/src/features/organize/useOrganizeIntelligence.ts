import {
  computed,
  onBeforeUnmount,
  ref,
  watch,
  type Ref,
} from "vue";

import { organizeApi } from "./api";
import type {
  ActiveLearningQueueView,
  ClusterIdentityCategory,
  ClusterOperationResult,
  ClusterRunType,
  ClusterScope,
  ClusterType,
  IdentityAnchorView,
  IntelligenceImage,
  LearningDecisionDraft,
  LearningDecisionValue,
  LearningCandidateKind,
  LearningReason,
  LearningReviewFailure,
  LearningReviewResult,
  LearningSample,
  SimilarityCluster,
  SimilarityClusterDetail,
  SimilarityClusterPage,
} from "./intelligenceTypes";
import type {
  OrganizeApi,
  OrganizeJobResponse,
  OrganizeJobWire,
  ToastKind,
} from "./types";
import { parseTagInput } from "./useOrganize";

const TERMINAL_STATUSES = new Set([
  "succeeded",
  "completed",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
  "interrupted",
]);
const SUCCESS_STATUSES = new Set([
  "succeeded",
  "completed",
  "partial",
  "needs_attention",
]);
const ACTIVE_LEARNING_REASONS = new Set<LearningReason>([
  "identity_conflict",
  "cluster_outlier",
  "ranking_disagreement",
  "combined_uncertainty",
  "low_information",
]);
const ACTIVE_LEARNING_CANDIDATE_KINDS = new Set<LearningCandidateKind>([
  "tag_review",
  "search_result",
  "cluster_membership",
]);
const CLUSTER_RUN_TYPES = new Set<ClusterRunType>([
  "exact",
  "perceptual",
  "semantic",
]);
const CLUSTER_TYPES = new Set<ClusterType>([
  "all",
  "exact",
  "perceptual",
  "semantic",
  "single",
]);
const CLUSTER_IDENTITY_CATEGORIES = new Set<ClusterIdentityCategory>([
  "real_person",
  "cosplayer",
  "character",
  "work",
]);

interface IntelligenceEvents {
  onToast?: (title: string, message: string, kind: ToastKind) => void;
  pollIntervalMs?: number;
  timeoutMs?: number;
}

interface NormalizedJob {
  id: string;
  status: string;
  message: string;
  percent: number;
  result: Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function integer(value: unknown, fallback = 0): number {
  return Math.trunc(numberValue(value, fallback));
}

function boundedScore(value: unknown): number {
  return Math.min(1, Math.max(0, numberValue(value)));
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => text(item)).filter(Boolean))];
}

function normalizeCandidateKind(value: unknown): LearningCandidateKind {
  const normalized = text(value).toLowerCase();
  return ACTIVE_LEARNING_CANDIDATE_KINDS.has(
    normalized as LearningCandidateKind,
  )
    ? (normalized as LearningCandidateKind)
    : "tag_review";
}

function normalizeSourceSha256(value: unknown): string {
  const normalized = text(value).toLowerCase();
  return /^[0-9a-f]{64}$/u.test(normalized) ? normalized : "";
}

function safeLocalUrl(value: unknown): string {
  const candidate = text(value);
  if (!candidate) return "";
  try {
    const url = new URL(candidate, document.baseURI);
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(
      url.hostname,
    );
    const supported = url.protocol === "http:" || url.protocol === "https:";
    return supported && (url.origin === window.location.origin || loopback)
      ? url.href
      : "";
  } catch {
    return "";
  }
}

function normalizeImage(raw: Record<string, unknown>): IntelligenceImage {
  const docId = text(raw.doc_id, raw.id, raw.image_id);
  const fileName = text(
    raw.file_name,
    raw.filename,
    raw.name,
    text(raw.relative_path).split(/[\\/]/u).filter(Boolean).at(-1),
    docId,
  );
  const thumbnailUrl = safeLocalUrl(raw.thumbnail_url);
  const imageUrl = safeLocalUrl(raw.image_url);
  return {
    docId,
    nativeImageId: text(raw.image_id, raw.native_image_id),
    fileName,
    relativePath: text(raw.relative_path),
    thumbnailUrl,
    imageUrl,
    imageAvailable:
      raw.image_available === true || Boolean(thumbnailUrl || imageUrl),
  };
}

function normalizeAnchor(raw: Record<string, unknown>): IdentityAnchorView | null {
  const category = text(raw.category, raw.entity_type);
  const value = text(raw.value, raw.name, raw.tag);
  if (!category || !value) return null;
  return {
    category,
    value,
    source: text(raw.source, "model"),
    confidence: boundedScore(raw.confidence),
    conflict: raw.conflict === true,
  };
}

function normalizeCluster(raw: Record<string, unknown>): SimilarityCluster | null {
  const id = text(raw.cluster_id, raw.id);
  if (!id) return null;
  const members = records(raw.members ?? raw.items);
  const representativeRaw =
    record(raw.representative) ??
    record(raw.representative_image) ??
    members[0] ??
    {
      doc_id: raw.representative_doc_id,
      image_id: raw.representative_image_id,
      file_name: raw.representative_file_name,
      relative_path: raw.representative_relative_path,
      thumbnail_url: raw.representative_thumbnail_url,
      image_url: raw.representative_image_url,
      image_available: raw.representative_image_available,
    };
  const edgeKinds = strings(raw.edge_kinds).filter((value): value is ClusterRunType =>
    CLUSTER_RUN_TYPES.has(value as ClusterRunType),
  );
  const memberCount = Math.max(
    1,
    integer(raw.member_count, integer(raw.image_count, members.length || 1)),
  );
  const rawType = text(raw.cluster_type, raw.type);
  const inferredType: ClusterType =
    memberCount === 1
      ? "single"
      : edgeKinds.includes("exact")
        ? "exact"
        : edgeKinds.includes("perceptual")
          ? "perceptual"
          : "semantic";
  const type = CLUSTER_TYPES.has(rawType as ClusterType)
    ? (rawType as ClusterType)
    : inferredType;
  return {
    id,
    type,
    memberCount,
    edgeKinds,
    representative: normalizeImage(representativeRaw),
    identityAnchors: records(raw.identity_anchors)
      .map(normalizeAnchor)
      .filter((value): value is IdentityAnchorView => value !== null),
  };
}

function resultRecord(payload: Record<string, unknown>): Record<string, unknown> {
  const job = record(payload.job);
  return record(job?.result) ?? record(payload.result) ?? payload;
}

export function normalizeClusterPage(
  raw: Record<string, unknown>,
  fallbackOffset = 0,
  fallbackLimit = 12,
): SimilarityClusterPage {
  const source = resultRecord(raw);
  const snapshot = record(source.snapshot);
  const clusterValues = records(source.clusters ?? snapshot?.clusters);
  const clusters = clusterValues
    .map(normalizeCluster)
    .filter((value): value is SimilarityCluster => value !== null);
  return {
    offset: Math.max(0, integer(source.offset, fallbackOffset)),
    limit: Math.max(1, integer(source.limit, fallbackLimit)),
    total: Math.max(clusters.length, integer(source.total, clusters.length)),
    undoAvailable: source.undo_available === true,
    clusters,
  };
}

export function normalizeClusterDetail(
  raw: Record<string, unknown>,
  fallbackOffset = 0,
  fallbackLimit = 20,
): SimilarityClusterDetail | null {
  const source = resultRecord(raw);
  const clusterRaw = record(source.cluster) ?? source;
  const cluster = normalizeCluster(clusterRaw);
  if (!cluster) return null;
  const members = records(source.members ?? source.items ?? clusterRaw.members).map(
    normalizeImage,
  );
  return {
    cluster,
    offset: Math.max(0, integer(source.offset, fallbackOffset)),
    limit: Math.max(1, integer(source.limit, fallbackLimit)),
    total: Math.max(
      members.length,
      integer(source.total, integer(source.member_count, cluster.memberCount)),
    ),
    members,
  };
}

export function normalizeClusterOperationResult(
  raw: Record<string, unknown>,
): ClusterOperationResult {
  const source = resultRecord(raw);
  return {
    batchId: text(source.batch_id, source.operation_id),
    applied: Math.max(0, integer(source.applied, integer(source.updated))),
    failed: Math.max(0, integer(source.failed)),
    conflicts: Math.max(
      0,
      integer(source.conflicts, integer(source.conflict)),
    ),
    undoAvailable: source.undo_available === true,
  };
}

function normalizeLearningSample(
  raw: Record<string, unknown>,
  index: number,
): LearningSample | null {
  const image = normalizeImage(raw);
  if (!image.docId) return null;
  const reasons = strings(raw.reasons).filter((value): value is LearningReason =>
    ACTIVE_LEARNING_REASONS.has(value as LearningReason),
  );
  return {
    ...image,
    rank: Math.max(1, integer(raw.rank, index + 1)),
    groupId: text(raw.group_id, raw.cluster_id, "ungrouped"),
    queryId: text(raw.query_id),
    candidateKind: normalizeCandidateKind(raw.candidate_kind),
    sourceSha256: normalizeSourceSha256(raw.source_sha256),
    uncertaintyScore: boundedScore(raw.uncertainty_score),
    reasons,
    suggestedTags: strings(raw.suggested_tags ?? raw.labels),
  };
}

export function normalizeLearningQueue(
  raw: Record<string, unknown>,
): ActiveLearningQueueView | null {
  const source = resultRecord(raw);
  const queue = record(source.queue) ?? source;
  const queueId = text(queue.queue_id, source.queue_id);
  if (!queueId) return null;
  const samples = records(queue.items ?? queue.samples)
    .map(normalizeLearningSample)
    .filter((value): value is LearningSample => value !== null);
  return {
    queueId,
    candidateCount: Math.max(
      samples.length,
      integer(queue.candidate_count, samples.length),
    ),
    undoAvailable:
      queue.undo_available === true || source.undo_available === true,
    samples,
  };
}

export function normalizeLearningReviewResult(
  raw: Record<string, unknown>,
): LearningReviewResult {
  const source = resultRecord(raw);
  const failures = records(source.failures).map(
    (item): LearningReviewFailure => ({
      docId: text(item.doc_id, item.proposal_id, item.image_id),
      code: text(item.code, item.error_code),
      message: text(
        item.message,
        item.error,
        item.reason,
        item.code,
        "处理失败，等待重试。",
      ),
      retryable: item.retryable !== false,
    }),
  );
  return {
    batchId: text(source.batch_id, source.operation_id),
    applied: Math.max(0, integer(source.applied, integer(source.updated))),
    failed: Math.max(0, integer(source.failed)),
    skipped: Math.max(0, integer(source.skipped)),
    conflicts: Math.max(
      0,
      integer(source.conflicts, integer(source.conflict)),
    ),
    undoAvailable: source.undo_available === true,
    failures,
  };
}

function normalizeJob(payload: OrganizeJobResponse): NormalizedJob {
  const raw = (payload.job ?? payload.jobs?.[0] ?? {}) as OrganizeJobWire;
  const progress = record(raw.progress);
  const status = text(raw.status, raw.state, payload.result ? "succeeded" : "queued").toLowerCase();
  const error = record(raw.error);
  return {
    id: text(raw.id, raw.job_id),
    status,
    message: text(raw.message, progress?.message, error?.message, raw.error),
    percent: Math.min(
      100,
      Math.max(
        0,
        integer(
          raw.progress_percent,
          integer(progress?.percent, integer(progress?.percentage)),
        ),
      ),
    ),
    result: record(raw.result) ?? record(payload.result) ?? {},
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "请求失败，请稍后重试。";
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = (): void => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function useTaskRunner(api: OrganizeApi, events: IntelligenceEvents) {
  const busy = ref(false);
  const cancelling = ref(false);
  const activeJobId = ref("");
  const statusMessage = ref("");
  const progressPercent = ref(0);
  const lastError = ref("");
  let operationController: AbortController | null = null;
  let cancelController: AbortController | null = null;

  const notify = (title: string, message: string, kind: ToastKind): void =>
    events.onToast?.(title, message, kind);

  async function run(
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown> | null> {
    if (busy.value) return null;
    operationController?.abort();
    const controller = new AbortController();
    operationController = controller;
    busy.value = true;
    lastError.value = "";
    statusMessage.value = "正在提交…";
    progressPercent.value = 0;
    const startedAt = Date.now();
    let pollFailures = 0;
    try {
      let payload = await api.submitJob(body, controller.signal);
      let job = normalizeJob(payload);
      if (
        !job.id &&
        !TERMINAL_STATUSES.has(job.status) &&
        !Object.keys(job.result).length
      ) {
        throw new Error("后端没有返回任务编号或可用结果。");
      }
      activeJobId.value = job.id;
      while (job.id && !TERMINAL_STATUSES.has(job.status)) {
        statusMessage.value = job.message || "后台处理中…";
        progressPercent.value = job.percent;
        if (Date.now() - startedAt > (events.timeoutMs ?? 30 * 60_000)) {
          throw new Error("等待任务超时，可稍后在任务中心查看运行结果。");
        }
        await wait(events.pollIntervalMs ?? 900, controller.signal);
        try {
          payload = await api.job(job.id, controller.signal);
          job = normalizeJob(payload);
          pollFailures = 0;
        } catch (error) {
          if (isAbort(error)) throw error;
          pollFailures += 1;
          if (pollFailures >= 3) throw error;
          statusMessage.value = `连接暂时中断，正在重试（${pollFailures}/3）…`;
        }
      }
      if (job.id && !SUCCESS_STATUSES.has(job.status)) {
        throw new Error(job.message || `任务状态：${job.status || "failed"}`);
      }
      statusMessage.value = job.message || "已完成";
      progressPercent.value = 100;
      return job.result && Object.keys(job.result).length
        ? job.result
        : resultRecord(payload as Record<string, unknown>);
    } catch (error) {
      if (isAbort(error)) {
        statusMessage.value = "已取消";
        return null;
      }
      lastError.value = errorMessage(error);
      statusMessage.value = "";
      notify("操作未完成", lastError.value, "error");
      return null;
    } finally {
      if (operationController === controller) {
        operationController = null;
        activeJobId.value = "";
        busy.value = false;
        cancelling.value = false;
      }
    }
  }

  async function cancel(): Promise<boolean> {
    if (!busy.value || cancelling.value) return false;
    cancelling.value = true;
    const jobId = activeJobId.value;
    if (!jobId || !api.cancelJob) {
      operationController?.abort();
      return true;
    }
    cancelController?.abort();
    const controller = new AbortController();
    cancelController = controller;
    try {
      await api.cancelJob(jobId, controller.signal);
      operationController?.abort();
      notify("任务已取消", "当前操作已停止，页面控件可以继续使用。", "info");
      return true;
    } catch (error) {
      if (!isAbort(error)) {
        notify("无法取消任务", errorMessage(error), "error");
      }
      return false;
    } finally {
      if (cancelController === controller) cancelController = null;
      cancelling.value = false;
    }
  }

  onBeforeUnmount(() => {
    operationController?.abort();
    cancelController?.abort();
  });

  return {
    busy,
    cancelling,
    activeJobId,
    statusMessage,
    progressPercent,
    lastError,
    run,
    cancel,
  };
}

export function useSimilarityGroups(
  libraryId: Ref<string>,
  suppliedApi?: OrganizeApi,
  events: IntelligenceEvents = {},
) {
  const api = suppliedApi ?? organizeApi;
  const runner = useTaskRunner(api, events);
  const scope = ref<ClusterScope>("new_or_changed");
  const clusterTypes = ref<ClusterRunType[]>([
    "exact",
    "perceptual",
    "semantic",
  ]);
  const clusterTypeFilter = ref<ClusterType>("all");
  const offset = ref(0);
  const limit = ref(12);
  const total = ref(0);
  const clusters = ref<SimilarityCluster[]>([]);
  const detail = ref<SimilarityClusterDetail | null>(null);
  const selectedClusterIds = ref<Set<string>>(new Set());
  const selectedMemberIds = ref<Set<string>>(new Set());
  const detailOffset = ref(0);
  const detailLimit = ref(20);
  const identityCategory = ref<ClusterIdentityCategory>("character");
  const identityValue = ref("");
  const lastOperationResult = ref<ClusterOperationResult | null>(null);
  const undoAvailable = ref(false);

  const page = computed(() => Math.floor(offset.value / limit.value) + 1);
  const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const selectedClusterCount = computed(() => selectedClusterIds.value.size);
  const selectedMemberCount = computed(() => selectedMemberIds.value.size);
  const detailPage = computed(
    () => Math.floor(detailOffset.value / detailLimit.value) + 1,
  );
  const detailTotalPages = computed(() =>
    Math.max(1, Math.ceil((detail.value?.total ?? 0) / detailLimit.value)),
  );

  function toggleClusterType(type: ClusterRunType): void {
    const values = new Set(clusterTypes.value);
    if (values.has(type)) values.delete(type);
    else values.add(type);
    clusterTypes.value = [...values].sort() as ClusterRunType[];
  }

  async function loadClusters(nextOffset = offset.value): Promise<boolean> {
    if (!libraryId.value || runner.busy.value) return false;
    const result = await runner.run({
      task_type: "cluster_list",
      library_id: libraryId.value,
      offset: Math.max(0, nextOffset),
      limit: limit.value,
      cluster_type: clusterTypeFilter.value,
    });
    if (!result) return false;
    const normalized = normalizeClusterPage(result, nextOffset, limit.value);
    clusters.value = normalized.clusters;
    offset.value = normalized.offset;
    total.value = normalized.total;
    if (normalized.undoAvailable) undoAvailable.value = true;
    return true;
  }

  async function runClustering(): Promise<boolean> {
    if (!libraryId.value || runner.busy.value) return false;
    if (!clusterTypes.value.length) {
      events.onToast?.("请选择分组方式", "至少保留一种相似度来源。", "error");
      return false;
    }
    const result = await runner.run({
      task_type: "cluster_images",
      library_id: libraryId.value,
      scope: scope.value,
      cluster_types: [...clusterTypes.value],
    });
    if (!result) return false;
    events.onToast?.(
      "相似分组已更新",
      "只读取现有向量与哈希，没有产生大模型费用。",
      "success",
    );
    offset.value = 0;
    selectedClusterIds.value = new Set();
    return loadClusters(0);
  }

  async function openCluster(
    clusterId: string,
    nextOffset = 0,
  ): Promise<boolean> {
    if (!libraryId.value || runner.busy.value) return false;
    const result = await runner.run({
      task_type: "cluster_detail",
      library_id: libraryId.value,
      cluster_id: clusterId,
      offset: Math.max(0, nextOffset),
      limit: detailLimit.value,
    });
    if (!result) return false;
    const normalized = normalizeClusterDetail(
      result,
      nextOffset,
      detailLimit.value,
    );
    if (!normalized) {
      events.onToast?.("无法读取分组", "后端没有返回有效的分组详情。", "error");
      return false;
    }
    const previousClusterId = detail.value?.cluster.id ?? "";
    detail.value = normalized;
    detailOffset.value = normalized.offset;
    detailLimit.value = normalized.limit;
    if (previousClusterId && previousClusterId !== normalized.cluster.id) {
      selectedMemberIds.value = new Set();
    }
    return true;
  }

  function closeDetail(): void {
    detail.value = null;
    selectedMemberIds.value = new Set();
    detailOffset.value = 0;
  }

  function toggleClusterSelection(clusterId: string, selected?: boolean): void {
    const next = new Set(selectedClusterIds.value);
    const shouldSelect = selected ?? !next.has(clusterId);
    if (shouldSelect) next.add(clusterId);
    else next.delete(clusterId);
    selectedClusterIds.value = next;
  }

  function clearClusterSelection(): void {
    selectedClusterIds.value = new Set();
  }

  function toggleMemberSelection(docId: string, selected?: boolean): void {
    const next = new Set(selectedMemberIds.value);
    const shouldSelect = selected ?? !next.has(docId);
    if (shouldSelect) next.add(docId);
    else next.delete(docId);
    selectedMemberIds.value = next;
  }

  function clearMemberSelection(): void {
    selectedMemberIds.value = new Set();
  }

  function operationSummary(result: ClusterOperationResult): string {
    return `已应用 ${result.applied} 项，失败 ${result.failed} 项，冲突 ${result.conflicts} 项。`;
  }

  async function runClusterOperation(
    body: Record<string, unknown>,
    successTitle: string,
  ): Promise<ClusterOperationResult | null> {
    const result = await runner.run(body);
    if (!result) return null;
    const normalized = normalizeClusterOperationResult(result);
    lastOperationResult.value = normalized;
    undoAvailable.value = normalized.undoAvailable;
    const hasUnresolved = normalized.failed > 0 || normalized.conflicts > 0;
    events.onToast?.(
      hasUnresolved ? `${successTitle}，但有项目未完成` : successTitle,
      hasUnresolved
        ? `${operationSummary(normalized)}所选项目已保留，可检查后重试。`
        : operationSummary(normalized),
      hasUnresolved ? (normalized.applied > 0 ? "info" : "error") : "success",
    );
    return normalized;
  }

  async function mergeSelectedClusters(): Promise<boolean> {
    if (!libraryId.value || runner.busy.value) return false;
    const clusterIds = [...selectedClusterIds.value].sort();
    if (clusterIds.length < 2) {
      events.onToast?.("无法合并分组", "请至少选择两个分组。", "error");
      return false;
    }
    const result = await runClusterOperation(
      {
        task_type: "cluster_merge",
        library_id: libraryId.value,
        cluster_ids: clusterIds,
      },
      "所选分组已合并",
    );
    if (!result) return false;
    if (!result.failed && !result.conflicts) clearClusterSelection();
    await loadClusters(offset.value);
    return true;
  }

  async function splitSelectedMembers(): Promise<boolean> {
    if (!libraryId.value || !detail.value || runner.busy.value) return false;
    const docIds = [...selectedMemberIds.value].sort();
    if (!docIds.length) {
      events.onToast?.("无法拆分图片", "请先选择一张或多张错误图片。", "error");
      return false;
    }
    const clusterId = detail.value.cluster.id;
    const result = await runClusterOperation(
      {
        task_type: "cluster_split",
        library_id: libraryId.value,
        cluster_id: clusterId,
        doc_ids: docIds,
      },
      "所选图片已拆出分组",
    );
    if (!result) return false;
    if (!result.failed && !result.conflicts) clearMemberSelection();
    await loadClusters(offset.value);
    if (clusters.value.some((cluster) => cluster.id === clusterId)) {
      await openCluster(clusterId, 0);
    } else {
      closeDetail();
    }
    return true;
  }

  async function applyIdentityToCluster(): Promise<boolean> {
    if (!libraryId.value || !detail.value || runner.busy.value) return false;
    if (!CLUSTER_IDENTITY_CATEGORIES.has(identityCategory.value)) return false;
    const value = identityValue.value.trim();
    if (!value) {
      events.onToast?.("身份标签不能为空", "请输入真人、Cosplayer、角色或作品名称。", "error");
      return false;
    }
    const clusterId = detail.value.cluster.id;
    const result = await runClusterOperation(
      {
        task_type: "cluster_apply_identity",
        library_id: libraryId.value,
        cluster_id: clusterId,
        identity_category: identityCategory.value,
        identity_value: value,
      },
      "整组身份标签已应用",
    );
    if (!result) return false;
    if (!result.failed && !result.conflicts) identityValue.value = "";
    await loadClusters(offset.value);
    if (clusters.value.some((cluster) => cluster.id === clusterId)) {
      await openCluster(clusterId, detailOffset.value);
    } else {
      closeDetail();
    }
    return true;
  }

  async function undoLastClusterOperation(): Promise<boolean> {
    if (!libraryId.value || runner.busy.value || !undoAvailable.value) return false;
    const openClusterId = detail.value?.cluster.id ?? "";
    const result = await runClusterOperation(
      {
        task_type: "cluster_undo",
        library_id: libraryId.value,
      },
      "最近一次整理已撤销",
    );
    if (!result) return false;
    await loadClusters(offset.value);
    if (
      openClusterId &&
      clusters.value.some((cluster) => cluster.id === openClusterId)
    ) {
      await openCluster(openClusterId, 0);
    } else if (openClusterId) {
      closeDetail();
    }
    return true;
  }

  async function previousPage(): Promise<void> {
    await loadClusters(Math.max(0, offset.value - limit.value));
  }

  async function nextPage(): Promise<void> {
    if (offset.value + limit.value >= total.value) return;
    await loadClusters(offset.value + limit.value);
  }

  async function previousDetailPage(): Promise<void> {
    if (!detail.value) return;
    await openCluster(
      detail.value.cluster.id,
      Math.max(0, detailOffset.value - detailLimit.value),
    );
  }

  async function nextDetailPage(): Promise<void> {
    if (
      !detail.value ||
      detailOffset.value + detailLimit.value >= detail.value.total
    ) return;
    await openCluster(
      detail.value.cluster.id,
      detailOffset.value + detailLimit.value,
    );
  }

  watch(libraryId, () => {
    clusters.value = [];
    detail.value = null;
    selectedClusterIds.value = new Set();
    selectedMemberIds.value = new Set();
    offset.value = 0;
    detailOffset.value = 0;
    total.value = 0;
    lastOperationResult.value = null;
    undoAvailable.value = false;
    if (libraryId.value) void loadClusters(0);
  });
  watch(clusterTypeFilter, () => {
    offset.value = 0;
    selectedClusterIds.value = new Set();
    if (libraryId.value) void loadClusters(0);
  });

  return {
    ...runner,
    scope,
    clusterTypes,
    clusterTypeFilter,
    clusters,
    detail,
    selectedClusterIds,
    selectedMemberIds,
    offset,
    limit,
    total,
    detailOffset,
    detailLimit,
    identityCategory,
    identityValue,
    lastOperationResult,
    undoAvailable,
    page,
    totalPages,
    selectedClusterCount,
    selectedMemberCount,
    detailPage,
    detailTotalPages,
    toggleClusterType,
    loadClusters,
    runClustering,
    openCluster,
    closeDetail,
    toggleClusterSelection,
    clearClusterSelection,
    toggleMemberSelection,
    clearMemberSelection,
    mergeSelectedClusters,
    splitSelectedMembers,
    applyIdentityToCluster,
    undoLastClusterOperation,
    previousPage,
    nextPage,
    previousDetailPage,
    nextDetailPage,
  };
}

export function useActiveLearning(
  libraryId: Ref<string>,
  suppliedApi?: OrganizeApi,
  events: IntelligenceEvents = {},
) {
  const api = suppliedApi ?? organizeApi;
  const runner = useTaskRunner(api, events);
  const reviewBudget = ref(25);
  const queue = ref<ActiveLearningQueueView | null>(null);
  const selectedIds = ref<Set<string>>(new Set());
  const decisions = ref<Record<string, LearningDecisionDraft>>({});
  const editTagInput = ref("");
  const lastReviewResult = ref<LearningReviewResult | null>(null);
  const reviewFailures = ref<Record<string, LearningReviewFailure>>({});
  const undoAvailable = ref(false);
  const undoMessage = ref("");
  let lastSubmittedDecisions: LearningDecisionDraft[] = [];

  const pendingDecisionCount = computed(() => Object.keys(decisions.value).length);
  const selectedCount = computed(() => selectedIds.value.size);

  async function loadQueue(): Promise<boolean> {
    if (!libraryId.value || runner.busy.value) return false;
    if (!Number.isInteger(reviewBudget.value) || reviewBudget.value < 20 || reviewBudget.value > 30) {
      events.onToast?.("审核预算无效", "请输入 20～30 之间的整数。", "error");
      return false;
    }
    const result = await runner.run({
      task_type: "active_learning_queue",
      library_id: libraryId.value,
      review_budget: reviewBudget.value,
    });
    if (!result) return false;
    const normalized = normalizeLearningQueue(result);
    if (!normalized) {
      events.onToast?.("无法读取待学习样本", "后端没有返回有效队列。", "error");
      return false;
    }
    queue.value = normalized;
    selectedIds.value = new Set();
    decisions.value = {};
    reviewFailures.value = {};
    if (normalized.undoAvailable) undoAvailable.value = true;
    return true;
  }

  function toggleSelection(docId: string, selected?: boolean): void {
    const next = new Set(selectedIds.value);
    const shouldSelect = selected ?? !next.has(docId);
    if (shouldSelect) next.add(docId);
    else next.delete(docId);
    selectedIds.value = next;
  }

  function selectAll(): void {
    selectedIds.value = new Set(queue.value?.samples.map((sample) => sample.docId) ?? []);
  }

  function clearSelection(): void {
    selectedIds.value = new Set();
  }

  function setDecision(
    docId: string,
    decision: LearningDecisionValue,
    labels: string[] = [],
  ): boolean {
    if (!queue.value?.samples.some((sample) => sample.docId === docId)) return false;
    if (decision === "edit" && !labels.length) {
      events.onToast?.("请输入修正标签", "编辑决定需要至少一个标签。", "error");
      return false;
    }
    const draft: LearningDecisionDraft = { doc_id: docId, decision };
    if (labels.length) draft.labels = labels;
    decisions.value = { ...decisions.value, [docId]: draft };
    return true;
  }

  function applyDecisionToSelection(decision: LearningDecisionValue): boolean {
    if (!selectedIds.value.size) {
      events.onToast?.("尚未选择样本", "请先选择一张或多张图片。", "error");
      return false;
    }
    const labels = decision === "edit" ? parseTagInput(editTagInput.value) : [];
    if (decision === "edit" && !labels.length) {
      events.onToast?.("请输入修正标签", "编辑决定需要至少一个标签。", "error");
      return false;
    }
    for (const docId of [...selectedIds.value].sort()) {
      setDecision(docId, decision, labels);
    }
    return true;
  }

  function decisionFor(docId: string): LearningDecisionDraft | null {
    return decisions.value[docId] ?? null;
  }

  function failureFor(docId: string): LearningReviewFailure | null {
    return reviewFailures.value[docId] ?? null;
  }

  function reviewSummary(result: LearningReviewResult): string {
    return `已应用 ${result.applied} 项，失败 ${result.failed} 项，跳过 ${result.skipped} 项，冲突 ${result.conflicts} 项。`;
  }

  async function submitReview(): Promise<boolean> {
    if (!libraryId.value || !queue.value || runner.busy.value) return false;
    const decisionValues = Object.values(decisions.value).sort((left, right) =>
      left.doc_id.localeCompare(right.doc_id, "zh-CN"),
    );
    if (!decisionValues.length) {
      events.onToast?.("尚无审核决定", "请先接受、拒绝、编辑或跳过样本。", "error");
      return false;
    }
    const result = await runner.run({
      task_type: "active_learning_review",
      library_id: libraryId.value,
      queue_id: queue.value.queueId,
      decisions: decisionValues,
    });
    if (!result) return false;
    const normalized = normalizeLearningReviewResult(result);
    lastReviewResult.value = normalized;
    undoAvailable.value = normalized.undoAvailable;
    undoMessage.value = "";
    lastSubmittedDecisions = decisionValues.map((decision) => ({
      ...decision,
      labels: decision.labels ? [...decision.labels] : undefined,
    }));

    const submittedById = new Map(
      decisionValues.map((decision) => [decision.doc_id, decision]),
    );
    const failuresById = new Map(
      normalized.failures
        .filter((failure) => failure.docId && submittedById.has(failure.docId))
        .map((failure) => [failure.docId, failure]),
    );
    const unresolvedCount = Math.max(
      normalized.failed + normalized.conflicts,
      failuresById.size,
    );
    const keepAll = unresolvedCount > 0 && failuresById.size < unresolvedCount;
    const remaining: Record<string, LearningDecisionDraft> = {};
    const retainedFailures: Record<string, LearningReviewFailure> = {};
    for (const decision of decisionValues) {
      const failure = failuresById.get(decision.doc_id);
      if (!failure && !keepAll) continue;
      remaining[decision.doc_id] = decision;
      retainedFailures[decision.doc_id] = failure ?? {
        docId: decision.doc_id,
        code: "incomplete_failure_details",
        message: "后端未返回完整失败明细，已保留此项以便安全重试。",
        retryable: true,
      };
    }
    decisions.value = remaining;
    reviewFailures.value = retainedFailures;
    selectedIds.value = new Set(Object.keys(remaining));

    const hasUnresolved = unresolvedCount > 0;
    events.onToast?.(
      hasUnresolved ? "审核部分完成" : "审核已应用",
      hasUnresolved
        ? `${reviewSummary(normalized)}失败或冲突项已保留，可直接重试。`
        : `${reviewSummary(normalized)}对应标签、反馈或分组关系已写入。`,
      hasUnresolved ? (normalized.applied > 0 ? "info" : "error") : "success",
    );
    return true;
  }

  async function undoLastReview(): Promise<boolean> {
    if (!libraryId.value || runner.busy.value || !undoAvailable.value) {
      return false;
    }
    const result = await runner.run({
      task_type: "active_learning_review_undo",
      library_id: libraryId.value,
    });
    if (!result) return false;
    const normalized = normalizeLearningReviewResult(result);
    undoAvailable.value = normalized.undoAvailable;
    undoMessage.value = normalized.batchId
      ? `批次 ${normalized.batchId} 已撤销。`
      : "最近一次主动学习审核已撤销。";
    if (
      lastSubmittedDecisions.length &&
      (!normalized.batchId || normalized.batchId === lastReviewResult.value?.batchId)
    ) {
      decisions.value = Object.fromEntries(
        lastSubmittedDecisions.map((decision) => [decision.doc_id, decision]),
      );
      selectedIds.value = new Set(lastSubmittedDecisions.map((item) => item.doc_id));
    }
    reviewFailures.value = {};
    events.onToast?.(
      "审核已撤销",
      `${undoMessage.value} 如需修改，可以调整已恢复的决定后重新提交。`,
      "success",
    );
    return true;
  }

  watch(libraryId, () => {
    queue.value = null;
    selectedIds.value = new Set();
    decisions.value = {};
    reviewFailures.value = {};
    lastReviewResult.value = null;
    undoAvailable.value = false;
    undoMessage.value = "";
    lastSubmittedDecisions = [];
    if (libraryId.value) void loadQueue();
  });

  return {
    ...runner,
    reviewBudget,
    queue,
    selectedIds,
    decisions,
    editTagInput,
    lastReviewResult,
    reviewFailures,
    undoAvailable,
    undoMessage,
    pendingDecisionCount,
    selectedCount,
    loadQueue,
    toggleSelection,
    selectAll,
    clearSelection,
    setDecision,
    applyDecisionToSelection,
    decisionFor,
    failureFor,
    submitReview,
    undoLastReview,
  };
}
