import type { ActivityLevel } from "./types";

const TASK_LABELS: Record<string, string> = {
  index: "建立索引",
  sync: "同步图库",
  auto_tag: "智能标注",
  index_and_auto_tag: "索引并智能标注",
  auto_index_and_auto_tag: "自动增量索引与标注",
  manual_tag_batch: "批量标签",
  folder_name_tag_apply: "文件夹名称标签",
  folder_delete: "文件夹清理",
  folder_delete_preview: "文件夹清理",
  folder_delete_commit: "文件夹清理",
  folder_name_tag_estimate: "预览文件夹名标签",
  search_results_cleanup: "清理搜索结果",
  clean_results: "清理历史结果",
  migrate_schema: "迁移图库",
  data_migration: "迁移数据",
  cache_clear: "清理缓存",
  auto_tag_estimate: "估算标注费用",
  manual_tag_undo: "撤销批量标签",
  auto_tag_policy_migrate: "核对旧标注",
  active_learning_queue: "整理待学习样本",
  active_learning_review: "审核待学习样本",
  active_learning_review_undo: "撤销待学习审核",
  metadata_backfill: "补全图片描述",
  folder_tag_backfill: "按文件夹补标签",
  tag_alias_upsert: "保存标签别名",
  tag_alias_delete: "删除标签别名",
  tag_alias_list: "查看标签别名",
  stats: "统计图库",
  organize_review: "审核标注",
  organize_identity_confirm: "确认人物标签",
  organize_batch_accept: "批量接受标注",
  organize_undo: "撤销标注审核",
  organize_refresh: "刷新整理结果",
  cluster_images: "相似分组",
  cluster_merge: "合并相似组",
  cluster_split: "拆开相似组",
  cluster_apply_identity: "给相似组加标签",
  cluster_undo: "撤销相似分组",
};

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  pending: "等待中",
  running: "运行中",
  cancelling: "正在取消",
  succeeded: "已完成",
  completed: "已完成",
  partial: "部分完成",
  needs_attention: "需要处理",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

const LEVEL_LABELS: Record<ActivityLevel, string> = {
  debug: "调试",
  info: "信息",
  warning: "警告",
  error: "错误",
};

const ACTIVE_STATUSES = new Set(["queued", "pending", "running", "cancelling"]);

export function taskTypeLabel(value: string): string {
  const known = TASK_LABELS[value];
  if (known) return known;
  if (!value) return "未知任务";
  if (/[A-Za-z]_[A-Za-z]/u.test(value)) return "后台整理";
  return value;
}

export function jobStatusLabel(value: string): string {
  return STATUS_LABELS[value] ?? (value || "未知状态");
}

export function activityLevelLabel(value: ActivityLevel): string {
  return LEVEL_LABELS[value];
}

const CATEGORY_LABELS: Record<string, string> = {
  search: "搜索",
  library_task: "图库任务",
  auto_tag: "智能标注",
  auto_index: "自动索引",
  manual_tag: "手工标签",
  folder_cleanup: "文件夹清理",
  image_failure: "出错的图片",
  settings: "设置",
  backend: "后端服务",
  frontend: "前端诊断",
  general: "一般操作",
};

const SOURCE_LABELS: Record<string, string> = {
  backend: "本机",
  frontend: "界面",
  gateway: "本机",
};

