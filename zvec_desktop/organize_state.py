"""State and payload adapters for the pure-Python organize workbench.

The module deliberately contains no UI toolkit code and never submits a job.
It converts the persistent backend's ``auto_tag_pending``/review/alias payloads
into conservative desktop state.  A malformed proposal is isolated as a
structured issue instead of making the remaining page unusable, but malformed
risk or identity data can never enter a batch-accept request.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, cast

from .backend_api import JsonObject
from .library_tasks import (
    AutoTagReviewDecision,
    AutoTagReviewFilters,
    AutoTagReviewRequest,
    AutoTagReviewState,
    IdentityConfirmationRequest,
    LowRiskBatchReviewRequest,
)

ReviewDecision = Literal["pending", "accept", "edit", "reject", "manual"]
ReviewBucket = Literal["pending", "low_risk", "identity", "conflict", "failed"]
EntityType = Literal["real_person", "cosplayer", "character", "work"]
ThumbnailStatus = Literal["ready", "missing_path", "missing_file"]

_REVIEW_DECISIONS = frozenset({"pending", "accept", "edit", "reject", "manual"})
_REVIEW_STATES = frozenset({"all", "low_risk", "identity", "conflict", "failed"})
_ENTITY_TYPES: tuple[EntityType, ...] = (
    "real_person",
    "cosplayer",
    "character",
    "work",
)
_IDENTITY_SOURCES = frozenset({"entity", "model_entity"})
_LOW_RISK_NAMES = frozenset({"low", "low_risk"})
_CONFLICT_MARKERS = (
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
_MAX_PAGE_SIZE = 500
_WORKBENCH_TARGET = 100
_MAX_TAGS = 10_000
_MAX_TAG_LENGTH = 4_096
_MAX_ALIAS_COUNT = 100
_MAX_ALIAS_LENGTH = 256
_MAX_FILTER_LENGTH = 128


class OrganizeStateError(RuntimeError):
    """Base class for organize-workbench state failures."""


@dataclass(frozen=True, slots=True)
class OrganizeDataIssue:
    code: str
    message: str
    path: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": dict(self.details),
        }


class OrganizeDataError(OrganizeStateError):
    """The backend returned a field that cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        code: str = "invalid_organize_payload",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.details = dict(details or {})
        super().__init__(message)

    @property
    def issue(self) -> OrganizeDataIssue:
        return OrganizeDataIssue(
            code=self.code,
            message=str(self),
            path=self.path,
            details=self.details,
        )

    def to_dict(self) -> JsonObject:
        return self.issue.to_dict()


class OrganizeValidationError(OrganizeStateError, ValueError):
    """A local review or alias edit is unsafe or incomplete."""

    def __init__(
        self,
        message: str,
        *,
        field_name: str | None = None,
        code: str = "invalid_organize_state",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.field_name = field_name
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "code": self.code,
            "message": str(self),
            "details": dict(self.details),
        }
        if self.field_name is not None:
            payload["field"] = self.field_name
        return payload


@dataclass(frozen=True, slots=True)
class ThumbnailReference:
    """A source image reference suitable for the asynchronous image loader."""

    source_text: str
    path: Path | None
    status: ThumbnailStatus

    @property
    def available(self) -> bool:
        return self.status == "ready" and self.path is not None


@dataclass(frozen=True, slots=True)
class TagSourceDetail:
    tag: str
    source: str
    sources: tuple[str, ...]
    source_label: str
    risk: str
    identity: bool
    field_name: str | None
    entity_type: str | None
    already_present: bool
    requires_individual_confirmation: bool
    confidence: float | None

    @property
    def is_identity(self) -> bool:
        source_values = {_key(self.source), *(_key(value) for value in self.sources)}
        return bool(
            self.identity
            or self.requires_individual_confirmation
            or source_values & _IDENTITY_SOURCES
            or _key(self.risk) in {"identity", "conflict"}
            or _key(self.entity_type or "") in _ENTITY_TYPES
        )

    @property
    def is_low_risk(self) -> bool:
        return _key(self.risk) in _LOW_RISK_NAMES and not self.is_identity

    @property
    def effective_source_label(self) -> str:
        if self.source_label:
            return self.source_label
        labels = {
            "manual": "人工标签",
            "folder": "文件夹标签",
            "accepted_auto": "已接受模型标签",
            "field": "模型字段",
            "model_field": "模型字段",
            "entity": "模型身份",
            "model_entity": "模型身份",
            "alias": "别名",
            "existing": "现有标签",
        }
        values = self.sources or ((self.source,) if self.source else ())
        resolved = tuple(
            dict.fromkeys(labels.get(_key(value), value) for value in values if value)
        )
        return "+".join(resolved) if resolved else "来源未记录"


@dataclass(frozen=True, slots=True)
class StableField:
    name: str
    values: tuple[str, ...]
    labels: tuple[str, ...]
    confidence: float

    @property
    def filter_values(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.values, *self.labels)))


@dataclass(frozen=True, slots=True)
class IdentityEntity:
    entity_type: EntityType
    name: str | None
    state: str
    evidence: tuple[str, ...]
    evidence_text: str | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class IdentityChoice:
    tag: str
    entity_type: str


@dataclass(frozen=True, slots=True)
class TagDifference:
    retained: tuple[str, ...]
    suggested_additions: tuple[str, ...]
    low_risk_additions: tuple[str, ...]
    identity_additions: tuple[str, ...]
    already_present_suggestions: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.suggested_additions)


@dataclass(frozen=True, slots=True)
class OrganizeProposal:
    proposal_id: str
    relative_path: str
    thumbnail: ThumbnailReference
    description: str
    status: str
    error: str
    failure_category: str
    manual_tags: tuple[str, ...]
    folder_tags: tuple[str, ...]
    existing_tags: tuple[str, ...]
    proposed_tags: tuple[str, ...]
    low_risk_tags: tuple[str, ...]
    identity_tags: tuple[str, ...]
    batch_safe_tags: tuple[str, ...]
    review_draft_tags: tuple[str, ...]
    tag_details: tuple[TagSourceDetail, ...]
    fields: Mapping[str, StableField]
    entities: Mapping[EntityType, tuple[IdentityEntity, ...]]
    identity_choices: tuple[IdentityChoice, ...]
    review_reasons: tuple[str, ...]
    review_buckets: frozenset[ReviewBucket]
    difference: TagDifference

    @property
    def source_path(self) -> Path | None:
        return self.thumbnail.path

    @property
    def is_failed(self) -> bool:
        return "failed" in self.review_buckets

    @property
    def has_identity_tags(self) -> bool:
        return bool(self.identity_tags)

    @property
    def can_batch_accept(self) -> bool:
        return not self.is_failed and bool(self.batch_safe_tags)

    def entity_names(self, entity_type: EntityType) -> tuple[str, ...]:
        return tuple(
            entity.name for entity in self.entities.get(entity_type, ()) if entity.name
        )


