"""Validated library-task workflow for the pure-Python desktop client.

This module is the boundary between UI state and the persistent backend job
contract.  Request data classes normalise values before any HTTP request is
made, while :class:`LibraryTaskService` provides submission, polling and safe
cancellation without coupling those operations to Tk widgets.

The ``tags`` field on index requests intentionally preserves three distinct
backend meanings:

* ``None`` leaves existing manual tags unchanged;
* a non-empty sequence is assigned only to documents inserted by this run;
* an empty sequence explicitly clears manual tags in the indexed scope.

In particular, non-empty tags are never retroactively applied to every image
already present in a Collection.
"""

from __future__ import annotations

import math
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Protocol, TypeAlias

from .backend_api import JsonObject

AutoTagScope = Literal[
    "latest_index_run",
    "untagged",
    "failed",
    "failed_all",
    "all",
]
AutoTagReviewAction = Literal["accept", "edit", "reject", "manual"]
AutoTagReviewState = Literal["all", "low_risk", "identity", "conflict", "failed"]

DEFAULT_AUTO_TAG_MODEL = "qwen3-vl-flash"
DEFAULT_AUTO_TAG_MAX_IMAGES = 300
DEFAULT_AUTO_TAG_BUDGET_CNY = 5.0

_AUTO_TAG_SCOPES = frozenset(
    {"latest_index_run", "untagged", "failed", "failed_all", "all"}
)
_REVIEW_ACTIONS = frozenset({"accept", "edit", "reject", "manual"})
_REVIEW_STATES = frozenset({"all", "low_risk", "identity", "conflict", "failed"})
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)
_SUCCESS_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})
_KNOWN_STATUSES = _TERMINAL_STATUSES | {"queued", "running", "cancelling"}

_MAX_AUTO_TAG_IMAGES = 10_000
_MAX_PENDING_PAGE_SIZE = 500
_MAX_REVIEW_ITEMS = 1_000
_MAX_ALIAS_ITEMS = 100
_MAX_ALIAS_CHARACTERS = 256
_MAX_FILTER_CHARACTERS = 128
_MAX_MODEL_CHARACTERS = 128
_MAX_LIBRARY_ID_CHARACTERS = 256
_MAX_TAG_CHARACTERS = 4_096
_MAX_TAG_ITEMS = 10_000


class LibraryTaskError(RuntimeError):
    """Base class for desktop-side library task failures."""


class LibraryTaskValidationError(LibraryTaskError, ValueError):
    """A request cannot be represented by the persistent backend contract."""

    def __init__(
        self,
        message: str,
        *,
        field_name: str | None = None,
        code: str = "invalid_request",
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


class LibraryTaskProtocolError(LibraryTaskError):
    """A nominal backend job response violates the desktop contract."""

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        job_id: str | None = None,
        code: str = "invalid_backend_job",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.command = command
        self.job_id = job_id
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> JsonObject:
        details = dict(self.details)
        if self.command is not None:
            details["command"] = self.command
        if self.job_id is not None:
            details["job_id"] = self.job_id
        return {
            "code": self.code,
            "message": str(self),
            "details": details,
        }


class LibraryTaskWaitTimeout(LibraryTaskError, TimeoutError):
    """A submitted task did not reach a terminal state in time."""

    def __init__(self, command: str, job_id: str, timeout_seconds: float) -> None:
        self.command = command
        self.job_id = job_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Library task {command!r} ({job_id}) did not finish within "
            f"{timeout_seconds:g} seconds."
        )

    def to_dict(self) -> JsonObject:
        return {
            "code": "library_task_timeout",
            "message": str(self),
            "details": {
                "command": self.command,
                "job_id": self.job_id,
                "timeout_seconds": self.timeout_seconds,
            },
        }


class LibraryTaskRequest(Protocol):
    """Structural request contract accepted by :class:`LibraryTaskService`."""

    command: ClassVar[str]

    def to_params(self) -> JsonObject: ...


class LibraryTaskClient(Protocol):
    """Narrow persistent-backend client contract used by this module."""

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> JsonObject: ...

    def get_job(self, job_id: str) -> JsonObject: ...

    def cancel_job(self, job_id: str) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class LibrariesRequest:
    """List configured libraries; this control job has no library selector."""

    command: ClassVar[str] = "libraries"

    def to_params(self) -> JsonObject:
        return {}


