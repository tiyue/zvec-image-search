"""Thread-safe adapter from the webview gateway to existing desktop services."""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar, cast

from image_vector_service.activity_store import (
    ActivityStore,
    ActivityStoreUnavailable,
    InvalidActivityCursor,
)
from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.data_migration import (
    DataMigrationCancelled,
    DataMigrationCoordinator,
    DataMigrationExecutionError,
    DataMigrationRequest,
    DataMigrationValidationError,
    WindowsNativeMigrationOperations,
)
from image_vector_service.library_browser import LibraryBrowser
from image_vector_service.migration_recovery import (
    MigrationRecoveryError,
    MigrationRecoveryRecord,
    MigrationRecoveryStore,
)
from image_vector_service.model_catalog import (
    DEFAULT_AUTO_TAG_CONCURRENCY,
    DEFAULT_EMBEDDING_CONCURRENCY,
    MODEL_CONCURRENCY_OPTIONS,
)
from image_vector_service.process_lock import ProcessLock
from image_vector_service.search_features import (
    FEATURE_SCHEMA_VERSION,
    NUMERIC_FEATURE_NAMES,
)
from image_vector_service.search_learning_evaluator import (
    LocalFixedEvaluationEvaluator,
)
from image_vector_service.search_learning_service import (
    SearchLearningService,
    SearchLearningServiceError,
)
from image_vector_service.search_learning_store import (
    InvalidSearchLearningCursor,
    SearchCandidateRecord,
    SearchLearningStore,
    SearchLearningStoreUnavailable,
    SearchLearningValidationError,
    SearchSessionRecord,
)
from zvec_host.backend_api import BackendApiError, BackendHttpError, JsonObject
from zvec_host.backend_host import BackendBusyError, BackendHost, BackendRuntime
from zvec_host.configuration_service import (
    ConfigurationSnapshot,
    DesktopConfigurationError,
    DesktopConfigurationService,
)
from zvec_host.credentials import CredentialStore, default_credential_store
from zvec_host.library_tasks import (
    ActiveLearningDecision,
    ActiveLearningQueueRequest,
    ActiveLearningReviewRequest,
    ActiveLearningReviewUndoRequest,
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
    ClusterApplyIdentityRequest,
    ClusterDetailRequest,
    ClusterImagesRequest,
    ClusterListRequest,
    ClusterMergeRequest,
    ClusterSplitRequest,
    ClusterUndoRequest,
    FolderDeleteCommitRequest,
    FolderDeletePreviewRequest,
    FolderNameTagApplyRequest,
    FolderNameTagEstimateRequest,
    FolderNameTagSelection,
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
from zvec_host.model_settings import ModelSettingsService, ModelSettingsSnapshot
from zvec_host.result_catalog import (
    ResultCatalog,
    ResultCatalogError,
    SearchResult,
    SearchResultPage,
)
from zvec_host.search_service import (
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SearchService,
    SearchSortMode,
    TagMode,
)

from .image_registry import ImageRegistry, ImageRegistryError
from .lan_access import LanAccessController, LanAccessError

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
        "auto_index_enabled",
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
        "folder_name_tag_estimate",
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
    service: SearchService | None = None
    status: str = "queued"
    progress: JsonObject | None = None
    outcome: SearchOutcome | None = None
    error: JsonObject | None = None
    finished_at: float | None = None
    future: Future[Any] | None = None
    learning_recorded: bool = False


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
class _MigrationOperation:
    operation_id: str
    request: DataMigrationRequest
    confirmation_token: str
    confirmation_phrase: str
    submitted_at: float
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    message: str = "迁移任务已排队。"
    result: JsonObject | None = None
    error: JsonObject | None = None
    finished_at: float | None = None
    future: Future[Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process_lock: ProcessLock | None = None
    recovery_backup: JsonObject | None = None
    recovery_marker_id: str = ""


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
        background_executor: ThreadPoolExecutor | None = None,
        activity_store: ActivityStore | None = None,
        search_learning_service: SearchLearningService | None = None,
        data_migration_coordinator: DataMigrationCoordinator | None = None,
        lan_access_controller: LanAccessController | None = None,
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
            cache_directory=self._configuration.config_home / "cache",
            max_render_workers=4,
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
        if search_learning_service is None:
            learning_store = SearchLearningStore(
                self._configuration.config_home,
                redactions=activity_redactions,
            )
            self._search_learning = SearchLearningService(
                self._configuration.config_home,
                store=learning_store,
                evaluator=LocalFixedEvaluationEvaluator(
                    self._configuration.config_home
                ),
            )
            self._owns_search_learning = True
        else:
            self._search_learning = search_learning_service
            self._owns_search_learning = False
        if data_migration_coordinator is None:
            try:
                configured = self._configuration.load(optional=True)
            except Exception:
                configured = None
            results_directory = (
                configured.configuration.results_directory
                if configured is not None
                else self._configuration.config_home / "results"
            )
            migration_operations = WindowsNativeMigrationOperations(
                config_home=self._configuration.config_home,
                config_path=self._configuration.path,
                results_directory=results_directory,
            )
            self._data_migrations = DataMigrationCoordinator(migration_operations)
        else:
            self._data_migrations = data_migration_coordinator
        self._migration_recovery = MigrationRecoveryStore(
            self._configuration.config_home
        )
        self._migration_process_lock_path = (
            self._configuration.config_home / "data-migration.lock"
        )
        self._observer_job_history_fallback = observer_job_history_fallback
        self._executor = executor or ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="zvec-webview-control",
        )
        self._owns_executor = executor is None
        # Long job/search waiters must never consume every control worker.  The
        # gateway uses the control pool for startup, health, cancellation and UI
        # refresh requests that need to remain responsive while jobs run.
        self._background_executor = background_executor or ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="zvec-webview-background",
        )
        self._owns_background_executor = background_executor is None
        self._lock = threading.RLock()
        self._backend_state = "idle"
        self._backend_error: JsonObject | None = None
        self._backend_runtime: BackendRuntime | None = None
        self._backend_future: Future[Any] | None = None
        self._search_service: SearchService | None = None
        self._lan_search_service: SearchService | None = None
        self._task_service: LibraryTaskService | None = None
        self._searches: dict[str, _SearchOperation] = {}
        self._tasks: dict[str, _TaskOperation] = {}
        self._migration_operations: dict[str, _MigrationOperation] = {}
        self._migration_gate = False
        self._organize = _OrganizeCache()
        self._result_catalog: ResultCatalog | None = None
        self._result_catalog_version: tuple[int, int] | None = None
        self._activity_job_statuses: dict[str, str] = {}
        self._activity_search_statuses: dict[str, str] = {}
        self._closing = False
        self._closed = False
        self._lan_access = lan_access_controller or LanAccessController(
            self,
            self._configuration.config_home,
        )

    @property
    def image_registry(self) -> ImageRegistry:
        return self._registry

    @property
    def config_home(self) -> Path:
        return self._configuration.config_home

    def image_edit_settings(self) -> JsonObject:
        return self._image_edit_call(lambda client: client.get_image_edit_settings())

    def update_image_edit_settings(self, output_directory: str) -> JsonObject:
        return self._image_edit_call(
            lambda client: client.update_image_edit_settings(output_directory)
        )

    def submit_image_edit_multipart(self, content_type: str, body: bytes) -> JsonObject:
        task = self._image_edit_call(
            lambda client: client.submit_image_edit_multipart(content_type, body)
        )
        return self._image_edit_task_view(task)

    def submit_registered_image_edit(
        self,
        image_id: str,
        metadata: Mapping[str, Any],
    ) -> JsonObject:
        try:
            source_path = self._registry.resolve(image_id)
            if source_path.stat().st_size > 10 * 1024 * 1024:
                raise FacadeError(
                    "image_too_large",
                    "单张图片不能超过 10 MiB。",
                    status=413,
                )
            source_bytes = source_path.read_bytes()
        except FacadeError:
            raise
        except ImageRegistryError:
            raise
        except OSError as exc:
            raise FacadeError(
                "image_read_failed", "无法读取原图，请刷新后重试。", status=409
            ) from exc
        task = self._image_edit_call(
            lambda client: client.submit_image_edit_file(
                source_path.name,
                source_bytes,
                metadata,
            )
        )
        return self._image_edit_task_view(task)

    def list_image_edit_tasks(
        self, *, active: bool | None = None, limit: int = 100
    ) -> JsonObject:
        payload = self._image_edit_call(
            lambda client: client.list_image_edit_tasks(active=active, limit=limit)
        )
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            payload["tasks"] = [
                self._image_edit_task_view(task)
                for task in tasks
                if isinstance(task, Mapping)
            ]
            payload["count"] = len(payload["tasks"])
        return payload

    def get_image_edit_task(self, task_id: str) -> JsonObject:
        task = self._image_edit_call(lambda client: client.get_image_edit_task(task_id))
        return self._image_edit_task_view(task)

    def cancel_image_edit_task(self, task_id: str) -> JsonObject:
        task = self._image_edit_call(
            lambda client: client.cancel_image_edit_task(task_id)
        )
        return self._image_edit_task_view(task)

    def abandon_image_edit_tasks(self) -> JsonObject:
        return self._image_edit_call(lambda client: client.abandon_image_edit_tasks())

    def _image_edit_task_view(self, task: Mapping[str, Any]) -> JsonObject:
        view = dict(task)
        result = view.get("result")
        if not isinstance(result, Mapping):
            return view
        safe_result = dict(result)
        output_path = safe_result.pop("output_path", None)
        if isinstance(output_path, str) and output_path:
            try:
                metadata = self._registry.register(output_path)
            except (ImageRegistryError, OSError):
                safe_result["preview_error"] = "结果已保存，但暂时无法加载预览。"
            else:
                safe_result.update(
                    {
                        "image_id": metadata.image_id,
                        "image_url": (f"api/image/{metadata.image_id}?variant=preview"),
                        "thumbnail_url": (
                            f"api/image/{metadata.image_id}?variant=thumbnail"
                        ),
                        "width": metadata.width,
                        "height": metadata.height,
                    }
                )
        view["result"] = safe_result
        return view

    def _image_edit_call(self, callback: Callable[[Any], JsonObject]) -> JsonObject:
        try:
            return callback(self._ready_client())
        except BackendHttpError as exc:
            raise FacadeError(
                exc.code or "image_edit_request_failed",
                exc.backend_message or "图片编辑请求失败。",
                status=exc.status_code,
                details=exc.details if isinstance(exc.details, Mapping) else None,
            ) from exc
        except BackendApiError as exc:
            raise FacadeError(
                "image_edit_backend_unavailable",
                "本地图片编辑服务暂时不可用，请稍后重试。",
                status=502,
            ) from exc

    def diagnostic_redactions(self) -> tuple[str, ...]:
        """Return process-local secrets for the private diagnostic writer."""

        return self._activity.redactions()

    def lan_access_status(self) -> JsonObject:
        return self._lan_access_call(self._lan_access.status)

    def update_lan_access(self, payload: Mapping[str, Any]) -> JsonObject:
        return self._lan_access_call(lambda: self._lan_access.update(payload))

    def start_lan_access(self) -> JsonObject:
        return self._lan_access_call(self._lan_access.start)

    def start_lan_access_if_enabled(self) -> None:
        self._lan_access.start_if_enabled()

    def stop_lan_access(self) -> JsonObject:
        return self._lan_access_call(self._lan_access.stop)

    def approve_lan_pairing(self, pairing_id: str) -> JsonObject:
        return self._lan_access_call(lambda: self._lan_access.approve(pairing_id))

    def reject_lan_pairing(self, pairing_id: str) -> JsonObject:
        return self._lan_access_call(lambda: self._lan_access.reject(pairing_id))

    def revoke_lan_device(self) -> JsonObject:
        return self._lan_access_call(self._lan_access.revoke_device)

    @staticmethod
    def _lan_access_call(call: Callable[[], JsonObject]) -> JsonObject:
        try:
            return call()
        except LanAccessError as exc:
            raise FacadeError(
                exc.code,
                exc.message,
                status=exc.status,
            ) from exc

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

    def search_learning_status(self) -> JsonObject:
        """Return privacy settings, sample readiness, jobs, and model versions."""

        return self._learning_call(self._search_learning.status)

    def search_learning_settings(self) -> JsonObject:
        return self._learning_call(self._search_learning.settings)

    def update_search_learning_settings(self, payload: Mapping[str, Any]) -> JsonObject:
        return self._learning_call(
            lambda: self._search_learning.update_settings(payload)
        )

    def record_search_feedback(self, payload: Mapping[str, Any]) -> JsonObject:
        return self._learning_call(lambda: self._search_learning.feedback(payload))

    def revoke_search_feedback(self, event_id: str) -> JsonObject:
        return self._learning_call(
            lambda: self._search_learning.revoke_feedback(event_id)
        )

    def search_feedback(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
        action: str | None = None,
        active_only: bool = True,
    ) -> JsonObject:
        return self._learning_call(
            lambda: self._search_learning.list_feedback(
                cursor=cursor,
                limit=limit,
                session_id=session_id,
                action=action,
                active_only=active_only,
            )
        )

    def search_learning_sessions(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        query_type: str | None = None,
    ) -> JsonObject:
        return self._learning_call(
            lambda: self._search_learning.list_sessions(
                cursor=cursor,
                limit=limit,
                query_type=query_type,
            )
        )

    def train_search_learning(self) -> JsonObject:
        return self._learning_call(self._search_learning.train)

    def install_search_learning_evaluation(
        self, payload: Mapping[str, Any]
    ) -> JsonObject:
        unknown = sorted(set(payload) - {"source_path"})
        if unknown:
            raise FacadeError(
                "invalid_request",
                "Fixed evaluation import contains unsupported fields.",
                details={"unknown_fields": unknown},
            )
        source_path = payload.get("source_path")
        if not isinstance(source_path, str) or not source_path.strip():
            raise FacadeError("invalid_request", "source_path must not be empty.")
        return self._learning_call(
            lambda: self._search_learning.install_fixed_evaluation(source_path.strip())
        )

    def search_learning_job(self, job_id: str) -> JsonObject:
        return self._learning_call(lambda: self._search_learning.training_job(job_id))

    def cancel_search_learning_job(self, job_id: str) -> JsonObject:
        return self._learning_call(
            lambda: self._search_learning.cancel_training(job_id)
        )

    def activate_search_learning(self, payload: Mapping[str, Any]) -> JsonObject:
        unknown = sorted(set(payload) - {"model_version", "shadow_mode"})
        if unknown:
            raise FacadeError(
                "invalid_request",
                "Search-learning activation contains unsupported fields.",
                details={"unknown_fields": unknown},
            )
        model_version = payload.get("model_version")
        if not isinstance(model_version, str) or not model_version.strip():
            raise FacadeError("invalid_request", "model_version must not be empty.")
        shadow_mode = payload.get("shadow_mode", True)
        if not isinstance(shadow_mode, bool):
            raise FacadeError("invalid_request", "shadow_mode must be a boolean.")
        return self._learning_call(
            lambda: self._search_learning.activate(
                model_version.strip(), shadow_mode=shadow_mode
            )
        )

    def rollback_search_learning(self, payload: Mapping[str, Any]) -> JsonObject:
        if payload:
            raise FacadeError(
                "invalid_request",
                "Search-learning rollback does not accept fields.",
                details={"unknown_fields": sorted(payload)},
            )
        return self._learning_call(self._search_learning.rollback)

    def clear_search_learning(self, payload: Mapping[str, Any]) -> JsonObject:
        unknown = sorted(set(payload) - {"confirm"})
        if unknown:
            raise FacadeError(
                "invalid_request",
                "Search-learning cleanup contains unsupported fields.",
                details={"unknown_fields": unknown},
            )
        confirm = payload.get("confirm", False)
        if not isinstance(confirm, bool):
            raise FacadeError("invalid_request", "confirm must be a boolean.")
        return self._learning_call(
            lambda: self._search_learning.clear(confirmed=confirm)
        )

    def export_search_learning(self) -> JsonObject:
        return self._learning_call(self._search_learning.anonymous_export)

    def _learning_call(self, operation: Callable[[], JsonObject]) -> JsonObject:
        try:
            return operation()
        except InvalidSearchLearningCursor as exc:
            raise FacadeError(
                "invalid_search_learning_cursor", str(exc), status=400
            ) from exc
        except SearchLearningStoreUnavailable as exc:
            raise FacadeError(
                "search_learning_unavailable",
                "Local search learning is unavailable; normal search can continue.",
                status=503,
            ) from exc
        except SearchLearningServiceError as exc:
            conflict_codes = {
                "evaluation_gate_not_passed",
                "insufficient_training_data",
                "rollback_unavailable",
                "training_already_running",
                "training_running",
            }
            not_found_codes = {"model_not_found", "search_feedback_not_found"}
            status = (
                409
                if exc.code in conflict_codes
                else 404
                if exc.code in not_found_codes
                else 400
            )
            raise FacadeError(exc.code, str(exc), status=status) from exc
        except SearchLearningValidationError as exc:
            raise FacadeError(
                "invalid_search_learning_request", str(exc), status=400
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

    def _record_search_learning_session(self, operation: _SearchOperation) -> None:
        """Capture bounded ranking features without affecting search success."""

        with self._lock:
            if operation.learning_recorded:
                return
            operation.learning_recorded = True
        outcome = operation.outcome
        if outcome is None or outcome.manifest_path is None:
            return
        try:
            catalog = self._catalog()
            first_page = catalog.load_manifest(
                outcome.manifest_path,
                page=1,
                page_size=500,
            )
            pages = [first_page]
            if first_page.total_pages > 1:
                pages.append(
                    catalog.load_manifest(
                        outcome.manifest_path,
                        page=2,
                        page_size=500,
                    )
                )
            query_library_ids = operation.query.get("library_ids")
            library_ids = (
                tuple(str(value) for value in query_library_ids if str(value))
                if isinstance(query_library_ids, Sequence)
                and not isinstance(query_library_ids, (str, bytes, bytearray))
                else ()
            )
            diagnostics = first_page.ranking_diagnostics or {}
            raw_learning_diagnostics = diagnostics.get("search_learning")
            learning_diagnostics = (
                raw_learning_diagnostics
                if isinstance(raw_learning_diagnostics, Mapping)
                else {}
            )
            candidates: list[SearchCandidateRecord] = []
            ranking_model_version: str | None = None
            calibration_version: str | None = None
            for page in pages:
                for result in page.items:
                    library_id = result.library_id or (
                        library_ids[0] if len(library_ids) == 1 else "unknown"
                    )
                    doc_id = result.doc_id or _anonymous_doc_id(
                        library_id, result.relative_path
                    )
                    ranking_model_version = (
                        ranking_model_version or result.ranking_model_version
                    )
                    calibration_version = (
                        calibration_version or result.calibration_version
                    )
                    if result.search_features is not None:
                        features = dict(result.search_features)
                    else:
                        # Old manifests remain learnable with an explicit,
                        # conservative approximation. New manifests always carry
                        # the exact feature row produced by the ranking runtime.
                        features = {name: 0.0 for name in NUMERIC_FEATURE_NAMES}
                        features.update(
                            {
                                "vector_raw_score": float(result.raw_score),
                                "vector_confidence": float(
                                    result.ranking_confidence
                                    if result.ranking_confidence is not None
                                    else result.confidence
                                ),
                                "tag_match_score": float(
                                    result.metadata_confidence or 0.0
                                ),
                                "image_text_agreement": float(
                                    result.rank_agreement or 0.0
                                ),
                                "collection_rank": float(result.rank),
                                "duplicate_group_size": 1.0,
                            }
                        )
                    candidates.append(
                        SearchCandidateRecord(
                            library_id=library_id,
                            doc_id=doc_id,
                            sha256=result.sha256 or None,
                            original_rank=result.rank,
                            displayed_rank=(result.rank if result.rank <= 15 else None),
                            feature_schema_version=(
                                result.feature_schema_version or FEATURE_SCHEMA_VERSION
                            ),
                            features=features,
                            ranking_score=float(
                                result.ranking_score
                                if result.ranking_score is not None
                                else result.ranking_confidence
                                if result.ranking_confidence is not None
                                else result.confidence
                            ),
                            displayed=result.rank <= 15,
                        )
                    )
            self._search_learning.store.try_record_search(
                SearchSessionRecord(
                    session_id=operation.operation_id,
                    created_at=datetime.fromtimestamp(
                        operation.submitted_at, tz=timezone.utc
                    ),
                    query_type=first_page.query_type or "text",
                    requested_count=_first_int(operation.query, "top_k", "page_size"),
                    returned_count=first_page.total_items,
                    library_ids=library_ids,
                    latency_ms=_elapsed_ms(
                        operation.submitted_at, operation.finished_at
                    )
                    or 0,
                    ranking_model_version=(
                        ranking_model_version
                        or _optional_diagnostic_identifier(
                            learning_diagnostics.get("ranking_model_version")
                        )
                    ),
                    calibration_version=(
                        calibration_version
                        or _optional_diagnostic_identifier(
                            learning_diagnostics.get("calibration_version")
                        )
                    ),
                    query_text=str(operation.query.get("text") or "") or None,
                    candidates=tuple(candidates),
                )
            )
        except Exception:
            # A read-only/full ConfigHome, stale result, or malformed optional
            # diagnostic must never change the already completed search.
            return

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
                "name": "YaoLens",
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

    def search_history(self, *, limit: int = 12) -> JsonObject:
        """Return persisted result sets as safe, reopenable sidebar records."""

        try:
            entries = self._catalog().list_history(limit=limit)
        except ResultCatalogError as exc:
            raise FacadeError("search_history_failed", str(exc), status=500) from exc
        return {
            "items": [
                {
                    "id": entry.history_id,
                    "label": entry.label,
                    "query_type": entry.query_type,
                    "created_at": entry.created_at,
                    "total_items": entry.total_items,
                    "status": entry.status,
                }
                for entry in entries
            ]
        }

    def historical_results(
        self,
        history_id: str,
        *,
        page: int = 1,
        page_size: int = 15,
    ) -> JsonObject:
        """Reopen one persisted search result set after a process restart."""

        catalog = self._catalog()
        try:
            entry = catalog.history_entry(history_id)
            result_page = catalog.load_history(
                history_id,
                page=page,
                page_size=page_size,
            )
        except ResultCatalogError as exc:
            raise FacadeError(
                "search_history_not_found",
                "这条搜索记录已被清理或无法读取。",
                status=404,
            ) from exc
        response = self._page_response(
            result_page,
            operation_id=f"history:{entry.history_id}",
            query=entry.label,
            status=result_page.status,
            elapsed_ms=None,
        )
        response["history_id"] = entry.history_id
        return response

    def submit_search(self, payload: Mapping[str, Any]) -> JsonObject:
        self._ensure_migration_not_active()
        service = self._ready_search_service()
        return self._submit_search_with_service(payload, service)

    def submit_lan_search(self, payload: Mapping[str, Any]) -> JsonObject:
        """Submit a trusted, already-streamed LAN query image without a size cap.

        The LAN gateway writes request bodies to a private temporary file in
        bounded chunks.  This second staging copy therefore keeps streaming
        file I/O while deliberately omitting the desktop upload byte ceiling.
        """

        self._ensure_migration_not_active()
        service = self._ready_lan_search_service()
        return self._submit_search_with_service(payload, service)

    def _submit_search_with_service(
        self,
        payload: Mapping[str, Any],
        service: SearchService,
    ) -> JsonObject:
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
            service=service,
        )
        with self._lock:
            self._searches[operation.operation_id] = operation
            self._trim_operations_locked()
            operation.future = self._background_executor.submit(
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
        with self._lock:
            operation = self._searches.get(normalized)
            if operation is None:
                raise FacadeError(
                    "search_not_found", "搜索任务不存在或已过期。", status=404
                )
            service = operation.service
        if service is None:
            service = self._ready_search_service()
        try:
            job = service.cancel(operation.submission)
        except Exception as exc:
            raise _facade_error(exc, code="search_cancel_failed") from exc
        with self._lock:
            operation.progress = _copy_object(job)
            operation.status = str(job.get("status") or "running").lower()
        self._record_search_operation(operation)
        return self.search(normalized)

    def precheck_data_migration(self, payload: Mapping[str, Any]) -> JsonObject:
        """Run the authoritative dry-run while no backend owns the Workspace."""

        unknown = sorted(set(payload) - {"request", "dry_run"})
        if unknown:
            raise FacadeError(
                "invalid_data_migration",
                "数据迁移预检查包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        if payload.get("dry_run", True) is not True:
            raise FacadeError(
                "invalid_data_migration",
                "预检查必须以 dry-run 模式执行。",
            )
        request = self._data_migration_request(payload.get("request"))
        self._reserve_migration_gate()
        backend_was_running = False
        process_lock: ProcessLock | None = None
        try:
            self._ensure_no_pending_migration_recovery()
            if self.has_active_jobs(include_migrations=False):
                raise FacadeError(
                    "migration_backend_busy",
                    "当前仍有搜索或图库任务，完成后才能预检查迁移。",
                    status=409,
                )
            process_lock = self._acquire_data_migration_process_lock()
            backend_was_running = self._stop_backend_for_migration()
            preview = self._data_migrations.preview(request)
            response = preview.to_dict()
            response["backend_was_running"] = backend_was_running
            return {"preview": response}
        except FacadeError:
            raise
        except (DataMigrationValidationError, ValueError) as exc:
            raise FacadeError(
                "invalid_data_migration",
                str(exc),
                status=400,
            ) from exc
        except Exception as exc:
            raise _facade_error(exc, code="data_migration_precheck_failed") from exc
        finally:
            restart = self._restart_backend_after_migration(
                required=backend_was_running
            )
            if process_lock is not None:
                process_lock.release()
            with self._lock:
                self._migration_gate = False
            if backend_was_running and restart.get("status") == "degraded":
                self._activity_log(
                    level="error",
                    category="migration",
                    event="migration_precheck_restart_failed",
                    message="迁移预检查后未能恢复本地后端。",
                    details=restart,
                )

    def submit_data_migration(self, payload: Mapping[str, Any]) -> JsonObject:
        unknown = sorted(
            set(payload) - {"request", "confirmation_token", "confirmation_phrase"}
        )
        if unknown:
            raise FacadeError(
                "invalid_data_migration",
                "数据迁移请求包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        request = self._data_migration_request(payload.get("request"))
        confirmation_token = _required_string(
            payload, "confirmation_token", maximum=512
        )
        confirmation_phrase = _required_string(
            payload, "confirmation_phrase", maximum=32
        )
        self._reserve_migration_gate()
        process_lock: ProcessLock | None = None
        try:
            self._ensure_no_pending_migration_recovery()
            if self.has_active_jobs(include_migrations=False):
                raise FacadeError(
                    "migration_backend_busy",
                    "当前仍有搜索或图库任务，完成后才能开始迁移。",
                    status=409,
                )
            process_lock = self._acquire_data_migration_process_lock()
            operation = _MigrationOperation(
                operation_id=uuid.uuid4().hex,
                request=request,
                confirmation_token=confirmation_token,
                confirmation_phrase=confirmation_phrase,
                submitted_at=time.time(),
                process_lock=process_lock,
            )
            with self._lock:
                self._migration_operations[operation.operation_id] = operation
                self._trim_migration_operations_locked()
                try:
                    operation.future = self._background_executor.submit(
                        self._run_data_migration_worker,
                        operation.operation_id,
                    )
                except Exception:
                    self._migration_operations.pop(operation.operation_id, None)
                    raise
            process_lock = None  # The background operation now owns the handle.
            self._record_migration_snapshot(operation)
            return self._data_migration_view(operation)
        except Exception:
            if process_lock is not None:
                process_lock.release()
            with self._lock:
                if not self._active_migration_locked():
                    self._migration_gate = False
            raise

    def data_migration(self, operation_id: str) -> JsonObject:
        normalized = _operation_id(operation_id)
        with self._lock:
            operation = self._migration_operations.get(normalized)
            if operation is None:
                raise FacadeError(
                    "data_migration_not_found",
                    "数据迁移任务不存在或已经过期。",
                    status=404,
                )
            return self._data_migration_view(operation)

    def cancel_data_migration(self, operation_id: str) -> JsonObject:
        normalized = _operation_id(operation_id)
        with self._lock:
            operation = self._migration_operations.get(normalized)
            if operation is None:
                raise FacadeError(
                    "data_migration_not_found",
                    "数据迁移任务不存在或已经过期。",
                    status=404,
                )
            if operation.status not in {
                "succeeded",
                "failed",
                "failed_recovered",
                "needs_attention",
                "cancelled",
            }:
                operation.cancel_event.set()
                operation.status = "cancelling"
                operation.message = "正在等待安全取消点。"
            view = self._data_migration_view(operation)
        self._record_migration_snapshot(operation)
        return view

    def data_migration_recovery(self) -> JsonObject:
        """Return an unfinished migration marker without exposing source paths."""

        try:
            recovery = self._pending_migration_recovery()
        except MigrationRecoveryError as exc:
            raise FacadeError(
                "migration_recovery_invalid",
                "检测到损坏的数据迁移恢复记录，请保留备份并查看操作日志。",
                status=409,
                details={"error": str(exc)},
            ) from exc
        if recovery is not None:
            with self._lock:
                live_owner = any(
                    operation.status
                    not in {
                        "succeeded",
                        "failed",
                        "failed_recovered",
                        "needs_attention",
                        "cancelled",
                    }
                    and (
                        operation.operation_id == recovery.operation_id
                        or operation.recovery_marker_id == recovery.operation_id
                    )
                    for operation in self._migration_operations.values()
                )
            if live_owner:
                return {"recovery": None, "migration_active": True}
        return {"recovery": recovery.public_view() if recovery else None}

    def submit_data_migration_recovery(self, payload: Mapping[str, Any]) -> JsonObject:
        unknown = sorted(set(payload) - {"operation_id", "confirmation_phrase"})
        if unknown:
            raise FacadeError(
                "invalid_data_migration_recovery",
                "恢复请求包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        marker_id = _required_string(payload, "operation_id", maximum=128)
        confirmation_phrase = _required_string(
            payload, "confirmation_phrase", maximum=32
        )
        if confirmation_phrase != "RESTORE":
            raise FacadeError(
                "invalid_data_migration_recovery",
                "必须输入 RESTORE 才能恢复完整备份。",
                status=400,
            )
        try:
            recovery = self._pending_migration_recovery()
            if recovery is None:
                raise MigrationRecoveryError(
                    "No unfinished data migration requires recovery."
                )
            if recovery.operation_id != marker_id:
                raise MigrationRecoveryError(
                    "The recovery marker belongs to a different migration."
                )
        except MigrationRecoveryError as exc:
            raise FacadeError(
                "migration_recovery_invalid",
                str(exc),
                status=409,
            ) from exc

        self._reserve_migration_gate()
        process_lock: ProcessLock | None = None
        try:
            if self.has_active_jobs(include_migrations=False):
                raise FacadeError(
                    "migration_backend_busy",
                    "当前仍有搜索或图库任务，完成后才能恢复迁移备份。",
                    status=409,
                )
            process_lock = self._acquire_data_migration_process_lock()
            operation = _MigrationOperation(
                operation_id=uuid.uuid4().hex,
                request=recovery.request,
                confirmation_token="",
                confirmation_phrase="RESTORE",
                submitted_at=time.time(),
                stage="recovery_queued",
                message="完整备份恢复任务已排队。",
                process_lock=process_lock,
                recovery_backup=_copy_object(recovery.backup),
                recovery_marker_id=recovery.operation_id,
            )
            with self._lock:
                self._migration_operations[operation.operation_id] = operation
                self._trim_migration_operations_locked()
                try:
                    operation.future = self._background_executor.submit(
                        self._run_data_migration_recovery_worker,
                        operation.operation_id,
                    )
                except Exception:
                    self._migration_operations.pop(operation.operation_id, None)
                    raise
            process_lock = None
            self._record_migration_snapshot(operation)
            return self._data_migration_view(operation)
        except Exception:
            if process_lock is not None:
                process_lock.release()
            with self._lock:
                if not self._active_migration_locked():
                    self._migration_gate = False
            raise

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
        self._ensure_migration_not_active()
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
            operation.future = self._background_executor.submit(
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

    def folder_name_tag_settings(self) -> JsonObject:
        try:
            return self._ready_client().get_folder_name_tag_settings()
        except Exception as exc:
            raise _facade_error(
                exc, code="folder_name_tag_settings_failed"
            ) from exc

    def update_folder_name_tag_settings(
        self, payload: Mapping[str, Any]
    ) -> JsonObject:
        unknown = sorted(set(payload) - {"blacklist"})
        if unknown:
            raise FacadeError(
                "invalid_request",
                "文件夹名称标签设置包含不支持的字段。",
                details={"unknown_fields": unknown},
            )
        blacklist = payload.get("blacklist")
        if isinstance(blacklist, (str, bytes)) or not isinstance(
            blacklist, Sequence
        ):
            raise FacadeError(
                "invalid_request", "blacklist 必须是字符串数组。"
            )
        try:
            return self._ready_client().update_folder_name_tag_settings(
                cast(Sequence[str], blacklist)
            )
        except Exception as exc:
            raise _facade_error(
                exc, code="folder_name_tag_settings_failed"
            ) from exc

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
                embedding_concurrency=_optional_model_concurrency(
                    payload, "embedding_concurrency"
                ),
                auto_tag_concurrency=_optional_model_concurrency(
                    payload, "auto_tag_concurrency"
                ),
            )
        except Exception as exc:
            raise _facade_error(exc, code="model_settings_failed") from exc
        restart_required, restart = self._configuration_restart_state()
        self._activity_log(
            level="info",
            category="model_configuration",
            event="model_roles_updated",
            message="模型职责与并发配置已更新。",
            details={
                "provider": snapshot.provider,
                "embedding_model": snapshot.embedding_model,
                "auto_tag_primary_model": snapshot.auto_tag_primary_model,
                "auto_tag_escalation_model": snapshot.auto_tag_escalation_model,
                "embedding_concurrency": snapshot.embedding_concurrency,
                "auto_tag_concurrency": snapshot.auto_tag_concurrency,
                "restart_required": restart_required,
            },
        )
        response = _models_view(snapshot)
        response["restart_required"] = restart_required
        response["restart"] = restart
        return response

    def replace_model_json(self, payload: Mapping[str, Any]) -> JsonObject:
        try:
            snapshot = self._models.replace_json(
                _required_json_text(payload, "json_text", maximum_bytes=1024 * 1024)
            )
        except Exception as exc:
            raise _facade_error(exc, code="model_settings_failed") from exc
        restart_required, restart = self._configuration_restart_state()
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
                "embedding_concurrency": snapshot.embedding_concurrency,
                "auto_tag_concurrency": snapshot.auto_tag_concurrency,
                "restart_required": restart_required,
            },
        )
        response = _models_view(snapshot)
        response["restart_required"] = restart_required
        response["restart"] = restart
        return response

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
        self._ensure_migration_not_active()
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
        self._ensure_migration_not_active()
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
        self._ensure_migration_not_active()
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

        self._ensure_migration_not_active()
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
        auto_index_enabled = (
            _boolean(payload.get("auto_index_enabled"), "auto_index_enabled")
            if "auto_index_enabled" in payload
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
                auto_index_enabled=auto_index_enabled,
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

    def preview_folder_name_tags(
        self,
        library_id: str,
        *,
        selection: Mapping[str, Any],
        mode: str = "normal",
        force: bool = True,
    ) -> JsonObject:
        """Return a model-free preview before folder-source tags are replaced."""

        _task_type, request, _request_library_id = _library_request(
            {
                "task_type": "folder_name_tag_estimate",
                "library_id": library_id,
                "selection": dict(selection),
                "mode": mode,
                "force": force,
            }
        )
        if not isinstance(request, FolderNameTagEstimateRequest):
            raise FacadeError(
                "folder_name_tag_preview_failed",
                "无法创建文件夹名称标签预览请求。",
                status=400,
            )
        service = self._ready_task_service()
        try:
            submission = service.estimate_folder_name_tags(request)
            outcome = service.wait(
                submission,
                timeout=5 * 60,
                poll_interval=0.1,
            )
        except Exception as exc:
            raise _facade_error(exc, code="folder_name_tag_preview_failed") from exc
        self._record_job_snapshot(outcome.job)
        if not outcome.successful or outcome.result is None:
            error = outcome.error or {}
            raise FacadeError(
                str(error.get("code") or "folder_name_tag_preview_failed"),
                str(error.get("message") or "无法生成文件夹名称标签预览。"),
                status=409,
                details=(
                    error.get("details")
                    if isinstance(error.get("details"), Mapping)
                    else None
                ),
            )
        return cast(JsonObject, dict(outcome.result))

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

    def _data_migration_request(self, value: object) -> DataMigrationRequest:
        if not isinstance(value, Mapping):
            raise FacadeError(
                "invalid_data_migration",
                "request 必须是数据迁移参数对象。",
            )
        try:
            request = DataMigrationRequest.from_mapping(value)
        except DataMigrationValidationError as exc:
            raise FacadeError(
                "invalid_data_migration",
                str(exc),
                status=400,
            ) from exc
        if request.migration_type == "legacy_config":
            try:
                target = Path(request.target).expanduser().resolve()
                expected = self._configuration.path.expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise FacadeError(
                    "invalid_data_migration",
                    "旧配置迁移目标路径无效。",
                    status=400,
                ) from exc
            if target != expected:
                raise FacadeError(
                    "invalid_data_migration",
                    "旧配置迁移必须写入当前软件使用的 config.json。",
                    status=400,
                    details={"expected_target": str(expected)},
                )
        return request

    def _reserve_migration_gate(self) -> None:
        with self._lock:
            if self._migration_gate or self._active_migration_locked():
                raise FacadeError(
                    "data_migration_busy",
                    "已有数据迁移正在预检查或执行。",
                    status=409,
                )
            self._migration_gate = True

    def _pending_migration_recovery(self) -> MigrationRecoveryRecord | None:
        recovery = self._migration_recovery.load()
        if recovery is not None and recovery.stage in {
            "complete",
            "restore_complete",
            "recovery_complete",
        }:
            # These stages are durably written only after verification or a
            # complete restore. A crash before normal cleanup must not offer a
            # stale destructive restore action on the next launch.
            self._migration_recovery.clear(recovery.operation_id)
            return None
        return recovery

    def _ensure_no_pending_migration_recovery(self) -> None:
        try:
            recovery = self._pending_migration_recovery()
        except MigrationRecoveryError as exc:
            raise FacadeError(
                "migration_recovery_invalid",
                "数据迁移恢复记录损坏，必须先处理恢复记录。",
                status=409,
                details={"error": str(exc)},
            ) from exc
        if recovery is not None:
            raise FacadeError(
                "migration_recovery_required",
                "检测到上次迁移未完整结束，请先从完整备份恢复。",
                status=409,
                details=recovery.public_view(),
            )

    def _acquire_data_migration_process_lock(self) -> ProcessLock:
        """Serialize migration and recovery across every desktop instance."""

        process_lock = ProcessLock(self._migration_process_lock_path)
        try:
            process_lock.acquire()
        except ConfigurationError as exc:
            raise FacadeError(
                "data_migration_busy",
                "另一个软件实例正在预检查、迁移或恢复数据。",
                status=409,
            ) from exc
        except OSError as exc:
            raise FacadeError(
                "data_migration_lock_unavailable",
                "无法创建数据迁移互斥锁，请检查应用配置目录权限。",
                status=503,
            ) from exc
        return process_lock

    def _active_migration_locked(self) -> bool:
        terminal = {
            "succeeded",
            "failed",
            "failed_recovered",
            "needs_attention",
            "cancelled",
        }
        return any(
            operation.status not in terminal
            for operation in self._migration_operations.values()
        )

    def _ensure_migration_not_active(self) -> None:
        with self._lock:
            if self._migration_gate or self._active_migration_locked():
                raise FacadeError(
                    "data_migration_busy",
                    "数据迁移期间不能提交新的搜索或图库任务。",
                    status=409,
                )

    def _stop_backend_for_migration(self) -> bool:
        """Stop only the child backend; keep facade, executor, and logs alive."""

        with self._lock:
            state = self._backend_state
            start_future = self._backend_future
        if state == "starting" and start_future is not None:
            try:
                start_future.result(timeout=90)
            except FutureTimeout as exc:
                raise FacadeError(
                    "migration_backend_stop_failed",
                    "后端仍在启动，暂时无法安全迁移。",
                    status=409,
                ) from exc
        if self.has_active_jobs(include_migrations=False):
            raise FacadeError(
                "migration_backend_busy",
                "后端仍有活动任务，未停止也未修改任何文件。",
                status=409,
            )
        with self._lock:
            was_running = self._backend_state in {"starting", "ready"} or bool(
                self._host.is_running
            )
        if was_running:
            try:
                self._host.stop(force=False)
            except BackendBusyError as exc:
                raise FacadeError(
                    "migration_backend_busy",
                    "后端仍有活动任务，未停止也未修改任何文件。",
                    status=409,
                ) from exc
            except Exception as exc:
                raise _facade_error(exc, code="migration_backend_stop_failed") from exc
        with self._lock:
            self._backend_state = "stopped"
            self._backend_error = None
            self._backend_runtime = None
            self._backend_future = None
            self._search_service = None
            self._lan_search_service = None
            self._task_service = None
        return was_running

    def _restart_backend_after_migration(self, *, required: bool) -> JsonObject:
        if not required:
            return {"status": "not_required", "restart_required": False}
        try:
            future = self.start_backend_async()
            if future is not None:
                future.result(timeout=120)
        except Exception as exc:
            return {
                "status": "degraded",
                "restart_required": True,
                "error": _error_payload(exc, "backend_restart_failed"),
            }
        with self._lock:
            state = self._backend_state
            error = _copy_object(self._backend_error)
        return {
            "status": state,
            "restart_required": state != "ready",
            **({"error": error} if error else {}),
        }

    def _run_data_migration_worker(self, operation_id: str) -> None:
        with self._lock:
            operation = self._migration_operations.get(operation_id)
            if operation is None:
                return
            operation.status = "running"
            operation.stage = "backend_stop"
            operation.progress = 1
            operation.message = "正在安全停止常驻后端。"
        self._record_migration_snapshot(operation)
        backend_was_running = False
        restart_required = False
        clear_recovery_marker = False
        try:
            backend_was_running = self._stop_backend_for_migration()
            restart_required = backend_was_running or self._configuration.path.is_file()

            def progress(payload: dict[str, object]) -> None:
                marker_stage = ""
                marker_backup: Mapping[str, object] | None = None
                with self._lock:
                    current = self._migration_operations.get(operation_id)
                    if current is None:
                        return
                    current.stage = str(payload.get("stage") or current.stage)
                    raw_progress = payload.get("progress")
                    if isinstance(raw_progress, int) and not isinstance(
                        raw_progress, bool
                    ):
                        current.progress = max(0, min(100, raw_progress))
                    current.message = str(payload.get("message") or current.message)
                    backup = payload.get("backup")
                    if isinstance(backup, Mapping):
                        current_result = dict(current.result or {})
                        current_result["backup"] = _json_safe(backup)
                        current.result = current_result
                        marker_backup = backup
                    snapshot = current
                    require_persisted = current.stage == "backup_complete"
                    marker_stage = current.stage
                if marker_stage == "backup_complete":
                    if marker_backup is None:
                        raise RuntimeError(
                            "Migration backup completed without a recovery receipt."
                        )
                    self._migration_recovery.arm(
                        operation_id,
                        operation.request,
                        marker_backup,
                        stage=marker_stage,
                    )
                elif marker_stage in {
                    "migrate",
                    "verify",
                    "complete",
                    "restore",
                    "restore_complete",
                }:
                    self._migration_recovery.update_stage(
                        operation_id,
                        marker_stage,
                    )
                self._record_migration_snapshot(
                    snapshot,
                    announce=False,
                    require_persisted=require_persisted,
                )

            result = self._data_migrations.execute(
                operation.request,
                confirmation_token=operation.confirmation_token,
                confirmation_phrase=operation.confirmation_phrase,
                cancel_event=operation.cancel_event,
                on_progress=progress,
            )
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "succeeded"
                    current.stage = "complete"
                    current.progress = 100
                    current.message = "迁移与本地搜索验证已完成。"
                    current.result = _json_safe(result)
                    current.finished_at = time.time()
            clear_recovery_marker = True
        except DataMigrationCancelled as exc:
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "cancelled"
                    current.stage = "cancelled"
                    current.message = str(exc)
                    current.finished_at = time.time()
            clear_recovery_marker = True
        except DataMigrationExecutionError as exc:
            report = _json_safe(exc.report)
            status = str(report.get("status") or "failed")
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = status
                    current.stage = "restore"
                    current.message = "迁移失败，已记录恢复结果。"
                    current.result = report
                    current.error = {
                        "code": "data_migration_failed",
                        "message": str(exc),
                    }
                    current.finished_at = time.time()
            clear_recovery_marker = status in {"failed", "failed_recovered"}
        except Exception as exc:
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "failed"
                    current.stage = "failed"
                    current.message = "迁移未执行完成。"
                    current.error = _error_payload(exc, "data_migration_failed")
                    current.finished_at = time.time()
        finally:
            if clear_recovery_marker:
                try:
                    self._migration_recovery.clear(operation_id)
                except MigrationRecoveryError as exc:
                    with self._lock:
                        current = self._migration_operations.get(operation_id)
                        if current is not None:
                            current.status = "needs_attention"
                            current.stage = "recovery_marker_cleanup"
                            current.message = (
                                "迁移已结束，但恢复标记无法清理，请勿重复恢复。"
                            )
                            current.error = {
                                "code": "migration_recovery_marker_cleanup_failed",
                                "message": str(exc),
                            }
            self._finish_data_migration_worker(
                operation_id,
                restart_required=restart_required,
            )

    def _run_data_migration_recovery_worker(self, operation_id: str) -> None:
        with self._lock:
            operation = self._migration_operations.get(operation_id)
            if operation is None:
                return
            operation.status = "running"
            operation.stage = "backend_stop"
            operation.progress = 1
            operation.message = "正在安全停止常驻后端并准备恢复。"
        self._record_migration_snapshot(operation)
        restart_required = False
        try:
            backend_was_running = self._stop_backend_for_migration()
            restart_required = backend_was_running or self._configuration.path.is_file()

            def progress(payload: dict[str, object]) -> None:
                with self._lock:
                    current = self._migration_operations.get(operation_id)
                    if current is None:
                        return
                    current.stage = str(payload.get("stage") or current.stage)
                    raw_progress = payload.get("progress")
                    if isinstance(raw_progress, int) and not isinstance(
                        raw_progress, bool
                    ):
                        current.progress = max(0, min(100, raw_progress))
                    current.message = str(payload.get("message") or current.message)
                    snapshot = current
                    marker_id = current.recovery_marker_id
                    marker_stage = current.stage
                self._migration_recovery.update_stage(marker_id, marker_stage)
                self._record_migration_snapshot(snapshot, announce=False)

            if operation.recovery_backup is None or not operation.recovery_marker_id:
                raise MigrationRecoveryError(
                    "The recovery operation has no durable backup marker."
                )
            result = self._data_migrations.recover(
                operation.request,
                operation.recovery_backup,
                on_progress=progress,
            )
            self._migration_recovery.clear(operation.recovery_marker_id)
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "succeeded"
                    current.stage = "recovery_complete"
                    current.progress = 100
                    current.message = "迁移前完整备份已恢复。"
                    current.result = _json_safe(result)
                    current.finished_at = time.time()
        except DataMigrationExecutionError as exc:
            report = _json_safe(exc.report)
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "needs_attention"
                    current.stage = "restore"
                    current.message = "完整备份恢复失败，恢复标记已保留。"
                    current.result = report
                    current.error = {
                        "code": "data_migration_recovery_failed",
                        "message": str(exc),
                    }
                    current.finished_at = time.time()
        except Exception as exc:
            with self._lock:
                current = self._migration_operations.get(operation_id)
                if current is not None:
                    current.status = "needs_attention"
                    current.stage = "restore"
                    current.message = "完整备份恢复未完成，恢复标记已保留。"
                    current.error = _error_payload(
                        exc, "data_migration_recovery_failed"
                    )
                    current.finished_at = time.time()
        finally:
            self._finish_data_migration_worker(
                operation_id,
                restart_required=restart_required,
            )

    def _finish_data_migration_worker(
        self,
        operation_id: str,
        *,
        restart_required: bool,
    ) -> None:
        """Restart the backend, persist the terminal state, and release locks."""

        restart = self._restart_backend_after_migration(required=restart_required)
        final_snapshot: _MigrationOperation | None
        process_lock: ProcessLock | None = None
        with self._lock:
            current = self._migration_operations.get(operation_id)
            if current is not None:
                result = dict(current.result or {})
                result["restart"] = restart
                result["restart_required"] = bool(restart.get("restart_required"))
                current.result = result
                restart_ok = restart.get("status") in {
                    "ready",
                    "not_required",
                } and not bool(restart.get("restart_required"))
                if not restart_ok:
                    # Migrated or restored data is not usable until the local
                    # backend can open it again. Never report a false success.
                    current.status = "needs_attention"
                    current.stage = "backend_restart"
                    current.message = (
                        "数据操作已结束，但常驻后端未能恢复，请查看错误后重试。"
                    )
                    restart_error = restart.get("error")
                    if current.error is None:
                        current.error = (
                            _copy_object(restart_error)
                            if isinstance(restart_error, Mapping)
                            else {
                                "code": "backend_restart_failed",
                                "message": "数据操作后常驻后端未能恢复。",
                            }
                        )
                    else:
                        current.error = {
                            **current.error,
                            "restart": _json_safe(restart_error),
                        }
                process_lock = current.process_lock
                current.process_lock = None
                final_snapshot = current
            else:
                final_snapshot = None
        try:
            if final_snapshot is not None:
                self._record_migration_snapshot(final_snapshot)
        finally:
            try:
                if process_lock is not None:
                    process_lock.release()
            finally:
                with self._lock:
                    self._migration_gate = False

    def _data_migration_view(self, operation: _MigrationOperation) -> JsonObject:
        result = _copy_object(operation.result)
        verification = (
            result.get("verification") if isinstance(result, Mapping) else None
        )
        backup = result.get("backup") if isinstance(result, Mapping) else None
        return {
            "id": operation.operation_id,
            "migration_id": operation.operation_id,
            "status": operation.status,
            "stage": operation.stage,
            "progress": operation.progress,
            "message": operation.message,
            "request": {
                "migration_type": operation.request.migration_type,
                "library_id": operation.request.library_id,
            },
            "submitted_at": operation.submitted_at,
            "finished_at": operation.finished_at,
            "result": result,
            "verification": _json_safe(verification),
            "backup": _json_safe(backup),
            "error": _copy_object(operation.error),
            "error_message": str((operation.error or {}).get("message") or ""),
        }

    def _record_migration_snapshot(
        self,
        operation: _MigrationOperation,
        *,
        announce: bool = True,
        require_persisted: bool = False,
    ) -> None:
        job: JsonObject = {
            "id": operation.operation_id,
            "command": "data_migration",
            "task_type": "data_migration",
            "params": {
                "library_id": operation.request.library_id,
                "migration_type": operation.request.migration_type,
            },
            "status": operation.status,
            "submitted_at": operation.submitted_at,
            "finished_at": operation.finished_at,
            "progress": {
                "percent": operation.progress,
                "message": operation.message,
                "stage": operation.stage,
            },
            "result": _copy_object(operation.result),
            "error": _copy_object(operation.error),
        }
        # Data migrations are owned by the facade rather than the backend
        # process. Persist every stage authoritatively so the recovery receipt
        # written at ``backup_complete`` survives a crash before mutation.
        persisted = False
        try:
            persisted = self._activity.record_job(job)
        except Exception:
            persisted = False
        if require_persisted and (
            not persisted or not self._activity.flush(timeout=5.0)
        ):
            raise RuntimeError(
                "Migration backup receipt could not be durably persisted."
            )
        self._record_job_snapshot(job, announce=announce)

    def _trim_migration_operations_locked(self) -> None:
        if len(self._migration_operations) <= _MAX_OPERATIONS:
            return
        terminal = {
            "succeeded",
            "failed",
            "failed_recovered",
            "needs_attention",
            "cancelled",
        }
        removable = sorted(
            (
                operation
                for operation in self._migration_operations.values()
                if operation.status in terminal
            ),
            key=lambda operation: operation.finished_at or operation.submitted_at,
        )
        for operation in removable[: len(self._migration_operations) - _MAX_OPERATIONS]:
            self._migration_operations.pop(operation.operation_id, None)

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

    def has_active_jobs(self, *, include_migrations: bool = True) -> bool:
        with self._lock:
            if include_migrations and self._active_migration_locked():
                return True
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
            client = self._ready_client()
            future = self._executor.submit(
                lambda: (
                    client.list_jobs(active=True, limit=1),
                    client.list_image_edit_tasks(active=True, limit=1),
                )
            )
            jobs_payload, image_edit_payload = future.result(timeout=1.0)
        except FutureTimeout:
            return True
        except Exception:
            # If idleness cannot be established, do not claim shutdown is safe.
            return True
        jobs = jobs_payload.get("jobs")
        image_edit_tasks = image_edit_payload.get("tasks")
        return (isinstance(jobs, list) and bool(jobs)) or (
            isinstance(image_edit_tasks, list) and bool(image_edit_tasks)
        )

    def close(self, *, force: bool = False) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
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
                else:
                    self._closing = False
                closed = self._closed
            if closed:
                self._activity_log(
                    level="info",
                    category="backend",
                    event="backend_stopped",
                    message="本地后端已停止。",
                )
                self._activity.flush(timeout=1.0)
                if self._owns_activity:
                    self._activity.close(timeout=2.0)
                if self._owns_search_learning:
                    self._search_learning.close()
                self._lan_access.close()
                self._registry.close()
                self._invalidate_catalog()
                if self._owns_executor:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                if self._owns_background_executor:
                    self._background_executor.shutdown(
                        wait=False,
                        cancel_futures=True,
                    )

    def _start_backend_worker(self) -> None:
        try:
            runtime = self._host.start()
            with self._lock:
                stop_after_start = self._closing or self._closed
            if stop_after_start:
                self._host.stop(force=True)
                return
            secret = self._credentials.read_secret()
            if secret:
                self._host.client.configure_credentials(secret)
            search_service = SearchService(self._host.client, runtime.query_root)
            lan_search_service = SearchService(
                self._host.client,
                runtime.query_root,
                max_query_image_bytes=None,
            )
            task_service = LibraryTaskService(self._host.client)
        except Exception as exc:
            with self._lock:
                if self._closing or self._closed:
                    self._backend_state = "stopped"
                    self._backend_error = None
                    return
                self._backend_state = "degraded"
                self._backend_error = _error_payload(exc, "backend_start_failed")
                self._backend_runtime = None
                self._search_service = None
                self._lan_search_service = None
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
            stop_after_start = self._closing or self._closed
            if not stop_after_start:
                self._backend_runtime = runtime
                self._search_service = search_service
                self._lan_search_service = lan_search_service
                self._task_service = task_service
                self._backend_state = "ready"
                self._backend_error = None
        if stop_after_start:
            self._host.stop(force=True)
            return
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
            service = operation.service if operation is not None else None
            if service is None:
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
            self._record_search_learning_session(current)

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
        self._background_executor.submit(self._refresh_organize_worker)

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
        query: JsonObject | str,
        status: str,
        elapsed_ms: int | None,
    ) -> JsonObject:
        items: list[JsonObject] = []
        query_library_ids = (
            query.get("library_ids") if isinstance(query, Mapping) else None
        )
        fallback_library_id = (
            str(query_library_ids[0])
            if isinstance(query_library_ids, Sequence)
            and not isinstance(query_library_ids, (str, bytes, bytearray))
            and len(query_library_ids) == 1
            else ""
        )
        # History results need their session registered so feedback can be
        # recorded against them.  The original fresh-search path already
        # recorded the session under the UUID operation_id, but history
        # re-opens use a different identifier.
        if operation_id.startswith("history:"):
            with suppress(Exception):
                self._search_learning.store.try_record_search(
                    SearchSessionRecord(
                        session_id=operation_id,
                        query_type=page.query_type or "text",
                        requested_count=page.page_size,
                        returned_count=page.total_items,
                        library_ids=(
                            (fallback_library_id,) if fallback_library_id else ()
                        ),
                        latency_ms=0,
                        candidates=(),
                    )
                )
        for result in page.items:
            try:
                item = self._search_result_view(result)
                item["search_session_id"] = operation_id
                library_id = str(item.get("library_id") or fallback_library_id)
                doc_id = str(item.get("doc_id") or "") or _anonymous_doc_id(
                    library_id or "unknown", result.relative_path
                )
                item["library_id"] = library_id
                item["doc_id"] = doc_id
                items.append(item)
                if operation_id != "latest" and library_id:
                    with suppress(Exception):
                        self._search_learning.store.ensure_candidate(
                            session_id=operation_id,
                            library_id=library_id,
                            doc_id=doc_id,
                            original_rank=result.rank,
                            ranking_score=float(
                                result.ranking_score
                                if result.ranking_score is not None
                                else result.ranking_confidence
                                if result.ranking_confidence is not None
                                else result.confidence
                            ),
                            sha256=result.sha256 or None,
                            feature_schema_version=(
                                result.feature_schema_version or FEATURE_SCHEMA_VERSION
                            ),
                            features=result.search_features or {},
                        )
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
            "query_type": page.query_type,
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
            "doc_id": result.doc_id,
            "sha256": result.sha256,
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
            "ranking_model_version": result.ranking_model_version,
            "ranking_score": result.ranking_score,
            "feature_schema_version": result.feature_schema_version,
            "ranking_fallback": result.ranking_fallback,
            "ranking_fallback_reason": result.ranking_fallback_reason,
            "calibrated_minimum_confidence": (result.calibrated_minimum_confidence),
            "calibration_version": result.calibration_version,
            "calibration_scope": result.calibration_scope,
            "calibration_fallback": result.calibration_fallback,
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
        public_result = _public_job_result(
            result_map,
            command=command,
            registry=self._registry,
        )
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

    def _ready_lan_search_service(self) -> SearchService:
        with self._lock:
            service = self._lan_search_service
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
        if self._closing or self._closed:
            raise FacadeError("preview_closed", "YaoLens 已关闭。", status=503)


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
                raw_selection.get("include_subfolders", True),
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
    elif task_type in {"folder_name_tag_estimate", "folder_name_tag_apply"}:
        raw_selection = payload.get("selection")
        if not isinstance(raw_selection, Mapping):
            raise FacadeError("invalid_request", "selection must be an object.")
        selection_mode = _choice(
            raw_selection.get("mode"),
            "selection.mode",
            supported=("library", "folder"),
            default="library",
        )
        folder_selection = FolderNameTagSelection(
            mode=cast(Literal["library", "folder"], selection_mode),
            folder_key=_optional_string(raw_selection.get("folder_key"), maximum=8_192),
            include_subfolders=_boolean(
                raw_selection.get("include_subfolders", False),
                "selection.include_subfolders",
            ),
        )
        folder_name_tag_mode = _choice(
            payload.get("mode"),
            "mode",
            supported=("normal", "clean", "mark_all"),
            default="normal",
        )
        force = _boolean(payload.get("force", True), "force")
        request = (
            FolderNameTagEstimateRequest(
                library_id,
                folder_selection,
                mode=cast(Any, folder_name_tag_mode),
                force=force,
            )
            if task_type == "folder_name_tag_estimate"
            else FolderNameTagApplyRequest(
                library_id,
                folder_selection,
                mode=cast(Any, folder_name_tag_mode),
                force=force,
                expected_rule_revision=_optional_string(
                    payload.get("expected_rule_revision"), maximum=64
                ),
            )
        )
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
    elif task_type == "cluster_images":
        request = ClusterImagesRequest(
            library_id=library_id,
            scope=_choice(
                payload.get("scope"),
                "scope",
                supported=("new_or_changed", "all"),
                default="new_or_changed",
            ),
            cluster_types=cast(
                Any,
                _string_tuple(
                    payload.get("cluster_types", ("exact", "perceptual")),
                    "cluster_types",
                ),
            ),
        )
    elif task_type == "cluster_list":
        cluster_type = _choice(
            payload.get("cluster_type"),
            "cluster_type",
            supported=(
                "all",
                "exact",
                "perceptual",
                "semantic",
                "single",
                "near_duplicate",
            ),
            default="all",
        )
        request = ClusterListRequest(
            library_id=library_id,
            offset=_bounded_int(payload.get("offset", 0), "offset", 0, 2**63 - 1),
            limit=_bounded_int(payload.get("limit", 100), "limit", 1, 500),
            cluster_type=cast(Any, cluster_type),
        )
    elif task_type == "cluster_detail":
        request = ClusterDetailRequest(
            library_id=library_id,
            cluster_id=_required_string(payload, "cluster_id", maximum=256),
            offset=_bounded_int(payload.get("offset", 0), "offset", 0, 2**63 - 1),
            limit=_bounded_int(payload.get("limit", 20), "limit", 1, 2_000),
        )
    elif task_type == "cluster_merge":
        request = ClusterMergeRequest(
            library_id=library_id,
            cluster_ids=_string_tuple(payload.get("cluster_ids"), "cluster_ids"),
        )
    elif task_type == "cluster_split":
        request = ClusterSplitRequest(
            library_id=library_id,
            cluster_id=_required_string(payload, "cluster_id", maximum=256),
            doc_ids=_string_tuple(payload.get("doc_ids"), "doc_ids"),
        )
    elif task_type == "cluster_apply_identity":
        request = ClusterApplyIdentityRequest(
            library_id=library_id,
            cluster_id=_required_string(payload, "cluster_id", maximum=256),
            identity_category=cast(
                Any,
                _choice(
                    payload.get("identity_category"),
                    "identity_category",
                    supported=("real_person", "cosplayer", "character", "work"),
                    default="",
                ),
            ),
            identity_value=_required_string(
                payload,
                "identity_value",
                maximum=256,
            ),
        )
    elif task_type == "cluster_undo":
        request = ClusterUndoRequest(library_id=library_id)
    elif task_type == "active_learning_queue":
        request = ActiveLearningQueueRequest(
            library_id=library_id,
            review_budget=_bounded_int(
                payload.get("review_budget", 25), "review_budget", 20, 30
            ),
        )
    elif task_type == "active_learning_review":
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, Sequence) or isinstance(
            raw_decisions, (str, bytes)
        ):
            raise FacadeError("invalid_request", "decisions must be an array.")
        if not raw_decisions:
            raise FacadeError(
                "invalid_request", "At least one learning decision is required."
            )
        if len(raw_decisions) > 1_000:
            raise FacadeError(
                "invalid_request", "decisions can contain at most 1000 items."
            )
        learning_decisions: list[ActiveLearningDecision] = []
        for index, raw_decision in enumerate(raw_decisions):
            if not isinstance(raw_decision, Mapping):
                raise FacadeError(
                    "invalid_request", f"decisions[{index}] must be an object."
                )
            learning_decisions.append(
                ActiveLearningDecision(
                    doc_id=_required_string(raw_decision, "doc_id", maximum=1_024),
                    decision=cast(
                        Any,
                        _choice(
                            raw_decision.get("decision"),
                            f"decisions[{index}].decision",
                            supported=("accept", "reject", "edit", "skip"),
                            default="accept",
                        ),
                    ),
                    labels=_string_tuple(
                        raw_decision.get("labels"),
                        f"decisions[{index}].labels",
                    ),
                )
            )
        request = ActiveLearningReviewRequest(
            library_id=library_id,
            queue_id=_required_string(payload, "queue_id", maximum=256),
            decisions=tuple(learning_decisions),
        )
    elif task_type == "active_learning_review_undo":
        request = ActiveLearningReviewUndoRequest(library_id=library_id)
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
            f"当前不支持任务类型：{task_type}",
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
            "auto_index_enabled": library.auto_index_enabled,
        }
        for library in snapshot.configuration.libraries
    ]


