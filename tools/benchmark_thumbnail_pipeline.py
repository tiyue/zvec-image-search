"""Benchmark the local HTTP thumbnail pipeline with real read-only images.

Every worker-count run uses its own temporary cache. Source images are opened
in place but never changed, and neither the production cache nor recommendation
database is accessed.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import multiprocessing
import os
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.request import urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import zvec_webview.image_registry as image_registry_module  # noqa: E402
from zvec_host.configuration_service import (  # noqa: E402
    DesktopConfigurationService,
)
from zvec_webview.image_registry import (  # noqa: E402
    ImageRegistry,
    ImageRegistryError,
)
from zvec_webview.server import GatewayServer  # noqa: E402

_IMAGE_SUFFIXES = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
_FORMAL_WORKER_COUNTS = (2, 3, 4, 6, 8, 12, 16)
_COMPARISON_ONLY_WORKER_COUNTS = (8, 12, 16)
_RESOURCE_SAMPLE_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class LatencySummary:
    samples: int
    p50_ms: float
    p95_ms: float
    minimum_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class WorkerBenchmark:
    max_render_workers: int
    cold_critical_six: LatencySummary
    cold_batches: LatencySummary
    cold_errors: int
    hot_critical_six: LatencySummary
    hot_batches: LatencySummary
    hot_errors: int
    contention_batch_critical_six: LatencySummary
    contention_batch_full: LatencySummary
    contention_search_thumbnail: LatencySummary
    contention_preview: LatencySummary
    contention_background: LatencySummary
    contention_errors: int
    http_request_errors: int
    decode_errors: int
    render_errors: int
    errors: int
    resources: ResourceUsage
    disk_cache_entries: int
    disk_cache_bytes: int


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


class _RenderErrorCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.decode_errors = 0
        self.render_errors = 0

    def render(self, path: Path, variant: Any, mtime_ns: int) -> Any:
        try:
            return self._real_render(path, variant, mtime_ns)
        except ImageRegistryError as exc:
            category = (
                "decode_errors"
                if isinstance(exc.__cause__, (OSError, ValueError))
                else "render_errors"
            )
            with self._lock:
                setattr(self, category, getattr(self, category) + 1)
            raise
        except BaseException:
            with self._lock:
                self.render_errors += 1
            raise

    _real_render = staticmethod(image_registry_module._render_image)


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
            name="zvec-thumbnail-benchmark-resources",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> ResourceUsage:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
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
                started_snapshot, finished_snapshot, "read_operations"
            ),
            process_write_operations=_counter_delta(
                started_snapshot, finished_snapshot, "write_operations"
            ),
            process_other_operations=_counter_delta(
                started_snapshot, finished_snapshot, "other_operations"
            ),
            process_read_bytes=_counter_delta(
                started_snapshot, finished_snapshot, "read_bytes"
            ),
            process_write_bytes=_counter_delta(
                started_snapshot, finished_snapshot, "write_bytes"
            ),
            process_other_bytes=_counter_delta(
                started_snapshot, finished_snapshot, "other_bytes"
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


class _BenchmarkFacade:
    """Small structural facade exposing only routes used by this benchmark."""

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


def _percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one latency sample is required")
    ordered = sorted(samples)
    rank = max(0, min(len(ordered) - 1, int(percentile * len(ordered) + 0.999) - 1))
    return ordered[rank]


def _summary(samples: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        samples=len(samples),
        p50_ms=round(statistics.median(samples), 3),
        p95_ms=round(_percentile(samples, 0.95), 3),
        minimum_ms=round(min(samples), 3),
        maximum_ms=round(max(samples), 3),
    )


def _configured_roots(config_path: Path | None) -> tuple[Path, ...]:
    snapshot = DesktopConfigurationService(config_path).load()
    if snapshot is None:
        raise RuntimeError("Zvec configuration is not initialized")
    roots = tuple(
        library.image_root
        for library in snapshot.configuration.libraries
        if library.enabled and library.image_root.is_dir()
    )
    if not roots:
        raise RuntimeError("no enabled image library is available")
    return roots


def _source_candidates(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(
                name for name in names if not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                source = Path(directory) / name
                if (
                    source.suffix.casefold() in _IMAGE_SUFFIXES
                    and not source.is_symlink()
                ):
                    yield source


def _collect_sources(roots: Sequence[Path], required: int) -> tuple[Path, ...]:
    sources: list[Path] = []
    probe = ImageRegistry(max_entries=1)
    try:
        for source in _source_candidates(roots):
            try:
                probe.register(source)
            except ImageRegistryError:
                continue
            sources.append(source)
            if len(sources) >= required:
                return tuple(sources)
    finally:
        probe.close()
    raise RuntimeError(
        f"benchmark requires {required} readable images, found {len(sources)}"
    )


def _source_digest(sources: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        stat_result = source.stat()
        digest.update(
            os.path.normcase(str(source)).encode("utf-8", errors="surrogatepass")
        )
        digest.update(f"\0{stat_result.st_size}\0{stat_result.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def _http_get(
    url: str,
    barrier: threading.Barrier,
    started_at: list[float],
) -> tuple[float, bool]:
    try:
        barrier.wait(timeout=30)
        with urlopen(url, timeout=120) as response:  # noqa: S310 - loopback URL only
            response.read()
            valid = response.status == 200
        return (time.perf_counter() - started_at[0]) * 1_000.0, valid
    except Exception:
        return 0.0, False


def _start_timer(started_at: list[float]) -> None:
    started_at[0] = time.perf_counter()


def _parallel_gets(urls: Sequence[str]) -> tuple[float, float, int]:
    started_at = [0.0]
    barrier = threading.Barrier(
        len(urls),
        action=lambda: _start_timer(started_at),
    )
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        results = tuple(
            executor.map(lambda url: _http_get(url, barrier, started_at), urls)
        )
    critical_count = min(6, len(results))
    critical_ms = max(latency for latency, _valid in results[:critical_count])
    full_ms = max(latency for latency, _valid in results)
    return critical_ms, full_ms, sum(not valid for _latency, valid in results)


def _contention_gets(
    batch_urls: Sequence[str],
    search_url: str,
    preview_url: str,
    background_url: str,
) -> tuple[tuple[float, float, float, float, float], int]:
    started_at = [0.0]
    barrier = threading.Barrier(
        len(batch_urls) + 3,
        action=lambda: _start_timer(started_at),
    )
    with ThreadPoolExecutor(max_workers=len(batch_urls) + 3) as executor:
        batch_futures = tuple(
            executor.submit(_http_get, url, barrier, started_at) for url in batch_urls
        )
        search = executor.submit(_http_get, search_url, barrier, started_at)
        preview = executor.submit(_http_get, preview_url, barrier, started_at)
        background = executor.submit(_http_get, background_url, barrier, started_at)
        results: tuple[tuple[float, bool], ...] = tuple(
            future.result(timeout=150)
            for future in (*batch_futures, search, preview, background)
        )
    errors = sum(not valid for _latency, valid in results)
    batch_results = results[: len(batch_urls)]
    critical_count = min(6, len(batch_results))
    critical_ms = max(latency for latency, _valid in batch_results[:critical_count])
    full_ms = max(latency for latency, _valid in batch_results)
    return (
        critical_ms,
        full_ms,
        search.result()[0],
        preview.result()[0],
        background.result()[0],
    ), errors


def _registered_urls(
    registry: ImageRegistry,
    base_url: str,
    sources: Sequence[Path],
    variant: str,
) -> tuple[str, ...]:
    return tuple(
        f"{base_url}api/image/{registry.register(source).image_id}?variant={variant}"
        for source in sources
    )


def run_worker_benchmark(
    sources: Sequence[Path],
    *,
    groups: int,
    batch_size: int,
    max_render_workers: int,
    cache_root: Path,
) -> WorkerBenchmark:
    batch_source_count = groups * batch_size
    batch_sources = sources[:batch_source_count]
    control_sources = sources[batch_source_count : batch_source_count + groups * 2]
    registry = ImageRegistry(
        cache_directory=cache_root,
        max_render_workers=max_render_workers,
        max_cache_bytes=256 * 1024 * 1024,
    )
    facade = _BenchmarkFacade(registry)
    gateway = GatewayServer(facade)
    gateway.start()
    render_error_counter = _RenderErrorCounter()
    resource_sampler = _ProcessResourceSampler()
    resource_sampler.start()
    cold_errors = 0
    hot_errors = 0
    contention_errors = 0
    cold_critical_samples: list[float] = []
    cold_full_samples: list[float] = []
    hot_critical_samples: list[float] = []
    hot_full_samples: list[float] = []
    contention_critical_samples: list[float] = []
    contention_full_samples: list[float] = []
    search_samples: list[float] = []
    preview_samples: list[float] = []
    background_samples: list[float] = []
    try:
        with patch.object(
            image_registry_module,
            "_render_image",
            render_error_counter.render,
        ):
            thumbnail_urls = _registered_urls(
                registry, gateway.url, batch_sources, "thumbnail"
            )
            for group in range(groups):
                start = group * batch_size
                critical_ms, full_ms, failures = _parallel_gets(
                    thumbnail_urls[start : start + batch_size]
                )
                cold_critical_samples.append(critical_ms)
                cold_full_samples.append(full_ms)
                cold_errors += failures
            for group in range(groups):
                start = group * batch_size
                critical_ms, full_ms, failures = _parallel_gets(
                    thumbnail_urls[start : start + batch_size]
                )
                hot_critical_samples.append(critical_ms)
                hot_full_samples.append(full_ms)
                hot_errors += failures

            # Keep the contention pass cold with a second isolated cache namespace.
            registry.close()
            registry = ImageRegistry(
                cache_directory=cache_root / "contention",
                max_render_workers=max_render_workers,
                max_cache_bytes=256 * 1024 * 1024,
            )
            facade.image_registry = registry
            thumbnail_urls = _registered_urls(
                registry, gateway.url, batch_sources, "thumbnail"
            )
            search_urls = _registered_urls(
                registry, gateway.url, control_sources[::2], "thumbnail"
            )
            preview_urls = _registered_urls(
                registry, gateway.url, control_sources[1::2], "preview"
            )
            for group in range(groups):
                start = group * batch_size
                latencies, failures = _contention_gets(
                    thumbnail_urls[start : start + batch_size],
                    search_urls[group],
                    preview_urls[group],
                    gateway.url + "api/bootstrap",
                )
                (
                    critical_latency,
                    full_latency,
                    search_latency,
                    preview_latency,
                    background_latency,
                ) = latencies
                contention_critical_samples.append(critical_latency)
                contention_full_samples.append(full_latency)
                search_samples.append(search_latency)
                preview_samples.append(preview_latency)
                background_samples.append(background_latency)
                contention_errors += failures
    finally:
        resources = resource_sampler.stop()
        gateway.stop()
        registry.close()
    disk_cache_entries, disk_cache_bytes = _disk_cache_stats(cache_root)
    errors = cold_errors + hot_errors + contention_errors
    return WorkerBenchmark(
        max_render_workers=max_render_workers,
        cold_critical_six=_summary(cold_critical_samples),
        cold_batches=_summary(cold_full_samples),
        cold_errors=cold_errors,
        hot_critical_six=_summary(hot_critical_samples),
        hot_batches=_summary(hot_full_samples),
        hot_errors=hot_errors,
        contention_batch_critical_six=_summary(contention_critical_samples),
        contention_batch_full=_summary(contention_full_samples),
        contention_search_thumbnail=_summary(search_samples),
        contention_preview=_summary(preview_samples),
        contention_background=_summary(background_samples),
        contention_errors=contention_errors,
        http_request_errors=errors,
        decode_errors=render_error_counter.decode_errors,
        render_errors=render_error_counter.render_errors,
        errors=errors,
        resources=resources,
        disk_cache_entries=disk_cache_entries,
        disk_cache_bytes=disk_cache_bytes,
    )


def _disk_cache_stats(cache_root: Path) -> tuple[int, int]:
    entries = 0
    total_bytes = 0
    for cache_file in cache_root.rglob("*.cache"):
        try:
            size = cache_file.stat().st_size
        except OSError:
            continue
        entries += 1
        total_bytes += max(size, 0)
    return entries, total_bytes


def _worker_process(
    connection: Any,
    sources: tuple[Path, ...],
    groups: int,
    batch_size: int,
    max_render_workers: int,
    cache_root: Path,
) -> None:
    try:
        result = run_worker_benchmark(
            sources,
            groups=groups,
            batch_size=batch_size,
            max_render_workers=max_render_workers,
            cache_root=cache_root,
        )
        connection.send({"ok": True, "result": asdict(result)})
    except BaseException as exc:
        connection.send(
            {
                "ok": False,
                "error": type(exc).__name__,
            }
        )
    finally:
        connection.close()


def _run_isolated_worker(
    sources: tuple[Path, ...],
    *,
    groups: int,
    batch_size: int,
    max_render_workers: int,
    cache_root: Path,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_process,
        args=(
            child_connection,
            sources,
            groups,
            batch_size,
            max_render_workers,
            cache_root,
        ),
        name=f"zvec-thumbnail-benchmark-{max_render_workers}",
    )
    process.start()
    child_connection.close()
    try:
        if not parent_connection.poll(30 * 60):
            process.terminate()
            process.join(timeout=5)
            raise RuntimeError(
                f"worker {max_render_workers} did not finish within 30 minutes"
            )
        message = parent_connection.recv()
    finally:
        parent_connection.close()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError(f"worker {max_render_workers} did not exit cleanly")
    if process.exitcode != 0 or not message.get("ok"):
        raise RuntimeError(
            f"worker {max_render_workers} failed: "
            f"{message.get('error', f'exit code {process.exitcode}')}"
        )
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"worker {max_render_workers} returned an invalid result")
    return result


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _worker_counts(value: str) -> tuple[int, ...]:
    counts = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if (
        not counts
        or len(set(counts)) != len(counts)
        or any(count not in _FORMAL_WORKER_COUNTS for count in counts)
    ):
        raise argparse.ArgumentTypeError(
            "workers must be unique comma-separated values from 2,3,4,6,8,12,16"
        )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--groups", type=_positive, default=20)
    parser.add_argument("--batch-size", type=_positive, default=15)
    parser.add_argument(
        "--workers",
        type=_worker_counts,
        default=_FORMAL_WORKER_COUNTS,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    required = args.groups * (args.batch_size + 2)
    sources = _collect_sources(_configured_roots(args.config), required)
    with tempfile.TemporaryDirectory(prefix="zvec-thumbnail-benchmark-") as temporary:
        cache_root = Path(temporary)
        results = tuple(
            _run_isolated_worker(
                sources,
                groups=args.groups,
                batch_size=args.batch_size,
                max_render_workers=workers,
                cache_root=cache_root / f"workers-{workers}",
            )
            for workers in args.workers
        )
    report: dict[str, Any] = {
        "groups": args.groups,
        "batch_size": args.batch_size,
        "source_count": len(sources),
        "source_order_digest": _source_digest(sources),
        "cache": "isolated-temporary",
        "transport": "loopback-http-full-body",
        "formal_worker_matrix": list(_FORMAL_WORKER_COUNTS),
        "comparison_only_workers": list(_COMPARISON_ONLY_WORKER_COUNTS),
        "formal_run": (
            args.groups == 20
            and args.batch_size == 15
            and args.workers == _FORMAL_WORKER_COUNTS
        ),
        "measurement_scope": {
            "critical_six": (
                "elapsed from simultaneous request release until fixed items 0..5 "
                "have each returned a full HTTP response body; a smaller diagnostic "
                "batch uses all its items"
            ),
            "full_batch": (
                "elapsed from simultaneous request release until every configured "
                "batch item has returned a full HTTP response body; the formal batch "
                "contains 15 items"
            ),
            "cpu": (
                "fresh sequential worker process per render limit, including loopback "
                "gateway, HTTP clients and sampler; 100 percent equals one fully "
                "occupied logical core; peak is sampled at 100 ms and is not "
                "whole-system CPU"
            ),
            "rss": (
                "fresh worker process working set sampled at 100 ms; native PIL "
                "allocations are included, parent and other Zvec processes are not"
            ),
            "process_io": (
                "Windows GetProcessIoCounters delta for the benchmark process; "
                "includes requested source/cache and loopback socket I/O and does "
                "not represent physical disk bytes after OS caching"
            ),
            "disk_cache": (
                "exact .cache entry count and payload bytes left in that worker's "
                "temporary cache before automatic cleanup"
            ),
            "errors": (
                "HTTP/request counts client-visible non-200 or transport failures; "
                "decode counts real render attempts whose ImageRegistryError wraps "
                "an OSError or ValueError; render counts other real render failures. "
                "A failed render can also produce one or more HTTP/request failures, "
                "so categories may overlap and total errors retains the client-visible "
                "HTTP/request count"
            ),
        },
        "results": list(results),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return int(any(int(result.get("errors", 0)) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