@dataclass(frozen=True, slots=True)
class IndexRequest:
    """Index one library while preserving the backend's manual-tag semantics."""

    library_id: str
    folder: str | Path | None = None
    recursive: bool = True
    verify_hash: bool = False
    tags: tuple[str, ...] | None = None

    command: ClassVar[str] = "index"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_optional_folder(self)
        _require_boolean(self.recursive, "recursive")
        _require_boolean(self.verify_hash, "verify_hash")
        object.__setattr__(self, "tags", _optional_tags(self.tags, "tags"))

    def to_params(self) -> JsonObject:
        params: JsonObject = {
            "library_id": self.library_id,
            "recursive": self.recursive,
            "verify_hash": self.verify_hash,
            # Keep null and [] distinct; see the module docstring.
            "tags": None if self.tags is None else list(self.tags),
        }
        if self.folder is not None:
            params["folder"] = str(self.folder)
        return params


@dataclass(frozen=True, slots=True)
class SyncRequest:
    library_id: str
    folder: str | Path | None = None
    recursive: bool = True
    verify_hash: bool = False
    dry_run: bool = False
    allow_scope_change: bool = False

    command: ClassVar[str] = "sync"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_optional_folder(self)
        for field_name in (
            "recursive",
            "verify_hash",
            "dry_run",
            "allow_scope_change",
        ):
            _require_boolean(getattr(self, field_name), field_name)

    def to_params(self) -> JsonObject:
        params: JsonObject = {
            "library_id": self.library_id,
            "recursive": self.recursive,
            "verify_hash": self.verify_hash,
            "dry_run": self.dry_run,
            "allow_scope_change": self.allow_scope_change,
        }
        if self.folder is not None:
            params["folder"] = str(self.folder)
        return params


@dataclass(frozen=True, slots=True)
class IndexAndAutoTagRequest:
    """Run embedding and visual annotation concurrently for new images only."""

    library_id: str
    folder: str | Path | None = None
    recursive: bool = True
    verify_hash: bool = False
    tags: tuple[str, ...] | None = None
    model: str | None = DEFAULT_AUTO_TAG_MODEL
    max_images: int = DEFAULT_AUTO_TAG_MAX_IMAGES
    max_budget_cny: float | None = DEFAULT_AUTO_TAG_BUDGET_CNY
    external_processing_confirmed: bool = False

    command: ClassVar[str] = "index_and_auto_tag"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_optional_folder(self)
        _require_boolean(self.recursive, "recursive")
        _require_boolean(self.verify_hash, "verify_hash")
        object.__setattr__(self, "tags", _optional_tags(self.tags, "tags"))
        _set_auto_tag_options(self)
        _require_external_processing_confirmation(
            self.external_processing_confirmed,
            command=self.command,
        )

    def to_params(self) -> JsonObject:
        params: JsonObject = {
            "library_id": self.library_id,
            "recursive": self.recursive,
            "verify_hash": self.verify_hash,
            "tags": None if self.tags is None else list(self.tags),
            "model": self.model,
            "max_images": self.max_images,
            "max_budget_cny": self.max_budget_cny,
            "external_processing_confirmed": True,
        }
        if self.folder is not None:
            params["folder"] = str(self.folder)
        return params


@dataclass(frozen=True, slots=True)
class StatsRequest:
    library_id: str

    command: ClassVar[str] = "stats"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class RootsRequest:
    library_id: str

    command: ClassVar[str] = "roots"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class AutoTagEstimateRequest:
    library_id: str
    scope: AutoTagScope = "untagged"
    model: str | None = DEFAULT_AUTO_TAG_MODEL
    max_images: int = DEFAULT_AUTO_TAG_MAX_IMAGES
    max_budget_cny: float | None = DEFAULT_AUTO_TAG_BUDGET_CNY

    command: ClassVar[str] = "auto_tag_estimate"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_auto_tag_scope(self)
        _set_auto_tag_options(self)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "scope": self.scope,
            "model": self.model,
            "max_images": self.max_images,
            "max_budget_cny": self.max_budget_cny,
            # Estimation never grants consent for a later model request.
            "external_processing_confirmed": False,
        }


@dataclass(frozen=True, slots=True)
class AutoTagRunRequest:
    library_id: str
    scope: AutoTagScope = "untagged"
    model: str | None = DEFAULT_AUTO_TAG_MODEL
    max_images: int = DEFAULT_AUTO_TAG_MAX_IMAGES
    max_budget_cny: float | None = DEFAULT_AUTO_TAG_BUDGET_CNY
    external_processing_confirmed: bool = False

    command: ClassVar[str] = "auto_tag"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_auto_tag_scope(self)
        _set_auto_tag_options(self)
        _require_external_processing_confirmation(
            self.external_processing_confirmed,
            command=self.command,
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "scope": self.scope,
            "model": self.model,
            "max_images": self.max_images,
            "max_budget_cny": self.max_budget_cny,
            "external_processing_confirmed": True,
        }


