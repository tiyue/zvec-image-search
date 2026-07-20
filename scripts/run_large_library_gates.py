from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MILLION_GATE_ENV = "ZVEC_RUN_MILLION_SCAN_STAGING_TEST"


@dataclass(frozen=True, slots=True)
class LargeLibraryGate:
    """One visible performance invariant backed by deterministic unit tests."""

    key: str
    description: str
    test_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateExecution:
    key: str
    duration_seconds: float
    tests_run: int
    skipped: int
    failures: int
    errors: int
    successful: bool


@dataclass(frozen=True, slots=True)
class GateRunSummary:
    profile: str
    million_enabled: bool
    gates_selected: int
    tests_selected: int
    executions: tuple[GateExecution, ...]

    @property
    def successful(self) -> bool:
        return bool(self.executions) and all(
            execution.successful for execution in self.executions
        )

    def to_payload(self) -> dict[str, object]:
        duration = sum(item.duration_seconds for item in self.executions)
        return {
            "schema_version": 1,
            "status": "passed" if self.successful else "failed",
            "profile": self.profile,
            "million_enabled": self.million_enabled,
            "gates_selected": self.gates_selected,
            "gates_run": len(self.executions),
            "tests_selected": self.tests_selected,
            "tests_run": sum(item.tests_run for item in self.executions),
            "skipped": sum(item.skipped for item in self.executions),
            "failures": sum(item.failures for item in self.executions),
            "errors": sum(item.errors for item in self.executions),
            "duration_seconds": round(duration, 6),
            "gates": [
                {
                    "key": item.key,
                    "duration_seconds": round(item.duration_seconds, 6),
                    "tests_run": item.tests_run,
                    "skipped": item.skipped,
                    "failures": item.failures,
                    "errors": item.errors,
                    "status": "passed" if item.successful else "failed",
                }
                for item in self.executions
            ],
        }


PR_GATES: tuple[LargeLibraryGate, ...] = (
    LargeLibraryGate(
        key="architecture_policy",
        description="source guardrails and runtime policy diagnostics cannot drift",
        test_ids=(
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_repository_mutation_guard_detects_direct_and_dynamic_bypasses",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_large_service_paths_never_use_materializing_state_apis",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_folder_failure_inheritance_never_falls_back_to_full_library",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_optimize_is_confined_to_idle_maintenance_and_migration",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_collection_writes_use_the_durable_coordinator",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_collection_write_batches_are_bounded_between_100_and_500",
            "tests.test_large_library_architecture_guardrails."
            "LargeLibraryArchitectureGuardrailTests."
            "test_desktop_search_is_source_only_and_persisted_as_pages",
            "tests.test_large_library_policy.LargeLibraryPolicySnapshotTests."
            "test_snapshot_reports_authoritative_bounded_runtime_policy",
            "tests.test_large_library_policy.LargeLibraryPolicySnapshotTests."
            "test_snapshot_is_small_canonical_and_contains_no_sensitive_field",
            "tests.test_large_library_policy.LargeLibraryPolicySnapshotTests."
            "test_invalid_runtime_values_are_rejected",
            "tests.test_large_library_policy.BackendStartupPolicyLogTests."
            "test_startup_event_uses_the_same_snapshot_and_digest",
        ),
    ),
    LargeLibraryGate(
        key="paged_results",
        description="100k search results use bounded random-access pages",
        test_ids=(
            "tests.test_paged_search_results.PagedSearchResultTests."
            "test_paged_manifest_reads_only_requested_rank_range",
        ),
    ),
    LargeLibraryGate(
        key="scan_staging",
        description="100k scan rows and large duplicate groups remain batch bounded",
        test_ids=(
            "tests.test_scan_staging.ScanStagingScaleTests."
            "test_large_duplicate_group_is_reloaded_in_bounded_pages",
            "tests.test_scan_staging.ScanStagingScaleTests."
            "test_one_hundred_thousand_rows_remain_batch_bounded",
        ),
    ),
    LargeLibraryGate(
        key="streaming_index",
        description="streaming index deduplicates model work and pages unchanged rows",
        test_ids=(
            "tests.test_streaming_index_integration."
            "StreamingIndexIntegrationTests."
            "test_duplicate_group_embeds_once_writes_bounded_and_callbacks_once",
            "tests.test_streaming_index_integration."
            "StreamingIndexIntegrationTests."
            "test_hundred_thousand_unchanged_records_use_page_bounded_state_reads",
        ),
    ),
    LargeLibraryGate(
        key="tag_top_n",
        description="100k tag matches materialize only the requested Top-N rows",
        test_ids=(
            "tests.test_tag_rank_query.RankedTagQueryTest."
            "test_large_match_set_materializes_only_requested_rows",
        ),
    ),
    LargeLibraryGate(
        key="large_cluster",
        description="100k clustering inputs keep reads and snapshots bounded",
        test_ids=(
            "tests.test_large_cluster_adapter.LargeClusterAdapterScaleTests."
            "test_one_hundred_thousand_mock_entries_keep_pages_and_memory_bounded",
            "tests.test_large_image_clustering.LargeImageClusteringScaleTests."
            "test_one_hundred_thousand_inputs_stay_bounded_and_do_not_build_snapshot",
        ),
    ),
    LargeLibraryGate(
        key="bounded_maintenance",
        description="metadata backfill and root rebind use bounded keyset pages",
        test_ids=(
            "tests.test_bounded_maintenance_service."
            "BoundedMetadataBackfillServiceTest."
            "test_100k_current_documents_use_only_bounded_page_reads",
            "tests.test_bounded_maintenance_service.BoundedRebindServiceTest."
            "test_missing_file_validation_uses_bounded_root_pages",
        ),
    ),
    LargeLibraryGate(
        key="folder_inheritance",
        description="folder tag inheritance retains a fixed global memory budget",
        test_ids=(
            "tests.test_annotation_service.FolderInheritanceAggregateTest."
            "test_one_hundred_thousand_donors_keep_only_bounded_audit_samples",
            "tests.test_annotation_service.FolderInheritanceAggregateTest."
            "test_one_hundred_thousand_unique_tags_stop_at_per_source_limit",
        ),
    ),
    LargeLibraryGate(
        key="collection_outbox",
        description="Outbox recovery is model-free and ignores old applied history",
        test_ids=(
            "tests.test_collection_write_outbox.CollectionWriteOutboxTest."
            "test_interrupted_upsert_replays_without_a_model_call",
            "tests.test_collection_write_outbox."
            "CollectionWriteOutboxMigrationAndScaleTest."
            "test_hundred_thousand_applied_rows_do_not_make_recovery_scan_full_table",
        ),
    ),
    LargeLibraryGate(
        key="idle_optimize",
        description="Collection optimize is thresholded and runs only while idle",
        test_ids=(
            "tests.test_optimize_policy.OptimizePolicyStoreTest."
            "test_one_hundred_thousand_accumulated_changes_trigger_once",
            "tests.test_optimize_policy.OptimizeRuntimeGateTest."
            "test_busy_queue_defers_optional_maintenance",
            "tests.test_optimize_policy_service_integration."
            "OptimizePolicyServiceIntegrationTests."
            "test_due_changes_optimize_once_and_clear_the_pending_hint",
        ),
    ),
    LargeLibraryGate(
        key="scheduler_backpressure",
        description="bounded queues return 429 and let search preempt long work",
        test_ids=(
            "tests.test_backend_scheduler_backpressure."
            "BackendSchedulerBackpressureTests."
            "test_index_yields_to_search_without_concurrent_collection_access",
            "tests.test_backend_scheduler_backpressure."
            "BackendSchedulerBackpressureTests."
            "test_http_queue_full_returns_429_retry_after_and_capacity_details",
        ),
    ),
)