def _models_view(snapshot: ModelSettingsSnapshot | None) -> JsonObject:
    if snapshot is None:
        return {
            "embedding_model": "",
            "auto_tag_primary_model": "",
            "auto_tag_escalation_model": "",
            "embedding_concurrency": DEFAULT_EMBEDDING_CONCURRENCY,
            "auto_tag_concurrency": DEFAULT_AUTO_TAG_CONCURRENCY,
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
        "embedding_concurrency": snapshot.embedding_concurrency,
        "auto_tag_concurrency": snapshot.auto_tag_concurrency,
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
            "reason": str(
                item.get("reason")
                or item.get("error")
                or item.get("message")
                or item.get("code")
                or ""
            ),
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
        "applied",
        "applied_count",
        "restored",
        "conflicts",
        "conflict_count",
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
        "input_count",
        "clustered_count",
        "new_or_changed_count",
        "removed_count",
        "reused_edge_count",
        "semantic_query_count",
        "cluster_count",
        "failure_count",
        "selected_count",
        "embedding_api_requests",
        "qwen_api_requests",
    }
)
_PUBLIC_JOB_TEXT_KEYS = frozenset({"batch_id", "operation_id", "operation", "status"})


def _public_job_result(
    result: Mapping[str, Any],
    *,
    command: str = "",
    registry: ImageRegistry | None = None,
) -> JsonObject:
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
    for key in (
        "undo_available",
        "dry_run",
        "needs_attention",
        "over_budget",
        "embedding_recomputed",
        "undone",
    ):
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
    if registry is not None:
        if command == "cluster_list":
            output.update(_cluster_list_result_view(result, registry))
        elif command == "cluster_detail":
            output.update(_cluster_detail_result_view(result, registry))
        elif command in {
            "active_learning_queue",
            "active_learning_review",
            "active_learning_review_undo",
        }:
            output.update(_active_learning_result_view(result, registry))
    return output


