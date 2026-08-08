export interface ImageEditModelOption {
  id: string;
  release: "alias" | "snapshot";
}

export interface ImageEditModelGroup {
  label: string;
  models: ImageEditModelOption[];
}

export const DEFAULT_IMAGE_EDIT_MODEL = "qwen-image-edit-plus";

export const IMAGE_EDIT_MODEL_GROUPS: ImageEditModelGroup[] = [
  {
    label: "Qwen Image 3.0",
    models: [
      { id: "qwen-image-3.0-pro", release: "alias" },
      { id: "qwen-image-3.0", release: "alias" },
    ],
  },
  {
    label: "Qwen Image 2.0 Pro",
    models: [
      { id: "qwen-image-2.0-pro", release: "alias" },
      { id: "qwen-image-2.0-pro-2026-06-22", release: "snapshot" },
      { id: "qwen-image-2.0-pro-2026-04-22", release: "snapshot" },
      { id: "qwen-image-2.0-pro-2026-03-03", release: "snapshot" },
    ],
  },
  {
    label: "Qwen Image 2.0",
    models: [
      { id: "qwen-image-2.0", release: "alias" },
      { id: "qwen-image-2.0-2026-03-03", release: "snapshot" },
    ],
  },
  {
    label: "Qwen Image Edit Max",
    models: [
      { id: "qwen-image-edit-max", release: "alias" },
      { id: "qwen-image-edit-max-2026-01-16", release: "snapshot" },
    ],
  },
  {
    label: "Qwen Image Edit Plus",
    models: [
      { id: "qwen-image-edit-plus", release: "alias" },
      { id: "qwen-image-edit-plus-2025-12-15", release: "snapshot" },
      { id: "qwen-image-edit-plus-2025-10-30", release: "snapshot" },
    ],
  },
  {
    label: "Qwen Image Edit",
    models: [{ id: "qwen-image-edit", release: "alias" }],
  },
];

export function supportsImageEditSize(modelId: string): boolean {
  return modelId !== "qwen-image-edit";
}

export function supportsPromptExtension(modelId: string): boolean {
  return modelId !== "qwen-image-edit";
}

export function isQwenImageThree(modelId: string): boolean {
  return modelId === "qwen-image-3.0" || modelId === "qwen-image-3.0-pro";
}

export type ImageEditSizeRule = "pixel-range" | "side-range" | "automatic-only";

export const IMAGE_EDIT_ASPECT_RATIOS = [
  "auto",
  "1:1",
  "2:3",
  "3:2",
  "3:4",
  "4:3",
  "9:16",
  "16:9",
  "21:9",
  "custom",
] as const;

export type ImageEditAspectRatio = (typeof IMAGE_EDIT_ASPECT_RATIOS)[number];

export const IMAGE_EDIT_SIZE_PRESETS: Record<
  Exclude<ImageEditAspectRatio, "auto" | "custom">,
  readonly string[]
> = {
  "1:1": ["1024*1024", "1536*1536"],
  "2:3": ["768*1152", "1024*1536"],
  "3:2": ["1152*768", "1536*1024"],
  "3:4": ["960*1280", "1080*1440"],
  "4:3": ["1280*960", "1440*1080"],
  "9:16": ["720*1280", "1080*1920"],
  "16:9": ["1280*720", "1920*1080"],
  "21:9": ["1344*576", "2048*872"],
};

export function imageEditSizeRule(modelId: string): ImageEditSizeRule {
  if (modelId === "qwen-image-edit") return "automatic-only";
  if (
    modelId.startsWith("qwen-image-edit-plus") ||
    modelId.startsWith("qwen-image-edit-max")
  ) {
    return "side-range";
  }
  return "pixel-range";
}

export function validateImageEditDimensions(
  modelId: string,
  width: number,
  height: number,
): string | null {
  if (
    !Number.isInteger(width) ||
    !Number.isInteger(height) ||
    width < 1 ||
    height < 1
  ) {
    return "宽度和高度必须是正整数。";
  }
  const rule = imageEditSizeRule(modelId);
  if (rule === "automatic-only") return "当前模型不支持指定尺寸。";
  if (rule === "side-range") {
    return width >= 512 && width <= 2048 && height >= 512 && height <= 2048
      ? null
      : "当前模型的宽度和高度都必须在 512 到 2048 像素之间。";
  }
  const pixels = width * height;
  if (pixels < 512 * 512 || pixels > 2048 * 2048) {
    return "当前模型的总像素必须在 512×512 到 2048×2048 之间。";
  }
  if (isQwenImageThree(modelId) && (width > height * 8 || height > width * 8)) {
    return "Qwen Image 3.0 的输出宽高比必须在 1:8 到 8:1 之间。";
  }
  return null;
}
