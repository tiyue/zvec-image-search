from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from image_vector_service.backend_server import (
    _recommendation_candidate,
    _recommendation_exposure_counts,
    _recommendation_response,
    create_backend_server,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition
from image_vector_service.recommendation_store import (
    RecommendationBatch,
    RecommendationBatchItem,
)
from zvec_host.backend_api import BackendApiClient


class _RecommendationService:
    def __init__(self, library: LibraryDefinition, source_root: Path) -> None:
        self.library = library
        self.owner_thread = threading.get_ident()
        self.call_threads: list[int] = []
        self.drop_first_recommendation_load = False
        self.items: dict[str, dict[str, object]] = {}
        source_root.mkdir(parents=True, exist_ok=True)
        for index in range(24):
            doc_id = f"{index:064x}"
            source = source_root / f"{library.library_id}-{index}.jpg"
            source.write_bytes(f"{library.library_id}-{index}".encode())
            self.items[doc_id] = {
                "doc_id": doc_id,
                "sha256": f"{index + (1 if library.library_id == 'a' else 100):064x}",
                "root_id": "root",
                "relative_path": source.name,
                "name": source.name,
                "content_type": "image/jpeg",
                "size_bytes": source.stat().st_size,
                "mtime_ns": index,
                "width": 2_000,
                "height": 1_200,
                "tags": [library.name],
                "album_id": f"root\0album-{index // 3}",
                "character": None,
                "vector": (1.0, float(index + 1)),
                "source_path": str(source),
            }

    def collect_recommendation_candidates(
        self, *, random_cursor: str, limit_per_pool: int
    ) -> dict[str, object]:
        self.call_threads.append(threading.get_ident())
        model = "space-a" if self.library.library_id == "a" else "space-b"
        return {
            "vector_space": {"model": model, "dimension": 2, "metric": "COSINE"},
            "candidates": list(self.items.values()),
        }

    def load_recommendation_items(self, doc_ids: list[str]) -> list[dict[str, object]]:
        self.call_threads.append(threading.get_ident())
        skipped = None
        if self.drop_first_recommendation_load and doc_ids:
            self.drop_first_recommendation_load = False
            skipped = doc_ids[0]
        return [
            {key: value for key, value in self.items[doc_id].items() if key != "vector"}
            for doc_id in doc_ids
            if doc_id in self.items and doc_id != skipped
        ]

    def close(self) -> None:
        return


class RecommendationBackendTests(unittest.TestCase):
    def test_exposure_reads_are_chunked_below_store_limit(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []

            def exposure_counts(
                self,
                viewer_id: str,
                sha256_values: tuple[str, ...],
            ) -> dict[str, int]:
                self.calls.append((viewer_id, sha256_values))
                return {value: 0 for value in sha256_values}

        store = Store()
        values = [f"sha-{index}" for index in range(1_001)]

        counts = _recommendation_exposure_counts(store, "viewer", values)

        self.assertEqual(len(counts), 1_001)
        self.assertEqual([len(values) for _viewer, values in store.calls], [800, 201])

    def test_same_album_from_different_libraries_has_distinct_album_keys(self) -> None:
        first_library = self._catalog_library("a")
        second_library = self._catalog_library("b")
        source = next(iter(self.services["a"].items.values()))
        shared = {**source, "album_id": "root\0same-album"}

        first = _recommendation_candidate(first_library, shared, 0)
        second = _recommendation_candidate(second_library, shared, 0)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        if first is not None and second is not None:
            self.assertEqual(first.album_id, "a\0root\0same-album")
            self.assertEqual(second.album_id, "b\0root\0same-album")
            self.assertNotEqual(first.album_id, second.album_id)

    def test_complete_batch_with_pool_fallback_reports_quota_degraded(self) -> None:
        slots = (
            ("quality",) * 4 + ("recent",) * 4 + ("low_exposure",) * 4 + ("random",) * 3
        )
        batch = RecommendationBatch(
            batch_id="batch-1",
            viewer_id="viewer-1",
            request_id="request-1",
            items=tuple(
                RecommendationBatchItem(
                    item_id=f"item-{index}",
                    position=index,
                    candidate_id=f"candidate-{index}",
                    library_id="a",
                    doc_id=f"doc-{index}",
                    sha256=f"sha-{index}",
                    slot=slot,
                )
                for index, slot in enumerate(slots)
            ),
        )

        response = _recommendation_response(
            batch,
            [{"item_id": item.item_id} for item in batch.items],
            history_window=0,
            diversity={"applied": False, "reason": "test", "missing_vectors": 0},
        )

        self.assertFalse(response["partial"])
        self.assertTrue(response["quota_degraded"])
        self.assertEqual(response["quota"]["quality"], 4)
        self.assertEqual(response["quota"]["random"], 3)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        libraries = tuple(
            LibraryDefinition(
                library_id,
                f"Library {library_id.upper()}",
                root / f"images-{library_id}",
                root / f"workspace-{library_id}",
            )
            for library_id in ("a", "b")
        )
        catalog = LibraryCatalog("a", libraries, root / "results")
        self.services: dict[str, _RecommendationService] = {}

        def factory(library: LibraryDefinition, _progress: Any, _cancel: Any) -> Any:
            service = _RecommendationService(library, root / "sources")
            self.services[library.library_id] = service
            return service

        self.token = "recommendation-backend-token"
        self.server, self.manager = create_backend_server(
            host="127.0.0.1",
            port=0,
            token=self.token,
            instance_lock_path=root / "backend.lock",
            config=ServiceConfig(
                workspace=root / "control",
                config_home=root / "config",
                results_directory=root / "results",
            ),
            library_catalog=catalog,
            library_service_factory=factory,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        deadline = time.monotonic() + 2
        while len(self.services) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(set(self.services), {"a", "b"})

    def _catalog_library(self, library_id: str) -> LibraryDefinition:
        return next(
            library
            for library in self.manager._catalog.libraries
            if library.library_id == library_id
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temporary.cleanup()

    def request(
        self, path: str, payload: dict[str, object]
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1], timeout=3
        )
        body = json.dumps(payload).encode()
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        decoded = json.loads(response.read().decode())
        connection.close()
        return response.status, decoded

    def test_create_is_idempotent_and_disables_incompatible_vector_spaces(self) -> None:
        status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["count"], 15)
        self.assertFalse(created["partial"])
        self.assertEqual(created["partial_reason"], "")
        self.assertFalse(created["quota_degraded"])
        self.assertFalse(created["diversity"]["applied"])
        self.assertEqual(created["diversity"]["reason"], "incompatible_vector_spaces")
        self.assertTrue(all("vector" not in item for item in created["items"]))
        self.assertTrue(all("source_path" in item for item in created["items"]))
        for library_id, service in self.services.items():
            self.assertTrue(service.call_threads)
            self.assertTrue(
                all(
                    thread_id == service.owner_thread
                    for thread_id in service.call_threads
                ),
                library_id,
            )

        repeated_status, repeated = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-1"},
        )
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated["batch_id"], created["batch_id"])
        self.assertEqual(
            [item["item_id"] for item in repeated["items"]],
            [item["item_id"] for item in created["items"]],
        )

    def test_shown_and_action_events_are_idempotent(self) -> None:
        _status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-events"},
        )
        batch_id = created["batch_id"]
        status, shown = self.request(
            f"/v1/recommendations/{batch_id}/shown",
            {"viewer_id": "desktop-viewer", "event_id": "shown-1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(shown["recorded"])
        _status, shown_again = self.request(
            f"/v1/recommendations/{batch_id}/shown",
            {"viewer_id": "desktop-viewer", "event_id": "shown-2"},
        )
        self.assertFalse(shown_again["recorded"])

        status, action = self.request(
            f"/v1/recommendations/{batch_id}/actions",
            {
                "viewer_id": "desktop-viewer",
                "event_id": "action-1",
                "item_id": created["items"][0]["item_id"],
                "action": "like",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(action["recorded"])
        _status, replay = self.request(
            f"/v1/recommendations/{batch_id}/actions",
            {
                "viewer_id": "desktop-viewer",
                "event_id": "action-1",
                "item_id": created["items"][0]["item_id"],
                "action": "like",
            },
        )
        self.assertFalse(replay["recorded"])

    def test_synchronous_client_calls_recommendation_contract(self) -> None:
        client = BackendApiClient(
            f"http://{self.server.server_address[0]}:{self.server.server_address[1]}",
            self.token,
        )

        created = client.create_recommendations("desktop-viewer", "request-client")
        shown = client.mark_recommendations_shown(
            "desktop-viewer",
            created["batch_id"],
            "shown-client",
        )
        action = client.record_recommendation_action(
            "desktop-viewer",
            created["batch_id"],
            "action-client",
            created["items"][0]["item_id"],
            "open",
            {"surface": "lan"},
        )

        self.assertTrue(shown["recorded"])
        self.assertTrue(action["recorded"])

    def test_missing_selected_source_returns_partial_contiguous_idempotent_batch(
        self,
    ) -> None:
        for service in self.services.values():
            service.drop_first_recommendation_load = True

        status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-missing"},
        )
        repeated_status, repeated = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-missing"},
        )
        batch = self.manager._recommendation_call(
            lambda store: store.batch_for_request("desktop-viewer", "request-missing")
        )

        self.assertEqual(status, 200)
        self.assertTrue(created["partial"])
        self.assertLess(created["count"], 15)
        self.assertTrue(created["quota_degraded"])
        self.assertEqual(sum(created["quota"].values()), created["count"])
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated["batch_id"], created["batch_id"])
        self.assertEqual(repeated["count"], created["count"])
        self.assertIsNotNone(batch)
        if batch is not None:
            self.assertEqual(
                [item.position for item in batch.items],
                list(range(len(batch.items))),
            )


if __name__ == "__main__":
    unittest.main()
