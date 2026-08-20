from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.search_learning_service import (
    FixedEvaluationGate,
    SearchLearningService,
    SearchLearningServiceError,
)
from image_vector_service.search_learning_store import (
    SearchCandidateRecord,
    SearchLearningStore,
    SearchSessionRecord,
)


def _populate_feedback(store: SearchLearningStore) -> None:
    for session_index in range(2):
        candidates = tuple(
            SearchCandidateRecord(
                library_id="lib-main",
                doc_id=f"doc-{session_index}-{candidate_index}",
                original_rank=candidate_index + 1,
                displayed_rank=candidate_index + 1,
                displayed=True,
                ranking_score=0.9 - candidate_index * 0.5,
                features={
                    "feature_schema_version": 1,
                    "vector_raw_score": 0.1 + candidate_index * 0.6,
                    "vector_confidence": 0.9 - candidate_index * 0.6,
                    "tag_match_score": 0.8 - candidate_index * 0.5,
                    "manual_tag_matches": int(candidate_index == 0),
                    "folder_tag_matches": int(session_index == 0),
                    "identity_match": int(candidate_index == 0),
                    "collection_rank": candidate_index + 1,
                    "collection_size": 1000 + session_index,
                    "duplicate_group_size": 1,
                    "query_type": "text",
                },
            )
            for candidate_index in range(2)
        )
        session_id = f"search-{session_index}"
        store.record_search(
            SearchSessionRecord(
                session_id=session_id,
                query_type="text",
                requested_count=15,
                returned_count=2,
                library_ids=("lib-main",),
                latency_ms=30,
                candidates=candidates,
            )
        )
        for candidate_index, action in enumerate(("relevant", "not_relevant")):
            store.record_feedback(
                session_id=session_id,
                library_id="lib-main",
                doc_id=f"doc-{session_index}-{candidate_index}",
                action=action,
                source="context_menu",
            )


def _passing_report(_model: object) -> dict[str, object]:
    return {
        "fixed_evaluation_set": True,
        "evaluation_set_id": "quality-fixture-v1",
        "current": {
            "precision_at_15": 0.60,
            "recall_at_15": 0.70,
            "cross_collection_bias": 0.10,
            "p95_latency_ms": 100.0,
        },
        "candidate": {
            "precision_at_15": 0.64,
            "recall_at_15": 0.69,
            "no_answer_false_positive_rate": 0.08,
            "cross_collection_bias": 0.09,
            "p95_latency_ms": 109.0,
            "external_api_calls": 0,
        },
    }


def _wait_job(service: SearchLearningService, job_id: str) -> dict[str, object]:
    for _attempt in range(200):
        job = service.training_job(job_id)
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.01)
    raise AssertionError("training job did not finish")