@dataclass(frozen=True, slots=True)
class AutoTagReviewFilters:
    latest_index_only: bool = False
    character: str = ""
    work: str = ""
    action: str = ""
    expression: str = ""
    review_state: AutoTagReviewState | Literal[""] = ""

    def __post_init__(self) -> None:
        _require_boolean(self.latest_index_only, "latest_index_only")
        for field_name in ("character", "work", "action", "expression"):
            value = _optional_display_text(
                getattr(self, field_name),
                field_name,
                maximum=_MAX_FILTER_CHARACTERS,
            )
            object.__setattr__(self, field_name, value or "")
        if not isinstance(self.review_state, str):
            raise _validation("review_state must be a string.", "review_state")
        state = self.review_state.strip().lower()
        if state and state not in _REVIEW_STATES:
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
            or (self.review_state and self.review_state != "all")
        )

    def to_params(self) -> JsonObject:
        params: JsonObject = {"latest_index_only": self.latest_index_only}
        for field_name in ("character", "work", "action", "expression"):
            value = getattr(self, field_name)
            if value:
                params[field_name] = value
        if self.review_state:
            params["review_state"] = self.review_state
        return params


@dataclass(frozen=True, slots=True)
class AutoTagPendingRequest:
    library_id: str
    offset: int = 0
    limit: int = 100
    filters: AutoTagReviewFilters = field(default_factory=AutoTagReviewFilters)

    command: ClassVar[str] = "auto_tag_pending"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _bounded_integer(self.offset, "offset", minimum=0, maximum=2**63 - 1)
        _bounded_integer(
            self.limit,
            "limit",
            minimum=1,
            maximum=_MAX_PENDING_PAGE_SIZE,
        )
        if not isinstance(self.filters, AutoTagReviewFilters):
            raise _validation(
                "filters must be an AutoTagReviewFilters instance.", "filters"
            )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "offset": self.offset,
            "limit": self.limit,
            "filters": self.filters.to_params(),
        }


@dataclass(frozen=True, slots=True)
class AutoTagReviewDecision:
    proposal_id: str
    decision: AutoTagReviewAction
    accepted_tags: tuple[str, ...] = ()
    confirmed_identity_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        proposal_id = _required_display_text(
            self.proposal_id,
            "proposal_id",
            maximum=1_024,
        )
        object.__setattr__(self, "proposal_id", proposal_id)
        if not isinstance(self.decision, str):
            raise _validation("decision must be a string.", "decision")
        decision = self.decision.strip().lower()
        if decision not in _REVIEW_ACTIONS:
            raise _validation(
                "decision must be accept, edit, reject, or manual.",
                "decision",
                details={"supported": sorted(_REVIEW_ACTIONS)},
            )
        object.__setattr__(self, "decision", decision)
        accepted = _tags(self.accepted_tags, "accepted_tags")
        confirmed = _tags(
            self.confirmed_identity_tags,
            "confirmed_identity_tags",
        )
        object.__setattr__(self, "accepted_tags", accepted)
        object.__setattr__(self, "confirmed_identity_tags", confirmed)

        if decision == "manual" and not accepted:
            raise _validation(
                "Manual labeling requires at least one accepted tag.",
                "accepted_tags",
                code="manual_tags_required",
            )
        if decision in {"reject", "manual"} and confirmed:
            raise _validation(
                "Rejected or manually labeled proposals cannot confirm identity tags.",
                "confirmed_identity_tags",
                code="identity_confirmation_not_allowed",
            )
        accepted_keys = {_comparison_key(tag) for tag in accepted}
        unknown = [
            tag for tag in confirmed if _comparison_key(tag) not in accepted_keys
        ]
        if unknown:
            raise _validation(
                "Confirmed identity tags must also be present in accepted_tags.",
                "confirmed_identity_tags",
                code="identity_confirmation_mismatch",
                details={"tags": unknown},
            )

    def to_dict(self) -> JsonObject:
        return {
            "proposal_id": self.proposal_id,
            "decision": self.decision,
            "accepted_tags": list(self.accepted_tags),
            "confirmed_identity_tags": list(self.confirmed_identity_tags),
        }