@dataclass(frozen=True, slots=True)
class OrganizeFilters:
    """Workbench filter state; the backend applies ``latest_index_only``."""

    latest_index_only: bool = False
    character: str = ""
    work: str = ""
    action: str = ""
    expression: str = ""
    review_state: str = "all"

    def __post_init__(self) -> None:
        if not isinstance(self.latest_index_only, bool):
            raise _validation(
                "latest_index_only must be a boolean.", "latest_index_only"
            )
        for name in ("character", "work", "action", "expression"):
            value = _filter_text(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if not isinstance(self.review_state, str):
            raise _validation("review_state must be a string.", "review_state")
        state = self.review_state.strip().lower() or "all"
        state = {"pending": "all", "pending_review": "all"}.get(state, state)
        if state not in _REVIEW_STATES:
            raise _validation(
                "review_state must be all, low_risk, identity, conflict, or failed.",
                "review_state",
                details={"supported": sorted(_REVIEW_STATES)},
            )
        object.__setattr__(self, "review_state", state)

    @property
    def is_active(self) -> bool:
        return bool(
            self.latest_index_only
            or self.character
            or self.work
            or self.action
            or self.expression
            or self.review_state != "all"
        )

    def to_backend_filters(self) -> AutoTagReviewFilters:
        return AutoTagReviewFilters(
            latest_index_only=self.latest_index_only,
            character=self.character,
            work=self.work,
            action=self.action,
            expression=self.expression,
            review_state=cast(AutoTagReviewState, self.review_state),
        )

    def matches_loaded(
        self,
        proposal: OrganizeProposal,
        aliases: AliasCatalog | None = None,
    ) -> bool:
        """Apply content filters to an already backend-scoped page.

        Membership in the latest index run is not present on individual
        proposals, so that one flag is represented in the backend request and
        intentionally not guessed here.
        """

        if self.character and not _matches_names(
            proposal.entity_names("character"), self.character, aliases
        ):
            return False
        if self.work and not _matches_names(
            proposal.entity_names("work"), self.work, aliases
        ):
            return False
        for query, field_name in (
            (self.action, "action"),
            (self.expression, "expression"),
        ):
            if query:
                field_value = proposal.fields.get(field_name)
                if field_value is None or not _contains_query(
                    field_value.filter_values, query
                ):
                    return False
        return (
            self.review_state == "all" or self.review_state in proposal.review_buckets
        )

    @classmethod
    def from_backend(cls, value: object) -> OrganizeFilters:
        if value is None:
            return cls()
        mapping = _mapping(value, "filters")
        return cls(
            latest_index_only=_optional_bool(
                mapping,
                "latest_index_only",
                "filters.latest_index_only",
                default=False,
            ),
            character=_optional_string(
                mapping, "character", "filters.character", default=""
            ),
            work=_optional_string(mapping, "work", "filters.work", default=""),
            action=_optional_string(mapping, "action", "filters.action", default=""),
            expression=_optional_string(
                mapping, "expression", "filters.expression", default=""
            ),
            review_state=_optional_string(
                mapping, "review_state", "filters.review_state", default="all"
            ),
        )


@dataclass(frozen=True, slots=True)
class PaginationState:
    pending_count: int
    total_count: int
    offset: int
    limit: int
    returned_count: int
    has_more: bool

    @property
    def current_page(self) -> int:
        return 0 if self.pending_count == 0 else self.offset // self.limit + 1

    @property
    def page_count(self) -> int:
        if self.pending_count == 0:
            return 0
        return (self.pending_count + self.limit - 1) // self.limit

    @property
    def first_item_number(self) -> int:
        return 0 if self.returned_count == 0 else self.offset + 1

    @property
    def last_item_number(self) -> int:
        return self.offset + self.returned_count

    @property
    def previous_offset(self) -> int | None:
        return None if self.offset == 0 else max(0, self.offset - self.limit)

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.limit if self.has_more else None

    def last_valid_offset(self) -> int:
        if self.pending_count <= 0:
            return 0
        return (self.pending_count - 1) // self.limit * self.limit


@dataclass(frozen=True, slots=True)
class OrganizePage:
    proposals: tuple[OrganizeProposal, ...]
    pagination: PaginationState
    filters: OrganizeFilters
    undo_available: bool
    issues: tuple[OrganizeDataIssue, ...] = ()

    @property
    def skipped_count(self) -> int:
        return len(self.issues)


@dataclass(slots=True)
class ProposalReviewState:
    decision: ReviewDecision
    initial_tags: tuple[str, ...]
    edited_tags: tuple[str, ...]
    confirmed_identity_tags: set[str] = field(default_factory=set)
    batch_selected: bool = False

    @property
    def dirty(self) -> bool:
        return bool(
            self.decision != "pending"
            or self.edited_tags != self.initial_tags
            or self.confirmed_identity_tags
            or self.batch_selected
        )


@dataclass(frozen=True, slots=True)
class ReviewProgress:
    page_items: int
    reviewed_items: int
    remaining_items: int
    batch_selected_items: int
    target_items: int = _WORKBENCH_TARGET

    @property
    def target_fraction(self) -> float:
        if self.target_items <= 0:
            return 1.0
        return min(1.0, self.reviewed_items / self.target_items)

    @property
    def target_reached(self) -> bool:
        return self.reviewed_items >= self.target_items


@dataclass(frozen=True, slots=True)
class IdentityExclusion:
    proposal_id: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchOperationState:
    batch_id: str
    accepted: int
    updated: int
    identity_excluded_count: int
    identity_exclusions: tuple[IdentityExclusion, ...]
    undo_available: bool


@dataclass(frozen=True, slots=True)
class UndoOperationState:
    undone: bool
    updated: int
    batch_id: str
    undo_available: bool


@dataclass(frozen=True, slots=True)
class ReviewFailure:
    proposal_id: str
    error: str


@dataclass(frozen=True, slots=True)
class ReviewOperationState:
    accepted: int
    rejected: int
    updated: int
    failed: int
    pending_count: int
    failures: tuple[ReviewFailure, ...]
    undo_available: bool


class OrganizeReviewSession:
    """Mutable draft state for one workbench page (normally 100 images)."""

    def __init__(self, page: OrganizePage) -> None:
        if not isinstance(page, OrganizePage):
            raise TypeError("page must be an OrganizePage")
        self.page = page
        self._proposals = {item.proposal_id: item for item in page.proposals}
        self._states = {
            item.proposal_id: ProposalReviewState(
                decision="pending",
                initial_tags=(
                    item.manual_tags if item.is_failed else item.review_draft_tags
                ),
                edited_tags=(
                    item.manual_tags if item.is_failed else item.review_draft_tags
                ),
            )
            for item in page.proposals
        }
        self.batch_operation: BatchOperationState | None = None
        self.undo_operation: UndoOperationState | None = None
        self.undo_available = page.undo_available

    @property
    def has_unsaved_changes(self) -> bool:
        return any(state.dirty for state in self._states.values())

    @property
    def progress(self) -> ReviewProgress:
        reviewed = sum(state.dirty for state in self._states.values())
        selected = sum(state.batch_selected for state in self._states.values())
        return ReviewProgress(
            page_items=len(self._states),
            reviewed_items=reviewed,
            remaining_items=max(0, len(self._states) - reviewed),
            batch_selected_items=selected,
        )

    def state_for(self, proposal_id: str) -> ProposalReviewState:
        return self._state(proposal_id)

    def set_decision(self, proposal_id: str, decision: ReviewDecision) -> None:
        if not isinstance(decision, str) or decision not in _REVIEW_DECISIONS:
            raise _validation(
                "decision must be pending, accept, edit, reject, or manual.",
                "decision",
            )
        proposal = self._proposal(proposal_id)
        if decision == "manual" and not proposal.is_failed:
            raise _validation(
                "Manual labeling is available only for failed proposals.",
                "decision",
                code="manual_review_not_allowed",
                details={"proposal_id": proposal_id},
            )
        self._state(proposal_id).decision = decision

    def set_edited_tags(self, proposal_id: str, tags: Sequence[str]) -> None:
        self._state(proposal_id).edited_tags = _review_tags(tags, "edited_tags")

    def confirm_identity(
        self,
        proposal_id: str,
        tag: str,
        *,
        confirmed: bool = True,
    ) -> None:
        if not isinstance(confirmed, bool):
            raise _validation("confirmed must be a boolean.", "confirmed")
        proposal = self._proposal(proposal_id)
        normalized = _required_text(tag, "tag", maximum=_MAX_TAG_LENGTH)
        identity_by_key = {_key(value): value for value in proposal.identity_tags}
        canonical = identity_by_key.get(_key(normalized))
        if canonical is None:
            raise _validation(
                "Only an identity tag from this proposal can be confirmed.",
                "tag",
                code="unknown_identity_tag",
                details={"proposal_id": proposal_id, "tag": normalized},
            )
        state = self._state(proposal_id)
        if confirmed:
            state.confirmed_identity_tags.add(canonical)
            if state.decision == "pending":
                state.decision = "accept"
        else:
            state.confirmed_identity_tags.discard(canonical)

    def select_low_risk(
        self,
        proposal_id: str,
        *,
        selected: bool = True,
    ) -> None:
        if not isinstance(selected, bool):
            raise _validation("selected must be a boolean.", "selected")
        proposal = self._proposal(proposal_id)
        if selected and not proposal.can_batch_accept:
            raise _validation(
                "The proposal has no safe low-risk tags for batch acceptance.",
                "proposal_id",
                code="batch_accept_not_allowed",
                details={"proposal_id": proposal_id},
            )
        self._state(proposal_id).batch_selected = selected

    def select_all_low_risk(self) -> int:
        selected = 0
        for proposal_id, proposal in self._proposals.items():
            state = self._states[proposal_id]
            state.batch_selected = proposal.can_batch_accept
            selected += int(state.batch_selected)
        return selected

    def clear_low_risk_selection(self) -> None:
        for state in self._states.values():
            state.batch_selected = False

    def build_low_risk_batch_request(
        self,
        library_id: str,
    ) -> LowRiskBatchReviewRequest:
        selected = [
            self._proposals[proposal_id]
            for proposal_id, state in self._states.items()
            if state.batch_selected
        ]
        if not selected:
            raise _validation(
                "Select at least one proposal with safe low-risk tags.",
                "proposal_ids",
                code="batch_selection_required",
            )
        return LowRiskBatchReviewRequest(
            library_id=library_id,
            proposal_ids=tuple(item.proposal_id for item in selected),
            accepted_tags_by_proposal={
                item.proposal_id: item.batch_safe_tags for item in selected
            },
        )

    def build_identity_confirmation_request(
        self,
        library_id: str,
        proposal_id: str,
    ) -> IdentityConfirmationRequest:
        proposal = self._proposal(proposal_id)
        state = self._state(proposal_id)
        confirmed = _ordered_subset(
            proposal.identity_tags,
            state.confirmed_identity_tags,
        )
        if not confirmed:
            raise _validation(
                "Confirm at least one identity tag for this image.",
                "confirmed_identity_tags",
                code="identity_confirmation_required",
                details={"proposal_id": proposal_id},
            )
        accepted = self._accepted_tags(proposal, state)
        return IdentityConfirmationRequest(
            library_id=library_id,
            proposal_id=proposal_id,
            accepted_tags=accepted,
            confirmed_identity_tags=confirmed,
        )

    def build_review_request(self, library_id: str) -> AutoTagReviewRequest:
        decisions: list[AutoTagReviewDecision] = []
        for proposal_id, state in self._states.items():
            if state.decision == "pending":
                continue
            proposal = self._proposals[proposal_id]
            if state.decision == "reject":
                accepted: tuple[str, ...] = ()
                confirmed: tuple[str, ...] = ()
            else:
                accepted = self._accepted_tags(proposal, state)
                confirmed = (
                    _ordered_subset(
                        proposal.identity_tags,
                        state.confirmed_identity_tags,
                    )
                    if state.decision in {"accept", "edit"}
                    else ()
                )
                if not accepted:
                    raise _validation(
                        "Accepted or manual review items require at least one tag.",
                        "accepted_tags",
                        code="accepted_tags_required",
                        details={"proposal_id": proposal_id},
                    )
            decisions.append(
                AutoTagReviewDecision(
                    proposal_id=proposal_id,
                    decision=state.decision,
                    accepted_tags=accepted,
                    confirmed_identity_tags=confirmed,
                )
            )
        if not decisions:
            raise _validation(
                "Mark at least one proposal as accept, reject, or manual.",
                "decisions",
                code="review_decision_required",
            )
        return AutoTagReviewRequest(
            library_id=library_id,
            decisions=tuple(decisions),
        )

    def record_batch_result(self, payload: object) -> BatchOperationState:
        result = parse_batch_review_result(payload)
        self.batch_operation = result
        self.undo_operation = None
        self.undo_available = result.undo_available
        return result

    def record_undo_result(self, payload: object) -> UndoOperationState:
        result = parse_undo_result(payload)
        self.undo_operation = result
        self.undo_available = result.undo_available
        return result

    def discard_changes(self) -> None:
        for proposal_id, proposal in self._proposals.items():
            state = self._states[proposal_id]
            initial = (
                proposal.manual_tags
                if proposal.is_failed
                else proposal.review_draft_tags
            )
            state.decision = "pending"
            state.initial_tags = initial
            state.edited_tags = initial
            state.confirmed_identity_tags.clear()
            state.batch_selected = False

    def _proposal(self, proposal_id: str) -> OrganizeProposal:
        normalized = _required_text(proposal_id, "proposal_id", maximum=_MAX_TAG_LENGTH)
        try:
            return self._proposals[normalized]
        except KeyError as exc:
            raise _validation(
                "The proposal is not present on this workbench page.",
                "proposal_id",
                code="proposal_not_found",
                details={"proposal_id": normalized},
            ) from exc

    def _state(self, proposal_id: str) -> ProposalReviewState:
        proposal = self._proposal(proposal_id)
        return self._states[proposal.proposal_id]

    @staticmethod
    def _accepted_tags(
        proposal: OrganizeProposal,
        state: ProposalReviewState,
    ) -> tuple[str, ...]:
        if state.decision == "manual":
            return state.edited_tags
        identity_keys = {_key(tag) for tag in proposal.identity_tags}
        ordinary = tuple(
            tag for tag in state.edited_tags if _key(tag) not in identity_keys
        )
        confirmed = _ordered_subset(
            proposal.identity_tags,
            state.confirmed_identity_tags,
        )
        return _dedupe((*ordinary, *confirmed))


@dataclass(frozen=True, slots=True)
class AliasEntry:
    canonical_name: str
    aliases: tuple[str, ...]

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)

    @property
    def display_text(self) -> str:
        if not self.aliases:
            return self.canonical_name
        return f"{self.canonical_name} → {' / '.join(self.aliases)}"