def _cluster_list_result_view(
    result: Mapping[str, Any], registry: ImageRegistry
) -> JsonObject:
    items = result.get("items")
    return {
        "total": _bounded_public_int(result.get("total")),
        "offset": _bounded_public_int(result.get("offset")),
        "limit": _bounded_public_int(result.get("limit")),
        "items": [
            view
            for raw in (items if isinstance(items, list) else [])[:500]
            if isinstance(raw, Mapping)
            and (view := _cluster_record_view(raw, registry)) is not None
        ],
    }


def _cluster_detail_result_view(
    result: Mapping[str, Any], registry: ImageRegistry
) -> JsonObject:
    raw_cluster = result.get("cluster")
    cluster = (
        _cluster_record_view(raw_cluster, registry)
        if isinstance(raw_cluster, Mapping)
        else None
    )
    raw_items = result.get("items")
    items = [
        _intelligence_image_view(raw, registry)
        for raw in (raw_items if isinstance(raw_items, list) else [])[:10_000]
        if isinstance(raw, Mapping)
    ]
    payload: JsonObject = {
        "items": items,
        "members": items,
        "offset": _bounded_public_int(result.get("offset")),
        "limit": _bounded_public_int(result.get("limit"), fallback=len(items)),
        "total": _bounded_public_int(result.get("member_count"), fallback=len(items)),
        "member_count": _bounded_public_int(
            result.get("member_count"), fallback=len(items)
        ),
        "edge_count": _bounded_public_int(result.get("edge_count")),
        "edge_offset": _bounded_public_int(result.get("edge_offset")),
        "edge_limit": _bounded_public_int(result.get("edge_limit")),
    }
    has_more = result.get("has_more")
    if isinstance(has_more, bool):
        payload["has_more"] = has_more
    edge_has_more = result.get("edge_has_more")
    if isinstance(edge_has_more, bool):
        payload["edge_has_more"] = edge_has_more
    if cluster is not None:
        if not isinstance(cluster.get("representative"), Mapping) and items:
            cluster["representative"] = items[0]
        payload["cluster"] = cluster
    return payload


