from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.search_features import FEATURE_SCHEMA_VERSION
from image_vector_service.search_learning_evaluator import (
    FIXED_EVALUATION_FILENAME,
    FixedEvaluationPackError,
    FixedEvaluationUnavailable,
    LocalFixedEvaluationEvaluator,
    fixed_evaluation_pack_from_mapping,
)
from image_vector_service.search_learning_service import FixedEvaluationGate


def _features(confidence: float, *, query_type: str = "text") -> dict[str, object]:
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "vector_raw_score": 1.0 - confidence,
        "vector_confidence": confidence,
        "tag_match_score": 0.0,
        "manual_tag_matches": 0.0,
        "folder_tag_matches": 0.0,
        "alias_tag_matches": 0.0,
        "model_high_confidence_tag_matches": 0.0,
        "model_tag_matches": 0.0,
        "vector_tag_matches": 0.0,
        "identity_match": 0.0,
        "work_match": 0.0,
        "action_match": 0.0,
        "expression_match": 0.0,
        "scene_match": 0.0,
        "image_text_agreement": 0.0,
        "collection_rank": 1.0,
        "collection_size": 100.0,
        "duplicate_group_size": 1.0,
        "query_type": query_type,
    }


def _pack() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for index in range(10):
        library = "lib-a" if index % 2 == 0 else "lib-b"
        has_answer = index < 5
        candidates = (
            [
                {
                    "doc_id": f"relevant-{index}",
                    "library_id": library,
                    "relevant": True,
                    "fallback_score": 0.9,
                    "fallback_rank": 1,
                    "features": _features(0.9),
                },
                {
                    "doc_id": f"noise-{index}",
                    "library_id": "lib-b" if library == "lib-a" else "lib-a",
                    "relevant": False,
                    "fallback_score": 0.1,
                    "fallback_rank": 2,
                    "features": _features(0.1),
                },
            ]
            if has_answer
            else [
                {
                    "doc_id": f"no-answer-{index}",
                    "library_id": library,
                    "relevant": False,
                    "fallback_score": 0.1,
                    "fallback_rank": 1,
                    "features": _features(0.1),
                }
            ]
        )
        cases.append(
            {
                "query_id": f"query-{index}",
                "query_type": "text",
                "has_answer": has_answer,
                "latency_ms": 50.0,
                "candidates": candidates,
            }
        )
    return {
        "schema_version": 1,
        "fixed_evaluation_set": True,
        "evaluation_set_id": "human-reviewed-v1",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "generated_at": "2026-07-19T00:00:00Z",
        "cases": cases,
    }


def _candidate_model() -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_version": "candidate-v1",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "kind": "logistic",
        "intercept": -5.0,
        "weights": {"vector_confidence": 10.0},
    }


class SearchLearningEvaluatorTests(unittest.TestCase):
    def test_missing_pack_is_pending_not_an_implicit_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evaluator = LocalFixedEvaluationEvaluator(temporary)

            self.assertEqual(evaluator.status()["status"], "missing")
            with self.assertRaises(FixedEvaluationUnavailable):
                evaluator(_candidate_model())

    def test_strict_pack_rejects_inconsistent_answer_labels(self) -> None:
        payload = _pack()
        payload["cases"][0]["has_answer"] = False  # type: ignore[index]

        with self.assertRaisesRegex(FixedEvaluationPackError, "has_answer"):
            fixed_evaluation_pack_from_mapping(payload)

    def test_local_report_passes_gate_without_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "search-learning"
            directory.mkdir()
            (directory / FIXED_EVALUATION_FILENAME).write_text(
                json.dumps(_pack()), encoding="utf-8"
            )
            evaluator = LocalFixedEvaluationEvaluator(root)

            status = evaluator.status()
            report = evaluator(_candidate_model())
            gate = FixedEvaluationGate().evaluate(report)

            self.assertTrue(status["available"])
            self.assertEqual(status["case_count"], 10)
            self.assertEqual(report["evaluation_set_id"], "human-reviewed-v1")
            self.assertEqual(report["candidate"]["external_api_calls"], 0)
            self.assertEqual(report["candidate"]["precision_at_15"], 1.0)
            self.assertEqual(report["candidate"]["recall_at_15"], 1.0)
            self.assertEqual(gate.status, "passed")

    def test_corrupt_present_pack_reports_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "search-learning"
            directory.mkdir()
            (directory / FIXED_EVALUATION_FILENAME).write_text(
                "{not-json", encoding="utf-8"
            )

            status = LocalFixedEvaluationEvaluator(root).status()

            self.assertEqual(status["status"], "invalid")
            self.assertEqual(status["error_code"], "fixed_evaluation_pack_invalid")

    def test_install_validates_and_atomically_replaces_the_fixed_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "reviewed-pack.json"
            source.write_text(json.dumps(_pack()), encoding="utf-8")
            evaluator = LocalFixedEvaluationEvaluator(root / "config")

            result = evaluator.install(source.resolve())

            self.assertTrue(result["installed"])
            self.assertEqual(result["evaluation_set_id"], "human-reviewed-v1")
            self.assertEqual(result["external_api_calls"], 0)
            self.assertEqual(evaluator.load().evaluation_set_id, "human-reviewed-v1")
            temporary_files = list(
                evaluator.path.parent.glob(f".{FIXED_EVALUATION_FILENAME}.*.tmp")
            )
            self.assertEqual(temporary_files, [])

    def test_failed_atomic_replace_preserves_the_previous_fixed_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator = LocalFixedEvaluationEvaluator(root / "config")
            evaluator.path.parent.mkdir(parents=True)
            previous = json.dumps(_pack(), ensure_ascii=False, indent=4) + "\n"
            evaluator.path.write_text(previous, encoding="utf-8")
            replacement = _pack()
            replacement["evaluation_set_id"] = "human-reviewed-v2"
            source = root / "replacement.json"
            source.write_text(json.dumps(replacement), encoding="utf-8")

            with (
                patch(
                    "image_vector_service.search_learning_evaluator.os.replace",
                    side_effect=OSError("locked"),
                ),
                self.assertRaisesRegex(
                    FixedEvaluationPackError,
                    "Could not install",
                ),
            ):
                evaluator.install(source.resolve())

            self.assertEqual(evaluator.path.read_text(encoding="utf-8"), previous)
            self.assertEqual(
                list(evaluator.path.parent.glob(f".{FIXED_EVALUATION_FILENAME}.*.tmp")),
                [],
            )

    def test_invalid_import_never_replaces_an_existing_fixed_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator = LocalFixedEvaluationEvaluator(root / "config")
            evaluator.path.parent.mkdir(parents=True)
            previous = json.dumps(_pack())
            evaluator.path.write_text(previous, encoding="utf-8")
            source = root / "invalid.json"
            source.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(FixedEvaluationPackError):
                evaluator.install(source.resolve())

            self.assertEqual(evaluator.path.read_text(encoding="utf-8"), previous)


if __name__ == "__main__":
    unittest.main()
