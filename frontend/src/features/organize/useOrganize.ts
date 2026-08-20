import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
} from "vue";

import { organizeApi } from "./api";
import {
  ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE,
  ORGANIZE_GALLERY_MAX_PAGE_SIZE,
} from "./galleryCapacity";
import type {
  FolderDeletePreview,
  FolderDeletePreviewWire,
  FolderNameTagPreview,
  FolderNameTagPreviewWire,
  FolderNameTagRunOptions,
  FolderNameTagSelection,
  ManualTagOperation,
  OrganizeAlias,
  OrganizeAliasWire,
  OrganizeApi,
  OrganizeBootstrapResponse,
  OrganizeFolder,
  OrganizeFolderImagesResponse,
  OrganizeFolderListResponse,
  OrganizeFolderWire,
  OrganizeImage,
  OrganizeImageWire,
  OrganizeJob,
  OrganizeJobResponse,
  OrganizeJobWire,
  OrganizeRootWire,
  OrganizeSelection,
  OrganizeTagSource,
  OrganizeTagSourceWire,
  TagPresence,
  ToastKind,
} from "./types";

const FOLDER_PAGE_SIZE = 200;
const TERMINAL_JOB_STATUSES = new Set([
  "succeeded",
  "completed",
  "partial",
  "needs_attention",
  "failed",
  "cancelled",
]);
const SUCCESS_JOB_STATUSES = new Set(["succeeded", "completed", "partial", "needs_attention"]);
const MAX_JOB_POLL_FAILURES = 3;

interface OrganizeEvents {
  onToast?: (title: string, message: string, kind: ToastKind) => void;
  autoLoad?: boolean;
  pollIntervalMs?: number;
}

export interface FolderRootGroup {
  id: string;
  name: string;
  folderKey: string;
  folderCount: number;
  imageCount: number;
  firstIndexedAt: string;
  lastIndexedAt: string;
}

function text(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => text(item)).filter(Boolean))];
}

function integer(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function percent(value: unknown, fallback = 0): number {
  return Math.min(100, Math.max(0, integer(value, fallback)));
}

function comparisonKey(value: string): string {
  return value.trim().toLocaleLowerCase("zh-CN");
}

function uniqueTags(values: Iterable<string>): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const tag = value.trim();
    const key = comparisonKey(tag);
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    result.push(tag);
  }
  return result;
}

export function parseTagInput(value: string): string[] {
  return uniqueTags(value.split(/[\n\r,，、;；]+/u));
}

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    manual: "人工",
    manual_tag: "人工",
    folder: "文件夹",
    folder_name: "文件夹",
    accepted_auto: "模型已采用",
    model: "模型",
    model_field: "模型字段",
    model_entity: "模型身份",
    inherited: "同文件夹继承",
    inherited_folder: "同文件夹继承",
    sibling_inherited: "同文件夹继承",
    alias: "别名词典",
  };
  return labels[source] ?? (source || "来源未知");
}

function safeLocalUrl(value: unknown): string {
  const candidate = text(value);
  if (!candidate) return "";
  try {
    const url = new URL(candidate, document.baseURI);
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    const supported = url.protocol === "http:" || url.protocol === "https:";
    return supported && (url.origin === window.location.origin || loopback) ? url.href : "";
  } catch {
    return "";
  }
}

function imageEndpoint(id: string, variant: "thumbnail" | "preview"): string {
  return safeLocalUrl(`api/image/${encodeURIComponent(id)}?variant=${variant}`);
}

function sourceRecord(
  tag: string,
  source: string,
  raw: Partial<OrganizeTagSourceWire> = {},
): OrganizeTagSource | null {
  if (!tag.trim()) return null;
  const normalizedSource = source || "unknown";
  const confidenceValue = Number(raw.confidence);
  return {
    tag: tag.trim(),
    source: normalizedSource,
    sourceLabel: sourceLabel(normalizedSource),
    field: text(raw.field),
    entityType: text(raw.entity_type),
    confidence: Number.isFinite(confidenceValue) ? confidenceValue : null,
    inherited:
      raw.inherited === true ||
      ["inherited", "inherited_folder", "sibling_inherited"].includes(normalizedSource),
  };
}

