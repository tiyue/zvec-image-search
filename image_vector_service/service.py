from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

from zvec_logging import initialize_zvec

from .config import ServiceConfig
from .dashscope_client import DashScopeEmbeddingClient, EmbeddingResponse
from .image_scanner import inspect_query_image, normalize_path, scan_folder
from .models import FileFailure, ImageRecord, IndexReport, SearchReport
from .rank_fusion import weighted_rrf
from .result_exporter import export_results
from .state import IndexState
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
        initialize_zvec()
        self.repository = repository or ZvecImageRepository(self.config)
        self._embedding_client = embedding_client
        self.state = IndexState(self.config.state_path)
        self.progress = progress or (lambda _message: None)

    @property
    def embedding_client(self):
        if self._embedding_client is None:
            self._embedding_client = DashScopeEmbeddingClient(self.config)
        return self._embedding_client

    def index_folder(
        self, folder_path: str, recursive: bool = True
    ) -> IndexReport:
        return self._index(folder_path, recursive=recursive, sync_deleted=False)

    def sync_folder(
        self, folder_path: str, recursive: bool = True
    ) -> IndexReport:
        return self._index(folder_path, recursive=recursive, sync_deleted=True)

    def _index(
        self, folder_path: str, recursive: bool, sync_deleted: bool
    ) -> IndexReport:
        root = Path(folder_path).expanduser().resolve()
        self.progress(f"Scanning: {root}")
        scan = scan_folder(root, recursive=recursive)
        report = IndexReport(
            root_path=normalize_path(root),
            collection=str(self.config.collection_path),
            scanned=scan.scanned,
            supported=len(scan.records) + len(scan.failures),
            skipped=scan.skipped,
            failures=list(scan.failures),
        )
        self.progress(
            f"Found {len(scan.records)} valid images; skipped {scan.skipped}; "
            f"invalid {len(scan.failures)}."
        )

        groups: dict[str, list[ImageRecord]] = defaultdict(list)
        for record in scan.records:
            existing = self.state.get(record.doc_id)
            if existing == record.state_dict():
                report.unchanged += 1
                continue
            groups[record.sha256].append(record)

        reusable: list[tuple[list[ImageRecord], list[float]]] = []
        pending: list[tuple[str, list[ImageRecord]]] = []
        for sha256, records in groups.items():
            cached_doc_id = self.state.find_doc_id_by_sha(sha256)
            cached_vector = (
                self.repository.fetch_vector(cached_doc_id) if cached_doc_id else None
            )
            if cached_vector is not None:
                reusable.append((records, cached_vector))
            else:
                pending.append((sha256, records))

        for records, vector in reusable:
            self._upsert_group(records, vector, report)

        for start in range(0, len(pending), self.config.batch_size):
            batch = pending[start : start + self.config.batch_size]
            self._embed_and_upsert_resilient(batch, report)
            completed = min(start + len(batch), len(pending))
            self.progress(f"Embedded {completed}/{len(pending)} unique images.")

        if sync_deleted:
            current_ids = scan.seen_supported_ids
            stale_ids = sorted(
                self.state.ids_for_root(normalize_path(root)) - current_ids
            )
            for chunk in _chunks(stale_ids, 256):
                succeeded, failures = self.repository.delete(chunk)
                for doc_id in succeeded:
                    self.state.remove(doc_id)
                report.deleted += len(succeeded)
                report.failures.extend(
                    FileFailure(doc_id, error) for doc_id, error in failures.items()
                )
                self.state.save()

        if report.inserted or report.updated or report.deleted:
            self.progress("Optimizing Zvec indexes...")
            self.repository.optimize()

        report.failed = len(report.failures)
        return report

    def _embed_and_upsert_resilient(
        self,
        batch: list[tuple[str, list[ImageRecord]]],
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
            report.api_requests += max(1, after - before)
        except Exception as exc:
            after = getattr(self.embedding_client, "request_count", before + 1)
            report.api_requests += max(1, after - before)
            if len(batch) > 1:
                midpoint = len(batch) // 2
                self._embed_and_upsert_resilient(batch[:midpoint], report)
                self._embed_and_upsert_resilient(batch[midpoint:], report)
                return
            for record in batch[0][1]:
                report.failures.append(FileFailure(record.absolute_path, str(exc)))
            return

        if response.request_id:
            report.usage.append(
                {"request_id": response.request_id, "usage": response.usage}
            )
        for (_sha256, records), vector in zip(
            batch, response.vectors, strict=True
        ):
            self._upsert_group(records, vector, report)

    def _upsert_group(
        self,
        records: list[ImageRecord],
        vector: list[float],
        report: IndexReport,
    ) -> None:
        if len(vector) != self.config.dimension:
            for record in records:
                report.failures.append(
                    FileFailure(
                        record.absolute_path,
                        f"invalid vector dimension: {len(vector)}",
                    )
                )
            return

        previous = {record.doc_id: self.state.get(record.doc_id) for record in records}
        succeeded, failures = self.repository.upsert_records(records, vector)
        by_id = {record.doc_id: record for record in records}
        for doc_id in succeeded:
            record = by_id[doc_id]
            if previous[doc_id] is None:
                report.inserted += 1
            else:
                report.updated += 1
            self.state.set(doc_id, record.state_dict())
        report.failures.extend(
            FileFailure(by_id[doc_id].absolute_path, error)
            for doc_id, error in failures.items()
        )
        self.state.save()

    def search_by_text(self, text: str, top_k: int = 10) -> SearchReport:
        self._validate_top_k(top_k)
        response = self.embedding_client.embed_text(text)
        hits = self.repository.query(response.vectors[0], self._candidate_count(top_k))
        return export_results(
            self.config,
            query_type="text",
            hits=hits,
            top_k=top_k,
            query={"text": text},
            request_ids=[response.request_id],
            usage=[response.usage],
        )

    def search_by_image(
        self, image_path: str, top_k: int = 10, include_self: bool = False
    ) -> SearchReport:
        self._validate_top_k(top_k)
        path = inspect_query_image(image_path)
        response = self.embedding_client.embed_images([path])
        hits = self.repository.query(response.vectors[0], self._candidate_count(top_k))
        return export_results(
            self.config,
            query_type="image",
            hits=hits,
            top_k=top_k,
            query={"image": str(path)},
            request_ids=[response.request_id],
            usage=[response.usage],
            exclude_path=None if include_self else str(path),
        )

    def search_by_image_and_text(
        self,
        image_path: str,
        text: str,
        top_k: int = 10,
        image_weight: float = 0.5,
        text_weight: float = 0.5,
        include_self: bool = False,
    ) -> SearchReport:
        self._validate_top_k(top_k)
        path = inspect_query_image(image_path)
        image_response = self.embedding_client.embed_images([path])
        text_response = self.embedding_client.embed_text(text)
        candidates = self._candidate_count(top_k)
        image_hits = self.repository.query(image_response.vectors[0], candidates)
        text_hits = self.repository.query(text_response.vectors[0], candidates)
        fused_hits = weighted_rrf(
            image_hits,
            text_hits,
            image_weight=image_weight,
            text_weight=text_weight,
        )
        return export_results(
            self.config,
            query_type="image_text",
            hits=fused_hits,
            top_k=top_k,
            query={
                "image": str(path),
                "text": text,
                "image_weight": image_weight,
                "text_weight": text_weight,
            },
            request_ids=[image_response.request_id, text_response.request_id],
            usage=[image_response.usage, text_response.usage],
            exclude_path=None if include_self else str(path),
        )

    def stats(self) -> dict:
        collection_stats = self.repository.stats
        return {
            "collection": str(self.config.collection_path),
            "collection_stats": {
                "doc_count": int(collection_stats.doc_count),
                "index_completeness": dict(collection_stats.index_completeness),
            },
            "tracked_files": len(self.state.entries),
            "model": self.config.model,
            "mode": "independent",
            "dimension": self.config.dimension,
            "metric": self.config.metric,
        }

    @staticmethod
    def _validate_top_k(top_k: int) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1.")

    @staticmethod
    def _candidate_count(top_k: int) -> int:
        return max(top_k * 3, top_k + 10)


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
