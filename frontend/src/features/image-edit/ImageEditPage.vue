<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { GatewayError } from "../../api/gateway";
import { imageEditApi } from "./api";
import {
  DEFAULT_IMAGE_EDIT_MODEL,
  IMAGE_EDIT_ASPECT_RATIOS,
  IMAGE_EDIT_MODEL_GROUPS,
  IMAGE_EDIT_SIZE_PRESETS,
  imageEditSizeRule,
  isQwenImageThree,
  supportsImageEditSize,
  supportsPromptExtension,
  validateImageEditDimensions,
} from "./modelCatalog";
import type { ImageEditAspectRatio } from "./modelCatalog";
import type {
  ImageEditIncomingSource,
  ImageEditPreviewStatus,
  ImageEditRequestPayload,
  ImageEditTaskWire,
} from "./types";

interface LocalPreviewTask {
  id: string;
  name: string;
  sourceUrl: string;
  ownsSourceUrl: boolean;
  file: File | null;
  sourceImageId: string;
  backendTaskId: string;
  sizeBytes: number;
  width: number | null;
  height: number | null;
  prompt: string;
  modelId: string;
  negativePrompt: string;
  aspectRatio: ImageEditAspectRatio;
  outputSize: string;
  widthInput: number;
  heightInput: number;
  seed: string | number;
  promptExtend: boolean;
  status: ImageEditPreviewStatus;
  error: string;
  resultUrl: string;
  outputFilename: string;
}

const props = withDefaults(defineProps<{
  visible?: boolean;
  incomingSource?: ImageEditIncomingSource | null;
}>(), {
  visible: true,
  incomingSource: null,
});

const emit = defineEmits<{
  toast: [title: string, message: string, kind: "success" | "error" | "info"];
  selectDirectory: [callback: (path: string) => void];
}>();

const SUPPORTED_IMAGE = /\.(?:jpe?g|png|webp|bmp|gif|tiff?)$/iu;
const SUPPORTED_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/bmp",
  "image/gif",
  "image/tiff",
]);
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_IMPORT_FILES = 50;
const MAX_SESSION_TASKS = 200;
const ACTIVE_STATUSES = new Set<ImageEditPreviewStatus>([
  "queued",
  "uploading",
  "generating",
  "downloading",
  "cancelling",
]);

const tasks = ref<LocalPreviewTask[]>([]);
const activeTaskId = ref("");
const canvasView = ref<"source" | "result">("source");
const fileInput = ref<HTMLInputElement | null>(null);
const dragActive = ref(false);
const outputDirectory = ref("");
const settingsLoading = ref(true);
const lastModelId = ref(DEFAULT_IMAGE_EDIT_MODEL);
const processedIncomingKey = ref("");
const submitControllers = new Map<string, AbortController>();
let taskSequence = 0;
let dragDepth = 0;
let pollTimer: number | null = null;
let polling = false;
let destroyed = false;
let pollErrorShown = false;

const activeTask = computed(
  () => tasks.value.find((task) => task.id === activeTaskId.value) ?? null,
);
const canvasImageUrl = computed(() => {
  const task = activeTask.value;
  if (!task) return "";
  return canvasView.value === "result" && task.resultUrl ? task.resultUrl : task.sourceUrl;
});
const activeTaskLocked = computed(
  () => Boolean(activeTask.value && ACTIVE_STATUSES.has(activeTask.value.status)),
);
const activeSupportsSize = computed(
  () => Boolean(activeTask.value && supportsImageEditSize(activeTask.value.modelId)),
);
const activeSupportsPromptExtension = computed(
  () => Boolean(activeTask.value && supportsPromptExtension(activeTask.value.modelId)),
);
const activeIsQwenThree = computed(
  () => Boolean(activeTask.value && isQwenImageThree(activeTask.value.modelId)),
);
const activeSizeRule = computed(() => (
  activeTask.value ? imageEditSizeRule(activeTask.value.modelId) : "automatic-only"
));
const activeSizeOptions = computed(() => {
  const task = activeTask.value;
  if (!task || !activeSupportsSize.value || task.aspectRatio === "auto") {
    return [{ value: "auto", label: "由模型根据原图自动决定" }];
  }
  if (task.aspectRatio === "custom") {
    return [{ value: "custom", label: "自定义宽高" }];
  }
  return IMAGE_EDIT_SIZE_PRESETS[task.aspectRatio].map((size) => ({
    value: size,
    label: size.replace("*", " × ") + " px",
  }));
});
const activeCount = computed(
  () => tasks.value.filter((task) => ACTIVE_STATUSES.has(task.status)).length,
);
const waitingCount = computed(
  () => tasks.value.filter((task) => task.status === "queued").length,
);
const canAbandonActive = computed(() => tasks.value
  .filter((task) => ACTIVE_STATUSES.has(task.status))
  .every((task) => Boolean(task.backendTaskId)));

function statusLabel(status: ImageEditPreviewStatus): string {
  const labels: Record<ImageEditPreviewStatus, string> = {
    draft: "待编辑",
    queued: "排队中",
    uploading: "正在上传",
    generating: "正在生成",
    downloading: "正在下载",
    cancelling: "正在取消",
    succeeded: "已保存",
    failed: "需要处理",
    cancelled: "已取消",
  };
  return labels[status];
}