function sourceDetails(raw: OrganizeImageWire): OrganizeTagSource[] {
  const details: OrganizeTagSource[] = [];
  const append = (value: OrganizeTagSource | null): void => {
    if (value) details.push(value);
  };
  const values = [raw.tag_sources, raw.tag_details];
  for (const value of values) {
    if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === "string") {
          append(sourceRecord(item, "unknown"));
          continue;
        }
        if (!item || typeof item !== "object") continue;
        const detail = item as OrganizeTagSourceWire;
        const tag = text(detail.tag, detail.label);
        const sources = strings(detail.sources);
        if (sources.length) {
          for (const source of sources) append(sourceRecord(tag, source, detail));
        } else {
          append(
            sourceRecord(
              tag,
              text(detail.source, detail.source_kind, detail.kind, "unknown"),
              detail,
            ),
          );
        }
      }
      continue;
    }
    if (!value || typeof value !== "object") continue;
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (Array.isArray(item)) {
        const itemStrings = strings(item);
        const knownSource = [
          "manual",
          "folder",
          "model",
          "accepted_auto",
          "inherited",
          "inherited_folder",
          "sibling_inherited",
        ].includes(key);
        if (knownSource) {
          for (const tag of itemStrings) append(sourceRecord(tag, key));
        } else {
          for (const source of itemStrings) append(sourceRecord(key, source));
        }
      } else if (typeof item === "string") {
        append(sourceRecord(key, item));
      }
    }
  }
  const manual = strings(raw.manual_tags ?? raw.tags);
  const folder = strings(raw.folder_tags);
  const model = strings(raw.model_tags ?? raw.accepted_auto_tags);
  const inherited = strings(raw.inherited_tags);
  for (const tag of manual) append(sourceRecord(tag, "manual"));
  for (const tag of folder) append(sourceRecord(tag, "folder"));
  for (const tag of model) append(sourceRecord(tag, "accepted_auto"));
  for (const tag of inherited) append(sourceRecord(tag, "inherited_folder", { inherited: true }));
  const seen = new Set<string>();
  return details.filter((item) => {
    const key = `${comparisonKey(item.tag)}|${item.source}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function normalizeAlias(raw: OrganizeAliasWire): OrganizeAlias | null {
  const canonical = text(raw.canonical_name, raw.canonical);
  if (!canonical) return null;
  const canonicalKey = comparisonKey(canonical);
  return {
    canonical,
    aliases: uniqueTags(strings(raw.aliases)).filter(
      (alias) => comparisonKey(alias) !== canonicalKey,
    ),
  };
}

export function normalizeFolder(raw: OrganizeFolderWire, index = 0): OrganizeFolder {
  const relativeFolder = text(raw.relative_folder, raw.relative_path);
  const name = text(
    raw.name,
    relativeFolder.split(/[\\/]/u).filter(Boolean).at(-1),
    "根目录",
  );
  return {
    key: text(raw.folder_key, raw.key, `folder-${index + 1}`),
    rootId: text(raw.root_id, "root"),
    rootName: text(raw.root_name, raw.root_path, raw.root_id, "图库根目录"),
    relativeFolder,
    name,
    imageCount: Math.max(0, integer(raw.image_count ?? raw.total)),
    directImageCount: Math.max(
      0,
      integer(raw.direct_image_count ?? raw.image_count ?? raw.total),
    ),
    descendantImageCount: Math.max(0, integer(raw.descendant_image_count)),
    firstIndexedAt: text(raw.first_indexed_at),
    lastIndexedAt: text(raw.last_indexed_at),
    timestampSource: text(raw.timestamp_source),
    manualTaggedCount: Math.max(0, integer(raw.manual_tagged_count)),
    modelTaggedCount: Math.max(0, integer(raw.model_tagged_count)),
    failedCount: Math.max(0, integer(raw.failed_count)),
  };
}

export function normalizeImage(raw: OrganizeImageWire, index = 0): OrganizeImage {
  const sourceId = text(raw.doc_id, raw.id, raw.proposal_id);
  const id = sourceId || `image-${index + 1}`;
  const relativePath = text(raw.relative_path);
  const fileName = text(
    raw.file_name,
    raw.filename,
    raw.name,
    relativePath.split(/[\\/]/u).at(-1),
    `图片 ${index + 1}`,
  );
  const manualTags = strings(raw.manual_tags ?? raw.tags);
  const folderTags = strings(raw.folder_tags);
  const modelTags = strings(raw.model_tags ?? raw.accepted_auto_tags);
  const inheritedTags = strings(raw.inherited_tags);
  const effectiveTags = uniqueTags([
    ...strings(raw.effective_tags ?? raw.existing_tags),
    ...manualTags,
    ...folderTags,
    ...modelTags,
    ...inheritedTags,
  ]);
  const declaredUnavailable = raw.image_available === false;
  const explicitThumbnail = safeLocalUrl(raw.thumbnail_url);
  const explicitImage = safeLocalUrl(raw.image_url);
  const canBuildEndpoint = !declaredUnavailable && Boolean(sourceId);
  const thumbnailUrl = declaredUnavailable
    ? ""
    : explicitThumbnail || (canBuildEndpoint ? imageEndpoint(sourceId, "thumbnail") : "");
  const imageUrl = declaredUnavailable
    ? ""
    : explicitImage || (canBuildEndpoint ? imageEndpoint(sourceId, "preview") : "");
  return {
    id,
    nativeImageId: text(raw.image_id),
    libraryId: text(raw.library_id),
    fileName,
    relativePath,
    width: Math.max(0, integer(raw.width)),
    height: Math.max(0, integer(raw.height)),
    manualTags,
    folderTags,
    modelTags,
    inheritedTags,
    effectiveTags,
    tagSources: sourceDetails(raw),
    annotationStatus: text(raw.annotation_status, raw.status, "unknown").toLowerCase(),
    thumbnailUrl,
    imageUrl,
    imageAvailable: !declaredUnavailable && Boolean(thumbnailUrl || imageUrl),
  };
}

function normalizeRoot(raw: OrganizeRootWire, index = 0): FolderRootGroup {
  return {
    id: text(raw.root_id, `root-${index + 1}`),
    name: text(raw.root_name, raw.root_path, raw.root_id, "图库根目录"),
    folderKey: text(raw.folder_key),
    folderCount: Math.max(0, integer(raw.folder_count)),
    imageCount: Math.max(0, integer(raw.image_count)),
    firstIndexedAt: text(raw.first_indexed_at),
    lastIndexedAt: text(raw.last_indexed_at),
  };
}

function normalizeDeletePreview(payload: FolderDeletePreviewWire): FolderDeletePreview | null {
  const raw = payload.result ?? payload;
  const operationId = text(raw.operation_id, raw.preview_id);
  const confirmationToken = text(raw.confirmation_token, raw.preview_token, raw.token);
  const folderKey = text(raw.folder_key);
  if (!operationId || !confirmationToken || !folderKey) return null;
  const folderName = text(raw.folder_name, raw.relative_folder, raw.root_name, "所选文件夹");
  return {
    operationId,
    confirmationToken,
    folderKey,
    folderName,
    relativeFolder: text(raw.relative_folder),
    rootName: text(raw.root_name),
    includeSubfolders: true,
    imageCount: Math.max(0, integer(raw.image_count)),
    fileCount: Math.max(0, integer(raw.file_count ?? raw.image_count)),
    sizeBytes: Math.max(0, integer(raw.size_bytes)),
    protectedCount: Math.max(0, integer(raw.protected_count)),
    changedCount: Math.max(0, integer(raw.changed_count)),
    missingCount: Math.max(0, integer(raw.missing_count)),
    confirmationPhrase: text(raw.confirmation_phrase, folderName),
    expiresAt: text(raw.expires_at),
    warnings: strings(raw.warnings),
    blocked: raw.blocked === true,
  };
}

function normalizeFolderNameTagPreview(
  payload: FolderNameTagPreviewWire,
): FolderNameTagPreview {
  const raw = payload.result ?? payload;
  const samples = Array.isArray(raw.samples)
    ? raw.samples
        .filter((item): item is Record<string, unknown> =>
          typeof item === "object" && item !== null,
        )
        .map((item) => ({
          rootId: text(item.root_id),
          relativeFolder: text(item.relative_folder),
          currentTags: strings(item.current_tags),
          proposedTags: strings(item.proposed_tags),
          affectedImages: Math.max(0, integer(item.affected_images)),
          manualRemoved: Math.max(0, integer(item.manual_removed)),
          inheritedRemoved: Math.max(0, integer(item.inherited_removed)),
        }))
    : [];
  return {
    selected: Math.max(0, integer(raw.selected)),
    processed: Math.max(0, integer(raw.processed)),
    changed: Math.max(0, integer(raw.changed)),
    unchanged: Math.max(0, integer(raw.unchanged)),
    untagged: Math.max(0, integer(raw.untagged)),
    foldersScanned: Math.max(0, integer(raw.folders_scanned)),
    changedFolders: Math.max(0, integer(raw.changed_folders)),
    skipped: Math.max(0, integer(raw.skipped)),
    removedBlacklist: Math.max(0, integer(raw.removed_blacklist)),
    removedLegacy: Math.max(0, integer(raw.removed_legacy)),
    removedDuplicates: Math.max(0, integer(raw.removed_duplicates)),
    mode: ["normal", "clean", "mark_all"].includes(String(raw.mode))
      ? raw.mode as FolderNameTagPreview["mode"]
      : "normal",
    force: raw.force !== false,
    ruleRevision: text(raw.rule_revision),
    blacklistCount: Math.max(0, integer(raw.blacklist_count)),
    samples,
    samplesTruncated: raw.samples_truncated === true,
  };
}

function jobWire(payload: OrganizeJobResponse): OrganizeJobWire | null {
  if (payload.job) return payload.job;
  if (payload.jobs?.length) return payload.jobs[0] ?? null;
  const possible = payload as OrganizeJobWire;
  return possible.id || possible.job_id ? possible : null;
}

export function normalizeOrganizeJob(
  payload: OrganizeJobResponse,
  fallbackTotal = 0,
): OrganizeJob {
  const raw = jobWire(payload) ?? {};
  const progress = raw.progress ?? {};
  const processed = Math.max(
    0,
    integer(raw.processed ?? progress.processed ?? progress.completed ?? progress.current),
  );
  const total = Math.max(0, integer(raw.total ?? progress.total, fallbackTotal));
  const calculated = total > 0 ? Math.round((processed / total) * 100) : 0;
  const result = raw.result ?? payload.result ?? {};
  const status = text(raw.status, raw.state, raw.id || raw.job_id ? "queued" : "succeeded")
    .toLowerCase();
  const reportedPercent = percent(
    raw.progress_percent ?? progress.percent ?? progress.percentage,
    calculated,
  );
  return {
    id: text(raw.id, raw.job_id),
    status,
    message: text(raw.message, progress.message),
    processed,
    total,
    percent: TERMINAL_JOB_STATUSES.has(status) ? 100 : reportedPercent,
    updated: Math.max(0, integer(result.updated ?? result.updated_count)),
    failed: Math.max(0, integer(result.failed ?? result.failed_count)),
    undoAvailable: Boolean(result.undo_available ?? payload.undo_available),
  };
}

function folderRows(payload: OrganizeFolderListResponse): OrganizeFolderWire[] {
  return payload.result?.folders ?? payload.folders ?? [];
}

function rootRows(payload: OrganizeFolderListResponse): OrganizeRootWire[] {
  return payload.result?.roots ?? payload.roots ?? [];
}

function folderResponseTotal(payload: OrganizeFolderListResponse, fallback: number): number {
  return Math.max(
    fallback,
    integer(payload.result?.total ?? payload.total, fallback),
  );
}

function folderResponseHasMore(
  payload: OrganizeFolderListResponse,
  loaded: number,
  total: number,
): boolean {
  const reported = payload.result?.has_more ?? payload.has_more;
  return typeof reported === "boolean" ? reported : loaded < total;
}

function imageRows(payload: OrganizeFolderImagesResponse): OrganizeImageWire[] {
  return payload.result?.items ?? payload.result?.images ?? payload.items ?? payload.images ?? [];
}

function responseTotal(payload: OrganizeFolderImagesResponse, fallback: number): number {
  return Math.max(
    fallback,
    integer(
      payload.result?.total_items ??
        payload.result?.total ??
        payload.total_items ??
        payload.total,
      fallback,
    ),
  );
}

function legacyImages(payload: OrganizeBootstrapResponse): OrganizeImage[] {
  const rows = payload.organize?.proposals?.length
    ? payload.organize.proposals
    : payload.organize?.pending_reviews?.length
      ? payload.organize.pending_reviews
      : payload.proposals ?? [];
  return rows.map(normalizeImage);
}

function parentFolder(relativePath: string): string {
  const parts = relativePath.split(/[\\/]/u).filter(Boolean);
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "";
}

function legacyFolderRows(images: OrganizeImage[], query: string): OrganizeFolder[] {
  const groups = new Map<string, OrganizeImage[]>();
  for (const image of images) {
    const folder = parentFolder(image.relativePath);
    const group = groups.get(folder) ?? [];
    group.push(image);
    groups.set(folder, group);
  }
  const term = comparisonKey(query);
  return [...groups.entries()]
    .filter(([folder]) => !term || comparisonKey(folder || "根目录").includes(term))
    .map(([folder, group], index) => ({
      key: `legacy:${encodeURIComponent(folder)}`,
      rootId: "legacy-root",
      rootName: "当前图库",
      relativeFolder: folder,
      name: folder.split("/").at(-1) || "根目录",
      imageCount: group.length,
      directImageCount: group.length,
      descendantImageCount: 0,
      firstIndexedAt: "",
      lastIndexedAt: "",
      timestampSource: "legacy",
      manualTaggedCount: group.filter((item) => item.manualTags.length).length,
      modelTaggedCount: group.filter((item) => item.modelTags.length).length,
      failedCount: group.filter((item) => item.annotationStatus === "failed").length,
      index,
    }))
    .map(({ index: _index, ...folder }) => folder);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorStatus(error: unknown): number {
  return typeof error === "object" && error !== null && "status" in error
    ? integer((error as { status?: unknown }).status)
    : 0;
}

export function useOrganize(api: OrganizeApi = organizeApi, events: OrganizeEvents = {}) {
  const libraries = shallowRef<OrganizeBootstrapResponse["libraries"]>([]);
  const aliases = shallowRef<OrganizeAlias[]>([]);
  const selectedLibraryId = ref("");
  const folders = shallowRef<OrganizeFolder[]>([]);
  const serverRoots = shallowRef<FolderRootGroup[]>([]);
  const folderTotal = ref(0);
  const folderHasMore = ref(false);
  const folderQuery = ref("");
  const selectedRootId = ref("");
  const selectedFolderKey = ref("");
  const images = shallowRef<OrganizeImage[]>([]);
  const knownImages = shallowRef<OrganizeImage[]>([]);
  const imagePage = ref(1);
  const imagePageSize = ref(ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE);
  const imageTotal = ref(0);
  const folderLoading = ref(false);
  const folderLoadingMore = ref(false);
  const imageLoading = ref(false);
  const selectedIds = ref<Set<string>>(new Set());
  const wholeFolderSelected = ref(false);
  const excludedIds = ref<Set<string>>(new Set());
  const selectionAnchorId = ref("");
  const tagInput = ref("");
  const draftTags = shallowRef<string[]>([]);
  const busyAction = ref("");
  const finalizingJob = ref(false);
  const undoAvailable = ref(false);
  const activeJob = shallowRef<OrganizeJob | null>(null);
  const activeJobKind = ref<
    "" | "mutation" | "undo" | "alias" | "delete" | "folder-tags"
  >("");
  const deletePreview = shallowRef<FolderDeletePreview | null>(null);
  const deletePreviewLoading = ref(false);
  const deleteCommitLoading = ref(false);
  const folderNameTagPreview = shallowRef<FolderNameTagPreview | null>(null);
  const folderNameTagPreviewSelection =
    shallowRef<FolderNameTagSelection | null>(null);
  const folderNameTagPreviewOptions =
    shallowRef<FolderNameTagRunOptions | null>(null);
  const folderNameTagPreviewLoading = ref(false);
  const lastError = ref("");
  const legacy = shallowRef<OrganizeImage[]>([]);

  let bootstrapController: AbortController | null = null;
  let folderController: AbortController | null = null;
  let imageController: AbortController | null = null;
  let jobController: AbortController | null = null;
  let deleteController: AbortController | null = null;
  let folderNameTagController: AbortController | null = null;
  let folderRequestSequence = 0;
  let imageRequestSequence = 0;
  let pollTimer: number | null = null;
  let jobPollFailures = 0;
  let pendingAlias: OrganizeAlias | null = null;

  const rootGroups = computed<FolderRootGroup[]>(() => {
    if (serverRoots.value.length) return serverRoots.value;
    const groups = new Map<string, FolderRootGroup>();
    for (const folder of folders.value) {
      const current = groups.get(folder.rootId) ?? {
        id: folder.rootId,
        name: folder.rootName,
        folderKey: folder.relativeFolder ? "" : folder.key,
        folderCount: 0,
        imageCount: 0,
        firstIndexedAt: folder.firstIndexedAt,
        lastIndexedAt: folder.lastIndexedAt,
      };
      current.folderCount += 1;
      current.imageCount += folder.directImageCount;
      if (!current.folderKey && !folder.relativeFolder) current.folderKey = folder.key;
      if (
        folder.firstIndexedAt &&
        (!current.firstIndexedAt || folder.firstIndexedAt < current.firstIndexedAt)
      ) {
        current.firstIndexedAt = folder.firstIndexedAt;
      }
      if (folder.lastIndexedAt > current.lastIndexedAt) {
        current.lastIndexedAt = folder.lastIndexedAt;
      }
      groups.set(folder.rootId, current);
    }
    return [...groups.values()];
  });
  const visibleFolders = computed(() => {
    const term = comparisonKey(folderQuery.value);
    return folders.value.filter((folder) => {
      if (selectedRootId.value && folder.rootId !== selectedRootId.value) return false;
      return !term || comparisonKey(`${folder.name} ${folder.relativeFolder}`).includes(term);
    });
  });
  const selectedFolder = computed<OrganizeFolder | null>(() => {
    const folder = folders.value.find((item) => item.key === selectedFolderKey.value);
    if (folder) return folder;

    // Roots are intentionally not returned as ordinary folder rows by the
    // backend. Their opaque folder_key represents relative_folder="", so it
    // is still a valid direct-only selection when include_subfolders=false.
    const root = rootGroups.value.find(
      (item) => item.folderKey && item.folderKey === selectedFolderKey.value,
    );
    if (!root) return null;
    return {
      key: root.folderKey,
      rootId: root.id,
      rootName: root.name,
      relativeFolder: "根目录",
      name: root.name,
      imageCount: imageTotal.value,
      directImageCount: imageTotal.value,
      descendantImageCount: Math.max(0, root.imageCount - imageTotal.value),
      firstIndexedAt: root.firstIndexedAt,
      lastIndexedAt: root.lastIndexedAt,
      timestampSource: "root",
      manualTaggedCount: 0,
      modelTaggedCount: 0,
      failedCount: 0,
    };
  });
  const imageTotalPages = computed(() =>
    imageTotal.value > 0 ? Math.ceil(imageTotal.value / imagePageSize.value) : 0,
  );
  const jobRunning = computed(
    () =>
      finalizingJob.value ||
      Boolean(activeJob.value && !TERMINAL_JOB_STATUSES.has(activeJob.value.status)),
  );
  const selectedCount = computed(() =>
    wholeFolderSelected.value
      ? Math.max(0, imageTotal.value - excludedIds.value.size)
      : selectedIds.value.size,
  );
  const selectedKnownImages = computed(() =>
    knownImages.value.filter((image) => isImageSelected(image.id)),
  );
  const tagPresence = computed(() => {
    const selected = selectedKnownImages.value;
    const counts = new Map<string, { tag: string; count: number; sources: Set<string> }>();
    for (const image of selected) {
      const imageKeys = new Set<string>();
      for (const tag of image.effectiveTags) {
        const key = comparisonKey(tag);
        if (!key || imageKeys.has(key)) continue;
        imageKeys.add(key);
        const current = counts.get(key) ?? { tag, count: 0, sources: new Set<string>() };
        current.count += 1;
        for (const detail of image.tagSources) {
          if (comparisonKey(detail.tag) === key) current.sources.add(detail.sourceLabel);
        }
        counts.set(key, current);
      }
    }
    const values: TagPresence[] = [...counts.values()].map((item) => ({
      tag: item.tag,
      count: item.count,
      total: selected.length,
      sourceLabels: [...item.sources],
    }));
    values.sort((left, right) => left.tag.localeCompare(right.tag, "zh-CN"));
    return values;
  });
  const commonTags = computed(() =>
    tagPresence.value.filter((item) => item.total > 0 && item.count === item.total),
  );
  const partialTags = computed(() =>
    tagPresence.value.filter((item) => item.count > 0 && item.count < item.total),
  );
  const selectionPreviewIncomplete = computed(
    () => selectedKnownImages.value.length < selectedCount.value,
  );
  const selectionDescription = computed(() => {
    if (!selectedCount.value) return "尚未选择图片";
    if (wholeFolderSelected.value) {
      return excludedIds.value.size
        ? `已选整文件夹，排除 ${excludedIds.value.size} 张`
        : "已选整文件夹全部图片";
    }
    return `已选 ${selectedCount.value} 张图片`;
  });

  function notify(title: string, message: string, kind: ToastKind): void {
    events.onToast?.(title, message, kind);
  }

  function clearPollTimer(): void {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function clearSelection(): void {
    selectedIds.value = new Set();
    excludedIds.value = new Set();
    wholeFolderSelected.value = false;
    selectionAnchorId.value = "";
  }

  function mergeKnown(next: OrganizeImage[]): void {
    const merged = new Map(knownImages.value.map((image) => [image.id, image]));
    for (const image of next) merged.set(image.id, image);
    knownImages.value = [...merged.values()];
  }

  function mergeAlias(entry: OrganizeAlias): void {
    const key = comparisonKey(entry.canonical);
    const next = aliases.value.filter((item) => comparisonKey(item.canonical) !== key);
    next.push(entry);
    next.sort((left, right) => left.canonical.localeCompare(right.canonical, "zh-CN"));
    aliases.value = next;
  }

  async function load(): Promise<boolean> {
    bootstrapController?.abort();
    bootstrapController = new AbortController();
    folderLoading.value = true;
    try {
      const payload = await api.bootstrap(bootstrapController.signal);
      libraries.value = payload.libraries ?? [];
      aliases.value = (payload.organize?.aliases ?? payload.aliases ?? [])
        .map(normalizeAlias)
        .filter((item): item is OrganizeAlias => item !== null)
        .sort((left, right) => left.canonical.localeCompare(right.canonical, "zh-CN"));
      legacy.value = legacyImages(payload);
      undoAvailable.value = Boolean(payload.organize?.undo_available ?? payload.undo_available);
      if (!libraries.value.some((item) => item.id === selectedLibraryId.value)) {
        selectedLibraryId.value =
          libraries.value.find((item) => item.is_default)?.id ?? libraries.value[0]?.id ?? "";
      }
      lastError.value = "";
      return await loadFolders();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      lastError.value = errorMessage(error);
      notify("无法读取批量标签数据", lastError.value, "error");
      return false;
    } finally {
      folderLoading.value = false;
    }
  }

  async function selectLibrary(libraryId: string): Promise<boolean> {
    cancelFolderNameTags();
    selectedLibraryId.value = libraryId;
    selectedRootId.value = "";
    selectedFolderKey.value = "";
    folderQuery.value = "";
    clearSelection();
    return loadFolders();
  }

  function mergeFolders(next: OrganizeFolder[]): void {
    const merged = new Map(folders.value.map((folder) => [folder.key, folder]));
    for (const folder of next) merged.set(folder.key, folder);
    folders.value = [...merged.values()];
  }

  async function loadFolderPage(offset: number, append: boolean): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    if (!libraryId) {
      folders.value = [];
      serverRoots.value = [];
      folderHasMore.value = false;
      images.value = [];
      return false;
    }
    folderController?.abort();
    const controller = new AbortController();
    folderController = controller;
    const requestSequence = ++folderRequestSequence;
    if (append) folderLoadingMore.value = true;
    else folderLoading.value = true;
    try {
      const payload = await api.listFolders(
        libraryId,
        folderQuery.value.trim(),
        offset,
        FOLDER_PAGE_SIZE,
        controller.signal,
      );
      const rows = folderRows(payload).map(normalizeFolder);
      if (requestSequence !== folderRequestSequence) return false;
      if (append) mergeFolders(rows);
      else folders.value = rows;
      const roots = rootRows(payload).map(normalizeRoot);
      if (roots.length) serverRoots.value = roots;
      else if (!append) serverRoots.value = [];
      folderTotal.value = folderResponseTotal(payload, folders.value.length);
      folderHasMore.value = folderResponseHasMore(
        payload,
        folders.value.length,
        folderTotal.value,
      );
      lastError.value = "";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      if (!append && legacy.value.length && [0, 404, 405, 501].includes(errorStatus(error))) {
        folders.value = legacyFolderRows(legacy.value, folderQuery.value);
        serverRoots.value = [];
        folderTotal.value = folders.value.length;
        folderHasMore.value = false;
        return true;
      } else {
        lastError.value = errorMessage(error);
        notify(append ? "无法继续加载文件夹" : "无法读取文件夹", lastError.value, "error");
        return false;
      }
    } finally {
      if (requestSequence === folderRequestSequence) {
        folderLoading.value = false;
        folderLoadingMore.value = false;
      }
    }
  }

  async function loadFolders(): Promise<boolean> {
    folders.value = [];
    serverRoots.value = [];
    folderTotal.value = 0;
    folderHasMore.value = false;
    const loaded = await loadFolderPage(0, false);
    if (!loaded) return false;
    if (!rootGroups.value.some((root) => root.id === selectedRootId.value)) {
      selectedRootId.value = rootGroups.value[0]?.id ?? "";
    }
    const candidates = folders.value.filter(
      (folder) => !selectedRootId.value || folder.rootId === selectedRootId.value,
    );
    const selectedRoot = rootGroups.value.find((root) => root.id === selectedRootId.value);
    const selectedKeyStillExists =
      candidates.some((folder) => folder.key === selectedFolderKey.value) ||
      rootGroups.value.some(
        (root) => root.folderKey && root.folderKey === selectedFolderKey.value,
      );
    if (!selectedKeyStillExists) {
      // A normal, unfiltered library view starts at the root itself. This is
      // important when images and subfolders coexist: root-level images must
      // not disappear merely because a child folder happens to sort first.
      selectedFolderKey.value = folderQuery.value.trim()
        ? (candidates[0]?.key ?? "")
        : (selectedRoot?.folderKey || candidates[0]?.key || "");
      clearSelection();
      knownImages.value = [];
    }
    if (!selectedFolderKey.value) {
      images.value = [];
      imageTotal.value = 0;
      return true;
    }
    return loadFolderImages(1);
  }

  async function loadMoreFolders(): Promise<boolean> {
    if (folderLoading.value || folderLoadingMore.value || !folderHasMore.value) return false;
    return loadFolderPage(folders.value.length, true);
  }

  async function searchFolders(): Promise<boolean> {
    selectedFolderKey.value = "";
    clearSelection();
    return loadFolders();
  }

  async function selectRoot(rootId: string): Promise<boolean> {
    selectedRootId.value = rootId;
    const root = rootGroups.value.find((item) => item.id === rootId);
    if (root?.folderKey) return selectFolder(root.folderKey);

    // Compatibility fallback for older backends that did not return root
    // folder keys. Those versions can only browse actionable child folders.
    let folder = folders.value.find((item) => item.rootId === rootId);
    while (!folder && folderHasMore.value) {
      const loaded = await loadMoreFolders();
      if (!loaded) break;
      folder = folders.value.find((item) => item.rootId === rootId);
    }
    return folder ? selectFolder(folder.key) : false;
  }

  async function selectFolder(folderKey: string): Promise<boolean> {
    if (!folderKey) return false;
    selectedFolderKey.value = folderKey;
    imagePage.value = 1;
    knownImages.value = [];
    clearSelection();
    return loadFolderImages(1);
  }

  async function setImagePageSize(pageSize: number, reload = true): Promise<boolean> {
    const nextSize = Math.min(
      ORGANIZE_GALLERY_MAX_PAGE_SIZE,
      Math.max(1, integer(pageSize, ORGANIZE_GALLERY_DEFAULT_PAGE_SIZE)),
    );
    const previousSize = imagePageSize.value;
    if (nextSize === previousSize) return false;
    const firstVisibleIndex = Math.max(0, (imagePage.value - 1) * previousSize);
    imagePageSize.value = nextSize;
    const nextPage = Math.floor(firstVisibleIndex / nextSize) + 1;
    imagePage.value = nextPage;
    if (!reload || !selectedLibraryId.value || !selectedFolderKey.value) return true;
    return loadFolderImages(nextPage);
  }

  async function loadFolderImages(page = imagePage.value): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    const folderKey = selectedFolderKey.value;
    if (!libraryId || !folderKey) return false;
    imageController?.abort();
    const controller = new AbortController();
    imageController = controller;
    const requestSequence = ++imageRequestSequence;
    imageLoading.value = true;
    try {
      if (folderKey.startsWith("legacy:")) {
        const folder = decodeURIComponent(folderKey.slice("legacy:".length));
        const rows = legacy.value.filter((image) => parentFolder(image.relativePath) === folder);
        const start = (page - 1) * imagePageSize.value;
        images.value = rows.slice(start, start + imagePageSize.value);
        imageTotal.value = rows.length;
      } else {
        const payload = await api.listFolderImages(
          libraryId,
          folderKey,
          page,
          imagePageSize.value,
          false,
          controller.signal,
        );
        const rows = imageRows(payload).map(normalizeImage);
        if (requestSequence !== imageRequestSequence) return false;
        images.value = rows;
        imageTotal.value = responseTotal(payload, rows.length);
      }
      imagePage.value = page;
      mergeKnown(images.value);
      lastError.value = "";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      lastError.value = errorMessage(error);
      notify("无法读取文件夹图片", lastError.value, "error");
      return false;
    } finally {
      if (requestSequence === imageRequestSequence) imageLoading.value = false;
    }
  }

  function isImageSelected(imageId: string): boolean {
    return wholeFolderSelected.value
      ? !excludedIds.value.has(imageId)
      : selectedIds.value.has(imageId);
  }

  function selectImage(
    imageId: string,
    options: { toggle?: boolean; range?: boolean } = {},
  ): void {
    const pageIds = images.value.map((image) => image.id);
    if (options.range && selectionAnchorId.value) {
      const anchorIndex = pageIds.indexOf(selectionAnchorId.value);
      const targetIndex = pageIds.indexOf(imageId);
      if (anchorIndex >= 0 && targetIndex >= 0) {
        const [start, end] = [anchorIndex, targetIndex].sort((left, right) => left - right);
        const range = pageIds.slice(start, end + 1);
        if (wholeFolderSelected.value) {
          const next = new Set(excludedIds.value);
          for (const id of range) next.delete(id);
          excludedIds.value = next;
        } else {
          const next = options.toggle ? new Set(selectedIds.value) : new Set<string>();
          for (const id of range) next.add(id);
          selectedIds.value = next;
        }
        return;
      }
    }
    if (options.toggle) {
      if (wholeFolderSelected.value) {
        const next = new Set(excludedIds.value);
        if (next.has(imageId)) next.delete(imageId);
        else next.add(imageId);
        excludedIds.value = next;
      } else {
        const next = new Set(selectedIds.value);
        if (next.has(imageId)) next.delete(imageId);
        else next.add(imageId);
        selectedIds.value = next;
      }
    } else {
      wholeFolderSelected.value = false;
      excludedIds.value = new Set();
      selectedIds.value = new Set([imageId]);
    }
    selectionAnchorId.value = imageId;
  }

  function selectCurrentPage(): void {
    if (wholeFolderSelected.value) {
      const next = new Set(excludedIds.value);
      for (const image of images.value) next.delete(image.id);
      excludedIds.value = next;
    } else {
      selectedIds.value = new Set([
        ...selectedIds.value,
        ...images.value.map((image) => image.id),
      ]);
    }
    selectionAnchorId.value = images.value.at(-1)?.id ?? selectionAnchorId.value;
  }

  function selectWholeFolder(): void {
    if (!selectedFolderKey.value || imageTotal.value < 1) return;
    wholeFolderSelected.value = true;
    selectedIds.value = new Set();
    excludedIds.value = new Set();
    selectionAnchorId.value = "";
  }

  function invertCurrentPage(): void {
    if (wholeFolderSelected.value) {
      const next = new Set(excludedIds.value);
      for (const image of images.value) {
        if (next.has(image.id)) next.delete(image.id);
        else next.add(image.id);
      }
      excludedIds.value = next;
    } else {
      const next = new Set(selectedIds.value);
      for (const image of images.value) {
        if (next.has(image.id)) next.delete(image.id);
        else next.add(image.id);
      }
      selectedIds.value = next;
    }
  }

  function commitTagInput(): void {
    const parsed = parseTagInput(tagInput.value);
    if (parsed.length) draftTags.value = uniqueTags([...draftTags.value, ...parsed]);
    tagInput.value = "";
  }

  function removeDraftTag(tag: string): void {
    const key = comparisonKey(tag);
    draftTags.value = draftTags.value.filter((item) => comparisonKey(item) !== key);
  }

  function selectionPayload(): OrganizeSelection | null {
    if (!selectedCount.value || !selectedFolderKey.value) return null;
    if (wholeFolderSelected.value) {
      return {
        mode: "folder",
        folder_key: selectedFolderKey.value,
        include_subfolders: false,
        excluded_doc_ids: [...excludedIds.value],
      };
    }
    return { mode: "selected", doc_ids: [...selectedIds.value] };
  }

  async function refreshAfterMutation(): Promise<void> {
    clearSelection();
    // loadFolders owns the follow-up image request. Calling both methods here
    // made their AbortControllers cancel each other and left the gallery stale.
    await loadFolders();
  }

  function scheduleJobPoll(): void {
    clearPollTimer();
    if (!activeJob.value?.id || TERMINAL_JOB_STATUSES.has(activeJob.value.status)) return;
    pollTimer = window.setTimeout(() => void pollActiveJob(), events.pollIntervalMs ?? 900);
  }

  async function finishJob(job: OrganizeJob): Promise<void> {
    const kind = activeJobKind.value;
    finalizingJob.value = true;
    activeJob.value = job;
    clearPollTimer();
    try {
      if (SUCCESS_JOB_STATUSES.has(job.status)) {
        if (kind === "alias") {
          if (pendingAlias) mergeAlias(pendingAlias);
          notify(
            "别名词典已保存",
            job.message || `已更新“${pendingAlias?.canonical ?? "别名组"}”。`,
            "success",
          );
        } else if (kind === "delete") {
          deletePreview.value = null;
          notify(
            "文件夹清理已完成",
            job.message || `已处理 ${job.updated || job.processed} 张图片，失败 ${job.failed} 张。`,
            job.failed ? "info" : "success",
          );
          await refreshAfterMutation();
        } else if (kind === "folder-tags") {
          folderNameTagPreview.value = null;
          notify(
            "文件夹名称标签已更新",
            job.message ||
              `已更新 ${job.updated || job.processed} 张图片，失败 ${job.failed} 张。`,
            job.failed ? "info" : "success",
          );
          await refreshAfterMutation();
        } else {
          undoAvailable.value = kind === "undo" ? job.undoAvailable : true;
          notify(
            kind === "undo" ? "批量操作已撤销" : "批量标签已完成",
            job.message || `已更新 ${job.updated || job.processed} 张，失败 ${job.failed} 张。`,
            job.failed ? "info" : "success",
          );
          await refreshAfterMutation();
        }
      } else if (["failed", "cancelled"].includes(job.status)) {
        notify(
          kind === "alias"
            ? "别名词典未保存"
            : kind === "delete"
              ? "文件夹清理未完成"
              : kind === "folder-tags"
                ? "文件夹名称标签任务未完成"
              : "批量标签任务未完成",
          job.message || "请查看任务页中的错误详情。",
          "error",
        );
      }
    } finally {
      if (kind === "alias") pendingAlias = null;
      activeJobKind.value = "";
      finalizingJob.value = false;
    }
  }

  async function pollActiveJob(): Promise<void> {
    const jobId = activeJob.value?.id;
    if (!jobId) return;
    jobController?.abort();
    jobController = new AbortController();
    try {
      const payload = await api.job(jobId, jobController.signal);
      const job = normalizeOrganizeJob(payload, activeJob.value?.total ?? selectedCount.value);
      activeJob.value = job;
      jobPollFailures = 0;
      if (TERMINAL_JOB_STATUSES.has(job.status)) await finishJob(job);
      else scheduleJobPoll();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      lastError.value = errorMessage(error);
      jobPollFailures += 1;
      if (jobPollFailures >= MAX_JOB_POLL_FAILURES && activeJob.value) {
        await finishJob({
          ...activeJob.value,
          status: "failed",
          message: `无法读取后台任务进度：${lastError.value}`,
        });
      } else {
        scheduleJobPoll();
      }
    }
  }

  async function submitTrackedJob(
    kind: "mutation" | "undo" | "alias" | "folder-tags",
    body: Record<string, unknown>,
    fallbackTotal = selectedCount.value,
  ): Promise<boolean> {
    if (busyAction.value || jobRunning.value) {
      notify("已有后台任务", "请等待当前批量任务结束后再提交下一项操作。", "info");
      return false;
    }
    busyAction.value = kind;
    try {
      const payload = await api.submitJob(body);
      const job = normalizeOrganizeJob(payload, fallbackTotal);
      activeJob.value = job;
      activeJobKind.value = kind;
      jobPollFailures = 0;
      lastError.value = "";
      if (!job.id || TERMINAL_JOB_STATUSES.has(job.status)) await finishJob(job);
      else {
        notify(
          kind === "folder-tags" ? "文件夹名称标签任务已提交" : "批量标签任务已提交",
          "任务会在后台执行，可继续浏览图片。",
          "success",
        );
        scheduleJobPoll();
      }
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify(
        kind === "folder-tags"
          ? "无法提交文件夹名称标签任务"
          : "无法提交批量标签任务",
        lastError.value,
        "error",
      );
      return false;
    } finally {
      busyAction.value = "";
    }
  }

  async function previewFolderNameTags(
    selection: FolderNameTagSelection,
    options: FolderNameTagRunOptions,
  ): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    if (!libraryId) return false;
    if (!api.previewFolderNameTags) {
      notify(
        "暂时无法生成文件夹名称标签",
        "当前后端版本尚未提供文件夹名称标签预览。",
        "error",
      );
      return false;
    }
    if (busyAction.value || jobRunning.value) {
      notify(
        "已有后台任务",
        "请等待当前任务结束后再生成预览。",
        "info",
      );
      return false;
    }
    folderNameTagController?.abort();
    const controller = new AbortController();
    folderNameTagController = controller;
    folderNameTagPreviewLoading.value = true;
    try {
      const payload = await api.previewFolderNameTags(
        libraryId,
        selection,
        options,
        controller.signal,
      );
      folderNameTagPreview.value = normalizeFolderNameTagPreview(payload);
      folderNameTagPreviewSelection.value = selection;
      folderNameTagPreviewOptions.value = options;
      lastError.value = "";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      lastError.value = errorMessage(error);
      notify("无法生成文件夹名称标签预览", lastError.value, "error");
      return false;
    } finally {
      if (folderNameTagController === controller) {
        folderNameTagPreviewLoading.value = false;
      }
    }
  }

  function cancelFolderNameTags(): void {
    folderNameTagController?.abort();
    folderNameTagPreview.value = null;
    folderNameTagPreviewSelection.value = null;
    folderNameTagPreviewOptions.value = null;
    folderNameTagPreviewLoading.value = false;
  }

  async function applyFolderNameTags(): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    const preview = folderNameTagPreview.value;
    const selection = folderNameTagPreviewSelection.value;
    const options = folderNameTagPreviewOptions.value;
    if (
      !libraryId
      || !preview
      || !selection
      || !options
      || (preview.changed < 1 && options.mode !== "mark_all")
    ) return false;
    const submitted = await submitTrackedJob(
      "folder-tags",
      {
        task_type: "folder_name_tag_apply",
        library_id: libraryId,
        selection,
        ...options,
        expected_rule_revision: preview.ruleRevision,
      },
      preview.selected,
    );
    if (submitted) {
      folderNameTagPreview.value = null;
      folderNameTagPreviewSelection.value = null;
      folderNameTagPreviewOptions.value = null;
    }
    return submitted;
  }

  async function previewFolderDeletion(folderKey: string): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    if (!libraryId || !folderKey) return false;
    if (!api.previewFolderDelete) {
      notify("暂时无法清理文件夹", "当前后端版本尚未提供安全删除预览。", "error");
      return false;
    }
    if (busyAction.value || jobRunning.value) {
      notify("已有后台任务", "请等待当前任务结束后再清理文件夹。", "info");
      return false;
    }
    deleteController?.abort();
    const controller = new AbortController();
    deleteController = controller;
    deletePreviewLoading.value = true;
    try {
      const payload = await api.previewFolderDelete(
        libraryId,
        folderKey,
        true,
        controller.signal,
      );
      const preview = normalizeDeletePreview(payload);
      if (!preview) throw new Error("后端没有返回可确认的文件夹清理预览。");
      deletePreview.value = preview;
      lastError.value = "";
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      lastError.value = errorMessage(error);
      notify("无法生成清理预览", lastError.value, "error");
      return false;
    } finally {
      if (deleteController === controller) deletePreviewLoading.value = false;
    }
  }

  function cancelFolderDeletion(): void {
    deleteController?.abort();
    deletePreview.value = null;
    deletePreviewLoading.value = false;
    deleteCommitLoading.value = false;
  }

  async function confirmFolderDeletion(confirmation: string): Promise<boolean> {
    const libraryId = selectedLibraryId.value;
    const preview = deletePreview.value;
    if (!libraryId || !preview || !api.commitFolderDelete) return false;
    if (preview.blocked) {
      notify("该文件夹不能清理", "请先处理预览中列出的阻止原因。", "error");
      return false;
    }
    if (confirmation.trim() !== preview.confirmationPhrase) {
      notify("确认文字不匹配", `请输入“${preview.confirmationPhrase}”后再继续。`, "error");
      return false;
    }
    if (busyAction.value || jobRunning.value) return false;
    busyAction.value = "delete";
    deleteCommitLoading.value = true;
    try {
      const payload = await api.commitFolderDelete(libraryId, {
        operation_id: preview.operationId,
        confirmation_token: preview.confirmationToken,
        confirm: true,
      });
      const job = normalizeOrganizeJob(payload, preview.imageCount);
      activeJob.value = job;
      activeJobKind.value = "delete";
      jobPollFailures = 0;
      lastError.value = "";
      if (!job.id || TERMINAL_JOB_STATUSES.has(job.status)) await finishJob(job);
      else {
        deletePreview.value = null;
        notify("文件夹清理已提交", "任务会在后台安全执行，可继续浏览其他页面。", "success");
        scheduleJobPoll();
      }
      return true;
    } catch (error) {
      lastError.value = errorMessage(error);
      notify("无法提交文件夹清理", lastError.value, "error");
      return false;
    } finally {
      busyAction.value = "";
      deleteCommitLoading.value = false;
    }
  }

  async function applyManualTags(operation: ManualTagOperation): Promise<boolean> {
    commitTagInput();
    const selection = selectionPayload();
    if (!selection) {
      notify("尚未选择图片", "请先选择当前页图片或整个文件夹。", "error");
      return false;
    }
    if (!draftTags.value.length) {
      notify("尚未输入标签", "按 Enter，或使用逗号、顿号、分号分隔多个标签。", "error");
      return false;
    }
    const libraryId = selectedLibraryId.value;
    if (!libraryId) return false;
    return submitTrackedJob("mutation", {
      task_type: "manual_tag_batch",
      library_id: libraryId,
      selection,
      operation,
      tags: [...draftTags.value],
    });
  }

  async function undoLastBatch(): Promise<boolean> {
    if (!selectedLibraryId.value || !undoAvailable.value) return false;
    return submitTrackedJob("undo", {
      task_type: "manual_tag_undo",
      library_id: selectedLibraryId.value,
    });
  }

  async function upsertAlias(canonicalName: string, aliasValues: string[]): Promise<boolean> {
    if (!selectedLibraryId.value) {
      notify("尚未选择图库", "请先选择一个已启用的图库。", "error");
      return false;
    }
    const canonical = text(canonicalName);
    if (!canonical) {
      notify("规范名称不能为空", "请输入角色、作品或人物的规范名称。", "error");
      return false;
    }
    const canonicalKey = comparisonKey(canonical);
    const normalizedAliases = uniqueTags(aliasValues).filter(
      (alias) => comparisonKey(alias) !== canonicalKey,
    );
    pendingAlias = { canonical, aliases: normalizedAliases };
    const submitted = await submitTrackedJob("alias", {
      task_type: "tag_alias_upsert",
      library_id: selectedLibraryId.value,
      canonical_name: canonical,
      aliases: normalizedAliases,
    });
    if (!submitted) pendingAlias = null;
    return submitted;
  }

  onMounted(() => {
    if (events.autoLoad !== false) void load();
  });
  onBeforeUnmount(() => {
    clearPollTimer();
    bootstrapController?.abort();
    folderController?.abort();
    imageController?.abort();
    jobController?.abort();
    deleteController?.abort();
    folderNameTagController?.abort();
  });

  return {
    libraries,
    aliases,
    selectedLibraryId,
    folders,
    folderTotal,
    folderHasMore,
    folderLoadingMore,
    folderQuery,
    selectedRootId,
    selectedFolderKey,
    selectedFolder,
    rootGroups,
    visibleFolders,
    images,
    imagePage,
    imagePageSize,
    imageTotal,
    imageTotalPages,
    folderLoading,
    imageLoading,
    selectedIds,
    wholeFolderSelected,
    excludedIds,
    selectedCount,
    selectedKnownImages,
    selectionDescription,
    selectionPreviewIncomplete,
    commonTags,
    partialTags,
    tagInput,
    draftTags,
    busyAction,
    jobRunning,
    undoAvailable,
    activeJob,
    activeJobKind,
    deletePreview,
    deletePreviewLoading,
    deleteCommitLoading,
    folderNameTagPreview,
    folderNameTagPreviewLoading,
    lastError,
    load,
    selectLibrary,
    loadFolders,
    loadMoreFolders,
    searchFolders,
    selectRoot,
    selectFolder,
    loadFolderImages,
    setImagePageSize,
    isImageSelected,
    selectImage,
    selectCurrentPage,
    selectWholeFolder,
    invertCurrentPage,
    clearSelection,
    commitTagInput,
    removeDraftTag,
    applyManualTags,
    undoLastBatch,
    upsertAlias,
    previewFolderNameTags,
    cancelFolderNameTags,
    applyFolderNameTags,
    previewFolderDeletion,
    cancelFolderDeletion,
    confirmFolderDeletion,
  };
}
