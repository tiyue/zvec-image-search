from __future__ import annotations

import os
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from zvec_logging import initialize_zvec

from .annotation_service import AutoTaggingCoordinator, StreamingAutoTagSession
from .app_logging import close_app_logger, get_app_logger
from .auto_tag_cache import SharedAutoTagCache
from .config import ConfigurationError, ServiceConfig
from .dashscope_client import (
    DashScopeEmbeddingClient,
    DashScopeError,
    EmbeddingResponse,
    ImageInputError,
)
from .failure_sink import IndexFailureSink
from .hybrid_search import (
    HybridTagIntent,
    detect_hybrid_tag_intent,
    hybrid_candidate_count,
)
from .image_scanner import (
    file_sha256,
    inspect_query_image,
    scan_folder,
)
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
)
from .process_lock import ProcessLock
from .rank_fusion import (
    DEFAULT_AGREEMENT_REWARD,
    DEFAULT_RANK_DECAY,
    DEFAULT_WEAK_CHANNEL_FLOOR,
    DEFAULT_WEAK_CHANNEL_PENALTY,
    MAX_CONFIDENCE_CANDIDATES,
    ConfidenceRanking,
    confidence_rank,
)
from .result_diversity import DiversityRanking, diversify_search_hits
from .result_exporter import clean_result_directories, export_results
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
            self.state_reset = self.state.ensure_collection_uuid(
                self.repository.collection_uuid,
                reset_if_unbound=self.repository.created,
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
            effective_tags = normalize_tags(
                [*manual_tags, *folder_tags, *accepted_auto_tags]
            )
            records_by_tags[effective_tags].append(record)
            state_values[record.doc_id] = {
                "tags": list(manual_tags),
                "folder_tags": list(folder_tags),
                "accepted_auto_tags": list(accepted_auto_tags),
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
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
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
            )
            if self.search_quality.configured
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
        candidate_count = ranking.candidate_count if ranking is not None else len(hits)
        diversity = diversify_search_hits(
            hits,
            top_k=top_k,
            enabled=diversify_results,
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
            ),
            status=ranking.status if ranking is not None else "ok",
            candidate_count=candidate_count,
            filtered_count=(
                ranking.filtered_count
                if ranking is not None
                else max(0, candidate_count - len(hits))
            ),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode=ranking_mode,
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
    ) -> SearchReport:
        """Search locally by fuzzy tag fragments without creating an embedding."""

        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        text = text.strip()
        if not text:
            raise ValueError("Tag search text cannot be empty.")

        candidates = self._tag_search_hits(
            text,
            tags=normalized_tags,
            tag_mode=tag_mode,
        )
        diversity = diversify_search_hits(
            candidates,
            top_k=top_k,
            enabled=diversify_results,
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
            },
            status="ok" if hits else "no_reliable_match",
            candidate_count=len(candidates),
            filtered_count=max(0, len(candidates) - len(hits)),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode="tag_match",
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
    ) -> dict[str, object]:
        return self.auto_tagging.review_batch(
            proposal_ids=proposal_ids,
            accepted_tags_by_proposal=accepted_tags_by_proposal,
            exclude_identity_tags=exclude_identity_tags,
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
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
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
            )
            if self.search_quality.configured
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
        candidate_count = ranking.candidate_count if ranking is not None else len(hits)
        diversity = diversify_search_hits(
            hits,
            top_k=top_k,
            enabled=diversify_results,
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
            ),
            status=ranking.status if ranking is not None else "ok",
            candidate_count=candidate_count,
            filtered_count=(
                ranking.filtered_count
                if ranking is not None
                else max(0, candidate_count - len(hits))
            ),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode="confidence" if ranking is not None else "distance",
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
    ) -> SearchReport:
        started_at = perf_counter()
        self.cancel_check()
        self._validate_top_k(top_k)
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
            )
            if self.search_quality.configured
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
        candidate_count = (
            ranking.candidate_count if ranking is not None else len(fused_hits)
        )
        diversity = diversify_search_hits(
            fused_hits,
            top_k=top_k,
            enabled=diversify_results,
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
            ),
            status=ranking.status if ranking is not None else "ok",
            candidate_count=candidate_count,
            filtered_count=(
                ranking.filtered_count
                if ranking is not None
                else max(0, candidate_count - len(fused_hits))
            ),
            latency_ms=(perf_counter() - started_at) * 1000,
            ranking_mode=(
                self.search_quality.fusion_mode
                if ranking is not None
                and self.search_quality.fusion_mode == "confidence_v2"
                else "confidence"
                if ranking is not None
                else str(metadata_search.get("mode") or "weighted_rrf")
            ),
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
        return [
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
        return diagnostics

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
    ) -> ConfidenceRanking:
        if query_type not in {"text", "image"}:
            raise ValueError(f"Unsupported single query type: {query_type}")
        total = self.repository.doc_count
        if total == 0:
            return ConfidenceRanking([], "no_reliable_match", 0, 0)
        candidate_k = min(total, MAX_CONFIDENCE_CANDIDATES)
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
            max_candidates=MAX_CONFIDENCE_CANDIDATES,
            show_low_confidence=show_low_confidence,
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
    ) -> ConfidenceRanking:
        total = self.repository.doc_count
        if total == 0:
            return ConfidenceRanking([], "no_reliable_match", 0, 0)
        candidate_k = min(total, MAX_CONFIDENCE_CANDIDATES)
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
            max_candidates=MAX_CONFIDENCE_CANDIDATES,
            show_low_confidence=show_low_confidence,
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
                return self._apply_search_quality(hits[:target], quality_mode)
            limit = min(total, limit + max(1, target - len(hits)))

    def _apply_search_quality(
        self, hits: list[SearchHit], quality_mode: QualityMode
    ) -> list[SearchHit]:
        thresholds = self.search_quality.thresholds_for(quality_mode)
        return annotate_search_hits(hits, thresholds)

    @staticmethod
    def _next_candidate_k(candidate_k: int, collection_size: int) -> int | None:
        if candidate_k >= collection_size:
            return None
        return min(collection_size, max(candidate_k + 1, candidate_k * 2))

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

    def _validate_top_k(self, top_k: int) -> None:
        if not 1 <= top_k <= self.config.max_top_k:
            raise ValueError(f"top_k must be between 1 and {self.config.max_top_k}.")

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
