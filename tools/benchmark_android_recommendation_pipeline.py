"""Orchestrate the bounded TC-007 Android recommendation matrix on Windows.

The real benchmark is deliberately opt-in.  Every selected representative
combination gets a fresh clone of one caller-supplied snapshot, an empty image
cache, one isolated backend, and one LAN listener bound to the Android device's
already-paired origin.  The tool never clears Android app data or Coil caches,
never mutates the source snapshot, and never prints an origin, credential,
filesystem path, device identifier, request/event identifier, or process id.

The Android instrumentation contract must accept ``tc007IdSeed`` and return an
``id_sequence_sha256`` field.  This lets the host prove that every combination
used the same deterministic request/shown/action sequence without disclosing
the identifiers themselves.  A real run also requires the source-level markers
for Coil 640px decode, 100ms resource sampling, prepared-batch consumption, and
an in-flight cancellation probe.  It fails closed before cloning data when any
part of that contract is absent; ``--dry-run`` remains useful while the APK is
prepared.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

RESULT_PREFIX: Final = "TC007_RECOMMENDATION_NETWORK_BENCHMARK="
RESULT_BUNDLE_KEY: Final = "tc007_network_benchmark_json"
RESULT_SCHEMA: Final = "tc007-android-network-v2"
HOST_SCHEMA: Final = "tc007-android-host-matrix-v1"
INSTRUMENTATION_CLASS: Final = (
    "com.zvec.lanviewer.data.network.RecommendationNetworkBenchmarkTest"
)
INSTRUMENTATION_RUNNER: Final = (
    "com.zvec.lanviewer.test/androidx.test.runner.AndroidJUnitRunner"
)
ID_ARGUMENT: Final = "tc007IdSeed"
ID_RESULT_FIELD: Final = "id_sequence_sha256"
DEFAULT_ID_SEED: Final = "tc007-paired-v1"
RESOURCE_SAMPLE_SECONDS: Final = 0.1
IMAGE_SUFFIXES: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)


@dataclass(frozen=True, slots=True)
class Combination:
    client_concurrency: int
    server_render_workers: int

    @property
    def selector(self) -> str:
        return f"{self.client_concurrency}x{self.server_render_workers}"


REPRESENTATIVE_COMBINATIONS: Final = (
    Combination(10, 2),
    Combination(10, 4),
    Combination(15, 4),
    Combination(16, 4),
    Combination(10, 6),
    Combination(10, 12),
    Combination(16, 12),
)
REPRESENTATIVE_BY_SELECTOR: Final = {
    combination.selector: combination for combination in REPRESENTATIVE_COMBINATIONS
}


class BenchmarkError(RuntimeError):
    """A safe orchestration failure whose details must not be printed."""


class InstrumentationContractError(BenchmarkError):
    """The installed/source Android benchmark cannot provide paired IDs."""


class CleanupSafetyError(BenchmarkError):
    """An isolated runtime did not become idle enough for a safe close."""


@dataclass(frozen=True, slots=True)
class _ProcessSnapshot:
    cpu_seconds: float
    rss_bytes: int
    peak_rss_bytes: int
    read_operations: int
    write_operations: int
    other_operations: int
    read_bytes: int
    write_bytes: int
    other_bytes: int


@dataclass(frozen=True, slots=True)
class _ProcessUsage:
    samples: int
    wall_ms: float
    cpu_seconds: float | None
    average_cpu_core_percent: float | None
    peak_cpu_core_percent: float | None
    rss_start_mib: float | None
    rss_end_mib: float | None
    peak_rss_mib: float | None
    read_operations: int | None
    write_operations: int | None
    other_operations: int | None
    read_bytes: int | None
    write_bytes: int | None
    other_bytes: int | None


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


def _filetime_seconds(value: wintypes.FILETIME) -> float:
    ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    return ticks / 10_000_000.0


def _process_snapshot(pid: int) -> _ProcessSnapshot | None:
    if os.name != "nt" or isinstance(pid, bool) or pid <= 0:
        return None
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    kernel32: Any = loader("kernel32", use_last_error=True)
    psapi: Any = loader("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
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
    process_query_limited_information = 0x1000
    process_vm_read = 0x0010
    handle = kernel32.OpenProcess(
        process_query_limited_information | process_vm_read,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        memory = _ProcessMemoryCounters()
        memory.cb = ctypes.sizeof(memory)
        io_counters = _ProcessIoCounters()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        if not psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(memory),
            ctypes.sizeof(memory),
        ):
            return None
        if not kernel32.GetProcessIoCounters(handle, ctypes.byref(io_counters)):
            return None
        return _ProcessSnapshot(
            cpu_seconds=_filetime_seconds(kernel) + _filetime_seconds(user),
            rss_bytes=int(memory.working_set_size),
            peak_rss_bytes=int(memory.peak_working_set_size),
            read_operations=int(io_counters.read_operation_count),
            write_operations=int(io_counters.write_operation_count),
            other_operations=int(io_counters.other_operation_count),
            read_bytes=int(io_counters.read_transfer_count),
            write_bytes=int(io_counters.write_transfer_count),
            other_bytes=int(io_counters.other_transfer_count),
        )
    finally:
        kernel32.CloseHandle(handle)


class _ProcessResourceSampler:
    def __init__(self, roles: Mapping[str, int]) -> None:
        self._roles = dict(roles)
        self._stop = threading.Event()
        self._failure = threading.Event()
        self._thread: threading.Thread | None = None
        self._wall_started = 0.0
        self._started: dict[str, _ProcessSnapshot | None] = {}
        self._latest: dict[str, _ProcessSnapshot | None] = {}
        self._peak_rss: dict[str, int] = {role: 0 for role in roles}
        self._peak_cpu: dict[str, float] = {role: 0.0 for role in roles}
        self._sample_count: dict[str, int] = {role: 0 for role in roles}
        self._last_wall = 0.0

    def start(self) -> None:
        self._wall_started = self._last_wall = time.perf_counter()
        self._started = {
            role: _process_snapshot(pid) for role, pid in self._roles.items()
        }
        self._latest = dict(self._started)
        for role, snapshot in self._started.items():
            if snapshot is not None:
                self._peak_rss[role] = snapshot.rss_bytes
                self._sample_count[role] = 1
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="tc007-android-host-resources",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, dict[str, object]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise BenchmarkError("process resource sampler did not stop")
        if self._failure.is_set():
            raise BenchmarkError("process resource sampler failed")
        self._sample_once()
        finished_wall = time.perf_counter()
        wall_seconds = max(finished_wall - self._wall_started, 1e-9)
        result: dict[str, dict[str, object]] = {}
        for role in self._roles:
            started = self._started.get(role)
            finished = self._latest.get(role)
            if started is None or finished is None:
                usage = _ProcessUsage(
                    samples=self._sample_count[role],
                    wall_ms=round(wall_seconds * 1_000.0, 3),
                    cpu_seconds=None,
                    average_cpu_core_percent=None,
                    peak_cpu_core_percent=None,
                    rss_start_mib=None,
                    rss_end_mib=None,
                    peak_rss_mib=None,
                    read_operations=None,
                    write_operations=None,
                    other_operations=None,
                    read_bytes=None,
                    write_bytes=None,
                    other_bytes=None,
                )
            else:
                cpu_seconds = max(finished.cpu_seconds - started.cpu_seconds, 0.0)
                mib = 1024 * 1024
                usage = _ProcessUsage(
                    samples=self._sample_count[role],
                    wall_ms=round(wall_seconds * 1_000.0, 3),
                    cpu_seconds=round(cpu_seconds, 6),
                    average_cpu_core_percent=round(
                        cpu_seconds / wall_seconds * 100.0, 3
                    ),
                    peak_cpu_core_percent=round(self._peak_cpu[role], 3),
                    rss_start_mib=round(started.rss_bytes / mib, 3),
                    rss_end_mib=round(finished.rss_bytes / mib, 3),
                    peak_rss_mib=round(self._peak_rss[role] / mib, 3),
                    read_operations=_counter_delta(
                        started, finished, "read_operations"
                    ),
                    write_operations=_counter_delta(
                        started, finished, "write_operations"
                    ),
                    other_operations=_counter_delta(
                        started, finished, "other_operations"
                    ),
                    read_bytes=_counter_delta(started, finished, "read_bytes"),
                    write_bytes=_counter_delta(started, finished, "write_bytes"),
                    other_bytes=_counter_delta(started, finished, "other_bytes"),
                )
            result[role] = asdict(usage)
        return result

    def _sample_loop(self) -> None:
        try:
            while not self._stop.wait(RESOURCE_SAMPLE_SECONDS):
                self._sample_once()
        except Exception:
            self._failure.set()

    def _sample_once(self) -> None:
        current_wall = time.perf_counter()
        interval = max(current_wall - self._last_wall, 1e-9)
        for role, pid in self._roles.items():
            current = _process_snapshot(pid)
            previous = self._latest.get(role)
            if current is None:
                continue
            self._sample_count[role] += 1
            self._peak_rss[role] = max(self._peak_rss[role], current.rss_bytes)
            if previous is not None:
                cpu_delta = max(current.cpu_seconds - previous.cpu_seconds, 0.0)
                self._peak_cpu[role] = max(
                    self._peak_cpu[role], cpu_delta / interval * 100.0
                )
            self._latest[role] = current
        self._last_wall = current_wall


def _counter_delta(
    started: _ProcessSnapshot, finished: _ProcessSnapshot, field: str
) -> int:
    return max(int(getattr(finished, field)) - int(getattr(started, field)), 0)


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise BenchmarkError("latency summary requires samples")
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _latency_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None}
    materialized = [float(value) for value in values]
    return {
        "samples": len(materialized),
        "p50_ms": round(statistics.median(materialized), 3),
        "p95_ms": round(_percentile(materialized, 0.95), 3),
        "minimum_ms": round(min(materialized), 3),
        "maximum_ms": round(max(materialized), 3),
    }


def _parse_combinations(value: str | None) -> tuple[Combination, ...]:
    if value is None or not value.strip():
        return REPRESENTATIVE_COMBINATIONS
    selectors = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not selectors:
        raise BenchmarkError("combination selector is empty")
    if len(selectors) != len(set(selectors)):
        raise BenchmarkError("combination selectors must be unique")
    try:
        selected = tuple(REPRESENTATIVE_BY_SELECTOR[item] for item in selectors)
    except KeyError as exc:
        raise BenchmarkError("unsupported representative combination") from exc
    return selected


def _validate_id_seed(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", normalized):
        raise BenchmarkError("ID seed must be an 8-128 character safe identifier")
    return normalized


def _deterministic_id(seed: str, index: int, kind: str) -> str:
    digest = hashlib.sha256(f"tc007-id-v1\0{seed}\0{index}\0{kind}".encode()).digest()
    uuid_bytes = bytearray(digest[:16])
    uuid_bytes[6] = (uuid_bytes[6] & 0x0F) | 0x50
    uuid_bytes[8] = (uuid_bytes[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(uuid_bytes)))


def _id_sequence_sha256(seed: str, rounds: int) -> str:
    digest = hashlib.sha256()
    for index in range(rounds):
        for role in ("foreground", "prepared"):
            for kind in ("request", "shown", "action"):
                digest.update(_deterministic_id(seed, index, f"{role}.{kind}").encode())
                digest.update(b"\0")
    return digest.hexdigest()


def _instrumentation_contract(path: Path) -> dict[str, bool]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        source = ""
    coil_fields = (
        '"coil"',
        '"size_px"',
        '"network_calls"',
        '"content_length_bytes"',
        '"unknown_content_lengths"',
        '"invalid_content_lengths"',
        '"cache_hit_reloads"',
        '"same_url_size"',
    )
    resource_fields = (
        '"resources"',
        '"cpu_avg_core_percent"',
        '"cpu_peak_core_percent"',
        '"pss_before_kb"',
        '"pss_after_kb"',
        '"pss_peak_kb"',
    )
    prepared_fields = (
        '"prepared"',
        '"created"',
        '"shown_before_consume"',
        '"duplicate_create_count"',
        '"prepared_click_commit_ms"',
        '"shown_after_consume"',
    )
    cancellation_fields = (
        '"cancel_probe"',
        '"requested"',
        '"latency_ms"',
        '"cancelled_calls"',
        '"late_completions"',
        '"queued_after"',
        '"running_after"',
        '"residual_calls"',
    )
    return {
        "benchmark_class": "class RecommendationNetworkBenchmarkTest" in source,
        "result_schema": RESULT_SCHEMA in source,
        "result_prefix": RESULT_PREFIX in source,
        "id_argument": ID_ARGUMENT in source and "ARG_ID_SEED" in source,
        "id_sequence_result": ID_RESULT_FIELD in source,
        "coil_thumbnail_decode_640": (
            "ImageLoader" in source
            and "THUMBNAIL_SIZE_PX" in source
            and re.search(r"THUMBNAIL_SIZE_PX\s*=\s*640\b", source) is not None
            and all(field in source for field in coil_fields)
        ),
        "resource_sampling_100ms": (
            "RESOURCE_SAMPLE_INTERVAL_MS" in source
            and re.search(r"RESOURCE_SAMPLE_INTERVAL_MS\s*=\s*100L?\b", source)
            is not None
            and all(field in source for field in resource_fields)
        ),
        "prepared_batch_consumption": all(field in source for field in prepared_fields),
        "inflight_cancellation_probe": all(
            field in source for field in cancellation_fields
        ),
    }


def _require_instrumentation_contract(path: Path) -> dict[str, bool]:
    contract = _instrumentation_contract(path)
    if not all(contract.values()):
        raise InstrumentationContractError(
            "Android instrumentation lacks deterministic paired-ID output"
        )
    return contract


def _snapshot_signature(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        stat_result = path.stat()
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(f"\0{stat_result.st_size}\0{stat_result.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def _clone_snapshot(
    snapshot_home: Path,
    run_home: Path,
    credential_store: Path,
) -> None:
    if run_home.exists():
        raise BenchmarkError("isolated run home already exists")
    shutil.copytree(snapshot_home, run_home)
    config_path = run_home / "config.json"
    recommendation_db = run_home / "recommendations.sqlite3"
    if not config_path.is_file() or not recommendation_db.is_file():
        raise BenchmarkError("snapshot is missing required configuration or database")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    libraries = config.get("libraries") if isinstance(config, dict) else None
    if not isinstance(libraries, list) or not libraries:
        raise BenchmarkError("snapshot has no library configuration")
    for index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise BenchmarkError("snapshot library configuration is invalid")
        workspace = run_home / "libraries" / f"library-{index:03d}"
        if not workspace.is_dir():
            raise BenchmarkError("snapshot library workspace is missing")
        library["workspace_directory"] = str(workspace)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lan_settings_path = run_home / "lan-access.json"
    if lan_settings_path.is_file():
        lan_settings = json.loads(lan_settings_path.read_text(encoding="utf-8"))
        if not isinstance(lan_settings, dict):
            raise BenchmarkError("snapshot LAN settings are invalid")
        lan_settings["enabled"] = False
        lan_settings_path.write_text(
            json.dumps(lan_settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if (run_home / "cache").exists():
        raise BenchmarkError("snapshot must not contain an image cache")
    shutil.copy2(credential_store, run_home / "lan-devices.json")
    cloned_revoked = run_home / "lan-devices.json.revoked"
    cloned_revoked.unlink(missing_ok=True)
    revoked = credential_store.with_name(f"{credential_store.name}.revoked")
    if revoked.is_file():
        shutil.copy2(revoked, cloned_revoked)


def _parse_paired_origin(value: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
    ):
        raise BenchmarkError("paired origin must be a plain HTTP IPv4 origin")
    try:
        address = socket.inet_ntoa(socket.inet_aton(parsed.hostname))
    except OSError as exc:
        raise BenchmarkError("paired origin must use an IPv4 address") from exc
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        raise BenchmarkError("paired origin port is invalid")
    return address, port, f"http://{address}:{port}"


def _validate_bind_host(value: str) -> str:
    normalized = value.strip()
    try:
        address = socket.inet_ntoa(socket.inet_aton(normalized))
    except OSError as exc:
        raise BenchmarkError("bind host must be an IPv4 address") from exc
    return address


def _require_port_available(bind_host: str, port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((bind_host, port))
    except OSError as exc:
        raise BenchmarkError(
            "paired LAN port is already in use; no existing service was stopped"
        ) from exc
    finally:
        probe.close()


def _wait_backend_ready(facade: Any, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = facade.bootstrap()
        service = payload.get("service") if isinstance(payload, Mapping) else None
        if isinstance(service, Mapping) and service.get("backend_ready") is True:
            return
        if isinstance(service, Mapping) and service.get("status") == "degraded":
            raise BenchmarkError("isolated backend entered degraded state")
        time.sleep(0.2)
    raise BenchmarkError("isolated backend did not become ready")


def _backend_pid(facade: Any) -> int:
    host = getattr(facade, "_host", None)
    process = getattr(host, "_process", None)
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise BenchmarkError("isolated backend process is unavailable")
    return pid


def _configured_image_roots(config_path: Path) -> tuple[Path, ...]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    libraries = payload.get("libraries") if isinstance(payload, dict) else None
    if not isinstance(libraries, list):
        raise BenchmarkError("isolated configuration has no libraries")
    roots: list[Path] = []
    for library in libraries:
        if not isinstance(library, dict) or library.get("enabled", True) is not True:
            continue
        raw_root = library.get("image_root")
        if isinstance(raw_root, str):
            root = Path(raw_root).expanduser().resolve(strict=True)
            if root.is_dir():
                roots.append(root)
    if not roots:
        raise BenchmarkError("no enabled real image root is available")
    return tuple(roots)


def _source_candidates(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(
                name for name in names if not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                source = Path(directory) / name
                if (
                    source.suffix.casefold() in IMAGE_SUFFIXES
                    and not source.is_symlink()
                ):
                    yield source


def _control_sources(
    registry: Any, roots: Sequence[Path], count: int
) -> tuple[Any, ...]:
    values: list[Any] = []
    for source in _source_candidates(roots):
        try:
            metadata = registry.register(source)
        except Exception:
            continue
        values.append(metadata)
        if len(values) >= count:
            return tuple(values)
    raise BenchmarkError("not enough readable images for Windows fairness controls")


def _loopback_get(url: str, *, expect_image: bool) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120.0) as response:
            content_type = response.headers.get_content_type()
            content = response.read()
        valid = bool(content) and (
            not expect_image or content_type.startswith("image/")
        )
    except Exception:
        valid = False
    return (time.perf_counter() - started) * 1_000.0, valid


class _WindowsFairnessLoad:
    def __init__(
        self,
        gateway_url: str,
        controls: Sequence[Any],
        *,
        interval_seconds: float,
    ) -> None:
        if len(controls) % 2:
            raise ValueError("fairness controls must be thumbnail/preview pairs")
        self._gateway_url = gateway_url
        self._controls = tuple(controls)
        self._interval_seconds = interval_seconds
        self._start = threading.Event()
        self._stop = threading.Event()
        self._failure = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._thumbnail_ms: list[float] = []
        self._preview_ms: list[float] = []
        self._light_ms: list[float] = []
        self._cold_thumbnail_ms: list[float] = []
        self._cold_preview_ms: list[float] = []
        self._cold_light_ms: list[float] = []
        self._hot_thumbnail_ms: list[float] = []
        self._hot_preview_ms: list[float] = []
        self._hot_light_ms: list[float] = []
        self._errors = 0

    def launch(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="tc007-windows-fairness",
            daemon=True,
        )
        self._thread.start()

    def begin(self) -> None:
        self._start.set()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        self._start.set()
        if self._thread is not None:
            self._thread.join(timeout=150.0)
            if self._thread.is_alive():
                raise BenchmarkError("Windows fairness worker did not stop")
        if self._failure.is_set():
            raise BenchmarkError("Windows fairness worker failed")
        with self._lock:
            return {
                "groups": len(self._thumbnail_ms),
                "cold_groups": len(self._cold_thumbnail_ms),
                "hot_groups": len(self._hot_thumbnail_ms),
                "search_thumbnail": _latency_summary(self._thumbnail_ms),
                "preview": _latency_summary(self._preview_ms),
                "light_http": _latency_summary(self._light_ms),
                "search_thumbnail_cold": _latency_summary(self._cold_thumbnail_ms),
                "preview_cold": _latency_summary(self._cold_preview_ms),
                "light_http_cold": _latency_summary(self._cold_light_ms),
                "search_thumbnail_hot": _latency_summary(self._hot_thumbnail_ms),
                "preview_hot": _latency_summary(self._hot_preview_ms),
                "light_http_hot": _latency_summary(self._hot_light_ms),
                "errors": self._errors,
            }

    def _run(self) -> None:
        try:
            self._start.wait()
            pairs = len(self._controls) // 2
            with ThreadPoolExecutor(max_workers=3) as executor:
                index = 0
                while not self._stop.is_set():
                    pair_index = index % pairs
                    thumbnail = self._controls[pair_index * 2]
                    preview = self._controls[pair_index * 2 + 1]
                    futures = (
                        executor.submit(
                            _loopback_get,
                            f"{self._gateway_url}api/image/{thumbnail.image_id}"
                            "?variant=thumbnail",
                            expect_image=True,
                        ),
                        executor.submit(
                            _loopback_get,
                            f"{self._gateway_url}api/image/{preview.image_id}"
                            "?variant=preview",
                            expect_image=True,
                        ),
                        executor.submit(
                            _loopback_get,
                            f"{self._gateway_url}api/bootstrap",
                            expect_image=False,
                        ),
                    )
                    results = tuple(future.result(timeout=150.0) for future in futures)
                    with self._lock:
                        self._thumbnail_ms.append(results[0][0])
                        self._preview_ms.append(results[1][0])
                        self._light_ms.append(results[2][0])
                        if index < pairs:
                            self._cold_thumbnail_ms.append(results[0][0])
                            self._cold_preview_ms.append(results[1][0])
                            self._cold_light_ms.append(results[2][0])
                        else:
                            self._hot_thumbnail_ms.append(results[0][0])
                            self._hot_preview_ms.append(results[1][0])
                            self._hot_light_ms.append(results[2][0])
                        self._errors += sum(not result[1] for result in results)
                    index += 1
                    if self._stop.wait(self._interval_seconds):
                        break
        except Exception:
            self._failure.set()


class _RegistryMetrics:
    def __init__(self, real_render: Any) -> None:
        self._real_render = real_render
        self._lock = threading.Lock()
        self.requests = {"thumbnail": 0, "preview": 0}
        self.memory_hits = {"thumbnail": 0, "preview": 0}
        self.disk_hits = {"thumbnail": 0, "preview": 0}
        self.cache_misses = {"thumbnail": 0, "preview": 0}
        self.payload_errors = {"thumbnail": 0, "preview": 0}
        self.renders = {"thumbnail": 0, "preview": 0}
        self.render_errors = {"thumbnail": 0, "preview": 0}
        self.render_ms = {"thumbnail": [], "preview": []}

    def render(self, path: Path, variant: str, mtime_ns: int) -> Any:
        started = time.perf_counter()
        try:
            result = self._real_render(path, variant, mtime_ns)
        except Exception:
            with self._lock:
                self.render_errors[variant] += 1
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1_000.0
            with self._lock:
                self.renders[variant] += 1
                self.render_ms[variant].append(elapsed_ms)
        return result

    def classify(self, variant: str, source: str) -> None:
        with self._lock:
            self.requests[variant] += 1
            if source == "memory":
                self.memory_hits[variant] += 1
            elif source == "disk":
                self.disk_hits[variant] += 1
            else:
                self.cache_misses[variant] += 1

    def payload_failed(self, variant: str) -> None:
        with self._lock:
            self.payload_errors[variant] += 1

    def result(self, cache_root: Path) -> dict[str, object]:
        entries = 0
        bytes_total = 0
        if cache_root.is_dir():
            for path in cache_root.rglob("*"):
                if not path.is_file():
                    continue
                with suppress(OSError):
                    entries += 1
                    bytes_total += path.stat().st_size
        with self._lock:
            return {
                "requests": dict(self.requests),
                "memory_hits": dict(self.memory_hits),
                "disk_hits": dict(self.disk_hits),
                "cache_misses": dict(self.cache_misses),
                "payload_errors": dict(self.payload_errors),
                "renders": dict(self.renders),
                "render_errors": dict(self.render_errors),
                "render_timing_ms": {
                    variant: _latency_summary(values)
                    for variant, values in self.render_ms.items()
                },
                "disk_cache_entries": entries,
                "disk_cache_bytes": bytes_total,
            }


def _instrumented_registry_class(image_registry_type: type[Any]) -> type[Any]:
    class InstrumentedImageRegistry(image_registry_type):
        def __init__(
            self, *args: Any, metrics: _RegistryMetrics, **kwargs: Any
        ) -> None:
            self._benchmark_metrics = metrics
            super().__init__(*args, **kwargs)

        def payload(self, image_id: str, variant: str) -> Any:
            source = "miss"
            try:
                path = self.resolve(image_id)
                stat_result = path.stat()
                cache_key = (
                    image_id,
                    variant,
                    stat_result.st_mtime_ns,
                    stat_result.st_size,
                )
                with self._lock:
                    memory_hit = cache_key in self._cache
                disk_path = self._disk_path(path, variant, stat_result)
                disk_hit = disk_path is not None and disk_path.is_file()
                source = "memory" if memory_hit else "disk" if disk_hit else "miss"
                result = super().payload(image_id, variant)
            except Exception:
                self._benchmark_metrics.payload_failed(variant)
                raise
            self._benchmark_metrics.classify(variant, source)
            return result

    return InstrumentedImageRegistry


def _adb_prefix(adb: str, serial: str | None) -> list[str]:
    prefix = [adb]
    if serial:
        prefix.extend(("-s", serial))
    return prefix


def _require_adb_device(adb: str, serial: str | None) -> None:
    try:
        completed = subprocess.run(
            [*_adb_prefix(adb, serial), "get-state"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError("ADB device preflight failed") from exc
    if completed.returncode != 0 or completed.stdout.strip() != "device":
        raise BenchmarkError("ADB device is not ready")


def _instrumentation_command(
    *,
    adb: str,
    serial: str | None,
    origin: str,
    combination: Combination,
    rounds: int,
    id_seed: str,
    instrumentation_class: str,
    instrumentation_runner: str,
) -> list[str]:
    return [
        *_adb_prefix(adb, serial),
        "shell",
        "am",
        "instrument",
        "-w",
        "-r",
        "-e",
        "class",
        instrumentation_class,
        "-e",
        "tc007NetworkBenchmark",
        "true",
        "-e",
        "tc007ClientConcurrency",
        str(combination.client_concurrency),
        "-e",
        "tc007ServerUrl",
        origin,
        "-e",
        "tc007BatchCount",
        str(rounds),
        "-e",
        ID_ARGUMENT,
        id_seed,
        instrumentation_runner,
    ]


def _parse_instrumentation_output(output: str) -> dict[str, Any]:
    candidates: list[str] = []
    for line in output.splitlines():
        if RESULT_PREFIX in line:
            candidates.append(line.split(RESULT_PREFIX, 1)[1].strip())
        marker = f"{RESULT_BUNDLE_KEY}="
        if marker in line:
            candidates.append(line.split(marker, 1)[1].strip())
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise BenchmarkError("instrumentation did not return its JSON result")


def _finite_number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise BenchmarkError(f"instrumentation field is invalid: {name}")
    return float(value)


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchmarkError(f"instrumentation field is invalid: {name}")
    return value


def _sanitized_timing(
    value: object,
    name: str,
    *,
    expected_samples: int,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"instrumentation timing is missing: {name}")
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) != expected_samples:
        raise BenchmarkError(f"instrumentation samples are invalid: {name}")
    return {
        "p50": round(_finite_number(value.get("p50"), f"{name}.p50"), 3),
        "p95": round(_finite_number(value.get("p95"), f"{name}.p95"), 3),
        "samples": [
            round(_finite_number(sample, f"{name}.sample"), 3) for sample in samples
        ],
    }


def _sanitize_instrumentation_result(
    payload: Mapping[str, Any],
    *,
    combination: Combination,
    rounds: int,
    expected_id_sha256: str,
) -> dict[str, object]:
    if payload.get("schema") != RESULT_SCHEMA:
        raise BenchmarkError("instrumentation result schema is invalid")
    if payload.get(ID_RESULT_FIELD) != expected_id_sha256:
        raise InstrumentationContractError(
            "instrumentation did not use the paired deterministic ID sequence"
        )
    integer_expectations = {
        "client_concurrency": combination.client_concurrency,
        "requested_cycles": rounds,
        "completed_foreground": rounds,
        "planned_new_requests": rounds * 2,
        "errors": 0,
        "cancellations": 0,
    }
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        ID_RESULT_FIELD: expected_id_sha256,
    }
    for name, expected in integer_expectations.items():
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise BenchmarkError(f"instrumentation field is invalid: {name}")
        result[name] = value
    timing = payload.get("timing_ms")
    timing_sample_counts = {
        "foreground_create": rounds,
        "foreground_t4": rounds,
        "foreground_t6": rounds,
        "foreground_t15": rounds,
        "prepared_create": rounds,
        "prepared_preload_t4": rounds,
        "prepared_preload_t6": rounds,
        "prepared_preload_t15": rounds,
        "prepared_click_commit": rounds,
        "shown": rounds * 2,
        "action": rounds * 2,
        "status_under_contention": rounds * 2,
    }
    if not isinstance(timing, Mapping):
        raise BenchmarkError("instrumentation timing payload is invalid")
    sanitized_timing: dict[str, object] = {}
    for name, sample_count in timing_sample_counts.items():
        sanitized_timing[name] = _sanitized_timing(
            timing.get(name),
            name,
            expected_samples=sample_count,
        )
    result["timing_ms"] = sanitized_timing
    traffic = payload.get("traffic_rx_bytes")
    if traffic is not None and (
        isinstance(traffic, bool) or not isinstance(traffic, int) or traffic < 0
    ):
        raise BenchmarkError("instrumentation traffic counter is invalid")
    result["traffic_rx_bytes"] = traffic
    coil = payload.get("coil")
    if not isinstance(coil, Mapping):
        raise BenchmarkError("instrumentation Coil payload is invalid")
    if _integer(coil.get("size_px"), "coil.size_px") != 640:
        raise BenchmarkError("instrumentation Coil decode size is invalid")
    if coil.get("same_url_size") is not True:
        raise BenchmarkError("instrumentation Coil cache identity is invalid")
    result["coil"] = {
        "size_px": 640,
        "network_calls": _integer(
            coil.get("network_calls"), "coil.network_calls", minimum=1
        ),
        "content_length_bytes": _integer(
            coil.get("content_length_bytes"),
            "coil.content_length_bytes",
            minimum=1,
        ),
        "unknown_content_lengths": _integer(
            coil.get("unknown_content_lengths"), "coil.unknown_content_lengths"
        ),
        "invalid_content_lengths": _integer(
            coil.get("invalid_content_lengths"), "coil.invalid_content_lengths"
        ),
        "cache_hit_reloads": _integer(
            coil.get("cache_hit_reloads"), "coil.cache_hit_reloads", minimum=1
        ),
        "same_url_size": True,
    }
    if (
        result["coil"]["unknown_content_lengths"] != 0
        or result["coil"]["invalid_content_lengths"] != 0
    ):
        raise BenchmarkError("instrumentation Coil content lengths are invalid")
    resources = payload.get("resources")
    if not isinstance(resources, Mapping):
        raise BenchmarkError("instrumentation resource payload is invalid")
    cpu_average = _finite_number(
        resources.get("cpu_avg_core_percent"), "resources.cpu_avg_core_percent"
    )
    cpu_peak = _finite_number(
        resources.get("cpu_peak_core_percent"), "resources.cpu_peak_core_percent"
    )
    pss_before = _integer(resources.get("pss_before_kb"), "resources.pss_before_kb")
    pss_after = _integer(resources.get("pss_after_kb"), "resources.pss_after_kb")
    pss_peak = _integer(resources.get("pss_peak_kb"), "resources.pss_peak_kb")
    if cpu_peak < cpu_average or pss_peak < max(pss_before, pss_after):
        raise BenchmarkError("instrumentation resource peaks are invalid")
    result["resources"] = {
        "cpu_avg_core_percent": round(cpu_average, 3),
        "cpu_peak_core_percent": round(cpu_peak, 3),
        "pss_before_kb": pss_before,
        "pss_after_kb": pss_after,
        "pss_peak_kb": pss_peak,
    }
    prepared = payload.get("prepared")
    if not isinstance(prepared, Mapping):
        raise BenchmarkError("instrumentation prepared-batch payload is invalid")
    prepared_count = _integer(prepared.get("created"), "prepared.created")
    shown_before = _integer(
        prepared.get("shown_before_consume"), "prepared.shown_before_consume"
    )
    duplicate_create = _integer(
        prepared.get("duplicate_create_count"), "prepared.duplicate_create_count"
    )
    shown_after = _integer(
        prepared.get("shown_after_consume"), "prepared.shown_after_consume"
    )
    if (
        prepared_count != rounds
        or shown_before != 0
        or duplicate_create != 0
        or shown_after != prepared_count
    ):
        raise BenchmarkError("instrumentation prepared-batch counters are invalid")
    result["prepared"] = {
        "scope": "production_preloader_state_materialization_no_compose_paint",
        "created": prepared_count,
        "shown_before_consume": shown_before,
        "duplicate_create_count": duplicate_create,
        "prepared_click_commit_ms": _sanitized_timing(
            prepared.get("prepared_click_commit_ms"),
            "prepared.prepared_click_commit_ms",
            expected_samples=prepared_count,
        ),
        "shown_after_consume": shown_after,
    }
    if prepared.get("scope") != result["prepared"]["scope"]:
        raise BenchmarkError("instrumentation prepared-batch scope is invalid")
    cancellation = payload.get("cancel_probe")
    if not isinstance(cancellation, Mapping):
        raise BenchmarkError("instrumentation cancellation payload is invalid")
    if cancellation.get("requested") is not True:
        raise BenchmarkError("instrumentation cancellation probe did not run")
    cancelled_calls = _integer(
        cancellation.get("cancelled_calls"),
        "cancel_probe.cancelled_calls",
        minimum=1,
    )
    late_completions = _integer(
        cancellation.get("late_completions"), "cancel_probe.late_completions"
    )
    queued_after = _integer(
        cancellation.get("queued_after"), "cancel_probe.queued_after"
    )
    running_after = _integer(
        cancellation.get("running_after"), "cancel_probe.running_after"
    )
    residual_calls = _integer(
        cancellation.get("residual_calls"), "cancel_probe.residual_calls"
    )
    if late_completions or queued_after or running_after or residual_calls:
        raise BenchmarkError("instrumentation cancellation probe did not drain")
    result["cancel_probe"] = {
        "scope": "test_owned_coil_preload_job",
        "requested": True,
        "latency_ms": round(
            _finite_number(cancellation.get("latency_ms"), "cancel_probe.latency_ms"),
            3,
        ),
        "cancelled_calls": cancelled_calls,
        "late_completions": late_completions,
        "queued_after": queued_after,
        "running_after": running_after,
        "residual_calls": residual_calls,
    }
    if cancellation.get("scope") != result["cancel_probe"]["scope"]:
        raise BenchmarkError("instrumentation cancellation scope is invalid")
    return result


def _run_instrumentation(
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError("Android instrumentation could not complete") from exc
    combined = f"{completed.stdout}\n{completed.stderr}"
    payload = _parse_instrumentation_output(combined)
    if completed.returncode != 0 or "FAILURES!!!" in combined:
        raise BenchmarkError("Android instrumentation reported a failure")
    return payload


def _safe_close_runtime(runtime: Any, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while runtime.facade.has_active_jobs() and time.monotonic() < deadline:
        time.sleep(0.2)
    if runtime.facade.has_active_jobs():
        raise CleanupSafetyError(
            "isolated backend remained active; force termination was refused"
        )
    runtime.close(force=False)


def _run_combination(
    *,
    combination: Combination,
    snapshot_home: Path,
    artifact_root: Path,
    credential_store_path: Path,
    paired_host: str,
    paired_port: int,
    paired_origin: str,
    bind_host: str,
    adb: str,
    serial: str | None,
    rounds: int,
    id_seed: str,
    expected_id_sha256: str,
    instrumentation_class: str,
    instrumentation_runner: str,
    instrumentation_timeout_seconds: float,
    control_groups: int,
    control_interval_seconds: float,
) -> dict[str, object]:
    run_home = artifact_root / f"android-matrix-run-{uuid.uuid4().hex}"
    runtime: Any | None = None
    lan_server: Any | None = None
    query_images: Any | None = None
    fairness: _WindowsFairnessLoad | None = None
    resources: _ProcessResourceSampler | None = None
    resource_result: dict[str, dict[str, object]] | None = None
    fairness_result: dict[str, object] | None = None
    registry_result: dict[str, object] | None = None
    registry: Any | None = None
    image_registry_patch: Any | None = None
    clone_safe_to_remove = True
    try:
        _require_port_available(paired_host, paired_port)
        if bind_host != paired_host:
            _require_port_available(bind_host, paired_port)
        _clone_snapshot(snapshot_home, run_home, credential_store_path)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from zvec_host.recommendation_service import RecommendationService
        from zvec_lan import (
            JsonCredentialStore,
            LanApiServer,
            PairingManager,
            QueryImageStore,
        )
        from zvec_webview import image_registry as image_registry_module
        from zvec_webview.facade import PreviewFacade
        from zvec_webview.image_registry import ImageRegistry
        from zvec_webview.lan_access import PreviewLanAdapter
        from zvec_webview.runtime import PreviewRuntime

        metrics = _RegistryMetrics(image_registry_module._render_image)
        registry_type = _instrumented_registry_class(ImageRegistry)
        cache_root = run_home / "cache"
        registry = registry_type(
            cache_directory=cache_root,
            max_render_workers=combination.server_render_workers,
            metrics=metrics,
        )
        facade = PreviewFacade(run_home / "config.json", image_registry=registry)
        runtime = PreviewRuntime(facade=facade)
        image_registry_patch = patch.object(
            image_registry_module,
            "_render_image",
            metrics.render,
        )
        image_registry_patch.start()
        started = runtime.start()
        _wait_backend_ready(facade)
        credentials = JsonCredentialStore(run_home / "lan-devices.json")
        if not credentials.list_clients():
            raise BenchmarkError("isolated credential clone has no paired client")
        recommendation_backend = RecommendationService(
            facade._ready_client,
            facade.image_registry,
            run_home,
        )
        adapter = PreviewLanAdapter(
            facade,
            recommendation_backend=recommendation_backend,
        )
        query_images = QueryImageStore(run_home / "lan-query-images")
        pairing = PairingManager(credentials)
        lan_server = LanApiServer(
            instance_id="tc007-android-matrix",
            name="TC007 Android matrix",
            search_backend=adapter,
            recommendation_backend=adapter,
            media_resolver=adapter,
            pairing_manager=pairing,
            query_images=query_images,
            host=bind_host,
            port=paired_port,
            allowed_hosts=(paired_host,),
        )
        lan_server.start()
        adapter.set_recommendation_media_origin(paired_origin)
        roots = _configured_image_roots(run_home / "config.json")
        controls = _control_sources(registry, roots, control_groups * 2)
        fairness = _WindowsFairnessLoad(
            started.address.url,
            controls,
            interval_seconds=control_interval_seconds,
        )
        fairness.launch()
        roles = {"gateway": os.getpid(), "backend": _backend_pid(facade)}
        resources = _ProcessResourceSampler(roles)
        resources.start()
        fairness.begin()
        command = _instrumentation_command(
            adb=adb,
            serial=serial,
            origin=paired_origin,
            combination=combination,
            rounds=rounds,
            id_seed=id_seed,
            instrumentation_class=instrumentation_class,
            instrumentation_runner=instrumentation_runner,
        )
        raw_android = _run_instrumentation(
            command,
            timeout_seconds=instrumentation_timeout_seconds,
        )
        android = _sanitize_instrumentation_result(
            raw_android,
            combination=combination,
            rounds=rounds,
            expected_id_sha256=expected_id_sha256,
        )
        fairness_result = fairness.stop()
        fairness = None
        resource_result = resources.stop()
        resources = None
        registry_result = metrics.result(cache_root)
        return {
            "combination": {
                "client_concurrency": combination.client_concurrency,
                "server_render_workers": combination.server_render_workers,
            },
            "android": android,
            "windows_processes": resource_result,
            "server_media": registry_result,
            "windows_fairness": fairness_result,
        }
    finally:
        cleanup_errors: list[Exception] = []
        if fairness is not None:
            try:
                fairness_result = fairness.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
                clone_safe_to_remove = False
        if resources is not None:
            try:
                resource_result = resources.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        if lan_server is not None:
            try:
                lan_server.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
                clone_safe_to_remove = False
        if query_images is not None:
            try:
                query_images.close()
            except Exception as exc:
                cleanup_errors.append(exc)
                clone_safe_to_remove = False
        if runtime is not None:
            try:
                _safe_close_runtime(runtime)
            except Exception as exc:
                cleanup_errors.append(exc)
                clone_safe_to_remove = False
        if image_registry_patch is not None:
            try:
                image_registry_patch.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        if run_home.exists():
            resolved = run_home.resolve()
            if resolved.parent != artifact_root:
                cleanup_errors.append(
                    CleanupSafetyError("isolated clone cleanup target is unsafe")
                )
            elif clone_safe_to_remove:
                try:
                    shutil.rmtree(resolved)
                except Exception as exc:
                    cleanup_errors.append(exc)
        if cleanup_errors:
            raise CleanupSafetyError("isolated combination cleanup did not complete")


def _fairness_regressions(results: Sequence[dict[str, object]]) -> None:
    if not results:
        return
    baseline = results[0]
    baseline_fairness = baseline.get("windows_fairness")
    if not isinstance(baseline_fairness, Mapping):
        return
    for result in results:
        fairness = result.get("windows_fairness")
        if not isinstance(fairness, Mapping):
            continue
        comparisons: dict[str, object] = {}
        for name in (
            "search_thumbnail",
            "preview",
            "light_http",
            "search_thumbnail_cold",
            "preview_cold",
            "light_http_cold",
            "search_thumbnail_hot",
            "preview_hot",
            "light_http_hot",
        ):
            base_metric = baseline_fairness.get(name)
            metric = fairness.get(name)
            if not isinstance(base_metric, Mapping) or not isinstance(metric, Mapping):
                continue
            base_p95 = base_metric.get("p95_ms")
            current_p95 = metric.get("p95_ms")
            if not isinstance(base_p95, (int, float)) or not isinstance(
                current_p95, (int, float)
            ):
                continue
            ratio = float(current_p95) / max(float(base_p95), 1e-9)
            comparisons[name] = {
                "p95_ratio": round(ratio, 4),
                "regressed_over_10_percent": ratio > 1.10,
            }
        result["relative_to_first_selected"] = comparisons


def _write_result(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _self_test() -> dict[str, object]:
    selected = _parse_combinations("10x2,16x12")
    if tuple(value.selector for value in selected) != ("10x2", "16x12"):
        raise AssertionError("combination parser failed")
    if _deterministic_id("matrix-seed-20260803", 0, "foreground.request") != (
        "b240bc7b-35a3-53c6-a33f-a48b2ca7bac8"
    ):
        raise AssertionError("deterministic ID derivation diverged from Android")
    if _id_sequence_sha256("matrix-seed-20260803", 2) != (
        "ced2815951e4f4f6954281d137d9cba79672dab1967bdd9f1df169cb61bcebde"
    ):
        raise AssertionError("ID sequence digest diverged from Android")
    sequence_sha = _id_sequence_sha256("tc007-self-test", 20)
    samples = [float(index) for index in range(1, 21)]
    double_samples = samples * 2
    if _latency_summary(samples)["p95_ms"] != 19.0:
        raise AssertionError("percentile calculation failed")
    fake = {
        "schema": RESULT_SCHEMA,
        ID_RESULT_FIELD: sequence_sha,
        "client_concurrency": 10,
        "requested_cycles": 20,
        "completed_foreground": 20,
        "planned_new_requests": 40,
        "timing_ms": {
            name: {"p50": 10.0, "p95": 20.0, "samples": samples}
            for name in (
                "foreground_create",
                "foreground_t4",
                "foreground_t6",
                "foreground_t15",
                "prepared_create",
                "prepared_preload_t4",
                "prepared_preload_t6",
                "prepared_preload_t15",
                "prepared_click_commit",
            )
        }
        | {
            name: {"p50": 10.0, "p95": 20.0, "samples": double_samples}
            for name in ("shown", "action", "status_under_contention")
        },
        "traffic_rx_bytes": 1,
        "coil": {
            "size_px": 640,
            "network_calls": 1,
            "content_length_bytes": 1,
            "unknown_content_lengths": 0,
            "invalid_content_lengths": 0,
            "cache_hit_reloads": 1,
            "same_url_size": True,
        },
        "resources": {
            "cpu_avg_core_percent": 1.0,
            "cpu_peak_core_percent": 2.0,
            "pss_before_kb": 1,
            "pss_after_kb": 1,
            "pss_peak_kb": 2,
        },
        "prepared": {
            "scope": "production_preloader_state_materialization_no_compose_paint",
            "created": 20,
            "shown_before_consume": 0,
            "duplicate_create_count": 0,
            "prepared_click_commit_ms": {
                "p50": 10.0,
                "p95": 20.0,
                "samples": samples,
            },
            "shown_after_consume": 20,
        },
        "cancel_probe": {
            "scope": "test_owned_coil_preload_job",
            "requested": True,
            "latency_ms": 1.0,
            "cancelled_calls": 1,
            "late_completions": 0,
            "queued_after": 0,
            "running_after": 0,
            "residual_calls": 0,
        },
        "errors": 0,
        "cancellations": 0,
    }
    text = f"noise\n{RESULT_PREFIX}{json.dumps(fake)}\nOK"
    parsed = _parse_instrumentation_output(text)
    sanitized = _sanitize_instrumentation_result(
        parsed,
        combination=Combination(10, 2),
        rounds=20,
        expected_id_sha256=sequence_sha,
    )
    if sanitized["errors"] != 0:
        raise AssertionError("instrumentation sanitizer failed")
    return {
        "ok": True,
        "checks": 6,
        "no_subprocesses_started": True,
        "no_filesystem_mutations": True,
    }


def _parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--combinations")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--id-seed", default=DEFAULT_ID_SEED)
    parser.add_argument("--snapshot-home", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--credential-store", type=Path)
    parser.add_argument("--paired-origin")
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument(
        "--instrumentation-source",
        type=Path,
        default=(
            repository_root
            / "android"
            / "app"
            / "src"
            / "androidTest"
            / "java"
            / "com"
            / "zvec"
            / "lanviewer"
            / "data"
            / "network"
            / "RecommendationNetworkBenchmarkTest.kt"
        ),
    )
    parser.add_argument("--instrumentation-class", default=INSTRUMENTATION_CLASS)
    parser.add_argument("--instrumentation-runner", default=INSTRUMENTATION_RUNNER)
    parser.add_argument("--instrumentation-timeout-seconds", type=float, default=1800)
    parser.add_argument("--control-groups", type=int)
    parser.add_argument("--control-interval-ms", type=float, default=20.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        combinations = _parse_combinations(args.combinations)
        if not 20 <= args.rounds <= 100:
            raise BenchmarkError("round count must be between 20 and 100")
        id_seed = _validate_id_seed(args.id_seed)
        expected_id_sha256 = _id_sequence_sha256(id_seed, args.rounds)
        contract = _instrumentation_contract(args.instrumentation_source)
        if args.self_test:
            print(json.dumps(_self_test(), sort_keys=True))
            return 0
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "schema": HOST_SCHEMA,
                        "mode": "dry_run",
                        "representative_combinations": [
                            {
                                "client_concurrency": value.client_concurrency,
                                "server_render_workers": value.server_render_workers,
                            }
                            for value in combinations
                        ],
                        "combination_count": len(combinations),
                        "cartesian_product": False,
                        "cycles_per_combination": args.rounds,
                        "planned_new_requests_per_combination": args.rounds * 2,
                        "id_sequence_sha256": expected_id_sha256,
                        "instrumentation_contract": contract,
                        "ready_for_real_run": all(contract.values()),
                        "subprocesses_started": 0,
                        "adb_invocations": 0,
                        "isolated_clones_created": 0,
                    },
                    sort_keys=True,
                )
            )
            return 0

        _require_instrumentation_contract(args.instrumentation_source)
        if os.name != "nt":
            raise BenchmarkError("real matrix orchestration requires Windows")
        required_paths = (
            args.snapshot_home,
            args.artifact_root,
            args.credential_store,
            args.output,
        )
        if any(path is None for path in required_paths) or args.paired_origin is None:
            raise BenchmarkError("real run arguments are incomplete")
        snapshot_home = args.snapshot_home.expanduser().resolve(strict=True)
        artifact_root = args.artifact_root.expanduser().resolve(strict=True)
        credential_store = args.credential_store.expanduser().resolve(strict=True)
        instrumentation_source = args.instrumentation_source.expanduser().resolve(
            strict=True
        )
        output_path = args.output.expanduser().resolve()
        if not snapshot_home.is_dir() or not artifact_root.is_dir():
            raise BenchmarkError("snapshot home and artifact root must be directories")
        if artifact_root == snapshot_home or artifact_root.is_relative_to(
            snapshot_home
        ):
            raise BenchmarkError("artifact root must be outside the source snapshot")
        if output_path.parent != artifact_root:
            raise BenchmarkError("output must be directly below artifact root")
        if output_path in {credential_store, instrumentation_source}:
            raise BenchmarkError("output must not overwrite an input file")
        if not credential_store.is_file():
            raise BenchmarkError("paired credential store is unavailable")
        paired_host, paired_port, paired_origin = _parse_paired_origin(
            args.paired_origin
        )
        bind_host = _validate_bind_host(args.bind_host)
        if bind_host not in {"0.0.0.0", paired_host}:
            raise BenchmarkError("bind host must be wildcard or the paired host")
        _require_adb_device(args.adb, args.serial)
        if args.instrumentation_timeout_seconds < 60:
            raise BenchmarkError("instrumentation timeout must be at least 60 seconds")
        control_groups = args.control_groups or max(args.rounds, 20)
        if not 1 <= control_groups <= 100:
            raise BenchmarkError("control groups must be between 1 and 100")
        if not 0 <= args.control_interval_ms <= 60_000:
            raise BenchmarkError("control interval is invalid")
        snapshot_before = _snapshot_signature(snapshot_home)
        results: list[dict[str, object]] = []
        for combination in combinations:
            results.append(
                _run_combination(
                    combination=combination,
                    snapshot_home=snapshot_home,
                    artifact_root=artifact_root,
                    credential_store_path=credential_store,
                    paired_host=paired_host,
                    paired_port=paired_port,
                    paired_origin=paired_origin,
                    bind_host=bind_host,
                    adb=args.adb,
                    serial=args.serial,
                    rounds=args.rounds,
                    id_seed=id_seed,
                    expected_id_sha256=expected_id_sha256,
                    instrumentation_class=args.instrumentation_class,
                    instrumentation_runner=args.instrumentation_runner,
                    instrumentation_timeout_seconds=(
                        args.instrumentation_timeout_seconds
                    ),
                    control_groups=control_groups,
                    control_interval_seconds=args.control_interval_ms / 1_000.0,
                )
            )
        snapshot_after = _snapshot_signature(snapshot_home)
        if snapshot_after != snapshot_before:
            raise BenchmarkError("source snapshot changed during the matrix")
        _fairness_regressions(results)
        payload = {
            "schema": HOST_SCHEMA,
            "matrix": {
                "representative_only": True,
                "combination_count": len(combinations),
                "cycles_per_combination": args.rounds,
                "planned_new_requests_per_combination": args.rounds * 2,
                "id_sequence_sha256": expected_id_sha256,
                "same_snapshot_for_every_combination": True,
                "fresh_recommendation_database_per_combination": True,
                "empty_image_cache_per_combination": True,
                "android_app_data_cleared": False,
                "android_coil_cache_cleared": False,
                "origin_disclosed": False,
                "credentials_disclosed": False,
                "snapshot_unchanged": True,
            },
            "instrumentation_contract": contract,
            "results": results,
        }
        _write_result(output_path, payload)
        print(
            json.dumps(
                {
                    "ok": True,
                    "completed_combinations": len(results),
                    "errors": sum(
                        int(
                            result.get("windows_fairness", {}).get("errors", 0)
                            if isinstance(result.get("windows_fairness"), Mapping)
                            else 0
                        )
                        for result in results
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