@dataclass(frozen=True, slots=True)
class AutoTagReviewRequest:
    library_id: str
    decisions: tuple[AutoTagReviewDecision, ...]

    command: ClassVar[str] = "auto_tag_review"

    def __post_init__(self) -> None:
        _set_library_id(self)
        decisions = _review_decisions(self.decisions)
        object.__setattr__(self, "decisions", decisions)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class IdentityConfirmationRequest:
    """Confirm identity tags for exactly one proposal.

    The one-proposal shape is intentional: real-person, Cosplayer, character
    and work identities must never flow through the batch acceptance endpoint.
    """

    library_id: str
    proposal_id: str
    accepted_tags: tuple[str, ...]
    confirmed_identity_tags: tuple[str, ...]
    decision: Literal["accept", "edit"] = "accept"

    command: ClassVar[str] = "auto_tag_review"

    def __post_init__(self) -> None:
        _set_library_id(self)
        decision = AutoTagReviewDecision(
            proposal_id=self.proposal_id,
            decision=self.decision,
            accepted_tags=self.accepted_tags,
            confirmed_identity_tags=self.confirmed_identity_tags,
        )
        if not decision.confirmed_identity_tags:
            raise _validation(
                "At least one identity tag must be confirmed explicitly.",
                "confirmed_identity_tags",
                code="identity_confirmation_required",
            )
        object.__setattr__(self, "proposal_id", decision.proposal_id)
        object.__setattr__(self, "accepted_tags", decision.accepted_tags)
        object.__setattr__(
            self,
            "confirmed_identity_tags",
            decision.confirmed_identity_tags,
        )
        object.__setattr__(self, "decision", decision.decision)

    def to_params(self) -> JsonObject:
        decision = AutoTagReviewDecision(
            proposal_id=self.proposal_id,
            decision=self.decision,
            accepted_tags=self.accepted_tags,
            confirmed_identity_tags=self.confirmed_identity_tags,
        )
        return {
            "library_id": self.library_id,
            "decisions": [decision.to_dict()],
        }


@dataclass(frozen=True, slots=True)
class LowRiskBatchReviewRequest:
    """Accept low-risk tags while forcing identity proposals to remain pending."""

    library_id: str
    proposal_ids: tuple[str, ...]
    accepted_tags_by_proposal: Mapping[str, Sequence[str]]

    command: ClassVar[str] = "auto_tag_review_batch"

    def __post_init__(self) -> None:
        _set_library_id(self)
        proposal_ids = _unique_strings(
            self.proposal_ids,
            "proposal_ids",
            minimum_items=1,
            maximum_items=_MAX_REVIEW_ITEMS,
            maximum_characters=1_024,
        )
        object.__setattr__(self, "proposal_ids", proposal_ids)
        if not isinstance(self.accepted_tags_by_proposal, Mapping):
            raise _validation(
                "accepted_tags_by_proposal must be a mapping.",
                "accepted_tags_by_proposal",
            )
        requested_ids = set(proposal_ids)
        raw_ids = set(self.accepted_tags_by_proposal)
        unknown = sorted(str(value) for value in raw_ids - requested_ids)
        missing = sorted(requested_ids - raw_ids)
        if unknown or missing:
            raise _validation(
                "accepted_tags_by_proposal must contain exactly the proposal IDs.",
                "accepted_tags_by_proposal",
                code="batch_proposal_mismatch",
                details={"unknown": unknown, "missing": missing},
            )
        normalized: dict[str, tuple[str, ...]] = {}
        for proposal_id in proposal_ids:
            tags = _tags(
                self.accepted_tags_by_proposal[proposal_id],
                f"accepted_tags_by_proposal[{proposal_id!r}]",
            )
            if not tags:
                raise _validation(
                    "Every batch proposal requires at least one accepted low-risk tag.",
                    "accepted_tags_by_proposal",
                    code="low_risk_tags_required",
                    details={"proposal_id": proposal_id},
                )
            normalized[proposal_id] = tags
        object.__setattr__(
            self,
            "accepted_tags_by_proposal",
            MappingProxyType(normalized),
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "proposal_ids": list(self.proposal_ids),
            "accepted_tags_by_proposal": {
                proposal_id: list(tags)
                for proposal_id, tags in self.accepted_tags_by_proposal.items()
            },
            # The backend verifies these against proposal metadata and leaves
            # every identity tag pending for individual confirmation.
            "exclude_identity_tags": True,
        }


