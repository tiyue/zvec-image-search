"""Validated library-task workflow for the local application host.

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
BatchAcceptanceMode = Literal[
    "low_risk_only",
    "recommended",
    "all_non_conflicting",
]
ManualTagOperation = Literal["add", "remove", "replace_manual"]
FolderNameTagMode = Literal["normal", "clean", "mark_all"]
ClusterScope = Literal["new_or_changed", "all"]
ClusterRunType = Literal["exact", "perceptual", "semantic", "near_duplicate"]
ClusterType = Literal[
    "all",
    "exact",
    "perceptual",
    "semantic",
    "single",
    "near_duplicate",
]
ClusterIdentityCategory = Literal["real_person", "cosplayer", "character", "work"]
LearningDecisionAction = Literal["accept", "reject", "edit", "skip"]

DEFAULT_AUTO_TAG_MODEL = "qwen3-vl-flash"
DEFAULT_AUTO_TAG_MAX_IMAGES = 300
DEFAULT_AUTO_TAG_BUDGET_CNY = 5.0

_AUTO_TAG_SCOPES = frozenset(
    {"latest_index_run", "untagged", "failed", "failed_all", "all"}
)
_REVIEW_ACTIONS = frozenset({"accept", "edit", "reject", "manual"})
_REVIEW_STATES = frozenset({"all", "low_risk", "identity", "conflict", "failed"})
_BATCH_ACCEPTANCE_MODES = frozenset(
    {"low_risk_only", "recommended", "all_non_conflicting"}
)
_CLUSTER_SCOPES = frozenset({"new_or_changed", "all"})
_CLUSTER_RUN_TYPES = frozenset({"exact", "perceptual", "semantic", "near_duplicate"})
_CLUSTER_LIST_TYPES = frozenset(
    {"all", "exact", "perceptual", "semantic", "single", "near_duplicate"}
)
_CLUSTER_IDENTITY_CATEGORIES = frozenset(
    {"real_person", "cosplayer", "character", "work"}
)
_LEARNING_DECISIONS = frozenset({"accept", "reject", "edit", "skip"})
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
_MAX_FOLDER_KEY_CHARACTERS = 8_192
_MAX_FOLDER_PAGE_SIZE = 1_000
_MAX_FOLDER_DELETE_TOKEN_CHARACTERS = 512
_MAX_MANUAL_SELECTION_IDS = 10_000
_MAX_CLUSTER_PAGE_SIZE = 500
_MAX_LEARNING_DECISIONS = 1_000


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
    """Index one library while preserving the backend's manual-tag semantics.

    Per-job concurrency and ``skip_errors`` are intentionally absent. Scanning
    and embedding use the process-wide ``ServiceConfig`` limits, while isolated
    image failures are always recorded and skipped by the index pipeline.
    """

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
    """Synchronize one library using the backend-wide execution policy.

    Like indexing, synchronization does not accept pretend per-job concurrency
    or error-skipping switches; those behaviours are owned by ``ServiceConfig``
    and the failure sink.
    """

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
class FolderListRequest:
    library_id: str
    root_id: str | None = None
    query: str = ""
    offset: int = 0
    limit: int = 200

    command: ClassVar[str] = "folder_list"

    def __post_init__(self) -> None:
        _set_library_id(self)
        root_id = _optional_display_text(
            self.root_id,
            "root_id",
            maximum=_MAX_LIBRARY_ID_CHARACTERS,
        )
        object.__setattr__(self, "root_id", root_id)
        if not isinstance(self.query, str):
            raise _validation("query must be a string.", "query")
        query = self.query.strip()
        if len(query) > 256 or _contains_control(query):
            raise _validation(
                "query cannot exceed 256 characters or contain controls.", "query"
            )
        object.__setattr__(self, "query", query)
        _bounded_integer(self.offset, "offset", minimum=0, maximum=2**63 - 1)
        _bounded_integer(
            self.limit,
            "limit",
            minimum=1,
            maximum=_MAX_FOLDER_PAGE_SIZE,
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "root_id": self.root_id,
            "query": self.query,
            "offset": self.offset,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class FolderImagesRequest:
    library_id: str
    folder_key: str
    include_subfolders: bool = False
    offset: int = 0
    limit: int = 100

    command: ClassVar[str] = "folder_images"

    def __post_init__(self) -> None:
        _set_library_id(self)
        folder_key = _required_display_text(
            self.folder_key,
            "folder_key",
            maximum=_MAX_FOLDER_KEY_CHARACTERS,
        )
        object.__setattr__(self, "folder_key", folder_key)
        _require_boolean(self.include_subfolders, "include_subfolders")
        _bounded_integer(self.offset, "offset", minimum=0, maximum=2**63 - 1)
        _bounded_integer(
            self.limit,
            "limit",
            minimum=1,
            maximum=_MAX_FOLDER_PAGE_SIZE,
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "folder_key": self.folder_key,
            "include_subfolders": self.include_subfolders,
            "offset": self.offset,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class FolderDeletePreviewRequest:
    """Create an expiring, model-free snapshot before destructive work."""

    library_id: str
    folder_key: str
    include_subfolders: bool = True

    command: ClassVar[str] = "folder_delete_preview"

    def __post_init__(self) -> None:
        _set_library_id(self)
        object.__setattr__(
            self,
            "folder_key",
            _required_display_text(
                self.folder_key,
                "folder_key",
                maximum=_MAX_FOLDER_KEY_CHARACTERS,
            ),
        )
        _require_boolean(self.include_subfolders, "include_subfolders")
        if not self.include_subfolders:
            raise _validation(
                "folder deletion must include every descendant folder.",
                "include_subfolders",
            )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "folder_key": self.folder_key,
            "include_subfolders": True,
        }


@dataclass(frozen=True, slots=True)
class FolderDeleteCommitRequest:
    """Commit exactly one valid preview after explicit user confirmation."""

    library_id: str
    operation_id: str
    confirmation_token: str
    confirm: bool = True

    command: ClassVar[str] = "folder_delete_commit"

    def __post_init__(self) -> None:
        _set_library_id(self)
        operation_id = _required_display_text(
            self.operation_id,
            "operation_id",
            maximum=32,
        ).lower()
        if len(operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise _validation(
                "operation_id must be a 32-character hexadecimal id.",
                "operation_id",
            )
        token = _required_display_text(
            self.confirmation_token,
            "confirmation_token",
            maximum=_MAX_FOLDER_DELETE_TOKEN_CHARACTERS,
        )
        _require_boolean(self.confirm, "confirm")
        if not self.confirm:
            raise _validation(
                "explicit folder deletion confirmation is required.",
                "confirm",
                code="confirmation_required",
            )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "confirmation_token", token)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "operation_id": self.operation_id,
            "confirmation_token": self.confirmation_token,
            "confirm": True,
        }


@dataclass(frozen=True, slots=True)
class ManualTagSelection:
    mode: Literal["selected", "folder"]
    doc_ids: tuple[str, ...] = ()
    folder_key: str | None = None
    include_subfolders: bool = False
    excluded_doc_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"selected", "folder"}:
            raise _validation("mode must be selected or folder.", "selection.mode")
        _require_boolean(self.include_subfolders, "selection.include_subfolders")
        doc_ids = _unique_strings(
            self.doc_ids,
            "selection.doc_ids",
            minimum_items=1 if self.mode == "selected" else 0,
            maximum_items=_MAX_MANUAL_SELECTION_IDS,
            maximum_characters=256,
        )
        excluded = _unique_strings(
            self.excluded_doc_ids,
            "selection.excluded_doc_ids",
            minimum_items=0,
            maximum_items=_MAX_MANUAL_SELECTION_IDS,
            maximum_characters=256,
        )
        folder_key = _optional_display_text(
            self.folder_key,
            "selection.folder_key",
            maximum=_MAX_FOLDER_KEY_CHARACTERS,
        )
        if self.mode == "selected":
            if folder_key is not None or excluded or self.include_subfolders:
                raise _validation("selected mode accepts only doc_ids.", "selection")
        elif folder_key is None:
            raise _validation(
                "folder mode requires folder_key.", "selection.folder_key"
            )
        elif doc_ids:
            raise _validation(
                "folder mode does not accept doc_ids.", "selection.doc_ids"
            )
        object.__setattr__(self, "doc_ids", doc_ids)
        object.__setattr__(self, "excluded_doc_ids", excluded)
        object.__setattr__(self, "folder_key", folder_key)

    def to_params(self) -> JsonObject:
        if self.mode == "selected":
            return {"mode": "selected", "doc_ids": list(self.doc_ids)}
        return {
            "mode": "folder",
            "folder_key": self.folder_key,
            "include_subfolders": self.include_subfolders,
            "excluded_doc_ids": list(self.excluded_doc_ids),
        }


@dataclass(frozen=True, slots=True)
class ManualTagBatchRequest:
    library_id: str
    selection: ManualTagSelection
    operation: ManualTagOperation
    tags: tuple[str, ...]

    command: ClassVar[str] = "manual_tag_batch"

    def __post_init__(self) -> None:
        _set_library_id(self)
        if not isinstance(self.selection, ManualTagSelection):
            raise _validation("selection must be a ManualTagSelection.", "selection")
        if self.operation not in {"add", "remove", "replace_manual"}:
            raise _validation(
                "operation must be add, remove, or replace_manual.", "operation"
            )
        tags = _tags(self.tags, "tags")
        if self.operation in {"add", "remove"} and not tags:
            raise _validation(f"{self.operation} requires at least one tag.", "tags")
        object.__setattr__(self, "tags", tags)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "selection": self.selection.to_params(),
            "operation": self.operation,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ManualTagUndoRequest:
    library_id: str

    command: ClassVar[str] = "manual_tag_undo"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class FolderNameTagSelection:
    mode: Literal["library", "folder"]
    folder_key: str | None = None
    include_subfolders: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"library", "folder"}:
            raise _validation("mode must be library or folder.", "selection.mode")
        _require_boolean(self.include_subfolders, "selection.include_subfolders")
        folder_key = _optional_display_text(
            self.folder_key,
            "selection.folder_key",
            maximum=_MAX_FOLDER_KEY_CHARACTERS,
        )
        if self.mode == "library" and folder_key is not None:
            raise _validation(
                "library mode does not accept folder_key.", "selection.folder_key"
            )
        if self.mode == "folder" and folder_key is None:
            raise _validation(
                "folder mode requires folder_key.", "selection.folder_key"
            )
        object.__setattr__(self, "folder_key", folder_key)

    def to_params(self) -> JsonObject:
        if self.mode == "library":
            return {"mode": "library"}
        return {
            "mode": "folder",
            "folder_key": self.folder_key,
            "include_subfolders": self.include_subfolders,
        }


@dataclass(frozen=True, slots=True)
class FolderNameTagEstimateRequest:
    library_id: str
    selection: FolderNameTagSelection
    mode: FolderNameTagMode = "normal"
    force: bool = True

    command: ClassVar[str] = "folder_name_tag_estimate"

    def __post_init__(self) -> None:
        _set_library_id(self)
        if not isinstance(self.selection, FolderNameTagSelection):
            raise _validation(
                "selection must be a FolderNameTagSelection.", "selection"
            )
        if self.mode not in {"normal", "clean", "mark_all"}:
            raise _validation("mode must be normal, clean, or mark_all.", "mode")
        _require_boolean(self.force, "force")

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "selection": self.selection.to_params(),
            "mode": self.mode,
            "force": self.force,
        }


@dataclass(frozen=True, slots=True)
class FolderNameTagApplyRequest:
    library_id: str
    selection: FolderNameTagSelection
    mode: FolderNameTagMode = "normal"
    force: bool = True
    expected_rule_revision: str | None = None

    command: ClassVar[str] = "folder_name_tag_apply"

    def __post_init__(self) -> None:
        _set_library_id(self)
        if not isinstance(self.selection, FolderNameTagSelection):
            raise _validation(
                "selection must be a FolderNameTagSelection.", "selection"
            )
        if self.mode not in {"normal", "clean", "mark_all"}:
            raise _validation("mode must be normal, clean, or mark_all.", "mode")
        _require_boolean(self.force, "force")
        object.__setattr__(
            self,
            "expected_rule_revision",
            _optional_display_text(
                self.expected_rule_revision,
                "expected_rule_revision",
                maximum=64,
            ),
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "selection": self.selection.to_params(),
            "mode": self.mode,
            "force": self.force,
            "expected_rule_revision": self.expected_rule_revision,
        }


@dataclass(frozen=True, slots=True)
class SearchResultsCleanupRequest:
    library_id: str
    keep_latest: int = 3
    dry_run: bool = False

    command: ClassVar[str] = "search_results_cleanup"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _bounded_integer(
            self.keep_latest,
            "keep_latest",
            minimum=1,
            maximum=100,
        )
        _require_boolean(self.dry_run, "dry_run")

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "keep_latest": self.keep_latest,
            "dry_run": self.dry_run,
        }


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
    """Atomically accept a validated batch of model suggestions.

    ``low_risk_only`` preserves the original desktop/backend contract.  Newer
    clients may explicitly confirm one whole batch and include recommended,
    non-conflicting identity suggestions without confirming every tag one by
    one.  The backend remains the trust boundary and recomputes eligibility
    from the current stored proposal.
    """

    library_id: str
    proposal_ids: tuple[str, ...]
    accepted_tags_by_proposal: Mapping[str, Sequence[str]]
    acceptance_mode: BatchAcceptanceMode = "low_risk_only"
    batch_confirmation: bool = False

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
        if not isinstance(self.acceptance_mode, str):
            raise _validation("acceptance_mode must be a string.", "acceptance_mode")
        mode = self.acceptance_mode.strip().lower()
        if mode not in _BATCH_ACCEPTANCE_MODES:
            raise _validation(
                "acceptance_mode must be low_risk_only, recommended, or "
                "all_non_conflicting.",
                "acceptance_mode",
                details={"supported": sorted(_BATCH_ACCEPTANCE_MODES)},
            )
        object.__setattr__(self, "acceptance_mode", mode)
        if not isinstance(self.batch_confirmation, bool):
            raise _validation(
                "batch_confirmation must be a boolean.", "batch_confirmation"
            )
        if mode != "low_risk_only" and not self.batch_confirmation:
            raise _validation(
                "Identity-inclusive batch review requires one explicit batch "
                "confirmation.",
                "batch_confirmation",
                code="batch_confirmation_required",
            )

    def to_params(self) -> JsonObject:
        payload: JsonObject = {
            "library_id": self.library_id,
            "proposal_ids": list(self.proposal_ids),
            "accepted_tags_by_proposal": {
                proposal_id: list(tags)
                for proposal_id, tags in self.accepted_tags_by_proposal.items()
            },
            # Preserve the legacy flag for older persistent backends.  New
            # backends additionally validate acceptance_mode and never trust
            # the client-provided classification of a tag.
            "exclude_identity_tags": self.acceptance_mode == "low_risk_only",
        }
        if self.acceptance_mode != "low_risk_only":
            payload["acceptance_mode"] = self.acceptance_mode
            payload["batch_confirmation"] = self.batch_confirmation
        return payload


@dataclass(frozen=True, slots=True)
class AutoTagUndoRequest:
    library_id: str

    command: ClassVar[str] = "auto_tag_review_undo"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class AutoTagPolicyMigrateRequest:
    library_id: str
    dry_run: bool = False

    command: ClassVar[str] = "auto_tag_policy_migrate"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _require_boolean(self.dry_run, "dry_run")

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id, "dry_run": self.dry_run}


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


@dataclass(frozen=True, slots=True)
class ClusterImagesRequest:
    """Build or refresh local groups using hashes and existing Zvec vectors."""

    library_id: str
    scope: ClusterScope = "new_or_changed"
    cluster_types: tuple[ClusterRunType, ...] = (
        "exact",
        "perceptual",
    )

    command: ClassVar[str] = "cluster_images"

    def __post_init__(self) -> None:
        _set_library_id(self)
        if not isinstance(self.scope, str) or self.scope not in _CLUSTER_SCOPES:
            raise _validation(
                "scope must be new_or_changed or all.",
                "scope",
                details={"supported": sorted(_CLUSTER_SCOPES)},
            )
        values = self.cluster_types
        if not isinstance(values, tuple) or not values:
            raise _validation(
                "cluster_types must contain exact, perceptual, semantic, "
                "and/or near_duplicate.",
                "cluster_types",
            )
        normalized = tuple(
            dict.fromkeys(str(value).strip().lower() for value in values)
        )
        unknown = sorted(set(normalized) - _CLUSTER_RUN_TYPES)
        if unknown:
            raise _validation(
                "cluster_types contains unsupported values.",
                "cluster_types",
                details={
                    "values": unknown,
                    "supported": sorted(_CLUSTER_RUN_TYPES),
                },
            )
        object.__setattr__(self, "scope", self.scope.strip().lower())
        object.__setattr__(self, "cluster_types", normalized)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "scope": self.scope,
            "cluster_types": list(self.cluster_types),
        }


@dataclass(frozen=True, slots=True)
class ClusterListRequest:
    library_id: str
    offset: int = 0
    limit: int = 100
    cluster_type: ClusterType | None = None

    command: ClassVar[str] = "cluster_list"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _bounded_integer(self.offset, "offset", minimum=0, maximum=2**63 - 1)
        _bounded_integer(self.limit, "limit", minimum=1, maximum=_MAX_CLUSTER_PAGE_SIZE)
        value = self.cluster_type
        if value is not None:
            normalized = str(value).strip().lower()
            if normalized not in _CLUSTER_LIST_TYPES:
                raise _validation(
                    "cluster_type must be all, exact, perceptual, semantic, "
                    "single, or near_duplicate.",
                    "cluster_type",
                )
            object.__setattr__(self, "cluster_type", normalized)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "offset": self.offset,
            "limit": self.limit,
            "cluster_type": self.cluster_type,
        }


@dataclass(frozen=True, slots=True)
class ClusterDetailRequest:
    library_id: str
    cluster_id: str
    offset: int = 0
    limit: int = 20

    command: ClassVar[str] = "cluster_detail"

    def __post_init__(self) -> None:
        _set_library_id(self)
        value = _required_display_text(self.cluster_id, "cluster_id", maximum=256)
        _bounded_integer(self.offset, "offset", minimum=0, maximum=2**63 - 1)
        _bounded_integer(self.limit, "limit", minimum=1, maximum=2_000)
        object.__setattr__(self, "cluster_id", value)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "cluster_id": self.cluster_id,
            "offset": self.offset,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class ClusterMergeRequest:
    library_id: str
    cluster_ids: tuple[str, ...]

    command: ClassVar[str] = "cluster_merge"

    def __post_init__(self) -> None:
        _set_library_id(self)
        values = tuple(
            _required_display_text(value, "cluster_ids", maximum=256)
            for value in self.cluster_ids
        )
        if len(set(values)) != len(values):
            raise _validation(
                "cluster_ids must not contain duplicate values.",
                "cluster_ids",
            )
        if not 2 <= len(values) <= 100:
            raise _validation(
                "cluster_ids must contain 2 to 100 unique values.",
                "cluster_ids",
            )
        object.__setattr__(self, "cluster_ids", values)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id, "cluster_ids": list(self.cluster_ids)}


@dataclass(frozen=True, slots=True)
class ClusterSplitRequest:
    library_id: str
    cluster_id: str
    doc_ids: tuple[str, ...]

    command: ClassVar[str] = "cluster_split"

    def __post_init__(self) -> None:
        _set_library_id(self)
        object.__setattr__(
            self,
            "cluster_id",
            _required_display_text(self.cluster_id, "cluster_id", maximum=256),
        )
        values = tuple(
            _required_display_text(value, "doc_ids", maximum=1_024)
            for value in self.doc_ids
        )
        if len(set(values)) != len(values):
            raise _validation(
                "doc_ids must not contain duplicate values.",
                "doc_ids",
            )
        if not 1 <= len(values) <= 10_000:
            raise _validation(
                "doc_ids must contain 1 to 10000 unique values.",
                "doc_ids",
            )
        object.__setattr__(self, "doc_ids", values)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "cluster_id": self.cluster_id,
            "doc_ids": list(self.doc_ids),
        }


@dataclass(frozen=True, slots=True)
class ClusterApplyIdentityRequest:
    library_id: str
    cluster_id: str
    identity_category: ClusterIdentityCategory
    identity_value: str

    command: ClassVar[str] = "cluster_apply_identity"

    def __post_init__(self) -> None:
        _set_library_id(self)
        object.__setattr__(
            self,
            "cluster_id",
            _required_display_text(self.cluster_id, "cluster_id", maximum=256),
        )
        category = str(self.identity_category).strip().casefold()
        if category not in _CLUSTER_IDENTITY_CATEGORIES:
            raise _validation(
                "identity_category must be real_person, cosplayer, character, or work.",
                "identity_category",
            )
        object.__setattr__(self, "identity_category", category)
        object.__setattr__(
            self,
            "identity_value",
            _required_display_text(
                self.identity_value,
                "identity_value",
                maximum=256,
            ),
        )

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "cluster_id": self.cluster_id,
            "identity_category": self.identity_category,
            "identity_value": self.identity_value,
        }


@dataclass(frozen=True, slots=True)
class ClusterUndoRequest:
    library_id: str

    command: ClassVar[str] = "cluster_undo"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


@dataclass(frozen=True, slots=True)
class ActiveLearningQueueRequest:
    library_id: str
    review_budget: int = 25

    command: ClassVar[str] = "active_learning_queue"

    def __post_init__(self) -> None:
        _set_library_id(self)
        _bounded_integer(self.review_budget, "review_budget", minimum=20, maximum=30)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "review_budget": self.review_budget,
        }


@dataclass(frozen=True, slots=True)
class ActiveLearningDecision:
    doc_id: str
    decision: LearningDecisionAction
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        doc_id = _required_display_text(self.doc_id, "doc_id", maximum=1_024)
        action = str(self.decision).strip().lower()
        if action not in _LEARNING_DECISIONS:
            raise _validation(
                "decision must be accept, reject, edit, or skip.", "decision"
            )
        labels = _optional_tags(self.labels, "labels") or ()
        if action == "edit" and not labels:
            raise _validation("edit decisions require labels.", "labels")
        if action != "edit" and labels:
            raise _validation("Only edit decisions may include labels.", "labels")
        object.__setattr__(self, "doc_id", doc_id)
        object.__setattr__(self, "decision", action)
        object.__setattr__(self, "labels", labels)

    def to_dict(self) -> JsonObject:
        return {
            "doc_id": self.doc_id,
            "decision": self.decision,
            "labels": list(self.labels),
        }


@dataclass(frozen=True, slots=True)
class ActiveLearningReviewRequest:
    library_id: str
    queue_id: str
    decisions: tuple[ActiveLearningDecision, ...]

    command: ClassVar[str] = "active_learning_review"

    def __post_init__(self) -> None:
        _set_library_id(self)
        queue_id = _required_display_text(self.queue_id, "queue_id", maximum=256)
        if not isinstance(self.decisions, tuple) or not self.decisions:
            raise _validation("decisions must be a non-empty tuple.", "decisions")
        if len(self.decisions) > _MAX_LEARNING_DECISIONS:
            raise _validation(
                f"decisions can contain at most {_MAX_LEARNING_DECISIONS} items.",
                "decisions",
            )
        if not all(isinstance(item, ActiveLearningDecision) for item in self.decisions):
            raise _validation(
                "decisions must contain ActiveLearningDecision values.", "decisions"
            )
        doc_ids = [item.doc_id for item in self.decisions]
        if len(set(doc_ids)) != len(doc_ids):
            raise _validation(
                "decisions contains duplicate doc_id values.", "decisions"
            )
        object.__setattr__(self, "queue_id", queue_id)

    def to_params(self) -> JsonObject:
        return {
            "library_id": self.library_id,
            "queue_id": self.queue_id,
            "decisions": [item.to_dict() for item in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class ActiveLearningReviewUndoRequest:
    library_id: str

    command: ClassVar[str] = "active_learning_review_undo"

    def __post_init__(self) -> None:
        _set_library_id(self)

    def to_params(self) -> JsonObject:
        return {"library_id": self.library_id}


LibraryRequest: TypeAlias = (
    LibrariesRequest
    | IndexRequest
    | SyncRequest
    | IndexAndAutoTagRequest
    | StatsRequest
    | RootsRequest
    | FolderListRequest
    | FolderImagesRequest
    | FolderDeletePreviewRequest
    | FolderDeleteCommitRequest
    | ManualTagBatchRequest
    | ManualTagUndoRequest
    | FolderNameTagEstimateRequest
    | FolderNameTagApplyRequest
    | SearchResultsCleanupRequest
    | AutoTagEstimateRequest
    | AutoTagRunRequest
    | AutoTagPendingRequest
    | AutoTagReviewRequest
    | IdentityConfirmationRequest
    | LowRiskBatchReviewRequest
    | AutoTagUndoRequest
    | AutoTagPolicyMigrateRequest
    | FolderTagBackfillRequest
    | MetadataBackfillRequest
    | TagAliasListRequest
    | TagAliasUpsertRequest
    | TagAliasDeleteRequest
    | ClusterImagesRequest
    | ClusterListRequest
    | ClusterDetailRequest
    | ClusterMergeRequest
    | ClusterSplitRequest
    | ClusterApplyIdentityRequest
    | ClusterUndoRequest
    | ActiveLearningQueueRequest
    | ActiveLearningReviewRequest
    | ActiveLearningReviewUndoRequest
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

    def list_folders(self, request: FolderListRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def list_folder_images(self, request: FolderImagesRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def preview_folder_delete(
        self, request: FolderDeletePreviewRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def commit_folder_delete(
        self, request: FolderDeleteCommitRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def submit_manual_tag_batch(
        self, request: ManualTagBatchRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def undo_latest_manual_tag_batch(
        self, request: ManualTagUndoRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def estimate_folder_name_tags(
        self, request: FolderNameTagEstimateRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def apply_folder_name_tags(
        self, request: FolderNameTagApplyRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def cleanup_search_results(
        self, request: SearchResultsCleanupRequest
    ) -> SubmittedLibraryTask:
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

    def migrate_auto_tag_policy(
        self, request: AutoTagPolicyMigrateRequest
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

    def cluster_images(self, request: ClusterImagesRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def list_clusters(self, request: ClusterListRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def cluster_detail(self, request: ClusterDetailRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def merge_clusters(self, request: ClusterMergeRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def split_cluster(self, request: ClusterSplitRequest) -> SubmittedLibraryTask:
        return self.submit(request)

    def apply_cluster_identity(
        self, request: ClusterApplyIdentityRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def undo_latest_cluster_operation(
        self, request: ClusterUndoRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def active_learning_queue(
        self, request: ActiveLearningQueueRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def review_active_learning(
        self, request: ActiveLearningReviewRequest
    ) -> SubmittedLibraryTask:
        return self.submit(request)

    def undo_latest_active_learning_review(
        self, request: ActiveLearningReviewUndoRequest
    ) -> SubmittedLibraryTask:
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
    "ActiveLearningDecision",
    "ActiveLearningQueueRequest",
    "ActiveLearningReviewRequest",
    "ActiveLearningReviewUndoRequest",
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
    "BatchAcceptanceMode",
    "ClusterDetailRequest",
    "ClusterApplyIdentityRequest",
    "ClusterIdentityCategory",
    "ClusterImagesRequest",
    "ClusterListRequest",
    "ClusterMergeRequest",
    "ClusterRunType",
    "ClusterScope",
    "ClusterSplitRequest",
    "ClusterType",
    "ClusterUndoRequest",
    "DEFAULT_AUTO_TAG_BUDGET_CNY",
    "DEFAULT_AUTO_TAG_MAX_IMAGES",
    "DEFAULT_AUTO_TAG_MODEL",
    "FolderNameTagApplyRequest",
    "FolderNameTagEstimateRequest",
    "FolderNameTagSelection",
    "FolderTagBackfillRequest",
    "FolderDeleteCommitRequest",
    "FolderDeletePreviewRequest",
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
    "LearningDecisionAction",
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