class SearchLearningServiceTests(unittest.TestCase):
    def test_default_minimum_gate_collects_without_training(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            service = SearchLearningService(temporary, store=store)
            status = service.status()
            self.assertEqual(
                status["minimum_requirements"],
                {
                    "query_sessions": 100,
                    "explicit_samples": 300,
                    "positive_and_negative_required": True,
                },
            )
            with self.assertRaises(SearchLearningServiceError) as caught:
                service.train()
            self.assertEqual(caught.exception.code, "insufficient_training_data")
            self.assertFalse(
                (Path(temporary) / "search-learning" / "active.json").exists()
            )

    def test_training_creates_candidate_but_never_updates_online_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            _populate_feedback(store)
            service = SearchLearningService(temporary, store=store)
            with (
                patch(
                    "image_vector_service.search_learning_service.MIN_QUERY_SESSIONS",
                    2,
                ),
                patch(
                    "image_vector_service.search_learning_service.MIN_EXPLICIT_SAMPLES",
                    4,
                ),
            ):
                submitted = service.train()
                job = _wait_job(service, str(submitted["job_id"]))
            self.assertEqual(job["status"], "succeeded")
            model = store.model_version(str(job["model_version"]))
            self.assertEqual(model["gate_status"], "pending")
            self.assertTrue(
                (Path(temporary) / "search-learning" / model["model_file"]).is_file()
            )
            self.assertFalse(
                (Path(temporary) / "search-learning" / "active.json").exists()
            )
            with self.assertRaises(SearchLearningServiceError) as caught:
                service.activate(str(job["model_version"]))
            self.assertEqual(caught.exception.code, "evaluation_gate_not_passed")

    def test_gate_approved_versions_activate_and_rollback_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            _populate_feedback(store)
            service = SearchLearningService(
                temporary, store=store, evaluator=_passing_report
            )
            with (
                patch(
                    "image_vector_service.search_learning_service.MIN_QUERY_SESSIONS",
                    2,
                ),
                patch(
                    "image_vector_service.search_learning_service.MIN_EXPLICIT_SAMPLES",
                    4,
                ),
            ):
                first = _wait_job(service, str(service.train()["job_id"]))
                first_version = str(first["model_version"])
                first_settings = service.activate(first_version, shadow_mode=True)
                self.assertTrue(first_settings["shadow_mode"])

                second = _wait_job(service, str(service.train()["job_id"]))
                second_version = str(second["model_version"])
                service.activate(second_version, shadow_mode=False)
                rolled_back = service.rollback()

            self.assertEqual(rolled_back["active_model_version"], first_version)
            active = json.loads(
                (Path(temporary) / "search-learning" / "active.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(active["ranker"], f"{first_version}.json")
            self.assertFalse(active["shadow_mode"])
            self.assertNotEqual(first_version, second_version)

    def test_shadow_candidate_promotes_without_destroying_rollback_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            _populate_feedback(store)
            service = SearchLearningService(
                temporary, store=store, evaluator=_passing_report
            )
            with (
                patch(
                    "image_vector_service.search_learning_service.MIN_QUERY_SESSIONS",
                    2,
                ),
                patch(
                    "image_vector_service.search_learning_service.MIN_EXPLICIT_SAMPLES",
                    4,
                ),
            ):
                first = _wait_job(service, str(service.train()["job_id"]))
                first_version = str(first["model_version"])
                service.activate(first_version, shadow_mode=False)
                second = _wait_job(service, str(service.train()["job_id"]))
                second_version = str(second["model_version"])
                shadow = service.activate(second_version, shadow_mode=True)
                promoted = service.activate(second_version, shadow_mode=False)
                rolled_back = service.rollback()

            self.assertEqual(shadow["previous_model_version"], first_version)
            self.assertEqual(promoted["previous_model_version"], first_version)
            self.assertEqual(rolled_back["active_model_version"], first_version)

    def test_disabled_learning_rejects_activation_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            _populate_feedback(store)
            service = SearchLearningService(
                temporary, store=store, evaluator=_passing_report
            )
            with (
                patch(
                    "image_vector_service.search_learning_service.MIN_QUERY_SESSIONS",
                    2,
                ),
                patch(
                    "image_vector_service.search_learning_service.MIN_EXPLICIT_SAMPLES",
                    4,
                ),
            ):
                first = _wait_job(service, str(service.train()["job_id"]))
                service.activate(str(first["model_version"]), shadow_mode=False)
                second = _wait_job(service, str(service.train()["job_id"]))
                second_version = str(second["model_version"])
                service.activate(second_version, shadow_mode=False)
            service.update_settings({"learning_enabled": False})

            with self.assertRaises(SearchLearningServiceError) as activate_error:
                service.activate(second_version, shadow_mode=False)
            with self.assertRaises(SearchLearningServiceError) as rollback_error:
                service.rollback()

            self.assertEqual(activate_error.exception.code, "search_learning_disabled")
            self.assertEqual(rollback_error.exception.code, "search_learning_disabled")
            active = json.loads(
                (Path(temporary) / "search-learning" / "active.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(active["enabled"])

    def test_quality_gate_rejects_every_required_regression(self) -> None:
        report = _passing_report(None)
        candidate = dict(report["candidate"])  # type: ignore[arg-type]
        candidate.update(
            {
                "precision_at_15": 0.59,
                "recall_at_15": 0.65,
                "no_answer_false_positive_rate": 0.11,
                "cross_collection_bias": 0.11,
                "p95_latency_ms": 116.0,
                "external_api_calls": 1,
            }
        )
        report["candidate"] = candidate
        result = FixedEvaluationGate().evaluate(report)
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.reasons), 6)

    def test_strict_feedback_contract_uses_server_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SearchLearningStore(temporary)
            _populate_feedback(store)
            service = SearchLearningService(temporary, store=store)
            with self.assertRaises(SearchLearningServiceError):
                service.feedback(
                    {
                        "session_id": "search-0",
                        "library_id": "lib-main",
                        "doc_id": "doc-0-0",
                        "action": "relevant",
                        "feedback_weight": 999,
                    }
                )
            with self.assertRaises(SearchLearningServiceError):
                service.feedback(
                    {
                        "session_id": 123,
                        "library_id": "lib-main",
                        "doc_id": "doc-0-0",
                        "action": "relevant",
                    }
                )
            event = service.feedback(
                {
                    "session_id": "search-0",
                    "library_id": "lib-main",
                    "doc_id": "doc-0-0",
                    "action": "relevant",
                    "source": "context_menu",
                }
            )
            self.assertEqual(event["feedback_weight"], 1.0)

    def test_clear_only_removes_learning_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "library.sqlite3"
            sentinel.write_text("keep", encoding="utf-8")
            store = SearchLearningStore(root)
            service = SearchLearningService(root, store=store)
            fixed_evaluation = root / "search-learning" / "fixed-evaluation.json"
            fixed_evaluation.parent.mkdir(parents=True, exist_ok=True)
            fixed_evaluation.write_text('{"fixed":true}', encoding="utf-8")
            obsolete = root / "search-learning" / "obsolete-model.json"
            obsolete.write_text("{}", encoding="utf-8")
            with self.assertRaises(SearchLearningServiceError):
                service.clear(confirmed=False)
            result = service.clear(confirmed=True)
            self.assertTrue(result["cleared"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            active = json.loads(
                (root / "search-learning" / "active.json").read_text(encoding="utf-8")
            )
            self.assertFalse(active["enabled"])
            self.assertEqual(
                fixed_evaluation.read_text(encoding="utf-8"),
                '{"fixed":true}',
            )
            self.assertFalse(obsolete.exists())


if __name__ == "__main__":
    unittest.main()
