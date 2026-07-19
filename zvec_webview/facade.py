"""Thread-safe adapter from the webview gateway to existing desktop services."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar, cast

from image_vector_service.activity_store import (
    ActivityStore,
    ActivityStoreUnavailable,
    InvalidActivityCursor,
)
from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.library_browser import LibraryBrowser
from zvec_desktop.backend_api import BackendApiError, JsonObject
from zvec_desktop.backend_host import BackendBusyError, BackendHost, BackendRuntime
from zvec_desktop.configuration_service import (
    ConfigurationSnapshot,
    DesktopConfigurationError,
    DesktopConfigurationService,
)
from zvec_desktop.credentials import CredentialStore, default_credential_store
from zvec_desktop.library_tasks import (
    AutoTagEstimateRequest,
    AutoTagPendingRequest,
    AutoTagPolicyMigrateRequest,
    AutoTagReviewAction,
    AutoTagReviewDecision,
    AutoTagReviewFilters,
    AutoTagReviewRequest,
    AutoTagReviewState,
    AutoTagRunRequest,
    AutoTagScope,
    AutoTagUndoRequest,
    BatchAcceptanceMode,
    FolderDeleteCommitRequest,
    FolderDeletePreviewRequest,
    FolderTagBackfillRequest,
    IdentityConfirmationRequest,
    IndexAndAutoTagRequest,
    IndexRequest,
    LibraryRequest,
    LibraryTaskOutcome,
    LibraryTaskService,
    LowRiskBatchReviewRequest,
    ManualTagBatchRequest,
    ManualTagOperation,
    ManualTagSelection,
    ManualTagUndoRequest,
    MetadataBackfillRequest,
    SearchResultsCleanupRequest,
    StatsRequest,
    SyncRequest,
    TagAliasDeleteRequest,
    TagAliasListRequest,
    TagAliasUpsertRequest,
)
from zvec_desktop.model_settings import ModelSettingsService, ModelSettingsSnapshot
from zvec_desktop.result_catalog import (
    ResultCatalog,
    ResultCatalogError,
    SearchResult,
    SearchResultPage,
)
from zvec_desktop.search_service import (
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SearchService,
    SearchSortMode,
    TagMode,
)

from .image_registry import ImageRegistry, ImageRegistryError

_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "partial",
        "needs_attention",
        "failed",
        "cancelled",
        "interrupted",
    }
)
_SUCCESS_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})
_MAX_OPERATIONS = 500
_LIBRARY_CREATE_FIELDS = frozenset(
    {
        "name",
        "image_root",
        "workspace_directory",
        "enabled",
        "results_directory",
    }
)
_LIBRARY_UPDATE_FIELDS = frozenset(
    {
        "name",
        "image_root",
        "workspace_directory",
        "enabled",
        "is_default",
        # This is a global setting, but it is edited from the library settings
        # surface and is returned separately in every response.
        "results_directory",
    }
)
_MAX_TECHNICAL_COUNT = 2**31 - 1
IdentityReviewAction = Literal["accept", "edit"]
_Choice = TypeVar("_Choice", bound=str)
_AUTO_TAG_SCOPE_OPTIONS: tuple[AutoTagScope, ...] = (
    "latest_index_run",
    "untagged",
    "failed",
    "failed_all",
    "all",
)
_REVIEW_ACTION_OPTIONS: tuple[AutoTagReviewAction, ...] = (
    "accept",
    "edit",
    "reject",
    "manual",
)
_IDENTITY_REVIEW_ACTION_OPTIONS: tuple[IdentityReviewAction, ...] = (
    "accept",
    "edit",
)
_BATCH_ACCEPTANCE_MODE_OPTIONS: tuple[BatchAcceptanceMode, ...] = (
    "low_risk_only",
    "recommended",
    "all_non_conflicting",
)
_MANUAL_TAG_OPERATION_OPTIONS: tuple[ManualTagOperation, ...] = (
    "add",
    "remove",
    "replace_manual",
)
_REVIEW_STATE_OPTIONS: tuple[AutoTagReviewState | Literal[""], ...] = (
    "",
    "all",
    "low_risk",
    "identity",
    "conflict",
    "failed",
)
_ACTIVITY_HISTORY_EXCLUDED_COMMANDS = frozenset(
    {
        "search",
        "stats",
        "roots",
        "libraries",
        "folder_list",
        "folder_images",
        "folder_delete_preview",
        "auto_tag_estimate",
        "auto_tag_pending",
        "tag_alias_list",
    }
)
_ACTIVITY_CATEGORY_GROUPS: Mapping[str, tuple[str, ...]] = {
    # The product UI exposes one concise "设置" filter while the durable log
    # keeps the three sources distinct for diagnostics and export.
    "settings": (
        "model_configuration",
        "credentials",
        "library_configuration",
    ),
}


class FacadeError(RuntimeError):
    """Structured error safe to return through the loopback gateway."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details) if details is not None else None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {"code": self.code, "message": str(self)}
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(slots=True)
class _SearchOperation:
    operation_id: str
    query: JsonObject
    submitted_at: float
    submission: Any
    status: str = "queued"
    progress: JsonObject | None = None
    outcome: SearchOutcome | None = None
    error: JsonObject | None = None
    finished_at: float | None = None
    future: Future[Any] | None = None


@dataclass(slots=True)
class _TaskOperation:
    operation_id: str
    task_type: str
    library_id: str
    submitted_at: float
    submission: Any
    status: str = "queued"
    progress: JsonObject | None = None
    outcome: LibraryTaskOutcome | None = None
    error: JsonObject | None = None
    finished_at: float | None = None
    future: Future[Any] | None = None


@dataclass(slots=True)
class _OrganizeCache:
    proposals: list[JsonObject] = field(default_factory=list)
    aliases: list[JsonObject] = field(default_factory=list)
    undo_available: bool = False
    updated_at: float | None = None
    loading: bool = False
    error: JsonObject | None = None


