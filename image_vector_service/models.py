from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ImageRecord:
    doc_id: str
    root_path: str
    relative_path: str
    absolute_path: str
    file_name: str
    extension: str
    mime_type: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    width: int
    height: int

    def state_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileFailure:
    path: str
    error: str


@dataclass
class ScanResult:
    scanned: int = 0
    supported: int = 0
    skipped: int = 0
    complete: bool = True
    seen_supported_ids: set[str] = field(default_factory=set)
    fast_unchanged_ids: set[str] = field(default_factory=set)
    records: list[ImageRecord] = field(default_factory=list)
    failures: list[FileFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class IndexReport:
    root_path: str
    collection: str = ""
    scanned: int = 0
    supported: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deleted: int = 0
    would_delete: int = 0
    sync_aborted: bool = False
    failed: int = 0
    api_requests: int = 0
    usage: list[dict[str, Any]] = field(default_factory=list)
    failures: list[FileFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    distance: float
    fields: dict[str, Any]
    rank: int = 0
    fused_score: float | None = None


@dataclass
class ExportedHit:
    rank: int
    distance: float
    source_path: str
    copied_path: str
    doc_id: str
    fused_score: float | None = None


@dataclass
class SearchReport:
    query_type: str
    output_dir: str
    result_count: int
    results: list[ExportedHit] = field(default_factory=list)
    copy_failures: list[FileFailure] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
