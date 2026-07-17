from __future__ import annotations

import unittest

from image_vector_service.models import SearchHit
from image_vector_service.rank_fusion import confidence_rank
from tests.search_quality import calibrate, collection_calibration


def _item(case_id: str, mode: str, relevant: str | None) -> dict:
    query_type = "no-answer" if relevant is None else mode
    return {
        "id": case_id,
        "query_type": query_type,
        "mode": mode,
        "query": {"text": case_id},
        "library_scope": {
            "mode": "selected",
            "library_ids": ["large", "small"],
        },
        "relevant_images": [] if relevant is None else [{"image_id": relevant}],
        "annotation": {"status": "synthetic_fixture", "notes": "test"},
    }


def _case(case_id: str, results: list[tuple[str, float]]) -> dict:
    return {
        "id": case_id,
        "library_ids": ["large", "small"],
        "latency_ms": 1.0,
        "api_requests": 0,
        "results": [
            {
                "image_id": image_id,
                "rank": rank,
                "score": confidence,
                "raw_score": confidence,
                "normalized_score": confidence,
                "confidence": confidence,
            }
            for rank, (image_id, confidence) in enumerate(results, start=1)
        ],
    }


class CollectionNullCalibrationTest(unittest.TestCase):
    def fixture(self) -> list[tuple[dict, dict]]:
        pairs: list[tuple[dict, dict]] = []
        for mode in ("text", "image", "combined"):
            pairs.extend(
                [
                    (
                        _item(f"{mode}-large", mode, "large:relevant.jpg"),
                        _case(
                            f"{mode}-large",
                            [
                                ("large:relevant.jpg", 0.90),
                                ("small:noise.jpg", 0.50),
                            ],
                        ),
                    ),
                    (
                        _item(f"{mode}-small", mode, "small:relevant.jpg"),
                        _case(
                            f"{mode}-small",
                            [
                                ("large:noise.jpg", 0.80),
                                ("small:relevant.jpg", 0.70),
                            ],
                        ),
                    ),
                    (
                        _item(f"{mode}-absent", mode, None),
                        _case(
                            f"{mode}-absent",
                            [
                                ("large:null.jpg", 0.60),
                                ("small:null.jpg", 0.40),
                            ],
                        ),
                    ),
                ]
            )
        return pairs

    def test_calibration_learns_offsets_only_from_no_answer_candidates(self) -> None:
        pairs = self.fixture()

        configuration, diagnostics = collection_calibration.calibrate(
            pairs,
            max_recall_drop=0.03,
        )

        self.assertIsNotNone(configuration)
        assert configuration is not None
        self.assertGreater(configuration["strength"], 0.0)
        self.assertEqual(diagnostics["source"], "calibration_no_answer_candidates")
        for mode in ("text", "image", "combined"):
            libraries = configuration["modes"][mode]["libraries"]
            self.assertLess(libraries["large"]["offset"], 0.0)
            self.assertGreater(libraries["small"]["offset"], 0.0)
            self.assertEqual(libraries["large"]["sample_count"], 1)
            self.assertEqual(libraries["small"]["sample_count"], 1)

        adjusted = collection_calibration.apply_to_pairs(pairs, configuration)
        small_case = next(case for item, case in adjusted if item["id"] == "text-small")
        self.assertEqual(small_case["results"][0]["image_id"], "small:relevant.jpg")
        self.assertEqual([result["rank"] for result in small_case["results"]], [1, 2])

    def test_unknown_library_uses_identity_fallback(self) -> None:
        item = _item("unknown", "text", "unknown:relevant.jpg")
        item["library_scope"]["library_ids"] = ["unknown"]
        case = _case("unknown", [("unknown:relevant.jpg", 0.73)])
        case["library_ids"] = ["unknown"]
        configuration = {
            "mode": collection_calibration.MODE,
            "modes": {
                "text": {
                    "libraries": {
                        "known": {"offset": -0.2},
                    }
                }
            },
        }

        confidence = collection_calibration.ranking_confidence(
            item,
            case,
            case["results"][0],
            configuration,
            fallback_confidence=0.73,
        )

        self.assertEqual(confidence, 0.73)

    def test_single_collection_fixture_keeps_calibration_disabled(self) -> None:
        item = _item("single", "text", None)
        item["library_scope"] = {"mode": "single", "library_ids": ["only"]}
        case = _case("single", [("only:null.jpg", 0.5)])
        case["library_ids"] = ["only"]

        configuration, diagnostics = collection_calibration.calibrate(
            [(item, case)],
            max_recall_drop=0.03,
        )

        self.assertIsNone(configuration)
        self.assertEqual(
            diagnostics["reason"],
            "needs_no_answer_candidates_from_two_libraries_per_mode",
        )

    def test_offline_and_runtime_ranking_filtering_are_equivalent(self) -> None:
        offsets = {"large": -0.20, "small": 0.10}
        calibration = {
            "mode": collection_calibration.MODE,
            "modes": {
                "text": {
                    "libraries": {
                        library_id: {"offset": offset}
                        for library_id, offset in offsets.items()
                    }
                },
                "combined": {
                    "libraries": {
                        library_id: {"offset": offset}
                        for library_id, offset in offsets.items()
                    }
                },
            },
        }
        for mode, query_type, score_field, semantics, threshold in (
            ("text", "text", "raw_score", "lower_is_better", 0.40),
            ("combined", "image_text", "confidence", "higher_is_better", 0.70),
        ):
            with self.subTest(mode=mode):
                item = _item("parity", mode, "small:relevant.jpg")
                case = _case(
                    "parity",
                    [
                        ("large:noise.jpg", 0.90),
                        ("small:relevant.jpg", 0.85),
                    ],
                )
                if mode == "text":
                    case["results"][0]["raw_score"] = 0.20
                    case["results"][1]["raw_score"] = 0.30
                offline = calibrate._filtered_pairs(
                    [(item, case)],
                    threshold,
                    semantics,
                    mode=mode,
                    score_field=score_field,
                    score_gap=1.0,
                    max_confidence_drop=1.0,
                    collection_calibration_config=calibration,
                )[0][1]["results"]

                hits = [
                    SearchHit(
                        doc_id=result["image_id"],
                        distance=float(result["raw_score"]),
                        fields={
                            "library_id": result["image_id"].split(":", 1)[0],
                            "sha256": result["image_id"],
                        },
                        rank=result["rank"],
                        raw_score=float(result["raw_score"]),
                        normalized_score=float(result["confidence"]),
                        confidence=float(result["confidence"]),
                        rank_source="fused" if mode == "combined" else "text",
                    )
                    for result in case["results"]
                ]
                production = calibrate._production_threshold(
                    mode,
                    {
                        "value": threshold,
                        "score_gap": 1.0,
                        "max_confidence_drop": 1.0,
                    },
                    score_field,
                )
                runtime = confidence_rank(
                    hits,
                    query_type=query_type,
                    top_k=10,
                    min_confidence=production["minimum_confidence"],
                    minimum_score=production["minimum_score"],
                    score_gap=production["score_gap"],
                    max_confidence_drop=production["max_confidence_drop"],
                    collection_confidence_offsets=offsets,
                )
                assert runtime is not None

                self.assertEqual(
                    [result["image_id"] for result in offline],
                    [hit.doc_id for hit in runtime.hits],
                )


if __name__ == "__main__":
    unittest.main()