def _active_learning_result_view(
    result: Mapping[str, Any], registry: ImageRegistry
) -> JsonObject:
    payload: JsonObject = {}
    for key in ("queue_id", "selection_version"):
        value = result.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 256:
            payload[key] = value.strip()
    for key in ("schema_version", "candidate_count", "selected_count"):
        if isinstance(result.get(key), int) and not isinstance(result.get(key), bool):
            payload[key] = _bounded_public_int(result.get(key))
    raw_items = result.get("items")
    payload["items"] = [
        _intelligence_image_view(raw, registry)
        for raw in (raw_items if isinstance(raw_items, list) else [])[:30]
        if isinstance(raw, Mapping)
    ]
    raw_decisions = result.get("decisions")
    decisions: list[JsonObject] = []
    for raw in (raw_decisions if isinstance(raw_decisions, list) else [])[:1_000]:
        if not isinstance(raw, Mapping):
            continue
        doc_id = str(raw.get("doc_id") or "").strip()
        action = str(raw.get("decision") or "").strip().lower()
        if (
            not doc_id
            or len(doc_id) > 1_024
            or action
            not in {
                "accept",
                "reject",
                "edit",
                "skip",
            }
        ):
            continue
        decisions.append(
            {
                "doc_id": doc_id,
                "decision": action,
                "labels": _public_string_list(raw.get("labels"), maximum=100),
            }
        )
    payload["decisions"] = decisions
    raw_failures = result.get("failures")
    payload["failures"] = [
        {
            "doc_id": str(raw.get("doc_id") or "")[:1_024],
            "error": str(
                raw.get("error") or raw.get("message") or raw.get("code") or ""
            )[:2_000],
        }
        for raw in (raw_failures if isinstance(raw_failures, list) else [])[:100]
        if isinstance(raw, Mapping)
    ]
    return payload