class PreviewFacade:
    """Own existing services while keeping every long operation off HTTP threads."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        backend_host: BackendHost | None = None,
        credential_store: CredentialStore | None = None,
        image_registry: ImageRegistry | None = None,
        executor: ThreadPoolExecutor | None = None,
        activity_store: ActivityStore | None = None,
        observer_job_history_fallback: bool = False,
    ) -> None:
        if not isinstance(observer_job_history_fallback, bool):
            raise TypeError("observer_job_history_fallback must be a boolean")
        self._configuration = DesktopConfigurationService(config_path)
        self._models = ModelSettingsService(
            self._configuration.config_home / "models.json"
        )
        self._credentials = credential_store or default_credential_store()
        self._registry = image_registry or ImageRegistry(
            cache_directory=self._configuration.config_home / "cache"
        )
        self._host = backend_host or BackendHost(config_path)
        activity_redactions: list[str] = []
        with suppress(Exception):
            if secret := self._credentials.read_secret():
                activity_redactions.append(secret)
        self._activity = activity_store or ActivityStore(
            self._configuration.config_home,
            redactions=activity_redactions,
            # Only the backend process may recover unfinished jobs, and only
            # after it owns backend.lock.  A second/reconnecting UI process
            # must never mark work in the live backend as interrupted.
            recover_interrupted=False,
        )
        self._owns_activity = activity_store is None
        self._observer_job_history_fallback = observer_job_history_fallback
        self._executor = executor or ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="zvec-webview",
        )
        self._owns_executor = executor is None
        self._lock = threading.RLock()
        self._backend_state = "idle"
        self._backend_error: JsonObject | None = None
        self._backend_runtime: BackendRuntime | None = None
        self._backend_future: Future[Any] | None = None
        self._search_service: SearchService | None = None
        self._task_service: LibraryTaskService | None = None
        self._searches: dict[str, _SearchOperation] = {}
        self._tasks: dict[str, _TaskOperation] = {}
        self._organize = _OrganizeCache()
        self._result_catalog: ResultCatalog | None = None
        self._result_catalog_version: tuple[int, int] | None = None
        self._activity_job_statuses: dict[str, str] = {}
        self._activity_search_statuses: dict[str, str] = {}
        self._closed = False

    @property
    def image_registry(self) -> ImageRegistry:
        return self._registry

    @property
    def config_home(self) -> Path:
        return self._configuration.config_home

    def diagnostic_redactions(self) -> tuple[str, ...]:
        """Return process-local secrets for the private diagnostic writer."""

        return self._activity.redactions()

    def job_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        task_type: str | None = None,
        library_id: str | None = None,
        query: str | None = None,
    ) -> JsonObject:
        """Return durable task history after reconciling a live backend."""

        self._refresh_activity_jobs()
        self._activity.flush(timeout=0.25)
        try:
            return self._activity.list_job_history(
                cursor=cursor,
                limit=limit,
                status=status,
                task_type=task_type,
                library_id=library_id,
                query=query,
            )
        except InvalidActivityCursor as exc:
            raise FacadeError(
                "invalid_activity_cursor",
                "任务历史分页位置无效，请刷新后重试。",
                status=400,
            ) from exc
        except ValueError as exc:
            raise FacadeError(
                "invalid_activity_filter",
                str(exc),
                status=400,
            ) from exc
        except ActivityStoreUnavailable as exc:
            raise FacadeError(
                "activity_store_unavailable",
                "任务历史暂时不可用，主任务仍可继续执行。",
                status=503,
            ) from exc

    def activity_logs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        level: str | None = None,
        category: str | None = None,
        library_id: str | None = None,
        job_id: str | None = None,
        query: str | None = None,
    ) -> JsonObject:
        """Return sanitized structured activity events with stable cursors."""

        self._activity.flush(timeout=0.25)
        normalized_category = category.strip().lower() if category else None
        category_filter: str | Sequence[str] | None = (
            _ACTIVITY_CATEGORY_GROUPS.get(normalized_category, normalized_category)
            if normalized_category
            else None
        )
        try:
            page = self._activity.list_operation_logs(
                cursor=cursor,
                limit=limit,
                level=level,
                category=category_filter,
                library_id=library_id,
                job_id=job_id,
                query=query,
            )
            self._hydrate_activity_image_urls(page)
            return page
        except InvalidActivityCursor as exc:
            raise FacadeError(
                "invalid_activity_cursor",
                "操作日志分页位置无效，请刷新后重试。",
                status=400,
            ) from exc
        except ValueError as exc:
            raise FacadeError(
                "invalid_activity_filter",
                str(exc),
                status=400,
            ) from exc
        except ActivityStoreUnavailable as exc:
            raise FacadeError(
                "activity_store_unavailable",
                "操作日志暂时不可用，主任务仍可继续执行。",
                status=503,
            ) from exc

    def record_frontend_activity(self, payload: Mapping[str, Any]) -> bool:
        """Mirror an already validated browser diagnostic into activity.sqlite3."""

        event = str(payload.get("event") or "frontend_event").strip()
        level = str(payload.get("level") or "info").strip().lower()
        if level not in {"debug", "info", "warning", "error"}:
            level = "info"
        safe_level = cast(Literal["debug", "info", "warning", "error"], level)
        details = payload.get("details")
        return self._activity.log(
            level=safe_level,
            category="frontend",
            event=event,
            source="webview",
            message=str(payload.get("message") or event),
            timestamp=(
                str(payload["timestamp"])
                if isinstance(payload.get("timestamp"), str)
                else None
            ),
            details=details if isinstance(details, Mapping) else {},
        )

    def _activity_log(
        self,
        *,
        level: Literal["debug", "info", "warning", "error"],
        category: str,
        event: str,
        message: str,
        source: str = "facade",
        library_id: str | None = None,
        library_name: str | None = None,
        job_id: str | None = None,
        operation_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # Activity logging is deliberately best-effort. A read-only ConfigHome,
        # antivirus lock, or full disk must never fail search or a library task.
        with suppress(Exception):
            self._activity.log(
                level=level,
                category=category,
                event=event,
                source=source,
                message=message,
                library_id=library_id,
                library_name=library_name,
                job_id=job_id,
                operation_id=operation_id,
                details=details or {},
            )

    def _record_job_snapshot(
        self,
        job: Mapping[str, Any],
        *,
        announce: bool = True,
    ) -> None:
        command = str(job.get("command") or job.get("task_type") or "").strip()
        job_id = str(job.get("id") or job.get("job_id") or "").strip()
        status = str(job.get("status") or "unknown").strip().lower()
        if not command or not job_id:
            return
        view = self._job_view(job)
        library_id = str(view.get("library_id") or "")
        library_name = str(view.get("library_name") or "")
        if (
            self._observer_job_history_fallback
            and command not in _ACTIVITY_HISTORY_EXCLUDED_COMMANDS
        ):
            record = dict(job)
            record["task_type"] = command
            record["library_id"] = library_id
            record["library_name"] = library_name
            with suppress(Exception):
                self._activity.record_observed_job(record)

        with self._lock:
            previous = self._remember_activity_status_locked(
                self._activity_job_statuses,
                job_id,
                status,
            )
        if not announce or previous == status:
            return
        category = _activity_task_category(command)
        level = _activity_status_level(status)
        error = job.get("error")
        error_map = error if isinstance(error, Mapping) else {}
        details: JsonObject = {
            "task_type": command,
            "processed": int(view.get("processed") or 0),
            "total": int(view.get("total") or 0),
            "failed": int(view.get("failure_count") or 0),
            "progress_percent": float(view.get("progress_percent") or 0),
        }
        if error_map.get("code"):
            details["error_code"] = str(error_map["code"])
        self._activity_log(
            level=level,
            category=category,
            event=_activity_status_event(status),
            source="backend",
            message=_activity_job_message(command, status),
            library_id=library_id or None,
            library_name=library_name or None,
            job_id=job_id,
            operation_id=job_id,
            details=details,
        )
        if status in _TERMINAL_STATUSES and self._observer_job_history_fallback:
            self._record_image_failures(view, command=command)

    def _record_image_failures(
        self,
        job_view: Mapping[str, Any],
        *,
        command: str,
    ) -> None:
        raw = job_view.get("error_images")
        if not isinstance(raw, list):
            return
        job_id = str(job_view.get("id") or "")
        library_id = str(job_view.get("library_id") or "")
        library_name = str(job_view.get("library_name") or "")
        for item in raw[:200]:
            if not isinstance(item, Mapping):
                continue
            details: JsonObject = {}
            for key in (
                "name",
                "relative_path",
                "reason",
            ):
                value = item.get(key)
                if value is not None and value != "":
                    details[key] = value
            self._activity_log(
                level="warning",
                category="image_failure",
                event="image_failed",
                source="backend",
                message=str(item.get("reason") or "单张图片处理失败，批次已继续。"),
                library_id=library_id or None,
                library_name=library_name or None,
                job_id=job_id or None,
                operation_id=job_id or None,
                details={"task_type": command, **details},
            )

    def _hydrate_activity_image_urls(self, page: Mapping[str, Any]) -> None:
        """Mint fresh ImageRegistry URLs from safe durable image identity."""

        items = page.get("items")
        if not isinstance(items, list):
            return
        try:
            snapshot = self._configuration.load()
            assert snapshot is not None
            libraries = snapshot.configuration.by_id
        except Exception:
            return
        for raw in items:
            if not isinstance(raw, dict) or raw.get("category") != "image_failure":
                continue
            details = raw.get("details")
            if not isinstance(details, Mapping):
                continue
            library_id = str(raw.get("library_id") or "")
            library = libraries.get(library_id)
            relative_path = _safe_relative_path(details.get("relative_path"))
            if library is None or not relative_path:
                continue
            try:
                root = library.image_root.resolve(strict=True)
                path = root.joinpath(*PurePosixPath(relative_path).parts).resolve(
                    strict=True
                )
                path.relative_to(root)
                metadata = self._registry.register(path)
            except (ImageRegistryError, OSError, ValueError):
                continue
            raw["thumbnail_url"] = f"api/image/{metadata.image_id}?variant=thumbnail"
            raw["image_url"] = f"api/image/{metadata.image_id}?variant=preview"

    def _refresh_activity_jobs(self, *, announce: bool = True) -> None:
        with self._lock:
            ready = self._backend_state == "ready" and not self._closed
        if not ready:
            return
        try:
            payload = self._ready_client().list_jobs(limit=500)
        except Exception:
            return
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            command = str(job.get("command") or "")
            if command == "search":
                self._record_reconciled_search(job, announce=announce)
            elif command not in {
                "stats",
                "roots",
                "libraries",
                "folder_list",
                "folder_images",
                "auto_tag_pending",
                "tag_alias_list",
            }:
                self._record_job_snapshot(job, announce=announce)

    def _record_reconciled_search(
        self,
        job: Mapping[str, Any],
        *,
        announce: bool = True,
    ) -> None:
        job_id = str(job.get("id") or "").strip()
        status = str(job.get("status") or "unknown").strip().lower()
        if not job_id:
            return
        with self._lock:
            previous = self._remember_activity_status_locked(
                self._activity_search_statuses,
                job_id,
                status,
            )
        if not announce or previous == status:
            return
        result = job.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        result_count = _first_int(result_map, "result_count", "returned_count")
        details: JsonObject = {
            "result_count": result_count,
            "candidate_count": _first_int(result_map, "candidate_count"),
        }
        event = _activity_search_event(status, result_count)
        self._activity_log(
            level=_activity_status_level(status),
            category="search",
            event=event,
            source="backend",
            message=_activity_search_message(status, result_count),
            job_id=job_id,
            operation_id=job_id,
            details=details,
        )

    def _record_search_operation(self, operation: _SearchOperation) -> None:
        status = str(operation.status or "unknown").lower()
        with self._lock:
            previous = self._remember_activity_status_locked(
                self._activity_search_statuses,
                operation.operation_id,
                status,
            )
        if previous == status:
            return
        result = operation.outcome.result if operation.outcome is not None else None
        result_map = result if isinstance(result, Mapping) else {}
        result_count = _first_int(result_map, "result_count", "returned_count")
        library_ids = operation.query.get("library_ids")
        if not isinstance(library_ids, Sequence) or isinstance(
            library_ids, (str, bytes, bytearray)
        ):
            library_ids = ()
        details: JsonObject = {
            "query_type": str(
                operation.query.get("mode")
                or operation.query.get("query_type")
                or "semantic"
            ),
            "requested_count": _first_int(operation.query, "top_k", "page_size"),
            "library_count": len(library_ids),
            "result_count": result_count,
            "candidate_count": _first_int(result_map, "candidate_count"),
            "elapsed_ms": _elapsed_ms(operation.submitted_at, operation.finished_at),
        }
        if operation.error and operation.error.get("code"):
            details["error_code"] = str(operation.error["code"])
        self._activity_log(
            level=_activity_status_level(status),
            category="search",
            event=_activity_search_event(status, result_count),
            source="facade",
            message=_activity_search_message(status, result_count),
            job_id=operation.operation_id,
            operation_id=operation.operation_id,
            details=details,
        )

    def start_backend_async(self) -> Future[Any] | None:
        """Start the persistent backend once without blocking the webview page."""

        with self._lock:
            self._ensure_open_locked()
            if self._backend_state in {"starting", "ready"}:
                return self._backend_future
            try:
                snapshot = self._configuration.load(optional=True)
            except Exception as exc:
                self._backend_state = "degraded"
                self._backend_error = _error_payload(exc, "configuration_unavailable")
                self._activity_log(
                    level="error",
                    category="backend",
                    event="configuration_unavailable",
                    message="后端配置无法读取。",
                    details={"error_type": exc.__class__.__name__},
                )
                return None
            if snapshot is None:
                self._backend_state = "needs_setup"
                self._backend_error = None
                self._activity_log(
                    level="info",
                    category="backend",
                    event="backend_needs_setup",
                    message="尚未创建图库，后端等待首次设置。",
                )
                return None
            self._backend_state = "starting"
            self._backend_error = None
            self._activity_log(
                level="info",
                category="backend",
                event="backend_starting",
                message="本地后端正在启动。",
                details={"library_count": len(snapshot.configuration.libraries)},
            )
            future = self._executor.submit(self._start_backend_worker)
            self._backend_future = future
            return future

    def bootstrap(self) -> JsonObject:
        """Return non-secret state needed to render all four pages."""

        snapshot, configuration_error = self._safe_configuration()
        models = self._safe_models()
        with self._lock:
            state = self._backend_state
            backend_error = _copy_object(self._backend_error)
            organize = {
                "proposals": [_copy_object(item) for item in self._organize.proposals],
                "aliases": [_copy_object(item) for item in self._organize.aliases],
                "undo_available": self._organize.undo_available,
                "loading": self._organize.loading,
                "updated_at": self._organize.updated_at,
                "error": _copy_object(self._organize.error),
            }
        libraries = _libraries_view(snapshot)
        return {
            "product": {
                "name": "Zvec 图片库 Preview",
                "version": _package_version(),
                "platform": "Windows x64",
                "preview": True,
            },
            "service": {
                "status": state,
                "backend_ready": state == "ready",
                "credentials_configured": self._safe_has_secret(),
                "error": backend_error or configuration_error,
            },
            "libraries": libraries,
            "models": _models_view(models),
            "organize": organize,
        }

    def settings(self) -> JsonObject:
        snapshot, configuration_error = self._safe_configuration()
        models = self._safe_models()
        return {
            "libraries": _libraries_view(snapshot),
            "results_directory": (
                str(snapshot.configuration.results_directory)
                if snapshot is not None
                else None
            ),
            "models": _models_view(models),
            "credentials": {
                "configured": self._safe_has_secret(),
                "persistent": bool(self._credentials.persistent),
                # A stored key is deliberately never returned or masked.
            },
            "config_path": str(self._configuration.path),
            "model_config_path": str(self._models.path),
            "configuration_error": configuration_error,
        }

    def latest_results(self, *, page: int = 1, page_size: int = 15) -> JsonObject:
        catalog = self._catalog()
        try:
            result_page = catalog.load_latest(page=page, page_size=page_size)
        except ResultCatalogError as exc:
            return _empty_search_response(
                status="empty",
                query={},
                message=str(exc),
            )
        return self._page_response(
            result_page,
            operation_id="latest",
            query={},
            status=result_page.status,
            elapsed_ms=None,
        )

    def submit_search(self, payload: Mapping[str, Any]) -> JsonObject:
        service = self._ready_search_service()
        request_payload = dict(payload)
        query_image_id = request_payload.pop("query_image_id", None)
        if query_image_id is not None:
            if request_payload.get("image_path") is not None:
                raise FacadeError(
                    "invalid_search",
                    "query_image_id 与 image_path 不能同时提供。",
                )
            try:
                request_payload["image_path"] = str(
                    self._registry.resolve(str(query_image_id))
                )
            except ImageRegistryError as exc:
                raise FacadeError(
                    "query_image_not_found",
                    str(exc),
                    status=404,
                ) from exc
        request, query, page_size = _search_request(request_payload)
        if query_image_id is not None:
            query["query_image_id"] = str(query_image_id)
        try:
            submission = service.submit(request)
        except Exception as exc:
            raise _facade_error(exc, code="search_submit_failed") from exc
        operation = _SearchOperation(
            operation_id=submission.job_id,
            query={**query, "page_size": page_size},
            submitted_at=time.time(),
            submission=submission,
        )
        with self._lock:
            self._searches[operation.operation_id] = operation
            self._trim_operations_locked()
            operation.future = self._executor.submit(
                self._wait_search_worker, operation.operation_id
            )
        self._record_search_operation(operation)
        return self.search(operation.operation_id, page=1, page_size=page_size)

    def search(
        self,
        operation_id: str,
        *,
        page: int = 1,
        page_size: int = 15,
    ) -> JsonObject:
        normalized = _operation_id(operation_id)
        with self._lock:
            operation = self._searches.get(normalized)
            if operation is None:
                raise FacadeError(
                    "search_not_found",
                    "搜索任务不存在或已过期。",
                    status=404,
                )
            status = operation.status
            outcome = operation.outcome
            error = _copy_object(operation.error)
            query = _copy_object(operation.query) or {}
            elapsed_ms = _elapsed_ms(operation.submitted_at, operation.finished_at)
        if outcome is None or status not in _SUCCESS_STATUSES:
            response = _empty_search_response(
                status=status,
                query=query,
                operation_id=normalized,
                elapsed_ms=elapsed_ms,
            )
            if error is not None:
                response["error"] = error
            return response
        if outcome.manifest_path is None:
            return _empty_search_response(
                status=status,
                query=query,
                operation_id=normalized,
                elapsed_ms=elapsed_ms,
            )
        try:
            result_page = self._catalog().load_manifest(
                outcome.manifest_path,
                page=page,
                page_size=page_size,
            )
        except ResultCatalogError as exc:
            raise FacadeError(
                "result_manifest_failed",
                str(exc),
                status=500,
            ) from exc
        return self._page_response(
            result_page,
            operation_id=normalized,
            query=query,
            status=status,
            elapsed_ms=elapsed_ms,
        )

    def cancel_search(self, operation_id: str) -> JsonObject:
        normalized = _operation_id(operation_id)
        service = self._ready_search_service()
        with self._lock:
            operation = self._searches.get(normalized)
            if operation is None:
                raise FacadeError(
                    "search_not_found", "搜索任务不存在或已过期。", status=404
                )
        try:
            job = service.cancel(operation.submission)
        except Exception as exc:
            raise _facade_error(exc, code="search_cancel_failed") from exc
        with self._lock:
            operation.progress = _copy_object(job)
            operation.status = str(job.get("status") or "running").lower()
        self._record_search_operation(operation)
        return self.search(normalized)

    def list_jobs(self) -> JsonObject:
        client = self._ready_client()
        try:
            payload = client.list_jobs(limit=200)
        except Exception as exc:
            raise _facade_error(exc, code="jobs_load_failed") from exc
        raw_jobs = payload.get("jobs")
        jobs = raw_jobs if isinstance(raw_jobs, list) else []
        views = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            command = str(job.get("command") or "")
            if command != "search":
                self._record_job_snapshot(job)
            views.append(self._job_view(job))
        return {"jobs": views}

    def submit_job(self, payload: Mapping[str, Any]) -> JsonObject:
        service = self._ready_task_service()
        request_payload = dict(payload)
        if (
            not isinstance(request_payload.get("library_id"), str)
            or not str(request_payload.get("library_id")).strip()
        ):
            snapshot = self._configuration.load()
            assert snapshot is not None
            request_payload["library_id"] = snapshot.configuration.default_library_id
        if request_payload.get(
            "task_type"
        ) == "organize_batch_accept" and not isinstance(
            request_payload.get("accepted_tags_by_proposal"), Mapping
        ):
            proposal_ids = set(
                _string_tuple(request_payload.get("proposal_ids"), "proposal_ids")
            )
            with self._lock:
                cached = list(self._organize.proposals)
            accepted: dict[str, tuple[str, ...]] = {}
            for proposal in cached:
                proposal_id = str(
                    proposal.get("proposal_id") or proposal.get("id") or ""
                )
                if proposal_id not in proposal_ids:
                    continue
                tags = proposal.get("low_risk_tags") or proposal.get("batch_safe_tags")
                accepted[proposal_id] = _string_tuple(
                    tags, f"accepted_tags_by_proposal.{proposal_id}"
                )
            request_payload["accepted_tags_by_proposal"] = accepted
        task_type, request, library_id = _library_request(request_payload)
        try:
            submission = service.submit(request)
        except Exception as exc:
            raise _facade_error(exc, code="job_submit_failed") from exc
        operation = _TaskOperation(
            operation_id=submission.job_id,
            task_type=task_type,
            library_id=library_id,
            submitted_at=time.time(),
            submission=submission,
        )
        with self._lock:
            self._tasks[operation.operation_id] = operation
            self._trim_operations_locked()
            operation.future = self._executor.submit(
                self._wait_task_worker, operation.operation_id
            )
        self._record_job_snapshot(submission.submitted_job)
        return self.job(operation.operation_id)

    def job(self, job_id: str) -> JsonObject:
        normalized = _operation_id(job_id)
        try:
            job = self._ready_client().get_job(normalized)
        except Exception as exc:
            raise _facade_error(exc, code="job_load_failed") from exc
        self._record_job_snapshot(job)
        return self._job_view(job)

    def cancel_job(self, job_id: str) -> JsonObject:
        normalized = _operation_id(job_id)
        try:
            job = self._ready_client().cancel_job(normalized)
        except Exception as exc:
            raise _facade_error(exc, code="job_cancel_failed") from exc
        self._record_job_snapshot(job)
        return self._job_view(job)

    def assign_models(self, payload: Mapping[str, Any]) -> JsonObject:
        try:
            snapshot = self._models.assign_roles(
                embedding_model=_required_string(payload, "embedding_model"),
                auto_tag_primary_model=_required_string(
                    payload, "auto_tag_primary_model"
                ),
                auto_tag_escalation_model=_required_string(
                    payload, "auto_tag_escalation_model"
                ),
            )
        except Exception as exc:
            raise _facade_error(exc, code="model_settings_failed") from exc
        self._activity_log(
            level="info",
            category="model_configuration",
            event="model_roles_updated",
            message="模型职责配置已更新。",
            details={
                "provider": snapshot.provider,
                "embedding_model": snapshot.embedding_model,
                "auto_tag_primary_model": snapshot.auto_tag_primary_model,
                "auto_tag_escalation_model": snapshot.auto_tag_escalation_model,
            },
        )
        return _models_view(snapshot)

    def replace_model_json(self, payload: Mapping[str, Any]) -> JsonObject:
        try:
            snapshot = self._models.replace_json(
                _required_json_text(payload, "json_text", maximum_bytes=1024 * 1024)
            )
        except Exception as exc:
            raise _facade_error(exc, code="model_settings_failed") from exc
        model_ids = {
            choice.model_id
            for choices in (
                snapshot.embedding_choices,
                snapshot.auto_tag_primary_choices,
                snapshot.auto_tag_escalation_choices,
            )
            for choice in choices
        }
        self._activity_log(
            level="info",
            category="model_configuration",
            event="model_catalog_replaced",
            message="模型配置文件已验证并更新。",
            details={
                "provider": snapshot.provider,
                "model_count": len(model_ids),
                "embedding_model": snapshot.embedding_model,
                "auto_tag_primary_model": snapshot.auto_tag_primary_model,
                "auto_tag_escalation_model": snapshot.auto_tag_escalation_model,
            },
        )
        return _models_view(snapshot)

    def save_credentials(self, payload: Mapping[str, Any]) -> JsonObject:
        secret = _required_string(payload, "api_key", maximum=4096)
        # Register before any persistence or backend call: even an exception
        # carrying the just-entered credential must be safe to log.
        with suppress(Exception):
            self._activity.add_redactions(secret)
        try:
            self._credentials.save_secret(secret)
            with self._lock:
                ready = self._backend_state == "ready"
            if ready:
                self._ready_client().configure_credentials(secret)
        except Exception as exc:
            self._activity_log(
                level="error",
                category="credentials",
                event="credentials_save_failed",
                message="阿里云凭据保存或应用失败。",
                details={
                    "persistent": bool(self._credentials.persistent),
                    "error_type": exc.__class__.__name__,
                },
            )
            raise _facade_error(exc, code="credential_save_failed") from exc
        self._activity_log(
            level="info",
            category="credentials",
            event="credentials_saved",
            message="阿里云凭据已安全保存。",
            details={
                "persistent": bool(self._credentials.persistent),
                "applied_to_running_backend": ready,
            },
        )
        return {
            "configured": True,
            "persistent": bool(self._credentials.persistent),
        }

    def delete_credentials(self) -> JsonObject:
        try:
            self._credentials.delete_secret()
        except Exception as exc:
            raise _facade_error(exc, code="credential_delete_failed") from exc
        # The backend API intentionally has no secret-read or secret-delete route.
        # A running session therefore keeps its in-memory copy until an idle restart.
        with self._lock:
            restart_required = self._backend_state == "ready"
        self._activity_log(
            level="info",
            category="credentials",
            event="credentials_deleted",
            message="已删除持久化的阿里云凭据。",
            details={
                "persistent": bool(self._credentials.persistent),
                "restart_required": restart_required,
            },
        )
        return {
            "configured": False,
            "persistent": bool(self._credentials.persistent),
            "restart_required": restart_required,
        }

    def add_library(self, payload: Mapping[str, Any]) -> JsonObject:
        unknown = sorted(set(payload) - _LIBRARY_CREATE_FIELDS)
        if unknown:
            raise FacadeError(
                "invalid_request",
                "新增图库包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        image_root = _required_string(payload, "image_root", maximum=32_768)
        name = _optional_string(payload.get("name"), maximum=128)
        workspace = _optional_string(payload.get("workspace_directory"), maximum=32_768)
        results_directory = _optional_string(
            payload.get("results_directory"), maximum=32_768
        )
        enabled = (
            _boolean(payload.get("enabled"), "enabled")
            if "enabled" in payload
            else True
        )
        existing_ids: set[str] = set()
        try:
            snapshot = self._configuration.load(optional=True)
            if snapshot is None:
                if not enabled:
                    raise FacadeError(
                        "invalid_request", "首个图库必须启用并作为默认图库。"
                    )
                updated = self._configuration.create_initial(
                    image_root,
                    name=name,
                    workspace_directory=workspace,
                    results_directory=results_directory,
                )
            else:
                existing_ids = {
                    library.library_id for library in snapshot.configuration.libraries
                }
                updated = self._configuration.add_library(
                    image_root,
                    name=name,
                    workspace_directory=workspace,
                    enabled=enabled,
                )
                if results_directory is not None:
                    # Results are global.  Updating through the new stable
                    # library ID keeps the mutation atomic and reuses the same
                    # validation as the edit surface.
                    new_library_id = updated.configuration.libraries[-1].library_id
                    updated = self._configuration.update_library(
                        new_library_id,
                        results_directory=results_directory,
                    )
        except FacadeError:
            raise
        except Exception as exc:
            raise _facade_error(exc, code="library_save_failed") from exc
        self._invalidate_catalog()
        restart_required, restart = self._configuration_restart_state()
        if not restart_required:
            # With no live child process the new configuration can be used by
            # the normal asynchronous startup without interrupting user work.
            self.start_backend_async()
        created = next(
            (
                library
                for library in updated.configuration.libraries
                if library.library_id not in existing_ids
            ),
            updated.configuration.by_id[updated.configuration.default_library_id],
        )
        self._activity_log(
            level="info",
            category="library_configuration",
            event="library_added",
            message="图库已添加。",
            library_id=created.library_id,
            library_name=created.name,
            details={
                "enabled": created.enabled,
                "is_default": (
                    created.library_id == updated.configuration.default_library_id
                ),
                "configured_fields": sorted(payload),
                "restart_required": restart_required,
            },
        )
        return {
            "libraries": _libraries_view(updated),
            "results_directory": str(updated.configuration.results_directory),
            "restart_required": restart_required,
            "restart": restart,
        }

    def set_library_enabled(self, library_id: str, enabled: bool) -> JsonObject:
        if not isinstance(enabled, bool):
            raise FacadeError("invalid_request", "enabled 必须是布尔值。")
        try:
            updated = self._configuration.set_library_enabled(library_id, enabled)
        except Exception as exc:
            raise _facade_error(exc, code="library_save_failed") from exc
        self._invalidate_catalog()
        library = updated.configuration.by_id[library_id]
        self._activity_log(
            level="info",
            category="library_configuration",
            event="library_enabled_changed",
            message="图库启用状态已更新。",
            library_id=library.library_id,
            library_name=library.name,
            details={"enabled": enabled, "restart_required": True},
        )
        return {"libraries": _libraries_view(updated), "restart_required": True}

    def set_default_library(self, library_id: str) -> JsonObject:
        try:
            updated = self._configuration.set_default_library(library_id)
        except Exception as exc:
            raise _facade_error(exc, code="library_save_failed") from exc
        self._invalidate_catalog()
        library = updated.configuration.by_id[library_id]
        self._activity_log(
            level="info",
            category="library_configuration",
            event="default_library_changed",
            message="默认图库已更新。",
            library_id=library.library_id,
            library_name=library.name,
            details={"restart_required": True},
        )
        return {"libraries": _libraries_view(updated), "restart_required": True}

    def update_library(self, library_id: str, payload: Mapping[str, Any]) -> JsonObject:
        """Apply a partial library/settings update without restarting jobs."""

        unknown = sorted(set(payload) - _LIBRARY_UPDATE_FIELDS)
        if unknown:
            raise FacadeError(
                "invalid_request",
                "图库设置包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        if not payload:
            raise FacadeError("invalid_request", "请至少修改一项图库设置。")

        name = (
            _required_string(payload, "name", maximum=128)
            if "name" in payload
            else None
        )
        image_root = (
            _required_string(payload, "image_root", maximum=32_768)
            if "image_root" in payload
            else None
        )
        workspace_directory = (
            _required_string(payload, "workspace_directory", maximum=32_768)
            if "workspace_directory" in payload
            else None
        )
        results_directory = (
            _required_string(payload, "results_directory", maximum=32_768)
            if "results_directory" in payload
            else None
        )
        enabled = (
            _boolean(payload.get("enabled"), "enabled")
            if "enabled" in payload
            else None
        )
        is_default = (
            _boolean(payload.get("is_default"), "is_default")
            if "is_default" in payload
            else None
        )
        try:
            updated = self._configuration.update_library(
                library_id,
                name=name,
                image_root=image_root,
                workspace_directory=workspace_directory,
                enabled=enabled,
                is_default=is_default,
                results_directory=results_directory,
            )
        except Exception as exc:
            raise _facade_error(exc, code="library_save_failed") from exc
        self._invalidate_catalog()

        restart_required, restart = self._configuration_restart_state()
        library = updated.configuration.by_id[library_id]
        activity_details: JsonObject = {
            "updated_fields": sorted(payload),
            "restart_required": restart_required,
        }
        if "enabled" in payload:
            activity_details["enabled"] = bool(payload["enabled"])
        if "is_default" in payload:
            activity_details["is_default"] = bool(payload["is_default"])
        self._activity_log(
            level="info",
            category="library_configuration",
            event="library_updated",
            message="图库设置已更新。",
            library_id=library.library_id,
            library_name=library.name,
            details=activity_details,
        )
        return {
            "libraries": _libraries_view(updated),
            "results_directory": str(updated.configuration.results_directory),
            "updated_fields": sorted(payload),
            # Keep the original scalar contract while providing enough detail
            # for the UI to explain why an immediate restart may be unsafe.
            "restart_required": restart_required,
            "restart": restart,
        }

    def tag_folders(
        self,
        library_id: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> JsonObject:
        """Read the folder catalog without waiting behind a long backend job."""

        try:
            with self._library_browser(library_id) as browser:
                return cast(
                    JsonObject,
                    browser.list_folders(
                        query=query,
                        offset=offset,
                        limit=limit,
                    ),
                )
        except FacadeError:
            raise
        except (ConfigurationError, OSError, ValueError) as exc:
            raise FacadeError(
                "library_browser_unavailable",
                "The selected library cannot be browsed yet. "
                "Build or repair its index first.",
                status=409,
            ) from exc

    def folder_images(
        self,
        library_id: str,
        *,
        folder_key: str,
        page: int = 1,
        page_size: int = 100,
        include_subfolders: bool = False,
    ) -> JsonObject:
        """Return one safe thumbnail page for an opaque, library-bound folder."""

        offset = (page - 1) * page_size
        try:
            with self._library_browser(library_id) as browser:
                payload = browser.folder_images(
                    folder_key,
                    include_subfolders=include_subfolders,
                    offset=offset,
                    limit=page_size,
                )
                items: list[JsonObject] = []
                for raw in payload.get("items", []):
                    if not isinstance(raw, Mapping):
                        continue
                    item = dict(raw)
                    item["library_id"] = library_id
                    # Keep stale metadata visible so the user can identify it;
                    # the path-sanitising view marks an unresolved image unavailable.
                    with suppress(ConfigurationError, OSError, ValueError):
                        item["source_path"] = str(
                            browser.resolve_image_path(str(item.get("doc_id") or ""))
                        )
                    items.append(self._folder_image_view(item))
        except FacadeError:
            raise
        except ValueError as exc:
            raise FacadeError(
                "invalid_folder_key",
                "The selected folder is invalid or belongs to another library.",
                status=400,
            ) from exc
        except (ConfigurationError, OSError) as exc:
            raise FacadeError(
                "library_browser_unavailable",
                "The selected library cannot be browsed yet. "
                "Build or repair its index first.",
                status=409,
            ) from exc
        total = int(payload.get("total") or 0)
        return {
            **{
                key: value
                for key, value in payload.items()
                if key not in {"items", "offset", "limit"}
            },
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_items": total,
            "has_more": offset + len(items) < total,
        }

    def preview_folder_delete(
        self,
        library_id: str,
        *,
        folder_key: str,
        include_subfolders: bool = True,
    ) -> JsonObject:
        """Return the durable preview required before a destructive commit."""

        service = self._ready_task_service()
        try:
            submission = service.preview_folder_delete(
                FolderDeletePreviewRequest(
                    library_id=library_id,
                    folder_key=folder_key,
                    include_subfolders=include_subfolders,
                )
            )
            outcome = service.wait(
                submission,
                timeout=5 * 60,
                poll_interval=0.1,
            )
        except Exception as exc:
            raise _facade_error(exc, code="folder_delete_preview_failed") from exc
        self._record_job_snapshot(outcome.job)
        if not outcome.successful or outcome.result is None:
            error = outcome.error or {}
            raise FacadeError(
                str(error.get("code") or "folder_delete_preview_failed"),
                str(error.get("message") or "无法生成安全文件夹清理预览。"),
                status=409,
                details=(
                    error.get("details")
                    if isinstance(error.get("details"), Mapping)
                    else None
                ),
            )
        preview = cast(JsonObject, dict(outcome.result))
        self._activity_log(
            level="warning" if preview.get("blocked") else "info",
            category="folder_cleanup",
            event="folder_delete_previewed",
            source="facade",
            message=(
                "文件夹清理预览发现安全阻断项。"
                if preview.get("blocked")
                else "文件夹清理预览已生成，等待明确确认。"
            ),
            library_id=library_id,
            library_name=self._library_name(library_id) or None,
            job_id=outcome.submission.job_id,
            operation_id=str(preview.get("operation_id") or "") or None,
            details={
                "image_count": int(preview.get("image_count") or 0),
                "file_count": int(preview.get("file_count") or 0),
                "size_bytes": int(preview.get("size_bytes") or 0),
                "blocked": bool(preview.get("blocked")),
                "protected_count": int(preview.get("protected_count") or 0),
                "changed_count": int(preview.get("changed_count") or 0),
                "missing_count": int(preview.get("missing_count") or 0),
            },
        )
        return preview

    def commit_folder_delete(
        self,
        library_id: str,
        *,
        operation_id: str,
        confirmation_token: str,
        confirm: bool,
    ) -> JsonObject:
        """Queue one confirmed commit and return the normal task wrapper."""

        job = self.submit_job(
            {
                "task_type": "folder_delete_commit",
                "library_id": library_id,
                "operation_id": operation_id,
                "confirmation_token": confirmation_token,
                "confirm": confirm,
            }
        )
        return {"job": job}

    def _library_browser(self, library_id: str) -> LibraryBrowser:
        normalized = str(library_id).strip()
        if not normalized:
            raise FacadeError(
                "invalid_library", "A library must be selected.", status=400
            )
        try:
            snapshot = self._configuration.load()
            assert snapshot is not None
            library = snapshot.configuration.by_id[normalized]
        except KeyError as exc:
            raise FacadeError(
                "library_not_found", "The selected library does not exist.", status=404
            ) from exc
        except Exception as exc:
            raise _facade_error(exc, code="configuration_unavailable") from exc
        state_path = ServiceConfig(workspace=library.workspace_directory).state_path
        return LibraryBrowser(library_id=normalized, state_path=state_path)

    def _configuration_restart_state(self) -> tuple[bool, JsonObject]:
        """Describe whether a saved configuration needs a process restart."""

        with self._lock:
            backend_status = self._backend_state
        backend_running = backend_status in {"starting", "ready"} or bool(
            self._host.is_running
        )
        active_jobs = self.has_active_jobs() if backend_status == "ready" else False
        # A degraded facade does not automatically retry startup after config
        # changes. Surface recovery even when the failed child already exited.
        restart_required = backend_running or backend_status == "degraded"
        restart_reason = (
            "active_jobs"
            if active_jobs
            else "backend_recovery"
            if backend_status == "degraded"
            else "configuration_changed"
            if restart_required
            else None
        )
        return restart_required, {
            "required": restart_required,
            "backend_status": backend_status,
            "active_jobs": active_jobs,
            "can_restart_now": not active_jobs,
            "reason": restart_reason,
        }

    def has_active_jobs(self) -> bool:
        with self._lock:
            if self._backend_state != "ready":
                return False
            if any(
                operation.status not in _TERMINAL_STATUSES
                for operation in self._searches.values()
            ) or any(
                operation.status not in _TERMINAL_STATUSES
                for operation in self._tasks.values()
            ):
                return True
        try:
            future = self._executor.submit(
                self._ready_client().list_jobs,
                active=True,
                limit=1,
            )
            payload = future.result(timeout=1.0)
        except FutureTimeout:
            return True
        except Exception:
            # If idleness cannot be established, do not claim shutdown is safe.
            return True
        jobs = payload.get("jobs")
        return isinstance(jobs, list) and bool(jobs)

    def close(self, *, force: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
        self._activity_log(
            level="warning" if force else "info",
            category="backend",
            event="backend_stop_requested",
            message=("正在强制停止本地后端。" if force else "正在安全停止本地后端。"),
            details={"force": force},
        )
        try:
            self._host.stop(force=force)
        except BackendBusyError:
            self._activity_log(
                level="warning",
                category="backend",
                event="backend_stop_deferred",
                message="后端仍有活动任务，已保留运行状态。",
            )
            raise
        except Exception as exc:
            self._activity_log(
                level="error",
                category="backend",
                event="backend_stop_failed",
                message="本地后端停止失败。",
                details={"error_type": exc.__class__.__name__},
            )
            raise
        finally:
            with self._lock:
                if force or not self._host.is_running:
                    self._closed = True
                    self._backend_state = "stopped"
            if self._closed:
                self._activity_log(
                    level="info",
                    category="backend",
                    event="backend_stopped",
                    message="本地后端已停止。",
                )
                self._activity.flush(timeout=1.0)
                if self._owns_activity:
                    self._activity.close(timeout=2.0)
                self._registry.close()
                self._invalidate_catalog()
                if self._owns_executor:
                    self._executor.shutdown(wait=False, cancel_futures=True)

    def _start_backend_worker(self) -> None:
        try:
            runtime = self._host.start()
            secret = self._credentials.read_secret()
            if secret:
                self._host.client.configure_credentials(secret)
            search_service = SearchService(self._host.client, runtime.query_root)
            task_service = LibraryTaskService(self._host.client)
        except Exception as exc:
            with self._lock:
                self._backend_state = "degraded"
                self._backend_error = _error_payload(exc, "backend_start_failed")
                self._backend_runtime = None
                self._search_service = None
                self._task_service = None
            self._activity_log(
                level="error",
                category="backend",
                event="backend_start_failed",
                message="本地后端启动失败。",
                details={
                    "error_code": str(getattr(exc, "code", "backend_start_failed")),
                    "error_type": exc.__class__.__name__,
                },
            )
            return
        with self._lock:
            self._backend_runtime = runtime
            self._search_service = search_service
            self._task_service = task_service
            self._backend_state = "ready"
            self._backend_error = None
        self._activity_log(
            level="info",
            category="backend",
            event="backend_started",
            message="本地后端已就绪。",
            details={"pid": runtime.pid},
        )
        with suppress(Exception):
            health = self._host.client.get_health()
            libraries = health.get("libraries")
            if isinstance(libraries, list):
                for item in libraries:
                    if not isinstance(item, Mapping):
                        continue
                    recovery = item.get("folder_delete_recovery")
                    if not isinstance(recovery, Mapping) or not int(
                        recovery.get("failed") or 0
                    ):
                        continue
                    self._activity_log(
                        level="warning",
                        category="folder_cleanup",
                        event="folder_delete_recovery_needs_attention",
                        source="backend",
                        message="存在未能自动恢复的文件夹清理操作。",
                        library_id=str(item.get("id") or "") or None,
                        library_name=str(item.get("name") or "") or None,
                        details={
                            "recovered": int(recovery.get("recovered") or 0),
                            "failed": int(recovery.get("failed") or 0),
                        },
                    )
        # Reconnecting to an already-running backend seeds the in-memory status
        # cache without replaying up to 500 historical task/search events (and
        # their image-failure rows) into the activity queue.
        self._refresh_activity_jobs(announce=False)
        self._schedule_policy_migrations()
        self._refresh_organize_async()

    def _schedule_policy_migrations(self) -> None:
        """Queue the idempotent, model-free legacy-tag migration per library."""

        try:
            snapshot = self._configuration.load()
            assert snapshot is not None
        except Exception:
            return
        for library in snapshot.configuration.libraries:
            if not library.enabled:
                continue
            try:
                self.submit_job(
                    {
                        "task_type": "auto_tag_policy_migrate",
                        "library_id": library.library_id,
                        "dry_run": False,
                    }
                )
            except Exception:
                # A migration job is repair work, not a startup prerequisite.
                # Its backend error remains available in the task list while
                # search and folder browsing continue to work.
                continue

    def _wait_search_worker(self, operation_id: str) -> None:
        with self._lock:
            operation = self._searches.get(operation_id)
            service = self._search_service
            if operation is None or service is None:
                return
            operation.status = "running"
        self._record_search_operation(operation)

        def progress(job: JsonObject) -> None:
            with self._lock:
                current = self._searches.get(operation_id)
                if current is None:
                    return
                current.progress = _copy_object(job)
                current.status = str(job.get("status") or current.status).lower()
            self._record_search_operation(current)

        try:
            outcome = service.wait(
                operation.submission,
                timeout=24 * 60 * 60,
                poll_interval=0.25,
                on_progress=progress,
            )
        except Exception as exc:
            with self._lock:
                current = self._searches.get(operation_id)
                if current is not None:
                    current.status = "failed"
                    current.error = _error_payload(exc, "search_failed")
                    current.finished_at = time.time()
            if current is not None:
                self._record_search_operation(current)
            return
        with self._lock:
            current = self._searches.get(operation_id)
            if current is not None:
                current.outcome = outcome
                current.status = outcome.status
                current.error = _copy_object(outcome.error)
                current.finished_at = time.time()
        if current is not None:
            self._record_search_operation(current)

    def _wait_task_worker(self, operation_id: str) -> None:
        with self._lock:
            operation = self._tasks.get(operation_id)
            service = self._task_service
            if operation is None or service is None:
                return
            operation.status = "running"
        running_job = dict(operation.submission.submitted_job)
        running_job["status"] = "running"
        running_job["progress"] = {"message": "Running."}
        self._record_job_snapshot(running_job)

        def progress(job: JsonObject) -> None:
            with self._lock:
                current = self._tasks.get(operation_id)
                if current is None:
                    return
                current.progress = _copy_object(job)
                current.status = str(job.get("status") or current.status).lower()
            self._record_job_snapshot(job)

        try:
            outcome = service.wait(
                operation.submission,
                timeout=24 * 60 * 60,
                poll_interval=0.25,
                on_progress=progress,
            )
        except Exception as exc:
            with self._lock:
                current = self._tasks.get(operation_id)
                if current is not None:
                    current.status = "failed"
                    current.error = _error_payload(exc, "job_failed")
                    current.finished_at = time.time()
            failed_job = dict(
                current.progress
                if current is not None and isinstance(current.progress, Mapping)
                else operation.submission.submitted_job
            )
            failed_job.update(
                {
                    "id": operation_id,
                    "command": operation.task_type,
                    "status": "failed",
                    "error": _error_payload(exc, "job_failed"),
                }
            )
            self._record_job_snapshot(failed_job)
            return
        with self._lock:
            current = self._tasks.get(operation_id)
            if current is not None:
                current.outcome = outcome
                current.status = outcome.status
                current.error = _copy_object(outcome.error)
                current.finished_at = time.time()
        self._record_job_snapshot(outcome.job)
        if (
            operation.task_type.startswith("organize_")
            or operation.task_type.startswith("tag_alias_")
            or operation.task_type == "auto_tag_policy_migrate"
        ):
            self._refresh_organize_async()
        if operation.task_type == "search_results_cleanup":
            # A cached latest manifest may have been removed by the cleanup.
            self._invalidate_catalog()

    def _refresh_organize_async(self) -> None:
        with self._lock:
            if self._organize.loading or self._backend_state != "ready":
                return
            self._organize.loading = True
            self._organize.error = None
        self._executor.submit(self._refresh_organize_worker)

    def _refresh_organize_worker(self) -> None:
        try:
            snapshot = self._configuration.load()
            assert snapshot is not None
            library_id = snapshot.configuration.default_library_id
            service = self._ready_task_service()
            pending = service.wait(
                service.list_pending_auto_tags(
                    AutoTagPendingRequest(library_id=library_id, limit=100)
                ),
                timeout=120,
            )
            aliases = service.wait(
                service.list_tag_aliases(TagAliasListRequest(library_id)),
                timeout=120,
            )
            pending_result = pending.result or {}
            alias_result = aliases.result or {}
            raw_proposals = pending_result.get("proposals")
            if not isinstance(raw_proposals, list):
                raw_proposals = pending_result.get("items")
            raw_aliases = alias_result.get("aliases")
            if not isinstance(raw_aliases, list):
                raw_aliases = alias_result.get("groups")
            proposals: list[JsonObject] = []
            for item in raw_proposals if isinstance(raw_proposals, list) else []:
                if not isinstance(item, Mapping):
                    continue
                proposal = self._proposal_view(item)
                proposal.setdefault("library_id", library_id)
                proposals.append(proposal)
            alias_items = [
                _json_safe(item)
                for item in (raw_aliases if isinstance(raw_aliases, list) else [])
                if isinstance(item, Mapping)
            ]
            undo_available = bool(
                pending_result.get("undo_available")
                or alias_result.get("undo_available")
            )
        except Exception as exc:
            with self._lock:
                self._organize.loading = False
                self._organize.error = _error_payload(exc, "organize_load_failed")
            return
        with self._lock:
            self._organize.proposals = proposals
            self._organize.aliases = alias_items
            self._organize.undo_available = undo_available
            self._organize.updated_at = time.time()
            self._organize.loading = False
            self._organize.error = None

    def _page_response(
        self,
        page: SearchResultPage,
        *,
        operation_id: str,
        query: JsonObject,
        status: str,
        elapsed_ms: int | None,
    ) -> JsonObject:
        items: list[JsonObject] = []
        for result in page.items:
            try:
                items.append(self._search_result_view(result))
            except ImageRegistryError:
                # A moved/deleted result should not make the entire gallery fail.
                continue
        return {
            "id": operation_id,
            "status": status,
            "query": query,
            "elapsed_ms": elapsed_ms,
            "items": items,
            "page": page.page,
            "page_size": page.page_size,
            "total_items": page.total_items,
            "total_pages": page.total_pages,
            "has_previous": page.has_previous,
            "has_next": page.has_next,
            "summary": page.summary,
            "source_label": page.source_label,
            "sort_mode": page.sort_mode,
            "ranking_diagnostics": page.ranking_diagnostics or {},
        }

    def _search_result_view(self, result: SearchResult) -> JsonObject:
        metadata = self._registry.register(result.display_path)
        image_url = f"api/image/{metadata.image_id}?variant=preview"
        thumbnail_url = f"api/image/{metadata.image_id}?variant=thumbnail"
        return {
            "id": metadata.image_id,
            "name": result.name,
            "relative_path": result.relative_path,
            "library_id": result.library_id,
            "library_name": result.library_name,
            "rank": result.rank,
            "match_state": result.match_state,
            "rank_source": result.rank_source,
            "raw_score": result.raw_score,
            "normalized_score": result.normalized_score,
            "confidence": result.confidence,
            "ranking_confidence": result.ranking_confidence,
            "image_confidence": result.image_confidence,
            "text_confidence": result.text_confidence,
            "metadata_confidence": result.metadata_confidence,
            "image_rank": result.image_rank,
            "text_rank": result.text_rank,
            "metadata_rank": result.metadata_rank,
            "rank_agreement": result.rank_agreement,
            "tags": list(result.tags),
            "matched_tags": list(result.matched_tags),
            "width": metadata.width,
            "height": metadata.height,
            "size_bytes": metadata.size_bytes,
            "image_url": image_url,
            "thumbnail_url": thumbnail_url,
        }

    def _proposal_view(self, proposal: Mapping[str, Any]) -> JsonObject:
        view = _json_safe(proposal)
        raw_path = _first_path(proposal)
        if raw_path is not None:
            try:
                metadata = self._registry.register(raw_path)
            except ImageRegistryError:
                pass
            else:
                view["image_id"] = metadata.image_id
                view["image_url"] = f"api/image/{metadata.image_id}?variant=preview"
                view["thumbnail_url"] = (
                    f"api/image/{metadata.image_id}?variant=thumbnail"
                )
        for key in _PATH_KEYS:
            view.pop(key, None)
        return view

    def _folder_image_view(self, item: Mapping[str, Any]) -> JsonObject:
        """Expose one indexed image without leaking its absolute host path."""

        view = _json_safe(item)
        raw_path = _first_path(item)
        if raw_path is not None:
            try:
                metadata = self._registry.register(raw_path)
            except ImageRegistryError:
                # A stale entry remains visible and can be selected for a later
                # cleanup operation, but it deliberately has no image URL.
                view["image_available"] = False
            else:
                view["image_available"] = True
                view["image_id"] = metadata.image_id
                view["image_url"] = f"api/image/{metadata.image_id}?variant=preview"
                view["thumbnail_url"] = (
                    f"api/image/{metadata.image_id}?variant=thumbnail"
                )
                view.setdefault("width", metadata.width)
                view.setdefault("height", metadata.height)
                view.setdefault("size_bytes", metadata.size_bytes)
        else:
            view["image_available"] = False
        for key in _PATH_KEYS:
            view.pop(key, None)
        return view

    def _job_view(self, job: Mapping[str, Any]) -> JsonObject:
        progress = job.get("progress")
        progress_map = progress if isinstance(progress, Mapping) else {}
        result = job.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        processed = _first_int(progress_map, "processed", "completed", "current")
        if processed == 0:
            processed = _first_int(result_map, "processed", "processed_count")
        total = _first_int(progress_map, "total", "candidate_count", "items")
        if total == 0:
            total = _first_int(
                result_map, "candidate_count", "total", "total_count", "selected"
            )
        failure_count = _first_int(job, "failure_count", "failed_count", "failed")
        if failure_count == 0:
            failure_count = _first_int(
                progress_map, "failure_count", "failed_count", "failed"
            )
        if failure_count == 0:
            failure_count = _first_int(
                result_map, "failure_count", "failed_count", "failed"
            )
        percent = _progress_percent(progress_map, processed, total)
        command = str(job.get("command") or "")
        library_id = str(
            (job.get("params") or {}).get("library_id", "")
            if isinstance(job.get("params"), Mapping)
            else ""
        )
        errors = _error_images(result_map, self._registry)
        view: JsonObject = {
            "id": str(job.get("id") or ""),
            "task_type": command,
            "type": command,
            "library_id": library_id,
            "library_name": self._library_name(library_id),
            "status": str(job.get("status") or "unknown"),
            "progress_percent": percent,
            "message": str(progress_map.get("message") or ""),
            "processed": processed,
            "total": total,
            "failure_count": failure_count,
            "error_images": errors,
            "created_at": job.get("submitted_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "error": _json_safe(job.get("error")),
        }
        public_result = _public_job_result(result_map)
        if public_result:
            view["result"] = public_result
            if "undo_available" in public_result:
                view["undo_available"] = public_result["undo_available"]
        return view

    def _library_name(self, library_id: str) -> str:
        snapshot, _error = self._safe_configuration()
        if snapshot is None:
            return ""
        for library in snapshot.configuration.libraries:
            if library.library_id == library_id:
                return library.name
        return ""

    def _catalog(self) -> ResultCatalog:
        path = self._configuration.path
        for _attempt in range(3):
            try:
                before = _file_stat_version(path)
            except OSError as exc:
                raise _facade_error(exc, code="catalog_unavailable") from exc
            with self._lock:
                if (
                    self._result_catalog is not None
                    and self._result_catalog_version == before
                ):
                    return self._result_catalog
            try:
                catalog = ResultCatalog.from_config(path)
                after = _file_stat_version(path)
            except Exception as exc:
                raise _facade_error(exc, code="catalog_unavailable") from exc
            if before != after:
                continue
            with self._lock:
                if (
                    self._result_catalog is not None
                    and self._result_catalog_version == after
                ):
                    catalog.clear_manifest_cache()
                    return self._result_catalog
                previous = self._result_catalog
                self._result_catalog = catalog
                self._result_catalog_version = after
            if previous is not None:
                previous.clear_manifest_cache()
            return catalog
        raise FacadeError(
            "catalog_unavailable",
            "配置文件正在变化，请稍后重试。",
            status=409,
        )

    def _invalidate_catalog(self) -> None:
        with self._lock:
            catalog = self._result_catalog
            self._result_catalog = None
            self._result_catalog_version = None
        if catalog is not None:
            catalog.clear_manifest_cache()

    def _ready_client(self) -> Any:
        with self._lock:
            if self._backend_state != "ready":
                raise FacadeError(
                    "backend_not_ready",
                    "后端尚未就绪，请稍后重试。",
                    status=503,
                    details={"status": self._backend_state},
                )
        return self._host.client

    def _ready_search_service(self) -> SearchService:
        with self._lock:
            service = self._search_service
            if self._backend_state != "ready" or service is None:
                raise FacadeError(
                    "backend_not_ready", "后端尚未就绪，请稍后重试。", status=503
                )
            return service

    def _ready_task_service(self) -> LibraryTaskService:
        with self._lock:
            service = self._task_service
            if self._backend_state != "ready" or service is None:
                raise FacadeError(
                    "backend_not_ready", "后端尚未就绪，请稍后重试。", status=503
                )
            return service

    def _safe_models(self) -> ModelSettingsSnapshot | None:
        try:
            return self._models.load(create=True)
        except Exception:
            return None

    def _safe_configuration(
        self,
    ) -> tuple[ConfigurationSnapshot | None, JsonObject | None]:
        try:
            return self._configuration.load(optional=True), None
        except Exception as exc:
            return None, _error_payload(exc, "configuration_unavailable")

    def _safe_has_secret(self) -> bool:
        try:
            return self._credentials.has_secret()
        except Exception:
            return False

    def _trim_operations_locked(self) -> None:
        for operations in (self._searches, self._tasks):
            if len(operations) <= _MAX_OPERATIONS:
                continue
            terminal = [
                (key, value.finished_at or value.submitted_at)
                for key, value in operations.items()
                if value.status in _TERMINAL_STATUSES
            ]
            terminal.sort(key=lambda item: item[1])
            for key, _timestamp in terminal[: len(operations) - _MAX_OPERATIONS]:
                operations.pop(key, None)

    @staticmethod
    def _remember_activity_status_locked(
        statuses: dict[str, str],
        operation_id: str,
        status: str,
    ) -> str | None:
        """Remember one status while bounding long-lived deduplication state."""

        previous = statuses.get(operation_id)
        statuses[operation_id] = status
        if len(statuses) <= _MAX_OPERATIONS * 2:
            return previous
        removable = len(statuses) - _MAX_OPERATIONS
        for candidate, candidate_status in tuple(statuses.items()):
            if removable <= 0:
                break
            if candidate == operation_id or candidate_status not in _TERMINAL_STATUSES:
                continue
            statuses.pop(candidate, None)
            removable -= 1
        return previous

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise FacadeError("preview_closed", "Preview 已关闭。", status=503)


def _search_request(
    payload: Mapping[str, Any],
) -> tuple[SearchRequest, JsonObject, int]:
    text = _optional_string(payload.get("text"), maximum=4096)
    image_path = _optional_string(payload.get("image_path"), maximum=32_768)
    raw_mode = str(payload.get("mode") or payload.get("search_mode") or "semantic")
    raw_mode = raw_mode.strip().lower()
    mode: SearchMode
    if raw_mode in {"tag", "tags"}:
        mode = "tags"
    elif raw_mode in {"semantic", "text", "image", "combined", "mix"}:
        mode = "semantic"
    else:
        raise FacadeError("invalid_search", "搜索模式无效。")
    raw_tag_mode = str(payload.get("tag_mode") or "all").strip().lower()
    if raw_tag_mode not in {"all", "any"}:
        raise FacadeError("invalid_search", "tag_mode must be 'all' or 'any'.")
    tag_mode: TagMode = "any" if raw_tag_mode == "any" else "all"
    library_ids = _string_tuple(payload.get("library_ids"), "library_ids")
    tags = _string_tuple(payload.get("tags"), "tags")
    top_k = _positive_int(payload.get("top_k", 15), "top_k")
    page_size = _bounded_int(payload.get("page_size", 15), "page_size", 1, 100)
    candidate_k = _positive_int(
        payload.get("candidate_k", max(50, top_k + max(25, top_k // 2))),
        "candidate_k",
    )
    if candidate_k < top_k:
        raise FacadeError("invalid_request", "candidate_k 不能小于 top_k。")
    raw_sort_mode = payload.get("sort_mode", "confidence")
    if not isinstance(raw_sort_mode, str) or raw_sort_mode not in {
        "relevance",
        "confidence",
        "diverse",
        "legacy",
    }:
        raise FacadeError(
            "invalid_search",
            "sort_mode must be relevance, confidence, diverse, or legacy.",
        )
    sort_mode = cast(SearchSortMode, raw_sort_mode)
    request = SearchRequest(
        text=text,
        image_path=image_path,
        library_ids=library_ids,
        top_k=top_k,
        candidate_k=candidate_k,
        tags=tags,
        tag_mode=tag_mode,
        image_weight=_finite_float(payload.get("image_weight", 0.5), "image_weight"),
        text_weight=_finite_float(payload.get("text_weight", 0.5), "text_weight"),
        include_self=bool(payload.get("include_self", False)),
        show_low_confidence=bool(payload.get("show_low_confidence", False)),
        diversify_results=bool(payload.get("diversify_results", True)),
        search_mode=mode,
        sort_mode=sort_mode,
    )
    query: JsonObject = {
        "text": text,
        "mode": mode,
        "library_ids": list(library_ids),
        "top_k": top_k,
        "sort_mode": sort_mode,
    }
    return request, query, page_size


def _library_request(
    payload: Mapping[str, Any],
) -> tuple[str, LibraryRequest, str]:
    task_type = _required_string(payload, "task_type", maximum=128).lower()
    unsupported_execution_fields = sorted(
        field for field in ("concurrency", "skip_errors") if field in payload
    )
    if unsupported_execution_fields:
        raise FacadeError(
            "unsupported_task_options",
            "并发与单图失败处理由后端运行配置统一控制，不能按单个任务覆盖。",
            details={
                "task_type": task_type,
                "fields": unsupported_execution_fields,
                "configuration": [
                    "scan_concurrency",
                    "embedding_concurrency",
                    "auto_tag_concurrency",
                ],
            },
        )
    library_id = _required_string(payload, "library_id", maximum=128)
    folder = _optional_string(payload.get("folder"), maximum=32_768)
    recursive = _boolean(payload.get("recursive", True), "recursive")
    verify_hash = _boolean(payload.get("verify_hash", False), "verify_hash")
    tags_value = payload.get("tags")
    tags = None if tags_value is None else _string_tuple(tags_value, "tags")

    request: LibraryRequest
    if task_type == "index":
        request = IndexRequest(library_id, folder, recursive, verify_hash, tags)
    elif task_type == "sync":
        request = SyncRequest(
            library_id,
            folder,
            recursive,
            verify_hash,
            _boolean(payload.get("dry_run", False), "dry_run"),
            _boolean(payload.get("allow_scope_change", False), "allow_scope_change"),
        )
    elif task_type == "index_and_auto_tag":
        request = IndexAndAutoTagRequest(
            library_id=library_id,
            folder=folder,
            recursive=recursive,
            verify_hash=verify_hash,
            tags=tags,
            model=_optional_string(payload.get("model"), maximum=256),
            max_images=_bounded_int(
                payload.get("max_images", 300), "max_images", 1, 10_000
            ),
            max_budget_cny=_optional_float(
                payload.get("max_budget_cny"), "max_budget_cny"
            ),
            external_processing_confirmed=_boolean(
                payload.get("external_processing_confirmed", False),
                "external_processing_confirmed",
            ),
        )
    elif task_type == "auto_tag_estimate":
        request = AutoTagEstimateRequest(
            library_id=library_id,
            scope=_choice(
                payload.get("scope"),
                "scope",
                supported=_AUTO_TAG_SCOPE_OPTIONS,
                default="untagged",
            ),
            model=_optional_string(payload.get("model"), maximum=256),
            max_images=_bounded_int(
                payload.get("max_images", 300), "max_images", 1, 10_000
            ),
            max_budget_cny=_optional_float(
                payload.get("max_budget_cny"), "max_budget_cny"
            ),
        )
    elif task_type == "auto_tag":
        scope = _choice(
            payload.get("scope"),
            "scope",
            supported=_AUTO_TAG_SCOPE_OPTIONS,
            default="untagged",
        )
        if scope == "all" and not _boolean(
            payload.get("all_scope_confirmed", False), "all_scope_confirmed"
        ):
            raise FacadeError(
                "confirmation_required",
                "全部图片重新处理需要额外确认。",
                status=400,
            )
        request = AutoTagRunRequest(
            library_id=library_id,
            scope=scope,
            model=_optional_string(payload.get("model"), maximum=256),
            max_images=_bounded_int(
                payload.get("max_images", 300), "max_images", 1, 10_000
            ),
            max_budget_cny=_optional_float(
                payload.get("max_budget_cny"), "max_budget_cny"
            ),
            external_processing_confirmed=_boolean(
                payload.get("external_processing_confirmed", False),
                "external_processing_confirmed",
            ),
        )
    elif task_type == "auto_tag_policy_migrate":
        request = AutoTagPolicyMigrateRequest(
            library_id,
            dry_run=_boolean(payload.get("dry_run", False), "dry_run"),
        )
    elif task_type == "stats":
        request = StatsRequest(library_id)
    elif task_type == "manual_tag_batch":
        raw_selection = payload.get("selection")
        if not isinstance(raw_selection, Mapping):
            raise FacadeError("invalid_request", "selection must be an object.")
        selection_mode = _choice(
            raw_selection.get("mode"),
            "selection.mode",
            supported=("selected", "folder"),
            default="selected",
        )
        selection = ManualTagSelection(
            mode=cast(Literal["selected", "folder"], selection_mode),
            doc_ids=_string_tuple(raw_selection.get("doc_ids"), "selection.doc_ids"),
            folder_key=_optional_string(
                raw_selection.get("folder_key"), maximum=16_384
            ),
            include_subfolders=_boolean(
                raw_selection.get("include_subfolders", False),
                "selection.include_subfolders",
            ),
            excluded_doc_ids=_string_tuple(
                raw_selection.get("excluded_doc_ids"),
                "selection.excluded_doc_ids",
            ),
        )
        request = ManualTagBatchRequest(
            library_id=library_id,
            selection=selection,
            operation=_choice(
                payload.get("operation"),
                "operation",
                supported=_MANUAL_TAG_OPERATION_OPTIONS,
                default="add",
            ),
            tags=_string_tuple(payload.get("tags"), "tags"),
        )
    elif task_type == "manual_tag_undo":
        request = ManualTagUndoRequest(library_id)
    elif task_type == "folder_delete_preview":
        request = FolderDeletePreviewRequest(
            library_id=library_id,
            folder_key=_required_string(payload, "folder_key", maximum=8_192),
            include_subfolders=_boolean(
                payload.get("include_subfolders", True), "include_subfolders"
            ),
        )
    elif task_type == "folder_delete_commit":
        request = FolderDeleteCommitRequest(
            library_id=library_id,
            operation_id=_required_string(payload, "operation_id", maximum=32),
            confirmation_token=_required_string(
                payload, "confirmation_token", maximum=512
            ),
            confirm=_boolean(payload.get("confirm", False), "confirm"),
        )
    elif task_type == "search_results_cleanup":
        request = SearchResultsCleanupRequest(
            library_id=library_id,
            keep_latest=_bounded_int(
                payload.get("keep_latest", 3), "keep_latest", 1, 100
            ),
            dry_run=_boolean(payload.get("dry_run", False), "dry_run"),
        )
    elif task_type == "organize_review":
        decisions_value = payload.get("decisions")
        if isinstance(decisions_value, Sequence) and not isinstance(
            decisions_value, (str, bytes)
        ):
            decisions = tuple(
                _review_decision(item)
                for item in decisions_value
                if isinstance(item, Mapping)
            )
            request = AutoTagReviewRequest(library_id, decisions)
        else:
            decision = _review_decision(payload)
            if decision.confirmed_identity_tags:
                identity_decision = decision.decision
                if identity_decision not in _IDENTITY_REVIEW_ACTION_OPTIONS:
                    raise FacadeError(
                        "invalid_request",
                        "确认身份标签时 decision 只能是 accept 或 edit。",
                    )
                request = IdentityConfirmationRequest(
                    library_id=library_id,
                    proposal_id=decision.proposal_id,
                    accepted_tags=decision.accepted_tags,
                    confirmed_identity_tags=decision.confirmed_identity_tags,
                    decision=cast(IdentityReviewAction, identity_decision),
                )
            else:
                request = AutoTagReviewRequest(library_id, (decision,))
    elif task_type == "organize_identity_confirm":
        request = IdentityConfirmationRequest(
            library_id=library_id,
            proposal_id=_required_string(payload, "proposal_id", maximum=1024),
            accepted_tags=_string_tuple(payload.get("accepted_tags"), "accepted_tags"),
            confirmed_identity_tags=_string_tuple(
                payload.get("confirmed_identity_tags"), "confirmed_identity_tags"
            ),
            decision=_choice(
                payload.get("decision"),
                "decision",
                supported=_IDENTITY_REVIEW_ACTION_OPTIONS,
                default="accept",
            ),
        )
    elif task_type == "organize_batch_accept":
        proposal_ids = _string_tuple(payload.get("proposal_ids"), "proposal_ids")
        raw_mapping = payload.get("accepted_tags_by_proposal")
        if not isinstance(raw_mapping, Mapping):
            raise FacadeError(
                "invalid_request", "accepted_tags_by_proposal 必须是对象。"
            )
        accepted = {
            str(key): _string_tuple(value, f"accepted_tags_by_proposal.{key}")
            for key, value in raw_mapping.items()
        }
        raw_mode = payload.get("acceptance_mode")
        legacy_exclude = _boolean(
            payload.get("exclude_identity_tags", True), "exclude_identity_tags"
        )
        acceptance_mode = cast(
            BatchAcceptanceMode,
            _choice(
                raw_mode,
                "acceptance_mode",
                supported=_BATCH_ACCEPTANCE_MODE_OPTIONS,
                default="low_risk_only" if legacy_exclude else "recommended",
            ),
        )
        expected_exclusion = acceptance_mode == "low_risk_only"
        if "exclude_identity_tags" in payload and legacy_exclude != expected_exclusion:
            raise FacadeError(
                "invalid_request",
                "exclude_identity_tags 与 acceptance_mode 不一致。",
            )
        request = LowRiskBatchReviewRequest(
            library_id,
            proposal_ids,
            accepted,
            acceptance_mode=acceptance_mode,
            batch_confirmation=_boolean(
                payload.get("batch_confirmation", False), "batch_confirmation"
            ),
        )
    elif task_type == "organize_undo":
        request = AutoTagUndoRequest(library_id)
    elif task_type == "folder_tag_backfill":
        request = FolderTagBackfillRequest(library_id, folder, recursive, verify_hash)
    elif task_type == "metadata_backfill":
        request = MetadataBackfillRequest(
            library_id,
            _bounded_int(payload.get("max_images", 300), "max_images", 1, 1_000_000),
        )
    elif task_type == "tag_alias_upsert":
        request = TagAliasUpsertRequest(
            library_id,
            _required_string(payload, "canonical_name", maximum=256),
            _string_tuple(payload.get("aliases"), "aliases"),
        )
    elif task_type == "tag_alias_delete":
        request = TagAliasDeleteRequest(
            library_id,
            _required_string(payload, "canonical_name", maximum=256),
        )
    elif task_type == "organize_refresh":
        filters = AutoTagReviewFilters(
            latest_index_only=_boolean(
                payload.get("latest_index_only", False), "latest_index_only"
            ),
            character=str(payload.get("character") or ""),
            work=str(payload.get("work") or ""),
            action=str(payload.get("action") or ""),
            expression=str(payload.get("expression") or ""),
            review_state=_choice(
                payload.get("review_state"),
                "review_state",
                supported=_REVIEW_STATE_OPTIONS,
                default="",
            ),
        )
        request = AutoTagPendingRequest(
            library_id,
            offset=_bounded_int(payload.get("offset", 0), "offset", 0, 2**63 - 1),
            limit=_bounded_int(payload.get("limit", 100), "limit", 1, 500),
            filters=filters,
        )
    else:
        raise FacadeError(
            "unsupported_task",
            f"Preview 暂不支持任务类型：{task_type}",
        )
    return task_type, request, library_id


def _review_decision(payload: Mapping[str, Any]) -> AutoTagReviewDecision:
    return AutoTagReviewDecision(
        proposal_id=_required_string(payload, "proposal_id", maximum=1024),
        decision=_choice(
            payload.get("decision"),
            "decision",
            supported=_REVIEW_ACTION_OPTIONS,
            default="accept",
        ),
        accepted_tags=_string_tuple(payload.get("accepted_tags"), "accepted_tags"),
        confirmed_identity_tags=_string_tuple(
            payload.get("confirmed_identity_tags"), "confirmed_identity_tags"
        ),
    )


def _libraries_view(
    snapshot: ConfigurationSnapshot | None,
) -> list[JsonObject]:
    if snapshot is None:
        return []
    default_id = snapshot.configuration.default_library_id
    return [
        {
            "id": library.library_id,
            "name": library.name,
            "image_root": str(library.image_root),
            "workspace_directory": str(library.workspace_directory),
            "enabled": library.enabled,
            "is_default": library.library_id == default_id,
        }
        for library in snapshot.configuration.libraries
    ]


def _models_view(snapshot: ModelSettingsSnapshot | None) -> JsonObject:
    if snapshot is None:
        return {
            "embedding_model": "",
            "auto_tag_primary_model": "",
            "auto_tag_escalation_model": "",
            "catalog": [],
            "error": {"message": "模型配置不可用。"},
        }
    by_id: dict[str, JsonObject] = {}
    for choice in (
        *snapshot.embedding_choices,
        *snapshot.auto_tag_primary_choices,
        *snapshot.auto_tag_escalation_choices,
    ):
        by_id.setdefault(
            choice.model_id,
            {
                "id": choice.model_id,
                "display_name": choice.display_name,
                "protocol": choice.protocol,
                "roles": list(choice.roles),
            },
        )
    return {
        "provider": snapshot.provider,
        "embedding_model": snapshot.embedding_model,
        "auto_tag_primary_model": snapshot.auto_tag_primary_model,
        "auto_tag_escalation_model": snapshot.auto_tag_escalation_model,
        "catalog": list(by_id.values()),
    }


def _empty_search_response(
    *,
    status: str,
    query: JsonObject,
    operation_id: str = "latest",
    elapsed_ms: int | None = None,
    message: str | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "id": operation_id,
        "status": status,
        "query": query,
        "elapsed_ms": elapsed_ms,
        "items": [],
        "page": 1,
        "page_size": 15,
        "total_items": 0,
        "total_pages": 0,
        "has_previous": False,
        "has_next": False,
    }
    if message:
        payload["message"] = message
    return payload


_PATH_KEYS = frozenset(
    {
        "path",
        "absolute_path",
        "image_path",
        "file_path",
        "original_path",
        "copied_path",
        "display_path",
        "source_path",
        "output_dir",
        "output_directory",
        "manifest_path",
        "query_root",
        "image_root",
        "workspace_directory",
        "results_directory",
    }
)


def _first_path(value: Mapping[str, Any]) -> Path | None:
    for key in _PATH_KEYS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                return candidate
    return None


def _error_images(
    result: Mapping[str, Any], registry: ImageRegistry
) -> list[JsonObject]:
    candidates = result.get("error_images")
    if not isinstance(candidates, list):
        candidates = result.get("failures")
    if not isinstance(candidates, list):
        return []
    output: list[JsonObject] = []
    for item in candidates[:200]:
        if not isinstance(item, Mapping):
            continue
        view: JsonObject = {
            "name": str(item.get("name") or item.get("file_name") or ""),
            "reason": str(item.get("reason") or item.get("error") or ""),
        }
        relative_path = _safe_relative_path(item.get("relative_path"))
        if relative_path:
            view["relative_path"] = relative_path
        path = _first_path(item)
        if path is not None:
            try:
                metadata = registry.register(path)
            except ImageRegistryError:
                pass
            else:
                view.update(
                    {
                        "id": metadata.image_id,
                        "thumbnail_url": (
                            f"api/image/{metadata.image_id}?variant=thumbnail"
                        ),
                        "image_url": (f"api/image/{metadata.image_id}?variant=preview"),
                    }
                )
        output.append(view)
    return output


_PUBLIC_JOB_COUNT_KEYS = frozenset(
    {
        "total",
        "total_count",
        "selected",
        "processed",
        "processed_count",
        "updated",
        "updated_count",
        "unchanged",
        "unchanged_count",
        "failed",
        "failed_count",
        "matched",
        "matched_count",
        "deleted",
        "deleted_count",
        "skipped",
        "skipped_count",
        "kept",
        "kept_count",
        "keep_latest",
        "candidate_count",
        "unique_image_count",
        "cached_count",
        "cache_hits",
        "api_candidate_count",
        "api_request_count",
        "estimated_input_tokens",
        "estimated_output_tokens",
    }
)
_PUBLIC_JOB_TEXT_KEYS = frozenset({"batch_id", "operation", "status"})


def _public_job_result(result: Mapping[str, Any]) -> JsonObject:
    """Expose UI summaries while keeping local paths and snapshots private."""

    output: JsonObject = {}
    for key in _PUBLIC_JOB_COUNT_KEYS:
        raw = result.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            output[key] = max(0, raw)
    for key in _PUBLIC_JOB_TEXT_KEYS:
        raw = result.get(key)
        if isinstance(raw, str) and raw and len(raw) <= 256:
            output[key] = raw
    for key in ("undo_available", "dry_run", "needs_attention", "over_budget"):
        raw = result.get(key)
        if isinstance(raw, bool):
            output[key] = raw
    estimated_cost = result.get("estimated_cost_cny")
    if (
        isinstance(estimated_cost, (int, float))
        and not isinstance(estimated_cost, bool)
        and math.isfinite(float(estimated_cost))
    ):
        output["estimated_cost_cny"] = max(0.0, float(estimated_cost))
    return output


def _progress_percent(progress: Mapping[str, Any], processed: int, total: int) -> float:
    raw = progress.get("percent")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        number = float(raw)
        if math.isfinite(number):
            return max(0.0, min(100.0, number))
    if total > 0:
        return max(0.0, min(100.0, processed * 100.0 / total))
    return 0.0


def _first_int(value: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return max(0, raw)
    return 0


def _elapsed_ms(started: float, finished: float | None) -> int:
    end = time.time() if finished is None else finished
    return max(0, round((end - started) * 1000))


def _file_stat_version(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_mtime_ns, metadata.st_size


def _package_version() -> str:
    try:
        return metadata.version("zvec-image-search")
    except metadata.PackageNotFoundError:
        return "0.4.0"


def _operation_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise FacadeError("invalid_id", "任务 ID 无效。")
    return value.strip()


def _required_string(
    payload: Mapping[str, Any], key: str, *, maximum: int = 4096
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FacadeError("invalid_request", f"{key} 不能为空。")
    normalized = value.strip()
    if len(normalized) > maximum or any(
        character in normalized for character in "\r\n\0"
    ):
        raise FacadeError("invalid_request", f"{key} 无效或过长。")
    return normalized


def _required_json_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    maximum_bytes: int,
) -> str:
    """Accept formatted JSON while retaining a strict bounded request surface."""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FacadeError("invalid_request", f"{key} 不能为空。")
    if "\0" in value or len(value.encode("utf-8")) > maximum_bytes:
        raise FacadeError("invalid_request", f"{key} 无效或过长。")
    return value


def _choice(
    value: Any,
    name: str,
    *,
    supported: Collection[_Choice],
    default: _Choice,
) -> _Choice:
    """Parse one case-insensitive JSON enum and preserve its Literal type."""

    if value is None:
        normalized = default
    elif not isinstance(value, str):
        raise FacadeError("invalid_request", f"{name} 必须是字符串。")
    else:
        text = value.strip().lower()
        normalized = cast(_Choice, text) if text else default
    if normalized not in supported:
        raise FacadeError(
            "invalid_request",
            f"{name} 不受支持。",
            details={"supported": list(supported)},
        )
    return normalized


def _optional_string(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FacadeError("invalid_request", "字段必须是字符串。")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum or any(
        character in normalized for character in "\r\0"
    ):
        raise FacadeError("invalid_request", "字符串字段无效或过长。")
    return normalized


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[Any] = [part for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise FacadeError("invalid_request", f"{name} 必须是字符串数组。")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise FacadeError("invalid_request", f"{name} 只能包含字符串。")
        normalized = item.strip()
        if not normalized:
            continue
        if len(normalized) > 1024:
            raise FacadeError("invalid_request", f"{name} 中的项目过长。")
        folded = normalized.casefold()
        if folded not in seen:
            result.append(normalized)
            seen.add(folded)
    return tuple(result)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise FacadeError("invalid_request", f"{name} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FacadeError("invalid_request", f"{name} 必须是整数。") from exc
    if not minimum <= number <= maximum:
        raise FacadeError(
            "invalid_request", f"{name} 必须在 {minimum} 到 {maximum} 之间。"
        )
    return number


def _positive_int(value: Any, name: str) -> int:
    """Parse a positive count without imposing a product-level result cap."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FacadeError("invalid_request", f"{name} 必须是正整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FacadeError("invalid_request", f"{name} 必须是正整数。") from exc
    if number < 1 or number > _MAX_TECHNICAL_COUNT:
        raise FacadeError("invalid_request", f"{name} 必须是正整数。")
    return number


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FacadeError("invalid_request", f"{name} 必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FacadeError("invalid_request", f"{name} 必须是数字。") from exc
    if not math.isfinite(number) or number < 0:
        raise FacadeError("invalid_request", f"{name} 必须是非负有限数字。")
    return number


def _optional_float(value: Any, name: str) -> float | None:
    if value in {None, ""}:
        return None
    return _finite_float(value, name)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise FacadeError("invalid_request", f"{name} 必须是布尔值。")
    return value


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = value.replace("\\", "/").strip("/")
    if not candidate or ":" in candidate.split("/", 1)[0]:
        return ""
    parts = PurePosixPath(candidate).parts
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    normalized = "/".join(parts)
    return normalized if len(normalized) <= 4_096 else ""


def _activity_task_category(command: str) -> str:
    if command.startswith("auto_tag") or command == "index_and_auto_tag":
        return "auto_tag"
    if command.startswith(("manual_tag", "tag_alias", "organize_")):
        return "manual_tag"
    if command.startswith("folder_delete"):
        return "folder_cleanup"
    if command in {"cache_clear", "clean_results", "search_results_cleanup"}:
        return "library_task"
    return "library_task"


def _activity_task_label(command: str) -> str:
    labels = {
        "index": "建立索引",
        "sync": "同步图库",
        "index_and_auto_tag": "索引并智能标注",
        "auto_tag": "智能标注",
        "auto_tag_estimate": "智能标注估算",
        "manual_tag_batch": "批量标签",
        "manual_tag_undo": "撤销批量标签",
        "folder_delete_preview": "文件夹清理预览",
        "folder_delete_commit": "文件夹清理",
        "search_results_cleanup": "清理搜索结果",
        "cache_clear": "清理缓存",
        "clean_results": "清理历史结果",
        "auto_tag_policy_migrate": "标注策略迁移",
        "metadata_backfill": "描述向量回填",
        "folder_tag_backfill": "文件夹标签回填",
        "tag_alias_upsert": "更新别名",
        "tag_alias_delete": "删除别名",
    }
    return labels.get(command, command or "图库任务")


def _activity_status_event(status: str) -> str:
    return {
        "queued": "job_submitted",
        "pending": "job_submitted",
        "running": "job_started",
        "cancelling": "job_cancel_requested",
        "succeeded": "job_completed",
        "partial": "job_completed_partial",
        "needs_attention": "job_needs_attention",
        "failed": "job_failed",
        "cancelled": "job_cancelled",
        "interrupted": "job_interrupted",
    }.get(status, "job_status_changed")


def _activity_status_level(
    status: str,
) -> Literal["debug", "info", "warning", "error"]:
    if status in {"failed", "interrupted"}:
        return "error"
    if status in {"partial", "needs_attention"}:
        return "warning"
    return "info"


def _activity_job_message(command: str, status: str) -> str:
    label = _activity_task_label(command)
    suffix = {
        "queued": "已提交。",
        "pending": "正在等待执行。",
        "running": "已开始执行。",
        "cancelling": "正在安全取消。",
        "succeeded": "已完成。",
        "partial": "已完成，但有单项失败。",
        "needs_attention": "已停止，需要处理异常项。",
        "failed": "执行失败。",
        "cancelled": "已取消。",
        "interrupted": "因后端重启而中断。",
    }.get(status, f"状态变为 {status}。")
    return f"{label}{suffix}"


def _activity_search_event(status: str, result_count: int) -> str:
    if status in _SUCCESS_STATUSES:
        return "search_no_result" if result_count == 0 else "search_completed"
    return {
        "queued": "search_submitted",
        "pending": "search_submitted",
        "running": "search_started",
        "cancelling": "search_cancel_requested",
        "cancelled": "search_cancelled",
        "failed": "search_failed",
        "interrupted": "search_interrupted",
    }.get(status, "search_status_changed")


def _activity_search_message(status: str, result_count: int) -> str:
    if status in _SUCCESS_STATUSES:
        return (
            f"搜索完成，返回 {result_count} 张图片。"
            if result_count
            else "搜索完成，未找到可靠结果。"
        )
    return {
        "queued": "搜索已提交。",
        "pending": "搜索正在排队。",
        "running": "搜索正在执行。",
        "cancelling": "正在取消搜索。",
        "cancelled": "搜索已取消。",
        "failed": "搜索失败。",
        "interrupted": "搜索因后端重启而中断。",
    }.get(status, f"搜索状态变为 {status}。")


def _copy_object(value: Mapping[str, Any] | None) -> JsonObject | None:
    return dict(value) if value is not None else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return value.name
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) not in _PATH_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return str(value)


def _error_payload(exc: BaseException, fallback_code: str) -> JsonObject:
    if isinstance(exc, FacadeError):
        return exc.to_dict()
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    payload: JsonObject = {
        "code": str(code or fallback_code),
        "message": str(exc) or exc.__class__.__name__,
    }
    if isinstance(details, Mapping):
        payload["details"] = _json_safe(details)
    return payload


def _facade_error(exc: BaseException, *, code: str) -> FacadeError:
    if isinstance(exc, FacadeError):
        return exc
    status = 502 if isinstance(exc, BackendApiError) else 400
    if isinstance(exc, (DesktopConfigurationError, ResultCatalogError)):
        status = 409
    return FacadeError(
        str(getattr(exc, "code", None) or code),
        str(exc) or exc.__class__.__name__,
        status=status,
        details=(
            getattr(exc, "details", None)
            if isinstance(getattr(exc, "details", None), Mapping)
            else None
        ),
    )


__all__ = ["FacadeError", "PreviewFacade"]
