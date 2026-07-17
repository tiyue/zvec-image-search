from __future__ import annotations

import copy
import unittest
from typing import Any

from tests.search_quality import evaluate
from tests.search_quality.image_identity import canonical_image_reference

HASH_A = "a" * 64
HASH_B = "b" * 64


def _dataset(relevant_images: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "identity-fixture",
        "items": [
            {
                "id": "text-001",
                "query_type": "text",
                "mode": "text",
                "query": {"text": "same bytes in another Collection"},
                "library_scope": {
                    "mode": "selected",
                    "library_ids": ["library-a", "library-b"],
                },
                "relevant_images": relevant_images,
                "annotation": {
                    "status": "synthetic_fixture",
                    "notes": "identity test",
                },
            }
        ],
    }


def _run(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "identity-run",
        "score_semantics": {"text": "lower_is_better"},
        "cases": [
            {
                "id": "text-001",
                "latency_ms": 1.0,
                "api_requests": 0,
                "results": results,
            }
        ],
    }


class SearchQualityImageIdentityTest(unittest.TestCase):
    def test_same_content_in_another_collection_matches_by_sha256(self):
        report = evaluate.evaluate_dataset(
            _dataset([{"image_id": "library-a:photos/original.jpg", "sha256": HASH_A}]),
            _run(
                [
                    {
                        "image_id": "library-b:archive/copy.jpg",
                        "sha256": HASH_A.upper(),
                        "rank": 1,
                        "score": 0.1,
                    }
                ]
            ),
            allow_synthetic=True,
        )

        metrics = report["metrics"]
        self.assertEqual(metrics["precision_at_5"], 1.0)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)

    def test_legacy_image_id_and_mixed_metadata_remain_compatible(self):
        legacy = evaluate.evaluate_dataset(
            _dataset([{"image_id": "library-a:photos/original.jpg"}]),
            _run(
                [
                    {
                        "image_id": "library-a:photos/original.jpg",
                        "rank": 1,
                        "score": 0.1,
                    }
                ]
            ),
            allow_synthetic=True,
        )
        mixed = evaluate.evaluate_dataset(
            _dataset([{"image_id": "library-a:photos/original.jpg"}]),
            _run(
                [
                    {
                        "image_id": "library-a:photos/original.jpg",
                        "sha256": HASH_A,
                        "rank": 1,
                        "score": 0.1,
                    }
                ]
            ),
            allow_synthetic=True,
        )

        self.assertEqual(legacy["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(mixed["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(
            canonical_image_reference(
                {"image_id": "library-a:x.jpg", "sha256": HASH_A.upper()},
                "fixture",
            )["sha256"],
            HASH_A,
        )

    def test_duplicate_global_identity_is_rejected_in_labels_and_results(self):
        duplicate_labels = _dataset(
            [
                {"image_id": "library-a:first.jpg", "sha256": HASH_A},
                {"image_id": "library-b:second.jpg", "sha256": HASH_A},
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate global image identity"):
            evaluate.validate_dataset(duplicate_labels)

        duplicate_results = _run(
            [
                {
                    "image_id": "library-a:first.jpg",
                    "sha256": HASH_A,
                    "rank": 1,
                    "score": 0.1,
                },
                {
                    "image_id": "library-b:second.jpg",
                    "sha256": HASH_A,
                    "rank": 2,
                    "score": 0.2,
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate global image identity"):
            evaluate.validate_run(duplicate_results)

    def test_invalid_and_conflicting_hashes_fail_closed(self):
        invalid_dataset = _dataset(
            [{"image_id": "library-a:first.jpg", "sha256": "not-a-digest"}]
        )
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            evaluate.validate_dataset(invalid_dataset)

        invalid_run = _run(
            [
                {
                    "image_id": "library-a:first.jpg",
                    "sha256": "",
                    "rank": 1,
                    "score": 0.1,
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            evaluate.validate_run(invalid_run)

        dataset = _dataset([{"image_id": "library-a:first.jpg", "sha256": HASH_A}])
        conflicting_run = _run(
            [
                {
                    "image_id": "library-a:first.jpg",
                    "sha256": HASH_B,
                    "rank": 1,
                    "score": 0.1,
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "conflicting sha256"):
            evaluate.evaluate_dataset(
                copy.deepcopy(dataset),
                conflicting_run,
                allow_synthetic=True,
            )


if __name__ == "__main__":
    unittest.main()
