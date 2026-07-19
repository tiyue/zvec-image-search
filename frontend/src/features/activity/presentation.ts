import type { ActivityLevel } from "./types";

const TASK_LABELS: Record<string, string> = {
  index: "建立索引",
  sync: "同步图库",
  auto_tag: "智能标注",
  index_and_auto_tag: "索引并智能标注",
  manual_tag_batch: "批量标签",
  folder_delete: "文件夹清理",
  folder_delete_preview: "文件夹清理",
  folder_delete_commit: "文件夹清理",
  search_results_cleanup: "清理搜索结果",
  migrate_schema: "迁移图库",
  cache_clear: "清理缓存",
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
  return TASK_LABELS[value] ?? (value || "未知任务");
}

export function jobStatusLabel(value: string): string {
  return STATUS_LABELS[value] ?? (value || "未知状态");
}

export function activityLevelLabel(value: ActivityLevel): string {
  return LEVEL_LABELS[value];
}

export function isActiveStatus(value: string): boolean {
  return ACTIVE_STATUSES.has(value);
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
  const seconds = Math.max(0, Math.round((finishTime - startTime) / 1_000));
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