function statusDescription(status: ImageEditPreviewStatus): string {
  return {
    draft: "等待手动提交。",
    queued: "任务已进入本地并发队列。",
    uploading: "正在上传原图。",
    generating: "阿里云模型正在编辑图片。",
    downloading: "正在下载并写入所选输出目录。",
    cancelling: "正在停止当前任务。",
    succeeded: "生成结果已保存。",
    failed: "任务失败，原图和编辑指令已保留。",
    cancelled: "任务已取消。",
  }[status];
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "大小未知";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function dimensionsLabel(task: LocalPreviewTask): string {
  return task.width && task.height ? `${task.width} × ${task.height}` : "尺寸读取中";
}

function nextTaskId(): string {
  taskSequence += 1;
  return `image-edit-preview-${Date.now()}-${taskSequence}`;
}

function createTask(
  name: string,
  sourceUrl: string,
  sizeBytes: number,
  ownsSourceUrl: boolean,
  file: File | null = null,
  sourceImageId = "",
): LocalPreviewTask {
  return {
    id: nextTaskId(),
    name,
    sourceUrl,
    ownsSourceUrl,
    file,
    sourceImageId,
    backendTaskId: "",
    sizeBytes,
    width: null,
    height: null,
    prompt: "",
    modelId: lastModelId.value,
    negativePrompt: "",
    aspectRatio: "auto",
    outputSize: "auto",
    widthInput: 1024,
    heightInput: 1024,
    seed: "",
    promptExtend: true,
    status: "draft",
    error: "",
    resultUrl: "",
    outputFilename: "",
  };
}

function readDimensions(task: LocalPreviewTask): void {
  const image = new Image();
  image.onload = () => {
    task.width = image.naturalWidth;
    task.height = image.naturalHeight;
  };
  image.onerror = () => {
    task.width = 0;
    task.height = 0;
  };
  image.src = task.sourceUrl;
}

function appendTasks(newTasks: LocalPreviewTask[]): void {
  if (newTasks.length === 0) return;
  tasks.value = [...tasks.value, ...newTasks];
  for (const task of tasks.value.slice(-newTasks.length)) readDimensions(task);
  activeTaskId.value = newTasks[0]?.id ?? activeTaskId.value;
  canvasView.value = "source";
}

function importFiles(files: File[]): void {
  if (files.length === 0) return;
  if (files.length > MAX_IMPORT_FILES) {
    emit("toast", "没有导入图片", `单次最多导入 ${MAX_IMPORT_FILES} 张，本次选择了 ${files.length} 张。`, "error");
    return;
  }
  const remaining = MAX_SESSION_TASKS - tasks.value.length;
  if (remaining <= 0 || files.length > remaining) {
    emit("toast", "没有导入图片", `当前会话最多保留 ${MAX_SESSION_TASKS} 个任务。`, "error");
    return;
  }

  const valid: File[] = [];
  const rejected: string[] = [];
  for (const file of files) {
    if (!(SUPPORTED_MIME_TYPES.has(file.type.toLowerCase()) || SUPPORTED_IMAGE.test(file.name))) {
      rejected.push(`${file.name}：格式不支持`);
    } else if (file.size <= 0) {
      rejected.push(`${file.name}：文件为空`);
    } else if (file.size > MAX_IMAGE_BYTES) {
      rejected.push(`${file.name}：超过 10 MB`);
    } else {
      valid.push(file);
    }
  }

  appendTasks(valid.map((file) => createTask(
    file.name,
    URL.createObjectURL(file),
    file.size,
    true,
    file,
  )));

  if (valid.length > 0) {
    emit("toast", "图片已导入", `已建立 ${valid.length} 个本地草稿，尚未上传。`, "success");
  }
  if (rejected.length > 0) {
    const preview = rejected.slice(0, 3).join("；");
    const suffix = rejected.length > 3 ? `；另有 ${rejected.length - 3} 项` : "";
    emit("toast", "部分图片未导入", `${preview}${suffix}`, "error");
  }
}

function importIncomingSource(source: ImageEditIncomingSource): void {
  if (tasks.value.length >= MAX_SESSION_TASKS) {
    emit("toast", "无法建立编辑草稿", `当前会话最多保留 ${MAX_SESSION_TASKS} 个任务。`, "error");
    return;
  }
  const task = createTask(
    source.name,
    source.url,
    source.sizeBytes ?? 0,
    false,
    null,
    source.imageId,
  );
  appendTasks([task]);
  emit("toast", "已加入图片编辑", `${source.name} 已建立为本地草稿。`, "success");
}

function openFilePicker(): void {
  fileInput.value?.click();
}

function handleFileSelection(event: Event): void {
  const input = event.target as HTMLInputElement;
  importFiles(Array.from(input.files ?? []));
  input.value = "";
}

function transferredFiles(data: DataTransfer | null): File[] {
  if (!data) return [];
  const files = Array.from(data.files ?? []);
  if (files.length > 0) return files;
  return Array.from(data.items ?? [])
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
}

function handleDragEnter(event: DragEvent): void {
  if (transferredFiles(event.dataTransfer).length === 0) return;
  event.preventDefault();
  dragDepth += 1;
  dragActive.value = true;
}

function handleDragOver(event: DragEvent): void {
  if (transferredFiles(event.dataTransfer).length === 0) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  dragActive.value = true;
}

function handleDragLeave(): void {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dragActive.value = false;
}

function handleDrop(event: DragEvent): void {
  event.preventDefault();
  dragDepth = 0;
  dragActive.value = false;
  importFiles(transferredFiles(event.dataTransfer));
}

function handlePaste(event: ClipboardEvent): void {
  const files = transferredFiles(event.clipboardData);
  if (files.length === 0) return;
  event.preventDefault();
  importFiles(files);
}

function selectTask(taskId: string): void {
  activeTaskId.value = taskId;
  canvasView.value = tasks.value.find((task) => task.id === taskId)?.resultUrl
    ? "result"
    : "source";
}

function clearTaskTimers(taskId: string): void {
  submitControllers.get(taskId)?.abort();
  submitControllers.delete(taskId);
}

function removeTask(task: LocalPreviewTask): void {
  if (ACTIVE_STATUSES.has(task.status)) return;
  clearTaskTimers(task.id);
  if (task.ownsSourceUrl) URL.revokeObjectURL(task.sourceUrl);
  const index = tasks.value.findIndex((candidate) => candidate.id === task.id);
  tasks.value = tasks.value.filter((candidate) => candidate.id !== task.id);
  if (activeTaskId.value === task.id) {
    activeTaskId.value = tasks.value[Math.min(index, tasks.value.length - 1)]?.id ?? "";
    canvasView.value = activeTask.value?.resultUrl ? "result" : "source";
  }
}

function updateModel(task: LocalPreviewTask): void {
  lastModelId.value = task.modelId;
  task.error = "";
  if (!supportsImageEditSize(task.modelId)) {
    task.aspectRatio = "auto";
    task.outputSize = "auto";
  } else if (task.aspectRatio !== "auto" && task.aspectRatio !== "custom") {
    task.outputSize = IMAGE_EDIT_SIZE_PRESETS[task.aspectRatio][0];
  }
}

function updateAspectRatio(task: LocalPreviewTask): void {
  if (task.aspectRatio === "auto") task.outputSize = "auto";
  else if (task.aspectRatio === "custom") task.outputSize = "custom";
  else task.outputSize = IMAGE_EDIT_SIZE_PRESETS[task.aspectRatio][0];
  task.error = "";
}

function requestPayload(task: LocalPreviewTask): ImageEditRequestPayload | null {
  if (!task.prompt.trim()) {
    task.error = "请先填写编辑指令。";
    return null;
  }
  if (!outputDirectory.value) {
    task.error = "请先选择生成结果的输出目录。";
    return null;
  }
  if (!isQwenImageThree(task.modelId) && task.negativePrompt.length > 500) {
    task.error = "当前模型的反向提示词最多为 500 个字符。";
    return null;
  }

  let size: string | undefined;
  if (supportsImageEditSize(task.modelId) && task.aspectRatio !== "auto") {
    if (task.aspectRatio === "custom") {
      const width = Number(task.widthInput);
      const height = Number(task.heightInput);
      const dimensionError = validateImageEditDimensions(task.modelId, width, height);
      if (dimensionError) {
        task.error = dimensionError;
        return null;
      }
      size = `${width}*${height}`;
    } else {
      size = task.outputSize;
    }
  }

  let seed: number | undefined;
  const seedInput = String(task.seed).trim();
  if (seedInput) {
    seed = Number(seedInput);
    if (!Number.isInteger(seed) || seed < 0 || seed > 2_147_483_647) {
      task.error = "随机种子必须是 0 到 2147483647 之间的整数。";
      return null;
    }
  }
  return {
    model: task.modelId,
    prompt: task.prompt.trim(),
    negative_prompt: task.negativePrompt.trim(),
    ...(size ? { size } : {}),
    ...(seed === undefined ? {} : { seed }),
    prompt_extend: supportsPromptExtension(task.modelId) && task.promptExtend,
  };
}

function applyTaskUpdate(task: LocalPreviewTask, wire: ImageEditTaskWire): void {
  task.backendTaskId = wire.id;
  task.status = wire.status;
  task.error = wire.error?.message ?? "";
  task.resultUrl = wire.result?.image_url ?? "";
  task.outputFilename = wire.result?.output_filename ?? "";
  if (wire.status === "succeeded" && wire.result?.preview_error) {
    task.error = wire.result.preview_error;
  }
  if (wire.status === "succeeded" && task.resultUrl && task.id === activeTaskId.value) {
    canvasView.value = "result";
  }
}

function gatewayMessage(error: unknown): string {
  if (error instanceof GatewayError) return error.message;
  if (error instanceof Error && error.name === "AbortError") return "操作已取消。";
  return "图片编辑请求失败，请稍后重试。";
}

async function generatePreview(task: LocalPreviewTask): Promise<void> {
  task.error = "";
  const metadata = requestPayload(task);
  if (!metadata) return;
  const controller = new AbortController();
  submitControllers.set(task.id, controller);
  task.status = "queued";
  task.backendTaskId = "";
  task.resultUrl = "";
  task.outputFilename = "";
  canvasView.value = "source";
  try {
    const wire = task.file
      ? await imageEditApi.submitFile(task.file, metadata, controller.signal)
      : await imageEditApi.submitRegistered(task.sourceImageId, metadata, controller.signal);
    applyTaskUpdate(task, wire);
    schedulePoll(250);
  } catch (error) {
    if (controller.signal.aborted) {
      task.status = "cancelled";
      return;
    }
    task.status = "failed";
    task.error = gatewayMessage(error);
  } finally {
    submitControllers.delete(task.id);
  }
}

function schedulePoll(delay = 900): void {
  if (destroyed || pollTimer !== null || polling) return;
  if (!tasks.value.some((task) => ACTIVE_STATUSES.has(task.status) && task.backendTaskId)) return;
  pollTimer = window.setTimeout(() => {
    pollTimer = null;
    void pollTasks();
  }, delay);
}

async function pollTasks(): Promise<void> {
  if (destroyed || polling) return;
  const activeBackendIds = new Set(tasks.value
    .filter((task) => ACTIVE_STATUSES.has(task.status) && task.backendTaskId)
    .map((task) => task.backendTaskId));
  if (activeBackendIds.size === 0) return;
  polling = true;
  try {
    const payload = await imageEditApi.list();
    const byId = new Map(payload.tasks.map((wire) => [wire.id, wire]));
    for (const task of tasks.value) {
      if (!activeBackendIds.has(task.backendTaskId)) continue;
      const wire = byId.get(task.backendTaskId);
      if (wire) applyTaskUpdate(task, wire);
    }
    pollErrorShown = false;
  } catch (error) {
    if (!pollErrorShown) {
      pollErrorShown = true;
      emit("toast", "任务状态暂不可用", gatewayMessage(error), "error");
    }
  } finally {
    polling = false;
    schedulePoll();
  }
}

async function cancelTask(task: LocalPreviewTask): Promise<void> {
  if (!ACTIVE_STATUSES.has(task.status)) {
    removeTask(task);
    return;
  }
  if (!task.backendTaskId) return;
  task.status = "cancelling";
  try {
    applyTaskUpdate(task, await imageEditApi.cancel(task.backendTaskId));
    schedulePoll(250);
  } catch (error) {
    task.error = gatewayMessage(error);
    emit("toast", "取消失败", task.error, "error");
    schedulePoll();
  }
}

async function abandonActiveTasks(): Promise<void> {
  if (!canAbandonActive.value || activeCount.value === 0) return;
  if (!window.confirm("确定放弃全部运行中的图片编辑任务吗？未保存的结果不会保留。")) return;
  try {
    await imageEditApi.abandonAll();
    for (const task of tasks.value) {
      if (ACTIVE_STATUSES.has(task.status)) task.status = "cancelled";
    }
    emit("toast", "已放弃运行任务", "原图和编辑指令仍保留在当前页面。", "info");
  } catch (error) {
    emit("toast", "无法放弃任务", gatewayMessage(error), "error");
  }
}

function chooseOutputDirectory(): void {
  emit("selectDirectory", (path) => {
    if (!path) return;
    void imageEditApi.updateSettings(path).then((settings) => {
      outputDirectory.value = settings.output_directory ?? "";
      emit("toast", "输出目录已保存", outputDirectory.value, "success");
    }).catch((error) => {
      emit("toast", "无法保存输出目录", gatewayMessage(error), "error");
    });
  });
}

async function loadSettings(): Promise<void> {
  settingsLoading.value = true;
  try {
    const settings = await imageEditApi.settings();
    outputDirectory.value = settings.output_directory ?? "";
  } catch (error) {
    emit("toast", "无法读取输出目录", gatewayMessage(error), "error");
  } finally {
    settingsLoading.value = false;
  }
}

watch(
  () => props.incomingSource,
  (source) => {
    if (!source || source.key === processedIncomingKey.value) return;
    processedIncomingKey.value = source.key;
    importIncomingSource(source);
  },
  { immediate: true },
);

watch(
  () => props.visible,
  (visible) => {
    if (!visible) dragActive.value = false;
    else schedulePoll(100);
  },
);

onMounted(() => {
  void loadSettings();
});

onBeforeUnmount(() => {
  destroyed = true;
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  for (const task of tasks.value) {
    clearTaskTimers(task.id);
    if (task.ownsSourceUrl) URL.revokeObjectURL(task.sourceUrl);
  }
});
</script>

<template>
  <section
    class="image-edit-page"
    :class="{ 'is-dragging': dragActive }"
    aria-labelledby="image-edit-title"
    @dragenter="handleDragEnter"
    @dragover="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
    @paste="handlePaste"
  >
    <input
      ref="fileInput"
      class="visually-hidden"
      type="file"
      accept=".jpg,.jpeg,.png,.webp,.bmp,.gif,.tif,.tiff,image/*"
      multiple
      data-testid="image-edit-file-input"
      @change="handleFileSelection"
    />

    <header class="workspace-header">
      <div class="title-copy">
        <p>Qwen 图片编辑</p>
        <h1 id="image-edit-title">图片编辑</h1>
      </div>
      <div class="task-overview" role="status">
        <span>进行中 {{ activeCount }}</span>
        <button
          v-if="activeCount > 0"
          class="button danger compact"
          type="button"
          :disabled="!canAbandonActive"
          @click="abandonActiveTasks"
        >放弃运行任务</button>
      </div>
      <div class="header-actions">
        <div class="directory-summary" title="任务提交时使用当前目录">
          <span>输出目录</span>
          <strong>{{ settingsLoading ? "正在读取…" : outputDirectory || "未选择" }}</strong>
        </div>
        <button class="button secondary" type="button" :disabled="settingsLoading" @click="chooseOutputDirectory">更改目录</button>
        <button class="button primary" type="button" @click="openFilePicker">导入图片</button>
      </div>
    </header>

    <div v-if="tasks.length === 0" class="empty-workspace">
      <div class="empty-visual" aria-hidden="true">
        <span>＋</span>
      </div>
      <div>
        <h2>导入要编辑的图片</h2>
        <p>拖放图片到此处，使用剪贴板粘贴，或一次选择最多 50 张。</p>
      </div>
      <button class="button primary" type="button" @click="openFilePicker">选择图片</button>
      <small>每张图片建立一个独立草稿；只有手动提交的任务才会开始处理。</small>
    </div>

    <div v-else class="workspace-body">
      <aside v-if="activeTask" class="settings-panel" aria-label="图片编辑参数">
        <header class="settings-heading">
          <span>当前任务</span>
          <strong :title="activeTask.name">{{ activeTask.name }}</strong>
          <small>{{ dimensionsLabel(activeTask) }} · {{ formatBytes(activeTask.sizeBytes) }}</small>
        </header>

        <div class="settings-fields">
          <label class="model-field">
            <span>模型</span>
            <select v-model="activeTask.modelId" :disabled="activeTaskLocked" @change="updateModel(activeTask)">
              <optgroup v-for="group in IMAGE_EDIT_MODEL_GROUPS" :key="group.label" :label="group.label">
                <option v-for="model in group.models" :key="model.id" :value="model.id">
                  {{ model.id }} · {{ model.release === "snapshot" ? "固定快照" : "浮动别名" }}
                </option>
              </optgroup>
            </select>
          </label>

          <label class="ratio-field">
            <span>画幅比例</span>
            <select
              v-model="activeTask.aspectRatio"
              :disabled="activeTaskLocked || !activeSupportsSize"
              @change="updateAspectRatio(activeTask)"
            >
              <option v-for="ratio in IMAGE_EDIT_ASPECT_RATIOS" :key="ratio" :value="ratio">
                {{ ratio === "auto" ? "跟随原图" : ratio === "custom" ? "自定义" : ratio }}
              </option>
            </select>
            <small v-if="!activeSupportsSize">当前模型不支持指定比例</small>
          </label>

          <label class="size-field">
            <span>图片尺寸</span>
            <select v-model="activeTask.outputSize" :disabled="activeTaskLocked || !activeSupportsSize">
              <option v-for="option in activeSizeOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
            <small v-if="!activeSupportsSize">当前模型不支持指定尺寸</small>
            <small v-else-if="activeTask.aspectRatio !== 'auto' && activeTask.aspectRatio !== 'custom'">
              {{ activeIsQwenThree ? "常用有效尺寸" : "阿里云推荐尺寸" }}
            </small>
          </label>

          <div v-if="activeTask.aspectRatio === 'custom' && activeSupportsSize" class="size-inputs">
            <label>
              <span>宽</span>
              <input
                v-model.number="activeTask.widthInput"
                :disabled="activeTaskLocked"
                type="number"
                :min="activeSizeRule === 'side-range' ? 512 : 1"
                :max="activeSizeRule === 'side-range' ? 2048 : undefined"
              />
            </label>
            <span aria-hidden="true">×</span>
            <label>
              <span>高</span>
              <input
                v-model.number="activeTask.heightInput"
                :disabled="activeTaskLocked"
                type="number"
                :min="activeSizeRule === 'side-range' ? 512 : 1"
                :max="activeSizeRule === 'side-range' ? 2048 : undefined"
              />
            </label>
            <small>
              {{ activeSizeRule === "side-range"
                ? "宽高均为 512–2048 px"
                : activeIsQwenThree
                  ? "总像素为 512×512–2048×2048，宽高比为 1:8–8:1"
                  : "总像素为 512×512–2048×2048" }}；服务可能调整为 16 px 的倍数
            </small>
          </div>

          <label>
            <span>随机种子</span>
            <input
              v-model="activeTask.seed"
              :disabled="activeTaskLocked"
              type="number"
              min="0"
              max="2147483647"
              placeholder="随机"
            />
          </label>

          <label class="negative-field">
            <span>反向提示词</span>
            <textarea
              v-model="activeTask.negativePrompt"
              :disabled="activeTaskLocked"
              :maxlength="activeIsQwenThree ? undefined : 500"
              rows="4"
              placeholder="例如：模糊、低清晰度、多余手指"
            ></textarea>
            <small v-if="!activeIsQwenThree">最多 500 字符</small>
          </label>

          <label class="check-setting" :class="{ disabled: !activeSupportsPromptExtension }">
            <input v-model="activeTask.promptExtend" :disabled="activeTaskLocked || !activeSupportsPromptExtension" type="checkbox" />
            <span>
              <strong>提示词智能改写</strong>
              <small>{{ activeSupportsPromptExtension ? "默认开启" : "当前模型不支持" }}</small>
            </span>
          </label>
        </div>
      </aside>

      <main v-if="activeTask" class="editor-workspace">
        <section class="canvas-section" aria-label="图片画布">
          <header class="canvas-toolbar">
            <div class="canvas-meta">
              <strong :title="activeTask.name">{{ activeTask.name }}</strong>
              <small>{{ dimensionsLabel(activeTask) }}</small>
            </div>
            <div class="canvas-mode" role="group" aria-label="画布内容">
              <button
                type="button"
                :class="{ active: canvasView === 'source' }"
                :aria-pressed="canvasView === 'source'"
                @click="canvasView = 'source'"
              >原图</button>
              <button
                type="button"
                :class="{ active: canvasView === 'result' }"
                :aria-pressed="canvasView === 'result'"
                :disabled="!activeTask.resultUrl"
                @click="canvasView = 'result'"
              >结果</button>
            </div>
            <span class="canvas-status">{{ statusLabel(activeTask.status) }}</span>
          </header>

          <div
            class="image-canvas checkerboard"
            :aria-busy="ACTIVE_STATUSES.has(activeTask.status)"
            :aria-label="canvasView === 'result' ? '生成结果画布' : '原图画布'"
          >
            <img
              :src="canvasImageUrl"
              :alt="canvasView === 'result' ? '图片编辑生成结果' : `原图：${activeTask.name}`"
            />
            <div v-if="ACTIVE_STATUSES.has(activeTask.status)" class="canvas-progress">
              <span class="progress-mark" aria-hidden="true"></span>
              <strong>{{ statusLabel(activeTask.status) }}</strong>
              <p>{{ statusDescription(activeTask.status) }}</p>
            </div>
            <p v-if="canvasView === 'result' && activeTask.outputFilename" class="result-label">
              已保存：{{ activeTask.outputFilename }}
            </p>
          </div>
        </section>

        <form class="prompt-composer" @submit.prevent="generatePreview(activeTask)">
          <label class="prompt-field">
            <span>编辑指令</span>
            <textarea
              v-model="activeTask.prompt"
              :disabled="activeTaskLocked"
              rows="3"
              placeholder="描述希望如何修改这张图片，例如：保留人物和构图，将背景改为雨后的东京街道"
            ></textarea>
          </label>

          <p v-if="activeTask.error" class="form-error" role="alert">{{ activeTask.error }}</p>

          <footer class="composer-footer">
            <div class="submit-actions">
              <button
                v-if="activeTaskLocked && activeTask.backendTaskId"
                class="button secondary"
                type="button"
                :disabled="activeTask.status === 'cancelling'"
                @click="cancelTask(activeTask)"
              >取消任务</button>
              <button
                class="button primary generate-button"
                type="button"
                :disabled="activeTaskLocked || !activeTask.prompt.trim() || !outputDirectory"
                @click="generatePreview(activeTask)"
              >{{ activeTaskLocked
                ? statusLabel(activeTask.status)
                : activeTask.status === "failed"
                  ? "重新生成"
                  : activeTask.status === "succeeded"
                    ? "再次生成"
                    : "生成图片" }}</button>
            </div>
          </footer>
        </form>
      </main>

      <aside class="task-rail" aria-label="图片编辑任务">
        <header>
          <button type="button" aria-label="继续导入图片" title="继续导入图片" @click="openFilePicker">＋</button>
          <span>{{ tasks.length }} 个任务</span>
        </header>
        <div class="task-summary" aria-live="polite">
          <span>进行中 {{ activeCount }}</span>
          <span>等待 {{ waitingCount }}</span>
        </div>
        <div class="task-list">
          <article
            v-for="task in tasks"
            :key="task.id"
            class="task-row"
            :class="{ active: task.id === activeTaskId }"
          >
            <button
              class="task-select"
              type="button"
              :aria-label="task.name + '，' + statusLabel(task.status)"
              :title="task.name"
              @click="selectTask(task.id)"
            >
              <img :src="task.sourceUrl" alt="" />
              <small>{{ statusLabel(task.status) }}</small>
            </button>
            <button
              class="task-remove"
              type="button"
              :disabled="ACTIVE_STATUSES.has(task.status) && !task.backendTaskId"
              :aria-label="(ACTIVE_STATUSES.has(task.status) ? '取消 ' : '移除 ') + task.name"
              :title="ACTIVE_STATUSES.has(task.status) ? '取消任务' : '移除任务，不删除原图或已保存文件'"
              @click="cancelTask(task)"
            >×</button>
          </article>
        </div>
      </aside>
    </div>

    <div v-if="dragActive" class="drop-overlay" aria-hidden="true">
      <div><strong>松开即可建立编辑草稿</strong><span>每张图片会成为独立任务</span></div>
    </div>
  </section>
</template>

<style scoped>
.image-edit-page {
  --ie-bg: #f6f6f4;
  --ie-surface: #fff;
  --ie-surface-soft: #f1f1ee;
  --ie-text: #181816;
  --ie-muted: #6d6d66;
  --ie-faint: #92928a;
  --ie-line: rgb(24 24 22 / 10%);
  --ie-line-strong: rgb(24 24 22 / 17%);
  --ie-accent: #252522;
  --ie-accent-text: #fff;
  --ie-danger: #9b333d;
  position: relative;
  display: grid;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  grid-template-rows: auto minmax(0, 1fr);
  overflow: hidden;
  color: var(--ie-text);
  background: var(--ie-bg);
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.workspace-header {
  display: grid;
  min-height: 72px;
  grid-template-columns: minmax(140px, auto) minmax(200px, 1fr) auto;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--ie-line);
  background: var(--ie-surface);
}

.title-copy p,
.title-copy h1 { margin: 0; }
.title-copy p { color: var(--ie-muted); font-size: 11px; letter-spacing: .07em; text-transform: uppercase; }
.title-copy h1 { margin-top: 2px; font-size: 21px; font-weight: 620; letter-spacing: -.02em; }
.task-overview { display: flex; min-width: 0; align-items: center; justify-content: center; gap: 10px; color: var(--ie-muted); font-size: 12px; }
.header-actions { display: flex; min-width: 0; align-items: center; justify-content: flex-end; gap: 8px; }
.directory-summary { display: grid; min-width: 150px; max-width: 260px; gap: 1px; text-align: right; }
.directory-summary span { color: var(--ie-muted); font-size: 11px; }
.directory-summary strong { overflow: hidden; font-size: 12px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }

.button {
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  padding: 0 13px;
  border: 1px solid var(--ie-line-strong);
  border-radius: 9px;
  color: var(--ie-text);
  font-weight: 580;
  background: var(--ie-surface);
}
.button:hover:not(:disabled) { background: var(--ie-surface-soft); }
.button.primary { color: var(--ie-accent-text); border-color: var(--ie-accent); background: var(--ie-accent); }
.button.primary:hover:not(:disabled) { background: #050505; }
.button.secondary { background: var(--ie-surface-soft); }
.button.danger { color: var(--ie-danger); border-color: color-mix(in srgb, var(--ie-danger) 35%, transparent); background: transparent; }
.button.compact { min-height: 30px; padding-inline: 10px; font-size: 11px; }
.button:disabled { opacity: .45; }

.empty-workspace {
  display: grid;
  place-items: center;
  align-content: center;
  gap: 18px;
  padding: 32px;
  text-align: center;
}
.empty-workspace h2 { margin: 0; font-size: 20px; font-weight: 600; }
.empty-workspace p { max-width: 470px; margin: 6px 0 0; color: var(--ie-muted); }
.empty-workspace small { color: var(--ie-faint); }
.empty-visual { display: grid; width: 72px; height: 72px; place-items: center; border: 1px dashed var(--ie-line-strong); border-radius: 18px; color: var(--ie-muted); background: var(--ie-surface); }
.empty-visual span { font-size: 28px; font-weight: 300; }

.workspace-body {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-columns: 260px minmax(0, 1fr) 104px;
  grid-template-areas: "settings canvas tasks";
}

.settings-panel {
  grid-area: settings;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  border-right: 1px solid var(--ie-line);
  background: var(--ie-surface);
  scrollbar-gutter: stable;
}
.settings-heading { display: grid; gap: 2px; padding: 14px 16px; border-bottom: 1px solid var(--ie-line); }
.settings-heading span { color: var(--ie-muted); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
.settings-heading strong { overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.settings-heading small { color: var(--ie-muted); font-size: 10px; }
.settings-fields { display: grid; gap: 14px; padding: 14px 16px 20px; }
.settings-fields label,
.prompt-composer label { display: grid; gap: 5px; color: var(--ie-muted); font-size: 11px; }
.settings-fields label > span,
.prompt-field > span { color: var(--ie-text); font-weight: 580; }
.settings-fields :is(input, select, textarea),
.prompt-composer textarea {
  width: 100%;
  border: 1px solid var(--ie-line-strong);
  border-radius: 8px;
  color: var(--ie-text);
  background: var(--ie-surface);
}
.settings-fields :is(input, select) { min-height: 36px; padding: 0 9px; }
.settings-fields textarea,
.prompt-composer textarea { resize: vertical; padding: 9px 10px; line-height: 1.5; }
.settings-fields :is(input, select, textarea):focus,
.prompt-composer textarea:focus { outline: 0; border-color: #77776f; box-shadow: 0 0 0 3px rgb(80 80 74 / 9%); }
.settings-fields label small { color: var(--ie-faint); }
.model-field { width: 100%; }
.size-inputs { display: grid; grid-template-columns: minmax(64px, 1fr) auto minmax(64px, 1fr); align-items: end; gap: 6px; }
.size-inputs > span { padding-bottom: 9px; color: var(--ie-muted); }
.size-inputs > small { grid-column: 1 / -1; color: var(--ie-faint); font-size: 10px; line-height: 1.45; }
.negative-field textarea { min-height: 104px; }
.check-setting {
  display: flex !important;
  min-height: 52px;
  align-items: center;
  gap: 9px !important;
  padding: 8px 9px;
  border-top: 1px solid var(--ie-line);
  border-bottom: 1px solid var(--ie-line);
}
.check-setting input { width: 16px !important; min-height: 16px !important; }
.check-setting span { display: grid; gap: 1px; }
.check-setting strong { color: var(--ie-text); font-size: 11px; }
.check-setting.disabled { opacity: .55; }

.editor-workspace {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-area: canvas;
  grid-template-rows: minmax(320px, 1fr) auto;
  overflow: hidden;
}
.canvas-section { display: grid; min-width: 0; min-height: 0; grid-template-rows: 48px minmax(0, 1fr); overflow: hidden; padding: 0 16px 16px; }
.canvas-toolbar { display: grid; min-width: 0; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 12px; }
.canvas-meta { display: flex; min-width: 0; align-items: baseline; gap: 7px; }
.canvas-meta strong { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.canvas-meta small { flex: 0 0 auto; color: var(--ie-muted); font-size: 10px; }
.canvas-mode { display: inline-flex; padding: 3px; border-radius: 10px; background: var(--ie-surface-soft); }
.canvas-mode button { min-height: 28px; padding: 0 12px; border: 0; border-radius: 7px; color: var(--ie-muted); font-size: 11px; background: transparent; }
.canvas-mode button.active { color: var(--ie-text); background: var(--ie-surface); box-shadow: 0 1px 3px rgb(0 0 0 / 8%); }
.canvas-mode button:disabled { opacity: .38; }
.canvas-status { justify-self: end; color: var(--ie-muted); font-size: 11px; }
.image-canvas { position: relative; display: grid; min-width: 0; min-height: 0; place-items: center; overflow: hidden; padding: 20px; background-color: #e5e8e4; }
.image-canvas img { display: block; width: 100%; height: 100%; min-height: 0; object-fit: contain; }
.checkerboard {
  background-image:
    linear-gradient(45deg, rgb(255 255 255 / 32%) 25%, transparent 25%),
    linear-gradient(-45deg, rgb(255 255 255 / 32%) 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, rgb(255 255 255 / 32%) 75%),
    linear-gradient(-45deg, transparent 75%, rgb(255 255 255 / 32%) 75%);
  background-position: 0 0, 0 10px, 10px -10px, -10px 0;
  background-size: 20px 20px;
}
.canvas-progress { position: absolute; inset: 0; display: grid; place-content: center; place-items: center; gap: 7px; padding: 24px; color: var(--ie-muted); text-align: center; background: rgb(246 246 244 / 88%); backdrop-filter: blur(4px); }
.canvas-progress > span { display: grid; width: 42px; height: 42px; place-items: center; border: 1px solid var(--ie-line-strong); }
.canvas-progress strong { color: var(--ie-text); font-size: 13px; }
.canvas-progress p { max-width: 320px; margin: 0; font-size: 11px; }
.progress-mark { border-color: var(--ie-line-strong) !important; border-top-color: var(--ie-accent) !important; border-radius: 50% !important; animation: ie-spin .9s linear infinite; }
.result-label { position: absolute; right: 10px; bottom: 10px; max-width: calc(100% - 20px); overflow: hidden; margin: 0; padding: 4px 7px; border-radius: 6px; color: #fff; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; background: rgb(0 0 0 / 68%); }

.prompt-composer { display: grid; gap: 10px; padding: 12px 16px 14px; border-top: 1px solid var(--ie-line); background: var(--ie-surface); }
.prompt-composer textarea { min-height: 74px; }
.form-error { margin: 0; color: var(--ie-danger); font-size: 12px; }
.composer-footer { display: flex; align-items: center; justify-content: flex-end; gap: 16px; }
.submit-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 8px; }
.submit-actions > span { color: var(--ie-muted); font-size: 11px; }
.generate-button { min-width: 106px; }

.task-rail {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-area: tasks;
  grid-template-rows: auto auto minmax(0, 1fr);
  border-left: 1px solid var(--ie-line);
  background: var(--ie-surface);
}
.task-rail > header { display: grid; justify-items: center; gap: 6px; padding: 10px 8px 8px; border-bottom: 1px solid var(--ie-line); }
.task-rail > header button { display: grid; width: 68px; height: 58px; place-items: center; padding: 0; border: 1px dashed var(--ie-line-strong); border-radius: 9px; color: var(--ie-muted); font-size: 22px; background: var(--ie-surface-soft); }
.task-rail > header button:hover { color: var(--ie-text); border-style: solid; }
.task-rail > header span { color: var(--ie-muted); font-size: 10px; }
.task-summary { display: grid; gap: 1px; padding: 7px 4px; border-bottom: 1px solid var(--ie-line); color: var(--ie-muted); font-size: 9px; text-align: center; }
.task-list { display: grid; min-height: 0; align-content: start; gap: 8px; overflow: auto; padding: 8px; }
.task-row { position: relative; min-width: 0; padding: 4px; border: 1px solid transparent; border-radius: 10px; }
.task-row:hover { background: var(--ie-surface-soft); }
.task-row.active { border-color: var(--ie-accent); background: var(--ie-surface-soft); }
.task-select { display: grid; width: 100%; min-width: 0; gap: 4px; padding: 0; border: 0; color: inherit; text-align: center; background: transparent; }
.task-select img { width: 100%; aspect-ratio: 1; border-radius: 6px; object-fit: cover; background: var(--ie-surface-soft); }
.task-select small { overflow: hidden; color: var(--ie-muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.task-remove { position: absolute; top: 0; right: 0; display: grid; width: 22px; height: 22px; place-items: center; padding: 0; border: 0; border-radius: 50%; color: var(--ie-faint); background: var(--ie-surface); box-shadow: 0 1px 4px rgb(0 0 0 / 12%); }
.task-remove:hover:not(:disabled) { color: var(--ie-danger); }

.drop-overlay { position: absolute; z-index: 50; inset: 10px; display: grid; place-items: center; border: 2px dashed #77776f; border-radius: 14px; color: var(--ie-text); background: rgb(246 246 244 / 94%); backdrop-filter: blur(5px); pointer-events: none; }
.drop-overlay div { display: grid; gap: 5px; text-align: center; }
.drop-overlay strong { font-size: 17px; }
.drop-overlay span { color: var(--ie-muted); font-size: 12px; }

@keyframes ie-spin { to { transform: rotate(360deg); } }

@media (max-width: 1180px) {
  .workspace-header { grid-template-columns: auto 1fr; }
  .task-overview { justify-self: end; }
  .header-actions { grid-column: 1 / -1; justify-content: stretch; }
  .directory-summary { max-width: none; flex: 1; text-align: left; }
  .workspace-body { grid-template-columns: 232px minmax(0, 1fr) 92px; }
  .settings-fields { padding-inline: 12px; }
  .task-rail > header button { width: 60px; }
}

@media (max-width: 820px) {
  .image-edit-page { overflow: auto; }
  .workspace-header { position: sticky; z-index: 5; top: 0; grid-template-columns: 1fr auto; }
  .task-overview > span { display: none; }
  .workspace-body {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto auto;
    grid-template-areas: "tasks" "canvas" "settings";
  }
  .task-rail {
    position: relative;
    z-index: 3;
    height: 88px;
    min-height: 88px;
    grid-template-columns: 90px 92px minmax(0, 1fr);
    grid-template-rows: auto;
    overflow: hidden;
    border-bottom: 1px solid var(--ie-line);
    border-left: 0;
  }
  .task-rail > header { align-items: center; padding: 7px; border-right: 1px solid var(--ie-line); border-bottom: 0; }
  .task-rail > header button { width: 52px; height: 52px; }
  .task-rail > header span { display: none; }
  .task-summary { align-content: center; border-right: 1px solid var(--ie-line); border-bottom: 0; }
  .task-list { display: flex; gap: 6px; overflow-x: auto; padding: 6px; }
  .task-row { width: 76px; min-width: 76px; }
  .task-select small { display: none; }
  .settings-panel { border-top: 1px solid var(--ie-line); border-right: 0; }
  .editor-workspace { min-height: 700px; grid-template-rows: minmax(430px, 1fr) auto; overflow: visible; }
  .submit-actions { justify-content: flex-end; }
}

@media (max-width: 560px) {
  .workspace-header { padding: 10px 12px; }
  .title-copy p { display: none; }
  .header-actions { display: grid; grid-template-columns: 1fr 1fr; }
  .directory-summary { grid-column: 1 / -1; }
  .task-rail { grid-template-columns: 72px minmax(0, 1fr); }
  .task-rail > header { grid-template-columns: 1fr; }
  .task-summary { display: none; }
  .canvas-section { padding: 0 8px 8px; }
  .canvas-toolbar { grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; }
  .canvas-meta small { display: none; }
  .image-canvas { padding: 10px; }
  .prompt-composer { padding-inline: 12px; }
  .composer-footer { align-items: stretch; flex-direction: column; }
  .submit-actions { align-items: stretch; flex-direction: column; }
}

@media (prefers-color-scheme: dark) {
  .image-edit-page {
    color-scheme: dark;
    --ie-bg: #1c1d1a;
    --ie-surface: #252622;
    --ie-surface-soft: #30312c;
    --ie-text: #f1f1eb;
    --ie-muted: #b2b2aa;
    --ie-faint: #8f9088;
    --ie-line: rgb(255 255 255 / 10%);
    --ie-line-strong: rgb(255 255 255 / 17%);
    --ie-accent: #ecece5;
    --ie-accent-text: #171714;
    --ie-danger: #ff9aa7;
  }
  .button.primary:hover:not(:disabled) { background: #fff; }
  .image-canvas { background-color: #161713; }
  .checkerboard { background-image: linear-gradient(45deg, #20211d 25%, transparent 25%), linear-gradient(-45deg, #20211d 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #20211d 75%), linear-gradient(-45deg, transparent 75%, #20211d 75%); }
  .canvas-progress { background: rgb(28 29 26 / 88%); }
  .drop-overlay { background: rgb(28 29 26 / 95%); }
}

@media (prefers-reduced-motion: reduce) {
  .progress-mark { animation: none; }
}
</style>
