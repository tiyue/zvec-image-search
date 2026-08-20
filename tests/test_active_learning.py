from __future__ import annotations

import random
import unittest
from collections import Counter

from image_vector_service.active_learning import (
    ActiveLearningConfig,
    ActiveLearningError,
    build_active_learning_queue,
    record_learning_decision,
)


def _candidate(
    index: int,
    *,
    group: int | None = None,
    query: int | None = None,
    conflict: float | None = None,
    outlier: float | None = None,
    disagreement: float | None = None,
) -> dict[str, object]:
    return {
        "doc_id": f"doc-{index:03d}",
        "group_id": f"group-{group if group is not None else index // 4:02d}",
        "query_id": f"query-{query if query is not None else index % 6:02d}",
        "relative_path": f"images/{index:03d}.jpg",
        "conflict_score": conflict if conflict is not None else (index % 10) / 10,
        "outlier_score": outlier if outlier is not None else ((index * 3) % 10) / 10,
        "ranking_disagreement": disagreement
        if disagreement is not None
        else ((index * 7) % 10) / 10,
    }


class ActiveLearningTest(unittest.TestCase):
    def test_queue_combines_signals_and_explains_score(self) -> None:
        queue = build_active_learning_queue(
            [
                _candidate(
                    1, group=1, query=1, conflict=1.0, outlier=0.5, disagreement=0.0
                ),
                _candidate(
                    2, group=1, query=1, conflict=0.0, outlier=0.0, disagreement=1.0
                ),
            ],
            ActiveLearningConfig(total_budget=20),
        )

        first = next(item for item in queue.items if item.doc_id == "doc-001")
        self.assertAlmostEqual(first.uncertainty_score, 0.65)
        self.assertEqual(first.reasons, ("identity_conflict", "cluster_outlier"))
        self.assertEqual(queue.api_requests, 0)

    def test_selection_is_order_independent_and_bounded(self) -> None:
        candidates = [_candidate(index) for index in range(60)]
        shuffled = list(candidates)
        random.Random(42).shuffle(shuffled)
        config = ActiveLearningConfig(total_budget=25)

        first = build_active_learning_queue(candidates, config)
        second = build_active_learning_queue(shuffled, config)

        self.assertEqual(first.queue_id, second.queue_id)
        self.assertEqual(first.items, second.items)
        self.assertEqual(len(first.items), 25)
        group_counts = Counter(item.group_id for item in first.items)
        query_counts = Counter(item.query_id for item in first.items)
        self.assertLessEqual(max(group_counts.values()), 3)
        self.assertLessEqual(max(query_counts.values()), 5)

    def test_first_pass_aims_for_two_per_group_then_allows_a_third(self) -> None:
        candidates = [
            _candidate(index, group=index // 4, query=index) for index in range(40)
        ]

        queue = build_active_learning_queue(
            candidates, ActiveLearningConfig(total_budget=25)
        )

        group_counts = Counter(item.group_id for item in queue.items)
        self.assertTrue(all(2 <= count <= 3 for count in group_counts.values()))
        self.assertEqual(sum(count == 3 for count in group_counts.values()), 5)

    def test_one_query_cannot_consume_more_than_five_slots(self) -> None:
        candidates = [
            _candidate(index, group=index // 2, query=1, conflict=1.0)
            for index in range(50)
        ]

        queue = build_active_learning_queue(candidates)

        self.assertEqual(len(queue.items), 5)
        self.assertEqual({item.query_id for item in queue.items}, {"query-01"})

    def test_candidates_without_query_ids_do_not_share_one_artificial_cap(self) -> None:
        candidates = [
            {**_candidate(index, group=index // 2), "query_id": ""}
            for index in range(30)
        ]

        queue = build_active_learning_queue(candidates)

        self.assertEqual(len(queue.items), 25)

    def test_invalid_and_duplicate_candidates_are_isolated(self) -> None:
        valid = _candidate(1)
        duplicate = dict(valid)
        queue = build_active_learning_queue(
            [
                valid,
                duplicate,
                {**_candidate(2), "conflict_score": float("nan")},
                _candidate(3),
            ]
        )

        self.assertEqual([item.doc_id for item in queue.items], ["doc-003"])
        codes = {(failure.doc_id, failure.code) for failure in queue.failures}
        self.assertIn(("doc-001", "duplicate_doc_id"), codes)
        self.assertIn(("doc-002", "invalid_active_learning_candidate"), codes)

    def test_budget_and_group_contract_is_validated(self) -> None:
        for value in (19, 31):
            with self.assertRaisesRegex(ValueError, "total_budget"):
                ActiveLearningConfig(total_budget=value)
        with self.assertRaisesRegex(ValueError, "max_per_group"):
            ActiveLearningConfig(max_per_group=4)
        with self.assertRaisesRegex(ValueError, "max_per_query"):
            ActiveLearningConfig(max_per_query=6)

    def test_decisions_are_immutable_replaced_and_validated(self) -> None:
        queue = build_active_learning_queue([_candidate(1), _candidate(2)])
        accepted = record_learning_decision(
            queue, "doc-001", "accept", labels=["原神", "原神", " 雷电将军 "]
        )
        edited = record_learning_decision(accepted, "doc-001", "edit", labels=["影"])

        self.assertEqual(queue.decisions, ())
        self.assertEqual(accepted.decisions[0].labels, ("原神", "雷电将军"))
        self.assertEqual(edited.decisions[0].decision, "edit")
        self.assertEqual(edited.decisions[0].labels, ("影",))
        with self.assertRaisesRegex(ActiveLearningError, "Unknown queue"):
            record_learning_decision(queue, "missing", "skip")
        with self.assertRaisesRegex(ActiveLearningError, "cannot contain labels"):
            record_learning_decision(queue, "doc-001", "reject", labels=["bad"])


if __name__ == "__main__":
    unittest.main()
