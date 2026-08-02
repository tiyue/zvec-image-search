from __future__ import annotations

import json

import pytest

from tools.benchmark_recommendations import (
    build_synthetic_candidates,
    main,
    run_recommendation_benchmark,
)


def test_synthetic_candidates_and_selection_digest_are_deterministic() -> None:
    first = build_synthetic_candidates(64, dimension=8, seed=17)
    second = build_synthetic_candidates(64, dimension=8, seed=17)

    assert first == second
    benchmark = run_recommendation_benchmark(
        candidate_count=64,
        dimension=8,
        seed=17,
        warmups=1,
        rounds=3,
    )

    assert benchmark.candidate_count == 64
    assert benchmark.dimension == 8
    assert len(benchmark.selection_digest) == 64
    assert 0 <= benchmark.minimum_ms <= benchmark.median_ms <= benchmark.maximum_ms
    assert benchmark.minimum_ms <= benchmark.p95_ms <= benchmark.maximum_ms


def test_cli_emits_json_and_enforces_the_optional_p95_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
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

    assert main(arguments) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenarios"][0]["candidate_count"] == 64

    arguments[-1] = "0"
    assert main(arguments) == 2
    assert "gate failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("candidate_count", "dimension"),
    ((0, 8), (64, 0)),
)
def test_rejects_invalid_synthetic_dimensions(
    candidate_count: int,
    dimension: int,
) -> None:
    with pytest.raises(ValueError):
        build_synthetic_candidates(
            candidate_count,
            dimension=dimension,
            seed=17,
        )