@dataclass(frozen=True, slots=True)
class AliasCatalog:
    entries: tuple[AliasEntry, ...]
    _owners: Mapping[str, AliasEntry] = field(repr=False)

    @property
    def count(self) -> int:
        return len(self.entries)

    def canonical_for(self, term: str) -> str | None:
        normalized = _alias_text(term, "term")
        owner = self._owners.get(_key(normalized))
        return None if owner is None else owner.canonical_name

    def equivalent_terms(self, term: str) -> tuple[str, ...]:
        normalized = _alias_text(term, "term")
        owner = self._owners.get(_key(normalized))
        return (normalized,) if owner is None else owner.terms

    def validate_upsert(
        self,
        canonical_name: str,
        aliases: Sequence[str],
    ) -> AliasEntry:
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            raise _validation("aliases must be a sequence of strings.", "aliases")
        try:
            candidate = _alias_entry(
                canonical_name,
                list(aliases),
                path="alias_edit",
            )
        except OrganizeDataError as exc:
            raise _validation(
                str(exc),
                "aliases",
                code=exc.code,
                details={"path": exc.path, **exc.details},
            ) from exc
        if not candidate.aliases:
            raise _validation(
                "At least one alias different from the canonical name is required.",
                "aliases",
                code="alias_required",
            )
        replacing_key = _key(candidate.canonical_name)
        for term in candidate.terms:
            owner = self._owners.get(_key(term))
            if owner is not None and _key(owner.canonical_name) != replacing_key:
                raise _validation(
                    f"Alias {term!r} already belongs to {owner.canonical_name!r}.",
                    "aliases",
                    code="alias_conflict",
                    details={
                        "term": term,
                        "owner": owner.canonical_name,
                    },
                )
        return candidate


