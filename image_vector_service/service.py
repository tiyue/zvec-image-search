from __future__ import annotations

import os
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from PIL import Image, UnidentifiedImageError

from zvec_logging import initialize_zvec

from .active_learning import (
    ActiveLearningCandidate,
    ActiveLearningConfig,
    ActiveLearningError,
    ActiveLearningQueue,
    build_active_learning_queue,
    read_active_learning_queue,
    record_learning_decision,
    write_active_learning_queue,
)
from .active_learning_review_store import (
    ActiveLearningReviewItem,
    ActiveLearningReviewStore,
    ActiveLearningReviewStoreError,
)
from .annotation_service import AutoTaggingCoordinator, StreamingAutoTagSession
from .app_logging import close_app_logger, get_app_logger
from .auto_tag_cache import SharedAutoTagCache
from .cluster_operation_store import (
    ClusterOperationItemInput,
    ClusterOperationNotFound,
    ClusterOperationStore,
    ClusterOperationStoreUnavailable,
    ClusterOperationValidationError,
)
from .config import ConfigurationError, ServiceConfig
from .dashscope_client import (
    DashScopeEmbeddingClient,
    DashScopeError,
    EmbeddingResponse,
    ImageInputError,
)
from .failure_sink import IndexFailureSink
from .folder_deletion import FolderDeletionManager
from .hybrid_search import (
    HybridTagIntent,
    detect_hybrid_tag_intent,
    hybrid_candidate_count,
)
from .image_clustering import (
    IDENTITY_CATEGORIES,
    ClusterSnapshot,
    IdentityEvidence,
    ImageClusteringConfig,
    ImageClusterInput,
    ImageClusterService,
    SemanticNeighbor,
    apply_manual_cluster_rules,
    cluster_detail,
    read_cluster_snapshot,
    write_cluster_snapshot,
)
from .image_scanner import (
    file_sha256,
    inspect_query_image,
    scan_folder,
)
from .library_browser import LibraryBrowser
from .logical_paths import normalize_path
from .metadata_backfill import (
    MetadataBackfillItem,
    MetadataBackfillReport,
    MetadataBackfillRunner,
)
from .metadata_search import (
    fuse_combined_metadata_hits,
    fuse_text_metadata_hits,
)
from .metadata_text import build_metadata_text
from .models import (
    FailureKind,
    FileFailure,
    ImageRecord,
    IndexReport,
    PreparedSearch,
    PreparedSearchCandidates,
    RankSource,
    ResolvedSearchHit,
    SearchHit,
    SearchReport,
    SearchSortMode,
)
from .process_lock import ProcessLock
from .rank_fusion import (
    DEFAULT_AGREEMENT_REWARD,
    DEFAULT_RANK_DECAY,
    DEFAULT_WEAK_CHANNEL_FLOOR,
    DEFAULT_WEAK_CHANNEL_PENALTY,
    MINIMUM_RESULT_CONFIDENCE,
    ConfidenceRanking,
    confidence_candidate_limit,
    confidence_rank,
    normalize_sort_mode,
    sort_confidence_hits,
    sort_mode_uses_diversity,
)
from .result_diversity import DiversityRanking, diversify_search_hits
from .result_exporter import (
    clean_result_directories,
    cleanup_search_results,
    export_results,
)
from .search_learning_config import (
    ACTIVE_CONFIG_FILENAME,
    SEARCH_LEARNING_DIRECTORY,
    SearchLearningBundle,
    load_search_learning,
)
from .search_learning_runtime import apply_search_learning
from .search_quality import (
    QualityMode,
    annotate_search_hits,
    load_search_quality,
)
from .source_resolver import SourcePathResolver
from .state import IndexState
from .tag_aliases import TagAliasDictionary, TagAliasStore
from .tag_search import (
    TagCatalog,
    TagMatchMode,
    TagSearchPlan,
    matched_tags_for_result,
    normalize_tag_search_text,
    result_matches_tag_plan,
)
from .tags import folder_tags_for_image, normalize_tags
from .zvec_repository import ZvecImageRepository


@dataclass(frozen=True)
class _EmbeddingFailure:
    items: list[tuple[str, list[ImageRecord]]]
    error: str
    kind: FailureKind


@dataclass
class _EmbeddingBatchResult:
    successes: list[tuple[tuple[str, list[ImageRecord]], list[float]]] = field(
        default_factory=list
    )
    failures: list[_EmbeddingFailure] = field(default_factory=list)
    usage: list[dict[str, object]] = field(default_factory=list)
    systemic_error: str = ""
    systemic_items: list[tuple[str, list[ImageRecord]]] = field(default_factory=list)

    def extend(self, other: _EmbeddingBatchResult) -> None:
        self.successes.extend(other.successes)
        self.failures.extend(other.failures)
        self.usage.extend(other.usage)
        self.systemic_error = self.systemic_error or other.systemic_error
        self.systemic_items.extend(other.systemic_items)


@dataclass
class _UpsertResult:
    committed: bool
    inserted_entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _ManualTagPlan:
    entry: dict[str, Any]
    before_tags: tuple[str, ...]
    after_tags: tuple[str, ...]


@dataclass
class _PipelineNetworkMetrics:
    embedding_active: int = 0
    flash_active: int = 0
    embedding_peak_in_flight: int = 0
    flash_peak_in_flight: int = 0
    overlap_observed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def embedding_started(self) -> None:
        with self._lock:
            self.embedding_active += 1
            self.embedding_peak_in_flight = max(
                self.embedding_peak_in_flight,
                self.embedding_active,
            )
            self._observe_overlap()

    def embedding_finished(self) -> None:
        with self._lock:
            self.embedding_active = max(0, self.embedding_active - 1)

    def set_flash_active(self, active: int) -> None:
        with self._lock:
            self.flash_active = max(0, int(active))
            self.flash_peak_in_flight = max(
                self.flash_peak_in_flight,
                self.flash_active,
            )
            self._observe_overlap()

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "overlap_observed": self.overlap_observed,
                "embedding_peak_in_flight": self.embedding_peak_in_flight,
                "flash_peak_in_flight": self.flash_peak_in_flight,
            }

    def _observe_overlap(self) -> None:
        self.overlap_observed = self.overlap_observed or (
            self.embedding_active > 0 and self.flash_active > 0
        )


