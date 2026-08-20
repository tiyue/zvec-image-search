"""Repeatable synthetic benchmark for recommendation selection.

Candidate construction is excluded from timings. The benchmark uses only local,
synthetic vectors and never opens a Collection or calls a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from image_vector_service.recommendations import (  # noqa: E402
    SLOT_QUOTAS,
    RecommendationCandidate,
    RecommendationSelection,
    select_recommendations,
)


@dataclass(frozen=True, slots=True)
class RecommendationBenchmarkResult:
    candidate_count: int
    dimension: int
    seed: int
    warmups: int
    rounds: int
    median_ms: float
    p95_ms: float
    minimum_ms: float
    maximum_ms: float
    selection_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_synthetic_candidates(
    candidate_count: int,
    *,
    dimension: int,
    seed: int,
) -> tuple[RecommendationCandidate, ...]:
    """Build deterministic clustered vectors without including setup in timings."""

    _validate_positive(candidate_count, "candidate_count", maximum=10_000)
    _validate_positive(dimension, "dimension", maximum=8_192)
    generator = random.Random(seed)
    prototype_count = min(32, candidate_count)
    prototypes = tuple(
        tuple(generator.uniform(-1.0, 1.0) for _axis in range(dimension))
        for _prototype in range(prototype_count)
    )
    candidates: list[RecommendationCandidate] = []
    for index in range(candidate_count):
        prototype = prototypes[index % prototype_count]
        vector = tuple(
            value + 0.0001 * math.sin((index + 1) * (axis + 1))
            for axis, value in enumerate(prototype)
        )
        candidates.append(
            RecommendationCandidate(
                candidate_id=f"library:doc-{index:05d}",
                doc_id=f"doc-{index:05d}",
                sha256=f"sha-{index:05d}",
                width=2_400 + index % 17,
                height=1_600 + index % 13,
                mtime_ns=candidate_count - index,
                exposure_count=index % 4,
                album_id=f"album-{index // 4:05d}",
                character=f"character-{index % 64:02d}",
                vector=vector,
                personalization_score=((index % 11) - 5) / 100.0,
            )
        )
    return tuple(candidates)


def run_recommendation_benchmark(
    *,
    candidate_count: int,
    dimension: int,
    seed: int,
    warmups: int,
    rounds: int,
) -> RecommendationBenchmarkResult:
    """Measure repeated pure selection and enforce deterministic correctness."""

    _validate_positive(warmups, "warmups", maximum=100)
    _validate_positive(rounds, "rounds", maximum=1_000)
    candidates = build_synthetic_candidates(
        candidate_count,
        dimension=dimension,
        seed=seed,
    )
    expected_digest: str | None = None
    for _index in range(warmups):
        selection = select_recommendations(candidates, rng_seed=seed)
        expected_digest = _validate_selection(selection, expected_digest)

    samples_ms: list[float] = []
    for _index in range(rounds):
        started = time.perf_counter()
        selection = select_recommendations(candidates, rng_seed=seed)
        samples_ms.append((time.perf_counter() - started) * 1_000.0)
        expected_digest = _validate_selection(selection, expected_digest)

    assert expected_digest is not None
    ordered = sorted(samples_ms)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return RecommendationBenchmarkResult(
        candidate_count=candidate_count,
        dimension=dimension,
        seed=seed,
        warmups=warmups,
        rounds=rounds,
        median_ms=round(statistics.median(samples_ms), 3),
        p95_ms=round(ordered[p95_index], 3),
        minimum_ms=round(min(samples_ms), 3),
        maximum_ms=round(max(samples_ms), 3),
        selection_digest=expected_digest,
    )


def _validate_selection(
    selection: RecommendationSelection,
    expected_digest: str | None,
) -> str:
    if selection.status != "complete" or len(selection.items) != sum(
        SLOT_QUOTAS.values()
    ):
        raise RuntimeError("recommendation benchmark produced an incomplete selection")
    if dict(selection.counts_by_slot) != dict(SLOT_QUOTAS):
        raise RuntimeError("recommendation benchmark violated the fixed slot quotas")
    identities = tuple(
        f"{item.slot}:{item.candidate.candidate_id}" for item in selection.items
    )
    if len(identities) != len(set(identities)):
        raise RuntimeError("recommendation benchmark produced duplicate candidates")
    digest = hashlib.sha256("\n".join(identities).encode()).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise RuntimeError("recommendation selection changed between identical rounds")
    return digest


def _validate_positive(value: int, label: str, *, maximum: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-counts",
        nargs="+",
        type=int,
        default=(300, 768),
    )
    parser.add_argument("--dimension", type=int, default=1_024)
    parser.add_argument("--seed", type=int, default=20_260_803)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        help="Optional P95 gate applied to the largest candidate-count scenario.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        results = [
            run_recommendation_benchmark(
                candidate_count=candidate_count,
                dimension=args.dimension,
                seed=args.seed,
                warmups=args.warmups,
                rounds=args.rounds,
            )
            for candidate_count in args.candidate_counts
        ]
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Recommendation benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"scenarios": [result.to_dict() for result in results]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if args.max_p95_ms is not None:
        largest = max(results, key=lambda result: result.candidate_count)
        if largest.p95_ms > args.max_p95_ms:
            print(
                f"Recommendation benchmark gate failed: {largest.p95_ms:.3f} ms "
                f"> {args.max_p95_ms:.3f} ms",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