@dataclass(frozen=True, slots=True)
class AliasMutationState:
    updated: bool
    deleted: bool
    entry: AliasEntry | None


def parse_pending_page(payload: object, *, strict: bool = False) -> OrganizePage:
    """Parse one real ``auto_tag_pending`` result.

    When ``strict`` is false, an unsafe individual proposal is omitted and
    represented in ``page.issues``.  Top-level pagination corruption always
    raises because the UI cannot navigate it reliably.
    """

    root = _mapping(payload, "result")
    pending_count = _required_integer(root, "pending_count", "pending_count", minimum=0)
    total_count = _optional_integer(
        root,
        "total_count",
        "total_count",
        default=pending_count,
        minimum=0,
    )
    if total_count < pending_count:
        raise _data_error(
            "total_count cannot be smaller than pending_count.",
            "total_count",
            details={"total_count": total_count, "pending_count": pending_count},
        )
    offset = _required_integer(root, "offset", "offset", minimum=0)
    limit = _required_integer(
        root,
        "limit",
        "limit",
        minimum=1,
        maximum=_MAX_PAGE_SIZE,
    )
    raw_proposals = _required_list(root, "proposals", "proposals")
    if len(raw_proposals) > limit:
        raise _data_error(
            "The proposal count exceeds the declared page limit.",
            "proposals",
            details={"count": len(raw_proposals), "limit": limit},
        )
    has_more = _required_bool(root, "has_more", "has_more")
    expected_has_more = offset + len(raw_proposals) < pending_count
    if has_more != expected_has_more:
        raise _data_error(
            "has_more is inconsistent with pending_count, offset and proposals.",
            "has_more",
            details={
                "reported": has_more,
                "expected": expected_has_more,
                "pending_count": pending_count,
                "offset": offset,
                "proposal_count": len(raw_proposals),
            },
        )
    undo_available = _optional_bool(
        root,
        "undo_available",
        "undo_available",
        default=False,
    )
    filters = OrganizeFilters.from_backend(root.get("filters"))

    proposals: list[OrganizeProposal] = []
    issues: list[OrganizeDataIssue] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_proposals):
        path = f"proposals[{index}]"
        try:
            proposal = _parse_proposal(raw, path)
            if proposal.proposal_id in seen:
                raise _data_error(
                    "The page contains a duplicate proposal ID.",
                    f"{path}.proposal_id",
                    code="duplicate_proposal",
                    details={"proposal_id": proposal.proposal_id},
                )
            seen.add(proposal.proposal_id)
            proposals.append(proposal)
        except OrganizeDataError as exc:
            if strict:
                raise
            issues.append(exc.issue)

    return OrganizePage(
        proposals=tuple(proposals),
        pagination=PaginationState(
            pending_count=pending_count,
            total_count=total_count,
            offset=offset,
            limit=limit,
            returned_count=len(proposals),
            has_more=has_more,
        ),
        filters=filters,
        undo_available=undo_available,
        issues=tuple(issues),
    )


