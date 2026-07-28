from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

MatchState = Literal["high", "possible", "weak"]
RankSource = Literal["image", "text", "metadata", "fused", "tag"]
SearchSortMode = Literal["relevance", "confidence", "diverse", "legacy"]
FailureKind = Literal["item", "retryable", "systemic"]
DEFAULT_HIGH_THRESHOLD = 0.75
DEFAULT_POSSIBLE_THRESHOLD = 0.5
RRF_RANK_CONSTANT = 60
MAX_INDEX_FAILURE_DETAILS = 100


def normalize_cosine_distance(raw_score: float) -> float:
    """Map zvec cosine distance [0, 2] to a bounded similarity indicator."""
    if not math.isfinite(raw_score):
        return 0.0
    return min(1.0, max(0.0, 1.0 - raw_score / 2.0))


def normalize_rrf_score(
    raw_score: float, rank_constant: int = RRF_RANK_CONSTANT
) -> float:
    """Map weighted RRF's theoretical maximum 1/(k+1) to 1.0."""
    if not math.isfinite(raw_score) or rank_constant < 0:
        return 0.0
    return min(1.0, max(0.0, raw_score * (rank_constant + 1)))


def classify_match_state(
    normalized_score: float,
    *,
    high: float = DEFAULT_HIGH_THRESHOLD,
    possible: float = DEFAULT_POSSIBLE_THRESHOLD,
) -> MatchState:
    if normalized_score >= high:
        return "high"
    if normalized_score >= possible:
        return "possible"
    return "weak"


@dataclass(frozen=True)
class ImageRecord:
    doc_id: str
    root_id: str
    relative_path: str
    absolute_path: str
    file_name: str
    extension: str
    mime_type: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    width: int
    height: int

    def state_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values.pop("absolute_path")
        return values


@dataclass
class FileFailure:
    path: str
    error: str
    kind: FailureKind = "item"
    stage: str = ""
    sha256: str = ""
    quarantined_path: str = ""
    copy_error: str = ""


@dataclass
class ScanResult:
    scanned: int = 0
    supported: int = 0
    skipped: int = 0
    peak_in_flight: int = 0
    complete: bool = True
    seen_supported_ids: set[str] = field(default_factory=set)
    fast_unchanged_ids: set[str] = field(default_factory=set)
    records: list[ImageRecord] = field(default_factory=list)
    failures: list[FileFailure] = field(default_factory=list)
    failure_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def add_failure(self, failure: FileFailure) -> None:
        self.failure_count += 1
        if len(self.failures) < MAX_INDEX_FAILURE_DETAILS:
            self.failures.append(failure)


@dataclass
class IndexReport:
    root_path: str
    collection: str = ""
    index_run_id: str = ""
    scanned: int = 0
    supported: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    scan_peak_in_flight: int = 0
    embedding_peak_in_flight: int = 0
    embedding_peak_bytes: int = 0
    deleted: int = 0
    would_delete: int = 0
    sync_aborted: bool = False
    failed: int = 0
    deferred: int = 0
    needs_attention: bool = False
    api_requests: int = 0
    usage: list[dict[str, Any]] = field(default_factory=list)
    failures: list[FileFailure] = field(default_factory=list)
    failure_counts: dict[str, int] = field(default_factory=dict)
    failure_manifest: str = ""
    quarantined: int = 0
    quarantine_copy_failures: int = 0
    warnings: list[str] = field(default_factory=list)

    def add_failure(self, failure: FileFailure) -> None:
        self.failed += 1
        self.failure_counts[failure.kind] = self.failure_counts.get(failure.kind, 0) + 1
        if failure.kind == "systemic":
            self.needs_attention = True
        if failure.copy_error:
            self.quarantine_copy_failures += 1
        if len(self.failures) < MAX_INDEX_FAILURE_DETAILS:
            self.failures.append(failure)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    distance: float
    fields: dict[str, Any]
    rank: int = 0
    fused_score: float | None = None
    raw_score: float | None = None
    normalized_score: float | None = None
    confidence: float | None = None
    ranking_confidence: float | None = None
    match_state: MatchState | None = None
    rank_source: RankSource | None = None
    image_confidence: float | None = None
    text_confidence: float | None = None
    metadata_confidence: float | None = None
    image_rank: int | None = None
    text_rank: int | None = None
    metadata_rank: int | None = None
    rank_agreement: float | None = None
    matched_tags: tuple[str, ...] = ()
    ranking_model_version: str | None = None
    ranking_score: float | None = None
    feature_schema_version: int | None = None
    ranking_fallback: bool = False
    ranking_fallback_reason: str | None = None
    calibrated_minimum_confidence: float | None = None
    calibration_version: str | None = None
    calibration_scope: str | None = None
    calibration_fallback: bool = False
    search_features: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.fused_score is not None:
            raw_score = float(self.fused_score)
            normalized_score = (
                normalize_rrf_score(raw_score)
                if self.normalized_score is None
                else min(1.0, max(0.0, float(self.normalized_score)))
            )
        else:
            raw_score = (
                float(self.distance)
                if self.raw_score is None
                else float(self.raw_score)
            )
            normalized_score = (
                normalize_cosine_distance(raw_score)
                if self.normalized_score is None
                else min(1.0, max(0.0, float(self.normalized_score)))
            )
        confidence = (
            normalized_score
            if self.confidence is None
            else min(1.0, max(0.0, float(self.confidence)))
        )
        object.__setattr__(self, "raw_score", raw_score)
        object.__setattr__(self, "normalized_score", normalized_score)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(
            self,
            "match_state",
            self.match_state or classify_match_state(normalized_score),
        )
        object.__setattr__(
            self,
            "rank_source",
            self.rank_source or ("fused" if self.fused_score is not None else "text"),
        )
        for name in (
            "ranking_confidence",
            "ranking_score",
            "image_confidence",
            "text_confidence",
            "metadata_confidence",
            "rank_agreement",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    min(1.0, max(0.0, float(value))),
                )
        if self.calibrated_minimum_confidence is not None:
            object.__setattr__(
                self,
                "calibrated_minimum_confidence",
                min(1.0, max(0.0, float(self.calibrated_minimum_confidence))),
            )
        if self.feature_schema_version is not None:
            version = int(self.feature_schema_version)
            object.__setattr__(
                self, "feature_schema_version", version if version > 0 else None
            )
        for name in ("image_rank", "text_rank", "metadata_rank"):
            value = getattr(self, name)
            if value is not None:
                normalized_rank = int(value)
                object.__setattr__(
                    self, name, normalized_rank if normalized_rank > 0 else None
                )
        object.__setattr__(
            self,
            "matched_tags",
            tuple(dict.fromkeys(str(tag) for tag in self.matched_tags if str(tag))),
        )