class ImageVectorService:
    def __init__(
        self,
        config: ServiceConfig | None = None,
        embedding_client=None,
        repository: ZvecImageRepository | None = None,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ):
        self.config = config or ServiceConfig()
        self.config.validate()
        self.search_quality = load_search_quality(self.config.workspace)
        self.search_learning = load_search_learning(self.config.config_home_path)
        self._search_learning_reload_lock = threading.Lock()
        self._search_learning_manifest_signature = (
            self._search_learning_active_signature()
        )
        self.progress = progress or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: None)
        self._lock = ProcessLock(self.config.lock_path)
        self._closed = False
        self._lock.acquire()
        try:
            initialize_zvec(self.config.log_dir)
            self.logger = get_app_logger(self.config.log_dir)
            self.repository = repository or ZvecImageRepository(self.config)
            self._embedding_client = embedding_client
            self.state = IndexState(
                self.config.state_path, legacy_path=self.config.legacy_state_path
            )
            self.active_learning_reviews: ActiveLearningReviewStore | None = None
            try:
                self.active_learning_reviews = ActiveLearningReviewStore(
                    self.config.state_path
                )
                recovery = self.active_learning_reviews.recover_incomplete()
                if recovery["recovered_count"]:
                    self.logger.warning(
                        "active_learning_review_recovered batches=%d api_requests=0",
                        recovery["recovered_count"],
                    )
            except ActiveLearningReviewStoreError as exc:
                # Search/index remain usable when the optional local review
                # journal cannot initialize; review commands fail explicitly.
                self.logger.warning(
                    "active_learning_review_store_unavailable error=%s",
                    str(exc) or exc.__class__.__name__,
                )
            self.state_reset = self.state.ensure_collection_uuid(
                self.repository.collection_uuid,
                reset_if_unbound=self.repository.created,
            )
            self.cluster_operations: ClusterOperationStore | None = None
            try:
                self.cluster_operations = ClusterOperationStore(
                    self.config.state_path,
                    recover_interrupted=True,
                )
                self._import_legacy_cluster_snapshot()
            except ClusterOperationStoreUnavailable as exc:
                self.logger.warning(
                    "cluster_operation_store_unavailable error=%s",
                    str(exc) or exc.__class__.__name__,
                )
            self._refresh_tag_catalog()
            self.source_resolver = SourcePathResolver(self.state)
            self.auto_tag_cache = SharedAutoTagCache(
                self.config.results_path / "auto_tag_cache.sqlite3"
            )
            self.auto_tag_cache_migration = self.auto_tag_cache.migrate_legacy_database(
                self.config.state_path
            )
            if self.auto_tag_cache_migration.changed:
                self.logger.info(
                    "legacy_auto_tag_cache_migrated scanned=%d imported=%d "
                    "upgraded=%d rekeyed=%d skipped=%d",
                    self.auto_tag_cache_migration.scanned,
                    self.auto_tag_cache_migration.imported,
                    self.auto_tag_cache_migration.upgraded,
                    self.auto_tag_cache_migration.rekeyed,
                    self.auto_tag_cache_migration.skipped,
                )
            if self.auto_tag_cache_migration.skipped:
                self.logger.warning(
                    "legacy_auto_tag_cache_skipped rows=%d source=%s",
                    self.auto_tag_cache_migration.skipped,
                    self.auto_tag_cache_migration.source_path,
                )
            self.auto_tagging = AutoTaggingCoordinator(
                config=self.config,
                state=self.state,
                repository=self.repository,
                cache=self.auto_tag_cache,
                source_resolver=self.source_resolver,
                progress=self.progress,
                cancel_check=self.cancel_check,
            )
            self.validate_documents = self.repository.doc_count != self.state.count()
        except Exception:
            if hasattr(self, "auto_tag_cache"):
                self.auto_tag_cache.close()
            if hasattr(self, "state"):
                self.state.close()
            if hasattr(self, "logger"):
                close_app_logger(self.logger)
            self._lock.release()
            raise

    @property
    def embedding_client(self):
        if self._embedding_client is None:
            self._embedding_client = DashScopeEmbeddingClient(self.config)
        return self._embedding_client

    def _clear_legacy_root_tags(self) -> None:
        cleared = 0
        for root in self.state.list_roots():
            if root["tags"]:
                self.state.set_root_tags(str(root["root_id"]), ())
                cleared += 1
        if cleared:
            self.logger.info("legacy_root_tags_cleared roots=%d", cleared)

    def _refresh_tag_catalog(self) -> None:
        configure = getattr(self.repository, "set_tag_catalog", None)
        if configure is not None:
            configure(
                self.state.list_effective_tags(),
                aliases=self._current_tag_aliases(),
            )

    def _current_tag_aliases(self) -> TagAliasDictionary:
        return TagAliasDictionary.load(
            self.config.results_path / "tag-aliases.json",
            missing_ok=True,
        )

    def _current_tag_catalog(self) -> TagCatalog:
        return TagCatalog(
            self.state.list_effective_tags(),
            aliases=self._current_tag_aliases(),
        )

    def list_tag_aliases(self) -> dict[str, object]:
        """Return the shared search aliases in the desktop API shape."""

        aliases = TagAliasDictionary.load(
            self.config.results_path / "tag-aliases.json",
            missing_ok=True,
        )
        return {
            "aliases": [
                {
                    "canonical_name": group.canonical,
                    "aliases": list(group.aliases),
                }
                for group in aliases.groups
            ],
            "count": aliases.count,
        }

    def upsert_tag_alias(
        self,
        canonical_name: str,
        aliases: Iterable[str],
    ) -> dict[str, object]:
        """Create or replace one alias group and refresh this Collection."""

        store = TagAliasStore.load(
            self.config.results_path / "tag-aliases.json",
            missing_ok=True,
        )
        group = store.upsert(canonical_name, aliases)
        self._refresh_tag_catalog()
        return {
            "updated": True,
            "deleted": False,
            "entry": {
                "canonical_name": group.canonical,
                "aliases": list(group.aliases),
            },
        }

    def delete_tag_alias(self, canonical_name: str) -> dict[str, object]:
        """Delete one canonical alias group without rewriting stored tags."""

        store = TagAliasStore.load(
            self.config.results_path / "tag-aliases.json",
            missing_ok=True,
        )
        deleted = store.delete(canonical_name)
        if deleted:
            self._refresh_tag_catalog()
        return {
            "updated": False,
            "deleted": deleted,
            "entry": None,
        }

    def preview_folder_deletion(
        self,
        *,
        library_id: str,
        folder_key: str,
        include_subfolders: bool = True,
    ) -> dict[str, Any]:
        """Create a model-free, expiring snapshot for one folder deletion."""

        manager = self._folder_deletion_manager(library_id)
        try:
            preview = manager.preview(
                folder_key=folder_key,
                include_subfolders=include_subfolders,
            )
        finally:
            manager.close()
        self.logger.info(
            "folder_delete_preview library_id=%s operation_id=%s images=%d "
            "files=%d blocked=%s api_requests=0",
            library_id,
            preview["operation_id"],
            preview["image_count"],
            preview["file_count"],
            preview["blocked"],
        )
        return preview

    def commit_folder_deletion(
        self,
        *,
        library_id: str,
        operation_id: str,
        confirmation_token: str,
        confirm: bool,
    ) -> dict[str, Any]:
        """Commit a previously previewed deletion without invoking any model."""

        manager = self._folder_deletion_manager(library_id)
        try:
            result = manager.commit(
                operation_id=operation_id,
                confirmation_token=confirmation_token,
                confirm=confirm,
            )
        finally:
            manager.close()
        self._refresh_tag_catalog()
        self.logger.info(
            "folder_delete_commit library_id=%s operation_id=%s status=%s "
            "deleted=%d failed=%d api_requests=0",
            library_id,
            operation_id,
            result.get("status"),
            result.get("indexed_deleted", 0),
            result.get("failed", 0),
        )
        return result

    def recover_folder_deletions(self, *, library_id: str) -> dict[str, Any]:
        """Resume deletion sagas that crossed the physical staging boundary."""

        manager = self._folder_deletion_manager(library_id)
        try:
            report = manager.recover_incomplete()
        finally:
            manager.close()
        if report["recovered"] or report["failed"]:
            self._refresh_tag_catalog()
        log = self.logger.warning if report["failed"] else self.logger.info
        log(
            "folder_delete_recovery library_id=%s recovered=%d failed=%d "
            "api_requests=0",
            library_id,
            report["recovered"],
            report["failed"],
        )
        return report

    def _folder_deletion_manager(self, library_id: str) -> FolderDeletionManager:
        normalized = str(library_id).strip()
        if not normalized:
            raise ValueError("library_id must not be empty.")
        return FolderDeletionManager(
            config=self.config,
            state=self.state,
            repository=self.repository,
            library_id=normalized,
            progress=self.progress,
            cancel_check=self.cancel_check,
        )

    def manual_tag_batch(
        self,
        *,
        library_id: str,
        selection: Mapping[str, Any],
        operation: str,
        tags: Iterable[str],
    ) -> dict[str, Any]:
        """Apply manual tags in bounded chunks without touching other sources."""

        normalized_operation = str(operation).strip().lower()
        if normalized_operation not in {"add", "remove", "replace_manual"}:
            raise ValueError("operation must be add, remove, or replace_manual.")
        normalized_tags = normalize_tags(tags)
        if normalized_operation in {"add", "remove"} and not normalized_tags:
            raise ValueError(f"{normalized_operation} requires at least one tag.")

        selection_view, total, chunks = self._manual_tag_selection(
            library_id=library_id,
            selection=selection,
        )
        batch_id = uuid.uuid4().hex
        self.state.create_manual_tag_batch(
            batch_id=batch_id,
            operation=normalized_operation,
            selection=selection_view,
            tags=normalized_tags,
            total_count=total,
        )

        processed = 0
        updated = 0
        unchanged = 0
        failed = 0
        failures: list[dict[str, str]] = []
        warnings: list[str] = []
        needs_attention = False
        try:
            for selected in chunks:
                self.cancel_check()
                plans: list[_ManualTagPlan] = []
                missing_snapshots: list[
                    tuple[str, tuple[str, ...], tuple[str, ...]]
                ] = []
                missing_outcomes: list[tuple[str, str, str]] = []
                for doc_id, entry in selected:
                    if entry is None:
                        error = "The selected indexed image no longer exists."
                        missing_snapshots.append((doc_id, (), ()))
                        missing_outcomes.append((doc_id, "failed", error))
                        failed += 1
                        _append_manual_failure(
                            failures,
                            doc_id=doc_id,
                            relative_path="",
                            error=error,
                        )
                        continue
                    before = normalize_tags(entry.get("tags", ()))
                    after = _manual_tags_after_operation(
                        before,
                        normalized_tags,
                        normalized_operation,
                    )
                    if before == after:
                        unchanged += 1
                        continue
                    plans.append(_ManualTagPlan(entry, before, after))

                if missing_snapshots:
                    self.state.record_manual_tag_batch_entries(
                        batch_id, missing_snapshots
                    )
                    self.state.update_manual_tag_batch_entries(
                        batch_id, missing_outcomes
                    )
                if plans:
                    self.state.record_manual_tag_batch_entries(
                        batch_id,
                        (
                            (
                                str(plan.entry["doc_id"]),
                                plan.before_tags,
                                plan.after_tags,
                            )
                            for plan in plans
                        ),
                    )
                    succeeded, plan_failures, inconsistent = (
                        self._apply_manual_tag_plans(plans)
                    )
                    needs_attention = needs_attention or inconsistent
                    succeeded_set = set(succeeded)
                    outcomes: list[tuple[str, str, str]] = []
                    for plan in plans:
                        doc_id = str(plan.entry["doc_id"])
                        if doc_id in succeeded_set:
                            updated += 1
                            outcomes.append((doc_id, "applied", ""))
                            continue
                        error = plan_failures.get(
                            doc_id, "The manual tag update failed."
                        )
                        failed += 1
                        outcomes.append((doc_id, "failed", error))
                        _append_manual_failure(
                            failures,
                            doc_id=doc_id,
                            relative_path=str(plan.entry.get("relative_path") or ""),
                            error=error,
                        )
                    self.state.update_manual_tag_batch_entries(batch_id, outcomes)

                processed += len(selected)
                self.state.update_manual_tag_batch_progress(
                    batch_id,
                    processed=processed,
                    updated=updated,
                    unchanged=unchanged,
                    failed=failed,
                )
                self.progress(f"Updated manual tags {processed}/{total} images.")
        except BaseException as exc:
            status = (
                "cancelled" if exc.__class__.__name__ == "JobCancelled" else "failed"
            )
            self.state.finish_manual_tag_batch(
                batch_id,
                status=status,
                processed=processed,
                updated=updated,
                unchanged=unchanged,
                failed=failed,
                result={
                    "batch_id": batch_id,
                    "processed": processed,
                    "updated": updated,
                    "unchanged": unchanged,
                    "failed": failed,
                },
                error=str(exc) or exc.__class__.__name__,
            )
            raise

        if updated:
            self._refresh_tag_catalog()
            try:
                self.repository.optimize()
            except Exception as exc:
                warnings.append(
                    "Tag updates were saved, but Collection optimization failed: "
                    f"{str(exc) or exc.__class__.__name__}"
                )
                needs_attention = True

        status = (
            "partial"
            if failed and updated
            else "failed"
            if failed
            else "no_changes"
            if not updated
            else "applied"
        )
        result = {
            "batch_id": batch_id,
            "operation": normalized_operation,
            "selection": selection_view,
            "selected": total,
            "processed": processed,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
            "failures": failures,
            "failures_truncated": failed > len(failures),
            "warnings": warnings,
            "needs_attention": needs_attention,
            "undo_available": updated > 0,
            "api_requests": 0,
        }
        self.state.finish_manual_tag_batch(
            batch_id,
            status=status,
            processed=processed,
            updated=updated,
            unchanged=unchanged,
            failed=failed,
            result=result,
        )
        return result

    def undo_latest_manual_tag_batch(self) -> dict[str, Any]:
        """Restore both SQLite and Zvec for the most recent manual batch."""

        latest = self.state.latest_manual_tag_batch()
        if latest is None:
            return {
                "undone": False,
                "already_undone": False,
                "restored": 0,
                "failed": 0,
                "failures": [],
                "undo_available": False,
                "api_requests": 0,
            }
        batch_id = str(latest["batch_id"])
        if str(latest["status"]) == "undone":
            return {
                "batch_id": batch_id,
                "undone": False,
                "already_undone": True,
                "restored": 0,
                "failed": 0,
                "failures": [],
                "undo_available": False,
                "api_requests": 0,
            }

        retryable_statuses = ("applied", "undo_failed", "conflict")
        total = self.state.count_manual_tag_batch_entries(
            batch_id,
            statuses=retryable_statuses,
        )
        self.state.finish_manual_tag_undo(
            batch_id,
            status="undoing",
            result={"batch_id": batch_id, "restored": 0, "failed": 0},
        )
        restored = 0
        failed = 0
        failures: list[dict[str, str]] = []
        needs_attention = False
        after_doc_id = ""
        try:
            while True:
                history = self.state.manual_tag_batch_entries(
                    batch_id,
                    statuses=retryable_statuses,
                    limit=128,
                    after_doc_id=after_doc_id,
                )
                if not history:
                    break
                after_doc_id = str(history[-1]["doc_id"])
                current = self.state.get_many(str(item["doc_id"]) for item in history)
                plans: list[_ManualTagPlan] = []
                outcomes: list[tuple[str, str, str]] = []
                for snapshot in history:
                    doc_id = str(snapshot["doc_id"])
                    entry = current.get(doc_id)
                    if entry is None:
                        error = "The indexed image no longer exists."
                        failed += 1
                        outcomes.append((doc_id, "undo_failed", error))
                        _append_manual_failure(
                            failures,
                            doc_id=doc_id,
                            relative_path="",
                            error=error,
                        )
                        continue
                    current_tags = normalize_tags(entry.get("tags", ()))
                    expected = normalize_tags(snapshot.get("after_tags", ()))
                    if current_tags != expected:
                        error = (
                            "Manual tags changed after this batch; undo did not "
                            "overwrite the newer edit."
                        )
                        failed += 1
                        outcomes.append((doc_id, "conflict", error))
                        _append_manual_failure(
                            failures,
                            doc_id=doc_id,
                            relative_path=str(entry.get("relative_path") or ""),
                            error=error,
                        )
                        continue
                    plans.append(
                        _ManualTagPlan(
                            entry,
                            current_tags,
                            normalize_tags(snapshot.get("before_tags", ())),
                        )
                    )

                if plans:
                    succeeded, plan_failures, inconsistent = (
                        self._apply_manual_tag_plans(plans)
                    )
                    needs_attention = needs_attention or inconsistent
                    succeeded_set = set(succeeded)
                    for plan in plans:
                        doc_id = str(plan.entry["doc_id"])
                        if doc_id in succeeded_set:
                            restored += 1
                            outcomes.append((doc_id, "undone", ""))
                        else:
                            error = plan_failures.get(
                                doc_id, "The manual tag undo failed."
                            )
                            failed += 1
                            outcomes.append((doc_id, "undo_failed", error))
                            _append_manual_failure(
                                failures,
                                doc_id=doc_id,
                                relative_path=str(
                                    plan.entry.get("relative_path") or ""
                                ),
                                error=error,
                            )
                self.state.update_manual_tag_batch_entries(batch_id, outcomes)
                self.progress(
                    f"Restored manual tags {restored + failed}/{total} images."
                )
                self.cancel_check()
        except BaseException as exc:
            self.state.finish_manual_tag_undo(
                batch_id,
                status="undo_failed",
                result={
                    "batch_id": batch_id,
                    "restored": restored,
                    "failed": failed,
                },
                error=str(exc) or exc.__class__.__name__,
            )
            raise

        if restored:
            self._refresh_tag_catalog()
            try:
                self.repository.optimize()
            except Exception as exc:
                needs_attention = True
                _append_manual_failure(
                    failures,
                    doc_id="",
                    relative_path="",
                    error=(
                        "Tags were restored, but Collection optimization failed: "
                        f"{str(exc) or exc.__class__.__name__}"
                    ),
                )
        remaining = self.state.count_manual_tag_batch_entries(
            batch_id,
            statuses=retryable_statuses,
        )
        result = {
            "batch_id": batch_id,
            "undone": remaining == 0,
            "already_undone": False,
            "restored": restored,
            "failed": failed,
            "failures": failures,
            "failures_truncated": failed > len(failures),
            "needs_attention": needs_attention,
            "undo_available": remaining > 0,
            "api_requests": 0,
        }
        self.state.finish_manual_tag_undo(
            batch_id,
            status="undone" if remaining == 0 else "undo_partial",
            result=result,
        )
        return result

    def _manual_tag_selection(
        self,
        *,
        library_id: str,
        selection: Mapping[str, Any],
    ) -> tuple[
        dict[str, Any],
        int,
        Iterator[list[tuple[str, dict[str, Any] | None]]],
    ]:
        if not isinstance(selection, Mapping):
            raise ValueError("selection must be an object.")
        mode = str(selection.get("mode") or "").strip().lower()
        if mode == "selected":
            raw_ids = selection.get("doc_ids")
            if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Sequence):
                raise ValueError("selected mode requires a doc_ids array.")
            if not all(isinstance(value, str) for value in raw_ids):
                raise ValueError("doc_ids must contain strings.")
            doc_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids))
            if not doc_ids or any(not value for value in doc_ids):
                raise ValueError("doc_ids must contain non-empty document ids.")
            if len(doc_ids) > 10_000:
                raise ValueError("doc_ids can contain at most 10000 items.")

            def selected_chunks() -> Iterator[list[tuple[str, dict[str, Any] | None]]]:
                for offset in range(0, len(doc_ids), 128):
                    chunk = doc_ids[offset : offset + 128]
                    entries = self.state.get_many(chunk)
                    yield [(doc_id, entries.get(doc_id)) for doc_id in chunk]

            return (
                {"mode": "selected", "doc_ids": doc_ids},
                len(doc_ids),
                selected_chunks(),
            )
        if mode != "folder":
            raise ValueError("selection.mode must be selected or folder.")
        folder_key = selection.get("folder_key")
        if not isinstance(folder_key, str) or not folder_key.strip():
            raise ValueError("folder selection requires folder_key.")
        include_subfolders = selection.get("include_subfolders", False)
        if not isinstance(include_subfolders, bool):
            raise ValueError("include_subfolders must be a boolean.")
        raw_excluded = selection.get("excluded_doc_ids", ())
        if isinstance(raw_excluded, (str, bytes)) or not isinstance(
            raw_excluded, Sequence
        ):
            raise ValueError("excluded_doc_ids must be an array.")
        if not all(isinstance(value, str) for value in raw_excluded):
            raise ValueError("excluded_doc_ids must contain strings.")
        excluded = {str(value).strip() for value in raw_excluded if str(value).strip()}
        if len(excluded) > 10_000:
            raise ValueError("excluded_doc_ids can contain at most 10000 items.")
        with LibraryBrowser(
            library_id=library_id,
            state_path=self.config.state_path,
        ) as browser:
            root_id, relative_folder = browser.decode_folder_key(folder_key)
        base_total = self.state.count_folder_entries(
            root_id,
            relative_folder,
            include_subfolders=include_subfolders,
        )
        excluded_entries = self.state.get_many(excluded)
        excluded_in_scope = {
            doc_id
            for doc_id, entry in excluded_entries.items()
            if _entry_is_in_folder(
                entry,
                root_id=root_id,
                relative_folder=relative_folder,
                include_subfolders=include_subfolders,
            )
        }

        def folder_chunks() -> Iterator[list[tuple[str, dict[str, Any] | None]]]:
            for entries in self.state.iter_folder_entries(
                root_id,
                relative_folder,
                include_subfolders=include_subfolders,
                chunk_size=128,
            ):
                selected: list[tuple[str, dict[str, Any] | None]] = [
                    (str(entry["doc_id"]), entry)
                    for entry in entries
                    if str(entry["doc_id"]) not in excluded
                ]
                if selected:
                    yield selected

        return (
            {
                "mode": "folder",
                "folder_key": folder_key,
                "root_id": root_id,
                "relative_folder": relative_folder,
                "include_subfolders": include_subfolders,
                "excluded_count": len(excluded_in_scope),
            },
            max(0, base_total - len(excluded_in_scope)),
            folder_chunks(),
        )

    def _apply_manual_tag_plans(
        self,
        plans: Sequence[_ManualTagPlan],
    ) -> tuple[list[str], dict[str, str], bool]:
        items = [
            (
                _record_from_state_entry(plan.entry),
                normalize_tags(
                    [
                        *plan.after_tags,
                        *plan.entry.get("folder_tags", ()),
                        *plan.entry.get("accepted_auto_tags", ()),
                        *plan.entry.get("inherited_tags", ()),
                    ]
                ),
            )
            for plan in plans
        ]
        succeeded, failures = self._repository_update_record_tags(items)
        if not succeeded:
            return [], failures, False
        succeeded_set = set(succeeded)
        state_entries = [
            {**plan.entry, "tags": list(plan.after_tags)}
            for plan in plans
            if str(plan.entry["doc_id"]) in succeeded_set
        ]
        try:
            self.state.set_many(state_entries)
        except Exception as exc:
            state_error = str(exc) or exc.__class__.__name__
            rollback_items = [
                (
                    _record_from_state_entry(plan.entry),
                    normalize_tags(
                        [
                            *plan.before_tags,
                            *plan.entry.get("folder_tags", ()),
                            *plan.entry.get("accepted_auto_tags", ()),
                            *plan.entry.get("inherited_tags", ()),
                        ]
                    ),
                )
                for plan in plans
                if str(plan.entry["doc_id"]) in succeeded_set
            ]
            rolled_back, rollback_failures = self._repository_update_record_tags(
                rollback_items
            )
            rolled_back_set = set(rolled_back)
            inconsistent = False
            for doc_id in succeeded:
                if doc_id in rolled_back_set:
                    failures[doc_id] = (
                        f"SQLite update failed and was rolled back: {state_error}"
                    )
                else:
                    inconsistent = True
                    rollback_error = rollback_failures.get(
                        doc_id, "Collection rollback failed."
                    )
                    failures[doc_id] = (
                        f"SQLite update failed ({state_error}); {rollback_error}"
                    )
            return [], failures, inconsistent
        return succeeded, failures, False

    def _repository_update_record_tags(
        self,
        items: Sequence[tuple[ImageRecord, Iterable[str]]],
    ) -> tuple[list[str], dict[str, str]]:
        updater = getattr(self.repository, "update_record_tags", None)
        if callable(updater):
            return updater(items)
        succeeded: list[str] = []
        failures: dict[str, str] = {}
        for record, tags in items:
            try:
                vector = self.repository.fetch_vector(record.doc_id)
                if vector is None:
                    raise ConfigurationError("The indexed image vector is missing.")
                stored, errors = self.repository.upsert_records([record], vector, tags)
                if stored == [record.doc_id] and not errors:
                    succeeded.append(record.doc_id)
                else:
                    failures[record.doc_id] = errors.get(
                        record.doc_id, "Collection tag update failed."
                    )
            except Exception as exc:
                failures[record.doc_id] = str(exc) or exc.__class__.__name__
        return succeeded, failures

    def index_folder(
        self,
        folder_path: str,
        recursive: bool = True,
        verify_hash: bool = False,
        tags: Iterable[str] | None = None,
    ) -> IndexReport:
        return self._index(
            folder_path,
            recursive=recursive,
            sync_deleted=False,
            verify_hash=verify_hash,
            dry_run=False,
            allow_scope_change=False,
            tags=tags,
        )

    def index_and_auto_tag_folder(
        self,
        folder_path: str,
        *,
        recursive: bool = True,
        verify_hash: bool = False,
        tags: Iterable[str] | None = None,
        model: str | None = None,
        max_images: int = 200,
        max_budget_cny: float | None = None,
        external_processing_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Index new images while primary visual-model requests run in parallel."""

        metrics = _PipelineNetworkMetrics()
        session: StreamingAutoTagSession = self.auto_tagging.begin_stream(
            model=model,
            max_images=max_images,
            max_budget_cny=max_budget_cny,
            external_processing_confirmed=external_processing_confirmed,
            activity_callback=metrics.set_flash_active,
        )
        try:
            index_report = self._index(
                folder_path,
                recursive=recursive,
                sync_deleted=False,
                verify_hash=verify_hash,
                dry_run=False,
                allow_scope_change=False,
                tags=tags,
                inserted_commit_callback=session.offer,
                network_metrics=metrics,
            )
            auto_tag_report = session.finish()
        except BaseException:
            session.abort()
            raise

        index_payload = index_report.to_dict()
        failed = int(index_payload.get("failed") or 0) + int(
            auto_tag_report.get("failed") or 0
        )
        needs_attention = bool(index_payload.get("needs_attention")) or bool(
            auto_tag_report.get("needs_attention")
        )
        failure_manifests = [
            str(path)
            for path in (
                index_payload.get("failure_manifest"),
                auto_tag_report.get("failure_manifest"),
            )
            if path
        ]
        pipeline = {
            **metrics.snapshot(),
            "primary_model": session.selected_model,
            "overlap_basis": "model_call",
            "flash_queue_capacity": session.max_pending,
            "flash_peak_pending": session.peak_pending,
            "flash_peak_pending_bytes": session.peak_pending_bytes,
            "max_inflight_request_bytes": self.config.max_inflight_request_bytes,
            "inserted_offered": session.candidate_count,
        }
        return {
            "failed": failed,
            "needs_attention": needs_attention,
            "failure_manifest": failure_manifests[0] if failure_manifests else "",
            "failure_manifests": failure_manifests,
            "quarantined": int(index_payload.get("quarantined") or 0)
            + int(auto_tag_report.get("quarantined") or 0),
            "quarantine_copy_failures": int(
                index_payload.get("quarantine_copy_failures") or 0
            )
            + int(auto_tag_report.get("quarantine_copy_failures") or 0),
            "index": index_payload,
            "auto_tag": auto_tag_report,
            "pipeline": pipeline,
        }

    def sync_folder(
        self,
        folder_path: str,
        recursive: bool = True,
        verify_hash: bool = False,
        dry_run: bool = False,
        allow_scope_change: bool = False,
    ) -> IndexReport:
        return self._index(
            folder_path,
            recursive=recursive,
            sync_deleted=True,
            verify_hash=verify_hash,
            dry_run=dry_run,
            allow_scope_change=allow_scope_change,
            tags=None,
        )

    def _index(
        self,
        folder_path: str,
        recursive: bool,
        sync_deleted: bool,
        verify_hash: bool,
        dry_run: bool,
        allow_scope_change: bool,
        tags: Iterable[str] | None,
        inserted_commit_callback: (
            Callable[[str, list[dict[str, Any]]], None] | None
        ) = None,
        network_metrics: _PipelineNetworkMetrics | None = None,
    ) -> IndexReport:
        root = Path(folder_path).expanduser().resolve()
        normalized_root = normalize_path(root)
        root_info = self.state.root_for_path(normalized_root)
        overlap = self.state.find_overlapping_root(normalized_root)
        if root_info is None and overlap:
            raise ConfigurationError(
                "The folder overlaps an already indexed root: "
                f"{overlap['current_path']}. "
                "Index one parent root or use a separate workspace."
            )

        previous_scope = bool(root_info["recursive"]) if root_info else None
        if (
            sync_deleted
            and previous_scope is not None
            and previous_scope != recursive
            and not allow_scope_change
        ):
            raise ConfigurationError(
                "The sync recursion scope differs from the original index. "
                "Use the original scope or pass --allow-scope-change explicitly."
            )
        root_id = (
            str(root_info["root_id"])
            if root_info
            else self.state.ensure_root(normalized_root, recursive)
        )
        requested_tags = normalize_tags(tags) if tags is not None else None
        clear_tags = requested_tags == ()
        new_document_tags = requested_tags or ()
        index_run_id = (
            ""
            if sync_deleted or dry_run
            else self.state.begin_index_run(root_id, normalized_root)
        )
        report = IndexReport(
            root_path=normalized_root,
            collection=str(self.config.collection_path),
            index_run_id=index_run_id,
        )
        failure_sink = (
            None
            if dry_run
            else IndexFailureSink(
                self.config.results_path,
                index_run_id or uuid.uuid4().hex,
                root,
            )
        )

        def capture_scan_failure(failure: FileFailure) -> None:
            self._record_index_failure(
                report,
                failure_sink,
                path=failure.path,
                error=failure.error,
                kind=failure.kind,
                stage=failure.stage or "scan",
                sha256_hex=failure.sha256,
                quarantine=Path(failure.path).is_file(),
            )

        self.progress(f"Scanning: {root}")
        self.cancel_check()
        self.logger.info(
            "index_start recursive=%s sync=%s verify_hash=%s tags=%d clear_tags=%s",
            recursive,
            sync_deleted,
            verify_hash,
            len(new_document_tags),
            clear_tags,
        )
        scan = scan_folder(
            root,
            root_id,
            recursive=recursive,
            previous_lookup=self.state.get,
            verify_hash=verify_hash,
            cancel_check=self.cancel_check,
            max_workers=self.config.scan_concurrency,
            # A user may deliberately place the application results directory
            # below the indexed root. Excluding the whole owned directory keeps
            # manifests, quarantined blobs, and copied search results out.
            excluded_roots=(self.config.results_path,),
            failure_handler=capture_scan_failure,
        )
        self.cancel_check()
        report.scanned = scan.scanned
        report.supported = scan.supported
        report.skipped = scan.skipped
        report.scan_peak_in_flight = scan.peak_in_flight
        report.warnings.extend(scan.warnings)
        # Test doubles and older scanner implementations may return failures
        # without invoking the streaming handler.
        if scan.failure_count == 0:
            for failure in scan.failures:
                capture_scan_failure(failure)
        if self.state_reset:
            report.warnings.append(
                "The Collection identity changed; stale incremental state was reset."
            )
        self.progress(
            f"Found {len(scan.records)} valid images; skipped {scan.skipped}; "
            f"invalid {scan.failure_count or len(scan.failures)}."
        )

        groups: dict[str, list[ImageRecord]] = defaultdict(list)
        for record in scan.records:
            self.cancel_check()
            existing = self.state.get(record.doc_id)
            record_state = record.state_dict()
            folder_tags = folder_tags_for_image(Path(record.absolute_path), root)
            file_unchanged = existing is not None and all(
                existing.get(key) == value for key, value in record_state.items()
            )
            folder_tags_unchanged = (
                existing is not None
                and tuple(existing.get("folder_tags", ())) == folder_tags
            )
            tags_need_clearing = (
                clear_tags and existing is not None and bool(existing.get("tags"))
            )
            if file_unchanged and folder_tags_unchanged and not tags_need_clearing:
                if self.validate_documents and not self.repository.contains(
                    record.doc_id
                ):
                    groups[record.sha256].append(record)
                else:
                    report.unchanged += 1
                continue
            groups[record.sha256].append(record)

        reusable: list[tuple[list[ImageRecord], list[float]]] = []
        pending: list[tuple[str, list[ImageRecord]]] = []
        for content_hash, records in groups.items():
            self.cancel_check()
            cached_doc_id = self.state.find_doc_id_by_sha(content_hash)
            cached_vector = (
                self.repository.fetch_vector(cached_doc_id) if cached_doc_id else None
            )
            if cached_vector is not None:
                reusable.append((records, cached_vector))
            else:
                pending.append((content_hash, records))

        reusable_halted = False
        for reusable_index, (records, vector) in enumerate(reusable):
            self.cancel_check()
            outcome: _UpsertResult | None = None
            try:
                outcome = self._upsert_group(
                    records,
                    vector,
                    new_document_tags,
                    clear_tags,
                    report,
                    root,
                    index_run_id,
                    failure_sink,
                )
            except Exception as exc:
                self.logger.exception("reused_vector_commit_failed")
                self._record_index_failure(
                    report,
                    failure_sink,
                    path=records[0].absolute_path,
                    error=f"Index storage commit failed: {exc}",
                    kind="systemic",
                    stage="index_commit",
                    sha256_hex=records[0].sha256,
                    quarantine=False,
                )
            if (
                outcome is not None
                and outcome.inserted_entries
                and inserted_commit_callback is not None
            ):
                inserted_commit_callback(records[0].sha256, outcome.inserted_entries)
            committed = outcome is not None and outcome.committed
            if not committed:
                reusable_halted = True
                report.deferred += len(records)
                report.deferred += sum(
                    len(remaining_records)
                    for remaining_records, _vector in reusable[reusable_index + 1 :]
                )
                report.deferred += sum(
                    len(pending_records) for _sha, pending_records in pending
                )
                break

        embeddable: list[tuple[str, list[ImageRecord]]] = []
        for content_hash, records in [] if reusable_halted else pending:
            self.cancel_check()
            representative = records[0]
            if representative.size_bytes > self.config.max_source_image_bytes:
                message = (
                    f"image exceeds local safety limit of "
                    f"{self.config.max_source_image_bytes} bytes"
                )
                for record in records:
                    self._record_index_failure(
                        report,
                        failure_sink,
                        path=record.absolute_path,
                        error=message,
                        kind="item",
                        stage="embedding_preflight",
                        sha256_hex=record.sha256,
                    )
            elif not _record_is_stable(representative):
                for record in records:
                    self._record_index_failure(
                        report,
                        failure_sink,
                        path=record.absolute_path,
                        error="file changed after scanning; run index again",
                        kind="item",
                        stage="embedding_preflight",
                    )
            else:
                embeddable.append((content_hash, records))

        self._run_embedding_pipeline(
            embeddable,
            new_document_tags,
            clear_tags,
            report,
            root,
            index_run_id,
            failure_sink,
            inserted_commit_callback=inserted_commit_callback,
            network_metrics=network_metrics,
        )

        if sync_deleted:
            stale_ids = sorted(
                self.state.ids_for_root(root_id) - scan.seen_supported_ids
            )
            report.would_delete = len(stale_ids)
            if report.needs_attention:
                report.sync_aborted = True
                report.warnings.append(
                    "Deletion was skipped because indexing needs storage or "
                    "credential attention."
                )
            elif not scan.complete:
                report.sync_aborted = True
                report.warnings.append(
                    "Deletion was skipped because one or more directories "
                    "could not be scanned."
                )
            elif dry_run:
                report.warnings.append("Dry run: no stale records were deleted.")
            else:
                for chunk in _chunks(stale_ids, 256):
                    self.cancel_check()
                    try:
                        succeeded, failures = self.repository.delete(chunk)
                        self.state.remove_many(succeeded)
                    except Exception as exc:
                        self.logger.exception("sync_delete_failed")
                        self._record_index_failure(
                            report,
                            failure_sink,
                            path=str(root),
                            error=f"Index storage delete failed: {exc}",
                            kind="systemic",
                            stage="sync_delete",
                            quarantine=False,
                        )
                        report.deferred += len(chunk)
                        break
                    report.deleted += len(succeeded)
                    for doc_id, error in failures.items():
                        self._record_index_failure(
                            report,
                            failure_sink,
                            path=doc_id,
                            error=error,
                            kind="systemic",
                            stage="sync_delete",
                            quarantine=False,
                        )

        if report.inserted or report.updated or report.deleted:
            try:
                self._refresh_tag_catalog()
                self.cancel_check()
                self.progress("Optimizing Zvec indexes...")
                self.repository.optimize()
            except Exception as exc:
                self.logger.exception("index_optimize_failed")
                self._record_index_failure(
                    report,
                    failure_sink,
                    path=str(root),
                    error=f"Index optimization failed: {exc}",
                    kind="systemic",
                    stage="index_optimize",
                    quarantine=False,
                )

        if previous_scope is None:
            self.state.record_root(root_id, normalized_root, recursive)
        elif recursive and not previous_scope:
            self.state.record_root(root_id, normalized_root, True)
        elif sync_deleted and allow_scope_change and scan.complete and not dry_run:
            self.state.record_root(root_id, normalized_root, recursive)
        if not dry_run:
            self._clear_legacy_root_tags()

        if requested_tags and report.inserted == 0:
            report.warnings.append(
                "No new documents were indexed; requested tags were not applied."
            )

        if index_run_id:
            try:
                self.state.finish_index_run(
                    index_run_id,
                    status=(
                        "partial"
                        if report.failed or report.deferred or report.needs_attention
                        else "succeeded"
                    ),
                    inserted=report.inserted,
                    updated=report.updated,
                    failed=report.failed,
                    deferred=report.deferred,
                    needs_attention=report.needs_attention,
                    failure_manifest=report.failure_manifest,
                )
            except Exception as exc:
                self.logger.exception("index_run_finish_failed")
                self._record_index_failure(
                    report,
                    failure_sink,
                    path=str(root),
                    error=f"Could not finalize index run state: {exc}",
                    kind="systemic",
                    stage="index_run_finalize",
                    quarantine=False,
                )
        try:
            counts_differ = self.repository.doc_count != self.state.count()
            if counts_differ:
                report.warnings.append(
                    "Zvec and incremental-state counts differ; unchanged documents "
                    "were validated."
                )
            self.validate_documents = counts_differ
        except Exception as exc:
            self.logger.exception("index_count_validation_failed")
            self._record_index_failure(
                report,
                failure_sink,
                path=str(root),
                error=f"Could not validate index storage counts: {exc}",
                kind="systemic",
                stage="index_validation",
                quarantine=False,
            )
        self.logger.info(
            "index_complete scanned=%d inserted=%d updated=%d unchanged=%d "
            "deleted=%d failed=%d deferred=%d needs_attention=%s api_requests=%d",
            report.scanned,
            report.inserted,
            report.updated,
            report.unchanged,
            report.deleted,
            report.failed,
            report.deferred,
            report.needs_attention,
            report.api_requests,
        )
        return report

    def _embedding_batches(
        self, pending: list[tuple[str, list[ImageRecord]]]
    ) -> Iterable[list[tuple[str, list[ImageRecord]]]]:
        batch: list[tuple[str, list[ImageRecord]]] = []
        estimated_bytes = 0
        for item in pending:
            raw_size = item[1][0].size_bytes
            encoded_size = min(
                4 * ((raw_size + 2) // 3) + 256,
                self.config.max_image_bytes,
            )
            if batch and (
                len(batch) >= self.config.batch_size
                or estimated_bytes + encoded_size > self.config.max_request_bytes
            ):
                yield batch
                batch = []
                estimated_bytes = 0
            batch.append(item)
            estimated_bytes += encoded_size
        if batch:
            yield batch

    def _run_embedding_pipeline(
        self,
        pending: list[tuple[str, list[ImageRecord]]],
        new_document_tags: tuple[str, ...],
        clear_tags: bool,
        report: IndexReport,
        root: Path,
        index_run_id: str,
        failure_sink: IndexFailureSink | None,
        *,
        inserted_commit_callback: (
            Callable[[str, list[dict[str, Any]]], None] | None
        ) = None,
        network_metrics: _PipelineNetworkMetrics | None = None,
    ) -> None:
        if not pending:
            return

        batches = iter(self._embedding_batches(pending))
        next_batch = next(batches, None)
        active: dict[
            Future[_EmbeddingBatchResult],
            tuple[list[tuple[str, list[ImageRecord]]], int],
        ] = {}
        inflight_bytes = 0
        completed = 0
        halt_submission = False
        client = self.embedding_client
        request_count_before = getattr(client, "request_count", 0)

        executor = ThreadPoolExecutor(
            max_workers=self.config.embedding_concurrency,
            thread_name_prefix="zvec-image-embedding",
        )
        try:
            while next_batch is not None or active:
                self.cancel_check()
                while (
                    next_batch is not None
                    and not halt_submission
                    and len(active) < self.config.embedding_concurrency
                ):
                    estimated_bytes = _embedding_batch_bytes(next_batch)
                    if (
                        active
                        and inflight_bytes + estimated_bytes
                        > self.config.max_inflight_request_bytes
                    ):
                        break
                    future = executor.submit(
                        _embed_batch_with_activity,
                        client,
                        next_batch,
                        network_metrics,
                    )
                    active[future] = (next_batch, estimated_bytes)
                    inflight_bytes += estimated_bytes
                    report.embedding_peak_in_flight = max(
                        report.embedding_peak_in_flight,
                        len(active),
                    )
                    report.embedding_peak_bytes = max(
                        report.embedding_peak_bytes,
                        inflight_bytes,
                    )
                    next_batch = next(batches, None)

                if not active:
                    break

                done, _pending_futures = wait(
                    active,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                # Cancellation may arrive while ``wait`` is polling.  Check
                # again before a completed network result reaches the owner
                # thread's SQLite/Zvec commit path.
                self.cancel_check()
                for future in done:
                    submitted_batch, estimated_bytes = active.pop(future)
                    inflight_bytes -= estimated_bytes
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = _EmbeddingBatchResult(
                            systemic_error=str(exc) or exc.__class__.__name__,
                            systemic_items=submitted_batch,
                        )
                    halt_submission = (
                        self._apply_embedding_result(
                            result,
                            new_document_tags,
                            clear_tags,
                            report,
                            root,
                            index_run_id,
                            failure_sink,
                            inserted_commit_callback=inserted_commit_callback,
                        )
                        or halt_submission
                    )
                    completed += len(submitted_batch)
                    self.progress(f"Embedded {completed}/{len(pending)} unique images.")

            if halt_submission:
                remaining = []
                if next_batch is not None:
                    remaining.extend(next_batch)
                for batch in batches:
                    remaining.extend(batch)
                report.deferred += sum(len(records) for _sha, records in remaining)
        except BaseException:
            # Worker tasks perform only encoding/network work.  Do not let the
            # executor context manager wait for a stuck HTTP call during UI
            # cancellation, and deliberately discard every late result so it
            # can never enter the single-writer persistence path.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

        request_count_after = getattr(client, "request_count", request_count_before)
        report.api_requests += max(0, request_count_after - request_count_before)

    def _apply_embedding_result(
        self,
        result: _EmbeddingBatchResult,
        new_document_tags: tuple[str, ...],
        clear_tags: bool,
        report: IndexReport,
        root: Path,
        index_run_id: str,
        failure_sink: IndexFailureSink | None,
        *,
        inserted_commit_callback: (
            Callable[[str, list[dict[str, Any]]], None] | None
        ) = None,
    ) -> bool:
        report.usage.extend(result.usage)
        for failure in result.failures:
            for content_hash, records in failure.items:
                for record in records:
                    self._record_index_failure(
                        report,
                        failure_sink,
                        path=record.absolute_path,
                        error=failure.error,
                        kind=failure.kind,
                        stage="embedding",
                        sha256_hex=content_hash,
                        quarantine=failure.kind == "item",
                    )

        halt_submission = False
        for index, ((content_hash, records), vector) in enumerate(result.successes):
            if not _record_is_stable(records[0]):
                for record in records:
                    self._record_index_failure(
                        report,
                        failure_sink,
                        path=record.absolute_path,
                        error="file changed during embedding; run index again",
                        kind="item",
                        stage="embedding_commit",
                    )
                continue
            outcome: _UpsertResult | None = None
            try:
                outcome = self._upsert_group(
                    records,
                    vector,
                    new_document_tags,
                    clear_tags,
                    report,
                    root,
                    index_run_id,
                    failure_sink,
                )
            except Exception as exc:
                self.logger.exception("index_commit_failed sha256=%s", content_hash)
                self._record_index_failure(
                    report,
                    failure_sink,
                    path=records[0].absolute_path,
                    error=f"Index storage commit failed: {exc}",
                    kind="systemic",
                    stage="index_commit",
                    sha256_hex=content_hash,
                    quarantine=False,
                )
                report.deferred += sum(
                    len(remaining_records)
                    for (_remaining_hash, remaining_records), _remaining_vector in (
                        result.successes[index:]
                    )
                )
                halt_submission = True
                break
            assert outcome is not None
            if outcome.inserted_entries and inserted_commit_callback is not None:
                inserted_commit_callback(content_hash, outcome.inserted_entries)
            committed = outcome.committed
            if not committed:
                halt_submission = True
                report.deferred += len(records)
                report.deferred += sum(
                    len(remaining_records)
                    for (_remaining_hash, remaining_records), _remaining_vector in (
                        result.successes[index + 1 :]
                    )
                )
                break

        if result.systemic_error:
            representative = next(
                (
                    records[0]
                    for _content_hash, records in result.systemic_items
                    if records
                ),
                None,
            )
            self._record_index_failure(
                report,
                failure_sink,
                path=representative.absolute_path if representative else str(root),
                error=result.systemic_error,
                kind="systemic",
                stage="embedding",
                sha256_hex=representative.sha256 if representative else "",
                quarantine=False,
            )
            report.deferred += sum(
                len(records) for _sha, records in result.systemic_items
            )
            halt_submission = True
        return halt_submission

    def _upsert_group(
        self,
        records: list[ImageRecord],
        vector: list[float],
        new_document_tags: tuple[str, ...],
        clear_tags: bool,
        report: IndexReport,
        root: Path,
        index_run_id: str,
        failure_sink: IndexFailureSink | None,
    ) -> _UpsertResult:
        if len(vector) != self.config.dimension:
            self._record_index_failure(
                report,
                failure_sink,
                path=records[0].absolute_path,
                error=f"invalid vector dimension: {len(vector)}",
                kind="systemic",
                stage="embedding_response",
                sha256_hex=records[0].sha256,
                quarantine=False,
            )
            return _UpsertResult(False)

        previous = {record.doc_id: self.state.get(record.doc_id) for record in records}
        records_by_tags: dict[tuple[str, ...], list[ImageRecord]] = defaultdict(list)
        state_values: dict[str, dict[str, list[str]]] = {}
        for record in records:
            existing = previous[record.doc_id]
            manual_tags = (
                ()
                if clear_tags
                else new_document_tags
                if existing is None
                else tuple(existing.get("tags", ()))
            )
            folder_tags = folder_tags_for_image(Path(record.absolute_path), root)
            accepted_auto_tags = (
                tuple(existing.get("accepted_auto_tags", ()))
                if existing is not None and existing.get("sha256") == record.sha256
                else ()
            )
            inherited_tags = (
                tuple(existing.get("inherited_tags", ()))
                if existing is not None and existing.get("sha256") == record.sha256
                else ()
            )
            effective_tags = normalize_tags(
                [
                    *manual_tags,
                    *folder_tags,
                    *accepted_auto_tags,
                    *inherited_tags,
                ]
            )
            records_by_tags[effective_tags].append(record)
            state_values[record.doc_id] = {
                "tags": list(manual_tags),
                "folder_tags": list(folder_tags),
                "accepted_auto_tags": list(accepted_auto_tags),
                "inherited_tags": list(inherited_tags),
            }

        by_id = {record.doc_id: record for record in records}
        successful_entries: list[dict] = []
        inserted_ids: list[str] = []
        updated_ids: list[str] = []
        systemic_failure = False
        for effective_tags, tagged_records in records_by_tags.items():
            succeeded, failures = self.repository.upsert_records(
                tagged_records, vector, effective_tags
            )
            for doc_id in succeeded:
                record = by_id[doc_id]
                if previous[doc_id] is None:
                    report.inserted += 1
                    inserted_ids.append(doc_id)
                else:
                    report.updated += 1
                    updated_ids.append(doc_id)
                successful_entries.append(
                    {**record.state_dict(), **state_values[doc_id]}
                )
            for doc_id, error in failures.items():
                failure_kind = _classify_storage_error(error)
                systemic_failure = systemic_failure or failure_kind == "systemic"
                failed_record = by_id.get(doc_id)
                self._record_index_failure(
                    report,
                    failure_sink,
                    path=failed_record.absolute_path if failed_record else doc_id,
                    error=error,
                    kind=failure_kind,
                    stage="zvec_upsert",
                    sha256_hex=failed_record.sha256 if failed_record else "",
                    quarantine=failure_kind != "systemic",
                )
        self.state.set_many(successful_entries)
        if index_run_id:
            self.state.record_index_run_entries(index_run_id, inserted_ids, "inserted")
            self.state.record_index_run_entries(index_run_id, updated_ids, "updated")
        inserted_set = set(inserted_ids)
        return _UpsertResult(
            committed=not systemic_failure,
            inserted_entries=[
                entry
                for entry in successful_entries
                if str(entry.get("doc_id") or "") in inserted_set
            ],
        )

    def _record_index_failure(
        self,
        report: IndexReport,
        failure_sink: IndexFailureSink | None,
        *,
        path: str | Path,
        error: str,
        kind: FailureKind,
        stage: str,
        sha256_hex: str = "",
        quarantine: bool = True,
    ) -> None:
        if failure_sink is None:
            report.add_failure(
                FileFailure(
                    path=str(path),
                    error=str(error) or "Index operation failed.",
                    kind=kind,
                    stage=stage,
                    sha256=sha256_hex,
                )
            )
            return

        capture = failure_sink.capture(
            path=path,
            error=error,
            kind=kind,
            stage=stage,
            sha256_hex=sha256_hex,
            quarantine=quarantine,
        )
        report.add_failure(capture.failure)
        if capture.manifest_path:
            report.failure_manifest = capture.manifest_path
        if capture.copied:
            report.quarantined += 1
        if capture.needs_attention:
            report.needs_attention = True
            warning = "The failed-image store needs attention; see the failure details."
            if warning not in report.warnings:
                report.warnings.append(warning)

    def search_by_text(
        self,
        text: str,
        top_k: int = 10,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
        show_low_confidence: bool = False,
        diversify_results: bool = True,
        sort_mode: SearchSortMode = "confidence",
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
        resolved_sort_mode = normalize_sort_mode(sort_mode)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        text = text.strip()
        if not text:
            raise ValueError("Search text cannot be empty.")
        vector, source, request_id, usage = self._text_embedding(text)
        self.cancel_check()
        ranking = (
            self._configured_single_ranking(
                vector,
                query_type="text",
                top_k=top_k,
                tags=normalized_tags,
                tag_mode=tag_mode,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            if self.search_quality.configured and resolved_sort_mode != "legacy"
            else None
        )
        hybrid_intent = HybridTagIntent()
        metadata_search: dict[str, object] = {
            "enabled": False,
            "candidate_count": 0,
            "extra_embedding_requests": 0,
            "calibrated": False,
        }
        if ranking is not None:
            hits = ranking.hits
            ranking_mode = "confidence"
        else:
            hybrid_intent, hits, metadata_search = self._hybrid_text_candidates(
                text,
                vector,
                top_k=top_k,
                tags=normalized_tags,
                tag_mode=tag_mode,
            )
            ranking_mode = str(metadata_search.get("mode") or "distance")
            ranking = self._sort_request_hits(
                hits,
                query_type="text",
                top_k=top_k,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            hits = ranking.hits
        candidate_count = ranking.candidate_count
        diversity_enabled = sort_mode_uses_diversity(
            resolved_sort_mode,
            legacy_diversity=diversify_results,
        )
        diversity = diversify_search_hits(
            hits,
            top_k=top_k,
            enabled=diversity_enabled,
        )
        hits = diversity.hits
        report = export_results(
            self.config,
            query_type="text",
            hits=hits,
            top_k=top_k,
            query={
                "text": text,
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
                "show_low_confidence": show_low_confidence,
                "diversify_results": diversify_results,
                "sort_mode": resolved_sort_mode,
            },
            request_ids=[request_id],
            usage=[usage] if usage else [],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={"text": source},
            search_quality=self._search_diagnostics(
                show_low_confidence=show_low_confidence,
                low_confidence_override=(
                    ranking is not None and ranking.status == "low_confidence_override"
                ),
                hybrid_intent=hybrid_intent,
                metadata_search=metadata_search,
                diversity=diversity,
                sorting=ranking.diagnostics,
            ),
            status=ranking.status,
            candidate_count=candidate_count,
            filtered_count=ranking.filtered_count
            + max(0, len(ranking.hits) - len(hits)),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode=ranking_mode,
            sort_mode=resolved_sort_mode,
            ranking_diagnostics=ranking.diagnostics,
            show_low_confidence=show_low_confidence,
            low_confidence_override=(
                ranking is not None and ranking.status == "low_confidence_override"
            ),
        )
        self._log_search(report)
        return report

    def search_by_tags(
        self,
        text: str,
        top_k: int = 10,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
        show_low_confidence: bool = False,
        diversify_results: bool = True,
        sort_mode: SearchSortMode = "confidence",
    ) -> SearchReport:
        """Search locally by fuzzy tag fragments without creating an embedding."""

        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
        resolved_sort_mode = normalize_sort_mode(sort_mode)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        text = text.strip()
        if not text:
            raise ValueError("Tag search text cannot be empty.")

        candidates = self._tag_search_hits(
            text,
            tags=normalized_tags,
            tag_mode=tag_mode,
        )
        ranking = self._sort_request_hits(
            candidates,
            query_type="tag",
            top_k=top_k,
            show_low_confidence=show_low_confidence,
            sort_mode=resolved_sort_mode,
        )
        diversity_enabled = sort_mode_uses_diversity(
            resolved_sort_mode,
            legacy_diversity=diversify_results,
        )
        diversity = diversify_search_hits(
            ranking.hits,
            top_k=top_k,
            enabled=diversity_enabled,
        )
        hits = diversity.hits
        report = export_results(
            self.config,
            query_type="tag",
            hits=hits,
            top_k=top_k,
            query={
                "text": text,
                "search_mode": "tags",
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
                "show_low_confidence": show_low_confidence,
                "diversify_results": diversify_results,
                "sort_mode": resolved_sort_mode,
            },
            request_ids=[],
            usage=[],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={},
            search_quality={
                "configured": False,
                "ranking_mode": "tag_match",
                "tag_only": True,
                "fuzzy": True,
                "diversity": diversity.diagnostics(),
                "sorting": ranking.diagnostics,
            },
            status=ranking.status,
            candidate_count=ranking.candidate_count,
            filtered_count=ranking.filtered_count
            + max(0, len(ranking.hits) - len(hits)),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode="tag_match",
            sort_mode=resolved_sort_mode,
            ranking_diagnostics=ranking.diagnostics,
            show_low_confidence=show_low_confidence,
        )
        self._log_search(report)
        return report

    def estimate_auto_tags(
        self,
        *,
        scope: str = "untagged",
        model: str | None = None,
        max_images: int = 200,
        max_budget_cny: float | None = None,
    ) -> dict[str, object]:
        return self.auto_tagging.estimate(
            scope=scope,
            model=model,
            max_images=max_images,
            max_budget_cny=max_budget_cny,
        )

    def pending_auto_tags(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        aliases = TagAliasDictionary.load(
            self.config.results_path / "tag-aliases.json",
            missing_ok=True,
        )
        return self.auto_tagging.pending(
            offset=offset,
            limit=limit,
            filters=filters,
            aliases=aliases,
        )

    def auto_tag_images(
        self,
        *,
        scope: str = "untagged",
        model: str | None = None,
        max_images: int = 200,
        max_budget_cny: float | None = None,
        external_processing_confirmed: bool = False,
    ) -> dict[str, object]:
        return self.auto_tagging.run(
            scope=scope,
            model=model,
            max_images=max_images,
            max_budget_cny=max_budget_cny,
            external_processing_confirmed=external_processing_confirmed,
        )

    def review_auto_tags(
        self, decisions: Iterable[dict[str, object]]
    ) -> dict[str, object]:
        return self.auto_tagging.review(decisions)

    def reconcile_pending_identity_tags(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        return self.auto_tagging.reconcile_pending_identity_tags(dry_run=dry_run)

    def review_auto_tags_batch(
        self,
        *,
        proposal_ids: Iterable[str],
        accepted_tags_by_proposal: Mapping[str, object] | None = None,
        exclude_identity_tags: bool = True,
        acceptance_mode: str = "low_risk_only",
        batch_confirmation: bool = False,
    ) -> dict[str, object]:
        return self.auto_tagging.review_batch(
            proposal_ids=proposal_ids,
            accepted_tags_by_proposal=accepted_tags_by_proposal,
            exclude_identity_tags=exclude_identity_tags,
            acceptance_mode=acceptance_mode,
            batch_confirmation=batch_confirmation,
        )

    def undo_latest_auto_tag_review_batch(self) -> dict[str, object]:
        return self.auto_tagging.undo_latest_review_batch()

    def search_by_image(
        self,
        image_path: str,
        top_k: int = 10,
        include_self: bool = False,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
        show_low_confidence: bool = False,
        diversify_results: bool = True,
        sort_mode: SearchSortMode = "confidence",
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
        resolved_sort_mode = normalize_sort_mode(sort_mode)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        path = inspect_query_image(image_path)
        vector, source, request_id, usage, image_hash = self._image_embedding(path)
        self.cancel_check()
        exclude = None if include_self else str(path)
        ranking = (
            self._configured_single_ranking(
                vector,
                query_type="image",
                top_k=top_k,
                exclude_sha256=None if include_self else image_hash,
                tags=normalized_tags,
                tag_mode=tag_mode,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            if self.search_quality.configured and resolved_sort_mode != "legacy"
            else None
        )
        hits = (
            ranking.hits
            if ranking is not None
            else self._query_until_sufficient(
                vector,
                top_k,
                exclude_path=exclude,
                exclude_sha256=None if include_self else image_hash,
                tags=normalized_tags,
                tag_mode=tag_mode,
                quality_mode="image",
                rank_source="image",
            )
        )
        ranking_mode = "confidence" if ranking is not None else "distance"
        if ranking is None:
            ranking = self._sort_request_hits(
                hits,
                query_type="image",
                top_k=top_k,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            hits = ranking.hits
        candidate_count = ranking.candidate_count
        diversity_enabled = sort_mode_uses_diversity(
            resolved_sort_mode,
            legacy_diversity=diversify_results,
        )
        diversity = diversify_search_hits(
            hits,
            top_k=top_k,
            enabled=diversity_enabled,
        )
        hits = diversity.hits
        report = export_results(
            self.config,
            query_type="image",
            hits=hits,
            top_k=top_k,
            query={
                "image": path.name,
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
                "show_low_confidence": show_low_confidence,
                "diversify_results": diversify_results,
                "sort_mode": resolved_sort_mode,
            },
            request_ids=[request_id],
            usage=[usage] if usage else [],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={"image": source},
            exclude_path=exclude,
            search_quality=self._search_diagnostics(
                show_low_confidence=show_low_confidence,
                low_confidence_override=(
                    ranking is not None and ranking.status == "low_confidence_override"
                ),
                diversity=diversity,
                sorting=ranking.diagnostics,
            ),
            status=ranking.status,
            candidate_count=candidate_count,
            filtered_count=ranking.filtered_count
            + max(0, len(ranking.hits) - len(hits)),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode=ranking_mode,
            sort_mode=resolved_sort_mode,
            ranking_diagnostics=ranking.diagnostics,
            show_low_confidence=show_low_confidence,
            low_confidence_override=(
                ranking is not None and ranking.status == "low_confidence_override"
            ),
        )
        self._log_search(report)
        return report

    def search_by_image_and_text(
        self,
        image_path: str,
        text: str,
        top_k: int = 10,
        image_weight: float = 0.5,
        text_weight: float = 0.5,
        include_self: bool = False,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
        show_low_confidence: bool = False,
        diversify_results: bool = True,
        sort_mode: SearchSortMode = "confidence",
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
        resolved_sort_mode = normalize_sort_mode(sort_mode)
        self._validate_weights(image_weight, text_weight)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        text = text.strip()
        if not text:
            raise ValueError("Search text cannot be empty.")
        path = inspect_query_image(image_path)
        image_key, image_vector, image_source, image_hash = self._cached_image(path)
        text_key = self._cache_key("text", text)
        text_vector = self.state.get_cached_vector(text_key, self.config.dimension)
        text_source = "cache" if text_vector is not None else None
        responses: dict[str, EmbeddingResponse] = {}
        missing = []
        if image_vector is None:
            missing.append("image")
        if text_vector is None:
            missing.append("text")
        if len(missing) == 2:
            with ThreadPoolExecutor(max_workers=2) as executor:
                image_future = executor.submit(
                    self.embedding_client.embed_images, [path]
                )
                text_future = executor.submit(self.embedding_client.embed_text, text)
                responses["image"] = image_future.result()
                responses["text"] = text_future.result()
        elif missing == ["image"]:
            responses["image"] = self.embedding_client.embed_images([path])
        elif missing == ["text"]:
            responses["text"] = self.embedding_client.embed_text(text)

        if "image" in responses:
            self._ensure_query_image_unchanged(path, image_hash)
            image_vector = responses["image"].vectors[0]
            self._cache_vector(image_key, "image", image_vector)
            image_source = "api"
        if "text" in responses:
            text_vector = responses["text"].vectors[0]
            self._cache_vector(text_key, "text", text_vector)
            text_source = "api"
        assert image_vector is not None and text_vector is not None
        self.cancel_check()
        exclude = None if include_self else str(path)
        ranking = (
            self._configured_combined_ranking(
                image_vector,
                text_vector,
                top_k=top_k,
                image_weight=image_weight,
                text_weight=text_weight,
                exclude_sha256=None if include_self else image_hash,
                tags=normalized_tags,
                tag_mode=tag_mode,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            if self.search_quality.configured and resolved_sort_mode != "legacy"
            else None
        )
        metadata_search: dict[str, object] = {
            "enabled": False,
            "candidate_count": 0,
            "extra_embedding_requests": 0,
            "calibrated": False,
        }
        if ranking is not None:
            fused_hits = ranking.hits
            ranking_mode = (
                self.search_quality.fusion_mode
                if self.search_quality.fusion_mode == "confidence_v2"
                else "confidence"
            )
        else:
            fused_hits, metadata_search = self._query_fused_until_sufficient(
                image_vector,
                text_vector,
                top_k,
                image_weight,
                text_weight,
                exclude,
                None if include_self else image_hash,
                normalized_tags,
                tag_mode,
            )
            ranking_mode = str(metadata_search.get("mode") or "weighted_rrf")
            ranking = self._sort_request_hits(
                fused_hits,
                query_type="image_text",
                top_k=top_k,
                show_low_confidence=show_low_confidence,
                sort_mode=resolved_sort_mode,
            )
            fused_hits = ranking.hits
        candidate_count = ranking.candidate_count
        diversity_enabled = sort_mode_uses_diversity(
            resolved_sort_mode,
            legacy_diversity=diversify_results,
        )
        diversity = diversify_search_hits(
            fused_hits,
            top_k=top_k,
            enabled=diversity_enabled,
        )
        fused_hits = diversity.hits
        report = export_results(
            self.config,
            query_type="image_text",
            hits=fused_hits,
            top_k=top_k,
            query={
                "image": path.name,
                "text": text,
                "image_weight": image_weight,
                "text_weight": text_weight,
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
                "show_low_confidence": show_low_confidence,
                "diversify_results": diversify_results,
                "sort_mode": resolved_sort_mode,
            },
            request_ids=[response.request_id for response in responses.values()],
            usage=[response.usage for response in responses.values()],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={
                "image": image_source or "unknown",
                "text": text_source or "unknown",
            },
            exclude_path=exclude,
            search_quality=self._search_diagnostics(
                show_low_confidence=show_low_confidence,
                low_confidence_override=(
                    ranking is not None and ranking.status == "low_confidence_override"
                ),
                metadata_search=metadata_search,
                diversity=diversity,
                sorting=ranking.diagnostics,
            ),
            status=ranking.status,
            candidate_count=candidate_count,
            filtered_count=ranking.filtered_count
            + max(0, len(ranking.hits) - len(fused_hits)),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode=ranking_mode,
            sort_mode=resolved_sort_mode,
            ranking_diagnostics=ranking.diagnostics,
            show_low_confidence=show_low_confidence,
            low_confidence_override=(
                ranking is not None and ranking.status == "low_confidence_override"
            ),
        )
        self._log_search(report)
        return report

    def prepare_search_query(
        self,
        *,
        text: str | None = None,
        image_path: str | None = None,
        search_mode: str = "semantic",
    ) -> PreparedSearch:
        """Create reusable query vectors without querying this Collection."""
        self.cancel_check()
        if not isinstance(search_mode, str):
            raise ValueError("search_mode must be 'semantic' or 'tags'.")
        search_mode = search_mode.strip().lower()
        if search_mode not in {"semantic", "tags"}:
            raise ValueError("search_mode must be 'semantic' or 'tags'.")
        normalized_text = text.strip() if text is not None else None
        if text is not None and not normalized_text:
            raise ValueError("Search text cannot be empty.")
        if search_mode == "tags":
            if image_path is not None:
                raise ValueError("Tag-only search does not accept a query image.")
            if normalized_text is None:
                raise ValueError("Tag-only search requires text.")
            return PreparedSearch(
                query_type="tag",
                search_mode="tags",
                text=normalized_text,
            )
        path = inspect_query_image(image_path) if image_path is not None else None
        if normalized_text is None and path is None:
            raise ValueError("Search requires text, image, or both.")

        request_ids: list[str] = []
        usage: list[dict] = []
        sources: dict[str, str] = {}
        text_vector: list[float] | None = None
        image_vector: list[float] | None = None
        image_sha256: str | None = None
        if path is not None:
            (
                image_vector,
                source,
                request_id,
                image_usage,
                image_sha256,
            ) = self._image_embedding(path)
            sources["image"] = source
            if request_id:
                request_ids.append(request_id)
            if image_usage:
                usage.append(image_usage)
        self.cancel_check()
        if normalized_text is not None:
            text_vector, source, request_id, text_usage = self._text_embedding(
                normalized_text
            )
            sources["text"] = source
            if request_id:
                request_ids.append(request_id)
            if text_usage:
                usage.append(text_usage)
        self.cancel_check()
        query_type = (
            "image_text"
            if path is not None and normalized_text is not None
            else "image"
            if path is not None
            else "text"
        )
        return PreparedSearch(
            query_type=query_type,
            search_mode="semantic",
            text=normalized_text,
            image_path=str(path) if path is not None else None,
            image_sha256=image_sha256,
            text_vector=text_vector,
            image_vector=image_vector,
            request_ids=request_ids,
            usage=usage,
            embedding_sources=sources,
        )

    def query_prepared_search(
        self,
        prepared: PreparedSearch,
        *,
        candidate_k: int,
        include_self: bool = False,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
    ) -> PreparedSearchCandidates:
        """Query this Collection with vectors prepared by any compatible service."""
        self.cancel_check()
        self._validate_top_k(candidate_k)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        if prepared.query_type == "tag":
            if prepared.search_mode != "tags" or prepared.text is None:
                raise ValueError("Prepared tag query is invalid.")
            resolved_hits = self._resolve_search_hits(
                self._tag_search_hits(
                    prepared.text,
                    tags=normalized_tags,
                    tag_mode=tag_mode,
                )
            )
            collection_size = len(resolved_hits)
            return PreparedSearchCandidates(
                query_type="tag",
                hits=resolved_hits[:candidate_k],
                candidate_k=candidate_k,
                collection_size=collection_size,
                next_candidate_k=self._next_candidate_k(
                    candidate_k,
                    collection_size,
                ),
                quality_configured=False,
            )
        exclude_sha256 = None if include_self else prepared.image_sha256
        collection_size = self.repository.doc_count
        next_candidate_k = self._next_candidate_k(candidate_k, collection_size)
        prepared_quality_mode: QualityMode = (
            "combined"
            if prepared.query_type == "image_text"
            else "image"
            if prepared.query_type == "image"
            else "text"
        )
        prepared_thresholds = self.search_quality.thresholds_for(prepared_quality_mode)
        if prepared.query_type == "text":
            if prepared.text_vector is None:
                raise ValueError("Prepared text query has no text vector.")
            hits = self._query_quality_candidates(
                prepared.text_vector,
                candidate_k,
                tags=normalized_tags,
                tag_mode=tag_mode,
                quality_mode="text",
                rank_source="text",
            )
            hybrid_intent = HybridTagIntent()
            tag_hits: list[SearchHit] = []
            metadata_hits: list[SearchHit] = []
            metadata_search: dict[str, object] = {
                "enabled": False,
                "candidate_count": 0,
                "extra_embedding_requests": 0,
                "calibrated": False,
            }
            if not self.search_quality.configured and prepared.text is not None:
                metadata_hits = self._query_metadata_candidates(
                    prepared.text_vector,
                    candidate_k,
                    tags=normalized_tags,
                    tag_mode=tag_mode,
                )
                metadata_search = {
                    "enabled": bool(metadata_hits),
                    "candidate_count": len(metadata_hits),
                    "extra_embedding_requests": 0,
                    "calibrated": False,
                }
                catalog = self._current_tag_catalog()
                hybrid_intent = detect_hybrid_tag_intent(prepared.text, catalog)
                if hybrid_intent.enabled:
                    tag_hits = self._tag_search_hits(
                        prepared.text,
                        tags=normalized_tags,
                        tag_mode=tag_mode,
                        query_plan=hybrid_intent.plan,
                    )[:candidate_k]
            return PreparedSearchCandidates(
                query_type="text",
                hits=self._resolve_search_hits(hits),
                metadata_hits=self._resolve_search_hits(metadata_hits),
                tag_hits=self._resolve_search_hits(tag_hits),
                candidate_k=candidate_k,
                collection_size=collection_size,
                next_candidate_k=next_candidate_k,
                quality_configured=self.search_quality.configured,
                minimum_confidence=prepared_thresholds.minimum,
                minimum_score=prepared_thresholds.minimum_score,
                possible_confidence=prepared_thresholds.possible,
                high_confidence=prepared_thresholds.high,
                score_gap=prepared_thresholds.score_gap,
                max_confidence_drop=prepared_thresholds.max_confidence_drop,
                fusion_mode=self.search_quality.fusion_mode,
                fusion_options=dict(self.search_quality.fusion_options),
                collection_calibration_mode=(
                    self.search_quality.collection_calibration.mode
                ),
                collection_confidence_offsets=(
                    self.search_quality.collection_calibration.offsets_for("text")
                ),
                ranking_mode=(
                    "hybrid_tag_visual_metadata"
                    if hybrid_intent.enabled and tag_hits and metadata_hits
                    else "hybrid_tag_vector"
                    if hybrid_intent.enabled and tag_hits
                    else "visual_metadata"
                    if metadata_hits
                    else "distance"
                ),
                hybrid_search=hybrid_intent.diagnostics(),
                metadata_search=metadata_search,
            )
        if prepared.query_type == "image":
            if prepared.image_vector is None:
                raise ValueError("Prepared image query has no image vector.")
            hits = self._query_quality_candidates(
                prepared.image_vector,
                candidate_k,
                exclude_sha256=exclude_sha256,
                tags=normalized_tags,
                tag_mode=tag_mode,
                quality_mode="image",
                rank_source="image",
            )
            return PreparedSearchCandidates(
                query_type="image",
                hits=self._resolve_search_hits(hits),
                candidate_k=candidate_k,
                collection_size=collection_size,
                next_candidate_k=next_candidate_k,
                quality_configured=self.search_quality.configured,
                minimum_confidence=prepared_thresholds.minimum,
                minimum_score=prepared_thresholds.minimum_score,
                possible_confidence=prepared_thresholds.possible,
                high_confidence=prepared_thresholds.high,
                score_gap=prepared_thresholds.score_gap,
                max_confidence_drop=prepared_thresholds.max_confidence_drop,
                fusion_mode=self.search_quality.fusion_mode,
                fusion_options=dict(self.search_quality.fusion_options),
                collection_calibration_mode=(
                    self.search_quality.collection_calibration.mode
                ),
                collection_confidence_offsets=(
                    self.search_quality.collection_calibration.offsets_for("image")
                ),
            )
        if prepared.query_type != "image_text":
            raise ValueError(f"Unsupported prepared query type: {prepared.query_type}")
        if prepared.image_vector is None or prepared.text_vector is None:
            raise ValueError("Prepared combined query is missing a vector.")
        image_hits = self._query_quality_candidates(
            prepared.image_vector,
            candidate_k,
            exclude_sha256=exclude_sha256,
            tags=normalized_tags,
            tag_mode=tag_mode,
            quality_mode="image",
            rank_source="image",
        )
        text_hits = self._query_quality_candidates(
            prepared.text_vector,
            candidate_k,
            exclude_sha256=exclude_sha256,
            tags=normalized_tags,
            tag_mode=tag_mode,
            quality_mode="text",
            rank_source="text",
        )
        metadata_hits = (
            []
            if self.search_quality.configured
            else self._query_metadata_candidates(
                prepared.text_vector,
                candidate_k,
                exclude_sha256=exclude_sha256,
                tags=normalized_tags,
                tag_mode=tag_mode,
            )
        )
        return PreparedSearchCandidates(
            query_type="image_text",
            image_hits=self._resolve_search_hits(image_hits),
            text_hits=self._resolve_search_hits(text_hits),
            metadata_hits=self._resolve_search_hits(metadata_hits),
            candidate_k=candidate_k,
            collection_size=collection_size,
            next_candidate_k=next_candidate_k,
            quality_configured=self.search_quality.configured,
            minimum_confidence=prepared_thresholds.minimum,
            minimum_score=prepared_thresholds.minimum_score,
            possible_confidence=prepared_thresholds.possible,
            high_confidence=prepared_thresholds.high,
            score_gap=prepared_thresholds.score_gap,
            max_confidence_drop=prepared_thresholds.max_confidence_drop,
            fusion_mode=self.search_quality.fusion_mode,
            fusion_options=dict(self.search_quality.fusion_options),
            collection_calibration_mode=(
                self.search_quality.collection_calibration.mode
            ),
            collection_confidence_offsets=(
                self.search_quality.collection_calibration.offsets_for("combined")
            ),
            ranking_mode=(
                "weighted_rrf_with_metadata" if metadata_hits else "weighted_rrf"
            ),
            metadata_search={
                "enabled": bool(metadata_hits),
                "candidate_count": len(metadata_hits),
                "extra_embedding_requests": 0,
                "calibrated": False,
            },
        )

    def _resolve_search_hits(self, hits: list[SearchHit]) -> list[ResolvedSearchHit]:
        resolved: list[ResolvedSearchHit] = []
        for hit in hits:
            self.cancel_check()
            try:
                source = self.source_resolver.resolve_hit(hit)
            except (OSError, ValueError, ConfigurationError):
                continue
            if source.is_file():
                resolved.append(ResolvedSearchHit(hit=hit, source_path=str(source)))
        return resolved

    def _hybrid_text_candidates(
        self,
        text: str,
        vector: list[float],
        *,
        top_k: int,
        tags: tuple[str, ...],
        tag_mode: str,
    ) -> tuple[HybridTagIntent, list[SearchHit], dict[str, object]]:
        """Combine tags, image vectors, and optional description vectors."""

        catalog = self._current_tag_catalog()
        intent = detect_hybrid_tag_intent(text, catalog)
        candidate_k = hybrid_candidate_count(top_k, self.repository.doc_count)
        vector_hits = self._query_quality_candidates(
            vector,
            candidate_k,
            tags=tags,
            tag_mode=tag_mode,
            quality_mode="text",
            rank_source="text",
        )
        metadata_hits = self._query_metadata_candidates(
            vector,
            candidate_k,
            tags=tags,
            tag_mode=tag_mode,
        )
        tag_hits = (
            self._tag_search_hits(
                text,
                tags=tags,
                tag_mode=tag_mode,
                query_plan=intent.plan,
            )[:candidate_k]
            if intent.enabled
            else []
        )
        if not tag_hits:
            # Explicit filters can remove every tag candidate. In that case the
            # semantic channel remains useful and must not be penalized.
            intent = HybridTagIntent()
        if not metadata_hits and not tag_hits:
            return (
                intent,
                vector_hits,
                {
                    "enabled": False,
                    "channels": ["text"] if vector_hits else [],
                    "weights": {"text": 1.0} if vector_hits else {},
                    "candidate_count": 0,
                    "metadata_candidate_count": 0,
                    "extra_embedding_requests": 0,
                    "calibrated": False,
                    "mode": "distance",
                },
            )
        fused, diagnostics = fuse_text_metadata_hits(
            vector_hits,
            metadata_hits,
            tag_hits,
        )
        diagnostics.update(
            {
                "candidate_count": len(metadata_hits),
                "calibrated": False,
            }
        )
        return intent, fused, diagnostics

    def _tag_search_hits(
        self,
        text: str,
        *,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
        query_plan: TagSearchPlan | None = None,
    ) -> list[SearchHit]:
        """Resolve fuzzy tags and rank matching state rows deterministically."""

        catalog = self._current_tag_catalog()
        query_plan = query_plan or catalog.resolve(text, mode="all")
        filter_plan = catalog.resolve(
            tags,
            mode=cast(TagMatchMode, tag_mode),
        )
        if query_plan.matches_nothing or filter_plan.matches_nothing:
            return []

        scored: list[tuple[float, tuple[str, ...], dict[str, Any]]] = []
        for entry in self.state.entries_for_any_effective_tags(query_plan.matched_tags):
            self.cancel_check()
            effective_tags = tuple(
                str(value) for value in entry.get("effective_tags", ())
            )
            if not result_matches_tag_plan(effective_tags, query_plan):
                continue
            if not result_matches_tag_plan(effective_tags, filter_plan):
                continue
            query_matches = matched_tags_for_result(effective_tags, query_plan)
            filter_matches = matched_tags_for_result(effective_tags, filter_plan)
            matched_tags = tuple(dict.fromkeys((*query_matches, *filter_matches)))
            confidence = _tag_match_confidence(query_plan, query_matches)
            fields = dict(entry)
            fields["tags"] = list(effective_tags)
            scored.append((confidence, matched_tags, fields))

        scored.sort(
            key=lambda item: (
                -item[0],
                -len(item[1]),
                str(item[2].get("root_id") or ""),
                str(item[2].get("relative_path") or ""),
                str(item[2].get("doc_id") or ""),
            )
        )
        hits = [
            SearchHit(
                doc_id=str(fields["doc_id"]),
                distance=1.0 - confidence,
                raw_score=1.0 - confidence,
                normalized_score=confidence,
                confidence=confidence,
                fields=fields,
                rank=rank,
                rank_source="tag",
                matched_tags=matched_tags,
            )
            for rank, (confidence, matched_tags, fields) in enumerate(
                scored,
                start=1,
            )
        ]
        return self._attach_search_learning_evidence(hits)

    def _cache_key(self, modality: str, value: str) -> str:
        identity = "\0".join(
            (
                self.repository.collection_uuid,
                self.config.model,
                str(self.config.dimension),
                modality,
                value,
            )
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    def _cache_vector(self, cache_key: str, modality: str, vector: list[float]) -> None:
        if len(vector) != self.config.dimension:
            raise ValueError(f"Invalid vector dimension: {len(vector)}")
        self.state.set_cached_vector(cache_key, modality, vector)

    def _text_embedding(self, text: str) -> tuple[list[float], str, str, dict]:
        cache_key = self._cache_key("text", text)
        vector = self.state.get_cached_vector(cache_key, self.config.dimension)
        if vector is not None:
            return vector, "cache", "", {}
        response = self.embedding_client.embed_text(text)
        vector = response.vectors[0]
        self._cache_vector(cache_key, "text", vector)
        return vector, "api", response.request_id, response.usage

    def _cached_image(
        self, path: Path
    ) -> tuple[str, list[float] | None, str | None, str]:
        stat = path.stat()
        entry = self.state.find_entry_for_path(path)
        if entry and (entry["size_bytes"], entry["mtime_ns"]) == (
            stat.st_size,
            stat.st_mtime_ns,
        ):
            vector = self.repository.fetch_vector(str(entry["doc_id"]))
            if vector is not None:
                return "", vector, "index", str(entry["sha256"])

        image_hash = file_sha256(path)
        cached_doc_id = self.state.find_doc_id_by_sha(image_hash)
        if cached_doc_id:
            vector = self.repository.fetch_vector(cached_doc_id)
            if vector is not None:
                return "", vector, "index", image_hash
        cache_key = self._cache_key("image", image_hash)
        vector = self.state.get_cached_vector(cache_key, self.config.dimension)
        return cache_key, vector, "cache" if vector is not None else None, image_hash

    def _image_embedding(self, path: Path) -> tuple[list[float], str, str, dict, str]:
        cache_key, vector, source, image_hash = self._cached_image(path)
        if vector is not None:
            return vector, source or "cache", "", {}, image_hash
        response = self.embedding_client.embed_images([path])
        self._ensure_query_image_unchanged(path, image_hash)
        vector = response.vectors[0]
        self._cache_vector(cache_key, "image", vector)
        return vector, "api", response.request_id, response.usage, image_hash

    @staticmethod
    def _ensure_query_image_unchanged(path: Path, expected_hash: str) -> None:
        if file_sha256(path) != expected_hash:
            raise ImageInputError("Query image changed during embedding; try again.")

    def _log_search(self, report: SearchReport) -> None:
        self.logger.info(
            "search_complete type=%s results=%d sources=%s api_requests=%d",
            report.query_type,
            report.result_count,
            ",".join(
                f"{key}:{value}"
                for key, value in sorted(report.embedding_sources.items())
            ),
            len(report.request_ids),
        )

    def _search_quality_diagnostics(
        self, show_low_confidence: bool, low_confidence_override: bool
    ) -> dict[str, object]:
        diagnostics: dict[str, object] = self.search_quality.to_dict()
        learning = self._current_search_learning()
        diagnostics["search_learning"] = (
            learning.diagnostics()
            if learning is not None
            else {"configured": False, "enabled": False, "ranking_fallback": True}
        )
        diagnostics["hard_minimum_confidence"] = MINIMUM_RESULT_CONFIDENCE
        if show_low_confidence:
            diagnostics = {
                **diagnostics,
                "low_confidence_requested": True,
                "low_confidence_override": low_confidence_override,
                "filtering_overridden": low_confidence_override,
            }
        return diagnostics

    def _search_diagnostics(
        self,
        *,
        show_low_confidence: bool,
        low_confidence_override: bool,
        hybrid_intent: HybridTagIntent | None = None,
        metadata_search: Mapping[str, object] | None = None,
        diversity: DiversityRanking | None = None,
        sorting: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        diagnostics = self._search_quality_diagnostics(
            show_low_confidence,
            low_confidence_override,
        )
        if hybrid_intent is not None:
            diagnostics["hybrid_search"] = hybrid_intent.diagnostics()
        if metadata_search is not None:
            diagnostics["metadata_search"] = dict(metadata_search)
        if diversity is not None:
            diagnostics["diversity"] = diversity.diagnostics()
        if sorting is not None:
            diagnostics["sorting"] = dict(sorting)
        return diagnostics

    def _sort_request_hits(
        self,
        hits: list[SearchHit],
        *,
        query_type: str,
        top_k: int,
        show_low_confidence: bool,
        sort_mode: SearchSortMode,
    ) -> ConfidenceRanking:
        """Apply the public confidence floor to non-calibrated/legacy hits."""

        learning_diagnostics: dict[str, object] | None = None
        search_learning = self._current_search_learning()
        if (
            search_learning is not None
            and search_learning.configured
            and sort_mode != "legacy"
        ):
            learning = apply_search_learning(
                hits,
                bundle=search_learning,
                query_type=query_type,
                default_library_id=self.config.library_id or "local",
                collection_sizes={
                    self.config.library_id or "local": self.repository.doc_count
                },
            )
            hits = learning.hits
            learning_diagnostics = learning.diagnostics
        ranking = sort_confidence_hits(
            hits,
            query_type=query_type,
            top_k=top_k,
            min_confidence=0.0,
            score_gap=1.0,
            max_confidence_drop=1.0,
            max_candidates=max(1, len(hits)),
            show_low_confidence=show_low_confidence,
            sort_mode=sort_mode,
        )
        if ranking is not None:
            if learning_diagnostics is not None:
                ranking = replace(
                    ranking,
                    diagnostics={
                        **ranking.diagnostics,
                        "search_learning": learning_diagnostics,
                    },
                )
            return ranking
        # Truly legacy records may not expose a finite confidence. Preserve
        # their previous order rather than dropping valid historical results.
        selected = [replace(hit, rank=rank) for rank, hit in enumerate(hits[:top_k], 1)]
        return ConfidenceRanking(
            selected,
            "legacy_fallback" if selected else "no_reliable_match",
            len(hits),
            max(0, len(hits) - len(selected)),
            sort_mode="legacy",
            diagnostics={
                "sort_mode": "legacy",
                "fallback": "missing_finite_confidence",
                "normalized_score_used": False,
            },
        )

    def _configured_single_ranking(
        self,
        vector: list[float],
        *,
        query_type: RankSource,
        top_k: int,
        exclude_sha256: str | None = None,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
        show_low_confidence: bool = False,
        sort_mode: SearchSortMode = "confidence",
    ) -> ConfidenceRanking:
        if query_type not in {"text", "image"}:
            raise ValueError(f"Unsupported single query type: {query_type}")
        total = self.repository.doc_count
        if total == 0:
            return ConfidenceRanking([], "no_reliable_match", 0, 0)
        candidate_k = min(total, confidence_candidate_limit(top_k))
        quality_mode: QualityMode = "image" if query_type == "image" else "text"
        candidates = self._query_quality_candidates(
            vector,
            candidate_k,
            exclude_sha256=exclude_sha256,
            tags=tags,
            tag_mode=tag_mode,
            quality_mode=quality_mode,
            rank_source=query_type,
        )
        thresholds = self.search_quality.thresholds_for(quality_mode)
        # Diagnostics and focused tests can use a lightweight service adapter
        # without a complete ServiceConfig. Ranking should still degrade to a
        # stable local library identifier in that shape.
        config = getattr(self, "config", None)
        library_id = str(getattr(config, "library_id", "") or "local")
        search_learning = self._current_search_learning()
        ranking = confidence_rank(
            candidates,
            query_type=query_type,
            top_k=top_k,
            min_confidence=thresholds.minimum_confidence,
            minimum_score=thresholds.minimum_score,
            possible_confidence=thresholds.possible,
            high_confidence=thresholds.high,
            score_gap=thresholds.score_gap,
            max_confidence_drop=thresholds.max_confidence_drop,
            max_candidates=candidate_k,
            show_low_confidence=show_low_confidence,
            sort_mode=sort_mode,
            search_learning=(
                search_learning
                if search_learning is not None and search_learning.configured
                else None
            ),
            default_library_id=library_id,
            collection_sizes={library_id: self.repository.doc_count},
        )
        if ranking is None:
            raise RuntimeError("Configured search candidates are missing confidence.")
        return ranking

    def _configured_combined_ranking(
        self,
        image_vector: list[float],
        text_vector: list[float],
        *,
        top_k: int,
        image_weight: float,
        text_weight: float,
        exclude_sha256: str | None,
        tags: tuple[str, ...],
        tag_mode: str,
        show_low_confidence: bool = False,
        sort_mode: SearchSortMode = "confidence",
    ) -> ConfidenceRanking:
        total = self.repository.doc_count
        if total == 0:
            return ConfidenceRanking([], "no_reliable_match", 0, 0)
        candidate_k = min(total, confidence_candidate_limit(top_k))
        with ThreadPoolExecutor(max_workers=2) as executor:
            image_future = executor.submit(
                self._query_quality_candidates,
                image_vector,
                candidate_k,
                exclude_sha256=exclude_sha256,
                tags=tags,
                tag_mode=tag_mode,
                quality_mode="image",
                rank_source="image",
            )
            text_future = executor.submit(
                self._query_quality_candidates,
                text_vector,
                candidate_k,
                exclude_sha256=exclude_sha256,
                tags=tags,
                tag_mode=tag_mode,
                quality_mode="text",
                rank_source="text",
            )
            candidates = image_future.result() + text_future.result()
        thresholds = self.search_quality.thresholds_for("combined")
        # Keep combined ranking available to the same lightweight/degraded
        # adapters instead of failing on a missing configuration attribute.
        config = getattr(self, "config", None)
        library_id = str(getattr(config, "library_id", "") or "local")
        search_learning = self._current_search_learning()
        ranking = confidence_rank(
            candidates,
            query_type="image_text",
            top_k=top_k,
            image_weight=image_weight,
            text_weight=text_weight,
            min_confidence=thresholds.minimum_confidence,
            minimum_score=thresholds.minimum_score,
            possible_confidence=thresholds.possible,
            high_confidence=thresholds.high,
            score_gap=thresholds.score_gap,
            max_confidence_drop=thresholds.max_confidence_drop,
            fusion_mode=self.search_quality.fusion_mode,
            agreement_reward=self.search_quality.fusion_options.get(
                "agreement_reward", DEFAULT_AGREEMENT_REWARD
            ),
            rank_decay=self.search_quality.fusion_options.get(
                "rank_decay", DEFAULT_RANK_DECAY
            ),
            weak_channel_floor=self.search_quality.fusion_options.get(
                "weak_channel_floor", DEFAULT_WEAK_CHANNEL_FLOOR
            ),
            weak_channel_penalty=self.search_quality.fusion_options.get(
                "weak_channel_penalty", DEFAULT_WEAK_CHANNEL_PENALTY
            ),
            max_candidates=candidate_k,
            show_low_confidence=show_low_confidence,
            sort_mode=sort_mode,
            search_learning=(
                search_learning
                if search_learning is not None and search_learning.configured
                else None
            ),
            default_library_id=library_id,
            collection_sizes={library_id: self.repository.doc_count},
        )
        if ranking is None:
            raise RuntimeError("Configured search candidates are missing confidence.")
        return ranking

    def _query_until_sufficient(
        self,
        vector: list[float],
        top_k: int,
        exclude_path: str | None = None,
        exclude_sha256: str | None = None,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
        quality_mode: QualityMode = "text",
        rank_source: RankSource = "text",
    ) -> list[SearchHit]:
        total = self.repository.doc_count
        if total == 0:
            return []
        limit = min(total, max(top_k * 3, top_k + 10))
        while True:
            self.cancel_check()
            hits = self._query_quality_candidates(
                vector,
                limit,
                exclude_sha256=exclude_sha256,
                tags=tags,
                tag_mode=tag_mode,
                quality_mode=quality_mode,
                rank_source=rank_source,
            )
            if (
                _usable_hit_count(hits, exclude_path, self.source_resolver.resolve_hit)
                >= top_k
                or limit >= total
            ):
                return hits
            limit = min(total, max(limit + 1, limit * 2))

    def _query_fused_until_sufficient(
        self,
        image_vector: list[float],
        text_vector: list[float],
        top_k: int,
        image_weight: float,
        text_weight: float,
        exclude_path: str | None,
        exclude_sha256: str | None,
        tags: tuple[str, ...],
        tag_mode: str,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        total = self.repository.doc_count
        if total == 0:
            return [], {
                "enabled": False,
                "candidate_count": 0,
                "extra_embedding_requests": 0,
                "calibrated": False,
                "mode": "weighted_rrf",
            }
        limit = min(total, max(top_k * 3, top_k + 10))
        while True:
            self.cancel_check()
            with ThreadPoolExecutor(max_workers=3) as executor:
                image_future = executor.submit(
                    self.repository.query,
                    image_vector,
                    limit,
                    tags,
                    tag_mode,
                    "image",
                )
                text_future = executor.submit(
                    self.repository.query,
                    text_vector,
                    limit,
                    tags,
                    tag_mode,
                    "text",
                )
                metadata_future = executor.submit(
                    self.repository.query_metadata,
                    text_vector,
                    limit,
                    tags,
                    tag_mode,
                    "metadata",
                )
                image_hits = image_future.result()
                text_hits = text_future.result()
                metadata_hits = metadata_future.result()
            image_hits = _exclude_content_hash(image_hits, exclude_sha256)
            text_hits = _exclude_content_hash(text_hits, exclude_sha256)
            metadata_hits = _exclude_content_hash(metadata_hits, exclude_sha256)
            fused, diagnostics = fuse_combined_metadata_hits(
                image_hits,
                text_hits,
                metadata_hits,
                image_weight=image_weight,
                text_weight=text_weight,
            )
            fused = self._apply_search_quality(fused, "combined")
            diagnostics.update(
                {
                    "candidate_count": len(metadata_hits),
                    "calibrated": False,
                }
            )
            if (
                _usable_hit_count(fused, exclude_path, self.source_resolver.resolve_hit)
                >= top_k
                or limit >= total
            ):
                return fused, diagnostics
            limit = min(total, max(limit + 1, limit * 2))

    def _query_metadata_candidates(
        self,
        vector: list[float],
        candidate_k: int,
        *,
        exclude_sha256: str | None = None,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
    ) -> list[SearchHit]:
        """Query only documents with a completed description-vector backfill."""

        if candidate_k <= 0 or self.repository.doc_count == 0:
            return []
        queried = self.repository.query_metadata(
            vector,
            min(candidate_k, self.repository.doc_count),
            tags,
            tag_mode,
            "metadata",
        )
        return _exclude_content_hash(queried, exclude_sha256)

    def _query_quality_candidates(
        self,
        vector: list[float],
        candidate_k: int,
        *,
        exclude_sha256: str | None = None,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
        quality_mode: QualityMode,
        rank_source: RankSource,
    ) -> list[SearchHit]:
        total = self.repository.doc_count
        if total == 0:
            return []

        # candidate_k is the usable candidate budget.  A staged query image can
        # have the same content as an indexed document even though its path is
        # different, so SHA-256 exclusion may remove an item from the first
        # local query.  Refill only the missing slots; this remains a local Zvec
        # query and does not add an embedding/API request.
        target = min(candidate_k, total)
        limit = target
        while True:
            queried = self.repository.query(
                vector,
                limit,
                tags,
                tag_mode,
                rank_source,
            )
            hits = _exclude_content_hash(queried, exclude_sha256)
            removed = len(queried) - len(hits)
            if removed == 0 or len(hits) >= target or limit >= total:
                return self._attach_search_learning_evidence(
                    self._apply_search_quality(hits[:target], quality_mode)
                )
            limit = min(total, limit + max(1, target - len(hits)))

    def _apply_search_quality(
        self, hits: list[SearchHit], quality_mode: QualityMode
    ) -> list[SearchHit]:
        thresholds = self.search_quality.thresholds_for(quality_mode)
        return annotate_search_hits(hits, thresholds)

    def _attach_search_learning_evidence(
        self, hits: list[SearchHit]
    ) -> list[SearchHit]:
        """Attach bounded, provenance-aware tag features for local ranking.

        Private fields are consumed by ``search_learning_runtime`` and are not
        included in the public result manifest. Unknown provenance stays in the
        vector bucket; it is never promoted to a manual or folder match.
        """

        search_learning = self._current_search_learning()
        if not hits or search_learning is None or not search_learning.configured:
            return hits
        entries = self.state.get_many(hit.doc_id for hit in hits)
        enriched: list[SearchHit] = []
        for hit in hits:
            entry = entries.get(hit.doc_id, {})
            matched = {_tag_key(value) for value in hit.matched_tags}
            sources = {
                "manual": _matched_source_tags(entry.get("tags"), matched),
                "folder": _matched_source_tags(entry.get("folder_tags"), matched),
                "alias": [],
                "model_high_confidence": [],
                "model": _matched_source_tags(entry.get("accepted_auto_tags"), matched),
                "vector": [],
            }
            signals: dict[str, float] = {}
            annotation = self.state.get_document_annotation(hit.doc_id)
            if annotation is not None:
                high_confidence, annotation_signals = _annotation_learning_evidence(
                    annotation, matched
                )
                sources["model_high_confidence"] = high_confidence
                signals.update(annotation_signals)
            claimed = {
                _tag_key(value)
                for source, values in sources.items()
                if source != "vector"
                for value in values
            }
            sources["vector"] = [
                value for value in hit.matched_tags if _tag_key(value) not in claimed
            ]
            fields = {
                **hit.fields,
                "_tag_evidence": sources,
                **signals,
            }
            enriched.append(replace(hit, fields=fields))
        return enriched

    def _current_search_learning(self) -> SearchLearningBundle | None:
        """Return the latest atomically activated learning bundle.

        A native backend keeps one service instance alive per library while the
        desktop facade activates candidates through a separate component.  A
        cheap active-manifest stat therefore makes activate, disable, and
        rollback visible on the next single-library search without restarting
        the backend. Invalid artifacts still use the normal safe fallback.
        """

        current = getattr(self, "search_learning", None)
        config = getattr(self, "config", None)
        config_home = getattr(config, "config_home_path", None)
        if config_home is None:
            return current

        signature = self._search_learning_active_signature()
        cached_signature = getattr(
            self,
            "_search_learning_manifest_signature",
            None,
        )
        if signature == cached_signature:
            return current

        reload_lock = getattr(self, "_search_learning_reload_lock", None)
        if reload_lock is None:
            # Focused tests and degraded adapters may construct the service
            # without __init__; retain that lightweight supported shape.
            reload_lock = threading.Lock()
            self._search_learning_reload_lock = reload_lock
        with reload_lock:
            signature = self._search_learning_active_signature()
            if signature == getattr(
                self,
                "_search_learning_manifest_signature",
                None,
            ):
                return getattr(self, "search_learning", current)
            reloaded = load_search_learning(Path(config_home))
            self.search_learning = reloaded
            self._search_learning_manifest_signature = (
                self._search_learning_active_signature()
            )
            return reloaded

    def _search_learning_active_signature(self) -> tuple[bool, int, int]:
        config = getattr(self, "config", None)
        config_home = getattr(config, "config_home_path", None)
        if config_home is None:
            return (False, 0, 0)
        active_path = (
            Path(config_home) / SEARCH_LEARNING_DIRECTORY / ACTIVE_CONFIG_FILENAME
        )
        try:
            stat = active_path.stat()
        except FileNotFoundError:
            return (False, 0, 0)
        except OSError:
            # Moving from a valid signature to this sentinel triggers one safe
            # fallback reload; repeated permission errors remain bounded.
            return (True, -1, -1)
        return (True, stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def _next_candidate_k(candidate_k: int, collection_size: int) -> int | None:
        if candidate_k >= collection_size:
            return None
        return min(collection_size, max(candidate_k + 1, candidate_k * 2))

    def _cluster_operation_store(self) -> ClusterOperationStore | None:
        return getattr(self, "cluster_operations", None)

    def _cluster_library_key(self) -> str:
        library_id = self.config.library_id or "local"
        collection_uuid = str(
            getattr(self.repository, "collection_uuid", "") or "unbound"
        )
        return f"{library_id}@{collection_uuid}"

    def _import_legacy_cluster_snapshot(self) -> None:
        """Migrate the old JSON snapshot once without recomputing vectors."""

        store = self._cluster_operation_store()
        if store is None or not self.config.cluster_snapshot_path.is_file():
            return
        library_key = self._cluster_library_key()
        if store.active_snapshot_version(library_key) is not None:
            return
        try:
            snapshot = read_cluster_snapshot(self.config.cluster_snapshot_path)
            store.save_snapshot(
                library_key,
                snapshot,
                metadata={"migrated_from": self.config.cluster_snapshot_path.name},
            )
        except Exception as exc:
            # A broken optional legacy snapshot must not prevent the library
            # from opening; the next explicit clustering run replaces it.
            self.logger.warning(
                "legacy_cluster_snapshot_import_failed error=%s",
                str(exc) or exc.__class__.__name__,
            )

    def _active_cluster_snapshot(self) -> ClusterSnapshot | None:
        store = self._cluster_operation_store()
        if store is not None:
            record = store.active_snapshot_version(self._cluster_library_key())
            if record is not None:
                return store.load_snapshot(record.snapshot_version)
        if self.config.cluster_snapshot_path.is_file():
            return read_cluster_snapshot(self.config.cluster_snapshot_path)
        return None

    def _active_cluster_rules(self) -> list[dict[str, Any]]:
        store = self._cluster_operation_store()
        if store is None:
            return []
        rules: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = store.list_rules(
                self._cluster_library_key(),
                active_only=True,
                cursor=cursor,
                limit=500,
            )
            rules.extend(cast(list[dict[str, Any]], page["items"]))
            if not page["has_more"]:
                return rules
            cursor = cast(str, page["next_cursor"])

    def cluster_images(
        self,
        *,
        scope: str = "new_or_changed",
        cluster_types: Iterable[str] = ("exact", "perceptual", "semantic"),
    ) -> dict[str, object]:
        """Build local clusters from persisted hashes and existing vectors only."""

        normalized_scope = str(scope).strip().lower()
        if normalized_scope not in {"new_or_changed", "all"}:
            raise ValueError("scope must be new_or_changed or all.")
        requested_types = tuple(
            dict.fromkeys(str(value).strip().lower() for value in cluster_types)
        )
        if not requested_types or set(requested_types) - {
            "exact",
            "perceptual",
            "semantic",
            "near_duplicate",
        }:
            raise ValueError(
                "cluster_types must contain exact, perceptual, semantic, and/or "
                "the legacy near_duplicate alias."
            )
        normalized_types = tuple(
            dict.fromkeys(
                expanded
                for requested in requested_types
                for expanded in (
                    ("exact", "perceptual")
                    if requested == "near_duplicate"
                    else (requested,)
                )
            )
        )
        self.cancel_check()
        count_entries = getattr(self.state, "count", None)
        fallback_entries: list[dict[str, Any]] | None = None
        if callable(count_entries):
            total = int(count_entries())
        else:
            fallback_entries = self.state.list_entries()
            total = len(fallback_entries)
        items: list[ImageClusterInput] = []
        preparation_failures: list[dict[str, str]] = []
        iter_entries = getattr(self.state, "iter_entries", None)
        entry_chunks = (
            iter_entries(chunk_size=256)
            if callable(iter_entries)
            else (fallback_entries or self.state.list_entries(),)
        )
        processed_entries = 0
        for entry_chunk in entry_chunks:
            annotation_reader = getattr(
                self.state,
                "get_document_annotations",
                None,
            )
            annotations = (
                annotation_reader(
                    str(entry.get("doc_id") or "") for entry in entry_chunk
                )
                if callable(annotation_reader)
                else {}
            )
            for entry in entry_chunk:
                self.cancel_check()
                processed_entries += 1
                doc_id = str(entry.get("doc_id") or "")
                sha = str(entry.get("sha256") or "").casefold()
                if len(sha) != 64:
                    preparation_failures.append(
                        {
                            "doc_id": doc_id,
                            "code": "missing_sha256",
                            "message": "Indexed SHA-256 is unavailable.",
                        }
                    )
                    continue
                relative_path = str(entry.get("relative_path") or "")
                source_path = ""
                with suppress(OSError, ValueError, ConfigurationError):
                    source_path = str(self.source_resolver.resolve_fields(entry))
                perceptual_hash = None
                if "perceptual" in normalized_types:
                    try:
                        source = (
                            Path(source_path)
                            if source_path
                            else self.source_resolver.resolve_fields(entry)
                        )
                        perceptual_hash = _average_image_hash(source)
                    except (
                        OSError,
                        ValueError,
                        ConfigurationError,
                        UnidentifiedImageError,
                    ) as exc:
                        # Exact SHA and semantic clustering remain usable.
                        perceptual_hash = None
                        preparation_failures.append(
                            {
                                "doc_id": doc_id,
                                "code": "perceptual_hash_failed",
                                "message": (
                                    str(exc) or "The image could not be decoded."
                                ),
                                "relative_path": relative_path,
                                **({"source_path": source_path} if source_path else {}),
                            }
                        )
                try:
                    annotation = (
                        annotations.get(doc_id)
                        if annotations
                        else self.state.get_document_annotation(doc_id)
                    )
                    identity_evidence = _cluster_identity_evidence(entry, annotation)
                except Exception as exc:
                    # Identity evidence is optional. A broken annotation record
                    # must not prevent hashes and vectors from being grouped.
                    preparation_failures.append(
                        {
                            "doc_id": doc_id,
                            "code": "identity_evidence_failed",
                            "message": str(exc) or exc.__class__.__name__,
                            "relative_path": relative_path,
                            **({"source_path": source_path} if source_path else {}),
                        }
                    )
                    identity_evidence = ()
                items.append(
                    ImageClusterInput(
                        doc_id=doc_id,
                        sha256=sha,
                        embedding=None,
                        perceptual_hash=perceptual_hash,
                        identity_evidence=identity_evidence,
                    )
                )
                if processed_entries == total or processed_entries % 100 == 0:
                    self.progress(
                        f"Preparing clustering inputs {processed_entries}/{total}"
                    )

        previous = (
            self._active_cluster_snapshot()
            if normalized_scope == "new_or_changed"
            else None
        )

        semantic_enabled = "semantic" in normalized_types
        semantic_progress = 0

        def semantic_neighbors(
            item: ImageClusterInput, top_k: int
        ) -> list[SemanticNeighbor]:
            nonlocal semantic_progress
            self.cancel_check()
            semantic_progress += 1
            if semantic_progress % 100 == 0 or semantic_progress == len(items):
                self.progress(
                    f"Clustering semantic neighbours {semantic_progress}/{len(items)}"
                )
            vector = self.repository.fetch_vector(item.doc_id)
            if vector is None:
                raise ValueError("Indexed embedding is unavailable.")
            hits = self.repository.query(vector, top_k + 1, (), "all", "image")
            return [
                SemanticNeighbor(hit.doc_id, min(1.0, max(-1.0, 1.0 - hit.distance)))
                for hit in hits
                if hit.doc_id != item.doc_id
            ][:top_k]

        runner = ImageClusterService(
            ImageClusteringConfig(
                embedding_version=self.config.model,
                embedding_dimension=self.config.dimension,
                enable_exact_hash="exact" in normalized_types,
                enable_perceptual_hash="perceptual" in normalized_types,
                enable_semantic=semantic_enabled,
                external_embedding_lookup=semantic_enabled,
            ),
            semantic_neighbor_provider=(
                semantic_neighbors if semantic_enabled else None
            ),
        )
        result = runner.run(
            items,
            previous_snapshot=previous,
            cancel_check=self.cancel_check,
        )
        manual_rules = self._active_cluster_rules()
        if manual_rules:
            result = replace(
                result,
                snapshot=apply_manual_cluster_rules(result.snapshot, manual_rules),
            )
        store = self._cluster_operation_store()
        snapshot_version = ""
        if store is not None:
            snapshot_record = store.save_snapshot(
                self._cluster_library_key(),
                result.snapshot,
                metadata={
                    "scope": normalized_scope,
                    "cluster_types": list(normalized_types),
                },
            )
            snapshot_version = snapshot_record.snapshot_version
            # Keep a small compatibility snapshot for older builds. Large
            # libraries use normalized SQLite only and avoid the former JSON
            # size limit and duplicate memory peak.
            if len(result.snapshot.items) <= 10_000:
                write_cluster_snapshot(
                    self.config.cluster_snapshot_path,
                    result.snapshot,
                )
        else:
            write_cluster_snapshot(self.config.cluster_snapshot_path, result.snapshot)
        payload = result.to_dict()
        # The complete snapshot is already persisted atomically. Keeping it in
        # the job record would duplicate a potentially large local data set and
        # make every poll transfer it again.
        payload.pop("snapshot", None)
        core_failures = payload.get("failures", [])
        failures = [
            *preparation_failures,
            *(core_failures if isinstance(core_failures, list) else []),
        ]
        payload["failures"] = failures
        payload["failure_count"] = len(failures)
        payload["cluster_count"] = len(result.snapshot.clusters)
        payload["processed"] = result.clustered_count
        payload["total"] = total
        payload["api_requests"] = 0
        payload["embedding_api_requests"] = 0
        payload["qwen_api_requests"] = 0
        payload["embedding_recomputed"] = False
        payload["snapshot_file"] = self.config.cluster_snapshot_path.name
        if snapshot_version:
            payload["snapshot_version"] = snapshot_version
            payload["snapshot_storage"] = "sqlite"
        else:
            payload["snapshot_storage"] = "json_compatibility"
        return payload

    def list_image_clusters(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        cluster_type: str | None = None,
    ) -> dict[str, object]:
        self.cancel_check()
        normalized_type = str(cluster_type or "").strip().lower()
        if normalized_type not in {
            "",
            "all",
            "exact",
            "perceptual",
            "semantic",
            "single",
            "near_duplicate",
        }:
            raise ValueError(
                "cluster_type must be all, exact, perceptual, semantic, single, "
                "or near_duplicate."
            )
        store = self._cluster_operation_store()
        active_record = (
            store.active_snapshot_version(self._cluster_library_key())
            if store is not None
            else None
        )
        if store is not None and active_record is not None:
            cluster_page = store.page_clusters(
                active_record.snapshot_version,
                offset=offset,
                limit=limit,
                cluster_type=normalized_type,
            )
            summaries = cast(list[dict[str, Any]], cluster_page["items"])
            representative_ids = [
                str(item.get("representative_doc_id") or "") for item in summaries
            ]
            entries = self.state.get_many(representative_ids)
            page_items: list[dict[str, object]] = []
            for index, summary in enumerate(summaries, 1):
                representative_id = str(summary.get("representative_doc_id") or "")
                page_items.append(
                    {
                        **summary,
                        "representative": self._cluster_image_payload(
                            representative_id,
                            entries.get(representative_id, {}),
                        ),
                    }
                )
                if index % 100 == 0:
                    self.progress(f"Loading image clusters {index}/{len(summaries)}")
            return {
                **cluster_page,
                "items": page_items,
                "embedding_api_requests": 0,
                "qwen_api_requests": 0,
            }
        if not self.config.cluster_snapshot_path.is_file():
            return {
                "total": 0,
                "offset": offset,
                "limit": limit,
                "items": [],
                "api_requests": 0,
                "embedding_api_requests": 0,
                "qwen_api_requests": 0,
            }
        snapshot = read_cluster_snapshot(self.config.cluster_snapshot_path)
        all_clusters = tuple(
            sorted(
                snapshot.clusters,
                key=lambda cluster: (-len(cluster.member_doc_ids), cluster.cluster_id),
            )
        )
        clusters = [
            cluster
            for cluster in all_clusters
            if (
                normalized_type == "single"
                and len(cluster.member_doc_ids) == 1
                or normalized_type != "single"
                and len(cluster.member_doc_ids) > 1
                and (
                    normalized_type in {"", "all"}
                    or normalized_type == "near_duplicate"
                    and bool({"exact", "perceptual"} & set(cluster.edge_kinds))
                    or normalized_type in {"exact", "perceptual", "semantic"}
                    and normalized_type in cluster.edge_kinds
                )
            )
        ]
        cluster_slice = clusters[offset : offset + limit]
        representative_ids = [cluster.member_doc_ids[0] for cluster in cluster_slice]
        entries = self.state.get_many(representative_ids)
        items: list[dict[str, object]] = []
        for index, cluster in enumerate(cluster_slice, 1):
            self.cancel_check()
            representative_id = cluster.member_doc_ids[0]
            representative = self._cluster_image_payload(
                representative_id,
                entries.get(representative_id, {}),
            )
            edge_kinds = set(cluster.edge_kinds)
            inferred_type = (
                "single"
                if len(cluster.member_doc_ids) == 1
                else "exact"
                if "exact" in edge_kinds
                else "perceptual"
                if "perceptual" in edge_kinds
                else "semantic"
            )
            items.append(
                {
                    **cluster.to_dict(),
                    "cluster_type": inferred_type,
                    "member_count": len(cluster.member_doc_ids),
                    "representative_doc_id": representative_id,
                    "representative": representative,
                }
            )
            if index % 100 == 0:
                self.progress(f"Loading image clusters {index}/{len(cluster_slice)}")
        return {
            "total": len(clusters),
            "offset": offset,
            "limit": limit,
            "items": items,
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def image_cluster_detail(
        self,
        cluster_id: str,
        *,
        offset: int = 0,
        limit: int = 256,
        edge_offset: int = 0,
        edge_limit: int = 1_000,
    ) -> dict[str, object]:
        self.cancel_check()
        store = self._cluster_operation_store()
        active_record = (
            store.active_snapshot_version(self._cluster_library_key())
            if store is not None
            else None
        )
        if store is not None and active_record is not None:
            try:
                detail_page = store.cluster_detail(
                    active_record.snapshot_version,
                    cluster_id,
                    offset=offset,
                    limit=limit,
                    edge_offset=edge_offset,
                    edge_limit=edge_limit,
                )
            except ClusterOperationNotFound as exc:
                raise ValueError(f"Unknown cluster: {cluster_id}") from exc
            members_page = cast(dict[str, Any], detail_page["members"])
            member_rows = cast(list[dict[str, Any]], members_page["items"])
            member_ids = [str(item.get("doc_id") or "") for item in member_rows]
            entries = self.state.get_many(member_ids)
            member_views = [
                {
                    **item,
                    **self._cluster_image_payload(
                        str(item.get("doc_id") or ""),
                        entries.get(str(item.get("doc_id") or ""), {}),
                    ),
                    "tags": list(
                        entries.get(str(item.get("doc_id") or ""), {}).get(
                            "effective_tags", ()
                        )
                    ),
                }
                for item in member_rows
            ]
            cluster = cast(dict[str, Any], detail_page["cluster"])
            representative_id = str(cluster.get("representative_doc_id") or "")
            representative = next(
                (
                    item
                    for item in member_views
                    if str(item.get("doc_id") or "") == representative_id
                ),
                self._cluster_image_payload(
                    representative_id,
                    self.state.get(representative_id) or {},
                ),
            )
            edges_page = cast(dict[str, Any], detail_page["edges"])
            return {
                "cluster": {**cluster, "representative": representative},
                "items": member_views,
                "member_count": members_page["total_count"],
                "offset": members_page["offset"],
                "limit": members_page["limit"],
                "has_more": members_page["has_more"],
                "edges": edges_page["items"],
                "edge_count": edges_page["total_count"],
                "edge_offset": edges_page["offset"],
                "edge_limit": edges_page["limit"],
                "edge_has_more": edges_page["has_more"],
                "api_requests": 0,
                "embedding_api_requests": 0,
                "qwen_api_requests": 0,
            }
        snapshot = read_cluster_snapshot(self.config.cluster_snapshot_path)
        detail = cluster_detail(snapshot, cluster_id)
        if detail is None:
            raise ValueError(f"Unknown cluster: {cluster_id}")
        entries = self.state.get_many(detail.cluster.member_doc_ids)
        item_views: list[dict[str, object]] = []
        for index, item in enumerate(detail.items, 1):
            self.cancel_check()
            entry = entries.get(item.doc_id, {})
            item_views.append(
                {
                    **item.to_dict(),
                    **self._cluster_image_payload(item.doc_id, entry),
                    "tags": list(entry.get("effective_tags", ())),
                }
            )
            if index % 100 == 0:
                self.progress(f"Loading cluster members {index}/{len(detail.items)}")
        representative = item_views[0] if item_views else {}
        return {
            "cluster": {
                **detail.cluster.to_dict(),
                "member_count": len(detail.cluster.member_doc_ids),
                "representative": representative,
            },
            "items": item_views,
            "member_count": len(item_views),
            "edges": [edge.to_dict() for edge in detail.edges],
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def _required_cluster_snapshot(
        self,
    ) -> tuple[ClusterOperationStore, Any, ClusterSnapshot]:
        store = self._cluster_operation_store()
        if store is None:
            raise ValueError("Cluster operation history is unavailable.")
        record = store.active_snapshot_version(self._cluster_library_key())
        if record is None:
            raise ValueError("Run image clustering before organizing groups.")
        return store, record, store.load_snapshot(record.snapshot_version)

    def _execute_cluster_rule_operation(
        self,
        *,
        store: ClusterOperationStore,
        source_record: Any,
        source_snapshot: ClusterSnapshot,
        operation_type: str,
        rule_kind: str,
        pairs: Sequence[tuple[str, str]],
        metadata: Mapping[str, Any],
    ) -> dict[str, object]:
        operation_items = tuple(
            ClusterOperationItemInput(
                item_key=_cluster_rule_item_key(left, right),
                left_doc_id=left,
                right_doc_id=right,
                before={"active": False},
            )
            for left, right in pairs
        )
        operation = store.create_operation(
            library_id=self._cluster_library_key(),
            operation_type=operation_type,
            source_snapshot_version=source_record.snapshot_version,
            items=operation_items,
            before_snapshot={"snapshot_version": source_record.snapshot_version},
            metadata=metadata,
        )
        operation_id = str(operation["operation_id"])
        created_rules: list[tuple[int, dict[str, Any]]] = []
        failures: list[dict[str, str]] = []
        warnings: list[str] = []
        try:
            for index, (left, right) in enumerate(pairs):
                self.cancel_check()
                try:
                    rule = store.add_rule(
                        library_id=self._cluster_library_key(),
                        snapshot_version=source_record.snapshot_version,
                        rule_kind=rule_kind,
                        left_doc_id=left,
                        right_doc_id=right,
                        operation_id=operation_id,
                    )
                    if rule["created"]:
                        created_rules.append((index, rule))
                    else:
                        store.record_item_result(
                            operation_id,
                            index,
                            status="skipped",
                            after=rule,
                        )
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    store.record_item_result(
                        operation_id,
                        index,
                        status="failed",
                        error_code="rule_create_failed",
                        error_message=error,
                    )
                    _append_active_learning_failure(failures, f"{left}|{right}", error)

            result_snapshot_version = source_record.snapshot_version
            if created_rules:
                try:
                    updated = apply_manual_cluster_rules(
                        source_snapshot,
                        self._active_cluster_rules(),
                    )
                    updated_record = store.save_snapshot(
                        self._cluster_library_key(),
                        updated,
                        metadata={"operation_id": operation_id},
                    )
                    result_snapshot_version = updated_record.snapshot_version
                    if len(updated.items) <= 10_000:
                        write_cluster_snapshot(
                            self.config.cluster_snapshot_path,
                            updated,
                        )
                    for index, rule in created_rules:
                        store.record_item_result(
                            operation_id,
                            index,
                            status="applied",
                            after=rule,
                        )
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    for index, rule in created_rules:
                        with suppress(Exception):
                            store.revoke_rule(
                                str(rule["rule_id"]),
                                operation_id=operation_id,
                            )
                        store.record_item_result(
                            operation_id,
                            index,
                            status="failed",
                            error_code="snapshot_update_failed",
                            error_message=error,
                        )
                    warnings.append(error)
            completed = store.complete_operation(
                operation_id,
                after_snapshot={"snapshot_version": result_snapshot_version},
                result_snapshot_version=result_snapshot_version,
            )
        except BaseException as exc:
            for _index, rule in created_rules:
                with suppress(Exception):
                    store.revoke_rule(str(rule["rule_id"]), operation_id=operation_id)
            with suppress(Exception):
                store.fail_operation(
                    operation_id,
                    error_code="operation_interrupted",
                    error_message=str(exc) or exc.__class__.__name__,
                )
            raise
        return self._cluster_operation_result(
            completed,
            failures=failures,
            warnings=warnings,
        )

    @staticmethod
    def _cluster_operation_result(
        operation: Mapping[str, Any],
        *,
        failures: Sequence[Mapping[str, str]] = (),
        warnings: Sequence[str] = (),
    ) -> dict[str, object]:
        return {
            "operation_id": str(operation.get("operation_id") or ""),
            "operation": str(operation.get("operation_type") or ""),
            "status": str(operation.get("status") or ""),
            "applied": int(operation.get("succeeded_count") or 0),
            "failed": int(operation.get("failed_count") or 0),
            "skipped": int(operation.get("skipped_count") or 0),
            "failures": [dict(value) for value in failures[:100]],
            "warnings": list(warnings),
            "needs_attention": bool(warnings or operation.get("failed_count")),
            "undo_available": operation.get("undo_status") == "available",
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
            "embedding_recomputed": False,
        }

    @staticmethod
    def _all_cluster_operation_items(
        store: ClusterOperationStore,
        operation_id: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = store.operation_items(operation_id, offset=offset, limit=2_000)
            values = cast(list[dict[str, Any]], page["items"])
            items.extend(values)
            if not page["has_more"]:
                return items
            offset += len(values)

    def merge_image_clusters(self, cluster_ids: Iterable[str]) -> dict[str, object]:
        normalized = tuple(
            dict.fromkeys(
                str(value).strip() for value in cluster_ids if str(value).strip()
            )
        )
        if not 2 <= len(normalized) <= 100:
            raise ValueError("cluster_ids must contain 2 to 100 unique groups.")
        store, record, snapshot = self._required_cluster_snapshot()
        clusters = {cluster.cluster_id: cluster for cluster in snapshot.clusters}
        missing = [
            cluster_id for cluster_id in normalized if cluster_id not in clusters
        ]
        if missing:
            raise ValueError(f"Unknown cluster: {missing[0]}")
        representatives = [
            clusters[cluster_id].member_doc_ids[0] for cluster_id in normalized
        ]
        anchor = representatives[0]
        pairs = tuple(
            (anchor, representative) for representative in representatives[1:]
        )
        return self._execute_cluster_rule_operation(
            store=store,
            source_record=record,
            source_snapshot=snapshot,
            operation_type="merge",
            rule_kind="must_link",
            pairs=pairs,
            metadata={"cluster_ids": list(normalized)},
        )

    def split_image_cluster(
        self,
        cluster_id: str,
        doc_ids: Iterable[str],
    ) -> dict[str, object]:
        selected = tuple(
            dict.fromkeys(str(value).strip() for value in doc_ids if str(value).strip())
        )
        if not selected:
            raise ValueError("doc_ids must contain at least one image.")
        store, record, snapshot = self._required_cluster_snapshot()
        cluster = next(
            (value for value in snapshot.clusters if value.cluster_id == cluster_id),
            None,
        )
        if cluster is None:
            raise ValueError(f"Unknown cluster: {cluster_id}")
        member_set = set(cluster.member_doc_ids)
        if any(doc_id not in member_set for doc_id in selected):
            raise ValueError("Every split image must belong to the selected cluster.")
        pairs = tuple(
            sorted(
                {
                    tuple(sorted((selected_doc, other_doc)))
                    for selected_doc in selected
                    for other_doc in cluster.member_doc_ids
                    if selected_doc != other_doc
                }
            )
        )
        if not pairs:
            raise ValueError("A single-image cluster cannot be split.")
        if len(pairs) > 10_000:
            raise ValueError("The split operation would exceed 10000 constraints.")
        return self._execute_cluster_rule_operation(
            store=store,
            source_record=record,
            source_snapshot=snapshot,
            operation_type="split",
            rule_kind="must_not_link",
            pairs=cast(tuple[tuple[str, str], ...], pairs),
            metadata={"cluster_id": cluster_id, "doc_ids": list(selected)},
        )

    def apply_image_cluster_identity(
        self,
        cluster_id: str,
        *,
        identity_category: str,
        identity_value: str,
    ) -> dict[str, object]:
        category = str(identity_category).strip().casefold()
        value = str(identity_value).strip()
        if category not in IDENTITY_CATEGORIES:
            raise ValueError(
                "identity_category must be real_person, cosplayer, character, or work."
            )
        if not value or len(value) > 256:
            raise ValueError("identity_value must contain 1 to 256 characters.")
        store, record, snapshot = self._required_cluster_snapshot()
        cluster = next(
            (
                candidate
                for candidate in snapshot.clusters
                if candidate.cluster_id == cluster_id
            ),
            None,
        )
        if cluster is None:
            raise ValueError(f"Unknown cluster: {cluster_id}")
        if len(cluster.member_doc_ids) > 10_000:
            raise ValueError(
                "Identity propagation is limited to 10000 images per batch."
            )
        snapshots: dict[str, dict[str, Any]] = {}
        snapshot_errors: dict[str, str] = {}
        operation_items: list[ClusterOperationItemInput] = []
        for doc_id in cluster.member_doc_ids:
            try:
                before = self.auto_tagging.cluster_identity_snapshot(doc_id)
                snapshots[doc_id] = before
            except Exception as exc:
                before = {"snapshot_unavailable": True}
                snapshot_errors[doc_id] = str(exc) or exc.__class__.__name__
            operation_items.append(
                ClusterOperationItemInput(
                    item_key=f"identity-{sha256(doc_id.encode('utf-8')).hexdigest()[:24]}",
                    doc_id=doc_id,
                    cluster_id=cluster_id,
                    identity_category=category,
                    identity_value=value,
                    before=before,
                )
            )
        operation = store.create_operation(
            library_id=self._cluster_library_key(),
            operation_type="apply_identity",
            source_snapshot_version=record.snapshot_version,
            items=tuple(operation_items),
            before_snapshot={"snapshot_version": record.snapshot_version},
            metadata={
                "cluster_id": cluster_id,
                "identity_category": category,
                "identity_value": value,
            },
        )
        operation_id = str(operation["operation_id"])
        failures: list[dict[str, str]] = []
        applied = 0
        warnings: list[str] = []
        try:
            for index, doc_id in enumerate(cluster.member_doc_ids):
                self.cancel_check()
                if doc_id in snapshot_errors:
                    error = snapshot_errors[doc_id]
                    store.record_item_result(
                        operation_id,
                        index,
                        status="failed",
                        error_code="snapshot_unavailable",
                        error_message=error,
                    )
                    _append_active_learning_failure(failures, doc_id, error)
                    continue
                before = snapshots[doc_id]
                try:
                    self.auto_tagging.apply_cluster_identity(
                        doc_id,
                        category=category,
                        value=value,
                    )
                    after = self.auto_tagging.cluster_identity_snapshot(doc_id)
                    store.record_item_result(
                        operation_id,
                        index,
                        status="applied",
                        after=after,
                    )
                    applied += 1
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    try:
                        self.auto_tagging.restore_cluster_identity_snapshot(before)
                    except Exception as rollback_exc:
                        error = (
                            f"{error}; rollback failed: "
                            f"{str(rollback_exc) or rollback_exc.__class__.__name__}"
                        )
                    store.record_item_result(
                        operation_id,
                        index,
                        status="failed",
                        error_code="identity_apply_failed",
                        error_message=error,
                    )
                    _append_active_learning_failure(failures, doc_id, error)
            if applied:
                try:
                    self.auto_tagging.finalize_active_learning_changes()
                except Exception as exc:
                    warnings.append(str(exc) or exc.__class__.__name__)
            completed = store.complete_operation(
                operation_id,
                after_snapshot={"snapshot_version": record.snapshot_version},
                result_snapshot_version=record.snapshot_version,
            )
        except BaseException as exc:
            # The tag write and operation journal span separate stores.  Never
            # leave a cancelled batch in "running"; persisted applied items can
            # then be safely selected by the recovery undo path.
            with suppress(Exception):
                store.fail_operation(
                    operation_id,
                    error_code="operation_interrupted",
                    error_message=str(exc) or exc.__class__.__name__,
                )
            raise
        return self._cluster_operation_result(
            completed,
            failures=failures,
            warnings=warnings,
        )

    def undo_latest_cluster_operation(self) -> dict[str, object]:
        store = self._cluster_operation_store()
        if store is None:
            raise ValueError("Cluster operation history is unavailable.")
        operations = store.list_operations(
            self._cluster_library_key(), offset=0, limit=100
        )["items"]
        original = next(
            (
                item
                for item in cast(list[dict[str, Any]], operations)
                if item.get("operation_type") != "undo"
                and item.get("undo_status") in {"available", "needs_attention"}
            ),
            None,
        )
        if original is None:
            return {
                "undone": False,
                "operation_id": "",
                "restored": 0,
                "failed": 0,
                "conflicts": 0,
                "undo_available": False,
                "api_requests": 0,
            }
        original_id = str(original["operation_id"])
        undo = store.begin_undo(original_id)
        undo_id = str(undo["operation_id"])
        undo_items = self._all_cluster_operation_items(store, undo_id)
        failures: list[dict[str, str]] = []
        restored = 0
        conflicts = 0
        identity_changed = False
        rules_changed = False
        warnings: list[str] = []
        try:
            for item in undo_items:
                self.cancel_check()
                index = int(item["item_index"])
                doc_id = str(item.get("doc_id") or "")
                before = item.get("before")
                current = before.get("current") if isinstance(before, Mapping) else None
                restore = before.get("restore") if isinstance(before, Mapping) else None
                try:
                    if str(original["operation_type"]) in {"merge", "split"}:
                        if not isinstance(current, Mapping):
                            raise ValueError("Cluster rule undo state is incomplete.")
                        rule_id = str(current.get("rule_id") or "")
                        revoked = store.revoke_rule(rule_id, operation_id=undo_id)
                        if revoked.get("revoked_by_operation_id") != undo_id:
                            raise ActiveLearningError(
                                "The cluster rule changed after this operation."
                            )
                        rules_changed = True
                    else:
                        if not isinstance(current, Mapping) or not isinstance(
                            restore, Mapping
                        ):
                            raise ValueError("Identity undo snapshot is incomplete.")
                        if not self.auto_tagging.cluster_identity_matches_snapshot(
                            current
                        ):
                            raise ActiveLearningError(
                                "Identity tags changed after this operation; "
                                "newer edits were kept."
                            )
                        self.auto_tagging.restore_cluster_identity_snapshot(restore)
                        identity_changed = True
                    store.record_item_result(
                        undo_id,
                        index,
                        status="undone",
                        after=cast(Mapping[str, Any], restore or {}),
                    )
                    restored += 1
                except Exception as exc:
                    conflict = _is_cluster_operation_conflict(exc)
                    error = str(exc) or exc.__class__.__name__
                    store.record_item_result(
                        undo_id,
                        index,
                        status="conflict" if conflict else "undo_failed",
                        error_code=("undo_conflict" if conflict else "undo_failed"),
                        error_message=error,
                    )
                    conflicts += int(conflict)
                    _append_active_learning_failure(failures, doc_id, error)
            result_snapshot_version = str(original["result_snapshot_version"] or "")
            if rules_changed:
                source = store.load_snapshot(str(original["source_snapshot_version"]))
                updated = apply_manual_cluster_rules(
                    source,
                    self._active_cluster_rules(),
                )
                snapshot_record = store.save_snapshot(
                    self._cluster_library_key(),
                    updated,
                    metadata={"undo_of_operation_id": original_id},
                )
                result_snapshot_version = snapshot_record.snapshot_version
            if identity_changed:
                try:
                    self.auto_tagging.finalize_active_learning_changes()
                except Exception as exc:
                    warnings.append(str(exc) or exc.__class__.__name__)
            if not result_snapshot_version:
                active = store.active_snapshot_version(self._cluster_library_key())
                result_snapshot_version = (
                    active.snapshot_version
                    if active
                    else str(original["source_snapshot_version"])
                )
            completed = store.complete_operation(
                undo_id,
                after_snapshot={"snapshot_version": result_snapshot_version},
                result_snapshot_version=result_snapshot_version,
            )
        except BaseException as exc:
            # A cancelled undo may already have restored some images or rules.
            # Persist that fact so restart/retry can continue with only the
            # remaining items instead of stranding a running journal entry.
            with suppress(Exception):
                store.fail_operation(
                    undo_id,
                    error_code="undo_interrupted",
                    error_message=str(exc) or exc.__class__.__name__,
                )
            raise
        result = self._cluster_operation_result(
            completed,
            failures=failures,
            warnings=warnings,
        )
        result.update(
            {
                "undone": completed["status"] == "succeeded",
                "restored": restored,
                "conflicts": conflicts,
            }
        )
        return result

    def build_active_learning_review_queue(
        self, *, review_budget: int = 25
    ) -> dict[str, object]:
        self.cancel_check()
        review_store = self._active_learning_review_store()
        reviewed_doc_ids = self._active_learning_reviewed_doc_ids(review_store)
        snapshot = self._active_cluster_snapshot()
        if snapshot is None:
            snapshot = (
                ImageClusterService(
                    ImageClusteringConfig(
                        embedding_version=self.config.model,
                        enable_exact_hash=False,
                        enable_perceptual_hash=False,
                        enable_semantic=False,
                    )
                )
                .run((), cancel_check=self.cancel_check)
                .snapshot
            )
        entries = self.state.get_many(item.doc_id for item in snapshot.items)
        edge_scores: dict[str, list[float]] = defaultdict(list)
        for edge in snapshot.edges:
            if edge.kind == "semantic" and edge.score is not None:
                edge_scores[edge.left_doc_id].append(float(edge.score))
                edge_scores[edge.right_doc_id].append(float(edge.score))
        cluster_by_id = {cluster.cluster_id: cluster for cluster in snapshot.clusters}
        candidates: list[ActiveLearningCandidate] = []
        for index, item in enumerate(snapshot.items, 1):
            self.cancel_check()
            if item.doc_id in reviewed_doc_ids:
                continue
            entry = entries.get(item.doc_id, {})
            annotation = self.state.get_document_annotation(item.doc_id)
            if not entry or annotation is None:
                continue
            source_sha256 = str(entry.get("sha256") or "").strip().lower()
            if source_sha256 != str(annotation.get("source_sha256") or "").lower():
                # A stale annotation must be regenerated instead of reviewed.
                continue
            cluster = cluster_by_id[item.cluster_id]
            scores = edge_scores.get(item.doc_id, [])
            outlier_score = (
                min(1.0, max(0.0, 1.0 - sum(scores) / len(scores)))
                if scores
                else 0.65
                if len(cluster.member_doc_ids) == 1
                else 0.80
            )
            candidates.append(
                ActiveLearningCandidate(
                    doc_id=item.doc_id,
                    group_id=item.cluster_id,
                    candidate_kind="tag_review",
                    library_id=self.config.library_id or "",
                    source_sha256=source_sha256,
                    tag_snapshot=normalize_tags(
                        annotation.get("proposed_tags")
                        or entry.get("accepted_auto_tags")
                        or entry.get("effective_tags")
                        or ()
                    ),
                    conflict_score=1.0
                    if any(anchor.conflict for anchor in cluster.identity_anchors)
                    else 0.0,
                    outlier_score=outlier_score,
                    ranking_disagreement=0.0,
                    relative_path=str(entry.get("relative_path", "")),
                )
            )
            if index % 100 == 0:
                self.progress(
                    "Preparing active-learning candidates "
                    f"{index}/{len(snapshot.items)}"
                )
        queue = build_active_learning_queue(
            candidates, ActiveLearningConfig(total_budget=review_budget)
        )
        write_active_learning_queue(self.config.active_learning_queue_path, queue)
        return self._active_learning_queue_payload(queue)

    def review_active_learning_queue(
        self,
        *,
        queue_id: str,
        decisions: Iterable[Mapping[str, object]],
    ) -> dict[str, object]:
        queue = read_active_learning_queue(self.config.active_learning_queue_path)
        if queue.queue_id != queue_id:
            raise ActiveLearningError("The active-learning queue has changed.")
        review_store = self._active_learning_review_store()
        review_store.recover_incomplete()
        raw_decisions = list(decisions)
        review_items: list[ActiveLearningReviewItem] = []
        for decision in raw_decisions:
            labels = decision.get("labels", ())
            if isinstance(labels, (str, bytes)) or not isinstance(labels, Iterable):
                raise ActiveLearningError("Decision labels must be an array.")
            review_items.append(
                ActiveLearningReviewItem(
                    doc_id=str(decision.get("doc_id") or ""),
                    decision=cast(Any, str(decision.get("decision") or "")),
                    labels=tuple(str(value) for value in labels),
                )
            )
        batch = review_store.start_batch(
            queue_id=queue_id,
            library_id=self._active_learning_review_library_key(),
            items=review_items,
            metadata={"selection_version": queue.selection_version},
        )
        batch_id = str(batch["batch_id"])
        queue_items = {item.doc_id: item for item in queue.items}
        updated = queue
        applied = 0
        skipped = 0
        failed = 0
        conflicts = 0
        changed = False
        failures: list[dict[str, str]] = []
        warnings: list[str] = []
        for decision in raw_decisions:
            self.cancel_check()
            doc_id = str(decision.get("doc_id") or "").strip()
            action = str(decision.get("decision") or "").strip().lower()
            labels = decision.get("labels", ())
            label_values = tuple(str(value) for value in cast(Iterable[object], labels))
            item = queue_items.get(doc_id)
            if item is None:
                failed += 1
                error = "The queued image no longer exists."
                review_store.record_entry_outcome(
                    batch_id,
                    doc_id,
                    status="failed",
                    error_code="queue_item_missing",
                    error_message=error,
                )
                _append_active_learning_failure(failures, doc_id, error)
                continue
            if action == "skip":
                skipped += 1
                review_store.record_entry_outcome(
                    batch_id,
                    doc_id,
                    status="skipped",
                )
                updated = record_learning_decision(updated, doc_id, "skip", labels=())
                continue

            before: dict[str, Any] | None = None
            try:
                self._validate_active_learning_queue_item(item)
                if item.candidate_kind != "tag_review":
                    raise ActiveLearningError(
                        f"Unsupported active-learning candidate: {item.candidate_kind}"
                    )
                before = self.auto_tagging.active_learning_snapshot(doc_id)
                review_decision: dict[str, object] = {
                    "doc_id": doc_id,
                    "action": action,
                }
                if action == "edit":
                    review_decision["tags"] = list(label_values)
                self.auto_tagging.apply_active_learning_decision(review_decision)
                after = self.auto_tagging.active_learning_snapshot(doc_id)
                review_store.record_entry_outcome(
                    batch_id,
                    doc_id,
                    status="applied",
                    before_snapshot=before,
                    after_snapshot=after,
                    requires_undo=before != after,
                )
                applied += 1
                changed = changed or before != after
                updated = record_learning_decision(
                    updated,
                    doc_id,
                    cast(Any, action),
                    labels=label_values,
                )
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                conflict = _is_active_learning_conflict(exc)
                rollback_error = ""
                if before is not None:
                    try:
                        # A document update spans SQLite and Zvec. Restore the
                        # saved snapshot when any stage fails, then continue.
                        self.auto_tagging.restore_active_learning_snapshot(before)
                    except Exception as restore_exc:
                        rollback_error = (
                            str(restore_exc) or restore_exc.__class__.__name__
                        )
                if rollback_error:
                    error = f"{error}; rollback failed: {rollback_error}"
                    warnings.append(f"{doc_id}: {rollback_error}")
                status = "conflict" if conflict else "failed"
                review_store.record_entry_outcome(
                    batch_id,
                    doc_id,
                    status=cast(Any, status),
                    error_code=(
                        "review_state_conflict" if conflict else "review_apply_failed"
                    ),
                    error_message=error,
                )
                if conflict:
                    conflicts += 1
                else:
                    failed += 1
                _append_active_learning_failure(failures, doc_id, error)

        if changed:
            try:
                self.auto_tagging.finalize_active_learning_changes()
            except Exception as exc:
                warning = str(exc) or exc.__class__.__name__
                warnings.append(f"Tag catalog optimization failed: {warning}")
        finished = review_store.finish_batch(batch_id)
        try:
            write_active_learning_queue(self.config.active_learning_queue_path, updated)
        except ActiveLearningError as exc:
            warnings.append(str(exc))
        payload = self._active_learning_queue_payload(updated)
        payload.update(
            {
                "batch_id": batch_id,
                "status": finished["status"],
                "applied": applied,
                "failed": failed,
                "skipped": skipped,
                "conflicts": conflicts,
                "failures": failures,
                "warnings": warnings,
                "needs_attention": bool(warnings or failed or conflicts),
                "undo_available": bool(finished["undo_available"]),
            }
        )
        return payload

    def undo_latest_active_learning_review(self) -> dict[str, object]:
        review_store = self._active_learning_review_store()
        review_store.recover_incomplete()
        library_id = self._active_learning_review_library_key()
        batches = review_store.list_batches(library_id=library_id, limit=50)["items"]
        target = next(
            (item for item in batches if bool(item.get("undo_available"))),
            None,
        )
        if target is None:
            return {
                "undone": False,
                "batch_id": "",
                "restored": 0,
                "failed": 0,
                "conflicts": 0,
                "failures": [],
                "undo_available": False,
                "api_requests": 0,
            }
        batch_id = str(target["batch_id"])
        undo = review_store.begin_undo(batch_id)
        restored = 0
        failed = 0
        conflicts = 0
        changed = False
        failures: list[dict[str, str]] = []
        for entry in undo["entries"]:
            self.cancel_check()
            doc_id = str(entry["doc_id"])
            before = entry.get("before_snapshot")
            after = entry.get("after_snapshot")
            try:
                if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                    raise ActiveLearningError("Review undo snapshot is incomplete.")
                if not self.auto_tagging.active_learning_matches_snapshot(after):
                    raise ActiveLearningError(
                        "The image tags changed after this review; "
                        "newer edits were kept."
                    )
                self.auto_tagging.restore_active_learning_snapshot(before)
                review_store.record_undo_outcome(
                    batch_id,
                    doc_id,
                    status="undone",
                )
                restored += 1
                changed = True
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                conflict = _is_active_learning_conflict(exc)
                review_store.record_undo_outcome(
                    batch_id,
                    doc_id,
                    status="conflict" if conflict else "failed",
                    error_code=(
                        "undo_state_conflict" if conflict else "undo_restore_failed"
                    ),
                    error_message=error,
                )
                if conflict:
                    conflicts += 1
                else:
                    failed += 1
                _append_active_learning_failure(failures, doc_id, error)
        warnings: list[str] = []
        if changed:
            try:
                self.auto_tagging.finalize_active_learning_changes()
            except Exception as exc:
                warnings.append(str(exc) or exc.__class__.__name__)
        finished = review_store.finish_undo(batch_id)
        return {
            "undone": finished["status"] == "undone",
            "batch_id": batch_id,
            "restored": restored,
            "failed": failed,
            "conflicts": conflicts,
            "failures": failures,
            "warnings": warnings,
            "needs_attention": bool(warnings or failed or conflicts),
            "undo_available": bool(finished["undo_available"]),
            "api_requests": 0,
            "embedding_api_requests": 0,
            "qwen_api_requests": 0,
        }

    def _active_learning_review_store(self) -> ActiveLearningReviewStore:
        store = getattr(self, "active_learning_reviews", None)
        if store is None:
            raise ActiveLearningError(
                "Active-learning review history is unavailable for this library."
            )
        return store

    def _active_learning_reviewed_doc_ids(
        self,
        store: ActiveLearningReviewStore,
    ) -> set[str]:
        reviewed: set[str] = set()
        cursor: str | None = None
        library_id = self._active_learning_review_library_key()
        while True:
            page = store.list_applied_doc_ids(
                library_id=library_id,
                limit=10_000,
                after_doc_id=cursor,
            )
            reviewed.update(str(value) for value in page["items"])
            if not page["has_more"]:
                return reviewed
            cursor = cast(str, page["next_cursor"])

    def _active_learning_review_library_key(self) -> str:
        library_id = self.config.library_id or "local"
        collection_uuid = str(
            getattr(self.repository, "collection_uuid", "") or "unbound"
        )
        return f"{library_id}@{collection_uuid}"

    def _validate_active_learning_queue_item(
        self,
        item: Any,
    ) -> None:
        entry = self.state.get(str(item.doc_id))
        if entry is None:
            raise ActiveLearningError("The indexed image no longer exists.")
        current_sha256 = str(entry.get("sha256") or "").strip().lower()
        if item.source_sha256 and current_sha256 != item.source_sha256:
            raise ActiveLearningError(
                "The image changed after the review queue was generated."
            )
        annotation = self.state.get_document_annotation(str(item.doc_id))
        if annotation is None:
            raise ActiveLearningError("The image annotation no longer exists.")
        current_tags = normalize_tags(
            annotation.get("proposed_tags")
            or entry.get("accepted_auto_tags")
            or entry.get("effective_tags")
            or ()
        )
        if item.tag_snapshot and current_tags != item.tag_snapshot:
            raise ActiveLearningError(
                "The image tags changed after the review queue was generated."
            )

    def _cluster_image_payload(
        self,
        doc_id: str,
        entry: Mapping[str, object],
    ) -> dict[str, object]:
        """Return one internal media record for safe WebView registration.

        ``source_path`` is used only on the authenticated loopback boundary and
        is removed by the WebView facade before the JSON reaches the browser.
        """

        relative_path = str(entry.get("relative_path") or "")
        payload: dict[str, object] = {
            "doc_id": doc_id,
            "relative_path": relative_path,
            "file_name": Path(relative_path).name if relative_path else doc_id,
        }
        with suppress(OSError, ValueError, ConfigurationError):
            payload["source_path"] = str(
                self.source_resolver.resolve_fields(dict(entry))
            )
        return payload

    def _active_learning_queue_payload(
        self, queue: ActiveLearningQueue
    ) -> dict[str, object]:
        payload = queue.to_dict()
        entries = self.state.get_many(item.doc_id for item in queue.items)
        enriched: list[dict[str, object]] = []
        for index, item in enumerate(queue.items, 1):
            self.cancel_check()
            entry = entries.get(item.doc_id, {})
            item_payload = item.to_dict()
            item_payload.update(self._cluster_image_payload(item.doc_id, entry))
            item_payload["suggested_tags"] = list(
                item.tag_snapshot
                or entry.get("effective_tags")
                or entry.get("accepted_auto_tags")
                or entry.get("tags")
                or ()
            )
            enriched.append(item_payload)
            if index % 100 == 0:
                self.progress(
                    f"Loading active-learning samples {index}/{len(queue.items)}"
                )
        payload["items"] = enriched
        payload["processed"] = len(enriched)
        candidate_count = payload.get("candidate_count")
        has_candidate_count = isinstance(candidate_count, int) and not isinstance(
            candidate_count, bool
        )
        payload["total"] = candidate_count if has_candidate_count else len(enriched)
        payload["embedding_api_requests"] = 0
        payload["qwen_api_requests"] = 0
        payload["embedding_recomputed"] = False
        return payload

    def stats(self) -> dict:
        collection_stats = self.repository.stats
        return {
            "workspace": str(self.config.workspace),
            "collection": str(self.config.collection_path),
            "collection_uuid": self.repository.collection_uuid,
            "collection_stats": {
                "doc_count": int(collection_stats.doc_count),
                "index_completeness": dict(collection_stats.index_completeness),
            },
            "tracked_files": self.state.count(),
            "roots": self.state.list_roots(),
            "embedding_cache": self.state.cache_stats(),
            "model": self.config.model,
            "mode": "independent",
            "dimension": self.config.dimension,
            "metric": self.config.metric,
            "search_quality": self.search_quality.to_dict(),
        }

    def backfill_metadata_embeddings(self, max_images: int = 200) -> dict[str, Any]:
        """Explicitly embed pending structured descriptions for schema-v4 documents.

        Successful documents are committed one at a time.  A later invocation
        compares the stored metadata hash and naturally resumes only pending or
        stale entries; this method is never called during service startup.
        """

        if (
            isinstance(max_images, bool)
            or not isinstance(max_images, int)
            or not 1 <= max_images <= 10_000
        ):
            raise ValueError("max_images must be between 1 and 10000.")

        fetch_metadata = getattr(self.repository, "fetch_metadata", None)
        upsert_metadata = getattr(
            self.repository,
            "upsert_metadata_embedding",
            None,
        )
        if not callable(fetch_metadata) or not callable(upsert_metadata):
            raise ConfigurationError(
                "Metadata embeddings require Collection schema v4. "
                "Run 'image_service.py migrate-schema' before starting backfill."
            )

        entries = self.state.list_entries()
        candidates: list[MetadataBackfillItem] = []
        discovery_failures: list[dict[str, str]] = []
        discovery_failed = 0
        skipped_empty = 0
        already_current = 0
        failure_detail_limit = 100

        for index, entry in enumerate(entries, start=1):
            self.cancel_check()
            if index == 1 or index % 100 == 0 or index == len(entries):
                self.progress(f"Metadata backfill scan {index}/{len(entries)}.")
            doc_id = str(entry.get("doc_id") or "").strip()
            try:
                if not doc_id:
                    raise ValueError("State entry has no doc_id.")
                annotation = self.state.get_document_annotation(doc_id)
                metadata_text = build_metadata_text(entry, annotation)
                if not metadata_text.text:
                    skipped_empty += 1
                    continue
                stored = fetch_metadata(doc_id)
                if stored is None:
                    raise ConfigurationError(
                        "Document exists in SQLite state but not in the Collection."
                    )
                if (
                    str(stored.get("metadata_text_hash") or "").lower()
                    == metadata_text.sha256
                ):
                    already_current += 1
                    continue
                candidates.append(
                    MetadataBackfillItem(
                        doc_id=doc_id,
                        text=metadata_text.text,
                        text_hash=metadata_text.sha256,
                        truncated=metadata_text.truncated,
                    )
                )
            except Exception as exc:
                discovery_failed += 1
                if len(discovery_failures) < failure_detail_limit:
                    discovery_failures.append(
                        {
                            "doc_id": doc_id,
                            "stage": "discovery",
                            "error": str(exc) or exc.__class__.__name__,
                            "error_type": exc.__class__.__name__,
                        }
                    )

        selected = candidates[:max_images]

        def still_current(item: MetadataBackfillItem) -> bool:
            current_entry = self.state.get(item.doc_id)
            if current_entry is None:
                return False
            current_annotation = self.state.get_document_annotation(item.doc_id)
            current_text = build_metadata_text(current_entry, current_annotation)
            return bool(current_text.text) and current_text.sha256 == item.text_hash

        def commit(item: MetadataBackfillItem, vector: list[float]) -> None:
            upsert_metadata(
                item.doc_id,
                item.text,
                item.text_hash,
                vector,
            )

        if selected:
            configured_client = self.embedding_client
            client_cancel_event = threading.Event()
            client = (
                DashScopeEmbeddingClient(
                    self.config,
                    limiter=configured_client.limiter,
                    cancel_event=client_cancel_event,
                )
                if isinstance(configured_client, DashScopeEmbeddingClient)
                else configured_client
            )
            runner = MetadataBackfillRunner(
                client=client,
                commit=commit,
                expected_dimension=self.config.dimension,
                progress=self.progress,
                cancel_check=self.cancel_check,
                still_current=still_current,
                client_cancel_event=client_cancel_event,
                concurrency=self.config.embedding_concurrency,
            )
            runner_report = runner.run(selected, eligible=len(candidates))
            limiter = getattr(client, "limiter", None)
            snapshot = (
                limiter.snapshot().as_dict()
                if limiter is not None and callable(getattr(limiter, "snapshot", None))
                else None
            )
        else:
            runner_report = MetadataBackfillReport(
                selected=0,
                eligible=len(candidates),
            )
            self.cancel_check()
            self.progress("Metadata backfill 0/0: no pending descriptions.")
            snapshot = None

        report = runner_report.to_dict()
        runner_failures = list(report["failures"])
        report.update(
            {
                "scanned": len(entries),
                "skipped_empty": skipped_empty,
                "already_current": already_current,
                "eligible": len(candidates) + discovery_failed,
                "deferred": max(0, len(candidates) - len(selected)),
                "failed": int(report["failed"]) + discovery_failed,
                "remaining": (
                    max(0, len(candidates) - int(report["succeeded"]))
                    + discovery_failed
                ),
                "failures": (discovery_failures + runner_failures)[
                    :failure_detail_limit
                ],
                "rate_limit": {
                    "shared_process_limiter": True,
                    "backfill_concurrency": report["concurrency"],
                    "configured_backfill_concurrency": (
                        self.config.embedding_concurrency
                    ),
                    "requests_per_minute": (self.config.rate_limit_requests_per_minute),
                    "tokens_per_minute": self.config.rate_limit_tokens_per_minute,
                    "hard_requests_per_minute": (
                        self.config.rate_limit_hard_requests_per_minute
                    ),
                    "hard_tokens_per_minute": (
                        self.config.rate_limit_hard_tokens_per_minute
                    ),
                    "snapshot": snapshot,
                },
            }
        )
        self.logger.info(
            "metadata_backfill scanned=%d selected=%d succeeded=%d failed=%d "
            "remaining=%d api_requests=%d",
            report["scanned"],
            report["selected"],
            report["succeeded"],
            report["failed"],
            report["remaining"],
            report["api_requests"],
        )
        return report

    def clear_embedding_cache(self) -> dict[str, int]:
        deleted = self.state.clear_cache()
        self.logger.info("cache_clear deleted=%d", deleted)
        return {"deleted": deleted}

    def list_roots(self) -> list[dict]:
        return self.state.list_roots()

    def rebind_root(self, root_id: str, new_path: str) -> dict[str, object]:
        report = self.state.rebind_root(root_id, new_path)
        missing = 0
        for entry in self.state.entries_for_root(root_id):
            source = self.source_resolver.resolve_fields(entry)
            if not source.is_file():
                missing += 1
        report["missing_files"] = missing
        self.logger.info("root_rebind missing_files=%d", missing)
        return report

    def clean_results(
        self, older_than_days: int, dry_run: bool = False
    ) -> dict[str, object]:
        report = clean_result_directories(
            self.config.results_path,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )
        failures = report["failures"]
        failure_count = len(failures) if isinstance(failures, list) else 0
        self.logger.info(
            "results_clean days=%d dry_run=%s matched=%d deleted=%d failures=%d",
            older_than_days,
            dry_run,
            report["matched"],
            report["deleted"],
            failure_count,
        )
        return report

    def cleanup_search_results(
        self,
        *,
        keep_latest: int = 3,
        dry_run: bool = False,
    ) -> dict[str, object]:
        report = cleanup_search_results(
            self.config.results_path,
            keep_latest=keep_latest,
            dry_run=dry_run,
        )
        self.logger.info(
            "search_results_cleanup keep_latest=%d dry_run=%s owned=%d "
            "deleted=%d skipped=%d failed=%d",
            keep_latest,
            dry_run,
            report["owned"],
            report["deleted"],
            report["skipped"],
            report["failed"],
        )
        return report

    def _validate_top_k(self, top_k: int) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive.")
        if self.config.max_top_k is not None and top_k > self.config.max_top_k:
            raise ValueError(f"top_k must not exceed {self.config.max_top_k}.")

    def _validate_search_tags(
        self, tags: Iterable[str] | None, tag_mode: str
    ) -> tuple[str, ...]:
        if tag_mode not in {"all", "any"}:
            raise ValueError("tag_mode must be 'all' or 'any'.")
        normalized = normalize_tags(tags)
        # Alias groups are shared by all Collections and can be edited while the
        # backend is running. Reload them before a filtered search so every
        # Collection observes the same dictionary without a restart.
        if normalized:
            self._refresh_tag_catalog()
        return normalized

    @staticmethod
    def _validate_weights(image_weight: float, text_weight: float) -> None:
        if image_weight < 0 or text_weight < 0 or image_weight + text_weight <= 0:
            raise ValueError("Search weights must be non-negative and not both zero.")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        if hasattr(self, "state"):
            self.state.close()
        if hasattr(self, "auto_tag_cache"):
            self.auto_tag_cache.close()
        if hasattr(self, "logger"):
            close_app_logger(self.logger)
        if hasattr(self, "_lock"):
            self._lock.release()
        self._closed = True

    def __del__(self):
        with suppress(Exception):
            self.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def _average_image_hash(path: Path) -> str:
    """Return a small local perceptual hash without retaining decoded pixels."""

    with Image.open(path) as image:
        grayscale = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(grayscale.tobytes())
    average = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | int(value >= average)
    return f"{bits:016x}"


def _cluster_identity_evidence(
    entry: Mapping[str, Any], annotation: Mapping[str, Any] | None
) -> tuple[IdentityEvidence, ...]:
    if annotation is None:
        return ()
    structured = annotation.get("structured")
    if not isinstance(structured, Mapping):
        return ()
    raw_entities = structured.get("entities")
    if not isinstance(raw_entities, Mapping):
        return ()
    sources = {
        "manual": {_tag_key(value) for value in entry.get("tags", ())},
        "folder": {_tag_key(value) for value in entry.get("folder_tags", ())},
        "accepted": {_tag_key(value) for value in entry.get("accepted_auto_tags", ())},
        "inherited": {_tag_key(value) for value in entry.get("inherited_tags", ())},
    }
    category_map = {
        "real_person": "real_person",
        "person": "real_person",
        "cosplayer": "cosplayer",
        "character": "character",
        "work": "work",
        "series": "work",
    }
    evidence: dict[tuple[str, str], IdentityEvidence] = {}
    source_lookup_order = ("manual", "folder", "accepted", "inherited")
    source_priority = ("manual", "folder", "accepted", "model", "inherited")
    for raw_category, raw_values in raw_entities.items():
        category = category_map.get(str(raw_category).strip().lower())
        if category is None:
            continue
        if isinstance(raw_values, Mapping):
            values: Iterable[object] = (raw_values,)
        elif isinstance(raw_values, Iterable) and not isinstance(
            raw_values, (str, bytes)
        ):
            values = raw_values
        else:
            continue
        for raw_value in values:
            if not isinstance(raw_value, Mapping):
                continue
            name = str(raw_value.get("name") or "").strip()
            if not name:
                continue
            key = _tag_key(name)
            source = next(
                (
                    candidate
                    for candidate in source_lookup_order
                    if key in sources[candidate]
                ),
                "model",
            )
            raw_confidence = raw_value.get("confidence", 1.0)
            confidence = (
                min(1.0, max(0.0, float(raw_confidence)))
                if isinstance(raw_confidence, (int, float))
                and not isinstance(raw_confidence, bool)
                else 1.0
            )
            candidate = IdentityEvidence(category, name, source, confidence)
            current = evidence.get((category, key))
            if current is None or source_priority.index(source) < source_priority.index(
                current.source
            ):
                evidence[(category, key)] = candidate
    return tuple(
        evidence[key]
        for key in sorted(evidence, key=lambda value: (value[0], value[1]))
    )


def _tag_key(value: object) -> str:
    return str(value).strip().casefold()


def _matched_source_tags(value: object, matched: set[str]) -> list[str]:
    if (
        not matched
        or isinstance(value, (str, bytes))
        or not isinstance(value, Iterable)
    ):
        return []
    return [
        tag for raw in value if (tag := str(raw).strip()) and _tag_key(tag) in matched
    ]


def _annotation_learning_evidence(
    annotation: Mapping[str, Any], matched: set[str]
) -> tuple[list[str], dict[str, float]]:
    if not matched:
        return [], {}
    structured = annotation.get("structured")
    if not isinstance(structured, Mapping):
        return [], {}
    high_confidence: list[str] = []
    signals: dict[str, float] = {}

    raw_fields = structured.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    field_signals = {
        "action": "action_match",
        "expression": "expression_match",
        "scene": "scene_match",
    }
    for field_name, raw_field in fields.items():
        if not isinstance(raw_field, Mapping):
            continue
        confidence = raw_field.get("confidence")
        confidence_value = (
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else 0.0
        )
        labels = _matched_source_tags(raw_field.get("labels"), matched)
        if confidence_value >= 0.85:
            high_confidence.extend(labels)
        signal_name = field_signals.get(str(field_name).strip().lower())
        if signal_name and labels:
            signals[signal_name] = 1.0

    raw_entities = structured.get("entities")
    entities = raw_entities if isinstance(raw_entities, Mapping) else {}
    for entity_type, raw_values in entities.items():
        if isinstance(raw_values, Mapping):
            values: Iterable[object] = (raw_values,)
        elif isinstance(raw_values, Iterable) and not isinstance(
            raw_values, (str, bytes)
        ):
            values = raw_values
        else:
            continue
        matched_entity = False
        for raw_entity in values:
            if not isinstance(raw_entity, Mapping):
                continue
            name = str(raw_entity.get("name") or "").strip()
            if not name or _tag_key(name) not in matched:
                continue
            matched_entity = True
            confidence = raw_entity.get("confidence")
            if (
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and float(confidence) >= 0.85
            ):
                high_confidence.append(name)
        normalized_type = str(entity_type).strip().lower()
        if matched_entity and normalized_type in {
            "real_person",
            "person",
            "cosplayer",
            "character",
        }:
            signals["identity_match"] = 1.0
        if matched_entity and normalized_type in {"work", "series"}:
            signals["work_match"] = 1.0
    return list(normalize_tags(high_confidence)), signals


def _tag_match_confidence(
    plan: TagSearchPlan,
    matched_tags: tuple[str, ...],
) -> float:
    """Score exact, alias and substring tag matches without implying probability."""

    if not matched_tags or not plan.expansions:
        return 0.0
    normalized_matches = {tag: normalize_tag_search_text(tag) for tag in matched_tags}
    expansion_scores: list[float] = []
    for expansion in plan.expansions:
        allowed = {normalize_tag_search_text(tag) for tag in expansion.matches}
        equivalents = {
            normalize_tag_search_text(term) for term in expansion.expanded_terms
        }
        best = 0.0
        for normalized_tag in normalized_matches.values():
            if normalized_tag not in allowed:
                continue
            fragment = expansion.normalized_fragment
            if normalized_tag == fragment:
                score = 1.0
            elif normalized_tag in equivalents:
                score = 0.96
            else:
                ratio = min(1.0, len(fragment) / max(1, len(normalized_tag)))
                if normalized_tag.startswith(fragment):
                    score = 0.85 + 0.10 * ratio
                elif fragment in normalized_tag:
                    score = 0.70 + 0.15 * ratio
                else:
                    score = 0.65
            best = max(best, score)
        expansion_scores.append(best)
    return min(1.0, max(0.0, sum(expansion_scores) / len(expansion_scores)))


def _embed_batch_with_activity(
    client: object,
    batch: list[tuple[str, list[ImageRecord]]],
    metrics: _PipelineNetworkMetrics | None,
) -> _EmbeddingBatchResult:
    if metrics is not None:
        metrics.embedding_started()
    try:
        return _embed_batch_resilient(client, batch)
    finally:
        if metrics is not None:
            metrics.embedding_finished()


def _embed_batch_resilient(
    client: object,
    batch: list[tuple[str, list[ImageRecord]]],
) -> _EmbeddingBatchResult:
    result = _EmbeddingBatchResult()
    if not batch:
        return result
    try:
        response: EmbeddingResponse = client.embed_images(  # type: ignore[attr-defined]
            [Path(records[0].absolute_path) for _sha, records in batch]
        )
    except (DashScopeError, ImageInputError) as exc:
        if getattr(exc, "splittable", False) and len(batch) > 1:
            midpoint = len(batch) // 2
            first = _embed_batch_resilient(client, batch[:midpoint])
            result.extend(first)
            if first.systemic_error:
                result.systemic_items.extend(batch[midpoint:])
                return result
            result.extend(_embed_batch_resilient(client, batch[midpoint:]))
            return result
        kind = _classify_embedding_error(exc)
        message = str(exc) or exc.__class__.__name__
        if kind == "systemic":
            result.systemic_error = message
            result.systemic_items.extend(batch)
        else:
            result.failures.append(_EmbeddingFailure(batch, message, kind))
        return result
    except Exception as exc:
        result.systemic_error = str(exc) or exc.__class__.__name__
        result.systemic_items.extend(batch)
        return result

    if len(response.vectors) != len(batch):
        result.systemic_error = (
            f"Expected {len(batch)} embedding vectors, received "
            f"{len(response.vectors)}."
        )
        result.systemic_items.extend(batch)
        return result
    if response.request_id:
        result.usage.append(
            {"request_id": response.request_id, "usage": response.usage}
        )
    result.successes.extend(zip(batch, response.vectors, strict=True))
    return result


def _classify_embedding_error(error: DashScopeError | ImageInputError) -> FailureKind:
    if isinstance(error, ImageInputError) or getattr(error, "splittable", False):
        return "item"
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return "systemic"
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return "retryable"
    message = str(error).casefold()
    if status_code is None and any(
        token in message
        for token in ("request failed", "timed out", "timeout", "connection")
    ):
        return "retryable"
    return "systemic"


def _classify_storage_error(_error: str) -> FailureKind:
    # Per-document failures returned by the repository still originate in the
    # Zvec storage layer. Treating them as bad images would copy an entire healthy
    # library when the disk, collection, or database is the real problem.
    return "systemic"


def _embedding_batch_bytes(
    batch: list[tuple[str, list[ImageRecord]]],
) -> int:
    return sum(4 * ((records[0].size_bytes + 2) // 3) + 4096 for _sha, records in batch)


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _record_is_stable(record: ImageRecord) -> bool:
    try:
        stat = Path(record.absolute_path).stat()
    except OSError:
        return False
    return (stat.st_size, stat.st_mtime_ns) == (
        record.size_bytes,
        record.mtime_ns,
    )


def _usable_hit_count(
    hits: list[SearchHit],
    exclude_path: str | None,
    resolve_source: Callable[[SearchHit], Path],
) -> int:
    normalized_exclude = (
        os.path.normcase(str(Path(exclude_path).resolve())) if exclude_path else None
    )
    hashes: set[str] = set()
    count = 0
    for hit in hits:
        try:
            source = resolve_source(hit)
        except (OSError, ValueError, ConfigurationError):
            continue
        if not source.is_file():
            continue
        if (
            normalized_exclude
            and os.path.normcase(str(source.resolve())) == normalized_exclude
        ):
            continue
        sha256 = str(hit.fields.get("sha256") or "")
        if sha256 and sha256 in hashes:
            continue
        hashes.add(sha256)
        count += 1
    return count


def _exclude_content_hash(
    hits: list[SearchHit], exclude_sha256: str | None
) -> list[SearchHit]:
    if not exclude_sha256:
        return hits
    return [
        hit for hit in hits if str(hit.fields.get("sha256") or "") != exclude_sha256
    ]


def _manual_tags_after_operation(
    before: Iterable[str],
    requested: Iterable[str],
    operation: str,
) -> tuple[str, ...]:
    current = normalize_tags(before)
    values = normalize_tags(requested)
    if operation == "add":
        return normalize_tags([*current, *values])
    if operation == "remove":
        removed = set(values)
        return tuple(tag for tag in current if tag not in removed)
    if operation == "replace_manual":
        return values
    raise ValueError("Unsupported manual tag operation.")


def _record_from_state_entry(entry: Mapping[str, Any]) -> ImageRecord:
    return ImageRecord(
        doc_id=str(entry["doc_id"]),
        root_id=str(entry["root_id"]),
        relative_path=str(entry["relative_path"]),
        # Tag-only Collection writes do not read the source image. Keeping this
        # empty lets manual work continue while an image root is offline.
        absolute_path="",
        file_name=str(entry["file_name"]),
        extension=str(entry["extension"]),
        mime_type=str(entry["mime_type"]),
        sha256=str(entry["sha256"]),
        size_bytes=int(entry["size_bytes"]),
        mtime_ns=int(entry["mtime_ns"]),
        width=int(entry["width"]),
        height=int(entry["height"]),
    )


def _entry_is_in_folder(
    entry: Mapping[str, Any],
    *,
    root_id: str,
    relative_folder: str,
    include_subfolders: bool,
) -> bool:
    if str(entry.get("root_id") or "") != root_id:
        return False
    parent = str(entry.get("parent_directory") or "").replace("\\", "/")
    normalized = str(relative_folder).replace("\\", "/").strip("/")
    if parent == normalized:
        return True
    return include_subfolders and (
        not normalized or parent.startswith(f"{normalized}/")
    )


def _append_manual_failure(
    failures: list[dict[str, str]],
    *,
    doc_id: str,
    relative_path: str,
    error: str,
) -> None:
    if len(failures) >= 100:
        return
    failures.append(
        {
            "doc_id": str(doc_id),
            "relative_path": str(relative_path),
            "error": str(error),
        }
    )


def _append_active_learning_failure(
    failures: list[dict[str, str]],
    doc_id: str,
    error: str,
) -> None:
    if len(failures) >= 100:
        return
    failures.append({"doc_id": str(doc_id), "error": str(error)})


def _is_active_learning_conflict(error: BaseException) -> bool:
    message = (str(error) or "").casefold()
    return isinstance(error, ActiveLearningError) and any(
        marker in message
        for marker in (
            "changed after",
            "no longer exists",
            "queue has changed",
            "annotation no longer exists",
            "newer edits",
        )
    )


def _is_cluster_operation_conflict(error: BaseException) -> bool:
    message = (str(error) or "").casefold()
    return isinstance(
        error,
        (ActiveLearningError, ClusterOperationValidationError),
    ) and any(
        marker in message
        for marker in (
            "changed after",
            "newer edits",
            "opposite manual rule",
            "already",
            "conflict",
        )
    )


def _cluster_rule_item_key(left: str, right: str) -> str:
    digest = sha256("\0".join((left, right)).encode()).hexdigest()[:24]
    return f"rule-{digest}"