const ACTION_LABELS: Record<string, string> = {
  image_failed: "有一张图片没有处理成功",
  image_failure_log_truncated: "出错图片太多，只保留了一部分",
  library_added: "添加了图库",
  library_updated: "修改了图库",
  library_enabled_changed: "开关了图库",
  default_library_changed: "更换了默认图库",
  credentials_saved: "保存了模型密钥",
  credentials_deleted: "清除了模型密钥",
  credentials_save_failed: "模型密钥没有保存",
  backend_started: "本机服务已启动",
  backend_stopped: "本机服务已停止",
  backend_starting: "正在启动本机服务",
  backend_start_failed: "本机服务没有启动",
  backend_stop_requested: "正在退出",
  backend_stop_deferred: "还有任务在跑，暂时不能退出",
  backend_stop_failed: "退出没有完成",
  backend_needs_setup: "还需要完成初始设置",
  backend_job_exception: "任务中途出错",
  backend_workers_started: "后台处理已开始",
  backend_workers_stopping: "正在停止后台处理",
  backend_workers_stopped: "后台处理已停止",
  configuration_unavailable: "配置暂时读不出来",
  model_roles_updated: "更换了模型",
  model_catalog_replaced: "更换了模型目录",
  folder_delete_previewed: "预览了文件夹清理",
  folder_delete_recovery_needs_attention: "文件夹清理需要再看一眼",
  folder_name_tag_settings_updated: "修改了文件夹名标签规则",
  migration_precheck_restart_failed: "迁移前的重启没有完成",
};

const DETAIL_LABELS: Record<string, string> = {
  inserted: "新增",
  inserted_count: "新增",
  updated: "更新",
  updated_count: "更新",
  deleted: "删除",
  deleted_count: "删除",
  failed: "失败",
  failed_count: "失败",
  relative_path: "图片",
  path: "图片",
  task_type: "任务",
  status: "结果",
  processed: "已处理",
  processed_count: "已处理",
  pending_remaining: "还没处理",
  batches: "批次",
  reason: "原因",
  error_message: "原因",
  message: "说明",
  changes: "文件变化",
  index: "索引",
  auto_tag: "智能标注",
  mode: "方式",
  folders_scanned: "检查的文件夹",
  removed_blacklist: "去掉的排除标签",
  removed_legacy: "去掉的旧标签",
  removed_duplicates: "去掉的重复标签",
  result_count: "结果数量",
};

export function activityCategoryLabel(value: string): string {
  return CATEGORY_LABELS[value] ?? "一般操作";
}

export function activitySourceLabel(value: string): string {
  return SOURCE_LABELS[value] ?? "本机";
}

export function activityActionLabel(event: string): { label: string; known: boolean } {
  if (/^auto_index_cycle_(succeeded|partial|failed|cancelled)$/u.test(event)) {
    return { label: "自动处理了图库变化", known: true };
  }
  if (event.startsWith("folder_name_tag_")) {
    return { label: "按文件夹名整理了标签", known: true };
  }
  const label = ACTION_LABELS[event];
  if (label) return { label, known: true };
  return { label: "完成了一项操作", known: false };
}

function detailLabel(key: string): string {
  return DETAIL_LABELS[key] ?? "其他";
}

function detailText(key: string, value: unknown): string {
  if (key === "task_type" && typeof value === "string") return taskTypeLabel(value);
  if (key === "status" && typeof value === "string") return jobStatusLabel(value);
  return detailValue(value);
}

export function activityDetailRows(
  details: Record<string, unknown>,
): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  const visit = (key: string, value: unknown): void => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [childKey, child] of Object.entries(value)) visit(childKey, child);
      return;
    }
    if (value === null || value === undefined || value === "") return;
    rows.push([detailLabel(key), detailText(key, value)]);
  };
  for (const [key, value] of Object.entries(details)) visit(key, value);
  return rows;
}

export function activitySummary(item: {
  event: string;
  message: string;
  details: Record<string, unknown>;
}): string {
  const message = item.message.trim();
  const counts = activityDetailRows(item.details)
    .filter(([label]) => ["新增", "更新", "删除", "失败"].includes(label))
    .map(([label, value]) => `${label} ${value} 张`)
    .join("，");
  if (/[\u4e00-\u9fff]/u.test(message)) {
    if (counts && !/新增|更新|删除|失败/u.test(message)) return `${message}，${counts}`;
    return message;
  }
  const action = activityActionLabel(item.event).label;
  if (counts) return `${action}，${counts}`;
  return action;
}