def _cluster_record_view(
    raw: Mapping[str, Any], registry: ImageRegistry
) -> JsonObject | None:
    cluster_id = str(raw.get("cluster_id") or raw.get("id") or "").strip()
    if not cluster_id or len(cluster_id) > 256:
        return None
    edge_kinds = [
        value
        for value in _public_string_list(raw.get("edge_kinds"), maximum=4)
        if value in {"exact", "perceptual", "semantic", "legacy"}
    ]
    cluster_type = str(raw.get("cluster_type") or raw.get("type") or "").strip()
    payload: JsonObject = {
        "cluster_id": cluster_id,
        "member_count": _bounded_public_int(
            raw.get("member_count"),
            fallback=len(raw.get("member_doc_ids", []))
            if isinstance(raw.get("member_doc_ids"), list)
            else 0,
        ),
        "edge_kinds": edge_kinds,
        "identity_anchors": _identity_anchor_views(raw.get("identity_anchors")),
    }
    if cluster_type in {"single", "exact", "perceptual", "semantic"}:
        payload["cluster_type"] = cluster_type
    representative = raw.get("representative")
    if isinstance(representative, Mapping):
        payload["representative"] = _intelligence_image_view(representative, registry)
    return payload


def _intelligence_image_view(
    raw: Mapping[str, Any], registry: ImageRegistry
) -> JsonObject:
    doc_id = str(raw.get("doc_id") or raw.get("id") or "").strip()
    relative_path = _safe_relative_path(raw.get("relative_path"))
    file_name = str(
        raw.get("file_name")
        or raw.get("filename")
        or raw.get("name")
        or (PurePosixPath(relative_path).name if relative_path else doc_id)
    ).strip()
    view: JsonObject = {
        "doc_id": doc_id[:1_024],
        "file_name": file_name[:1_024],
        "relative_path": relative_path,
        "image_available": False,
    }
    for key in ("rank",):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            view[key] = max(0, value)
    for key in (
        "uncertainty_score",
        "conflict_score",
        "outlier_score",
        "ranking_disagreement",
    ):
        value = raw.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            view[key] = max(0.0, min(1.0, float(value)))
    for key in ("group_id", "query_id", "cluster_id", "candidate_kind"):
        value = raw.get(key)
        if isinstance(value, str) and len(value.strip()) <= 1_024:
            view[key] = value.strip()
    for key, maximum in (
        ("reasons", 8),
        ("suggested_tags", 100),
        ("tags", 100),
    ):
        view[key] = _public_string_list(raw.get(key), maximum=maximum)
    path = _first_path(raw)
    if path is not None:
        try:
            metadata = registry.register(path)
        except ImageRegistryError:
            pass
        else:
            view.update(
                {
                    "id": metadata.image_id,
                    "image_id": metadata.image_id,
                    "thumbnail_url": (
                        f"api/image/{metadata.image_id}?variant=thumbnail"
                    ),
                    "image_url": f"api/image/{metadata.image_id}?variant=preview",
                    "image_available": True,
                    "width": metadata.width,
                    "height": metadata.height,
                    "size_bytes": metadata.size_bytes,
                }
            )
    return view


