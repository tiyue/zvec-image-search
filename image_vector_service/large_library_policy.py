"""Versioned, path-free diagnostics for bounded large-library behavior.

The values in this snapshot are operational policy, not user data.  Support
tools can compare the canonical digest across runs to prove that queueing,
pagination, batching, memory budgets, and deferred optimization are configured
consistently.  Constants are imported from their owning modules so diagnostics
cannot silently drift away from the implementation they describe.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Final

from .annotation_service import (
    FOLDER_INHERITANCE_AUDIT_LIMIT,
    FOLDER_INHERITANCE_GLOBAL_FILTER_SAMPLE_LIMIT,
    FOLDER_INHERITANCE_GLOBAL_SOURCE_SAMPLE_LIMIT,
    FOLDER_INHERITANCE_GLOBAL_TAG_LIMIT,
    FOLDER_INHERITANCE_ROW_BATCH_SIZE,
    FOLDER_INHERITANCE_TAG_LIMIT_PER_SOURCE,
)
from .collection_write_coordinator import DEFAULT_COLLECTION_WRITE_BATCH_SIZE
from .large_cluster_adapter import (
    DEFAULT_CLUSTER_TYPES,
    DEFAULT_LARGE_CLUSTER_READ_PAGE_SIZE,
    DEFAULT_LARGE_LIBRARY_THRESHOLD,
    MAX_LARGE_CLUSTER_READ_PAGE_SIZE,
)
from .large_image_clustering import (
    DEFAULT_LARGE_CLUSTER_STAGING_BATCH_SIZE,
    MAX_LARGE_CLUSTER_STAGING_BATCH_SIZE,
)
from .optimize_policy import OptimizePolicyConfig
from .scan_staging import (
    DEFAULT_SCAN_WRITE_BATCH_SIZE,
    MAX_SCAN_WRITE_BATCH_SIZE,
)
from .search_result_store import MAX_RESULT_PAGE_SIZE, RESULT_STORE_WRITE_BATCH
from .state import DEFAULT_STATE_READ_PAGE_SIZE, MAX_STATE_READ_PAGE_SIZE
from .zvec_repository import (
    DEFAULT_METADATA_READ_BATCH_SIZE,
    MAX_METADATA_READ_BATCH_SIZE,
)

LARGE_LIBRARY_POLICY_SCHEMA_VERSION: Final = 1


def build_large_library_policy_snapshot(
    *,
    queue_capacity_per_library: int,
    queue_max_capacity_per_library: int,
    queue_retry_after_seconds: float,
    cooperative_checkpoint_items: int,
    cooperative_max_checkpoint_items: int,
    cooperative_max_interval_seconds: float,
    cooperative_max_high_priority_calls: int,
    result_preview_page_size: int,
    optimize_idle_grace_seconds: float,
    optimize_config: OptimizePolicyConfig | None = None,
) -> dict[str, object]:
    """Return one JSON-safe snapshot containing no paths or credentials."""

    integer_values = {
        "queue_capacity_per_library": queue_capacity_per_library,
        "queue_max_capacity_per_library": queue_max_capacity_per_library,
        "cooperative_checkpoint_items": cooperative_checkpoint_items,
        "cooperative_max_checkpoint_items": cooperative_max_checkpoint_items,
        "cooperative_max_high_priority_calls": (cooperative_max_high_priority_calls),
        "result_preview_page_size": result_preview_page_size,
    }
    for name, value in integer_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if queue_capacity_per_library > queue_max_capacity_per_library:
        raise ValueError("queue capacity cannot exceed its configured maximum")
    if cooperative_checkpoint_items > cooperative_max_checkpoint_items:
        raise ValueError("cooperative checkpoint cannot exceed its maximum")
    numeric_values: dict[str, int | float] = {
        "queue_retry_after_seconds": queue_retry_after_seconds,
        "cooperative_max_interval_seconds": cooperative_max_interval_seconds,
        "optimize_idle_grace_seconds": optimize_idle_grace_seconds,
    }
    for numeric_name, numeric_value in numeric_values.items():
        if isinstance(numeric_value, bool) or not isinstance(
            numeric_value, (int, float)
        ):
            raise ValueError(f"{numeric_name} must be a positive finite number")
        if not math.isfinite(float(numeric_value)) or float(numeric_value) <= 0:
            raise ValueError(f"{numeric_name} must be a positive finite number")

    optimize = optimize_config or OptimizePolicyConfig()
    return {
        "schema_version": LARGE_LIBRARY_POLICY_SCHEMA_VERSION,
        "queue": {
            "bounded": True,
            "priority_scheduling": True,
            "capacity_per_library": queue_capacity_per_library,
            "maximum_capacity_per_library": queue_max_capacity_per_library,
            "retry_after_seconds": float(queue_retry_after_seconds),
            "cooperative_checkpoint_items": cooperative_checkpoint_items,
            "cooperative_maximum_checkpoint_items": (cooperative_max_checkpoint_items),
            "cooperative_maximum_interval_seconds": float(
                cooperative_max_interval_seconds
            ),
            "cooperative_maximum_high_priority_calls": (
                cooperative_max_high_priority_calls
            ),
        },
        "collection_writes": {
            "durable_outbox": True,
            "batch_size": DEFAULT_COLLECTION_WRITE_BATCH_SIZE,
        },
        "scan_and_reads": {
            "scan_write_batch_size": DEFAULT_SCAN_WRITE_BATCH_SIZE,
            "scan_read_page_size": DEFAULT_SCAN_WRITE_BATCH_SIZE,
            "scan_maximum_page_size": MAX_SCAN_WRITE_BATCH_SIZE,
            "state_read_page_size": DEFAULT_STATE_READ_PAGE_SIZE,
            "state_maximum_page_size": MAX_STATE_READ_PAGE_SIZE,
            "metadata_read_page_size": DEFAULT_METADATA_READ_BATCH_SIZE,
            "metadata_maximum_page_size": MAX_METADATA_READ_BATCH_SIZE,
            "large_cluster_read_page_size": (DEFAULT_LARGE_CLUSTER_READ_PAGE_SIZE),
            "large_cluster_maximum_read_page_size": (MAX_LARGE_CLUSTER_READ_PAGE_SIZE),
        },
        "results": {
            "preview_page_size": result_preview_page_size,
            "store_write_batch_size": RESULT_STORE_WRITE_BATCH,
            "store_maximum_read_page_size": MAX_RESULT_PAGE_SIZE,
        },
        "folder_inheritance": {
            "row_batch_size": FOLDER_INHERITANCE_ROW_BATCH_SIZE,
            "tag_limit_per_source": FOLDER_INHERITANCE_TAG_LIMIT_PER_SOURCE,
            "global_tag_limit": FOLDER_INHERITANCE_GLOBAL_TAG_LIMIT,
            "global_source_sample_limit": (
                FOLDER_INHERITANCE_GLOBAL_SOURCE_SAMPLE_LIMIT
            ),
            "global_filter_sample_limit": (
                FOLDER_INHERITANCE_GLOBAL_FILTER_SAMPLE_LIMIT
            ),
            "audit_sample_limit": FOLDER_INHERITANCE_AUDIT_LIMIT,
        },
        "large_clustering": {
            "entry_threshold_exclusive": DEFAULT_LARGE_LIBRARY_THRESHOLD,
            "default_types": list(DEFAULT_CLUSTER_TYPES),
            "semantic_enabled_by_default": "semantic" in DEFAULT_CLUSTER_TYPES,
            "staging_batch_size": DEFAULT_LARGE_CLUSTER_STAGING_BATCH_SIZE,
            "staging_maximum_batch_size": (MAX_LARGE_CLUSTER_STAGING_BATCH_SIZE),
            "external_model_requests": 0,
        },
        "optimize": {
            "deferred_until_idle": True,
            "change_threshold": optimize.change_threshold,
            "delete_count_threshold": optimize.delete_count_threshold,
            "delete_ratio_threshold": optimize.delete_ratio_threshold,
            "delete_ratio_minimum_count": optimize.delete_ratio_minimum_count,
            "maximum_interval_seconds": optimize.max_interval.total_seconds(),
            "idle_grace_seconds": float(optimize_idle_grace_seconds),
            "failure_backoff_initial_seconds": (
                optimize.failure_backoff_initial.total_seconds()
            ),
            "failure_backoff_maximum_seconds": (
                optimize.failure_backoff_maximum.total_seconds()
            ),
            "sqlite_timeout_seconds": optimize.sqlite_timeout_seconds,
        },
    }


def canonical_large_library_policy_sha256(snapshot: Mapping[str, object]) -> str:
    """Hash the canonical JSON representation used by support diagnostics."""

    encoded = json.dumps(
        dict(snapshot),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "LARGE_LIBRARY_POLICY_SCHEMA_VERSION",
    "build_large_library_policy_snapshot",
    "canonical_large_library_policy_sha256",
]
