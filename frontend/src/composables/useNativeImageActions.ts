import { computed, ref } from "vue";

import type {
  NativeImageSelection,
  NativeExportJob,
  NativeExportResult,
  NativeResult,
  PywebviewApi,
} from "../types/contracts";

interface NativeEvents {
  onError?: (title: string, message: string) => void;
  onInfo?: (title: string, message: string) => void;
}

const MAX_QUERY_IMAGE_BYTES = 128 * 1024 * 1024;
const EXPORT_POLL_INTERVAL_MS = 300;
const FINISHED_EXPORT_STATES = new Set(["succeeded", "partial", "failed"]);

interface ImageSelectionPayload {
  ok: boolean;
  id?: string;
  image_id?: string;
  name?: string;
  message?: string;
  image?: {
    id?: string;
    image_id?: string;
    name?: string;
    thumbnail_url?: string;
  } | null;
}

function errorMessage(result: NativeResult | null | undefined, fallback: string): string {
  return result?.error || result?.message || fallback;
}

function fileName(value: string): string {
  const parts = value.split(/[\\/]/u).filter(Boolean);
  return parts.at(-1) || "已选择查询图片";
}

function selectedImage(result: ImageSelectionPayload): { id: string; name: string } | null {
  if (Object.hasOwn(result, "image") && result.image === null) return null;
  const selected = result.image ?? result;
  const id = selected.id || selected.image_id || result.id || result.image_id || "";
  if (!id) throw new Error("桌面端没有返回可用的图片编号。");
  return { id, name: fileName(selected.name || result.name || "已选择查询图片") };
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export function useNativeImageActions(
  events: NativeEvents = {},
  bridge: () => PywebviewApi | undefined = () => window.pywebview?.api,
) {
  const activeKeys = new Set<string>();
  const busy = ref(false);
  const exportJob = ref<NativeExportJob | null>(null);
  const exporting = computed(
    () => exportJob.value !== null && !FINISHED_EXPORT_STATES.has(exportJob.value.status),
  );

  async function run(
    method: "open_image" | "reveal_image" | "copy_image",
    imageId: string,
    label: string,
  ): Promise<boolean> {
    if (!imageId) {
      events.onError?.("尚未选择图片", "请先在画廊中选择一张图片。");
      return false;
    }
    const api = bridge();
    if (!api || typeof api[method] !== "function") {
      events.onError?.(`无法${label}`, "桌面桥接尚未就绪，请稍后重试。");
      return false;
    }
    const key = `${method}:${imageId}`;
    if (activeKeys.has(key)) {
      events.onInfo?.("操作正在进行", `正在${label}，请勿重复操作。`);
      return false;
    }
    activeKeys.add(key);
    busy.value = true;
    try {
      const result = await api[method](imageId);
      if (!result || result.ok !== true) {
        throw new Error(errorMessage(result, `${label}失败。`));
      }
      if (result.message) events.onInfo?.(label, result.message);
      return true;
    } catch (error) {
      events.onError?.(`无法${label}`, error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      activeKeys.delete(key);
      busy.value = activeKeys.size > 0;
    }
  }

  async function selectQueryImage(): Promise<{ id: string; name: string } | null> {
    const api = bridge();
    if (!api || typeof api.select_query_image !== "function") {
      events.onError?.("无法选择图片", "桌面桥接尚未就绪，请稍后重试。");
      return null;
    }
    const key = "select_query_image";
    if (activeKeys.has(key)) return null;
    activeKeys.add(key);
    busy.value = true;
    try {
      const result: NativeImageSelection = await api.select_query_image();
      if (!result || result.ok !== true) {
        throw new Error(errorMessage(result, "无法选择查询图片。"));
      }
      return selectedImage(result);
    } catch (error) {
      events.onError?.("无法选择图片", error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      activeKeys.delete(key);
      busy.value = activeKeys.size > 0;
    }
  }

  async function registerQueryImage(file: File): Promise<{ id: string; name: string } | null> {
    if (!(file instanceof File) || file.size <= 0) {
      events.onError?.("无法添加图片", "拖放或粘贴的内容不是有效图片文件。");
      return null;
    }
    if (file.size > MAX_QUERY_IMAGE_BYTES) {
      events.onError?.("图片过大", "查询图片不能超过 128 MiB。");
      return null;
    }
    const key = "register_query_image";
    if (activeKeys.has(key)) {
      events.onInfo?.("正在读取图片", "请等待当前图片完成验证。");
      return null;
    }
    activeKeys.add(key);
    busy.value = true;
    try {
      const response = await fetch(`api/query-image?name=${encodeURIComponent(file.name || "粘贴图片")}`, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
        cache: "no-store",
        credentials: "same-origin",
      });
      const payload = (await response.json()) as ImageSelectionPayload & {
        error?: { message?: string } | string;
      };
      if (!response.ok || payload.ok !== true) {
        const nestedMessage =
          typeof payload.error === "object" ? payload.error?.message : payload.error;
        throw new Error(nestedMessage || payload.message || `图片读取失败（HTTP ${response.status}）。`);
      }
      return selectedImage(payload);
    } catch (error) {
      events.onError?.("无法添加图片", error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      activeKeys.delete(key);
      busy.value = activeKeys.size > 0;
    }
  }

  async function runBatch(
    method: "copy_files" | "copy_image_paths",
    imageIds: string[],
    label: string,
  ): Promise<boolean> {
    const ids = [...new Set(imageIds.filter(Boolean))];
    if (ids.length === 0) {
      events.onError?.("尚未选择图片", "请先选择至少一张图片。");
      return false;
    }
    const api = bridge();
    if (!api || typeof api[method] !== "function") {
      events.onError?.(`无法${label}`, "桌面桥接尚未就绪，请稍后重试。");
      return false;
    }
    const key = `${method}:${ids.join(",")}`;
    if (activeKeys.has(key)) return false;
    activeKeys.add(key);
    busy.value = true;
    try {
      const result = await api[method](ids);
      if (!result || result.ok !== true) {
        throw new Error(errorMessage(result, `${label}失败。`));
      }
      events.onInfo?.(`${label}完成`, `已处理 ${result.count ?? ids.length} 张图片。`);
      return true;
    } catch (error) {
      events.onError?.(`无法${label}`, error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      activeKeys.delete(key);
      busy.value = activeKeys.size > 0;
    }
  }

  async function exportImages(imageIds: string[]): Promise<boolean> {
    const ids = [...new Set(imageIds.filter(Boolean))];
    if (ids.length === 0) {
      events.onError?.("尚未选择图片", "请先选择至少一张图片。");
      return false;
    }
    const api = bridge();
    if (!api || typeof api.export_images !== "function") {
      events.onError?.("无法导出", "桌面桥接尚未就绪，请稍后重试。");
      return false;
    }
    if (exporting.value) {
      events.onInfo?.("导出正在进行", exportJob.value?.message || "请等待当前导出完成。");
      return false;
    }
    const key = "export_images";
    activeKeys.add(key);
    busy.value = true;
    try {
      const started: NativeExportResult = await api.export_images(ids);
      if (!started || started.ok !== true) {
        throw new Error(errorMessage(started, "无法开始导出。"));
      }
      if (started.cancelled) return false;
      if (!started.job?.id) throw new Error("桌面端没有返回导出任务编号。");
      exportJob.value = started.job;
      while (!FINISHED_EXPORT_STATES.has(exportJob.value.status)) {
        await delay(EXPORT_POLL_INTERVAL_MS);
        const status = await api.export_status(exportJob.value.id);
        if (!status || status.ok !== true || !status.job) {
          throw new Error(errorMessage(status, "无法读取导出进度。"));
        }
        exportJob.value = status.job;
      }
      const completed = exportJob.value;
      if (completed.status === "failed") throw new Error(completed.message || "导出失败。");
      events.onInfo?.(
        completed.status === "partial" ? "导出完成（有跳过）" : "导出完成",
        completed.message,
      );
      return true;
    } catch (error) {
      events.onError?.("无法导出", error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      activeKeys.delete(key);
      busy.value = activeKeys.size > 0;
    }
  }

  return {
    busy,
    exportJob,
    exporting,
    open: (imageId: string) => run("open_image", imageId, "系统打开图片"),
    reveal: (imageId: string) => run("reveal_image", imageId, "打开所在文件夹"),
    copyImage: (imageId: string) => run("copy_image", imageId, "复制图片"),
    copyFiles: (imageIds: string[]) => runBatch("copy_files", imageIds, "复制文件"),
    copyPaths: (imageIds: string[]) => runBatch("copy_image_paths", imageIds, "复制路径"),
    exportImages,
    selectQueryImage,
    registerQueryImage,
  };
}
