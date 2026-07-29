from __future__ import annotations

import copy
import math
import os
import re
import threading
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, TypedDict

from .auto_tag_cache import (
    AutoTagCacheFlight,
    SharedAutoTagCache,
    make_auto_tag_cache_key,
)
from .auto_tagging_assets import (
    AUTO_TAGGING_SCHEMA_VERSION,
    CATEGORY_TAGS,
    ENTITY_TYPES,
    FIELD_SPECS,
    FIELD_TAG_LABELS,
    PROMPT_VERSION,
)
from .collection_write_coordinator import (
    CollectionWriteCoordinator,
    PreparedCollectionUpsert,
)
from .config import ConfigurationError, ServiceConfig
from .failure_sink import FailureSink
from .model_catalog import ModelConfiguration, default_model_configuration
from .models import FailureKind, ImageRecord
from .source_resolver import SourcePathResolver
from .state import IndexState
from .tag_aliases import TagAliasDictionary
from .tags import normalize_tags
from .vision_tagging_client import (
    DashScopeVisionTaggingClient,
    TaggingBudget,
    TaggingBudgetExceeded,
    TaggingBudgetTracker,
    TaggingContext,
    VisionPricing,
    VisionTaggingConfig,
    VisionTaggingError,
    VisionTaggingResponse,
    sanitize_generated_tag,
)
from .zvec_repository import ZvecImageRepository

DEFAULT_AUTO_TAG_LIMIT = 200
MAX_AUTO_TAG_LIMIT = 10_000
DEFAULT_PENDING_REVIEW_LIMIT = 100
MAX_PENDING_REVIEW_LIMIT = 500
AUTO_TAG_RESULT_PROPOSAL_LIMIT = 100
ESTIMATED_INPUT_TOKENS_PER_IMAGE = 1_000
ESTIMATED_OUTPUT_TOKENS_PER_IMAGE = 250
PRICING_EFFECTIVE_FROM = "2026-07-14"
SUPPORTED_SCOPES = {
    "latest_index_run",
    "untagged",
    "failed",
    "failed_all",
    "all",
}
FLASH_MODEL = "qwen3-vl-flash"
PLUS_MODEL = "qwen3-vl-plus"
HIGH_CONFIDENCE_ENTITY_THRESHOLD = 0.80
POLICY_VERSION = 3
LOW_RISK_FIELD_CONFIDENCE = 0.80
REVIEW_STATES = {"all", "low_risk", "identity", "conflict", "failed"}
FOLDER_INHERITANCE_POLICY_VERSION = 2
FOLDER_INHERITANCE_AUDIT_LIMIT = 100
FOLDER_INHERITANCE_ROW_BATCH_SIZE = 500
FOLDER_INHERITANCE_TAG_LIMIT_PER_SOURCE = 512
FOLDER_INHERITANCE_GLOBAL_TAG_LIMIT = 50_000
FOLDER_INHERITANCE_GLOBAL_SOURCE_SAMPLE_LIMIT = 50_000
FOLDER_INHERITANCE_GLOBAL_FILTER_SAMPLE_LIMIT = 50_000
_FOLDER_INHERITANCE_FILTER_BUFFER = FOLDER_INHERITANCE_AUDIT_LIMIT * 2
CLUSTER_IDENTITY_CATEGORIES = frozenset(
    {"real_person", "cosplayer", "character", "work"}
)
FOLDER_INHERITANCE_BLOCKED_CATEGORIES = frozenset(
    {
        "action",
        "pose",
        "expression",
        "emotion",
        "facial_expression",
    }
)
_ENTITY_STATES = {
    "confirmed",
    "suggested",
    "conflict",
    "unknown",
    "unable_to_confirm",
    "not_applicable",
}
_EXPLICIT_EVIDENCE = {
    "manual_tag",
    "folder_name",
    "file_name",
    "sidecar",
    "ocr",
    "watermark",
}
_CONTEXT_EVIDENCE_SOURCES = (
    "manual_tag",
    "folder_name",
    "file_name",
    "sidecar",
    "ocr",
    "watermark",
)
_CONTROLLED_TAGS = {
    key: label
    for code, label in FIELD_TAG_LABELS.items()
    for key in (
        unicodedata.normalize("NFKC", code).strip().casefold(),
        unicodedata.normalize("NFKC", label).strip().casefold(),
    )
}
_CONTROLLED_TAG_FIELDS = {
    unicodedata.normalize("NFKC", value).strip().casefold(): field_name
    for field_name, spec in FIELD_SPECS.items()
    for code in spec.values
    for value in (code, FIELD_TAG_LABELS[code])
}


class AutoTagRunReport(TypedDict):
    scope: str
    model: str
    primary_model: str
    escalation_model: str
    candidate_count: int
    unique_image_count: int
    processed: int
    unique_processed: int
    cached: int
    cache_hits: int
    succeeded: int
    failed: int
    model_failed: int
    deferred: int
    needs_attention: bool
    failures: list[dict[str, Any]]
    failure_manifest: str
    quarantined: int
    quarantine_copy_failures: int
    stopped_reason: str
    actual_cost_cny: float
    input_tokens: int
    output_tokens: int
    api_request_count: int
    http_attempt_count: int
    logical_model_calls: int
    retry_count: int
    network_concurrency: int
    flash_requests: int
    plus_requests: int
    escalated_count: int
    escalation_failures: int
    auto_accepted_tag_count: int
    auto_accepted_identity_count: int
    folder_inheritance_recovered: int
    folder_inheritance_unresolved: int
    folder_inheritance_failures: list[dict[str, Any]]
    folder_inheritance_failures_total: int
    folder_inheritance_failures_truncated: bool
    pending_count: int
    proposals: list[dict[str, Any]]


class AutoTaggingRequestError(ValueError):
    pass


@dataclass(frozen=True)
class _ModelResolution:
    model: str
    cache_key: str
    status: str
    annotation: dict[str, Any] | None = None
    cached: bool = False
    request_id: str = ""
    error: str = ""
    budget_exhausted: bool = False
    failure_kind: FailureKind = "item"


@dataclass(frozen=True)
class _PreparedModelRequest:
    model: str
    cache_key: str
    content_hash: str
    context: TaggingContext
    path: Path
    config: VisionTaggingConfig
    tracker: TaggingBudgetTracker
    flight: AutoTagCacheFlight
    reserved_cost_cny: float = 0.0


@dataclass(frozen=True)
class _CompletedModelRequest:
    prepared: _PreparedModelRequest
    response: VisionTaggingResponse | None = None
    error: str = ""
    budget_exhausted: bool = False
    http_attempts: int = 0
    failure_kind: FailureKind = "item"


@dataclass
class _RunAccounting:
    max_budget_cny: float | None
    model_configuration: ModelConfiguration
    logical_model_calls: int = 0
    api_request_count: int = 0
    flash_requests: int = 0
    plus_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    actual_cost_cny: float = 0.0
    reserved_cost_cny: float = 0.0

    @property
    def remaining_budget_cny(self) -> float | None:
        if self.max_budget_cny is None:
            return None
        return max(
            0.0,
            self.max_budget_cny - self.actual_cost_cny - self.reserved_cost_cny,
        )

    def create_tracker(self, model: str) -> tuple[TaggingBudgetTracker, float]:
        pricing = _pricing_for_model(model, self.model_configuration)
        remaining = self.remaining_budget_cny
        estimated_cost = pricing.cost(
            ESTIMATED_INPUT_TOKENS_PER_IMAGE,
            ESTIMATED_OUTPUT_TOKENS_PER_IMAGE,
        )
        if remaining is not None and estimated_cost > remaining + 1e-12:
            raise TaggingBudgetExceeded("The monetary budget is exhausted.")
        reserved = estimated_cost if remaining is not None else 0.0
        self.reserved_cost_cny += reserved
        self.logical_model_calls += 1
        return (
            TaggingBudgetTracker(
                TaggingBudget(
                    max_images=1,
                    max_cost_yuan=remaining if remaining is not None else None,
                    estimated_input_tokens_per_image=(ESTIMATED_INPUT_TOKENS_PER_IMAGE),
                    estimated_output_tokens_per_image=(
                        ESTIMATED_OUTPUT_TOKENS_PER_IMAGE
                    ),
                ),
                pricing,
            ),
            reserved,
        )

    def release_reservation(self, reserved_cost_cny: float) -> None:
        self.reserved_cost_cny = max(
            0.0,
            self.reserved_cost_cny - max(0.0, reserved_cost_cny),
        )

    def record_request(
        self,
        model: str,
        response: Any | None = None,
        *,
        reserved_cost_cny: float = 0.0,
        attempts: int = 1,
    ) -> None:
        self.release_reservation(reserved_cost_cny)
        normalized_attempts = max(0, int(attempts))
        self.api_request_count += normalized_attempts
        if model == self.model_configuration.auto_tag_primary_model:
            self.flash_requests += normalized_attempts
        else:
            self.plus_requests += normalized_attempts
        if response is None:
            return
        usage = response.usage
        self.input_tokens += int(usage.input_tokens)
        self.output_tokens += int(usage.output_tokens)
        cost = response.cost_yuan
        if cost is None:
            cost = _pricing_for_model(model, self.model_configuration).cost(
                int(usage.input_tokens),
                int(usage.output_tokens),
            )
        self.actual_cost_cny += max(0.0, float(cost))


@dataclass
class _FolderTagOccurrences:
    """Track one normalized tag without retaining every donor document."""

    count: int = 0
    first_occurrences: list[tuple[str, str]] = field(default_factory=list)

    def add(self, *, doc_id: str, tag: str) -> None:
        self.count += 1
        if len(self.first_occurrences) < 2:
            self.first_occurrences.append((doc_id, tag))

    def value_excluding(self, doc_id: str | None) -> str | None:
        for contributor_id, tag in self.first_occurrences:
            if contributor_id != doc_id:
                return tag
        # A donor contributes a normalized tag at most once per source. Thus,
        # if both retained slots fail to produce a different document, the
        # excluded target was the sole contributor. If the target appeared
        # third or later, the first slot necessarily belongs to another donor.
        return None


@dataclass
class _FolderInheritanceMemoryBudget:
    """One run-wide cap shared by every requested folder aggregate."""

    tag_limit: int = FOLDER_INHERITANCE_GLOBAL_TAG_LIMIT
    source_sample_limit: int = FOLDER_INHERITANCE_GLOBAL_SOURCE_SAMPLE_LIMIT
    filtered_sample_limit: int = FOLDER_INHERITANCE_GLOBAL_FILTER_SAMPLE_LIMIT
    retained_tags: int = 0
    retained_source_samples: int = 0
    retained_filtered_samples: int = 0

    def reserve_tag(self) -> bool:
        if self.retained_tags >= self.tag_limit:
            return False
        self.retained_tags += 1
        return True

    def reserve_source_sample(self) -> bool:
        if self.retained_source_samples >= self.source_sample_limit:
            return False
        self.retained_source_samples += 1
        return True

    def reserve_filtered_sample(self) -> bool:
        if self.retained_filtered_samples >= self.filtered_sample_limit:
            return False
        self.retained_filtered_samples += 1
        return True


@dataclass
class _FolderInheritanceAggregate:
    """Bounded donor summary for one direct folder.

    Only unique tag keys and small audit samples survive a consumed database
    page.  Candidate documents are still allowed to donate manual labels to
    their peers; two deterministic provenance slots let a target subtract only
    its own contribution so it can never recover from its own labels.
    """

    target_doc_ids: frozenset[str]
    memory_budget: _FolderInheritanceMemoryBudget = field(
        default_factory=_FolderInheritanceMemoryBudget
    )
    tags_by_source: dict[str, dict[str, _FolderTagOccurrences]] = field(
        default_factory=lambda: {
            "manual": {},
            "folder": {},
            "accepted_auto": {},
        }
    )
    source_count: int = 0
    source_samples: list[tuple[str, str]] = field(default_factory=list)
    eligible_target_doc_ids: set[str] = field(default_factory=set)
    filtered_count: int = 0
    filtered_samples: list[dict[str, str]] = field(default_factory=list)
    filtered_counts_by_target: dict[str, int] = field(default_factory=dict)
    tag_totals_by_source: dict[str, int] = field(
        default_factory=lambda: {
            "manual": 0,
            "folder": 0,
            "accepted_auto": 0,
        }
    )
    omitted_tags_by_source: dict[str, int] = field(
        default_factory=lambda: {
            "manual": 0,
            "folder": 0,
            "accepted_auto": 0,
        }
    )
    tag_totals_by_target: dict[str, dict[str, int]] = field(default_factory=dict)
    omitted_tags_by_target: dict[str, dict[str, int]] = field(default_factory=dict)

    def consume(
        self,
        donor: Mapping[str, Any],
        annotation: Mapping[str, Any] | None,
    ) -> bool:
        donor_id = str(donor.get("doc_id") or "")
        current_annotation = (
            annotation
            if annotation is not None
            and str(annotation.get("source_sha256") or "")
            == str(donor.get("sha256") or "")
            else None
        )
        manual_tags = normalize_tags(donor.get("tags", ()))
        annotation_accepted = bool(
            current_annotation is not None
            and current_annotation.get("status") == "accepted"
        )
        if not manual_tags and not annotation_accepted:
            return False

        is_target = donor_id in self.target_doc_ids
        self.source_count += 1
        if is_target:
            self.eligible_target_doc_ids.add(donor_id)
        if (
            len(self.source_samples) < FOLDER_INHERITANCE_AUDIT_LIMIT + 1
            and self.memory_budget.reserve_source_sample()
        ):
            self.source_samples.append(
                (donor_id, str(donor.get("relative_path") or ""))
            )

        values_by_source: tuple[tuple[str, Iterable[str]], ...] = (
            ("manual", manual_tags),
            ("folder", normalize_tags(donor.get("folder_tags", ()))),
            (
                "accepted_auto",
                normalize_tags(donor.get("accepted_auto_tags", ()))
                if annotation_accepted
                else (),
            ),
        )
        for source, values in values_by_source:
            bucket = self.tags_by_source[source]
            # One document may contain spelling/case variants that collapse to
            # the same comparison key. Count it once so the two provenance
            # slots always represent two different donor documents.
            for tag in _stable_strings(values):
                blocked_category = _blocked_folder_inheritance_category(
                    tag,
                    current_annotation,
                )
                if blocked_category is not None:
                    self.filtered_count += 1
                    if is_target:
                        self.filtered_counts_by_target[donor_id] = (
                            self.filtered_counts_by_target.get(donor_id, 0) + 1
                        )
                    if (
                        len(self.filtered_samples) < _FOLDER_INHERITANCE_FILTER_BUFFER
                        and self.memory_budget.reserve_filtered_sample()
                    ):
                        self.filtered_samples.append(
                            {
                                "tag": tag,
                                "category": blocked_category,
                                "source": source,
                                "source_doc_id": donor_id,
                                "reason": "transient_category",
                            }
                        )
                    continue
                key = _comparison_key(tag)
                self.tag_totals_by_source[source] += 1
                if is_target:
                    target_totals = self.tag_totals_by_target.setdefault(donor_id, {})
                    target_totals[source] = target_totals.get(source, 0) + 1
                occurrence = bucket.get(key)
                if occurrence is None:
                    if (
                        len(bucket) >= FOLDER_INHERITANCE_TAG_LIMIT_PER_SOURCE
                        or not self.memory_budget.reserve_tag()
                    ):
                        # Count every omitted contribution without retaining its
                        # key. This keeps pathological folders with a unique tag
                        # per image strictly bounded while making truncation
                        # visible in the persisted audit.
                        self.omitted_tags_by_source[source] += 1
                        if is_target:
                            target_omitted = self.omitted_tags_by_target.setdefault(
                                donor_id, {}
                            )
                            target_omitted[source] = target_omitted.get(source, 0) + 1
                        continue
                    occurrence = _FolderTagOccurrences()
                    bucket[key] = occurrence
                occurrence.add(doc_id=donor_id, tag=tag)
        return True

    def payload(self, *, exclude_doc_id: str | None = None) -> dict[str, Any]:
        source_tags: dict[str, list[str]] = {}
        accepted: list[str] = []
        for source in ("manual", "folder", "accepted_auto"):
            values = [
                value
                for occurrence in self.tags_by_source[source].values()
                if (value := occurrence.value_excluding(exclude_doc_id)) is not None
            ]
            normalized = list(normalize_tags(values))
            source_tags[source] = normalized
            accepted.extend(normalized)

        source_total = self.source_count - int(
            bool(exclude_doc_id and exclude_doc_id in self.eligible_target_doc_ids)
        )
        source_samples = [
            (doc_id, relative_path)
            for doc_id, relative_path in self.source_samples
            if doc_id != exclude_doc_id
        ][:FOLDER_INHERITANCE_AUDIT_LIMIT]
        filtered_total = self.filtered_count - (
            self.filtered_counts_by_target.get(exclude_doc_id, 0)
            if exclude_doc_id
            else 0
        )
        filtered_samples = [
            dict(item)
            for item in self.filtered_samples
            if item.get("source_doc_id") != exclude_doc_id
        ][:FOLDER_INHERITANCE_AUDIT_LIMIT]
        source_ids = _stable_strings(item[0] for item in source_samples)
        source_paths = _stable_strings(item[1] for item in source_samples)
        excluded_tag_totals = (
            self.tag_totals_by_target.get(exclude_doc_id, {}) if exclude_doc_id else {}
        )
        excluded_omitted = (
            self.omitted_tags_by_target.get(exclude_doc_id, {})
            if exclude_doc_id
            else {}
        )
        source_tags_total = {
            source: total - excluded_tag_totals.get(source, 0)
            for source, total in self.tag_totals_by_source.items()
        }
        source_tags_omitted = {
            source: omitted - excluded_omitted.get(source, 0)
            for source, omitted in self.omitted_tags_by_source.items()
        }
        source_tags_truncated = {
            source: omitted > 0 for source, omitted in source_tags_omitted.items()
        }
        return {
            # Comparison-key de-duplication across sources is what makes the
            # manual spelling win over folder/model variants of the same tag.
            "accepted_tags": _stable_strings(accepted),
            "filtered_tags": filtered_samples,
            "filtered_tags_total": filtered_total,
            "filtered_tags_truncated": filtered_total > len(filtered_samples),
            "source_doc_ids": source_ids,
            "source_doc_ids_total": source_total,
            "source_doc_ids_truncated": source_total > len(source_ids),
            "source_relative_paths": source_paths,
            "source_relative_paths_total": source_total,
            "source_relative_paths_truncated": source_total > len(source_paths),
            "source_tags": source_tags,
            "source_tags_total": source_tags_total,
            "source_tags_omitted": source_tags_omitted,
            "source_tags_truncated": source_tags_truncated,
        }


def _empty_folder_inheritance_payload() -> dict[str, Any]:
    return {
        "accepted_tags": [],
        "filtered_tags": [],
        "filtered_tags_total": 0,
        "filtered_tags_truncated": False,
        "source_doc_ids": [],
        "source_doc_ids_total": 0,
        "source_doc_ids_truncated": False,
        "source_relative_paths": [],
        "source_relative_paths_total": 0,
        "source_relative_paths_truncated": False,
        "source_tags": {
            "manual": [],
            "folder": [],
            "accepted_auto": [],
        },
        "source_tags_total": {
            "manual": 0,
            "folder": 0,
            "accepted_auto": 0,
        },
        "source_tags_omitted": {
            "manual": 0,
            "folder": 0,
            "accepted_auto": 0,
        },
        "source_tags_truncated": {
            "manual": False,
            "folder": False,
            "accepted_auto": False,
        },
    }