def parse_batch_review_result(payload: object) -> BatchOperationState:
    root = _mapping(payload, "batch_result")
    exclusions: list[IdentityExclusion] = []
    raw_exclusions = _optional_list(
        root,
        "identity_exclusions",
        "batch_result.identity_exclusions",
        default=(),
    )
    for index, raw in enumerate(raw_exclusions):
        path = f"batch_result.identity_exclusions[{index}]"
        item = _mapping(raw, path)
        exclusions.append(
            IdentityExclusion(
                proposal_id=_required_string(
                    item, "proposal_id", f"{path}.proposal_id"
                ),
                tags=_string_list(item.get("tags", []), f"{path}.tags"),
            )
        )
    exclusion_total = sum(len(item.tags) for item in exclusions)
    explicit_count = _optional_integer(
        root,
        "identity_excluded_count",
        "batch_result.identity_excluded_count",
        default=0,
        minimum=0,
    )
    legacy_count = _optional_integer(
        root,
        "identity_excluded",
        "batch_result.identity_excluded",
        default=0,
        minimum=0,
    )
    return BatchOperationState(
        batch_id=_optional_string(
            root, "batch_id", "batch_result.batch_id", default=""
        ),
        accepted=_required_integer(
            root, "accepted", "batch_result.accepted", minimum=0
        ),
        updated=_required_integer(root, "updated", "batch_result.updated", minimum=0),
        identity_excluded_count=max(explicit_count, legacy_count, exclusion_total),
        identity_exclusions=tuple(exclusions),
        undo_available=_required_bool(
            root, "undo_available", "batch_result.undo_available"
        ),
    )


def parse_undo_result(payload: object) -> UndoOperationState:
    root = _mapping(payload, "undo_result")
    return UndoOperationState(
        undone=_required_bool(root, "undone", "undo_result.undone"),
        updated=_required_integer(root, "updated", "undo_result.updated", minimum=0),
        batch_id=_optional_string(root, "batch_id", "undo_result.batch_id", default=""),
        undo_available=_required_bool(
            root, "undo_available", "undo_result.undo_available"
        ),
    )


def parse_review_result(payload: object) -> ReviewOperationState:
    root = _mapping(payload, "review_result")
    raw_failures = _optional_list(
        root,
        "failures",
        "review_result.failures",
        default=(),
    )
    failures: list[ReviewFailure] = []
    for index, raw in enumerate(raw_failures):
        path = f"review_result.failures[{index}]"
        item = _mapping(raw, path)
        proposal_id = item.get("proposal_id", item.get("doc_id"))
        failures.append(
            ReviewFailure(
                proposal_id=_required_text(
                    proposal_id,
                    f"{path}.proposal_id",
                    maximum=_MAX_TAG_LENGTH,
                ),
                error=_required_string(item, "error", f"{path}.error"),
            )
        )
    reported_failed = _optional_integer(
        root,
        "failed",
        "review_result.failed",
        default=len(failures),
        minimum=0,
    )
    if reported_failed < len(failures):
        raise _data_error(
            "failed cannot be smaller than the returned failure list.",
            "review_result.failed",
            details={"reported": reported_failed, "failures": len(failures)},
        )
    return ReviewOperationState(
        accepted=_required_integer(
            root, "accepted", "review_result.accepted", minimum=0
        ),
        rejected=_required_integer(
            root, "rejected", "review_result.rejected", minimum=0
        ),
        updated=_required_integer(root, "updated", "review_result.updated", minimum=0),
        failed=reported_failed,
        pending_count=_optional_integer(
            root,
            "pending_count",
            "review_result.pending_count",
            default=0,
            minimum=0,
        ),
        failures=tuple(failures),
        undo_available=_optional_bool(
            root,
            "undo_available",
            "review_result.undo_available",
            default=False,
        ),
    )


def parse_alias_catalog(payload: object) -> AliasCatalog:
    root = _mapping(payload, "alias_result")
    raw_entries = root.get("aliases")
    if raw_entries is None:
        raw_entries = root.get("items")
    if raw_entries is None:
        raw_entries = root.get("groups")
    if raw_entries is None:
        raise _data_error(
            "Alias result requires aliases, items, or groups.",
            "alias_result",
        )
    values = _list(raw_entries, "alias_result.aliases")
    entries: list[AliasEntry] = []
    owners: dict[str, AliasEntry] = {}
    for index, raw in enumerate(values):
        path = f"alias_result.aliases[{index}]"
        item = _mapping(raw, path)
        canonical = item.get("canonical_name", item.get("canonical"))
        entry = _alias_entry(canonical, item.get("aliases", []), path=path)
        for term in entry.terms:
            key = _key(term)
            owner = owners.get(key)
            if owner is not None:
                raise _data_error(
                    f"Alias term {term!r} belongs to more than one group.",
                    path,
                    code="alias_conflict",
                    details={
                        "term": term,
                        "owner": owner.canonical_name,
                        "conflicting_owner": entry.canonical_name,
                    },
                )
            owners[key] = entry
        entries.append(entry)
    count = root.get("count")
    if count is not None:
        parsed_count = _integer(count, "alias_result.count", minimum=0)
        if parsed_count != len(entries):
            raise _data_error(
                "Alias count does not match the returned groups.",
                "alias_result.count",
                details={"reported": parsed_count, "actual": len(entries)},
            )
    entries.sort(key=lambda entry: (_key(entry.canonical_name), entry.canonical_name))
    return AliasCatalog(entries=tuple(entries), _owners=owners)


def parse_alias_mutation_result(payload: object) -> AliasMutationState:
    root = _mapping(payload, "alias_mutation")
    raw_entry = root.get("entry")
    entry: AliasEntry | None
    if raw_entry is None:
        entry = None
    else:
        item = _mapping(raw_entry, "alias_mutation.entry")
        canonical = item.get("canonical_name", item.get("canonical"))
        entry = _alias_entry(
            canonical,
            item.get("aliases", []),
            path="alias_mutation.entry",
        )
    return AliasMutationState(
        updated=_required_bool(root, "updated", "alias_mutation.updated"),
        deleted=_required_bool(root, "deleted", "alias_mutation.deleted"),
        entry=entry,
    )


