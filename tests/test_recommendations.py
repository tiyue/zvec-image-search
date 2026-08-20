from __future__ import annotations

import hashlib
import math
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import image_vector_service.recommendations as recommendation_module
from image_vector_service.recommendations import (
    PreferenceVector,
    PreparedPersonalizationProfile,
    RecommendationCandidate,
    _ranked_for_slot,
    _similarity_penalty,
    _slot_personalization_score,
    apply_personalization_profile,
    personalize_candidates,
    prepare_personalization_profile,
    select_recommendations,
)
from tools.benchmark_recommendations import build_synthetic_candidates


def _candidate(
    number: int,
    *,
    sha: str | None = None,
    width: int = 2_000,
    height: int = 1_000,
    mtime_ns: int | None = None,
    exposure_count: int = 0,
    album_id: str | None = None,
    character: str | None = None,
    vector: tuple[float, ...] | None = (1.0, 0.0),
) -> RecommendationCandidate:
    return RecommendationCandidate(
        candidate_id=f"library-a:doc-{number}",
        doc_id=f"doc-{number}",
        sha256=sha or f"sha-{number}",
        width=width,
        height=height,
        mtime_ns=number if mtime_ns is None else mtime_ns,
        exposure_count=exposure_count,
        album_id=album_id,
        character=character,
        vector=vector,
    )


def _selection_digest(candidate_count: int) -> str:
    selection = select_recommendations(
        build_synthetic_candidates(
            candidate_count,
            dimension=1_024,
            seed=20_260_803,
        ),
        rng_seed=20_260_803,
    )
    identities = (
        f"{item.slot}:{item.candidate.candidate_id}" for item in selection.items
    )
    return hashlib.sha256("\n".join(identities).encode()).hexdigest()


