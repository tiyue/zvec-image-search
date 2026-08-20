"""Repeatable Sony A7M4 RAW-selection performance benchmark.

The benchmark opens source images read-only and keeps every SQLite database,
derived cache and intermediate file under an owned temporary directory.  It
never uses the production RAW-selection database or cache and never attempts
to flush the Windows file-system cache.

The same script is intentionally compatible with the dfe2d75 implementation
and later scheduler implementations.  Pure decode matrices use a benchmark-
owned bounded executor so the fixed worker counts can be measured even when a
production scheduler does not expose configurable limits.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import suppress
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

_SUPPORTED_EXTENSIONS: Final = frozenset({".arw", ".jpeg", ".jpg", ".png"})
_MATRIX_WORKERS: Final[dict[str, tuple[int, ...]]] = {
    "arw_embed": (8, 12, 16, 24),
    "jpg_thumbnail": (8, 12, 16),
    "png_thumbnail": (4, 6, 8),
    "arw_full": (2, 3, 4, 6),
}
_MATRIX_EXTENSIONS: Final[dict[str, frozenset[str]]] = {
    "arw_embed": frozenset({".arw"}),
    "jpg_thumbnail": frozenset({".jpg", ".jpeg"}),
    "png_thumbnail": frozenset({".png"}),
    "arw_full": frozenset({".arw"}),
}
_LOOK_IDS: Final = ("ST", "PT", "NT", "VV", "VV2", "FL", "IN", "SH", "BW", "SE")
_BASELINE_SCENARIOS: Final = (
    "cold24",
    "cold100",
    "project",
    "hot24",
    "preview",
)
_ALL_SCENARIOS: Final = _BASELINE_SCENARIOS + ("matrix", "looks")
_RESOURCE_SAMPLE_SECONDS: Final = 0.1
_MAX_ERROR_DETAILS: Final = 32
_REPARSE_POINT_ATTRIBUTE: Final = 0x400
_SAMPLE_MANIFEST_SCHEMA_VERSION: Final = 1
_A7M4_CAMERA_MODEL: Final = "ILCE-7M4"
_REQUIRED_A7M4_RAW_VARIANTS: Final = frozenset(
    {
        ("compressed", "L"),
        ("lossless_compressed", "L"),
        ("lossless_compressed", "M"),
        ("lossless_compressed", "S"),
        ("uncompressed", "L"),
    }
)
_TARGET_PYTHON: Final = (3, 12)
_LOCKED_RUNTIME_PACKAGES: Final = ("numpy", "Pillow", "rawpy", "zvec")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative_name: str
    extension: str
    file_size: int
    mtime_ns: int
    file_identity: str


@dataclass(frozen=True, slots=True)
class LatencySummary:
    samples: int
    p50_ms: float
    p95_ms: float
    minimum_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    wall_ms: float
    process_cpu_seconds: float
    average_cpu_core_percent: float
    peak_cpu_core_percent: float
    rss_start_mib: float | None
    peak_rss_mib: float | None
    process_read_operations: int | None
    process_write_operations: int | None
    process_other_operations: int | None
    process_read_bytes: int | None
    process_write_bytes: int | None
    process_other_bytes: int | None


@dataclass(frozen=True, slots=True)
class _ProcessSnapshot:
    rss_bytes: int
    read_operations: int
    write_operations: int
    other_operations: int
    read_bytes: int
    write_bytes: int
    other_bytes: int


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    )


class _ProcessIoCounters(ctypes.Structure):
    _fields_ = (
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    )


def _process_snapshot() -> _ProcessSnapshot | None:
    if os.name != "nt":
        return None
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    kernel32: Any = loader("kernel32", use_last_error=True)
    psapi: Any = loader("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessIoCounters.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessIoCounters),
    ]
    kernel32.GetProcessIoCounters.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    memory = _ProcessMemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    io_counters = _ProcessIoCounters()
    if not psapi.GetProcessMemoryInfo(
        handle,
        ctypes.byref(memory),
        ctypes.sizeof(memory),
    ) or not kernel32.GetProcessIoCounters(handle, ctypes.byref(io_counters)):
        return None
    return _ProcessSnapshot(
        rss_bytes=int(memory.working_set_size),
        read_operations=int(io_counters.read_operation_count),
        write_operations=int(io_counters.write_operation_count),
        other_operations=int(io_counters.other_operation_count),
        read_bytes=int(io_counters.read_transfer_count),
        write_bytes=int(io_counters.write_transfer_count),
        other_bytes=int(io_counters.other_transfer_count),
    )


class _ProcessResourceSampler:
    def __init__(self, interval_seconds: float = _RESOURCE_SAMPLE_SECONDS) -> None:
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_wall = 0.0
        self._started_cpu = 0.0
        self._started_snapshot: _ProcessSnapshot | None = None
        self._last_wall = 0.0
        self._last_cpu = 0.0
        self._peak_cpu = 0.0
        self._peak_rss = 0

    def start(self) -> None:
        self._started_wall = self._last_wall = time.perf_counter()
        self._started_cpu = self._last_cpu = time.process_time()
        self._started_snapshot = _process_snapshot()
        if self._started_snapshot is not None:
            self._peak_rss = self._started_snapshot.rss_bytes
        self._thread = threading.Thread(
            target=self._sample_until_stopped,
            name="raw-selection-benchmark-resources",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> ResourceUsage:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        finished_wall, finished_cpu, finished_snapshot = self._sample_once()
        wall_seconds = max(finished_wall - self._started_wall, 1e-9)
        cpu_seconds = max(finished_cpu - self._started_cpu, 0.0)
        started_snapshot = self._started_snapshot
        mib = 1024 * 1024
        return ResourceUsage(
            wall_ms=round(wall_seconds * 1_000.0, 3),
            process_cpu_seconds=round(cpu_seconds, 6),
            average_cpu_core_percent=round(cpu_seconds / wall_seconds * 100.0, 3),
            peak_cpu_core_percent=round(self._peak_cpu, 3),
            rss_start_mib=(
                round(started_snapshot.rss_bytes / mib, 3)
                if started_snapshot is not None
                else None
            ),
            peak_rss_mib=(
                round(self._peak_rss / mib, 3)
                if finished_snapshot is not None
                else None
            ),
            process_read_operations=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "read_operations",
            ),
            process_write_operations=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "write_operations",
            ),
            process_other_operations=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "other_operations",
            ),
            process_read_bytes=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "read_bytes",
            ),
            process_write_bytes=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "write_bytes",
            ),
            process_other_bytes=_counter_delta(
                started_snapshot,
                finished_snapshot,
                "other_bytes",
            ),
        )

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._sample_once()

    def _sample_once(self) -> tuple[float, float, _ProcessSnapshot | None]:
        current_wall = time.perf_counter()
        current_cpu = time.process_time()
        elapsed = current_wall - self._last_wall
        if elapsed > 0:
            self._peak_cpu = max(
                self._peak_cpu,
                max(current_cpu - self._last_cpu, 0.0) / elapsed * 100.0,
            )
        self._last_wall = current_wall
        self._last_cpu = current_cpu
        snapshot = _process_snapshot()
        if snapshot is not None:
            self._peak_rss = max(self._peak_rss, snapshot.rss_bytes)
        return current_wall, current_cpu, snapshot


def _counter_delta(
    started: _ProcessSnapshot | None,
    finished: _ProcessSnapshot | None,
    field: str,
) -> int | None:
    if started is None or finished is None:
        return None
    return max(int(getattr(finished, field)) - int(getattr(started, field)), 0)


def _percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one latency sample is required")
    ordered = sorted(samples)
    rank = max(0, min(len(ordered) - 1, int(percentile * len(ordered) + 0.999) - 1))
    return ordered[rank]


def _summary(samples: Sequence[float]) -> dict[str, Any]:
    if not samples:
        return {
            "samples": 0,
            "p50_ms": None,
            "p95_ms": None,
            "minimum_ms": None,
            "maximum_ms": None,
        }
    return asdict(
        LatencySummary(
            samples=len(samples),
            p50_ms=round(statistics.median(samples), 3),
            p95_ms=round(_percentile(samples, 0.95), 3),
            minimum_ms=round(min(samples), 3),
            maximum_ms=round(max(samples), 3),
        )
    )


def _is_reparse_or_symlink(path: Path, stat_result: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _collect_sources(root: Path) -> tuple[SourceFile, ...]:
    resolved_root = root.expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("source root must be a directory")
    sources: list[SourceFile] = []
    for directory, names, files in os.walk(resolved_root, followlinks=False):
        directory_path = Path(directory)
        kept_names: list[str] = []
        for name in sorted(names, key=lambda value: (value.casefold(), value)):
            candidate = directory_path / name
            try:
                stat_result = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if not _is_reparse_or_symlink(candidate, stat_result):
                kept_names.append(name)
        names[:] = kept_names
        for name in sorted(files, key=lambda value: (value.casefold(), value)):
            # Match the production folder importer: macOS resource-fork artifacts
            # are metadata, not user images, even when their suffix is supported.
            if name.startswith("._"):
                continue
            candidate = directory_path / name
            extension = candidate.suffix.casefold()
            if extension not in _SUPPORTED_EXTENSIONS:
                continue
            try:
                stat_result = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_reparse_or_symlink(candidate, stat_result):
                continue
            relative_name = candidate.relative_to(resolved_root).as_posix()
            sources.append(
                SourceFile(
                    path=candidate.resolve(strict=True),
                    relative_name=relative_name,
                    extension=extension,
                    file_size=int(stat_result.st_size),
                    mtime_ns=int(stat_result.st_mtime_ns),
                    file_identity=f"{stat_result.st_dev:x}:{stat_result.st_ino:x}",
                )
            )
    sources.sort(key=lambda item: (item.relative_name.casefold(), item.relative_name))
    if not sources:
        raise RuntimeError("source root contains no supported images")
    return tuple(sources)


def _source_digest(sources: Sequence[SourceFile]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_name.encode("utf-8", errors="surrogatepass"))
        digest.update(
            (
                f"\0{source.extension}\0{source.file_size}\0{source.mtime_ns}"
                f"\0{source.file_identity}\0"
            ).encode()
        )
    return digest.hexdigest()


def _extension_summary(sources: Sequence[SourceFile]) -> dict[str, Any]:
    counts = Counter(source.extension for source in sources)
    sizes: dict[str, list[int]] = {}
    for source in sources:
        sizes.setdefault(source.extension, []).append(source.file_size)
    return {
        extension: {
            "count": counts[extension],
            "total_bytes": sum(values),
            "minimum_bytes": min(values),
            "median_bytes": int(statistics.median(values)),
            "maximum_bytes": max(values),
        }
        for extension, values in sorted(sizes.items())
    }


def _sample_manifest_contract() -> dict[str, Any]:
    return {
        "schema_version": _SAMPLE_MANIFEST_SCHEMA_VERSION,
        "source_order_and_version_digest": (
            "copy input.source_order_and_version_digest from a prior non-formal run"
        ),
        "arw_samples": [
            {
                "relative_name": "relative/path/example.ARW",
                "camera_model": _A7M4_CAMERA_MODEL,
                "raw_mode": ("compressed | lossless_compressed | uncompressed"),
                "size_category": "L | M | S",
            }
        ],
        "required_raw_mode_and_size_pairs": [
            {"raw_mode": raw_mode, "size_category": size_category}
            for raw_mode, size_category in sorted(_REQUIRED_A7M4_RAW_VARIANTS)
        ],
        "coverage_rule": (
            "every ARW in the benchmark source set must have one manifest entry; "
            "at least 300 entries must identify ILCE-7M4 and all required mode/size "
            "pairs must be present"
        ),
    }


def _missing_sample_manifest_report() -> dict[str, Any]:
    return {
        "status": "not_provided",
        "formal_manifest_coverage": False,
        "limitations": ["sample_manifest_not_provided"],
        "contract": _sample_manifest_contract(),
    }


def _load_sample_manifest(
    path: Path,
    sources: Sequence[SourceFile],
) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("sample manifest must be a JSON file")
    encoded = resolved.read_bytes()
    try:
        document = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sample manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("sample manifest root must be an object")
    return _validate_sample_manifest(
        document,
        sources,
        resolved_path=resolved,
        manifest_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _validate_sample_manifest(
    document: Mapping[str, Any],
    sources: Sequence[SourceFile],
    *,
    resolved_path: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    schema_version = document.get("schema_version")
    if schema_version != _SAMPLE_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"sample manifest schema_version must be {_SAMPLE_MANIFEST_SCHEMA_VERSION}"
        )
    declared_digest = document.get("source_order_and_version_digest")
    if not isinstance(declared_digest, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", declared_digest
    ):
        raise ValueError(
            "sample manifest source_order_and_version_digest must be a SHA-256 hex "
            "string"
        )
    samples = document.get("arw_samples")
    if not isinstance(samples, list):
        raise ValueError("sample manifest arw_samples must be an array")

    actual_digest = _source_digest(sources)
    source_by_name = {source.relative_name: source for source in sources}
    actual_arw_names = {
        source.relative_name for source in sources if source.extension == ".arw"
    }
    manifested_names: set[str] = set()
    verified_a7m4_names: set[str] = set()
    verified_variants: set[tuple[str, str]] = set()
    duplicate_entries = 0
    unknown_source_entries = 0
    non_arw_entries = 0
    camera_model_mismatches = 0
    invalid_variant_pairs = 0

    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise ValueError(f"sample manifest arw_samples[{index}] must be an object")
        relative_name = sample.get("relative_name")
        camera_model = sample.get("camera_model")
        raw_mode = sample.get("raw_mode")
        size_category = sample.get("size_category")
        if not isinstance(relative_name, str) or not relative_name:
            raise ValueError(
                f"sample manifest arw_samples[{index}].relative_name must be non-empty"
            )
        if not isinstance(camera_model, str) or not camera_model:
            raise ValueError(
                f"sample manifest arw_samples[{index}].camera_model must be non-empty"
            )
        if raw_mode not in {"compressed", "lossless_compressed", "uncompressed"}:
            raise ValueError(
                f"sample manifest arw_samples[{index}].raw_mode is unsupported"
            )
        if size_category not in {"L", "M", "S"}:
            raise ValueError(
                f"sample manifest arw_samples[{index}].size_category is unsupported"
            )
        if relative_name in manifested_names:
            duplicate_entries += 1
            continue
        manifested_names.add(relative_name)
        source = source_by_name.get(relative_name)
        if source is None:
            unknown_source_entries += 1
            continue
        if source.extension != ".arw":
            non_arw_entries += 1
            continue
        if camera_model != _A7M4_CAMERA_MODEL:
            camera_model_mismatches += 1
            continue
        variant = (str(raw_mode), str(size_category))
        if variant not in _REQUIRED_A7M4_RAW_VARIANTS:
            invalid_variant_pairs += 1
            continue
        verified_a7m4_names.add(relative_name)
        verified_variants.add(variant)

    missing_arw_names = actual_arw_names - manifested_names
    extra_manifest_names = manifested_names - actual_arw_names
    missing_variants = _REQUIRED_A7M4_RAW_VARIANTS - verified_variants
    limitations: list[str] = []
    if declared_digest.casefold() != actual_digest:
        limitations.append("sample_manifest_source_digest_mismatch")
    if duplicate_entries:
        limitations.append("sample_manifest_has_duplicate_arw_entries")
    if unknown_source_entries:
        limitations.append("sample_manifest_references_unknown_sources")
    if non_arw_entries:
        limitations.append("sample_manifest_references_non_arw_sources")
    if camera_model_mismatches:
        limitations.append("sample_manifest_camera_model_is_not_ilce_7m4")
    if invalid_variant_pairs:
        limitations.append("sample_manifest_has_unsupported_raw_mode_and_size_pair")
    if missing_arw_names:
        limitations.append("sample_manifest_does_not_cover_every_arw_source")
    if extra_manifest_names:
        limitations.append("sample_manifest_contains_non_project_arw_entries")
    if len(verified_a7m4_names) < 300:
        limitations.append("fewer_than_300_manifest_verified_a7m4_arw_sources")
    if missing_variants:
        limitations.append("a7m4_raw_mode_and_size_coverage_incomplete")

    return {
        "status": "verified" if not limitations else "not_formal",
        "path": str(resolved_path),
        "sha256": manifest_sha256,
        "schema_version": schema_version,
        "declared_source_order_and_version_digest": declared_digest.casefold(),
        "actual_source_order_and_version_digest": actual_digest,
        "source_digest_matches": declared_digest.casefold() == actual_digest,
        "manifest_entry_count": len(samples),
        "actual_arw_count": len(actual_arw_names),
        "manifested_unique_name_count": len(manifested_names),
        "verified_a7m4_arw_count": len(verified_a7m4_names),
        "camera_model": _A7M4_CAMERA_MODEL,
        "camera_model_mismatch_count": camera_model_mismatches,
        "invalid_raw_mode_and_size_pair_count": invalid_variant_pairs,
        "duplicate_entry_count": duplicate_entries,
        "unknown_source_entry_count": unknown_source_entries,
        "non_arw_entry_count": non_arw_entries,
        "unmanifested_arw_count": len(missing_arw_names),
        "extra_manifest_name_count": len(extra_manifest_names),
        "verified_raw_mode_and_size_pairs": [
            {"raw_mode": raw_mode, "size_category": size_category}
            for raw_mode, size_category in sorted(verified_variants)
        ],
        "missing_required_raw_mode_and_size_pairs": [
            {"raw_mode": raw_mode, "size_category": size_category}
            for raw_mode, size_category in sorted(missing_variants)
        ],
        "formal_manifest_coverage": not limitations,
        "limitations": limitations,
        "contract": _sample_manifest_contract(),
    }


def _source_report(
    root: Path,
    sources: Sequence[SourceFile],
    sample_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    extensions = _extension_summary(sources)
    arw_count = int(extensions.get(".arw", {}).get("count", 0))
    jpg_count = int(extensions.get(".jpg", {}).get("count", 0)) + int(
        extensions.get(".jpeg", {}).get("count", 0)
    )
    png_count = int(extensions.get(".png", {}).get("count", 0))
    limitations: list[str] = []
    if arw_count < 300:
        limitations.append("fewer_than_300_real_arw_sources")
    if jpg_count == 0:
        limitations.append("no_real_jpg_or_jpeg_source")
    if png_count == 0:
        limitations.append("no_real_png_source")
    if len(sources) < 1_000:
        limitations.append("fewer_than_1000_project_members")
    manifest_report = (
        dict(sample_manifest)
        if sample_manifest is not None
        else _missing_sample_manifest_report()
    )
    limitations.extend(str(item) for item in manifest_report["limitations"])
    return {
        "root": str(root.expanduser().resolve()),
        "source_count": len(sources),
        "total_bytes": sum(source.file_size for source in sources),
        "source_order_and_version_digest": _source_digest(sources),
        "extensions": extensions,
        "a7m4_sample_manifest": manifest_report,
        "a7m4_raw_mode_coverage": (
            "verified"
            if manifest_report.get("formal_manifest_coverage") is True
            else "not_verified"
        ),
        "formal_input_coverage": not limitations,
        "limitations": limitations,
        "immutability_check": (
            "relative path, size, mtime_ns and file identity are compared before "
            "and after; the tool never opens a source for writing"
        ),
    }


def _rotated_sources(
    sources: Sequence[SourceFile],
    count: int,
    round_index: int,
) -> tuple[SourceFile, ...]:
    if count > len(sources):
        raise ValueError("requested source count exceeds available sources")
    if count == len(sources):
        offset = round_index % len(sources)
    else:
        offset = (round_index * count) % len(sources)
    ordered = tuple(sources[offset:]) + tuple(sources[:offset])
    return ordered[:count]


def _tree_stats(root: Path) -> dict[str, int]:
    files = 0
    bytes_total = 0
    temporary_files = 0
    if not root.exists():
        return {"files": 0, "bytes": 0, "temporary_files": 0}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        files += 1
        with suppress(OSError):
            bytes_total += path.stat().st_size
        if path.suffix.casefold() == ".tmp" or path.name.startswith("."):
            temporary_files += 1
    return {
        "files": files,
        "bytes": bytes_total,
        "temporary_files": temporary_files,
    }


def _append_error(errors: list[str], message: str) -> None:
    if len(errors) < _MAX_ERROR_DETAILS:
        errors.append(message)


def _safe_message(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text or 'unspecified error'}"


def _gate(summary: Mapping[str, Any], maximum_ms: float) -> dict[str, Any]:
    p95 = summary.get("p95_ms")
    if not isinstance(p95, (int, float)):
        return {"status": "not_measured", "maximum_ms": maximum_ms}
    return {
        "status": "pass" if float(p95) <= maximum_ms else "fail",
        "maximum_ms": maximum_ms,
        "observed_p95_ms": float(p95),
    }


def _parallel_thumbnails(
    service: Any,
    members: Sequence[Mapping[str, Any]],
    request_concurrency: int,
) -> dict[str, Any]:
    """Request a fixed member list together and include caller queue delay."""

    if not members:
        raise ValueError("thumbnail batch cannot be empty")
    release = threading.Event()
    released_at = [0.0]

    def request(index_and_member: tuple[int, Mapping[str, Any]]) -> dict[str, Any]:
        index, member = index_and_member
        release.wait()
        executed_at = time.perf_counter()
        try:
            result = service.get_thumbnail_bytes(str(member["id"]))
            error = result.error
            payload_size = len(result.data)
        except BaseException as exc:  # retain a failed sample without a traceback
            error = _safe_message(exc)
            payload_size = 0
        finished_at = time.perf_counter()
        return {
            "index": index,
            "completion_ms": (finished_at - released_at[0]) * 1_000.0,
            "execution_ms": (finished_at - executed_at) * 1_000.0,
            "payload_size": payload_size,
            "error": error,
        }

    max_workers = min(len(members), request_concurrency)
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="raw-benchmark-request",
    ) as executor:
        futures = [executor.submit(request, pair) for pair in enumerate(members)]
        released_at[0] = time.perf_counter()
        release.set()
        results = [future.result() for future in futures]
    results.sort(key=lambda item: int(item["index"]))
    completion = [float(item["completion_ms"]) for item in results]
    execution = [float(item["execution_ms"]) for item in results]
    errors = [str(item["error"]) for item in results if item["error"]]
    return {
        "batch_ms": max(completion),
        "first_24_ms": max(completion[: min(24, len(completion))]),
        "completion_latency": _summary(completion),
        "execution_latency": _summary(execution),
        "_completion_samples": completion,
        "_execution_samples": execution,
        "payload_bytes": sum(int(item["payload_size"]) for item in results),
        "errors": len(errors),
        "error_details": errors[:_MAX_ERROR_DETAILS],
    }


def _create_project_with_sources(
    data_dir: Path,
    sources: Sequence[SourceFile],
) -> tuple[Any, str, list[dict[str, Any]], float, dict[str, Any]]:
    from image_vector_service.raw_selection.service import RawSelectionService

    service = RawSelectionService(data_dir)
    project = service.create_project("benchmark")
    started = time.perf_counter()
    imported = service.import_files(
        str(project["id"]),
        [source.path for source in sources],
    )
    import_ms = (time.perf_counter() - started) * 1_000.0
    members_result = service.list_members(
        str(project["id"]),
        offset=0,
        limit=max(1, len(sources)),
        sort_field="import_order",
        sort_direction="asc",
    )
    members = list(members_result["members"])
    return service, str(project["id"]), members, import_ms, imported


def _run_cold_scenario(request: Mapping[str, Any]) -> dict[str, Any]:
    sources = tuple(request["sources"])
    target_count = int(request["target_count"])
    groups = int(request["groups"])
    concurrency = int(request["request_concurrency"])
    scenario_root = Path(request["temporary_root"])
    if len(sources) < target_count:
        return {
            "status": "not_formal",
            "reason": "insufficient_unique_sources",
            "required": target_count,
            "available": len(sources),
        }

    batches: list[float] = []
    first_24: list[float] = []
    import_times: list[float] = []
    completion_samples: list[float] = []
    execution_samples: list[float] = []
    payload_bytes = 0
    errors: list[str] = []
    imported_counts: list[int] = []
    residual_temporary_files = 0
    sampler = _ProcessResourceSampler()
    sampler.start()
    for group in range(groups):
        chosen = _rotated_sources(sources, target_count, group)
        with tempfile.TemporaryDirectory(
            prefix=f"group-{group:03d}-",
            dir=scenario_root,
        ) as temporary:
            data_dir = Path(temporary) / "data"
            service, _project_id, members, import_ms, imported = (
                _create_project_with_sources(data_dir, chosen)
            )
            try:
                import_times.append(import_ms)
                imported_counts.append(int(imported.get("registered", 0)))
                if len(members) != target_count:
                    _append_error(
                        errors,
                        f"group {group}: expected {target_count} members, "
                        f"got {len(members)}",
                    )
                    continue
                measured = _parallel_thumbnails(service, members, concurrency)
                batches.append(float(measured["batch_ms"]))
                first_24.append(float(measured["first_24_ms"]))
                completion_samples.extend(
                    float(value) for value in measured["_completion_samples"]
                )
                execution_samples.extend(
                    float(value) for value in measured["_execution_samples"]
                )
                payload_bytes += int(measured["payload_bytes"])
                for detail in measured["error_details"]:
                    _append_error(errors, f"group {group}: {detail}")
                if int(measured["errors"]):
                    _append_error(
                        errors,
                        f"group {group}: {measured['errors']} thumbnail failures",
                    )
            finally:
                service.close()
                residual_temporary_files += _tree_stats(data_dir)["temporary_files"]
    resources = sampler.stop()
    batch_summary = _summary(batches)
    first_24_summary = _summary(first_24)
    gate_limit = (
        1_000.0 if target_count == 24 else 3_000.0 if target_count == 100 else 20_000.0
    )
    formal_project_size = target_count == 1_000 if request.get("project_all") else True
    return {
        "status": "measured" if formal_project_size else "not_formal",
        "reason": None
        if formal_project_size
        else "project_does_not_contain_1000_members",
        "groups": groups,
        "target_count": target_count,
        "request_concurrency": concurrency,
        "batch_latency": batch_summary,
        "fixed_first_24_latency": first_24_summary,
        "import_registration_latency": _summary(import_times),
        "per_item_completion_latency": _summary(completion_samples),
        "per_item_execution_latency": _summary(execution_samples),
        "throughput_items_per_second": round(
            (groups * target_count) / max(resources.wall_ms / 1_000.0, 1e-9),
            3,
        ),
        "payload_bytes": payload_bytes,
        "registered_counts": imported_counts,
        "errors": len(errors),
        "error_details": errors,
        "resources": asdict(resources),
        "residual": {
            "temporary_files": residual_temporary_files,
            "active_futures": 0,
        },
        "gate": (
            _gate(batch_summary, gate_limit)
            if formal_project_size
            else {
                "status": "not_formal",
                "maximum_ms": gate_limit,
                "observed_p95_ms": batch_summary.get("p95_ms"),
            }
        ),
        "scope": (
            "service calls include SQLite registration, decode, JPEG encode and "
            "cache write; no HTTP or DOM"
        ),
    }


def _run_hot_restart_scenario(request: Mapping[str, Any]) -> dict[str, Any]:
    sources = tuple(request["sources"])
    groups = int(request["groups"])
    target_count = 24
    concurrency = int(request["request_concurrency"])
    scenario_root = Path(request["temporary_root"])
    if len(sources) < target_count:
        return {
            "status": "not_formal",
            "reason": "insufficient_unique_sources",
            "required": target_count,
            "available": len(sources),
        }

    prepared: list[tuple[Path, str]] = []
    errors: list[str] = []
    cold_prepare_ms: list[float] = []
    for group in range(groups):
        group_root = scenario_root / f"prepared-{group:03d}"
        chosen = _rotated_sources(sources, target_count, group)
        service, project_id, members, _import_ms, _imported = (
            _create_project_with_sources(group_root / "data", chosen)
        )
        try:
            measured = _parallel_thumbnails(service, members, concurrency)
            cold_prepare_ms.append(float(measured["batch_ms"]))
            if int(measured["errors"]):
                _append_error(
                    errors,
                    f"prepare group {group}: {measured['errors']} failures",
                )
        finally:
            service.close()
        prepared.append((group_root / "data", project_id))

    from image_vector_service.raw_selection.service import RawSelectionService

    hot_batches: list[float] = []
    hot_first_24: list[float] = []
    payload_bytes = 0
    residual_temporary_files = 0
    sampler = _ProcessResourceSampler()
    sampler.start()
    for group, (data_dir, project_id) in enumerate(prepared):
        service = RawSelectionService(data_dir)
        try:
            members_result = service.list_members(
                project_id,
                offset=0,
                limit=target_count,
                sort_field="import_order",
                sort_direction="asc",
            )
            members = list(members_result["members"])
            measured = _parallel_thumbnails(service, members, concurrency)
            hot_batches.append(float(measured["batch_ms"]))
            hot_first_24.append(float(measured["first_24_ms"]))
            payload_bytes += int(measured["payload_bytes"])
            for detail in measured["error_details"]:
                _append_error(errors, f"hot group {group}: {detail}")
        finally:
            service.close()
            residual_temporary_files += _tree_stats(data_dir)["temporary_files"]
    resources = sampler.stop()
    hot_summary = _summary(hot_batches)
    return {
        "status": "measured",
        "groups": groups,
        "target_count": target_count,
        "cold_preparation_latency": _summary(cold_prepare_ms),
        "restart_hot_batch_latency": hot_summary,
        "restart_hot_first_24_latency": _summary(hot_first_24),
        "payload_bytes": payload_bytes,
        "errors": len(errors),
        "error_details": errors,
        "resources": asdict(resources),
        "residual": {
            "temporary_files": residual_temporary_files,
            "active_futures": 0,
        },
        "gate": _gate(hot_summary, 200.0),
        "scope": (
            "service restart with the same isolated SQLite database and persistent "
            "derived cache; no HTTP or DOM"
        ),
    }


def _decode_source(
    source: SourceFile,
    kind: str,
    cache_root: Path,
) -> tuple[int, int, int, str | None]:
    from image_vector_service.raw_selection.cache import DerivedCache
    from image_vector_service.raw_selection.decoder import (
        decode_full,
        decode_thumbnail,
        image_to_jpeg_bytes,
    )

    result = (
        decode_full(str(source.path), source.extension)
        if kind == "arw_full"
        else decode_thumbnail(str(source.path), source.extension)
    )
    if result.image is None or result.error is not None:
        if result.image is not None:
            result.image.close()
        return 0, 0, 0, result.error or "decode returned no image"
    image = result.image
    try:
        width, height = image.size
        if kind == "arw_full":
            return width, height, 0, None
        payload = image_to_jpeg_bytes(image, quality=85)
        DerivedCache(cache_root).put(
            payload,
            normalized_path=str(source.path),
            file_size=source.file_size,
            mtime_ns=source.mtime_ns,
            kind="thumbnails",
        )
        return width, height, len(payload), None
    finally:
        image.close()


def _decode_matrix_round(
    sources: Sequence[SourceFile],
    *,
    kind: str,
    workers: int,
    cache_root: Path,
) -> dict[str, Any]:
    release = threading.Event()
    released_at = [0.0]

    def work(index_and_source: tuple[int, SourceFile]) -> dict[str, Any]:
        index, source = index_and_source
        release.wait()
        executed_at = time.perf_counter()
        try:
            width, height, payload_bytes, error = _decode_source(
                source,
                kind,
                cache_root,
            )
        except BaseException as exc:
            width = height = payload_bytes = 0
            error = _safe_message(exc)
        finished_at = time.perf_counter()
        return {
            "index": index,
            "completion_ms": (finished_at - released_at[0]) * 1_000.0,
            "execution_ms": (finished_at - executed_at) * 1_000.0,
            "width": width,
            "height": height,
            "payload_bytes": payload_bytes,
            "error": error,
        }

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"raw-matrix-{kind}",
    ) as executor:
        futures = [executor.submit(work, pair) for pair in enumerate(sources)]
        released_at[0] = time.perf_counter()
        release.set()
        results = [future.result() for future in futures]
    results.sort(key=lambda item: int(item["index"]))
    completion = [float(item["completion_ms"]) for item in results]
    execution = [float(item["execution_ms"]) for item in results]
    errors = [str(item["error"]) for item in results if item["error"]]
    dimensions = sorted(
        {
            (int(item["width"]), int(item["height"]))
            for item in results
            if int(item["width"]) > 0 and int(item["height"]) > 0
        }
    )
    return {
        "batch_ms": max(completion),
        "completion_samples": completion,
        "execution_samples": execution,
        "payload_bytes": sum(int(item["payload_bytes"]) for item in results),
        "dimensions": [list(value) for value in dimensions],
        "errors": len(errors),
        "error_details": errors[:_MAX_ERROR_DETAILS],
    }


def _executor_cancellation_probe(
    sources: Sequence[SourceFile],
    *,
    kind: str,
    workers: int,
    cache_root: Path,
) -> dict[str, Any]:
    """Cancel pending real decodes after an event proves all workers started."""

    task_count = min(len(sources), max(workers * 3, workers + 1))
    if task_count <= workers:
        return {
            "status": "not_formal",
            "reason": "insufficient_sources_for_pending_work",
        }
    selected = tuple(sources[:task_count])
    release_running = threading.Event()
    all_workers_started = threading.Event()
    start_lock = threading.Lock()
    started = 0

    def work(source: SourceFile) -> tuple[bool, str | None]:
        nonlocal started
        with start_lock:
            started += 1
            if started >= workers:
                all_workers_started.set()
        if not release_running.wait(timeout=30.0):
            return False, "release event timed out"
        try:
            _width, _height, _payload, error = _decode_source(
                source,
                kind,
                cache_root,
            )
            return error is None, error
        except BaseException as exc:
            return False, _safe_message(exc)

    futures: list[Future[tuple[bool, str | None]]] = []
    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"raw-cancel-{kind}",
    )
    try:
        futures = [executor.submit(work, source) for source in selected]
        if not all_workers_started.wait(timeout=30.0):
            release_running.set()
            return {
                "status": "failed",
                "reason": "workers_did_not_reach_event_barrier",
            }
        cancel_started = time.perf_counter()
        cancelled_pending = sum(future.cancel() for future in futures)
        release_running.set()
        completed_running = 0
        failed_running = 0
        errors: list[str] = []
        for future in futures:
            if future.cancelled():
                continue
            succeeded, error = future.result(timeout=300.0)
            if succeeded:
                completed_running += 1
            else:
                failed_running += 1
                if error:
                    _append_error(errors, error)
        latency_ms = (time.perf_counter() - cancel_started) * 1_000.0
    finally:
        release_running.set()
        executor.shutdown(wait=True, cancel_futures=True)
    residual_futures = sum(not future.done() for future in futures)
    return {
        "status": "measured",
        "scope": (
            "benchmark-owned bounded executor; dfe2d75 production scheduler exposes "
            "no pending-task cancellation API"
        ),
        "task_count": task_count,
        "workers_confirmed_started": min(started, workers),
        "cancelled_pending": cancelled_pending,
        "completed_running": completed_running,
        "failed_running": failed_running,
        "cancel_effective_latency_ms": round(latency_ms, 3),
        "residual_futures": residual_futures,
        "residual_temporary_files": _tree_stats(cache_root)["temporary_files"],
        "error_details": errors,
        "production_scheduler_cancellation": "not_available_on_dfe2d75",
    }


def _scheduler_cancellation_probe(
    sources: Sequence[SourceFile],
    *,
    kind: str,
    workers: int,
    cache_root: Path,
) -> dict[str, Any]:
    """Exercise the production scheduler when its bounded submit API exists."""

    try:
        from image_vector_service.raw_selection.scheduler import (
            DecodeScheduler,
            TaskPriority,
        )
    except ImportError:
        return _executor_cancellation_probe(
            sources,
            kind=kind,
            workers=workers,
            cache_root=cache_root,
        )
    if not hasattr(DecodeScheduler, "submit"):
        return _executor_cancellation_probe(
            sources,
            kind=kind,
            workers=workers,
            cache_root=cache_root,
        )

    task_count = min(len(sources), workers * 3)
    if task_count < workers * 3:
        return {
            "status": "not_formal",
            "reason": "insufficient_sources_for_bounded_queue_probe",
            "required": workers * 3,
            "available": len(sources),
        }
    selected = tuple(sources[:task_count])
    lane_by_kind = {
        "arw_embed": "arw_embed",
        "jpg_thumbnail": "jpg_thumbnail",
        "png_thumbnail": "png_thumbnail",
        "arw_full": "arw_full",
    }
    selected_lane = lane_by_kind[kind]
    lane_names = tuple(lane_by_kind.values())
    worker_counts = {lane: 1 for lane in lane_names}
    worker_counts[selected_lane] = workers
    queue_capacities = {lane: 1 for lane in lane_names}
    queue_capacities[selected_lane] = workers * 2
    try:
        scheduler = DecodeScheduler(
            worker_counts=worker_counts,
            queue_capacities=queue_capacities,
        )
    except TypeError:
        return _executor_cancellation_probe(
            sources,
            kind=kind,
            workers=workers,
            cache_root=cache_root,
        )

    release_running = threading.Event()
    all_workers_started = threading.Event()
    start_lock = threading.Lock()
    started = 0

    def work(source: SourceFile) -> tuple[bool, str | None]:
        nonlocal started
        with start_lock:
            started += 1
            if started >= workers:
                all_workers_started.set()
        if not release_running.wait(timeout=30.0):
            return False, "release event timed out"
        try:
            _width, _height, _payload, error = _decode_source(
                source,
                kind,
                cache_root,
            )
            return error is None, error
        except BaseException as exc:
            return False, _safe_message(exc)

    def task_for(source: SourceFile) -> Callable[[], tuple[bool, str | None]]:
        def run() -> tuple[bool, str | None]:
            return work(source)

        return run

    extension = selected[0].extension
    decode_kind = "full" if kind == "arw_full" else "thumb"
    running_tasks: list[Any] = []
    queued_tasks: list[Any] = []
    errors: list[str] = []
    try:
        for source in selected[:workers]:
            running_tasks.append(
                scheduler.submit(
                    extension,
                    decode_kind,
                    task_for(source),
                    priority=TaskPriority.BACKGROUND,
                    scope="benchmark-cancel",
                )
            )
        if not all_workers_started.wait(timeout=30.0):
            return {
                "status": "failed",
                "reason": "production_workers_did_not_reach_event_barrier",
            }
        for source in selected[workers:]:
            queued_tasks.append(
                scheduler.submit(
                    extension,
                    decode_kind,
                    task_for(source),
                    priority=TaskPriority.BACKGROUND,
                    scope="benchmark-cancel",
                )
            )
        cancel_started = time.perf_counter()
        scheduler.cancel_scope("benchmark-cancel")
        queued_cancel_latency_ms = (time.perf_counter() - cancel_started) * 1_000.0
        cancelled_pending = sum(task.future.cancelled() for task in queued_tasks)
        release_running.set()
        drain_started = time.perf_counter()
        completed_running = 0
        cancelled_running = 0
        failed_running = 0
        for task in running_tasks:
            try:
                succeeded, error = task.result(timeout=300.0)
            except CancelledError:
                cancelled_running += 1
                continue
            if succeeded:
                completed_running += 1
            else:
                failed_running += 1
                if error:
                    _append_error(errors, error)
        self_drain_ms = (time.perf_counter() - drain_started) * 1_000.0
        self_metrics = scheduler.metrics()
    finally:
        release_running.set()
        scheduler.shutdown(
            wait=True,
            cancel_queued=True,
            timeout=300.0,
        )
    return {
        "status": "measured",
        "scope": "production DecodeScheduler bounded lane and scope generation",
        "task_count": task_count,
        "workers_confirmed_started": min(started, workers),
        "cancelled_pending": cancelled_pending,
        "completed_running": completed_running,
        "cancelled_running": cancelled_running,
        "failed_running": failed_running,
        "queued_cancel_latency_ms": round(queued_cancel_latency_ms, 3),
        "running_drain_latency_ms": round(self_drain_ms, 3),
        "residual_futures": sum(
            not task.future.done() for task in (*running_tasks, *queued_tasks)
        ),
        "residual_temporary_files": _tree_stats(cache_root)["temporary_files"],
        "error_details": errors,
        "production_scheduler_cancellation": "measured",
        "scheduler_metrics_before_shutdown": self_metrics,
    }


def _real_cancellation_probe(
    sources: Sequence[SourceFile],
    *,
    kind: str,
    workers: int,
    cache_root: Path,
) -> dict[str, Any]:
    return _scheduler_cancellation_probe(
        sources,
        kind=kind,
        workers=workers,
        cache_root=cache_root,
    )


def _run_matrix_worker(request: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(request["kind"])
    workers = int(request["workers"])
    rounds = int(request["rounds"])
    all_sources = tuple(request["sources"])
    maximum_items = int(request["maximum_items"])
    scenario_root = Path(request["temporary_root"])
    accepted = _MATRIX_EXTENSIONS[kind]
    selected = tuple(source for source in all_sources if source.extension in accepted)
    if maximum_items > 0:
        selected = selected[:maximum_items]
    if not selected:
        extension_label = ",".join(sorted(accepted))
        return {
            "status": "not_formal",
            "reason": f"no_real_source_for_{extension_label}",
            "kind": kind,
            "workers": workers,
            "rounds": rounds,
            "source_count": 0,
        }

    batches: list[float] = []
    completion: list[float] = []
    execution: list[float] = []
    payload_bytes = 0
    errors: list[str] = []
    dimension_sets: list[list[list[int]]] = []
    residual_temporary_files = 0
    sampler = _ProcessResourceSampler()
    sampler.start()
    for round_index in range(rounds):
        with tempfile.TemporaryDirectory(
            prefix=f"matrix-{kind}-{workers}-{round_index:03d}-",
            dir=scenario_root,
        ) as temporary:
            cache_root = Path(temporary) / "cache"
            measured = _decode_matrix_round(
                selected,
                kind=kind,
                workers=workers,
                cache_root=cache_root,
            )
            batches.append(float(measured["batch_ms"]))
            completion.extend(float(value) for value in measured["completion_samples"])
            execution.extend(float(value) for value in measured["execution_samples"])
            payload_bytes += int(measured["payload_bytes"])
            dimension_sets.append(measured["dimensions"])
            for detail in measured["error_details"]:
                _append_error(errors, f"round {round_index}: {detail}")
            if int(measured["errors"]):
                _append_error(
                    errors,
                    f"round {round_index}: {measured['errors']} decode failures",
                )
            residual_temporary_files += _tree_stats(cache_root)["temporary_files"]
    resources = sampler.stop()
    with tempfile.TemporaryDirectory(
        prefix=f"cancel-{kind}-{workers}-",
        dir=scenario_root,
    ) as cancellation_temporary:
        cancellation = _real_cancellation_probe(
            selected,
            kind=kind,
            workers=workers,
            cache_root=Path(cancellation_temporary) / "cache",
        )
    batch_summary = _summary(batches)
    runtime_facts = None
    if kind == "arw_full":
        try:
            from image_vector_service.raw_selection.decoder import (
                full_decode_runtime_facts,
            )
        except ImportError:
            runtime_facts = {
                "status": "not_reported_by_code_revision",
                "reason": "full_decode_runtime_facts is unavailable",
            }
        else:
            runtime_facts = asdict(full_decode_runtime_facts())
    return {
        "status": "measured",
        "kind": kind,
        "workers": workers,
        "rounds": rounds,
        "source_count": len(selected),
        "batch_latency": batch_summary,
        "per_item_completion_latency": _summary(completion),
        "per_item_execution_latency": _summary(execution),
        "throughput_items_per_second": round(
            (len(selected) * rounds) / max(sum(batches) / 1_000.0, 1e-9),
            3,
        ),
        "payload_bytes": payload_bytes,
        "dimension_sets_identical": all(
            dimensions == dimension_sets[0] for dimensions in dimension_sets[1:]
        ),
        "errors": len(errors),
        "error_details": errors,
        "resources": asdict(resources),
        "cancellation": cancellation,
        "full_decode_runtime_facts": runtime_facts,
        "residual": {
            "temporary_files": residual_temporary_files,
            "active_futures": 0,
        },
        "scope": (
            "bounded pure decode plus thumbnail JPEG/cache write"
            if kind != "arw_full"
            else (
                "bounded full RAW decode only; image is released before the task "
                "returns"
            )
        ),
    }


def _parallel_cached_previews(
    service: Any,
    member_ids: Sequence[str],
    *,
    display_width: int,
    display_height: int,
) -> dict[str, Any]:
    release = threading.Event()
    released_at = [0.0]

    def request(member_id: str) -> tuple[float, str | None]:
        release.wait()
        try:
            result = service.get_preview_bytes(
                member_id,
                display_width=display_width,
                display_height=display_height,
                look="as_shot",
                quality="embedded",
            )
            error = result.error
        except BaseException as exc:
            error = _safe_message(exc)
        return (time.perf_counter() - released_at[0]) * 1_000.0, error

    with ThreadPoolExecutor(
        max_workers=len(member_ids),
        thread_name_prefix="raw-preview-compare",
    ) as executor:
        futures = [executor.submit(request, member_id) for member_id in member_ids]
        released_at[0] = time.perf_counter()
        release.set()
        results = [future.result() for future in futures]
    return {
        "batch_ms": max(latency for latency, _error in results),
        "errors": [error for _latency, error in results if error],
    }


def _run_preview_scenario(request: Mapping[str, Any]) -> dict[str, Any]:
    sources = tuple(
        source for source in request["sources"] if source.extension == ".arw"
    )
    groups = int(request["groups"])
    scenario_root = Path(request["temporary_root"])
    display_width = int(request["display_width"])
    display_height = int(request["display_height"])
    if len(sources) < 2:
        return {
            "status": "not_formal",
            "reason": "at_least_two_arw_sources_are_required",
            "available": len(sources),
        }

    cold: list[float] = []
    hot: list[float] = []
    restart_hot: list[float] = []
    compare: list[float] = []
    errors: list[str] = []
    residual_temporary_files = 0
    sampler = _ProcessResourceSampler()
    sampler.start()
    for group in range(groups):
        chosen = _rotated_sources(sources, 2, group)
        with tempfile.TemporaryDirectory(
            prefix=f"preview-{group:03d}-",
            dir=scenario_root,
        ) as temporary:
            data_dir = Path(temporary) / "data"
            service, project_id, members, _import_ms, _imported = (
                _create_project_with_sources(data_dir, chosen)
            )
            member_ids = [str(member["id"]) for member in members]
            try:
                started = time.perf_counter()
                first = service.get_preview_bytes(
                    member_ids[0],
                    display_width=display_width,
                    display_height=display_height,
                    look="as_shot",
                    quality="embedded",
                )
                cold.append((time.perf_counter() - started) * 1_000.0)
                if first.error:
                    _append_error(errors, f"group {group} cold: {first.error}")

                started = time.perf_counter()
                first_hot = service.get_preview_bytes(
                    member_ids[0],
                    display_width=display_width,
                    display_height=display_height,
                    look="as_shot",
                    quality="embedded",
                )
                hot.append((time.perf_counter() - started) * 1_000.0)
                if first_hot.error:
                    _append_error(errors, f"group {group} hot: {first_hot.error}")

                second = service.get_preview_bytes(
                    member_ids[1],
                    display_width=display_width,
                    display_height=display_height,
                    look="as_shot",
                    quality="embedded",
                )
                if second.error:
                    _append_error(
                        errors, f"group {group} prepare second: {second.error}"
                    )
                compared = _parallel_cached_previews(
                    service,
                    member_ids,
                    display_width=display_width,
                    display_height=display_height,
                )
                compare.append(float(compared["batch_ms"]))
                for error in compared["errors"]:
                    _append_error(errors, f"group {group} compare: {error}")
            finally:
                service.close()

            from image_vector_service.raw_selection.service import RawSelectionService

            restarted = RawSelectionService(data_dir)
            try:
                persisted = restarted.list_members(
                    project_id,
                    offset=0,
                    limit=2,
                    sort_field="import_order",
                    sort_direction="asc",
                )["members"]
                started = time.perf_counter()
                result = restarted.get_preview_bytes(
                    str(persisted[0]["id"]),
                    display_width=display_width,
                    display_height=display_height,
                    look="as_shot",
                    quality="embedded",
                )
                restart_hot.append((time.perf_counter() - started) * 1_000.0)
                if result.error:
                    _append_error(errors, f"group {group} restart: {result.error}")
            finally:
                restarted.close()
                residual_temporary_files += _tree_stats(data_dir)["temporary_files"]
    resources = sampler.stop()
    cold_summary = _summary(cold)
    hot_summary = _summary(hot)
    restart_summary = _summary(restart_hot)
    compare_summary = _summary(compare)
    return {
        "status": "measured",
        "groups": groups,
        "display_size": [display_width, display_height],
        "as_shot_cold_latency": cold_summary,
        "as_shot_hot_latency": hot_summary,
        "as_shot_restart_hot_latency": restart_summary,
        "cached_compare_two_latency": compare_summary,
        "errors": len(errors),
        "error_details": errors,
        "resources": asdict(resources),
        "residual": {
            "temporary_files": residual_temporary_files,
            "active_futures": 0,
        },
        "gates": {
            "embedded_preview_cold": _gate(cold_summary, 300.0),
            "cached_preview": _gate(hot_summary, 100.0),
            "cached_restart_preview": _gate(restart_summary, 100.0),
            "cached_compare_two": _gate(compare_summary, 200.0),
        },
        "scope": (
            "direct service calls include decode, JPEG, persistent cache; no HTTP "
            "or DOM"
        ),
    }


def _run_look_scenario(request: Mapping[str, Any]) -> dict[str, Any]:
    sources = tuple(
        source for source in request["sources"] if source.extension == ".arw"
    )
    groups = int(request["groups"])
    look_ids = tuple(str(value) for value in request["look_ids"])
    scenario_root = Path(request["temporary_root"])
    display_width = int(request["display_width"])
    display_height = int(request["display_height"])
    if not sources:
        return {
            "status": "not_formal",
            "reason": "no_real_arw_source",
            "available": 0,
        }

    from image_vector_service.raw_selection.cache import DerivedCache
    from image_vector_service.raw_selection.creative_look import (
        LOOK_CONFIG_VERSION,
        apply_creative_look,
        is_valid_look,
    )
    from image_vector_service.raw_selection.decoder import (
        decode_full,
        image_to_jpeg_bytes,
    )

    invalid_looks = [look for look in look_ids if not is_valid_look(look)]
    if invalid_looks:
        return {
            "status": "failed",
            "reason": "invalid_creative_look",
            "invalid_looks": invalid_looks,
        }

    results: dict[str, Any] = {}
    all_errors: list[str] = []
    residual_temporary_files = 0
    sampler = _ProcessResourceSampler()
    sampler.start()
    for look in look_ids:
        decode_times: list[float] = []
        transform_times: list[float] = []
        encode_times: list[float] = []
        cache_write_times: list[float] = []
        cache_hit_times: list[float] = []
        total_times: list[float] = []
        payload_bytes = 0
        look_errors: list[str] = []
        for group in range(groups):
            source = sources[group % len(sources)]
            with tempfile.TemporaryDirectory(
                prefix=f"look-{look}-{group:03d}-",
                dir=scenario_root,
            ) as temporary:
                cache_root = Path(temporary) / "cache"
                cache = DerivedCache(cache_root)
                total_started = time.perf_counter()
                decode_started = time.perf_counter()
                decoded = decode_full(str(source.path), source.extension)
                decode_times.append((time.perf_counter() - decode_started) * 1_000.0)
                if decoded.image is None or decoded.error is not None:
                    error = decoded.error or "full decode returned no image"
                    _append_error(look_errors, f"group {group}: {error}")
                    _append_error(all_errors, f"{look} group {group}: {error}")
                    if decoded.image is not None:
                        decoded.image.close()
                    continue
                base_image = decoded.image
                looked_image = None
                try:
                    transform_started = time.perf_counter()
                    looked_image = apply_creative_look(base_image, look)
                    transform_times.append(
                        (time.perf_counter() - transform_started) * 1_000.0
                    )
                    encode_started = time.perf_counter()
                    payload = image_to_jpeg_bytes(looked_image, quality=88)
                    encode_times.append(
                        (time.perf_counter() - encode_started) * 1_000.0
                    )
                    payload_bytes += len(payload)
                    extra = (
                        f"{display_width}x{display_height}|{look}|{LOOK_CONFIG_VERSION}"
                    )
                    write_started = time.perf_counter()
                    cache.put(
                        payload,
                        normalized_path=str(source.path),
                        file_size=source.file_size,
                        mtime_ns=source.mtime_ns,
                        kind="previews",
                        extra=extra,
                    )
                    cache_write_times.append(
                        (time.perf_counter() - write_started) * 1_000.0
                    )
                    hit_started = time.perf_counter()
                    cached = cache.get(
                        normalized_path=str(source.path),
                        file_size=source.file_size,
                        mtime_ns=source.mtime_ns,
                        kind="previews",
                        extra=extra,
                    )
                    cache_hit_times.append(
                        (time.perf_counter() - hit_started) * 1_000.0
                    )
                    if cached != payload:
                        error = f"group {group}: derived cache payload mismatch"
                        _append_error(look_errors, error)
                        _append_error(all_errors, f"{look} {error}")
                    total_times.append((time.perf_counter() - total_started) * 1_000.0)
                except BaseException as exc:
                    error = f"group {group}: {_safe_message(exc)}"
                    _append_error(look_errors, error)
                    _append_error(all_errors, f"{look} {error}")
                finally:
                    if looked_image is not None and looked_image is not base_image:
                        looked_image.close()
                    base_image.close()
                    residual_temporary_files += _tree_stats(cache_root)[
                        "temporary_files"
                    ]
                    gc.collect()
        results[look] = {
            "groups": groups,
            "base_decode_latency": _summary(decode_times),
            "look_transform_latency": _summary(transform_times),
            "jpeg_encode_latency": _summary(encode_times),
            "cache_write_latency": _summary(cache_write_times),
            "derived_cache_hit_latency": _summary(cache_hit_times),
            "cold_total_latency": _summary(total_times),
            "payload_bytes": payload_bytes,
            "errors": len(look_errors),
            "error_details": look_errors,
        }
    resources = sampler.stop()
    return {
        "status": "measured",
        "groups_per_look": groups,
        "looks": results,
        "errors": len(all_errors),
        "error_details": all_errors,
        "resources": asdict(resources),
        "residual": {
            "temporary_files": residual_temporary_files,
            "active_futures": 0,
        },
        "base_decode_reuse": {
            "status": "not_measured_by_this_stage_benchmark",
            "reason": (
                "this stage benchmark intentionally times decode, transform, encode "
                "and cache independently of the production service LRU"
            ),
        },
        "calibration_status": "not_formal_without_trusted_camera_reference_jpegs",
        "scope": (
            "stage timings call the same decoder, look transform, JPEG encoder and "
            "DerivedCache used by production; no HTTP or DOM"
        ),
    }


def _scenario_child(connection: Any, request: Mapping[str, Any]) -> None:
    try:
        action = str(request["action"])
        dispatch: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "cold": _run_cold_scenario,
            "hot_restart": _run_hot_restart_scenario,
            "matrix": _run_matrix_worker,
            "preview": _run_preview_scenario,
            "looks": _run_look_scenario,
        }
        runner = dispatch.get(action)
        if runner is None:
            raise ValueError(f"unknown scenario action: {action}")
        result = runner(request)
        connection.send({"ok": True, "result": result})
    except BaseException as exc:
        connection.send({"ok": False, "error": _safe_message(exc)})
    finally:
        connection.close()


def _run_isolated(
    request: Mapping[str, Any],
    *,
    run_root: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    scenario_name = str(request.get("name") or request["action"])
    scenario_path: Path | None = None
    result: dict[str, Any]
    with tempfile.TemporaryDirectory(
        prefix=f"scenario-{scenario_name}-",
        dir=run_root,
    ) as temporary:
        scenario_path = Path(temporary).resolve()
        child_request = dict(request)
        child_request["temporary_root"] = str(scenario_path)
        parent_connection, child_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_scenario_child,
            args=(child_connection, child_request),
            name=f"raw-selection-benchmark-{scenario_name}",
        )
        process.start()
        child_connection.close()
        message: dict[str, Any] | None = None
        try:
            if not parent_connection.poll(timeout_seconds):
                process.terminate()
                process.join(timeout=10.0)
                result = {
                    "status": "failed",
                    "reason": "child_timeout",
                    "timeout_seconds": timeout_seconds,
                }
            else:
                message = parent_connection.recv()
                if not message.get("ok"):
                    result = {
                        "status": "failed",
                        "reason": "child_error",
                        "error": str(message.get("error") or "unknown child error"),
                    }
                else:
                    payload = message.get("result")
                    result = (
                        dict(payload)
                        if isinstance(payload, Mapping)
                        else {
                            "status": "failed",
                            "reason": "invalid_child_result",
                        }
                    )
        finally:
            parent_connection.close()
        process.join(timeout=30.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10.0)
            result = {
                "status": "failed",
                "reason": "child_did_not_exit_cleanly",
            }
        result["child_exit_code"] = process.exitcode
        result["child_exit_clean"] = process.exitcode == 0
        result["owned_temporary_before_cleanup"] = _tree_stats(scenario_path)
    result["owned_temporary_cleanup_complete"] = bool(
        scenario_path is not None and not scenario_path.exists()
    )
    return result


def _git_revision() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if len(value) == 40 else None


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_version_marker_matches(marker: str, target: tuple[int, int]) -> bool:
    match = re.fullmatch(
        r"""python_version\s*(==|!=|<=|>=|<|>)\s*["'](\d+)\.(\d+)["']""",
        marker.strip(),
    )
    if match is None:
        raise ValueError(f"unsupported requirements-lock marker: {marker}")
    operator, major, minor = match.groups()
    expected = (int(major), int(minor))
    return {
        "==": target == expected,
        "!=": target != expected,
        "<=": target <= expected,
        ">=": target >= expected,
        "<": target < expected,
        ">": target > expected,
    }[operator]


