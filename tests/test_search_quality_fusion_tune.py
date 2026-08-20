from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.config import ConfigurationError
from image_vector_service.search_quality import load_search_quality
from tests.search_quality import fusion_tune


def _dataset() -> dict:
    return {
        "schema_version": 1,
        "name": "fusion-tuning-draft-fixture",
        "items": [
            {
                "id": "combined-answer",
                "query_type": "combined",
                "mode": "combined",
                "query": {"text": "answer", "image": "answer.png"},
                "library_scope": {"mode": "selected", "library_ids": ["lib"]},
                "relevant_images": [],
                "suggested_relevant_images": [{"image_id": "lib:relevant.jpg"}],
                "annotation": {"status": "pending", "notes": "AI draft"},
            },
            {
                "id": "combined-empty",
                "query_type": "no-answer",
                "mode": "combined",
                "query": {"text": "empty", "image": "empty.png"},
                "library_scope": {"mode": "selected", "library_ids": ["lib"]},
                "relevant_images": [],
                "suggested_relevant_images": [],
                "annotation": {"status": "pending", "notes": "AI draft"},
            },
            {
                "id": "text-ignored",
                "query_type": "text",
                "mode": "text",
                "query": {"text": "not part of combined tuning"},
                "library_scope": {"mode": "selected", "library_ids": ["lib"]},
                "relevant_images": [],
                "annotation": {"status": "pending", "notes": "AI draft"},
            },
        ],
    }


def _result(
    image_id: str,
    image_confidence: float | None,
    text_confidence: float | None,
) -> dict:
    result = {
        "image_id": image_id,
        "rank": 1,
        "score": max(image_confidence or 0.0, text_confidence or 0.0),
        "raw_score": max(image_confidence or 0.0, text_confidence or 0.0),
        "normalized_score": max(image_confidence or 0.0, text_confidence or 0.0),
        "confidence": max(image_confidence or 0.0, text_confidence or 0.0),
        "match_state": "possible",
        "rank_source": "fused",
    }
    if image_confidence is not None:
        result["image_confidence"] = image_confidence
        result["image_rank"] = 1
    if text_confidence is not None:
        result["text_confidence"] = text_confidence
        result["text_rank"] = 1
    return result


def _run() -> dict:
    return {
        "schema_version": 1,
        "name": "fusion-tuning-capture-fixture",
        "draft": True,
        "baseline_eligible": False,
        "score_semantics": {
            "text": "lower_is_better",
            "image": "lower_is_better",
            "combined": "higher_is_better",
        },
        "cases": [
            {
                "id": "combined-answer",
                "mode": "combined",
                "latency_ms": 1.0,
                "api_requests": 0,
                "results": [_result("lib:relevant.jpg", 0.9, 0.9)],
            },
            {
                "id": "combined-empty",
                "mode": "combined",
                "latency_ms": 1.0,
                "api_requests": 0,
                "results": [_result("lib:false.jpg", 0.3, 0.3)],
            },
        ],
    }


class FusionTuneTest(unittest.TestCase):
    def test_grid_finds_draft_only_threshold_without_losing_recall(self):
        grids = {
            "image_weight": [0.5],
            "agreement_reward": [0.0],
            "rank_decay": [10.0],
            "weak_channel_floor": [0.45],
            "weak_channel_penalty": [0.08],
            "minimum_confidence": [0.25, 0.5],
            "score_gap": [1.0],
        }
        report = fusion_tune.tune_combined_fusion(
            _dataset(),
            _run(),
            grids=grids,
            top_k=5,
        )
        self.assertTrue(report["draft"])
        self.assertFalse(report["baseline_eligible"])
        self.assertFalse(report["production_config_generated"])
        self.assertEqual(report["coverage"]["dataset_total"], 3)
        self.assertEqual(report["coverage"]["combined_cases"], 2)
        self.assertEqual(
            report["best_candidate"]["parameters"]["minimum_confidence"],
            0.5,
        )
        self.assertEqual(
            report["best_candidate"]["metrics"]["no_answer_false_return_rate"],
            0.0,
        )
        self.assertIsNotNone(report["best_strict_candidate"])
        self.assertEqual(
            report["best_strict_candidate"]["metrics"]["strict_precision_at_5"],
            0.2,
        )
        self.assertIn(
            "Best strict-Precision diagnostic",
            fusion_tune.render_markdown(report),
        )

    def test_absent_channel_may_omit_both_diagnostic_fields(self):
        run = _run()
        run["cases"][0]["results"] = [_result("lib:relevant.jpg", 0.9, None)]
        report = fusion_tune.tune_combined_fusion(
            _dataset(),
            run,
            grids={
                name: [values[0]] for name, values in fusion_tune.DEFAULT_GRIDS.items()
            },
            top_k=5,
            max_false_return_rate=1.0,
        )
        self.assertEqual(report["coverage"]["combined_cases"], 2)

    def test_grid_selects_one_global_complementary_query_weight(self):
        run = _run()
        relevant = _result("lib:relevant.jpg", 0.95, 0.5)
        text_noise = _result("lib:text-noise.jpg", 0.5, 0.95)
        text_noise["rank"] = 2
        run["cases"][0]["results"] = [relevant, text_noise]
        run["cases"][1]["results"] = [_result("lib:false.jpg", 0.75, 0.75)]

        report = fusion_tune.tune_combined_fusion(
            _dataset(),
            run,
            grids={
                "image_weight": [0.5, 0.8],
                "agreement_reward": [0.08],
                "rank_decay": [10.0],
                "weak_channel_floor": [0.45],
                "weak_channel_penalty": [0.08],
                "minimum_confidence": [0.8, 0.85],
                "score_gap": [1.0],
            },
            top_k=5,
        )

        best = report["best_candidate"]
        self.assertEqual(best["parameters"]["image_weight"], 0.8)
        self.assertAlmostEqual(best["parameters"]["text_weight"], 0.2)
        self.assertEqual(best["metrics"]["precision_at_5"], 1.0)
        self.assertEqual(best["metrics"]["strict_precision_at_5"], 0.2)
        self.assertEqual(best["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(best["metrics"]["no_answer_false_return_rate"], 0.0)
        difference = report["difference_from_default_v2"]
        self.assertAlmostEqual(
            difference["parameters"]["image_weight"]["delta"],
            0.3,
        )

    def test_report_is_rejected_by_production_loader_and_marked_in_markdown(self):
        report = fusion_tune.tune_combined_fusion(
            _dataset(),
            _run(),
            grids={
                name: [values[0]] for name, values in fusion_tune.DEFAULT_GRIDS.items()
            },
            top_k=5,
            max_false_return_rate=1.0,
        )
        markdown = fusion_tune.render_markdown(report)
        self.assertIn("DRAFT ONLY", markdown)
        self.assertIn("NOT A PRODUCTION CONFIG", markdown)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search-quality.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_search_quality(Path(temporary))

    def test_partial_channel_diagnostic_is_rejected(self):
        run = copy.deepcopy(_run())
        del run["cases"][0]["results"][0]["image_rank"]
        with self.assertRaisesRegex(ValueError, "must both be present"):
            fusion_tune.tune_combined_fusion(
                _dataset(),
                run,
                grids={
                    name: [values[0]]
                    for name, values in fusion_tune.DEFAULT_GRIDS.items()
                },
                top_k=5,
            )


if __name__ == "__main__":
    unittest.main()
