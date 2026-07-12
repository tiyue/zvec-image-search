from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from zvec_logging import initialize_zvec

from .app_logging import close_app_logger, get_app_logger
from .config import ConfigurationError, ServiceConfig
from .dashscope_client import (
    DashScopeEmbeddingClient,
    DashScopeError,
    EmbeddingResponse,
    ImageInputError,
)
from .image_scanner import (
    file_sha256,
    inspect_query_image,
    scan_folder,
)
from .logical_paths import normalize_path
from .models import FileFailure, ImageRecord, IndexReport, SearchHit, SearchReport
from .process_lock import ProcessLock
from .rank_fusion import weighted_rrf
from .result_exporter import clean_result_directories, export_results
from .source_resolver import SourcePathResolver
from .state import IndexState
from .tags import normalize_tags
from .zvec_repository import ZvecImageRepository


class ImageVectorService:
    def __init__(
        self,
        config: ServiceConfig | None = None,
        embedding_client=None,
        repository: ZvecImageRepository | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.config = config or ServiceConfig()
        self.config.validate()
        self.progress = progress or (lambda _message: None)
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
            self.source_resolver = SourcePathResolver(self.state)
            self.validate_documents = self.repository.doc_count != self.state.count()
        except Exception:
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
        effective_tags = (
            requested_tags
            if requested_tags is not None
            else tuple(self.state.tags_for_root(root_id))
        )

        self.progress(f"Scanning: {root}")
        self.logger.info(
            "index_start recursive=%s sync=%s verify_hash=%s tags=%d",
            recursive,
            sync_deleted,
            verify_hash,
            len(effective_tags),
        )
        scan = scan_folder(
            root,
            root_id,
            recursive=recursive,
            previous_lookup=self.state.get,
            verify_hash=verify_hash,
        )
        report = IndexReport(
            root_path=normalized_root,
            collection=str(self.config.collection_path),
            scanned=scan.scanned,
            supported=scan.supported,
            skipped=scan.skipped,
            failures=list(scan.failures),
            warnings=list(scan.warnings),
        )
        if self.state_reset:
            report.warnings.append(
                "The Collection identity changed; stale incremental state was reset."
            )
        self.progress(
            f"Found {len(scan.records)} valid images; skipped {scan.skipped}; "
            f"invalid {len(scan.failures)}."
        )

        groups: dict[str, list[ImageRecord]] = defaultdict(list)
        for record in scan.records:
            existing = self.state.get(record.doc_id)
            record_state = record.state_dict()
            file_unchanged = existing is not None and all(
                existing.get(key) == value for key, value in record_state.items()
            )
            tags_unchanged = (
                existing is not None
                and tuple(existing.get("tags", ())) == effective_tags
            )
            if file_unchanged and tags_unchanged:
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
            cached_doc_id = self.state.find_doc_id_by_sha(content_hash)
            cached_vector = (
                self.repository.fetch_vector(cached_doc_id) if cached_doc_id else None
            )
            if cached_vector is not None:
                reusable.append((records, cached_vector))
            else:
                pending.append((content_hash, records))

        for records, vector in reusable:
            self._upsert_group(records, vector, effective_tags, report)

        embeddable: list[tuple[str, list[ImageRecord]]] = []
        for content_hash, records in pending:
            representative = records[0]
            if representative.size_bytes > self.config.max_image_bytes:
                message = (
                    f"image exceeds configured limit of "
                    f"{self.config.max_image_bytes} bytes"
                )
                report.failures.extend(
                    FileFailure(record.absolute_path, message) for record in records
                )
            elif not _record_is_stable(representative):
                report.failures.extend(
                    FileFailure(
                        record.absolute_path,
                        "file changed after scanning; run index again",
                    )
                    for record in records
                )
            else:
                embeddable.append((content_hash, records))

        processed = 0
        for batch in self._embedding_batches(embeddable):
            self._embed_and_upsert_resilient(batch, effective_tags, report)
            processed += len(batch)
            self.progress(f"Embedded {processed}/{len(embeddable)} unique images.")

        if sync_deleted:
            stale_ids = sorted(
                self.state.ids_for_root(root_id) - scan.seen_supported_ids
            )
            report.would_delete = len(stale_ids)
            if not scan.complete:
                report.sync_aborted = True
                report.warnings.append(
                    "Deletion was skipped because one or more directories "
                    "could not be scanned."
                )
            elif dry_run:
                report.warnings.append("Dry run: no stale records were deleted.")
            else:
                for chunk in _chunks(stale_ids, 256):
                    succeeded, failures = self.repository.delete(chunk)
                    self.state.remove_many(succeeded)
                    report.deleted += len(succeeded)
                    report.failures.extend(
                        FileFailure(doc_id, error) for doc_id, error in failures.items()
                    )

        if report.inserted or report.updated or report.deleted:
            self.progress("Optimizing Zvec indexes...")
            self.repository.optimize()

        if previous_scope is None:
            self.state.record_root(root_id, normalized_root, recursive)
        elif recursive and not previous_scope:
            self.state.record_root(root_id, normalized_root, True)
        elif sync_deleted and allow_scope_change and scan.complete and not dry_run:
            self.state.record_root(root_id, normalized_root, recursive)
        if requested_tags is not None:
            self.state.set_root_tags(root_id, requested_tags)

        report.failed = len(report.failures)
        if self.repository.doc_count != self.state.count():
            report.warnings.append(
                "Zvec and incremental-state counts differ; unchanged documents "
                "were validated."
            )
        self.validate_documents = self.repository.doc_count != self.state.count()
        self.logger.info(
            "index_complete scanned=%d inserted=%d updated=%d unchanged=%d "
            "deleted=%d failed=%d api_requests=%d",
            report.scanned,
            report.inserted,
            report.updated,
            report.unchanged,
            report.deleted,
            report.failed,
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
            encoded_size = 4 * ((raw_size + 2) // 3) + 256
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

    def _embed_and_upsert_resilient(
        self,
        batch: list[tuple[str, list[ImageRecord]]],
        tags: tuple[str, ...],
        report: IndexReport,
    ) -> None:
        if not batch:
            return
        before = getattr(self.embedding_client, "request_count", 0)
        try:
            response: EmbeddingResponse = self.embedding_client.embed_images(
                [Path(records[0].absolute_path) for _sha, records in batch]
            )
            after = getattr(self.embedding_client, "request_count", before + 1)
            report.api_requests += max(0, after - before)
        except (DashScopeError, ImageInputError) as exc:
            after = getattr(self.embedding_client, "request_count", before + 1)
            report.api_requests += max(0, after - before)
            if getattr(exc, "splittable", False) and len(batch) > 1:
                midpoint = len(batch) // 2
                self._embed_and_upsert_resilient(batch[:midpoint], tags, report)
                self._embed_and_upsert_resilient(batch[midpoint:], tags, report)
                return
            if getattr(exc, "splittable", False):
                report.failures.extend(
                    FileFailure(record.absolute_path, str(exc))
                    for record in batch[0][1]
                )
                return
            raise

        if response.request_id:
            report.usage.append(
                {"request_id": response.request_id, "usage": response.usage}
            )
        for (_sha256, records), vector in zip(batch, response.vectors, strict=True):
            if not _record_is_stable(records[0]):
                report.failures.extend(
                    FileFailure(
                        record.absolute_path,
                        "file changed during embedding; run index again",
                    )
                    for record in records
                )
            else:
                self._upsert_group(records, vector, tags, report)

    def _upsert_group(
        self,
        records: list[ImageRecord],
        vector: list[float],
        tags: tuple[str, ...],
        report: IndexReport,
    ) -> None:
        if len(vector) != self.config.dimension:
            report.failures.extend(
                FileFailure(
                    record.absolute_path,
                    f"invalid vector dimension: {len(vector)}",
                )
                for record in records
            )
            return

        previous = {record.doc_id: self.state.get(record.doc_id) for record in records}
        succeeded, failures = self.repository.upsert_records(records, vector, tags)
        by_id = {record.doc_id: record for record in records}
        successful_entries = []
        for doc_id in succeeded:
            record = by_id[doc_id]
            if previous[doc_id] is None:
                report.inserted += 1
            else:
                report.updated += 1
            successful_entries.append({**record.state_dict(), "tags": list(tags)})
        self.state.set_many(successful_entries)
        report.failures.extend(
            FileFailure(by_id[doc_id].absolute_path, error)
            for doc_id, error in failures.items()
        )

    def search_by_text(
        self,
        text: str,
        top_k: int = 10,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
    ) -> SearchReport:
        self._validate_top_k(top_k)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        text = text.strip()
        if not text:
            raise ValueError("Search text cannot be empty.")
        vector, source, request_id, usage = self._text_embedding(text)
        hits = self._query_until_sufficient(
            vector, top_k, tags=normalized_tags, tag_mode=tag_mode
        )
        report = export_results(
            self.config,
            query_type="text",
            hits=hits,
            top_k=top_k,
            query={
                "text": text,
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
            },
            request_ids=[request_id],
            usage=[usage] if usage else [],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={"text": source},
        )
        self._log_search(report)
        return report

    def search_by_image(
        self,
        image_path: str,
        top_k: int = 10,
        include_self: bool = False,
        tags: Iterable[str] | None = None,
        tag_mode: str = "all",
    ) -> SearchReport:
        self._validate_top_k(top_k)
        normalized_tags = self._validate_search_tags(tags, tag_mode)
        path = inspect_query_image(image_path)
        vector, source, request_id, usage = self._image_embedding(path)
        exclude = None if include_self else str(path)
        hits = self._query_until_sufficient(
            vector,
            top_k,
            exclude_path=exclude,
            tags=normalized_tags,
            tag_mode=tag_mode,
        )
        report = export_results(
            self.config,
            query_type="image",
            hits=hits,
            top_k=top_k,
            query={
                "image": path.name,
                "tags": list(normalized_tags),
                "tag_mode": tag_mode,
            },
            request_ids=[request_id],
            usage=[usage] if usage else [],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={"image": source},
            exclude_path=exclude,
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
    ) -> SearchReport:
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
        exclude = None if include_self else str(path)
        fused_hits = self._query_fused_until_sufficient(
            image_vector,
            text_vector,
            top_k,
            image_weight,
            text_weight,
            exclude,
            normalized_tags,
            tag_mode,
        )
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
            },
            request_ids=[response.request_id for response in responses.values()],
            usage=[response.usage for response in responses.values()],
            resolve_source=self.source_resolver.resolve_hit,
            embedding_sources={
                "image": image_source or "unknown",
                "text": text_source or "unknown",
            },
            exclude_path=exclude,
        )
        self._log_search(report)
        return report

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

    def _image_embedding(self, path: Path) -> tuple[list[float], str, str, dict]:
        cache_key, vector, source, image_hash = self._cached_image(path)
        if vector is not None:
            return vector, source or "cache", "", {}
        response = self.embedding_client.embed_images([path])
        self._ensure_query_image_unchanged(path, image_hash)
        vector = response.vectors[0]
        self._cache_vector(cache_key, "image", vector)
        return vector, "api", response.request_id, response.usage

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

    def _query_until_sufficient(
        self,
        vector: list[float],
        top_k: int,
        exclude_path: str | None = None,
        tags: tuple[str, ...] = (),
        tag_mode: str = "all",
    ) -> list[SearchHit]:
        total = self.repository.doc_count
        if total == 0:
            return []
        limit = min(total, max(top_k * 3, top_k + 10))
        while True:
            hits = self.repository.query(vector, limit, tags, tag_mode)
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
        tags: tuple[str, ...],
        tag_mode: str,
    ) -> list[SearchHit]:
        total = self.repository.doc_count
        if total == 0:
            return []
        limit = min(total, max(top_k * 3, top_k + 10))
        while True:
            with ThreadPoolExecutor(max_workers=2) as executor:
                image_future = executor.submit(
                    self.repository.query,
                    image_vector,
                    limit,
                    tags,
                    tag_mode,
                )
                text_future = executor.submit(
                    self.repository.query,
                    text_vector,
                    limit,
                    tags,
                    tag_mode,
                )
                image_hits = image_future.result()
                text_hits = text_future.result()
            fused = weighted_rrf(
                image_hits,
                text_hits,
                image_weight=image_weight,
                text_weight=text_weight,
            )
            if (
                _usable_hit_count(fused, exclude_path, self.source_resolver.resolve_hit)
                >= top_k
                or limit >= total
            ):
                return fused
            limit = min(total, max(limit + 1, limit * 2))

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
        }

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

    @staticmethod
    def _validate_search_tags(
        tags: Iterable[str] | None, tag_mode: str
    ) -> tuple[str, ...]:
        if tag_mode not in {"all", "any"}:
            raise ValueError("tag_mode must be 'all' or 'any'.")
        return normalize_tags(tags)

    @staticmethod
    def _validate_weights(image_weight: float, text_weight: float) -> None:
        if image_weight < 0 or text_weight < 0 or image_weight + text_weight <= 0:
            raise ValueError("Search weights must be non-negative and not both zero.")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        if hasattr(self, "state"):
            self.state.close()
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
