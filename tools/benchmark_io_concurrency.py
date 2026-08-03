"""Repeatable, isolated scan and Windows export-copy concurrency benchmark.

The public ``scan`` and ``export`` commands never open a user workspace, task
store, recommendation database, or configured export directory.  Every
measured worker runs in a fresh spawned process and writes only below one
newly-created temporary benchmark directory.

Examples (formal runs should point the temporary parent at the intended disk)::

    python tools/benchmark_io_concurrency.py scan --root D:\\readonly-images \
        --temporary-parent D:\\zvec-benchmark-temp
    python tools/benchmark_io_concurrency.py export --root D:\\readonly-images \
        --temporary-parent E:\\zvec-benchmark-temp --file-limit 300

The JSON report contains hashes and aggregate sizes, never source/output paths
or file names.  Windows process I/O counters are logical process I/O and are
not claimed to be physical-disk throughput after filesystem caching.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import multiprocessing
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Final, Literal, TypeVar, cast

from PIL import Image

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import image_vector_service.image_scanner as scanner_module  # noqa: E402
import zvec_webview.native_bridge as native_bridge_module  # noqa: E402
from image_vector_service.models import ScanResult  # noqa: E402
from image_vector_service.recommendations import (  # noqa: E402
    RecommendationCandidate,
    select_recommendations,
)
from image_vector_service.scan_staging import (  # noqa: E402
    StagedImageRecord,
    discover_scan_staging,
)
from zvec_webview.image_registry import (  # noqa: E402
    ImageRegistry,
    ImageRegistryError,
)
from zvec_webview.native_bridge import NativeBridge  # noqa: E402
from zvec_webview.server import GatewayServer  # noqa: E402

_SCAN_WORKERS: Final = (4, 6, 8, 12)
_EXPORT_WORKERS: Final = (1, 2, 4)
_RESOURCE_SAMPLE_SECONDS: Final = 0.1
_CHILD_TIMEOUT_SECONDS: Final = 60 * 60
_MAX_EXPORT_FILES: Final = 10_000
_TERMINAL_EXPORT_STATES: Final = frozenset({"succeeded", "partial", "failed"})
_IMAGE_SUFFIXES: Final = frozenset(scanner_module.SUPPORTED_EXTENSIONS)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class LatencySummary:
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    discovered_count: int
    supported_count: int
    discovered_bytes: int
    supported_bytes: int
    ordered_path_digest: str
    version_digest: str
    extension_counts: dict[str, int]
    discovery_error_count: int


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
        handle, ctypes.byref(memory), ctypes.sizeof(memory)
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
            name="zvec-io-benchmark-resources",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> ResourceUsage:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        finished_wall, finished_cpu, finished_snapshot = self._sample_once()
        wall_seconds = max(finished_wall - self._started_wall, 1e-9)
        cpu_seconds = max(finished_cpu - self._started_cpu, 0.0)
        started = self._started_snapshot
        mib = 1024 * 1024
        return ResourceUsage(
            wall_ms=round(wall_seconds * 1_000, 3),
            process_cpu_seconds=round(cpu_seconds, 6),
            average_cpu_core_percent=round(cpu_seconds / wall_seconds * 100, 3),
            peak_cpu_core_percent=round(self._peak_cpu, 3),
            rss_start_mib=(
                round(started.rss_bytes / mib, 3) if started is not None else None
            ),
            peak_rss_mib=(
                round(self._peak_rss / mib, 3)
                if finished_snapshot is not None
                else None
            ),
            process_read_operations=_counter_delta(
                started, finished_snapshot, "read_operations"
            ),
            process_write_operations=_counter_delta(
                started, finished_snapshot, "write_operations"
            ),
            process_other_operations=_counter_delta(
                started, finished_snapshot, "other_operations"
            ),
            process_read_bytes=_counter_delta(started, finished_snapshot, "read_bytes"),
            process_write_bytes=_counter_delta(
                started, finished_snapshot, "write_bytes"
            ),
            process_other_bytes=_counter_delta(
                started, finished_snapshot, "other_bytes"
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
                max(current_cpu - self._last_cpu, 0.0) / elapsed * 100,
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


def _percentile(samples: Sequence[float], percentile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _summary(samples: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        samples=len(samples),
        p50_ms=(round(statistics.median(samples), 3) if samples else None),
        p95_ms=(round(cast(float, _percentile(samples, 0.95)), 3) if samples else None),
        minimum_ms=round(min(samples), 3) if samples else None,
        maximum_ms=round(max(samples), 3) if samples else None,
    )


def _digest_lines(lines: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\n")
    return digest.hexdigest()


def _source_paths(root: Path) -> tuple[tuple[Path, ...], ScanResult]:
    result = ScanResult()
    iterator = scanner_module._iter_files(  # noqa: SLF001 - benchmark parity
        root,
        True,
        result,
        excluded_roots=(),
        failure_handler=None,
    )
    return tuple(iterator), result


def _snapshot_sources(root: Path) -> tuple[SourceSnapshot, tuple[Path, ...]]:
    paths, discovery = _source_paths(root)
    path_lines: list[str] = []
    version_lines: list[str] = []
    supported: list[Path] = []
    discovered_bytes = 0
    supported_bytes = 0
    extension_counts: dict[str, int] = defaultdict(int)
    resolved_root = root.resolve(strict=True)
    for path in paths:
        try:
            stat = path.stat()
            relative = path.resolve(strict=True).relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            continue
        discovered_bytes += max(int(stat.st_size), 0)
        path_lines.append(relative)
        version_lines.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
        suffix = path.suffix.casefold()
        extension_counts[suffix or "<none>"] += 1
        if suffix in _IMAGE_SUFFIXES:
            supported.append(path)
            supported_bytes += max(int(stat.st_size), 0)
    return (
        SourceSnapshot(
            discovered_count=len(paths),
            supported_count=len(supported),
            discovered_bytes=discovered_bytes,
            supported_bytes=supported_bytes,
            ordered_path_digest=_digest_lines(path_lines),
            version_digest=_digest_lines(version_lines),
            extension_counts=dict(sorted(extension_counts.items())),
            discovery_error_count=discovery.failure_count,
        ),
        tuple(supported),
    )


def _same_snapshot(before: SourceSnapshot, after: SourceSnapshot) -> bool:
    return before == after


class _BenchmarkFacade:
    """Minimal, isolated gateway surface used only for contention probes."""

    def __init__(self, registry: ImageRegistry) -> None:
        self.image_registry = registry

    def bootstrap(self) -> dict[str, object]:
        return {
            "service": {"status": "ready", "backend_ready": True},
            "libraries": [],
            "models": {},
            "organize": {"proposals": [], "aliases": []},
        }

    def settings(self) -> dict[str, object]:
        return {"credentials": {"configured": False}}

    def latest_results(self, *, page: int, page_size: int) -> dict[str, object]:
        return {"page": page, "page_size": page_size, "items": []}


def _control_candidates() -> tuple[RecommendationCandidate, ...]:
    dimension = 128
    candidates: list[RecommendationCandidate] = []
    for index in range(300):
        vector = tuple(
            math.sin((index + 1) * (axis + 1) * 0.001) for axis in range(dimension)
        )
        candidates.append(
            RecommendationCandidate(
                candidate_id=f"control:{index:04d}",
                doc_id=f"control-{index:04d}",
                sha256=f"control-sha-{index:04d}",
                width=2_000 + index % 31,
                height=1_400 + index % 29,
                mtime_ns=300 - index,
                exposure_count=index % 4,
                album_id=f"album-{index // 4:03d}",
                character=f"character-{index % 64:02d}",
                vector=vector,
                personalization_score=((index % 9) - 4) / 100,
            )
        )
    return tuple(candidates)


def _http_latency(url: str) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            response.read()
            valid = 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        valid = False
    return (time.perf_counter() - started) * 1_000, valid


class _ContentionHarness:
    def __init__(
        self,
        sources: Sequence[Path],
        *,
        rounds: int,
        cache_directory: Path,
    ) -> None:
        self._rounds = rounds
        self._workload_started = threading.Event()
        self._workload_finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._result: dict[str, Any] | None = None
        self._error: BaseException | None = None
        self._registry: ImageRegistry | None = None
        self._gateway: GatewayServer | None = None
        self._urls: tuple[str, str, str] | None = None
        self._candidates: tuple[RecommendationCandidate, ...] = ()
        if rounds <= 0 or not sources:
            return
        cache_directory.mkdir(parents=True, exist_ok=False)
        registry = ImageRegistry(
            cache_directory=cache_directory,
            max_render_workers=2,
            max_cache_bytes=64 * 1024 * 1024,
        )
        first = registry.register(sources[0])
        second = registry.register(sources[min(1, len(sources) - 1)])
        gateway = GatewayServer(_BenchmarkFacade(registry))
        gateway.start()
        urls = (
            f"{gateway.url}api/image/{first.image_id}?variant=thumbnail",
            f"{gateway.url}api/image/{second.image_id}?variant=preview",
            gateway.url + "api/bootstrap",
        )
        for url in urls:
            _latency, valid = _http_latency(url)
            if not valid:
                gateway.stop()
                registry.close()
                raise RuntimeError("isolated contention gateway warmup failed")
        self._registry = registry
        self._gateway = gateway
        self._urls = urls
        self._candidates = _control_candidates()

    @property
    def enabled(self) -> bool:
        return self._urls is not None

    def mark_workload_started(self) -> None:
        self._workload_started.set()

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="zvec-io-benchmark-contention",
            daemon=True,
        )
        self._thread.start()

    def finish(self) -> dict[str, Any]:
        self._workload_finished.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=300)
            if thread.is_alive():
                raise RuntimeError(
                    "contention probes did not finish within five minutes"
                )
        try:
            if self._error is not None:
                raise RuntimeError(
                    f"contention probes failed: {type(self._error).__name__}"
                ) from self._error
            return self._result or _empty_contention_result(self._rounds)
        finally:
            if self._gateway is not None:
                self._gateway.stop()
            if self._registry is not None:
                self._registry.close()

    def _run(self) -> None:
        try:
            if not self._workload_started.wait(timeout=60):
                raise TimeoutError("workload did not reach its start barrier")
            assert self._urls is not None
            thumbnail: list[float] = []
            preview: list[float] = []
            bootstrap: list[float] = []
            recommendation: list[float] = []
            errors = 0
            overlap_rounds = 0
            for _round in range(self._rounds):
                overlapped = not self._workload_finished.is_set()
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = tuple(
                        executor.submit(_http_latency, url) for url in self._urls
                    )
                    started = time.perf_counter()
                    selection = select_recommendations(
                        self._candidates,
                        rng_seed=20_260_803,
                    )
                    recommendation.append((time.perf_counter() - started) * 1_000)
                    responses = tuple(future.result(timeout=60) for future in futures)
                if selection.status != "complete" or len(selection.items) != 15:
                    raise RuntimeError("pure recommendation control was not complete")
                for target, (latency, valid) in zip(
                    (thumbnail, preview, bootstrap), responses, strict=True
                ):
                    target.append(latency)
                    errors += int(not valid)
                overlap_rounds += int(overlapped)
            self._result = {
                "configured_rounds": self._rounds,
                "overlap_rounds": overlap_rounds,
                "search_thumbnail": asdict(_summary(thumbnail)),
                "preview": asdict(_summary(preview)),
                "bootstrap": asdict(_summary(bootstrap)),
                "pure_recommendation_selection": asdict(_summary(recommendation)),
                "errors": errors,
                "cache_state": "isolated_temporary_prewarmed",
                "recommendation_scope": (
                    "fixed-seed pure local selection over 300 synthetic 128D "
                    "candidates; no create/shown, model, task store, or "
                    "recommendation DB"
                ),
            }
        except BaseException as exc:
            self._error = exc


def _empty_contention_result(rounds: int) -> dict[str, Any]:
    empty = asdict(_summary(()))
    return {
        "configured_rounds": rounds,
        "overlap_rounds": 0,
        "search_thumbnail": empty,
        "preview": empty,
        "bootstrap": empty,
        "pure_recommendation_selection": empty,
        "errors": 0,
        "cache_state": "disabled",
        "recommendation_scope": (
            "fixed-seed pure local selection is available when contention rounds > 0"
        ),
    }


_VOLUME_FACT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$candidate = [System.IO.Path]::GetFullPath($env:ZVEC_BENCHMARK_VOLUME_PATH)
$root = [System.IO.Path]::GetPathRoot($candidate)
$drive = $root.TrimEnd('\').TrimEnd(':')
if ($drive.Length -ne 1) { throw 'not a drive-letter volume' }
$partition = Get-Partition -DriveLetter $drive
$disk = Get-Disk -Number $partition.DiskNumber
$physical = Get-PhysicalDisk | Where-Object FriendlyName -eq $disk.FriendlyName |
    Select-Object -First 1
[pscustomobject]@{
    identified = $true
    disk_number = [int]$disk.Number
    friendly_name = [string]$disk.FriendlyName
    media_type = if ($null -ne $physical) {
        [string]$physical.MediaType
    } else {
        'Unspecified'
    }
    bus_type = [string]$disk.BusType
    size_bytes = [int64]$disk.Size
    health_status = if ($null -ne $physical) {
        [string]$physical.HealthStatus
    } else {
        'Unknown'
    }
} | ConvertTo-Json -Compress
"""