def _locked_versions_for_python(
    lock_path: Path,
    target: tuple[int, int],
) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        lock_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, separator, marker = line.partition(";")
        if separator and not _python_version_marker_matches(marker, target):
            continue
        name, pin_separator, version = requirement.strip().partition("==")
        if not pin_separator or not name.strip() or not version.strip():
            raise ValueError(
                f"requirements-lock line {line_number} is not an exact == pin"
            )
        normalized_name = name.strip().casefold().replace("_", "-")
        if normalized_name in pins:
            raise ValueError(
                f"requirements-lock has multiple active pins for {name.strip()}"
            )
        pins[normalized_name] = version.strip()
    return pins


def _runtime_compatibility_report() -> dict[str, Any]:
    lock_path = _REPOSITORY_ROOT / "requirements-lock.txt"
    installed_versions = {
        package: _package_version(package) for package in _LOCKED_RUNTIME_PACKAGES
    }
    limitations: list[str] = []
    locked_versions: dict[str, str] = {}
    lock_sha256: str | None = None
    if not lock_path.is_file():
        limitations.append("requirements_lock_missing")
    else:
        encoded = lock_path.read_bytes()
        lock_sha256 = hashlib.sha256(encoded).hexdigest()
        locked_versions = _locked_versions_for_python(lock_path, _TARGET_PYTHON)

    expected_versions = {
        package: locked_versions.get(package.casefold().replace("_", "-"))
        for package in _LOCKED_RUNTIME_PACKAGES
    }
    missing_pins = [
        package for package, version in expected_versions.items() if version is None
    ]
    dependency_matches = {
        package: (
            expected_versions[package] is not None
            and installed_versions[package] == expected_versions[package]
        )
        for package in _LOCKED_RUNTIME_PACKAGES
    }
    python_matches = sys.version_info[:2] == _TARGET_PYTHON
    dependencies_match = not missing_pins and all(dependency_matches.values())
    if not python_matches:
        limitations.append("runtime_python_does_not_match_target_python_3_12")
    if missing_pins:
        limitations.append("target_runtime_dependency_pin_missing")
    if not dependencies_match:
        limitations.append("installed_dependencies_do_not_match_target_lock")
    return {
        "target_python": f"{_TARGET_PYTHON[0]}.{_TARGET_PYTHON[1]}",
        "current_python": platform.python_version(),
        "python_matches_target": python_matches,
        "requirements_lock_path": str(lock_path.resolve()),
        "requirements_lock_sha256": lock_sha256,
        "locked_versions_for_target_python": expected_versions,
        "installed_versions": installed_versions,
        "dependency_matches_lock": dependency_matches,
        "dependencies_match_lock": dependencies_match,
        "matches_target_closure": python_matches and dependencies_match,
        "limitations": limitations,
    }


