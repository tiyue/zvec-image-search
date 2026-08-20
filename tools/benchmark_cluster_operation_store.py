"""Measure SQLite cluster snapshot persistence and bounded page reads.

The benchmark uses synthetic metadata and already-existing vector version names.
It does not construct a model client, generate embeddings, or make network calls.
Run the normal regression-sized case with::

    python tools/benchmark_cluster_operation_store.py --items 10000

The 100k case is intentionally opt-in because tracing Python allocations adds
substantial overhead::

    python tools/benchmark_cluster_operation_store.py --items 100000
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from image_vector_service.cluster_operation_store import (  # noqa: E402
    ClusterOperationStore,
)
from image_vector_service.image_clustering import (  # noqa: E402
    ClusterEdge,
    ClusterItemState,
    ClusterSnapshot,
    ImageCluster,
)

MIB = 1024 * 1024
MAX_BENCHMARK_ITEMS = 100_000


@dataclass(frozen=True, slots=True)
class ClusterStoreBenchmarkResult:
    """Serializable measurements from one isolated benchmark database."""

    item_count: int
    cluster_count: int
    edge_count: int
    page_offset: int
    page_limit: int
    page_returned: int
    page_total_count: int
    page_has_more: bool
    page_first_doc_id: str | None
    page_last_doc_id: str | None
    snapshot_build_seconds: float
    snapshot_save_seconds: float
    snapshot_total_seconds: float
    snapshot_peak_memory_bytes: int
    page_seconds: float
    page_peak_memory_bytes: int
    page_payload_bytes: int
    database_bytes: int
    external_api_calls: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["snapshot_peak_memory_mib"] = round(
            self.snapshot_peak_memory_bytes / MIB, 3
        )
        payload["page_peak_memory_mib"] = round(self.page_peak_memory_bytes / MIB, 3)
        payload["database_mib"] = round(self.database_bytes / MIB, 3)
        return payload


def build_synthetic_snapshot(
    item_count: int,
    *,
    cluster_size: int = 25,
) -> ClusterSnapshot:
    """Create deterministic groups and local edges without model involvement."""

    _validate_positive(item_count, "item_count", maximum=MAX_BENCHMARK_ITEMS)
    _validate_positive(cluster_size, "cluster_size", maximum=item_count)
    items: list[ClusterItemState] = []
    edges: list[ClusterEdge] = []
    clusters: list[ImageCluster] = []
    for cluster_start in range(0, item_count, cluster_size):
        cluster_index = cluster_start // cluster_size
        cluster_id = f"cluster-{cluster_index:06d}"
        member_ids = tuple(
            f"doc-{item_index:06d}"
            for item_index in range(
                cluster_start, min(item_count, cluster_start + cluster_size)
            )
        )
        clusters.append(
            ImageCluster(
                cluster_id=cluster_id,
                member_doc_ids=member_ids,
                edge_kinds=("exact",) if len(member_ids) > 1 else (),
            )
        )
        items.extend(
            ClusterItemState(
                doc_id=doc_id,
                fingerprint=f"{item_index:064x}",
                cluster_id=cluster_id,
            )
            for item_index, doc_id in enumerate(member_ids, start=cluster_start)
        )
        edges.extend(
            ClusterEdge(member_ids[index - 1], member_ids[index], "exact", 1.0)
            for index in range(1, len(member_ids))
        )
    return ClusterSnapshot(
        algorithm_version="cluster-store-scale-v1",
        embedding_version="existing-vectors-only",
        items=tuple(items),
        edges=tuple(edges),
        clusters=tuple(clusters),
    )


def run_cluster_store_benchmark(
    database_path: str | Path,
    *,
    item_count: int,
    cluster_size: int = 25,
    page_limit: int = 257,
    page_offset: int | None = None,
) -> ClusterStoreBenchmarkResult:
    """Persist one synthetic snapshot and read one bounded middle page."""

    _validate_positive(item_count, "item_count", maximum=MAX_BENCHMARK_ITEMS)
    _validate_positive(cluster_size, "cluster_size", maximum=item_count)
    _validate_positive(page_limit, "page_limit", maximum=2_000)
    resolved_database = Path(database_path).expanduser().resolve()
    if resolved_database.exists():
        raise FileExistsError(
            f"Benchmark database already exists; refusing to overwrite: "
            f"{resolved_database}"
        )
    resolved_offset = item_count // 2 if page_offset is None else page_offset
    if isinstance(resolved_offset, bool) or not 0 <= resolved_offset < item_count:
        raise ValueError("page_offset must refer to an item in the snapshot")

    store = ClusterOperationStore(resolved_database, recover_interrupted=False)
    if tracemalloc.is_tracing():
        raise RuntimeError("The cluster benchmark requires exclusive tracemalloc use")

    tracemalloc.start()
    try:
        build_started = time.perf_counter()
        snapshot = build_synthetic_snapshot(item_count, cluster_size=cluster_size)
        build_seconds = time.perf_counter() - build_started
        _, build_peak = tracemalloc.get_traced_memory()

        tracemalloc.reset_peak()
        save_started = time.perf_counter()
        record = store.save_snapshot(
            "scale-library",
            snapshot,
            snapshot_version=f"scale-{item_count}",
            metadata={"synthetic": True, "model_calls": 0},
        )
        save_seconds = time.perf_counter() - save_started
        _, save_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    cluster_count = len(snapshot.clusters)
    edge_count = len(snapshot.edges)
    del snapshot
    gc.collect()

    tracemalloc.start()
    try:
        page_started = time.perf_counter()
        page = store.page_snapshot_items(
            record.snapshot_version,
            offset=resolved_offset,
            limit=page_limit,
        )
        page_seconds = time.perf_counter() - page_started
        _, page_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    page_items = page["items"]
    if not isinstance(page_items, list):
        raise RuntimeError("Cluster snapshot page returned an invalid items payload")
    page_payload_bytes = len(
        json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return ClusterStoreBenchmarkResult(
        item_count=record.item_count,
        cluster_count=cluster_count,
        edge_count=edge_count,
        page_offset=resolved_offset,
        page_limit=page_limit,
        page_returned=len(page_items),
        page_total_count=int(page["total_count"]),
        page_has_more=bool(page["has_more"]),
        page_first_doc_id=(str(page_items[0]["doc_id"]) if page_items else None),
        page_last_doc_id=(str(page_items[-1]["doc_id"]) if page_items else None),
        snapshot_build_seconds=build_seconds,
        snapshot_save_seconds=save_seconds,
        snapshot_total_seconds=build_seconds + save_seconds,
        snapshot_peak_memory_bytes=max(build_peak, save_peak),
        page_seconds=page_seconds,
        page_peak_memory_bytes=page_peak,
        page_payload_bytes=page_payload_bytes,
        database_bytes=resolved_database.stat().st_size,
        external_api_calls=store.external_api_calls,
    )


def _validate_positive(value: int, label: str, *, maximum: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--items",
        type=int,
        choices=(10_000, 100_000),
        default=10_000,
        help="Synthetic image count; 100000 is the explicit slow benchmark.",
    )
    parser.add_argument("--cluster-size", type=int, default=25)
    parser.add_argument("--page-limit", type=int, default=257)
    parser.add_argument(
        "--database",
        type=Path,
        help="Optional new SQLite path to retain; an existing file is never replaced.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.database is not None:
            result = run_cluster_store_benchmark(
                args.database,
                item_count=args.items,
                cluster_size=args.cluster_size,
                page_limit=args.page_limit,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="zvec-cluster-benchmark-") as root:
                result = run_cluster_store_benchmark(
                    Path(root) / "cluster-operations.sqlite3",
                    item_count=args.items,
                    cluster_size=args.cluster_size,
                    page_limit=args.page_limit,
                )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cluster store benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
