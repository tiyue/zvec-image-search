export type ClusterScope = "new_or_changed" | "all";
export type ClusterType = "all" | "exact" | "perceptual" | "semantic" | "single";
export type ClusterRunType = Exclude<ClusterType, "all" | "single">;
export type ClusterIdentityCategory =
  | "real_person"
  | "cosplayer"
  | "character"
  | "work";
export type LearningDecisionValue = "accept" | "reject" | "edit" | "skip";
export type LearningCandidateKind =
  | "tag_review"
  | "search_result"
  | "cluster_membership";

export interface IntelligenceImage {
  docId: string;
  nativeImageId: string;
  fileName: string;
  relativePath: string;
  thumbnailUrl: string;
  imageUrl: string;
  imageAvailable: boolean;
}

export interface IdentityAnchorView {
  category: string;
  value: string;
  source: string;
  confidence: number;
  conflict: boolean;
}

export interface SimilarityCluster {
  id: string;
  type: ClusterType;
  memberCount: number;
  edgeKinds: ClusterRunType[];
  representative: IntelligenceImage;
  identityAnchors: IdentityAnchorView[];
}

export interface SimilarityClusterPage {
  offset: number;
  limit: number;
  total: number;
  undoAvailable: boolean;
  clusters: SimilarityCluster[];
}

export interface SimilarityClusterDetail {
  cluster: SimilarityCluster;
  offset: number;
  limit: number;
  total: number;
  members: IntelligenceImage[];
}

export interface ClusterOperationResult {
  batchId: string;
  applied: number;
  failed: number;
  conflicts: number;
  undoAvailable: boolean;
}

export type LearningReason =
  | "identity_conflict"
  | "cluster_outlier"
  | "ranking_disagreement"
  | "combined_uncertainty"
  | "low_information";

export interface LearningSample extends IntelligenceImage {
  rank: number;
  groupId: string;
  queryId: string;
  candidateKind: LearningCandidateKind;
  sourceSha256: string;
  uncertaintyScore: number;
  reasons: LearningReason[];
  suggestedTags: string[];
}

export interface ActiveLearningQueueView {
  queueId: string;
  candidateCount: number;
  undoAvailable: boolean;
  samples: LearningSample[];
}

export interface LearningDecisionDraft {
  doc_id: string;
  decision: LearningDecisionValue;
  labels?: string[];
}

export interface LearningReviewFailure {
  docId: string;
  code: string;
  message: string;
  retryable: boolean;
}

export interface LearningReviewResult {
  batchId: string;
  applied: number;
  failed: number;
  skipped: number;
  conflicts: number;
  undoAvailable: boolean;
  failures: LearningReviewFailure[];
}