export function activityCopyLine(item: {
  timestamp: string;
  level: ActivityLevel;
  event: string;
  message: string;
  details: Record<string, unknown>;
}): string {
  const action = activityActionLabel(item.event).label;
  return `${formatActivityTime(item.timestamp)} ${activityLevelLabel(item.level)} ${action} ${activitySummary(item)}`.trim();
}

export function isActiveStatus(value: string): boolean {
  return ACTIVE_STATUSES.has(value);
}

function summaryCount(summary: Record<string, unknown>, names: string[]): number {
  for (const name of names) {
    const value = summary[name];
    if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, Math.round(value));
  }
  for (const value of Object.values(summary)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = summaryCount(value as Record<string, unknown>, names);
      if (nested) return nested;
    }
  }
  return 0;
}

export interface JobListPiece {
  text: string;
  alert: boolean;
}

export interface JobListParts {
  status: string;
  title: string;
  library: string;
  detail: string;
  pieces: JobListPiece[];
  showStatus: boolean;
}

export interface JobListSource {
  taskType: string;
  status: string;
  libraryName: string;
  processed: number;
  total: number;
  failed: number;
  resultSummary: Record<string, unknown>;
}

function countPiece(label: string, value: number, alert = false): JobListPiece | null {
  if (!value) return null;
  return { text: `${label} ${value} 张`, alert };
}

export function jobListParts(job: JobListSource): JobListParts {
  const title = taskTypeLabel(job.taskType);
  const status = jobStatusLabel(job.status);
  const library = job.libraryName && job.libraryName !== "全部图库" ? job.libraryName : "";
  const showStatus = job.status !== "succeeded" && job.status !== "completed";
  if (isActiveStatus(job.status)) {
    const progress = job.total
      ? `已处理 ${job.processed} / ${job.total} 张`
      : job.processed
        ? `已处理 ${job.processed} 张`
        : job.status === "queued" || job.status === "pending"
          ? "等待开始"
          : "正在处理";
    const pieces = [countPiece("失败", job.failed, true)].filter((piece): piece is JobListPiece => piece !== null);
    pieces.unshift({ text: progress, alert: false });
    return { status, title, library, detail: pieces.map((piece) => piece.text).join(" · "), pieces, showStatus };
  }
  const pieces = [
    countPiece("新增", summaryCount(job.resultSummary, ["inserted", "inserted_count"])),
    countPiece("更新", summaryCount(job.resultSummary, ["updated", "updated_count"])),
    countPiece("删除", summaryCount(job.resultSummary, ["deleted", "deleted_count"])),
    countPiece(
      "失败",
      summaryCount(job.resultSummary, ["failed", "failed_count", "failure_count"]) || job.failed,
      true,
    ),
  ].filter((piece): piece is JobListPiece => piece !== null);
  if (!pieces.length) pieces.push({ text: "没有变化", alert: false });
  return { status, title, library, detail: pieces.map((piece) => piece.text).join(" · "), pieces, showStatus };
}

export function jobListHeadline(job: JobListSource): string {
  const parts = jobListParts(job);
  const bits = [parts.title];
  if (parts.library) bits.push(parts.library);
  bits.push(parts.detail);
  if (parts.showStatus && !parts.detail.includes(parts.status)) bits.unshift(parts.status);
  return bits.join(" · ");
}

export function formatActivityTime(value: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}

export function formatDuration(start: string, finish: string): string {
  if (!start) return "—";
  const startTime = new Date(start).getTime();
  const finishTime = finish ? new Date(finish).getTime() : Date.now();
  if (!Number.isFinite(startTime) || !Number.isFinite(finishTime) || finishTime < startTime) {
    return "—";
  }
  const elapsedMilliseconds = finishTime - startTime;
  if (elapsedMilliseconds < 1_000) return "< 1 秒";
  const seconds = Math.round(elapsedMilliseconds / 1_000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

export function detailValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