MILLION_GATES: tuple[LargeLibraryGate, ...] = (
    LargeLibraryGate(
        key="million_paged_results",
        description="one million stored results support bounded page and cursor reads",
        test_ids=(
            "tests.test_paged_search_results.PagedSearchResultTests."
            "test_million_result_store_supports_bounded_range_and_cursor_reads",
        ),
    ),
    LargeLibraryGate(
        key="million_scan_staging",
        description="one million staged scan rows retain the 256-row memory bound",
        test_ids=(
            "tests.test_scan_staging.ScanStagingScaleTests."
            "test_one_million_rows_remain_batch_bounded",
        ),
    ),
)

REQUIRED_PR_GATE_KEYS = frozenset(
    {
        "architecture_policy",
        "paged_results",
        "scan_staging",
        "streaming_index",
        "tag_top_n",
        "large_cluster",
        "bounded_maintenance",
        "folder_inheritance",
        "collection_outbox",
        "idle_optimize",
        "scheduler_backpressure",
    }
)


class GateConfigurationError(ValueError):
    """Raised before test loading when a costly gate was not opted into."""


def select_gates(
    *,
    profile: str,
    million: bool = False,
    environ: Mapping[str, str] | None = None,
    require_million_opt_in: bool = True,
) -> tuple[LargeLibraryGate, ...]:
    """Return the deterministic gate plan for a PR or nightly invocation."""

    if profile not in {"pr", "nightly"}:
        raise GateConfigurationError(f"unsupported gate profile: {profile}")
    include_million = million or profile == "nightly"
    if include_million and require_million_opt_in:
        environment = os.environ if environ is None else environ
        if environment.get(MILLION_GATE_ENV) != "1":
            raise GateConfigurationError(
                f"million-row gates require {MILLION_GATE_ENV}=1"
            )
    return PR_GATES + (MILLION_GATES if include_million else ())


