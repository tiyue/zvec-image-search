export interface ImageEditIncomingSource {
  key: string;
  imageId: string;
  name: string;
  url: string;
  sizeBytes?: number;
}

export type ImageEditPreviewStatus =
  | "draft"
  | "queued"
  | "uploading"
  | "generating"
  | "downloading"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface ImageEditRequestPayload {
  model: string;
  prompt: string;
  negative_prompt: string;
  size?: string;
  seed?: number;
  prompt_extend: boolean;
}

export interface ImageEditTaskError {
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string | null;
  status_code?: number | null;
}

export interface ImageEditTaskResult {
  output_filename?: string;
  request_id?: string | null;
  width?: number | null;
  height?: number | null;
  image_count?: number;
  image_id?: string;
  image_url?: string;
  thumbnail_url?: string;
  preview_error?: string;
}

export interface ImageEditTaskWire {
  id: string;
  source_name: string;
  submitted_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  status: ImageEditPreviewStatus;
  stage: string;
  model: string;
  prompt: string;
  negative_prompt: string;
  size?: string | null;
  seed?: number | null;
  prompt_extend: boolean;
  error?: ImageEditTaskError | null;
  result?: ImageEditTaskResult | null;
}

export interface ImageEditSettings {
  configured: boolean;
  output_directory: string | null;
}