@dataclass(frozen=True, slots=True)
class AutoTagUndoRequest:
    library_id: str

    command: ClassVar[str] = "auto_tag_review_undo"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class FolderTagBackfillRequest:
    """Recalculate folder-derived tags without injecting manual tags."""

    library_id: str
    folder: str | Path | None = None
    recursive: bool = True
    verify_hash: bool = False

    command: ClassVar[str] = "folder_tag_backfill"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _set_optional_folder(self)
        _require_boolean(self.recursive, "recursive")
        _require_boolean(self.verify_hash, "verify_hash")

    def to_params(self) -> JsonObject:
        params: JsonObject = {
            "library_id": self.library_id,
            "recursive": self.recursive,
            "verify_hash": self.verify_hash,
        }
        if self.folder is not None:
            params["folder"] = str(self.folder)
        return params


@dataclass(frozen=True, slots=True)
class MetadataBackfillRequest:
    library_id: str
    max_images: int = DEFAULT_AUTO_TAG_MAX_IMAGES

    command: ClassVar[str] = "metadata_backfill"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _bounded_integer(
            self.max_images,
            "max_images",
            minimum=1,
            maximum=_MAX_AUTO_TAG_IMAGES,
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "max_images": self.max_images,
        }


@dataclass(frozen=True, slots=True)
class TagAliasListRequest:
    library_id: str

    command: ClassVar[str] = "tag_alias_list"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class TagAliasUpsertRequest:
    library_id: str
    canonical_name: str
    aliases: tuple[str, ...]

    command: ClassVar[str] = "tag_alias_upsert"

    def __post_init__(self) -> None:
        _set_library_id(self)
        canonical = _required_display_text(
            self.canonical_name,
            "canonical_name",
            maximum=_MAX_ALIAS_CHARACTERS,
            normalize_nfkc=True,
        )
        aliases = _unique_strings(
            self.aliases,
            "aliases",
            minimum_items=1,
            maximum_items=_MAX_ALIAS_ITEMS,
            maximum_characters=_MAX_ALIAS_CHARACTERS,
            normalize_nfkc=True,
            comparison_key=_comparison_key,
        )
        canonical_key = _comparison_key(canonical)
        aliases = tuple(
            alias for alias in aliases if _comparison_key(alias) != canonical_key
        )
        if not aliases:
            raise _validation(
                "At least one alias different from canonical_name is required.",
                "aliases",
                code="alias_required",
            )
        object.__setattr__(self, "canonical_name", canonical)
        object.__setattr__(self, "aliases", aliases)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class TagAliasDeleteRequest:
    library_id: str
    canonical_name: str

    command: ClassVar[str] = "tag_alias_delete"

    def __post_init__(self) -> None:
        _set_library_id(self)
        canonical = _required_display_text(
            self.canonical_name,
            "canonical_name",
            maximum=_MAX_ALIAS_CHARACTERS,
            normalize_nfkc=True,
        )
        object.__setattr__(self, "canonical_name", canonical)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "canonical_name": self.canonical_name,
        }


LibraryRequest: TypeAlias = (
    LibrariesRequest
    | IndexRequest
    | SyncRequest
    | IndexAndAutoTagRequest
    | StatsRequest
    | RootsRequest
    | AutoTagEstimateRequest
    | AutoTagRunRequest
    | AutoTagPendingRequest
    | AutoTagReviewRequest
    | IdentityConfirmationRequest
    | LowRiskBatchReviewRequest
    | AutoTagUndoRequest
    | FolderTagBackfillRequest
    | MetadataBackfillRequest
    | TagAliasListRequest
    | TagAliasUpsertRequest
    | TagAliasDeleteRequest
)


@dataclass(frozen=True, slots=True)
class SubmittedLibraryTask:
    command: str
    job_id: str
    params: Mapping[str, Any]
    submitted_job: JsonObject


@dataclass(frozen=True, slots=True)
class LibraryTaskOutcome:
    submission: SubmittedLibraryTask
    status: str
    job: JsonObject
    result: JsonObject | None
    error: JsonObject | None

    @property
    def successful(self) -> bool:
        return self.status in _SUCCESS_STATUSES


ProgressCallback = Callable[[JsonObject], None]