def _identity_anchor_views(value: Any) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    output: list[JsonObject] = []
    for raw in value[:100]:
        if not isinstance(raw, Mapping):
            continue
        category = str(raw.get("category") or "").strip()
        anchor = str(raw.get("value") or "").strip()
        if not category or not anchor:
            continue
        confidence = raw.get("confidence")
        confidence_value = (
            max(0.0, min(1.0, float(confidence)))
            if isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
            else 0.0
        )
        output.append(
            {
                "category": category[:128],
                "value": anchor[:4_096],
                "source": str(raw.get("source") or "")[:128],
                "confidence": confidence_value,
                "support": _bounded_public_int(raw.get("support")),
                "conflict": raw.get("conflict") is True,
            }
        )
    return output


def _public_string_list(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in value[:maximum]:
        if not isinstance(raw, str):
            continue
        normalized = raw.strip()
        if not normalized or len(normalized) > 4_096 or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _bounded_public_int(value: Any, *, fallback: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, min(_MAX_TECHNICAL_COUNT, value))
    return max(0, min(_MAX_TECHNICAL_COUNT, fallback))


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


def _anonymous_doc_id(library_id: str, relative_path: str) -> str:
    """Create a stable opaque fallback without persisting a relative path."""

    digest = hashlib.sha256(
        f"{library_id}\0{relative_path}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"anon-{digest}"


def _optional_diagnostic_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    if any(
        not (character.isalnum() or character in "_.:-") for character in normalized
    ):
        return None
    return normalized


def _file_stat_version(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_mtime_ns, metadata.st_size


def _package_version() -> str:
    try:
        return metadata.version("zvec-image-search")
    except metadata.PackageNotFoundError:
        return "0.3.0"


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


def _optional_model_concurrency(
    payload: Mapping[str, Any],
    name: str,
) -> int | None:
    if name not in payload:
        return None
    value = payload.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in MODEL_CONCURRENCY_OPTIONS
    ):
        raise FacadeError(
            "invalid_request",
            f"{name} 必须是 1、2、4 或 6。",
            details={"supported": list(MODEL_CONCURRENCY_OPTIONS)},
        )
    return value


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
    if command == "auto_index_and_auto_tag":
        return "auto_index"
    if command.startswith("auto_tag") or command == "index_and_auto_tag":
        return "auto_tag"
    if command.startswith(("manual_tag", "folder_name_tag", "tag_alias", "organize_")):
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
        "auto_index_and_auto_tag": "自动增量索引与标注",
        "auto_tag": "智能标注",
        "auto_tag_estimate": "智能标注估算",
        "manual_tag_batch": "批量标签",
        "manual_tag_undo": "撤销批量标签",
        "folder_name_tag_estimate": "文件夹名称标签预览",
        "folder_name_tag_apply": "文件夹名称标签",
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