class RecommendationSelectionTests(unittest.TestCase):
    def test_returns_the_fixed_five_six_four_quota_without_recent(self) -> None:
        candidates = [_candidate(number) for number in range(30)]

        result = select_recommendations(candidates, rng_seed=7)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.items), 15)
        self.assertEqual(
            result.counts_by_slot,
            {
                "quality": 5,
                "low_exposure": 6,
                "random": 4,
            },
        )
        self.assertNotIn("recent", {item.slot for item in result.items})
        self.assertEqual(len({item.candidate.doc_id for item in result.items}), 15)

    def test_quality_pool_requires_technical_usability_and_prefers_vectors(
        self,
    ) -> None:
        vectorless = _candidate(1, vector=None, mtime_ns=100)
        vector_present = _candidate(2, vector=(0.0, 1.0), mtime_ns=1)
        too_small = _candidate(3, width=1_000, height=1_000)
        extreme_ratio = _candidate(4, width=8_001, height=2_000)
        remaining = [_candidate(number, width=1, height=1) for number in range(5, 24)]

        result = select_recommendations(
            [vectorless, vector_present, too_small, extreme_ratio, *remaining],
            rng_seed=3,
        )

        quality_ids = [
            item.candidate.doc_id for item in result.items if item.slot == "quality"
        ]
        self.assertIn("doc-2", quality_ids)
        self.assertLess(quality_ids.index("doc-2"), quality_ids.index("doc-1"))
        self.assertNotIn("doc-3", quality_ids)
        self.assertNotIn("doc-4", quality_ids)

    def test_enforces_sha_album_and_character_limits_without_author_data(self) -> None:
        candidates = [
            _candidate(number, sha="same" if number < 2 else None)
            for number in range(22)
        ]
        candidates.extend(
            _candidate(30 + number, album_id="album", character="same-character")
            for number in range(8)
        )

        result = select_recommendations(candidates, rng_seed=9)

        sha_values = [item.candidate.sha256 for item in result.items]
        self.assertLessEqual(sha_values.count("same"), 1)
        self.assertLessEqual(
            sum(item.candidate.album_id == "album" for item in result.items), 3
        )
        self.assertLessEqual(
            sum(item.candidate.character == "same-character" for item in result.items),
            5,
        )

    def test_history_uses_the_first_progressive_window_that_completes(self) -> None:
        candidates = [_candidate(number, vector=None) for number in range(225)]

        result = select_recommendations(
            candidates,
            recent_sha256=[f"sha-{number}" for number in range(225)],
            rng_seed=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.history_window, 210)
        self.assertEqual(len(result.items), 15)

    def test_low_exposure_and_random_rank_low_exposure_before_slot_order(self) -> None:
        candidates = (
            _candidate(1, exposure_count=3, mtime_ns=100, vector=None),
            _candidate(2, exposure_count=0, mtime_ns=1, vector=None),
            _candidate(3, exposure_count=1, mtime_ns=50, vector=None),
        )

        low_exposure = _ranked_for_slot(candidates, "low_exposure", 7)
        random = _ranked_for_slot(candidates, "random", 7)

        self.assertEqual([item.exposure_count for item in low_exposure], [0, 1, 3])
        self.assertEqual([item.exposure_count for item in random], [0, 1, 3])

    def test_low_exposure_does_not_skip_unshown_candidate_for_vector_diversity(
        self,
    ) -> None:
        basis = tuple(
            tuple(1.0 if index == axis else 0.0 for index in range(6))
            for axis in range(6)
        )
        quality = [
            _candidate(number, vector=basis[number], mtime_ns=100 - number)
            for number in range(5)
        ]
        unshown = _candidate(
            10,
            width=1,
            height=1,
            exposure_count=0,
            vector=basis[0],
            mtime_ns=200,
        )
        shown = [
            _candidate(
                number,
                width=1,
                height=1,
                exposure_count=1,
                vector=basis[5],
            )
            for number in range(11, 30)
        ]

        result = select_recommendations([*quality, unshown, *shown], rng_seed=8)
        low_exposure = [
            item.candidate for item in result.items if item.slot == "low_exposure"
        ]

        self.assertEqual(low_exposure[0].candidate_id, unshown.candidate_id)

    def test_random_does_not_skip_unshown_candidate_for_vector_diversity(
        self,
    ) -> None:
        basis = tuple(
            tuple(1.0 if index == axis else 0.0 for index in range(7))
            for axis in range(7)
        )
        quality = [
            _candidate(number, vector=basis[number], mtime_ns=100 - number)
            for number in range(5)
        ]
        unshown_for_low_exposure = [
            _candidate(
                10 + number,
                width=1,
                height=1,
                exposure_count=0,
                vector=basis[5],
                mtime_ns=100 - number,
            )
            for number in range(6)
        ]
        unshown_for_random = _candidate(
            20,
            width=1,
            height=1,
            exposure_count=0,
            vector=basis[0],
            mtime_ns=1,
        )
        shown = [
            _candidate(
                30 + number,
                width=1,
                height=1,
                exposure_count=1,
                vector=basis[6],
            )
            for number in range(10)
        ]

        result = select_recommendations(
            [*quality, *unshown_for_low_exposure, unshown_for_random, *shown],
            rng_seed=8,
        )
        random = [item.candidate for item in result.items if item.slot == "random"]

        self.assertEqual(random[0].candidate_id, unshown_for_random.candidate_id)

    def test_explicit_preferences_are_never_relaxed_with_history(self) -> None:
        candidates = [_candidate(number) for number in range(15)]

        result = select_recommendations(
            candidates,
            recent_sha256=[candidate.sha256 for candidate in candidates],
            excluded_sha256=[candidate.sha256 for candidate in candidates],
            rng_seed=1,
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.history_window, 0)
        self.assertEqual(result.items, ())

    def test_240_history_prevents_repeats_across_sixteen_full_batches(self) -> None:
        shown: list[str] = []
        exposure: dict[str, int] = {}
        batches: list[set[str]] = []
        for batch_index in range(17):
            candidates = [
                _candidate(
                    number,
                    exposure_count=exposure.get(f"sha-{number}", 0),
                    vector=None,
                )
                for number in range(300)
            ]
            result = select_recommendations(
                candidates,
                recent_sha256=shown,
                rng_seed=batch_index,
            )
            selected = {item.candidate.sha256 for item in result.items}
            self.assertEqual(result.status, "complete")
            self.assertTrue(all(selected.isdisjoint(old) for old in batches[-16:]))
            batches.append(selected)
            for sha256 in selected:
                exposure[sha256] = exposure.get(sha256, 0) + 1
            shown = [*selected, *shown]

    def test_missing_vectors_never_block_a_partial_result(self) -> None:
        result = select_recommendations(
            [_candidate(number, vector=None) for number in range(4)],
            rng_seed=2,
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.items), 4)
        self.assertEqual(result.history_window, 0)

    def test_random_backfill_completes_a_short_pool(self) -> None:
        candidates = [_candidate(number, width=1, height=1) for number in range(20)]

        result = select_recommendations(candidates, rng_seed=5)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.items), 15)
        self.assertEqual(result.counts_by_slot["quality"], 0)
        self.assertGreater(result.counts_by_slot["random"], 4)

    def test_mmr_penalizes_near_duplicates_when_a_different_vector_exists(self) -> None:
        duplicate = _candidate(1, vector=(1.0, 0.0), mtime_ns=20)
        near_duplicate = _candidate(2, vector=(0.99, 0.01), mtime_ns=19)
        distinct = _candidate(3, vector=(0.0, 1.0), mtime_ns=1)
        candidates = [duplicate, near_duplicate, distinct] + [
            _candidate(number, width=1, height=1, vector=None)
            for number in range(4, 22)
        ]

        result = select_recommendations(candidates, rng_seed=4)

        quality_ids = [
            item.candidate.doc_id for item in result.items if item.slot == "quality"
        ]
        self.assertEqual(quality_ids[:2], ["doc-1", "doc-3"])

    def test_selection_matches_the_pre_optimization_golden_digests(self) -> None:
        expected = {
            300: "6021a28914d429a769226f5f679ba3beb23ea7d8685a4888cdb0a4ecabb71ecf",
            768: "c0811e391efd727b86c8c580549cc08060abdf5e9cafdf21c524531ba3bb4de7",
        }

        for candidate_count, digest in expected.items():
            with self.subTest(candidate_count=candidate_count):
                self.assertEqual(_selection_digest(candidate_count), digest)
                self.assertEqual(_selection_digest(candidate_count), digest)

    def test_selection_prepares_vectors_rankings_and_base_scores_once(self) -> None:
        candidates = tuple(
            _candidate(number, vector=(float(number + 1), 1.0)) for number in range(225)
        )
        history = [candidate.sha256 for candidate in candidates]

        with (
            patch.object(
                recommendation_module,
                "_unit_vector",
                wraps=recommendation_module._unit_vector,
            ) as normalize,
            patch.object(
                recommendation_module,
                "_ranked_for_slot",
                wraps=recommendation_module._ranked_for_slot,
            ) as rank,
            patch.object(
                recommendation_module,
                "_base_score",
                wraps=recommendation_module._base_score,
            ) as base_score,
            patch.object(
                recommendation_module,
                "_unit_vector_similarity",
                wraps=recommendation_module._unit_vector_similarity,
            ) as similarity,
        ):
            result = select_recommendations(
                candidates,
                recent_sha256=history,
                rng_seed=1,
            )

        self.assertEqual(result.history_window, 210)
        self.assertEqual(normalize.call_count, len(candidates))
        self.assertEqual(rank.call_count, 3)
        self.assertEqual(base_score.call_count, len(candidates) * 3)
        self.assertEqual(similarity.call_count, len(candidates) * len(result.items))

    def test_invalid_and_incompatible_candidate_vectors_degrade_deterministically(
        self,
    ) -> None:
        vectors: tuple[tuple[float, ...] | None, ...] = (
            None,
            (),
            (0.0, 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 0.0),
        )
        candidates = tuple(
            _candidate(number, vector=vectors[number % len(vectors)])
            for number in range(30)
        )

        first = select_recommendations(candidates, rng_seed=91)
        second = select_recommendations(candidates, rng_seed=91)
        first_identities = [
            (item.slot, item.candidate.candidate_id) for item in first.items
        ]
        second_identities = [
            (item.slot, item.candidate.candidate_id) for item in second.items
        ]

        self.assertEqual(first.status, "complete")
        self.assertEqual(first_identities, second_identities)
        self.assertTrue(all(math.isfinite(item.score) for item in first.items))

    def test_mmr_thresholds_are_unchanged(self) -> None:
        self.assertEqual(_similarity_penalty(0.849_999), 0.0)
        self.assertEqual(_similarity_penalty(0.85), 0.10)
        self.assertAlmostEqual(_similarity_penalty(0.90), 0.55)
        self.assertEqual(_similarity_penalty(0.95), 1.0)

    def test_personalization_is_cold_until_ten_valid_vectors_and_capped(self) -> None:
        candidates = (
            _candidate(1, vector=(1.0, 0.0)),
            _candidate(2, vector=(0.0, 1.0)),
        )
        cold = personalize_candidates(
            candidates,
            [PreferenceVector("like", (1.0, 0.0)) for _index in range(9)],
        )
        active = personalize_candidates(
            candidates,
            [PreferenceVector("like", (1.0, 0.0)) for _index in range(100)],
        )

        self.assertFalse(cold.applied)
        self.assertEqual(cold.reason, "insufficient_preferences")
        self.assertEqual(cold.candidates, candidates)
        self.assertTrue(active.applied)
        self.assertEqual(active.effective_count, 100)
        self.assertAlmostEqual(active.candidates[0].personalization_score, 0.25)
        self.assertLessEqual(
            max(abs(item.personalization_score) for item in active.candidates),
            0.25,
        )

    def test_like_and_dislike_vectors_adjust_only_non_random_slots(self) -> None:
        candidates = (
            _candidate(1, vector=(1.0, 0.0)),
            _candidate(2, vector=(0.0, 1.0)),
        )
        profile = personalize_candidates(
            candidates,
            [
                *[PreferenceVector("like", (1.0, 0.0)) for _index in range(5)],
                *[PreferenceVector("dislike", (0.0, 1.0)) for _index in range(5)],
            ],
        )

        self.assertTrue(profile.applied)
        self.assertAlmostEqual(profile.candidates[0].personalization_score, 0.025)
        self.assertAlmostEqual(profile.candidates[1].personalization_score, -0.025)
        self.assertGreater(profile.candidates[0].personalization_score, 0)
        self.assertLess(profile.candidates[1].personalization_score, 0)
        self.assertGreater(
            _slot_personalization_score(profile.candidates[0], "low_exposure"), 0
        )
        self.assertEqual(
            _slot_personalization_score(profile.candidates[0], "random"), 0
        )

    def test_prepared_and_direct_personalization_are_equivalent_and_immutable(
        self,
    ) -> None:
        candidates = (
            _candidate(1, vector=(2.0, 0.0)),
            _candidate(2, vector=(0.0, 3.0)),
            _candidate(3, vector=None),
        )
        preferences = (
            *[PreferenceVector("like", (4.0, 0.0)) for _index in range(5)],
            *[PreferenceVector("dislike", (0.0, 5.0)) for _index in range(5)],
        )

        prepared = prepare_personalization_profile(preferences)
        applied = apply_personalization_profile(candidates, prepared)
        direct = personalize_candidates(candidates, preferences)

        self.assertIsInstance(prepared, PreparedPersonalizationProfile)
        self.assertEqual(applied, direct)
        with self.assertRaises(FrozenInstanceError):
            prepared.reason = "changed"  # type: ignore[misc]

    def test_prepared_profile_normalizes_each_candidate_only_once(self) -> None:
        profile = prepare_personalization_profile(
            [PreferenceVector("like", (1.0, 0.0)) for _index in range(10)]
        )
        candidates = (
            _candidate(1, vector=(2.0, 0.0)),
            _candidate(2, vector=(0.0, 0.0)),
            _candidate(3, vector=(float("nan"), 0.0)),
            _candidate(4, vector=(float("inf"), 0.0)),
            _candidate(5, vector=(1.0, 0.0, 0.0)),
            _candidate(6, vector=None),
        )

        with patch.object(
            recommendation_module,
            "_unit_vector",
            wraps=recommendation_module._unit_vector,
        ) as normalize:
            result = apply_personalization_profile(candidates, profile)

        self.assertTrue(result.applied)
        self.assertEqual(normalize.call_count, len(candidates) - 1)
        self.assertEqual(result.candidates[-1].personalization_score, 0.0)

    def test_invalid_or_incompatible_preference_vectors_degrade_safely(self) -> None:
        candidates = (_candidate(1, vector=(1.0, 0.0)),)

        invalid = personalize_candidates(
            candidates,
            [PreferenceVector("like", (float("nan"), 0.0)) for _index in range(10)],
        )
        incompatible = personalize_candidates(
            candidates,
            [
                *[PreferenceVector("like", (1.0, 0.0)) for _index in range(5)],
                *[PreferenceVector("dislike", (0.0, 1.0, 0.0)) for _index in range(5)],
            ],
        )

        self.assertFalse(invalid.applied)
        self.assertEqual(invalid.effective_count, 0)
        self.assertEqual(invalid.reason, "insufficient_preferences")
        self.assertFalse(incompatible.applied)
        self.assertEqual(incompatible.reason, "incompatible_vector_spaces")

        invalid_prepared = prepare_personalization_profile(
            [
                *[PreferenceVector("like", (0.0, 0.0)) for _index in range(4)],
                *[PreferenceVector("like", (float("nan"), 0.0)) for _index in range(4)],
                *[PreferenceVector("like", (float("inf"), 0.0)) for _index in range(4)],
            ]
        )
        self.assertEqual(invalid_prepared.effective_count, 0)
        self.assertEqual(invalid_prepared.reason, "insufficient_preferences")
        self.assertEqual(
            apply_personalization_profile(candidates, invalid_prepared),
            personalize_candidates(
                candidates,
                [PreferenceVector("like", (0.0, 0.0)) for _index in range(10)],
            ),
        )

        with self.assertRaises(TypeError):
            apply_personalization_profile(candidates, object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
