from __future__ import annotations

import contextlib
import io
import json
import unittest

from tools.benchmark_recommendations import (
    build_synthetic_candidates,
    main,
    run_recommendation_benchmark,
)


class RecommendationBenchmarkTest(unittest.TestCase):
    def test_synthetic_candidates_and_selection_digest_are_deterministic(
        self,
    ) -> None:
        first = build_synthetic_candidates(64, dimension=8, seed=17)
        second = build_synthetic_candidates(64, dimension=8, seed=17)

        self.assertEqual(first, second)
        benchmark = run_recommendation_benchmark(
            candidate_count=64,
            dimension=8,
            seed=17,
            warmups=1,
            rounds=3,
        )

        self.assertEqual(benchmark.candidate_count, 64)
        self.assertEqual(benchmark.dimension, 8)
        self.assertEqual(len(benchmark.selection_digest), 64)
        self.assertLessEqual(0, benchmark.minimum_ms)
        self.assertLessEqual(benchmark.minimum_ms, benchmark.median_ms)
        self.assertLessEqual(benchmark.median_ms, benchmark.maximum_ms)
        self.assertLessEqual(benchmark.minimum_ms, benchmark.p95_ms)
        self.assertLessEqual(benchmark.p95_ms, benchmark.maximum_ms)

    def test_cli_emits_json_and_enforces_the_optional_p95_gate(self) -> None:
        arguments = [
            "--candidate-counts",
            "64",
            "--dimension",
            "8",
            "--seed",
            "17",
            "--warmups",
            "1",
            "--rounds",
            "2",
            "--max-p95-ms",
            "100000",
        ]

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["scenarios"][0]["candidate_count"], 64)

        arguments[-1] = "0"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        self.assertEqual(result, 2)
        self.assertIn("gate failed", stderr.getvalue())

    def test_rejects_invalid_synthetic_dimensions(self) -> None:
        for candidate_count, dimension in ((0, 8), (64, 0)):
            with (
                self.subTest(
                    candidate_count=candidate_count,
                    dimension=dimension,
                ),
                self.assertRaises(ValueError),
            ):
                build_synthetic_candidates(
                    candidate_count,
                    dimension=dimension,
                    seed=17,
                )