def validate_gate_plan(gates: Sequence[LargeLibraryGate]) -> None:
    """Fail early when a renamed test leaves the curated CI manifest stale."""

    if not gates:
        raise GateConfigurationError("the selected large-library gate plan is empty")
    keys = [gate.key for gate in gates]
    if len(keys) != len(set(keys)):
        raise GateConfigurationError("large-library gate keys must be unique")

    for gate in gates:
        if not gate.test_ids:
            raise GateConfigurationError(f"gate {gate.key!r} has no tests")
        for test_id in gate.test_ids:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromName(test_id)
            if loader.errors:
                details = "\n".join(str(error) for error in loader.errors)
                raise GateConfigurationError(
                    f"cannot load curated test {test_id!r}:\n{details}"
                )
            if suite.countTestCases() != 1:
                raise GateConfigurationError(
                    f"curated test {test_id!r} resolved to "
                    f"{suite.countTestCases()} test cases instead of one"
                )


def _print_plan(gates: Sequence[LargeLibraryGate]) -> None:
    total = sum(len(gate.test_ids) for gate in gates)
    print(f"Large-library gate plan: {len(gates)} gates, {total} tests")
    for gate in gates:
        print(f"- {gate.key}: {gate.description}")
        for test_id in gate.test_ids:
            print(f"    {test_id}")


def run_gates(
    gates: Sequence[LargeLibraryGate],
    *,
    profile: str,
    million_enabled: bool,
    verbosity: int = 2,
    fail_fast: bool = False,
) -> GateRunSummary:
    """Run every category separately so CI exposes the regressed invariant."""

    executions: list[GateExecution] = []
    for gate in gates:
        print(f"\n=== {gate.key}: {gate.description} ===", flush=True)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(list(gate.test_ids))
        started = time.monotonic()
        result = unittest.TextTestRunner(
            stream=sys.stdout,
            verbosity=verbosity,
            failfast=fail_fast,
        ).run(suite)
        duration = time.monotonic() - started
        # A curated gate must execute. A skip is usually a missing opt-in or a
        # platform regression, so it is a failure rather than silent coverage.
        successful = result.wasSuccessful() and not result.skipped
        executions.append(
            GateExecution(
                key=gate.key,
                duration_seconds=duration,
                tests_run=result.testsRun,
                skipped=len(result.skipped),
                failures=len(result.failures),
                errors=len(result.errors),
                successful=successful,
            )
        )
        if result.skipped:
            print(
                f"ERROR: {gate.key} skipped {len(result.skipped)} selected test(s).",
                file=sys.stderr,
            )
        if fail_fast and not successful:
            break

    summary = GateRunSummary(
        profile=profile,
        million_enabled=million_enabled,
        gates_selected=len(gates),
        tests_selected=sum(len(gate.test_ids) for gate in gates),
        executions=tuple(executions),
    )
    payload = summary.to_payload()
    print("\nLarge-library gate timings:")
    for execution in executions:
        print(
            f"- {execution.key}: {execution.duration_seconds:.3f}s "
            f"({execution.tests_run} tests)"
        )
    print(
        "Large-library summary: "
        f"status={payload['status']} "
        f"tests={payload['tests_selected']} "
        f"run={payload['tests_run']} "
        f"skipped={payload['skipped']} "
        f"duration={payload['duration_seconds']}s "
        f"million_enabled={str(million_enabled).lower()}"
    )
    print(
        "LARGE_LIBRARY_GATE_SUMMARY="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return summary


def write_json_report(path: Path, summary: GateRunSummary) -> None:
    """Write an optional machine-readable artifact without hiding stdout data."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(summary.to_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise GateConfigurationError(
            f"cannot write gate JSON report {path}: {exc}"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic, model-free performance gates for large libraries."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("pr", "nightly"),
        default="pr",
        help="PR runs 100k gates; nightly also includes million-row gates.",
    )
    parser.add_argument(
        "--million",
        action="store_true",
        help=(f"Add million-row gates. Execution requires {MILLION_GATE_ENV}=1."),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the selected tests without executing them.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the selected gate manifest and exit without loading tests.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing gate category.",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Optionally write the final structured summary to this path.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Use compact unittest output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        gates = select_gates(
            profile=args.profile,
            million=args.million,
            require_million_opt_in=not (args.dry_run or args.list),
        )
        if args.list:
            _print_plan(gates)
            return 0

        # unittest module names are repository-relative. Normalizing the
        # process here also makes the script safe to invoke from another cwd.
        os.chdir(REPOSITORY_ROOT)
        repository = str(REPOSITORY_ROOT)
        if repository not in sys.path:
            sys.path.insert(0, repository)
        validate_gate_plan(gates)
        _print_plan(gates)
        if args.dry_run:
            print("Dry run passed; every curated test resolved exactly once.")
            return 0
        million_enabled = args.million or args.profile == "nightly"
        summary = run_gates(
            gates,
            profile=args.profile,
            million_enabled=million_enabled,
            verbosity=1 if args.quiet else 2,
            fail_fast=args.fail_fast,
        )
        if args.json_report is not None:
            write_json_report(args.json_report, summary)
        return 0 if summary.successful else 1
    except GateConfigurationError as exc:
        print(f"large-library gate configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("large-library gates interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
