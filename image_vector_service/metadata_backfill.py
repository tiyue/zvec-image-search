"""Resumable execution primitives for metadata-description embeddings.

The runner deliberately knows nothing about Zvec or SQLite.  A service supplies
already-selected items and one commit callback, which keeps schema migration and
storage ownership inside the repository layer.  Each successful vector is
committed immediately, so rerunning the explicit job naturally resumes from the
remaining stale or empty metadata hashes.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class TextEmbeddingClient(Protocol):
    """Small public surface required from the configured embedding client."""

    def embed_text(self, text: str) -> Any:
        """Return an object with one vector in its ``vectors`` attribute."""


@dataclass(frozen=True)
class MetadataBackfillItem:
    """One immutable text snapshot selected for embedding."""

    doc_id: str
    text: str
    text_hash: str
    truncated: bool = False

    def __post_init__(self) -> None:
        if not self.doc_id.strip():
            raise ValueError("Metadata backfill doc_id must not be empty.")
        if not self.text.strip():
            raise ValueError("Metadata backfill text must not be empty.")
        normalized_hash = self.text_hash.strip().lower()
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("Metadata backfill text_hash must be SHA-256 hex.")
        object.__setattr__(self, "text_hash", normalized_hash)


@dataclass(frozen=True)
class MetadataBackfillFailure:
    """A bounded, JSON-safe diagnostic for one skipped document."""

    doc_id: str
    stage: str
    error: str
    error_type: str


@dataclass
class MetadataBackfillReport:
    """Stable result payload consumed by the CLI and persistent backend."""

    selected: int
    eligible: int
    concurrency: int = 0
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    changed_during_run: int = 0
    api_requests: int = 0
    truncated_texts: int = 0
    request_ids: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    failures: list[MetadataBackfillFailure] = field(default_factory=list)

    @property
    def deferred(self) -> int:
        return max(0, self.eligible - self.selected)

    @property
    def remaining(self) -> int:
        # Failed and concurrently changed documents intentionally remain pending.
        return max(0, self.eligible - self.succeeded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "metadata_backfill",
            "explicit_trigger": True,
            "resumable": True,
            "selected": self.selected,
            "eligible": self.eligible,
            "concurrency": self.concurrency,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "changed_during_run": self.changed_during_run,
            "deferred": self.deferred,
            "remaining": self.remaining,
            "api_requests": self.api_requests,
            "truncated_texts": self.truncated_texts,
            "request_ids": list(self.request_ids),
            "usage": list(self.usage),
            "failures": [asdict(item) for item in self.failures],
        }


CommitMetadataEmbedding = Callable[
    [MetadataBackfillItem, list[float]],
    None,
]
StillCurrentCheck = Callable[[MetadataBackfillItem], bool]
ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], None]


class _ItemFailure(RuntimeError):
    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(str(cause) or cause.__class__.__name__)
        self.stage = stage
        self.cause = cause


class MetadataBackfillRunner:
    """Embed and immediately commit independent metadata documents.

    The configured DashScope client remains responsible for the process-wide
    RPM/TPM permit.  This runner keeps a bounded number of network requests in
    flight, but validates and commits completed results on the calling thread so
    Collection writes remain serialized.  It polls the job's cancellation
    callback while requests are pending and never catches cancellation
    exceptions raised by that callback.
    """

    def __init__(
        self,
        *,
        client: TextEmbeddingClient,
        commit: CommitMetadataEmbedding,
        expected_dimension: int,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        still_current: StillCurrentCheck | None = None,
        client_cancel_event: threading.Event | None = None,
        concurrency: int = 2,
        cancellation_poll_seconds: float = 0.1,
        failure_detail_limit: int = 100,
    ) -> None:
        if isinstance(expected_dimension, bool) or expected_dimension < 1:
            raise ValueError("expected_dimension must be a positive integer.")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise ValueError("concurrency must be a positive integer.")
        if (
            not math.isfinite(cancellation_poll_seconds)
            or cancellation_poll_seconds <= 0
        ):
            raise ValueError("cancellation_poll_seconds must be finite and positive.")
        if isinstance(failure_detail_limit, bool) or failure_detail_limit < 0:
            raise ValueError("failure_detail_limit cannot be negative.")
        self.client = client
        self.commit = commit
        self.expected_dimension = expected_dimension
        self.progress = progress or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: None)
        self.still_current = still_current or (lambda _item: True)
        self.client_cancel_event = client_cancel_event
        self.concurrency = concurrency
        self.cancellation_poll_seconds = cancellation_poll_seconds
        self.failure_detail_limit = failure_detail_limit

    def run(
        self,
        items: Iterable[MetadataBackfillItem],
        *,
        eligible: int | None = None,
    ) -> MetadataBackfillReport:
        selected = list(items)
        eligible_count = len(selected) if eligible is None else eligible
        if isinstance(eligible_count, bool) or eligible_count < len(selected):
            raise ValueError("eligible cannot be smaller than the selected item count.")

        report = MetadataBackfillReport(
            selected=len(selected),
            eligible=eligible_count,
            concurrency=min(self.concurrency, len(selected)),
            truncated_texts=sum(1 for item in selected if item.truncated),
        )
        if not selected:
            self.cancel_check()
            self.progress(
                f"Metadata backfill 0/{eligible_count}: no pending descriptions."
            )
            return report

        worker_count = min(self.concurrency, len(selected))
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="zvec-metadata-backfill",
        )
        pending: dict[Future[Any], tuple[int, MetadataBackfillItem]] = {}
        next_index = 0
        clean_shutdown = False
        try:
            while next_index < len(selected) and len(pending) < worker_count:
                self.cancel_check()
                item = selected[next_index]
                self.progress(
                    "Metadata backfill "
                    f"{report.attempted}/{eligible_count}: embedding {item.doc_id}."
                )
                future = executor.submit(self.client.embed_text, item.text)
                pending[future] = (next_index, item)
                next_index += 1

            while pending:
                self.cancel_check()
                done, _not_done = wait(
                    tuple(pending),
                    timeout=self.cancellation_poll_seconds,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                # A wave can complete simultaneously.  Stable input-order
                # processing keeps diagnostics deterministic without reducing
                # network concurrency.
                completed = sorted(done, key=lambda future: pending[future][0])
                for future in completed:
                    self.cancel_check()
                    _index, item = pending.pop(future)
                    self._consume_completed(
                        report,
                        item,
                        future,
                        eligible_count=eligible_count,
                    )

                while next_index < len(selected) and len(pending) < worker_count:
                    self.cancel_check()
                    item = selected[next_index]
                    self.progress(
                        "Metadata backfill "
                        f"{report.attempted}/{eligible_count}: embedding {item.doc_id}."
                    )
                    future = executor.submit(self.client.embed_text, item.text)
                    pending[future] = (next_index, item)
                    next_index += 1
            clean_shutdown = True
            return report
        finally:
            # On cancellation, do not make the Collection worker wait for a late
            # HTTP response.  The request may finish in the background, but its
            # result has no path to the commit callback and is safely discarded.
            # A DashScope client wired to this event also abandons a pending
            # process-wide limiter wait before it can consume an API request.
            if not clean_shutdown and self.client_cancel_event is not None:
                self.client_cancel_event.set()
            executor.shutdown(wait=clean_shutdown, cancel_futures=not clean_shutdown)

    def _consume_completed(
        self,
        report: MetadataBackfillReport,
        item: MetadataBackfillItem,
        future: Future[Any],
        *,
        eligible_count: int,
    ) -> None:
        report.attempted += 1
        report.api_requests += 1
        try:
            response = future.result()
            vector = self._extract_vector(response)
        except _ItemFailure as failure:
            self._record_failure(report, item, failure)
            self.progress(
                "Metadata backfill "
                f"{report.attempted}/{eligible_count}: skipped {item.doc_id}."
            )
            return
        except Exception as exc:
            self._record_failure(
                report,
                item,
                _ItemFailure("embedding", exc),
            )
            self.progress(
                "Metadata backfill "
                f"{report.attempted}/{eligible_count}: skipped {item.doc_id}."
            )
            return

        request_id = str(getattr(response, "request_id", "") or "").strip()
        if request_id and len(report.request_ids) < self.failure_detail_limit:
            report.request_ids.append(request_id)
        usage = getattr(response, "usage", None)
        if isinstance(usage, Mapping) and len(report.usage) < self.failure_detail_limit:
            report.usage.append(dict(usage))

        # A tag or annotation can change while an HTTP request is in flight.
        # Never commit an embedding for an obsolete text snapshot.
        self.cancel_check()
        try:
            current = self.still_current(item)
        except Exception as exc:
            self._record_failure(
                report,
                item,
                _ItemFailure("validation", exc),
            )
            self.progress(
                "Metadata backfill "
                f"{report.attempted}/{eligible_count}: skipped {item.doc_id}."
            )
            return
        if not current:
            report.changed_during_run += 1
            self.progress(
                "Metadata backfill "
                f"{report.attempted}/{eligible_count}: "
                f"metadata changed for {item.doc_id}; deferred."
            )
            return

        self.cancel_check()
        try:
            self.commit(item, vector)
        except Exception as exc:
            self._record_failure(
                report,
                item,
                _ItemFailure("commit", exc),
            )
            self.progress(
                "Metadata backfill "
                f"{report.attempted}/{eligible_count}: skipped {item.doc_id}."
            )
            return

        report.succeeded += 1
        self.progress(
            "Metadata backfill "
            f"{report.attempted}/{eligible_count}: committed {item.doc_id}."
        )

    def _extract_vector(self, response: Any) -> list[float]:
        try:
            vectors = response.vectors
            if not isinstance(vectors, list) or len(vectors) != 1:
                raise ValueError("Embedding response must contain exactly one vector.")
            vector = [float(value) for value in vectors[0]]
            if len(vector) != self.expected_dimension:
                raise ValueError(
                    "Metadata embedding dimension mismatch: "
                    f"{len(vector)} != {self.expected_dimension}."
                )
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Metadata embedding contains non-finite values.")
            return vector
        except Exception as exc:
            raise _ItemFailure("response", exc) from exc

    def _record_failure(
        self,
        report: MetadataBackfillReport,
        item: MetadataBackfillItem,
        failure: _ItemFailure,
    ) -> None:
        report.failed += 1
        if len(report.failures) >= self.failure_detail_limit:
            return
        report.failures.append(
            MetadataBackfillFailure(
                doc_id=item.doc_id,
                stage=failure.stage,
                error=str(failure.cause) or failure.cause.__class__.__name__,
                error_type=failure.cause.__class__.__name__,
            )
        )
