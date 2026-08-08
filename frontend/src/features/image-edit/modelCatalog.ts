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
