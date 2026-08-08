export interface ImageEditIncomingSource {
  key: string;
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
  | "succeeded"
  | "failed";