def _parse_proposal(value: object, path: str) -> OrganizeProposal:
    raw = _mapping(value, path)
    proposal_id = _first_non_empty(
        raw,
        ("proposal_id", "doc_id", "annotation_id", "image_id", "relative_path"),
        path,
    )
    relative_path = _optional_string(
        raw, "relative_path", f"{path}.relative_path", default=proposal_id
    )
    source_text = _optional_string(
        raw, "source_path", f"{path}.source_path", default=""
    )
    thumbnail = _thumbnail_reference(source_text, f"{path}.source_path")
    manual_tags = _string_list(raw.get("manual_tags", []), f"{path}.manual_tags")
    folder_tags = _string_list(raw.get("folder_tags", []), f"{path}.folder_tags")
    if "existing_tags" in raw:
        existing_tags = _string_list(raw["existing_tags"], f"{path}.existing_tags")
    else:
        existing_tags = _dedupe((*manual_tags, *folder_tags))
    proposed_tags = _string_list(raw.get("proposed_tags", []), f"{path}.proposed_tags")
    explicit_low_risk = _string_list(
        raw.get("low_risk_tags", []), f"{path}.low_risk_tags"
    )
    explicit_identity = _string_list(
        raw.get("identity_tags", []), f"{path}.identity_tags"
    )
    details = _parse_tag_details(
        raw.get("tag_details", []),
        f"{path}.tag_details",
    )
    if not details:
        details = _fallback_tag_details(
            manual_tags=manual_tags,
            folder_tags=folder_tags,
            existing_tags=existing_tags,
            proposed_tags=proposed_tags,
            identity_tags=explicit_identity,
        )
    fields = _parse_fields(
        raw.get("fields", raw.get("stable_fields", {})),
        f"{path}.fields",
    )
    entities = _parse_entities(raw.get("entities", {}), f"{path}.entities")
    review_reasons = _string_list(
        raw.get("review_reasons", []), f"{path}.review_reasons"
    )

    detail_identity = tuple(detail.tag for detail in details if detail.is_identity)
    proposed_or_low_risk_keys = {
        _key(tag) for tag in (*proposed_tags, *explicit_low_risk)
    }
    entity_identity = tuple(
        entity.name
        for values in entities.values()
        for entity in values
        if entity.name and _key(entity.name) in proposed_or_low_risk_keys
    )
    identity_tags = _dedupe((*explicit_identity, *detail_identity, *entity_identity))
    identity_keys = {_key(tag) for tag in identity_tags}
    existing_keys = {_key(tag) for tag in existing_tags}
    detail_low_risk = tuple(detail.tag for detail in details if detail.is_low_risk)
    low_risk_tags = _dedupe((*explicit_low_risk, *detail_low_risk))
    batch_safe = tuple(
        tag
        for tag in low_risk_tags
        if _key(tag) not in identity_keys and _key(tag) not in existing_keys
    )
    proposed_additions = tuple(
        tag for tag in proposed_tags if _key(tag) not in existing_keys
    )
    review_draft = batch_safe or tuple(
        tag for tag in proposed_additions if _key(tag) not in identity_keys
    )
    identity_additions = tuple(
        tag for tag in identity_tags if _key(tag) not in existing_keys
    )
    already_present = tuple(tag for tag in proposed_tags if _key(tag) in existing_keys)
    status = _optional_string(raw, "status", f"{path}.status", default="pending_review")
    error = _optional_string(raw, "error", f"{path}.error", default="")
    failure_category = _optional_string(
        raw,
        "failure_category",
        f"{path}.failure_category",
        default="",
    )
    conflict = _has_conflict(review_reasons, details)
    buckets: set[ReviewBucket] = set()
    if _key(status) == "failed":
        buckets.add("failed")
    else:
        if conflict:
            buckets.add("conflict")
        if identity_tags:
            buckets.add("identity")
        if batch_safe and not identity_tags and not conflict:
            buckets.add("low_risk")
        if not buckets:
            buckets.add("pending")

    entity_type_by_tag: dict[str, str] = {}
    for detail in details:
        if detail.is_identity:
            entity_type_by_tag.setdefault(
                _key(detail.tag), detail.entity_type or "identity"
            )
    for entity_type, values in entities.items():
        for entity in values:
            if entity.name:
                entity_type_by_tag.setdefault(_key(entity.name), entity_type)
    choices = tuple(
        IdentityChoice(
            tag=tag, entity_type=entity_type_by_tag.get(_key(tag), "identity")
        )
        for tag in identity_additions
    )
    return OrganizeProposal(
        proposal_id=proposal_id,
        relative_path=relative_path or proposal_id,
        thumbnail=thumbnail,
        description=_optional_string(
            raw, "description", f"{path}.description", default=""
        ),
        status=status,
        error=error,
        failure_category=failure_category,
        manual_tags=manual_tags,
        folder_tags=folder_tags,
        existing_tags=existing_tags,
        proposed_tags=proposed_tags,
        low_risk_tags=low_risk_tags,
        identity_tags=identity_tags,
        batch_safe_tags=batch_safe,
        review_draft_tags=review_draft,
        tag_details=details,
        fields=fields,
        entities=entities,
        identity_choices=choices,
        review_reasons=review_reasons,
        review_buckets=frozenset(buckets),
        difference=TagDifference(
            retained=existing_tags,
            suggested_additions=proposed_additions,
            low_risk_additions=batch_safe,
            identity_additions=identity_additions,
            already_present_suggestions=already_present,
        ),
    )


def _parse_tag_details(value: object, path: str) -> tuple[TagSourceDetail, ...]:
    values = _list(value, path)
    result: list[TagSourceDetail] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(values):
        item_path = f"{path}[{index}]"
        item = _mapping(raw, item_path)
        tag = _required_string(item, "tag", f"{item_path}.tag")
        source = _optional_string(item, "source", f"{item_path}.source", default="")
        sources = _string_list(item.get("sources", []), f"{item_path}.sources")
        if not sources and source:
            sources = (source,)
        key = (_key(tag), _key(source))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            TagSourceDetail(
                tag=tag,
                source=source,
                sources=sources,
                source_label=_optional_string(
                    item,
                    "source_label",
                    f"{item_path}.source_label",
                    default="",
                ),
                risk=_optional_string(item, "risk", f"{item_path}.risk", default=""),
                identity=_optional_bool(
                    item,
                    "identity",
                    f"{item_path}.identity",
                    default=False,
                ),
                field_name=_optional_nullable_string(
                    item, "field", f"{item_path}.field"
                ),
                entity_type=_optional_nullable_string(
                    item, "entity_type", f"{item_path}.entity_type"
                ),
                already_present=_optional_bool(
                    item,
                    "already_present",
                    f"{item_path}.already_present",
                    default=False,
                ),
                requires_individual_confirmation=_optional_bool(
                    item,
                    "requires_individual_confirmation",
                    f"{item_path}.requires_individual_confirmation",
                    default=False,
                ),
                confidence=_optional_confidence(
                    item.get("confidence"), f"{item_path}.confidence"
                ),
            )
        )
    return tuple(result)