class AutoTaggingCoordinator:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        state: IndexState,
        repository: ZvecImageRepository,
        cache: SharedAutoTagCache,
        source_resolver: SourcePathResolver,
        progress: Callable[[str], None],
        cancel_check: Callable[[], None],
        collection_writes: CollectionWriteCoordinator,
    ) -> None:
        self.config = config
        self.state = state
        self.repository = repository
        self.cache = cache
        self.source_resolver = source_resolver
        self.progress = progress
        self.cancel_check = cancel_check
        self.collection_writes = collection_writes

    def estimate(
        self,
        *,
        scope: str = "untagged",
        model: str | None = None,
        max_images: int = DEFAULT_AUTO_TAG_LIMIT,
        max_budget_cny: float | None = None,
    ) -> dict[str, Any]:
        selected_model = _normalize_model(model, self.config.model_configuration)
        escalation_model = self.config.auto_tag_escalation_model
        limit = _validate_limit(max_images)
        budget = _validate_budget(max_budget_cny)
        candidates = self._candidates(scope, limit)
        groups = _group_by_sha(candidates)
        cached_groups = 0
        cache_hits = 0
        flash_requests = 0
        plus_requests = 0
        potential_plus_requests = 0
        escalated_count = 0
        for content_hash in groups:
            group_requires_api = False
            if selected_model == escalation_model:
                plus_cached = self.cache.get(_cache_key(content_hash, selected_model))
                if plus_cached is None:
                    plus_requests += 1
                    group_requires_api = True
                else:
                    cache_hits += 1
            else:
                flash_cached = self.cache.get(_cache_key(content_hash, selected_model))
                if flash_cached is None:
                    flash_requests += 1
                    potential_plus_requests += 1
                    group_requires_api = True
                else:
                    cache_hits += 1
                    if flash_cached[
                        "status"
                    ] == "succeeded" and _annotation_plus_recommended(
                        flash_cached["annotation"]
                    ):
                        escalated_count += 1
                        plus_cached = self.cache.get(
                            _cache_key(content_hash, escalation_model)
                        )
                        if plus_cached is None:
                            plus_requests += 1
                            group_requires_api = True
                        else:
                            cache_hits += 1
            if not group_requires_api:
                cached_groups += 1

        known_requests = flash_requests + plus_requests
        estimated_plus_requests = plus_requests + potential_plus_requests
        total_estimated_requests = flash_requests + estimated_plus_requests
        estimated_input = total_estimated_requests * ESTIMATED_INPUT_TOKENS_PER_IMAGE
        estimated_output = total_estimated_requests * ESTIMATED_OUTPUT_TOKENS_PER_IMAGE
        flash_pricing = _pricing_for_model(
            selected_model, self.config.model_configuration
        )
        plus_pricing = _pricing_for_model(
            escalation_model, self.config.model_configuration
        )
        estimated_cost = flash_pricing.cost(
            flash_requests * ESTIMATED_INPUT_TOKENS_PER_IMAGE,
            flash_requests * ESTIMATED_OUTPUT_TOKENS_PER_IMAGE,
        ) + plus_pricing.cost(
            estimated_plus_requests * ESTIMATED_INPUT_TOKENS_PER_IMAGE,
            estimated_plus_requests * ESTIMATED_OUTPUT_TOKENS_PER_IMAGE,
        )
        pending_count = self._annotation_count_in_library("pending_review")
        pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for annotation, entry in self._annotations_in_library("pending_review"):
            pending.append((annotation, entry))
            if len(pending) >= 50:
                break
        return {
            "scope": scope,
            "model": selected_model,
            "primary_model": selected_model,
            "escalation_model": escalation_model,
            "candidate_count": len(candidates),
            "unique_image_count": len(groups),
            "cached_count": cached_groups,
            "cache_hits": cache_hits,
            "api_candidate_count": known_requests,
            "api_request_count": known_requests,
            "flash_requests": flash_requests,
            "plus_requests": plus_requests,
            "potential_plus_requests": potential_plus_requests,
            "escalated_count": escalated_count,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "estimated_cost_cny": estimated_cost,
            "max_budget_cny": budget,
            "over_budget": budget is not None and estimated_cost > budget,
            "pricing_effective_from": flash_pricing.effective_from,
            "folder_tag_preview": [
                {
                    "doc_id": str(entry["doc_id"]),
                    "relative_path": str(entry["relative_path"]),
                    "folder_tags": list(entry.get("folder_tags", ())),
                }
                for entry in candidates[:12]
            ],
            "pending_count": pending_count,
            "proposals": [
                self._stored_proposal(annotation, entry)
                for annotation, entry in pending
            ],
        }

    def pending(
        self,
        *,
        offset: int = 0,
        limit: int = DEFAULT_PENDING_REVIEW_LIMIT,
        filters: Mapping[str, Any] | None = None,
        aliases: TagAliasDictionary | None = None,
    ) -> dict[str, Any]:
        page_offset = _validate_pending_offset(offset)
        page_limit = _validate_pending_limit(limit)
        normalized_filters = _normalize_pending_filters(filters)
        annotation_status = (
            "failed"
            if normalized_filters["review_state"] == "failed"
            else "pending_review"
        )
        latest_run_id: str | None = None
        if normalized_filters["latest_index_only"]:
            scoped, root_id = self._configured_root()
            latest_run_id = (
                None
                if scoped and root_id is None
                else self.state.latest_index_run_id(root_id if scoped else None)
            )
            if latest_run_id is None:
                latest_batch = self.state.latest_auto_tag_review_batch()
                return {
                    "pending_count": 0,
                    "total_count": 0,
                    "offset": page_offset,
                    "limit": page_limit,
                    "proposals": [],
                    "has_more": False,
                    "filters": normalized_filters,
                    "undo_available": self._review_batch_is_undoable(latest_batch),
                }
        requires_python_filtering = any(
            str(normalized_filters.get(name) or "")
            for name in ("character", "work", "action", "expression")
        ) or normalized_filters["review_state"] in {"low_risk", "identity", "conflict"}
        pending_count = (
            0
            if requires_python_filtering
            else self._annotation_count_in_library(
                annotation_status,
                run_id=latest_run_id,
            )
        )
        proposals: list[dict[str, Any]] = []
        matched_index = 0
        for annotation, entry in self._annotations_in_library(
            annotation_status,
            run_id=latest_run_id,
        ):
            proposal = self._stored_proposal(annotation, entry)
            if requires_python_filtering:
                if not _proposal_matches_pending_filters(
                    proposal,
                    normalized_filters,
                    latest_ids=None,
                    aliases=aliases,
                ):
                    continue
                if page_offset <= pending_count < page_offset + page_limit:
                    proposals.append(proposal)
                pending_count += 1
                continue
            if page_offset <= matched_index < page_offset + page_limit:
                proposals.append(proposal)
            matched_index += 1
            if matched_index >= page_offset + page_limit:
                break
        latest_batch = self.state.latest_auto_tag_review_batch()
        return {
            "pending_count": pending_count,
            "total_count": pending_count,
            "offset": page_offset,
            "limit": page_limit,
            "proposals": proposals,
            "has_more": page_offset + len(proposals) < pending_count,
            "filters": normalized_filters,
            "undo_available": self._review_batch_is_undoable(latest_batch),
        }

    def run(
        self,
        *,
        scope: str = "untagged",
        model: str | None = None,
        max_images: int = DEFAULT_AUTO_TAG_LIMIT,
        max_budget_cny: float | None = None,
        external_processing_confirmed: bool = False,
    ) -> AutoTagRunReport:
        if not external_processing_confirmed:
            raise AutoTaggingRequestError(
                "External image processing must be explicitly confirmed."
            )
        selected_model = _normalize_model(model, self.config.model_configuration)
        limit = _validate_limit(max_images)
        budget_limit = _validate_budget(max_budget_cny)
        candidates = self._candidates(scope, limit)
        groups = _group_by_sha(candidates)
        accounting = _RunAccounting(budget_limit, self.config.model_configuration)
        failure_sink = FailureSink(
            self.config.results_path,
            f"auto-tag-{uuid.uuid4().hex}",
            self.config.workspace,
        )
        work_items = [
            (content_hash, entries, _aggregate_context(entries))
            for content_hash, entries in groups.items()
        ]
        primary_model = selected_model
        primary_results = self._resolve_model_wave(
            work_items,
            model=primary_model,
            accounting=accounting,
        )
        return self._complete_run(
            scope=scope,
            selected_model=selected_model,
            candidate_count=len(candidates),
            work_items=work_items,
            primary_results=primary_results,
            accounting=accounting,
            failure_sink=failure_sink,
        )

    def reconcile_pending_identity_tags(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply the current automatic-approval policy to legacy proposals.

        The public method name is retained for compatibility.  This is a local
        metadata migration: it never calls a model or regenerates a vector.
        Every still-valid proposed tag is accepted, while tags the user already
        rejected remain suppressed.
        """

        scanned = 0
        eligible = 0
        updated = 0
        auto_accepted_documents = 0
        auto_accepted_tags = 0
        auto_accepted_identity_tags = 0
        conflict_documents = 0
        conflict_identity_tags = 0
        skipped_current_policy = 0
        skipped_stale = 0
        failures: list[dict[str, str]] = []

        for annotation, entry in self._annotations_in_library("pending_review"):
            self.cancel_check()
            scanned += 1
            policy = dict(annotation.get("policy") or {})
            try:
                policy_version = int(policy.get("policy_version") or 1)
            except (TypeError, ValueError):
                policy_version = 1
            if policy_version >= POLICY_VERSION:
                skipped_current_policy += 1
                continue

            doc_id = str(annotation["doc_id"])
            if str(annotation.get("source_sha256") or "") != str(
                entry.get("sha256") or ""
            ):
                skipped_stale += 1
                continue
            structured = dict(annotation.get("structured") or {})
            if not structured:
                failures.append(
                    {
                        "doc_id": doc_id,
                        "error": "Legacy annotation has no structured payload.",
                    }
                )
                continue

            rejected_tags = tuple(annotation.get("rejected_tags", ()))
            local_structured = _finalize_structured_annotation(
                _revalidate_annotation_for_context(
                    structured,
                    _aggregate_context([entry]),
                )
            )
            proposed_tags = _proposed_tags(entry, local_structured, rejected_tags)
            metadata = _proposal_tag_metadata(
                entry,
                local_structured,
                proposed_tags,
            )
            approved_identity_tags = normalize_tags(
                str(detail.get("tag") or "")
                for detail in metadata["tag_details"]
                if isinstance(detail, Mapping) and bool(detail.get("identity"))
            )
            conflicting_tags = list(metadata["conflicting_identity_tags"])
            eligible += 1
            auto_accepted_documents += int(bool(proposed_tags))
            auto_accepted_tags += len(proposed_tags)
            auto_accepted_identity_tags += len(approved_identity_tags)
            conflict_documents += int(bool(conflicting_tags))
            conflict_identity_tags += len(conflicting_tags)
            if dry_run:
                continue

            migrated_policy = {
                **policy,
                "policy_version": POLICY_VERSION,
                "identity_auto_accept_migrated": True,
                "identity_auto_accept_migration": "all-valid-model-tags-v3",
                "automatic_approval_mode": "all_valid_model_tags",
            }
            try:
                self._attach_proposal(
                    entry=entry,
                    cache_key=str(annotation.get("cache_key") or ""),
                    structured=local_structured,
                    policy=migrated_policy,
                )
            except Exception as exc:
                failures.append(
                    {
                        "doc_id": doc_id,
                        "error": str(exc) or exc.__class__.__name__,
                    }
                )
                continue
            updated += 1
            if updated % 100 == 0:
                self.progress(
                    f"Reconciled identity policy for {updated}/{eligible} annotations."
                )

        if not dry_run and auto_accepted_tags:
            self._finalize_review_repository()
        return {
            "dry_run": dry_run,
            "scanned": scanned,
            "eligible": eligible,
            "would_update": eligible,
            "updated": updated,
            "auto_accepted_documents": auto_accepted_documents,
            "auto_accepted_tags": auto_accepted_tags,
            "auto_accepted_identity_tags": auto_accepted_identity_tags,
            "conflict_documents": conflict_documents,
            "conflict_identity_tags": conflict_identity_tags,
            "skipped_current_policy": skipped_current_policy,
            "skipped_stale": skipped_stale,
            "failed": len(failures),
            "failures": failures[:100],
            "api_request_count": 0,
        }

    def begin_stream(
        self,
        *,
        model: str | None = None,
        max_images: int = DEFAULT_AUTO_TAG_LIMIT,
        max_budget_cny: float | None = None,
        external_processing_confirmed: bool = False,
        activity_callback: Callable[[int], None] | None = None,
    ) -> StreamingAutoTagSession:
        """Create an owner-thread session fed only after index commits succeed."""

        if not external_processing_confirmed:
            raise AutoTaggingRequestError(
                "External image processing must be explicitly confirmed."
            )
        return StreamingAutoTagSession(
            self,
            selected_model=_normalize_model(model, self.config.model_configuration),
            limit=_validate_limit(max_images),
            budget_limit=_validate_budget(max_budget_cny),
            activity_callback=activity_callback,
        )

    def _complete_run(
        self,
        *,
        scope: str,
        selected_model: str,
        candidate_count: int,
        work_items: list[tuple[str, list[dict[str, Any]], TaggingContext]],
        primary_results: dict[str, _ModelResolution],
        accounting: _RunAccounting,
        failure_sink: FailureSink,
        freshness_validator: Callable[[str], bool] | None = None,
    ) -> AutoTagRunReport:
        processed = 0
        cached_count = 0
        cache_hits = 0
        succeeded = 0
        failed = 0
        escalation_failures = 0
        deferred = 0
        stopped_reason = ""
        proposals: list[dict[str, Any]] = []
        failure_details: list[dict[str, Any]] = []
        quarantined = 0
        quarantine_copy_failures = 0
        needs_attention = False
        auto_accepted_tag_count = 0
        auto_accepted_identity_count = 0
        content_policy_failures: list[dict[str, Any]] = []
        deferred_content_policy_captures: list[
            tuple[list[dict[str, Any]], _ModelResolution]
        ] = []
        escalation_model = self.config.auto_tag_escalation_model
        plus_work_items: list[tuple[str, list[dict[str, Any]], TaggingContext]] = []
        normalized_flash: dict[str, dict[str, Any]] = {}
        if selected_model != escalation_model:
            for content_hash, entries, context in work_items:
                flash = primary_results[content_hash]
                if flash.annotation is None or flash.budget_exhausted:
                    continue
                structured = _normalize_annotation(flash.annotation)
                normalized_flash[content_hash] = structured
                if bool(structured.get("plus_recommended")):
                    plus_work_items.append((content_hash, entries, context))
        escalated_count = len(plus_work_items)
        plus_results = self._resolve_model_wave(
            plus_work_items,
            model=escalation_model,
            accounting=accounting,
            freshness_validator=freshness_validator,
        )

        last_commit_progress_at = 0.0
        for content_hash, entries, _context in work_items:
            self.cancel_check()
            primary = primary_results[content_hash]
            if freshness_validator is not None:
                freshness_validator(content_hash)
            model_steps: list[_ModelResolution] = [primary]
            if primary.budget_exhausted:
                stopped_reason = "budget_exhausted"
                deferred += len(entries)
                continue
            if primary.annotation is None:
                failure_category = _annotation_failure_category(
                    primary.error,
                    primary.failure_kind,
                )
                self._store_resolution_failure(
                    entries,
                    content_hash,
                    primary,
                    selected_model,
                    model_steps,
                )
                if failure_category == "content_policy":
                    content_policy_failures.extend(entries)
                    deferred_content_policy_captures.append((list(entries), primary))
                else:
                    for capture in self._capture_resolution_failures(
                        failure_sink,
                        entries,
                        primary,
                    ):
                        if len(failure_details) < 100:
                            failure_details.append(asdict(capture.failure))
                        quarantined += int(bool(capture.failure.quarantined_path))
                        quarantine_copy_failures += int(
                            bool(capture.failure.copy_error)
                        )
                        needs_attention = (
                            needs_attention
                            or capture.needs_attention
                            or primary.failure_kind == "systemic"
                        )
                failed += len(entries)
                processed += 1
                cache_hits += int(primary.cached)
                cached_count += int(primary.cached)
                continue

            if selected_model == escalation_model:
                structured = _finalize_structured_annotation(
                    _normalize_annotation(primary.annotation)
                )
            else:
                structured = normalized_flash[content_hash]
                plus = plus_results.get(content_hash)
                if plus is not None:
                    model_steps.append(plus)
                    if plus.annotation is not None:
                        structured = _merge_annotations(
                            structured,
                            _normalize_annotation(plus.annotation),
                        )
                    elif plus.budget_exhausted:
                        structured = _add_review_reason(
                            structured,
                            "plus_budget_exhausted",
                        )
                        stopped_reason = "budget_exhausted"
                    else:
                        escalation_failures += 1
                        structured = _add_review_reason(
                            structured,
                            "plus_cached_failure"
                            if plus.cached
                            else "plus_request_failed",
                        )
                structured = _finalize_structured_annotation(structured)

            cache_hits += sum(int(step.cached) for step in model_steps)
            cached_count += int(
                bool(model_steps) and all(step.cached for step in model_steps)
            )
            policy = _build_policy(selected_model, structured, model_steps)
            cache_key = next(
                (
                    step.cache_key
                    for step in reversed(model_steps)
                    if step.annotation is not None
                ),
                model_steps[-1].cache_key,
            )
            for entry in entries:
                proposal = self._attach_proposal(
                    entry=entry,
                    cache_key=cache_key,
                    structured=structured,
                    policy=policy,
                )
                auto_accepted_tag_count += len(proposal.get("auto_accepted_tags", ()))
                auto_accepted_identity_count += len(
                    proposal.get("auto_accepted_identity_tags", ())
                )
                if len(proposals) < AUTO_TAG_RESULT_PROPOSAL_LIMIT:
                    proposals.append(proposal)
                succeeded += 1
            processed += 1
            now = monotonic()
            if now - last_commit_progress_at >= 0.25 or processed == len(work_items):
                self.progress(
                    f"Auto-tagged {processed}/{len(work_items)} unique images."
                )
                last_commit_progress_at = now

        model_failed = failed
        inheritance = self._inherit_content_policy_failures(content_policy_failures)
        recovered = int(inheritance["recovered"])
        succeeded += recovered
        failed = max(0, failed - recovered)
        for captured_entries, resolution in deferred_content_policy_captures:
            unresolved_entries = []
            for captured_entry in captured_entries:
                current_annotation = self.state.get_document_annotation(
                    str(captured_entry.get("doc_id") or "")
                )
                if (
                    current_annotation is not None
                    and current_annotation.get("status") == "failed"
                ):
                    unresolved_entries.append(captured_entry)
            for capture in self._capture_resolution_failures(
                failure_sink,
                unresolved_entries,
                resolution,
            ):
                if len(failure_details) < 100:
                    failure_details.append(asdict(capture.failure))
                quarantined += int(bool(capture.failure.quarantined_path))
                quarantine_copy_failures += int(bool(capture.failure.copy_error))
                needs_attention = needs_attention or capture.needs_attention
        for proposal in inheritance["proposals"]:
            if len(proposals) >= AUTO_TAG_RESULT_PROPOSAL_LIMIT:
                break
            proposals.append(proposal)

        if auto_accepted_tag_count or recovered:
            self._finalize_review_repository()
        pending_count = self.state.count_document_annotations("pending_review")
        return {
            "scope": scope,
            "model": selected_model,
            "primary_model": selected_model,
            "escalation_model": escalation_model,
            "candidate_count": candidate_count,
            "unique_image_count": len(work_items),
            "processed": succeeded + failed,
            "unique_processed": processed,
            "cached": cached_count,
            "cache_hits": cache_hits,
            "succeeded": succeeded,
            "failed": failed,
            "model_failed": model_failed,
            "deferred": deferred,
            "needs_attention": needs_attention,
            "failures": failure_details,
            "failure_manifest": failure_sink.manifest_path,
            "quarantined": quarantined,
            "quarantine_copy_failures": quarantine_copy_failures,
            "stopped_reason": stopped_reason,
            "actual_cost_cny": accounting.actual_cost_cny,
            "input_tokens": accounting.input_tokens,
            "output_tokens": accounting.output_tokens,
            "api_request_count": accounting.api_request_count,
            "http_attempt_count": accounting.api_request_count,
            "logical_model_calls": accounting.logical_model_calls,
            "retry_count": max(
                0,
                accounting.api_request_count - accounting.logical_model_calls,
            ),
            "network_concurrency": max(
                1,
                int(getattr(self.config, "auto_tag_concurrency", 2)),
            ),
            "flash_requests": accounting.flash_requests,
            "plus_requests": accounting.plus_requests,
            "escalated_count": escalated_count,
            "escalation_failures": escalation_failures,
            "auto_accepted_tag_count": auto_accepted_tag_count,
            "auto_accepted_identity_count": auto_accepted_identity_count,
            "folder_inheritance_recovered": recovered,
            "folder_inheritance_unresolved": int(inheritance["unresolved"]),
            "folder_inheritance_failures": list(inheritance["failures"]),
            "folder_inheritance_failures_total": int(inheritance["failures_total"]),
            "folder_inheritance_failures_truncated": bool(
                inheritance["failures_truncated"]
            ),
            "pending_count": pending_count,
            "proposals": proposals,
        }

    def _resolve_model_annotation(
        self,
        *,
        content_hash: str,
        entries: list[dict[str, Any]],
        context: TaggingContext,
        model: str,
        accounting: _RunAccounting,
        processed: int,
        total: int,
    ) -> _ModelResolution:
        prepared = self._prepare_model_annotation(
            content_hash=content_hash,
            entries=entries,
            context=context,
            model=model,
            accounting=accounting,
        )
        if isinstance(prepared, _ModelResolution):
            return prepared
        self.progress(f"Auto-tagged {processed}/{total} unique images with {model}.")
        try:
            return self._finalize_model_request(
                self._execute_model_request(prepared),
                accounting,
            )
        except BaseException:
            accounting.release_reservation(prepared.reserved_cost_cny)
            prepared.flight.release()
            raise

    def _prepare_model_annotation(
        self,
        *,
        content_hash: str,
        entries: list[dict[str, Any]],
        context: TaggingContext,
        model: str,
        accounting: _RunAccounting,
    ) -> _ModelResolution | _PreparedModelRequest:
        """Prepare one model request on the Collection owner thread.

        Cache, path resolution and budget reservation stay on the owner thread;
        only image encoding and HTTP are handed to network workers.
        """

        cache_key = _cache_key(content_hash, model)
        while True:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return _cached_model_resolution(cached, context, model, cache_key)

            flight = self.cache.claim(cache_key)
            if flight.is_leader:
                # A different leader may have committed after our miss and released
                # just before this claim. Re-read before spending budget or issuing
                # another model request.
                cached = self.cache.get(cache_key)
                if cached is not None:
                    flight.release()
                    return _cached_model_resolution(cached, context, model, cache_key)
                break

            cached = self.cache.wait_for_result(
                flight,
                cancel_check=self.cancel_check,
            )
            if cached is not None:
                return _cached_model_resolution(cached, context, model, cache_key)
            # Retryable/systemic failures and pre-request exits are intentionally
            # not durable cache entries. Compete for a fresh leader claim instead
            # of issuing simultaneous duplicate requests.

        reserved_cost = 0.0
        try:
            tracker, reserved_cost = accounting.create_tracker(model)
            representative = entries[0]
            path = self.source_resolver.resolve_fields(representative)
            pricing = _pricing_for_model(model, self.config.model_configuration)
            return _PreparedModelRequest(
                model=model,
                cache_key=cache_key,
                content_hash=content_hash,
                context=context,
                path=path,
                config=VisionTaggingConfig(
                    model=model,
                    api_key=self.config.api_key,
                    max_image_bytes=self.config.max_image_bytes,
                    max_source_image_bytes=self.config.max_source_image_bytes,
                    pricing=pricing,
                    concurrency=self.config.auto_tag_concurrency,
                    expected_source_sha256=content_hash,
                    rate_limit_requests_per_minute=(
                        self.config.rate_limit_requests_per_minute
                    ),
                    rate_limit_tokens_per_minute=(
                        self.config.rate_limit_tokens_per_minute
                    ),
                    rate_limit_hard_requests_per_minute=(
                        self.config.rate_limit_hard_requests_per_minute
                    ),
                    rate_limit_hard_tokens_per_minute=(
                        self.config.rate_limit_hard_tokens_per_minute
                    ),
                    rate_limit_max_concurrency=self.config.rate_limit_max_concurrency,
                ),
                tracker=tracker,
                flight=flight,
                reserved_cost_cny=reserved_cost,
            )
        except TaggingBudgetExceeded as exc:
            accounting.release_reservation(reserved_cost)
            flight.release()
            return _ModelResolution(
                model=model,
                cache_key=cache_key,
                status="budget_exhausted",
                error=str(exc),
                budget_exhausted=True,
            )
        except (ConfigurationError, OSError, ValueError) as exc:
            accounting.release_reservation(reserved_cost)
            flight.release()
            return _ModelResolution(
                model=model,
                cache_key=cache_key,
                status="failed",
                error=str(exc) or exc.__class__.__name__,
            )
        except BaseException:
            accounting.release_reservation(reserved_cost)
            flight.release()
            raise

    @staticmethod
    def _execute_model_request(
        prepared: _PreparedModelRequest,
    ) -> _CompletedModelRequest:
        """Run image encoding and HTTP without touching Collection state."""

        client = DashScopeVisionTaggingClient(
            prepared.config,
            budget_tracker=prepared.tracker,
        )
        try:
            response = client.tag_image(prepared.path, context=prepared.context)
        except TaggingBudgetExceeded as exc:
            return _CompletedModelRequest(
                prepared=prepared,
                error=str(exc),
                budget_exhausted=True,
                http_attempts=max(0, int(getattr(client, "request_count", 0))),
            )
        except VisionTaggingError as exc:
            return _CompletedModelRequest(
                prepared=prepared,
                error=str(exc) or exc.__class__.__name__,
                # Real clients expose an exact count. Local encoding/SHA
                # failures happen before HTTP and must remain zero-cost.
                http_attempts=max(0, int(getattr(client, "request_count", 1))),
                failure_kind=_vision_failure_kind(exc),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            # File disappearance, image encoding failures and malformed remote
            # responses are item-scoped. The owner thread records the failure and
            # continues with the remaining SHA groups.
            return _CompletedModelRequest(
                prepared=prepared,
                error=str(exc) or exc.__class__.__name__,
                http_attempts=max(1, int(getattr(client, "request_count", 1))),
                failure_kind="item",
            )
        return _CompletedModelRequest(
            prepared=prepared,
            response=response,
            http_attempts=max(1, int(getattr(client, "request_count", 1))),
        )

    def _finalize_model_request(
        self,
        completed: _CompletedModelRequest,
        accounting: _RunAccounting,
    ) -> _ModelResolution:
        """Persist a worker result on the Collection owner thread."""

        prepared = completed.prepared
        try:
            return self._persist_model_request(completed, accounting)
        finally:
            # Publish to SQLite before waking followers. Idempotent release also
            # covers persistence/accounting exceptions without stranding a job.
            prepared.flight.release()

    def _persist_model_request(
        self,
        completed: _CompletedModelRequest,
        accounting: _RunAccounting,
    ) -> _ModelResolution:
        prepared = completed.prepared
        if completed.budget_exhausted:
            accounting.release_reservation(prepared.reserved_cost_cny)
            return _ModelResolution(
                model=prepared.model,
                cache_key=prepared.cache_key,
                status="budget_exhausted",
                error=completed.error,
                budget_exhausted=True,
            )
        if completed.response is None:
            accounting.record_request(
                prepared.model,
                reserved_cost_cny=prepared.reserved_cost_cny,
                attempts=completed.http_attempts,
            )
            if completed.failure_kind == "item":
                self.cache.set(
                    cache_key=prepared.cache_key,
                    sha256=prepared.content_hash,
                    model=prepared.model,
                    prompt_version=PROMPT_VERSION,
                    schema_version=AUTO_TAGGING_SCHEMA_VERSION,
                    status="failed",
                    error=completed.error,
                )
            return _ModelResolution(
                model=prepared.model,
                cache_key=prepared.cache_key,
                status="failed",
                error=completed.error,
                failure_kind=completed.failure_kind,
            )

        response = completed.response
        accounting.record_request(
            prepared.model,
            response,
            reserved_cost_cny=prepared.reserved_cost_cny,
            attempts=completed.http_attempts,
        )
        annotation = _annotation_to_dict(response.annotation)
        self.cache.set(
            cache_key=prepared.cache_key,
            sha256=prepared.content_hash,
            model=prepared.model,
            prompt_version=PROMPT_VERSION,
            schema_version=AUTO_TAGGING_SCHEMA_VERSION,
            status="succeeded",
            annotation=annotation,
            request_id=response.request_id,
            usage=response.usage.raw,
            cost_yuan=response.cost_yuan,
        )
        return _ModelResolution(
            model=prepared.model,
            cache_key=prepared.cache_key,
            status="succeeded",
            # Keep the shared cache independent of Collection metadata.  The
            # model response is rechecked locally for every current context so
            # a name supported by an old folder/manual tag cannot leak into a
            # proposal in another Collection with the same image bytes.
            annotation=_revalidate_annotation_for_context(
                annotation,
                prepared.context,
            ),
            request_id=response.request_id,
        )

    def _resolve_model_wave(
        self,
        work_items: list[tuple[str, list[dict[str, Any]], TaggingContext]],
        *,
        model: str,
        accounting: _RunAccounting,
        freshness_validator: Callable[[str], bool] | None = None,
    ) -> dict[str, _ModelResolution]:
        """Resolve a bounded wave of independent SHA requests concurrently."""

        if not work_items:
            return {}
        workers = max(1, int(getattr(self.config, "auto_tag_concurrency", 2)))
        max_pending = max(workers, workers * 2)
        results: dict[str, _ModelResolution] = {}
        active: dict[
            Future[_CompletedModelRequest], tuple[str, _PreparedModelRequest, int]
        ] = {}
        pending_bytes = 0
        next_index = 0
        last_progress_at = 0.0

        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"zvec-{model}",
        )
        try:
            while next_index < len(work_items) or active:
                self.cancel_check()
                while next_index < len(work_items) and len(active) < max_pending:
                    content_hash, entries, context = work_items[next_index]
                    estimated_bytes = _stream_request_bytes(entries[0])
                    if (
                        active
                        and pending_bytes + estimated_bytes
                        > self.config.max_inflight_request_bytes
                    ):
                        break
                    next_index += 1
                    if freshness_validator is not None and not freshness_validator(
                        content_hash
                    ):
                        results[content_hash] = _ModelResolution(
                            model=model,
                            cache_key=_cache_key(content_hash, model),
                            status="failed",
                            error=(
                                "Source image changed while auto-tagging; "
                                "run the pipeline again."
                            ),
                            failure_kind="retryable",
                        )
                        continue
                    prepared = self._prepare_model_annotation(
                        content_hash=content_hash,
                        entries=entries,
                        context=context,
                        model=model,
                        accounting=accounting,
                    )
                    if isinstance(prepared, _ModelResolution):
                        results[content_hash] = prepared
                        continue
                    try:
                        future = executor.submit(self._execute_model_request, prepared)
                    except BaseException:
                        accounting.release_reservation(prepared.reserved_cost_cny)
                        prepared.flight.release()
                        raise
                    active[future] = (content_hash, prepared, estimated_bytes)
                    pending_bytes += estimated_bytes

                if not active:
                    continue
                completed_futures, _pending = wait(
                    active,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                # Do not let cancellation that arrives during the bounded wait
                # leak a completed response into cache or Collection state.
                self.cancel_check()
                for future in completed_futures:
                    content_hash, prepared, estimated_bytes = active.pop(future)
                    pending_bytes = max(0, pending_bytes - estimated_bytes)
                    try:
                        try:
                            completed = future.result()
                        except Exception as exc:  # pragma: no cover - defensive
                            completed = _CompletedModelRequest(
                                prepared=prepared,
                                error=str(exc) or exc.__class__.__name__,
                                failure_kind="systemic",
                            )
                        if (
                            freshness_validator is not None
                            and not completed.budget_exhausted
                            and not freshness_validator(content_hash)
                        ):
                            accounting.record_request(
                                prepared.model,
                                completed.response,
                                reserved_cost_cny=prepared.reserved_cost_cny,
                                attempts=completed.http_attempts,
                            )
                            results[content_hash] = _ModelResolution(
                                model=prepared.model,
                                cache_key=prepared.cache_key,
                                status="failed",
                                error=(
                                    "Source image changed while auto-tagging; "
                                    "run the pipeline again."
                                ),
                                failure_kind="retryable",
                            )
                        else:
                            results[content_hash] = self._finalize_model_request(
                                completed,
                                accounting,
                            )
                    except BaseException:
                        accounting.release_reservation(prepared.reserved_cost_cny)
                        raise
                    finally:
                        prepared.flight.release()
                    now = monotonic()
                    if now - last_progress_at >= 0.25 or len(results) == len(
                        work_items
                    ):
                        self.progress(
                            f"Resolved {len(results)}/{len(work_items)} unique "
                            f"images with {model}."
                        )
                        last_progress_at = now
        except BaseException:
            # Requests already inside urllib cannot be forcefully interrupted,
            # but they are pure worker-side network work.  Return control to the
            # caller immediately and discard late responses instead of waiting
            # for ThreadPoolExecutor.__exit__ to join them.
            for future, (_content_hash, prepared, _estimated_bytes) in active.items():
                accounting.release_reservation(prepared.reserved_cost_cny)
                _release_flight_when_done(future, prepared.flight)
            active.clear()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        return results

    def _store_resolution_failure(
        self,
        entries: list[dict[str, Any]],
        content_hash: str,
        resolution: _ModelResolution,
        selected_model: str,
        model_steps: list[_ModelResolution],
    ) -> None:
        error = resolution.error or "Visual annotation is unavailable."
        failure_category = _annotation_failure_category(
            error,
            resolution.failure_kind,
        )
        retry_eligible = _failure_category_is_retryable(failure_category)
        policy = {
            "policy_version": POLICY_VERSION,
            "selected_model": selected_model,
            "model_chain": [step.model for step in model_steps],
            "model_trace": [step.model for step in model_steps],
            "resolved_model": "",
            "model_steps": [_model_step(step) for step in model_steps],
            "escalated": len(model_steps) > 1,
            "requires_review": True,
            "review_required": True,
            "review_reasons": ["model_request_failed"],
            "plus_recommended": False,
            "entity_confidence_threshold": HIGH_CONFIDENCE_ENTITY_THRESHOLD,
            "failure_category": failure_category,
            "retry_eligible": retry_eligible,
        }
        for entry in entries:
            previous = self.state.get_document_annotation(str(entry["doc_id"]))
            rejected_tags = (
                tuple(previous.get("rejected_tags", ()))
                if previous is not None
                and str(previous.get("source_sha256") or "")
                == str(entry.get("sha256") or "")
                else ()
            )
            self.state.set_document_annotation(
                doc_id=str(entry["doc_id"]),
                source_sha256=content_hash,
                cache_key=resolution.cache_key,
                status="failed",
                accepted_tags=entry.get("accepted_auto_tags", ()),
                rejected_tags=rejected_tags,
                structured={},
                policy=policy,
                error=error,
            )

    def _capture_resolution_failures(
        self,
        sink: FailureSink,
        entries: list[dict[str, Any]],
        resolution: _ModelResolution,
    ) -> list[Any]:
        """Record per-document failures while quarantining only bad media.

        Provider throttling, authentication failures and temporary outages describe
        the request rather than the source image, so those failures remain in the
        manifest without copying otherwise healthy files into the quarantine store.
        """

        captures = []
        for entry in entries:
            try:
                path = self.source_resolver.resolve_fields(entry)
            except (ConfigurationError, OSError, ValueError):
                # Preserve the original Collection identity in the manifest even if
                # the configured source root is currently unavailable.
                path = Path(
                    str(entry.get("absolute_path") or entry.get("relative_path") or "")
                )
            captures.append(
                sink.capture(
                    path=path,
                    error=resolution.error or "Visual annotation is unavailable.",
                    kind=resolution.failure_kind,
                    stage="auto_tag",
                    sha256_hex=str(entry.get("sha256") or ""),
                    quarantine=resolution.failure_kind == "item",
                    metadata={
                        "doc_id": str(entry.get("doc_id") or ""),
                        "relative_path": str(entry.get("relative_path") or ""),
                        "model": resolution.model,
                        "cached": resolution.cached,
                        "failure_category": _annotation_failure_category(
                            resolution.error,
                            resolution.failure_kind,
                        ),
                    },
                )
            )
        return captures

    def _inherit_content_policy_failures(
        self,
        entries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve explicit model refusals from already labelled folder peers.

        This intentionally runs only after the model wave and normal persistence
        loop have finished.  It never handles transport/auth/schema failures and
        never calls a model.  Folder identity includes ``root_id`` so equal folder
        names in different roots cannot leak tags into each other.
        """

        candidates = {
            str(entry.get("doc_id") or ""): entry
            for entry in entries
            if str(entry.get("doc_id") or "")
        }
        if not candidates:
            return {
                "recovered": 0,
                "unresolved": 0,
                "failures": [],
                "failures_total": 0,
                "failures_truncated": False,
                "proposals": [],
            }

        requested_folders = sorted(
            {
                (
                    str(entry.get("root_id") or ""),
                    _folder_display_path(entry),
                )
                for entry in candidates.values()
            },
            key=lambda value: (value[0], _comparison_key(value[1])),
        )
        target_doc_ids = frozenset(candidates)
        memory_budget = _FolderInheritanceMemoryBudget()
        donors_by_folder: dict[tuple[str, str], _FolderInheritanceAggregate] = {}

        def consume_donor(
            member: Mapping[str, Any],
            member_annotation: Mapping[str, Any] | None,
        ) -> None:
            identity = _folder_identity(member)
            aggregate = donors_by_folder.get(identity)
            if aggregate is None:
                # Persist summaries only after an eligible donor is observed;
                # folders containing only failed/unlabelled targets allocate no
                # retained audit lists.
                candidate = _FolderInheritanceAggregate(
                    target_doc_ids,
                    memory_budget,
                )
                if candidate.consume(member, member_annotation):
                    donors_by_folder[identity] = candidate
                return
            aggregate.consume(member, member_annotation)

        folder_reader = getattr(
            self.state,
            "iter_entries_for_folders_with_annotations",
            None,
        )
        if not callable(folder_reader):
            raise ConfigurationError(
                "Folder inheritance requires the bounded folder annotation reader."
            )
        for batch in folder_reader(
            requested_folders,
            row_batch_size=FOLDER_INHERITANCE_ROW_BATCH_SIZE,
        ):
            self.cancel_check()
            for member, member_annotation in batch:
                consume_donor(member, member_annotation)

        recovered = 0
        unresolved = 0
        failure_count = 0
        failures: list[dict[str, str]] = []
        proposals: list[dict[str, Any]] = []

        def record_failure(item: dict[str, str]) -> None:
            nonlocal failure_count
            failure_count += 1
            if len(failures) < FOLDER_INHERITANCE_AUDIT_LIMIT:
                failures.append(item)

        current_entries = self.state.get_many(candidates)
        current_annotations = self.state.get_document_annotations(candidates)
        for doc_id in sorted(candidates):
            self.cancel_check()
            entry = current_entries.get(doc_id)
            annotation = current_annotations.get(doc_id)
            if entry is None or annotation is None:
                unresolved += 1
                record_failure(
                    {
                        "doc_id": doc_id,
                        "code": "folder_inheritance_target_missing",
                        "error": "The failed image no longer exists in index state.",
                    }
                )
                continue
            policy = dict(annotation.get("policy") or {})
            failure_category = str(policy.get("failure_category") or "").strip()
            if not failure_category:
                failure_category = _annotation_failure_category(
                    str(annotation.get("error") or ""),
                    "item",
                )
            if (
                annotation.get("status") != "failed"
                or str(annotation.get("source_sha256") or "")
                != str(entry.get("sha256") or "")
                or failure_category != "content_policy"
            ):
                # The target changed or was independently resolved after the
                # model pass.  Never overwrite that newer state.
                continue

            aggregate = donors_by_folder.get(_folder_identity(entry))
            inheritance = (
                aggregate.payload(exclude_doc_id=doc_id)
                if aggregate is not None
                else _empty_folder_inheritance_payload()
            )
            rejected_keys = {
                _comparison_key(tag)
                for tag in _iter_strings(annotation.get("rejected_tags"))
            }
            inherited_tags: list[str] = []
            filtered_tags = list(inheritance["filtered_tags"])
            filtered_total = int(inheritance["filtered_tags_total"])
            for tag in inheritance["accepted_tags"]:
                if _comparison_key(tag) in rejected_keys:
                    filtered_total += 1
                    if len(filtered_tags) < FOLDER_INHERITANCE_AUDIT_LIMIT:
                        filtered_tags.append(
                            {
                                "tag": tag,
                                "category": "user_suppressed",
                                "source": "target_annotation",
                                "source_doc_id": doc_id,
                                "reason": "target_rejected_tag",
                            }
                        )
                    continue
                inherited_tags.append(tag)

            audit: dict[str, Any] = {
                "schema_version": FOLDER_INHERITANCE_POLICY_VERSION,
                "blocked_categories": sorted(FOLDER_INHERITANCE_BLOCKED_CATEGORIES),
                "folder": _folder_display_path(entry),
                "source_doc_ids": list(inheritance["source_doc_ids"]),
                "source_doc_ids_total": int(inheritance["source_doc_ids_total"]),
                "source_doc_ids_truncated": bool(
                    inheritance["source_doc_ids_truncated"]
                ),
                "source_relative_paths": list(inheritance["source_relative_paths"]),
                "source_relative_paths_total": int(
                    inheritance["source_relative_paths_total"]
                ),
                "source_relative_paths_truncated": bool(
                    inheritance["source_relative_paths_truncated"]
                ),
                "source_tags": dict(inheritance["source_tags"]),
                "source_tags_total": dict(inheritance["source_tags_total"]),
                "source_tags_omitted": dict(inheritance["source_tags_omitted"]),
                "source_tags_truncated": dict(inheritance["source_tags_truncated"]),
                "accepted_tags": list(normalize_tags(inherited_tags)),
                "filtered_tags": filtered_tags,
                "filtered_tags_total": filtered_total,
                "filtered_tags_truncated": filtered_total > len(filtered_tags),
                "original_failure_category": failure_category,
                "original_error": str(annotation.get("error") or ""),
                "original_review_reasons": list(policy.get("review_reasons", ())),
            }
            if not inherited_tags:
                audit.update(
                    {
                        "status": "failed",
                        "error_code": "no_inheritable_folder_tags",
                    }
                )
                message = (
                    "Folder inheritance could not resolve the model refusal: "
                    "no inheritable tags were found after action/expression "
                    "filtering."
                )
                self._update_failed_folder_inheritance(
                    entry=entry,
                    annotation=annotation,
                    policy=policy,
                    audit=audit,
                    error=message,
                )
                unresolved += 1
                record_failure(
                    {
                        "doc_id": doc_id,
                        "code": "no_inheritable_folder_tags",
                        "error": message,
                    }
                )
                continue

            normalized_inherited = normalize_tags(
                [*entry.get("inherited_tags", ()), *inherited_tags]
            )
            audit["status"] = "applied"
            resolved_policy = {
                **policy,
                "requires_review": False,
                "review_required": False,
                "review_reasons": [],
                "retry_eligible": False,
                "resolved_by": "folder_inheritance",
                "folder_inheritance": audit,
            }
            annotation_to_write = {
                **annotation,
                "policy": resolved_policy,
                "error": "",
            }
            try:
                self._write_review_document(
                    entry=entry,
                    annotation=annotation_to_write,
                    accepted_tags=entry.get("accepted_auto_tags", ()),
                    inherited_tags=normalized_inherited,
                    rejected_tags=annotation.get("rejected_tags", ()),
                    proposed_tags=(),
                    status="accepted",
                )
            except Exception as exc:
                error_text = str(exc) or exc.__class__.__name__
                audit.update(
                    {
                        "status": "failed",
                        "error_code": "folder_inheritance_write_failed",
                        "write_error": error_text,
                    }
                )
                self._update_failed_folder_inheritance(
                    entry=entry,
                    annotation=annotation,
                    policy=policy,
                    audit=audit,
                    error=f"Folder inheritance failed to persist tags: {error_text}",
                )
                unresolved += 1
                record_failure(
                    {
                        "doc_id": doc_id,
                        "code": "folder_inheritance_write_failed",
                        "error": error_text,
                    }
                )
                continue

            recovered += 1
            stored = self.state.get_document_annotation(doc_id)
            if stored is not None and len(proposals) < AUTO_TAG_RESULT_PROPOSAL_LIMIT:
                proposals.append(self._stored_proposal(stored))
            self.progress(
                f"Recovered refused image {recovered}/{len(candidates)} from "
                "same-folder labels."
            )

        return {
            "recovered": recovered,
            "unresolved": unresolved,
            "failures": failures,
            "failures_total": failure_count,
            "failures_truncated": failure_count > len(failures),
            "proposals": proposals,
        }

    def _folder_inheritance_payload(
        self,
        donors: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compatibility helper for tests and older state adapters.

        The production path consumes SQLite's already ordered pages directly.
        This helper deliberately avoids sorting or retaining the input iterable.
        """

        aggregate = _FolderInheritanceAggregate(frozenset())
        for donor in donors:
            donor_id = str(donor.get("doc_id") or "")
            joined_annotation = (
                donor.get("_joined_annotation")
                if "_joined_annotation" in donor
                else self.state.get_document_annotation(donor_id)
            )
            annotation = (
                dict(joined_annotation)
                if isinstance(joined_annotation, Mapping)
                else None
            )
            aggregate.consume(donor, annotation)
        return aggregate.payload()

    def _update_failed_folder_inheritance(
        self,
        *,
        entry: dict[str, Any],
        annotation: dict[str, Any],
        policy: dict[str, Any],
        audit: dict[str, Any],
        error: str,
    ) -> None:
        original_error = str(annotation.get("error") or "").strip()
        combined_error = f"{original_error} {error}".strip()
        self.state.set_document_annotation(
            doc_id=str(entry["doc_id"]),
            source_sha256=str(entry["sha256"]),
            cache_key=annotation.get("cache_key"),
            status="failed",
            proposed_tags=annotation.get("proposed_tags", ()),
            accepted_tags=entry.get("accepted_auto_tags", ()),
            rejected_tags=annotation.get("rejected_tags", ()),
            description=str(annotation.get("description") or ""),
            entities=dict(annotation.get("entities") or {}),
            warnings=annotation.get("warnings", ()),
            structured=dict(annotation.get("structured") or {}),
            policy={**policy, "folder_inheritance": audit},
            error=combined_error,
        )

    def review(self, decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        values = list(decisions)
        if not values:
            raise AutoTaggingRequestError("At least one review decision is required.")
        accepted_count = 0
        rejected_count = 0
        failures: list[dict[str, str]] = []
        changed = False

        for decision in values:
            self.cancel_check()
            try:
                result = self._review_one(decision)
            except Exception as exc:
                failures.append(
                    {
                        "doc_id": str(decision.get("doc_id") or ""),
                        "error": str(exc) or exc.__class__.__name__,
                    }
                )
                continue
            changed = True
            if result == "accepted":
                accepted_count += 1
            else:
                rejected_count += 1

        if changed:
            self.repository.set_tag_catalog(self.state.list_effective_tags())
        pending_count, pending = self.state.page_document_annotations(
            "pending_review", offset=0, limit=50
        )
        return {
            "accepted": accepted_count,
            "rejected": rejected_count,
            "updated": accepted_count + rejected_count,
            "failed": len(failures),
            "failures": failures,
            "pending_count": pending_count,
            "proposals": [self._stored_proposal(item) for item in pending],
        }

    def review_batch(
        self,
        *,
        proposal_ids: Iterable[str],
        accepted_tags_by_proposal: Mapping[str, Any] | None = None,
        exclude_identity_tags: bool = True,
        acceptance_mode: str = "low_risk_only",
        batch_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Atomically accept a server-validated batch of current proposals.

        Legacy callers keep the original ``low_risk_only`` behaviour.  A newer
        client can confirm one whole batch and include recommended identity
        proposals; conflicting identities are still removed by the service,
        regardless of what the client sends.
        """

        mode = _normalize_batch_acceptance_mode(acceptance_mode)
        if not isinstance(exclude_identity_tags, bool):
            raise AutoTaggingRequestError("exclude_identity_tags must be a boolean.")
        if exclude_identity_tags != (mode == "low_risk_only"):
            raise AutoTaggingRequestError(
                "exclude_identity_tags conflicts with acceptance_mode."
            )
        if not isinstance(batch_confirmation, bool):
            raise AutoTaggingRequestError("batch_confirmation must be a boolean.")
        if mode != "low_risk_only" and not batch_confirmation:
            raise AutoTaggingRequestError(
                "Identity-inclusive batch review requires one explicit batch "
                "confirmation."
            )
        doc_ids = _validate_proposal_ids(proposal_ids)
        requested_by_id = _validate_accepted_tags_by_proposal(
            accepted_tags_by_proposal, doc_ids
        )
        self._recover_incomplete_review_batch()

        plans: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        identity_excluded: list[dict[str, Any]] = []
        accepted_tag_count = 0
        accepted_identity_count = 0
        for doc_id in doc_ids:
            self.cancel_check()
            entry, annotation = self._current_review_document(doc_id)
            if annotation["status"] != "pending_review":
                raise AutoTaggingRequestError(
                    f"Proposal is no longer pending review: {doc_id}"
                )
            metadata = _proposal_tag_metadata(
                entry,
                dict(annotation.get("structured") or {}),
                annotation.get("proposed_tags", ()),
            )
            low_risk = tuple(metadata["low_risk_tags"])
            identity = tuple(metadata["identity_tags"])
            conflicting_identity = tuple(metadata["conflicting_identity_tags"])
            identity_keys = {_comparison_key(tag) for tag in identity}
            conflict_keys = {_comparison_key(tag) for tag in conflicting_identity}
            proposed = tuple(annotation.get("proposed_tags", ()))
            if mode == "low_risk_only":
                default_requested = low_risk
            elif mode == "recommended":
                default_requested = normalize_tags(
                    [
                        *low_risk,
                        *(
                            tag
                            for tag in identity
                            if _comparison_key(tag) not in conflict_keys
                        ),
                    ]
                )
            else:
                default_requested = normalize_tags(
                    tag for tag in proposed if _comparison_key(tag) not in conflict_keys
                )
            requested = requested_by_id.get(doc_id, default_requested)
            requested_tags = _sanitize_review_tags(requested)
            requested_keys = {_comparison_key(tag) for tag in requested_tags}
            identity_by_key = {_comparison_key(tag): tag for tag in identity}
            excluded_keys = (
                requested_keys & identity_keys
                if mode == "low_risk_only"
                else requested_keys & conflict_keys
            )
            excluded = [
                identity_by_key[key] for key in excluded_keys if key in identity_by_key
            ]
            if excluded:
                identity_excluded.append(
                    {
                        "proposal_id": doc_id,
                        "tags": _stable_strings(excluded),
                        "reason": (
                            "legacy_identity_exclusion"
                            if mode == "low_risk_only"
                            else "identity_conflict"
                        ),
                    }
                )
            selected = tuple(
                tag
                for tag in requested_tags
                if _comparison_key(tag) not in excluded_keys
            )
            low_risk_keys = {_comparison_key(tag) for tag in low_risk}
            proposed_keys = {_comparison_key(tag) for tag in proposed}
            already_accepted_keys = {
                _comparison_key(tag) for tag in entry.get("accepted_auto_tags", ())
            }
            if mode == "low_risk_only":
                allowed_keys = low_risk_keys - identity_keys
            elif mode == "recommended":
                allowed_keys = low_risk_keys | (identity_keys - conflict_keys)
            else:
                allowed_keys = proposed_keys - conflict_keys
            invalid = [
                tag
                for tag in selected
                if _comparison_key(tag) not in allowed_keys
                and _comparison_key(tag) not in already_accepted_keys
            ]
            if invalid:
                raise AutoTaggingRequestError(
                    "Batch review contains tags that are not eligible under "
                    f"{mode}; invalid tags for "
                    f"{doc_id}: {', '.join(invalid)}"
                )
            newly_selected = tuple(
                tag
                for tag in selected
                if _comparison_key(tag) not in already_accepted_keys
            )
            if not newly_selected:
                continue
            accepted_tags = normalize_tags(
                [*entry.get("accepted_auto_tags", ()), *newly_selected]
            )
            accepted_keys = {_comparison_key(tag) for tag in accepted_tags}
            remaining = tuple(
                tag
                for tag in annotation.get("proposed_tags", ())
                if _comparison_key(tag) not in accepted_keys
            )
            plans.append(
                {
                    "doc_id": doc_id,
                    "entry": entry,
                    "annotation": annotation,
                    "accepted_tags": accepted_tags,
                    "remaining_tags": remaining,
                    "status": "pending_review" if remaining else "accepted",
                }
            )
            snapshots.append(_review_snapshot(entry, annotation))
            accepted_tag_count += len(newly_selected)
            accepted_identity_count += sum(
                1 for tag in newly_selected if _comparison_key(tag) in identity_keys
            )

        if not plans:
            latest = self.state.latest_auto_tag_review_batch()
            excluded_count = sum(len(item["tags"]) for item in identity_excluded)
            return {
                "batch_id": "",
                "accepted": 0,
                "rejected": 0,
                "updated": 0,
                "failed": 0,
                "failures": [],
                "accepted_tag_count": 0,
                "accepted_identity_count": 0,
                "acceptance_mode": mode,
                "identity_excluded": excluded_count,
                "identity_excluded_count": excluded_count,
                "identity_exclusions": identity_excluded,
                "undo_available": self._review_batch_is_undoable(latest),
                "pending_count": self.state.count_document_annotations(
                    "pending_review"
                ),
            }

        batch_id = uuid.uuid4().hex
        self.state.create_auto_tag_review_batch(
            batch_id=batch_id,
            snapshots=snapshots,
        )
        try:
            for plan in plans:
                self.cancel_check()
                self._write_review_document(
                    entry=plan["entry"],
                    annotation=plan["annotation"],
                    accepted_tags=plan["accepted_tags"],
                    rejected_tags=plan["annotation"].get("rejected_tags", ()),
                    proposed_tags=plan["remaining_tags"],
                    status=plan["status"],
                )
            self._finalize_review_repository()
            after_snapshots = [self._current_snapshot(plan["doc_id"]) for plan in plans]
            excluded_count = sum(len(item["tags"]) for item in identity_excluded)
            result = {
                "batch_id": batch_id,
                "accepted": len(plans),
                "rejected": 0,
                "updated": len(plans),
                "failed": 0,
                "failures": [],
                "accepted_tag_count": accepted_tag_count,
                "accepted_identity_count": accepted_identity_count,
                "acceptance_mode": mode,
                "identity_excluded": excluded_count,
                "identity_excluded_count": excluded_count,
                "identity_exclusions": identity_excluded,
                "undo_available": True,
                "pending_count": self.state.count_document_annotations(
                    "pending_review"
                ),
                "after_snapshots": after_snapshots,
            }
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="applied",
                result=result,
            )
        except Exception as exc:
            self._roll_back_review_batch(batch_id, snapshots, exc)
            raise AutoTaggingRequestError(
                f"Batch review failed and was rolled back: {exc}"
            ) from exc

        public_result = dict(result)
        public_result.pop("after_snapshots", None)
        return public_result

    def undo_latest_review_batch(self) -> dict[str, Any]:
        latest = self.state.latest_auto_tag_review_batch()
        if latest is None:
            return {
                "undone": False,
                "already_undone": False,
                "batch_id": "",
                "updated": 0,
                "undo_available": False,
                "pending_count": self.state.count_document_annotations(
                    "pending_review"
                ),
                "reason": "no_batch",
            }
        batch_id = str(latest["batch_id"])
        status = str(latest["status"])
        snapshots = list(latest.get("snapshots") or ())
        if status == "undone":
            return {
                "undone": False,
                "already_undone": True,
                "batch_id": batch_id,
                "updated": 0,
                "undo_available": False,
                "pending_count": self.state.count_document_annotations(
                    "pending_review"
                ),
            }
        if status in {"rolled_back", "rollback_failed"}:
            return {
                "undone": False,
                "already_undone": False,
                "batch_id": batch_id,
                "updated": 0,
                "undo_available": False,
                "pending_count": self.state.count_document_annotations(
                    "pending_review"
                ),
                "reason": status,
            }
        if status not in {"applied", "applying", "undoing", "undo_failed"}:
            raise AutoTaggingRequestError(
                f"Latest batch cannot be undone while it is {status}."
            )

        result = dict(latest.get("result") or {})
        after_snapshots = list(result.get("after_snapshots") or ())
        if status == "applied":
            if len(after_snapshots) != len(snapshots):
                raise AutoTaggingRequestError(
                    "The latest batch lacks a complete undo checkpoint."
                )
            changed = [
                str(snapshot.get("doc_id") or "")
                for snapshot in after_snapshots
                if not self._matches_current_snapshot(snapshot)
            ]
            if changed:
                raise AutoTaggingRequestError(
                    "Cannot undo because reviewed images changed after the batch: "
                    + ", ".join(changed[:10])
                )

        self.state.update_auto_tag_review_batch(
            batch_id,
            status="undoing",
            result=result,
        )
        try:
            self._restore_review_snapshots(snapshots)
            undone_result = {
                **result,
                "undone": True,
                "already_undone": False,
                "updated": len(snapshots),
                "undo_available": False,
            }
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="undone",
                result=undone_result,
            )
        except Exception as exc:
            try:
                if after_snapshots:
                    self._restore_review_snapshots(after_snapshots)
                self.state.update_auto_tag_review_batch(
                    batch_id,
                    status="undo_failed",
                    result=result,
                    error=str(exc),
                )
            except Exception as recovery_exc:
                raise AutoTaggingRequestError(
                    "Undo failed and the post-batch state could not be restored: "
                    f"{recovery_exc}"
                ) from recovery_exc
            raise AutoTaggingRequestError(
                f"Undo failed; the post-batch state was restored: {exc}"
            ) from exc
        return {
            "undone": True,
            "already_undone": False,
            "batch_id": batch_id,
            "updated": len(snapshots),
            "undo_available": False,
            "pending_count": self.state.count_document_annotations("pending_review"),
        }

    def _recover_incomplete_review_batch(self) -> None:
        latest = self.state.latest_auto_tag_review_batch()
        if latest is None or latest["status"] not in {
            "applying",
            "rolling_back",
            "rollback_failed",
        }:
            return
        batch_id = str(latest["batch_id"])
        try:
            self._restore_review_snapshots(list(latest.get("snapshots") or ()))
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="rolled_back",
                result=dict(latest.get("result") or {}),
                error="Recovered an incomplete batch before starting a new batch.",
            )
        except Exception as exc:
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="rollback_failed",
                result=dict(latest.get("result") or {}),
                error=str(exc),
            )
            raise AutoTaggingRequestError(
                "An incomplete batch could not be recovered safely."
            ) from exc

    def _roll_back_review_batch(
        self,
        batch_id: str,
        snapshots: list[dict[str, Any]],
        original_error: Exception,
    ) -> None:
        try:
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="rolling_back",
                error=str(original_error),
            )
            self._restore_review_snapshots(snapshots)
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="rolled_back",
                error=str(original_error),
            )
        except Exception as rollback_error:
            self.state.update_auto_tag_review_batch(
                batch_id,
                status="rollback_failed",
                error=f"{original_error}; rollback failed: {rollback_error}",
            )
            raise AutoTaggingRequestError(
                "Batch review failed and rollback also failed."
            ) from rollback_error

    def _current_review_document(
        self, doc_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entry = self.state.get(doc_id)
        annotation = self.state.get_document_annotation(doc_id)
        if entry is None or annotation is None:
            raise AutoTaggingRequestError(f"The annotation no longer exists: {doc_id}")
        if annotation["source_sha256"] != entry["sha256"]:
            raise AutoTaggingRequestError(
                f"The image changed after annotation; annotate it again: {doc_id}"
            )
        return entry, annotation

    def _current_snapshot(self, doc_id: str) -> dict[str, Any]:
        entry, annotation = self._current_review_document(doc_id)
        return _review_snapshot(entry, annotation)

    def active_learning_snapshot(self, doc_id: str) -> dict[str, Any]:
        """Capture one current, restorable review snapshot without model I/O."""

        return self._current_snapshot(doc_id)

    def active_learning_matches_snapshot(self, snapshot: Mapping[str, Any]) -> bool:
        """Return whether a stored after-snapshot still matches current data."""

        return self._matches_current_snapshot(snapshot)

    def apply_active_learning_decision(
        self,
        decision: Mapping[str, Any],
    ) -> str:
        """Apply one human-confirmed learning decision without final optimize.

        Submitting an active-learning batch is the user's single confirmation
        for every selected tag. Identity proposals are still recomputed from
        server-owned annotation metadata; arbitrary client labels are never
        promoted to identity confirmations merely because the client says so.
        """

        payload = dict(decision)
        doc_id = str(payload.get("doc_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if action in {"accept", "edit"}:
            entry, annotation = self._current_review_document(doc_id)
            raw_tags = payload.get("tags")
            if raw_tags is None:
                raw_tags = annotation.get("proposed_tags", ())
            selected = _sanitize_review_tags(raw_tags)
            metadata = _proposal_tag_metadata(
                entry,
                dict(annotation.get("structured") or {}),
                annotation.get("proposed_tags", ()),
            )
            identity_keys = {_comparison_key(tag) for tag in metadata["identity_tags"]}
            payload["confirmed_identity_tags"] = [
                tag for tag in selected if _comparison_key(tag) in identity_keys
            ]
        return self._review_one(payload)

    def restore_active_learning_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> None:
        """Restore one validated snapshot; caller batches repository optimize."""

        entry = snapshot.get("entry")
        annotation = snapshot.get("annotation")
        if not isinstance(entry, Mapping) or not isinstance(annotation, Mapping):
            raise AutoTaggingRequestError("Active-learning snapshot is invalid.")
        self._write_review_document(
            entry=dict(entry),
            annotation=dict(annotation),
            accepted_tags=entry.get("accepted_auto_tags", ()),
            rejected_tags=annotation.get("rejected_tags", ()),
            proposed_tags=annotation.get("proposed_tags", ()),
            status=str(annotation.get("status") or "pending_review"),
        )

    def finalize_active_learning_changes(self) -> None:
        """Refresh the local tag catalog once after a learning batch."""

        self._finalize_review_repository()

    def apply_cluster_identity(
        self,
        doc_id: str,
        *,
        category: str,
        value: str,
    ) -> None:
        """Persist one human-selected identity as inherited metadata.

        Only the four identity categories are accepted. Action, expression and
        other transient visual properties cannot enter this propagation path.
        Existing vectors are reused for the Collection metadata update.
        """

        normalized_category = str(category).strip().casefold()
        normalized_value = str(value).strip()
        if normalized_category not in CLUSTER_IDENTITY_CATEGORIES:
            raise AutoTaggingRequestError("Cluster identity category is unsupported.")
        if not normalized_value:
            raise AutoTaggingRequestError("Cluster identity value must not be empty.")
        entry = self.state.get(str(doc_id).strip())
        if entry is None:
            raise AutoTaggingRequestError("The indexed image no longer exists.")
        annotation = self.state.get_document_annotation(str(entry["doc_id"]))
        if annotation is not None and (
            str(annotation.get("source_sha256") or "") != str(entry["sha256"])
        ):
            raise AutoTaggingRequestError(
                "The image changed after annotation; annotate it again."
            )
        if annotation is None:
            annotation = {
                "doc_id": str(entry["doc_id"]),
                "source_sha256": str(entry["sha256"]),
                "cache_key": None,
                "status": "accepted",
                "proposed_tags": [],
                "rejected_tags": [],
                "description": "",
                "entities": {},
                "warnings": [],
                "structured": {},
                "policy": {},
                "error": "",
            }
        structured = copy.deepcopy(dict(annotation.get("structured") or {}))
        raw_entities = structured.get("entities")
        entities = (
            copy.deepcopy(dict(raw_entities))
            if isinstance(raw_entities, Mapping)
            else {}
        )
        category_values = entities.get(normalized_category)
        normalized_entities = (
            list(category_values)
            if isinstance(category_values, Iterable)
            and not isinstance(category_values, (str, bytes, Mapping))
            else []
        )
        if not any(
            isinstance(entity, Mapping)
            and _comparison_key(str(entity.get("name") or ""))
            == _comparison_key(normalized_value)
            for entity in normalized_entities
        ):
            normalized_entities.append(
                {
                    "name": normalized_value,
                    "confidence": 1.0,
                    "state": "confirmed",
                    "source": "cluster_inherited",
                }
            )
        entities[normalized_category] = normalized_entities
        structured["entities"] = entities
        policy = dict(annotation.get("policy") or {})
        policy.update(
            {
                "cluster_identity_applied": True,
                "requires_review": False,
                "review_required": False,
            }
        )
        annotation_to_write = {
            **annotation,
            "structured": structured,
            "entities": entities,
            "policy": policy,
        }
        self._write_review_document(
            entry=entry,
            annotation=annotation_to_write,
            accepted_tags=entry.get("accepted_auto_tags", ()),
            rejected_tags=annotation.get("rejected_tags", ()),
            proposed_tags=annotation.get("proposed_tags", ()),
            status="accepted",
            inherited_tags=normalize_tags(
                [*entry.get("inherited_tags", ()), normalized_value]
            ),
        )

    def cluster_identity_snapshot(self, doc_id: str) -> dict[str, Any]:
        entry = self.state.get(str(doc_id).strip())
        if entry is None:
            raise AutoTaggingRequestError("The indexed image no longer exists.")
        annotation = self.state.get_document_annotation(str(entry["doc_id"]))
        return {
            "entry": dict(entry),
            "annotation": dict(annotation) if annotation is not None else None,
        }

    def cluster_identity_matches_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> bool:
        entry = snapshot.get("entry")
        if not isinstance(entry, Mapping):
            return False
        try:
            current = self.cluster_identity_snapshot(str(entry.get("doc_id") or ""))
        except AutoTaggingRequestError:
            return False
        return _cluster_identity_snapshot_state(current) == (
            _cluster_identity_snapshot_state(snapshot)
        )

    def restore_cluster_identity_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> None:
        entry = snapshot.get("entry")
        annotation = snapshot.get("annotation")
        if not isinstance(entry, Mapping):
            raise AutoTaggingRequestError("Cluster identity snapshot is invalid.")
        if isinstance(annotation, Mapping):
            self.restore_active_learning_snapshot(snapshot)
            return
        doc_id = str(entry.get("doc_id") or "")
        vector = self.repository.fetch_vector(doc_id)
        if vector is None:
            raise AutoTaggingRequestError(f"The indexed vector is missing: {doc_id}")
        restored_entry = dict(entry)
        record = _record_from_entry(restored_entry, self.source_resolver)
        effective_tags = normalize_tags(
            [
                *restored_entry.get("tags", ()),
                *restored_entry.get("folder_tags", ()),
                *restored_entry.get("accepted_auto_tags", ()),
                *restored_entry.get("inherited_tags", ()),
            ]
        )
        write_result = self.collection_writes.upsert(
            [
                PreparedCollectionUpsert(
                    record=record,
                    image_vector=vector,
                    effective_tags=effective_tags,
                    state_entry=restored_entry,
                )
            ],
            operation_kind="auto_tag_restore",
        )
        if write_result.failures or write_result.succeeded != [doc_id]:
            raise AutoTaggingRequestError(
                write_result.failures.get(
                    doc_id, "Failed to restore the Collection tags."
                )
            )
        self.state.delete_document_annotation(doc_id)

    def _matches_current_snapshot(self, snapshot: Mapping[str, Any]) -> bool:
        doc_id = str(snapshot.get("doc_id") or "")
        try:
            current = self._current_snapshot(doc_id)
        except AutoTaggingRequestError:
            return False
        return _semantic_review_snapshot(current) == _semantic_review_snapshot(snapshot)

    def _review_batch_is_undoable(self, batch: Mapping[str, Any] | None) -> bool:
        if batch is None or batch.get("status") != "applied":
            return False
        snapshots = batch.get("snapshots")
        result = batch.get("result")
        after_snapshots = (
            result.get("after_snapshots") if isinstance(result, Mapping) else None
        )
        if not isinstance(snapshots, list) or not isinstance(after_snapshots, list):
            return False
        return len(snapshots) == len(after_snapshots) and all(
            isinstance(snapshot, Mapping) and self._matches_current_snapshot(snapshot)
            for snapshot in after_snapshots
        )

    def _restore_review_snapshots(self, snapshots: Iterable[Mapping[str, Any]]) -> None:
        for snapshot in snapshots:
            entry = snapshot.get("entry")
            annotation = snapshot.get("annotation")
            if not isinstance(entry, Mapping) or not isinstance(annotation, Mapping):
                raise AutoTaggingRequestError("Review batch snapshot is invalid.")
            self._write_review_document(
                entry=dict(entry),
                annotation=dict(annotation),
                accepted_tags=entry.get("accepted_auto_tags", ()),
                rejected_tags=annotation.get("rejected_tags", ()),
                proposed_tags=annotation.get("proposed_tags", ()),
                status=str(annotation.get("status") or "pending_review"),
            )
        self._finalize_review_repository()

    def _write_review_document(
        self,
        *,
        entry: dict[str, Any],
        annotation: dict[str, Any],
        accepted_tags: Iterable[str],
        rejected_tags: Iterable[str],
        proposed_tags: Iterable[str],
        status: str,
        manual_tags: Iterable[str] | None = None,
        inherited_tags: Iterable[str] | None = None,
    ) -> None:
        doc_id = str(entry["doc_id"])
        vector = self.repository.fetch_vector(doc_id)
        if vector is None:
            raise AutoTaggingRequestError(f"The indexed vector is missing: {doc_id}")
        record = _record_from_entry(entry, self.source_resolver)
        normalized_accepted = normalize_tags(accepted_tags)
        normalized_manual = normalize_tags(
            entry.get("tags", ()) if manual_tags is None else manual_tags
        )
        normalized_inherited = normalize_tags(
            entry.get("inherited_tags", ())
            if inherited_tags is None
            else inherited_tags
        )
        effective_tags = normalize_tags(
            [
                *normalized_manual,
                *entry.get("folder_tags", ()),
                *normalized_accepted,
                *normalized_inherited,
            ]
        )
        state_entry = {
            **record.state_dict(),
            "tags": list(normalized_manual),
            "folder_tags": list(entry.get("folder_tags", ())),
            "accepted_auto_tags": list(normalized_accepted),
            "inherited_tags": list(normalized_inherited),
        }
        write_result = self.collection_writes.upsert(
            [
                PreparedCollectionUpsert(
                    record=record,
                    image_vector=vector,
                    effective_tags=effective_tags,
                    state_entry=state_entry,
                )
            ],
            operation_kind="auto_tag_update",
        )
        if write_result.failures or write_result.succeeded != [doc_id]:
            raise AutoTaggingRequestError(
                write_result.failures.get(
                    doc_id, "Failed to update the Collection tags."
                )
            )
        self.state.set_document_annotation(
            doc_id=doc_id,
            source_sha256=str(entry["sha256"]),
            cache_key=annotation.get("cache_key"),
            status=status,
            proposed_tags=proposed_tags,
            accepted_tags=normalized_accepted,
            rejected_tags=rejected_tags,
            description=str(annotation.get("description") or ""),
            entities=dict(annotation.get("entities") or {}),
            warnings=annotation.get("warnings", ()),
            structured=dict(annotation.get("structured") or {}),
            policy=dict(annotation.get("policy") or {}),
            error=str(annotation.get("error") or ""),
        )

    def _finalize_review_repository(self) -> None:
        self.repository.set_tag_catalog(self.state.list_effective_tags())

    def _candidates(self, scope: str, limit: int) -> list[dict[str, Any]]:
        if scope not in SUPPORTED_SCOPES:
            raise AutoTaggingRequestError(
                "scope must be latest_index_run, untagged, failed, failed_all, or all."
            )
        scoped, root_id = self._configured_root()
        if scoped and root_id is None:
            return []
        run_id = (
            self.state.latest_index_run_id(root_id if scoped else None)
            if scope == "latest_index_run"
            else None
        )
        selector = getattr(self.state, "list_auto_tag_candidates", None)
        if not callable(selector):
            raise ConfigurationError(
                "Auto-tagging requires the bounded candidate selector."
            )
        return selector(
            scope,
            limit=limit,
            root_id=root_id if scoped else None,
            run_id=run_id,
        )

    def _configured_root(self) -> tuple[bool, str | None]:
        root = self.config.library_image_root
        if root is None:
            return False, None
        return True, self.state.find_root_id(str(root))

    def _annotations_in_library(
        self,
        status: str,
        *,
        run_id: str | None = None,
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        scoped, root_id = self._configured_root()
        if scoped and root_id is None:
            return
        pager = getattr(self.state, "page_document_annotations_with_entries", None)
        if not callable(pager):
            raise ConfigurationError(
                "Auto-tagging requires the bounded annotation pager."
            )
        after: tuple[str, str] | None = None
        while True:
            page = pager(
                status,
                root_id=root_id if scoped else None,
                run_id=run_id,
                after=after,
                limit=500,
            )
            if not page:
                return
            yield from page
            last_annotation = page[-1][0]
            after = (
                str(last_annotation.get("updated_at") or ""),
                str(last_annotation.get("doc_id") or ""),
            )

    def _annotation_count_in_library(
        self,
        status: str,
        *,
        run_id: str | None = None,
    ) -> int:
        scoped, root_id = self._configured_root()
        if scoped and root_id is None:
            return 0
        counter = getattr(self.state, "count_document_annotations", None)
        if not callable(counter):
            raise ConfigurationError(
                "Auto-tagging requires the bounded annotation counter."
            )
        return int(
            counter(
                status,
                root_id=root_id if scoped else None,
                run_id=run_id,
            )
        )

    def _attach_proposal(
        self,
        *,
        entry: dict[str, Any],
        cache_key: str,
        structured: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        previous = self.state.get_document_annotation(str(entry["doc_id"]))
        rejected_tags = (
            tuple(previous["rejected_tags"])
            if previous is not None and previous["source_sha256"] == entry["sha256"]
            else ()
        )
        # A SHA group can contain files from several folders.  Revalidate once
        # more for the individual document so explicit evidence from one copy
        # cannot authorize an entity proposal for another copy.
        local_structured = _finalize_structured_annotation(
            _revalidate_annotation_for_context(
                structured,
                _aggregate_context([entry]),
            )
        )
        local_policy = dict(policy)
        local_policy.update(
            {
                "requires_review": bool(local_structured.get("requires_review")),
                "review_required": bool(local_structured.get("requires_review")),
                "review_reasons": list(local_structured.get("review_reasons", ())),
                "plus_recommended": bool(local_structured.get("plus_recommended")),
            }
        )
        proposed_tags = _proposed_tags(entry, local_structured, rejected_tags)
        initial_metadata = _proposal_tag_metadata(
            entry,
            local_structured,
            proposed_tags,
        )
        identity_keys = {
            _comparison_key(str(detail.get("tag") or ""))
            for detail in initial_metadata["tag_details"]
            if isinstance(detail, Mapping) and bool(detail.get("identity"))
        }
        auto_accepted_tags = tuple(proposed_tags)
        auto_accepted_identity_tags = tuple(
            tag for tag in auto_accepted_tags if _comparison_key(tag) in identity_keys
        )
        remaining_proposed_tags: tuple[str, ...] = ()
        rejected_keys = {_comparison_key(tag) for tag in rejected_tags}
        accepted_tags = normalize_tags(
            [
                *(
                    tag
                    for tag in entry.get("accepted_auto_tags", ())
                    if _comparison_key(tag) not in rejected_keys
                ),
                *auto_accepted_tags,
            ]
        )
        entities = dict(local_structured.get("entities") or {})
        warnings = tuple(str(value) for value in local_structured.get("warnings", ()))
        status = "accepted"
        local_policy.update(
            {
                "automatic_approval_mode": "all_valid_model_tags",
                "auto_approved_tags": list(auto_accepted_tags),
                "user_suppressed_tags": list(rejected_tags),
            }
        )
        annotation_payload = {
            "cache_key": cache_key,
            "description": str(local_structured.get("description") or ""),
            "entities": entities,
            "warnings": warnings,
            "structured": local_structured,
            "policy": local_policy,
            "error": "",
        }
        if auto_accepted_tags:
            self._write_review_document(
                entry=entry,
                annotation=annotation_payload,
                accepted_tags=accepted_tags,
                rejected_tags=rejected_tags,
                proposed_tags=remaining_proposed_tags,
                status=status,
            )
        else:
            self.state.set_document_annotation(
                doc_id=str(entry["doc_id"]),
                source_sha256=str(entry["sha256"]),
                cache_key=cache_key,
                status=status,
                proposed_tags=remaining_proposed_tags,
                accepted_tags=accepted_tags,
                rejected_tags=rejected_tags,
                description=str(local_structured.get("description") or ""),
                entities=entities,
                warnings=warnings,
                structured=local_structured,
                policy=local_policy,
            )
        metadata_entry = {**entry, "accepted_auto_tags": list(accepted_tags)}
        tag_metadata = _proposal_tag_metadata(
            metadata_entry,
            local_structured,
            remaining_proposed_tags,
        )
        return {
            "proposal_id": str(entry["doc_id"]),
            "doc_id": str(entry["doc_id"]),
            "relative_path": str(entry["relative_path"]),
            "source_path": self._proposal_source_path(entry),
            "manual_tags": list(entry.get("tags", ())),
            "folder_tags": list(entry.get("folder_tags", ())),
            "accepted_auto_tags": list(accepted_tags),
            "inherited_tags": list(entry.get("inherited_tags", ())),
            "existing_tags": tag_metadata["existing_tags"],
            "tags": list(local_structured.get("controlled_tags", ())),
            "controlled_tags": list(local_structured.get("controlled_tags", ())),
            "suggested_tags": list(local_structured.get("suggested_tags", ())),
            "proposed_tags": list(remaining_proposed_tags),
            "model_proposed_tags": list(auto_accepted_tags),
            "tag_details": initial_metadata["tag_details"],
            "low_risk_tags": initial_metadata["low_risk_tags"],
            "identity_tags": list(
                normalize_tags(
                    [
                        *initial_metadata["identity_tags"],
                        *auto_accepted_identity_tags,
                    ]
                )
            ),
            "auto_accepted_tags": list(auto_accepted_tags),
            "auto_accepted_identity_tags": list(auto_accepted_identity_tags),
            "auto_accepted_tag_details": initial_metadata["tag_details"],
            "description": str(local_structured.get("description") or ""),
            "fields": dict(local_structured.get("fields") or {}),
            "entities": entities,
            "warnings": list(warnings),
            "requires_review": bool(local_structured.get("requires_review")),
            "review_required": bool(local_structured.get("requires_review")),
            "review_reasons": list(local_structured.get("review_reasons", ())),
            "plus_recommended": bool(local_structured.get("plus_recommended")),
            "model_chain": list(local_policy.get("model_chain", ())),
            "model_trace": list(local_policy.get("model_trace", ())),
            "resolved_model": str(local_policy.get("resolved_model") or ""),
            "escalated": bool(local_policy.get("escalated")),
            "structured": local_structured,
            "policy": local_policy,
            "folder_inheritance": dict(local_policy.get("folder_inheritance") or {}),
            "status": status,
        }

    def _stored_proposal(
        self,
        annotation: dict[str, Any],
        entry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        doc_id = str(annotation["doc_id"])
        if entry is None:
            entry = self.state.get(doc_id)
        policy = dict(annotation.get("policy") or {})
        status = str(annotation.get("status") or "pending_review")
        structured = dict(annotation.get("structured") or {})
        if not structured:
            requires_review = bool(
                policy.get(
                    "requires_review",
                    status in {"pending_review", "failed"},
                )
            )
            structured = {
                "description": str(annotation.get("description") or ""),
                "fields": {},
                "entities": dict(annotation.get("entities") or {}),
                "controlled_tags": list(annotation.get("proposed_tags", ())),
                "suggested_tags": [],
                "warnings": list(annotation.get("warnings", ())),
                "requires_review": requires_review,
                "review_reasons": list(
                    policy.get("review_reasons")
                    or (["legacy_annotation"] if requires_review else [])
                ),
                "plus_recommended": False,
            }
        proposed_tags = tuple(annotation.get("proposed_tags", ()))
        tag_metadata = _proposal_tag_metadata(entry or {}, structured, proposed_tags)
        return {
            "proposal_id": doc_id,
            "doc_id": doc_id,
            "relative_path": str(entry["relative_path"]) if entry else "",
            "source_path": self._proposal_source_path(entry),
            "description": str(structured.get("description") or ""),
            "tags": list(proposed_tags),
            "controlled_tags": list(structured.get("controlled_tags", ())),
            "suggested_tags": list(structured.get("suggested_tags", ())),
            "proposed_tags": list(proposed_tags),
            "existing_tags": tag_metadata["existing_tags"],
            "tag_details": tag_metadata["tag_details"],
            "low_risk_tags": tag_metadata["low_risk_tags"],
            "identity_tags": tag_metadata["identity_tags"],
            "manual_tags": list(entry.get("tags", ())) if entry else [],
            "folder_tags": list(entry.get("folder_tags", ())) if entry else [],
            "accepted_auto_tags": (
                list(entry.get("accepted_auto_tags", ())) if entry else []
            ),
            "inherited_tags": (list(entry.get("inherited_tags", ())) if entry else []),
            "fields": dict(structured.get("fields") or {}),
            "entities": dict(structured.get("entities") or {}),
            "warnings": list(structured.get("warnings", ())),
            "requires_review": bool(structured.get("requires_review")),
            "review_required": bool(structured.get("requires_review")),
            "review_reasons": list(structured.get("review_reasons", ())),
            "plus_recommended": bool(structured.get("plus_recommended")),
            "model_chain": list(policy.get("model_chain", ())),
            "model_trace": list(policy.get("model_trace", ())),
            "resolved_model": str(policy.get("resolved_model") or ""),
            "escalated": bool(policy.get("escalated")),
            "structured": structured,
            "policy": policy,
            "folder_inheritance": dict(policy.get("folder_inheritance") or {}),
            "status": status,
            "error": str(annotation.get("error") or ""),
            "failure_category": str(
                (
                    policy.get("failure_category")
                    or _annotation_failure_category(
                        str(annotation.get("error") or ""),
                        "item",
                    )
                )
                if status == "failed"
                else ""
            ),
        }

    def _proposal_source_path(self, entry: Mapping[str, Any] | None) -> str:
        if entry is None:
            return ""
        try:
            source = self.source_resolver.resolve_fields(dict(entry))
            return str(source) if source.is_file() else ""
        except (ConfigurationError, OSError, ValueError):
            return ""

    def _review_one(self, decision: Mapping[str, Any]) -> str:
        doc_id = str(decision.get("doc_id") or "").strip()
        action = str(decision.get("action") or "").strip().lower()
        if not doc_id or action not in {"accept", "edit", "reject", "manual"}:
            raise AutoTaggingRequestError("Review decision is invalid.")
        entry = self.state.get(doc_id)
        annotation = self.state.get_document_annotation(doc_id)
        if entry is None or annotation is None:
            raise AutoTaggingRequestError("The annotation no longer exists.")
        if annotation["source_sha256"] != entry["sha256"]:
            raise AutoTaggingRequestError(
                "The image changed after annotation; run auto-tagging again."
            )

        manual_tags: tuple[str, ...] | None = None
        annotation_to_write = annotation
        if action == "manual":
            if annotation["status"] != "failed":
                raise AutoTaggingRequestError(
                    "Manual labeling is available only for failed annotations."
                )
            cleaned = _sanitize_review_tags(decision.get("tags", ()))
            if not cleaned:
                raise AutoTaggingRequestError(
                    "Manual labeling requires at least one non-empty tag."
                )
            manual_tags = normalize_tags([*entry.get("tags", ()), *cleaned])
            accepted_tags = normalize_tags(entry.get("accepted_auto_tags", ()))
            rejected_tags = tuple(annotation.get("rejected_tags", ()))
            status = "accepted"
            manual_policy = dict(annotation.get("policy") or {})
            manual_policy.update(
                {
                    "manually_resolved": True,
                    "resolution": "manual",
                    "requires_review": False,
                    "review_required": False,
                }
            )
            annotation_to_write = {**annotation, "policy": manual_policy}
        elif action == "reject":
            accepted_tags = normalize_tags(entry.get("accepted_auto_tags", ()))
            rejected_tags = normalize_tags(
                [*annotation.get("rejected_tags", ()), *annotation["proposed_tags"]]
            )
            status = "rejected"
        else:
            raw_tags = decision.get("tags")
            if raw_tags is None:
                raw_tags = annotation["proposed_tags"]
            cleaned = _sanitize_review_tags(raw_tags)
            metadata = _proposal_tag_metadata(
                entry,
                dict(annotation.get("structured") or {}),
                annotation.get("proposed_tags", ()),
            )
            identity_by_key = {
                _comparison_key(tag): tag for tag in metadata["identity_tags"]
            }
            accepted_identity_keys = {
                _comparison_key(tag)
                for tag in cleaned
                if _comparison_key(tag) in identity_by_key
            }
            confirmed_identity_tags = _sanitize_review_tags(
                decision.get("confirmed_identity_tags", ())
            )
            confirmed_identity_keys = {
                _comparison_key(tag) for tag in confirmed_identity_tags
            }
            unknown_confirmations = confirmed_identity_keys - set(identity_by_key)
            if unknown_confirmations:
                raise AutoTaggingRequestError(
                    "confirmed_identity_tags contains tags that are not "
                    "identity proposals."
                )
            unconfirmed = accepted_identity_keys - confirmed_identity_keys
            if unconfirmed:
                raise AutoTaggingRequestError(
                    "Identity tags require per-item confirmation: "
                    + ", ".join(identity_by_key[key] for key in sorted(unconfirmed))
                )
            selected_entity_types: dict[str, list[str]] = defaultdict(list)
            for detail in metadata["tag_details"]:
                if not isinstance(detail, Mapping):
                    continue
                tag = str(detail.get("tag") or "")
                key = _comparison_key(tag)
                if key not in confirmed_identity_keys:
                    continue
                entity_type = str(detail.get("entity_type") or "")
                if entity_type:
                    selected_entity_types[entity_type].append(tag)
            duplicate_conflicts = {
                entity_type: tags
                for entity_type, tags in selected_entity_types.items()
                if len({_comparison_key(tag) for tag in tags}) > 1
                and not _allows_multiple_confirmed_identities(
                    dict(annotation.get("structured") or {}),
                    entity_type,
                )
            }
            if duplicate_conflicts:
                details = "; ".join(
                    f"{entity_type}: {', '.join(tags)}"
                    for entity_type, tags in sorted(duplicate_conflicts.items())
                )
                raise AutoTaggingRequestError(
                    "Only one conflicting identity can be confirmed per entity "
                    f"type: {details}"
                )
            accepted_tags = normalize_tags(
                [*entry.get("accepted_auto_tags", ()), *cleaned]
            )
            selected_keys = {_comparison_key(tag) for tag in cleaned}
            rejected_tags = tuple(
                tag
                for tag in annotation["proposed_tags"]
                if _comparison_key(tag) not in selected_keys
            )
            status = "accepted"

        self._write_review_document(
            entry=entry,
            annotation=annotation_to_write,
            accepted_tags=accepted_tags,
            rejected_tags=rejected_tags,
            proposed_tags=(() if action == "manual" else annotation["proposed_tags"]),
            status=status,
            manual_tags=manual_tags,
        )
        return status


class StreamingAutoTagSession:
    """Bounded primary-model pipeline fed by successful index commits.

    The Collection owner thread prepares and finalizes requests. Worker threads
    only encode the already committed source image and perform model HTTP calls.
    Plus escalation is intentionally deferred to ``_complete_run`` so the normal
    and combined workflows retain one merge, policy and failure-isolation path.
    """

    def __init__(
        self,
        coordinator: AutoTaggingCoordinator,
        *,
        selected_model: str,
        limit: int,
        budget_limit: float | None,
        activity_callback: Callable[[int], None] | None,
    ) -> None:
        self.coordinator = coordinator
        self.selected_model = selected_model
        self.limit = limit
        self.accounting = _RunAccounting(
            budget_limit,
            coordinator.config.model_configuration,
        )
        self.failure_sink = FailureSink(
            coordinator.config.results_path,
            f"auto-tag-{uuid.uuid4().hex}",
            coordinator.config.workspace,
        )
        self.activity_callback = activity_callback or (lambda _active: None)
        self.owner_thread_id = threading.get_ident()
        self.workers = max(
            1,
            int(getattr(coordinator.config, "auto_tag_concurrency", 2)),
        )
        self.max_pending = max(self.workers, self.workers * 2)
        self.executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="zvec-stream-auto-tag",
        )
        self.active: dict[
            Future[_CompletedModelRequest], tuple[_PreparedModelRequest, int]
        ] = {}
        self.work_items: list[tuple[str, list[dict[str, Any]], TaggingContext]] = []
        self.primary_results: dict[str, _ModelResolution] = {}
        self.entries_by_hash: dict[str, list[dict[str, Any]]] = {}
        self.stale_entries: list[tuple[str, dict[str, Any]]] = []
        self.stale_doc_ids: set[str] = set()
        self.seen_hashes: set[str] = set()
        self.candidate_count = 0
        self.peak_pending = 0
        self.peak_in_flight = 0
        self.pending_bytes = 0
        self.peak_pending_bytes = 0
        self._activity_lock = threading.Lock()
        self._active_network = 0
        self.closed = False
        self.aborted = False

    @property
    def in_flight(self) -> int:
        with self._activity_lock:
            return self._active_network

    def offer(self, content_hash: str, entries: list[dict[str, Any]]) -> None:
        """Queue one committed SHA group, applying bounded backpressure."""

        self._assert_owner_open()
        normalized_hash = str(content_hash).strip().lower()
        if not normalized_hash or normalized_hash in self.seen_hashes or not entries:
            return
        remaining = self.limit - self.candidate_count
        if remaining <= 0:
            return

        selected: list[dict[str, Any]] = []
        for entry in entries[:remaining]:
            current = self.coordinator.state.get(str(entry.get("doc_id") or ""))
            if (
                current is not None
                and str(current.get("sha256") or "") == normalized_hash
            ):
                selected.append(current)
        if not selected:
            return

        self._drain(block=False)
        self.seen_hashes.add(normalized_hash)
        self.candidate_count += len(selected)
        self.entries_by_hash[normalized_hash] = selected
        self._refresh_current_entries(normalized_hash)
        selected = self.entries_by_hash[normalized_hash]
        context = _aggregate_context(selected) if selected else TaggingContext()
        self.work_items.append((normalized_hash, selected, context))
        primary_model = self.selected_model
        if not selected:
            self.primary_results[normalized_hash] = _ModelResolution(
                model=primary_model,
                cache_key=_cache_key(normalized_hash, primary_model),
                status="failed",
                error="Source image changed after indexing; run the pipeline again.",
                failure_kind="retryable",
            )
            return

        estimated_bytes = _stream_request_bytes(selected[0])
        while self.active and (
            len(self.active) >= self.max_pending
            or self.pending_bytes + estimated_bytes
            > self.coordinator.config.max_inflight_request_bytes
        ):
            self.coordinator.cancel_check()
            self._drain(block=True)

        prepared = self.coordinator._prepare_model_annotation(
            content_hash=normalized_hash,
            entries=selected,
            context=context,
            model=primary_model,
            accounting=self.accounting,
        )
        if isinstance(prepared, _ModelResolution):
            self.primary_results[normalized_hash] = prepared
            return

        try:
            future = self.executor.submit(
                self._execute_primary_request,
                prepared,
            )
        except BaseException:
            self.accounting.release_reservation(prepared.reserved_cost_cny)
            prepared.flight.release()
            raise
        self.active[future] = (prepared, estimated_bytes)
        self.pending_bytes += estimated_bytes
        self.peak_pending = max(self.peak_pending, len(self.active))
        self.peak_pending_bytes = max(
            self.peak_pending_bytes,
            self.pending_bytes,
        )

    def pump(self) -> None:
        self._assert_owner_open()
        self._drain(block=False)

    def finish(self) -> AutoTagRunReport:
        self._assert_owner_open()
        self._finish_deadline = monotonic() + self._finish_timeout_seconds()
        self._finish_last_progress = 0.0
        try:
            while self.active:
                self.coordinator.cancel_check()
                self._drain(block=True)
            if not self.closed:
                self.executor.shutdown(wait=True)
                self.closed = True
            self._invalidate_changed_sources()
            report = self.coordinator._complete_run(
                scope="index_run_inserted",
                selected_model=self.selected_model,
                candidate_count=self.candidate_count,
                work_items=self.work_items,
                primary_results=self.primary_results,
                accounting=self.accounting,
                failure_sink=self.failure_sink,
                freshness_validator=self._entries_are_current,
            )
            return self._append_stale_failures(report)
        except BaseException:
            self.abort()
            raise

    def _report_stream_progress(self) -> None:
        """Emit a periodic auto-tagging progress message during _drain waits."""

        deadline = getattr(self, "_finish_deadline", 0.0)
        if not deadline:
            return
        now = monotonic()
        last = getattr(self, "_finish_last_progress", 0.0)
        if now - last < 1.0:
            return
        self._finish_last_progress = now
        remaining = len(self.active)
        done = max(0, self.candidate_count - remaining)
        self.coordinator.progress(f"智能标注进行中 {done}/{self.candidate_count}")

    def _finish_timeout_seconds(self) -> float:
        """Overall safety deadline so auto-tagging never blocks silently.

        Each candidate may retry rate-limiter and HTTP timeouts in the worst
        case.  The bound keeps the floor proportional to the batch size while
        never exceeding a hard ceiling, so a stuck session always surfaces.
        """

        return min(max(self.limit * 120.0, 1800.0), 7200.0)

    def abort(self) -> None:
        self._assert_owner()
        if self.closed:
            return
        self.aborted = True
        self.closed = True
        for future, (prepared, _estimated_bytes) in self.active.items():
            self.accounting.release_reservation(prepared.reserved_cost_cny)
            # A running worker is pure network work. Keep its claim until it exits
            # so another Collection cannot overlap a duplicate request, then wake
            # followers without ever touching owner-thread cache/state.
            _release_flight_when_done(future, prepared.flight)
        self.active.clear()
        self.pending_bytes = 0
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _drain(self, *, block: bool) -> None:
        if not self.active:
            return
        if block:
            completed: set[Future[_CompletedModelRequest]] = set()
            while not completed:
                self.coordinator.cancel_check()
                completed, _pending = wait(
                    self.active,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                self._report_stream_progress()
                deadline = getattr(self, "_finish_deadline", 0.0)
                if deadline and self.active and monotonic() >= deadline:
                    self.coordinator.progress("智能标注超时，正在中止剩余请求。")
                    self.abort()
                    return
        else:
            completed = {future for future in self.active if future.done()}
        if completed:
            # Cancellation may arrive while FIRST_COMPLETED is blocking. Check
            # again before any completed response can touch cache or state.
            self.coordinator.cancel_check()
        resubmissions: list[tuple[_PreparedModelRequest, int]] = []
        for future in completed:
            prepared, estimated_bytes = self.active.pop(future)
            self.pending_bytes = max(0, self.pending_bytes - estimated_bytes)
            try:
                completed_request = future.result()
            except Exception as exc:  # pragma: no cover - defensive boundary
                completed_request = _CompletedModelRequest(
                    prepared=prepared,
                    error=str(exc) or exc.__class__.__name__,
                    failure_kind="systemic",
                )
            retried = self._consume_completed_request(prepared, completed_request)
            if retried is not None:
                current_entries = self.entries_by_hash[prepared.content_hash]
                resubmissions.append(
                    (retried, _stream_request_bytes(current_entries[0]))
                )
        for prepared, estimated_bytes in resubmissions:
            while self.active and (
                len(self.active) >= self.max_pending
                or self.pending_bytes + estimated_bytes
                > self.coordinator.config.max_inflight_request_bytes
            ):
                self.coordinator.cancel_check()
                self._drain(block=True)
            try:
                future = self.executor.submit(self._execute_primary_request, prepared)
            except BaseException:
                self.accounting.release_reservation(prepared.reserved_cost_cny)
                prepared.flight.release()
                raise
            self.active[future] = (prepared, estimated_bytes)
            self.pending_bytes += estimated_bytes
            self.peak_pending = max(self.peak_pending, len(self.active))
            self.peak_pending_bytes = max(
                self.peak_pending_bytes,
                self.pending_bytes,
            )

    def _consume_completed_request(
        self,
        prepared: _PreparedModelRequest,
        completed_request: _CompletedModelRequest,
    ) -> _PreparedModelRequest | None:
        """Finalize one response and always retire its original flight claim."""

        try:
            if completed_request.budget_exhausted or self._entries_are_current(
                prepared.content_hash
            ):
                self.primary_results[prepared.content_hash] = (
                    self.coordinator._finalize_model_request(
                        completed_request,
                        self.accounting,
                    )
                )
                return None

            self.accounting.record_request(
                prepared.model,
                completed_request.response,
                reserved_cost_cny=prepared.reserved_cost_cny,
                attempts=completed_request.http_attempts,
            )
            current_entries = self.entries_by_hash.get(prepared.content_hash, [])
            if not current_entries:
                self.primary_results[prepared.content_hash] = _ModelResolution(
                    model=prepared.model,
                    cache_key=prepared.cache_key,
                    status="failed",
                    error=(
                        "Source image changed while auto-tagging; "
                        "run the pipeline again."
                    ),
                    failure_kind="retryable",
                )
                return None

            context = _aggregate_context(current_entries)
            self._replace_work_context(
                prepared.content_hash,
                current_entries,
                context,
            )
            # The stale request must surrender its key before preparing a retry;
            # otherwise it would become a follower waiting on its own claim.
            prepared.flight.release()
            retried = self.coordinator._prepare_model_annotation(
                content_hash=prepared.content_hash,
                entries=current_entries,
                context=context,
                model=prepared.model,
                accounting=self.accounting,
            )
            if isinstance(retried, _ModelResolution):
                self.primary_results[prepared.content_hash] = retried
                return None
            return retried
        except BaseException:
            self.accounting.release_reservation(prepared.reserved_cost_cny)
            raise
        finally:
            prepared.flight.release()

    def _invalidate_changed_sources(self) -> None:
        for content_hash, _entries, _context in self.work_items:
            self._refresh_current_entries(content_hash)

    def _entries_are_current(self, content_hash: str) -> bool:
        stale_before = len(self.stale_doc_ids)
        self._refresh_current_entries(content_hash)
        return (
            bool(self.entries_by_hash.get(content_hash))
            and len(self.stale_doc_ids) == stale_before
        )

    def _refresh_current_entries(self, content_hash: str) -> None:
        entries = self.entries_by_hash.get(content_hash, [])
        current_entries: list[dict[str, Any]] = []
        for entry in entries:
            is_current = False
            try:
                current = self.coordinator.state.get(str(entry["doc_id"]))
                path = self.coordinator.source_resolver.resolve_fields(entry)
                stat = path.stat()
                is_current = not (
                    current is None
                    or str(current.get("sha256") or "") != content_hash
                    or int(current.get("size_bytes") or -1) != int(entry["size_bytes"])
                    or int(current.get("mtime_ns") or -1) != int(entry["mtime_ns"])
                    or stat.st_size != int(entry["size_bytes"])
                    or stat.st_mtime_ns != int(entry["mtime_ns"])
                )
            except (ConfigurationError, OSError, ValueError, KeyError):
                is_current = False
            if is_current:
                current_entries.append(entry)
                continue
            doc_id = str(entry.get("doc_id") or "")
            if doc_id not in self.stale_doc_ids:
                self.stale_doc_ids.add(doc_id)
                self.stale_entries.append((content_hash, entry))
        entries[:] = current_entries
        self._replace_work_context(
            content_hash,
            entries,
            _aggregate_context(entries) if entries else TaggingContext(),
        )

    def _replace_work_context(
        self,
        content_hash: str,
        entries: list[dict[str, Any]],
        context: TaggingContext,
    ) -> None:
        for index, (group_hash, _entries, _context) in enumerate(self.work_items):
            if group_hash == content_hash:
                self.work_items[index] = (group_hash, entries, context)
                return

    def _append_stale_failures(self, report: AutoTagRunReport) -> AutoTagRunReport:
        for content_hash, entry in self.stale_entries:
            resolution = _ModelResolution(
                model=self.selected_model,
                cache_key=_cache_key(content_hash, self.selected_model),
                status="failed",
                error="Source image changed after indexing; run the pipeline again.",
                failure_kind="retryable",
            )
            self.coordinator._store_resolution_failure(
                [entry],
                content_hash,
                resolution,
                self.selected_model,
                [resolution],
            )
            captures = self.coordinator._capture_resolution_failures(
                self.failure_sink,
                [entry],
                resolution,
            )
            report["failed"] = int(report.get("failed") or 0) + 1
            report["processed"] = int(report.get("processed") or 0) + 1
            failures = list(report.get("failures") or [])
            for capture in captures:
                if len(failures) < 100:
                    failures.append(asdict(capture.failure))
            report["failures"] = failures
        report["failure_manifest"] = self.failure_sink.manifest_path
        return report

    def _execute_primary_request(
        self,
        prepared: _PreparedModelRequest,
    ) -> _CompletedModelRequest:
        with self._activity_lock:
            self._active_network += 1
            self.peak_in_flight = max(self.peak_in_flight, self._active_network)
            active = self._active_network
            self._notify_activity(active)
        try:
            return self.coordinator._execute_model_request(prepared)
        finally:
            with self._activity_lock:
                self._active_network = max(0, self._active_network - 1)
                active = self._active_network
                self._notify_activity(active)

    def _notify_activity(self, active: int) -> None:
        try:
            self.activity_callback(active)
        except Exception:
            # Diagnostics must never turn a completed model call into an item
            # failure or cross the owner-thread persistence boundary.
            return

    def _assert_owner_open(self) -> None:
        self._assert_owner()
        if self.closed:
            raise RuntimeError("The streaming auto-tag session is already closed.")

    def _assert_owner(self) -> None:
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError(
                "Streaming auto-tag state must stay on the Collection owner thread."
            )


def _proposal_tag_metadata(
    entry: Mapping[str, Any],
    structured: Mapping[str, Any],
    proposed_tags: Iterable[str],
) -> dict[str, list[Any]]:
    """Build a stable, UI-oriented provenance/risk view without free-form tags."""

    existing_sources: dict[str, list[str]] = {}
    canonical_tags: dict[str, str] = {}
    for source, values in (
        ("manual", entry.get("tags", ())),
        ("folder", entry.get("folder_tags", ())),
        ("accepted_auto", entry.get("accepted_auto_tags", ())),
        ("inherited", entry.get("inherited_tags", ())),
    ):
        for tag in normalize_tags(_iter_strings(values)):
            key = _comparison_key(tag)
            canonical_tags.setdefault(key, tag)
            existing_sources.setdefault(key, []).append(source)

    proposed = normalize_tags(proposed_tags)
    proposed_keys = {_comparison_key(tag) for tag in proposed}
    for tag in proposed:
        canonical_tags.setdefault(_comparison_key(tag), tag)

    reasons = tuple(_iter_strings(structured.get("review_reasons")))
    field_values: dict[str, dict[str, Any]] = {}
    raw_fields = structured.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    for field_name, raw_field in fields.items():
        if not isinstance(raw_field, Mapping):
            continue
        confidence = _normalized_confidence(raw_field.get("confidence"))
        risky = any(
            reason.startswith(f"field:{field_name}:")
            and ("conflict" in reason or "overflow" in reason)
            for reason in reasons
        )
        for tag in _iter_strings(raw_field.get("labels")):
            key = _comparison_key(tag)
            if key not in proposed_keys:
                continue
            field_values[key] = {
                "field": str(field_name),
                "confidence": confidence,
                "low_risk": confidence is not None
                and confidence >= LOW_RISK_FIELD_CONFIDENCE
                and not risky,
            }

    identity_values: dict[str, dict[str, Any]] = {}
    raw_entities = structured.get("entities")
    entities = raw_entities if isinstance(raw_entities, Mapping) else {}
    for entity_type in ENTITY_TYPES:
        entity_items = _entity_values(entities.get(entity_type))
        entity_type_conflict = any(
            entity.get("state") == "conflict" for entity in entity_items
        )
        for entity in entity_items:
            name = entity.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = _comparison_key(name)
            if key not in proposed_keys:
                continue
            identity_values[key] = {
                "entity_type": entity_type,
                "confidence": _normalized_confidence(entity.get("confidence")),
                "state": str(entity.get("state") or ""),
                "conflict": entity_type_conflict or entity.get("state") == "conflict",
                "auto_accept": _identity_entity_is_auto_acceptable(
                    entity_type,
                    entity,
                    entity_type_conflict=entity_type_conflict,
                ),
            }

    source_priority = ("manual", "folder", "accepted_auto", "inherited")
    ordered_keys = list(canonical_tags)
    tag_details: list[dict[str, Any]] = []
    low_risk_tags: list[str] = []
    identity_tags: list[str] = []
    auto_accept_identity_tags: list[str] = []
    conflicting_identity_tags: list[str] = []
    for key in ordered_keys:
        tag = canonical_tags[key]
        sources = _stable_strings(existing_sources.get(key, ()))
        field = field_values.get(key)
        identity = identity_values.get(key)
        if field is not None:
            sources.append("model_field")
        if identity is not None:
            sources.append("model_entity")
        sources = _stable_strings(sources)
        source = next(
            (candidate for candidate in source_priority if candidate in sources),
            sources[0] if sources else "model",
        )
        already_present = key in existing_sources
        is_identity = identity is not None
        if identity is not None:
            if bool(identity["conflict"]):
                risk = "conflict"
                identity_tags.append(tag)
                conflicting_identity_tags.append(tag)
            elif bool(identity["auto_accept"]):
                risk = "low"
                low_risk_tags.append(tag)
                auto_accept_identity_tags.append(tag)
            else:
                risk = "identity"
                identity_tags.append(tag)
        elif field is not None and bool(field["low_risk"]):
            risk = "low"
            low_risk_tags.append(tag)
        elif already_present:
            risk = "existing"
        else:
            risk = "review"
        detail: dict[str, Any] = {
            "tag": tag,
            "source": source,
            "sources": sources,
            "risk": risk,
            "identity": is_identity,
            "already_present": already_present,
            "requires_individual_confirmation": bool(
                is_identity and identity and identity["conflict"]
            ),
        }
        if field is not None:
            detail["field"] = field["field"]
            detail["confidence"] = field["confidence"]
        if identity is not None:
            detail["entity_type"] = identity["entity_type"]
            detail["confidence"] = identity["confidence"]
        tag_details.append(detail)
    return {
        "existing_tags": list(
            normalize_tags(canonical_tags[key] for key in existing_sources)
        ),
        "tag_details": tag_details,
        "low_risk_tags": list(normalize_tags(low_risk_tags)),
        "identity_tags": list(normalize_tags(identity_tags)),
        "auto_accept_identity_tags": list(normalize_tags(auto_accept_identity_tags)),
        "conflicting_identity_tags": list(normalize_tags(conflicting_identity_tags)),
    }


def _folder_display_path(entry: Mapping[str, Any]) -> str:
    configured = str(entry.get("parent_directory") or "").strip()
    if configured:
        return configured.replace("\\", "/").strip("/")
    relative_path = str(entry.get("relative_path") or "").replace("\\", "/")
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "" if parent == "." else parent.strip("/")


def _folder_identity(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(entry.get("root_id") or ""),
        _comparison_key(_folder_display_path(entry)),
    )


def _normalized_inheritance_category(value: Any) -> str:
    normalized = _comparison_key(str(value or "")).replace("-", "_").replace(" ", "_")
    aliases = {
        "actions": "action",
        "poses": "pose",
        "expressions": "expression",
        "emotions": "emotion",
        "facial_expressions": "facial_expression",
    }
    return aliases.get(normalized, normalized)


def _blocked_folder_inheritance_category(
    tag: str,
    annotation: Mapping[str, Any] | None,
) -> str | None:
    """Return the transient category for a tag, if it must not be inherited."""

    key = _comparison_key(tag)
    controlled_field = _CONTROLLED_TAG_FIELDS.get(key)
    normalized_controlled = _normalized_inheritance_category(controlled_field)
    if normalized_controlled in FOLDER_INHERITANCE_BLOCKED_CATEGORIES:
        return normalized_controlled
    if annotation is None:
        return None
    structured = annotation.get("structured")
    structured_mapping = structured if isinstance(structured, Mapping) else {}
    fields = structured_mapping.get("fields")
    fields_mapping = fields if isinstance(fields, Mapping) else {}
    for raw_field_name, raw_field in fields_mapping.items():
        category = _normalized_inheritance_category(raw_field_name)
        if category not in FOLDER_INHERITANCE_BLOCKED_CATEGORIES or not isinstance(
            raw_field, Mapping
        ):
            continue
        field_tags = normalize_tags(
            [
                *_iter_strings(raw_field.get("labels")),
                *_iter_strings(raw_field.get("values")),
            ]
        )
        if key in {_comparison_key(value) for value in field_tags}:
            return category
    return None


def _normalize_pending_filters(
    filters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if filters is None:
        values: dict[str, Any] = {}
    elif isinstance(filters, Mapping):
        values = dict(filters)
    else:
        raise AutoTaggingRequestError("filters must be an object.")
    allowed = {
        "latest_index_only",
        "latest_only",
        "character",
        "work",
        "action",
        "expression",
        "review_state",
        "review_status",
    }
    unknown = set(values) - allowed
    if unknown:
        raise AutoTaggingRequestError(
            "Unknown pending filter fields: " + ", ".join(sorted(unknown))
        )
    latest_value = values.get("latest_index_only", values.get("latest_only", False))
    if not isinstance(latest_value, bool):
        raise AutoTaggingRequestError("latest_index_only must be a boolean.")
    review_state = (
        str(values.get("review_state", values.get("review_status", "all")) or "all")
        .strip()
        .lower()
    )
    compatibility_states = {"pending": "all", "pending_review": "all"}
    review_state = compatibility_states.get(review_state, review_state)
    if review_state not in REVIEW_STATES:
        raise AutoTaggingRequestError(
            "review_state must be all, low_risk, identity, conflict, or failed."
        )
    result: dict[str, Any] = {
        "latest_index_only": latest_value,
        "review_state": review_state,
    }
    for name in ("character", "work", "action", "expression"):
        value = values.get(name)
        if value is None:
            result[name] = ""
            continue
        if not isinstance(value, str):
            raise AutoTaggingRequestError(f"{name} filter must be a string.")
        normalized = value.strip()
        if len(normalized) > 128:
            raise AutoTaggingRequestError(f"{name} filter is too long.")
        result[name] = normalized
    return result


def _proposal_matches_pending_filters(
    proposal: Mapping[str, Any],
    filters: Mapping[str, Any],
    *,
    latest_ids: set[str] | None,
    aliases: TagAliasDictionary | None = None,
) -> bool:
    doc_id = str(proposal.get("doc_id") or "")
    if latest_ids is not None and doc_id not in latest_ids:
        return False
    entities = proposal.get("entities")
    entity_mapping = entities if isinstance(entities, Mapping) else {}
    for filter_name, entity_type in (("character", "character"), ("work", "work")):
        query = str(filters.get(filter_name) or "")
        if query:
            names = tuple(
                str(entity.get("name") or "")
                for entity in _entity_values(entity_mapping.get(entity_type))
            )
            equivalent_queries = (
                aliases.equivalent_terms(query) if aliases is not None else (query,)
            )
            if not any(
                _values_contain_query(names, equivalent_query)
                for equivalent_query in equivalent_queries
            ):
                return False
    fields = proposal.get("fields")
    field_mapping = fields if isinstance(fields, Mapping) else {}
    for field_name in ("action", "expression"):
        query = str(filters.get(field_name) or "")
        if not query:
            continue
        field = field_mapping.get(field_name)
        raw_field = field if isinstance(field, Mapping) else {}
        if not _values_contain_query(
            [
                *_iter_strings(raw_field.get("values")),
                *_iter_strings(raw_field.get("labels")),
            ],
            query,
        ):
            return False
    review_state = str(filters.get("review_state") or "all")
    if review_state == "failed":
        return str(proposal.get("status") or "") == "failed"
    identity_tags = _iter_strings(proposal.get("identity_tags"))
    low_risk_tags = _iter_strings(proposal.get("low_risk_tags"))
    conflict = _proposal_requires_attention(proposal)
    if review_state == "low_risk":
        return bool(low_risk_tags) and not identity_tags and not conflict
    if review_state == "identity":
        return bool(identity_tags)
    if review_state == "conflict":
        return conflict
    return True


def _values_contain_query(values: Iterable[str], query: str) -> bool:
    needle = _comparison_key(query)
    return bool(needle) and any(needle in _comparison_key(value) for value in values)


def _proposal_requires_attention(proposal: Mapping[str, Any]) -> bool:
    attention_markers = (
        "conflict",
        "low_confidence",
        "context_mismatch",
        "context_changed",
        "missing_explicit",
        "unconfirmed",
        "unable_to_confirm",
        "request_failed",
        "budget_exhausted",
    )
    return any(
        any(marker in _comparison_key(reason) for marker in attention_markers)
        for reason in _iter_strings(proposal.get("review_reasons"))
    )


def _allows_multiple_confirmed_identities(
    structured: Mapping[str, Any],
    entity_type: str,
) -> bool:
    """Allow separately confirmed identities when the image contains many people."""

    if entity_type not in {"real_person", "cosplayer", "character"}:
        return False
    fields = structured.get("fields")
    field_mapping = fields if isinstance(fields, Mapping) else {}
    people_count = field_mapping.get("people_count")
    count_mapping = people_count if isinstance(people_count, Mapping) else {}
    values = {
        _comparison_key(value) for value in _iter_strings(count_mapping.get("values"))
    }
    if values & {
        "count_two_people",
        "count_small_group",
        "count_crowd",
    }:
        return True
    labels = {
        _comparison_key(label) for label in _iter_strings(count_mapping.get("labels"))
    }
    return bool(labels & {"双人", "多人", "多人小组", "人群"})


def _validate_proposal_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AutoTaggingRequestError("proposal_ids must be an array of strings.")
    try:
        raw_values = list(values)
    except TypeError as exc:
        raise AutoTaggingRequestError(
            "proposal_ids must be an array of strings."
        ) from exc
    if not raw_values:
        raise AutoTaggingRequestError("At least one proposal_id is required.")
    if len(raw_values) > 1_000:
        raise AutoTaggingRequestError("proposal_ids can contain at most 1000 items.")
    result: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            raise AutoTaggingRequestError(
                "proposal_ids must contain non-empty strings."
            )
        doc_id = value.strip()
        if doc_id in seen:
            continue
        seen.add(doc_id)
        result.append(doc_id)
    return tuple(result)


def _normalize_batch_acceptance_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise AutoTaggingRequestError("acceptance_mode must be a string.")
    normalized = value.strip().lower()
    supported = {"low_risk_only", "recommended", "all_non_conflicting"}
    if normalized not in supported:
        raise AutoTaggingRequestError(
            "acceptance_mode must be low_risk_only, recommended, or "
            "all_non_conflicting."
        )
    return normalized


def _validate_accepted_tags_by_proposal(
    value: Mapping[str, Any] | None,
    proposal_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AutoTaggingRequestError("accepted_tags_by_proposal must be an object.")
    allowed = set(proposal_ids)
    unknown = {str(key) for key in value} - allowed
    if unknown:
        raise AutoTaggingRequestError(
            "accepted_tags_by_proposal contains unknown proposal ids: "
            + ", ".join(sorted(unknown))
        )
    return {str(doc_id): _sanitize_review_tags(tags) for doc_id, tags in value.items()}


def _sanitize_review_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AutoTaggingRequestError("Review tags must be an array.")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AutoTaggingRequestError("Review tags must be strings.")
        try:
            tag = sanitize_generated_tag(item)
        except (TypeError, ValueError) as exc:
            raise AutoTaggingRequestError(f"Invalid review tag: {item}") from exc
        if tag:
            cleaned.append(tag)
    return normalize_tags(cleaned)


def _review_snapshot(
    entry: Mapping[str, Any], annotation: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "doc_id": str(entry.get("doc_id") or annotation.get("doc_id") or ""),
        "entry": dict(entry),
        "annotation": dict(annotation),
    }


def _semantic_review_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_entry = value.get("entry")
    raw_annotation = value.get("annotation")
    entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
    annotation = dict(raw_annotation) if isinstance(raw_annotation, Mapping) else {}
    entry.pop("effective_tags", None)
    annotation.pop("updated_at", None)
    return {"entry": entry, "annotation": annotation}


def _cluster_identity_snapshot_state(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_entry = value.get("entry")
    raw_annotation = value.get("annotation")
    entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
    entry.pop("effective_tags", None)
    annotation: dict[str, Any] | None
    if isinstance(raw_annotation, Mapping):
        annotation = dict(raw_annotation)
        annotation.pop("updated_at", None)
    else:
        annotation = None
    return {"entry": entry, "annotation": annotation}


def _annotation_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return dict(raw)
    raise TypeError("Vision annotation must be a mapping or dataclass instance.")


def _annotation_plus_recommended(value: Any) -> bool:
    try:
        payload = _annotation_to_dict(value)
    except TypeError:
        return False
    return payload.get("plus_recommended") is True


def _normalize_annotation(value: Any) -> dict[str, Any]:
    payload = _annotation_to_dict(value)
    fields = _normalize_fields(payload.get("stable_fields", payload.get("fields")))
    raw_categories = payload.get("categories")
    category_mapping = raw_categories if isinstance(raw_categories, Mapping) else {}
    categories: dict[str, list[str]] = {}
    category_tags: list[str] = []
    for category, allowed_values in CATEGORY_TAGS.items():
        allowed = {
            key: FIELD_TAG_LABELS[code]
            for code in allowed_values
            for key in (
                _comparison_key(code),
                _comparison_key(FIELD_TAG_LABELS[code]),
            )
        }
        values = _iter_strings(category_mapping.get(category))
        normalized = _stable_strings(
            allowed[key]
            for value in values
            if (key := _comparison_key(value)) in allowed
        )
        categories[category] = normalized
        category_tags.extend(normalized)

    controlled_values = [
        *category_tags,
        *(
            label
            for field in fields.values()
            for label in _iter_strings(field.get("labels"))
        ),
        *_iter_strings(payload.get("controlled_tags")),
        *_iter_strings(payload.get("tags")),
    ]
    controlled_tags = _stable_strings(
        canonical
        for value in controlled_values
        if (canonical := _CONTROLLED_TAGS.get(_comparison_key(value))) is not None
    )
    entities = _normalize_entities(payload.get("entities"))
    review_reasons = _stable_strings(
        value.strip()
        for value in _iter_strings(payload.get("review_reasons"))
        if value.strip()
    )
    warnings = _stable_strings(
        value.strip()
        for value in _iter_strings(payload.get("warnings"))
        if value.strip()
    )
    suggested_tags = _stable_strings(
        tag
        for value in _iter_strings(payload.get("suggested_tags"))
        if (tag := _safe_generated_tag(value)) is not None
    )
    return {
        "schema_version": int(
            payload.get("schema_version") or AUTO_TAGGING_SCHEMA_VERSION
        ),
        "prompt_version": str(payload.get("prompt_version") or PROMPT_VERSION),
        "description": str(payload.get("description") or "").strip(),
        "fields": fields,
        "categories": categories,
        "entities": entities,
        "controlled_tags": controlled_tags,
        "suggested_tags": suggested_tags,
        "requires_review": payload.get("requires_review") is True,
        "review_reasons": review_reasons,
        "plus_recommended": payload.get("plus_recommended") is True,
        "warnings": warnings,
    }


def _normalize_fields(value: Any) -> dict[str, dict[str, Any]]:
    mapping = value if isinstance(value, Mapping) else {}
    fields: dict[str, dict[str, Any]] = {}
    for raw_name in sorted(mapping, key=lambda item: str(item)):
        raw_field = mapping[raw_name]
        if not isinstance(raw_field, Mapping):
            continue
        name = str(raw_name).strip()
        if not name:
            continue
        values = _iter_strings(raw_field.get("values"))
        labels = _iter_strings(raw_field.get("labels"))
        if values and not labels:
            labels = [FIELD_TAG_LABELS.get(code, code) for code in values]
        pairs = _field_pairs(values, labels)
        fields[name] = {
            "values": [code for code, _label in pairs],
            "labels": [label for _code, label in pairs],
            "confidence": _normalized_confidence(raw_field.get("confidence")),
        }
    return fields


def _normalize_entities(value: Any) -> dict[str, list[dict[str, Any]]]:
    mapping = value if isinstance(value, Mapping) else {}
    result: dict[str, list[dict[str, Any]]] = {}
    for entity_type in ENTITY_TYPES:
        raw_values = mapping.get(entity_type, ())
        values = (
            list(raw_values)
            if isinstance(raw_values, (list, tuple))
            else [raw_values]
            if raw_values
            else []
        )
        parsed = [
            entity for raw in values if (entity := _normalize_entity(raw)) is not None
        ]
        result[entity_type] = _merge_entity_values(parsed)
    return result


def _normalize_entity(value: Any) -> dict[str, Any] | None:
    try:
        payload = _annotation_to_dict(value)
    except TypeError:
        return None
    raw_name = payload.get("name")
    name = _safe_generated_tag(raw_name) if isinstance(raw_name, str) else None
    state = str(payload.get("state") or "unknown").strip().lower()
    if state not in _ENTITY_STATES:
        state = "unknown"
    evidence = _stable_strings(
        str(item).strip()
        for item in _iter_values(payload.get("evidence"))
        if str(item).strip()
    )
    confidence = _normalized_confidence(payload.get("confidence"))
    return {
        "name": name,
        "state": state,
        "evidence": evidence,
        "evidence_text": str(payload.get("evidence_text") or "").strip(),
        "confidence": confidence,
        "rejection_reason": str(payload.get("rejection_reason") or "").strip(),
    }


def _merge_annotations(flash: dict[str, Any], plus: dict[str, Any]) -> dict[str, Any]:
    field_review_reasons: list[str] = []
    fields = _merge_fields(
        flash.get("fields") or {},
        plus.get("fields") or {},
        review_reasons=field_review_reasons,
    )
    categories = {
        field_name: list(fields[field_name]["labels"]) for field_name in CATEGORY_TAGS
    }
    entities = {
        entity_type: _merge_entity_values(
            [
                *_entity_values((flash.get("entities") or {}).get(entity_type)),
                *_entity_values((plus.get("entities") or {}).get(entity_type)),
            ]
        )
        for entity_type in ENTITY_TYPES
    }
    return {
        "schema_version": max(
            int(flash.get("schema_version") or AUTO_TAGGING_SCHEMA_VERSION),
            int(plus.get("schema_version") or AUTO_TAGGING_SCHEMA_VERSION),
        ),
        "prompt_version": str(plus.get("prompt_version") or PROMPT_VERSION),
        "description": str(plus.get("description") or flash.get("description") or ""),
        "fields": fields,
        "categories": categories,
        "entities": entities,
        "controlled_tags": _stable_strings(
            label
            for field in fields.values()
            for label in _iter_strings(field.get("labels"))
        ),
        "suggested_tags": _stable_strings(
            [
                *_iter_strings(flash.get("suggested_tags")),
                *_iter_strings(plus.get("suggested_tags")),
            ]
        ),
        "requires_review": bool(
            flash.get("requires_review")
            or plus.get("requires_review")
            or field_review_reasons
        ),
        "review_reasons": _stable_strings(
            [
                *_iter_strings(flash.get("review_reasons")),
                *_iter_strings(plus.get("review_reasons")),
                *field_review_reasons,
            ]
        ),
        "plus_recommended": bool(
            flash.get("plus_recommended") or plus.get("plus_recommended")
        ),
        "warnings": _stable_strings(
            [
                *_iter_strings(flash.get("warnings")),
                *_iter_strings(plus.get("warnings")),
            ]
        ),
    }


def _merge_fields(
    flash: Mapping[str, Any],
    plus: Mapping[str, Any],
    *,
    review_reasons: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    reasons = review_reasons if review_reasons is not None else []
    result: dict[str, dict[str, Any]] = {}
    unknown_fields = sorted(
        {
            str(key).strip()
            for key in [*flash.keys(), *plus.keys()]
            if str(key).strip() not in FIELD_SPECS
        }
    )
    reasons.extend(f"field:{name}:invalid" for name in unknown_fields)

    for name, spec in FIELD_SPECS.items():
        raw_flash_field = flash.get(name)
        raw_plus_field = plus.get(name)
        flash_field: Mapping[str, Any] = (
            raw_flash_field if isinstance(raw_flash_field, Mapping) else {}
        )
        plus_field: Mapping[str, Any] = (
            raw_plus_field if isinstance(raw_plus_field, Mapping) else {}
        )
        if raw_flash_field is not None and not isinstance(raw_flash_field, Mapping):
            reasons.append(f"field:{name}:invalid")
        if raw_plus_field is not None and not isinstance(raw_plus_field, Mapping):
            reasons.append(f"field:{name}:invalid")

        flash_values, flash_confidence, flash_invalid = _field_input(
            name,
            flash_field,
        )
        plus_values, plus_confidence, plus_invalid = _field_input(
            name,
            plus_field,
        )
        if flash_invalid or plus_invalid:
            reasons.append(f"field:{name}:invalid")
        if len(flash_values) > spec.max_items or len(plus_values) > spec.max_items:
            reasons.append(f"field:{name}:overflow")

        flash_set = set(flash_values)
        plus_set = set(plus_values)
        if flash_set and plus_set and flash_set != plus_set:
            # Even when one model has a clearly higher field confidence, keep
            # an explicit audit signal instead of silently discarding the
            # disagreement.  Equal-confidence conflicts also deterministically
            # prefer Plus while remaining reviewable.
            reasons.append(f"field:{name}:conflict")

        if not spec.multiple:
            selected_values, selected_confidence = _select_single_field_value(
                flash_values,
                flash_confidence,
                plus_values,
                plus_confidence,
            )
            if len(flash_values) > 1 or len(plus_values) > 1:
                reasons.append(f"field:{name}:conflict")
        else:
            selected_values, selected_confidence = _select_multi_field_values(
                name,
                flash_values,
                flash_confidence,
                plus_values,
                plus_confidence,
            )
            if len(set(flash_values) | set(plus_values)) > spec.max_items:
                reasons.append(f"field:{name}:overflow")

        result[name] = {
            "values": selected_values[: spec.max_items],
            "labels": [FIELD_TAG_LABELS[code] for code in selected_values][
                : spec.max_items
            ],
            "confidence": selected_confidence,
        }
    return result


def _field_input(
    field_name: str,
    field: Mapping[str, Any],
) -> tuple[list[str], float | None, bool]:
    spec = FIELD_SPECS[field_name]
    allowed = {
        key: code
        for code in spec.values
        for key in (
            _comparison_key(code),
            _comparison_key(FIELD_TAG_LABELS[code]),
        )
    }
    raw_values = _iter_strings(field.get("values"))
    if not raw_values:
        raw_values = _iter_strings(field.get("labels"))
    values: list[str] = []
    invalid = False
    for raw_value in raw_values:
        code = allowed.get(_comparison_key(raw_value))
        if code is None:
            invalid = True
            continue
        if code not in values:
            values.append(code)
    field_order = {code: index for index, code in enumerate(spec.values)}
    values.sort(key=field_order.__getitem__)
    return values, _normalized_confidence(field.get("confidence")), invalid


def _select_single_field_value(
    flash_values: list[str],
    flash_confidence: float | None,
    plus_values: list[str],
    plus_confidence: float | None,
) -> tuple[list[str], float | None]:
    flash_value = flash_values[0] if flash_values else None
    plus_value = plus_values[0] if plus_values else None
    if flash_value is None and plus_value is None:
        confidences = [
            value for value in (flash_confidence, plus_confidence) if value is not None
        ]
        return [], max(confidences) if confidences else None
    if flash_value is None:
        assert plus_value is not None
        return [plus_value], plus_confidence
    if plus_value is None:
        return [flash_value], flash_confidence
    if flash_value == plus_value:
        confidences = [
            value for value in (flash_confidence, plus_confidence) if value is not None
        ]
        return [flash_value], max(confidences) if confidences else None

    flash_score = -1.0 if flash_confidence is None else flash_confidence
    plus_score = -1.0 if plus_confidence is None else plus_confidence
    if flash_score > plus_score:
        return [flash_value], flash_confidence
    # Plus is the deterministic tie-break because it is the explicit second
    # pass used to resolve an uncertain Flash result.
    return [plus_value], plus_confidence


def _select_multi_field_values(
    field_name: str,
    flash_values: list[str],
    flash_confidence: float | None,
    plus_values: list[str],
    plus_confidence: float | None,
) -> tuple[list[str], float | None]:
    spec = FIELD_SPECS[field_name]
    field_order = {code: index for index, code in enumerate(spec.values)}
    candidates: dict[str, tuple[float, int]] = {}
    for source_priority, values, confidence in (
        (0, flash_values, flash_confidence),
        (1, plus_values, plus_confidence),
    ):
        score = -1.0 if confidence is None else confidence
        for code in values:
            candidates[code] = max(
                candidates.get(code, (-1.0, -1)),
                (score, source_priority),
            )
    selected = sorted(
        candidates,
        key=lambda code: (
            -candidates[code][0],
            -candidates[code][1],
            field_order[code],
        ),
    )[: spec.max_items]
    if not selected:
        confidences = [
            value for value in (flash_confidence, plus_confidence) if value is not None
        ]
        return [], max(confidences) if confidences else None

    source_confidences = {
        0: flash_confidence,
        1: plus_confidence,
    }
    contributing = {candidates[code][1] for code in selected}
    contributing_confidences = [source_confidences[source] for source in contributing]
    # A merged set is only as trustworthy as its least-confident contributing
    # source.  Do not make a low-confidence Flash-only value look like it has
    # Plus's higher confidence merely because both occur in one result field.
    if any(value is None for value in contributing_confidences):
        return selected, None
    return selected, min(
        value for value in contributing_confidences if value is not None
    )


def _merge_entity_values(
    values: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [dict(value) for value in values if isinstance(value, Mapping)]
    named: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unnamed: list[dict[str, Any]] = []
    for value in normalized:
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            named[_comparison_key(name)].append(value)
        else:
            unnamed.append(value)

    if len(named) > 1:
        conflicts = [
            {**_best_entity(group), "state": "conflict"}
            for _key, group in sorted(named.items())
        ]
        return sorted(conflicts, key=_entity_output_key)
    if len(named) == 1:
        return [_best_entity(next(iter(named.values())))]
    if unnamed:
        return [_best_entity(unnamed)]
    return []


def _best_entity(values: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(values, key=_entity_preference_key, reverse=True)
    selected = dict(ordered[0])
    selected["evidence"] = _stable_strings(
        evidence
        for value in ordered
        for evidence in _iter_strings(value.get("evidence"))
    )
    confidences = [
        confidence
        for value in ordered
        if (confidence := _normalized_confidence(value.get("confidence"))) is not None
    ]
    selected["confidence"] = max(confidences) if confidences else None
    if any(value.get("state") == "conflict" for value in ordered):
        selected["state"] = "conflict"
    return selected


def _entity_preference_key(value: dict[str, Any]) -> tuple[Any, ...]:
    evidence = set(_iter_strings(value.get("evidence")))
    evidence_priority = (
        3
        if "manual_tag" in evidence
        else 2
        if evidence & _EXPLICIT_EVIDENCE
        else 1
        if "visual" in evidence
        else 0
    )
    state_priority = {
        "confirmed": 4,
        "suggested": 3,
        "conflict": 2,
        "unknown": 1,
        "unable_to_confirm": 1,
        "not_applicable": 1,
    }.get(str(value.get("state")), 0)
    confidence = _normalized_confidence(value.get("confidence"))
    return (
        evidence_priority,
        state_priority,
        -1.0 if confidence is None else confidence,
        _comparison_key(str(value.get("name") or "")),
        str(value.get("evidence_text") or ""),
    )


def _entity_output_key(value: dict[str, Any]) -> tuple[str, str]:
    return (
        _comparison_key(str(value.get("name") or "")),
        str(value.get("state") or ""),
    )


def _revalidate_annotation_for_context(
    value: Any,
    context: TaggingContext,
) -> dict[str, Any]:
    """Recheck context-dependent entities without changing the shared cache.

    The API result is cached by image/model/prompt/schema so identical bytes can
    be reused across Collections.  Explicit evidence is Collection-local,
    however.  This function always builds a normalized copy and removes or
    downgrades names whose claimed evidence is absent from the current context.
    """

    structured = _normalize_annotation(value)
    reasons = list(_iter_strings(structured.get("review_reasons")))
    entities: dict[str, list[dict[str, Any]]] = {}
    raw_entities = structured.get("entities")
    entity_mapping = raw_entities if isinstance(raw_entities, Mapping) else {}
    for entity_type in ENTITY_TYPES:
        revalidated: list[dict[str, Any]] = []
        for raw_entity in _entity_values(entity_mapping.get(entity_type)):
            entity = dict(raw_entity)
            name = entity.get("name")
            if not isinstance(name, str) or not name.strip():
                revalidated.append(entity)
                continue

            evidence_text = str(entity.get("evidence_text") or "").strip()
            claimed_evidence = _stable_strings(_iter_strings(entity.get("evidence")))
            current_matches = [
                source
                for source in _CONTEXT_EVIDENCE_SOURCES
                if _context_supports_entity(
                    source,
                    name=name,
                    evidence_text=evidence_text,
                    context=context,
                )
            ]
            stale_explicit = (set(claimed_evidence) & _EXPLICIT_EVIDENCE) - set(
                current_matches
            )
            usable_evidence = _stable_strings(
                [
                    *(
                        source
                        for source in claimed_evidence
                        if source in {"visual", "none"} or source in current_matches
                    ),
                    *current_matches,
                ]
            )
            entity["evidence"] = usable_evidence or ["none"]

            rejection_reason = str(entity.get("rejection_reason") or "").strip()
            if entity_type in {"real_person", "cosplayer"} and not current_matches:
                entity["name"] = None
                entity["state"] = "unable_to_confirm"
                entity["rejection_reason"] = _join_rejection_reason(
                    rejection_reason,
                    "explicit identity evidence is unavailable in the current context",
                )
                reasons.append(f"entity:{entity_type}:context_mismatch")
            elif (
                entity_type in {"character", "work"}
                and entity.get("state") == "confirmed"
                and stale_explicit
                and not current_matches
            ):
                entity["state"] = "suggested"
                entity["rejection_reason"] = _join_rejection_reason(
                    rejection_reason,
                    "cached explicit evidence is unavailable in the current context",
                )
                reasons.append(f"entity:{entity_type}:context_mismatch")
            elif stale_explicit:
                # The entity is already non-confirmed, or another current
                # source still supports it.  Preserve that state but record the
                # provenance change for the reviewer.
                reasons.append(f"entity:{entity_type}:context_changed")
            revalidated.append(entity)
        entities[entity_type] = _merge_entity_values(revalidated)

    structured["entities"] = entities
    structured["review_reasons"] = _stable_strings(reasons)
    structured["requires_review"] = bool(
        structured.get("requires_review") or structured["review_reasons"]
    )
    return structured


def _context_supports_entity(
    source: str,
    *,
    name: str,
    evidence_text: str,
    context: TaggingContext,
) -> bool:
    source_values: dict[str, Any] = {
        "manual_tag": context.manual_tags,
        "folder_name": context.folder_name,
        "file_name": context.file_name,
        "sidecar": context.sidecar_metadata,
        "ocr": context.ocr_text,
        "watermark": context.watermark_text,
    }
    haystack = _flatten_context_text(source_values.get(source))
    if not haystack:
        return False
    normalized_haystack = _comparison_key(haystack)
    return any(
        needle and needle in normalized_haystack
        for needle in (
            _comparison_key(name),
            _comparison_key(evidence_text),
        )
    )


def _flatten_context_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(
            _flatten_context_text(item)
            for key in sorted(value, key=lambda item: str(item))
            for item in (key, value[key])
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_flatten_context_text(item) for item in value)
    return str(value).strip()


def _join_rejection_reason(current: str, added: str) -> str:
    if not current:
        return added
    if _comparison_key(added) in _comparison_key(current):
        return current
    return f"{current}; {added}"


def _finalize_structured_annotation(value: dict[str, Any]) -> dict[str, Any]:
    structured = dict(value)
    field_reasons: list[str] = []
    raw_fields = structured.get("fields")
    field_mapping: Mapping[str, Any] = (
        raw_fields if isinstance(raw_fields, Mapping) else {}
    )
    fields = _merge_fields(
        field_mapping,
        {},
        review_reasons=field_reasons,
    )
    entities = {
        entity_type: _merge_entity_values(
            _entity_values((structured.get("entities") or {}).get(entity_type))
        )
        for entity_type in ENTITY_TYPES
    }
    reasons = [
        *(
            reason
            for reason in _iter_strings(structured.get("review_reasons"))
            if _identity_review_reason_requires_manual(reason)
        ),
        *field_reasons,
    ]
    for entity_type, values in entities.items():
        if any(item.get("state") == "conflict" for item in values):
            reasons.append(f"entity_conflict:{entity_type}")
    structured["fields"] = fields
    structured["categories"] = {
        field_name: list(fields[field_name]["labels"]) for field_name in CATEGORY_TAGS
    }
    structured["entities"] = entities
    structured["review_reasons"] = _stable_strings(reasons)
    structured["requires_review"] = bool(structured["review_reasons"])
    structured["controlled_tags"] = _stable_strings(
        label
        for field in fields.values()
        for label in _iter_strings(field.get("labels"))
    )
    return structured


def _identity_review_reason_requires_manual(reason: str) -> bool:
    normalized = _comparison_key(reason)
    if normalized.startswith(
        (
            "unconfirmed_entity:",
            "low_confidence_entity:",
            "missing_explicit_identity_evidence:",
        )
    ):
        return False
    if normalized.startswith("entity:"):
        return any(
            marker in normalized
            for marker in (":conflict", ":context_mismatch", ":context_changed")
        )
    return True


def _add_review_reason(structured: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = dict(structured)
    updated["review_reasons"] = _stable_strings(
        [*_iter_strings(structured.get("review_reasons")), reason]
    )
    updated["requires_review"] = True
    return updated


def _proposed_tags(
    entry: dict[str, Any],
    structured: dict[str, Any],
    rejected_tags: Iterable[str],
) -> tuple[str, ...]:
    candidates = list(_iter_strings(structured.get("controlled_tags")))
    entities = structured.get("entities") or {}
    for entity_type in ENTITY_TYPES:
        values = _entity_values(entities.get(entity_type))
        entity_type_conflict = any(item.get("state") == "conflict" for item in values)
        for item in values:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            if entity_type_conflict:
                evidence = set(_iter_strings(item.get("evidence")))
                if entity_type in {"real_person", "cosplayer"} and not (
                    evidence & _EXPLICIT_EVIDENCE
                ):
                    continue
                candidates.append(name)
                continue
            if _identity_entity_is_auto_acceptable(
                entity_type,
                item,
                entity_type_conflict=False,
            ):
                candidates.append(name)

    excluded = {
        _comparison_key(tag)
        for tag in [
            *_iter_strings(entry.get("tags")),
            *_iter_strings(entry.get("folder_tags")),
            *_iter_strings(entry.get("accepted_auto_tags")),
            *_iter_strings(entry.get("inherited_tags")),
            *_iter_strings(rejected_tags),
        ]
    }
    return normalize_tags(
        tag
        for tag in _stable_strings(candidates)
        if _comparison_key(tag) not in excluded
    )


def _identity_entity_is_auto_acceptable(
    entity_type: str,
    entity: Mapping[str, Any],
    *,
    entity_type_conflict: bool,
) -> bool:
    if entity_type_conflict or entity.get("state") != "confirmed":
        return False
    confidence = _normalized_confidence(entity.get("confidence"))
    if confidence is None or confidence < HIGH_CONFIDENCE_ENTITY_THRESHOLD:
        return False
    if str(entity.get("rejection_reason") or "").strip():
        return False
    if entity_type in {"real_person", "cosplayer"} and not (
        set(_iter_strings(entity.get("evidence"))) & _EXPLICIT_EVIDENCE
    ):
        return False
    return bool(str(entity.get("name") or "").strip())


def _build_policy(
    selected_model: str,
    structured: dict[str, Any],
    steps: list[_ModelResolution],
) -> dict[str, Any]:
    model_trace = [step.model for step in steps]
    resolved_model = next(
        (step.model for step in reversed(steps) if step.annotation is not None),
        "",
    )
    return {
        "policy_version": POLICY_VERSION,
        "selected_model": selected_model,
        "model_chain": model_trace,
        "model_trace": model_trace,
        "resolved_model": resolved_model,
        "model_steps": [_model_step(step) for step in steps],
        "escalated": len(steps) > 1,
        "requires_review": bool(structured.get("requires_review")),
        "review_required": bool(structured.get("requires_review")),
        "review_reasons": list(structured.get("review_reasons", ())),
        "plus_recommended": bool(structured.get("plus_recommended")),
        "entity_confidence_threshold": HIGH_CONFIDENCE_ENTITY_THRESHOLD,
    }


def _model_step(step: _ModelResolution) -> dict[str, Any]:
    return {
        "model": step.model,
        "cache_key": step.cache_key,
        "status": step.status,
        "cached": step.cached,
        "api_called": not step.cached and step.status in {"succeeded", "failed"},
        "request_id": step.request_id,
        "error": step.error,
    }


def _entity_values(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _field_pairs(values: list[str], labels: list[str]) -> list[tuple[str, str]]:
    if not values and labels:
        values = list(labels)
    if values and not labels:
        labels = list(values)
    count = min(len(values), len(labels))
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for code, label in zip(values[:count], labels[:count], strict=True):
        normalized_code = code.strip()
        normalized_label = label.strip()
        if not normalized_code or not normalized_label:
            continue
        key = (_comparison_key(normalized_code), _comparison_key(normalized_label))
        if key in seen:
            continue
        seen.add(key)
        result.append((normalized_code, normalized_label))
    return result


def _iter_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _iter_strings(value: Any) -> list[str]:
    return [item for item in _iter_values(value) if isinstance(item, str)]


def _stable_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized:
            continue
        key = _comparison_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _safe_generated_tag(value: str) -> str | None:
    try:
        return sanitize_generated_tag(value)
    except (ValueError, TypeError):
        return None


def _normalized_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence):
        return None
    return min(1.0, max(0.0, confidence))


def _group_by_sha(
    entries: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        groups[str(entry["sha256"])].append(entry)
    return dict(groups)


def _stream_request_bytes(entry: Mapping[str, Any]) -> int:
    source_bytes = max(0, int(entry.get("size_bytes") or 0))
    # Base64 expansion plus a conservative prompt/JSON envelope allowance.
    return 4 * ((source_bytes + 2) // 3) + 1024 * 1024


def _release_flight_when_done(
    future: Future[_CompletedModelRequest],
    flight: AutoTagCacheFlight,
) -> None:
    """Retire a cancelled claim now or a running claim when network work exits."""

    if future.cancel():
        flight.release()
        return
    future.add_done_callback(lambda _completed: flight.release())


def _aggregate_context(entries: list[dict[str, Any]]) -> TaggingContext:
    return TaggingContext(
        folder_name=" | ".join(
            dict.fromkeys(
                tag for entry in entries for tag in entry.get("folder_tags", ())
            )
        ),
        file_name=" | ".join(
            dict.fromkeys(str(entry["file_name"]) for entry in entries)
        ),
        manual_tags=normalize_tags(
            tag for entry in entries for tag in entry.get("tags", ())
        ),
    )


def _cache_key(content_hash: str, model: str) -> str:
    return make_auto_tag_cache_key(
        content_hash,
        model,
        PROMPT_VERSION,
        AUTO_TAGGING_SCHEMA_VERSION,
    )


def _cached_model_resolution(
    cached: Mapping[str, Any],
    context: TaggingContext,
    model: str,
    cache_key: str,
) -> _ModelResolution:
    cached_annotation = (
        _revalidate_annotation_for_context(cached["annotation"], context)
        if cached["status"] == "succeeded"
        else None
    )
    return _ModelResolution(
        model=model,
        cache_key=cache_key,
        status=str(cached["status"]),
        annotation=cached_annotation,
        cached=True,
        request_id=str(cached.get("request_id") or ""),
        error=str(cached.get("error") or ""),
    )


def _normalize_model(
    value: str | None,
    model_configuration: ModelConfiguration | None = None,
) -> str:
    configuration = model_configuration or default_model_configuration()
    try:
        return configuration.resolve_auto_tag_model(value)
    except ValueError as exc:
        raise AutoTaggingRequestError(str(exc)) from exc


def _pricing_for_model(
    model: str,
    model_configuration: ModelConfiguration | None = None,
) -> VisionPricing:
    configuration = model_configuration or default_model_configuration()
    definition = configuration.by_id.get(model)
    if definition is None or definition.pricing is None:
        raise AutoTaggingRequestError(
            f"Visual model {model!r} has no configured pricing metadata."
        )
    if model == FLASH_MODEL:
        prefix = "FLASH"
    elif model == PLUS_MODEL:
        prefix = "PLUS"
    else:
        prefix = re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")
    default_input = definition.pricing.input_yuan_per_million
    default_output = definition.pricing.output_yuan_per_million
    try:
        input_price = float(
            os.getenv(
                f"DASHSCOPE_VISION_{prefix}_INPUT_YUAN_PER_MILLION",
                str(default_input),
            )
        )
        output_price = float(
            os.getenv(
                f"DASHSCOPE_VISION_{prefix}_OUTPUT_YUAN_PER_MILLION",
                str(default_output),
            )
        )
    except ValueError as exc:
        raise AutoTaggingRequestError("Visual model pricing is invalid.") from exc
    return VisionPricing(
        input_price,
        output_price,
        effective_from=os.getenv(
            f"DASHSCOPE_VISION_{prefix}_PRICING_EFFECTIVE_FROM",
            os.getenv(
                "DASHSCOPE_VISION_PRICING_EFFECTIVE_FROM",
                definition.pricing.effective_from,
            ),
        ),
    )


def _vision_failure_kind(error: VisionTaggingError) -> FailureKind:
    """Classify provider failures without treating a service outage as bad media."""

    if bool(getattr(error, "retryable", False)):
        return "retryable"
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return "systemic"
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return "retryable"
    message = str(error).casefold()
    if any(token in message for token in ("timed out", "timeout", "connection")):
        return "retryable"
    return "item"


def _annotation_failure_category(error: str, kind: FailureKind) -> str:
    text = str(error or "").casefold()
    if any(
        marker in text
        for marker in (
            "data_inspection_failed",
            "datainspectionfailed",
            "inappropriate content",
            "content policy",
        )
    ):
        return "content_policy"
    if any(
        marker in text
        for marker in (
            "invalid api key",
            "invalidapikey",
            "unauthorized",
            "authentication",
            "permission denied",
        )
    ):
        return "provider_auth"
    if any(
        marker in text
        for marker in (
            "description must",
            "model annotation",
            "schema_version",
            "fields.",
            "fields must",
            "invalid fields",
            "entities.",
            "entities must",
        )
    ):
        return "schema_response"
    if any(
        marker in text
        for marker in (
            "provided url does not appear to be valid",
            "image size should be",
            "file size is too large",
            "max bytes per data-uri",
            "cannot encode image",
            "cannot transcode image",
        )
    ):
        return "invalid_input"
    if kind == "retryable" or any(
        marker in text
        for marker in ("timed out", "timeout", "connection", "temporarily unavailable")
    ):
        return "retryable_transport"
    if kind == "systemic":
        return "systemic"
    return "unknown_item"


def _failure_category_is_retryable(category: str) -> bool:
    return category not in {"content_policy", "provider_auth", "systemic"}


def _failed_annotation_is_retryable(annotation: Mapping[str, Any]) -> bool:
    policy = annotation.get("policy")
    policy_mapping = policy if isinstance(policy, Mapping) else {}
    explicit = policy_mapping.get("retry_eligible")
    if isinstance(explicit, bool):
        return explicit
    category = str(policy_mapping.get("failure_category") or "").strip()
    if not category:
        category = _annotation_failure_category(
            str(annotation.get("error") or ""),
            "item",
        )
    return _failure_category_is_retryable(category)


def _validate_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoTaggingRequestError("max_images must be an integer.")
    if not 1 <= value <= MAX_AUTO_TAG_LIMIT:
        raise AutoTaggingRequestError(
            f"max_images must be between 1 and {MAX_AUTO_TAG_LIMIT}."
        )
    return value


def _validate_pending_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AutoTaggingRequestError("offset must be a non-negative integer.")
    return value


def _validate_pending_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoTaggingRequestError("limit must be an integer.")
    if not 1 <= value <= MAX_PENDING_REVIEW_LIMIT:
        raise AutoTaggingRequestError(
            f"limit must be between 1 and {MAX_PENDING_REVIEW_LIMIT}."
        )
    return value


def _validate_budget(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AutoTaggingRequestError("max_budget_cny must be positive.")
    return float(value)


def _record_from_entry(
    entry: dict[str, Any], resolver: SourcePathResolver
) -> ImageRecord:
    path: Path = resolver.resolve_fields(entry)
    return ImageRecord(
        doc_id=str(entry["doc_id"]),
        root_id=str(entry["root_id"]),
        relative_path=str(entry["relative_path"]),
        absolute_path=str(path),
        file_name=str(entry["file_name"]),
        extension=str(entry["extension"]),
        mime_type=str(entry["mime_type"]),
        sha256=str(entry["sha256"]),
        size_bytes=int(entry["size_bytes"]),
        mtime_ns=int(entry["mtime_ns"]),
        width=int(entry["width"]),
        height=int(entry["height"]),
    )