def _volume_facts(path: Path) -> dict[str, Any]:
    if os.name != "nt":
        return {
            "identified": False,
            "platform": platform.system(),
            "reason": "Windows disk inventory is unavailable on this platform",
        }
    environment = os.environ.copy()
    environment["ZVEC_BENCHMARK_VOLUME_PATH"] = str(path.resolve(strict=True))
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _VOLUME_FACT_SCRIPT,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=environment,
        )
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass
    return {
        "identified": False,
        "platform": platform.system(),
        "reason": "disk inventory query failed; no SSD/HDD inference was made",
    }


class _BenchmarkCancellation(RuntimeError):
    pass


def _staged_record_digests(
    records: Sequence[StagedImageRecord],
) -> tuple[str, str]:
    path_ordered = sorted(
        records,
        key=lambda item: (item.root_id, item.relative_path, item.doc_id),
    )
    sequence_ordered = sorted(records, key=lambda item: item.sequence)

    def line(item: StagedImageRecord) -> str:
        return (
            f"{item.sequence}\0{item.relative_path}\0{item.sha256}\0"
            f"{item.width}\0{item.height}\0{item.size_bytes}\0{item.mtime_ns}"
        )

    return _digest_lines(map(line, path_ordered)), _digest_lines(
        map(line, sequence_ordered)
    )


def _failure_digest(failures: Sequence[Any]) -> str:
    return _digest_lines(
        sorted(
            f"{getattr(item, 'kind', '')}\0{getattr(item, 'stage', '')}\0"
            f"{type(getattr(item, 'error', '')).__name__}"
            for item in failures
        )
    )