def _fallback_tag_details(
    *,
    manual_tags: tuple[str, ...],
    folder_tags: tuple[str, ...],
    existing_tags: tuple[str, ...],
    proposed_tags: tuple[str, ...],
    identity_tags: tuple[str, ...],
) -> tuple[TagSourceDetail, ...]:
    result: list[TagSourceDetail] = []
    identity_keys = {_key(tag) for tag in identity_tags}
    existing_keys = {_key(tag) for tag in existing_tags}
    represented: set[tuple[str, str]] = set()
    for source, values in (
        ("manual", manual_tags),
        ("folder", folder_tags),
        ("existing", existing_tags),
    ):
        for tag in values:
            key = (_key(tag), source)
            if key in represented:
                continue
            represented.add(key)
            result.append(
                TagSourceDetail(
                    tag=tag,
                    source=source,
                    sources=(source,),
                    source_label="",
                    risk="existing",
                    identity=False,
                    field_name=None,
                    entity_type=None,
                    already_present=True,
                    requires_individual_confirmation=False,
                    confidence=None,
                )
            )
    for tag in proposed_tags:
        identity = _key(tag) in identity_keys
        source = "model_entity" if identity else "model_field"
        result.append(
            TagSourceDetail(
                tag=tag,
                source=source,
                sources=(source,),
                source_label="",
                risk="identity" if identity else "review",
                identity=identity,
                field_name=None,
                entity_type="identity" if identity else None,
                already_present=_key(tag) in existing_keys,
                requires_individual_confirmation=identity,
                confidence=None,
            )
        )
    return tuple(result)


def _parse_fields(value: object, path: str) -> Mapping[str, StableField]:
    raw = _mapping(value, path)
    result: dict[str, StableField] = {}
    for name, field_value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise _data_error("Field names must be non-empty strings.", path)
        item_path = f"{path}.{name}"
        item = _mapping(field_value, item_path)
        result[name] = StableField(
            name=name,
            values=_string_list(item.get("values", []), f"{item_path}.values"),
            labels=_string_list(item.get("labels", []), f"{item_path}.labels"),
            confidence=(
                _optional_confidence(
                    item.get("confidence", 0.0), f"{item_path}.confidence"
                )
                or 0.0
            ),
        )
    return result


def _parse_entities(
    value: object,
    path: str,
) -> Mapping[EntityType, tuple[IdentityEntity, ...]]:
    raw = _mapping(value, path)
    unknown = set(raw) - set(_ENTITY_TYPES)
    if unknown:
        raise _data_error(
            "entities contains unsupported entity types.",
            path,
            details={"fields": sorted(str(value) for value in unknown)},
        )
    result: dict[EntityType, tuple[IdentityEntity, ...]] = {}
    for entity_type in _ENTITY_TYPES:
        raw_entities = raw.get(entity_type, [])
        if isinstance(raw_entities, Mapping):
            values = [raw_entities]
        else:
            values = _list(raw_entities, f"{path}.{entity_type}")
        entities: list[IdentityEntity] = []
        for index, raw_entity in enumerate(values):
            item_path = f"{path}.{entity_type}[{index}]"
            item = _mapping(raw_entity, item_path)
            entities.append(
                IdentityEntity(
                    entity_type=entity_type,
                    name=_optional_nullable_string(item, "name", f"{item_path}.name"),
                    state=_optional_string(
                        item, "state", f"{item_path}.state", default="unknown"
                    ),
                    evidence=_string_list(
                        item.get("evidence", []), f"{item_path}.evidence"
                    ),
                    evidence_text=_optional_nullable_string(
                        item, "evidence_text", f"{item_path}.evidence_text"
                    ),
                    confidence=_optional_confidence(
                        item.get("confidence"), f"{item_path}.confidence"
                    ),
                )
            )
        result[entity_type] = tuple(entities)
    return result