def _tool_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _atomic_write_json(path: Path, report: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _parse_unique_csv(
    value: str,
    *,
    allowed: Iterable[str],
    label: str,
) -> tuple[str, ...]:
    allowed_values = frozenset(allowed)
    parsed = tuple(part.strip().casefold() for part in value.split(",") if part.strip())
    if not parsed or len(set(parsed)) != len(parsed):
        raise argparse.ArgumentTypeError(f"{label} must contain unique values")
    invalid = [part for part in parsed if part not in allowed_values]
    if invalid:
        raise argparse.ArgumentTypeError(f"unsupported {label}: {','.join(invalid)}")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _scenario_values(value: str) -> tuple[str, ...]:
    parsed = _parse_unique_csv(
        value,
        allowed=("baseline", "all", *_ALL_SCENARIOS),
        label="scenarios",
    )
    if "all" in parsed:
        if len(parsed) != 1:
            raise argparse.ArgumentTypeError(
                "all cannot be combined with other scenarios"
            )
        return _ALL_SCENARIOS
    expanded: list[str] = []
    for item in parsed:
        values = _BASELINE_SCENARIOS if item == "baseline" else (item,)
        for candidate in values:
            if candidate not in expanded:
                expanded.append(candidate)
    return tuple(expanded)


def _matrix_values(value: str) -> tuple[str, ...]:
    return _parse_unique_csv(
        value,
        allowed=_MATRIX_WORKERS,
        label="matrix kinds",
    )


def _look_values(value: str) -> tuple[str, ...]:
    return _parse_unique_csv(value, allowed=_LOOK_IDS, label="look IDs")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--sample-manifest",
        type=Path,
        help=(
            "UTF-8 JSON manifest binding every ARW source to ILCE-7M4, RAW mode "
            "and size category; omit only for a deliberately non-formal run"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--temporary-parent",
        type=Path,
        default=Path(tempfile.gettempdir()),
    )
    parser.add_argument(
        "--scenarios",
        type=_scenario_values,
        default=_BASELINE_SCENARIOS,
        help=(
            "baseline, all, or comma-separated cold24,cold100,project,hot24,"
            "preview,matrix,looks"
        ),
    )
    parser.add_argument("--groups", type=_positive_int, default=20)
    parser.add_argument("--look-groups", type=_positive_int, default=20)
    parser.add_argument("--matrix-rounds", type=_positive_int, default=3)
    parser.add_argument("--matrix-items", type=_non_negative_int, default=0)
    parser.add_argument(
        "--matrix-kinds",
        type=_matrix_values,
        default=tuple(_MATRIX_WORKERS),
    )
    parser.add_argument(
        "--look-ids",
        type=_look_values,
        default=_LOOK_IDS,
    )
    parser.add_argument("--request-concurrency", type=_positive_int, default=32)
    parser.add_argument("--display-width", type=_positive_int, default=1280)
    parser.add_argument("--display-height", type=_positive_int, default=720)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--code-revision",
        default="",
        help="40-character revision for a git-archive checkout without .git metadata",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser


def _base_request(
    args: argparse.Namespace, sources: Sequence[SourceFile]
) -> dict[str, Any]:
    return {
        "sources": tuple(sources),
        "groups": args.groups,
        "request_concurrency": args.request_concurrency,
        "display_width": args.display_width,
        "display_height": args.display_height,
    }


def _run_requested_benchmark(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.source_root is None:
        raise ValueError("--source-root is required unless --self-test is used")
    if args.output is None:
        raise ValueError("--output is required unless --self-test is used")
    if not isinstance(args.timeout_seconds, (int, float)) or args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if args.code_revision and (
        len(args.code_revision) != 40
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in args.code_revision
        )
    ):
        raise ValueError("--code-revision must be a 40-character hexadecimal commit")

    source_root = args.source_root.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    temporary_parent = args.temporary_parent.expanduser().resolve()
    if _is_within(output, source_root):
        raise ValueError("benchmark output must not be written under the source root")
    if _is_within(temporary_parent, source_root):
        raise ValueError("temporary parent must not be under the source root")
    temporary_parent.mkdir(parents=True, exist_ok=True)

    sources_before = _collect_sources(source_root)
    sample_manifest = (
        _load_sample_manifest(args.sample_manifest, sources_before)
        if args.sample_manifest is not None
        else None
    )
    input_report = _source_report(source_root, sources_before, sample_manifest)
    runtime_compatibility = _runtime_compatibility_report()
    scenarios: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(
        prefix="zvec-raw-selection-benchmark-",
        dir=temporary_parent,
    ) as run_temporary:
        run_root = Path(run_temporary).resolve()
        base = _base_request(args, sources_before)
        if "cold24" in args.scenarios:
            scenarios["cold_first_24"] = _run_isolated(
                {
                    **base,
                    "name": "cold24",
                    "action": "cold",
                    "target_count": 24,
                    "project_all": False,
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        if "cold100" in args.scenarios:
            scenarios["cold_first_100"] = _run_isolated(
                {
                    **base,
                    "name": "cold100",
                    "action": "cold",
                    "target_count": 100,
                    "project_all": False,
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        if "project" in args.scenarios:
            scenarios["cold_project_all"] = _run_isolated(
                {
                    **base,
                    "name": "project",
                    "action": "cold",
                    "target_count": len(sources_before),
                    "project_all": True,
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        if "hot24" in args.scenarios:
            scenarios["restart_hot_first_24"] = _run_isolated(
                {
                    **base,
                    "name": "hot24",
                    "action": "hot_restart",
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        if "preview" in args.scenarios:
            scenarios["preview"] = _run_isolated(
                {
                    **base,
                    "name": "preview",
                    "action": "preview",
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        if "matrix" in args.scenarios:
            matrix: dict[str, Any] = {}
            for kind in args.matrix_kinds:
                worker_results: dict[str, Any] = {}
                for workers in _MATRIX_WORKERS[kind]:
                    worker_results[str(workers)] = _run_isolated(
                        {
                            **base,
                            "name": f"matrix-{kind}-{workers}",
                            "action": "matrix",
                            "kind": kind,
                            "workers": workers,
                            "rounds": args.matrix_rounds,
                            "maximum_items": args.matrix_items,
                        },
                        run_root=run_root,
                        timeout_seconds=args.timeout_seconds,
                    )
                matrix[kind] = {
                    "fixed_workers": list(_MATRIX_WORKERS[kind]),
                    "results": worker_results,
                }
            scenarios["decode_matrix"] = matrix
        if "looks" in args.scenarios:
            scenarios["creative_looks"] = _run_isolated(
                {
                    **base,
                    "name": "looks",
                    "action": "looks",
                    "groups": args.look_groups,
                    "look_ids": args.look_ids,
                },
                run_root=run_root,
                timeout_seconds=args.timeout_seconds,
            )
        owned_run_before_cleanup = _tree_stats(run_root)
    owned_run_cleanup_complete = not run_root.exists()

    sources_after = _collect_sources(source_root)
    before_digest = _source_digest(sources_before)
    after_digest = _source_digest(sources_after)
    source_unchanged = before_digest == after_digest
    failed_scenarios = _collect_failed_scenarios(scenarios)
    formal_eligible = (
        source_unchanged
        and bool(input_report["formal_input_coverage"])
        and runtime_compatibility["matches_target_closure"] is True
        and set(args.scenarios) == set(_ALL_SCENARIOS)
        and args.groups >= 20
        and args.look_groups >= 20
    )
    formal_status = (
        "fail"
        if formal_eligible and failed_scenarios
        else "pass"
        if formal_eligible
        else "not_formal"
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "run": {
            "label": str(args.label),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": args.code_revision.casefold() or _git_revision(),
            "revision_source": "cli" if args.code_revision else "git",
            "tool_sha256": _tool_digest(),
            "python": sys.version,
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
            "pillow_version": _package_version("Pillow"),
            "rawpy_version": _package_version("rawpy"),
            "numpy_version": _package_version("numpy"),
            "runtime_compatibility": runtime_compatibility,
            "scenarios": list(args.scenarios),
            "temporary_parent": str(temporary_parent),
            "owned_run_before_cleanup": owned_run_before_cleanup,
            "owned_run_cleanup_complete": owned_run_cleanup_complete,
        },
        "input": input_report,
        "source_after": {
            "source_count": len(sources_after),
            "source_order_and_version_digest": after_digest,
            "unchanged": source_unchanged,
        },
        "measurement_scope": {
            "cold": (
                "fresh derived cache and SQLite per group; Windows file-system cache "
                "is not flushed"
            ),
            "resource_cpu": (
                "100 percent equals one fully occupied logical core; peak sampled "
                "every 100 ms"
            ),
            "resource_rss": (
                "benchmark child process working set; child helper processes would "
                "require separate aggregation"
            ),
            "process_io": (
                "Windows GetProcessIoCounters; includes file operations and is not "
                "physical-disk throughput"
            ),
            "frontend": (
                "service/decode benchmark only; HTTP, DOM commit, paint and native "
                "dialog time are not included"
            ),
            "privacy": (
                "full source paths remain local in this JSON; no source bytes or "
                "image names are printed as samples"
            ),
        },
        "scenarios": scenarios,
        "formal_run": {
            "status": formal_status,
            "failed_scenarios": failed_scenarios,
            "input_coverage": bool(input_report["formal_input_coverage"]),
            "runtime_matches_target_closure": runtime_compatibility[
                "matches_target_closure"
            ],
            "source_unchanged": source_unchanged,
            "note": (
                "hard gates with status=fail and operational scenarios with "
                "status=failed are failures and produce a non-zero exit code; "
                "missing input coverage, target-runtime mismatch or source changes "
                "prevent a formal pass"
            ),
        },
    }
    _atomic_write_json(output, report)
    exit_code = _benchmark_exit_code(failed_scenarios, source_unchanged)
    return report, exit_code


def _collect_failed_scenarios(value: Any, prefix: str = "") -> list[str]:
    failed: list[str] = []
    if isinstance(value, Mapping):
        if value.get("status") in {"fail", "failed"}:
            failed.append(prefix or "root")
        for key, child in value.items():
            if key in {"error_details", "resources"}:
                continue
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            failed.extend(_collect_failed_scenarios(child, child_prefix))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            failed.extend(_collect_failed_scenarios(child, f"{prefix}[{index}]"))
    return failed


def _benchmark_exit_code(
    failed_scenarios: Sequence[str], source_unchanged: bool
) -> int:
    return int(bool(failed_scenarios) or not source_unchanged)


def _build_self_test_manifest_fixture(
    root: Path,
) -> tuple[tuple[SourceFile, ...], dict[str, Any]]:
    sources: list[SourceFile] = []
    samples: list[dict[str, str]] = []
    variants = sorted(_REQUIRED_A7M4_RAW_VARIANTS)
    for index in range(300):
        raw_mode, size_category = variants[index % len(variants)]
        relative_name = f"raw/sample-{index:03d}.ARW"
        sources.append(
            SourceFile(
                path=root / relative_name,
                relative_name=relative_name,
                extension=".arw",
                file_size=64_000_000 + index,
                mtime_ns=1_700_000_000_000_000_000 + index,
                file_identity=f"self-arw-{index}",
            )
        )
        samples.append(
            {
                "relative_name": relative_name,
                "camera_model": _A7M4_CAMERA_MODEL,
                "raw_mode": raw_mode,
                "size_category": size_category,
            }
        )
    for index in range(699):
        relative_name = f"jpg/sample-{index:03d}.jpg"
        sources.append(
            SourceFile(
                path=root / relative_name,
                relative_name=relative_name,
                extension=".jpg",
                file_size=4_000_000 + index,
                mtime_ns=1_700_000_001_000_000_000 + index,
                file_identity=f"self-jpg-{index}",
            )
        )
    sources.append(
        SourceFile(
            path=root / "png/alpha.png",
            relative_name="png/alpha.png",
            extension=".png",
            file_size=2_000_000,
            mtime_ns=1_700_000_002_000_000_000,
            file_identity="self-png-0",
        )
    )
    ordered = tuple(
        sorted(
            sources,
            key=lambda item: (item.relative_name.casefold(), item.relative_name),
        )
    )
    document: dict[str, Any] = {
        "schema_version": _SAMPLE_MANIFEST_SCHEMA_VERSION,
        "source_order_and_version_digest": _source_digest(ordered),
        "arw_samples": samples,
    }
    return ordered, document


def _run_self_test(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from PIL import Image

    temporary_parent = args.temporary_parent.expanduser().resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="zvec-raw-selection-benchmark-self-test-",
        dir=temporary_parent,
    ) as temporary:
        root = Path(temporary)
        source_root = root / "sources"
        runtime_root = root / "runtime"
        source_root.mkdir()
        runtime_root.mkdir()
        for index in range(24):
            path = source_root / f"sample-{index:03d}.jpg"
            Image.new(
                "RGB",
                (96 + index, 64 + index),
                (index * 7 % 255, 80, 140),
            ).save(path, quality=90)
        for index in range(4):
            path = source_root / f"alpha-{index:03d}.png"
            Image.new("RGBA", (80, 60), (20, 100, 180, 160)).save(path)
        (source_root / "._ignored.jpg").write_bytes(b"resource fork metadata")

        before = _collect_sources(source_root)
        base = {
            "sources": before,
            "groups": 1,
            "request_concurrency": 4,
            "display_width": 320,
            "display_height": 240,
        }
        cold = _run_isolated(
            {
                **base,
                "name": "self-cold",
                "action": "cold",
                "target_count": 4,
                "project_all": False,
            },
            run_root=runtime_root,
            timeout_seconds=120.0,
        )
        hot = _run_isolated(
            {
                **base,
                "name": "self-hot",
                "action": "hot_restart",
            },
            run_root=runtime_root,
            timeout_seconds=120.0,
        )
        jpg_matrix = _run_isolated(
            {
                **base,
                "name": "self-jpg-matrix",
                "action": "matrix",
                "kind": "jpg_thumbnail",
                "workers": 2,
                "rounds": 1,
                "maximum_items": 6,
            },
            run_root=runtime_root,
            timeout_seconds=120.0,
        )
        missing_arw = _run_isolated(
            {
                **base,
                "name": "self-missing-arw",
                "action": "matrix",
                "kind": "arw_full",
                "workers": 2,
                "rounds": 1,
                "maximum_items": 0,
            },
            run_root=runtime_root,
            timeout_seconds=120.0,
        )
        after = _collect_sources(source_root)
        manifest_sources, manifest_document = _build_self_test_manifest_fixture(root)
        manifest_path = root / "sample-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_document, ensure_ascii=False), encoding="utf-8"
        )
        manifest_report = _load_sample_manifest(manifest_path, manifest_sources)
        formal_input_report = _source_report(
            root,
            manifest_sources,
            manifest_report,
        )
        incomplete_document = {
            **manifest_document,
            "arw_samples": [
                {
                    **sample,
                    "size_category": "M",
                }
                if sample["raw_mode"] == "lossless_compressed"
                and sample["size_category"] == "S"
                else dict(sample)
                for sample in manifest_document["arw_samples"]
            ],
        }
        incomplete_manifest_path = root / "incomplete-sample-manifest.json"
        incomplete_manifest_path.write_text(
            json.dumps(incomplete_document, ensure_ascii=False), encoding="utf-8"
        )
        incomplete_manifest_report = _load_sample_manifest(
            incomplete_manifest_path, manifest_sources
        )
        missing_manifest_input_report = _source_report(root, before, None)
        runtime_compatibility = _runtime_compatibility_report()
        hard_gate_failures = _collect_failed_scenarios(
            {
                "scenario": {
                    "gate": {"status": "fail"},
                    "passing_gate": {"status": "pass"},
                }
            }
        )
        checks = {
            "cold_measured": cold.get("status") == "measured",
            "hot_measured": hot.get("status") == "measured",
            "matrix_measured": jpg_matrix.get("status") == "measured",
            "missing_arw_not_formal": missing_arw.get("status") == "not_formal",
            "resource_fork_ignored": len(before) == 28,
            "source_unchanged": _source_digest(before) == _source_digest(after),
            "runtime_has_no_residue": _tree_stats(runtime_root)["files"] == 0,
            "hard_gate_failure_collected": hard_gate_failures == ["scenario.gate"],
            "hard_gate_failure_is_nonzero": _benchmark_exit_code(
                hard_gate_failures, True
            )
            == 1,
            "passing_gates_exit_zero": _benchmark_exit_code([], True) == 0,
            "source_change_exits_nonzero": _benchmark_exit_code([], False) == 1,
            "valid_manifest_is_formal": manifest_report["formal_manifest_coverage"]
            is True
            and formal_input_report["formal_input_coverage"] is True,
            "incomplete_manifest_is_not_formal": incomplete_manifest_report[
                "formal_manifest_coverage"
            ]
            is False
            and bool(
                incomplete_manifest_report["missing_required_raw_mode_and_size_pairs"]
            ),
            "missing_manifest_is_not_formal": missing_manifest_input_report[
                "formal_input_coverage"
            ]
            is False
            and missing_manifest_input_report["a7m4_sample_manifest"]["status"]
            == "not_provided",
            "runtime_target_reported": runtime_compatibility["target_python"] == "3.12"
            and runtime_compatibility["matches_target_closure"]
            == (
                runtime_compatibility["python_matches_target"]
                and runtime_compatibility["dependencies_match_lock"]
            ),
        }
        report = {
            "self_test": checks,
            "passed": all(checks.values()),
            "cold": cold,
            "hot": hot,
            "jpg_matrix": jpg_matrix,
            "missing_arw": missing_arw,
            "manifest": manifest_report,
            "incomplete_manifest": incomplete_manifest_report,
            "runtime_compatibility": runtime_compatibility,
        }
        if args.output is not None:
            _atomic_write_json(args.output, report)
        return report, int(not all(checks.values()))


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report, exit_code = (
            _run_self_test(args) if args.self_test else _run_requested_benchmark(args)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(_safe_message(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