def _scan_normal_child(
    root: Path,
    workers: int,
    work_directory: Path,
    contention_rounds: int,
) -> dict[str, Any]:
    work_directory.mkdir(parents=True, exist_ok=False)
    before, sources = _snapshot_sources(root)
    if not sources:
        raise RuntimeError("scan benchmark requires at least one supported image")
    contention = _ContentionHarness(
        sources,
        rounds=contention_rounds,
        cache_directory=work_directory / "contention-cache",
    )
    original_inspect = scanner_module.inspect_image
    timings_ms: list[float] = []
    timings_lock = threading.Lock()
    active = 0
    peak_active = 0
    first_started = threading.Event()

    def timed_inspect(path: Path, scan_root: Path, root_id: str) -> Any:
        nonlocal active, peak_active
        with timings_lock:
            active += 1
            peak_active = max(peak_active, active)
        if not first_started.is_set():
            first_started.set()
            contention.mark_workload_started()
        started = time.perf_counter()
        try:
            return original_inspect(path, scan_root, root_id)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1_000
            with timings_lock:
                timings_ms.append(elapsed_ms)
                active -= 1

    scanner_module.inspect_image = timed_inspect
    sampler = _ProcessResourceSampler()
    result: Any = None
    contention_result: dict[str, Any]
    sampler.start()
    contention.start()
    started = time.perf_counter()
    try:
        result = scanner_module.scan_folder_to_staging(
            root,
            "benchmark-root",
            staging_directory=work_directory / "staging",
            max_workers=workers,
        )
        workload_ms = (time.perf_counter() - started) * 1_000
    finally:
        if not first_started.is_set():
            contention.mark_workload_started()
        contention_result = contention.finish()
        resources = sampler.stop()
        scanner_module.inspect_image = original_inspect
    if result is None:
        raise RuntimeError("scan benchmark produced no staged result")
    records = [
        record
        for batch in result.iter_staged_records_by_path(batch_size=500)
        for record in batch
    ]
    path_digest, sequence_digest = _staged_record_digests(records)
    failure_digest = _failure_digest(result.failures)
    record_count = result.record_count
    staging_directory = work_directory / "staging"
    result.discard()
    staging_residual = len(discover_scan_staging(staging_directory))
    thread_residual = sum(
        thread.name.startswith("zvec-image-scan") for thread in threading.enumerate()
    )
    after, _after_sources = _snapshot_sources(root)
    seconds = max(workload_ms / 1_000, 1e-9)
    return {
        "workers": workers,
        "workload_ms": round(workload_ms, 3),
        "images_per_second": round(result.supported / seconds, 3),
        "input_mib_per_second": round(
            before.supported_bytes / (1024 * 1024) / seconds, 3
        ),
        "inspect_latency": asdict(_summary(timings_ms)),
        "scanned": result.scanned,
        "supported": result.supported,
        "skipped": result.skipped,
        "record_count": record_count,
        "failure_count": result.failure_count,
        "failure_fact_digest": failure_digest,
        "peak_active_inspections": peak_active,
        "peak_in_flight": result.peak_in_flight,
        "path_order_result_digest": path_digest,
        "sequence_order_result_digest": sequence_digest,
        "input": asdict(before),
        "source_unchanged": _same_snapshot(before, after),
        "source_after_version_digest": after.version_digest,
        "staging_residual_count": staging_residual,
        "scanner_thread_residual_count": thread_residual,
        "resources": asdict(resources),
        "contention": contention_result,
    }