def _thumbnail_reference(value: str, path: str) -> ThumbnailReference:
    if not value:
        return ThumbnailReference("", None, "missing_path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and not PureWindowsPath(value).is_absolute():
        raise _data_error("source_path must be absolute when supplied.", path)
    try:
        available = candidate.is_file()
    except OSError:
        available = False
    return ThumbnailReference(
        source_text=value,
        path=candidate,
        status="ready" if available else "missing_file",
    )


def _alias_entry(canonical: object, aliases: object, *, path: str) -> AliasEntry:
    canonical_name = _alias_text(canonical, f"{path}.canonical_name")
    values = _list(aliases, f"{path}.aliases")
    if len(values) > _MAX_ALIAS_COUNT:
        raise _data_error(
            f"aliases can contain at most {_MAX_ALIAS_COUNT} items.",
            f"{path}.aliases",
        )
    result: list[str] = []
    seen = {_key(canonical_name)}
    for index, value in enumerate(values):
        alias = _alias_text(value, f"{path}.aliases[{index}]")
        key = _key(alias)
        if key not in seen:
            seen.add(key)
            result.append(alias)
    return AliasEntry(canonical_name, tuple(result))


def _alias_text(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise _data_error("Alias text must be a string.", path)
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise _data_error("Alias text cannot be empty.", path)
    if len(normalized) > _MAX_ALIAS_LENGTH:
        raise _data_error(
            f"Alias text cannot exceed {_MAX_ALIAS_LENGTH} characters.", path
        )
    if _contains_control(normalized):
        raise _data_error("Alias text cannot contain control characters.", path)
    return normalized


def _matches_names(
    values: Iterable[str],
    query: str,
    aliases: AliasCatalog | None,
) -> bool:
    queries = aliases.equivalent_terms(query) if aliases is not None else (query,)
    return any(_contains_query(values, candidate) for candidate in queries)


def _contains_query(values: Iterable[str], query: str) -> bool:
    needle = _key(query)
    return bool(needle) and any(needle in _key(value) for value in values)


def _has_conflict(
    review_reasons: Iterable[str],
    details: Iterable[TagSourceDetail],
) -> bool:
    return bool(
        any(
            any(marker in _key(reason) for marker in _CONFLICT_MARKERS)
            for reason in review_reasons
        )
        or any(_key(detail.risk) == "conflict" for detail in details)
    )


def _review_tags(values: object, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise _validation(f"{field_name} must be a sequence of strings.", field_name)
    try:
        return _normalize_strings(list(values), field_name)
    except OrganizeDataError as exc:
        raise _validation(str(exc), field_name, details=exc.details) from exc


def _string_list(value: object, path: str) -> tuple[str, ...]:
    return _normalize_strings(value, path)


def _normalize_strings(value: object, path: str) -> tuple[str, ...]:
    values = _list(value, path)
    if len(values) > _MAX_TAGS:
        raise _data_error(f"The list can contain at most {_MAX_TAGS} items.", path)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        tag = _required_text(item, f"{path}[{index}]", maximum=_MAX_TAG_LENGTH)
        key = _key(tag)
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return tuple(result)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)


def _ordered_subset(values: Iterable[str], selected: set[str]) -> tuple[str, ...]:
    selected_keys = {_key(value) for value in selected}
    return tuple(value for value in values if _key(value) in selected_keys)


def _filter_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise _validation(f"{field_name} must be a string.", field_name)
    normalized = value.strip()
    if len(normalized) > _MAX_FILTER_LENGTH:
        raise _validation(
            f"{field_name} cannot exceed {_MAX_FILTER_LENGTH} characters.",
            field_name,
        )
    if _contains_control(normalized):
        raise _validation(
            f"{field_name} cannot contain control characters.", field_name
        )
    return normalized


def _first_non_empty(
    mapping: Mapping[str, Any],
    names: Sequence[str],
    path: str,
) -> str:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, str) and value.strip():
            return _required_text(value, f"{path}.{name}", maximum=_MAX_TAG_LENGTH)
        if value is not None and not isinstance(value, str):
            raise _data_error(f"{name} must be a string.", f"{path}.{name}")
    raise _data_error(
        "Proposal requires proposal_id, doc_id, annotation_id, image_id, "
        "or relative_path.",
        path,
        code="missing_proposal_id",
    )


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _data_error("Expected an object.", path)
    if any(not isinstance(key, str) for key in value):
        raise _data_error("Object keys must be strings.", path)
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _data_error("Expected an array.", path)
    return value


def _required_list(mapping: Mapping[str, Any], name: str, path: str) -> list[Any]:
    if name not in mapping:
        raise _data_error(f"Missing required field: {name}.", path)
    return _list(mapping[name], path)


def _optional_list(
    mapping: Mapping[str, Any],
    name: str,
    path: str,
    *,
    default: Sequence[Any],
) -> list[Any]:
    if name not in mapping:
        return list(default)
    return _list(mapping[name], path)


def _required_string(mapping: Mapping[str, Any], name: str, path: str) -> str:
    if name not in mapping:
        raise _data_error(f"Missing required field: {name}.", path)
    return _required_text(mapping[name], path, maximum=_MAX_TAG_LENGTH)


def _optional_string(
    mapping: Mapping[str, Any],
    name: str,
    path: str,
    *,
    default: str,
) -> str:
    if name not in mapping:
        return default
    value = mapping[name]
    if not isinstance(value, str):
        raise _data_error(f"{name} must be a string.", path)
    normalized = value.strip()
    if _contains_control(normalized):
        raise _data_error(f"{name} cannot contain control characters.", path)
    return normalized


def _optional_nullable_string(
    mapping: Mapping[str, Any], name: str, path: str
) -> str | None:
    if name not in mapping or mapping[name] is None:
        return None
    return _optional_string(mapping, name, path, default="") or None


def _required_bool(mapping: Mapping[str, Any], name: str, path: str) -> bool:
    if name not in mapping:
        raise _data_error(f"Missing required field: {name}.", path)
    value = mapping[name]
    if not isinstance(value, bool):
        raise _data_error(f"{name} must be a boolean.", path)
    return value


def _optional_bool(
    mapping: Mapping[str, Any],
    name: str,
    path: str,
    *,
    default: bool,
) -> bool:
    if name not in mapping:
        return default
    value = mapping[name]
    if not isinstance(value, bool):
        raise _data_error(f"{name} must be a boolean.", path)
    return value


def _integer(
    value: object,
    path: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _data_error("Expected an integer.", path)
    if value < minimum or (maximum is not None and value > maximum):
        details: JsonObject = {"minimum": minimum, "actual": value}
        if maximum is not None:
            details["maximum"] = maximum
        raise _data_error(
            "Integer is outside the supported range.", path, details=details
        )
    return value


def _required_integer(
    mapping: Mapping[str, Any],
    name: str,
    path: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if name not in mapping:
        raise _data_error(f"Missing required field: {name}.", path)
    return _integer(mapping[name], path, minimum=minimum, maximum=maximum)


def _optional_integer(
    mapping: Mapping[str, Any],
    name: str,
    path: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if name not in mapping:
        return default
    return _integer(mapping[name], path, minimum=minimum, maximum=maximum)


def _optional_confidence(value: object, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _data_error("confidence must be a number or null.", path)
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise _data_error("confidence must be a finite number between 0 and 1.", path)
    return normalized


def _required_text(value: object, path: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise _data_error("Expected a string.", path)
    normalized = value.strip()
    if not normalized:
        raise _data_error("String cannot be empty.", path)
    if len(normalized) > maximum:
        raise _data_error(f"String cannot exceed {maximum} characters.", path)
    if _contains_control(normalized):
        raise _data_error("String cannot contain control characters.", path)
    return normalized


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _data_error(
    message: str,
    path: str,
    *,
    code: str = "invalid_organize_payload",
    details: Mapping[str, Any] | None = None,
) -> OrganizeDataError:
    return OrganizeDataError(
        message,
        path=path,
        code=code,
        details=details,
    )


def _validation(
    message: str,
    field_name: str,
    *,
    code: str = "invalid_organize_state",
    details: Mapping[str, Any] | None = None,
) -> OrganizeValidationError:
    return OrganizeValidationError(
        message,
        field_name=field_name,
        code=code,
        details=details,
    )


__all__ = [
    "AliasCatalog",
    "AliasEntry",
    "AliasMutationState",
    "BatchOperationState",
    "EntityType",
    "IdentityChoice",
    "IdentityEntity",
    "IdentityExclusion",
    "OrganizeDataError",
    "OrganizeDataIssue",
    "OrganizeFilters",
    "OrganizePage",
    "OrganizeProposal",
    "OrganizeReviewSession",
    "OrganizeStateError",
    "OrganizeValidationError",
    "PaginationState",
    "ProposalReviewState",
    "ReviewBucket",
    "ReviewDecision",
    "ReviewFailure",
    "ReviewOperationState",
    "ReviewProgress",
    "StableField",
    "TagDifference",
    "TagSourceDetail",
    "ThumbnailReference",
    "ThumbnailStatus",
    "UndoOperationState",
    "parse_alias_catalog",
    "parse_alias_mutation_result",
    "parse_batch_review_result",
    "parse_pending_page",
    "parse_review_result",
    "parse_undo_result",
]