class LibraryTaskService:
    """Submit and monitor validated library jobs from background workers."""

    def __init__(self, client: LibraryTaskClient) -> None:
        self._client = client

    def submit(self, request: LibraryRequest) -> SubmittedLibraryTask:
        if not hasattr(request, "command") or not callable(
            getattr(request, "to_params", None)
        ):
            raise TypeError("request must be a supported library task request")
        command = request.command
        params = request.to_params()
        job = self._client.submit_job(command, params)
        job_id = _job_id(job, command=command)
        _validate_job_identity(job, command=command, job_id=job_id)
        return SubmittedLibraryTask(
            command=command,
            job_id=job_id,
            params=MappingProxyType(dict(params)),
            submitted_job=job,
        )

    def list_libraries(self) -> SubmittedLibraryTask:
        return self.submit(LibrariesRequest())

    def submit_index(self, request: IndexRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_sync(self, request: SyncRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_index_and_auto_tag(
        self, request: IndexAndAutoTagRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_stats(self, request: StatsRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_roots(self, request: RootsRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def estimate_auto_tags(
        self, request: AutoTagEstimateRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_auto_tag(self, request: AutoTagRunRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def list_pending_auto_tags(
        self, request: AutoTagPendingRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_auto_tag_review(
        self, request: AutoTagReviewRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def confirm_identity(
        self, request: IdentityConfirmationRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def accept_low_risk_batch(
        self, request: LowRiskBatchReviewRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def undo_latest_auto_tag_batch(
        self, request: AutoTagUndoRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def backfill_folder_tags(
        self, request: FolderTagBackfillRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def backfill_metadata(
        self, request: MetadataBackfillRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def list_tag_aliases(self, request: TagAliasListRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def upsert_tag_alias(self, request: TagAliasUpsertRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def delete_tag_alias(self, request: TagAliasDeleteRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def wait(
        self,
        submission: SubmittedLibraryTask,
        *,
        timeout: float = 3_600.0,
        poll_interval: float = 0.25,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> LibraryTaskOutcome:
        """Wait from a worker thread; never call this on the Tk event thread."""

        if not isinstance(submission, SubmittedLibraryTask):
            raise TypeError("submission must be a SubmittedLibraryTask")
        timeout_value = _positive_finite(timeout, "timeout")
        interval = _positive_finite(poll_interval, "poll_interval")
        deadline = time.monotonic() + timeout_value
        cancellation_sent = False
        last_signature: tuple[str, str] | None = None

        while True:
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and not cancellation_sent
            ):
                cancelled = self._client.cancel_job(submission.job_id)
                _validate_job_identity(
                    cancelled,
                    command=submission.command,
                    job_id=submission.job_id,
                )
                cancellation_sent = True
                if on_progress is not None:
                    on_progress(cancelled)

            job = self._client.get_job(submission.job_id)
            _validate_job_identity(
                job,
                command=submission.command,
                job_id=submission.job_id,
            )
            status = _job_status(job, submission)
            signature = (status, repr(job.get("progress")))
            if on_progress is not None and signature != last_signature:
                on_progress(job)
                last_signature = signature
            if status in _TERMINAL_STATUSES:
                return _outcome(submission, job, status)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LibraryTaskWaitTimeout(
                    submission.command,
                    submission.job_id,
                    timeout_value,
                )
            time.sleep(min(interval, remaining))

    def cancel(self, submission: SubmittedLibraryTask) -> JsonObject:
        if not isinstance(submission, SubmittedLibraryTask):
            raise TypeError("submission must be a SubmittedLibraryTask")
        job = self._client.cancel_job(submission.job_id)
        _validate_job_identity(
            job,
            command=submission.command,
            job_id=submission.job_id,
        )
        return job


def _set_library_id(instance: Any) -> None:
    value = _required_display_text(
        instance.library_id,
        "library_id",
        maximum=_MAX_LIBRARY_ID_CHARACTERS,
    )
    object.__setattr__(instance, "library_id", value)


def _set_optional_folder(instance: Any) -> None:
    value = instance.folder
    if value is None:
        return
    if isinstance(value, Path):
        display = str(value.expanduser())
    elif isinstance(value, str):
        display = value.strip()
    else:
        raise _validation("folder must be a path string or None.", "folder")
    if not display:
        raise _validation("folder cannot be empty when supplied.", "folder")
    if _contains_control(display):
        raise _validation("folder cannot contain control characters.", "folder")
    object.__setattr__(instance, "folder", display)


def _set_auto_tag_scope(instance: Any) -> None:
    if not isinstance(instance.scope, str):
        raise _validation("scope must be a string.", "scope")
    scope = instance.scope.strip().lower()
    if scope not in _AUTO_TAG_SCOPES:
        raise _validation(
            "scope must be latest_index_run, untagged, failed, failed_all, or all.",
            "scope",
            details={"supported": sorted(_AUTO_TAG_SCOPES)},
        )
    object.__setattr__(instance, "scope", scope)


def _set_auto_tag_options(instance: Any) -> None:
    model = _optional_display_text(
        instance.model,
        "model",
        maximum=_MAX_MODEL_CHARACTERS,
    )
    object.__setattr__(instance, "model", model)
    _bounded_integer(
        instance.max_images,
        "max_images",
        minimum=1,
        maximum=_MAX_AUTO_TAG_IMAGES,
    )
    budget = instance.max_budget_cny
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            raise _validation(
                "max_budget_cny must be a positive finite number or None.",
                "max_budget_cny",
            )
        budget = float(budget)
        if budget <= 0 or not math.isfinite(budget):
            raise _validation(
                "max_budget_cny must be a positive finite number or None.",
                "max_budget_cny",
            )
        object.__setattr__(instance, "max_budget_cny", budget)


def _require_external_processing_confirmation(value: Any, *, command: str) -> None:
    _require_boolean(value, "external_processing_confirmed")
    if value is not True:
        raise _validation(
            "External processing must be explicitly confirmed before submitting "
            f"{command}.",
            "external_processing_confirmed",
            code="external_processing_not_confirmed",
            details={"command": command},
        )


def _review_decisions(value: Any) -> tuple[AutoTagReviewDecision, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _validation("decisions must be a sequence.", "decisions")
    if not value:
        raise _validation(
            "At least one review decision is required.",
            "decisions",
            code="review_decision_required",
        )
    if len(value) > _MAX_REVIEW_ITEMS:
        raise _validation(
            f"decisions can contain at most {_MAX_REVIEW_ITEMS} items.",
            "decisions",
        )
    result: list[AutoTagReviewDecision] = []
    seen: set[str] = set()
    for index, decision in enumerate(value):
        if not isinstance(decision, AutoTagReviewDecision):
            raise _validation(
                f"decisions[{index}] must be an AutoTagReviewDecision.",
                "decisions",
            )
        if decision.proposal_id in seen:
            raise _validation(
                "A proposal can appear only once in one review request.",
                "decisions",
                code="duplicate_proposal",
                details={"proposal_id": decision.proposal_id},
            )
        seen.add(decision.proposal_id)
        result.append(decision)
    return tuple(result)


def _optional_tags(value: Any, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _tags(value, field_name)


def _tags(value: Any, field_name: str) -> tuple[str, ...]:
    return _unique_strings(
        value,
        field_name,
        minimum_items=0,
        maximum_items=_MAX_TAG_ITEMS,
        maximum_characters=_MAX_TAG_CHARACTERS,
    )


def _unique_strings(
    values: Any,
    field_name: str,
    *,
    minimum_items: int,
    maximum_items: int,
    maximum_characters: int,
    normalize_nfkc: bool = False,
    comparison_key: Callable[[str], str] | None = None,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise _validation(f"{field_name} must be a sequence of strings.", field_name)
    if not minimum_items <= len(values) <= maximum_items:
        raise _validation(
            f"{field_name} must contain between {minimum_items} and "
            f"{maximum_items} items.",
            field_name,
        )
    key_function = comparison_key or (lambda value: value)
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        normalized = _required_display_text(
            value,
            f"{field_name}[{index}]",
            maximum=maximum_characters,
            normalize_nfkc=normalize_nfkc,
        )
        key = key_function(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def _required_display_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    normalize_nfkc: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _validation(f"{field_name} must be a string.", field_name)
    normalized = unicodedata.normalize("NFKC", value) if normalize_nfkc else value
    normalized = normalized.strip()
    if not normalized:
        raise _validation(f"{field_name} cannot be empty.", field_name)
    if len(normalized) > maximum:
        raise _validation(
            f"{field_name} cannot exceed {maximum} characters.", field_name
        )
    if _contains_control(normalized):
        raise _validation(
            f"{field_name} cannot contain control characters.", field_name
        )
    return normalized


def _optional_display_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"{field_name} must be a string or None.", field_name)
    if not value.strip():
        return None
    return _required_display_text(value, field_name, maximum=maximum)


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _require_boolean(value: Any, field_name: str) -> None:
    if not isinstance(value, bool):
        raise _validation(f"{field_name} must be a boolean.", field_name)


def _bounded_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _validation(f"{field_name} must be an integer.", field_name)
    if not minimum <= value <= maximum:
        raise _validation(
            f"{field_name} must be between {minimum} and {maximum}.", field_name
        )
    return value


def _positive_finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive finite number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be a positive finite number")
    return normalized


def _validation(
    message: str,
    field_name: str,
    *,
    code: str = "invalid_request",
    details: Mapping[str, Any] | None = None,
) -> LibraryTaskValidationError:
    return LibraryTaskValidationError(
        message,
        field_name=field_name,
        code=code,
        details=details,
    )


def _job_id(job: Mapping[str, Any], *, command: str) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value.strip():
        raise LibraryTaskProtocolError(
            "The backend did not return a non-empty job ID.",
            command=command,
            details={"job": dict(job)},
        )
    return value.strip()


def _validate_job_identity(
    job: Mapping[str, Any],
    *,
    command: str,
    job_id: str,
) -> None:
    actual_id = _job_id(job, command=command)
    if actual_id != job_id:
        raise LibraryTaskProtocolError(
            f"The backend returned job {actual_id!r} instead of {job_id!r}.",
            command=command,
            job_id=job_id,
            details={"actual_job_id": actual_id},
        )
    actual_command = job.get("command")
    if actual_command is not None and actual_command != command:
        raise LibraryTaskProtocolError(
            f"The backend returned command {actual_command!r} instead of {command!r}.",
            command=command,
            job_id=job_id,
            details={"actual_command": actual_command},
        )


def _job_status(job: Mapping[str, Any], submission: SubmittedLibraryTask) -> str:
    value = job.get("status")
    if not isinstance(value, str) or not value.strip():
        raise LibraryTaskProtocolError(
            "The backend job did not contain a valid status.",
            command=submission.command,
            job_id=submission.job_id,
        )
    status = value.strip().lower()
    if status not in _KNOWN_STATUSES:
        raise LibraryTaskProtocolError(
            f"The backend returned an unsupported job status: {status!r}.",
            command=submission.command,
            job_id=submission.job_id,
            details={"status": status},
        )
    return status


def _outcome(
    submission: SubmittedLibraryTask,
    job: JsonObject,
    status: str,
) -> LibraryTaskOutcome:
    raw_result = job.get("result")
    result = raw_result if isinstance(raw_result, dict) else None
    raw_error = job.get("error")
    error = raw_error if isinstance(raw_error, dict) else None
    if status in _SUCCESS_STATUSES and result is None:
        raise LibraryTaskProtocolError(
            "The completed backend task did not return a result object.",
            command=submission.command,
            job_id=submission.job_id,
        )
    if status == "failed" and error is None:
        raise LibraryTaskProtocolError(
            "The failed backend task did not return an error object.",
            command=submission.command,
            job_id=submission.job_id,
        )
    return LibraryTaskOutcome(
        submission=submission,
        status=status,
        job=job,
        result=result,
        error=error,
    )


__all__ = [
    "AutoTagEstimateRequest",
    "AutoTagPendingRequest",
    "AutoTagReviewAction",
    "AutoTagReviewDecision",
    "AutoTagReviewFilters",
    "AutoTagReviewRequest",
    "AutoTagReviewState",
    "AutoTagRunRequest",
    "AutoTagScope",
    "AutoTagUndoRequest",
    "DEFAULT_AUTO_TAG_BUDGET_CNY",
    "DEFAULT_AUTO_TAG_MAX_IMAGES",
    "DEFAULT_AUTO_TAG_MODEL",
    "FolderTagBackfillRequest",
    "IdentityConfirmationRequest",
    "IndexAndAutoTagRequest",
    "IndexRequest",
    "LibrariesRequest",
    "LibraryRequest",
    "LibraryTaskClient",
    "LibraryTaskError",
    "LibraryTaskOutcome",
    "LibraryTaskProtocolError",
    "LibraryTaskRequest",
    "LibraryTaskService",
    "LibraryTaskValidationError",
    "LibraryTaskWaitTimeout",
    "LowRiskBatchReviewRequest",
    "MetadataBackfillRequest",
    "ProgressCallback",
    "RootsRequest",
    "StatsRequest",
    "SubmittedLibraryTask",
    "SyncRequest",
    "TagAliasDeleteRequest",
    "TagAliasListRequest",
    "TagAliasUpsertRequest",
]