def _scan_cancel_child(
    root: Path,
    workers: int,
    work_directory: Path,
    scenario: Literal["mid_scan", "final_drain"],
) -> dict[str, Any]:
    work_directory.mkdir(parents=True, exist_ok=False)
    before, sources = _snapshot_sources(root)
    supported_count = len(sources)
    if supported_count < 1:
        raise RuntimeError("scan cancellation benchmark needs one supported image")
    original_inspect = scanner_module.inspect_image
    gate_target = (
        min(workers, supported_count) if scenario == "mid_scan" else supported_count
    )
    gate_reached = threading.Event()
    release_inspection = threading.Event()
    cancel_requested = threading.Event()
    finished = threading.Event()
    counter_lock = threading.Lock()
    started_count = 0
    completed_count = 0
    result_holder: list[bool] = []
    exception_holder: list[BaseException] = []
    residual_before_holder: list[int] = []
    residual_after_holder: list[int] = []
    request_time = 0.0

    def controlled_inspect(path: Path, scan_root: Path, root_id: str) -> Any:
        nonlocal started_count, completed_count
        with counter_lock:
            started_count += 1
            ordinal = started_count
            if ordinal >= gate_target:
                gate_reached.set()
        should_wait = (scenario == "mid_scan" and ordinal <= gate_target) or (
            scenario == "final_drain" and ordinal == gate_target
        )
        if should_wait and not release_inspection.wait(timeout=300):
            raise TimeoutError("cancellation benchmark did not release inspection")
        try:
            return original_inspect(path, scan_root, root_id)
        finally:
            with counter_lock:
                completed_count += 1

    def cancel_check() -> None:
        if cancel_requested.is_set():
            raise _BenchmarkCancellation("benchmark cancellation requested")

    def run_scan() -> None:
        try:
            result = scanner_module.scan_folder_to_staging(
                root,
                "benchmark-root",
                staging_directory=work_directory / "staging",
                cancel_check=cancel_check,
                max_workers=workers,
            )
            residual_before_holder.append(
                len(discover_scan_staging(work_directory / "staging"))
            )
            result.discard()
            residual_after_holder.append(
                len(discover_scan_staging(work_directory / "staging"))
            )
            result_holder.append(True)
        except BaseException as exc:
            exception_holder.append(exc)
        finally:
            finished.set()

    scanner_module.inspect_image = controlled_inspect
    scan_thread = threading.Thread(
        target=run_scan,
        name=f"zvec-scan-cancel-{scenario}",
        daemon=True,
    )
    sampler = _ProcessResourceSampler()
    sampler.start()
    scan_thread.start()
    try:
        if not gate_reached.wait(timeout=300):
            raise TimeoutError("scan did not reach cancellation synchronization gate")
        request_time = time.perf_counter()
        cancel_requested.set()
        release_inspection.set()
        if not finished.wait(timeout=_CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("scan cancellation did not finish within one hour")
        scan_thread.join(timeout=5)
        if scan_thread.is_alive():
            raise RuntimeError("scan cancellation owner thread did not exit")
        cancellation_ms = (time.perf_counter() - request_time) * 1_000
    finally:
        release_inspection.set()
        scanner_module.inspect_image = original_inspect
        resources = sampler.stop()
    staging_directory = work_directory / "staging"
    residual_before_cleanup = (
        residual_before_holder[0]
        if residual_before_holder
        else len(discover_scan_staging(staging_directory))
    )
    residual_after_cleanup = (
        residual_after_holder[0]
        if residual_after_holder
        else len(discover_scan_staging(staging_directory))
    )
    scanner_threads = sum(
        thread.name.startswith("zvec-image-scan") for thread in threading.enumerate()
    )
    after, _after_sources = _snapshot_sources(root)
    exception = exception_holder[0] if exception_holder else None
    return {
        "workers": workers,
        "scenario": scenario,
        "requested": True,
        "scanner_observed_cancellation": isinstance(exception, _BenchmarkCancellation),
        "returned_normally_after_request": bool(result_holder),
        "exception_type": type(exception).__name__ if exception is not None else None,
        "latency_ms": round(cancellation_ms, 3),
        "gate_target": gate_target,
        "started_inspections": started_count,
        "completed_inspections": completed_count,
        "staging_residual_before_explicit_cleanup": residual_before_cleanup,
        "staging_residual_after_explicit_cleanup": residual_after_cleanup,
        "scanner_thread_residual_count": scanner_threads,
        "source_unchanged": _same_snapshot(before, after),
        "source_before_version_digest": before.version_digest,
        "source_after_version_digest": after.version_digest,
        "resources": asdict(resources),
        "scope": (
            "direct scan cancellation; production service performs another "
            "cancel_check immediately after scan returns"
        ),
    }


class _FolderWindow:
    def __init__(self, selected: Path | None) -> None:
        self._selected = selected

    def create_file_dialog(self, _dialog_type: object, **_kwargs: object) -> object:
        return () if self._selected is None else (str(self._selected),)


def _fake_webview() -> ModuleType:
    module = ModuleType("webview")
    module.FileDialog = SimpleNamespace(FOLDER="folder", OPEN="open")  # type: ignore[attr-defined]
    return module


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_digest(paths: Sequence[Path]) -> str:
    return _digest_lines(
        f"{index}\0{path.stat().st_size}\0{_file_sha256(path)}"
        for index, path in enumerate(paths)
    )


def _source_subset_summary(root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    path_lines: list[str] = []
    version_lines: list[str] = []
    total_bytes = 0
    for path in paths:
        stat = path.stat()
        relative = path.resolve(strict=True).relative_to(resolved_root).as_posix()
        total_bytes += max(int(stat.st_size), 0)
        path_lines.append(relative)
        version_lines.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
    return {
        "count": len(paths),
        "total_bytes": total_bytes,
        "ordered_path_digest": _digest_lines(path_lines),
        "version_digest": _digest_lines(version_lines),
        "ordered_content_digest": _content_digest(paths),
    }


def _copy_scan_snapshot(
    sources: Sequence[Path],
    snapshot_root: Path,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    snapshot_root.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    intended_paths: list[Path] = []
    for index, source in enumerate(sources):
        target = snapshot_root / f"{index:08d}{source.suffix.casefold()}"
        shutil.copy2(source, target)
        intended_paths.append(target)
    copy_ms = (time.perf_counter() - started) * 1_000
    snapshot_inventory, scanner_order = _snapshot_sources(snapshot_root)
    intended_summary = _source_subset_summary(snapshot_root, intended_paths)
    scanner_order_summary = _source_subset_summary(snapshot_root, scanner_order)
    return (
        {
            "copy_ms_excluded_from_worker_timings": round(copy_ms, 3),
            "copied_count": len(intended_paths),
            "copied_bytes": sum(path.stat().st_size for path in intended_paths),
            "inventory": asdict(snapshot_inventory),
            "intended_order": intended_summary,
            "scanner_order": scanner_order_summary,
            "scanner_order_matches_intended_content_order": (
                scanner_order_summary["ordered_content_digest"]
                == intended_summary["ordered_content_digest"]
            ),
        },
        scanner_order,
    )


def _output_summary(directory: Path) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    manifest_count = 0
    total_bytes = 0
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        if path.name.startswith("yaolens-export-errors-") and path.suffix == ".json":
            manifest_count += 1
            continue
        name_digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
        size = path.stat().st_size
        total_bytes += max(size, 0)
        entries[name_digest] = {
            "size_bytes": size,
            "content_sha256": _file_sha256(path),
        }
    digest = _digest_lines(
        f"{name_digest}\0{entry['size_bytes']}\0{entry['content_sha256']}"
        for name_digest, entry in sorted(entries.items())
    )
    return {
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "relative_name_content_digest": digest,
        "entries_by_relative_name_sha256": entries,
        "error_manifest_count": manifest_count,
    }


def _error_fact_digest(errors: Sequence[Mapping[str, str]]) -> str:
    return _digest_lines(
        f"{hashlib.sha256(str(item.get('name', '')).encode()).hexdigest()}\0"
        f"{bool(item.get('reason'))}"
        for item in errors
    )


def _read_native_errors(directory: Path) -> list[dict[str, str]]:
    manifests = tuple(directory.glob("yaolens-export-errors-*.json"))
    if not manifests:
        return []
    if len(manifests) != 1:
        raise RuntimeError(
            "native export wrote an unexpected number of error manifests"
        )
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        raise RuntimeError("native export error manifest is malformed")
    return [
        {"name": str(item.get("name") or ""), "reason": str(item.get("reason") or "")}
        for item in errors
        if isinstance(item, dict)
    ]


def _progress_contract(
    progress: Sequence[tuple[int, int, int]],
    *,
    processed: int,
    exported: int,
    skipped: int,
) -> tuple[bool, bool]:
    monotonic = all(
        current[0] >= previous[0]
        and current[1] >= previous[1]
        and current[2] >= previous[2]
        and current[1] + current[2] == current[0]
        for previous, current in zip(progress, progress[1:], strict=False)
    ) and all(item[1] + item[2] == item[0] for item in progress)
    final_matches = bool(progress) and progress[-1] == (processed, exported, skipped)
    return monotonic, final_matches


def _register_export_sources(
    root: Path,
    *,
    file_limit: int,
    registry: ImageRegistry,
) -> tuple[tuple[Path, ...], tuple[str, ...], int]:
    _snapshot, candidates = _snapshot_sources(root)
    selected_paths: list[Path] = []
    selected_ids: list[str] = []
    rejected = 0
    for source in candidates:
        try:
            metadata = registry.register(source)
        except (OSError, ImageRegistryError):
            rejected += 1
            continue
        selected_paths.append(source)
        selected_ids.append(metadata.image_id)
        if len(selected_paths) >= file_limit:
            break
    if not selected_paths:
        raise RuntimeError("export benchmark found no registrable image")
    return tuple(selected_paths), tuple(selected_ids), rejected


def _install_fake_webview() -> ModuleType | None:
    previous = sys.modules.get("webview")
    sys.modules["webview"] = _fake_webview()
    return previous


def _restore_webview(previous: ModuleType | None) -> None:
    if previous is None:
        sys.modules.pop("webview", None)
    else:
        sys.modules["webview"] = previous


def _run_native_export(
    registry: ImageRegistry,
    image_ids: Sequence[str],
    destination: Path,
    *,
    contention: _ContentionHarness | None,
) -> dict[str, Any]:
    bridge = NativeBridge(registry)
    bridge.attach_window(_FolderWindow(destination))
    previous_webview = _install_fake_webview()
    original_copy = native_bridge_module.shutil.copy2
    timing_lock = threading.Lock()
    timings_ms: list[float] = []
    active = 0
    peak_active = 0
    first_copy = threading.Event()

    def timed_copy(source: Path, target: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak_active
        with timing_lock:
            active += 1
            peak_active = max(peak_active, active)
        if not first_copy.is_set():
            first_copy.set()
            if contention is not None:
                contention.mark_workload_started()
        started = time.perf_counter()
        try:
            return original_copy(source, target, *args, **kwargs)
        finally:
            with timing_lock:
                timings_ms.append((time.perf_counter() - started) * 1_000)
                active -= 1

    native_bridge_module.shutil.copy2 = timed_copy
    progress: list[tuple[int, int, int]] = []
    started = time.perf_counter()
    try:
        response = bridge.export_images(list(image_ids))
        if not response.get("ok") or response.get("cancelled"):
            raise RuntimeError("native export did not start")
        job = response.get("job")
        if not isinstance(job, dict) or not job.get("id"):
            raise RuntimeError("native export returned no job")
        job_id = str(job["id"])
        while True:
            status = bridge.export_status(job_id)
            current = status.get("job") if status.get("ok") else None
            if not isinstance(current, dict):
                raise RuntimeError("native export status became unavailable")
            point = (
                int(current.get("processed") or 0),
                int(current.get("exported") or 0),
                int(current.get("skipped") or 0),
            )
            if not progress or progress[-1] != point:
                progress.append(point)
            if str(current.get("status")) in _TERMINAL_EXPORT_STATES:
                final = current
                break
            threading.Event().wait(0.01)
        workload_ms = (time.perf_counter() - started) * 1_000
    finally:
        if contention is not None and not first_copy.is_set():
            contention.mark_workload_started()
        native_bridge_module.shutil.copy2 = original_copy
        _restore_webview(previous_webview)
    errors = _read_native_errors(destination)
    processed = int(final.get("processed") or 0)
    exported = int(final.get("exported") or 0)
    skipped = int(final.get("skipped") or 0)
    progress_monotonic, progress_final_matches = _progress_contract(
        progress,
        processed=processed,
        exported=exported,
        skipped=skipped,
    )
    return {
        "implementation": "production_native_bridge_oracle",
        "workload_ms": round(workload_ms, 3),
        "copy_latency": asdict(_summary(timings_ms)),
        "peak_active_copies": peak_active,
        "status": str(final.get("status")),
        "total": int(final.get("total") or 0),
        "processed": processed,
        "exported": exported,
        "skipped": skipped,
        "progress_digest": _digest_lines(
            "\0".join(map(str, item)) for item in progress
        ),
        "progress_sample_count": len(progress),
        "progress_monotonic": progress_monotonic,
        "progress_final_matches": progress_final_matches,
        "error_count": len(errors),
        "error_fact_digest": _error_fact_digest(errors),
    }


@dataclass(frozen=True, slots=True)
class _CandidateOutcome:
    index: int
    exported: bool
    name: str
    error: str = ""


def _run_candidate_export(
    registry: ImageRegistry,
    image_ids: Sequence[str],
    destination: Path,
    *,
    workers: int,
    contention: _ContentionHarness | None,
) -> dict[str, Any]:
    normalized = native_bridge_module._normalize_image_ids(list(image_ids))  # noqa: SLF001
    groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    outcomes: dict[int, _CandidateOutcome] = {}
    for index, image_id in enumerate(normalized):
        try:
            source = registry.resolve(image_id)
        except Exception as exc:
            outcomes[index] = _CandidateOutcome(
                index,
                False,
                "未知图片",
                str(exc) or type(exc).__name__,
            )
            continue
        groups[os.path.normcase(source.name)].append((index, source))

    timing_lock = threading.Lock()
    outcome_lock = threading.Lock()
    timings_ms: list[float] = []
    active = 0
    peak_active = 0
    first_copy = threading.Event()

    def copy_group(items: Sequence[tuple[int, Path]]) -> None:
        nonlocal active, peak_active
        for index, source in items:
            target: Path | None = None
            try:
                target = native_bridge_module._unique_export_path(  # noqa: SLF001
                    destination, source.name
                )
                with timing_lock:
                    active += 1
                    peak_active = max(peak_active, active)
                if not first_copy.is_set():
                    first_copy.set()
                    if contention is not None:
                        contention.mark_workload_started()
                started = time.perf_counter()
                try:
                    shutil.copy2(source, target)
                finally:
                    with timing_lock:
                        timings_ms.append((time.perf_counter() - started) * 1_000)
                        active -= 1
            except Exception as exc:
                outcome = _CandidateOutcome(
                    index,
                    False,
                    source.name,
                    str(exc) or type(exc).__name__,
                )
            else:
                outcome = _CandidateOutcome(index, True, source.name)
            with outcome_lock:
                outcomes[index] = outcome

    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="zvec-export-candidate",
    ) as executor:
        futures = tuple(
            executor.submit(copy_group, tuple(items)) for items in groups.values()
        )
        for future in futures:
            future.result(timeout=_CHILD_TIMEOUT_SECONDS)
    if contention is not None and not first_copy.is_set():
        contention.mark_workload_started()
    workload_ms = (time.perf_counter() - started) * 1_000
    ordered = [outcomes[index] for index in range(len(normalized))]
    errors: list[dict[str, str]] = []
    progress: list[tuple[int, int, int]] = []
    exported = 0
    skipped = 0
    for processed, outcome in enumerate(ordered, start=1):
        if outcome.exported:
            exported += 1
        else:
            skipped += 1
            if len(errors) < 1_000:
                errors.append({"name": outcome.name, "reason": outcome.error})
        progress.append((processed, exported, skipped))
    if errors:
        manifest = destination / "yaolens-export-errors-benchmark.json"
        manifest.write_text(
            json.dumps(
                {"schema_version": 1, "job_id": "benchmark", "errors": errors},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    progress_monotonic, progress_final_matches = _progress_contract(
        progress,
        processed=len(normalized),
        exported=exported,
        skipped=skipped,
    )
    return {
        "implementation": "tool_only_bounded_candidate",
        "workload_ms": round(workload_ms, 3),
        "copy_latency": asdict(_summary(timings_ms)),
        "peak_active_copies": peak_active,
        "status": "succeeded" if skipped == 0 else "partial",
        "total": len(normalized),
        "processed": len(normalized),
        "exported": exported,
        "skipped": skipped,
        "progress_digest": _digest_lines(
            "\0".join(map(str, item)) for item in progress
        ),
        "progress_sample_count": len(progress),
        "progress_monotonic": progress_monotonic,
        "progress_final_matches": progress_final_matches,
        "error_count": len(errors),
        "error_fact_digest": _error_fact_digest(errors),
        "candidate_scope": (
            "tool-only static-source candidate: resolves the frozen registry snapshot "
            "before bounded copy, serializes same-basename groups, and commits facts "
            "in input order; it is not a production implementation"
        ),
    }


def _folder_cancel_check(
    registry: ImageRegistry, image_id: str, directory: Path
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=False)
    bridge = NativeBridge(registry)
    bridge.attach_window(_FolderWindow(None))
    previous_webview = _install_fake_webview()
    started = time.perf_counter()
    try:
        response = bridge.export_images([image_id])
    finally:
        _restore_webview(previous_webview)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    return {
        "supported": True,
        "latency_ms": round(elapsed_ms, 3),
        "cancelled": response == {"ok": True, "action": "export", "cancelled": True},
        "job_created": "job" in response,
        "output_entry_count": sum(1 for _item in directory.iterdir()),
        "scope": "folder selector cancellation before an export job exists",
    }


def _semantic_export_fixture(
    sources: Sequence[Path],
    *,
    workers: int,
    work_directory: Path,
) -> dict[str, Any]:
    fixture_root = work_directory / "semantic-sources"
    first_parent = fixture_root / "first"
    second_parent = fixture_root / "second"
    first_parent.mkdir(parents=True)
    second_parent.mkdir(parents=True)
    suffix = sources[0].suffix.casefold() or ".jpg"
    first = first_parent / f"same{suffix}"
    second = second_parent / f"same{suffix}"
    missing = fixture_root / f"missing{suffix}"
    shutil.copy2(sources[0], first)
    shutil.copy2(sources[min(1, len(sources) - 1)], second)
    shutil.copy2(sources[0], missing)
    output = work_directory / "semantic-output"
    output.mkdir()
    (output / first.name).write_bytes(b"zvec-benchmark-preexisting")
    registry = ImageRegistry(max_entries=8)
    try:
        first_id = registry.register(first).image_id
        second_id = registry.register(second).image_id
        missing_id = registry.register(missing).image_id
        missing.unlink()
        image_ids = (first_id, first_id, missing_id, second_id)
        if workers == 1:
            facts = _run_native_export(
                registry,
                image_ids,
                output,
                contention=None,
            )
        else:
            facts = _run_candidate_export(
                registry,
                image_ids,
                output,
                workers=workers,
                contention=None,
            )
        summary = _output_summary(output)
    finally:
        registry.close()
    return {
        "facts": facts,
        "output": summary,
        "covers": [
            "duplicate_image_id",
            "same_basename_sources",
            "preexisting_target_conflict",
            "missing_registered_source",
            "partial_error_manifest",
        ],
    }


def _export_child(
    root: Path,
    workers: int,
    file_limit: int,
    work_directory: Path,
    contention_rounds: int,
) -> dict[str, Any]:
    work_directory.mkdir(parents=True, exist_ok=False)
    registry = ImageRegistry(max_entries=max(file_limit + 8, 16))
    try:
        before, _all_supported = _snapshot_sources(root)
        sources, image_ids, rejected = _register_export_sources(
            root,
            file_limit=file_limit,
            registry=registry,
        )
        content_before = _content_digest(sources)
        semantic = _semantic_export_fixture(
            sources,
            workers=workers,
            work_directory=work_directory,
        )
        folder_cancel = _folder_cancel_check(
            registry,
            image_ids[0],
            work_directory / "folder-cancel-output",
        )
        destination = work_directory / "measured-output"
        destination.mkdir()
        preexisting = destination / sources[0].name
        preexisting.write_bytes(b"zvec-benchmark-preexisting")
        contention = _ContentionHarness(
            sources,
            rounds=contention_rounds,
            cache_directory=work_directory / "contention-cache",
        )
        sampler = _ProcessResourceSampler()
        sampler.start()
        contention.start()
        try:
            if workers == 1:
                facts = _run_native_export(
                    registry,
                    image_ids,
                    destination,
                    contention=contention,
                )
            else:
                facts = _run_candidate_export(
                    registry,
                    image_ids,
                    destination,
                    workers=workers,
                    contention=contention,
                )
        finally:
            contention_result = contention.finish()
            resources = sampler.stop()
        output = _output_summary(destination)
        content_after = _content_digest(sources)
        after, _after_supported = _snapshot_sources(root)
    finally:
        registry.close()
    workload_seconds = max(float(facts["workload_ms"]) / 1_000, 1e-9)
    selected_bytes = sum(path.stat().st_size for path in sources)
    export_threads = sum(
        thread.name.startswith(("zvec-export-", "zvec-export-candidate"))
        for thread in threading.enumerate()
    )
    return {
        "workers": workers,
        "implementation": facts["implementation"],
        "selected_source_count": len(sources),
        "selected_source_bytes": selected_bytes,
        "input_id_count": len(image_ids),
        "registry_rejected_count": rejected,
        "files_per_second": round(int(facts["exported"]) / workload_seconds, 3),
        "input_mib_per_second": round(
            selected_bytes / (1024 * 1024) / workload_seconds,
            3,
        ),
        "facts": facts,
        "output": output,
        "semantic_fixture": semantic,
        "folder_selector_cancel": folder_cancel,
        "in_flight_cancel": {
            "supported": False,
            "scope": (
                "NativeBridge exposes no running-export cancel API; no synthetic "
                "benchmark cancellation is reported as production behavior"
            ),
        },
        "source_content_digest_before": content_before,
        "source_content_digest_after": content_after,
        "source_content_unchanged": content_before == content_after,
        "source_version_digest_before": before.version_digest,
        "source_version_digest_after": after.version_digest,
        "source_version_unchanged": _same_snapshot(before, after),
        "export_thread_residual_count": export_threads,
        "resources": asdict(resources),
        "contention": contention_result,
    }


def _child_entry(connection: Any, mode: str, arguments: tuple[Any, ...]) -> None:
    try:
        if mode == "scan":
            result = _scan_normal_child(*arguments)
        elif mode == "scan_cancel":
            result = _scan_cancel_child(*arguments)
        elif mode == "export":
            result = _export_child(*arguments)
        else:
            raise ValueError("unknown benchmark child mode")
        connection.send({"ok": True, "result": result})
    except BaseException as exc:
        connection.send({"ok": False, "error_type": type(exc).__name__})
    finally:
        connection.close()


def _spawn_child(
    mode: str,
    arguments: tuple[Any, ...],
    *,
    process_name: str,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_entry,
        args=(child_connection, mode, arguments),
        name=process_name,
    )
    process.start()
    child_connection.close()
    message: Mapping[str, Any] = {}
    try:
        if not parent_connection.poll(_CHILD_TIMEOUT_SECONDS):
            process.terminate()
            process.join(timeout=10)
            raise RuntimeError("benchmark child did not finish within one hour")
        received = parent_connection.recv()
        if isinstance(received, Mapping):
            message = received
    finally:
        parent_connection.close()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)
        raise RuntimeError("benchmark child did not exit cleanly")
    if process.exitcode != 0 or not message.get("ok"):
        error_type = str(message.get("error_type") or f"exit_{process.exitcode}")
        raise RuntimeError(f"benchmark child failed: {error_type}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("benchmark child returned an invalid result")
    return cast(dict[str, Any], result)


def _execution_orders(base: Sequence[int], trials: int) -> tuple[tuple[int, ...], ...]:
    output: list[tuple[int, ...]] = []
    normalized = tuple(base)
    for trial in range(trials):
        rotation = trial % len(normalized)
        rotated = normalized[rotation:] + normalized[:rotation]
        if trial % 2:
            rotated = tuple(reversed(rotated))
        output.append(rotated)
    return tuple(output)


def _validate_worker_order(
    values: Sequence[int], expected: Sequence[int]
) -> tuple[int, ...]:
    normalized = tuple(values)
    if len(normalized) != len(expected) or set(normalized) != set(expected):
        raise ValueError(f"worker order must contain exactly {tuple(expected)}")
    return normalized


def _validate_temporary_parent(source_root: Path, value: Path | None) -> Path:
    parent = (
        value.expanduser().resolve(strict=True)
        if value is not None
        else Path(tempfile.gettempdir()).resolve(strict=True)
    )
    if not parent.is_dir():
        raise NotADirectoryError("temporary parent is not a directory")
    resolved_source = source_root.resolve(strict=True)
    if parent == resolved_source or resolved_source in parent.parents:
        raise ValueError(
            "temporary parent must not be inside the read-only source root"
        )
    return parent


def _scan_equivalence_key(result: Mapping[str, Any]) -> tuple[Any, ...]:
    input_payload = cast(Mapping[str, Any], result["input"])
    return (
        result.get("scanned"),
        result.get("supported"),
        result.get("skipped"),
        result.get("record_count"),
        result.get("failure_count"),
        result.get("failure_fact_digest"),
        result.get("path_order_result_digest"),
        result.get("sequence_order_result_digest"),
        input_payload.get("ordered_path_digest"),
        input_payload.get("version_digest"),
    )


def _export_equivalence_key(result: Mapping[str, Any]) -> tuple[Any, ...]:
    facts = cast(Mapping[str, Any], result["facts"])
    output = cast(Mapping[str, Any], result["output"])
    semantic = cast(Mapping[str, Any], result["semantic_fixture"])
    semantic_facts = cast(Mapping[str, Any], semantic["facts"])
    semantic_output = cast(Mapping[str, Any], semantic["output"])
    return (
        facts.get("status"),
        facts.get("total"),
        facts.get("processed"),
        facts.get("exported"),
        facts.get("skipped"),
        facts.get("error_count"),
        facts.get("error_fact_digest"),
        facts.get("progress_monotonic"),
        facts.get("progress_final_matches"),
        output.get("entry_count"),
        output.get("total_bytes"),
        output.get("relative_name_content_digest"),
        output.get("error_manifest_count"),
        semantic_facts.get("status"),
        semantic_facts.get("total"),
        semantic_facts.get("processed"),
        semantic_facts.get("exported"),
        semantic_facts.get("skipped"),
        semantic_facts.get("error_count"),
        semantic_facts.get("error_fact_digest"),
        semantic_facts.get("progress_monotonic"),
        semantic_facts.get("progress_final_matches"),
        semantic_output.get("entry_count"),
        semantic_output.get("total_bytes"),
        semantic_output.get("relative_name_content_digest"),
        semantic_output.get("error_manifest_count"),
    )


def _worker_rollup(results: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    by_worker: dict[int, list[float]] = defaultdict(list)
    for result in results:
        by_worker[int(result["workers"])].append(float(result[metric]))
    return {
        str(worker): asdict(_summary(samples))
        for worker, samples in sorted(by_worker.items())
    }


def _nested_worker_rollup(
    results: Sequence[Mapping[str, Any]],
    container: str,
    metric: str,
) -> dict[str, Any]:
    by_worker: dict[int, list[float]] = defaultdict(list)
    for result in results:
        nested = cast(Mapping[str, Any], result[container])
        by_worker[int(result["workers"])].append(float(nested[metric]))
    return {
        str(worker): asdict(_summary(samples))
        for worker, samples in sorted(by_worker.items())
    }


def _run_scan_matrix(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("scan root is not a directory")
    temporary_parent = _validate_temporary_parent(root, args.temporary_parent)
    worker_order = _validate_worker_order(args.order, _SCAN_WORKERS)
    original_before, original_sources = _snapshot_sources(root)
    if len(original_sources) < args.file_limit:
        raise RuntimeError("scan root has fewer supported files than file-limit")
    selected_sources = original_sources[: args.file_limit]
    selected_before = _source_subset_summary(root, selected_sources)
    execution_orders = _execution_orders(worker_order, args.trials)
    normal_results: list[dict[str, Any]] = []
    cancellation_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(
        prefix="zvec-scan-concurrency-",
        dir=temporary_parent,
    ) as temporary:
        benchmark_root = Path(temporary).resolve(strict=True)
        isolated_root = benchmark_root / "isolated-input"
        snapshot_copy, snapshot_sources = _copy_scan_snapshot(
            selected_sources,
            isolated_root,
        )
        if len(snapshot_sources) != args.file_limit:
            raise RuntimeError("isolated scan snapshot count does not match file-limit")
        snapshot_before = _source_subset_summary(isolated_root, snapshot_sources)
        for trial, order in enumerate(execution_orders):
            for workers in order:
                result = _spawn_child(
                    "scan",
                    (
                        isolated_root,
                        workers,
                        benchmark_root / f"trial-{trial}-workers-{workers}",
                        args.contention_rounds,
                    ),
                    process_name=f"zvec-scan-benchmark-{workers}",
                )
                result["trial"] = trial
                normal_results.append(result)
        if not args.skip_cancellation:
            for workers in _SCAN_WORKERS:
                for scenario in ("mid_scan", "final_drain"):
                    cancellation_results.append(
                        _spawn_child(
                            "scan_cancel",
                            (
                                isolated_root,
                                workers,
                                benchmark_root / f"cancel-{scenario}-{workers}",
                                scenario,
                            ),
                            process_name=f"zvec-scan-cancel-{scenario}-{workers}",
                        )
                    )
        snapshot_after_inventory, snapshot_after_sources = _snapshot_sources(
            isolated_root
        )
        snapshot_after = _source_subset_summary(
            isolated_root,
            snapshot_after_sources,
        )
        target_volume = _volume_facts(benchmark_root)
    original_after, _after_sources = _snapshot_sources(root)
    selected_after = _source_subset_summary(root, selected_sources)
    original_unchanged = (
        original_before == original_after and selected_before == selected_after
    )
    snapshot_unchanged = snapshot_before == snapshot_after and snapshot_copy[
        "inventory"
    ] == asdict(snapshot_after_inventory)
    content_order_preserved = selected_before[
        "ordered_content_digest"
    ] == snapshot_before["ordered_content_digest"] and bool(
        snapshot_copy["scanner_order_matches_intended_content_order"]
    )
    oracle = next(result for result in normal_results if result["workers"] == 4)
    oracle_key = _scan_equivalence_key(oracle)
    for result in normal_results:
        result["equivalent_to_workers_4"] = _scan_equivalence_key(result) == oracle_key
    all_equivalent = all(result["equivalent_to_workers_4"] for result in normal_results)
    all_overlap = all(
        int(cast(Mapping[str, Any], result["contention"])["overlap_rounds"])
        >= args.contention_rounds
        for result in normal_results
    )
    all_clean = all(
        bool(result["source_unchanged"])
        and int(result["staging_residual_count"]) == 0
        and int(result["scanner_thread_residual_count"]) == 0
        and int(cast(Mapping[str, Any], result["contention"])["errors"]) == 0
        for result in normal_results
    ) and all(
        bool(result["source_unchanged"])
        and int(result["staging_residual_after_explicit_cleanup"]) == 0
        and int(result["scanner_thread_residual_count"]) == 0
        for result in cancellation_results
    )
    formal = (
        args.trials >= 3
        and args.contention_rounds >= 20
        and args.file_limit >= 300
        and not args.skip_cancellation
        and original_unchanged
        and snapshot_unchanged
        and content_order_preserved
        and all_equivalent
        and all_overlap
        and all_clean
    )
    return {
        "benchmark": "scan_concurrency",
        "formal_worker_matrix": list(_SCAN_WORKERS),
        "execution_orders": [list(order) for order in execution_orders],
        "trials": args.trials,
        "file_limit": args.file_limit,
        "contention_rounds": args.contention_rounds,
        "formal_run": formal,
        "original_source_inventory_before": asdict(original_before),
        "original_source_inventory_after": asdict(original_after),
        "selected_original_before": selected_before,
        "selected_original_after": selected_after,
        "source_unchanged_across_matrix": original_unchanged,
        "isolated_snapshot": snapshot_copy,
        "isolated_snapshot_before": snapshot_before,
        "isolated_snapshot_after": snapshot_after,
        "isolated_snapshot_unchanged": snapshot_unchanged,
        "selected_content_order_preserved_in_snapshot": content_order_preserved,
        "source_volume": _volume_facts(root),
        "isolated_snapshot_and_staging_volume": target_volume,
        "temporary_parent_explicit": args.temporary_parent is not None,
        "workload_ms_by_worker": _worker_rollup(normal_results, "workload_ms"),
        "images_per_second_by_worker": _worker_rollup(
            normal_results, "images_per_second"
        ),
        "all_results_equivalent_to_workers_4": all_equivalent,
        "all_runs_clean": all_clean,
        "normal_runs": normal_results,
        "cancellation_runs": cancellation_results,
        "measurement_limits": {
            "process_io": (
                "Windows GetProcessIoCounters logical process I/O; it includes "
                "filesystem cache effects and is not physical-disk throughput"
            ),
            "cpu": (
                "time.process_time across all process threads; 100 percent equals "
                "one occupied logical core; peak sampled every 100 ms"
            ),
            "disk_cache": (
                "the one-time isolated snapshot copy and content summaries warm the "
                "Windows system file cache before measured workers; fresh children "
                "do not flush it, and rotated order reduces one-way bias but does "
                "not make this a strict cold-disk benchmark"
            ),
            "progress": (
                "scan staging counters are measured; the product currently emits "
                "only Scanning/Found stage messages, not per-image UI progress"
            ),
        },
    }


def _run_export_matrix(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("export source root is not a directory")
    temporary_parent = _validate_temporary_parent(root, args.temporary_parent)
    worker_order = _validate_worker_order(args.order, _EXPORT_WORKERS)
    before, _sources = _snapshot_sources(root)
    execution_orders = _execution_orders(worker_order, args.trials)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(
        prefix="zvec-export-concurrency-",
        dir=temporary_parent,
    ) as temporary:
        benchmark_root = Path(temporary).resolve(strict=True)
        for trial, order in enumerate(execution_orders):
            for workers in order:
                result = _spawn_child(
                    "export",
                    (
                        root,
                        workers,
                        args.file_limit,
                        benchmark_root / f"trial-{trial}-workers-{workers}",
                        args.contention_rounds,
                    ),
                    process_name=f"zvec-export-benchmark-{workers}",
                )
                result["trial"] = trial
                results.append(result)
        target_volume = _volume_facts(benchmark_root)
    after, _after_sources = _snapshot_sources(root)
    oracle = next(result for result in results if result["workers"] == 1)
    oracle_key = _export_equivalence_key(oracle)
    for result in results:
        result["equivalent_to_workers_1"] = (
            _export_equivalence_key(result) == oracle_key
        )
    all_equivalent = all(result["equivalent_to_workers_1"] for result in results)
    all_bounded = all(
        int(cast(Mapping[str, Any], result["facts"])["peak_active_copies"])
        <= int(result["workers"])
        for result in results
    )
    all_overlap = all(
        int(cast(Mapping[str, Any], result["contention"])["overlap_rounds"])
        >= args.contention_rounds
        for result in results
    )
    all_clean = all(
        bool(result["source_content_unchanged"])
        and bool(result["source_version_unchanged"])
        and int(result["export_thread_residual_count"]) == 0
        and bool(cast(Mapping[str, Any], result["facts"])["progress_monotonic"])
        and bool(cast(Mapping[str, Any], result["facts"])["progress_final_matches"])
        and bool(cast(Mapping[str, Any], result["folder_selector_cancel"])["cancelled"])
        and not bool(
            cast(Mapping[str, Any], result["folder_selector_cancel"])["job_created"]
        )
        and int(
            cast(Mapping[str, Any], result["folder_selector_cancel"])[
                "output_entry_count"
            ]
        )
        == 0
        and int(cast(Mapping[str, Any], result["contention"])["errors"]) == 0
        for result in results
    )
    selected_counts = {int(result["selected_source_count"]) for result in results}
    formal = (
        args.trials >= 3
        and args.contention_rounds >= 20
        and before == after
        and all_equivalent
        and all_bounded
        and all_overlap
        and all_clean
        and selected_counts == {args.file_limit}
    )
    return {
        "benchmark": "windows_native_export_copy_concurrency",
        "formal_worker_matrix": list(_EXPORT_WORKERS),
        "execution_orders": [list(order) for order in execution_orders],
        "trials": args.trials,
        "file_limit": args.file_limit,
        "contention_rounds": args.contention_rounds,
        "formal_run": formal,
        "source_inventory": asdict(before),
        "source_unchanged_across_matrix": before == after,
        "source_after_version_digest": after.version_digest,
        "source_volume": _volume_facts(root),
        "temporary_output_volume": target_volume,
        "temporary_parent_explicit": args.temporary_parent is not None,
        "workload_ms_by_worker": _nested_worker_rollup(results, "facts", "workload_ms"),
        "files_per_second_by_worker": _worker_rollup(results, "files_per_second"),
        "all_results_equivalent_to_workers_1": all_equivalent,
        "all_candidates_strictly_bounded": all_bounded,
        "all_runs_clean": all_clean,
        "runs": results,
        "measurement_limits": {
            "worker_1": (
                "calls the production NativeBridge export API and polls its real job"
            ),
            "workers_2_4": (
                "tool-only bounded candidates over a frozen source snapshot; passing "
                "this benchmark is necessary but does not authorize production code"
            ),
            "copy_files": (
                "NativeBridge.copy_files is CF_HDROP clipboard metadata; actual "
                "Explorer paste is outside Zvec and is not benchmarked as disk copy"
            ),
            "in_flight_cancel": (
                "the production NativeBridge has no running-export cancel API; only "
                "folder-selector cancellation is measured"
            ),
            "process_io": (
                "Windows GetProcessIoCounters logical process I/O; it is not a "
                "physical-disk counter after filesystem caching"
            ),
            "source_validation": (
                "each child hashes the selected source content before and after "
                "copying; this proves source stability but also warms system cache, "
                "so results describe the observed cached/current-device workload"
            ),
        },
    }


def _run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="zvec-io-concurrency-smoke-") as temporary:
        root = Path(temporary)
        source = root / "source"
        temporary_parent = root / "benchmark-temp"
        for group in ("a", "b"):
            (source / group).mkdir(parents=True)
        temporary_parent.mkdir()
        for index in range(24):
            group = "a" if index % 2 == 0 else "b"
            name = f"shared-{index // 2:02d}.png"
            Image.new(
                "RGB",
                (64 + index % 3, 64 + index % 5),
                (
                    (index * 17) % 256,
                    (index * 31) % 256,
                    (index * 47) % 256,
                ),
            ).save(source / group / name)
        scan = _run_scan_matrix(
            argparse.Namespace(
                root=source,
                temporary_parent=temporary_parent,
                order=_SCAN_WORKERS,
                trials=1,
                contention_rounds=2,
                skip_cancellation=False,
                file_limit=24,
            )
        )
        export = _run_export_matrix(
            argparse.Namespace(
                root=source,
                temporary_parent=temporary_parent,
                order=_EXPORT_WORKERS,
                trials=1,
                contention_rounds=2,
                file_limit=12,
            )
        )
    passed = (
        not scan["formal_run"]
        and len(scan["normal_runs"]) == len(_SCAN_WORKERS)
        and len(scan["cancellation_runs"]) == len(_SCAN_WORKERS) * 2
        and scan["all_results_equivalent_to_workers_4"]
        and scan["all_runs_clean"]
        and scan["source_unchanged_across_matrix"]
        and not export["formal_run"]
        and len(export["runs"]) == len(_EXPORT_WORKERS)
        and export["all_results_equivalent_to_workers_1"]
        and export["all_candidates_strictly_bounded"]
        and export["all_runs_clean"]
        and export["source_unchanged_across_matrix"]
    )
    return {
        "benchmark": "io_concurrency_smoke",
        "passed": bool(passed),
        "formal_run": False,
        "scan": {
            "normal_runs": len(scan["normal_runs"]),
            "cancellation_runs": len(scan["cancellation_runs"]),
            "equivalent": scan["all_results_equivalent_to_workers_4"],
            "clean": scan["all_runs_clean"],
            "source_unchanged": scan["source_unchanged_across_matrix"],
        },
        "export": {
            "runs": len(export["runs"]),
            "equivalent": export["all_results_equivalent_to_workers_1"],
            "bounded": export["all_candidates_strictly_bounded"],
            "clean": export["all_runs_clean"],
            "source_unchanged": export["source_unchanged_across_matrix"],
        },
        "scope": (
            "24 generated 64px images, one trial, two contention rounds; this "
            "validates execution/contracts only and is never a formal performance run"
        ),
    }


def _add_common_arguments(
    parser: argparse.ArgumentParser, workers: Sequence[int]
) -> None:
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Read-only real image root. The path is never included in JSON output.",
    )
    parser.add_argument(
        "--temporary-parent",
        type=Path,
        help=(
            "Existing parent on the intended staging/output volume. A new owned "
            "temporary directory is created below it and removed after the run."
        ),
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=3,
        help="Rotated fresh-process trials per worker level (formal minimum: 3).",
    )
    parser.add_argument(
        "--contention-rounds",
        type=int,
        default=20,
        help="Isolated loopback/pure-selection probe rounds per measured run.",
    )
    parser.add_argument(
        "--order",
        nargs=len(workers),
        type=int,
        default=tuple(workers),
        metavar="WORKERS",
        help=f"First trial order; must contain exactly {tuple(workers)}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional UTF-8 JSON report path. Parent directories must already exist.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="Run the fixed 4/6/8/12 scan matrix.")
    _add_common_arguments(scan, _SCAN_WORKERS)
    scan.add_argument(
        "--skip-cancellation",
        action="store_true",
        help="Diagnostic only; omitting cancellation scenarios makes formal_run false.",
    )
    scan.add_argument(
        "--file-limit",
        type=int,
        default=300,
        help=(
            "Existing-order real sources copied once into the isolated input "
            "snapshot (1..10000; formal minimum: 300)."
        ),
    )
    export = subparsers.add_parser(
        "export",
        help="Run production worker=1 and tool-only bounded 2/4 export candidates.",
    )
    _add_common_arguments(export, _EXPORT_WORKERS)
    export.add_argument(
        "--file-limit",
        type=int,
        default=300,
        help="Ordered registrable source count (1..10000; formal run must reach it).",
    )
    subparsers.add_parser(
        "smoke",
        help="Run a tiny temporary contract smoke; never a formal benchmark.",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.command == "smoke":
        return
    if isinstance(args.trials, bool) or not 1 <= args.trials <= 100:
        raise ValueError("trials must be between 1 and 100")
    if (
        isinstance(args.contention_rounds, bool)
        or not 0 <= args.contention_rounds <= 1_000
    ):
        raise ValueError("contention-rounds must be between 0 and 1000")
    if args.command in {"scan", "export"} and (
        isinstance(args.file_limit, bool)
        or not 1 <= args.file_limit <= _MAX_EXPORT_FILES
    ):
        raise ValueError(f"file-limit must be between 1 and {_MAX_EXPORT_FILES}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_args(args)
        report = (
            _run_scan_matrix(args)
            if args.command == "scan"
            else _run_export_matrix(args)
            if args.command == "export"
            else _run_smoke()
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        output_argument = getattr(args, "output", None)
        if output_argument is not None:
            output = cast(Path, output_argument).expanduser().resolve()
            if not output.parent.is_dir():
                raise NotADirectoryError("output parent does not exist")
            output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 2 if args.command == "smoke" and not report["passed"] else 0
    except (OSError, RuntimeError, ValueError) as exc:
        # Do not echo exception text: filesystem exceptions can include private paths.
        print(
            f"I/O concurrency benchmark failed: {type(exc).__name__}", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
