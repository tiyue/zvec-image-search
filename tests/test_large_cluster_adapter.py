from __future__ import annotations

import math
import tempfile
import tracemalloc
import unittest
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from PIL import Image

from image_vector_service.cluster_operation_store import ClusterOperationStore
from image_vector_service.image_clustering import (
    ClusteringCancelled,
    IdentityEvidence,
    ImageClusteringConfig,
    ImageClusterInput,
)
from image_vector_service.large_cluster_adapter import (
    LargeClusterAdapter,
    should_use_large_cluster_engine,
)
from image_vector_service.models import RankSource, SearchHit


def _sha(value: int) -> str:
    return f"{value:064x}"


class _PagedState:
    def __init__(
        self,
        rows: Iterable[tuple[dict[str, Any], dict[str, Any] | None]],
    ) -> None:
        self._rows = list(rows)
        self.count_calls = 0
        self.page_selects = 0
        self.max_requested = 0
        self.max_selected = 0

    def count(self) -> int:
        self.count_calls += 1
        return len(self._rows)

    def iter_entries_with_annotations(
        self, *, chunk_size: int = 256
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
        self.max_requested = max(self.max_requested, chunk_size)
        for offset in range(0, len(self._rows), chunk_size):
            self.page_selects += 1
            page = self._rows[offset : offset + chunk_size]
            self.max_selected = max(self.max_selected, len(page))
            yield page


class _GeneratedState:
    """Generate a large logical table without retaining it in the test process."""

    def __init__(self, count: int) -> None:
        self.row_count = count
        self.count_calls = 0
        self.page_selects = 0
        self.max_requested = 0
        self.max_selected = 0

    def count(self) -> int:
        self.count_calls += 1
        return self.row_count

    def iter_entries_with_annotations(
        self, *, chunk_size: int = 256
    ) -> Iterator[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
        self.max_requested = max(self.max_requested, chunk_size)
        for offset in range(0, self.row_count, chunk_size):
            selected = min(chunk_size, self.row_count - offset)
            self.page_selects += 1
            self.max_selected = max(self.max_selected, selected)
            yield [
                (
                    {
                        "doc_id": f"doc-{index:06d}",
                        "sha256": _sha(index + 1),
                        "relative_path": f"images/{index:06d}.jpg",
                    },
                    None,
                )
                for index in range(offset, offset + selected)
            ]


class _Resolver:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    def resolve_fields(self, fields: dict[str, Any]) -> Path:
        self.calls += 1
        return self.root / str(fields["relative_path"])


class _Repository:
    def __init__(self, vectors: Mapping[str, list[float]] | None = None) -> None:
        self.vectors = dict(vectors or {})
        self.fetch_calls: list[str] = []
        self.query_top_ks: list[int] = []

    def fetch_vector(self, doc_id: str) -> list[float] | None:
        self.fetch_calls.append(doc_id)
        vector = self.vectors.get(doc_id)
        return list(vector) if vector is not None else None

    def query(
        self,
        vector: list[float],
        top_k: int,
        tags: Iterable[str] = (),
        tag_mode: str = "all",
        rank_source: RankSource = "text",
    ) -> list[SearchHit]:
        del tags, tag_mode, rank_source
        self.query_top_ks.append(top_k)
        scored: list[tuple[float, str]] = []
        for doc_id, candidate in self.vectors.items():
            dot = sum(
                left * right for left, right in zip(vector, candidate, strict=True)
            )
            left_norm = math.sqrt(sum(value * value for value in vector))
            right_norm = math.sqrt(sum(value * value for value in candidate))
            similarity = dot / (left_norm * right_norm)
            scored.append((1.0 - similarity, doc_id))
        scored.sort()
        return [
            SearchHit(doc_id=doc_id, distance=distance, fields={})
            for distance, doc_id in scored[:top_k]
        ]


class LargeClusterAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_streams_joined_pages_and_isolates_perceptual_failure(self) -> None:
        good_path = self.root / "images" / "good.png"
        good_path.parent.mkdir(parents=True)
        Image.new("RGB", (32, 24), (120, 40, 180)).save(good_path)
        rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
            (
                {
                    "doc_id": "good",
                    "sha256": _sha(1),
                    "relative_path": "images/good.png",
                    "tags": ["Raiden"],
                },
                {"identity": "Raiden"},
            ),
            (
                {
                    "doc_id": "missing",
                    "sha256": _sha(2),
                    "relative_path": "images/missing.png",
                },
                None,
            ),
        ]
        state = _PagedState(rows)
        builder_calls: list[str] = []

        def identity_builder(
            entry: Mapping[str, Any], annotation: Mapping[str, Any] | None
        ) -> Iterable[IdentityEvidence]:
            builder_calls.append(str(entry["doc_id"]))
            if annotation is None:
                return ()
            return (IdentityEvidence("character", "Raiden", "manual", 1.0),)

        adapter = LargeClusterAdapter(
            state,
            _Resolver(self.root),
            _Repository(),
            ImageClusteringConfig(
                enable_exact_hash=True,
                enable_perceptual_hash=True,
                enable_semantic=False,
            ),
            identity_evidence_builder=identity_builder,
            read_batch_size=2,
        )
        session = adapter.create_session()
        items = list(session.iter_inputs())

        self.assertEqual([item.doc_id for item in items], ["good", "missing"])
        self.assertEqual(items[0].perceptual_hash, "ffffffffffffffff")
        self.assertIsNone(items[1].perceptual_hash)
        self.assertEqual(items[0].identity_evidence[0].value, "Raiden")
        self.assertEqual(builder_calls, ["good", "missing"])
        self.assertEqual(session.report.failure_count, 1)
        failure = session.report.failures[0].to_dict()
        self.assertEqual(failure["relative_path"], "images/missing.png")
        self.assertNotIn(str(self.root), str(failure))
        self.assertEqual(state.count_calls, 1)
        self.assertLessEqual(state.max_requested, 256)
        self.assertLessEqual(session.report.max_read_batch, 2)
        self.assertEqual(session.external_api_calls, 0)

    def test_failure_preview_is_capped_and_never_contains_absolute_paths(self) -> None:
        rows = [
            (
                {
                    "doc_id": f"bad-{index}",
                    "sha256": "not-a-digest",
                    "relative_path": (
                        f"C:\\Users\\secret\\bad-{index}.png"
                        if index % 2 == 0
                        else f"folder/bad-{index}.png"
                    ),
                },
                None,
            )
            for index in range(250)
        ]
        adapter = LargeClusterAdapter(
            _PagedState(rows),
            _Resolver(self.root),
            _Repository(),
            ImageClusteringConfig(
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
        )
        session = adapter.create_session()

        self.assertEqual(list(session.iter_inputs()), [])
        report = session.report
        self.assertEqual(report.failure_count, 250)
        self.assertEqual(len(report.failures), 200)
        self.assertTrue(report.failure_details_truncated)
        rendered = str(report.to_dict())
        self.assertNotIn("C:/Users/secret", rendered)
        self.assertNotIn("C:\\Users\\secret", rendered)

    def test_semantic_provider_uses_only_existing_vectors_and_bounded_top_k(
        self,
    ) -> None:
        repository = _Repository(
            {
                "a": [1.0, 0.0],
                "b": [0.99, 0.01],
                "c": [0.0, 1.0],
                "d": [-1.0, 0.0],
            }
        )
        progress: list[str] = []
        adapter = LargeClusterAdapter(
            _PagedState([]),
            _Resolver(self.root),
            repository,
            ImageClusteringConfig(
                embedding_dimension=2,
                enable_exact_hash=False,
                enable_perceptual_hash=False,
                enable_semantic=True,
                external_embedding_lookup=True,
            ),
            progress_interval=1,
        )
        session = adapter.create_session(progress=progress.append)
        neighbors = session.semantic_neighbors(ImageClusterInput("a", _sha(1)), 2)

        self.assertEqual(repository.fetch_calls, ["a"])
        self.assertEqual(repository.query_top_ks, [3])
        self.assertEqual([neighbor.doc_id for neighbor in neighbors], ["b", "c"])
        self.assertLessEqual(len(neighbors), 2)
        self.assertEqual(session.semantic_query_count, 1)
        self.assertTrue(any("semantic neighbours 1/0" in line for line in progress))
        self.assertEqual(session.external_api_calls, 0)

    def test_semantic_repository_error_does_not_expose_collection_path(self) -> None:
        class _BrokenRepository(_Repository):
            def fetch_vector(self, doc_id: str) -> list[float] | None:
                del doc_id
                raise OSError(r"C:\Users\secret\collection is unavailable")

        adapter = LargeClusterAdapter(
            _PagedState([]),
            _Resolver(self.root),
            _BrokenRepository(),
            ImageClusteringConfig(
                enable_exact_hash=False,
                enable_perceptual_hash=False,
                enable_semantic=True,
                external_embedding_lookup=True,
            ),
        )
        session = adapter.create_session()

        with self.assertRaisesRegex(
            RuntimeError, "Existing-vector lookup failed"
        ) as caught:
            session.semantic_neighbors(ImageClusterInput("a", _sha(1)), 2)
        self.assertNotIn("Users", str(caught.exception))

    def test_cancellation_stops_between_bounded_entries(self) -> None:
        state = _GeneratedState(1_000)
        checks = 0
        progress: list[str] = []

        def cancel_check() -> bool:
            nonlocal checks
            checks += 1
            return checks > 40

        adapter = LargeClusterAdapter(
            state,
            _Resolver(self.root),
            _Repository(),
            ImageClusteringConfig(
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
            progress_interval=10,
        )
        session = adapter.create_session(
            cancel_check=cancel_check,
            progress=progress.append,
        )

        with self.assertRaises(ClusteringCancelled):
            list(session.iter_inputs())
        self.assertFalse(session.report.completed)
        self.assertLess(session.report.scanned_count, 1_000)
        self.assertTrue(progress)

    def test_run_persists_snapshot_and_reports_zero_model_requests(self) -> None:
        state = _PagedState(
            [
                (
                    {
                        "doc_id": f"doc-{index}",
                        "sha256": _sha(1 if index < 2 else 2),
                        "relative_path": f"images/{index}.png",
                    },
                    None,
                )
                for index in range(3)
            ]
        )
        adapter = LargeClusterAdapter(
            state,
            _Resolver(self.root),
            _Repository(),
            ImageClusteringConfig(
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
            read_batch_size=2,
        )
        store = ClusterOperationStore(self.root / "clusters.sqlite3")
        result = adapter.run(store, "library-test", engine_batch_size=2)

        self.assertEqual(result.cluster.input_count, 3)
        self.assertEqual(result.cluster.clustered_count, 3)
        self.assertEqual(result.cluster.cluster_count, 2)
        self.assertEqual(result.preparation.prepared_count, 3)
        self.assertEqual(result.api_requests, 0)
        payload = result.to_dict()
        self.assertEqual(payload["embedding_api_requests"], 0)
        self.assertFalse(payload["embedding_recomputed"])
        self.assertEqual(
            store.snapshot_version(result.cluster.snapshot_version).item_count,
            3,
        )

    def test_large_engine_selection_keeps_manual_rules_and_small_libraries_compatible(
        self,
    ) -> None:
        self.assertFalse(
            should_use_large_cluster_engine(
                10_000, has_active_manual_rules=False, threshold=10_000
            )
        )
        self.assertTrue(
            should_use_large_cluster_engine(
                10_001, has_active_manual_rules=False, threshold=10_000
            )
        )
        self.assertFalse(
            should_use_large_cluster_engine(
                100_000, has_active_manual_rules=True, threshold=10_000
            )
        )


class LargeClusterAdapterScaleTests(unittest.TestCase):
    def test_one_hundred_thousand_mock_entries_keep_pages_and_memory_bounded(
        self,
    ) -> None:
        state = _GeneratedState(100_000)
        adapter = LargeClusterAdapter(
            state,
            _Resolver(Path("unused")),
            _Repository(),
            ImageClusteringConfig(
                enable_exact_hash=True,
                enable_perceptual_hash=False,
                enable_semantic=False,
            ),
            read_batch_size=256,
            progress_interval=100_000,
        )
        session = adapter.create_session()

        tracemalloc.start()
        try:
            count = sum(1 for _item in session.iter_inputs())
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(count, 100_000)
        self.assertEqual(session.report.prepared_count, 100_000)
        self.assertEqual(state.count_calls, 1)
        self.assertLessEqual(state.max_requested, 256)
        self.assertLessEqual(state.max_selected, 256)
        self.assertLessEqual(state.page_selects, 391)
        self.assertLessEqual(session.report.max_read_batch, 256)
        # A full list of 100k entry dictionaries is tens of MiB.  The adapter
        # should retain only one 256-row page and the current input object.
        self.assertLess(peak, 16 * 1024 * 1024, peak)
        self.assertEqual(adapter.external_api_calls, 0)


if __name__ == "__main__":
    unittest.main()
