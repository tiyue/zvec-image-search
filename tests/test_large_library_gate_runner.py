from __future__ import annotations

import unittest

from scripts import run_large_library_gates as gates


class LargeLibraryGateRunnerTests(unittest.TestCase):
    def test_pr_manifest_covers_every_required_invariant_without_million_gate(
        self,
    ) -> None:
        selected = gates.select_gates(profile="pr", environ={})

        self.assertEqual(
            {gate.key for gate in selected},
            gates.REQUIRED_PR_GATE_KEYS,
        )
        self.assertFalse(
            any(
                "one_million" in test_id
                for gate in selected
                for test_id in gate.test_ids
            )
        )

    def test_million_execution_requires_explicit_environment_opt_in(self) -> None:
        with self.assertRaisesRegex(
            gates.GateConfigurationError,
            gates.MILLION_GATE_ENV,
        ):
            gates.select_gates(profile="nightly", environ={})

        selected = gates.select_gates(
            profile="pr",
            million=True,
            environ={gates.MILLION_GATE_ENV: "1"},
        )
        self.assertEqual(selected[-2:], gates.MILLION_GATES)

    def test_dry_run_manifest_resolves_every_test_exactly_once(self) -> None:
        selected = gates.select_gates(
            profile="nightly",
            environ={},
            require_million_opt_in=False,
        )

        gates.validate_gate_plan(selected)

    def test_structured_summary_exposes_ci_accounting_fields(self) -> None:
        summary = gates.GateRunSummary(
            profile="pr",
            million_enabled=False,
            gates_selected=1,
            tests_selected=2,
            executions=(
                gates.GateExecution(
                    key="bounded-example",
                    duration_seconds=1.25,
                    tests_run=2,
                    skipped=0,
                    failures=0,
                    errors=0,
                    successful=True,
                ),
            ),
        )

        payload = summary.to_payload()

        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["tests_selected"], 2)
        self.assertEqual(payload["tests_run"], 2)
        self.assertEqual(payload["skipped"], 0)
        self.assertEqual(payload["duration_seconds"], 1.25)
        self.assertFalse(payload["million_enabled"])


if __name__ == "__main__":
    unittest.main()
