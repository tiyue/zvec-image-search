"""Bounded service adapter for clustering large, already-indexed libraries.

The core :mod:`large_image_clustering` engine deliberately knows nothing about
library state, source paths, or Zvec.  This module bridges those dependencies
without rebuilding the old full-library ``items`` list:

* entries and annotations are read in keyset pages of at most 256 rows;
* perceptual hashes are calculated one image at a time and failures are
  isolated;
* semantic neighbours come exclusively from vectors already stored in Zvec;
* preparation failures retain a count and at most a small, path-safe preview.

There is intentionally no model client dependency in this module.  A complete
run therefore performs zero embedding or other external model requests.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Final, Protocol

from PIL import Image

from .cluster_operation_store import ClusterOperationStore
from .image_clustering import (
    ClusteringCancelled,
    IdentityEvidence,
    ImageClusteringConfig,
    ImageClusterInput,
    SemanticNeighbor,
)
from .large_image_clustering import LargeClusterRunSummary, LargeImageClusterEngine
from .models import RankSource, SearchHit

DEFAULT_LARGE_CLUSTER_READ_PAGE_SIZE: Final = 256
MAX_LARGE_CLUSTER_READ_PAGE_SIZE: Final = 256
_DEFAULT_FAILURE_DETAIL_LIMIT: Final = 200
_MAX_FAILURE_DETAIL_LIMIT: Final = 1_000
_DEFAULT_PROGRESS_INTERVAL: Final = 2_000
DEFAULT_LARGE_LIBRARY_THRESHOLD: Final = 10_000
DEFAULT_CLUSTER_TYPES: Final = ("exact", "perceptual")
_HEX_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_PATH: Final = re.compile(r"^[a-zA-Z]:[/\\]")

CancelCheck = Callable[[], object]
ProgressCallback = Callable[[str], object]
IdentityEvidenceBuilder = Callable[
    [Mapping[str, Any], Mapping[str, Any] | None],
    Iterable[IdentityEvidence],
]
PerceptualHashBuilder = Callable[[Path], str]


class ClusterStateReader(Protocol):
    """The bounded subset of :class:`IndexState` needed by this adapter."""

    def count(self) -> int: ...

    def iter_entries_with_annotations(
        self, *, chunk_size: int = DEFAULT_LARGE_CLUSTER_READ_PAGE_SIZE
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]: ...


class ClusterSourceResolver(Protocol):
    def resolve_fields(self, fields: dict[str, Any]) -> Path: ...


class ExistingVectorRepository(Protocol):
    """Zvec reads used for semantic clustering; no embedding method is exposed."""

    def fetch_vector(self, doc_id: str) -> list[float] | None: ...

    def query(
        self,
        vector: list[float],
        top_k: int,
        tags: Iterable[str] = (),
        tag_mode: str = "all",
        rank_source: RankSource = "text",
    ) -> list[SearchHit]: ...


@dataclass(frozen=True, slots=True)
class ClusterPreparationFailure:
    """One safe, bounded failure preview for the task UI."""

    doc_id: str
    code: str
    message: str
    relative_path: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "doc_id": self.doc_id,
            "code": self.code,
            "message": self.message,
        }
        if self.relative_path:
            payload["relative_path"] = self.relative_path
        return payload


@dataclass(frozen=True, slots=True)
class ClusterPreparationReport:
    """Compact preparation metrics; complete failure data is not kept in RAM."""

    expected_count: int
    scanned_count: int
    prepared_count: int
    failure_count: int
    failures: tuple[ClusterPreparationFailure, ...]
    max_read_batch: int
    completed: bool

    @property
    def failure_details_truncated(self) -> bool:
        return self.failure_count > len(self.failures)

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_count": self.expected_count,
            "scanned_count": self.scanned_count,
            "prepared_count": self.prepared_count,
            "failure_count": self.failure_count,
            "failures": [failure.to_dict() for failure in self.failures],
            "failure_details_truncated": self.failure_details_truncated,
            "max_read_batch": self.max_read_batch,
            "completed": self.completed,
        }


@dataclass(frozen=True, slots=True)
class LargeClusterAdapterRunSummary:
    """Large-engine result plus bounded service-side preparation diagnostics."""

    cluster: LargeClusterRunSummary
    preparation: ClusterPreparationReport

    @property
    def api_requests(self) -> int:
        return 0

    def to_dict(self) -> dict[str, object]:
        payload = self.cluster.to_dict()
        payload.update(
            {
                "processed": self.cluster.clustered_count,
                "total": self.preparation.expected_count,
                "failure_count": (
                    self.cluster.failure_count + self.preparation.failure_count
                ),
                "preparation": self.preparation.to_dict(),
                "api_requests": 0,
                "embedding_api_requests": 0,
                "qwen_api_requests": 0,
                "embedding_recomputed": False,
                "snapshot_storage": "sqlite",
            }
        )
        return payload


class LargeClusterPreparationSession:
    """One cancellable, one-pass input and existing-vector lookup session."""

    def __init__(
        self,
        *,
        state: ClusterStateReader,
        source_resolver: ClusterSourceResolver,
        repository: ExistingVectorRepository,
        config: ImageClusteringConfig,
        identity_evidence_builder: IdentityEvidenceBuilder | None,
        perceptual_hash_builder: PerceptualHashBuilder,
        read_batch_size: int,
        failure_detail_limit: int,
        progress_interval: int,
        cancel_check: CancelCheck | None,
        progress: ProgressCallback | None,
    ) -> None:
        self._state = state
        self._source_resolver = source_resolver
        self._repository = repository
        self._config = config
        self._identity_evidence_builder = identity_evidence_builder
        self._perceptual_hash_builder = perceptual_hash_builder
        self._read_batch_size = read_batch_size
        self._failure_detail_limit = failure_detail_limit
        self._progress_interval = progress_interval
        self._cancel_check = cancel_check
        self._progress = progress
        self._expected_count = 0
        self._scanned_count = 0
        self._prepared_count = 0
        self._failure_count = 0
        self._failure_details: list[ClusterPreparationFailure] = []
        self._max_read_batch = 0
        self._semantic_query_count = 0
        self._stream_started = False
        self._stream_completed = False
        self._next_prepare_progress = progress_interval
        self._next_semantic_progress = progress_interval

    @property
    def external_api_calls(self) -> int:
        """This session only reads persisted local state and Zvec vectors."""

        return 0

    @property
    def semantic_query_count(self) -> int:
        return self._semantic_query_count

    @property
    def report(self) -> ClusterPreparationReport:
        return ClusterPreparationReport(
            expected_count=self._expected_count,
            scanned_count=self._scanned_count,
            prepared_count=self._prepared_count,
            failure_count=self._failure_count,
            failures=tuple(self._failure_details),
            max_read_batch=self._max_read_batch,
            completed=self._stream_completed,
        )

    def iter_inputs(self) -> Iterator[ImageClusterInput]:
        """Yield cluster inputs without retaining a library-sized Python list."""

        if self._stream_started:
            raise RuntimeError("A clustering preparation stream can only be read once.")
        self._stream_started = True
        self._expected_count = max(0, int(self._state.count()))
        try:
            pages = self._state.iter_entries_with_annotations(
                chunk_size=self._read_batch_size
            )
            for page in pages:
                self._check_cancel()
                if len(page) > self._read_batch_size:
                    raise RuntimeError(
                        "The clustering state reader exceeded its bounded page size."
                    )
                self._max_read_batch = max(self._max_read_batch, len(page))
                for entry, annotation in page:
                    self._check_cancel()
                    self._scanned_count += 1
                    cluster_input = self._prepare_one(entry, annotation)
                    self._report_preparation_progress()
                    if cluster_input is None:
                        continue
                    self._prepared_count += 1
                    yield cluster_input
            self._stream_completed = True
            self._emit_progress(
                "Preparing clustering inputs "
                f"{self._scanned_count}/{self._expected_count}"
            )
        finally:
            # ``completed`` intentionally remains false when cancellation or a
            # state-reader failure stops the generator before exhaustion.
            pass

    def semantic_neighbors(
        self, item: ImageClusterInput, top_k: int
    ) -> list[SemanticNeighbor]:
        """Return bounded neighbours from the existing Zvec image vector only."""

        self._check_cancel()
        if isinstance(top_k, bool) or not 1 <= int(top_k) <= 200:
            raise ValueError("Semantic neighbour top_k must be between 1 and 200.")
        bounded_top_k = int(top_k)
        try:
            vector = self._repository.fetch_vector(item.doc_id)
        except Exception as exc:
            raise RuntimeError(
                _generic_failure_message("Existing-vector lookup failed.", exc)
            ) from None
        if vector is None:
            raise ValueError("The indexed image vector is unavailable.")
        self._check_cancel()
        try:
            hits = self._repository.query(
                vector,
                bounded_top_k + 1,
                (),
                "all",
                "image",
            )
        except Exception as exc:
            raise RuntimeError(
                _generic_failure_message("Existing-vector query failed.", exc)
            ) from None
        neighbors: list[SemanticNeighbor] = []
        for hit in islice(hits, bounded_top_k + 1):
            self._check_cancel()
            if hit.doc_id == item.doc_id:
                continue
            distance = float(hit.distance)
            if not math.isfinite(distance):
                continue
            similarity = min(1.0, max(-1.0, 1.0 - distance))
            neighbors.append(SemanticNeighbor(hit.doc_id, similarity))
            if len(neighbors) >= bounded_top_k:
                break
        self._semantic_query_count += 1
        if self._semantic_query_count >= self._next_semantic_progress:
            self._emit_progress(
                "Clustering semantic neighbours "
                f"{self._semantic_query_count}/{self._prepared_count}"
            )
            self._next_semantic_progress += self._progress_interval
        return neighbors

    def _prepare_one(
        self,
        entry: Mapping[str, Any],
        annotation: Mapping[str, Any] | None,
    ) -> ImageClusterInput | None:
        fallback_doc_id = f"entry-{self._scanned_count}"
        doc_id = _safe_doc_id(entry.get("doc_id"), fallback=fallback_doc_id)
        raw_doc_id = str(entry.get("doc_id") or "").strip()
        if doc_id == fallback_doc_id and raw_doc_id != fallback_doc_id:
            self._record_failure(
                doc_id,
                "invalid_doc_id",
                "The indexed document id is missing or unsafe.",
                entry,
            )
            return None
        sha256 = str(entry.get("sha256") or "").strip().casefold()
        if _HEX_DIGEST.fullmatch(sha256) is None:
            self._record_failure(
                doc_id,
                "missing_sha256",
                "Indexed SHA-256 is unavailable.",
                entry,
            )
            return None

        perceptual_hash: str | None = None
        if self._config.enable_perceptual_hash:
            try:
                source = self._source_resolver.resolve_fields(dict(entry))
                perceptual_hash = self._perceptual_hash_builder(source)
            except Exception as exc:
                # Exact and semantic clustering remain useful when one source
                # file is missing or cannot be decoded.
                self._record_failure(
                    doc_id,
                    "perceptual_hash_failed",
                    _generic_failure_message(
                        "The image could not be decoded for perceptual hashing.", exc
                    ),
                    entry,
                )

        identity_evidence: tuple[IdentityEvidence, ...] = ()
        if self._identity_evidence_builder is not None:
            try:
                identity_evidence = tuple(
                    self._identity_evidence_builder(entry, annotation)
                )
            except Exception as exc:
                # Identity is optional and must not block hash-based grouping.
                self._record_failure(
                    doc_id,
                    "identity_evidence_failed",
                    _generic_failure_message(
                        "Identity evidence could not be prepared.", exc
                    ),
                    entry,
                )
                identity_evidence = ()

        return ImageClusterInput(
            doc_id=doc_id,
            sha256=sha256,
            embedding=None,
            perceptual_hash=perceptual_hash,
            identity_evidence=identity_evidence,
        )

    def _record_failure(
        self,
        doc_id: str,
        code: str,
        message: str,
        entry: Mapping[str, Any],
    ) -> None:
        self._failure_count += 1
        if len(self._failure_details) >= self._failure_detail_limit:
            return
        self._failure_details.append(
            ClusterPreparationFailure(
                doc_id=_safe_doc_id(doc_id, fallback=f"entry-{self._scanned_count}"),
                code=_safe_text(code, maximum=64, fallback="preparation_failed"),
                message=_safe_text(
                    message,
                    maximum=512,
                    fallback="Clustering input preparation failed.",
                ),
                relative_path=_safe_relative_path(entry.get("relative_path")),
            )
        )

    def _report_preparation_progress(self) -> None:
        if self._scanned_count < self._next_prepare_progress:
            return
        self._emit_progress(
            f"Preparing clustering inputs {self._scanned_count}/{self._expected_count}"
        )
        self._next_prepare_progress += self._progress_interval

    def _check_cancel(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise ClusteringCancelled("Image clustering was cancelled.")

    def _emit_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)


class LargeClusterAdapter:
    """Create bounded preparation sessions and run the large SQLite engine."""

    def __init__(
        self,
        state: ClusterStateReader,
        source_resolver: ClusterSourceResolver,
        repository: ExistingVectorRepository,
        config: ImageClusteringConfig,
        *,
        identity_evidence_builder: IdentityEvidenceBuilder | None = None,
        perceptual_hash_builder: PerceptualHashBuilder | None = None,
        read_batch_size: int = DEFAULT_LARGE_CLUSTER_READ_PAGE_SIZE,
        failure_detail_limit: int = _DEFAULT_FAILURE_DETAIL_LIMIT,
        progress_interval: int = _DEFAULT_PROGRESS_INTERVAL,
    ) -> None:
        if not isinstance(config, ImageClusteringConfig):
            raise TypeError("config must be ImageClusteringConfig")
        if (
            isinstance(read_batch_size, bool)
            or not 1 <= read_batch_size <= MAX_LARGE_CLUSTER_READ_PAGE_SIZE
        ):
            raise ValueError("read_batch_size must be between 1 and 256")
        if (
            isinstance(failure_detail_limit, bool)
            or not 0 <= failure_detail_limit <= _MAX_FAILURE_DETAIL_LIMIT
        ):
            raise ValueError("failure_detail_limit must be between 0 and 1000")
        if (
            isinstance(progress_interval, bool)
            or not 1 <= progress_interval <= 1_000_000
        ):
            raise ValueError("progress_interval must be between 1 and 1000000")
        self.state = state
        self.source_resolver = source_resolver
        self.repository = repository
        self.config = config
        self.identity_evidence_builder = identity_evidence_builder
        self.perceptual_hash_builder = perceptual_hash_builder or average_image_hash
        self.read_batch_size = read_batch_size
        self.failure_detail_limit = failure_detail_limit
        self.progress_interval = progress_interval

    @property
    def external_api_calls(self) -> int:
        return 0

    def create_session(
        self,
        *,
        cancel_check: CancelCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> LargeClusterPreparationSession:
        return LargeClusterPreparationSession(
            state=self.state,
            source_resolver=self.source_resolver,
            repository=self.repository,
            config=self.config,
            identity_evidence_builder=self.identity_evidence_builder,
            perceptual_hash_builder=self.perceptual_hash_builder,
            read_batch_size=self.read_batch_size,
            failure_detail_limit=self.failure_detail_limit,
            progress_interval=self.progress_interval,
            cancel_check=cancel_check,
            progress=progress,
        )

    def run(
        self,
        store: ClusterOperationStore,
        library_id: str,
        *,
        previous_snapshot_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        activate: bool = True,
        engine_batch_size: int = 512,
        max_perceptual_neighbors: int = 32,
        cancel_check: CancelCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> LargeClusterAdapterRunSummary:
        session = self.create_session(
            cancel_check=cancel_check,
            progress=progress,
        )
        engine = LargeImageClusterEngine(
            store,
            self.config,
            session.semantic_neighbors if self.config.enable_semantic else None,
            batch_size=engine_batch_size,
            max_perceptual_neighbors=max_perceptual_neighbors,
        )
        cluster = engine.run(
            library_id,
            session.iter_inputs(),
            previous_snapshot_version=previous_snapshot_version,
            metadata=metadata,
            activate=activate,
            cancel_check=cancel_check,
            progress=progress,
        )
        return LargeClusterAdapterRunSummary(cluster, session.report)


def should_use_large_cluster_engine(
    entry_count: int,
    *,
    has_active_manual_rules: bool,
    threshold: int = DEFAULT_LARGE_LIBRARY_THRESHOLD,
) -> bool:
    """Keep manual-rule/small-library behavior on the compatibility engine."""

    if isinstance(entry_count, bool) or entry_count < 0:
        raise ValueError("entry_count must be a non-negative integer")
    if isinstance(threshold, bool) or threshold < 1:
        raise ValueError("threshold must be a positive integer")
    return entry_count > threshold and not has_active_manual_rules


def average_image_hash(path: Path) -> str:
    """Calculate one 64-bit local hash without retaining decoded image data."""

    with Image.open(path) as image:
        grayscale = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        pixels = grayscale.tobytes()
    average = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | int(value >= average)
    return f"{bits:016x}"


def _generic_failure_message(prefix: str, exc: Exception) -> str:
    # Exception messages frequently contain absolute local paths.  The class
    # name is enough for bounded diagnostics while avoiding path disclosure.
    error_type = _safe_text(exc.__class__.__name__, maximum=80, fallback="Error")
    return f"{prefix} ({error_type})"


def _safe_doc_id(value: object, *, fallback: str) -> str:
    raw = str(value or "")
    if any(character in raw for character in "\r\n\x00"):
        return fallback
    normalized = raw.strip()
    if not normalized or len(normalized) > 512:
        return fallback
    return normalized


def _safe_relative_path(value: object) -> str:
    normalized = _safe_text(value, maximum=512, fallback="").replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or _WINDOWS_ABSOLUTE_PATH.match(normalized) is not None
    ):
        return ""
    return normalized.strip("/")


def _safe_text(
    value: object,
    *,
    maximum: int,
    fallback: str,
) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    return (normalized or fallback)[:maximum]


__all__ = [
    "ClusterPreparationFailure",
    "ClusterPreparationReport",
    "LargeClusterAdapter",
    "LargeClusterAdapterRunSummary",
    "LargeClusterPreparationSession",
    "average_image_hash",
    "should_use_large_cluster_engine",
]