@dataclass(frozen=True)
class PreparedSearch:
    query_type: str
    search_mode: str = "semantic"
    text: str | None = None
    semantic_queries: tuple[str, ...] = ()
    image_path: str | None = None
    image_sha256: str | None = None
    text_vector: list[float] | None = None
    text_vectors: list[list[float]] = field(default_factory=list)
    image_vector: list[float] | None = None
    request_ids: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    embedding_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedSearchHit:
    hit: SearchHit
    source_path: str


@dataclass(frozen=True)
class PreparedSearchCandidates:
    query_type: str
    hits: list[ResolvedSearchHit] = field(default_factory=list)
    image_hits: list[ResolvedSearchHit] = field(default_factory=list)
    text_hits: list[ResolvedSearchHit] = field(default_factory=list)
    metadata_hits: list[ResolvedSearchHit] = field(default_factory=list)
    tag_hits: list[ResolvedSearchHit] = field(default_factory=list)
    candidate_k: int = 0
    collection_size: int = 0
    next_candidate_k: int | None = None
    quality_configured: bool = False
    minimum_confidence: float = 0.0
    minimum_score: float | None = None
    possible_confidence: float = DEFAULT_POSSIBLE_THRESHOLD
    high_confidence: float = DEFAULT_HIGH_THRESHOLD
    score_gap: float = 0.20
    max_confidence_drop: float = 1.0
    fusion_mode: str = "confidence_v1"
    fusion_options: dict[str, float] = field(default_factory=dict)
    collection_calibration_mode: str = "none"
    collection_confidence_offsets: dict[str, float] = field(default_factory=dict)
    ranking_mode: str = "distance"
    hybrid_search: dict[str, Any] = field(default_factory=dict)
    metadata_search: dict[str, Any] = field(default_factory=dict)
    semantic_search: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportedHit:
    rank: int
    distance: float
    root_id: str
    relative_path: str
    # Search sessions may either own a copied result file or point back to the
    # immutable logical source (library/root/relative path).  Keeping this
    # optional lets the desktop gallery avoid duplicating every hit while the
    # explicit export workflow can still create real copies on demand.
    copied_file: str | None
    doc_id: str
    tags: list[str] = field(default_factory=list)
    sha256: str | None = None
    fused_score: float | None = None
    raw_score: float = 0.0
    normalized_score: float = 0.0
    confidence: float = 0.0
    ranking_confidence: float | None = None
    match_state: MatchState = "weak"
    rank_source: RankSource = "text"
    image_confidence: float | None = None
    text_confidence: float | None = None
    metadata_confidence: float | None = None
    image_rank: int | None = None
    text_rank: int | None = None
    metadata_rank: int | None = None
    rank_agreement: float | None = None
    matched_tags: list[str] = field(default_factory=list)
    library_id: str = ""
    library_name: str = ""
    ranking_model_version: str | None = None
    ranking_score: float | None = None
    feature_schema_version: int | None = None
    ranking_fallback: bool = False
    ranking_fallback_reason: str | None = None
    calibrated_minimum_confidence: float | None = None
    calibration_version: str | None = None
    calibration_scope: str | None = None
    calibration_fallback: bool = False
    search_features: dict[str, object] | None = None


@dataclass
class SearchReport:
    query_type: str
    output_dir: str
    result_count: int
    results: list[ExportedHit] = field(default_factory=list)
    # ``results`` can be a bounded first-page preview while ``result_count``
    # remains authoritative and arbitrary pages live in results.sqlite3.
    results_truncated: bool = False
    copy_failures: list[FileFailure] = field(default_factory=list)
    copy_failure_count: int = 0
    request_ids: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    embedding_sources: dict[str, str] = field(default_factory=dict)
    library_ids: list[str] = field(default_factory=list)
    library_names: list[str] = field(default_factory=list)
    result_storage: str = "copied"
    status: str = "ok"
    candidate_count: int = 0
    filtered_count: int = 0
    latency_ms: float = 0.0
    ranking_mode: str = "distance"
    sort_mode: SearchSortMode = "confidence"
    ranking_diagnostics: dict[str, Any] | None = None
    show_low_confidence: bool = False
    low_confidence_override: bool = False
    search_quality: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        if self.ranking_diagnostics is None:
            values.pop("ranking_diagnostics")
        if self.search_quality is None:
            values.pop("search_quality")
        return values
